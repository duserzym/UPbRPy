"""Database mode of UPbRPy Studio: links, sign-in, open, submit, prefer.

All HTTP goes through :mod:`UPbRPy.integration.upbdb` on Qt's worker
pool; this module only sequences those calls and shows their results. When
the ``[database]`` extra is missing, :func:`database_availability` says why
and every entry point shows that sentence instead of failing.

Settings (``QSettings("UPbRPy", "Studio")``) keep the server addresses used,
never tokens: tokens live in the OS keychain.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import functools
import json
from pathlib import Path
import secrets
import shutil
import tempfile
from typing import Any
from urllib.parse import urlsplit

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.launch import LaunchTarget
from GUI.workers import FunctionWorker
from UPbRPy.session_bundle import SessionBundle, find_project_for_run


SETTINGS_LAST_SERVER = "database/last_server"
SETTINGS_ACCOUNT_NAMES = "database/account_names"
SETTINGS_SERVERS = "database/servers"


@functools.cache
def database_availability() -> tuple[bool, str]:
    """``(available, reason)`` for the ``[database]`` extra (checked once)."""
    from UPbRPy.integration import upbdb

    return upbdb.availability()


def client():
    """The client module (import is cheap; it never imports requests early)."""
    from UPbRPy.integration import upbdb

    return upbdb


class DatabaseLinks(QtCore.QObject):
    """Run database flows in the background and hand bundles to windows.

    ``open_bundle(bundle)`` opens a fetched :class:`SessionBundle`;
    ``open_project(path)`` opens a cached ``.upbr``. Both return the reduction
    widget. ``activate_project(**identity)`` returns an existing reduction,
    selecting its tab and raising its shell, or None. ``parent_widget()``
    returns the widget dialogs attach to; ``sign_in(server, then)`` asks the user to sign
    in and calls ``then()`` once a token is stored (set by the GUI layer).
    """

    # Presentation-only events, keyed by session so other tabs ignore them.
    operation_changed = QtCore.Signal(object, str, str, object)
    # The signed-in account changed (sign-in confirmed, or signed out).
    signed_in_changed = QtCore.Signal()

    def __init__(
        self,
        *,
        open_bundle: Callable[[SessionBundle], Any],
        open_project: Callable[[str], Any],
        parent_widget: Callable[[], QtWidgets.QWidget | None],
        settings: QtCore.QSettings | None = None,
        activate_project: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self.open_bundle = open_bundle
        self.open_project = open_project
        self.activate_project = activate_project
        self.parent_widget = parent_widget
        self.settings = settings or QtCore.QSettings("UPbRPy", "Studio")
        # (server, continuation, state) of a browser sign-in Studio started.
        self.pending_sign_in: tuple[str, Callable[[], None] | None, str] | None = None
        self.sign_in: Callable[[str, Callable[[], None]], None] | None = None
        self.cache_root: Path | None = None
        self._signals: set[QtCore.QObject] = set()
        self._open_contexts: dict[QtCore.QObject, list[Callable[[], None]]] = {}
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

    def _operation_run(self, session, operation, function, on_success, *, title, on_failure):
        """Observe a worker without changing its callbacks or retry policy."""
        self.operation_changed.emit(session, operation, "working", None)

        def done(result):
            self.operation_changed.emit(session, operation, "idle", None)
            on_success(result)

        def failed(message):
            self.operation_changed.emit(session, operation, "error", str(message))
            on_failure(message)

        self.run(function, done, title=title, on_failure=failed)

    def _submit_export(self, session, export):
        try:
            return export()
        except Exception as exc:
            self.operation_changed.emit(session, "submit", "error", str(exc))
            raise

    def remember_server(self, server: str) -> None:
        servers = [str(s) for s in (self.settings.value(SETTINGS_SERVERS, [], type=list) or [])]
        if server in servers:
            servers.remove(server)
        self.settings.setValue(SETTINGS_SERVERS, [server, *servers][:10])
        self.settings.setValue(SETTINGS_LAST_SERVER, server)

    def known_servers(self) -> list[str]:
        return [str(s) for s in (self.settings.value(SETTINGS_SERVERS, [], type=list) or [])]

    # ------------------------------------------------------- who is signed in
    def current_server(self) -> str:
        return str(self.settings.value(SETTINGS_LAST_SERVER, "", type=str) or "")

    def _account_names(self) -> dict:
        raw = self.settings.value(SETTINGS_ACCOUNT_NAMES, "", type=str) or ""
        try:
            names = json.loads(raw) if raw else {}
        except ValueError:
            names = {}
        return names if isinstance(names, dict) else {}

    def _set_account_name(self, server: str, name: str | None) -> None:
        names = self._account_names()
        if name:
            names[server] = name
        else:
            names.pop(server, None)
        self.settings.setValue(SETTINGS_ACCOUNT_NAMES, json.dumps(names))

    def is_signed_in(self, server: str | None = None) -> bool:
        server = server or self.current_server()
        return bool(server) and client().is_signed_in(server)

    def status_text(self) -> str:
        """One line for the Database menu: who is signed in, and where."""
        server = self.current_server()
        if not server:
            return "Not signed in to a database"
        host = server.split("://", 1)[-1].rstrip("/")
        if not self.is_signed_in(server):
            return f"Not signed in · {host}"
        name = self._account_names().get(server)
        return f"Signed in as {name} · {host}" if name else f"Signed in · {host}"

    def sign_in_from_menu(self) -> None:
        """File > Database > Sign In…: choose the server, then UPbDB's sign-in page."""
        title = "Sign in"
        if not self.require_available(title):
            return
        server = self.choose_server(title, "Sign in to which UPbDB server?")
        if not server:
            return
        self.remember_server(server)
        self.prompt_sign_in(server, lambda: self.announce_signed_in(server))

    def announce_signed_in(self, server: str) -> None:
        """Say who Studio is now signed in as (and remember it for the menu)."""
        db = client()

        def shown(account: dict) -> None:
            name = str(account.get("display_name") or account.get("username") or "")
            self._set_account_name(server, name or None)
            self.signed_in_changed.emit()
            QtWidgets.QMessageBox.information(
                self.parent_widget(), "Signed in",
                f"Studio is signed in to {server}" + (f" as {name}." if name else "."),
            )

        self.run(lambda: db.whoami(server), shown, title="Sign in")

    def require_available(self, title: str) -> bool:
        ok, reason = database_availability()
        if not ok:
            self.show_error(title, reason)
        return ok

    # -------------------------------------------------------------- links
    def handle(self, target: LaunchTarget) -> None:
        """Act on a ``upbr://`` link."""
        if target.kind == "auth_callback":
            self.complete_sign_in(target.code, target.server or None, target.state)
        elif target.kind == "session":
            self.open_session(target.server, target.identifier, code=target.code,
                              acquisition=target.acquisition, via=target.via)
        elif target.kind == "run":
            self.open_run(target.server, target.identifier, code=target.code,
                          acquisition=target.acquisition, via=target.via)

    def _activate(self, **identity) -> Any:
        return self.activate_project(**identity) if self.activate_project else None

    def _opened(self, window, *, acquisition: str = "", via: str = "") -> None:
        """Apply opener context after the asynchronous project load finishes."""
        if window is None:
            return

        def apply() -> None:
            if not window.session.loaded:
                return
            if via == "upblab" and window.session.origin:
                window.session.origin["opened_via"] = "upblab"
                if window.session.pipeline is not None:
                    window.session.pipeline.origin = dict(window.session.origin)
            if not acquisition:
                return
            from UPbRPy.session_manifest import find_session_manifest, read_session_manifest

            try:
                source = Path(window.session.directory)
                manifest_path = find_session_manifest(source.parent if source.is_file() else source)
                if manifest_path is None and window.project_path is not None:
                    manifest_path = find_session_manifest(Path(window.project_path).parent)
                manifest = read_session_manifest(manifest_path) if manifest_path else None
                label = next((row.label for row in manifest.acquisitions
                              if row.acquisition_id == acquisition), "") if manifest else ""
                if label in window.session.spot_names:
                    window.spot_tree_filter.clear()
                    window.show_spot_signal(label)
                    return
                message = f"Acquisition {acquisition} is not in this project."
            except (OSError, ValueError) as exc:
                message = f"Could not locate acquisition {acquisition}: {exc}"
            window.append_log(message)
            window.statusBar().showMessage(message)

        worker = window._active_worker
        if worker is not None:
            signals = worker.signals
            if signals not in self._open_contexts:
                self._open_contexts[signals] = []
                signals.finished.connect(self._finish_open, QtCore.Qt.ConnectionType.QueuedConnection)
            self._open_contexts[signals].append(apply)
        else:
            apply()

    @QtCore.Slot()
    def _finish_open(self) -> None:
        # Keep the signals alive until their queued GUI-thread callback runs.
        for apply in self._open_contexts.pop(self.sender(), []):
            apply()

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

    def open_session(
        self, server: str, session_id: str, *, code: str = "",
        acquisition: str = "", via: str = "",
    ) -> None:
        """Fetch a session bundle (exchanging a link's code first) and open it."""
        existing = self._activate(server=server, session_id=session_id)
        if existing is not None:
            self._opened(existing, acquisition=acquisition, via=via)
            return
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
                lambda bundle: self._opened(self.open_bundle(bundle), acquisition=acquisition, via=via),
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

    def open_run(
        self, server: str, run_id: str, *, code: str = "",
        acquisition: str = "", via: str = "",
    ) -> None:
        """Open the cached project of a run, else re-fetch its session."""
        existing = self._activate(server=server, run_id=run_id)
        if existing is not None:
            self._opened(existing, acquisition=acquisition, via=via)
            return
        project = find_project_for_run(run_id, self.cache_root)
        if project is not None:
            from UPbRPy.project import read_reduction_project

            try:
                origin = read_reduction_project(project).get("origin") or {}
            except (OSError, ValueError):
                # Let the normal project loader report a malformed cached file.
                origin = {}
            existing = self._activate(server=server or origin.get("server", ""),
                                      session_id=origin.get("session_id", ""), project_file=str(project))
            if existing is not None:
                self._opened(existing, acquisition=acquisition, via=via)
                return

            def open_cached() -> None:
                self._opened(self.open_project(str(project)), acquisition=acquisition, via=via)

            if code:
                title = "Open reduction run"
                if not self.require_available(title):
                    return
                db = client()
                try:
                    server = db.normalize_server(server or origin.get("server", ""))
                except db.DatabaseError as exc:
                    self.show_error(title, exc)
                    return
                self._authorize(server, code, open_cached, title)
            else:
                open_cached()
            return
        title = "Open reduction run"
        if not server:
            self.show_error(
                title,
                f"This run ({short_id(run_id)}) is not in the local cache and the link "
                "names no server.",
            )
            return
        if not self.require_available(title):
            return
        db = client()
        try:
            server = db.normalize_server(server)
        except db.DatabaseError as exc:
            self.show_error(title, exc)
            return

        def lookup() -> None:
            self.run(
                lambda: db.get_run(server, run_id),
                lambda run: self.open_session(server, str(run["session_id"]),
                                              acquisition=acquisition, via=via),
                title=title,
            )

        self._authorize(server, code, lookup, title)

    def complete_sign_in(self, code: str, server: str | None = None, state: str = "") -> None:
        """Finish a browser sign-in from ``upbr://auth/callback``. A sign-in
        Studio started through UPbDB's page must come back with its state."""
        title = "Sign in"
        if not self.require_available(title):
            return
        pending = self.pending_sign_in
        if pending and pending[2] and pending[2] != state:
            self.show_error(title, "This sign-in link is not the one Studio started; sign in again.")
            return
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
        """Open UPbDB's sign-in page in the browser; call ``then`` once signed in."""
        dialog = SignInDialog(self, server, self.parent_widget())
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted and dialog.signed_in:
            self.remember_server(server)
            if then is not None:
                then()
        elif dialog.browser_started:
            self.pending_sign_in = (server, then, dialog.state)

    def sign_out(self, server: str | None = None) -> None:
        title = "Sign out"
        if not self.require_available(title):
            return
        server = server or self.current_server()
        if not server:
            return
        db = client()

        def done(_r) -> None:
            self._set_account_name(server, None)
            self.signed_in_changed.emit()
            QtWidgets.QMessageBox.information(self.parent_widget(), title, f"Signed out of {server}.")

        self.run(lambda: db.sign_out(server), done, title=title)

    # ----------------------------------------------------- open from menu
    def choose_server(self, title: str = "Open from database",
                      label: str = "UPbDB server address:") -> str | None:
        servers = self.known_servers()
        value, ok = QtWidgets.QInputDialog.getItem(
            self.parent_widget(),
            title,
            label,
            servers or ["https://"],
            0,
            True,
        )
        if not ok or not value.strip():
            return None
        try:
            return client().normalize_server(value)
        except client().DatabaseError as exc:
            self.show_error(title, exc)
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

    def link_session(self, window) -> None:
        """Verify a local project's inputs against a temporary session export."""
        title = "Link to database session"
        if not window.session.loaded or window.session.origin or not self.require_available(title):
            return
        server = self.choose_server()
        if not server:
            return
        db = client()
        session = window.session

        def pick(sessions) -> None:
            if window.session is not session or session.origin:
                return
            self.remember_server(server)
            if not sessions:
                message = f"No sessions on {server} are visible to you."
                self.operation_changed.emit(session, "link", "error", message)
                self.show_error(title, message)
                return
            dialog = SessionPickerDialog(sessions, server, window)
            keys = _session_key_candidates(window)
            for key in keys:
                row = next((i for i, item in enumerate(sessions)
                            if item.get("session_key") == key), None)
                if row is not None:
                    dialog.table.selectRow(row)
                    break
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted or not dialog.session_id:
                return
            session_id = dialog.session_id

            def verify():
                # fetch_session_bundle normally uses a persistent project cache.
                # This export is evidence only and must never become a project.
                with tempfile.TemporaryDirectory(prefix="upbrpy-link-") as scratch:
                    bundle = db.fetch_session_bundle(server, session_id, Path(scratch))
                    if bundle.session_id != session_id:
                        raise ValueError("Cannot link: the export belongs to a different session.")
                    return _verified_session_origin(session, bundle)

            def linked(result) -> None:
                if window.session is not session or session.origin:
                    return
                origin, verification = result
                session.origin = dict(origin)
                if session.pipeline is not None:
                    session.pipeline.origin = dict(origin)
                self.operation_changed.emit(session, "link", "done", verification)
                # Use Studio's normal save path, including pending user edits.
                # An unnamed project stays modified until Save As is chosen.
                window.update_project_window_title()
                if window.project_path is not None:
                    window.save_project()
                label = origin.get("session_key") or origin["session_id"]
                message = f"Linked to database session {label} on {origin['server']}. {verification}"
                window.append_log(message)
                window.statusBar().showMessage(message)
                QtWidgets.QMessageBox.information(
                    window, title, message + (
                        "\n\nSave the project to keep this link."
                        if window.has_unsaved_changes() else "\n\nThe project has been saved."
                    )
                )

            def fetch() -> None:
                self._operation_run(session, "link", verify, linked, title=title, on_failure=lambda message:
                         self._retry_after_sign_in(server, fetch, title, message))

            fetch()

        def listing() -> None:
            self._operation_run(session, "link", lambda: db.list_sessions(server), pick, title=title,
                     on_failure=lambda message:
                     self._retry_after_sign_in(server, listing, title, message))

        self._authorize(server, "", listing, title)

    # ----------------------------------------------------- submit, prefer
    def submit(self, window) -> None:
        """Export the reduction bundle, post it to the origin server, record the run."""
        title = "Submit to database"
        if not self.require_available(title):
            return
        origin = dict(window.session.origin or {})
        if not window.session.loaded or not origin:
            self.operation_changed.emit(window.session, "submit", "error", ORIGIN_REQUIRED)
            self.show_error(title, ORIGIN_REQUIRED)
            return
        label = origin.get("session_key") or origin["session_id"]
        question = (
            f"Submit this reduction to session {label} on {origin['server']} as a new run?"
        )
        if origin.get("run_id"):
            question += (
                "\n\nIt will supersede the run this reduction submitted earlier "
                f"({short_id(origin['run_id'])})."
            )
        if QtWidgets.QMessageBox.question(
            window, title, question,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        from GUI.services import ensure_shared_pipeline
        from UPbRPy.pipeline import PipelineStage
        from UPbRPy.project import write_reduction_project
        from UPbRPy.reproducibility import export_reduction_bundle

        try:
            window.apply_pending_project_edits()
            pipeline = ensure_shared_pipeline(window.session, window.workflow_preset)
        except Exception as exc:
            self.operation_changed.emit(window.session, "submit", "error", str(exc))
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
            self._operation_run(
                session, "submit",
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
            self.operation_changed.emit(session, "submit", "idle", None)
            window.append_log(f"Reduction bundle for the database written to {folder}.")
            self._authorize(server, "", upload, title)

        if getattr(window, "_active_worker", None) is None:
            self.operation_changed.emit(session, "submit", "working", None)
        window.run_background("Exporting the reduction bundle for the database",
                              lambda: self._submit_export(session, export), exported)

    def _submitted(self, window, result, scratch: Path) -> None:
        shutil.rmtree(scratch, ignore_errors=True)
        record_run_id(window, result.run_id)
        self.operation_changed.emit(window.session, "submit", "done", result)
        notify_lab(window, result.run_id)
        counts = ", ".join(f"{k}: {v}" for k, v in result.counts.items()) or "none reported"
        held = result.quarantine_items
        window.append_log(f"Submitted to the database as run {result.run_id} ({counts}).")
        origin = window.session.origin
        label = origin.get("session_key") or origin["session_id"]
        window.statusBar().showMessage(f"Submitted to {label} as a new run.")
        box = QtWidgets.QMessageBox(window)
        box.setIcon(
            QtWidgets.QMessageBox.Icon.Warning if held else QtWidgets.QMessageBox.Icon.Information
        )
        box.setWindowTitle("Submitted to database")
        box.setText(
            f"A new run of {label} was created ({short_id(result.run_id)}).\n\nCounts: {counts}.\n"
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
        if origin.get("opened_via") == "upblab":
            try:
                opened = QtGui.QDesktopServices.openUrl(QtCore.QUrl(f"upbdb://run/{result.run_id}"))
            except Exception as exc:
                window.append_log(f"Could not open the submitted run in UPb Lab: {exc}")
            else:
                if not opened:
                    window.append_log("Could not open the submitted run in UPb Lab.")

    def prefer(self, window) -> None:
        """Mark the window's last submitted run as the session's preferred run."""
        title = "Mark as preferred"
        if not self.require_available(title):
            return
        origin = dict(window.session.origin or {})
        if not origin.get("run_id"):
            self.operation_changed.emit(window.session, "prefer", "error", RUN_REQUIRED)
            self.show_error(title, RUN_REQUIRED)
            return
        server, session_id, run_id = origin["server"], origin["session_id"], origin["run_id"]
        label = origin.get("session_key") or session_id
        if QtWidgets.QMessageBox.question(
            window, title,
            f"Make the run this reduction submitted ({short_id(run_id)}) the preferred run "
            f"of session {label} on {urlsplit(server).netloc or server}?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        db = client()
        session = window.session

        def call() -> None:
            self._operation_run(
                session, "prefer",
                lambda: db.prefer_run(server, session_id, run_id),
                lambda _r: self._preferred(window, run_id),
                title=title,
                on_failure=lambda message: self._retry_after_sign_in(server, call, title, message),
            )

        self._authorize(server, "", call, title)

    def _preferred(self, window, run_id: str) -> None:
        self.operation_changed.emit(window.session, "prefer", "done", run_id)
        window.append_log(f"Run {run_id} is now the preferred run of its session.")
        origin = window.session.origin
        label = origin.get("session_key") or origin["session_id"]
        window.statusBar().showMessage(f"The submitted run is now preferred for session {label}.")
        QtWidgets.QMessageBox.information(
            window, "Mark as preferred",
            f"The run this reduction submitted ({short_id(run_id)}) is now the preferred run "
            f"of session {label}.",
        )


ORIGIN_REQUIRED = (
    "This reduction did not come from a database session. Use "
    "File > Database > Link to Database Session… to link it before submitting."
)
RUN_REQUIRED = "Submit this reduction to the database first; then mark that run as preferred."


def _session_key_candidates(window) -> list[str]:
    """Session key, project stem and raw stems, in matching priority order."""
    from UPbRPy.session_manifest import find_session_manifest, read_session_manifest

    source = Path(window.session.directory)
    manifest = find_session_manifest(source.parent if source.is_file() else source)
    keys = []
    if manifest is not None:
        keys.append(read_session_manifest(manifest).session_key)
    if window.project_path is not None:
        keys.append(Path(window.project_path).stem)
    keys.append(source.stem)
    if window.session.qtegra is not None:
        keys.extend(item.path.stem for item in window.session.qtegra.sources)
    return list(dict.fromkeys(key for key in keys if key))


def _verified_session_origin(session, bundle: SessionBundle) -> tuple[dict[str, str], str]:
    """Compare raw bytes and acquisition labels without changing the project.

    Different export layouts may only support label/count verification. Known
    file pairs must always have equal hashes, including manifest hashes.
    """
    import tomllib

    from GUI.services import raw_input_files
    from UPbRPy.pipeline import sha256_file
    from UPbRPy.project import project_origin, read_reduction_project
    from UPbRPy.qtegra import open_qtegra
    from UPbRPy.session_manifest import SESSION_MANIFEST_FILENAME

    local = session.qtegra or open_qtegra(session.directory)
    remote = open_qtegra(bundle.raw_directory)
    local_files = raw_input_files(local)
    remote_files = raw_input_files(remote)
    local_hashes = {p: sha256_file(p) for p in local_files}
    remote_hashes = {p: sha256_file(p) for p in remote_files}
    manifest_hashes = dict(remote_hashes)
    differences = []

    def compare_labels(actual, expected, where):
        actual, expected = Counter(actual), Counter(expected)
        missing, extra = expected - actual, actual - expected
        if missing:
            differences.append(f"Missing {where} labels: " + ", ".join(
                f"{label} ({count})" for label, count in sorted(missing.items())))
        if extra:
            differences.append(f"Extra {where} labels: " + ", ".join(
                f"{label} ({count})" for label, count in sorted(extra.items())))
        if actual.total() != expected.total():
            differences.append(f"{where.capitalize()} acquisition counts: "
                               f"{actual.total()} local, {expected.total()} database.")

    with (bundle.path / SESSION_MANIFEST_FILENAME).open("rb") as stream:
        manifest = tomllib.load(stream)
    rows = manifest.get("acquisition", [])
    labels = [row["label"] for row in rows] if rows else remote.labels
    compare_labels(local.labels, labels, "project")
    compare_labels(session.spot_names, labels, "loaded project")
    compare_labels(remote.labels, labels, "bundle raw")
    for row in rows:
        for filename_key, hash_key in (("filename", "sha256"), ("spot_filename", "spot_sha256")):
            name, expected = row.get(filename_key), row.get(hash_key)
            if not name:
                continue
            path = bundle.raw_directory / name
            if not path.is_file():
                differences.append(f"Missing database file: {name}")
            elif expected:
                if path not in manifest_hashes:
                    manifest_hashes[path] = sha256_file(path)
                if manifest_hashes[path] != expected.lower():
                    differences.append(f"SHA-256 difference for {name}: database file differs from manifest.")
    for item in local.sources:
        if item.sha256 and local_hashes[item.path] != item.sha256:
            differences.append(f"SHA-256 difference for {item.path.name}: local file changed since loading.")

    # Names are preserved by the server export. Renamed local files can also
    # be paired by identical bytes, or by their format and acquisition labels.
    remaining = set(remote_files)
    unpaired = []

    @functools.cache
    def file_identity(path):
        single = open_qtegra(path)
        return single.format, Counter(single.labels)

    for path in local_files:
        matches = [p for p in remaining if p.name == path.name]
        if len(matches) != 1:
            matches = [p for p in remaining if remote_hashes[p] == local_hashes[path]]
        if len(matches) != 1:
            identity = file_identity(path)
            matches = [p for p in remaining if file_identity(p) == identity]
        if len(matches) == 1:
            other = matches[0]
            remaining.remove(other)
            if local_hashes[path] != remote_hashes[other]:
                differences.append(f"SHA-256 difference for {path.name} (database: {other.name}).")
        else:
            unpaired.append(path)

    fallback = bool(unpaired or remaining)
    if fallback and local.format == remote.format:
        if unpaired:
            differences.append("Extra local files: " + ", ".join(sorted(p.name for p in unpaired)))
        if remaining:
            differences.append("Missing local files: " + ", ".join(sorted(p.name for p in remaining)))
    if differences:
        raise ValueError("Cannot link: raw data differs from the database session.\n\n"
                         + "\n".join(dict.fromkeys(differences)))
    if bundle.project_file is None:
        raise ValueError("Cannot link: the session bundle has no .upbr origin.")
    origin = project_origin(read_reduction_project(bundle.project_file))
    if not origin or origin["session_id"] != bundle.session_id:
        raise ValueError("Cannot link: the bundle's project origin does not identify its session.")
    if fallback:
        result = ("Verified acquisition labels and counts only; raw files could not be compared "
                  "one to one (different export layouts).")
        unmatched = [p.name for p in local_files if local_hashes[p] not in remote_hashes.values()]
        if unmatched:
            result += (" The server will quarantine unmatched input hashes for: "
                       + ", ".join(unmatched) + ".")
            if len(unmatched) == len(local_files):
                result += " Submission will be rejected because no input hash belongs to this session."
    else:
        result = f"Verified SHA-256 for {len(local_files)} raw file(s) and {len(labels)} acquisition labels."
    return origin, result


def quarantine_line(item: object) -> str:
    if isinstance(item, dict):
        return ", ".join(f"{k}={v}" for k, v in item.items())
    return str(item)


def format_quarantine(items: list) -> str:
    return "\n".join(quarantine_line(item) for item in items)


def record_run_id(window, run_id: str) -> None:
    """Store ``origin.run_id`` in the session, its pipeline and the saved .upbr."""
    was_modified = window.has_unsaved_changes()
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
            if not was_modified:
                window._saved_project_fingerprint = window.scientific_project_fingerprint()
        except (OSError, ValueError) as exc:
            window.append_log(f"Could not record run {run_id} in {window.project_path}: {exc}")
    window.update_project_window_title()


def notify_lab(window, run_id: str) -> None:
    """Leave a run-submitted notice for UPb Lab; a failure is only logged."""
    origin = window.session.origin or {}
    try:
        from GUI.lab_inbox import write_run_submitted

        write_run_submitted(origin["session_id"], run_id, origin["server"])
    except Exception as exc:  # noqa: BLE001 - the submit itself succeeded
        window.append_log(f"Could not leave a notice for UPb Lab: {exc}")


def short_id(value: object) -> str:
    """A run or session id as people can read it: "id 1a2b3c4d"."""
    text = str(value or "")
    return f"id {text[:8]}" if text else "no id"


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
    """One button: UPbDB's own sign-in page in the system browser.

    The page offers every sign-in the server has (ORCID, Google, GitHub, a
    password, ORCID sign-up) and returns to ``upbr://auth/callback`` with a
    one-time code and this dialog's ``state``.
    """

    def __init__(self, links: DatabaseLinks, server: str, parent=None) -> None:
        super().__init__(parent)
        self.links = links
        self.server = server
        self.signed_in = False
        self.browser_started = False
        self.state = secrets.token_urlsafe(24)
        self.setWindowTitle("Sign in to the database")
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            f"Sign in to <b>{server}</b> on its page in your browser, with ORCID, "
            "Google, GitHub or a password, whatever the server offers. New here? "
            "Create your account with ORCID on that page."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.message_label = QtWidgets.QLabel()
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)
        buttons = QtWidgets.QDialogButtonBox()
        self.browser_button = buttons.addButton(
            "Sign in…", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        self.browser_button.clicked.connect(self.sign_in_with_browser)
        self.browser_button.setDefault(True)
        self.browser_button.setToolTip(
            "Opens your browser; Studio finishes the sign-in when the "
            "browser returns to upbr://auth/callback."
        )
        layout.addWidget(buttons)

    def sign_in_with_browser(self) -> None:
        url = client().connect_url(self.server, self.state)
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
