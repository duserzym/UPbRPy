"""Dragging the floating controls moves a snapshot without repainting plots."""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtTest import QTest

from GUI import main_window
from GUI.main_window import MainWindow, _snap_plot_controls
from GUI.theme import build_application


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
LEFT = QtCore.Qt.MouseButton.LeftButton
NONE = QtCore.Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def settle(app):
    for _ in range(15):
        app.processEvents()


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow(theme_mode="light")
    window.resize(1440, 900)
    window.show()
    window.load_experiment_directory(str(RICH), background=False)
    window.reset_plot_controls()
    settle(app)
    yield window
    window.has_unsaved_changes = lambda: False
    window.close()
    window.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def press(window):
    grip = window.plot_control_grip
    local = grip.rect().center()
    start = grip.mapToGlobal(local)
    QTest.mousePress(grip, LEFT, NONE, local)
    return start


def move(window, global_point, buttons=LEFT):
    grip = window.plot_control_grip
    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove,
        QtCore.QPointF(grip.mapFromGlobal(global_point)), QtCore.QPointF(global_point),
        QtCore.Qt.MouseButton.NoButton, buttons, NONE)
    QtWidgets.QApplication.sendEvent(grip, event)


def release(window, global_point):
    grip = window.plot_control_grip
    QTest.mouseRelease(grip, LEFT, NONE, grip.mapFromGlobal(global_point))


class PaintCounter(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.count = 0

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Paint:
            self.count += 1
        return False


def assert_anchor(window, kind="plots"):
    scroll, stack = window.plot_control_scroll, window.plots_stack
    corner, dx, dy = window._plot_control_anchors[kind]
    assert scroll.x() == (dx if corner.endswith("left") else stack.width() - scroll.width() - dx)
    assert scroll.y() == (dy if corner.startswith("top") else stack.height() - scroll.height() - dy)


def test_ghost_tracks_thirty_moves_without_plot_paints(window, app, monkeypatch):
    scroll, stack = window.plot_control_scroll, window.plots_stack
    origin, size, area = scroll.pos(), scroll.size(), window._plot_control_area()
    start = press(window)
    settle(app)  # Drain the press/hover paints before measuring drag moves.
    assert window._plot_control_ghost is None
    counter = PaintCounter()
    views = window.findChildren(pg.GraphicsView)
    assert views
    for view in views:
        view.viewport().installEventFilter(counter)
    calls = []
    syncs = []
    area_reads = []
    original_move = window._move_plot_controls
    original_sync = window._sync_plot_control_overlay
    original_area = window._plot_control_area
    monkeypatch.setattr(window, "_move_plot_controls", lambda point: (calls.append(point), original_move(point)))
    monkeypatch.setattr(window, "_sync_plot_control_overlay", lambda: (syncs.append(True), original_sync()))
    monkeypatch.setattr(window, "_plot_control_area", lambda: (area_reads.append(True), original_area())[1])
    for index in range(30):
        delta = QtCore.QPoint(-QtWidgets.QApplication.startDragDistance() - 7 * index, 4 * index)
        move(window, start + delta)
        window._position_plot_controls()
        app.processEvents()
        ghost = window._plot_control_ghost
        expected = _snap_plot_controls(origin + delta, size, area)
        assert ghost.isVisible()
        assert ghost.pos() == stack.mapToGlobal(expected)
        assert ghost.pixmap().devicePixelRatio() == scroll.devicePixelRatioF()
        assert scroll.pos() == origin
        assert scroll.isVisible() and window.plot_control_grip.isVisible()
        assert QtWidgets.QApplication.overrideCursor().shape() == QtCore.Qt.CursorShape.ClosedHandCursor
        assert counter.count == 0, index
    assert counter.count == 0
    assert calls == []
    assert syncs == []
    assert len(area_reads) == 1
    release(window, start + delta)
    assert not ghost.isVisible()
    assert calls == [expected]
    assert len(syncs) == 1
    assert scroll.pos() == expected
    assert_anchor(window)
    assert QtWidgets.QApplication.overrideCursor() is None
    settle(app)
    assert scroll.pos() == expected


def test_click_and_double_click_do_not_show_ghost(window, app):
    origin = window.plot_control_scroll.pos()
    start = press(window)
    move(window, start + QtCore.QPoint(1, 0))
    release(window, start)
    settle(app)
    assert window._plot_control_ghost is None
    assert window.plot_control_grip.cursor().shape() == QtCore.Qt.CursorShape.OpenHandCursor
    assert window.plot_control_scroll.pos() == origin
    assert not window._plot_control_anchors
    assert QtWidgets.QApplication.overrideCursor() is None
    QTest.mouseDClick(window.plot_control_grip, LEFT, NONE, window.plot_control_grip.rect().center())
    settle(app)
    assert window._plot_control_vertical
    assert window._plot_control_ghost is None
    assert QtWidgets.QApplication.overrideCursor() is None


@pytest.mark.parametrize("point, expected", [
    ((-100, -100), (20, 30)),
    ((1000, 1000), (240, 190)),
    ((27, 38), (20, 30)),
    ((233, 183), (231, 190)),  # Default inset is nearer than the right edge.
    ((238, 180), (240, 180)),
    ((224, 100), (231, 100)),
    ((222, 100), (222, 100)),
    ((100, 100), (100, 100)),
])
def test_snap_helper(point, expected):
    assert _snap_plot_controls(QtCore.QPoint(*point), QtCore.QSize(80, 40),
                               QtCore.QRect(20, 30, 300, 200)) == QtCore.QPoint(*expected)


def test_snap_helper_handles_empty_and_undersized_areas():
    point = QtCore.QPoint(50, 60)
    assert _snap_plot_controls(point, QtCore.QSize(80, 40), QtCore.QRect()) == point
    assert _snap_plot_controls(point, QtCore.QSize(80, 40),
                               QtCore.QRect(20, 30, 10, 10)) == QtCore.QPoint(20, 30)


@pytest.mark.parametrize("drop", ["outside", "edge", "inset"])
def test_drag_clamps_and_snaps_on_drop(window, app, drop):
    scroll = window.plot_control_scroll
    area = window._plot_control_area()
    window._move_plot_controls(area.topLeft() + QtCore.QPoint(40, 50))
    settle(app)
    origin = scroll.pos()
    right = area.right() + 1 - scroll.width()
    targets = {
        "outside": area.topLeft() - QtCore.QPoint(1000, 1000),
        "edge": area.topLeft() + QtCore.QPoint(6, 7),
        "inset": QtCore.QPoint(right - 15, area.top() + 40),
    }
    target = targets[drop]
    start = press(window)
    move(window, start + target - origin)
    app.processEvents()
    expected = _snap_plot_controls(target, scroll.size(), area)
    assert window._plot_control_ghost.pos() == window.plots_stack.mapToGlobal(expected)
    release(window, start + target - origin)
    assert scroll.pos() == expected
    assert area.contains(scroll.geometry())
    assert_anchor(window)
    assert QtWidgets.QApplication.overrideCursor() is None


@pytest.mark.parametrize("ending", ["lost_release", "hide", "mode", "stack", "tab", "close"])
def test_interrupted_drag_finishes_and_restores_cursor(window, app, ending):
    start = press(window)
    move(window, start + QtCore.QPoint(-100, 50))
    app.processEvents()
    target = window._plot_control_drag.target
    assert window._plot_control_ghost.isVisible()
    if ending == "lost_release":
        move(window, start + QtCore.QPoint(-120, 70), QtCore.Qt.MouseButton.NoButton)
    elif ending == "hide":
        window.plot_control_grip.hide()
    elif ending == "mode":
        window.set_plot_mode("final_results", refresh=False)
    elif ending == "stack":
        window.plots_stack.setCurrentWidget(window.results_table)
    elif ending == "tab":
        window.results_table.tabs.setCurrentIndex(1)
    else:
        window.has_unsaved_changes = lambda: False
        window.close()
    assert window._plot_control_drag is None
    assert not window._plot_control_ghost.isVisible()
    assert QtWidgets.QApplication.overrideCursor() is None
    assert window.plot_control_scroll.pos() == target
    assert_anchor(window)
    assert "concordia" not in window._plot_control_anchors


def test_ghost_reused_and_prior_override_cursor_preserved(window, app):
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
    previous_ghost = None
    try:
        for _ in range(2):
            start = press(window)
            move(window, start + QtCore.QPoint(-50, 40))
            app.processEvents()
            ghost = window._plot_control_ghost
            if previous_ghost is not None:
                assert ghost is previous_ghost
            previous_ghost = ghost
            release(window, start + QtCore.QPoint(-50, 40))
            assert window._plot_control_ghost is ghost
            assert QtWidgets.QApplication.overrideCursor().shape() == QtCore.Qt.CursorShape.WaitCursor
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()


def test_reset_during_drag_clears_saved_places(window, app):
    start = press(window)
    move(window, start + QtCore.QPoint(-100, 50))
    window.reset_plot_controls()
    settle(app)
    assert window._plot_control_drag is None
    assert not window._plot_control_ghost.isVisible()
    assert not window._plot_control_anchors
    assert QtWidgets.QApplication.overrideCursor() is None
    area = window._plot_control_area()
    assert window.plot_control_scroll.pos() == QtCore.QPoint(
        area.right() + 1 - window.plot_control_scroll.width() - 9, area.top())


def test_moving_real_controls_does_not_schedule_overlay_timer(window, app):
    settle(app)
    timer = window._plot_control_overlay_timer
    assert not timer.isActive()
    window.plot_control_scroll.move(window.plot_control_scroll.pos() + QtCore.QPoint(-20, 20))
    assert not timer.isActive()
