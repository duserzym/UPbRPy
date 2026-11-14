"""Status motion timing, stable layout and UI responsiveness during restore."""

import time
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtTest, QtWidgets
import shiboken6

from GUI.busy_indicator import BusyIndicator
from GUI.theme import build_application
from test_restore_process import fixture_project  # noqa: F401 - shared pytest fixture


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def test_busy_indicator_delay_fades_and_fixed_slot(app, monkeypatch):
    monkeypatch.setenv("UPBRPY_REDUCE_MOTION", "0")
    window = QtWidgets.QMainWindow()
    indicator = BusyIndicator()
    neighbour = QtWidgets.QLabel("Project summary")
    window.statusBar().addPermanentWidget(indicator)
    window.statusBar().addPermanentWidget(neighbour)
    window.resize(900, 400)
    window.show()
    app.processEvents()
    geometry = neighbour.geometry()
    try:
        assert indicator.isVisible()  # Its transparent slot always participates in layout.
        indicator.start("Opening project")
        QtTest.QTest.qWait(110)
        assert indicator.opacity == 0
        assert not indicator.motion_timer.isActive()
        QtTest.QTest.qWait(90)
        assert 0 < indicator.opacity < 1
        assert indicator.fade.state() == QtCore.QAbstractAnimation.State.Running
        assert neighbour.geometry() == geometry
        indicator.set_progress("Ratios")
        assert indicator.toolTip() == "Opening project · Ratios"
        indicator.stop()
        QtTest.QTest.qWait(190)
        assert indicator.opacity == 0
        assert not indicator.motion_timer.isActive()
        assert neighbour.geometry() == geometry
        indicator.start("Fast task")
        indicator.stop()
        QtTest.QTest.qWait(180)
        assert indicator.opacity == 0
    finally:
        window.close()


def test_busy_indicator_reduced_motion(app, monkeypatch):
    monkeypatch.setenv("UPBRPY_REDUCE_MOTION", "1")
    indicator = BusyIndicator()
    indicator.show()
    try:
        indicator.start("Opening project")
        QtTest.QTest.qWait(180)
        assert indicator.opacity == 1
        assert not indicator.motion_timer.isActive()
        assert indicator.fade.state() == QtCore.QAbstractAnimation.State.Stopped
        indicator.stop()
        assert indicator.opacity == 0
    finally:
        indicator.close()


def test_background_restore_frame_gaps(app, fixture_project, monkeypatch):
    from GUI import main_window

    monkeypatch.delenv("UPBRPY_RESTORE_IN_PROCESS", raising=False)
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args)))
    window = main_window.MainWindow(theme_mode="light")
    window.resize(1440, 900)
    window.show()
    app.processEvents()
    gaps = []
    last = time.perf_counter()

    def tick():
        nonlocal last
        now = time.perf_counter()
        gaps.append(now - last)
        last = now

    timer = QtCore.QTimer()
    timer.setInterval(16)
    timer.timeout.connect(tick)
    timer.start()
    started = time.perf_counter()
    try:
        window.load_project_file(fixture_project, background=True)
        while window._active_worker is not None and time.perf_counter() - started < 30:
            QtTest.QTest.qWait(5)
        elapsed = time.perf_counter() - started
        tick()  # Include the final install even if it completed between timer ticks.
        assert window._active_worker is None
        assert window.session.loaded
        if elapsed < 0.5:
            pytest.skip("Restore was too short for a meaningful frame-gap measurement")
        assert max(gaps) <= 0.1, f"UI gap {max(gaps) * 1000:.1f} ms over {elapsed:.2f} s"
    finally:
        timer.stop()
        window._cancel_restore()
        window.thread_pool.waitForDone(3000)
        app.processEvents()
        window.has_unsaved_changes = lambda: False
        if shiboken6.isValid(window):
            window.close()


def test_window_close_terminates_restore(app, fixture_project, monkeypatch):
    from GUI import main_window

    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args)))
    window = main_window.MainWindow(theme_mode="light")
    window.resize(1440, 900)
    task = None

    def close_on_progress(stage):
        window.close()

    window.restore_progress.connect(close_on_progress, QtCore.Qt.ConnectionType.QueuedConnection)
    try:
        window.load_project_file(fixture_project)
        task = window._restore_task
        started = time.perf_counter()
        while window._active_worker is not None and time.perf_counter() - started < 10:
            QtTest.QTest.qWait(5)
        assert task.cancelled
        assert task._process is None
        assert window._active_worker is None
        assert not window.session.loaded
    finally:
        window._cancel_restore()
        window.thread_pool.waitForDone(3000)
        app.processEvents()
        window.has_unsaved_changes = lambda: False
        if shiboken6.isValid(window):
            window.close()


def test_system_motion_preference_cached_and_overridden(monkeypatch):
    from GUI import busy_indicator

    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="1\n")

    busy_indicator._system_reduced_motion.cache_clear()
    monkeypatch.setattr(busy_indicator.sys, "platform", "darwin")
    monkeypatch.setattr(busy_indicator.subprocess, "run", read)
    monkeypatch.delenv("UPBRPY_REDUCE_MOTION", raising=False)
    try:
        assert busy_indicator.reduced_motion()
        assert busy_indicator.reduced_motion()
        assert len(calls) == 1
        assert calls[0][1]["timeout"] == 1
        monkeypatch.setenv("UPBRPY_REDUCE_MOTION", "0")
        assert not busy_indicator.reduced_motion()
        monkeypatch.setenv("UPBRPY_REDUCE_MOTION", "1")
        assert busy_indicator.reduced_motion()
        assert len(calls) == 1
    finally:
        busy_indicator._system_reduced_motion.cache_clear()
