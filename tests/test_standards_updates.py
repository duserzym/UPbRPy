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
