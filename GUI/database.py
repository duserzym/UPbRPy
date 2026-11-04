"""Database mode of UPbRPy Studio: links, sign-in, open, submit, prefer.

All HTTP goes through :mod:`UPbRPy.integration.laicpmsdb` on Qt's worker
pool; this module only sequences those calls and shows their results. When
the ``[database]`` extra is missing, :func:`database_availability` says why
and every entry point shows that sentence instead of failing.

Settings (``QSettings("UPbRPy", "Studio")``) keep the server addresses used,
never tokens: tokens live in the OS keychain.
"""

from __future__ import annotations

from collections.abc import Callable
import functools
from pathlib import Path
import shutil
import tempfile
from typing import Any
from urllib.parse import urlsplit

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.launch import LaunchTarget
from GUI.workers import FunctionWorker
from UPbRPy.session_bundle import SessionBundle, find_project_for_run


SETTINGS_LAST_SERVER = "database/last_server"
SETTINGS_SERVERS = "database/servers"


@functools.cache
def database_availability() -> tuple[bool, str]:
    """``(available, reason)`` for the ``[database]`` extra (checked once)."""
    from UPbRPy.integration import laicpmsdb

    return laicpmsdb.availability()


def client():
    """The client module (import is cheap; it never imports requests early)."""
    from UPbRPy.integration import laicpmsdb

    return laicpmsdb


class DatabaseLinks(QtCore.QObject):
    """Run database flows in the background and hand bundles to windows.

    ``open_bundle(bundle)`` opens a fetched :class:`SessionBundle`;
    ``open_project(path)`` opens a cached ``.upbr``; ``parent()`` returns the
    widget dialogs attach to; ``sign_in(server, then)`` asks the user to sign
    in and calls ``then()`` once a token is stored (set by the GUI layer).
    """

    def __init__(
        self,
        *,
        open_bundle: Callable[[SessionBundle], None],
        open_project: Callable[[str], None],
        parent_widget: Callable[[], QtWidgets.QWidget | None],
        settings: QtCore.QSettings | None = None,
    ) -> None:
        super().__init__()
        self.open_bundle = open_bundle
        self.open_project = open_project
        self.parent_widget = parent_widget
        self.settings = settings or QtCore.QSettings("UPbRPy", "Studio")
        self.pending_sign_in: tuple[str, Callable[[], None] | None] | None = None
        self.sign_in: Callable[[str, Callable[[], None]], None] | None = None
        self.cache_root: Path | None = None
        self._signals: set[QtCore.QObject] = set()
        self.sign_in = self.prompt_sign_in

    # ----------------------------------------------------------- plumbing
    def run(
        self,
        function: Callable[[], Any],
        on_success: Callable[[Any], None],
        *,
        title: str,
        on_failure: Callable[[Exception | str], None] | None = None,
    ) -> None:
        """Run ``function`` on the worker pool; report errors in a dialog."""
        worker = FunctionWorker(function)
        signals = worker.signals
        self._signals.add(signals)
        failure_box = on_failure or (lambda message: self.show_error(title, message))
        signals.succeeded.connect(on_success)
        signals.failed.connect(lambda message, _trace: failure_box(message))
        signals.finished.connect(lambda: self._signals.discard(signals))
        QtCore.QThreadPool.globalInstance().start(worker)

    def show_error(self, title: str, message: object) -> None:
        QtWidgets.QMessageBox.warning(self.parent_widget(), title, str(message))

    def remember_server(self, server: str) -> None:
        servers = [str(s) for s in (self.settings.value(SETTINGS_SERVERS, [], type=list) or [])]
        if server in servers:
            servers.remove(server)
        self.settings.setValue(SETTINGS_SERVERS, [server, *servers][:10])
        self.settings.setValue(SETTINGS_LAST_SERVER, server)

    def known_servers(self) -> list[str]:
        return [str(s) for s in (self.settings.value(SETTINGS_SERVERS, [], type=list) or [])]

    def require_available(self, title: str) -> bool:
        ok, reason = database_availability()
        if not ok:
            self.show_error(title, reason)
        return ok

    # -------------------------------------------------------------- links
    def handle(self, target: LaunchTarget) -> None:
        """Act on a ``upbr://`` link."""
        if target.kind == "auth_callback":
            self.complete_sign_in(target.code, target.server or None)
        elif target.kind == "session":
            self.open_session(target.server, target.identifier, code=target.code)
        elif target.kind == "run":
            self.open_run(target.server, target.identifier)

    def _authorize(self, server: str, code: str, then: Callable[[], None], title: str) -> None:
        """Exchange ``code`` if given, else use the stored token, else sign in."""
        db = client()
        if code:
            self.run(
                lambda: db.exchange_code(server, code),
                lambda _token: then(),
                title=title,
                on_failure=lambda message: self._sign_in_then(server, then, title, message),
            )
            return
        if db.is_signed_in(server):
            then()
            return
        self._sign_in_then(server, then, title, None)

    def _sign_in_then(self, server: str, then: Callable[[], None], title: str, message) -> None:
        if self.sign_in is None:
            self.show_error(title, message or f"Sign in to {server} first.")
            return
        self.sign_in(server, then)

    def open_session(self, server: str, session_id: str, *, code: str = "") -> None:
        """Fetch a session bundle (exchanging a link's code first) and open it."""
        title = "Open from database"
        if not self.require_available(title):
            return
        db = client()
        try:
            server = db.normalize_server(server)
        except db.DatabaseError as exc:
            self.show_error(title, exc)
            return
        self.remember_server(server)

        def fetch() -> None:
            self.run(
                lambda: db.fetch_session_bundle(server, session_id, self.cache_root),
                self.open_bundle,
                title=title,
                on_failure=lambda message: self._retry_after_sign_in(
                    server, fetch, title, message
                ),
            )

        self._authorize(server, code, fetch, title)

    def _retry_after_sign_in(self, server: str, again: Callable[[], None], title: str, message) -> None:
        if not client().is_signed_in(server) and self.sign_in is not None:
            self.sign_in(server, again)
        else:
            self.show_error(title, message)

    def open_run(self, server: str, run_id: str) -> None:
        """Open the cached project of a run, else re-fetch its session."""
        project = find_project_for_run(run_id, self.cache_root)
        if project is not None:
            self.open_project(str(project))
            return
        title = "Open reduction run"
        if not server:
            self.show_error(
                title,
                f"Run {run_id} is not in the local cache and the link names no server.",
            )
            return
        if not self.require_available(title):
            return
        db = client()
        server = db.normalize_server(server)

        def lookup() -> None:
            self.run(
                lambda: db.get_run(server, run_id),
                lambda run: self.open_session(server, str(run["session_id"])),
                title=title,
            )

        self._authorize(server, "", lookup, title)

    def complete_sign_in(self, code: str, server: str | None = None) -> None:
        """Finish a browser (ORCID) sign-in from ``upbr://auth/callback``."""
        title = "Sign in"
        if not self.require_available(title):
            return
        pending = self.pending_sign_in
        target_server = server or (pending[0] if pending else None) or str(
            self.settings.value(SETTINGS_LAST_SERVER, "", type=str) or ""
        )
        if not target_server:
            self.show_error(title, "Studio was not waiting for a sign-in.")
            return
        then = pending[1] if pending and pending[0] == target_server else None
        self.pending_sign_in = None
        db = client()

        def done(_token) -> None:
            self.remember_server(target_server)
            if then is not None:
                then()

        self.run(lambda: db.exchange_code(target_server, code), done, title=title)


    # ------------------------------------------------------------ sign in
    def prompt_sign_in(self, server: str, then: Callable[[], None] | None = None) -> None:
        """Ask for a password or start ORCID; call ``then`` once signed in."""
        dialog = SignInDialog(self, server, self.parent_widget())
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted and dialog.signed_in:
            self.remember_server(server)
            if then is not None:
                then()
        elif dialog.browser_started:
            self.pending_sign_in = (server, then)

    def sign_out(self, server: str | None = None) -> None:
        title = "Sign out"
        if not self.require_available(title):
            return
        server = server or str(self.settings.value(SETTINGS_LAST_SERVER, "", type=str) or "")
        if not server:
            return
        db = client()
        self.run(
            lambda: db.sign_out(server),
            lambda _r: QtWidgets.QMessageBox.information(
                self.parent_widget(), title, f"Signed out of {server}."
            ),
            title=title,
        )

    # ----------------------------------------------------- open from menu
    def choose_server(self) -> str | None:
        servers = self.known_servers()
        value, ok = QtWidgets.QInputDialog.getItem(
            self.parent_widget(),
            "Open from database",
            "laicpmsdb server address:",
            servers or ["https://"],
            0,
            True,
        )
        if not ok or not value.strip():
            return None
        try:
            return client().normalize_server(value)
        except client().DatabaseError as exc:
            self.show_error("Open from database", exc)
            return None

    def open_from_database(self) -> None:
        """File > Database > Open from Database…: server, sign-in, session."""
        title = "Open from database"
        if not self.require_available(title):
            return
        server = self.choose_server()
        if not server:
            return
        db = client()

        def pick() -> None:
            self.run(
                lambda: db.list_sessions(server),
                lambda sessions: self._pick_session(server, sessions),
                title=title,
                on_failure=lambda message: self._retry_after_sign_in(
                    server, pick, title, message
                ),
            )

        self._authorize(server, "", pick, title)

    def _pick_session(self, server: str, sessions: list[dict]) -> None:
        self.remember_server(server)
        if not sessions:
            self.show_error("Open from database", f"No sessions on {server} are visible to you.")
            return
        dialog = SessionPickerDialog(sessions, server, self.parent_widget())
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted and dialog.session_id:
            self.open_session(server, dialog.session_id)

    # ----------------------------------------------------- submit, prefer
    def submit(self, window) -> None:
        """Export the reduction bundle, post it to the origin server, record the run."""
        title = "Submit to database"
        if not self.require_available(title):
            return
        origin = dict(window.session.origin or {})
        if not window.session.loaded or not origin:
            self.show_error(title, ORIGIN_REQUIRED)
            return
        from GUI.services import ensure_shared_pipeline
        from UPbRPy.pipeline import PipelineStage
        from UPbRPy.project import write_reduction_project
        from UPbRPy.reproducibility import export_reduction_bundle

        try:
            window.apply_pending_project_edits()
            pipeline = ensure_shared_pipeline(window.session, window.workflow_preset)
        except Exception as exc:
            self.show_error(title, exc)
            return
        workspace = window.project_workspace_state()
        resume = window.current_workflow_stage() or PipelineStage.IMPORT
        key = origin.get("session_key") or Path(window.session.directory).parent.name or "reduction"
        scratch = Path(tempfile.mkdtemp(prefix="upbrpy-submit-"))
        folder = scratch / f"{key}_reduction"
        session = window.session

        def export() -> Path:
            export_reduction_bundle(
                pipeline,
                folder,
                include_depth_profile=window.workflow_preset.supports_depth_profiles,
                hash_inputs=True,
                include_concentrations=session.include_export_concentrations,
                include_depth_concentrations=session.include_export_depth_concentrations,
            )
            write_reduction_project(
                pipeline, folder / f"{key}.upbr", resume_stage=resume, workspace=workspace
            )
            return folder

        db = client()
        server, session_id = origin["server"], origin["session_id"]

        def upload(_folder=None) -> None:
            self.run(
                lambda: db.submit_reduction(
                    server, session_id, folder, supersedes=origin.get("run_id")
                ),
                lambda result: self._submitted(window, result, scratch),
                title=title,
                on_failure=lambda message: self._retry_after_sign_in(
                    server, upload, title, message
                ),
            )

        def exported(_folder) -> None:
            window.append_log(f"Reduction bundle for the database written to {folder}.")
            self._authorize(server, "", upload, title)

        window.run_background("Exporting the reduction bundle for the database", export, exported)

    def _submitted(self, window, result, scratch: Path) -> None:
        shutil.rmtree(scratch, ignore_errors=True)
        record_run_id(window, result.run_id)
        counts = ", ".join(f"{k}: {v}" for k, v in result.counts.items()) or "none reported"
        held = result.quarantine_items
        window.append_log(f"Submitted to the database as run {result.run_id} ({counts}).")
        box = QtWidgets.QMessageBox(window)
        box.setIcon(
            QtWidgets.QMessageBox.Icon.Warning if held else QtWidgets.QMessageBox.Icon.Information
        )
        box.setWindowTitle("Submitted to database")
        box.setText(
            f"Run {result.run_id} was created.\n\nCounts: {counts}.\n"
            + (
                f"{len(held)} item(s) were held in quarantine (see details)."
                if held
                else "Nothing was quarantined."
            )
        )
        if held:
            box.setDetailedText(format_quarantine(held))
            for item in held:
                window.append_log(f"Quarantine: {quarantine_line(item)}")
        box.exec()

    def prefer(self, window) -> None:
        """Mark the window's last submitted run as the session's preferred run."""
        title = "Mark as preferred"
        if not self.require_available(title):
            return
        origin = dict(window.session.origin or {})
        if not origin.get("run_id"):
            self.show_error(title, RUN_REQUIRED)
            return
        db = client()
        server, session_id, run_id = origin["server"], origin["session_id"], origin["run_id"]

        def call() -> None:
            self.run(
                lambda: db.prefer_run(server, session_id, run_id),
                lambda _r: self._preferred(window, run_id),
                title=title,
                on_failure=lambda message: self._retry_after_sign_in(server, call, title, message),
            )

        self._authorize(server, "", call, title)

    def _preferred(self, window, run_id: str) -> None:
        window.append_log(f"Run {run_id} is now the preferred run of its session.")
        window.statusBar().showMessage(f"Run {run_id} marked as preferred.")
        QtWidgets.QMessageBox.information(
            window, "Mark as preferred", f"Run {run_id} is now the session's preferred run."
        )


ORIGIN_REQUIRED = (
    "This reduction did not come from a database session. Open it with "
    "File > Database > Open from Database… (or a upbr:// link) to submit it."
)
RUN_REQUIRED = "Submit this reduction to the database first; then mark that run as preferred."


def quarantine_line(item: object) -> str:
    if isinstance(item, dict):
        return ", ".join(f"{k}={v}" for k, v in item.items())
    return str(item)


def format_quarantine(items: list) -> str:
    return "\n".join(quarantine_line(item) for item in items)


def record_run_id(window, run_id: str) -> None:
    """Store ``origin.run_id`` in the session, its pipeline and the saved .upbr."""
    origin = dict(window.session.origin or {})
    origin["run_id"] = str(run_id)
    window.session.origin = origin
    if window.session.pipeline is not None:
        window.session.pipeline.origin = dict(origin)
    if window.project_path is not None and Path(window.project_path).is_file():
        from UPbRPy.project import read_reduction_project, write_project_document

        try:
            document = read_reduction_project(window.project_path)
            document["origin"] = {**dict(document.get("origin") or {}), **origin}
            if int(document.get("format_version", 1)) < 2:
                document["format_version"] = 2
            write_project_document(document, window.project_path)
        except (OSError, ValueError) as exc:
            window.append_log(f"Could not record run {run_id} in {window.project_path}: {exc}")
    window.update_project_window_title()


def origin_summary(origin: dict | None) -> tuple[str, str]:
    """(short text, tooltip) describing a database origin for the window."""
    if not origin:
        return "", ""
    host = urlsplit(origin.get("server", "")).netloc or origin.get("server", "")
    key = origin.get("session_key") or origin.get("session_id", "")[:8]
    text = f"Database: {key} @ {host}"
    if origin.get("run_id"):
        text += f" · run {origin['run_id'][:8]}"
    tooltip = "\n".join(
        f"{name}: {origin[name]}"
        for name in ("server", "session_key", "session_id", "exported_at", "export_sha256", "run_id")
        if origin.get(name)
    )
    return text, tooltip


# ---------------------------------------------------------------- dialogs
class SignInDialog(QtWidgets.QDialog):
    """Password sign-in, or ORCID through the system browser."""

    def __init__(self, links: DatabaseLinks, server: str, parent=None) -> None:
        super().__init__(parent)
        self.links = links
        self.server = server
        self.signed_in = False
        self.browser_started = False
        self.setWindowTitle("Sign in to the database")
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(f"Sign in to <b>{server}</b>")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QtWidgets.QFormLayout()
        self.username_edit = QtWidgets.QLineEdit()
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("Username", self.username_edit)
        form.addRow("Password", self.password_edit)
        layout.addLayout(form)
        self.message_label = QtWidgets.QLabel()
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)
        buttons = QtWidgets.QDialogButtonBox()
        self.sign_in_button = buttons.addButton(
            "Sign in", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.orcid_button = buttons.addButton(
            "Sign in with ORCID…", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole
        )
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        self.sign_in_button.clicked.connect(self.sign_in_with_password)
        self.orcid_button.clicked.connect(self.sign_in_with_orcid)
        self.orcid_button.setToolTip(
            "Opens your browser; Studio finishes the sign-in when the "
            "browser returns to upbr://auth/callback."
        )
        layout.addWidget(buttons)

    def sign_in_with_password(self) -> None:
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if not username or not password:
            self.message_label.setText("Enter your username and password.")
            return
        self.setEnabled(False)
        self.message_label.setText("Signing in…")
        db = client()

        def done(_token) -> None:
            self.signed_in = True
            self.accept()

        def failed(message) -> None:
            self.setEnabled(True)
            self.message_label.setText(str(message))

        self.links.run(
            lambda: db.login(self.server, username, password),
            done,
            title="Sign in",
            on_failure=failed,
        )

    def sign_in_with_orcid(self) -> None:
        url = client().sign_in_url(self.server, "orcid")
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            self.message_label.setText(f"Could not open the browser. Visit {url}")
            return
        self.browser_started = True
        self.reject()


class SessionPickerDialog(QtWidgets.QDialog):
    """Choose one session from ``GET /v1/sessions``."""

    COLUMNS = (
        ("session_key", "Session"),
        ("material", "Material"),
        ("started_at", "Started"),
        ("acquisition_count", "Acquisitions"),
        ("status", "Status"),
    )

    def __init__(self, sessions: list[dict], server: str, parent=None) -> None:
        super().__init__(parent)
        self.session_id: str | None = None
        self.setWindowTitle(f"Sessions on {server}")
        self.resize(640, 420)
        layout = QtWidgets.QVBoxLayout(self)
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by session key or material")
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)
        self.table = QtWidgets.QTableWidget(len(sessions), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([label for _key, label in self.COLUMNS])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        for row, session in enumerate(sessions):
            for column, (key, _label) in enumerate(self.COLUMNS):
                value = session.get(key)
                text = "" if value is None else str(value)
                if key == "started_at":
                    text = text.replace("T", " ")[:16]
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, str(session.get("id", "")))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(lambda _index: self.accept())
        if sessions:
            self.table.selectRow(0)
        layout.addWidget(self.table, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Open
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, c).text().casefold() for c in range(self.table.columnCount())]
            self.table.setRowHidden(row, bool(needle) and not any(needle in c for c in cells))

    def accept(self) -> None:
        rows = [
            index.row()
            for index in self.table.selectionModel().selectedRows()
            if not self.table.isRowHidden(index.row())
        ]
        if not rows:
            return
        self.session_id = self.table.item(rows[0], 0).data(QtCore.Qt.ItemDataRole.UserRole)
        super().accept()


__all__ = [
    "ORIGIN_REQUIRED",
    "RUN_REQUIRED",
    "SignInDialog",
    "SessionPickerDialog",
    "origin_summary",
    "record_run_id",
    "format_quarantine",
    "SETTINGS_LAST_SERVER",
    "SETTINGS_SERVERS",
    "DatabaseLinks",
    "database_availability",
    "client",
]
