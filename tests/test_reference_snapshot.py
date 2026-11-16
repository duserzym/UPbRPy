"""The bundled upstream snapshot is complete, reproducible and used without CSVs."""
from pathlib import Path
import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tomllib

import pytest

from UPbRPy.standards import load_standard_measurements, reference_library_info

ROOT = Path(__file__).parents[1]
SNAPSHOT = ROOT / "UPbRPy/reference_materials"
SCRIPT = ROOT / "scripts/sync_upb_standards.py"


def tree(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_snapshot_file_list_hashes_and_every_material_loads():
    metadata = tomllib.loads((SNAPSHOT / "UPSTREAM.toml").read_text())
    actual = tree(SNAPSHOT)
    assert set(actual) == set(metadata["files"]) | {"UPSTREAM.toml"}
    assert set(metadata["sha256"]) == set(metadata["files"])
    for name in metadata["files"]:
        assert hashlib.sha256(actual[name]).hexdigest() == metadata["sha256"][name], name
    manifest = tomllib.loads(actual["library.toml"].decode())
    assert set(metadata["files"]) == {"library.toml", "sources.toml", "LICENSE", *manifest["material_files"]}
    loaded = load_standard_measurements()
    expected_standards = set()
    for name in manifest["material_files"]:
        document = tomllib.loads(actual[name].decode())
        expected_standards.update(document["standards"])
        assert set(document["standards"]) <= set(loaded["standard"])
    assert set(loaded["standard"]) == expected_standards
    info = reference_library_info()
    assert info["revision"] == metadata["commit"]
    assert info["commit_date"] == metadata["commit_date"]
    assert info["repository"] == metadata["repository"]
    assert metadata["license"] == "MIT"
    assert actual["LICENSE"].startswith(b"MIT License\n")
    spec = importlib.util.spec_from_file_location("sync_upb_standards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DEFAULT_REV == metadata["commit"]


def git(path, *args):
    # All state-changing Git commands in this test are confined to tmp_path.
    return subprocess.check_output(
        ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        text=True,
    ).strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
def test_sync_script_reproduces_snapshot_from_temporary_git_repository(tmp_path):
    checkout = tmp_path / "upstream"
    shutil.copytree(SNAPSHOT, checkout)
    (checkout / "UPSTREAM.toml").unlink()
    (checkout / "README.md").write_text("Not data\n")
    (checkout / "docs").mkdir()
    (checkout / "docs/example.toml").write_text("not_data = true\n")
    git(checkout, "init", "-b", "main")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Test data snapshot")
    commit = git(checkout, "rev-parse", "HEAD")
    date = git(checkout, "show", "-s", "--format=%cI", commit)
    # Dirty edits, deletions and untracked data must not leak into the snapshot.
    (checkout / "sources.toml").write_text("invalid TOML [")
    (checkout / "zircon/S513.toml").unlink()
    (checkout / "zircon/untracked.toml").write_text("not_data = true\n")
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    script = project / "scripts" / SCRIPT.name
    shutil.copyfile(SCRIPT, script)
    destination = project / "UPbRPy/reference_materials"
    destination.mkdir(parents=True)
    (destination / "stale.toml").write_text("stale = true\n")
    command = [sys.executable, str(script), str(checkout), "--rev", commit]
    subprocess.run(command, check=True)
    generated = tree(destination)
    expected = tree(SNAPSHOT)
    metadata_text = expected.pop("UPSTREAM.toml").decode()
    metadata = tomllib.loads(metadata_text)
    expected["UPSTREAM.toml"] = metadata_text.replace(metadata["commit"], commit).replace(metadata["commit_date"], date).encode()
    assert generated == expected
    subprocess.run(command, check=True)
    assert tree(destination) == generated
    assert git(checkout, "rev-parse", "HEAD") == commit
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run([*command[:-1], "missing-revision"], check=True, capture_output=True)
    assert tree(destination) == generated


def test_no_code_reads_the_removed_wide_csv_libraries():
    for directory in ("UPbRPy", "GUI", "scripts", "benchmarks"):
        for path in (ROOT / directory).rglob("*.py"):
            assert "standards.csv" not in path.read_text(), path
            assert "RM.csv" not in path.read_text(), path
    assert not (ROOT / "UPbRPy/standards.csv").exists()
    assert not (ROOT / "GUI/reference_materials/RM.csv").exists()
    from UPbRPy import constants
    import UPbRPy
    for name in ("standard_archive_legacy", "standard_archive_csv"):
        assert not hasattr(constants, name)
        assert not hasattr(UPbRPy, name)


def test_packages_ship_snapshot_metadata_and_license():
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["UPbRPy"]
    shipped = {p.relative_to(ROOT / "UPbRPy").as_posix() for pattern in patterns
               for p in (ROOT / "UPbRPy").glob(pattern) if p.is_file()}
    assert {"reference_materials/" + name for name in tree(SNAPSHOT)} <= shipped
    recipe = (ROOT / "packaging/UPbRPyStudio.spec").read_text()
    assert 'str(PROJECT_ROOT / "UPbRPy" / "reference_materials")' in recipe
    assert '"UPbRPy/reference_materials"' in recipe
    assert "standards.csv" not in recipe
