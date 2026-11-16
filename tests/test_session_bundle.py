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
