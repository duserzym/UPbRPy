"""Offer laser data once after a fresh session import, without blocking it."""

from pathlib import Path

from PySide6 import QtCore
from shiboken6 import isValid

from GUI.laser_log_dialog import LaserOfferDialog, laser_log_start_path
from UPbRPy.integration import upbdb as db


def is_fresh_bundle(bundle):
    """Recognize UPbDB's empty export project, preserving its import settings."""
    from UPbRPy.project import read_reduction_project

    if bundle.project_file is None:
        return True
    try:
        document = read_reduction_project(bundle.project_file)
    except (OSError, ValueError):
        return False
    return not any((document.get("selections"), document.get("workspace"),
                    document.get("laser_log"), (document.get("origin") or {}).get("run_id"),
                    document["pipeline"].get("completed_stages")))


def _acquisition_starts(session):
    """Prefer manifest instants to the instrument's local wall clock."""
    from UPbRPy.io import resolve_session_manifest

    starts = {a.label: a.start for a in session.qtegra.acquisitions if a.start is not None}
    manifest = resolve_session_manifest(session.directory, session.import_settings.get("session_manifest"))
    if manifest is not None:
        starts.update((row.label, row.started_at) for row in manifest.acquisitions if row.started_at)
    return starts


class LaserOffer(QtCore.QObject):
    """Own one import's background check and discard replies after replacement."""

    def __init__(self, window, *, manual=False):
        super().__init__(window)
        self.window = window
        self.manual = manual
        self.rows = {}
        self.logs = {}
        self.seen = set()
        self.cursor = self.logs_cursor = None
        self.time_zone = None

    def valid(self):
        return (isValid(self.window) and getattr(self.window, "_laser_offer", None) is self
                and self.window.session.loaded and not self.window.session.laser_data_available
                and self.window.session.laser_tie is None)

    def ready(self, callback):
        if not self.valid():
            return
        if self.window._active_worker is not None:
            QtCore.QTimer.singleShot(100, self, lambda: self.ready(callback))
        else:
            callback()

    def start(self):
        if not self.valid():
            return
        session = self.window.session
        if not self.manual and (session.laser_offer_checked or session.laser_offer_declined):
            return
        session.laser_offer_checked = True
        self.source_session = session
        try:
            path = Path(laser_log_start_path(session.directory))
            self.path = str(path) if path.is_file() and path.suffix.lower() == ".lase" else None
        except OSError:
            self.path = None
        origin = session.origin or {}
        self.server, self.session_id = origin.get("server"), origin.get("session_id")
        if self.server and self.session_id and db.is_signed_in(self.server):
            self.account = db.stored_token(self.server)
            self.window.database().run(self.check_database, self.checked,
                title="Check laser data", on_failure=self.check_failed)
        else:
            self.local_fallback()

    def local_fallback(self):
        if self.manual:
            self.ready(self.window._choose_local_laser_log)
        elif self.path:
            self.ready(lambda: self.offer(False))

    def check_database(self):
        detail = db.get_session(self.server, self.session_id)
        if not detail.get("laser_log_count") or detail.get("laser_has_times") is not True:
            return None
        self.time_zone = detail.get("timezone")
        # Validate the whole acquisition clock before offering any timing.
        db.session_laser_tie([], [], _acquisition_starts(self.source_session), time_zone=self.time_zone)
        # Probe the route before offering it: old servers can lack the route.
        page = db.get_session_laser(self.server, self.session_id, limit=1)
        return page if page.logs else None

    def database_valid(self):
        return self.valid() and db.stored_token(self.server) == self.account

    def checked(self, page):
        if not self.valid():
            return
        if page is not None and self.database_valid():
            self.ready(lambda: self.offer(True))
        else:
            self.local_fallback()

    def check_failed(self, error):
        if not self.valid():
            return
        if getattr(error, "status", None) != 404:
            self.window.append_log(f"Laser data check: {error}")
        if isinstance(error, ValueError):
            return
        self.local_fallback()

    def offer(self, database):
        if not self.valid():
            return
        dialog = LaserOfferDialog("tied in UPbDB" if database else Path(self.path).name, self.window)
        self.dialog = dialog
        dialog.finished.connect(lambda _result: self.answered(database))
        dialog.open()

    def answered(self, database):
        if not self.valid():
            return
        if self.dialog.clickedButton() is not self.dialog.import_button:
            self.window.session.laser_offer_declined = True
            self.window.update_project_window_title()
        elif database:
            self.fetch_page()
        else:
            self.ready(lambda: self.window._preview_laser_log(self.path))

    def fetch_page(self):
        if not self.database_valid():
            return
        self.window.database().run(
            lambda: db.get_session_laser(self.server, self.session_id,
                cursor=self.cursor, logs_cursor=self.logs_cursor), self.page_loaded,
            title="Import laser data", on_failure=self.fetch_failed)

    def page_loaded(self, page):
        if not self.database_valid():
            self.rows.clear()
            self.logs.clear()
            return
        self.rows.update((row["acquisition_id"], row) for row in page.rows)
        self.logs.update((log["id"], log) for log in page.logs)
        self.window.statusBar().showMessage(f"Loading laser data: {len(self.rows)} acquisitions")
        if page.next_cursor or page.logs_next_cursor:
            # Keep a finished collection at its final page while the other advances.
            cursors = (page.next_cursor or self.cursor, page.logs_next_cursor or self.logs_cursor)
            if cursors in self.seen:
                self.fetch_failed("The server repeated a laser page cursor.")
                return
            self.seen.add(cursors)
            self.cursor, self.logs_cursor = cursors
            self.fetch_page()
        else:
            self.ready(self.apply)

    def apply(self):
        from GUI.change_guard import copy_session
        from GUI.services import apply_session_laser_tie

        if not self.window.flush_selection_edits():
            QtCore.QTimer.singleShot(100, self, lambda: self.ready(self.apply))
            return
        session = copy_session(self.window.session)

        def prepare():
            try:
                tie = db.session_laser_tie(list(self.rows.values()), list(self.logs.values()),
                                          _acquisition_starts(session), time_zone=self.time_zone)
            except ValueError as exc:
                return session, str(exc)
            if tie.has_laser_times:
                apply_session_laser_tie(session, tie)
            return session, "\n".join(tie.warnings)

        def installed(result):
            updated, message = result
            if self.database_valid():
                if updated.laser_tie is not None:
                    self.window._install_laser_tie_session(updated)
                if message:
                    self.window.append_log(message)
                    self.window.statusBar().showMessage(message)
            self.rows.clear()
            self.logs.clear()

        self.window.run_background("Preparing laser timing and QC", prepare, installed)

    def fetch_failed(self, error):
        self.rows.clear()
        self.logs.clear()
        if self.valid():
            self.window._handle_action_error("Laser log failed", error)
