"""Selection edits survive navigation, workers, and project recovery."""

import json
import hashlib
from pathlib import Path
import threading
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from shiboken6 import isValid

from GUI import main_window, services
from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application
from UPbRPy.autosave import default_autosave_dir, selection_autosave_path
from UPbRPy.pipeline import PipelineStage


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
RICH = FIXTURE / "Zircon_demo_Rich.csv"


def test_autosave_locations(tmp_path, monkeypatch):
    monkeypatch.setenv("UPBRPY_AUTOSAVE_DIR", str(tmp_path / "recovery"))
    assert default_autosave_dir() == tmp_path / "recovery"
    source = tmp_path / "exports" / "session.csv"
    digest = hashlib.sha1(str(source.resolve()).encode("utf-8")).hexdigest()[:10]
    assert selection_autosave_path(source) == tmp_path / "recovery" / f"session-{digest}.upbr"
    assert selection_autosave_path(tmp_path / "other" / "session.csv") != selection_autosave_path(source)
    monkeypatch.delenv("UPBRPY_AUTOSAVE_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_autosave_dir() == tmp_path / "UPbRPy" / "autosave"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def wait_for(app, condition, timeout=15):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert condition(), "Timed out waiting for selection work"


def idle(app, window):
    wait_for(app, lambda: window._active_worker is None and not window.pending_region_updates
             and not window._selection_recompute_timer.isActive())


@pytest.fixture
def windows(app, monkeypatch):
    created = []
    errors = []
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))

    def make(source=RICH, project=False):
        window = MainWindow(theme_mode="light")
        created.append(window)
        window.selection_edit_debounce_ms = 30
        window.selection_recompute_debounce_ms = 50
        window.selection_autosave_debounce_ms = 80
        if source:
            if project:
                window.load_project_file(source, background=False)
            else:
                window.load_experiment_directory(str(source), background=False)
        assert not errors
        return window

    yield make
    for window in created:
        if not isValid(window):
            continue
        idle(app, window)
        for timer in (window._selection_edit_timer, window._selection_recompute_timer,
                      window._selection_autosave_timer):
            timer.stop()
        window.has_unsaved_changes = lambda: False
        window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not errors


def regions(window, spot=None):
    return services.get_selection_regions(window.session, spot or window.current_spot_name())


def edit_plateau(window):
    spot = window.current_spot_name()
    start, end = regions(window, spot)["plateau"]
    bounds = (start + 0.5, end - 0.5)
    window.on_plot_region_changed("plateau", bounds)
    return spot, bounds


def test_edit_applies_after_debounce_and_emits_only_on_finish(windows, app):
    window = windows()
    overlay = window.trace_plot._region_overlays["plateau"]["region"]
    spy = QtTest.QSignalSpy(window.trace_plot.regionChanged)
    start, end = regions(window)["plateau"]
    # Model intermediate mouse movement without setRegion's synthetic finish.
    with QtCore.QSignalBlocker(overlay):
        overlay.setRegion((start + 0.5, end - 0.5))
    for _ in range(8):
        overlay.sigRegionChanged.emit(overlay)
    assert spy.count() == 0
    expected = tuple(overlay.getRegion())
    overlay.sigRegionChangeFinished.emit(overlay)
    assert spy.count() == 1
    spot = window.current_spot_name()
    assert window.has_unsaved_changes()
    assert window.pending_region_updates[spot]["plateau"] == expected
    wait_for(app, lambda: regions(window, spot)["plateau"] == expected)


def test_spot_switch_flushes_original_spot(windows):
    window = windows()
    spot, bounds = edit_plateau(window)
    other = next(name for name in window.session.spot_names if name != spot)
    other_before = regions(window, other)
    window._select_spot_in_tree(other)
    assert regions(window, spot)["plateau"] == bounds
    assert regions(window, other) == other_before
    assert not window.pending_region_updates


def test_plot_coupled_signal_edges_are_saved_with_plateau(windows, app):
    window = windows()
    plateau = window.trace_plot._region_overlays["plateau"]["region"]
    original = regions(window)
    # Extending the plateau also moves the signal edge locally in the plot.
    start = original["signal"][0] - 0.6
    end = original["plateau"][1]
    with QtCore.QSignalBlocker(plateau):
        plateau.setRegion((start, end))
    plateau.sigRegionChanged.emit(plateau)
    expected = window.trace_plot.current_regions()
    plateau.sigRegionChangeFinished.emit(plateau)
    wait_for(app, lambda: not window.pending_region_updates)
    assert regions(window)["signal"] == expected["signal"]
    assert regions(window)["plateau"] == expected["plateau"]
    window.undo_selection_edit()
    assert regions(window) == original


def test_signal_boundary_names_queue_the_signal_interval(windows, app):
    window = windows()
    start, end = regions(window)["signal"]
    window.on_plot_region_changed("signal_end", (start, end + 0.5))
    spot = window.current_spot_name()
    assert window.pending_region_updates[spot] == {"signal": (start, end + 0.5)}
    wait_for(app, lambda: not window.pending_region_updates)
    assert regions(window)["signal"] == (start, end + 0.5)


@pytest.mark.parametrize("redraw", ["refresh_view", "on_sigma_toggle_clicked"])
def test_redraw_keeps_latest_bounds(windows, redraw):
    window = windows()
    spot, bounds = edit_plateau(window)
    getattr(window, redraw)()
    assert regions(window, spot)["plateau"] == bounds
    assert window.trace_plot.current_regions()["plateau"] == pytest.approx(bounds)


def test_tab_and_step_switch_flush(windows, app):
    window = windows()
    second = windows()
    shell = StudioWindow(theme_mode="light", create_empty=False)
    shell.add_tab(window)
    shell.add_tab(second)
    try:
        shell.tabs.setCurrentWidget(window)
        spot, bounds = edit_plateau(window)
        shell.tabs.setCurrentWidget(second)
        assert regions(window, spot)["plateau"] == bounds
        shell.tabs.setCurrentWidget(window)
        spot, bounds = edit_plateau(window)
        window.workflow_step_list.setCurrentRow(1)
        assert regions(window, spot)["plateau"] == bounds
    finally:
        for reduction in (window, second):
            idle(app, reduction)
            reduction.has_unsaved_changes = lambda: False
        shell.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_autosave_real_project_reopens_without_save(windows, app, tmp_path):
    window = windows()
    target = tmp_path / "reviewed.upbr"
    window.save_project(target)
    assert not window.has_unsaved_changes()
    spot, bounds = edit_plateau(window)
    assert window.has_unsaved_changes()
    wait_for(app, lambda: not window._selection_autosave_pending and not window.pending_region_updates)
    assert not window.has_unsaved_changes()
    saved = json.loads(target.read_text())
    row = next(row for row in saved["selections"] if row["spot_names"] == spot)
    assert row["plateau_start_times"] == bounds[0]
    restored = windows(target, project=True)
    assert regions(restored, spot)["plateau"] == bounds
    assert window.log_view.toPlainText().count("Autosaved selection edits to ") == 1


def test_bare_source_sidecar_restore_discard_and_save_as(windows, app, tmp_path, monkeypatch):
    window = windows()
    spot, bounds = edit_plateau(window)
    sidecar = selection_autosave_path(RICH)
    assert sidecar.parent == default_autosave_dir()
    assert selection_autosave_path(RICH.parent / "." / RICH.name) == sidecar
    wait_for(app, sidecar.exists)
    assert window.project_path is None and window.has_unsaved_changes()
    prompts = []

    def restore(dialog):
        prompts.append(dialog.text())
        assert [button.text() for button in dialog.buttons()] == ["Restore", "Discard"]
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", restore)
    restored = windows()
    assert len(prompts) == 1
    assert "Studio kept selection edits" in prompts[0]
    assert regions(restored, spot)["plateau"] == bounds
    assert restored.project_path is None and restored.has_unsaved_changes()
    restored.save_project(tmp_path / "real.upbr")
    assert not sidecar.exists()
    assert not restored.has_unsaved_changes()
    edit_plateau(window)
    wait_for(app, sidecar.exists)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.StandardButton.Discard)
    discarded = windows()
    assert not sidecar.exists()
    assert regions(discarded, spot)["plateau"] != bounds


def test_completed_stages_recompute_in_background(windows, app):
    window = windows()
    before = services.ensure_baseline_subtracted(window.session).copy()
    services.ensure_normalized_plateau(window.session, window.workflow_preset)
    spot = window.current_spot_name()
    start, end = regions(window)["baseline"]
    window.on_plot_region_changed("baseline", (start + 0.7, end))
    wait_for(app, lambda: not window.pending_region_updates)
    assert PipelineStage.SELECTION in window.session.pipeline.cache.stages()
    assert PipelineStage.NORMALIZATION not in window.session.pipeline.cache.stages()
    idle(app, window)
    assert {PipelineStage.BASELINE, PipelineStage.NORMALIZATION}.issubset(window.session.pipeline.cache.stages())
    after = window.session.reduction_cache["baseline_subtracted_total_data"]
    mask = before["spot_name"].eq(spot)
    assert not np.allclose(before.loc[mask, "238U"], after.loc[mask, "238U"])
    raw = services.get_spot_data(window.session, spot)
    baseline = raw[raw.Time.ge(start + 0.7) & raw.Time.lt(end)]["238U"].mean()
    np.testing.assert_allclose(after.loc[mask, "238U"], raw["238U"] - baseline)
    assert window.step_next_button.isEnabled()


def test_undo_redo_drag_and_reset(windows, app):
    window = windows()
    before = regions(window)
    spot, bounds = edit_plateau(window)
    wait_for(app, lambda: not window.pending_region_updates)
    edited = regions(window)
    window.undo_selection_edit()
    assert regions(window) == before
    window.redo_selection_edit()
    assert regions(window) == edited
    window.run_autopick_current_spot()
    reset = regions(window)
    assert reset != edited
    window.undo_selection_edit()
    assert regions(window, spot) == edited
    window.redo_selection_edit()
    assert regions(window, spot) == reset
    assert bounds == edited["plateau"]


def test_second_edit_during_worker_is_retained_and_redrawn(windows, app, monkeypatch):
    window = windows()
    services.ensure_normalized_plateau(window.session, window.workflow_preset)
    entered = threading.Event()
    release = threading.Event()
    original = main_window.populate_recalculated_cache

    def delayed(*args):
        entered.set()
        assert release.wait(10), "Test did not release recomputation"
        return original(*args)

    monkeypatch.setattr(main_window, "populate_recalculated_cache", delayed)
    spot, first = edit_plateau(window)
    try:
        wait_for(app, entered.is_set)
        assert window._quiet_worker
        assert window.spot_list.isEnabled() and window.workflow_step_list.isEnabled()
        assert window.step_actions_block.isEnabled() and window.trace_plot.isEnabled()
        assert window.inspector_panel.isEnabled() and window.standards_card.isEnabled()
        wait_for(app, lambda: not window._selection_autosave_timer.isActive())
        assert not selection_autosave_path(RICH).exists()
        second = (first[0] + 0.5, first[1] - 0.5)
        window.on_plot_region_changed("plateau", second)
        window.refresh_view()
        assert window.pending_region_updates[spot]["plateau"] == second
        assert regions(window, spot)["plateau"] == first
        assert window.trace_plot.current_regions()["plateau"] == pytest.approx(second)
        # Scientific tasks still share one worker slot.
        worker = window._active_worker
        window.run_baseline_preview()
        assert window._active_worker is worker
        assert "Please wait" in window.statusBar().currentMessage()
        assert not window.flush_selection_edits()
    finally:
        release.set()
    idle(app, window)
    assert regions(window, spot)["plateau"] == second
    assert PipelineStage.NORMALIZATION in window.session.pipeline.cache.stages()


@pytest.mark.parametrize("action", ["apply_pending_project_edits", "save_project", "request_close",
                                    "choose_notebook_export", "choose_reduction_bundle_export"])
def test_actions_flush_the_queue(windows, tmp_path, monkeypatch, action):
    window = windows()
    spot, bounds = edit_plateau(window)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    # Inspect just the export boundary; the fixture cannot reduce all ratios.
    if action == "choose_reduction_bundle_export":
        monkeypatch.setattr(window, "run_background", lambda *args, **kwargs: None)
    if action == "save_project":
        window.save_project(tmp_path / "immediate.upbr")
    else:
        getattr(window, action)()
    assert not window.pending_region_updates
    assert regions(window, spot)["plateau"] == bounds


def test_invalid_plateau_is_rejected_without_dialog(windows, app):
    window = windows()
    before = regions(window)
    signal = before["signal"]
    window.on_plot_region_changed("plateau", (signal[0] - 1, signal[1] + 1))
    wait_for(app, lambda: not window.pending_region_updates)
    assert regions(window) == before
    assert "must enclose" in window.statusBar().currentMessage()
    assert "rejected" in window.log_view.toPlainText()
    assert window.trace_plot.current_regions()["plateau"] == pytest.approx(before["plateau"])
    assert not window._selection_undo


def test_invalid_window_does_not_discard_other_queued_windows(windows, app):
    window = windows()
    before = regions(window)
    start, end = before["baseline"]
    baseline = (start + 0.5, end)
    signal = before["signal"]
    window.on_plot_region_changed("baseline", baseline)
    window.on_plot_region_changed("plateau", (signal[0] - 1, signal[1] + 1))
    wait_for(app, lambda: not window.pending_region_updates)
    assert regions(window)["baseline"] == baseline
    assert regions(window)["plateau"] == before["plateau"]
    window.undo_selection_edit()
    assert regions(window) == before


def test_undo_shortcuts_respect_text_focus_and_workflow_step(windows, app):
    window = windows()
    window.show()
    window.activateWindow()
    edit_plateau(window)
    window.flush_selection_edits()
    window.spot_list.setFocus()
    app.processEvents()
    assert window.undo_selection_action.isEnabled()
    for editor in (QtWidgets.QLineEdit(window), QtWidgets.QSpinBox(window)):
        editor.show()
        editor.setFocus()
        app.processEvents()
        assert not window.undo_selection_action.isEnabled()
        assert not window.redo_selection_action.isEnabled()
        editor.hide()
    window.workflow_step_list.setCurrentRow(2)
    window.spot_list.setFocus()
    app.processEvents()
    assert not window.undo_selection_action.isEnabled()


def test_repick_all_is_one_compound_undo_and_autosaves(windows, app):
    window = windows()
    edit_plateau(window)
    window.flush_selection_edits()
    window._select_spot_in_tree(window.session.spot_names[1])
    edit_plateau(window)
    window.flush_selection_edits()
    before = window._selection_snapshot(window.session.spot_names)
    count = len(window._selection_undo)
    # Hand-edited spots are kept unless the overwrite box is ticked.
    window.repick_overwrite_manual_check.setChecked(True)
    window.run_autopick_all_spots()
    assert window.change_guard.pending
    assert window._selection_snapshot(window.session.spot_names) == before
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    after = window._selection_snapshot(window.session.spot_names)
    assert after != before
    assert len(window._selection_undo) == count + 1
    window.undo_selection_edit()
    assert window._selection_snapshot(window.session.spot_names) == before
    assert window.current_spot_name() == window.session.spot_names[0]
    window.redo_selection_edit()
    assert window._selection_snapshot(window.session.spot_names) == after
    wait_for(app, selection_autosave_path(RICH).exists)


def test_repick_bar_undo_autosaves_restored_picks(windows, app, tmp_path):
    window = windows()
    edit_plateau(window)
    window.flush_selection_edits()
    target = tmp_path / "repick-undo.upbr"
    window.save_project(target)
    before = window._selection_snapshot(window.session.spot_names)
    window.repick_overwrite_manual_check.setChecked(True)
    window.request_autopick_all_spots()
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    wait_for(app, lambda: not window._selection_autosave_pending)
    assert window._selection_snapshot(window.session.spot_names) != before
    repicked = windows(target, project=True)
    assert repicked._selection_snapshot(repicked.session.spot_names) != before
    window.pending_change_bar.undo_button.click()
    assert window._selection_snapshot(window.session.spot_names) == before
    wait_for(app, lambda: not window._selection_autosave_pending)
    restored = windows(target, project=True)
    assert restored._selection_snapshot(restored.session.spot_names) == before


def test_reload_and_primary_channel_change_do_not_offer_restore(windows, app, monkeypatch):
    window = windows()
    edit_plateau(window)
    sidecar = selection_autosave_path(RICH)
    wait_for(app, sidecar.exists)
    prompts = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda dialog: prompts.append(dialog.text()) or 0)
    window.load_experiment_directory(window.session.directory, background=False, offer_restore=False)
    idle(app, window)
    assert prompts == []
    assert sidecar.exists()
