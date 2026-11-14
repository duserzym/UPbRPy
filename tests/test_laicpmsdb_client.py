"""UPbRPy.integration.laicpmsdb against a fake laicpmsdb (http.server)."""

from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

pytest.importorskip("requests")

from UPbRPy.integration import laicpmsdb as db
from UPbRPy.project import read_reduction_project
from UPbRPy.session_bundle import find_cached_session, read_bundle_record

from fake_laicpmsdb import FakeLaicpmsdb, FakeState
from synthetic_bundles import sha256, write_session_bundle


SESSION_ID = "0192f0c4-0000-7000-8000-000000000001"


@pytest.fixture()
def tokens():
    store = db.MemoryTokenStore()
    previous = db.set_token_store(store)
    yield store
    db.set_token_store(previous)


@pytest.fixture()
def fake(tmp_path, tokens):
    source = tmp_path / "server_bundles"
    bundle = write_session_bundle(source, session_id=SESSION_ID)
    state = FakeState(sessions={SESSION_ID: bundle})
    with FakeLaicpmsdb(state) as server:
        yield server


def reduction_bundle(tmp_path: Path, raw: Path) -> Path:
    """A minimal reduction bundle folder whose manifest hashes ``raw``."""
    folder = tmp_path / "reduction"
    folder.mkdir()
    files = [{"path": str(p), "sha256": sha256(p)} for p in sorted(raw.glob("*.csv"))]
    (folder / "reduction_manifest.json").write_text(
        json.dumps({"input_files": files, "metadata": {}}), encoding="utf-8"
    )
    (folder / "spot_mean_results.csv").write_text("spot_names,age\nA,1\nB,2\n")
    (folder / "selection_windows.csv").write_text("spot_names\nA\nB\n")
    return folder


def test_normalize_server():
    assert db.normalize_server("lab.example.org/") == "https://lab.example.org"
    assert db.normalize_server("localhost:8010") == "http://localhost:8010"
    assert db.normalize_server("HTTP://Lab.Example.org/db/v1/") == "http://lab.example.org/db"
    with pytest.raises(db.DatabaseError):
        db.normalize_server("ftp://x")
    with pytest.raises(db.DatabaseError):
        db.normalize_server("")


def test_password_login_whoami_and_user_agent(fake, tokens):
    with pytest.raises(db.AuthenticationRequired):
        db.whoami(fake.url)
    with pytest.raises(db.AuthenticationRequired, match="Invalid username"):
        db.login(fake.url, "analyst", "wrong")
    token = db.login(fake.url, "analyst", "correct horse")
    assert tokens.get(fake.url) == token
    assert db.whoami(fake.url)["username"] == "analyst"
    call = fake.state.calls[-1]
    assert call.headers["User-Agent"].startswith("UPbRPy-Studio/")
    assert call.headers["Authorization"] == f"Bearer {token}"


def test_exchange_code_is_single_use_and_stores_token(fake, tokens):
    code = fake.state.issue_code()
    token = db.exchange_code(fake.url, code)
    assert db.stored_token(fake.url + "/") == token
    with pytest.raises(db.AuthenticationRequired, match="expired"):
        db.exchange_code(fake.url, code)


def test_sign_in_url_and_providers(fake):
    url = db.sign_in_url(fake.url)
    assert url == f"{fake.url}/v1/auth/orcid/start?next=upbr%3A%2F%2Fauth%2Fcallback"
    assert db.providers(fake.url)["orcid"] is True


def test_stale_token_is_forgotten_on_401(fake, tokens):
    db.store_token(fake.url, "lcp_revoked")
    with pytest.raises(db.AuthenticationRequired):
        db.list_sessions(fake.url)
    assert tokens.get(fake.url) is None


def test_fetch_unpack_verify_and_cache(fake, tokens, tmp_path):
    db.exchange_code(fake.url, fake.state.issue_code())
    sessions = db.list_sessions(fake.url)
    assert [s["session_key"] for s in sessions] == ["Synthetic_20Mar25"]

    cache = tmp_path / "cache"
    bundle = db.fetch_session_bundle(fake.url, SESSION_ID, cache)
    assert Path(bundle) == cache / "Synthetic_20Mar25"
    assert (bundle.raw_directory / "Synthetic_1.csv").is_file()
    assert bundle.project_file == bundle.path / "Synthetic_20Mar25.upbr"
    assert not bundle.reused
    manifest_hash = sha256(bundle.path / "session_manifest.toml")
    assert bundle.export_sha256 == manifest_hash
    origin = read_reduction_project(bundle.project_file)["origin"]
    assert origin["server"] == fake.url
    assert origin["session_id"] == SESSION_ID
    assert origin["export_sha256"] == manifest_hash
    assert read_bundle_record(bundle.path)["zip_sha256"] == bundle.zip_sha256
    assert find_cached_session(SESSION_ID, cache) == bundle.path

    # Same export: the cached folder, with the user's edits, is kept.
    note = bundle.path / "my_notes.txt"
    note.write_text("keep me")
    again = db.fetch_session_bundle(fake.url, SESSION_ID, cache)
    assert again.reused and note.read_text() == "keep me"

    # Changed export: the old folder is moved aside, never overwritten.
    server_folder = fake.state.sessions[SESSION_ID]
    manifest = server_folder / "session_manifest.toml"
    manifest.write_text(manifest.read_text() + "\n# changed\n")
    newer = db.fetch_session_bundle(fake.url, SESSION_ID, cache)
    assert not newer.reused
    assert newer.superseded is not None and (newer.superseded / "my_notes.txt").is_file()
    assert not (newer.path / "my_notes.txt").exists()


def test_fetch_errors_are_typed(fake, tokens, tmp_path):
    db.exchange_code(fake.url, fake.state.issue_code())
    with pytest.raises(db.NotFound):
        db.fetch_session_bundle(fake.url, "missing", tmp_path)
    fake.state.fail_next[r"/v1/sessions/.*/export/upbrpy"] = (403, "not your session")
    with pytest.raises(db.PermissionDenied, match="not your session"):
        db.fetch_session_bundle(fake.url, SESSION_ID, tmp_path)
    fake.state.fail_next[r"/v1/sessions"] = (500, "boom")
    with pytest.raises(db.ServerError, match="had a problem") as failed:
        db.list_sessions(fake.url)
    assert failed.value.status == 500 and "boom" in failed.value.details
    assert "boom" not in str(failed.value) and "HTTP" not in str(failed.value)
    with pytest.raises(db.ConnectionFailed, match="Cannot reach"):
        db.login("http://127.0.0.1:9", "a", "b")


def test_submit_and_prefer(fake, tokens, tmp_path):
    db.exchange_code(fake.url, fake.state.issue_code())
    bundle = db.fetch_session_bundle(fake.url, SESSION_ID, tmp_path / "cache")
    folder = reduction_bundle(tmp_path, bundle.raw_directory)

    result = db.submit_reduction(fake.url, SESSION_ID, folder)
    assert result.run_id == "run-1" and result["run_id"] == "run-1"
    assert result.counts == {"input_files": 4, "spot_results": 2}
    assert result.quarantine_items == []
    session_id, supersedes, data, manifest = fake.state.imports[-1]
    assert session_id == SESSION_ID and supersedes is None
    names = zipfile.ZipFile(__import__("io").BytesIO(data)).namelist()
    assert "reduction_manifest.json" in names  # files at the zip root
    upload = fake.state.calls[-1]
    assert upload.form["bundle"][0] == "reduction.zip"

    second = db.submit_reduction(fake.url, SESSION_ID, folder, supersedes="run-1")
    assert fake.state.imports[-1][1] == "run-1" and second.run_id == "run-2"

    assert db.prefer_run(fake.url, SESSION_ID, "run-2")["preferred_run_id"] == "run-2"
    assert fake.state.preferred == {SESSION_ID: "run-2"}
    assert db.get_run(fake.url, "run-2")["session_id"] == SESSION_ID
    with pytest.raises(db.NotFound):
        db.prefer_run(fake.url, SESSION_ID, "run-99")


def test_submit_rejects_folder_without_manifest(tmp_path, tokens):
    with pytest.raises(db.BundleError, match="reduction_manifest.json"):
        db.submit_reduction("http://127.0.0.1:9", SESSION_ID, tmp_path)


def test_quarantine_report_is_passed_through(fake, tokens, tmp_path):
    db.exchange_code(fake.url, fake.state.issue_code())
    raw = tmp_path / "other_raw"
    raw.mkdir()
    (raw / "stray.csv").write_text("not from this session")
    result = db.submit_reduction(fake.url, SESSION_ID, reduction_bundle(tmp_path, raw))
    assert result.quarantine_items[0]["kind"] == "unmatched_input"


def test_sign_out_revokes_and_forgets(fake, tokens):
    token = db.login(fake.url, "analyst", "correct horse")
    db.sign_out(fake.url)
    assert token not in fake.state.tokens and tokens.get(fake.url) is None


def test_keyring_store_uses_service_upbrpy():
    keyring = pytest.importorskip("keyring")
    from keyring.backend import KeyringBackend

    class MemoryKeyring(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.items = {}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            self.items.pop((service, username), None)

    backend = MemoryKeyring()
    previous_backend = keyring.get_keyring()
    keyring.set_keyring(backend)
    previous_store = db.set_token_store(db.KeyringTokenStore())
    try:
        db.store_token("lab.example.org", "lcp_x")
        assert backend.items == {("UPbRPy", "https://lab.example.org"): "lcp_x"}
        assert db.stored_token("https://lab.example.org/") == "lcp_x"
        db.forget_token("lab.example.org")
        assert backend.items == {}
    finally:
        db.set_token_store(previous_store)
        keyring.set_keyring(previous_backend)


class _Reply:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_error_replies_are_short_sentences_with_codes():
    problems = [
        {"kind": "spot", "id": "x", "label": "Z1", "code": "spot_outside_grain",
         "reason": "Spot Z1 lies outside grain a/7.", "related": []},
        {"kind": "spot", "id": "y", "label": "Z2", "code": "spot_outside_grain",
         "reason": "Spot Z2 lies outside grain a/7.", "related": []},
        {"kind": "grain", "id": "z", "label": "a/9", "code": "duplicate_label",
         "reason": "Grain label a/9 is used twice.", "related": []},
    ]
    with pytest.raises(db.RequestRejected) as rejected:
        db._raise_for(_Reply(422, {"detail": ["spot Z1 is outside grain a/7"] * 3,
                                   "problems": problems, "code": "validation_failed"}),
                      "https://lab.example", "sync")
    text = str(rejected.value)
    assert "Spot Z1 lies outside grain a/7.; Spot Z2" in text and text.endswith("and 1 more")
    assert rejected.value.code == "validation_failed" and "https://" not in text
    with pytest.raises(db.RequestRejected, match="3 items could not be synced"):
        db._raise_for(_Reply(422, {"detail": ["a", "b", "c"], "message": "3 items could not be synced."}),
                      "https://lab.example", "sync")
    with pytest.raises(db.RequestRejected) as conflict:
        db._raise_for(_Reply(409, {"detail": "This mount changed in another window.",
                                   "code": "revision_conflict"}), "https://lab.example", "sync")
    assert conflict.value.code == "revision_conflict"
    with pytest.raises(db.ServerError) as html:
        db._raise_for(_Reply(502, text="<html><body>Bad gateway</body></html>"),
                      "https://lab.example", "list sessions")
    assert "html" not in str(html.value) and "Bad gateway" in html.value.details
    duplicated = [{"msg": "field required", "loc": ["body", "name"]}] * 2
    assert db._detail(_Reply(422, {"detail": duplicated})) == "name: field required"
