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
import math
import random
from pathlib import Path
import secrets
import shutil
import tempfile
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from GUI.launch import LaunchTarget
from GUI.workers import FunctionWorker
from GUI.widgets import IssueBanner
from UPbRPy.session_bundle import (
    SessionBundle, find_project_for_run, holds_reduced_work,
    install_run_project, pick_session_run,
)


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


class _PendingAction(QtCore.QObject):
    """One tab's prepared call, retried only by a GUI-thread timer."""

    export_failed = QtCore.Signal(object)

    def __init__(self, links, window, operation, server):
        super().__init__(links)
        self.links = links
        self.window = window
        self.session = window.session
        self.operation = operation
        self.server = server
        self.key = str(uuid.uuid4())
        self.scratch = None
        self.function = None
        self.on_success = None
        self.in_flight = False
        self.stopped = False
        self.failures = 0
        self.deadline = 0.0
        self.connection_failed = False
        self.authorization = None
        self.on_behalf = False
        self.oversight_offered = False
        self.runner = links.run
        self.export_failed.connect(self._failed, QtCore.Qt.ConnectionType.QueuedConnection)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)
        window.destroyed.connect(self.stop)

    def valid(self):
        return (not self.stopped and isValid(self.window)
                and self.window.session is self.session)

    def execute(self, function, on_success):
        self.function, self.on_success = function, on_success
        self.failures = 0
        self.retry_now()

    def retry_now(self):
        if self.in_flight or not self.valid() or self.function is None:
            return
        self.timer.stop()
        self.deadline = 0.0
        self.in_flight = True
        self.links.operation_changed.emit(self.session, self.operation, "working", None)
        self.runner(self.function, self._done, title=self.operation, on_failure=self._failed)

    def _done(self, result):
        self.in_flight = False
        if not self.valid():
            self.stop()
            return
        self.on_success(result)

    def _failed(self, error):
        self.in_flight = False
        if not self.valid():
            self.stop()
            return
        from UPbRPy.integration import upbdb

        if (isinstance(error, upbdb.OversightConfirmationRequired)
                and self.operation in {"submit", "prefer"} and not self.oversight_offered):
            self.oversight_offered = True
            self.timer.stop()
            # A modal question runs a nested event loop. Retry now must not
            # send another request while consent is still being decided.
            self.in_flight = True
            try:
                confirmed = self.links._confirm_on_behalf(self, error.owner)
            finally:
                self.in_flight = False
            if confirmed and self.valid():
                self.on_behalf = True
                self.retry_now()
            else:
                self.stop()
            return

        self.connection_failed = isinstance(error, upbdb.ConnectionFailed)
        pending = (getattr(error, "status", None) == 409
                   and getattr(error, "code", None) == "idempotency_in_progress")
        if self.connection_failed or pending:
            delay = (getattr(error, "retry_after", None) if pending else None)
            if delay is None:
                delay = min(300, 5 * 2 ** min(self.failures, 6))
                delay = min(300, delay * random.uniform(1, 1.15))
            self.failures += 1
            self.deadline = time.monotonic() + max(0.1, delay)
            self.timer.start()
            self._show_waiting()
        else:
            self.timer.stop()
            self.links.operation_changed.emit(
                self.session, self.operation, "error", _answered_error(self.server, error)
            )

    def _show_waiting(self):
        seconds = max(1, math.ceil(self.deadline - time.monotonic()))
        host = urlsplit(self.server).netloc or self.server
        if self.connection_failed:
            message = f"Server unreachable: {host}. Will retry in {seconds} s."
            queue = f"Queued: will {self.operation} when {host} answers."
        else:
            message = f"{host} answered: 409 idempotency_in_progress. Will retry in {seconds} s."
            queue = "The server is still processing this action."
        self.links.operation_changed.emit(
            self.session, self.operation, "queued",
            f"{message}\n{queue} This queue lives in this open tab only; it is lost on app restart.",
        )

    @QtCore.Slot()
    def _tick(self):
        if not self.valid():
            self.stop()
        elif time.monotonic() >= self.deadline:
            self.retry_now()
        else:
            self._show_waiting()

    def complete(self):
        self.stop(announce=False)

    def stop(self, *_args, announce=True):
        was_stopped = self.stopped
        self.stopped = True
        pending = self.links.pending_sign_in
        if pending and self.authorization is not None and pending[1] is self.authorization:
            self.links.pending_sign_in = (pending[0], None, pending[2])
        self.authorization = None
        self.timer.stop()
        # An in-flight upload may still be reading the bundle. Its callback
        # performs cleanup, without touching a closed tab, when it settles.
        if self.in_flight:
            if announce and not was_stopped:
                self.links.operation_changed.emit(self.session, self.operation, "stopped",
                                                  "Stopped. An in-flight request may already have reached the server.")
            return
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None
        identity = (id(self.window), self.operation)
        if self.links._actions.get(identity) is self:
            self.links._actions.pop(identity)
        if announce and isValid(self.window) and self.window.session is self.session:
            self.links.operation_changed.emit(self.session, self.operation, "stopped", "Stopped.")
        self.function = self.on_success = None
        self.deleteLater()


def _answered_error(server, error):
    """Short server evidence followed by an actionable next step."""
    from UPbRPy.integration import upbdb

    if not isinstance(error, upbdb.DatabaseError):
        return str(error)
    host = urlsplit(server).netloc or server
    if isinstance(error, upbdb.AuthenticationRequired) and error.status is None:
        return f"Sign in to {host}, then press Retry now."
    if isinstance(error, upbdb.AuthenticationRequired):
        next_step = "Sign in again, then press Retry now."
    elif isinstance(error, (upbdb.PermissionDenied, upbdb.NotFound)):
        next_step = "Ask an administrator to check your access and the session."
    elif isinstance(error, upbdb.RequestRejected):
        next_step = "Check the bundle and session; stop this action before preparing a corrected submission."
    else:
        next_step = "Retry now, or ask the database administrator if this continues."
    excerpt = " ".join((error.details or str(error)).split())[:240]
    return f"{host} answered: {error.status or 'unexpected response'} {excerpt}. {next_step}"


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
        self._open_bundle = open_bundle
        self.open_project = open_project
        self.activate_project = activate_project
        self.parent_widget = parent_widget
        self.settings = settings or QtCore.QSettings("UPbRPy", "Studio")
        # (server, continuation, state) of a browser sign-in Studio started.
        self.pending_sign_in: tuple[str, Callable[[], None] | None, str] | None = None
        self.sign_in: Callable[[str, Callable[[], None]], None] | None = None
        self.cache_root: Path | None = None
        self._signals: set[QtCore.QObject] = set()
        self._workers = {}
        self._actions = {}
        self._flow_dialogs = set()
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
        """Run off-thread; pass the original exception to a supplied failure handler.

        The worker's legacy ``failed(message, trace)`` signal stays unchanged.
        """
        worker = FunctionWorker(function)
        signals = worker.signals
        self._signals.add(signals)
        failure_box = on_failure or (lambda message: self.show_error(title, message))
        self._workers[signals] = (worker, on_success, failure_box)
        signals.succeeded.connect(self._worker_done, QtCore.Qt.ConnectionType.QueuedConnection)
        signals.failed.connect(self._worker_failed, QtCore.Qt.ConnectionType.QueuedConnection)
        signals.finished.connect(self._worker_finished, QtCore.Qt.ConnectionType.QueuedConnection)
        QtCore.QThreadPool.globalInstance().start(worker)

    @QtCore.Slot(object)
    def _worker_done(self, result):
        self._workers[self.sender()][1](result)

    @QtCore.Slot(str, str)
    def _worker_failed(self, message, _trace):
        worker, _done, failed = self._workers[self.sender()]
        failed(worker.error if worker.error is not None else message)

    @QtCore.Slot()
    def _worker_finished(self):
        signals = self.sender()
        self._signals.discard(signals)
        self._workers.pop(signals, None)

    def _flow_dialog(self, parent, show):
        """Permit only one database flow dialog per shell, including nested events."""
        parent = parent or self.parent_widget()
        identity = id(parent.window()) if parent is not None else None
        if identity in self._flow_dialogs:
            return None
        self._flow_dialogs.add(identity)
        try:
            return show()
        finally:
            self._flow_dialogs.discard(identity)

    def _busy(self, window):
        identity = id(window.window())
        return (identity in self._flow_dialogs or getattr(window, "_active_worker", None) is not None
                or any(action.window is window for action in self._actions.values()))

    def _new_action(self, window, operation, server):
        action = _PendingAction(self, window, operation, server)
        self._actions[id(window), operation] = action
        self.operation_changed.emit(window.session, operation, "waiting", "Waiting for sign-in. You can stop this action.")
        return action

    def _authorize_action(self, action, start, title):
        def resume():
            if action.valid():
                start()
        action.authorization = resume
        self._authorize(action.server, "", resume, title)

    def retry_operation(self, window, operation: str) -> None:
        """Retry the prepared call immediately, preserving its identity and inputs."""
        action = self._actions.get((id(window), operation))
        if action is not None:
            action.retry_now()

    def _confirm_on_behalf(self, action, owner: str) -> bool:
        if action.operation == "submit":
            question = (
                f"This session belongs to {owner}. Submit this run on their behalf? "
                "It will be recorded as your run on their session."
            )
            title = "Submit on behalf"
        else:
            question = f"Also mark it as the preferred run for {owner}'s session?"
            title = "Mark as preferred on behalf"
        return self._flow_dialog(action.window, lambda: QtWidgets.QMessageBox.question(
            action.window, title, question,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )) == QtWidgets.QMessageBox.StandardButton.Yes

    def stop_operation(self, window, operation: str) -> None:
        """Cancel a tab's queued action and discard its temporary export."""
        action = self._actions.get((id(window), operation))
        if action is not None:
            action.stop()

    def stop_window(self, window) -> None:
        """Stop all database retries when this tab is actually closed."""
        for action in list(self._actions.values()):
            if action.window is window:
                action.stop()

    def _status(self, message):
        parent = self.parent_widget()
        if parent is not None and hasattr(parent, "statusBar"):
            parent.statusBar().showMessage(message)

    def _available_inline(self, window, operation):
        ok, reason = database_availability()
        if not ok:
            self.operation_changed.emit(window.session, operation, "error", reason)
        return ok

    def show_error(self, title: str, message: object) -> None:
        self._flow_dialog(self.parent_widget(), lambda: QtWidgets.QMessageBox.warning(
            self.parent_widget(), title, str(message)
        ))

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
            self._status(f"Studio is signed in to {server}" + (f" as {name}." if name else "."))

        self.run(lambda: db.whoami(server), shown, title="Sign in", on_failure=lambda error: self._status(str(error)))

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
                              acquisition=target.acquisition, via=target.via, fresh=target.fresh)
        elif target.kind == "run":
            self.open_run(target.server, target.identifier, code=target.code,
                          acquisition=target.acquisition, via=target.via)

    def _activate(self, **identity) -> Any:
        return self.activate_project(**identity) if self.activate_project else None

    @staticmethod
    def _bundle_messages(bundle) -> tuple[str, ...]:
        if bundle.superseded is None:
            return ()
        detail = " It holds reduced work." if holds_reduced_work(bundle.superseded) else ""
        return (f"The previous cache folder was moved to {bundle.superseded}.{detail}",)

    def open_bundle(self, bundle: SessionBundle, *, acquisition: str = "", via: str = "",
                    messages: tuple[str, ...] = ()):
        """Open a bundle and report any preserved cache folder after loading."""
        window = self._open_bundle(bundle)
        self._opened(window, acquisition=acquisition, via=via,
                     messages=(*messages, *self._bundle_messages(bundle)))
        return window

    def _opened(self, window, *, acquisition: str = "", via: str = "",
                messages: tuple[str, ...] = ()) -> None:
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
            for message in messages:
                window.append_log(message)
            if messages:
                window.statusBar().showMessage(" ".join(messages))
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
                from UPbRPy.spot_edits import original_spot_label
                name = next((name for name in window.session.spot_names
                             if original_spot_label(window.session.experiment, name) == label), None)
                if name:
                    window.spot_tree_filter.clear()
                    window.show_spot_signal(name)
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
        acquisition: str = "", via: str = "", fresh: bool = False,
    ) -> None:
        """Open a session's chosen complete run, or its raw data when fresh."""
        if fresh:
            existing = self._activate(server=server, session_id=session_id, fresh=True)
        else:
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

        messages = ()

        def opened(bundle) -> None:
            self.open_bundle(bundle, acquisition=acquisition, via=via, messages=messages)

        def fetch() -> None:
            self.run(
                lambda: db.fetch_session_bundle(server, session_id, self.cache_root),
                opened,
                title=title,
                on_failure=lambda message: self._retry_after_sign_in(
                    server, fetch, title, message
                ),
            )

        def selected(result) -> None:
            nonlocal messages
            run, warning = result
            if warning:
                messages = (warning,)
            if run is None:
                fetch()
                return
            run_id = str(run.get("id") or run["run_id"])
            reason = "preferred run" if run.get("is_preferred_for_session") else "newest complete run, none preferred"
            message = (f"Opened run {short_id(run_id)} ({reason}). To start a fresh reduction, use "
                       "File > Database > New Reduction from Session Data.")
            self.open_run(server, run_id, acquisition=acquisition, via=via, _messages=(message,))

        def listing():
            # A listing failure may fall back to opening the raw session data.
            try:
                return pick_session_run(db.list_session_runs(server, session_id)), ""
            except Exception as exc:
                return None, f"Could not list session runs; opened the session data instead: {exc}"

        def start() -> None:
            if fresh:
                fetch()
            else:
                self.run(listing, selected, title=title)

        self._authorize(server, code, start, title)

    def _retry_after_sign_in(self, server: str, again: Callable[[], None], title: str, message) -> None:
        if not client().is_signed_in(server) and self.sign_in is not None:
            self.sign_in(server, again)
        else:
            self.show_error(title, message)

    def open_run(
        self, server: str, run_id: str, *, code: str = "",
        acquisition: str = "", via: str = "", _messages: tuple[str, ...] = (),
    ) -> None:
        """Activate a run, reopen its cached project, or download its project."""
        existing = self._activate(server=server, run_id=run_id)
        if existing is not None:
            self._opened(existing, acquisition=acquisition, via=via, messages=_messages)
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
                                      project_file=str(project))
            if existing is not None:
                self._opened(existing, acquisition=acquisition, via=via, messages=_messages)
                return

            def open_cached() -> None:
                self._opened(self.open_project(str(project)), acquisition=acquisition, via=via,
                             messages=_messages)

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
        self.remember_server(server)

        def downloaded(result) -> None:
            bundle, project = result
            if project is None:
                messages = (f"Run {short_id(run_id)} has no saved project on the server; "
                            "opened the session's raw data instead.",)
                self.open_bundle(bundle, acquisition=acquisition, via=via,
                                 messages=(*_messages, *messages))
                return
            window = self.open_project(str(project.path))
            messages = (*project.notes, *self._bundle_messages(bundle))
            self._opened(window, acquisition=acquisition, via=via,
                         messages=(*_messages, *messages))

        def fetch(run) -> None:
            session_id = str(run["session_id"])
            # Older client adapters can still open session data.
            if not hasattr(db, "fetch_run_project"):
                self.open_session(server, session_id, acquisition=acquisition, via=via, fresh=True)
                return

            def download():
                bundle = db.fetch_session_bundle(server, session_id, self.cache_root)
                try:
                    data = db.fetch_run_project(server, run_id)
                except db.DatabaseError as exc:
                    if exc.status != 404:
                        raise
                    return bundle, None
                project = install_run_project(
                    bundle.path, run_id, data, server=server, session_id=session_id,
                    session_key=bundle.session_key, export_sha256=bundle.export_sha256,
                )
                return bundle, project

            self.run(download, downloaded, title=title, on_failure=lambda message:
                     self._retry_after_sign_in(server, lookup, title, message))

        def lookup() -> None:
            self.run(
                lambda: db.get_run(server, run_id),
                fetch,
                title=title,
                on_failure=lambda message: self._retry_after_sign_in(server, lookup, title, message),
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
        if self._flow_dialog(self.parent_widget(), dialog.exec) == QtWidgets.QDialog.DialogCode.Accepted and dialog.signed_in:
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
            self._status(f"Signed out of {server}.")

        self.run(lambda: db.sign_out(server), done, title=title, on_failure=lambda error: self._status(str(error)))

    # ----------------------------------------------------- open from menu
    def choose_server(self, title: str = "Open from database",
                      label: str = "UPbDB server address:") -> str | None:
        """Pick the UPbDB server: saved servers with whether they answer, or a new address."""
        dialog = ServerPickerDialog(
            self.known_servers(), self.current_server(), title, label, self.parent_widget()
        )
        if self._flow_dialog(self.parent_widget(), dialog.exec) != QtWidgets.QDialog.DialogCode.Accepted or not dialog.server:
            return None
        if dialog.forgotten:
            self.forget_servers(dialog.forgotten)
        try:
            return client().normalize_server(dialog.server)
        except client().DatabaseError as exc:
            self.show_error(title, exc)
            return None

    def forget_servers(self, servers: list[str]) -> None:
        kept = [s for s in self.known_servers() if s not in servers]
        self.settings.setValue(SETTINGS_SERVERS, kept)
        if self.current_server() in servers:
            self.settings.setValue(SETTINGS_LAST_SERVER, kept[0] if kept else "")

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
        dialog.set_next_cursor(getattr(sessions, "next_cursor", None))
        active = True

        def more():
            cursor = dialog.next_cursor
            if not cursor:
                return
            dialog.more_button.setEnabled(False)
            dialog.table.setEnabled(False)

            def done(page):
                if active:
                    dialog.append_sessions(page)
                    dialog.set_next_cursor(getattr(page, "next_cursor", None))
                    dialog.table.setEnabled(True)

            def failed(error):
                if active:
                    dialog.table.setEnabled(True)
                    dialog.more_button.setEnabled(True)
                    dialog.issue.show_issue(str(error), stage="Database")

            self.run(lambda: client().list_sessions(server, cursor=cursor), done,
                     title="List sessions", on_failure=failed)

        dialog.more_button.clicked.connect(more)
        selected = self._flow_dialog(self.parent_widget(), dialog.exec)
        active = False
        if selected == QtWidgets.QDialog.DialogCode.Accepted and dialog.session_id:
            self.open_session(server, dialog.session_id)

    def link_session(self, window) -> None:
        """Verify local inputs; keep the chosen session across unreachable retries."""
        title = "Link to database session"
        session = window.session
        if (not session.loaded or session.origin or self._busy(window)
                or not self._available_inline(window, "link")):
            return
        server = self.choose_server()
        if not server:
            return
        db = client()
        action = self._new_action(window, "link", server)

        def pick(sessions) -> None:
            if not action.valid():
                action.stop()
                return
            if id(window.window()) in self._flow_dialogs:
                # Keep the received page while an explicit flow dialog is open.
                # The QObject context cancels this callback on Stop/close.
                QtCore.QTimer.singleShot(250, action, lambda: pick(sessions))
                return
            self.remember_server(server)
            if not sessions:
                action.complete()
                self.operation_changed.emit(session, "link", "error", f"No sessions on {server} are visible to you.")
                return
            dialog = SessionPickerDialog(sessions, server, window)
            keys = _session_key_candidates(window)
            for key in keys:
                row = next((i for i, item in enumerate(sessions) if item.get("session_key") == key), None)
                if row is not None:
                    dialog.table.selectRow(row)
                    break
            cursor = getattr(sessions, "next_cursor", None)
            dialog.set_next_cursor(cursor)

            def more():
                cursor = dialog.next_cursor
                if not cursor:
                    return
                if action.timer.isActive() or (dialog.more_button.text() == "Retry now"):
                    action.retry_now()
                    return
                dialog.more_button.setEnabled(False)

                def appended(page):
                    dialog.append_sessions(page)
                    dialog.set_next_cursor(getattr(page, "next_cursor", None))

                action.execute(lambda: db.list_sessions(server, cursor=cursor), appended)

            def page_status(changed_session, operation, state, result):
                if changed_session is session and operation == "link":
                    dialog.table.setEnabled(state != "working")
                    if state in {"queued", "error"}:
                        dialog.issue.show_issue(str(result), stage="Database", details=str(result))
                        dialog.more_button.setText("Retry now")
                        dialog.more_button.setEnabled(True)
            dialog.more_button.clicked.connect(more)
            self.operation_changed.connect(page_status)
            try:
                selected = self._flow_dialog(window, dialog.exec)
            finally:
                self.operation_changed.disconnect(page_status)
            if selected != QtWidgets.QDialog.DialogCode.Accepted or not dialog.session_id:
                action.stop()
                return
            # Cancel any pending list-page retry before starting verification.
            action.timer.stop()
            if action.in_flight:
                # Selection stays disabled while a page is loading.
                action.stop()
                return
            session_id = dialog.session_id

            def verify():
                with tempfile.TemporaryDirectory(prefix="upbrpy-link-") as scratch:
                    bundle = db.fetch_session_bundle(server, session_id, Path(scratch))
                    if bundle.session_id != session_id:
                        raise ValueError("Cannot link: the export belongs to a different session.")
                    return _verified_session_origin(session, bundle)

            def linked(result) -> None:
                origin, verification = result
                session.origin = dict(origin)
                if session.pipeline is not None:
                    session.pipeline.origin = dict(origin)
                action.complete()
                window.update_project_window_title()
                if window.project_path is not None:
                    window.save_project()
                label = origin.get("session_key") or origin["session_id"]
                if window.has_unsaved_changes():
                    verification += " Save the project to keep this link."
                message = f"Linked to database session {label} on {origin['server']}. {verification}"
                window.append_log(message)
                window.statusBar().showMessage(message)
                self.operation_changed.emit(session, "link", "done", verification)

            action.execute(verify, linked)

        def listing():
            if action.valid():
                action.execute(lambda: db.list_sessions(server), pick)

        action.function, action.on_success = lambda: db.list_sessions(server), pick
        self._authorize_action(action, listing, title)

    # ----------------------------------------------------- submit, prefer
    def submit(self, window) -> None:
        """Export the reduction bundle, post it to the origin server, record the run."""
        title = "Submit to database"
        if self._busy(window) or not self._available_inline(window, "submit"):
            return
        origin = dict(window.session.origin or {})
        if not window.session.loaded or not origin:
            self.operation_changed.emit(window.session, "submit", "error", ORIGIN_REQUIRED)
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
        if self._flow_dialog(window, lambda: QtWidgets.QMessageBox.question(
            window, title, question,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        if getattr(window, "_active_worker", None) is not None:
            self.operation_changed.emit(window.session, "submit", "error", "Wait for the running reduction before submitting.")
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
            return
        workspace = window.project_workspace_state()
        resume = window.current_workflow_stage() or PipelineStage.IMPORT
        key = origin.get("session_key") or Path(window.session.directory).parent.name or "reduction"
        scratch = Path(tempfile.mkdtemp(prefix="upbrpy-submit-"))
        folder = scratch / f"{key}_reduction"
        session = window.session

        def export() -> Path:
            from GUI.services import pipeline_for_database_submission
            submission_pipeline = pipeline_for_database_submission(session, window.workflow_preset)
            export_reduction_bundle(
                submission_pipeline,
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

        action = self._new_action(window, "submit", server)
        action.scratch = scratch

        def upload():
            if action.valid():
                action.execute(
                    lambda: db.submit_reduction(
                        server, session_id, folder, supersedes=origin.get("run_id"),
                        idempotency_key=action.key,
                        **({"on_behalf": True} if action.on_behalf else {}),
                    ),
                    submitted,
                )

        def submitted(result):
            action.complete()
            self._submitted(window, result, scratch)

        def exported(_folder):
            action.runner = self.run
            window.append_log(f"Reduction bundle for the database written to {folder}.")
            self.operation_changed.emit(session, "submit", "waiting", "Bundle prepared. Waiting for sign-in; Retry now continues without exporting again.")
            action.function, action.on_success = (
                lambda: db.submit_reduction(server, session_id, folder, supersedes=origin.get("run_id"),
                                             idempotency_key=action.key,
                                             **({"on_behalf": True} if action.on_behalf else {})), submitted)
            self._authorize_action(action, upload, title)

        if hasattr(window, "run_background"):
            # Keep the scientific worker's edit guards while preparing the
            # bundle. Its Brief 2 issue banner and our row both report errors.
            def export_runner(function, done, *, title, on_failure):
                def guarded_export():
                    try:
                        return function()
                    except Exception as exc:
                        action.export_failed.emit(exc)
                        raise
                window.run_background("Exporting the reduction bundle for the database", guarded_export, done)
            action.runner = export_runner
        action.execute(export, exported)

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
        for item in held:
            window.append_log(f"Quarantine: {quarantine_line(item)}")
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
        if self._busy(window) or not self._available_inline(window, "prefer"):
            return
        origin = dict(window.session.origin or {})
        if not origin.get("run_id"):
            self.operation_changed.emit(window.session, "prefer", "error", RUN_REQUIRED)
            return
        server, session_id, run_id = origin["server"], origin["session_id"], origin["run_id"]
        label = origin.get("session_key") or session_id
        if self._flow_dialog(window, lambda: QtWidgets.QMessageBox.question(
            window, title,
            f"Make the run this reduction submitted ({short_id(run_id)}) the preferred run "
            f"of session {label} on {urlsplit(server).netloc or server}?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        db = client()
        session = window.session

        action = self._new_action(window, "prefer", server)

        def done(_result):
            action.complete()
            self._preferred(window, run_id)

        def call():
            if action.valid():
                action.execute(lambda: db.prefer_run(server, session_id, run_id,
                                                     idempotency_key=action.key,
                                                     **({"on_behalf": True} if action.on_behalf else {})), done)

        action.function = lambda: db.prefer_run(
            server, session_id, run_id, idempotency_key=action.key,
            **({"on_behalf": True} if action.on_behalf else {}))
        action.on_success = done
        self._authorize_action(action, call, title)

    def _preferred(self, window, run_id: str) -> None:
        self.operation_changed.emit(window.session, "prefer", "done", run_id)
        window.append_log(f"Run {run_id} is now the preferred run of its session.")
        origin = window.session.origin
        label = origin.get("session_key") or origin["session_id"]
        window.statusBar().showMessage(f"The submitted run is now preferred for session {label}.")



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
    from UPbRPy.spot_edits import original_spot_label
    compare_labels([original_spot_label(session.experiment, name) for name in session.spot_names]
                   + list(session.experiment.attrs.get("removed_spots", [])), labels, "loaded project")
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
        self.setWindowTitle("Sign in to UPbDB")
        self.setMinimumWidth(440)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        layout.addWidget(_heading("Sign in to UPbDB"))
        where = QtWidgets.QLabel(server)
        where.setObjectName("SecondaryText")
        where.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(where)
        intro = QtWidgets.QLabel(
            "Your browser opens UPbDB's sign-in page. Sign in there with ORCID, "
            "Google, GitHub or a password, then click <b>Sign UPbRPy Studio in</b>; "
            "the browser hands you back to Studio.<br><br>New here? Create your "
            "account with ORCID on that page."
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
        self.browser_button.setProperty("accent", True)
        cancel = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        cancel.setProperty("tertiary", True)
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


def _heading(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    font = label.font()
    font.setBold(True)
    font.setPointSizeF(font.pointSizeF() * 1.35)
    label.setFont(font)
    return label


def _server_answers(server: str, timeout: float = 3.0) -> bool:
    """True when ``<server>/v1/health`` answers (any HTTP reply counts as alive)."""
    import urllib.error
    from UPbRPy.integration import tls

    try:
        with tls.urlopen(server.rstrip("/") + "/v1/health", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


class ServerPickerDialog(QtWidgets.QDialog):
    """Choose a UPbDB server: saved servers with a live "answering" check, or a new address."""

    def __init__(self, servers: list[str], current: str, title: str, prompt: str,
                 parent=None, *, check=_server_answers) -> None:
        super().__init__(parent)
        self.server: str | None = None
        self.forgotten: list[str] = []
        self._check = check
        self._status: dict[str, bool | None] = {s: None for s in servers}
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        layout.addWidget(_heading("Choose the UPbDB server"))
        intro = QtWidgets.QLabel(
            "The lab's database runs on the lab desktop. Its address looks like "
            "<b>https://upb-host.&lt;tailnet&gt;.ts.net</b> and works while <b>Tailscale</b> is "
            "connected on this computer. Ask the lab manager if you do not have it."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("ServerList")
        self.list.setSpacing(2)
        self.list.setUniformItemSizes(True)
        self.list.itemSelectionChanged.connect(self._selected)
        self.list.itemDoubleClicked.connect(lambda _item: self._accept())
        saved = QtWidgets.QLabel(prompt if servers else "No saved servers yet.")
        saved.setObjectName("SectionTitle")
        layout.addWidget(saved)
        layout.addWidget(self.list)
        self.list.setVisible(bool(servers))
        self.forget_button = QtWidgets.QPushButton("Forget")
        self.forget_button.setProperty("tertiary", True)
        self.forget_button.setToolTip("Remove the selected server from this list.")
        self.forget_button.clicked.connect(self._forget)
        self.forget_button.setVisible(bool(servers))
        forget_row = QtWidgets.QHBoxLayout()
        forget_row.addStretch(1)
        forget_row.addWidget(self.forget_button)
        layout.addLayout(forget_row)

        other = QtWidgets.QLabel("Another address")
        other.setObjectName("SectionTitle")
        layout.addWidget(other)
        self.address = QtWidgets.QLineEdit()
        self.address.setPlaceholderText("https://upb-host.<tailnet>.ts.net")
        self.address.textEdited.connect(lambda _t: self.list.clearSelection())
        self.address.returnPressed.connect(self._accept)
        layout.addWidget(self.address)

        self.message = QtWidgets.QLabel()
        self.message.setObjectName("SecondaryText")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QtWidgets.QDialogButtonBox()
        self.continue_button = buttons.addButton("Continue", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.continue_button.setProperty("accent", True)
        self.continue_button.setDefault(True)
        cancel = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        cancel.setProperty("tertiary", True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._render(prefer=current)
        if not servers:
            self.address.setFocus()
        self._start_checks()

    # -- status checks run in threads; a timer brings results to this thread
    def _start_checks(self) -> None:
        import concurrent.futures

        if not self._status:
            return
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._futures = {s: self._pool.submit(self._check, s) for s in self._status}
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._collect)
        self._timer.start(150)

    def _collect(self) -> None:
        changed = False
        for server, future in list(self._futures.items()):
            if future.done():
                self._status[server] = bool(future.result()) if not future.exception() else False
                del self._futures[server]
                changed = True
        if changed:
            self._render(prefer=self._chosen_item_server())
        if not self._futures:
            self._timer.stop()
            self._pool.shutdown(wait=False)

    def _chosen_item_server(self) -> str | None:
        item = self.list.currentItem()
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item and item.isSelected() else None

    def _render(self, prefer: str | None = None) -> None:
        from GUI.theme import active_theme, theme_color

        theme = active_theme()
        order = {True: 0, None: 1, False: 2}
        servers = sorted(self._status, key=lambda s: order[self._status[s]])
        self.list.blockSignals(True)
        self.list.clear()
        for server in servers:
            state = self._status[server]
            mark, note, color = {
                True: ("●", "answering", theme.accent),
                None: ("○", "checking…", theme.muted),
                False: ("○", "not answering (switched off, or Tailscale not connected)", theme.faint),
            }[state]
            item = QtWidgets.QListWidgetItem(f"{mark}  {server}\n     {note}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, server)
            item.setForeground(theme_color(color) if state is not True else self.palette().text().color())
            self.list.addItem(item)
        self.list.blockSignals(False)
        rows = {self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole): i for i in range(self.list.count())}
        answering = [s for s in servers if self._status[s]]
        pick = prefer if prefer in rows and self._status.get(prefer) is not False else (answering[0] if answering else None)
        if pick is not None and not self.address.text().strip():
            self.list.setCurrentRow(rows[pick])
        lines = self.list.sizeHintForRow(0) if self.list.count() else 0
        self.list.setFixedHeight(min(6, max(1, self.list.count())) * (lines + 8) + 16)
        self._selected()

    def _selected(self) -> None:
        server = self._chosen_item_server()
        self.forget_button.setEnabled(server is not None)
        if server is not None and self._status.get(server) is False:
            self.message.setText(f"{server} is not answering. Connect Tailscale, or choose another server.")
        else:
            self.message.setText("")

    def _forget(self) -> None:
        server = self._chosen_item_server()
        if server is None:
            return
        self.forgotten.append(server)
        self._status.pop(server, None)
        self._render()
        self.list.setVisible(bool(self._status))
        self.forget_button.setVisible(bool(self._status))

    def _accept(self) -> None:
        typed = self.address.text().strip()
        server = typed or self._chosen_item_server()
        if not server or server == "https://":
            self.message.setText("Choose a server above or type its address.")
            return
        if not server.startswith(("http://", "https://")):
            server = "https://" + server
        self.server = server
        self.accept()


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
        self.next_cursor = None
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
        self.table.setRowCount(0)
        self.append_sessions(sessions)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(lambda _index: self.accept())
        if sessions:
            self.table.selectRow(0)
        layout.addWidget(self.table, 1)
        self.issue = IssueBanner(self)
        layout.addWidget(self.issue)
        self.more_button = QtWidgets.QPushButton("Load more sessions")
        self.more_button.hide()
        layout.addWidget(self.more_button)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Open
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_next_cursor(self, cursor):
        self.next_cursor = cursor
        self.more_button.setVisible(bool(cursor))
        self.more_button.setText("Load more sessions")
        self.more_button.setEnabled(True)
        self.issue.hide()

    def append_sessions(self, sessions):
        start = self.table.rowCount()
        self.table.setRowCount(start + len(sessions))
        for row, session in enumerate(sessions, start):
            for column, (key, _label) in enumerate(self.COLUMNS):
                value = session.get(key)
                text = "" if value is None else str(value)
                if key == "started_at":
                    text = text.replace("T", " ")[:16]
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, str(session.get("id", "")))
                self.table.setItem(row, column, item)
        self._apply_filter(self.filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, c).text().casefold() for c in range(self.table.columnCount())]
            self.table.setRowHidden(row, bool(needle) and not any(needle in c for c in cells))

    def accept(self) -> None:
        if not self.table.isEnabled():
            return
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
