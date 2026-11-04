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
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtWidgets

from GUI.launch import LaunchTarget
from GUI.workers import FunctionWorker
from UPbRPy.session_bundle import SessionBundle, find_project_for_run


SETTINGS_LAST_SERVER = "database/last_server"
SETTINGS_SERVERS = "database/servers"


def database_availability() -> tuple[bool, str]:
    """``(available, reason)`` for the ``[database]`` extra."""
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


__all__ = [
    "SETTINGS_LAST_SERVER",
    "SETTINGS_SERVERS",
    "DatabaseLinks",
    "database_availability",
    "client",
]
