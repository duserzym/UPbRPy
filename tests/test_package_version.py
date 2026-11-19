"""Version and commit provenance must describe the imported source tree."""

import subprocess
import sys
from pathlib import Path

import pytest

import UPbRPy.pipeline as pipeline
from test_reproducibility import example_pipeline


@pytest.fixture(autouse=True)
def clear_provenance_caches():
    pipeline.package_version.cache_clear()
    pipeline.source_revision.cache_clear()
    yield
    pipeline.package_version.cache_clear()
    pipeline.source_revision.cache_clear()


def source_tree(root, version="9.8.7"):
    (root / "UPbRPy").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "UPbRPy"\nversion = "{version}"\n'
    )
    return root / "UPbRPy" / "pipeline.py"


@pytest.mark.parametrize("metadata_dir, filename", [
    ("UPbRPy-0.1.2.dist-info", "METADATA"),
    ("UPbRPy.egg-info", "PKG-INFO"),
])
def test_source_version_wins_over_stale_metadata(tmp_path, monkeypatch, metadata_dir, filename):
    package_file = source_tree(tmp_path)
    metadata = tmp_path / metadata_dir
    metadata.mkdir()
    (metadata / filename).write_text("Metadata-Version: 2.1\nName: UPbRPy\nVersion: 0.1.2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(pipeline, "__file__", str(package_file))

    assert pipeline.version("UPbRPy") == "0.1.2"
    assert pipeline._package_version(tmp_path) == "9.8.7"
    assert pipeline.package_version() == "9.8.7"
    (tmp_path / "pyproject.toml").unlink()
    assert pipeline.package_version() == "9.8.7"


@pytest.mark.parametrize("pyproject", [
    None,
    '[project]\nname = "another-project"\nversion = "9.8.7"\n',
    '[project\n',
])
def test_non_source_install_uses_metadata(tmp_path, monkeypatch, pyproject):
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    monkeypatch.setattr(pipeline, "version", lambda name: "2.3.4")
    assert pipeline._package_version(tmp_path) == "2.3.4"


def test_missing_distribution_uses_source_fallback(tmp_path, monkeypatch):
    def missing(name):
        raise pipeline.PackageNotFoundError(name)

    monkeypatch.setattr(pipeline, "version", missing)
    assert pipeline._package_version(tmp_path) == "0.1.0+source"


def git(root, *args):
    # Repository mutations in these tests are confined to pytest's temp tree.
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=True, timeout=5,
    ).stdout.strip()


def test_source_revision_tracks_commit_and_dirty_state(tmp_path, monkeypatch):
    package_file = source_tree(tmp_path)
    git(tmp_path, "init")
    git(tmp_path, "add", "pyproject.toml")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "Test source provenance")
    monkeypatch.setattr(pipeline, "__file__", str(package_file))
    commit = git(tmp_path, "rev-parse", "HEAD")
    assert pipeline.source_revision() == {"commit": commit, "dirty": False}

    (tmp_path / "untracked.txt").write_text("Ignored for provenance\n")
    assert pipeline._source_revision(tmp_path) == {"commit": commit, "dirty": False}
    with (tmp_path / "pyproject.toml").open("a") as stream:
        stream.write("# Tracked change\n")
    assert pipeline.source_revision()["dirty"] is False  # Cached until restart.
    pipeline.source_revision.cache_clear()
    assert pipeline.source_revision() == {"commit": commit, "dirty": True}


def test_non_git_source_has_no_revision(tmp_path):
    source_tree(tmp_path)
    assert pipeline._source_revision(tmp_path) is None


@pytest.mark.parametrize("error", [
    FileNotFoundError("git is missing"),
    subprocess.TimeoutExpired("git", 2),
    subprocess.CalledProcessError(128, "git"),
])
def test_git_failures_have_no_revision(tmp_path, monkeypatch, error):
    source_tree(tmp_path)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(pipeline.subprocess, "run", fail)
    assert pipeline._source_revision(tmp_path) is None


def test_frozen_uses_metadata_and_has_no_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "__file__", str(source_tree(tmp_path)))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(pipeline, "version", lambda name: "2.3.4")
    assert pipeline.package_version() == "2.3.4"
    assert pipeline.source_revision() is None


def test_manifest_records_running_worktree_commit(tmp_path):
    root = Path(pipeline.__file__).resolve().parent.parent
    manifest = example_pipeline(tmp_path).manifest()
    assert manifest["upbrpy_source"]["commit"] == git(root, "rev-parse", "HEAD")
    assert isinstance(manifest["upbrpy_source"]["dirty"], bool)
    assert {"package_version", "source_revision"} <= set(pipeline.__all__)
