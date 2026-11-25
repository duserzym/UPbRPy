"""Client for a UPbDB server: database mode of UPbRPy Studio.

Thin, typed and free of Qt. It transfers session and reduction bundles
and TIMS markup over HTTP; everything scientific stays in the engine.

Endpoints (all under ``<server>/v1/``, bearer token unless noted)::

    POST auth/login        {username, password} -> {token, account}  (no token)
    POST auth/exchange     {code}               -> {token, account}  (no token)
    browser: <server>/app/#/connect?app=upbrpy&callback=upbr://auth/callback&state=… (no token)
    POST auth/logout
    GET  accounts/me                            -> account
    GET  sessions?q=                            -> [session]
    GET  sessions/{id}                          -> session detail
    GET  sessions/{id}/laser                    -> {rows, logs, logs_next_cursor}
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

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import importlib
import os
import shutil
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from types import SimpleNamespace
from threading import RLock
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import zipfile

from .. import tls

from ..pipeline import package_version
from ..session_bundle import (
    SessionBundle,
    SessionBundleError,
    _verify_unpacked, find_cached_session, read_bundle_record, find_bundle_project,
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
        details: str = "", retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        # ``code`` is UPbDB's stable error code when it sent one;
        # ``details`` keeps the raw reply for logs, never for the message.
        self.code = code
        self.details = details
        self.retry_after = retry_after

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


class OversightConfirmationRequired(PermissionDenied):
    """The server requires consent for a write on the session owner's behalf."""

    def __init__(self, owner: str, *, details: str = "") -> None:
        self.owner = owner
        super().__init__(
            f"HTTP 403: confirm this action on behalf of {owner}.",
            status=403, code="oversight_confirm", details=details,
        )


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
        return SimpleNamespace(request=tls.request,
                               exceptions=importlib.import_module("requests").exceptions)
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
    base = normalize_server(server)
    _token_store.delete(base)
    with _tims_lock:
        for key in list(_tims_responses):
            if key[0] == base:
                del _tims_responses[key]
    for key in list(_session_pages):
        if key[0] == base:
            del _session_pages[key]
    with _laser_lock:
        for key in list(_laser_responses):
            if key[0] == base:
                del _laser_responses[key]


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


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def _raise_for(response, server: str, action: str) -> None:
    status = response.status_code
    if status < 400:
        return
    detail, code, raw = _reply(response)
    extra = {"status": status, "code": code, "details": raw,
             "retry_after": _retry_after(getattr(response, "headers", {}).get("Retry-After"))}
    host = _host(server)
    suffix = f": {detail}" if detail else "."
    if status == 401:
        forget_token(server)
        raise AuthenticationRequired(
            f"Sign in to {host} again to {action}" + (f" ({detail})." if detail else "."), **extra
        )
    if status == 403:
        if code == "oversight_confirm":
            owner = response.json().get("owner")
            raise OversightConfirmationRequired(
                owner.strip() if isinstance(owner, str) and owner.strip() else "the session owner",
                details=raw,
            )
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
    transport = _requests()
    base = normalize_server(server)
    headers = {"User-Agent": user_agent(), "Accept": "application/json",
               "Accept-Encoding": "gzip"}
    headers.update(kwargs.pop("headers", {}) or {})
    if auth:
        token = token or stored_token(base)
        if not token:
            raise AuthenticationRequired(f"Sign in to {base} to {action}.")
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = transport.request(
            method,
            api_url(base, path),
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            stream=stream,
            allow_redirects=False,
            **kwargs,
        )
    except transport.exceptions.Timeout as exc:
        raise ConnectionFailed(f"{base} did not answer in time.", details=str(exc)) from exc
    except transport.exceptions.RequestException as exc:
        message = (tls.certificate_error_message(_host(base)) if tls.is_certificate_error(exc)
                   else f"Cannot reach {base}. Check the address and your internet connection.")
        raise ConnectionFailed(message, details=str(exc)) from exc
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


# --------------------------------------------------------------- laser writes
_laser_upload_unsupported: set[str] = set()
MAX_LASER_LOG_BYTES = 20 * 1024 * 1024


def laser_upload_availability(server: str) -> tuple[bool, str]:
    """Check local availability without contacting the server."""
    available, reason = availability()
    if not available:
        return available, reason
    if not server or not is_signed_in(server):
        return False, "Sign in to UPbDB to upload a laser log."
    if normalize_server(server) in _laser_upload_unsupported:
        return False, "This UPbDB server cannot receive laser logs"
    return True, ""


def _laser_write(server, path, **kwargs):
    try:
        return _request("POST", server, path, action="save laser data", **kwargs)
    except DatabaseError as exc:
        if path == "laser-logs" and exc.status in (404, 405):
            _laser_upload_unsupported.add(normalize_server(server))
        if exc.status is not None:
            excerpt = _summary([exc.details])[:300]
            exc.message = f"HTTP {exc.status}: {exc.message}" + (f" Reply: {excerpt}" if excerpt else "")
        raise


def upload_laser_log(server, path, *, lab_id=None, time_zone=None) -> dict:
    """Upload original bytes; return log detail plus HTTP ``status_code``.

    The file digest and upload options determine the idempotency key.
    Retries with the same file and options have identical wire bodies.
    """
    if not is_signed_in(server):
        raise AuthenticationRequired("Sign in to UPbDB to upload a laser log.")
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_LASER_LOG_BYTES + 1)
    except OSError as exc:
        raise DatabaseError(f"Cannot read laser log {path.name}: {exc}") from exc
    if len(data) > MAX_LASER_LOG_BYTES:
        raise RequestRejected("Laser logs must be at most 20 MB.", status=413)
    digest = hashlib.sha256(data).hexdigest()
    if time_zone is not None:
        try:
            ZoneInfo(time_zone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise RequestRejected("Enter an IANA time zone, such as America/Boise.") from exc
    fields = []
    if lab_id is not None:
        fields.append(("lab_id", str(lab_id)))
    if time_zone is not None:
        fields.append(("time_zone", time_zone))
    fields.append(("file", (path.name, data, "application/octet-stream")))
    # UPbDB hashes the wire body, including the multipart boundary.
    _requests()
    encode = importlib.import_module("urllib3.filepost").encode_multipart_formdata
    body, content_type = encode(fields, boundary=f"upbrpy-{digest}")
    options = hashlib.sha256(body).hexdigest()[:16]
    response = _laser_write(server, "laser-logs", data=body,
        headers={"Idempotency-Key": f"laser-log-{digest}-{options}", "Content-Type": content_type})
    payload = _json(response, server, "upload the laser log")
    if response.status_code not in (200, 201) or not isinstance(payload, Mapping) or not payload.get("id"):
        raise ServerError("UPbDB returned no laser log id after upload.")
    return {**payload, "status_code": response.status_code}


def preview_laser_log_tie(server, log_id, session_id) -> dict:
    """Preview one session using position and a 30 second threshold.

    Only the first row page is requested; session counts cover all rows.
    """
    response = _laser_write(server, f"laser-logs/{quote(str(log_id), safe='')}/tie/preview",
        params={"limit": 1}, json={"session_ids": [session_id], "spot_key": "position", "threshold_s": 30})
    return _json(response, server, "preview the laser tie")


def tie_laser_log(server, log_id, session_id, *, revision) -> dict:
    """Save a user-confirmed preview for one session, checking its revision."""
    response = _laser_write(server, f"laser-logs/{quote(str(log_id), safe='')}/tie",
        json={"session_ids": [session_id], "spot_key": "position", "threshold_s": 30, "revision": revision})
    return _json(response, server, "tie the laser log")


# --------------------------------------------------------------- sessions
class SessionPage(list):
    """One bounded session list page, compatible with existing list callers."""

    def __init__(self, items=(), *, next_cursor: str | None = None):
        super().__init__(items)
        self.next_cursor = next_cursor


# Bounded, per-account/server cache. Sign-out clears the server's entries.
_session_pages: OrderedDict[tuple, tuple[str | None, SessionPage]] = OrderedDict()


def list_sessions(server: str, query: str | None = None, *, limit: int = 100,
                  cursor: str | None = None) -> SessionPage:
    """GET one session page; follow ``next_cursor`` only on user demand."""
    base = normalize_server(server)
    limit = max(1, min(int(limit), 500))
    account = hashlib.sha256((stored_token(base) or "").encode()).hexdigest()
    key = (base, account, query, limit, cursor)
    cached = _session_pages.get(key)
    headers = {"If-None-Match": cached[0]} if cached and cached[0] else {}
    params = {"limit": limit}
    if query:
        params["q"] = query
    if cursor:
        params["cursor"] = cursor
    response = _request("GET", base, "sessions", action="list sessions",
                        params=params, headers=headers)
    if response.status_code == 304 and cached:
        return SessionPage(cached[1], next_cursor=cached[1].next_cursor)
    payload = _json(response, base, "list sessions")
    if isinstance(payload, Mapping) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    if not isinstance(payload, list):
        raise ServerError(f"{base} sent no session list.")
    page = SessionPage((dict(item) for item in payload if isinstance(item, Mapping)),
                       next_cursor=response.headers.get("X-Next-Cursor") or None)
    _session_pages[key] = (response.headers.get("ETag"), page)
    _session_pages.move_to_end(key)
    while len(_session_pages) > 64:
        _session_pages.popitem(last=False)
    return SessionPage(page, next_cursor=page.next_cursor)


_laser_lock = RLock()
_laser_responses: OrderedDict = OrderedDict()


def _laser_read(server, path, params=None):
    """Revalidate one bounded detail or laser page per server and account."""
    base = normalize_server(server)
    token = stored_token(base)
    account = hashlib.sha256((token or "").encode()).hexdigest()
    key = (base, account, path, tuple(sorted((params or {}).items())))
    with _laser_lock:
        cached = _laser_responses.get(key)
    response = _request("GET", base, path, action="read session laser data", params=params,
                        headers={"If-None-Match": cached[0]} if cached and cached[0] else {})
    if response.status_code == 304:
        if cached is None:
            raise ServerError(f"{base} returned HTTP 304 without cached laser data.")
        return cached[1], cached[2]
    payload = _json(response, base, "read session laser data")
    if (not isinstance(payload, dict) or (path.endswith("/laser") and any(
            not isinstance(payload.get(name, []), list)
            or any(not isinstance(row, dict) for row in payload.get(name, []))
            for name in ("rows", "logs")))):
        raise ServerError(
            f"{base} returned HTTP {response.status_code} with invalid session laser data: "
            f"{response.text[:200]}", status=response.status_code,
        )
    cursor = response.headers.get("X-Next-Cursor") or None
    with _laser_lock:
        if stored_token(base) == token:
            _laser_responses[key] = (response.headers.get("ETag"), payload, cursor)
            _laser_responses.move_to_end(key)
            while len(_laser_responses) > 128:
                _laser_responses.popitem(last=False)
    return payload, cursor


def get_session(server: str, session_id: str) -> dict:
    """Read and revalidate session detail, including optional laser indicators."""
    return _laser_read(server, f"sessions/{quote(session_id, safe='')}")[0]


@dataclass(frozen=True)
class SessionLaserPage:
    """Independent bounded acquisition and tied-log pages."""

    rows: list[dict]
    logs: list[dict]
    next_cursor: str | None = None
    logs_next_cursor: str | None = None


def get_session_laser(server: str, session_id: str, *, limit: int = 500,
                      cursor: str | None = None, logs_cursor: str | None = None) -> SessionLaserPage:
    """Read one laser page; follow both cursors, revalidating with ETag."""
    params = {"limit": max(1, min(int(limit), 500)), "logs_limit": 500}
    if cursor:
        params["after"] = cursor
    if logs_cursor:
        params["logs_after"] = logs_cursor
    payload, following = _laser_read(server, f"sessions/{quote(session_id, safe='')}/laser", params)
    return SessionLaserPage(payload.get("rows", []), payload.get("logs", []),
                            following, payload.get("logs_next_cursor") or None)


def session_laser_tie(rows: list[dict], logs: list[dict], acquisition_starts: Mapping,
                      *, time_zone: str | None = None):
    """Convert saved database ties to portable acquisition-relative windows.

    Aware starts retain their instants. Naive instrument starts require the
    session's IANA time zone, using fold=0 at an ambiguous local time. Raise
    ValueError if that zone is unavailable or more than half the timed links
    have corrected on-times over one hour from their acquisition starts.
    Other such links are dropped and counted as clock_mismatch. This is a
    clock sanity check, not the standalone 30 s matching rule.
    """
    from ..laser_tie import LaserTie, LaserTieRow

    def utc(value):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    starts = {}
    zone = None
    for label, value in acquisition_starts.items():
        try:
            start = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            continue
        if start.tzinfo is None:
            if zone is None:
                try:
                    zone = ZoneInfo(time_zone)
                except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
                    raise ValueError(
                        "Laser data unavailable: naive acquisition starts need a valid "
                        "session time zone from UPbDB."
                    ) from exc
            start = start.replace(tzinfo=zone, fold=0)
        starts[label] = start.astimezone(timezone.utc)

    by_id = {str(log["id"]): log for log in logs}
    tied = {}
    candidates = set()
    mismatched = set()
    for row in rows:
        label = row["label"]
        log = by_id.get(str(row.get("log_id")))
        if (log is None or log.get("clock_offset_s") is None or label not in starts
                or label in candidates):
            continue
        try:
            start = starts[label]
            endpoints = [((utc(row[key]) - start).total_seconds() + float(log["clock_offset_s"]))
                         if row.get(key) else None for key in ("laser_on_at", "laser_off_at")]
        except (ValueError, TypeError, OverflowError):
            continue
        if all(value is None for value in endpoints):
            continue
        on, off = endpoints
        candidates.add(label)
        if on is not None and abs(on) > 3600.:
            mismatched.add(label)
            continue
        tied.setdefault(label, LaserTieRow(
            status="linked", laser_on=on, laser_off=off,
            duration=off - on if on is not None and off is not None else None,
            fluence_j_cm2=row.get("fluence_j_cm2"), rep_rate_hz=row.get("rep_rate_hz"),
            spot_um=row.get("spot_size_um"), spot_shape=row.get("spot_shape"),
            shot_count=row.get("shot_count"), output_pct=row.get("output_pct"),
            stage_um=tuple(row["stage_um"]) if row.get("stage_um") else None))
    warning = (f"Laser clock or zone mismatch: dropped {len(mismatched)} of {len(candidates)} "
               "timed links with laser-on more than one hour from acquisition start.")
    if len(mismatched) * 2 > len(candidates):
        raise ValueError(warning + " Laser timing was not applied.")
    provenance = tuple({
        "id": log["id"], "sha256": "", "file_name": log.get("file_name", ""),
        "system_type": "", "laser_instrument": "", "spot_key": log.get("spot_key", ""),
        "clock_offset_s": log.get("clock_offset_s"),
        "clock_offset_spread_s": log.get("clock_offset_spread_s"), "pairs": None,
    } for log in logs)
    return LaserTie(
        sha256="", file_name=", ".join(log.get("file_name", "") for log in logs),
        system_type="", laser_instrument="", spot_key=",".join(dict.fromkeys(
            log.get("spot_key", "") for log in logs)), threshold_s=0., has_laser_times=bool(tied),
        clock_offset_s=None, clock_offset_spread_s=None, pairs=0,
        counts={"linked": len(tied), "doubtful": 0,
                "unlinked": len(acquisition_starts) - len(tied) - len(mismatched),
                **({"clock_mismatch": len(mismatched)} if mismatched else {})},
        warnings=(warning,) if mismatched else (), rows=tied, source="database", logs=provenance,
    )


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
    cached = find_cached_session(session_id, dest_dir, server=base)
    record = read_bundle_record(cached) if cached is not None else {}
    if cached is not None:
        try:
            key, identity, export_hash = _verify_unpacked(cached)
            if identity != session_id or export_hash != record.get("export_sha256"):
                cached = None
        except (OSError, ValueError):
            cached = None
    headers = {"Accept": "application/zip"}
    if cached is not None:
        headers["If-None-Match"] = f'"{export_hash}"'
    response = _request(
        "GET", base, f"sessions/{quote(str(session_id))}/export/upbrpy",
        action=action, stream=True, headers=headers,
    )
    if cached is not None and (
        response.status_code == 304
        or response.headers.get("X-Export-SHA256", "").lower() == export_hash
    ):
        response.close()
        return SessionBundle(
            cached, key, identity, export_hash, str(record.get("zip_sha256", "")),
            find_bundle_project(cached, key), reused=True,
        )
    expected = response.headers.get("X-Export-SHA256") or None
    with tempfile.TemporaryDirectory(prefix="upbrpy-fetch-") as scratch:
        target = Path(scratch) / "bundle.zip"
        digest = hashlib.sha256()
        try:
            with target.open("wb") as out:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if chunk:
                        out.write(chunk)
                        digest.update(chunk)
        except _requests().exceptions.RequestException as exc:
            raise ConnectionFailed(
                f"The download from {base} was interrupted."
            ) from exc
        finally:
            response.close()
        try:
            return unpack_session_bundle(
                target, dest_dir, server=base, expected_export_sha256=expected,
                zip_sha256=digest.hexdigest()
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
    idempotency_key: str | None = None,
    on_behalf: bool = False,
) -> ImportResult:
    """Zip a reduction bundle and post it as a new run of the session.

    Multipart field ``bundle`` (``application/zip``), plus ``supersedes``
    when the run replaces an earlier one. Returns the run id, the counts and
    the quarantine report. Set ``on_behalf`` only after explicit consent.
    Reuse ``idempotency_key`` when confirming or retrying the same submit.
    """
    action = "import the reduction"
    with tempfile.TemporaryDirectory(prefix="upbrpy-submit-") as scratch:
        archive = zip_reduction_bundle(
            bundle_dir, Path(scratch) / f"{Path(bundle_dir).name or 'reduction'}.zip"
        )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        if on_behalf:
            headers["X-UPbDB-Oversight"] = "on-behalf"
        if idempotency_key:
            # Stable multipart bytes matter: the server hashes the request body.
            # Stream via disk so large bundles never need a second copy in RAM.
            boundary = "upbrpy-" + hashlib.sha256(idempotency_key.encode()).hexdigest()
            multipart = Path(scratch) / "request.multipart"
            with multipart.open("wb") as out:
                if supersedes:
                    out.write((f'--{boundary}\r\nContent-Disposition: form-data; name="supersedes"'
                               f'\r\n\r\n{supersedes}\r\n').encode())
                out.write((f'--{boundary}\r\nContent-Disposition: form-data; name="bundle"; '
                           f'filename="reduction.zip"\r\nContent-Type: application/zip\r\n\r\n').encode())
                with archive.open("rb") as source:
                    shutil.copyfileobj(source, out)
                out.write(f"\r\n--{boundary}--\r\n".encode())
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            with multipart.open("rb") as stream:
                response = _request(
                    "POST", server, f"sessions/{quote(str(session_id))}/runs/import",
                    action=action, headers=headers, data=stream,
                )
        else:
            with archive.open("rb") as stream:
                response = _request(
                    "POST", server, f"sessions/{quote(str(session_id))}/runs/import",
                    action=action, headers=headers,
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


def prefer_run(server: str, session_id: str, run_id: str, *,
               idempotency_key: str | None = None, on_behalf: bool = False) -> dict:
    """Mark a run as preferred; ``on_behalf`` requires separate explicit consent."""
    action = "mark the run as preferred"
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    if on_behalf:
        headers["X-UPbDB-Oversight"] = "on-behalf"
    response = _request(
        "POST", server,
        f"sessions/{quote(str(session_id))}/runs/{quote(str(run_id))}/prefer",
        action=action, headers=headers,
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
    "SessionPage",
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
    "get_session", "get_session_laser", "SessionLaserPage", "session_laser_tie",
    "upload_laser_log", "laser_upload_availability", "preview_laser_log_tie", "tie_laser_log",
    "fetch_session_bundle",
    "zip_reduction_bundle",
    "submit_reduction",
    "prefer_run",
    "get_run",
    "fetch_run_project",
    "list_session_runs",
]


# --------------------------------------------------------------- TIMS
_tims_lock = RLock()
_tims_responses: OrderedDict = OrderedDict()


def _tims_read(server, path, params=None):
    base = normalize_server(server)
    token = stored_token(base)
    key = (base, token, path, tuple(sorted((params or {}).items())))
    with _tims_lock:
        cached = _tims_responses.get(key)
    response = _request("GET", server, path, action="read TIMS choices", params=params,
                        headers={"If-None-Match": cached[0]} if cached and cached[0] else {})
    if response.status_code == 304:
        if cached is None:
            raise ServerError("TIMS returned 304 without a cached response.")
        return cached[1], cached[2]
    payload = _json(response, server, "read TIMS choices")
    cursor = response.headers.get("X-Next-Cursor")
    with _tims_lock:
        if stored_token(base) == token:
            _tims_responses[key] = (response.headers.get("ETag"), payload, cursor)
            _tims_responses.move_to_end(key)
            while len(_tims_responses) > 128:
                _tims_responses.popitem(last=False)
    return payload, cursor


class FractionPage(list):
    """A TIMS list page, or a 304 retaining the caller's cached page."""

    def __init__(self, items=(), *, next_cursor=None, etag=None, not_modified=False):
        super().__init__(items)
        self.next_cursor = next_cursor
        self.etag = etag
        self.not_modified = not_modified


def list_fractions(server, *, sample=None, grain=None, spot=None, limit=500,
                   cursor=None, etag=None):
    """Read one bounded fraction page; ``cursor`` is sent as ``after``."""
    params = {key: value for key, value in
              (("sample", sample), ("grain", grain), ("spot", spot),
               ("limit", limit), ("after", cursor)) if value is not None}
    response = _request("GET", server, "fractions", action="read TIMS marks",
                        params=params, headers={"If-None-Match": etag} if etag else {})
    return FractionPage(
        () if response.status_code == 304 else _json(response, server, "read TIMS marks"),
        next_cursor=response.headers.get("X-Next-Cursor"),
        etag=response.headers.get("ETag", etag), not_modified=response.status_code == 304,
    )


def all_fractions(server, *, sample=None, grain=None, spot=None, limit=500):
    """Follow fraction cursors for one filter on a worker thread."""
    rows, cursor, seen = [], None, set()
    while True:
        page = list_fractions(server, sample=sample, grain=grain, spot=spot,
                              limit=limit, cursor=cursor)
        rows.extend(page)
        cursor = page.next_cursor
        if not cursor:
            return rows
        if cursor in seen:
            raise ServerError("The TIMS list repeated a page cursor.")
        seen.add(cursor)


def suggest_fraction_name(server, sample_id, mineral) -> str:
    """Return the server's next fraction name for this sample and mineral."""
    response = _request("GET", server, "fractions/suggest-name", action="suggest a fraction name",
                        params={"sample": sample_id, "mineral": mineral})
    return str(_json(response, server, "suggest a fraction name")["suggestion"])


def _tims_list(server, path, params):
    rows, cursor, seen = [], None, set()
    while True:
        payload, cursor = _tims_read(server, path,
                                     {**params, "limit": 500, **({"after": cursor} if cursor else {})})
        rows.extend(payload)
        if not cursor:
            return rows
        if cursor in seen:
            raise ServerError("The TIMS list repeated a page cursor.")
        seen.add(cursor)


def list_capsule_sets(server, *, active=True):
    """Read capsule sets in server order."""
    return _tims_list(server, "tims/capsule-sets", {"active": str(active).lower()})


def list_batches(server, capsule_set_id):
    """Read batches belonging to a capsule set."""
    return _tims_list(server, "tims/batches", {"capsule_set": capsule_set_id})


def get_batch(server, batch_id):
    """Read a batch and its capsule map in one request."""
    return _tims_read(server, f"tims/batches/{quote(str(batch_id), safe='')}")[0]


def list_recipes(server, *, lab_id, mineral):
    """Read active recipes for a lab and mineral."""
    return _tims_list(server, "tims/recipes", {"lab": lab_id, "mineral": mineral, "active": "true"})


def _tims_create(server, path, payload, idempotency_key):
    if not idempotency_key:
        raise ValueError("A TIMS write requires an idempotency key.")
    response = _request("POST", server, path, action="save TIMS markup", json=payload,
                        headers={"Idempotency-Key": idempotency_key})
    return _json(response, server, "save TIMS markup")


def create_batch(server, payload, *, idempotency_key):
    """Create a batch, retaining the supplied key when retrying."""
    return _tims_create(server, "tims/batches", payload, idempotency_key)


def create_fraction(server, payload, *, idempotency_key):
    """Create a fraction and its sources atomically."""
    return _tims_create(server, "fractions", payload, idempotency_key)


def list_sample_batches(server, sample_id, *, limit=500, cursor=None, etag=None):
    """Read one sample's batch summaries, without fetching batch details per spot."""
    response = _request("GET", server, "tims/batches", action="read TIMS batch dates",
                        params={"sample": sample_id, "limit": limit,
                                **({"after": cursor} if cursor else {})},
                        headers={"If-None-Match": etag} if etag else {})
    return FractionPage(
        () if response.status_code == 304 else _json(response, server, "read TIMS batch dates"),
        next_cursor=response.headers.get("X-Next-Cursor"),
        etag=response.headers.get("ETag", etag), not_modified=response.status_code == 304,
    )
