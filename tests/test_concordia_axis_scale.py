"""Ratio ticks stay unscaled and default views tolerate large uncertainties."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtTest, QtWidgets
import pyqtgraph as pg

from GUI.theme import THEMES, build_application
from GUI.widgets import (ConcordiaReviewWidget, StandardBiasWidget,
                         _CompactAxis, _make_diag_axis)
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def canvas(app):
    widget = ConcordiaReviewWidget()
    widget.resize(1100, 700)
    widget.show()
    yield widget
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def ratio_frame(ages):
    ages = np.asarray(ages, dtype=float)
    frame = pd.DataFrame({"spot_name": [f"Z_{i}" for i in range(len(ages))],
                          "sample": "Z"})
    for ratio, values in zip(("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"),
                             (*wetherill_point(ages), *tera_wasserburg_point(ages))):
        frame[ratio] = values
        frame[ratio + "_2e"] = values * .02
    return frame


def show(canvas, frame, diagram="wetherill", **kwargs):
    canvas.show_data(frame, diagram=diagram, kind="spot", theme=THEMES["light"],
                     include_calibration_uncertainty=True, **kwargs)
    QtWidgets.QApplication.processEvents()


def assert_ticks(plot, name, value, expected):
    axis = plot.getAxis(name)
    assert not axis.autoSIPrefix
    assert axis.autoSIPrefixScale == 1.0
    assert axis.labelUnitPrefix == ""
    assert axis.tickStrings([value], axis.autoSIPrefixScale, .01) == [expected]


@pytest.mark.parametrize("age", [560., 1500.])
@pytest.mark.parametrize("previous", ["empty", "sub_one"])
@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
def test_ratio_ticks_after_rebuild_and_retained_range(canvas, app, age, previous, diagram):
    frame = ratio_frame([age - 5, age, age + 5])
    show(canvas, frame, diagram)
    if previous == "empty":
        show(canvas, frame.iloc[:0], diagram)
        np.testing.assert_allclose(canvas._plot_item.vb.viewRange(), [[0, 1], [0, 1]])
    else:
        canvas._plot_item.setRange(xRange=(.2, .8), yRange=(.1, .9), padding=0)
    show(canvas, frame, diagram, reset_view=False)
    if previous == "sub_one":
        np.testing.assert_allclose(canvas._plot_item.vb.viewRange(), [[.2, .8], [.1, .9]])
    left, bottom = ((.26, "0.26"), (.73, "0.73")) if diagram == "wetherill" else ((.09, "0.09"), (11., "11.00"))
    assert_ticks(canvas._plot_item, "left", *left)
    assert_ticks(canvas._plot_item, "bottom", *bottom)
    # Rebuilding labelled axes must also work from the data's own range.
    canvas._draw()
    app.processEvents()
    assert_ticks(canvas._plot_item, "left", *left)
    assert_ticks(canvas._plot_item, "bottom", *bottom)


@pytest.mark.parametrize("age", [560., 1500.])
def test_switching_diagrams_does_not_keep_previous_scale(canvas, age):
    frame = ratio_frame([age - 5, age, age + 5])
    for diagram in ("wetherill", "tera_wasserburg", "wetherill", "tera_wasserburg"):
        show(canvas, frame, diagram)
        if diagram == "tera_wasserburg":
            assert_ticks(canvas._plot_item, "left", .09, "0.09")
            assert_ticks(canvas._plot_item, "bottom", 11., "11.00")
        else:
            assert_ticks(canvas._plot_item, "left", .26, "0.26")
            assert_ticks(canvas._plot_item, "bottom", .73, "0.73")


def test_label_and_prefix_refresh_paths_keep_disabled_scale(canvas, app):
    show(canvas, ratio_frame([1500.]))
    for name in ("left", "bottom"):
        axis = canvas._plot_item.getAxis(name)
        axis.setRange(.2, .8)
        axis.setLabel("Ratio")
        axis.showLabel(False)
        axis.showLabel(True)
        axis.updateAutoSIPrefix()
        app.processEvents()
        assert_ticks(canvas._plot_item, name, .26, "0.26")
        axis.enableAutoSIPrefix(False)
        assert_ticks(canvas._plot_item, name, .26, "0.26")


def test_secondary_standard_ticks_after_redraw(app):
    widget = StandardBiasWidget()
    widget.resize(900, 600)
    widget.show()
    try:
        table = pd.DataFrame({"spot_name": ["S513_1", "S513_2"], "sample": "S513",
                              "x": [.3, .6], "offset_percent": [.2, .4],
                              "offset_2s_percent": .01, "plottable": True})
        widget.show_bias(table, {}, theme=THEMES["light"],
                         x_label="U, ug/g", y_label="Offset (%)")
        widget._plot_item.setRange(xRange=(.2, .8), yRange=(.1, .9), padding=0)
        app.processEvents()
        widget.apply_theme(THEMES["light"])
        app.processEvents()
        for name in ("left", "bottom"):
            assert_ticks(widget._plot_item, name, .26, "0.26")
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("compact", [False, True])
def test_other_diagnostic_axes_never_scale_without_a_hint(app, compact):
    axes = {name: _CompactAxis(name) for name in ("left", "bottom")} if compact else None
    widget = pg.PlotWidget(axisItems=axes)
    widget.show()
    try:
        plot = widget.getPlotItem()
        _make_diag_axis(plot, THEMES["light"])
        for labelled in (False, True):
            for name in ("left", "bottom"):
                axis = plot.getAxis(name)
                if labelled:
                    axis.setLabel("Ratio")
                axis.setRange(.2, .8)
                app.processEvents()
                if axis.autoSIPrefixScale != 1.0:
                    assert axis.label.isVisible()
                    assert f"(x{1 / axis.autoSIPrefixScale:g})" in axis.labelString()
                else:
                    assert axis.tickStrings([.26], axis.autoSIPrefixScale, .01) == ["0.26"]
    finally:
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("x_offset", [0., 10.])
def test_default_range_limits_large_errors_and_resets(canvas, app, x_offset):
    frame = ratio_frame([1500., 2000., 2500., 3000.])
    frame["207Pb/235U"] = np.array([3., 8., 13., 20.]) + x_offset
    frame["207Pb/235U_2e"] = [.1, 20., .1, .1]
    frame["206Pb/238U_2e"] = [20., .01, .01, .01]
    show(canvas, frame)
    default = np.asarray(canvas._default_range)
    assert default[0, 0] >= max(0., 3. + x_offset - .25 * (20. - 3.))
    assert default[0, 1] <= 20. + x_offset + .25 * (20. - 3.)
    assert np.all(default[:, 0] >= 0)
    assert np.all(canvas._point_xy >= default[:, 0])
    assert np.all(canvas._point_xy <= default[:, 1])
    cx, cy = canvas._concordia_coordinates
    assert np.any((cx >= default[0, 0]) & (cx <= default[0, 1])
                  & (cy >= default[1, 0]) & (cy <= default[1, 1]))
    # Limiting the frame must not shrink the actual uncertainty ellipse.
    assert canvas._visible_errors[1, 0] == 20.
    for reset in (canvas.restore_default_view,
                  lambda: QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_0)):
        canvas._plot_item.setRange(xRange=(5, 6), yRange=(.2, .3), padding=0)
        reset()
        app.processEvents()
        np.testing.assert_allclose(canvas._plot_item.vb.viewRange(), default)


@pytest.mark.parametrize("ages", [[560.], [1500., 1500.]])
@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
def test_default_range_with_no_centre_span_is_finite(canvas, ages, diagram):
    frame = ratio_frame(ages)
    for column in frame.filter(like="_2e"):
        frame[column] = np.nan
    show(canvas, frame, diagram)
    bounds = np.asarray(canvas._default_range)
    assert np.isfinite(bounds).all()
    assert (bounds[:, 0] >= 0).all()
    assert (bounds[:, 1] > bounds[:, 0]).all()
    assert np.all(canvas._point_xy >= bounds[:, 0])
    assert np.all(canvas._point_xy <= bounds[:, 1])
