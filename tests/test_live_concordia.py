"""Live ages, persistent colour choices and depth colour orientation."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def finalized():
    x, y = wetherill_point(np.full(4, 500.))
    tx, ty = tera_wasserburg_point(np.full(4, 500.))
    frame = pd.DataFrame({"spot_name": ["A_1", "A_2", "B_3", "B_4"],
                          "sample": ["A", "A", "B", "B"],
                          "207Pb/235U": x, "206Pb/238U": y,
                          "238U/206Pb": tx, "207Pb/206Pb": ty})
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[f"{ratio}_2e_with_calibration"] = frame[ratio] * .02
        frame[f"{ratio}_2e_no_calibration"] = frame[ratio] * .01
    return SimpleNamespace(parallel_results=frame, selected_results=frame.copy())


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def review(app, finalized):
    widget = ResultsTableWidget()
    try:
        widget.apply_theme(THEMES["light"])
        widget.show_results(finalized)
        widget.tabs.setCurrentWidget(widget.spot_concordia_page)
        widget.resize(1000, 550)
        widget.show()
        app.processEvents()
        yield widget
    finally:
        dispose(widget, app)


def test_live_readout_follows_shared_selection_inclusion_diagram_and_results(review, app, finalized):
    widget = review
    assert widget.concordia_age_label.text().startswith("Included: Concordia age")
    assert "n = 4" in widget.concordia_age_label.text()
    assert widget.concordia_readout.age_ma == pytest.approx(500, abs=.001)
    marker = widget.spot_concordia._age_marker
    np.testing.assert_allclose([data[0] for data in marker.getData()], wetherill_point(500.))
    widget.set_selected_spots({"A_1", "B_3"})
    app.processEvents()
    assert widget.concordia_age_label.text().startswith("Selected: Concordia age")
    assert "n = 2" in widget.concordia_age_label.text()
    widget.set_spot_included({"B_3"}, False)
    app.processEvents()
    assert widget.concordia_readout.n == 1
    assert "1 of 2 selected are included" in widget.concordia_age_label.toolTip()
    previous = widget.concordia_readout
    widget.spot_diagram_buttons["tera_wasserburg"].click()
    app.processEvents()
    assert widget.concordia_readout.space == "tera_wasserburg"
    assert widget.concordia_readout.age_2se != previous.age_2se
    marker = widget.spot_concordia._age_marker
    np.testing.assert_allclose([data[0] for data in marker.getData()], tera_wasserburg_point(500.))
    finalized.decision = SimpleNamespace(include_calibration_uncertainty=False)
    before = widget.concordia_readout.age_2se
    widget.show_results(finalized)
    app.processEvents()
    assert widget.concordia_readout.age_2se == pytest.approx(before / 2, rel=.001)
    widget.set_selected_spots(set())
    app.processEvents()
    assert "Included:" in widget.concordia_age_label.text()
    assert widget.concordia_readout.n == 3
    widget.set_all_spot_choices(False)
    app.processEvents()
    assert "No concordia age for this set" in widget.concordia_age_label.text()
    assert widget.concordia_readout is None
    assert len(widget.spot_concordia._age_marker.getData()[0]) == 0


def test_readout_debounces_selection_changes_and_does_not_fit_on_zoom(review, app, monkeypatch):
    import GUI.widgets as widgets

    calls = []
    original = widgets.concordia_age_readout

    def fit(frame, **kwargs):
        calls.append(len(frame))
        return original(frame, **kwargs)

    monkeypatch.setattr(widgets, "concordia_age_readout", fit)
    for names in ({"A_1"}, {"A_1", "A_2"}, {"A_1", "A_2", "B_3"}):
        review.set_selected_spots(names)
    assert calls == []
    app.processEvents()
    assert calls == [3]
    review.spot_concordia._plot_item.setRange(xRange=(.1, .3), yRange=(.01, .05))
    app.processEvents()
    assert calls == [3]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_sample_default_single_colour_selection_and_label_fit(review, app, theme):
    review.apply_theme(THEMES[theme])
    app.processEvents()
    plot = review.spot_concordia
    assert review.spot_colour_mode() == "sample"
    assert [label.text for _, label in plot._legend.items] == ["A", "B"]
    colours = [colour.name() for _, _, _, colour in plot._points]
    assert colours[0] == colours[1] != colours[2] == colours[3]
    review.spot_colour_buttons["single"].click()
    assert plot._legend is None
    assert {color.name() for _, _, _, color in plot._points} == {QtGui.QColor(THEMES[theme].accent).name()}
    assert {curve.opts["pen"].color().name() for _, curve, _ in plot._ellipses} == {
        QtGui.QColor(THEMES[theme].accent).name()}
    review.set_selected_spots({"A_1"})
    assert plot._scatter.points()[0].size() > plot._scatter.points()[1].size()
    app.processEvents()
    label = review.concordia_age_label
    bounds = label.fontMetrics().boundingRect(
        QtCore.QRect(0, 0, label.contentsRect().width(), 1000),
        int(QtCore.Qt.TextFlag.TextWordWrap), label.text())
    assert bounds.height() <= label.height()
    assert bounds.height() <= label.fontMetrics().lineSpacing() * 3 + 6
    assert review.spot_choice_list.height() >= 100


def luminance(color):
    rgb = np.array([color.redF(), color.greenF(), color.blueF()])
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    return linear @ [.2126, .7152, .0722]


@pytest.mark.parametrize("use_time", [False, True])
@pytest.mark.parametrize("cmap", ["plasma", "viridis", "CET-L1"])
def test_depth_map_shallow_lighter_and_recolour_preserves_range(review, finalized, app, use_time, cmap):
    frame = finalized.parallel_results.copy()
    frame["spot_name"] = "A_1"
    # Non-consecutive source indices must not become the sweep positions.
    frame.index = [4, 8, 20, 21]
    if use_time:
        frame["signal_relative_time"] = [0., .3, .9, 2.]
    frame = frame.rename(columns={column: column.replace(ratio, ratio + "_fractionation_corrected")
                                  for column in frame for ratio in
                                  ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb")
                                  if column.startswith(ratio)})
    assert review.depth_colormap_combo.currentText() == "plasma"
    review.show_depth_profile(frame, "A_1", include_calibration_uncertainty=True)
    review.tabs.setCurrentWidget(review.depth_concordia_page)
    app.processEvents()
    plot = review.depth_concordia
    plot._plot_item.setRange(xRange=(.2, .5), yRange=(.02, .06), padding=0)
    expected = plot._plot_item.vb.viewRange()
    review.depth_colormap_combo.setCurrentText(cmap)
    app.processEvents()
    np.testing.assert_allclose(plot._plot_item.vb.viewRange(), expected)
    colours = [color for _, _, _, color in plot._points]
    assert luminance(colours[0]) > luminance(colours[-1])
    assert len(plot._ellipses) == 4
    assert all(color == point[3] for (_, _, color), point in zip(plot._ellipses, plot._points))
    bar = plot._colorbar
    assert bar.isVisible()
    assert "shallow" in bar.axis._tickLevels[0][0][1]
    image = QtGui.QImage(800, 100, QtGui.QImage.Format.Format_ARGB32)
    painter = QtGui.QPainter(image)
    try:
        specs = bar.axis.generateDrawSpecs(painter)
        assert any("shallow" in text for _, _, text in specs[2])
    finally:
        painter.end()
    assert bar.axis.labelText == ("Time in ablation (s)" if use_time else "Sweep")
    assert bar.values == (0., 2. if use_time else 3.)
    assert not bar.sceneBoundingRect().intersects(plot._plot_item.vb.sceneBoundingRect())


@pytest.mark.parametrize("use_time", [False, True])
def test_depth_colour_bar_endpoint_units_and_rounding(review, finalized, app, use_time):
    frame = pd.concat([finalized.parallel_results] * 6, ignore_index=True).iloc[:22].copy()
    frame["spot_name"] = "A_1"
    frame.index = np.arange(22) * 3 + 4
    if use_time:
        frame["signal_relative_time"] = np.linspace(0., 8.9342, len(frame))
    frame = frame.rename(columns={column: column.replace(ratio, ratio + "_fractionation_corrected")
                                  for column in frame for ratio in
                                  ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb")
                                  if column.startswith(ratio)})
    review.show_depth_profile(frame, "A_1")
    review.tabs.setCurrentWidget(review.depth_concordia_page)
    app.processEvents()
    bar = review.depth_concordia._colorbar
    labels = [text for _, text in bar.axis._tickLevels[0]]
    assert labels == (["0 s (shallow)", "8.9 s (deep)"] if use_time else
                      ["sweep 1 (shallow)", "sweep 22 (deep)"])
    image = QtGui.QImage(800, 100, QtGui.QImage.Format.Format_ARGB32)
    painter = QtGui.QPainter(image)
    try:
        specs = bar.axis.generateDrawSpecs(painter)
        assert [text for _, _, text in specs[2]] == labels
    finally:
        painter.end()


def test_colour_settings_round_trip(app, tmp_path):
    path = str(tmp_path / "review.ini")
    first = ResultsTableWidget(settings=QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat))
    try:
        first.set_spot_colour_mode("single")
        first.set_depth_colormap("viridis")
        first._settings.sync()
    finally:
        dispose(first, app)
    second = ResultsTableWidget(settings=QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat))
    try:
        assert second.spot_colour_mode() == "single"
        assert second.depth_colormap_combo.currentText() == "viridis"
    finally:
        dispose(second, app)


def test_colour_preferences_save_and_restore_project_workspace(app, tmp_path, monkeypatch):
    from GUI.services import RestoredProject
    from GUI.studio_window import StudioWindow
    from UPbRPy.pipeline import PipelineStage
    from synthetic_bundles import write_raw_directory

    monkeypatch.setattr("UPbRPy.io.auto_select_baseline_peak", lambda experiment, *args, **kwargs:
                        experiment.assign(baseline_start_times=0., baseline_end_times=4.,
                                          plateau_start_times=8., plateau_end_times=16.,
                                          signal_start_times=6., signal_end_times=18.))
    raw = tmp_path / "Apatite_synthetic"
    write_raw_directory(raw)
    shell = StudioWindow()
    shell.add_tab()
    window = shell.active_reduction
    keys = ("review_spot_colour_v1", "review_depth_colormap_v1")
    previous = {key: window.settings.value(key) for key in keys}
    try:
        window.load_experiment_directory(str(raw), background=False)
        assert window.session.loaded
        window.results_table.set_spot_colour_mode("spot")
        window.results_table.set_depth_colormap("magma")
        workspace = window.project_workspace_state()
        assert workspace["review_spot_colour"] == "spot"
        assert workspace["review_depth_colormap"] == "magma"
        restored = RestoredProject(window.session, window.workflow_preset, tmp_path / "review.upbr",
                                   None, PipelineStage.IMPORT, (PipelineStage.IMPORT,), workspace)
        window.results_table.set_spot_colour_mode("single")
        window.results_table.set_depth_colormap("viridis")
        window.install_restored_project(restored)
        assert window.results_table.spot_colour_mode() == "spot"
        assert window.results_table.depth_colormap_combo.currentText() == "magma"
    finally:
        for key, value in previous.items():
            if value is None:
                window.settings.remove(key)
            else:
                window.settings.setValue(key, value)
        window.has_unsaved_changes = lambda: False
        dispose(shell, app)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_readout_controls_stay_visible_at_laptop_sizes(app, finalized, theme):
    from GUI.studio_window import StudioWindow

    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    shell = StudioWindow(theme_mode=theme)
    window = shell.active_reduction
    try:
        review = window.results_table
        review.show_results(finalized)
        window.plots_stack.setCurrentWidget(review)
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        shell.show()
        for width, height in ((1280, 800), (1440, 900)):
            shell.resize(width, height)
            window.apply_default_layout()
            for _ in range(5):
                app.processEvents()
            page = review.spot_concordia_page
            assert page.verticalScrollBar().maximum() == 0
            assert page.horizontalScrollBar().maximum() == 0
            label = review.concordia_age_label
            rect = QtCore.QRect(label.mapTo(page.viewport(), QtCore.QPoint()), label.size())
            assert page.viewport().rect().contains(rect)
            assert review._chooser_stack.height() >= 80
            assert review.spot_concordia.height() >= 250
    finally:
        window.has_unsaved_changes = lambda: False
        dispose(shell, app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)
