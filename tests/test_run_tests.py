"""Fast selection and recording checks using small, disposable repositories."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import struct
import time
import zlib

import pytest
import conftest


_spec = importlib.util.spec_from_file_location("upbrpy_test_runner", Path(__file__).resolve().parents[1] / "scripts/run_tests.py")
runner = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = runner
_spec.loader.exec_module(runner)


SOURCE = """import os
CONSTANT = 1
@decorate
class Widget:
    value = 2
    @decorate
    def show(self):
        def nested():
            return 1
        return nested()

@decorate
def outer():
    class Local:
        def method(self):
            return 2
    return Local
"""


@pytest.mark.parametrize("line,expected", [
    (1, ("<module>", False)), (2, ("<module>", False)),
    (3, ("<module>", False)), (5, ("Widget", True)),
    (6, ("Widget", True)), (7, ("Widget.show", False)),
    (9, ("Widget.show.<locals>.nested", False)),
    (10, ("Widget.show", False)), (11, ("<module>", False)),
    (12, ("<module>", False)), (15, ("outer.<locals>.Local.method", False)),
])
def test_line_qualnames(line, expected):
    assert runner.line_qualnames(SOURCE, [(line, line)]) == [expected]


def test_ranges_cover_all_scopes():
    assert runner.line_qualnames(SOURCE, [(5, 10)]) == [
        ("Widget", True), ("Widget.show", False), ("Widget.show.<locals>.nested", False),
    ]
    assert runner.line_qualnames("async def run():\n    pass\n", [(2, 2)]) == [("run", False)]


def write(root, path, contents):
    dest = root / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(contents)


def commit_tree(root, files, parent=None):
    """Write loose Git objects directly; never run a state-changing git command."""
    git = root / ".git"
    (git / "objects").mkdir(parents=True, exist_ok=True)
    (git / "refs/heads").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n")

    def obj(kind, body):
        data = kind.encode() + b" " + str(len(body)).encode() + b"\0" + body
        digest = hashlib.sha1(data).hexdigest()
        path = git / "objects" / digest[:2] / digest[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(data))
        return digest

    def tree(entries):
        folders, blobs = {}, {}
        for name, value in entries.items():
            head, sep, tail = name.partition("/")
            if sep:
                folders.setdefault(head, {})[tail] = value
            else:
                blobs[head] = obj("blob", value.encode())
        records = [(name, "100644", sha) for name, sha in blobs.items()]
        records += [(name, "40000", tree(children)) for name, children in folders.items()]
        return obj("tree", b"".join(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(sha) for name, mode, sha in sorted(records, key=lambda entry: entry[0] + ("/" if entry[1] == "40000" else ""))))

    body = f"tree {tree(files)}\n" + (f"parent {parent}\n" if parent else "")
    body += "author Test <test@example.com> 1000000000 +0000\ncommitter Test <test@example.com> 1000000000 +0000\n\nfixture\n"
    sha = obj("commit", body.encode())
    (git / "refs/heads/main").write_text(sha + "\n")
    entries = []
    for name, contents in sorted(files.items()):
        digest = bytes.fromhex(obj("blob", contents.encode()))
        entry = struct.pack(">10I20sH", 0, 0, 0, 0, 0, 0, 0o100644, 0, 0, len(contents.encode()), digest, len(name.encode())) + name.encode() + b"\0"
        entries.append(entry + b"\0" * (-len(entry) % 8))
    index = b"DIRC" + struct.pack(">II", 2, len(entries)) + b"".join(entries)
    (git / "index").write_bytes(index + hashlib.sha1(index).digest())
    return sha


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "UPbRPy/age.py": SOURCE,
        "tests/test_one.py": "from UPbRPy.age import Widget\n",
        "tests/test_two.py": "from UPbRPy.age import outer\n",
        "tests/test_three.py": "pass\n",
    }
    for path, contents in files.items():
        write(root, path, contents)
    sha = commit_tree(root, files)
    cache = tmp_path / "map.json"
    monkeypatch.setattr(runner, "map_path", lambda root=runner.ROOT: cache)
    data = {"commit": sha, "timestamp": time.time(), "tests": {
        "tests/test_one.py": {"functions": [["UPbRPy/age.py", "Widget.show"]], "files": ["UPbRPy/reference_materials/zircon/S513.toml"]},
        "tests/test_two.py": {"functions": [["UPbRPy/age.py", "outer"]], "files": []},
        "tests/test_three.py": {"functions": [], "files": []},
    }}
    cache.write_text(json.dumps(data))
    return root, cache, data


def test_map_function_class_module_and_data(repo):
    root, _, _ = repo
    assert runner.select_tests(["UPbRPy/age.py:Widget.show"], root) == {"tests/test_one.py": "UPbRPy/age.py:Widget.show"}
    assert list(runner.select_tests(["UPbRPy/age.py:5"], root)) == ["tests/test_one.py"]
    assert list(runner.select_tests(["UPbRPy/age.py:2"], root)) == ["tests/test_one.py", "tests/test_two.py"]
    assert list(runner.select_tests(["UPbRPy/reference_materials/zircon/S513.toml"], root)) == ["tests/test_one.py"]
    assert runner.select_tests(["tests/test_three.py"], root) == {"tests/test_three.py": "tests/test_three.py"}
    assert len(runner.select_tests(["tests/conftest.py"], root)) == 3


@pytest.mark.parametrize("invalid", ["missing", "stale", "unrelated"])
def test_fallback(repo, invalid, capsys):
    root, cache, data = repo
    if invalid == "missing":
        cache.unlink()
    else:
        if invalid == "stale":
            data["timestamp"] -= 15 * 86400
        else:
            data["commit"] = commit_tree(root, {"other": "unrelated"})
            # An existing root commit with another tree has no ancestry relation.
            commit_tree(root, {"current": "different"})
        cache.write_text(json.dumps(data))
    assert list(runner.select_tests(["UPbRPy/age.py:outer"], root)) == ["tests/test_one.py", "tests/test_two.py"]
    assert "A FULL run will refresh" in capsys.readouterr().out


def test_new_source_static_fallback(repo):
    root, _, _ = repo
    write(root, "UPbRPy/new.py", "def new():\n    pass\n")
    write(root, "tests/test_three.py", "from UPbRPy.new import new\n")
    assert list(runner.select_tests(["UPbRPy/new.py"], root)) == ["tests/test_three.py"]


def test_diff_old_and_new_ranges(repo):
    root, _, _ = repo
    write(root, "UPbRPy/age.py", SOURCE.replace("def show(self):", "def renamed(self):"))
    names = runner.changed_qualnames("UPbRPy/age.py", None, root, "HEAD", False)
    assert ("Widget.show", False) in names
    assert ("Widget.renamed", False) in names
    write(root, "UPbRPy/age.py", SOURCE.replace("@decorate\ndef outer():\n    class Local:\n        def method(self):\n            return 2\n    return Local\n", ""))
    assert ("outer", False) in runner.changed_qualnames("UPbRPy/age.py", None, root, "HEAD", False)
    # Removing a module-level decorator also changes module scope.
    assert list(runner.select_tests(["UPbRPy/age.py"], root)) == ["tests/test_one.py", "tests/test_two.py"]


def test_merge_unions_shards(repo, tmp_path):
    root, cache, data = repo
    folder = tmp_path / "workers"
    folder.mkdir()
    for index, name in enumerate(("one", "two")):
        (folder / f"{index}.json").write_text(json.dumps({"tests/test_one.py": {"functions": [["UPbRPy/age.py", name]], "files": ["docs/testing.md"]}}))
    runner.merge_test_maps(folder, data["commit"], root)
    merged = json.loads(cache.read_text())
    assert merged["tests"]["tests/test_one.py"] == {"functions": [["UPbRPy/age.py", "one"], ["UPbRPy/age.py", "two"]], "files": ["docs/testing.md"]}


def test_recording_only_repository_paths_and_file_resets(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    write(root, "UPbRPy/example.py", "def called():\n    return 1\n")
    write(root, "docs/example.txt", "data")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (root / "docs/link.txt").symlink_to(outside)
    folder = tmp_path / "records"
    recorder = conftest.TestMapRecorder(root, folder)
    try:
        recorder.start_file(root / "tests/test_one.py")
        namespace = {}
        exec(compile((root / "UPbRPy/example.py").read_text(), str(root / "UPbRPy/example.py"), "exec"), namespace)
        namespace["called"]()
        namespace["called"]()
        (root / "docs/example.txt").read_text()
        outside.read_text()
        sys.audit("object.__getattr__", object(), "__class__")
        sys.audit("upbrpy.test.ignored", str(root / "docs/not-opened.txt"))
        (root / "docs/link.txt").read_text()
        write(root, "tests/__pycache__/junk", "cache")
        write(root, ".git/ignored", "git")
        recorder.start_file(root / "tests/test_two.py")
        namespace["called"]()
    finally:
        recorder.finish()
    data = json.loads(next(folder.glob("*.json")).read_text())
    assert data["tests/test_one.py"]["functions"] == [["UPbRPy/example.py", "<module>"], ["UPbRPy/example.py", "called"]]
    assert data["tests/test_two.py"]["functions"] == [["UPbRPy/example.py", "called"]]
    assert "docs/example.txt" in data["tests/test_one.py"]["files"]
    for entry in data.values():
        assert all(not Path(p).is_absolute() and ".." not in Path(p).parts and ".git" not in Path(p).parts and "__pycache__" not in Path(p).parts and p not in {"docs/link.txt", "docs/not-opened.txt"} for p in entry["files"])
