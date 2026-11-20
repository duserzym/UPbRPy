"""A fake UPbDB server for client and GUI tests (no Django, no database).

It serves the routes UPbRPy's database mode uses, from session-bundle
folders on disk, and records every call. Run it by hand for manual checks::

    python tests/fake_upbdb.py --port 8765 --bundle path/to/<session_key>

Routes follow ``upblab/docs/WORKFLOW.md`` v1 and UPbDB's plan 8.1-8.4.
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


def _capsule_sets():
    names = ("Becquerel", "Curie", "Holmes", "Wilson", "Krogh", "Aston", "Nier", "Ussher", "Rutherford", "Soddy")
    return [{"id": name.lower(), "name": name, "code": name[0], "positions": 15,
             "positions_per_puck": 5, "lab_id": "lab-1", "active": True} for name in names]


@dataclass
class FakeState:
    username: str = "analyst"
    password: str = "correct horse"
    sessions: dict = field(default_factory=dict)  # id -> bundle folder
    conditional_bundles: bool = True
    tokens: set = field(default_factory=set)
    codes: set = field(default_factory=set)
    calls: list = field(default_factory=list)
    imports: list = field(default_factory=list)  # (session_id, form, zip bytes)
    run_projects: dict = field(default_factory=dict)  # run_id -> project bytes
    runs: dict = field(default_factory=dict)  # run_id -> session_id
    preferred: dict = field(default_factory=dict)  # session_id -> run_id
    fail_next: dict = field(default_factory=dict)  # path regex -> (status, detail or full error body)
    session_owners: dict = field(default_factory=dict)  # id -> owner's username/name
    explicit_roles: dict = field(default_factory=dict)  # id -> caller's Write/Admin role
    idempotent_imports: dict = field(default_factory=dict)  # (session_id, key) -> (hash, reply)

    capsule_sets: list = field(default_factory=_capsule_sets)
    batches: dict = field(default_factory=dict)
    fractions: list = field(default_factory=list)
    recipes: list = field(default_factory=list)
    tims_writes: dict = field(default_factory=dict)
    tims_page_size: int = 500

    def issue_code(self) -> str:
        code = secrets.token_urlsafe(12)
        self.codes.add(code)
        return code

    def issue_token(self) -> str:
        token = "lcp_" + secrets.token_urlsafe(16)
        self.tokens.add(token)
        return token


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeUpbdb/1"
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
                body = detail if isinstance(detail, dict) and "detail" in detail and "code" in detail else {"detail": detail}
                self._send(status, body)
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
        if self._tims_get(path, parse_qs(url.query)):
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
            export_hash = hashlib.sha256(manifest).hexdigest()
            if (self.state.conditional_bundles
                    and self.headers.get("If-None-Match") == f'"{export_hash}"'):
                return self._send(304, headers={"ETag": f'"{export_hash}"',
                                                "X-Export-SHA256": export_hash})
            return self._send(200, raw=_zip_folder(folder), headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{folder.name}.zip"',
                "X-Export-SHA256": export_hash,
                "ETag": f'"{export_hash}"',
                "X-Session-Key": folder.name,
            })
        match = re.fullmatch(r"/v1/sessions/([^/]+)/reductions", path)
        if match:
            sid = match.group(1)
            return self._send(200, [
                {"id": rid, "session_id": sid, "status": "completed",
                 "is_preferred_for_session": self.state.preferred.get(sid) == rid}
                for rid, session in self.state.runs.items() if session == sid
            ])
        match = re.fullmatch(r"/v1/runs/([^/]+)/project", path)
        if match:
            data = self.state.run_projects.get(match.group(1))
            if data is None:
                return self._send(404, {"detail": "No saved project"})
            return self._send(200, raw=data, headers={"Content-Type": "application/json"})
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
        if path in {"/v1/tims/batches", "/v1/fractions"}:
            return self._tims_post(path, call)
        if path == "/v1/auth/logout":
            self.state.tokens.discard(self.headers["Authorization"][7:])
            return self._send(204)
        match = re.fullmatch(r"/v1/sessions/([^/]+)/runs/import", path)
        if match:
            if not self._oversight_allowed(match.group(1)):
                return
            return self._import(match.group(1), call)
        match = re.fullmatch(r"/v1/sessions/([^/]+)/runs/([^/]+)/prefer", path)
        if match:
            session_id, run_id = match.groups()
            if not self._oversight_allowed(session_id):
                return
            if self.state.runs.get(run_id) != session_id:
                return self._send(404, {"detail": "Not Found"})
            self.state.preferred[session_id] = run_id
            return self._send(200, {"session_id": session_id, "preferred_run_id": run_id})
        self._send(404, {"detail": "Not Found"})

    # ------------------------------------------------------------- routes
    def _tims_page(self, rows, query):
        limit = min(int(query.get("limit", [500])[0]), self.state.tims_page_size)
        start = int(query.get("after", [0])[0])
        etag = '"' + hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest() + '"'
        headers = {"ETag": etag}
        if start + limit < len(rows):
            headers["X-Next-Cursor"] = str(start + limit)
        if self.headers.get("If-None-Match") == etag:
            self._send(304, headers=headers)
        else:
            self._send(200, rows[start:start + limit], headers=headers)

    def _tims_get(self, path, query):
        value = lambda key: query.get(key, [None])[0]
        if path == "/v1/fractions/suggest-name":
            letter = dict(zip(("zircon", "baddeleyite", "titanite", "rutile", "apatite", "monazite", "xenotime", "other"), "zbtramxo"))[value("mineral")]
            numbers = [int(match[1]) for row in self.state.fractions
                       if row["sample"]["id"] == value("sample")
                       if (match := re.fullmatch(letter + r"([0-9]{1,3})[a-z]?", row["name"]))]
            self._send(200, {"suggestion": f"{letter}{max(numbers, default=0) + 1}"})
        elif path == "/v1/fractions":
            rows = [f for f in self.state.fractions if not f.get("retired_at")]
            if value("sample"):
                rows = [f for f in rows if f["sample"]["id"] == value("sample")]
            for key in ("grain", "spot"):
                if value(key):
                    rows = [f for f in rows if any(s.get(key + "_id") == value(key) for s in f["sources"])]
            self._tims_page(rows, query)
        elif path == "/v1/tims/capsule-sets":
            rows = [r for r in self.state.capsule_sets if value("active") is None or r["active"] == (value("active") == "true")]
            self._tims_page(rows, query)
        elif path == "/v1/tims/recipes":
            self._tims_page([r for r in self.state.recipes if r["lab_id"] == value("lab") and r["mineral"] == value("mineral") and r.get("active", True)], query)
        elif path == "/v1/tims/batches":
            rows = list(self.state.batches.values())
            if value("capsule_set"):
                rows = [b for b in rows if b["capsule_set_id"] == value("capsule_set")]
            if value("sample"):
                ids = {f.get("batch_id") for f in self.state.fractions
                       if f["sample"]["id"] == value("sample") and not f.get("retired_at")}
                rows = [b for b in rows if b["id"] in ids]
            self._tims_page(rows, query)
        elif path.startswith("/v1/tims/batches/"):
            batch = self.state.batches.get(path.rsplit("/", 1)[1])
            if batch:
                detail = self._batch_detail(batch)
                etag = '"' + hashlib.sha256(json.dumps(detail, sort_keys=True).encode()).hexdigest() + '"'
                self._send(304, headers={"ETag": etag}) if self.headers.get("If-None-Match") == etag else self._send(200, detail, headers={"ETag": etag})
            else:
                self._send(404, {"detail": "Not found"})
        else:
            return False
        return True

    def _batch_detail(self, batch):
        capsule_set = next(s for s in self.state.capsule_sets if s["id"] == batch["capsule_set_id"])
        fractions = [f for f in self.state.fractions if f.get("batch_id") == batch["id"] and not f.get("retired_at")]
        occupied = {f["capsule_number"]: f["id"] for f in fractions}
        return {**batch, "capsule_set": capsule_set, "fractions": fractions,
                "capsule_map": {str(n): occupied.get(n) for n in range(1, capsule_set["positions"] + 1)}}

    def _tims_post(self, path, call):
        key = self.headers.get("Idempotency-Key")
        identity = (path, key)
        previous = self.state.tims_writes.get(identity)
        if not key:
            return self._send(422, {"detail": "Idempotency-Key required", "code": "validation_failed"})
        if previous:
            if previous[0] != call.json:
                return self._send(409, {"detail": "Key reused with different inputs", "code": "idempotency_conflict"})
            return self._send(200, previous[1])
        payload = call.json
        if path == "/v1/tims/batches":
            capsule_set = next(s for s in self.state.capsule_sets if s["id"] == payload["capsule_set_id"])
            batch = {**payload, "id": f"batch-{len(self.state.batches)+1}", "lab_id": capsule_set["lab_id"]}
            self.state.batches[batch["id"]] = batch
            result = self._batch_detail(batch)
        else:
            name = payload["name"].lower()
            detail = ""
            active = [f for f in self.state.fractions if not f.get("retired_at")]
            if not re.fullmatch(r"[a-z]{1,3}[0-9]{1,3}[a-z]?", name):
                detail = "Invalid fraction name"
            if any(f["sample"]["id"] == payload["sample_id"] and f["name"] == name for f in active):
                detail = "Duplicate fraction name"
            if payload.get("batch_id") and any(f.get("batch_id") == payload["batch_id"] and f.get("capsule_number") == payload.get("capsule_number") for f in active):
                detail = "Capsule is taken"
            for source in payload.get("sources", []):
                if source.get("is_fragment") and not source.get("fragment_note", "").strip():
                    detail = "Fragment note required"
                for fraction in active:
                    for old in fraction["sources"]:
                        if source.get("grain_id") and old.get("grain_id") == source["grain_id"] and not (source.get("is_fragment") and old.get("is_fragment")):
                            detail = "Grain already plucked"
            if detail:
                return self._send(422, {"detail": detail, "code": "validation_failed"})
            batch = self.state.batches.get(payload.get("batch_id"))
            capsule_set = next((s for s in self.state.capsule_sets if batch and s["id"] == batch["capsule_set_id"]), None)
            result = {**payload, "id": f"fraction-{len(self.state.fractions)+1}", "name": name,
                      "sample": {"id": payload["sample_id"], "name": "Synthetic"},
                      "capsule_label": f"{capsule_set['code']}{payload['capsule_number']}" if capsule_set else None,
                      "capsule_set_name": capsule_set["name"] if capsule_set else None}
            self.state.fractions.append(result)
        self.state.tims_writes[identity] = (payload, result)
        self._send(200, result)

    def _oversight_allowed(self, session_id: str) -> bool:
        owner = self.state.session_owners.get(session_id, self.state.username)
        if (owner != self.state.username
                and self.state.explicit_roles.get(session_id, "").lower() not in {"write", "admin"}
                and self.headers.get("X-UPbDB-Oversight") != "on-behalf"):
            self._send(403, {"code": "oversight_confirm", "owner": owner})
            return False
        return True

    def _session_out(self, session_id, folder: Path) -> dict:
        return {"id": session_id, "session_key": folder.name, "material": "apatite",
                "kind": "u_pb", "status": "ingested", "started_at": "2025-03-20T21:00:00Z",
                "acquisition_count": len(list((folder / "raw").glob("*.csv")))}

    def _import(self, session_id: str, call: Call):
        folder = self.state.sessions.get(session_id)
        if folder is None:
            return self._send(404, {"detail": "Not Found"})
        key = self.headers.get("Idempotency-Key")
        identity = (session_id, key)
        body_hash = hashlib.sha256(call.body).hexdigest()
        if key and identity in self.state.idempotent_imports:
            previous_hash, payload = self.state.idempotent_imports[identity]
            if body_hash != previous_hash:
                return self._send(409, {"code": "idempotency_conflict"})
            return self._send(201, payload, headers={"Idempotent-Replayed": "true"})
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
        payload = {
            "run_id": run_id,
            "counts": {"input_files": len(hashes), "spot_results": spot_rows},
            "quarantine": [{"kind": "unmatched_input", "sha256": h} for h in unmatched],
        }
        if key:
            self.state.idempotent_imports[identity] = (body_hash, payload)
        self._send(201, payload)


class FakeUpbdb:
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

    def __enter__(self) -> "FakeUpbdb":
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
    with FakeUpbdb(state, port=args.port) as fake:
        code = state.issue_code()
        print(f"fake UPbDB at {fake.url}; user {state.username!r} / {state.password!r}")
        for session_id in state.sessions:
            print(f"  upbr://session/{session_id}?server={fake.url}&code={code}")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":  # pragma: no cover
    main()


class InProcessUpbdb:
    """Use the same fake routes through a requests-like transport, without ports."""

    url = "https://lab.example.org"

    def __init__(self, state=None):
        self.state = state or FakeState()
        self.responses = []

    def transport(self, request=None):
        """Supply the client adapter without installing requests or keyring."""
        from types import SimpleNamespace
        return SimpleNamespace(
            request=request or self.request,
            exceptions=SimpleNamespace(Timeout=TimeoutError, RequestException=ConnectionError),
        )

    def request(self, method, url, **kwargs):
        from email.message import Message

        class Route(Handler):
            def _send(route, status, payload=None, *, headers=None, raw=None):
                route.reply = MemoryReply(status, payload, headers, raw)

        route = object.__new__(Route)
        route.state = self.state
        route.command = method
        parts = urlsplit(url)
        route.path = parts.path + ("?" + parts.query if parts.query else "")
        if kwargs.get("params"):
            route.path += ("&" if "?" in route.path else "?") + urlencode(kwargs["params"])
        route.headers = Message()
        for key, value in kwargs.get("headers", {}).items():
            route.headers[key] = value
        body = kwargs.get("data", b"")
        if "json" in kwargs:
            body = json.dumps(kwargs["json"]).encode()
            route.headers["Content-Type"] = "application/json"
        if hasattr(body, "read"):
            body = body.read()
        route.headers["Content-Length"] = str(len(body))
        route.rfile = io.BytesIO(body)
        getattr(route, "do_" + method)()
        self.responses.append(route.reply)
        return route.reply


class MemoryReply:
    def __init__(self, status, payload=None, headers=None, raw=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = raw if raw is not None else json.dumps(payload).encode()
        self.text = self.content.decode(errors="replace")
        self.body_reads = 0
        self.closed = False

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size):
        self.body_reads += 1
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True
