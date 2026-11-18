"""Floating controls stay within the active plot, clear of review navigation."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window
from GUI.studio_window import StudioWindow
from GUI.theme import THEMES, build_application
from GUI.widgets import CALIBRATION_GRID
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def finalized():
    ages = np.linspace(490., 510., 40)
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({
        "spot_name": [f"A_{i}" for i in range(40)], "sample": "A",
        "207Pb/235U": x, "206Pb/238U": y,
        "238U/206Pb": tx, "207Pb/206Pb": ty,
    })
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[f"{ratio}_2e_with_calibration"] = frame[ratio] * .02
        frame[f"{ratio}_2e_no_calibration"] = frame[ratio] * .01
    return SimpleNamespace(parallel_results=frame, selected_results=frame.copy())


def settle(app):
    for _ in range(15):
        app.processEvents()


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(params=((1280, 800), (1440, 900)))
def studio(app, monkeypatch, request):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    shell = StudioWindow(theme_mode="light")
    window = shell.active_reduction
    try:
        shell.show()
        shell.resize(*request.param)
        window.apply_default_layout()
        settle(app)
        yield shell, window
    finally:
        window.has_unsaved_changes = lambda: False
        dispose(shell, app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)


def rect_in(widget, parent):
    return QtCore.QRect(widget.mapTo(parent, QtCore.QPoint()), widget.size())


def data_rect(plot, parent):
    item = (getattr(plot, "plot_item", None) or plot._plot_item
            or next(iter(plot._grid_items.values())))
    local = plot.mapFromScene(item.vb.sceneBoundingRect()).boundingRect()
    return QtCore.QRect(plot.mapTo(parent, local.topLeft()), local.size())


def assert_inside_plot(window, plot):
    controls = rect_in(window.plot_control_scroll, window)
    assert window.plot_control_scroll.isVisible()
    assert rect_in(plot, window).contains(controls)
    assert data_rect(plot, window).contains(controls), (data_rect(plot, window), controls)
    # The plot title occupies the row above the ViewBox.
    title_row = rect_in(plot, window)
    title_row.setBottom(data_rect(plot, window).top() - 1)
    assert not title_row.intersects(controls)
    assert window.plot_control_scroll.horizontalScrollBar().maximum() == 0
    assert not window._plot_control_overlay_timer.isActive()
    assert window.reset_scale_button.height() >= window.reset_scale_button.minimumSizeHint().height()
    return controls


def test_review_controls_clear_navigation_and_stay_in_active_plot(studio, app, finalized):
    _shell, window = studio
    review = window.results_table
    window.set_plot_mode("final_results", refresh=False)
    review.show_results(finalized)
    window.plots_stack.setCurrentWidget(review)
    for index in range(review.tabs.count()):
        review.tabs.setCurrentIndex(index)
        settle(app)
        assert not window.session.loaded
        assert not window.empty_state_overlay.isVisible()
        plot = review.active_concordia()
        if plot is None:
            assert not window.plot_control_scroll.isVisible()
            continue
        assert not window.plot_control_scroll.isVisible()
        toolbar = review.spot_toolbar if plot is review.spot_concordia else review.depth_toolbar
        assert rect_in(toolbar, window).bottom() < rect_in(plot, window).top()
        for offset in (-10000, 10000):
            window._plot_control_anchors[window._plot_control_kind()] = ("bottom_right", offset, offset)
            window._position_plot_controls()
            settle(app)
            assert not window.plot_control_scroll.isVisible()


def test_trace_controls_stay_in_trace_and_empty_state_follows_stack(studio, app):
    _shell, window = studio
    assert window.plots_stack.currentIndex() == 0
    assert window.empty_state_overlay.isVisible()
    assert_inside_plot(window, window.trace_plot)
    window._move_plot_controls(QtCore.QPoint(10000, 10000))
    settle(app)
    assert_inside_plot(window, window.trace_plot)
    window.plots_stack.setCurrentWidget(window.results_table)
    settle(app)
    assert not window.empty_state_overlay.isVisible()
    window.plots_stack.setCurrentIndex(0)
    settle(app)
    assert window.empty_state_overlay.isVisible()
    assert (window.empty_state_overlay.geometry().center()
            - window.plots_stack.rect().center()).manhattanLength() <= 2


def test_diagnostic_controls_stay_inside_each_plot(studio, app):
    _shell, window = studio
    times = np.linspace(0., 10., 5)
    factors = np.ones(5)
    calibration = {"channels": ["U", "Pb"], "channel_data": {
        channel: {"ppm": times, "cps": times * 100., "slope": .01}
        for channel in ("U", "Pb")}}
    window.calibration_slopes_plot.show_data(calibration, THEMES["light"])
    window.downhole_frac_plot.show_data({
        "ratio_name": "206Pb/238U", "primary_standard": "91500",
        "unified_time": times, "mean_frac": factors, "std_frac": .01,
        "spot_times": [times], "spot_ratios": [factors], "known_ratio": 1.,
    }, THEMES["light"])
    window.session_frac_plot.show_data({
        "ratio_name": "206Pb/238U", "primary_standard": "91500",
        "spot_mean_times": times, "spot_mean_fracs": factors,
        "spot_frac_errs": factors * .01, "fit_times": times,
        "fit_vals": factors, "fit_std": .01,
    }, THEMES["light"])
    for index in (1, 2, 3, 5):
        plot = window.plots_stack.widget(index)
        window.plots_stack.setCurrentIndex(index)
        settle(app)
        assert_inside_plot(window, plot)
        assert not window.empty_state_overlay.isVisible()
    window.plots_stack.setCurrentIndex(1)
    window.calibration_slopes_plot.set_channel(CALIBRATION_GRID)
    settle(app)
    assert_inside_plot(window, window.calibration_slopes_plot)
