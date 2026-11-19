"""Overview navigation and live splitter drags on a large synthetic session."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtTest import QTest
import pyqtgraph as pg

from GUI.main_window import MainWindow
from GUI.splitters import StudioSplitter
from GUI.studio_window import StudioWindow
from GUI.theme import build_application


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def settle(app):
    for _ in range(12):
        app.processEvents()


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr("GUI.main_window.Updater.start_scheduler", lambda self: None)
    QtCore.QSettings("UPbRPy", "Studio").clear()
    shell = StudioWindow(reduction_factory=lambda: MainWindow(theme_mode="light"))
    shell.resize(1440, 900)
    shell.show()
    window = shell.active_reduction
    x = np.repeat(np.arange(200) * 49., 100) + np.tile(np.arange(100) * .4255, 200)
    rng = np.random.default_rng(13)
    frame = pd.DataFrame({"Time": x, "spot_name": np.repeat([f"Spot_{i}" for i in range(200)], 100)})
    channels = ["206Pb", "207Pb", "238U"]
    for channel in channels:
        frame[channel] = rng.lognormal(8, 1, len(x))
    colors = dict.fromkeys(channels, "#6177a2")
    window.trace_plot.show_spot(frame, channels, colors, {"baseline": (0, 8), "plateau": (10, 30)},
                                selection_frame=frame.iloc[:100], stats_channel="238U")
    window.trace_plot.set_x_window((0, 45))
    window.session_overview_plot.show_series(frame, "238U", "#6177a2", (0, 45))
    settle(app)
    QTest.qWait(60)
    yield window
    window.has_unsaved_changes = lambda: False
    shell.close()
    shell.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    settle(app)


class RegionDrag:
    """The small portion of pyqtgraph's drag event used by LinearRegionItem."""
    def __init__(self, x, *, start=False, finish=False):
        self.x, self.start, self.finish = x, start, finish

    def button(self):
        return QtCore.Qt.MouseButton.LeftButton

    def accept(self):
        pass

    def isStart(self):
        return self.start

    def isFinish(self):
        return self.finish

    def buttonDownPos(self):
        return QtCore.QPointF(20, 0)

    def pos(self):
        return QtCore.QPointF(self.x, 0)


class PaintCounter(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.count = 0

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Paint:
            self.count += 1
        return False


def test_overview_drag_coalesces_and_defers_heavy_work(window, app, monkeypatch):
    overview, trace = window.session_overview_plot, window.trace_plot
    region = overview._window_region
    fits, applications, data_calls, labels, tooltips = [], [], [], [], []
    original = trace.set_x_window
    monkeypatch.setattr(trace, "set_x_window", lambda bounds: (applications.append(bounds), original(bounds)))
    monkeypatch.setattr(trace, "fit_y_to_visible_data", lambda: fits.append(True))
    monkeypatch.setattr(trace, "_layout_signal_labels", lambda: labels.append(True))
    monkeypatch.setattr(QtWidgets.QToolTip, "showText", lambda *args: tooltips.append(args))
    for curve in [*trace._series_items.values(), *trace._measurement_layers.values()]:
        original_set = curve.setData
        monkeypatch.setattr(curve, "setData", lambda *a, _original=original_set, **kw:
                            (data_calls.append(True), _original(*a, **kw)))
    curve, = overview.plot_item.listDataItems()
    assert curve.curve.cacheMode() == QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache
    paints = PaintCounter()
    trace.viewport().installEventFilter(paints)
    for index in range(15):
        paints.count = 0
        for offset in range(4):
            region.mouseDragEvent(RegionDrag(20 + index * 200 + offset, start=index == offset == 0))
        assert overview._region_dragging
        overview._on_mouse_moved((overview.plot_item.vb.mapViewToScene(QtCore.QPointF(1, 1000)),))
        before = len(applications)
        app.processEvents()
        app.processEvents()
        assert len(applications) == before + 1
        assert paints.count <= 1
        assert trace.visible_x_range() == pytest.approx(region.getRegion())
        assert all(item.curve.opts["antialias"] and item.curve.opts["segmentedLineMode"] == "on"
                   for item in trace._series_items.values())
        assert not fits and not data_calls and not labels and not tooltips
    # A pause while the mouse remains down must not run an idle y fit/glyph rebuild.
    QTest.qWait(70)
    assert not fits and not data_calls and not labels
    region.mouseDragEvent(RegionDrag(3500, finish=True))
    settle(app)
    QTest.qWait(100)
    assert fits == [True]
    assert trace.visible_x_range() == pytest.approx(region.getRegion())
    assert not trace._navigation_active and not overview._region_dragging
    assert all(item.curve.opts["antialias"] and item.curve.opts["segmentedLineMode"] == "auto"
               for item in trace._series_items.values())
    assert labels and len(data_calls) == len(trace._measurement_layers)
    assert all(item.opts["clipToView"] and item.opts["autoDownsample"]
               for item in trace._series_items.values())


def test_set_region_bursts_use_latest_bounds_once_per_turn(window, app, monkeypatch):
    calls = []
    original = window.trace_plot.set_x_window
    monkeypatch.setattr(window.trace_plot, "set_x_window", lambda bounds: (calls.append(bounds), original(bounds)))
    region = window.session_overview_plot._window_region
    for start in range(100, 150):
        region.setRegion((start, start + 45))
    assert calls == []
    app.processEvents()
    assert calls == [(149., 194.)]
    QTest.qWait(100)
    assert not window.trace_plot._navigation_active


def test_trace_window_sync_does_not_start_overview_navigation(window, app):
    overview = window.session_overview_plot
    changed, finished = [], []
    overview.windowChanged.connect(changed.append)
    overview.windowChangeFinished.connect(lambda: finished.append(True))
    for start in range(100, 105):
        overview.set_window((start, start + 45))
        settle(app)
        assert overview._window_region.getRegion() == (start, start + 45)
        assert not window.trace_plot._navigation_active
        assert window._pending_overview_window is None
    assert not changed and not finished


def test_every_studio_handle_has_cursor_and_unobstructed_hit_area(window, app):
    window.set_notes_log_visible(True)
    settle(app)
    splitters = window.window().findChildren(QtWidgets.QSplitter)
    assert len(splitters) == 3
    shell = window.window()
    origin = shell.pos()
    for splitter in splitters:
        assert isinstance(splitter, StudioSplitter)
        horizontal = splitter.orientation() == QtCore.Qt.Orientation.Horizontal
        shape = QtCore.Qt.CursorShape.SplitHCursor if horizontal else QtCore.Qt.CursorShape.SplitVCursor
        for index in range(1, splitter.count()):
            handle = splitter.handle(index)
            assert handle.isVisible()
            assert handle.cursor().shape() == shape
            # The default offscreen screen may be only 800 px wide. Place
            # each handle on that screen before asking Qt for its hit target.
            shell.move(shell.pos() + app.primaryScreen().geometry().center()
                       - handle.mapToGlobal(handle.rect().center()))
            settle(app)
            thickness = handle.width() if horizontal else handle.height()
            assert thickness >= 6
            for offset in range(thickness):
                point = handle.rect().center()
                if horizontal:
                    point.setX(offset)
                else:
                    point.setY(offset)
                assert app.widgetAt(handle.mapToGlobal(point)) is handle
    shell.move(origin)


@pytest.mark.parametrize("ending", ["release", "hide", "lost_release", "deactivate"])
def test_splitter_pins_cursor_and_restores_previous_override(window, app, ending):
    handle = window.workspace_splitter.handle(2)
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
    try:
        QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=handle.rect().center())
        assert QtWidgets.QApplication.overrideCursor().shape() == QtCore.Qt.CursorShape.SplitHCursor
        assert window.trace_plot._navigation_active
        if ending == "release":
            QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton, pos=handle.rect().center())
        elif ending == "hide":
            handle.hide()
        elif ending == "deactivate":
            app.sendEvent(handle, QtCore.QEvent(QtCore.QEvent.Type.WindowDeactivate))
        else:
            app.sendEvent(handle, QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(1, 1),
                          QtCore.QPointF(handle.mapToGlobal(QtCore.QPoint(1, 1))),
                          QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton,
                          QtCore.Qt.KeyboardModifier.NoModifier))
        assert not window.trace_plot._navigation_active
        assert QtWidgets.QApplication.overrideCursor().shape() == QtCore.Qt.CursorShape.WaitCursor
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()


def test_later_handles_and_orientation_changes(app):
    splitter = StudioSplitter(QtCore.Qt.Orientation.Horizontal)
    try:
        for _ in range(4):
            splitter.addWidget(QtWidgets.QWidget())
        assert all(splitter.handle(i).cursor().shape() == QtCore.Qt.CursorShape.SplitHCursor for i in range(4))
        splitter.setOrientation(QtCore.Qt.Orientation.Vertical)
        assert all(splitter.handle(i).cursor().shape() == QtCore.Qt.CursorShape.SplitVCursor for i in range(4))
    finally:
        splitter.deleteLater()


@pytest.mark.parametrize("index", [1, 2])
def test_live_workspace_resize_does_not_restroke_overview(window, app, monkeypatch, index):
    splitter = window.workspace_splitter
    handle = splitter.handle(index)
    series, = window.session_overview_plot.plot_item.listDataItems()
    curve = series.curve
    cached_curves = {curve, *(item.curve for item in window.trace_plot._series_items.values())}
    strokes = []
    original = pg.PlotCurveItem.paint

    def paint(item, *args):
        if item in cached_curves:
            strokes.append(item)
        return original(item, *args)

    monkeypatch.setattr(pg.PlotCurveItem, "paint", paint)
    start = handle.mapToGlobal(handle.rect().center())
    left = QtCore.Qt.MouseButton.LeftButton
    QTest.mousePress(handle, left, pos=handle.rect().center())
    assert splitter.opaqueResize()
    assert curve._resize_image is not None
    assert len(strokes) == len(cached_curves) and set(strokes) == cached_curves
    strokes.clear()
    initial_sizes = splitter.sizes()
    for step in range(1, 10):
        point = start + QtCore.QPoint(step * (4 if index == 1 else -4), 0)
        app.sendEvent(handle, QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove,
                      QtCore.QPointF(handle.mapFromGlobal(point)), QtCore.QPointF(point),
                      QtCore.Qt.MouseButton.NoButton, left, QtCore.Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        app.processEvents()
        assert strokes == []
    final_sizes = splitter.sizes()
    assert final_sizes != initial_sizes
    QTest.mouseRelease(handle, left, pos=handle.mapFromGlobal(point))
    settle(app)
    assert curve._resize_image is None
    assert strokes
    assert splitter.sizes() == final_sizes
    assert window.sidebar_panel.isVisible() and window.inspector_panel.isVisible()
    assert QtWidgets.QApplication.overrideCursor() is None
