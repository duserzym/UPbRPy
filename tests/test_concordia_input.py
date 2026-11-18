"""Weighted dates and concordia-only lab gestures use shared viewer state."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget
from UPbRPy.isochron import wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def review(app, tmp_path, monkeypatch):
    import UPbRPy.isochron as isochron
    def forbidden(*args, **kwargs):
        pytest.fail("Studio must not calculate a concordia age")
    monkeypatch.setattr(isochron, "concordia_age", forbidden)
    monkeypatch.setattr(isochron, "concordia_age_readout", forbidden)
    settings = QtCore.QSettings(str(tmp_path / "viewer.ini"), QtCore.QSettings.Format.IniFormat)
    # An implicitly saved old Pan must not override the new default.
    settings.setValue("review_concordia_mouse_v1", "pan")
    widget = ResultsTableWidget(settings=settings)
    x, y = wetherill_point(np.array([500., 510., 520., 530.]))
    frame = pd.DataFrame({"spot_name": ["A_1", "A_2", "B_1", "C_1"],
                          "sample": ["A", "A", "B", "C"],
                          "206Pb/238U": y, "207Pb/235U": x,
                          "206Pb/238U_2e": y*.02, "207Pb/235U_2e": x*.02})
    for index, system in enumerate(("206Pb/238U", "207Pb/235U", "207Pb/206Pb")):
        frame[system + "_age"] = np.array([100., 104., 108., 112.])+index*10
        frame[system + "_age_2e_with_calibration"] = [2., 4., 2., 4.]
        frame[system + "_age_2e_no_calibration"] = [1., 2., 1., 2.]
    widget.apply_theme(THEMES["light"])
    widget.show_results(SimpleNamespace(selected_results=frame.copy(), parallel_results=frame))
    widget.tabs.setCurrentWidget(widget.spot_concordia_page)
    widget.resize(1200, 700)
    widget.show()
    for _ in range(5): app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_weighted_dates_selected_shown_hidden_and_uncertainty_branch(review, app):
    result = review.weighted_mean_dates["206Pb/238U"]
    # Weights .25, .0625, .25, .0625, using absolute 2sigma inputs.
    assert result["weighted_mean"] == pytest.approx(104.8)
    assert result["weighted_mean_2e"] == pytest.approx(np.sqrt(1/.625))
    assert result["n"] == 4
    assert "Included 4" in review.spot_age_label.text()
    assert "MSWD" in review.spot_age_label.toolTip() and "n=4" in review.spot_age_label.toolTip()
    review.set_selected_spots({"A_1", "A_2"})
    app.processEvents()
    assert review.weighted_mean_dates["206Pb/238U"]["weighted_mean"] == pytest.approx(100.8)
    review.set_spot_excluded({"A_2"}, True)
    app.processEvents()
    assert "Selected 1 of 2 used" in review.spot_age_label.text()
    for result in review.weighted_mean_dates.values():
        assert result["n"] == 1 and result["weighted_mean_2e"] == 2.
    review._include_calibration_uncertainty = False
    review._update_age_readout()
    assert review.weighted_mean_dates["206Pb/238U"]["weighted_mean_2e"] == 1.
    review.set_selected_spots(set())
    review.set_spot_included({"C_1"}, False)
    app.processEvents()
    assert review.weighted_mean_dates["206Pb/238U"]["n"] == 2
    assert not hasattr(review.spot_concordia, "_age_marker")
    assert not hasattr(review, "concordia_age_label")
    assert not hasattr(review.spot_concordia, "set_concordia_age")


def test_plain_chip_toggle_partial_option_range_keyboard_and_one_update(review, app, monkeypatch):
    area = review.spot_sample_chips
    menu = area.menu_button.menu()
    menu.popup(review.mapToGlobal(QtCore.QPoint(10, 10)))
    app.processEvents()
    calls = []
    original = review._set_spot_inclusions
    def change(changes):
        calls.append(changes.copy())
        original(changes)
    monkeypatch.setattr(review, "_set_spot_inclusions", change)
    chip = area.chips["A"]
    QtTest.QTest.mouseClick(chip, QtCore.Qt.MouseButton.LeftButton)
    assert len(calls) == 1 and calls[0] == {"A_1": False, "A_2": False}
    assert review.include_flags["B_1"] and review.include_flags["C_1"]
    QtTest.QTest.mouseClick(chip, QtCore.Qt.MouseButton.LeftButton)
    assert review.include_flags["A_1"] and review.include_flags["A_2"]
    review.set_spot_included({"A_1"}, False)
    QtTest.QTest.mouseClick(chip, QtCore.Qt.MouseButton.LeftButton)
    assert review.include_flags["A_1"] and review.include_flags["A_2"]
    QtTest.QTest.mouseClick(chip, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.AltModifier)
    assert not review.include_flags["B_1"] and not review.include_flags["C_1"]
    QtTest.QTest.mouseClick(area.chips["C"], QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert all(review.include_flags.values())
    chip.setFocus()
    QtTest.QTest.keyClick(chip, QtCore.Qt.Key.Key_Space)
    assert not review.include_flags["A_1"]
    QtTest.QTest.keyClick(chip, QtCore.Qt.Key.Key_Return)
    assert review.include_flags["A_1"]
    menu.hide()


class Drag:
    def __init__(self, button, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
        self._button, self._modifiers = button, modifiers
    def button(self): return self._button
    def modifiers(self): return self._modifiers
    def pos(self): return pg.Point(220, 170)
    def lastPos(self): return pg.Point(180, 140)
    def buttonDownPos(self, *args): return pg.Point(180, 140)
    def isFinish(self): return True
    def accept(self): pass


@pytest.mark.parametrize("middle", [False, True])
def test_space_drag_and_middle_drag_pan_in_select_mode(review, app, middle):
    canvas = review.spot_concordia
    assert canvas.interaction_mode() == "select"
    canvas.setFocus()
    if not middle:
        QtTest.QTest.keyPress(canvas, QtCore.Qt.Key.Key_Space)
        assert canvas._space_pan
        assert canvas.viewport().cursor().shape() == QtCore.Qt.CursorShape.OpenHandCursor
    vb = canvas._plot_item.vb
    before = np.array(vb.viewRange())
    vb.mouseDragEvent(Drag(QtCore.Qt.MouseButton.MiddleButton if middle else QtCore.Qt.MouseButton.LeftButton))
    after = np.array(vb.viewRange())
    np.testing.assert_allclose(np.diff(after, axis=1), np.diff(before, axis=1))
    assert not np.allclose(after, before)
    assert not review.selected_spots
    if not middle:
        QtTest.QTest.keyRelease(canvas, QtCore.Qt.Key.Key_Space)
        assert not canvas._space_pan


def test_trackpad_pans_mouse_wheel_and_modified_trackpad_zoom(review, app):
    canvas = review.spot_concordia
    vb = canvas._plot_item.vb
    pos = QtCore.QPointF(canvas.viewport().rect().center())
    def wheel(pixel, angle, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
        event = QtGui.QWheelEvent(pos, pos, pixel, angle, QtCore.Qt.MouseButton.NoButton,
                                 modifiers, QtCore.Qt.ScrollPhase.NoScrollPhase, False)
        QtWidgets.QApplication.sendEvent(canvas.viewport(), event)
        assert event.isAccepted()
    before = np.array(vb.viewRange())
    wheel(QtCore.QPoint(12, 20), QtCore.QPoint())
    after = np.array(vb.viewRange())
    np.testing.assert_allclose(np.diff(after, axis=1), np.diff(before, axis=1))
    assert not np.allclose(after, before)
    wheel(QtCore.QPoint(), QtCore.QPoint(0, 120))
    zoomed = np.array(vb.viewRange())
    assert np.all(np.diff(zoomed, axis=1) < np.diff(after, axis=1))
    review.set_zoom_mode("x")
    wheel(QtCore.QPoint(0, 20), QtCore.QPoint(), QtCore.Qt.KeyboardModifier.ControlModifier)
    limited = np.array(vb.viewRange())
    np.testing.assert_allclose(limited[1], zoomed[1])
    assert np.diff(limited[0])[0] < np.diff(zoomed[0])[0]


def test_command_drag_toggles_and_versioned_choice_is_restored(review, app):
    canvas = review.spot_concordia
    vb = canvas._plot_item.vb
    review.set_selected_spots({"A_1", "A_2"})
    def selected(rect, **kwargs):
        canvas.select_spots({"A_2", "B_1"}, **kwargs)
    canvas.select_rectangle = selected
    vb.mouseDragEvent(Drag(QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.ControlModifier))
    assert review.selected_spots == {"A_1", "B_1"}
    review.set_interaction_mode("pan")
    settings = review._settings
    assert settings.value("review_concordia_mouse_v2") == "pan"
    other = ResultsTableWidget(settings=settings)
    try:
        assert other.spot_concordia.interaction_mode() == "pan"
        assert other.depth_concordia.interaction_mode() == "pan"
    finally:
        other.deleteLater()
    settings.remove("review_concordia_mouse_v2")
    settings.setValue("review_concordia_mouse_v1", "zoom")
    migrated = ResultsTableWidget(settings=settings)
    try:
        assert migrated.spot_concordia.interaction_mode() == "zoom"
        assert settings.value("review_concordia_mouse_v2") == "zoom"
    finally:
        migrated.deleteLater()


def test_native_pinch_zooms_selected_axis_about_fingers(review):
    canvas = review.spot_concordia
    vb = canvas._plot_item.vb
    review.set_zoom_mode("y")
    pos = QtCore.QPointF(canvas.viewport().rect().center())
    event = QtGui.QNativeGestureEvent(QtCore.Qt.NativeGestureType.ZoomNativeGesture,
        QtGui.QPointingDevice.primaryPointingDevice(), 2, pos, pos, pos, .15, QtCore.QPointF())
    before = np.array(vb.viewRange())
    QtWidgets.QApplication.sendEvent(canvas.viewport(), event)
    after = np.array(vb.viewRange())
    assert event.isAccepted()
    np.testing.assert_allclose(after[0], before[0])
    assert np.diff(after[1])[0] < np.diff(before[1])[0]
