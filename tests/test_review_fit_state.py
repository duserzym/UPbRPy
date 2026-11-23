"""Fit visibility, empty scopes and workspace state use synthetic results."""

from copy import deepcopy
import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtTest

from GUI.theme import THEMES
from GUI.widgets import ResultsTableWidget
from UPbRPy.project import json_safe_value
from test_review_calculation_fits import app, review, depths, fit, frame, wait_for
from test_spot_edits import window


SELECTED = {f"A_{i}" for i in range(4)}


def plotted_fit_items(panel, previous=()):
    """Count retained fit items still attached to their plots."""
    return {item for plot, items in [*previous, *panel._overlays]
            for item in items if item in plot.items}


def refresh_views(review, app):
    for kind in ("spot", "depth"):
        canvas = getattr(review, kind + "_concordia")
        if canvas._plot_item is None:
            continue
        vb = canvas._plot_item.vb
        x, y = vb.viewRange()
        vb.setRange(xRange=(x[0]-.1, x[1]+.1), yRange=y, padding=0)
        vb.autoRange()
        getattr(review, "refresh_" + kind + "_concordia")()
    review.refresh_colours()
    QtTest.QTest.qWait(150)
    app.processEvents()


def assert_off(panel, previous=()):
    assert not panel.fit_button.isChecked()
    assert not getattr(panel.review, panel.kind + "_fit_button").isChecked()
    assert not plotted_fit_items(panel, previous)
    assert not panel._timer.isActive()


@pytest.mark.parametrize("kind", ["spot", "depth"])
@pytest.mark.parametrize("action", ["hide", "exclude", "deselect"])
def test_scope_edits_and_reranges_never_revive_off_fit(review, app, monkeypatch, kind, action):
    review.set_selected_spots(SELECTED)
    panel = fit(app, review, kind, resamples=4)
    previous = list(panel._overlays)
    assert plotted_fit_items(panel)
    wait_for(app, lambda: not panel._workers)
    saved = deepcopy(review.fit_records())
    panel.fit_button.setChecked(False)
    generation = panel._generation
    monkeypatch.setattr("GUI.review_fits._FitWorker", lambda *a, **k: pytest.fail("Fit is off"))
    if action == "hide":
        review.hide_selected()
        assert review.selected_spots == SELECTED
    elif action == "exclude":
        review.set_spot_excluded(SELECTED, True)
    else:
        review.set_selected_spots(set())
    refresh_views(review, app)
    assert_off(panel, previous)
    review.set_selected_spots(SELECTED)
    refresh_views(review, app)
    assert_off(panel, previous)
    assert panel._generation == generation and not panel._workers
    assert json_safe_value(review.fit_records()) == json_safe_value(saved)
    assert getattr(review, kind + "_fit_label").text().startswith("Fit off · ")


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_diagram_changes_draw_only_current_space_when_on(review, app, kind):
    review.tabs.setCurrentWidget(getattr(review, kind + "_concordia_page"))
    review.set_selected_spots(SELECTED)
    panel = fit(app, review, kind, resamples=4)
    buttons = getattr(review, kind + "_diagram_buttons")
    for enabled in (False, True):
        panel.fit_button.setChecked(enabled)
        for space in ("tera_wasserburg", "wetherill"):
            previous = list(panel._overlays)
            buttons[space].setChecked(True)
            getattr(review, "refresh_" + kind + "_concordia")()
            # Even a curve notification before invalidate must not draw a
            # retained line in another coordinate space.
            panel._draw_records()
            if enabled:
                wait_for(app, lambda: panel._errors or (panel.records and all(
                    r["space"] == space and r.get("bootstrap_resamples") == 4 for r in panel.records)))
                assert not panel._errors, panel.readout.toPlainText()
                assert plotted_fit_items(panel)
                for plot, items in previous:
                    if (plot, items) not in panel._overlays:
                        assert not any(item in plot.items for item in items)
                assert panel.fit_button.isChecked()
            else:
                refresh_views(review, app)
                assert_off(panel, previous)


@pytest.mark.parametrize("kind", ["spot", "depth"])
@pytest.mark.parametrize("action", ["hide", "exclude"])
@pytest.mark.parametrize("together", [False, True])
def test_empty_selected_scope_is_neutral_and_returns_when_shown(review, app, kind, action, together):
    review.set_selected_spots(SELECTED)
    panel = review.fit_panels[kind]
    panel.together.setChecked(together)
    panel = fit(app, review, kind, resamples=4)
    wait_for(app, lambda: not panel._workers)
    previous = list(panel._overlays)
    if action == "hide":
        review.hide_selected()
    else:
        review.set_spot_excluded(SELECTED, True)
    refresh_views(review, app)
    assert review.selected_spots == SELECTED
    assert panel.fit_button.isChecked()
    assert not plotted_fit_items(panel, previous)
    assert panel.records == [] and panel._errors == []
    assert not panel._timer.isActive() and not panel._workers
    assert panel.readout.toPlainText() == "No spots to fit"
    assert getattr(review, kind + "_fit_label").text() == "No spots to fit"
    if action == "hide":
        review.include_selected()
    else:
        review.set_spot_excluded(SELECTED, False)
    wait_for(app, lambda: panel.records and panel.records[0].get("bootstrap_resamples") == 4)
    assert set(panel.records[0]["items"]) == SELECTED
    assert plotted_fit_items(panel)


@pytest.mark.parametrize("action", ["hide", "exclude", "invalid"])
def test_empty_sample_group_drops_only_its_record(review, app, action):
    panel = fit(app, review, resamples=4)
    names = {name for name in review.include_flags if name.startswith("A_")}
    if action == "hide":
        review.set_spot_included(names, False)
    elif action == "exclude":
        review.set_spot_excluded(names, True)
    else:
        review._spot_frame.loc[review._spot_frame.spot_name.isin(names), "206Pb/238U"] = np.nan
        review.refresh_spot_concordia()
    wait_for(app, lambda: panel.records and [r["label"] for r in panel.records] == ["B"]
             and panel.records[0].get("bootstrap_resamples") == 4)
    assert panel._errors == []
    assert len(plotted_fit_items(panel)) == 4


def test_explicit_deselect_after_hiding_uses_all_shown_spots(review, app):
    review.set_selected_spots(SELECTED)
    panel = fit(app, review, resamples=4)
    review.hide_selected()
    assert not panel.records
    review.set_selected_spots(set())
    wait_for(app, lambda: panel.records and all(r.get("bootstrap_resamples") == 4 for r in panel.records))
    assert {name for r in panel.records for name in r["items"]} == set(review.include_flags)-SELECTED


@pytest.mark.parametrize("off_kind", ["spot", "depth"])
def test_fit_state_is_independent_per_page(review, app, off_kind):
    panels = {kind: fit(app, review, kind, resamples=4) for kind in ("spot", "depth")}
    panels[off_kind].fit_button.setChecked(False)
    review.set_selected_spots(SELECTED)
    refresh_views(review, app)
    assert_off(panels[off_kind])
    on = panels["depth" if off_kind == "spot" else "spot"]
    wait_for(app, lambda: on.records and on.records[0].get("bootstrap_resamples") == 4)
    assert on.fit_button.isChecked() and plotted_fit_items(on)


@pytest.mark.parametrize("enabled", [False, True])
def test_workspace_roundtrip_retains_records_and_page_states(review, app, tmp_path, enabled):
    for kind in ("spot", "depth"):
        panel = fit(app, review, kind, resamples=4)
        panel.fit_button.setChecked(enabled if kind == "spot" else not enabled)
    saved = json_safe_value(dict(review_fit_settings=review.fit_settings(), review_fits=review.fit_records()))
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps(saved, allow_nan=False))
    restored = ResultsTableWidget()
    try:
        restored.apply_theme(THEMES["light"])
        f = frame()
        restored.show_results(SimpleNamespace(parallel_results=f, selected_results=f.copy()))
        restored.show_depth_profile(depths(f), "A_0")
        restored.restore_review_state(json.loads(path.read_text()), f.spot_name)
        refresh_views(restored, app)
        assert json_safe_value(restored.fit_records()) == saved["review_fits"]
        for kind, panel in restored.fit_panels.items():
            expected = saved["review_fit_settings"][kind]["enabled"]
            assert panel.fit_button.isChecked() == expected
            assert getattr(restored, kind + "_fit_button").isChecked() == expected
            assert bool(plotted_fit_items(panel)) == expected
    finally:
        for panel in restored.fit_panels.values():
            panel.fit_button.setChecked(False)
        QtCore.QThreadPool.globalInstance().waitForDone(10000)
        restored.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("kind", ["spot", "depth"])
def test_only_explicit_fit_controls_enable_fits_and_popover_tracks_state(review, app, kind):
    panel = review.fit_panels[kind]
    popover = getattr(review, kind + "_fit_button")
    for enabled in (True, False):
        panel.set_enabled_default(enabled)
        assert panel.fit_button.isChecked() == popover.isChecked() == enabled
        panel.restore_settings(dict(enabled=not enabled))
        assert panel.fit_button.isChecked() == popover.isChecked() == (not enabled)
    panel.set_enabled_default(False)
    # Opening the toolbar menu must not toggle its checked indication.
    popover.nextCheckState()
    assert_off(panel)
    review.set_selected_spots(SELECTED)
    for action in ("only", "hide", "show", "exclude", "include", "all"):
        review._review_action(action, kind)
        assert_off(panel)
    panel.resamples.setValue(4)
    review._review_action("fit", kind)
    wait_for(app, lambda: panel.records and panel.records[0].get("bootstrap_resamples") == 4)
    assert panel.fit_button.isChecked() and popover.isChecked() and plotted_fit_items(panel)
    panel.fit_button.click()
    assert_off(panel)


def test_toggle_preserves_fit_records_and_weighted_dates(review, app):
    f = frame()
    for system in ("206Pb/238U", "207Pb/235U", "207Pb/206Pb"):
        f[system + "_age"] = np.linspace(490, 510, len(f))
        f[system + "_age_2e_with_calibration"] = 10.
    review.show_results(SimpleNamespace(parallel_results=f, selected_results=f.copy()))
    panel = fit(app, review, resamples=4)
    app.processEvents()
    records = json_safe_value(review.fit_records())
    means = deepcopy(review.weighted_mean_dates)
    readout = review.spot_age_label.text()
    assert means and all(value["n"] for value in means.values())
    panel.fit_button.setChecked(False)
    refresh_views(review, app)
    assert json_safe_value(review.fit_records()) == records
    fit(app, review, resamples=4)
    assert json_safe_value(review.fit_records()) == records
    assert review.weighted_mean_dates == means
    assert review.spot_age_label.text() == readout


def window_results(window):
    review = window.results_table
    f = frame(len(window.session.spot_names))
    f["spot_name"] = list(window.session.spot_names)
    f["sample"] = f.spot_name.map(window.session.experiment.set_index("spot_names")["sample"])
    review.show_results(SimpleNamespace(parallel_results=f, selected_results=f.copy()))
    review.tabs.setCurrentWidget(review.spot_concordia_page)
    return review


def test_spot_edit_undo_redo_cannot_revive_disabled_saved_fit(window, app, monkeypatch):
    review = window_results(window)
    panel = fit(app, review, resamples=4)
    previous = list(panel._overlays)
    name = window.session.spot_names[0]
    assert window.request_calculation_exclusion(name, True)
    panel.fit_button.setChecked(False)
    wait_for(app, lambda: not panel._workers)
    generation = panel._generation
    monkeypatch.setattr("GUI.review_fits._FitWorker", lambda *a, **k: pytest.fail("Fit is off"))
    for operation in (window.undo_selection_edit, window.redo_selection_edit, window.undo_selection_edit):
        operation()
        refresh_views(review, app)
        assert_off(panel, previous)
        assert panel._generation == generation and not panel._workers


@pytest.mark.parametrize("enabled", [False, True])
def test_project_install_restores_enabled_state_and_last_records(window, app, tmp_path, enabled):
    from GUI.services import RestoredProject
    from UPbRPy.pipeline import PipelineStage
    from UPbRPy.project import read_reduction_project, write_reduction_project

    review = window_results(window)
    panel = fit(app, review, resamples=4)
    panel.fit_button.setChecked(enabled)
    records = json_safe_value(review.fit_records())
    path = write_reduction_project(window.session.pipeline, tmp_path / "fits.upbr",
                                   workspace=window.project_workspace_state())
    workspace = read_reduction_project(path)["workspace"]
    restored = RestoredProject(window.session, window.workflow_preset, path, None,
                               PipelineStage.IMPORT, (PipelineStage.IMPORT,), workspace)
    panel.fit_button.setChecked(not enabled)
    window.install_restored_project(restored)
    refresh_views(review, app)
    assert panel.fit_button.isChecked() == enabled
    assert review.spot_fit_button.isChecked() == enabled
    assert json_safe_value(review.fit_records()) == records
    assert bool(plotted_fit_items(panel)) == enabled
