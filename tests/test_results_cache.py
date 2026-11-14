"""Disposable results preserve exact restore products and fail closed."""
import json
import os
from pathlib import Path
import pickle
import threading

import pytest

from GUI import results_cache as cache, restore_process, services
from scripts.snapshot_restore import compare_snapshots, snapshot_restored
from test_restore_process import fixture_project  # shared small, temporary Rich export


@pytest.fixture
def cache_root(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("results-cache")
    monkeypatch.setenv("UPBRPY_RESULTS_CACHE_DIR", str(root))
    return root


@pytest.fixture
def seeded(fixture_project, cache_root):
    request = cache.prepare(fixture_project)
    assert request is not None
    with cache.DependencyRecorder(request) as recorder:
        restored = services.restore_session_from_project(fixture_project)
    payload = pickle.dumps(restored, protocol=5)
    cache.store(request, payload, recorder.dependencies(restored))
    assert cache.load(request) == payload
    return request, restored


def edit_document(path, edit):
    document = json.loads(path.read_text())
    edit(document)
    path.write_text(json.dumps(document))


def no_spawn(monkeypatch):
    monkeypatch.setattr(restore_process.multiprocessing, "get_context",
                        lambda *a: pytest.fail("A cache hit must not spawn"))


def test_hit_exact_snapshot_and_archive(seeded, monkeypatch):
    request, fresh = seeded
    no_spawn(monkeypatch)
    stages = []
    hit = restore_process.RestoreTask().restore(request.project, stages.append)
    assert compare_snapshots(snapshot_restored(fresh), snapshot_restored(hit)) == []
    assert stages == ["Loading saved results"]
    assert hit.session.pipeline.standard_archive is services.current_standard_archive()
    stages = hit.session.pipeline.cache.stages()
    services.ensure_shared_pipeline(hit.session, hit.preset)
    assert hit.session.pipeline.cache.stages() == stages
    # Manifest time is generated on demand, never frozen in the pipeline.
    before = fresh.session.pipeline.manifest(hash_inputs=True)
    after = hit.session.pipeline.manifest(hash_inputs=True)
    assert before.pop("created_utc") <= after.pop("created_utc")
    assert before == after


@pytest.mark.parametrize("change", ["selection", "setting", "export", "unknown", "engine"])
def test_scientific_key_misses(seeded, monkeypatch, change):
    request, _ = seeded
    if change == "engine":
        monkeypatch.setattr(cache, "engine_fingerprint", lambda: "different engine")
    else:
        def edit(d):
            if change == "selection":
                d["selections"][0]["baseline_end_times"] += 0.01
            elif change == "setting":
                d["pipeline"]["config"]["decision"]["age_transition_ma"] = 1001
            elif change == "export":
                d.setdefault("workspace", {})["export_settings"] = {"a_sio2": 0.5}
            else:
                d.setdefault("workspace", {})["future_numeric_option"] = True
        edit_document(request.project, edit)
    changed = cache.prepare(request.project)
    assert changed is not None and changed.key != request.key
    assert cache.load(changed) is None


def test_view_choices_overlay_and_reset_defaults(seeded, monkeypatch):
    request, fresh = seeded
    def edit(d):
        d["workspace"] = dict(selected_spot=fresh.session.spot_names[-1],
                              plot_mode="baseline", visible_channels=[fresh.session.channels[-1]],
                              sigma_level=1, channel_colors={"238U": "red"})
    edit_document(request.project, edit)
    current = cache.prepare(request.project)
    assert current.key == request.key
    no_spawn(monkeypatch)
    hit = restore_process.RestoreTask().restore(request.project)
    expected = services.restore_session_from_project(request.project)
    assert compare_snapshots(snapshot_restored(expected), snapshot_restored(hit)) == []
    assert hit.session.selected_spot == expected.session.selected_spot
    assert hit.session.visible_channels == expected.session.visible_channels
    assert hit.session.plot_mode == expected.session.plot_mode
    edit_document(request.project, lambda d: d.update(workspace={}))
    hit = restore_process.RestoreTask().restore(request.project)
    assert hit.session.selected_spot == fresh.session.spot_names[0]
    assert hit.session.visible_channels == fresh.session.visible_channels


def test_same_mtime_raw_change_misses(seeded):
    request, _ = seeded
    stat = request.source.stat()
    data = request.source.read_bytes()
    request.source.write_bytes(data.replace(b"1.0", b"2.0", 1))
    os.utime(request.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert request.source.stat().st_mtime_ns == stat.st_mtime_ns
    assert cache.load(cache.prepare(request.project)) is None


def test_reference_value_change_misses(fixture_project, cache_root, tmp_path):
    library = tmp_path / "references.csv"
    services.current_standard_archive().to_csv(library)
    edit_document(fixture_project, lambda d: d["paths"].update(reference_library=str(library)))
    request = cache.prepare(fixture_project)
    try:
        with cache.DependencyRecorder(request) as recorder:
            restored = services.restore_session_from_project(fixture_project)
        cache.store(request, pickle.dumps(restored, protocol=5), recorder.dependencies(restored))
        assert cache.load(request)
        text = library.read_text()
        library.write_text(text.replace("337.1", "337.2", 1) if "337.1" in text else text.replace("0.0", "0.1", 1))
        assert library.read_text() != text
        assert cache.load(cache.prepare(fixture_project)) is None
    finally:
        services.load_standard_archive_from_path(None)


@pytest.mark.parametrize("failure", ["corrupt", "truncated", "index", "missing", "unpickle", "permission"])
def test_bad_cache_silently_recalculates(seeded, monkeypatch, failure):
    request, fresh = seeded
    root = cache.cache_directory()
    payload = root / (request.key + ".payload")
    index = root / (request.key + ".json")
    if failure == "corrupt":
        payload.write_bytes(b"garbage")
    elif failure == "truncated":
        payload.write_bytes(payload.read_bytes()[:100])
    elif failure == "index":
        index.write_bytes(b"{")
    elif failure == "missing":
        request.source.unlink()
    elif failure == "unpickle":
        cache.store(request, b"not a pickle", json.loads(index.read_bytes())["deps"])
    else:
        original = Path.read_bytes
        def unreadable(path):
            if path.is_relative_to(root):
                raise PermissionError("unreadable cache")
            return original(path)
        monkeypatch.setattr(Path, "read_bytes", unreadable)
    # A controlled fresh fallback proves all failure paths are silent without
    # spawning six redundant children (the spawn path is covered below).
    monkeypatch.setenv("UPBRPY_RESTORE_IN_PROCESS", "0")
    called = []
    def recalculate(*args, **kwargs):
        called.append(True)
        return fresh
    monkeypatch.setattr(services, "restore_session_from_project", recalculate)
    actual = restore_process.RestoreTask().restore(request.project)
    assert called == [True] and actual is fresh
    if failure in {"corrupt", "truncated", "index", "unpickle"}:
        assert not index.exists() and not payload.exists()


def test_disabled_and_write_failures(seeded, monkeypatch):
    request, fresh = seeded
    monkeypatch.setenv("UPBRPY_RESULTS_CACHE", "0")
    assert cache.prepare(request.project) is None
    assert cache.load(request) is None
    monkeypatch.setenv("UPBRPY_RESTORE_IN_PROCESS", "0")
    monkeypatch.setattr(services, "restore_session_from_project", lambda *a, **k: fresh)
    assert restore_process.RestoreTask().restore(request.project) is fresh
    monkeypatch.delenv("UPBRPY_RESULTS_CACHE")
    monkeypatch.setattr(cache, "_atomic", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    cache.store(request, b"bytes", {"files": {}, "directories": {}})


def test_directory_and_absent_manifest_dependencies(seeded):
    request, _ = seeded
    # Project saves/autosaves and their temporaries do not alter discovery.
    (request.project.parent / "another.upbr").write_text("{}")
    (request.project.parent / ".another.upbr.abc.tmp").write_text("{}")
    assert cache.load(request)
    (request.project.parent / "session_manifest.toml").write_text('session_key = "new"')
    assert cache.load(request) is None


def test_directory_addition_misses(seeded):
    request, _ = seeded
    (request.source.parent / "companion.upbr").mkdir()
    assert cache.load(request) is None
    candidate = request.source.parent / "same-name"
    candidate.write_text("non-input")
    before = cache._listing(candidate.parent)
    candidate.unlink()
    candidate.mkdir()
    assert cache._listing(candidate.parent) != before
    if os.name != "nt":
        first, second = candidate.parent / "target-a", candidate.parent / "target-b"
        first.write_text("first")
        second.write_text("second")
        link = candidate.parent / "linked-input"
        link.symlink_to(first)
        recorder = cache.DependencyRecorder(request)
        recorder._record_file(link)
        deps = dict(files=recorder.files, directories={}, aliases=recorder.aliases)
        assert cache._validate(deps)
        link.unlink()
        link.symlink_to(second)
        assert not cache._validate(deps)


def test_eviction_oldest_last_use_and_size(cache_root, monkeypatch):
    monkeypatch.setenv("UPBRPY_RESULTS_CACHE_MAX_MB", "0.001")
    for i in range(3):
        (cache_root / f"{i}.payload").write_bytes(b"x" * 450)
        index = cache_root / f"{i}.json"
        index.write_text("{}")
        os.utime(index, ns=(i + 1, i + 1))
    cache.evict()
    assert not (cache_root / "0.payload").exists()
    assert (cache_root / "1.payload").exists() and (cache_root / "2.payload").exists()
    assert sum(p.stat().st_size for p in cache_root.iterdir()) <= 1048


def test_spawn_bypass_recalculates_and_refreshes(fixture_project, cache_root, monkeypatch):
    stages = []
    first = restore_process.RestoreTask().restore(fixture_project, stages.append)
    assert "Reading data" in stages
    request = cache.prepare(fixture_project)
    assert cache.load(request)
    stages.clear()
    again = restore_process.RestoreTask().restore(fixture_project, stages.append, use_cache=False)
    assert "Reading data" in stages and "Loading saved results" not in stages
    assert compare_snapshots(snapshot_restored(first), snapshot_restored(again)) == []
    assert cache.load(request)
    no_spawn(monkeypatch)
    assert compare_snapshots(snapshot_restored(again), snapshot_restored(
        restore_process.RestoreTask().restore(fixture_project))) == []


def test_warmer_debounces_cancels_and_has_no_parent_install(monkeypatch):
    warmer = restore_process.ResultsCacheWarmer()
    started = threading.Event()
    finished = threading.Event()
    calls = []
    class Task:
        def __init__(self, *, warm_only):
            assert warm_only
        def restore(self, path):
            calls.append(path)
            started.set()
            assert finished.wait(5)
        def cancel(self):
            finished.set()
    monkeypatch.setattr(restore_process, "RestoreTask", Task)
    warmer.schedule("superseded", delay=30)
    warmer.schedule("saved", delay=0)
    assert started.wait(5)
    warmer.cancel()
    with warmer._worker_lock:
        assert calls == ["saved"]


def test_engine_fingerprint_covers_source_and_format(monkeypatch):
    original = cache.engine_fingerprint()
    monkeypatch.setattr(cache, "CACHE_FORMAT", cache.CACHE_FORMAT + 1)
    assert cache.engine_fingerprint() != original


def test_audit_captures_extra_reads_and_detects_mid_restore_change(seeded, tmp_path):
    request, restored = seeded
    extra = tmp_path / "extra-reference.txt"
    extra.write_text("first")
    with cache.DependencyRecorder(request) as recorder:
        extra.read_bytes()
    assert str(extra.resolve()) in recorder.dependencies(restored)["files"]
    with cache.DependencyRecorder(request) as recorder:
        extra.read_bytes()
        extra.write_text("second")
    with pytest.raises(ValueError, match="changed"):
        recorder.dependencies(restored)
    # The permanent interpreter hook is inert outside its scope.
    assert not recorder.active
    with extra.open("rb") as existing:
        with cache.DependencyRecorder(request) as unknown:
            with os.fdopen(os.dup(existing.fileno()), "rb") as stream:
                stream.read()
    assert not unknown.valid


def test_distribution_metadata_is_not_an_input(seeded, tmp_path):
    # Version lookups read *.egg-info/*.dist-info; the version is already in the
    # engine fingerprint, and an editable reinstall rewrites these in place.
    request, restored = seeded
    metadata = tmp_path / "UPbRPy.egg-info" / "PKG-INFO"
    metadata.parent.mkdir()
    metadata.write_text("Version: 1")
    with cache.DependencyRecorder(request) as recorder:
        metadata.read_bytes()
    assert str(metadata.resolve()) not in recorder.dependencies(restored)["files"]


def test_frozen_fingerprint_uses_each_bound_loader(tmp_path, monkeypatch):
    from types import SimpleNamespace
    real_import = cache.importlib.import_module
    calls = []
    class Loader:
        def __init__(self, name):
            self.name = name
            self._pyz_archive = SimpleNamespace(toc={
                "UPbRPy": None, "UPbRPy.unloaded": None,
                "GUI": None, "GUI.unloaded": None})
        def get_code(self, name):
            assert name == self.name
            calls.append(name)
            return compile("value = 1", name, "exec")
    def package(name):
        if name not in {"UPbRPy", "GUI"}:
            return real_import(name)
        return SimpleNamespace(__file__=str(tmp_path / name / "__init__.py"),
                               __spec__=SimpleNamespace(loader=Loader(name)))
    monkeypatch.setattr(cache.importlib, "import_module", package)
    monkeypatch.setattr(cache.importlib.machinery.PathFinder, "find_spec",
                        lambda name, paths: SimpleNamespace(loader=Loader(name)))
    assert cache.engine_fingerprint()
    assert {"GUI.unloaded", "UPbRPy.unloaded"}.issubset(calls)


def test_warm_child_priority_and_no_parent_archive(fixture_project, cache_root, monkeypatch):
    import time
    warmer = restore_process.ResultsCacheWarmer()
    request = cache.prepare(fixture_project)
    monkeypatch.setattr(restore_process, "_finish_payload",
                        lambda *a: pytest.fail("Warmer must not install an archive in parent"))
    warmer.schedule(fixture_project, delay=0)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if cache.load(request):
            break
        time.sleep(0.05)
    else:
        pytest.fail("Warmer did not create results")
    warmer.cancel()
    with warmer._worker_lock:
        assert warmer._task is None
    nice = []
    monkeypatch.setattr(restore_process.os, "nice", nice.append, raising=False)
    monkeypatch.setattr(restore_process, "_child_restore", lambda *a: None)
    restore_process._child_warm(None, "project")
    assert nice == [10]
