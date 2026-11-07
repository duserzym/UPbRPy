"""A fake laicpmsdb server for client and GUI tests (no Django, no database).

It serves the routes UPbRPy's database mode uses, from session-bundle
folders on disk, and records every call. Run it by hand for manual checks::

    python tests/fake_laicpmsdb.py --port 8765 --bundle path/to/<session_key>

Routes follow ``upblab/docs/WORKFLOW.md`` v1 and laicpmsdb's plan 8.1-8.4.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
import zipfile


def _zip_folder(folder: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(folder.name) / path.relative_to(folder)).as_posix())
    return buffer.getvalue()


@dataclass
class Call:
    method: str
    path: str
    headers: dict
    body: bytes = b""
    json: object = None
    form: dict = field(default_factory=dict)


@dataclass
class FakeState:
    username: str = "analyst"
    password: str = "correct horse"
    sessions: dict = field(default_factory=dict)  # id -> bundle folder
    tokens: set = field(default_factory=set)
    codes: set = field(default_factory=set)
    calls: list = field(default_factory=list)
    imports: list = field(default_factory=list)  # (session_id, form, zip bytes)
    runs: dict = field(default_factory=dict)  # run_id -> session_id
    preferred: dict = field(default_factory=dict)  # session_id -> run_id
    fail_next: dict = field(default_factory=dict)  # path regex -> (status, detail)

    def issue_code(self) -> str:
        code = secrets.token_urlsafe(12)
        self.codes.add(code)
        return code

    def issue_token(self) -> str:
        token = "lcp_" + secrets.token_urlsafe(16)
        self.tokens.add(token)
        return token


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeLaicpmsdb/1"
    state: FakeState

    def log_message(self, *args):  # keep pytest output clean
        pass

    # ------------------------------------------------------------ helpers
    def _send(self, status: int, payload=None, *, headers=None, raw: bytes | None = None):
        body = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
        self.send_response(status)
        if raw is None and payload is not None:
            self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> Call:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        call = Call(self.command, self.path, dict(self.headers.items()), body)
        ctype = self.headers.get("Content-Type", "")
        if body and ctype.startswith("application/json"):
            call.json = json.loads(body)
        elif body and ctype.startswith("multipart/form-data"):
            message = BytesParser(policy=email_policy).parsebytes(
                f"Content-Type: {ctype}\r\n\r\n".encode() + body
            )
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                call.form[name] = (part.get_filename(), part.get_payload(decode=True))
        self.state.calls.append(call)
        return call

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] in self.state.tokens:
            return True
        self._send(401, {"detail": "Unauthorized"})
        return False

    def _injected_failure(self, path: str) -> bool:
        for pattern, (status, detail) in list(self.state.fail_next.items()):
            if re.fullmatch(pattern, path):
                del self.state.fail_next[pattern]
                self._send(status, {"detail": detail})
                return True
        return False

    # -------------------------------------------------------------- verbs
    def do_GET(self):
        call = self._record()
        url = urlsplit(call.path)
        path = url.path
        if self._injected_failure(path):
            return
        if path == "/v1/auth/providers":
            return self._send(200, {"password_registration": False, "orcid": True,
                                    "google": False, "google_domains": []})
        match = re.fullmatch(r"/v1/auth/(\w+)/start", path)
        if match:
            nxt = parse_qs(url.query).get("next", [""])[0]
            sep = "&" if "?" in nxt else "?"
            return self._send(302, headers={"Location": f"{nxt}{sep}{urlencode({'code': self.state.issue_code()})}"})
        if not self._authorized():
            return
        if path == "/v1/accounts/me":
            return self._send(200, {"id": "acct-1", "username": self.state.username,
                                    "display_name": "Synthetic Analyst"})
        if path == "/v1/sessions":
            return self._send(200, [self._session_out(sid, folder)
                                    for sid, folder in self.state.sessions.items()])
        match = re.fullmatch(r"/v1/sessions/([^/]+)/export/upbrpy", path)
        if match:
            folder = self.state.sessions.get(match.group(1))
            if folder is None:
                return self._send(404, {"detail": "Not Found"})
            manifest = (folder / "session_manifest.toml").read_bytes()
            return self._send(200, raw=_zip_folder(folder), headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{folder.name}.zip"',
                "X-Export-SHA256": hashlib.sha256(manifest).hexdigest(),
                "X-Session-Key": folder.name,
            })
        match = re.fullmatch(r"/v1/runs/([^/]+)", path)
        if match:
            session_id = self.state.runs.get(match.group(1))
            if session_id is None:
                return self._send(404, {"detail": "Not Found"})
            return self._send(200, {"id": match.group(1), "session_id": session_id})
        self._send(404, {"detail": "Not Found"})

    def do_POST(self):
        call = self._record()
        path = urlsplit(call.path).path
        if self._injected_failure(path):
            return
        if path == "/v1/auth/login":
            body = call.json or {}
            if body.get("username") == self.state.username and body.get("password") == self.state.password:
                return self._send(200, {"token": self.state.issue_token(), "account": {}})
            return self._send(401, {"detail": "Invalid username or password"})
        if path == "/v1/auth/exchange":
            code = (call.json or {}).get("code")
            if code in self.state.codes:
                self.state.codes.discard(code)
                return self._send(200, {"token": self.state.issue_token(), "account": {}})
            return self._send(401, {"detail": "Sign-in link expired; please try again"})
        if not self._authorized():
            return
        if path == "/v1/auth/logout":
            self.state.tokens.discard(self.headers["Authorization"][7:])
            return self._send(204)
        match = re.fullmatch(r"/v1/sessions/([^/]+)/runs/import", path)
        if match:
            return self._import(match.group(1), call)
        match = re.fullmatch(r"/v1/sessions/([^/]+)/runs/([^/]+)/prefer", path)
        if match:
            session_id, run_id = match.groups()
            if self.state.runs.get(run_id) != session_id:
                return self._send(404, {"detail": "Not Found"})
            self.state.preferred[session_id] = run_id
            return self._send(200, {"session_id": session_id, "preferred_run_id": run_id})
        self._send(404, {"detail": "Not Found"})

    # ------------------------------------------------------------- routes
    def _session_out(self, session_id, folder: Path) -> dict:
        return {"id": session_id, "session_key": folder.name, "material": "apatite",
                "kind": "u_pb", "status": "ingested", "started_at": "2025-03-20T21:00:00Z",
                "acquisition_count": len(list((folder / "raw").glob("*.csv")))}

    def _import(self, session_id: str, call: Call):
        folder = self.state.sessions.get(session_id)
        if folder is None:
            return self._send(404, {"detail": "Not Found"})
        if "bundle" not in call.form:
            return self._send(422, {"detail": "multipart field 'bundle' is required"})
        _, data = call.form["bundle"]
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            manifest = json.loads(archive.read("reduction_manifest.json"))
        except (zipfile.BadZipFile, KeyError, ValueError):
            return self._send(422, {"detail": "not a reduction bundle"})
        known = {hashlib.sha256(p.read_bytes()).hexdigest() for p in (folder / "raw").glob("*.csv")}
        hashes = [f.get("sha256") for f in manifest.get("input_files", [])]
        if not hashes or not all(hashes):
            return self._send(422, {"detail": "input_files[].sha256 is required"})
        unmatched = [h for h in hashes if h not in known]
        run_id = f"run-{len(self.state.runs) + 1}"
        self.state.runs[run_id] = session_id
        supersedes = call.form.get("supersedes", (None, b""))[1].decode() or None
        self.state.imports.append((session_id, supersedes, data, manifest))
        names = archive.namelist()
        spot_rows = 0
        if "spot_mean_results.csv" in names:
            spot_rows = max(0, len(archive.read("spot_mean_results.csv").decode().splitlines()) - 1)
        self._send(201, {
            "run_id": run_id,
            "counts": {"input_files": len(hashes), "spot_results": spot_rows},
            "quarantine": [{"kind": "unmatched_input", "sha256": h} for h in unmatched],
        })


class FakeLaicpmsdb:
    """Context manager running the fake server on a free localhost port."""

    def __init__(self, state: FakeState | None = None, port: int = 0):
        self.state = state or FakeState()
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> "FakeLaicpmsdb":
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def main() -> None:  # pragma: no cover - manual use
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bundle", action="append", default=[], type=Path,
                        help="session bundle folder; repeat for more")
    parser.add_argument("--session-id", action="append", default=[])
    args = parser.parse_args()
    state = FakeState()
    for index, folder in enumerate(args.bundle):
        session_id = args.session_id[index] if index < len(args.session_id) else f"session-{index + 1}"
        state.sessions[session_id] = folder.resolve()
    with FakeLaicpmsdb(state, port=args.port) as fake:
        code = state.issue_code()
        print(f"fake laicpmsdb at {fake.url}; user {state.username!r} / {state.password!r}")
        for session_id in state.sessions:
            print(f"  upbr://session/{session_id}?server={fake.url}&code={code}")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":  # pragma: no cover
    main()
