"""Cycle collection stays on the Qt application thread."""
import gc
import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtTest, QtWidgets
from GUI import gc_policy


@pytest.fixture
def app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    old = getattr(app, gc_policy._ATTRIBUTE, None)
    was_enabled = gc.isenabled()
    if old is not None:
        gc_policy.uninstall(app)
    yield app
    gc_policy.uninstall(app)
    if old is not None:
        gc_policy.install(app)
    gc.enable() if was_enabled else gc.disable()


@pytest.mark.parametrize("enabled", [True, False])
def test_install_idempotent_and_restore(app, enabled):
    gc.enable() if enabled else gc.disable()
    gc_policy.install(app)
    policy = getattr(app, gc_policy._ATTRIBUTE)
    gc_policy.install(app)
    assert getattr(app, gc_policy._ATTRIBUTE) is policy
    assert policy.timer.isActive()
    assert not gc.isenabled()
    gc_policy.uninstall(app)
    assert not policy.timer.isActive()
    assert gc.isenabled() is enabled
    gc_policy.uninstall(app)


@pytest.mark.parametrize("counts,thresholds,expected", [
    ((0, 0, 0), (10, 10, 10), []),
    ((11, 0, 0), (0, 10, 10), []),
    ((11, 0, 0), (10, 10, 10), [0]),
    ((11, 11, 0), (10, 10, 10), [1]),
    ((11, 11, 11), (10, 10, 10), [2]),
    ((11, 11, 11), (10, 10, 0), [2]),
    ((11, 0, 11), (10, 10, 10), [2]),
])
def test_due_generations(app, monkeypatch, counts, thresholds, expected):
    gc_policy.install(app)
    calls = []
    monkeypatch.setattr(gc_policy, "_INCREMENTAL", False)
    monkeypatch.setattr(gc, "get_count", lambda: counts)
    monkeypatch.setattr(gc, "get_threshold", lambda: thresholds)
    monkeypatch.setattr(gc, "collect", calls.append)
    getattr(app, gc_policy._ATTRIBUTE).tick()
    assert calls == expected


def test_incremental_python_uses_old_generation_increment(app, monkeypatch):
    gc_policy.install(app)
    calls = []
    monkeypatch.setattr(gc_policy, "_INCREMENTAL", True)
    monkeypatch.setattr(gc, "get_count", lambda: (100, 0, 0))
    monkeypatch.setattr(gc, "get_threshold", lambda: (10, 10, 10))
    monkeypatch.setattr(gc, "collect", calls.append)
    getattr(app, gc_policy._ATTRIBUTE).tick()
    assert calls == [1]


def test_worker_cycle_collected_by_timer_on_gui_thread(app, monkeypatch):
    gc_policy.install(app)
    gc.collect()
    destroyed = []
    class Cycle:
        def __init__(self):
            self.self = self
        def __del__(self):
            destroyed.append(threading.current_thread())
    def create():
        Cycle()
        for _ in range(5000):
            value = []
    worker = threading.Thread(target=create)
    worker.start()
    worker.join()
    assert destroyed == []
    # Force eligibility without explicitly collecting: the live timer must run.
    monkeypatch.setattr(gc, "get_count", lambda: (100, 0, 0))
    monkeypatch.setattr(gc, "get_threshold", lambda: (10, 10, 10))
    QtTest.QTest.qWait(650)
    assert destroyed == [threading.main_thread()]



def _child_gc_state(queue):
    queue.put(gc.isenabled())


def test_spawn_keeps_automatic_gc(app):
    import multiprocessing
    gc_policy.install(app)
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    process = context.Process(target=_child_gc_state, args=(queue,))
    try:
        process.start()
        assert queue.get(timeout=20) is True
        process.join(timeout=20)
        assert process.exitcode == 0
        assert not gc.isenabled()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        queue.close()
        queue.join_thread()
