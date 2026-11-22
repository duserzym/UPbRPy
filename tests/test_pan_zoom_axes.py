"""Zoom-axis choices leave plot panning free in both directions."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from GUI.theme import THEMES, build_application
from GUI.widgets import (
    CalibrationSlopesWidget,
    ConcordiaReviewWidget,
    DownholeFractionationWidget,
    SessionFractionationWidget,
    StandardBiasWidget,
    TracePlotWidget,
)
from UPbRPy.isochron import wetherill_point


class Drag:
    def __init__(self, button=QtCore.Qt.MouseButton.LeftButton, *, start=None, end=None, finish=True):
        self._button = button
        self.start = pg.Point(120, 100) if start is None else pg.Point(start)
        self.end = pg.Point(180, 140) if end is None else pg.Point(end)
        self.finish = finish
        self.accepted = False

    def button(self):
        return self._button

    def pos(self):
        return self.end

    def lastPos(self):
        return self.start

    def screenPos(self):
        return self.end

    def lastScreenPos(self):
        return self.start

    def buttonDownPos(self, *args):
        return self.start

    def isStart(self):
        return False

    def isFinish(self):
        return self.finish

    def modifiers(self):
        return QtCore.Qt.KeyboardModifier.NoModifier

    def accept(self):
        self.accepted = True


class Wheel:
    def __init__(self):
        self.accepted = False

    def delta(self):
        return 120

    def pos(self):
        return pg.Point(150, 120)

    def accept(self):
        self.accepted = True


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def populate_concordia(widget):
    x, y = wetherill_point(np.array([300., 500., 700.]))
    frame = pd.DataFrame({
        "spot_name": ["A_1", "A_2", "B_1"], "sample": ["A", "A", "B"],
        "207Pb/235U": x, "206Pb/238U": y,
        "207Pb/235U_2e": [.01] * 3, "206Pb/238U_2e": [.002] * 3,
    })
    widget.show_data(frame, diagram="wetherill", kind="spot",
                     include_calibration_uncertainty=True, theme=THEMES["light"])


@pytest.fixture(params=["concordia", "downhole", "session", "calibration", "trace", "bias"])
def plot(app, request):
    theme = THEMES["light"]
    if request.param == "concordia":
        widget = ConcordiaReviewWidget()
        populate_concordia(widget)
    elif request.param == "downhole":
        widget = DownholeFractionationWidget()
        times = np.array([0., 1., 2.])
        widget.show_data({
            "ratio_name": "206Pb/238U", "unified_time": times,
            "mean_frac": np.array([1.1, 1., .9]), "std_frac": .02,
            "primary_standard": "91500", "known_ratio": .1,
            "spot_times": [times], "spot_ratios": [np.array([.09, .1, .11])],
        }, theme)
    elif request.param == "session":
        widget = SessionFractionationWidget()
        times = np.array([0., 1., 2.])
        factors = np.array([1.1, 1., .9])
        widget.show_data({
            "ratio_name": "206Pb/238U", "primary_standard": "91500",
            "spot_mean_times": times, "spot_mean_fracs": factors,
            "spot_frac_errs": np.full(3, .02), "fit_times": times,
            "fit_vals": factors, "fit_std": .02,
        }, theme)
    elif request.param == "calibration":
        widget = CalibrationSlopesWidget()
        widget.show_data({"channels": ["238U"], "channel_data": {
            "238U": {"ppm": [1., 2., 3.], "cps": [10., 20., 30.], "slope": .1},
        }}, theme)
    elif request.param == "trace":
        widget = TracePlotWidget()
    else:
        widget = StandardBiasWidget()
    widget.resize(800, 500)
    widget.show()
    app.processEvents()
    vb = widget.plot_item.vb if request.param == "trace" else widget._plot_item.vb
    vb.setRange(xRange=(10, 30), yRange=(20, 60), padding=0)
    try:
        yield widget, vb
    finally:
        # Hide before pyqtgraph replaces its viewport during close, so resize
        # callbacks cannot redraw graphics items that close has just deleted.
        widget.hide()
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("mode", ["xy", "x", "y"])
def test_pan_moves_both_axes_in_pan_and_box_modes(plot, mode):
    widget, vb = plot
    widget.set_zoom_mode(mode)
    assert vb.zoom_axes() == mode
    assert vb.state["mouseEnabled"] == [True, True]
    masks = []
    vb.sigRangeChangedManually.connect(masks.append)
    for interaction, button in (("pan", QtCore.Qt.MouseButton.LeftButton),
                                ("pan", QtCore.Qt.MouseButton.MiddleButton),
                                ("zoom", QtCore.Qt.MouseButton.MiddleButton)):
        widget.set_interaction_mode(interaction)
        before = np.array(vb.viewRange())
        event = Drag(button)
        vb.mouseDragEvent(event)
        after = np.array(vb.viewRange())
        assert event.accepted
        assert all(not np.allclose(before[axis], after[axis]) for axis in (0, 1))
        np.testing.assert_allclose(np.ptp(after, axis=1), np.ptp(before, axis=1))
        assert masks[-1] == [True, True]


@pytest.mark.parametrize("mode", ["xy", "x", "y"])
@pytest.mark.parametrize("gesture", ["wheel", "right-drag", "box", "box-drag"])
def test_zoom_changes_only_chosen_axes(plot, mode, gesture):
    widget, vb = plot
    widget.set_zoom_mode(mode)
    widget.set_interaction_mode("zoom")
    masks = []
    vb.sigRangeChangedManually.connect(masks.append)
    before = np.array(vb.viewRange())
    if gesture == "wheel":
        event = Wheel()
        vb.wheelEvent(event)
        assert event.accepted
    elif gesture == "right-drag":
        vb.mouseDragEvent(Drag(QtCore.Qt.MouseButton.RightButton))
    elif gesture == "box":
        vb.showAxRect(QtCore.QRectF(12, 25, 6, 10), padding=0)
    else:
        vb.mouseDragEvent(Drag())
    after = np.array(vb.viewRange())
    expected = [mode != "y", mode != "x"]
    for axis, changed in enumerate(expected):
        assert (not np.isclose(np.ptp(before[axis]), np.ptp(after[axis]))) == changed
        if not changed:
            np.testing.assert_allclose(after[axis], before[axis])
    assert masks == [expected]


@pytest.mark.parametrize("mode", ["xy", "x", "y"])
@pytest.mark.parametrize("axis", [0, 1])
def test_axis_item_pan_and_wheel_use_only_that_axis(plot, mode, axis):
    widget, vb = plot
    widget.set_zoom_mode(mode)
    widget.set_interaction_mode("zoom")
    masks = []
    vb.sigRangeChangedManually.connect(masks.append)
    before = np.array(vb.viewRange())
    vb.mouseDragEvent(Drag(), axis=axis)
    panned = np.array(vb.viewRange())
    assert not np.allclose(panned[axis], before[axis])
    np.testing.assert_allclose(panned[1-axis], before[1-axis])
    np.testing.assert_allclose(np.ptp(panned, axis=1), np.ptp(before, axis=1))
    vb.wheelEvent(Wheel(), axis=axis)
    after = np.array(vb.viewRange())
    assert np.ptp(after[axis]) < np.ptp(panned[axis])
    np.testing.assert_allclose(after[1-axis], panned[1-axis])
    assert masks == [[axis == 0, axis == 1]] * 2


@pytest.mark.parametrize("mode", ["xy", "x", "y"])
def test_concordia_select_drag_and_double_click_reset(app, mode):
    widget = ConcordiaReviewWidget()
    populate_concordia(widget)
    widget.resize(800, 500)
    widget.show()
    app.processEvents()
    try:
        vb = widget._plot_item.vb
        widget.set_zoom_mode(mode)
        widget.set_interaction_mode("select")
        before = np.array(vb.viewRange())
        masks = []
        vb.sigRangeChangedManually.connect(masks.append)
        point = vb.mapFromView(QtCore.QPointF(*widget._point_xy[0]))
        for finish in (False, True):
            event = Drag(start=point - pg.Point(5, 5), end=point + pg.Point(5, 5), finish=finish)
            vb.mouseDragEvent(event)
            assert event.accepted
            np.testing.assert_allclose(vb.viewRange(), before)
        assert widget.selected_spots == {"A_1"}
        assert masks == []

        # Middle drag remains a pan gesture even while left drag selects.
        vb.mouseDragEvent(Drag(QtCore.Qt.MouseButton.MiddleButton))
        panned = np.array(vb.viewRange())
        assert all(not np.allclose(before[axis], panned[axis]) for axis in (0, 1))
        assert widget.selected_spots == {"A_1"}
        assert masks == [[True, True]]

        vb.setRange(xRange=(10, 30), yRange=(20, 60), padding=0)

        class DoubleClick:
            def double(self):
                return True

            def button(self):
                return QtCore.Qt.MouseButton.LeftButton

            def scenePos(self):
                return vb.sceneBoundingRect().center()

            def accept(self):
                pass

        vb.mouseClickEvent(DoubleClick())
        np.testing.assert_allclose(vb.viewRange(), widget._default_range)
        assert widget.selected_spots == {"A_1"}
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_plot_toolbar_zoom_axis_text_and_single_row(app):
    from GUI.main_window import MainWindow

    window = MainWindow(theme_mode="light")
    window.resize(1280, 800)
    window.show()
    window.apply_default_layout()
    for _ in range(10):
        app.processEvents()
    try:
        window.set_plot_controls_vertical(False)
        app.processEvents()
        bar = window.plot_control_bar
        assert isinstance(bar.layout(), QtWidgets.QHBoxLayout)
        labels = [label.text() for label in bar.findChildren(QtWidgets.QLabel)]
        assert "Zoom axis" in labels
        assert "Drag" in labels
        assert window.interact_mode_buttons["pan"].toolTip() == "Drag to pan in x and y"
        assert {mode: button.toolTip() for mode, button in window.zoom_mode_buttons.items()} == {
            "xy": "Zoom both axes (wheel and box)",
            "x": "Zoom the x axis only; panning still moves both axes",
            "y": "Zoom the y axis only; panning still moves both axes",
        }
        buttons = [button for button in bar.findChildren(QtWidgets.QToolButton)
                   if button.isVisibleTo(bar)]
        centers = []
        for button in buttons:
            rect = QtCore.QRect(button.mapTo(bar, QtCore.QPoint()), button.size())
            assert bar.rect().contains(rect)
            centers.append(rect.center().y())
        assert max(centers) - min(centers) <= 1
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
