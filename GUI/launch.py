"""How UPbRPy Studio is launched: ``--open`` targets, ``upbr://`` links,
the single running instance, and the URL-scheme registration.

``--open`` accepts

- an experiment directory, a Qtegra CSV, or a session-bundle root,
- a ``.upbr`` project,
- a session-bundle ``.zip``,
- ``upbr://session/<id>?server=<base url>&code=<one-time code>``,
- ``upbr://run/<id>?server=<base url>``,
- ``upbr://auth/callback?code=<one-time code>&state=<state>`` (end of a
  browser sign-in through UPbDB's ``#/connect`` page).

A second launch with a ``upbr://`` link hands it to the running Studio over
a local socket (:class:`SingleInstance`) and exits. macOS delivers links as
``QFileOpenEvent`` (the scheme is declared in the app bundle's
``CFBundleURLTypes``); Windows passes them on the command line once
``upbrpy-gui --register-url-scheme`` has written
``HKCU\\Software\\Classes\\upbr``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from PySide6 import QtCore, QtNetwork


URL_SCHEME = "upbr"


class LaunchError(ValueError):
    """An ``--open`` value that Studio cannot open."""


@dataclass(frozen=True)
class LaunchTarget:
    """One thing to open. ``kind`` is one of ``directory``, ``project``,
    ``bundle_zip``, ``session``, ``run`` or ``auth_callback``."""

    kind: str
    path: Path | None = None
    identifier: str = ""
    server: str = ""
    code: str = ""
    original: str = ""
    acquisition: str = ""
    via: str = ""
    state: str = ""
    fresh: bool = False

    @property
    def is_url(self) -> bool:
        return self.kind in {"session", "run", "auth_callback"}

    @property
    def needs_database(self) -> bool:
        return self.is_url

    def describe(self) -> str:
        if self.kind == "session":
            return f"session {self.identifier} on {self.server}"
        if self.kind == "run":
            return f"run {self.identifier}" + (f" on {self.server}" if self.server else "")
        if self.kind == "auth_callback":
            return "the sign-in callback"
        return str(self.path)


def _query(parts) -> dict[str, str]:
    values = {k: v[-1] for k, v in parse_qs(parts.query).items() if v}
    # UPbDB's browser callbacks put parameters after a fragment route
    # (``#/auth/callback?code=…``); accept those too.
    if "?" in parts.fragment:
        fragment_query = parts.fragment.split("?", 1)[1]
        for key, value in parse_qs(fragment_query).items():
            values.setdefault(key, value[-1])
    return values


def parse_upbr_url(value: str) -> LaunchTarget:
    """Parse a ``upbr://`` link into a :class:`LaunchTarget`."""
    parts = urlsplit(value.strip())
    if parts.scheme.lower() != URL_SCHEME:
        raise LaunchError(f"{value!r} is not a upbr:// link.")
    segments = [unquote(s) for s in [parts.netloc, *parts.path.split("/")] if s]
    query = _query(parts)
    server = query.get("server", "").strip()
    if not segments:
        raise LaunchError(f"{value!r} names nothing to open.")
    route = segments[0].lower()
    if route in {"session", "run"}:
        if len(segments) != 2:
            raise LaunchError(f"Expected upbr://{route}/<id>, got {value!r}.")
        if route == "session" and not server:
            raise LaunchError("A upbr://session link needs ?server=<database URL>.")
        acquisition = query.get("acquisition", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", acquisition):
            acquisition = ""
        return LaunchTarget(
            kind=route,
            identifier=segments[1],
            server=server,
            code=query.get("code", ""),
            original=value,
            acquisition=acquisition,
            via="upblab" if query.get("via") == "upblab" else "",
            fresh=route == "session" and query.get("fresh") == "1",
        )
    if route == "auth" and segments[1:] == ["callback"]:
        error = query.get("error")
        if error == "cancelled":
            raise LaunchError("Sign-in was cancelled in the browser.")
        if error == "orcid_required":
            raise LaunchError(
                "No UPbDB account uses that sign-in yet. Create your account with ORCID "
                "on the sign-in page, then link Google or GitHub on your Account page."
            )
        if error:
            raise LaunchError(f"Sign-in failed: {error}.")
        if not query.get("code"):
            raise LaunchError("The sign-in callback carries no code.")
        state = query.get("state", "")
        if state and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", state):
            raise LaunchError("The sign-in callback carries an invalid state.")
        return LaunchTarget(
            kind="auth_callback", code=query["code"], server=server, original=value,
            state=state,
        )
    raise LaunchError(f"Studio does not know the link {value!r}.")


def parse_open_target(value: str | os.PathLike) -> LaunchTarget:
    """Classify an ``--open`` value (path or link)."""
    text = os.fspath(value).strip()
    if not text:
        raise LaunchError("Nothing to open.")
    if text.lower().startswith(f"{URL_SCHEME}:"):
        return parse_upbr_url(text)
    if text.lower().startswith("file://"):
        text = unquote(urlsplit(text).path)
        if sys.platform == "win32" and text.startswith("/") and ":" in text[:4]:
            text = text[1:]
    path = Path(text).expanduser()
    if path.is_dir() or (path.is_file() and path.suffix.lower() == ".csv"):
        # Keep CSVs on the same dispatch path as raw experiment folders.
        return LaunchTarget(kind="directory", path=path.resolve(), original=text)
    suffix = path.suffix.lower()
    if suffix not in {".upbr", ".zip"}:
        if not path.exists():
            raise LaunchError(f"{path} does not exist.")
        raise LaunchError(
            f"{path.name} is not a folder, a Qtegra .csv, a .upbr project or a session-bundle .zip."
        )
    if not path.is_file():
        raise LaunchError(f"{path} does not exist.")
    kind = "project" if suffix == ".upbr" else "bundle_zip"
    return LaunchTarget(kind=kind, path=path.resolve(), original=text)


# ----------------------------------------------------------- single instance
def instance_server_name() -> str:
    """A local-socket name private to this user (a pipe name on Windows).

    ``UPBRPY_INSTANCE_NAME`` overrides it (tests, or a second Studio kept
    apart on purpose).
    """
    override = os.environ.get("UPBRPY_INSTANCE_NAME")
    if override:
        return override
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no user database
        user = str(Path.home())
    digest = hashlib.sha256(f"{user}|{Path.home()}".encode()).hexdigest()[:12]
    return f"UPbRPy-Studio-{digest}"


class SingleInstance(QtCore.QObject):
    """Hand ``--open`` values from a second launch to the running Studio.

    The running instance :meth:`listen`\\ s; a new process first tries
    :meth:`send`, and exits if the running instance acknowledged.
    Messages are one JSON line, ``{"open": "<value>"}``, answered ``ok``.
    """

    received = QtCore.Signal(str)

    def __init__(self, name: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.name = name or instance_server_name()
        self.server: QtNetwork.QLocalServer | None = None

    def send(self, value: str, timeout_ms: int = 2000) -> bool:
        """Deliver ``value`` to a running instance; ``False`` if none answered."""
        socket = QtNetwork.QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write((json.dumps({"open": value}) + "\n").encode("utf-8"))
        socket.flush()
        # flush() may already have written everything, in which case
        # waitForBytesWritten() reports False with nothing left to send.
        if socket.bytesToWrite() and not socket.waitForBytesWritten(timeout_ms):
            socket.abort()
            return False
        acknowledged = False
        if socket.waitForReadyRead(timeout_ms):
            acknowledged = bytes(socket.readAll()).strip() == b"ok"
        socket.disconnectFromServer()
        return acknowledged

    def listen(self) -> bool:
        """Become the instance that receives hand-offs.

        Returns ``False`` when another instance already answers under this
        name. A socket file left by a crashed instance (Unix) answers no one,
        so it is removed first. (Qt's ``UserAccessOption`` listen would
        silently replace even a live socket, hence the explicit probe.)
        """
        probe = QtNetwork.QLocalSocket()
        probe.connectToServer(self.name)
        if probe.waitForConnected(300):
            probe.abort()
            return False
        QtNetwork.QLocalServer.removeServer(self.name)
        server = QtNetwork.QLocalServer(self)
        server.setSocketOptions(QtNetwork.QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.name):
            return False
        server.newConnection.connect(self._accept)
        self.server = server
        return True

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    def _accept(self) -> None:
        while self.server is not None and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            buffer = bytearray()

            def read(socket=socket, buffer=buffer) -> None:
                buffer.extend(bytes(socket.readAll()))
                if b"\n" not in buffer:
                    return
                line = bytes(buffer).split(b"\n", 1)[0]
                try:
                    value = str(json.loads(line.decode("utf-8"))["open"])
                except (ValueError, KeyError, TypeError):
                    socket.write(b"error\n")
                else:
                    socket.write(b"ok\n")
                    self.received.emit(value)
                socket.flush()
                socket.disconnectFromServer()

            socket.readyRead.connect(read)
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                read()


# ------------------------------------------------------------ macOS events
class FileOpenFilter(QtCore.QObject):
    """Route macOS ``QFileOpenEvent``\\ s (links, Finder opens) to a callback."""

    def __init__(self, callback: Callable[[str], None], parent=None) -> None:
        super().__init__(parent)
        self.callback = callback

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QtCore.QEvent.Type.FileOpen:
            url = event.url()
            if url.isValid() and url.scheme().lower() == URL_SCHEME:
                value = url.toString(QtCore.QUrl.ComponentFormattingOption.FullyEncoded)
            else:
                value = event.file() or url.toLocalFile()
            if value:
                self.callback(value)
                return True
        return False


# -------------------------------------------------- Windows URL registration
def url_scheme_command(executable: str | None = None) -> str:
    """The ``shell\\open\\command`` that launches Studio with a link."""
    if executable:
        return f'"{executable}" --open "%1"'
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --open "%1"'
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    if windowed.exists():
        python = windowed
    return f'"{python}" -m GUI.main --open "%1"'


def register_url_scheme(executable: str | None = None, *, winreg_module=None) -> str:
    """Write ``HKCU\\Software\\Classes\\upbr`` so Windows opens links in Studio.

    Per user, no administrator rights. Returns the registered command.
    ``winreg_module`` is for tests.
    """
    if winreg_module is None:
        if sys.platform != "win32":
            raise LaunchError(
                "--register-url-scheme is for Windows; on macOS the app bundle "
                "declares the upbr scheme (CFBundleURLTypes)."
            )
        import winreg as winreg_module  # type: ignore[import-not-found]
    winreg = winreg_module
    command = url_scheme_command(executable)
    icon = command.split('"')[1] if command.startswith('"') else ""
    root = rf"Software\Classes\{URL_SCHEME}"
    entries = [
        (root, "", "URL:UPbRPy Studio"),
        (root, "URL Protocol", ""),
        (rf"{root}\DefaultIcon", "", f'"{icon}",0' if icon else ""),
        (rf"{root}\shell\open\command", "", command),
    ]
    for key_path, name, value in entries:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        try:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        finally:
            winreg.CloseKey(key)
    return command


def unregister_url_scheme(*, winreg_module=None) -> None:
    """Remove ``HKCU\\Software\\Classes\\upbr`` (Windows)."""
    if winreg_module is None:
        if sys.platform != "win32":
            return
        import winreg as winreg_module  # type: ignore[import-not-found]
    winreg = winreg_module
    root = rf"Software\Classes\{URL_SCHEME}"
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", r"\DefaultIcon", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, root + sub)
        except OSError:
            pass


__all__ = [
    "URL_SCHEME",
    "LaunchError",
    "LaunchTarget",
    "parse_upbr_url",
    "parse_open_target",
    "instance_server_name",
    "SingleInstance",
    "FileOpenFilter",
    "url_scheme_command",
    "register_url_scheme",
    "unregister_url_scheme",
]
