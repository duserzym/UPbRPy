"""Offline transport tests for stable write identities and bounded lists."""
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import pytest

from UPbRPy.integration import upbdb as db

SERVER = "https://lab.example.org"


class Reply:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self.payload


@pytest.fixture
def transport(monkeypatch):
    previous = db.set_token_store(db.MemoryTokenStore())
    db.store_token(SERVER, "token-a")
    calls = []
    reply = Reply(payload={"run_id": "run-1"}, headers={"Idempotent-Replayed": "true"})

    def request(method, url, **kwargs):
        data = kwargs.get("data")
        if hasattr(data, "read"):
            kwargs["body_sha256"] = hashlib.sha256(data.read()).hexdigest()
        calls.append((method, url, kwargs))
        return reply

    monkeypatch.setattr(db, "_requests", lambda: SimpleNamespace(request=request))
    yield calls, reply
    db.forget_token(SERVER)
    db.set_token_store(previous)


def test_write_retries_have_identical_keys_and_body_and_accept_replay(transport, tmp_path):
    calls, _reply = transport
    folder = tmp_path / "reduction"
    folder.mkdir()
    (folder / "reduction_manifest.json").write_text("{}")
    (folder / "results.csv").write_text("spot,value\nA,1\n")
    key = str(uuid.uuid4())
    for _ in range(3):
        result = db.submit_reduction(SERVER, "session-1", folder, supersedes="old", idempotency_key=key)
        assert result.run_id == "run-1"
    assert len(calls) == 3
    assert {c[2]["headers"]["Idempotency-Key"] for c in calls} == {key}
    assert len({c[2]["body_sha256"] for c in calls}) == 1
    assert all(c[2]["timeout"] == (db.CONNECT_TIMEOUT, db.READ_TIMEOUT) for c in calls)
    assert all(c[2]["headers"]["Accept-Encoding"] == "gzip" for c in calls)
    db.prefer_run(SERVER, "session-1", "run-1", idempotency_key="prefer-key")
    assert calls[-1][2]["headers"]["Idempotency-Key"] == "prefer-key"


@pytest.mark.parametrize("delay, expected", [("17", 17), ("invalid", None)])
def test_in_progress_retains_retry_after_and_type(transport, delay, expected):
    _calls, reply = transport
    reply.status_code = 409
    reply.payload = {"code": "idempotency_in_progress", "detail": "Already processing"}
    reply.text = json.dumps(reply.payload)
    reply.headers["Retry-After"] = delay
    with pytest.raises(db.RequestRejected) as raised:
        db.prefer_run(SERVER, "session-1", "run-1", idempotency_key="key")
    assert raised.value.status == 409
    assert raised.value.code == "idempotency_in_progress"
    assert raised.value.retry_after == expected


def test_large_list_pages_revalidate_and_isolate_accounts(transport, monkeypatch, capsys):
    calls, _reply = transport
    rows = [{"id": str(i), "session_key": f"Session-{i}"} for i in range(5000)]
    downloaded = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert kwargs["params"]["limit"] == 100
        start = int(kwargs["params"].get("cursor", "0"))
        tag = f'"page-{start}"'
        if kwargs["headers"].get("If-None-Match") == tag:
            return Reply(status=304)
        page = rows[start:start + 100]
        downloaded.extend(page)
        return Reply(payload={"items": page}, headers={"ETag": tag, "X-Next-Cursor": str(start + 100)})

    monkeypatch.setattr(db, "_requests", lambda: SimpleNamespace(request=request))
    baseline_start = time.perf_counter()
    baseline = [dict(row) for row in rows]
    baseline_time = time.perf_counter() - baseline_start
    start = time.perf_counter()
    page = db.list_sessions(SERVER)
    page_time = time.perf_counter() - start
    assert len(page) == 100 and page.next_cursor == "100"
    assert len(calls) == 1 and len(downloaded) == 100
    assert db.list_sessions(SERVER) == page
    assert len(downloaded) == 100  # 304 did not download again
    more = db.list_sessions(SERVER, cursor=page.next_cursor)
    assert more[0]["id"] == "100" and len(downloaded) == 200
    db.store_token(SERVER, "token-b")
    db.list_sessions(SERVER)
    assert "If-None-Match" not in calls[-1][2]["headers"]
    db.forget_token(SERVER)
    db.store_token(SERVER, "token-a")
    db.list_sessions(SERVER)
    assert "If-None-Match" not in calls[-1][2]["headers"]
    assert len(baseline) == 5000
    print(f"Mock list baseline: 1 call, 5000 rows, {baseline_time * 1000:.3f} ms; "
          f"paged: 1 call, 100 rows, {page_time * 1000:.3f} ms; "
          "revalidation: 1 call, 0 rows transferred; no per-row requests.")
