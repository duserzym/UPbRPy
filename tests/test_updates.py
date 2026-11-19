"""Updater rules and background I/O with no network or real installer execution."""

import hashlib
import io
import json
from pathlib import Path
import shutil
import time
import tomllib
import zipfile

import pytest

from GUI import updates
from GUI.updates import UpdateState, transition


@pytest.mark.parametrize("raw", ["PUBKEY", "Public key invalid", "bad Base64", "decode failed", "not configured"])
def test_friendly_update_configuration_errors(raw):
    assert updates.friendly_update_error(raw) == "Updates are not configured for this build yet."


@pytest.mark.parametrize("raw", ["404", "release JSON", "Could not fetch a valid release", "Not Found", "No assets"])
def test_friendly_update_missing_release(raw):
    assert updates.friendly_update_error(raw) == "No release has been published yet."


@pytest.mark.parametrize("raw", ["error sending request", "DNS", "connect failed", "timed out", "timeout",
                                 "Offline", "NETWORK", "Failed to fetch", "ENOTFOUND", "URLError"])
def test_friendly_update_network_errors(raw):
    assert updates.friendly_update_error(raw) == (
        "Can't reach the update server. Check your internet connection and try again.")


def test_friendly_update_fallback():
    assert updates.friendly_update_error("  Checksum mismatch  ") == "The update check failed: Checksum mismatch"
    assert updates.friendly_update_error("x" * 200) == "The update check failed: " + "x" * 117 + "…"
    assert updates.friendly_update_error("404 public key") == "Updates are not configured for this build yet."


@pytest.mark.parametrize("raw", [
    "URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "unable to get local issuer certificate (_ssl.c:1010)>",
    "SSL: untrusted issuer", "certificate rejected", "CERTIFICATE_VERIFY_FAILED",
])
def test_friendly_update_certificate_errors(raw):
    assert updates.friendly_update_error(raw) == updates.certificate_error_message("GitHub")


def test_friendly_update_urlerror_timeout():
    assert updates.friendly_update_error("URLError: <urlopen error timed out>") == (
        "Can't reach the update server. Check your internet connection and try again.")


@pytest.mark.parametrize("status,text,action,badge,busy", [
    ("idle", "Check for a new version of Studio.", None, False, False),
    ("checking", "Checking for updates…", None, False, True),
    ("none", "Up to date (v0.1.0)", None, False, False),
    ("available", "Update available: v0.2.0\nCurrent version: v0.1.0", "download", True, False),
    ("downloading", "Downloading v0.2.0… 43%", None, False, True),
    ("ready", "v0.2.0 is ready", "restart", True, False),
    ("installing", "Installing and restarting…", None, False, False),
    ("error", "No release has been published yet.", None, False, False),
    ("unsupported", "Update available: v0.2.0\nCurrent version: v0.1.0", "page", True, False),
])
def test_check_button_view(status, text, action, badge, busy):
    state = UpdateState(status, info={"version": "0.2.0"}, message="404", progress=43, total=100)
    view = updates.check_button_view(state, "0.1.0", auto_download=False)
    assert (view.text, view.action, view.badge, view.busy) == (text, action, badge, busy)


def test_check_button_view_preferences_and_unsupported_reason():
    assert updates.check_button_view(UpdateState("available"), "0.1.0").action is None
    view = updates.check_button_view(UpdateState("unsupported", message="Portable build"), "0.1.0")
    assert (view.text, view.action, view.badge) == ("Portable build", None, False)
    assert updates.check_button_view(UpdateState("downloading"), "0.1.0").text.endswith("0%")


@pytest.mark.parametrize("left,right,expected", [
    ("0.2.0", "0.1.0", 1), ("0.10.0", "0.9.0", 1),
    ("v0.2.0", "0.2.0", 0), ("0.2.0b1", "0.2.0", -1),
    ("0.2.0rc1", "0.2.0", -1), ("0.2.0b9", "0.2.0rc1", -1),
    ("0.2.0b10", "0.2.0b2", 1), ("0.2.0rc2", "0.2.0rc1", 1),
    ("1.0.0", "0.99.99", 1), ("0.1.9", "0.2.0b1", -1),
    ("v0.2.0rc1", "0.2.0b1", 1), ("0.2.0", "0.2.0", 0),
])
def test_versions(left, right, expected):
    assert updates.compare_versions(left, right) == expected
    assert updates.compare_versions(right, left) == -expected
    assert (updates.parse_version(left) == updates.parse_version(right)) == (expected == 0)


@pytest.mark.parametrize("text", ["", "0.2", "garbage", "0.2.0post1", "0.2.0b"])
def test_invalid_versions(text):
    with pytest.raises(ValueError):
        updates.parse_version(text)


def test_release_selection():
    releases = [
        {"tag_name": "v0.9.0"}, {"tag_name": "v2.0.0", "draft": True},
        {"tag_name": "v0.11.0b1", "prerelease": True}, {"tag_name": "v0.10.0"},
        {"tag_name": "v0.10.1rc1"}, {"tag_name": "nonsense"},
    ]
    assert updates.select_release(releases, "stable")["tag_name"] == "v0.10.0"
    assert updates.select_release(releases, "beta")["tag_name"] == "v0.11.0b1"
    assert updates.select_release([], "stable") is None
    assert updates.select_release([releases[1], releases[2]], "stable") is None
    with pytest.raises(ValueError):
        updates.select_release(releases, "nightly")


@pytest.mark.parametrize("system,machine,label", [
    ("Darwin", "arm64", "macOS-arm64"), ("Darwin", "aarch64", "macOS-arm64"),
    ("Windows", "AMD64", "Windows-x86_64"), ("Windows", "x86_64", "Windows-x86_64"),
])
def test_assets(system, machine, label):
    name = f"UPbRPy-Studio-0.2.0-{label}.zip"
    assert updates.platform_asset_name("v0.2.0", system, machine) == name
    asset = {"name": name}
    assert updates.find_asset({"assets": [asset]}, name) is asset
    assert updates.find_asset({}, name) is None


def test_checksum_text():
    checksum = "aB" * 32
    assert updates.parse_sha256sums(f"{checksum}  app one.zip\r\n{checksum} *app.zip\ninvalid") == {
        "app one.zip": checksum.lower(), "app.zip": checksum.lower(),
    }


STATUSES = ["idle", "checking", "none", "available", "downloading", "ready",
            "installing", "error", "unsupported"]
EVENTS = [("no_update",), ("found", {"version": "0.2.0"}), ("download_started",),
          ("progress", 43, 100), ("downloaded", "/tmp/update.zip"),
          ("install_started",), ("failed", "Oops")]


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("event", EVENTS, ids=lambda e: e[0])
def test_transition_table(status, event):
    legal = {
        ("checking", "no_update"): "none", ("checking", "found"): "available",
        ("available", "download_started"): "downloading",
        ("downloading", "progress"): "downloading", ("downloading", "downloaded"): "ready",
        ("ready", "install_started"): "installing",
        **{(s, "failed"): "error" for s in ("checking", "downloading", "installing")},
    }
    state = UpdateState(status, info={"version": "0.2.0"})
    result = transition(state, event)
    assert result.status == legal.get((status, event[0]), status)
    if (status, event[0]) not in legal:
        assert result is state
    if status == "downloading" and event[0] == "progress":
        assert (result.progress, result.total) == (43, 100)
    if status == "downloading" and event[0] == "downloaded":
        assert result.path == Path("/tmp/update.zip")


@pytest.mark.parametrize("status", STATUSES)
def test_check_and_unsupported_transitions(status):
    state = UpdateState(status)
    for manual in (False, True):
        result = transition(state, ("check_started", manual))
        blocked = status in {"checking", "downloading", "installing"} or (status == "ready" and not manual)
        assert result.status == (status if blocked else "checking")
        if not blocked:
            assert result.manual is manual
    result = transition(state, ("unsupported", "Reason"))
    assert result.status == ("installing" if status == "installing" else "unsupported")


def test_installed_version_and_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.metadata, "version", lambda name: "0.3.0")
    assert updates.installed_version() == "0.3.0"

    def missing(name):
        raise updates.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(updates.metadata, "version", missing)
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert updates.installed_version() == tomllib.loads(project.read_text())["project"]["version"]
    monkeypatch.setattr(updates.sys, "frozen", False, raising=False)
    assert not updates.is_frozen() and updates.install_root() is None
    monkeypatch.setattr(updates.sys, "frozen", True)
    monkeypatch.setattr(updates.sys, "platform", "darwin")
    root = tmp_path / "UPbRPy Studio.app"
    monkeypatch.setattr(updates.sys, "executable", str(root / "Contents/MacOS/UPbRPy Studio"))
    assert updates.install_root() == root
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates.sys, "executable", str(tmp_path / "Studio.exe"))
    assert updates.install_root() == tmp_path
    assert updates.can_replace(tmp_path)
    monkeypatch.setattr(updates.os, "access", lambda *args: False)
    assert not updates.can_replace(tmp_path) and not updates.can_replace(None)


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from GUI.theme import build_application
    return build_application([], "light")


def wait_for(app, predicate):
    from PySide6 import QtTest
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QtTest.QTest.qWait(5)
    assert predicate(), "Updater did not finish"


@pytest.fixture
def frozen(app, monkeypatch, tmp_path):
    from GUI import updater
    from PySide6 import QtCore
    root = tmp_path / "UPbRPy Studio"
    root.mkdir()
    (root / "UPbRPy Studio.exe").write_bytes(b"old")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "install_root", lambda: root)
    monkeypatch.setattr(updater, "platform_asset_name", lambda v: updates.platform_asset_name(v, "Windows", "AMD64"))
    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat)
    settings.setValue("updates/auto_download", False)
    original = updater.tempfile.mkdtemp
    folders = []

    def mkdtemp(**kwargs):
        kwargs.setdefault("dir", tmp_path)
        path = original(**kwargs)
        folders.append(Path(path))
        return path

    monkeypatch.setattr(updater.tempfile, "mkdtemp", mkdtemp)
    yield updater, root, settings, folders
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)


def canned_opener(*, bad_checksum=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("UPbRPy Studio/UPbRPy Studio.exe", b"new executable")
    payload = buffer.getvalue()
    name = updates.platform_asset_name("0.2.0", "Windows", "AMD64")
    release = {"tag_name": "v0.2.0", "body": "Publication export, faster canvas",
               "assets": [{"name": name, "browser_download_url": "https://example.test/archive", "size": len(payload)},
                          {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.test/sums"}]}
    digest = "0" * 64 if bad_checksum else hashlib.sha256(payload).hexdigest()
    responses = {"archive": payload, "sums": f"{digest}  {name}\n".encode()}
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        assert timeout == 20
        assert request.get_header("User-agent") == "UPbRPy-Studio-updater"
        assert request.get_header("Accept") == "application/vnd.github+json"
        data = json.dumps([release]).encode() if "api.github.com" in request.full_url else responses[request.full_url.rsplit("/", 1)[1]]
        response = io.BytesIO(data)
        response.headers = {"Content-Length": str(len(data))}
        return response

    return opener, payload, calls


def check_finished(app, instance):
    wait_for(app, lambda: not instance._jobs and instance.state.status != "checking")


def test_check_download_and_install(app, frozen, monkeypatch):
    module, root, settings, folders = frozen
    opener, payload, calls = canned_opener()
    commands = []
    quits = []
    from PySide6 import QtWidgets
    monkeypatch.setattr(QtWidgets.QApplication, "quit", lambda: quits.append(True))
    instance = module.Updater(opener=opener, runner=commands.append, version="0.1.0", settings=settings)
    states = []
    instance.stateChanged.connect(lambda state: states.append(state.status))
    instance.check(manual=True)
    check_finished(app, instance)
    assert instance.state.status == "available"
    assert instance.state.info["version"] == "0.2.0"
    assert settings.value("updates/last_check")
    instance.download()
    wait_for(app, lambda: not instance._jobs)
    assert instance.state.status == "ready"
    assert instance.state.path.read_bytes() == payload
    assert "downloading" in states and "ready" in states
    instance.check()  # the scheduler cannot discard a verified download
    assert instance.state.status == "ready" and len(calls) == 3
    instance.install()
    wait_for(app, lambda: not instance._jobs)
    assert instance.state.status == "installing"
    assert quits == [True] and len(commands) == 1
    assert commands[0][:3] == ["cmd.exe", "/d", "/c"]
    script = Path(commands[0][-1]).read_text()
    assert "ping 127.0.0.1" in script and "tasklist" in script
    assert "UPbRPy Studio.previous" in script and 'start ""' in script
    assert (root / "UPbRPy Studio.exe").read_bytes() == b"old"
    assert any((folder / "UPbRPy Studio/UPbRPy Studio.exe").exists() for folder in folders)


def test_wrong_checksum_removes_download(app, frozen):
    module, root, settings, folders = frozen
    opener, _, _ = canned_opener(bad_checksum=True)
    instance = module.Updater(opener=opener, version="0.1.0", settings=settings)
    instance.check(manual=True)
    check_finished(app, instance)
    instance.download()
    wait_for(app, lambda: not instance._jobs)
    assert instance.state.status == "error" and instance.state.message == "Checksum mismatch"
    assert folders and all(not folder.exists() for folder in folders)
    assert (root / "UPbRPy Studio.exe").read_bytes() == b"old"


def test_auto_download_and_manual_recheck(app, frozen):
    module, _, settings, _ = frozen
    settings.setValue("updates/auto_download", True)
    opener, _, calls = canned_opener()
    instance = module.Updater(opener=opener, version="0.1.0", settings=settings)
    instance.check()
    wait_for(app, lambda: not instance._jobs and instance.state.status == "ready")
    instance.check(manual=True)
    assert instance.state.status == "checking"
    wait_for(app, lambda: not instance._jobs and instance.state.status == "ready")
    assert len(calls) == 6


def test_scheduler_support_cleanup_and_intervals(app, frozen, monkeypatch):
    module, root, settings, _ = frozen
    backup = module.previous_path(root)
    backup.mkdir()
    instance = module.Updater(settings=settings)
    instance.start_scheduler()
    assert not backup.exists()
    assert instance._initial_timer.interval() == 8000 and instance._initial_timer.isActive()
    assert instance._timer.interval() == 6 * 60 * 60 * 1000 and instance._timer.isActive()
    instance._shutdown()
    monkeypatch.setattr(module, "is_frozen", lambda: False)
    source = module.Updater(settings=settings)
    source.start_scheduler()
    assert source.state.message == module.CHECKOUT_REASON and not source._timer.isActive()
    monkeypatch.setattr(module, "is_frozen", lambda: True)
    monkeypatch.setattr(module, "can_replace", lambda root: False)
    locked = module.Updater(settings=settings)
    locked.start_scheduler()
    assert locked.state.message == module.REPLACE_REASON and not locked._timer.isActive()


def test_unsupported_manual_check_and_network_error(app, frozen, monkeypatch):
    module, _, settings, _ = frozen
    monkeypatch.setattr(module, "is_frozen", lambda: False)
    opener, _, calls = canned_opener()
    instance = module.Updater(settings=settings, opener=opener, version="0.1.0")
    instance.check(manual=True)
    check_finished(app, instance)
    assert instance.state.status == "unsupported" and instance.state.info["version"] == "0.2.0"
    instance.download()
    instance.install()
    assert len(calls) == 1

    def failing_opener(*args, **kwargs):
        raise OSError("Offline")

    failed = module.Updater(settings=settings, opener=failing_opener)
    failed.check(manual=True)
    check_finished(app, failed)
    assert failed.state.status == "error" and failed.state.message == "Offline"


def test_no_update_and_missing_checksum(app, frozen):
    module, _, settings, _ = frozen
    opener, _, _ = canned_opener()
    instance = module.Updater(settings=settings, opener=opener, version="0.10.0")
    instance.check(manual=True)
    check_finished(app, instance)
    assert instance.state.status == "none"
    assert settings.value("updates/last_result") == "Up to date"
    instance._state = UpdateState("available", info={"version": "0.2.0", "assets": []})
    instance.download()
    wait_for(app, lambda: not instance._jobs)
    assert instance.state.status == "error" and "missing" in instance.state.message


def test_mac_script_and_archive_validation(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from GUI import updater
    root = tmp_path / "UPbRPy Studio.app"
    root.mkdir()
    archive = tmp_path / "mac.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("UPbRPy Studio.app/Contents/MacOS/UPbRPy Studio", b"app")
    extractions = []

    def extract(command, check):
        extractions.append(command)
        with zipfile.ZipFile(command[3]) as bundle:
            bundle.extractall(command[4])

    monkeypatch.setattr(updater.subprocess, "run", extract)
    commands = []
    script = updater._prepare_install(archive, root, commands.append)
    assert extractions[0][:3] == ["ditto", "-x", "-k"]
    assert commands == [["/bin/sh", str(script)]]
    text = script.read_text()
    for expected in ("while kill -0 $PID", "UPbRPy Studio.previous.app", "xattr -dr com.apple.quarantine", "open -n"):
        assert expected in text
    assert root.exists()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside", b"bad")
    with pytest.raises(ValueError, match="Unsafe path"):
        updater._prepare_install(archive, root, commands.append)
    assert len(commands) == 1 and not (tmp_path / "outside").exists()


def test_checksum_script(tmp_path, capsys):
    from scripts.sha256sums import main
    path = tmp_path / "desktop.zip"
    path.write_bytes(b"zip bytes")
    assert main([str(path)]) == 0
    assert capsys.readouterr().out == f"{hashlib.sha256(b'zip bytes').hexdigest()}  desktop.zip\n"


def test_detached_runner_uses_staging_directory(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from GUI import updater
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)))
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    command = ["/bin/sh", str(tmp_path / "install.sh")]
    updater._detached_runner(command)
    assert calls[0][0] == command
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["cwd"] == str(tmp_path)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(updater.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    updater._detached_runner(["cmd.exe", "/d", "/c", str(tmp_path / "install.cmd")])
    assert calls[1][1]["creationflags"] == 520
    assert "start_new_session" not in calls[1][1]
