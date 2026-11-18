"""Nominal on-peak duration and scoped re-picks in Studio."""

from pathlib import Path
import time

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from GUI import main_window, services
from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
RICH = FIXTURE / "Zircon_demo_Rich.csv"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def wait_for(app, condition, timeout=20):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert condition(), "Timed out"


def idle(app, window):
    wait_for(app, lambda: window._active_worker is None and not window.pending_region_updates
             and not window._selection_recompute_timer.isActive()
             and not window._selection_edit_timer.isActive())


def make_window(app):
    window = MainWindow(theme_mode="light")
    window.selection_edit_debounce_ms = 20
    window.selection_recompute_debounce_ms = 40
    window.selection_autosave_debounce_ms = 60
    window.resize(1280, 800)
    window.show()
    window.load_experiment_directory(str(RICH), background=False)
    window.workflow_step_list.setCurrentRow(1)
    app.processEvents()
    idle(app, window)
    return window


def close(app, window):
    if isValid(window):
        idle(app, window)
        for timer in (window._selection_edit_timer, window._selection_recompute_timer,
                      window._selection_autosave_timer):
            timer.stop()
        window.has_unsaved_changes = lambda: False
        window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    window = make_window(app)
    yield window
    close(app, window)
    assert not errors


def regions(window, spot):
    return services.get_selection_regions(window.session, spot)


def test_detected_duration_is_shown_and_setting_is_saved(window, app, tmp_path):
    label = window.on_peak_detected_label
    assert label.text().startswith("Detected: median ")
    assert window.on_peak_duration_spin.specialValueText() == "Auto"
    median = services.detected_on_peak_summary(window.session)["median"]
    window.on_peak_duration_spin.setValue(round(0.8 * median, 1))
    assert window.session.import_settings["signal_duration_limit_seconds"] == pytest.approx(
        round(0.8 * median, 1))
    project = tmp_path / "onpeak.upbr"
    window.save_project(project)
    reopened = MainWindow(theme_mode="light")
    try:
        reopened.load_project_file(project, background=False)
        idle(app, reopened)
        assert reopened.on_peak_duration_spin.value() == pytest.approx(round(0.8 * median, 1))
    finally:
        close(app, reopened)
    window.on_peak_duration_spin.setValue(0.0)
    assert window.session.import_settings["signal_duration_limit_seconds"] is None


def test_repick_all_applies_duration_and_skips_hand_edits(window, app):
    names = [str(name) for name in window.session.spot_names]
    edited = window.current_spot_name()
    start, end = regions(window, edited)["plateau"]
    window.on_plot_region_changed("plateau", (start, end - 0.4))
    idle(app, window)
    hand = regions(window, edited)["plateau"]
    other = next(name for name in names if name != edited)
    other_before = regions(window, other)["plateau"]
    median = services.detected_on_peak_summary(window.session)["median"]
    window.on_peak_duration_spin.setValue(round(0.6 * median, 1))
    assert not window.repick_overwrite_manual_check.isChecked()
    window.run_autopick_all_spots()
    assert window.change_guard.pending
    assert regions(window, other)["plateau"] == other_before
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    assert regions(window, edited)["plateau"] == hand
    assert regions(window, other)["plateau"][1] < other_before[1]
    message = window.statusBar().currentMessage()
    assert "skipped 1 hand-edited spot" in message
    assert "nominal on-peak" in message
    # Undo restores the earlier windows and keeps hand-edit flags as they were.
    window.undo_selection_edit()
    idle(app, window)
    assert regions(window, other)["plateau"] == other_before
    assert services.manual_selection_spots(window.session) == {edited}
    window.repick_overwrite_manual_check.setChecked(True)
    window.run_autopick_all_spots()
    assert window.change_guard.pending
    assert regions(window, edited)["plateau"] == hand
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    assert regions(window, edited)["plateau"] != hand
    assert services.manual_selection_spots(window.session) == set()


def test_repick_current_sample_only_touches_that_sample(window, app):
    experiment = window.session.experiment
    spot = window.current_spot_name()
    sample = str(experiment.loc[experiment["spot_names"].astype(str).eq(spot), "sample"].iloc[0])
    in_sample = set(experiment.loc[experiment["sample"].astype(str).eq(sample), "spot_names"].astype(str))
    outside = next(str(name) for name in experiment["spot_names"] if str(name) not in in_sample)
    outside_before = regions(window, outside)["plateau"]
    median = services.detected_on_peak_summary(window.session)["median"]
    window.on_peak_duration_spin.setValue(round(0.6 * median, 1))
    window.repick_sample_action.trigger()
    assert window.change_guard.pending
    assert window.pending_change_bar.message.text() == (
        f"Re-pick all {len(in_sample)} spots (sample {sample}) with the auto-pick settings. "
        "Undo restores the current picks."
    )
    assert window._active_worker is None
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    assert regions(window, outside)["plateau"] == outside_before
    assert f"sample {sample}" in window.statusBar().currentMessage()


def test_repick_all_open_tabs_uses_each_tabs_settings(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    shell = StudioWindow(theme_mode="light")
    try:
        shell.show()
        first = shell.active_reduction
        second = shell.add_tab()
        for reduction in (first, second):
            reduction.selection_recompute_debounce_ms = 40
            reduction.selection_autosave_debounce_ms = 60
            reduction.load_experiment_directory(str(RICH), background=False)
            idle(app, reduction)
        median = services.detected_on_peak_summary(first.session)["median"]
        first.on_peak_duration_spin.setValue(round(0.6 * median, 1))
        spot = str(first.session.spot_names[0])
        before_first = regions(first, spot)["plateau"]
        before_second = regions(second, spot)["plateau"]
        second.repick_tabs_action.trigger()
        for reduction, before in ((first, before_first), (second, before_second)):
            assert reduction.change_guard.pending
            assert reduction._active_worker is None
            assert regions(reduction, spot)["plateau"] == before
        # Each tab retains its own confirmation; cancelling one does not apply either.
        first.pending_change_bar.cancel_button.click()
        assert regions(first, spot)["plateau"] == before_first
        assert second.change_guard.pending
        shell.repick_all_tabs()
        for reduction in (first, second):
            reduction.pending_change_bar.apply_button.click()
        for reduction in (first, second):
            idle(app, reduction)
        assert regions(first, spot)["plateau"][1] < before_first[1]
        assert regions(second, spot)["plateau"] == before_second
        assert "Re-picked" in first.statusBar().currentMessage()
        assert "Re-picked" in second.statusBar().currentMessage()
    finally:
        for index in range(shell.tabs.count()):
            reduction = shell.tabs.widget(index)
            idle(app, reduction)
            reduction.has_unsaved_changes = lambda: False
        shell.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
