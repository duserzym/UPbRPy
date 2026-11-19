"""Background GitHub update checks, verified downloads and detached replacement."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import atexit
import weakref

from PySide6 import QtCore, QtWidgets
import shiboken6

from UPbRPy.integration import tls
from GUI.standards_updates import StandardsCheck, check_standards
from GUI.updates import (
    CHANNELS, UpdateState, can_replace, compare_versions, find_asset,
    install_root, installed_version, is_frozen, parse_sha256sums,
    platform_asset_name, select_release, transition,
)


RELEASES_API = "https://api.github.com/repos/duserzym/UPbRPy/releases?per_page=100"
DOWNLOAD_PAGE = "https://github.com/duserzym/UPbRPy/releases/latest"
CHECKOUT_REASON = "Running from a checkout: update with git pull."
REPLACE_REASON = "This copy cannot be replaced: download the new version from GitHub."
CHUNK_SIZE = 1024 * 1024


def previous_path(root: Path) -> Path:
    """The backup sibling retained until the next successful application start."""
    return root.with_name("UPbRPy Studio.previous.app" if root.suffix == ".app"
                          else "UPbRPy Studio.previous")


def cleanup_previous(root) -> None:
    """Delete only the known backup sibling, never the active installation."""
    if root is None:
        return
    previous = previous_path(Path(root))
    if previous != Path(root) and previous.is_dir() and not previous.is_symlink():
        shutil.rmtree(previous)


def _detached_runner(command):
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "cwd": str(Path(command[-1]).parent)}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def _prepare_install(archive: Path, root: Path, runner) -> Path:
    """Stage the bundle on the same volume and launch a script that waits for us."""
    mac = root.suffix == ".app"
    stage = Path(tempfile.mkdtemp(prefix=".upbrpy-install-", dir=root.parent))
    try:
        # Validate paths before either extractor is allowed to write anything.
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (stage / member.filename.replace("\\", "/")).resolve()
                if not target.is_relative_to(stage.resolve()):
                    raise ValueError("Unsafe path in update archive")
                if not mac and (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Unexpected symbolic link in update archive")
            if not mac:
                bundle.extractall(stage)
        if mac:
            subprocess.run(["ditto", "-x", "-k", str(archive), str(stage)], check=True)
        new = stage / ("UPbRPy Studio.app" if mac else "UPbRPy Studio")
        executable = new / ("Contents/MacOS/UPbRPy Studio" if mac else "UPbRPy Studio.exe")
        if not executable.is_file():
            raise ValueError("Update archive does not contain UPbRPy Studio")
        previous = previous_path(root)
        if previous.exists():
            raise ValueError("Previous installation still exists; restart Studio before updating")
        if mac:
            script = stage / "install.sh"
            old_q, new_q, previous_q = map(shlex.quote, map(str, (root, new, previous)))
            content = f"""#!/bin/sh
PID={os.getpid()}
while kill -0 $PID 2>/dev/null; do sleep 1; done
mv {old_q} {previous_q} || exit 1
if ! mv {new_q} {old_q}; then
    mv {previous_q} {old_q}
    exit 1
fi
xattr -dr com.apple.quarantine {old_q}
open -n {old_q}
rm -rf {shlex.quote(str(stage))}
"""
            command = ["/bin/sh", str(script)]
        else:
            script = stage / "install.cmd"

            def quote(path):
                return '"' + str(path).replace("%", "%%") + '"'

            old_q, new_q, previous_q = map(quote, (root, new, previous))
            content = f"""@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
set PID={os.getpid()}
:wait
tasklist /FI "PID eq %PID%" /NH | find " %PID% " >nul
if errorlevel 1 goto replace
ping 127.0.0.1 -n 2 >nul
goto wait
:replace
move {old_q} {previous_q} >nul
if errorlevel 1 exit /b 1
move {new_q} {old_q} >nul
if errorlevel 1 (
    move {previous_q} {old_q} >nul
    exit /b 1
)
cd /d {quote(root.parent)}
start "" /D {old_q} {quote(root / 'UPbRPy Studio.exe')}
rmdir /s /q {quote(stage)}
"""
            command = ["cmd.exe", "/d", "/c", str(script)]
        script.write_text(content, encoding="utf-8", newline="\n" if mac else "\r\n")
        runner(command)
        return script
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


class UpdateWorker(QtCore.QThread):
    """Run one network or staging operation without touching GUI objects."""

    result = QtCore.Signal(object)

    def __init__(self, operation, *, opener, version, channel, info=None,
                 path=None, root=None, runner=None, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.opener = opener
        self.version = version
        self.channel = channel
        self.info = info
        self.path = path
        self.root = root
        self.runner = runner

    def _open(self, url):
        request = urllib.request.Request(url, headers={
            "User-Agent": "UPbRPy-Studio-updater", "Accept": "application/vnd.github+json",
        })
        return self.opener(request, timeout=20)

    def _download(self):
        asset = find_asset(self.info, platform_asset_name(self.info["version"]))
        sums = find_asset(self.info, "SHA256SUMS.txt")
        if asset is None or sums is None:
            raise ValueError("Release is missing the platform archive or SHA256SUMS.txt")
        with self._open(sums["browser_download_url"]) as response:
            expected = parse_sha256sums(response.read().decode("utf-8")).get(asset["name"])
        if expected is None:
            raise ValueError("SHA256SUMS.txt does not list the platform archive")
        folder = Path(tempfile.mkdtemp(prefix="upbrpy-update-"))
        path = folder / asset["name"]
        try:
            digest = hashlib.sha256()
            received = 0
            with self._open(asset["browser_download_url"]) as response, path.open("wb") as output:
                total = int(response.headers.get("Content-Length", 0) or asset.get("size", 0))
                while chunk := response.read(CHUNK_SIZE):
                    if QtCore.QThread.currentThread().isInterruptionRequested():
                        raise RuntimeError("Download cancelled")
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    self.result.emit(("progress", received, total))
            if digest.hexdigest() != expected:
                raise ValueError("Checksum mismatch")
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self.result.emit(("downloaded", path))

    @QtCore.Slot()
    def run(self):
        try:
            if self.operation == "check":
                with self._open(RELEASES_API) as response:
                    releases = json.loads(response.read())
                release = select_release(releases, self.channel)
                if release and compare_versions(release["tag_name"], self.version) > 0:
                    info = {**release, "version": release["tag_name"].removeprefix("v")}
                    self.result.emit(("found", info))
                else:
                    self.result.emit(("no_update",))
            elif self.operation == "download":
                self._download()
            elif self.operation == "install":
                script = _prepare_install(self.path, self.root, self.runner)
                self.result.emit(("installed", script))
        except Exception as exc:
            self.result.emit(("failed", str(exc)))


_RUNNING_THREADS: "weakref.WeakSet[QtCore.QThread]" = weakref.WeakSet()


@atexit.register
def _wait_for_running_threads() -> None:
    """Let checks still running at exit finish; Qt aborts on a live QThread."""
    for thread in list(_RUNNING_THREADS):
        if shiboken6.isValid(thread) and thread.isRunning():
            thread.requestInterruption()
            thread.wait(5000)


class _StandardsWorker(QtCore.QThread):
    result = QtCore.Signal(object)

    def __init__(self, path, checkout, parent):
        super().__init__(parent)
        self.path = path
        self.checkout = checkout

    def run(self):
        self.result.emit(check_standards(self.path, self.checkout))


class Updater(QtCore.QObject):
    """One application-wide updater; previews and injected I/O never need a network."""

    stateChanged = QtCore.Signal(object)
    standardsChanged = QtCore.Signal()

    def __init__(self, parent=None, *, opener=None, runner=None, preview=None,
                 version=None, settings=None):
        super().__init__(parent or QtWidgets.QApplication.instance())
        self.version = version or installed_version()
        self._prefs = settings if settings is not None else QtCore.QSettings("UPbRPy", "Studio")
        self._state = UpdateState()
        self._opener = opener or tls.urlopen
        self._runner = runner or _detached_runner
        self._jobs = []
        self._standards_job = None
        self.standards = StandardsCheck("Standards have not been checked.")
        self._started = False
        self._closing = False
        self._reason = ""
        self._preview = preview or os.environ.get("UPBRPY_UPDATE_PREVIEW")
        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(600)
        self._preview_timer.timeout.connect(self._preview_checked)
        self._initial_timer = QtCore.QTimer(self)
        self._initial_timer.setSingleShot(True)
        self._initial_timer.setInterval(8000)
        self._initial_timer.timeout.connect(self.check)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(6 * 60 * 60 * 1000)
        self._timer.timeout.connect(self.check)
        self._standards_timer = QtCore.QTimer(self)
        self._standards_timer.setInterval(self._initial_timer.interval())
        self._standards_timer.timeout.connect(self._scheduled_standards_check)
        if self._preview:
            if self._preview not in {"idle", "checking", "none", "available", "downloading", "ready", "error", "unsupported"}:
                raise ValueError(f"Unknown update preview: {self._preview}")
            info = {"version": "0.2.0", "body": "Publication export, faster canvas",
                    "html_url": DOWNLOAD_PAGE, "tag_name": "v0.2.0", "assets": []}
            self._state = UpdateState(self._preview, info=info if self._preview in {
                "available", "downloading", "ready", "unsupported"} else None, progress=43, total=100,
                                      message=CHECKOUT_REASON if self._preview == "unsupported"
                                      else "Network error reaching GitHub" if self._preview == "error" else "")
        self._preview_state = self._state
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)

    @property
    def state(self) -> UpdateState:
        return self._state

    @property
    def prefs(self) -> QtCore.QSettings:
        """updates/channel, auto_download, last_check and last_result settings."""
        return self._prefs

    @property
    def channel(self) -> str:
        value = self.prefs.value("updates/channel", "stable", type=str)
        return value if value in CHANNELS else "stable"

    @property
    def auto_download(self) -> bool:
        return self.prefs.value("updates/auto_download", True, type=bool)

    def _emit(self, event):
        self._state = transition(self.state, event)
        self.stateChanged.emit(self.state)

    def _support_reason(self):
        if not is_frozen():
            return CHECKOUT_REASON
        if not can_replace(install_root()):
            return REPLACE_REASON
        return ""

    def start_scheduler(self):
        """Schedule standards checks and app checks supported by this install."""
        if self._started or self._preview:
            return
        self._started = True
        self._standards_timer.start()
        self._reason = self._support_reason()
        if self._reason:
            self._emit(("unsupported", self._reason))
            return
        try:
            cleanup_previous(install_root())
        except OSError:
            # A backup that cannot be removed is reported if installation is requested.
            pass
        self._initial_timer.start()
        self._timer.start()

    @property
    def standards_library_changed(self):
        previous = self.prefs.value("standards/reviewed_digest", "", type=str)
        return bool(previous and self.standards.digest and previous != self.standards.digest)

    @property
    def standards_warning(self):
        return self.standards.warning or self.standards_library_changed

    def acknowledge_standards(self):
        if self.standards.digest:
            self.prefs.setValue("standards/reviewed_digest", self.standards.digest)
            self.standardsChanged.emit()

    def _scheduled_standards_check(self):
        self._standards_timer.setInterval(self._timer.interval())
        self.check_standards()

    def check_standards(self):
        if self._preview or self._standards_job is not None or self._closing:
            return
        from GUI.services import current_standard_archive_path

        default_checkout = Path.home() / "UPbRPy" / "upb-standards"
        checkout = Path(os.environ.get("UPB_STANDARDS_DIR") or default_checkout)
        job = _StandardsWorker(current_standard_archive_path(), checkout.expanduser(), self)
        self._standards_job = job
        job.result.connect(self._standards_result)
        job.finished.connect(self._standards_finished)
        _RUNNING_THREADS.add(job)
        job.start()
        self.standardsChanged.emit()

    @property
    def standards_checking(self):
        return self._standards_job is not None

    @QtCore.Slot(object)
    def _standards_result(self, result):
        self.standards = result
        if result.digest and not self.prefs.value("standards/reviewed_digest", "", type=str):
            self.prefs.setValue("standards/reviewed_digest", result.digest)
        self.standardsChanged.emit()

    @QtCore.Slot()
    def _standards_finished(self):
        self._standards_job.deleteLater()
        self._standards_job = None
        self.standardsChanged.emit()

    def check(self, manual=False):
        if self._closing:
            return
        if manual:
            self.check_standards()
        if self._jobs or self.state.status in {"checking", "downloading", "installing"}:
            return
        if self._preview:
            if manual:
                self._emit(("check_started", True))
                self._preview_timer.start()
            return
        self._reason = self._support_reason()
        if self._reason and not manual:
            self._emit(("unsupported", self._reason))
            return
        before = self.state
        self._emit(("check_started", manual))
        if self.state is not before:
            self._work("check")

    def _preview_checked(self):
        outcome = self._preview_state
        if outcome.status == "idle":
            outcome = UpdateState("none")
        self._state = replace(outcome, manual=True)
        self.stateChanged.emit(self.state)

    def download(self):
        if self._preview or self._jobs or self._closing or self.state.status != "available":
            return
        self._reason = self._support_reason()
        if self._reason:
            self._emit(("unsupported", self._reason))
            return
        self._emit("download_started")
        self._work("download")

    def install(self):
        if self._preview or self._jobs or self._closing or self.state.status != "ready":
            return
        reason = self._support_reason()
        if reason:
            self._emit(("unsupported", reason))
            return
        self._emit("install_started")
        self._work("install")

    def _work(self, operation):
        worker = UpdateWorker(operation, opener=self._opener, version=self.version,
                              channel=self.channel, info=self.state.info, path=self.state.path,
                              root=install_root(), runner=self._runner, parent=self)
        self._jobs.append(worker)
        worker.result.connect(self._result)
        worker.finished.connect(self._job_finished)
        _RUNNING_THREADS.add(worker)
        worker.start()

    @QtCore.Slot(object)
    def _result(self, event):
        if event[0] == "installed":
            QtWidgets.QApplication.quit()
            return
        was_checking = self.state.status == "checking"
        self._state = transition(self.state, event)
        if was_checking and event[0] in {"found", "no_update", "failed"}:
            self.prefs.setValue("updates/last_result", self.state.message or (
                "Version " + self.state.info["version"] + " is available"
                if self.state.info else "Up to date"))
            self.prefs.setValue("updates/last_check", datetime.now(timezone.utc).isoformat())
            if self._reason and event[0] != "failed":
                self._state = transition(self.state, ("unsupported", self._reason))
        self.stateChanged.emit(self.state)

    @QtCore.Slot()
    def _job_finished(self):
        thread = self.sender()
        thread.wait()
        self._jobs.remove(thread)
        thread.deleteLater()
        if not self._closing and self.state.status == "available" and self.auto_download:
            self.download()

    @QtCore.Slot()
    def _shutdown(self):
        self._closing = True
        self._initial_timer.stop()
        self._timer.stop()
        self._standards_timer.stop()
        self._preview_timer.stop()
        for thread in self._jobs:
            thread.requestInterruption()
            thread.quit()
        for thread in self._jobs:
            thread.wait()
        if self._standards_job is not None:
            self._standards_job.wait()
