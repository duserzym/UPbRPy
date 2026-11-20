"""Session-bundle zips without HTTP (no [database] extra needed)."""

from __future__ import annotations

import zipfile

import pytest

from UPbRPy.integration import upbdb as db
from UPbRPy.project import read_reduction_project
from UPbRPy.session_bundle import (
    SessionBundleError,
    find_cached_session,
    find_project_for_run,
    unpack_session_bundle,
)

from synthetic_bundles import write_session_bundle, zip_directory


@pytest.mark.parametrize("format_name", ["upbdb-session-bundle", "other-session-bundle"])
def test_session_bundle_format(format_name, tmp_path):
    source = write_session_bundle(tmp_path / "src")
    manifest = source / "session_manifest.toml"
    manifest.write_text(f'format = "{format_name}"\n' + manifest.read_text())
    if format_name == "upbdb-session-bundle":
        assert unpack_session_bundle(zip_directory(source), tmp_path / "cache").path.is_dir()
    else:
        with pytest.raises(SessionBundleError, match="Unsupported session bundle format"):
            unpack_session_bundle(zip_directory(source), tmp_path / "cache")


def test_local_zip_unpacks_like_a_download(tmp_path):
    source = write_session_bundle(tmp_path / "src", server="https://lab.example.org")
    archive = tmp_path / "Synthetic_20Mar25.zip"
    archive.write_bytes(zip_directory(source))
    bundle = unpack_session_bundle(archive, tmp_path / "cache")
    assert bundle.path == tmp_path / "cache" / "Synthetic_20Mar25"
    origin = read_reduction_project(bundle.project_file)["origin"]
    # No server given: the bundle's own origin server is kept.
    assert origin["server"] == "https://lab.example.org"
    assert origin["export_sha256"] == bundle.export_sha256
    assert find_cached_session(bundle.session_id, tmp_path / "cache") == bundle.path
    assert find_project_for_run("run-1", tmp_path / "cache") is None


def test_damaged_raw_file_is_rejected(tmp_path):
    bundle = write_session_bundle(tmp_path / "src")
    (bundle / "raw" / "Synthetic_1.csv").write_text("tampered")
    with pytest.raises(SessionBundleError, match="does not match"):
        unpack_session_bundle(zip_directory(bundle), tmp_path / "cache")
    with pytest.raises(SessionBundleError, match="not a zip"):
        unpack_session_bundle(b"not a zip", tmp_path / "cache")


def test_unsafe_zip_paths_are_rejected(tmp_path):
    target = tmp_path / "evil.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("key/../../escape.txt", "x")
    with pytest.raises(SessionBundleError, match="Unsafe"):
        unpack_session_bundle(target, tmp_path / "cache")


def test_availability_reports_missing_extra(monkeypatch):
    real_import = __import__("importlib").import_module

    def fake_import(name, *args):
        if name == "keyring":
            raise ImportError(name)
        return real_import(name, *args)

    monkeypatch.setattr(db.importlib, "import_module", fake_import)
    ok, reason = db.availability()
    assert not ok and "keyring" in reason and "UPbRPy[database]" in reason


def test_client_module_imports_without_the_extra():
    # The module itself never needs requests at import time.
    ok, reason = db.availability()
    assert ok == (not db.missing_requirements())
    assert bool(reason) != ok


@pytest.mark.parametrize("runs, expected", [
    ([], None),
    ([{"id": "pending", "status": "running", "is_preferred_for_session": True}], None),
    ([{"id": "old", "status": "completed", "started_at": "2026-01-01T00:00:00Z",
       "is_preferred_for_session": True},
      {"id": "new", "status": "complete", "started_at": "2026-02-01T00:00:00Z"}], "old"),
    ([{"id": "old", "status": "completed", "started_at": "2026-01-01T00:00:00Z"},
      {"id": "new", "status": "complete", "started_at": "2026-02-01T00:00:00Z"},
      {"id": "pending", "status": "running", "started_at": "2027-01-01T00:00:00Z",
       "is_preferred_for_session": True}], "new"),
    ([{"id": "old", "status": "complete", "started_at": "2026-01-01T01:00:00+02:00"},
      {"id": "new", "status": "complete", "started_at": "2026-01-01T00:00:00Z"}], "new"),
])
def test_pick_session_run(runs, expected):
    from UPbRPy.session_bundle import pick_session_run

    chosen = pick_session_run(runs)
    assert (chosen["id"] if chosen else None) == expected


def test_install_run_project_rebases_dependencies_and_preserves_edits(tmp_path):
    import json
    from UPbRPy.project import resolve_project_path
    from UPbRPy.session_bundle import install_run_project

    source = write_session_bundle(tmp_path / "submitter")
    document = read_reduction_project(source / f"{source.name}.upbr")
    document["paths"]["source_directory"] = {"relative": "../../submitter/raw", "absolute": "/old/raw"}
    document["paths"]["reference_library"] = {"relative": "missing.json", "absolute": "/missing/library.json"}
    document["origin"].update(run_id="superseded-run", export_sha256="old-export", custom="kept")
    document["selections"] = [{"spot_names": "A", "plateau_start_times": 12}]
    folder = tmp_path / "cache"
    (folder / "raw").mkdir(parents=True)
    fields = dict(server="https://lab.example.org", session_id="session-id",
                  session_key="session-key", export_sha256="current-export")
    result = install_run_project(folder, "full-run-id", json.dumps(document).encode(), **fields)
    assert result.path == folder / "session-key.run-full-run-id.upbr" and result.created
    loaded = read_reduction_project(result.path)
    assert loaded["paths"]["source_directory"] == {"relative": "raw", "absolute": str(folder / "raw")}
    assert resolve_project_path(result.path, loaded["paths"]["source_directory"], label="raw") == folder / "raw"
    assert loaded["paths"]["reference_library"] is None
    assert loaded["selections"] == document["selections"]
    assert loaded["origin"] == {**document["origin"], **fields, "run_id": "full-run-id"}
    assert loaded["format_version"] >= 2
    assert any("earlier export" in note for note in result.notes)
    assert any("built-in library" in note for note in result.notes)
    result.path.write_text("local edits")
    again = install_run_project(folder, "full-run-id", b"invalid", **fields)
    assert not again.created and again.path.read_text() == "local edits"
    assert not list(folder.glob("*.tmp"))


@pytest.mark.parametrize("dependency", ["relative", "absolute", None])
def test_install_run_project_keeps_resolvable_reference_library(tmp_path, dependency):
    import json
    from UPbRPy.session_bundle import install_run_project

    folder = write_session_bundle(tmp_path)
    document = read_reduction_project(folder / f"{folder.name}.upbr")
    document["format_version"] = 1
    library = folder / "library.json"
    library.write_text("{}")
    reference = {dependency: "library.json" if dependency == "relative" else str(library)} if dependency else None
    document["paths"]["reference_library"] = reference
    result = install_run_project(folder, "run-1", json.dumps(document).encode(),
                                 server="https://lab.example.org", session_id="s",
                                 session_key=folder.name, export_sha256="current")
    loaded = read_reduction_project(result.path)
    assert loaded["paths"]["reference_library"] == reference and not result.notes
    assert loaded["format_version"] == 2


@pytest.mark.parametrize("data", [b"not json", b"[]", b"{}", b'{"format_version": null}', b"\xff"])
def test_install_run_project_rejects_invalid_project(tmp_path, data):
    from UPbRPy.session_bundle import install_run_project

    with pytest.raises(SessionBundleError, match="Invalid saved UPbRPy reduction project"):
        install_run_project(tmp_path, "run-1", data, server="https://lab.example.org",
                            session_id="s", session_key="key", export_sha256="current")
    assert not list(tmp_path.glob("*.upbr"))


def test_find_run_project_prefers_live_cache(tmp_path):
    import json

    old = tmp_path / "aaa.superseded-20261101"
    live = tmp_path / "zzz"
    for folder in (old, live):
        folder.mkdir()
        (folder / "run.upbr").write_text(json.dumps({"origin": {"run_id": "run-1"}}))
    assert find_project_for_run("run-1", tmp_path) == live / "run.upbr"
    (live / "run.upbr").unlink()
    assert find_project_for_run("run-1", tmp_path) == old / "run.upbr"


@pytest.mark.parametrize("document, expected", [
    ({"selections": [], "origin": None}, False),
    ({"selections": [{"spot_names": "A"}]}, True),
    ({"origin": {"run_id": "run-1"}}, True),
    ({"origin": {"session_id": "s"}}, False),
])
def test_holds_reduced_work(tmp_path, document, expected):
    import json
    from UPbRPy.session_bundle import holds_reduced_work

    assert not holds_reduced_work(tmp_path)
    (tmp_path / "broken.upbr").write_text("invalid json")
    (tmp_path / "project.upbr").write_text(json.dumps(document))
    assert holds_reduced_work(tmp_path) is expected


def test_shared_raw_file_is_hashed_once_and_conflicting_row_rejected(tmp_path, monkeypatch):
    from collections import Counter
    from UPbRPy import session_bundle as bundles
    from synthetic_bundles import manifest_toml

    source = write_session_bundle(tmp_path / "src")
    raw = source / "raw" / "Synthetic_1.csv"
    manifest = source / "session_manifest.toml"
    text = manifest_toml(session_id="session", session_key=source.name,
                         raw_paths=[raw] * 300, labels=[f"A_{i}" for i in range(300)])
    manifest.write_text(text)
    original = bundles._sha256_file
    calls = Counter()

    def hashed(path):
        calls[path.name] += 1
        return original(path)

    monkeypatch.setattr(bundles, "_sha256_file", hashed)
    unpack_session_bundle(zip_directory(source), tmp_path / "cache")
    assert calls[raw.name] == 1
    # A second digest for the same filename must retain the existing mismatch message.
    digest = original(raw)
    before, after = text.rsplit(f'sha256 = "{digest}"', 1)
    manifest.write_text(before + f'sha256 = "{"0" * 64}"' + after)
    with pytest.raises(SessionBundleError, match="does not match the SHA-256"):
        unpack_session_bundle(zip_directory(source), tmp_path / "bad")
    assert not list((tmp_path / "bad").glob(".unpack-*"))


def test_path_unpack_streams_and_extracts_once(tmp_path, monkeypatch):
    from UPbRPy import session_bundle as bundles
    source = write_session_bundle(tmp_path / "src")
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(zip_directory(source))
    digest = bundles._sha256_file(archive)
    original_read = type(archive).read_bytes

    def read(path):
        assert path != archive, "zip must not be read into memory"
        return original_read(path)

    monkeypatch.setattr(type(archive), "read_bytes", read)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda *_: pytest.fail("second decompression"))
    result = unpack_session_bundle(archive, tmp_path / "cache", zip_sha256=digest)
    assert result.zip_sha256 == digest


def test_crc_failure_removes_staging_and_preserves_installed_folder(tmp_path):
    import io
    source = write_session_bundle(tmp_path / "src")
    good = zip_directory(source)
    installed = unpack_session_bundle(good, tmp_path / "cache")
    # ZIP_STORED lets us corrupt member bytes without damaging directory metadata.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, source.name + "/" + str(path.relative_to(source)))
    damaged = bytearray(buffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(damaged)) as archive:
        info = next(i for i in archive.infolist() if "/raw/" in i.filename)
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        damaged[offset] ^= 1
    with pytest.raises(SessionBundleError, match="zip is damaged at"):
        unpack_session_bundle(bytes(damaged), tmp_path / "cache")
    assert installed.project_file.is_file()
    assert not list((tmp_path / "cache").glob(".unpack-*"))
