"""Bundle revalidation over the real fake routes, with no sockets."""
from collections import Counter
import time

import pytest

from UPbRPy.integration import upbdb as db
from UPbRPy import session_bundle as bundles
from fake_upbdb import FakeState, InProcessUpbdb
from synthetic_bundles import write_session_bundle

SESSION = "session-1"


@pytest.fixture
def server(tmp_path, monkeypatch):
    source = write_session_bundle(tmp_path / "source", session_id=SESSION)
    fake = InProcessUpbdb(FakeState(sessions={SESSION: source}))
    previous = db.set_token_store(db.MemoryTokenStore())
    db.store_token(fake.url, fake.state.issue_token())
    monkeypatch.setattr(db.tls, "request", fake.request)
    monkeypatch.setattr(db, "_requests", lambda: fake.transport(db.tls.request))
    yield fake
    db.set_token_store(previous)


@pytest.mark.parametrize("conditional", [True, False])
def test_current_bundle_reuses_without_reading_body(server, tmp_path, monkeypatch, conditional):
    cache = tmp_path / "cache"
    cold = db.fetch_session_bundle(server.url, SESSION, cache)
    cold.project_file.write_text("user edits")
    record = (cold.path / bundles.BUNDLE_RECORD_FILENAME).read_bytes()
    counts = Counter()
    original = bundles._sha256_file

    def hashed(path):
        counts[path] += 1
        return original(path)

    monkeypatch.setattr(bundles, "_sha256_file", hashed)
    # Thousands of cache entries must not lead to project parsing or downloads.
    for i in range(2000):
        folder = cache / f"other-{i}"
        folder.mkdir()
        (folder / bundles.BUNDLE_RECORD_FILENAME).write_text('{"session_id":"other"}')
        (folder / "huge.upbr").write_text("must not be parsed")

    server.state.conditional_bundles = conditional
    before = time.perf_counter()
    reused = db.fetch_session_bundle(server.url, SESSION, cache)
    elapsed = time.perf_counter() - before
    assert reused.reused and reused.path == cold.path
    assert reused.project_file.read_text() == "user edits"
    assert (reused.path / bundles.BUNDLE_RECORD_FILENAME).read_bytes() == record
    assert server.responses[-1].status_code == (304 if conditional else 200)
    assert server.responses[-1].closed and server.responses[-1].body_reads == 0
    assert server.state.calls[-1].headers["If-None-Match"] == f'"{cold.export_sha256}"'
    assert all(counts[path] == 1 for path in cold.raw_directory.iterdir())
    assert len(server.state.calls) == 2
    print(f"bundle: cold=1 body, warm=0 bodies; 2001 cache folders revalidated in {elapsed:.3f}s")


@pytest.mark.parametrize("damage", ["manifest", "missing_manifest", "raw", "missing_raw"])
def test_corrupt_cache_downloads_unconditionally_and_preserves_old(server, tmp_path, damage):
    cache = tmp_path / "cache"
    cold = db.fetch_session_bundle(server.url, SESSION, cache)
    path = cold.path / "session_manifest.toml" if "manifest" in damage else next(cold.raw_directory.iterdir())
    if damage.startswith("missing"):
        path.unlink()
    else:
        path.write_bytes(b"damaged")
    fetched = db.fetch_session_bundle(server.url, SESSION, cache)
    assert not fetched.reused and fetched.superseded.is_dir()
    assert "If-None-Match" not in server.state.calls[-1].headers
    assert server.responses[-1].body_reads == 1
    assert bundles._verify_unpacked(fetched.path)[2] == cold.export_sha256


def test_changed_bundle_and_other_server_are_downloaded(server, tmp_path):
    cache = tmp_path / "cache"
    first = db.fetch_session_bundle(server.url, SESSION, cache)
    source = server.state.sessions[SESSION] / "session_manifest.toml"
    source.write_text(source.read_text() + "\n# changed export\n")
    changed = db.fetch_session_bundle(server.url, SESSION, cache)
    assert not changed.reused and changed.superseded == first.path.with_name(changed.superseded.name)
    assert server.responses[-1].body_reads == 1
    assert bundles.find_cached_session(SESSION, cache, server="https://other.example.org") is None


def test_old_server_without_hash_header_still_downloads_and_reuses(server, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    first = db.fetch_session_bundle(server.url, SESSION, cache)
    server.state.conditional_bundles = False
    original = server.request

    def without_header(*args, **kwargs):
        reply = original(*args, **kwargs)
        reply.headers.pop("X-Export-SHA256", None)
        return reply

    monkeypatch.setattr(db.tls, "request", without_header)
    second = db.fetch_session_bundle(server.url, SESSION, cache)
    assert second.reused and second.path == first.path
    assert server.responses[-1].body_reads == 1


def test_downloader_supplies_streaming_zip_digest(server, tmp_path, monkeypatch):
    original = bundles._sha256_file

    def hash_raw_only(path):
        assert path.suffix != ".zip", "downloaded zip must not be reread for hashing"
        return original(path)
    monkeypatch.setattr(bundles, "_sha256_file", hash_raw_only)
    bundle = db.fetch_session_bundle(server.url, SESSION, tmp_path / "cache")
    import hashlib
    assert bundle.zip_sha256 == hashlib.sha256(server.responses[-1].content).hexdigest()
