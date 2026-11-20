"""Spawned restores preserve calculations, errors and parent archive state."""

import csv
import json
import multiprocessing
import os
import pickle
import sys

import pytest

from GUI import restore_process, services
from GUI.workflows import get_workflow_preset
from scripts.snapshot_restore import compare_snapshots, snapshot_restored
from test_restore_identity import RICH


@pytest.fixture
def fixture_project(tmp_path):
    # The shared identity test supplies 202Hg with a load_session monkeypatch.
    # Spawn does not inherit monkeypatches: add that channel to a temporary
    # Rich copy instead, leaving the generated repository fixture untouched.
    raw = tmp_path / "fixture_Rich.csv"
    with RICH.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    extra = []
    for row in rows:
        if len(row) > 3 and row[0] == "MainRuns" and row[2] == "204Hg":
            copied = row.copy()
            copied[2] = "202Hg"
            if row[3] == "Y":
                copied[4:] = ["1.0" if value else "" for value in row[4:]]
            extra.append(copied)
    with raw.open("w", encoding="utf-8-sig", newline="") as stream:
        csv.writer(stream).writerows(rows + extra)
    preset = get_workflow_preset("zircon")
    session = services.load_session(str(raw), preset=preset)
    session.session_fit_method = "mean"
    session.primary_68_standard = session.primary_76_standard = session.primary_82_standard = "S513"
    pipeline = services.ensure_shared_pipeline(session, preset)
    pipeline.run_finalize()
    from UPbRPy.project import write_reduction_project

    return write_reduction_project(pipeline, tmp_path / "fixture.upbr")


def test_restore_process_identity_progress_and_no_qt(fixture_project, monkeypatch, caplog):
    context = multiprocessing.get_context("spawn")
    pipe = context.Pipe
    received = []

    class Receiver:
        def __init__(self, connection):
            self.connection = connection

        def poll(self, timeout):
            return self.connection.poll(timeout)

        def recv(self):
            value = self.connection.recv()
            received.append(value[0:2] if value[0] == "imports" else value[0:1])
            return value

        def close(self):
            self.connection.close()

    def tracked_pipe(*args, **kwargs):
        receiver, sender = pipe(*args, **kwargs)
        return Receiver(receiver), sender

    monkeypatch.setattr(context, "Pipe", tracked_pipe)
    expected_stages = []
    expected = services.restore_session_from_project(fixture_project, progress=expected_stages.append)
    services.load_standard_archive_from_path(None)
    stages = []
    original_main = sys.modules["__main__"]
    actual = restore_process.restore_project_in_process(fixture_project, stages.append)
    assert sys.modules["__main__"] is original_main
    assert compare_snapshots(snapshot_restored(expected), snapshot_restored(actual)) == []
    assert stages == expected_stages
    assert stages[0] == "Reading data"
    assert stages.index("Baseline") < stages.index("Ratios") < stages.index("Session")
    assert [item for item in received if item[0] == "imports"] == [("imports", False)] * 2
    assert ("result",) in received
    assert "restoring in thread" not in caplog.text
    assert services.current_standard_archive_path() == str(
        actual.reference_library_path or services.DEFAULT_REFERENCE_LIBRARY_PATH)
    assert actual.session.pipeline.standard_archive is services.current_standard_archive()
    stages = actual.session.pipeline.cache.stages()
    services.ensure_shared_pipeline(actual.session, actual.preset)
    assert actual.session.pipeline.cache.stages() == stages


@pytest.mark.parametrize("bad_input", ["missing_csv", "bad_project"])
def test_restore_process_preserves_errors(fixture_project, bad_input, tmp_path):
    if bad_input == "missing_csv":
        document = json.loads(fixture_project.read_text())
        document["paths"]["source_directory"] = str(tmp_path / "missing.csv")
        fixture_project.write_text(json.dumps(document))
    else:
        fixture_project.write_text("not a project")
    with pytest.raises(Exception) as threaded:
        services.restore_session_from_project(fixture_project)
    with pytest.raises(type(threaded.value)) as spawned:
        restore_process.restore_project_in_process(fixture_project)
    assert str(spawned.value) == str(threaded.value)


def test_restore_process_disabled(fixture_project, monkeypatch):
    monkeypatch.setenv("UPBRPY_RESTORE_IN_PROCESS", "0")
    monkeypatch.setattr(restore_process.multiprocessing, "get_context",
                        lambda *args: pytest.fail("Should not spawn"))
    expected = services.restore_session_from_project(fixture_project)
    actual = restore_process.restore_project_in_process(fixture_project)
    assert compare_snapshots(snapshot_restored(expected), snapshot_restored(actual)) == []


def test_restore_process_start_failure_falls_back(fixture_project, monkeypatch, caplog):
    def fail(self):
        raise OSError("forced start failure")

    monkeypatch.setattr(multiprocessing.get_context("spawn").Process, "start", fail)
    expected = services.restore_session_from_project(fixture_project)
    original_main = sys.modules["__main__"]
    actual = restore_process.restore_project_in_process(fixture_project)
    assert sys.modules["__main__"] is original_main
    assert compare_snapshots(snapshot_restored(expected), snapshot_restored(actual)) == []
    assert caplog.text.count("Process restore unavailable") == 1


def test_restore_cancellation(fixture_project):
    task = restore_process.RestoreTask()

    def cancel(stage):
        task.cancel()

    with pytest.raises(restore_process.RestoreCancelled):
        task.restore(fixture_project, cancel)
    assert task._process is None


def _exit_without_result(connection, project_file):
    os._exit(9)


@pytest.mark.parametrize("failure", ["child_exit", "unpickle"])
def test_restore_transport_failure_falls_back(fixture_project, monkeypatch, caplog, failure):
    expected = services.restore_session_from_project(fixture_project)
    if failure == "child_exit":
        monkeypatch.setattr(restore_process, "_child_restore", _exit_without_result)
    else:
        def fail(payload):
            raise pickle.UnpicklingError("forced unpickle failure")

        monkeypatch.setattr(restore_process, "_unpickle", fail)
    actual = restore_process.restore_project_in_process(fixture_project)
    assert compare_snapshots(snapshot_restored(expected), snapshot_restored(actual)) == []
    assert caplog.text.count("Process restore unavailable") == 1


def test_restore_parent_custom_archive(fixture_project, tmp_path):
    import shutil
    library = tmp_path / "references"
    shutil.copytree(services.DEFAULT_REFERENCE_LIBRARY_PATH, library)
    document = json.loads(fixture_project.read_text())
    document["paths"]["reference_library"] = str(library)
    fixture_project.write_text(json.dumps(document))
    try:
        expected = services.restore_session_from_project(fixture_project)
        services.load_standard_archive_from_path(None)
        actual = restore_process.restore_project_in_process(fixture_project)
        assert services.current_standard_archive_path() == str(library)
        assert actual.session.pipeline.standard_archive is services.current_standard_archive()
        assert compare_snapshots(snapshot_restored(expected), snapshot_restored(actual)) == []
    finally:
        services.load_standard_archive_from_path(None)


def test_unpickle_avoids_pyside_import_hook(fixture_project, monkeypatch, caplog):
    import UPbRPy

    original = UPbRPy.__getattr__

    def lookup(name):
        assert name != "__wrapped__", "PySide inspected the lazy package during unpickling"
        return original(name)

    monkeypatch.setattr(UPbRPy, "__getattr__", lookup)
    restored = restore_process.restore_project_in_process(fixture_project)
    assert restored.session.loaded
    assert "Process restore unavailable" not in caplog.text


def _blocked_store_child(connection, project_file):
    import time
    from pathlib import Path
    from GUI import results_cache

    real_store = results_cache.store

    def store(*args):
        marker = Path(os.environ["UPBRPY_TEST_STORE_MARKER"])
        marker.write_text("storing")
        deadline = time.monotonic() + 30
        while not marker.with_suffix(".release").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        real_store(*args)
        marker.with_suffix(".done").write_text("done")

    results_cache.store = store
    restore_process._child_restore(connection, project_file)


def test_result_returns_while_child_is_still_storing(fixture_project, tmp_path, monkeypatch):
    import time
    from GUI import results_cache

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    marker = cache_dir / "store"
    monkeypatch.setenv("UPBRPY_TEST_STORE_MARKER", str(marker))
    monkeypatch.setenv("UPBRPY_RESULTS_CACHE_DIR", str(tmp_path / "cache"))
    real = restore_process._child_restore
    # Use a wrapper in the spawn bootstrap; the child imports the real module.
    monkeypatch.setattr(restore_process, "_child_restore", _blocked_store_child)
    task = restore_process.RestoreTask()
    try:
        restored = task.restore(fixture_project, use_cache=False)
        assert restored.session.loaded
        assert task._process is None
        assert not marker.with_suffix(".done").exists()
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        assert results_cache.load(results_cache.prepare(fixture_project)) is None
    finally:
        monkeypatch.setattr(restore_process, "_child_restore", real)
        marker.with_suffix(".release").touch()
    deadline = time.monotonic() + 15
    while not marker.with_suffix(".done").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.with_suffix(".done").exists()
    assert results_cache.load(results_cache.prepare(fixture_project)) is not None
