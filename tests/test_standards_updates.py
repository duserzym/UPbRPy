"""Git checks distinguish reference changes from documentation changes."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from GUI.standards_updates import check_standards


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", *args],
        text=True,
    ).strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
def test_standards_check_compares_values_without_changing_files(tmp_path):
    source = Path(__file__).parents[1] / "UPbRPy" / "reference_materials"
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    shutil.copytree(source, remote)
    git(remote, "init", "-b", "main")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "Initial library")
    git(tmp_path, "clone", str(remote), str(local))
    initial_head = git(local, "rev-parse", "HEAD")

    assert not check_standards(str(source), local).warning
    (remote / "README.md").write_text("Notes\n")
    git(remote, "add", "README.md")
    git(remote, "commit", "-m", "Documentation")
    assert not check_standards(str(source), local).warning

    material = next((remote / "rutile").glob("*.toml")).relative_to(remote)
    old = (local / material).read_bytes()
    revised, count = re.subn(rb"value = [0-9.]+", b"value = 12345", old, count=1)
    assert count == 1 and old != revised
    (remote / material).write_bytes(revised)
    git(remote, "add", str(material))
    git(remote, "commit", "-m", "Change standard")
    checked = check_standards(str(source), local)
    assert checked.warning and checked.changed == (material.as_posix(),)
    assert git(local, "rev-parse", "HEAD") == initial_head
    assert (local / material).read_bytes() == old
    assert git(local, "status", "--porcelain") == ""

    git(local, "pull", "--ff-only")
    checked = check_standards(str(source), local)
    assert checked.warning and not checked.changed
    assert "configured library on disk differs" in checked.message

    (local / "new.toml").write_text("value = 12\n")
    git(local, "remote", "set-url", "origin", str(tmp_path / "offline"))
    checked = check_standards(str(local), local)
    assert checked.warning and checked.changed == ("new.toml",)
    assert "GitHub standards check unavailable" in checked.message


def test_snapshot_revision_is_compared_and_reported_without_network(tmp_path, monkeypatch):
    from GUI import standards_updates
    from UPbRPy.standards import reference_library_info

    (tmp_path / ".git").mkdir()
    snapshot = reference_library_info()
    latest = "a" * 40
    calls = []

    def mocked_git(path, *args):
        calls.append(args)
        if args[0] == "fetch":
            return ""
        if args[0] == "rev-parse":
            return latest
        if args[:2] == ("diff", "--name-only"):
            assert args[2:4] == (snapshot["revision"], latest)
            return "zircon/S513.toml\n"
        return ""

    monkeypatch.setattr(standards_updates, "_git", mocked_git)
    monkeypatch.setattr(standards_updates, "reference_library_info", lambda path: snapshot)
    result = check_standards(None, tmp_path)
    assert result.warning
    assert snapshot["revision"][:12] in result.message
    assert latest[:12] in result.message
    assert "differs from the bundled snapshot revision" in result.message
    assert any(args[0] == "diff" for args in calls)


def test_library_revision_for_snapshot_and_shared_checkout(tmp_path, monkeypatch):
    from GUI import standards_updates
    from UPbRPy.standards import reference_library_info

    expected_revision = reference_library_info()["revision"][:12]
    def no_hashing(*args):
        pytest.fail("Displaying a revision must not hash the library on the UI thread")
    monkeypatch.setattr(standards_updates, "reference_library_info", no_hashing)
    assert standards_updates.library_revision(None) == expected_revision
    source = Path(__file__).parents[1] / "UPbRPy/reference_materials"
    shutil.copytree(source, tmp_path / "library")
    (tmp_path / "library/UPSTREAM.toml").unlink()

    def mocked_git(path, *args):
        assert path == tmp_path / "library"
        return "b" * 40 if args[0] == "rev-parse" else " M zircon/S513.toml"

    monkeypatch.setattr(standards_updates, "_git", mocked_git)
    assert standards_updates.library_revision(str(tmp_path / "library/library.toml")) == "bbbbbbbbbbbb (modified)"
