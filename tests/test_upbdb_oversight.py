"""Oversight transport, owner errors and idempotent confirmation replay."""

import hashlib
from email.message import Message
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from UPbRPy.integration import upbdb as db
from fake_upbdb import FakeState, Handler
from synthetic_bundles import write_session_bundle
from test_upbdb_retry import Reply, transport  # noqa: F401


OWNER = "Synthetic Owner"
SESSION_ID = "session-oversight"


def write_reduction(folder, raw):
    folder = Path(folder)
    folder.mkdir(parents=True)
    files = [{"sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in raw.glob("*.csv")]
    (folder / "reduction_manifest.json").write_text(json.dumps({"input_files": files}))
    return folder


@pytest.fixture
def oversight_server(tmp_path, monkeypatch):
    """Dispatch fake HTTP handlers without sockets or optional requests/keyring."""
    source = write_session_bundle(tmp_path / "server", session_id=SESSION_ID)
    state = FakeState(sessions={SESSION_ID: source}, session_owners={SESSION_ID: OWNER})
    previous = db.set_token_store(db.MemoryTokenStore())
    timings = []

    def request(method, url, *, headers, data=None, **kwargs):
        parsed = urlsplit(url)
        body = data.read() if hasattr(data, "read") else data
        handler = Handler.__new__(Handler)
        handler.state = state
        handler.command, handler.path = method, parsed.path
        handler.headers = Message()
        for name, value in headers.items():
            handler.headers[name] = value
        handler.headers["Content-Length"] = str(len(body or b""))
        handler.rfile = io.BytesIO(body or b"")
        replies = []
        handler._send = lambda status, payload=None, headers=None, **kwargs: replies.append(
            Reply(status, payload, headers))
        started = time.perf_counter()
        try:
            getattr(handler, "do_" + method)()
            assert len(replies) == 1
            return replies[0]
        finally:
            timings.append(time.perf_counter() - started)

    adapter = SimpleNamespace(request=request,
                              exceptions=SimpleNamespace(Timeout=TimeoutError, RequestException=OSError))
    monkeypatch.setattr(db, "_requests", lambda: adapter)
    try:
        server = SimpleNamespace(url="http://127.0.0.1:8765")
        db.store_token(server.url, state.issue_token())
        yield SimpleNamespace(server=server, state=state, source=source, adapter=adapter,
                              timings=timings)
    finally:
        db.set_token_store(previous)


@pytest.mark.parametrize("key", [None, "stable-key"])
@pytest.mark.parametrize("on_behalf", [False, True])
def test_header_is_opt_in_for_both_writes(transport, tmp_path, key, on_behalf):
    calls, _reply = transport
    folder = tmp_path / "bundle"
    folder.mkdir()
    (folder / "reduction_manifest.json").write_text("{}")
    db.submit_reduction("https://lab.example.org", "session", folder,
                        idempotency_key=key, on_behalf=on_behalf)
    db.prefer_run("https://lab.example.org", "session", "run", idempotency_key=key,
                  on_behalf=on_behalf)
    assert len(calls) == 2
    for _method, _url, kwargs in calls:
        assert kwargs["headers"].get("X-UPbDB-Oversight") == ("on-behalf" if on_behalf else None)
        assert kwargs["headers"].get("Idempotency-Key") == key


def test_confirm_and_replay_create_one_run(oversight_server, tmp_path):
    case = oversight_server
    folder = write_reduction(tmp_path / "reduction", case.source / "raw")
    with pytest.raises(db.OversightConfirmationRequired) as raised:
        db.submit_reduction(case.server.url, SESSION_ID, folder, idempotency_key="same-key")
    assert raised.value.owner == OWNER
    assert raised.value.status == 403 and raised.value.code == "oversight_confirm"
    assert OWNER in raised.value.details
    assert not case.state.runs
    for _ in range(2):
        result = db.submit_reduction(case.server.url, SESSION_ID, folder,
                                     idempotency_key="same-key", on_behalf=True)
        assert result.run_id == "run-1"
    assert len(case.state.runs) == len(case.state.imports) == 1
    assert len({c.headers["Idempotency-Key"] for c in case.state.calls}) == 1
    assert len({c.body for c in case.state.calls}) == 1
    assert [c.headers.get("X-UPbDB-Oversight") for c in case.state.calls] == [None, "on-behalf", "on-behalf"]
    print(f"On-behalf submit: 3 requests including replay, 1 run; {sum(case.timings) * 1000:.2f} ms.")
    with pytest.raises(db.OversightConfirmationRequired) as raised:
        db.prefer_run(case.server.url, SESSION_ID, "run-1")
    assert raised.value.owner == OWNER and not case.state.preferred
    db.prefer_run(case.server.url, SESSION_ID, "run-1", on_behalf=True)
    assert case.state.preferred == {SESSION_ID: "run-1"}


def test_other_403_remains_permission_denied(transport):
    _calls, reply = transport
    reply.status_code = 403
    reply.payload = {"code": "view_as_read_only", "owner": OWNER}
    with pytest.raises(db.PermissionDenied) as raised:
        db.prefer_run("https://lab.example.org", "session", "run")
    assert not isinstance(raised.value, db.OversightConfirmationRequired)


def test_engine_imports_without_http_or_qt():
    script = """
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'requests', 'httpx', 'keyring', 'PySide6', 'PyQt6', 'pyqtgraph', 'GUI'}:
            raise AssertionError('Engine imported ' + fullname)
        if fullname.startswith('UPbRPy.integration'):
            raise AssertionError('Engine imported ' + fullname)
sys.meta_path.insert(0, Block())
import UPbRPy
from UPbRPy import pipeline, reproducibility, project, session_bundle, qtegra
"""
    subprocess.run([sys.executable, "-c", script], check=True, cwd=Path(__file__).resolve().parents[1])
