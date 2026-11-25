"""Confirmed, background laser upload and optional revision-checked tie."""

import hashlib
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from GUI.laser_log_dialog import LaserUploadDialog
from UPbRPy.integration import upbdb as db


def upload_server(window):
    """Use the linked session's server, otherwise Studio's selected server."""
    return (window.session.origin or {}).get("server") or window.database().current_server()


def source_path(window):
    """Locate the original file; a changed nearby file is never substituted."""
    tie = window.session.laser_tie
    known = getattr(window, "_laser_source_paths", {}).get(tie.sha256)
    if known and Path(known).is_file():
        return Path(known)
    folder = Path(window.session.directory)
    folder = folder.parent if folder.is_file() else folder
    for parent in (folder, folder.parent):
        candidate = parent / Path(tie.file_name).name
        if candidate.is_file():
            return candidate
    raise ValueError(f"Cannot find the original {tie.file_name} beside this session. Load the original laser log again.")


def original_bytes(path, sha256):
    """Read a bounded original and refuse a changed file before any upload."""
    with Path(path).open("rb") as handle:
        data = handle.read(db.MAX_LASER_LOG_BYTES + 1)
    if len(data) > db.MAX_LASER_LOG_BYTES:
        raise ValueError("Laser logs must be at most 20 MB.")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"{Path(path).name} has changed since it was loaded. Upload refused; load the original laser log again.")
    return data


def system_zone():
    """Return the system's IANA zone, without using a timezone abbreviation."""
    candidates = [os.environ.get("TZ", "")]
    try:
        candidates.append(str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[-1])
    except OSError:
        pass
    candidates.append(bytes(QtCore.QTimeZone.systemTimeZoneId()).decode())
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except (ValueError, ZoneInfoNotFoundError):
            continue
    return "UTC"


class LaserUpload(QtCore.QObject):
    """Keep one upload bound to the original file, account and open session."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.server = upload_server(window)
        self.directory = window.session.directory
        self.tie = window.session.laser_tie
        self.origin = dict(window.session.origin or {})
        self.token = db.stored_token(self.server)
        self.busy = False

    def valid(self, *, check_account=True):
        return (isValid(self.window) and getattr(self.window, "_laser_upload", None) is self
                and self.window.session.directory == self.directory
                and self.window.session.laser_tie == self.tie
                and dict(self.window.session.origin or {}) == self.origin
                and (not check_account or db.stored_token(self.server) == self.token))

    def run(self, function, done):
        self.busy = True
        self.window._sync_laser_controls()

        def success(result):
            self.busy = False
            if self.valid():
                self.window._sync_laser_controls()
                done(result)

        self.window.database().run(function, success, title="Upload laser log", on_failure=self.failed)

    def start(self):
        self.path = source_path(self.window)
        session = self.window.session

        def prepare():
            size = len(original_bytes(self.path, self.tie.sha256))
            account = db.whoami(self.server)
            zone = session.import_settings.get("time_zone") or session.import_settings.get("timezone")
            if not zone:
                from UPbRPy.io import resolve_session_manifest
                manifest = resolve_session_manifest(self.directory, session.import_settings.get("session_manifest"))
                zones = {log.time_zone for log in manifest.laser_logs if log.time_zone} if manifest else set()
                if len(zones) == 1:
                    zone = zones.pop()
            if not zone and self.origin.get("session_id"):
                zone = db.get_session(self.server, self.origin["session_id"]).get("timezone")
            return size, account, zone or system_zone()

        self.run(prepare, self.confirm_upload)

    def confirm_upload(self, result):
        size, account, zone = result
        self.dialog = LaserUploadDialog(self.path, size, self.server, account, zone, self.window)
        self.dialog.finished.connect(self.upload_confirmed)
        self.dialog.open()

    def upload_confirmed(self, result):
        if result != QtWidgets.QDialog.DialogCode.Accepted or not self.valid():
            return
        lab = self.dialog.lab_combo.currentData()
        zone = self.dialog.zone_edit.text().strip()

        def upload():
            # Snapshot the verified bytes so a concurrent file edit cannot race
            # the client's second read. Keep the original name in multipart.
            data = original_bytes(self.path, self.tie.sha256)
            with tempfile.TemporaryDirectory(prefix="upbrpy-laser-") as scratch:
                snapshot = Path(scratch) / self.path.name
                snapshot.write_bytes(data)
                return db.upload_laser_log(self.server, snapshot, lab_id=lab, time_zone=zone)

        self.run(upload, self.uploaded)

    def uploaded(self, result):
        self.log_id = result["id"]
        heading = "Uploaded" if result["status_code"] == 201 else "Already in UPbDB"
        self.dialog = QtWidgets.QMessageBox(self.window)
        self.dialog.setWindowTitle(heading)
        self.dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.dialog.setText(f"{heading}\nLaser log id: {self.log_id}")
        self.dialog.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        self.tie_button = None
        if self.origin.get("session_id"):
            self.tie_button = self.dialog.addButton("Tie to this session in UPbDB…", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        else:
            self.dialog.setInformativeText("Tie this log in UPb Lab's Laser logs page.")
        self.dialog.buttonClicked.connect(self.result_answered)
        self.dialog.open()

    def result_answered(self, button):
        if self.valid() and self.tie_button is not None and button is self.tie_button:
            self.dialog.close()
            self.preview()

    def preview(self):
        self.run(lambda: db.preview_laser_log_tie(self.server, self.log_id, self.origin["session_id"]), self.previewed)

    def previewed(self, preview):
        self.revision = preview["revision"]
        session = next(s for s in preview["sessions"] if str(s["id"]) == str(self.origin["session_id"]))
        counts = session["counts"]
        self.dialog = QtWidgets.QMessageBox(self.window)
        self.dialog.setWindowTitle("Tie laser log in UPbDB?")
        self.dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.dialog.setText(f"Linked: {counts.get('linked', 0)}    Doubtful: {counts.get('doubtful', 0)}    Unlinked: {counts.get('unlinked', 0)}")
        other = "    ".join(f"{key}: {value}" for key, value in counts.items()
                            if key not in {"linked", "doubtful", "unlinked"})
        warnings = "\n".join(session.get("warnings", []))
        detail = "\n".join(filter(None, (other, warnings)))
        if detail:
            self.dialog.setDetailedText(detail)
        self.dialog.setInformativeText("Save this tie in UPbDB using Position and a 30 s threshold?")
        self.dialog.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel)
        self.dialog.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        self.dialog.finished.connect(self.tie_confirmed)
        self.dialog.open()

    def tie_confirmed(self, result):
        if result != QtWidgets.QMessageBox.StandardButton.Yes or not self.valid():
            return
        self.run(lambda: db.tie_laser_log(self.server, self.log_id, self.origin["session_id"], revision=self.revision), self.tied)

    def tied(self, _result):
        self.window.statusBar().showMessage(f"Laser log {self.log_id} tied to this session in UPbDB.")

    def failed(self, error):
        self.busy = False
        if not self.valid(check_account=False):
            return
        self.window._sync_laser_controls()
        # A 401 forgets the expired token in the client. Still show that error
        # for this session, while discarding replies for a different account.
        if not self.valid() and not isinstance(error, db.AuthenticationRequired):
            return
        if getattr(error, "status", None) == 409 and hasattr(self, "revision"):
            self.dialog = QtWidgets.QMessageBox(self.window)
            self.dialog.setWindowTitle("Laser tie preview is stale")
            self.dialog.setText("The preview is stale (HTTP 409). Refresh it and review the new counts before tying.")
            refresh = self.dialog.addButton("Refresh preview", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            self.dialog.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
            self.dialog.finished.connect(lambda _: self.preview() if self.valid() and self.dialog.clickedButton() is refresh else None)
            self.dialog.open()
        else:
            self.window._handle_action_error("Laser log upload failed", error)
