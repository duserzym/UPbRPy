"""Client for a UPbDB server: database mode of UPbRPy Studio.

Thin, typed and free of Qt. It moves the two bundles of
``upblab/docs/WORKFLOW.md`` (sections 3.2 and 3.3) over HTTP and nothing
else; everything scientific stays in the engine.

Endpoints (all under ``<server>/v1/``, bearer token unless noted)::

    POST auth/login        {username, password} -> {token, account}  (no token)
    POST auth/exchange     {code}               -> {token, account}  (no token)
    browser: <server>/app/#/connect?app=upbrpy&callback=upbr://auth/callback&state=… (no token)
    POST auth/logout
    GET  accounts/me                            -> account
    GET  sessions?q=                            -> [session]
    GET  sessions/{id}/export/upbrpy            -> zip (X-Export-SHA256)
    GET  sessions/{id}/reductions               -> [run]
    POST sessions/{id}/runs/import  multipart "bundle" (zip) [+ "supersedes"]
                                                -> {run_id, counts, quarantine}
    POST sessions/{id}/runs/{run_id}/prefer     -> {...}
    GET  runs/{run_id}                          -> {id, session_id, ...}
    GET  runs/{run_id}/project                  -> saved .upbr bytes

Tokens are kept per server in the OS keychain (``keyring``, service
``UPbRPy``, user name = the normalized server URL), never in a file. Every
request sends ``User-Agent: UPbRPy-Studio/<version>``. Failures raise a
:class:`DatabaseError` subclass whose ``str()`` is a sentence the GUI can
show as is.

Requires the ``[database]`` extra (``requests`` and ``keyring``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import importlib
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit
import zipfile

from ..pipeline import package_version
from ..session_bundle import (
    SessionBundle,
    SessionBundleError,
    unpack_session_bundle,
)


KEYRING_SERVICE = "UPbRPy"
AUTH_CALLBACK_URL = "upbr://auth/callback"
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 300.0


# ------------------------------------------------------------------ errors
class DatabaseError(Exception):
    """A failure the GUI can show; ``str(error)`` is the message."""

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None,
        details: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        # ``code`` is UPbDB's stable error code when it sent one;
        # ``details`` keeps the raw reply for logs, never for the message.
        self.code = code
        self.details = details

    def __str__(self) -> str:
        return self.message


class DatabaseUnavailable(DatabaseError):
    """The ``[database]`` extra is not installed."""


class ConnectionFailed(DatabaseError):
    """The server could not be reached or did not answer in time."""


class AuthenticationRequired(DatabaseError):
    """No valid token for this server: the user has to sign in."""


class PermissionDenied(DatabaseError):
    """Signed in, but the account may not do this (HTTP 403)."""


class NotFound(DatabaseError):
    """The session or run does not exist or is not visible (HTTP 404)."""


class RequestRejected(DatabaseError):
    """The server refused the request as invalid (HTTP 400, 409, 413, 422)."""


class ServerError(DatabaseError):
    """The server failed (HTTP 5xx) or answered something unexpected."""


class BundleError(DatabaseError):
    """The session bundle download is not a usable bundle."""


# ------------------------------------------------------------ availability
def missing_requirements() -> list[str]:
    """Names of the ``[database]`` extra's packages that cannot be imported."""
    missing = []
    for name in ("requests", "keyring"):
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    return missing


def availability() -> tuple[bool, str]:
    """Return ``(available, reason)``; ``reason`` is empty when available."""
    missing = missing_requirements()
    if not missing:
        return True, ""
    return False, (
        "Database mode needs the optional [database] extra "
        f"({', '.join(missing)} missing): pip install 'UPbRPy[database]'."
    )


def _requests():
    try:
        return importlib.import_module("requests")
    except ImportError as exc:
        raise DatabaseUnavailable(availability()[1]) from exc


def user_agent() -> str:
    return f"UPbRPy-Studio/{package_version()}"


# ------------------------------------------------------------------ server
def normalize_server(server: str) -> str:
    """Return the base URL a token is stored under.

    Adds ``https://`` (``http://`` for localhost) when no scheme is given,
    drops a trailing ``/`` and a trailing ``/v1``, lower-cases scheme and host.
    """
    text = str(server or "").strip()
    if not text:
        raise DatabaseError("Enter the database server address.")
    if "://" not in text:
        host = text.split("/", 1)[0].split(":", 1)[0].lower()
        local = host in {"localhost", "127.0.0.1", "[::1]"} or host.endswith(
            ".localhost"
        )
        text = ("http://" if local else "https://") + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise DatabaseError(f"{server!r} is not an http(s) server address.")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def api_url(server: str, path: str) -> str:
    return f"{normalize_server(server)}/v1/{path.lstrip('/')}"


# ------------------------------------------------------------ token store
class TokenStore(Protocol):
    def get(self, server: str) -> str | None: ...

    def set(self, server: str, token: str) -> None: ...

    def delete(self, server: str) -> None: ...


class MemoryTokenStore:
    """Tokens for this process only (tests, or no usable keychain)."""

    persistent = False

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    def get(self, server: str) -> str | None:
        return self.tokens.get(server)

    def set(self, server: str, token: str) -> None:
        self.tokens[server] = token

    def delete(self, server: str) -> None:
        self.tokens.pop(server, None)


class KeyringTokenStore:
    """Tokens in the OS keychain (macOS Keychain, Windows Credential Manager).

    If the platform has no usable keychain the token is kept in memory for
    this process instead; it is never written to a file.
    """

    persistent = True

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service
        self._fallback = MemoryTokenStore()

    def _keyring(self):
        try:
            return importlib.import_module("keyring")
        except ImportError as exc:
            raise DatabaseUnavailable(availability()[1]) from exc

    def get(self, server: str) -> str | None:
        keyring = self._keyring()
        try:
            return keyring.get_password(self.service, server) or self._fallback.get(server)
        except Exception:  # keyring.errors.KeyringError and backend failures
            return self._fallback.get(server)

    def set(self, server: str, token: str) -> None:
        keyring = self._keyring()
        try:
            keyring.set_password(self.service, server, token)
        except Exception:
            self.persistent = False
            self._fallback.set(server, token)

    def delete(self, server: str) -> None:
        self._fallback.delete(server)
        keyring = self._keyring()
        try:
            keyring.delete_password(self.service, server)
        except Exception:
            pass


_token_store: TokenStore = KeyringTokenStore()


def set_token_store(store: TokenStore) -> TokenStore:
    """Replace the token store (tests); returns the previous one."""
    global _token_store
    previous, _token_store = _token_store, store
    return previous


def stored_token(server: str) -> str | None:
    return _token_store.get(normalize_server(server))


def store_token(server: str, token: str) -> None:
    _token_store.set(normalize_server(server), token)


def forget_token(server: str) -> None:
    _token_store.delete(normalize_server(server))


def is_signed_in(server: str) -> bool:
    try:
        return bool(stored_token(server))
    except DatabaseError:
        return False


# ------------------------------------------------------------- transport
_MAX_LISTED = 2


def _summary(items: list) -> str:
    """At most two problems in a sentence, then "and N more"; duplicates once."""
    texts = []
    for item in items:
        if isinstance(item, Mapping):
            text = str(item.get("reason") or item.get("msg") or "").strip()
            location = item.get("loc")
            if item.get("msg") and isinstance(location, (list, tuple)) and location:
                text = f"{location[-1]}: {text}"
        else:
            text = str(item).strip()
        if text and text not in texts:
            texts.append(text)
    if not texts:
        return ""
    shown = "; ".join(texts[:_MAX_LISTED])
    extra = len(texts) - _MAX_LISTED
    return shown + (f"; and {extra} more" if extra > 0 else "")


def _reply(response) -> tuple[str, str | None, str]:
    """(short human detail, UPbDB error code, raw text for logs)."""
    raw = (response.text or "").strip()[:2000]
    try:
        payload = response.json()
    except ValueError:
        return "", None, raw
    if not isinstance(payload, Mapping):
        return "", None, raw
    code = payload.get("code") if isinstance(payload.get("code"), str) else None
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip(), code, raw
    detail = payload.get("detail") or payload.get("error")
    if isinstance(detail, list):
        problems = payload.get("problems")
        return _summary(problems if isinstance(problems, list) and problems else detail), code, raw
    if isinstance(detail, str):
        return detail.strip(), code, raw
    return "", code, raw


def _detail(response) -> str:
    return _reply(response)[0]


def _host(server: str) -> str:
    return urlsplit(server).netloc or server


def _raise_for(response, server: str, action: str) -> None:
    status = response.status_code
    if status < 400:
        return
    detail, code, raw = _reply(response)
    extra = {"status": status, "code": code, "details": raw}
    host = _host(server)
    suffix = f": {detail}" if detail else "."
    if status == 401:
        forget_token(server)
        raise AuthenticationRequired(
            f"Sign in to {host} again to {action}" + (f" ({detail})." if detail else "."), **extra
        )
    if status == 403:
        raise PermissionDenied(f"Your account may not {action} on {host}{suffix}", **extra)
    if status == 404:
        raise NotFound(f"Not found on {host}, or not shared with you{suffix}", **extra)
    if status in (400, 409, 413, 415, 422):
        raise RequestRejected(f"{host} did not accept the request to {action}{suffix}", **extra)
    raise ServerError(
        f"{host} had a problem while trying to {action}. Try again in a minute; "
        "if it keeps failing, tell the database administrator.",
        **extra,
    )


def _request(
    method: str,
    server: str,
    path: str,
    *,
    action: str,
    auth: bool = True,
    token: str | None = None,
    stream: bool = False,
    **kwargs: Any,
):
    requests = _requests()
    base = normalize_server(server)
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    headers.update(kwargs.pop("headers", {}) or {})
    if auth:
        token = token or stored_token(base)
        if not token:
            raise AuthenticationRequired(f"Sign in to {base} to {action}.")
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(
            method,
            api_url(base, path),
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            stream=stream,
            allow_redirects=False,
            **kwargs,
        )
    except requests.exceptions.Timeout as exc:
        raise ConnectionFailed(f"{base} did not answer in time.") from exc
    except requests.exceptions.RequestException as exc:
        raise ConnectionFailed(
            f"Cannot reach {base}. Check the address and your network."
        ) from exc
    _raise_for(response, base, action)
    return response


def _json(response, server: str, action: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ServerError(
            f"{normalize_server(server)} sent an unexpected answer while "
            f"trying to {action}."
        ) from exc


# ----------------------------------------------------------------- auth
def login(server: str, username: str, password: str) -> str:
    """Password sign-in; stores and returns the bearer token."""
    response = _request(
        "POST", server, "auth/login", action="sign in", auth=False,
        json={"username": username, "password": password},
    )
    return _keep_token(server, _json(response, server, "sign in"))


def exchange_code(server: str, code: str) -> str:
    """Trade a one-time code (handoff or ORCID callback) for a token.

    Stores the token in the keychain for ``server`` and returns it.
    """
    if not code:
        raise AuthenticationRequired("The sign-in link has no code.")
    response = _request(
        "POST", server, "auth/exchange", action="complete the sign-in",
        auth=False, json={"code": code},
    )
    return _keep_token(server, _json(response, server, "complete the sign-in"))


def _keep_token(server: str, payload: Any) -> str:
    token = payload.get("token") if isinstance(payload, Mapping) else None
    if not token:
        raise ServerError(f"{normalize_server(server)} returned no token.")
    store_token(server, str(token))
    return str(token)


def sign_in_url(
    server: str,
    provider: str = "orcid",
    callback: str = AUTH_CALLBACK_URL,
) -> str:
    """The browser URL that starts ORCID (or Google) sign-in.

    The server redirects back to ``callback?code=<one-time code>``, which the
    OS hands to Studio (``upbr://auth/callback``); Studio then calls
    :func:`exchange_code`.
    """
    query = urlencode({"next": callback})
    return api_url(server, f"auth/{quote(provider)}/start") + "?" + query


def connect_url(server: str, state: str, callback: str = AUTH_CALLBACK_URL) -> str:
    """UPbDB's own sign-in page for Studio (``#/connect``).

    It offers every sign-in the server has (ORCID, Google, GitHub, a password,
    ORCID sign-up). After the person confirms, the page sends the browser to
    ``callback?code=<one-time code>&state=<state>``; Studio checks ``state``
    and calls :func:`exchange_code`.
    """
    query = urlencode({"app": "upbrpy", "callback": callback, "state": state})
    return server.rstrip("/") + "/app/#/connect?" + query


def providers(server: str) -> dict:
    """``GET /v1/auth/providers``: which sign-in methods the server offers."""
    response = _request(
        "GET", server, "auth/providers", action="list sign-in methods", auth=False
    )
    payload = _json(response, server, "list sign-in methods")
    return dict(payload) if isinstance(payload, Mapping) else {}


def whoami(server: str) -> dict:
    """``GET /v1/accounts/me``: the signed-in account."""
    response = _request("GET", server, "accounts/me", action="read your account")
    payload = _json(response, server, "read your account")
    if not isinstance(payload, Mapping):
        raise ServerError(f"{normalize_server(server)} sent no account.")
    return dict(payload)


def sign_out(server: str) -> None:
    """Revoke the token on the server (best effort) and forget it."""
    try:
        if stored_token(server):
            _request("POST", server, "auth/logout", action="sign out")
    except DatabaseError:
        pass
    finally:
        forget_token(server)


# --------------------------------------------------------------- sessions
def list_sessions(server: str, query: str | None = None) -> list[dict]:
    """``GET /v1/sessions``: sessions visible to the account, newest first."""
    params = {"q": query} if query else None
    response = _request(
        "GET", server, "sessions", action="list sessions", params=params
    )
    payload = _json(response, server, "list sessions")
    if isinstance(payload, Mapping) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    if not isinstance(payload, list):
        raise ServerError(f"{normalize_server(server)} sent no session list.")
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def fetch_session_bundle(
    server: str,
    session_id: str,
    dest_dir: str | Path | None = None,
) -> SessionBundle:
    """Download, verify and unpack a session's bundle into the cache.

    ``dest_dir`` is the cache root (default ``~/UPbRPy/sessions``); the
    bundle lands in ``dest_dir/<session_key>/``. The returned
    :class:`~UPbRPy.session_bundle.SessionBundle` is path-like and carries
    the project file, ``export_sha256`` (hash of the manifest, compared with
    the ``X-Export-SHA256`` header) and the zip's own hash. A cached copy
    with the same ``export_sha256`` is kept as it is (``reused=True``).
    """
    base = normalize_server(server)
    action = "download the session"
    response = _request(
        "GET", base, f"sessions/{quote(str(session_id))}/export/upbrpy",
        action=action, stream=True, headers={"Accept": "application/zip"},
    )
    expected = response.headers.get("X-Export-SHA256") or None
    with tempfile.TemporaryDirectory(prefix="upbrpy-fetch-") as scratch:
        target = Path(scratch) / "bundle.zip"
        try:
            with target.open("wb") as out:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if chunk:
                        out.write(chunk)
        except _requests().exceptions.RequestException as exc:
            raise ConnectionFailed(
                f"The download from {base} was interrupted."
            ) from exc
        finally:
            response.close()
        try:
            return unpack_session_bundle(
                target, dest_dir, server=base, expected_export_sha256=expected
            )
        except SessionBundleError as exc:
            raise BundleError(str(exc)) from exc


# ------------------------------------------------------------------ runs
@dataclass(frozen=True)
class ImportResult:
    """What ``POST /v1/sessions/{id}/runs/import`` returned."""

    run_id: str
    counts: dict = field(default_factory=dict)
    quarantine: Any = None
    raw: dict = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:  # result["run_id"] works too
        return {"run_id": self.run_id, "counts": self.counts,
                "quarantine": self.quarantine}.get(key, self.raw.get(key))

    @property
    def quarantine_items(self) -> list:
        """The quarantine report as a list (empty when nothing was held)."""
        q = self.quarantine
        if not q:
            return []
        if isinstance(q, Mapping):
            items = q.get("items") or q.get("entries")
            if isinstance(items, list):
                return items
            return [{"key": k, "value": v} for k, v in q.items() if v]
        return list(q) if isinstance(q, (list, tuple)) else [q]


def zip_reduction_bundle(bundle_dir: str | Path, target: str | Path) -> Path:
    """Zip a reduction bundle folder with its files at the zip root."""
    folder = Path(bundle_dir)
    if not (folder / "reduction_manifest.json").is_file():
        raise BundleError(
            f"{folder} is not a reduction bundle (no reduction_manifest.json)."
        )
    target = Path(target)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.name != ".DS_Store":
                archive.write(path, path.relative_to(folder).as_posix())
    return target


def submit_reduction(
    server: str,
    session_id: str,
    bundle_dir: str | Path,
    *,
    supersedes: str | None = None,
) -> ImportResult:
    """Zip a reduction bundle and post it as a new run of the session.

    Multipart field ``bundle`` (``application/zip``), plus ``supersedes``
    when the run replaces an earlier one. Returns the run id, the counts and
    the quarantine report.
    """
    action = "import the reduction"
    with tempfile.TemporaryDirectory(prefix="upbrpy-submit-") as scratch:
        archive = zip_reduction_bundle(
            bundle_dir, Path(scratch) / f"{Path(bundle_dir).name or 'reduction'}.zip"
        )
        with archive.open("rb") as stream:
            response = _request(
                "POST", server, f"sessions/{quote(str(session_id))}/runs/import",
                action=action,
                files={"bundle": (archive.name, stream, "application/zip")},
                data={"supersedes": supersedes} if supersedes else None,
            )
    payload = _json(response, server, action)
    if not isinstance(payload, Mapping):
        raise ServerError(f"{normalize_server(server)} sent no run.")
    run_id = payload.get("run_id") or payload.get("id")
    if not run_id and isinstance(payload.get("run"), Mapping):
        run_id = payload["run"].get("id")
    if not run_id:
        raise ServerError(f"{normalize_server(server)} did not return a run id.")
    counts = payload.get("counts")
    return ImportResult(
        run_id=str(run_id),
        counts=dict(counts) if isinstance(counts, Mapping) else {},
        quarantine=payload.get("quarantine"),
        raw=dict(payload),
    )


def prefer_run(server: str, session_id: str, run_id: str) -> dict:
    """Mark a run as the session's preferred run."""
    action = "mark the run as preferred"
    response = _request(
        "POST", server,
        f"sessions/{quote(str(session_id))}/runs/{quote(str(run_id))}/prefer",
        action=action,
    )
    if not response.content:
        return {}
    payload = _json(response, server, action)
    return dict(payload) if isinstance(payload, Mapping) else {}


def get_run(server: str, run_id: str) -> dict:
    """``GET /v1/runs/{run_id}``: at least ``id`` and ``session_id``."""
    action = "look up the run"
    response = _request("GET", server, f"runs/{quote(str(run_id))}", action=action)
    payload = _json(response, server, action)
    if not isinstance(payload, Mapping) or not payload.get("session_id"):
        raise ServerError(f"{normalize_server(server)} sent no session for the run.")
    return dict(payload)


def fetch_run_project(server: str, run_id: str) -> bytes:
    """Download a run's saved project; a missing file raises NotFound (404)."""
    response = _request(
        "GET", server, f"runs/{quote(str(run_id), safe='')}/project",
        action="download the run project", headers={"Accept": "application/json"},
    )
    return response.content


def list_session_runs(server: str, session_id: str) -> list[dict]:
    """Return the runs visible to the account for a session."""
    action = "list session runs"
    response = _request(
        "GET", server, f"sessions/{quote(str(session_id), safe='')}/reductions",
        action=action,
    )
    payload = _json(response, server, action)
    if isinstance(payload, Mapping) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    if not isinstance(payload, list):
        raise ServerError(f"{normalize_server(server)} sent no run list.")
    return [dict(item) for item in payload if isinstance(item, Mapping)]


__all__ = [
    "KEYRING_SERVICE",
    "AUTH_CALLBACK_URL",
    "DatabaseError",
    "DatabaseUnavailable",
    "ConnectionFailed",
    "AuthenticationRequired",
    "PermissionDenied",
    "NotFound",
    "RequestRejected",
    "ServerError",
    "BundleError",
    "ImportResult",
    "TokenStore",
    "MemoryTokenStore",
    "KeyringTokenStore",
    "availability",
    "missing_requirements",
    "user_agent",
    "normalize_server",
    "api_url",
    "set_token_store",
    "stored_token",
    "store_token",
    "forget_token",
    "is_signed_in",
    "login",
    "exchange_code",
    "sign_in_url",
    "providers",
    "whoami",
    "sign_out",
    "list_sessions",
    "fetch_session_bundle",
    "zip_reduction_bundle",
    "submit_reduction",
    "prefer_run",
    "get_run",
    "fetch_run_project",
    "list_session_runs",
]
