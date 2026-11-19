"""Project display colours share one resolver and the existing spot-edit undo."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
import pyqtgraph as pg

from GUI.spot_colours import SpotColours, SAMPLE_PALETTE, SPOT_PALETTE, normalise_colour
from GUI.colour_picker import ColourPicker
from GUI.widgets import SPOT_TREE_COLOUR_ROLE
from GUI.theme import active_theme
from UPbRPy.isochron import wetherill_point
from UPbRPy.pipeline import PipelineStage
from UPbRPy.bias import bias_summary
from test_spot_edits import app, window, pipeline
from test_bias_legend_series import bias_table


def test_resolver_modes_order_and_restore():
    colours = SpotColours()
    colours.set_order(["B", "A"], ["B_1", "A_1"])
    assert colours.sample_colour("A") == SAMPLE_PALETTE[0]
    assert colours.sample_colour("B") == SAMPLE_PALETTE[1]
    assert colours.spot_colour("A_1", "A", "spot") == SPOT_PALETTE[1]
    colours.sample_colours["A"] = "#123456"
    assert colours.spot_colour("A_1", "A", "sample") == "#123456"
    colours.spot_colours["A_1"] = "#abcdef"
    for mode in ("sample", "spot"):
        assert colours.spot_colour("A_1", "A", mode) == "#abcdef"
    assert colours.spot_colour("A_1", "A", "single", accent="#112233") == "#112233"
    colours.restore({"A": "#ABCDEF", "B": "bad", "missing": "#123456"},
                    {"A_1": "#123456", "missing": "#123456"})
    assert colours.sample_colours == {"A": "#abcdef"}
    assert colours.spot_colours == {"A_1": "#123456"}
    with pytest.raises(ValueError):
        normalise_colour("red")
    with pytest.raises(ValueError):
        colours.spot_colour("A_1", "A", "invalid")


def install_concordia(window):
    names = ["unknown_1", "unknown_2", "91500_1"]
    x, y = wetherill_point(np.array([300., 500., 700.]))
    frame = pd.DataFrame({"spot_name": names, "sample": ["unknown", "unknown", "91500"],
                          "207Pb/235U": x, "206Pb/238U": y,
                          "207Pb/235U_2e": .01, "206Pb/238U_2e": .002})
    review = window.results_table
    review.apply_theme(active_theme())
    review.set_spot_colour_mode("sample")
    review.show_results(SimpleNamespace(parallel_results=frame, selected_results=frame.copy()))
    review.tabs.setCurrentWidget(review.spot_concordia_page)
    review.refresh_spot_concordia()
    return review.spot_concordia


def brushes(canvas):
    return {name: colour.name() for name, _x, _y, colour in canvas._all_points}


def test_sample_recolours_concordia_legend_bias_and_undo(window):
    canvas = install_concordia(window)
    table = bias_table().assign(sample=["91500"] * 3 + ["unknown"] * 3)
    window.bias_plot.show_bias(table, bias_summary(table), theme=active_theme(), x_label="U", y_label="Bias")
    before = brushes(canvas)
    assert window.request_sample_colour("unknown", "#123456")
    assert brushes(canvas)["unknown_1"] == brushes(canvas)["unknown_2"] == "#123456"
    swatch = next(s.item for s, label in canvas._legend.items if label.text == "unknown")
    assert swatch.opts["brush"].color().name() == "#123456"
    scatter = window.bias_plot._scatters["unknown"]
    assert all(p.brush().color().name() == "#123456" for p in scatter.points())
    assert scatter.opts["brush"].color().name() == "#123456"
    assert window._spot_tree_item("unknown_1").parent().data(0, SPOT_TREE_COLOUR_ROLE) == "#123456"
    window.undo_selection_edit()
    assert brushes(canvas) == before
    window.redo_selection_edit()
    assert brushes(canvas)["unknown_1"] == "#123456"


def test_override_multiselect_reset_and_each_undo(window):
    canvas = install_concordia(window)
    names = ["unknown_1", "unknown_2"]
    window.request_sample_colour("unknown", "#123456")
    items = [window._spot_tree_item(name) for name in names]
    window.spot_list.clearSelection()
    for item in items:
        item.setSelected(True)
    assert set(window._colour_targets(names[0])) == set(names)
    assert window._colour_targets("91500_1") == ["91500_1"]
    count = len(window._selection_undo)
    assert window.request_spot_colour(window._colour_targets(names[0]), "#abcdef")
    assert len(window._selection_undo) == count + 1
    assert all(brushes(canvas)[name] == "#abcdef" for name in names)
    for name in names:
        assert window._spot_tree_item(name).data(0, SPOT_TREE_COLOUR_ROLE) == "#abcdef"
    window.undo_selection_edit()
    assert not window.spot_colours.spot_colours
    window.redo_selection_edit()
    assert window.spot_colours.spot_colours == dict.fromkeys(names, "#abcdef")
    assert window.request_spot_colour(names, None)
    assert all(brushes(canvas)[name] == "#123456" for name in names)
    window.undo_selection_edit()
    assert window.spot_colours.spot_colours == dict.fromkeys(names, "#abcdef")
    window.redo_selection_edit()
    assert not window.spot_colours.spot_colours
    assert not window.request_spot_colour(names, None)
    window.request_spot_colour(names, "#abcdef")
    count = len(window._selection_undo)
    window.results_table.reset_colours_button.click()
    assert len(window._selection_undo) == count + 1
    assert not window.spot_colours.sample_colours and not window.spot_colours.spot_colours
    window.undo_selection_edit()
    assert window.spot_colours.sample_colours == {"unknown": "#123456"}
    assert window.spot_colours.spot_colours == dict.fromkeys(names, "#abcdef")
    window.redo_selection_edit()
    assert not window.spot_colours.sample_colours and not window.spot_colours.spot_colours


def test_rename_material_change_remove_and_undo(window):
    old, new = "NIST612_849", "NIST610_849"
    window._review_flags[old] = "review counts"
    window.request_spot_colour([old], "#123456")
    assert window.request_rename_spot(old, new)
    assert "· Studio" in window._spot_tree_item(new).text(0)
    assert not window._spot_tree_item(new).icon(0).isNull()
    assert window.spot_colours.spot_colours == {new: "#123456"}
    window.undo_selection_edit()
    assert window.spot_colours.spot_colours == {old: "#123456"}
    window.redo_selection_edit()
    assert window.spot_colours.spot_colours == {new: "#123456"}
    assert window.request_move_spot(new, "NIST612")
    assert window.spot_colours.spot_colours == {new: "#123456"}
    window.request_remove_spot(new)
    assert not window.spot_colours.spot_colours
    window.undo_selection_edit()
    assert window.spot_colours.spot_colours == {new: "#123456"}
    window.redo_selection_edit()
    assert not window.spot_colours.spot_colours


def test_project_roundtrip_and_new_session_clears_colours(window, tmp_path):
    window.request_sample_colour("unknown", "#123456")
    window.request_spot_colour(["unknown_1"], "#abcdef")
    project = tmp_path / "colours.upbr"
    window.save_project(project)
    window.request_reset_colours()
    window.load_project_file(project, background=False, use_cache=False)
    assert window.spot_colours.sample_colours == {"unknown": "#123456"}
    assert window.spot_colours.spot_colours == {"unknown_1": "#abcdef"}
    source = window.session.directory
    window.load_experiment_directory(source, background=False, offer_restore=False)
    assert not window.spot_colours.sample_colours and not window.spot_colours.spot_colours


def test_colour_edits_do_not_invalidate_or_compute(window, monkeypatch):
    pipe = pipeline(window)
    pipe.run_baseline()
    baseline = pipe.cache.get(PipelineStage.BASELINE)
    fingerprint = window.scientific_project_fingerprint()
    monkeypatch.setattr(pipe.cache, "invalidate", lambda *args, **kwargs: pytest.fail("invalidated"))
    for method in ("run_baseline", "run_normalization", "run_ratios", "run"):
        if hasattr(pipe, method):
            monkeypatch.setattr(pipe, method, lambda *args, **kwargs: pytest.fail("reduced"))
    window.request_sample_colour("unknown", "#123456")
    window.request_spot_colour(["unknown_1", "unknown_2"], "#abcdef")
    window.undo_selection_edit()
    window.redo_selection_edit()
    window.request_reset_colours()
    window.undo_selection_edit()
    assert window.scientific_project_fingerprint() == fingerprint
    assert pipe.cache.get(PipelineStage.BASELINE) is baseline


@pytest.mark.parametrize("spots", [False, True])
def test_picker_swatches_and_reset(app, spots):
    picker = ColourPicker("#123456", spots=spots)
    chosen = []
    picker.colourChosen.connect(chosen.append)
    for colour, button in picker.swatches.items():
        button.click()
        assert chosen[-1] == colour
    assert list(picker.swatches) == (SPOT_PALETTE if spots else SAMPLE_PALETTE)
    picker.actions()[-1].trigger()
    assert chosen[-1] is None
    picker.deleteLater()


def test_picker_custom_colour(app, monkeypatch):
    picker = ColourPicker("#123456")
    chosen = []
    picker.colourChosen.connect(chosen.append)
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor", lambda *args: QtGui.QColor("#abcdef"))
    picker.actions()[-2].trigger()
    assert chosen == ["#abcdef"]
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor", lambda *args: QtGui.QColor())
    picker.actions()[-2].trigger()
    assert chosen == ["#abcdef"]
    picker.deleteLater()


def test_hidden_series_survive_recolour_including_errors(window):
    canvas = install_concordia(window)
    primary = next(s.item for s, label in canvas._legend.items if label.text == "unknown")
    primary.hide()
    table = bias_table().assign(sample=["91500"] * 3 + ["unknown"] * 3)
    window.bias_plot.show_bias(table, bias_summary(table), theme=active_theme(), x_label="U", y_label="Bias")
    window.bias_plot._scatters["unknown"].hide()
    window.request_sample_colour("unknown", "#123456")
    window.request_spot_colour(["RM_3"], "#abcdef")  # unknown spot is ignored
    assert not ({"unknown_1", "unknown_2"} & canvas._visible_spot_names)
    assert all(not ellipse.isVisible() for name, ellipse, _ in canvas._ellipses if name.startswith("unknown"))
    assert "unknown" in canvas._legend_series.hidden
    key, primary, parts = next(x for x in window.bias_plot._legend_series.items() if x[0] == "unknown")
    assert not primary.isVisible() and parts and all(not item.isVisible() for item in parts)
    window.undo_selection_edit()
    window.redo_selection_edit()
    assert "unknown" in canvas._legend_series.hidden


def test_tree_dot_and_legend_right_click_open_picker(window, app, monkeypatch):
    canvas = install_concordia(window)
    chosen = []
    monkeypatch.setattr(ColourPicker, "popup", lambda self, pos: (chosen.append(self.current), self._choose("#abcdef")))
    item = window._spot_tree_item("unknown_1").parent()
    window._choose_tree_colour(item)
    assert chosen and window.spot_colours.sample_colours["unknown"] == "#abcdef"
    swatch = next(s for s, label in canvas._legend.items if label.text == "unknown")
    event = SimpleNamespace(button=lambda: QtCore.Qt.MouseButton.RightButton, accept=lambda: None)
    swatch.mouseClickEvent(event)
    assert len(chosen) == 2


def test_context_menu_multiselect_set_and_reset(window, monkeypatch):
    names = ["unknown_1", "unknown_2"]
    selected_action = ["Set colour…"]
    def menu_exec(menu, pos):
        return next(action for action in menu.actions() if action.text() == selected_action[0])
    class Menu(QtWidgets.QMenu):
        exec = menu_exec
    monkeypatch.setattr("GUI.main_window.QtWidgets.QMenu", Menu)
    monkeypatch.setattr(ColourPicker, "popup", lambda self, pos: self._choose("#abcdef"))
    monkeypatch.setattr(window.spot_list, "itemAt", lambda pos: window._spot_tree_item(names[0]))
    window.spot_list.clearSelection()
    for name in names:
        window._spot_tree_item(name).setSelected(True)
    window._on_spot_tree_context_menu(QtCore.QPoint())
    assert window.spot_colours.spot_colours == dict.fromkeys(names, "#abcdef")
    assert set(window._colour_targets(names[0])) == set(names)
    selected_action[0] = "Reset to sample colour"
    window._on_spot_tree_context_menu(QtCore.QPoint())
    assert not window.spot_colours.spot_colours
    window.undo_selection_edit()
    assert window.spot_colours.spot_colours == dict.fromkeys(names, "#abcdef")
    window.undo_selection_edit()
    assert not window.spot_colours.spot_colours


def test_bias_override_brush_pen_errorbar_and_legend(window):
    table = bias_table()
    names = window.session.spot_names[:6]
    table["spot_name"] = names
    table["sample"] = ["NIST610"] * 3 + ["NIST612"] * 3
    window.bias_plot.show_bias(table, bias_summary(table), theme=active_theme(), x_label="U", y_label="Bias")
    window.request_sample_colour("NIST610", "#123456")
    window.request_spot_colour([names[0]], "#abcdef")
    scatter = window.bias_plot._scatters["NIST610"]
    points = scatter.points()
    assert [p.brush().color().name() for p in points] == ["#abcdef", "#123456", "#123456"]
    assert [p.pen().color().name() for p in points] == ["#abcdef", "#123456", "#123456"]
    assert scatter.opts["brush"].color().name() == "#123456"
    parts = next(parts for key, _, parts in window.bias_plot._legend_series.items() if key == "NIST610")
    bars = [part for part in parts if isinstance(part, pg.ErrorBarItem)]
    assert {bar.opts["pen"].color().name() for bar in bars} == {"#123456", "#abcdef"}
    scatter.hide()
    window.request_sample_colour("NIST610", "#654321")
    assert all(not part.isVisible() for key, _, parts in window.bias_plot._legend_series.items()
               if key == "NIST610" for part in parts)


def test_real_tree_swatch_and_legend_label_clicks(window, app, monkeypatch):
    canvas = install_concordia(window)
    window.results_table.fit_panels["spot"].fit_button.setChecked(False)
    window.resize(1280, 800)
    window.show()
    app.processEvents()
    picked = []
    monkeypatch.setattr(ColourPicker, "popup", lambda self, pos: (picked.append(self.current), self._choose("#abcdef")))
    item = window._spot_tree_item("unknown_1").parent()
    window.spot_list.scrollToItem(item)
    rect = window.spot_list.visualItemRect(item)
    QtTest.QTest.mouseClick(window.spot_list.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           pos=QtCore.QPoint(rect.right() - 8, rect.center().y()))
    assert picked and window.spot_colours.sample_colours["unknown"] == "#abcdef"
    # Put concordia on screen without asking the reduction service to install results.
    window.plots_stack.setCurrentWidget(window.results_table)
    app.processEvents()
    sample, label = next((s, label) for s, label in canvas._legend.items if label.text == "unknown")
    QtTest.QTest.mouseClick(canvas.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           pos=canvas.mapFromScene(label.mapToScene(label.boundingRect().center())))
    assert "unknown" in canvas._legend_series.hidden
    assert not canvas.selected_spots
    QtTest.QTest.mouseClick(canvas.viewport(), QtCore.Qt.MouseButton.RightButton,
                           pos=canvas.mapFromScene(sample.mapToScene(sample.boundingRect().center())))
    assert len(picked) == 2 and "unknown" in canvas._legend_series.hidden


def test_colours_are_view_only_results_cache_keys(window, tmp_path, monkeypatch):
    import json
    from GUI import results_cache
    project = tmp_path / "colours.upbr"
    window.save_project(project)
    monkeypatch.setattr(results_cache, "engine_fingerprint", lambda: "same-code")
    first = results_cache.prepare(project)
    assert first is not None
    data = json.loads(project.read_text())
    data["workspace"].update(sample_colours={"unknown": "#123456"}, spot_colours={"unknown_1": "#abcdef"})
    project.write_text(json.dumps(data))
    second = results_cache.prepare(project)
    assert second is not None and first.key == second.key


@pytest.mark.parametrize("grid", [False, True])
def test_diagnostic_plots_share_assigned_sample_and_spot_colours(window, grid):
    from GUI.widgets import CALIBRATION_GRID
    names = ["91500_1", "91500_2"]
    time = np.linspace(0., 2., 5)
    downhole = dict(ratio_name="206Pb/238U", primary_standard="91500", known_ratio=1.,
                    unified_time=time, mean_frac=np.ones(5), std_frac=.01,
                    spot_times=[time, time], spot_ratios=[np.ones(5), np.ones(5)], spot_names=names)
    session = dict(ratio_name="206Pb/238U", primary_standard="91500", spot_names=names,
                   spot_mean_times=np.array([1., 2.]), spot_mean_fracs=np.array([1., 1.1]),
                   spot_frac_errs=np.array([.01, .01]), fit_times=time, fit_vals=np.ones(5),
                   fit_std=.01, show_uncertainty=True)
    calibration = dict(channels=["238U", "232Th"], channel_data={
        channel: dict(ppm=np.array([1., 2.]), cps=np.array([2., 3.]), slope=1.,
                      spot_names=names, labels=["91500", "91500"])
        for channel in ("238U", "232Th")})
    theme = active_theme()
    window.downhole_frac_plot.show_data(downhole, theme)
    window.session_frac_plot.show_data(session, theme)
    window.calibration_slopes_plot.show_data(calibration, theme, channel=CALIBRATION_GRID if grid else None)
    window.request_sample_colour("91500", "#123456")
    window.request_spot_colour([names[1]], "#abcdef")
    traces = window.downhole_frac_plot._plot_item.listDataItems()[:2]
    assert [trace.opts["pen"].color().name() for trace in traces] == ["#123456", "#abcdef"]
    points = next(item for item in window.session_frac_plot._plot_item.items if isinstance(item, pg.ScatterPlotItem))
    assert [point.brush().color().name() for point in points.points()] == ["#123456", "#abcdef"]
    bars = [item for item in window.session_frac_plot._plot_item.items if isinstance(item, pg.ErrorBarItem)]
    assert {bar.opts["pen"].color().name() for bar in bars} == {"#123456", "#abcdef"}
    view = window.calibration_slopes_plot
    plots = list(view._grid_items.values()) if grid else [view._plot_item]
    for plot in plots:
        for scatter in (item for item in plot.items if isinstance(item, pg.ScatterPlotItem)):
            assert [point.brush().color().name() for point in scatter.points()] == ["#123456", "#abcdef"]
    window.undo_selection_edit()
    traces = window.downhole_frac_plot._plot_item.listDataItems()[:2]
    assert all(trace.opts["pen"].color().name() == "#123456" for trace in traces)
