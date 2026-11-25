"""Laser guidance controls, analysis warnings and persistence in Studio."""

from datetime import datetime, timezone
import json
import os
import time

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window, services
from GUI.main_window import MainWindow
from GUI.theme import build_application
from GUI.workflows import get_workflow_preset
from UPbRPy.laser_guidance import update_laser_qc
from synthetic_bundles import write_session_bundle


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def idle(app, window):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        app.processEvents()
        if (window._active_worker is None and not window.pending_region_updates
                and not window._selection_recompute_timer.isActive()):
            return
        time.sleep(.005)
    pytest.fail("Selection worker did not finish")


@pytest.fixture
def windows(app, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    monkeypatch.setenv("UPBRPY_AUTOSAVE_DIR", str(tmp_path / "recovery"))
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    created = []

    def make(*, laser=True, project=None, settings_only=False, shell=None):
        window = shell.add_tab() if shell is not None else MainWindow(theme_mode="light")
        created.append(window)
        if project:
            window.load_project_file(project, background=False)
        else:
            bundle = write_session_bundle(tmp_path / str(len(created)), laser=laser, upbr=False)
            if settings_only:
                path = bundle / "session_manifest.toml"
                path.write_text("\n".join(line for line in path.read_text().splitlines()
                                          if not line.startswith(("laser_on_at", "laser_off_at", "clock_offset_s")))
                                .replace("has_laser_times = true", "has_laser_times = false"))
            window.workflow_preset = get_workflow_preset("apatite")
            session = services.load_session(
                str(bundle), primary_channel="44Ca", preset=window.workflow_preset,
                import_settings={"threshold": .1, "baseline_sweep_n": 10, "peak_sweep_n": 20,
                                 "baseline_offset": 2, "plateau_offset": 2,
                                 "selection_options": {"plateau_width_mode": "sweeps"}},
            )
            window.install_loaded_session(session, str(bundle))
        idle(app, window)
        assert not errors
        return window

    yield make
    for window in created:
        idle(app, window)
        for timer in (window._selection_edit_timer, window._selection_recompute_timer,
                      window._selection_autosave_timer):
            timer.stop()
        window.has_unsaved_changes = lambda: False
        window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not errors


def spot_item(window, name):
    iterator = QtWidgets.QTreeWidgetItemIterator(window.spot_list)
    while item := iterator.value():
        if item.data(0, QtCore.Qt.ItemDataRole.UserRole) == name:
            return item
        iterator += 1
    pytest.fail(f"Missing analysis {name}")


def test_marks_use_delay_and_do_not_capture_selection_drag(windows):
    window = windows()
    row = window.session.experiment.iloc[0]
    assert window.laser_marks_action.isVisible() and window.laser_marks_action.isChecked()
    marks = window.trace_plot._laser_marks
    assert set(marks) == {"on", "off"}
    for endpoint, line in marks.items():
        assert line.value() == row[f"laser_{endpoint}_time"] + 2.
        assert line.pen.style() == QtCore.Qt.PenStyle.DashLine
        assert line.pen.color().name() not in window.selection_colors.values()
        assert not line.movable
        assert line.acceptedMouseButtons() == QtCore.Qt.MouseButton.NoButton
        assert line.zValue() < window.trace_plot._signal_boundaries["signal_start"].zValue()
    window.laser_marks_action.setChecked(False)
    assert not window.trace_plot._laser_marks
    window.laser_marks_action.setChecked(True)
    assert len(window.trace_plot._laser_marks) == 2
    window.session.experiment.attrs["laser"]["transport_delay_s"] = None
    window.refresh_view()
    assert window.trace_plot._laser_marks["on"].value() == row.laser_on_time
    assert "unknown; using 0 s" in window.trace_plot._laser_marks["on"].toolTip()
    # An incomplete firing draws its remaining endpoint only.
    window.session.experiment.loc[0, "laser_on_time"] = np.nan
    window.refresh_view()
    assert set(window.trace_plot._laser_marks) == {"off"}


def test_method_presence_menu_repick_and_single_spot(windows, app):
    window = windows()
    combo = window.selection_method_combo
    assert combo.findData("laser_guided") >= 0
    assert combo.currentData() == "threshold"
    assert "laser_guided" in window.selection_menu_actions
    # Match the predicted on/off to a different interval than the data pick.
    window.session.experiment.loc[0, "laser_on_time"] -= 3.
    before = window.session.experiment.signal_end_times.copy()
    combo.setCurrentIndex(combo.findData("laser_guided"))
    assert window.change_guard.pending
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    # Like the other methods, a new method applies at the next re-pick.
    window.run_autopick_all_spots()
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    experiment = window.session.experiment
    assert experiment.selection_method.eq("laser_guided").all()
    assert window.selection_menu_actions["laser_guided"].isChecked()
    assert not experiment.signal_end_times.equals(before)
    start = experiment.spot_starting_times.iloc[0]
    assert experiment.laser_detected_onset_time.iloc[0] == start + 6.
    assert experiment.signal_start_times.iloc[0] == start + 3.
    assert experiment.laser_onset_residual_s.iloc[0] == 3.
    assert not spot_item(window, experiment.spot_names.iloc[0]).icon(1).isNull()
    # The service path must pass both predicted times on a single-spot pick.
    window.run_autopick_current_spot()
    assert window.session.experiment.signal_end_times.iloc[0] == start + 16.
    assert window.session.experiment.laser_onset_residual_s.iloc[0] == 3.


def test_qc_threshold_updates_without_reading_or_changing_picks(windows, monkeypatch):
    window = windows()
    experiment = window.session.experiment
    experiment.loc[0, "laser_detected_onset_time"] += 3.
    update_laser_qc(experiment)
    window.refresh_view()
    assert window.spot_list.headerItem().text(1) == "Laser Δ (s)"
    item = spot_item(window, experiment.spot_names.iloc[0])
    assert item.text(1) == "3.00" and not item.icon(1).isNull()
    assert window.transport_delay_label.text() == "Transport delay: 2.00 s (4 spots)"
    before = experiment[["baseline_start_times", "plateau_start_times", "signal_end_times"]].copy()
    monkeypatch.setattr(services, "read_experiment_data", lambda *a, **k: pytest.fail("Re-read data"))
    monkeypatch.setattr(services, "autopick_signal_regions", lambda *a, **k: pytest.fail("Re-picked data"))
    window.laser_qc_threshold_spin.setValue(3.0)
    assert item.icon(1).isNull() and not experiment.laser_onset_flag.any()
    assert item.text(1) == "3.00"
    pd.testing.assert_frame_equal(before, experiment[before.columns])
    window.laser_qc_threshold_spin.setValue(2.0)
    assert not item.icon(1).isNull()
    assert "laser_qc_threshold_s" not in window.session.import_settings


def test_duration_source_enabled_and_applied_to_single_and_batch(windows):
    window = windows()
    combo = window.signal_duration_source_combo
    assert combo.isEnabled() and combo.currentData() == "fixed"
    combo.setCurrentIndex(combo.findData("laser"))
    services.autopick_selection_regions(window.session, window.current_spot_name())
    assert window.session.experiment.selection_signal_duration_limit_seconds.iloc[0] == 10.
    result = services.autopick_all_selection_regions(window.session)
    assert result["selected_count"] == 4 and not result["failures"]
    assert window.session.experiment.selection_signal_duration_limit_seconds.eq(10.).all()
    combo.setCurrentIndex(combo.findData("fixed"))
    assert "signal_duration_limit_source" not in window.session.import_settings


def test_single_spot_fallback_and_incomplete_firing(windows):
    window = windows()
    session = window.session
    name = window.current_spot_name()
    row_mask = session.experiment.spot_names.eq(name)
    session.import_settings["selection_method"] = "laser_guided"
    session.import_settings["selection_fallback_methods"] = ("threshold",)
    session.import_settings["signal_duration_limit_source"] = "laser"
    session.import_settings["signal_duration_limit_seconds"] = 7.
    session.experiment.loc[row_mask, "laser_on_time"] = np.nan
    session.experiment.loc[row_mask, "laser_off_time"] = np.nan
    session.experiment.loc[row_mask, "laser_duration_s"] = np.nan
    services.autopick_selection_regions(session, name)
    row = session.experiment.loc[row_mask].iloc[0]
    assert row.selection_method == "threshold"
    assert row.selection_signal_duration_limit_seconds == 7.
    assert pd.isna(row.laser_onset_residual_s)
    window.refresh_view()
    assert not window.trace_plot._laser_marks
    assert spot_item(window, name).text(1) == ""


def test_opening_another_session_does_not_carry_laser_method(windows, app, tmp_path):
    window = windows()
    window.selection_menu_actions["laser_guided"].trigger()
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    # Like the other methods, a new method applies at the next re-pick.
    window.run_autopick_all_spots()
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    assert window.selection_method == "laser_guided"
    bundle = write_session_bundle(tmp_path / "plain", laser=False, upbr=False)
    window.load_experiment_directory(str(bundle), primary_channel="44Ca", background=False, offer_restore=False)
    assert window.selection_method == "threshold"
    assert window.selection_method_combo.findData("laser_guided") == -1
    assert window.session.import_settings["selection_method"] == "threshold"
    assert "laser" not in window.session.experiment.attrs
    assert window.spot_list.columnCount() == 1
    assert window.laser_selection_controls.isHidden()
    assert not window.laser_marks_action.isVisible()


def test_settings_only_has_qc_but_no_timing_tools(windows):
    window = windows(settings_only=True)
    assert window.spot_list.columnCount() == 2
    assert window.laser_marks_action.isVisible() and not window.laser_marks_action.isEnabled()
    assert not window.trace_plot._laser_marks
    assert not window.signal_duration_source_combo.isEnabled()
    assert window.selection_method_combo.findData("laser_guided") == -1


def test_project_roundtrip_and_default_omission(windows, app, tmp_path):
    window = windows()
    path = tmp_path / "laser.upbr"
    window.save_project(path)
    document = json.loads(path.read_text())
    assert "laser_qc_threshold_s" not in json.dumps(document)
    assert "signal_duration_limit_source" not in json.dumps(document)
    assert "laser_marks" not in document["workspace"]
    window.laser_qc_threshold_spin.setValue(.5)
    window.signal_duration_source_combo.setCurrentIndex(1)
    window.laser_marks_action.setChecked(False)
    window.selection_menu_actions["laser_guided"].trigger()
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    # Like the other methods, a new method applies at the next re-pick.
    window.run_autopick_all_spots()
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    window.save_project(path)
    document = json.loads(path.read_text())
    config = document["pipeline"]["config"]
    assert config["selection_options"]["laser_qc_threshold_s"] == .5
    assert config["selection_options"]["signal_duration_limit_source"] == "laser"
    assert config["selection_method"] == "laser_guided"
    reopened = windows(project=path)
    assert reopened.laser_qc_threshold_spin.value() == .5
    assert reopened.signal_duration_source_combo.currentData() == "laser"
    assert reopened.selection_method_combo.currentData() == "laser_guided"
    assert not reopened.laser_marks_action.isChecked()
    assert not reopened.trace_plot._laser_marks


def test_no_laser_ui_and_project_bytes_stay_identical(windows, tmp_path, monkeypatch):
    import UPbRPy.project as project

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 3, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(project, "datetime", FrozenDateTime)
    window = windows(laser=False)
    assert window.spot_list.columnCount() == 1 and window.spot_list.isHeaderHidden()
    assert window.laser_selection_controls.isHidden()
    assert window.signal_duration_source_combo.isHidden()
    assert not window.signal_duration_source_combo.isEnabled()
    assert not window.laser_marks_action.isVisible()
    assert window.selection_method_combo.findData("laser_guided") == -1
    assert "laser_guided" not in window.selection_menu_actions
    assert not window.trace_plot._laser_marks
    path = tmp_path / "plain.upbr"
    window.save_project(path)
    before = path.read_bytes()
    window.laser_qc_threshold_spin.setValue(.25)
    window.signal_duration_source_combo.setCurrentIndex(1)
    window.laser_marks_action.setChecked(False)
    window.refresh_view()
    window.save_project(path)
    assert path.read_bytes() == before
    assert "laser_qc_threshold_s" not in before.decode()
    assert "signal_duration_limit_source" not in before.decode()
    assert "laser_marks" not in before.decode()


@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_laser_controls_fit_laptop(windows, app, size):
    from GUI.studio_window import StudioWindow
    from test_small_screens import clipped_widgets, process

    shell = StudioWindow(theme_mode="light")
    window = windows(shell=shell)
    try:
        shell.resize(*size)
        shell.show()
        window.apply_default_layout()
        window.workflow_step_list.setCurrentRow(1)
        window.autopick_settings_toggle.setChecked(True)
        process(app)
        assert (shell.width(), shell.height()) == size
        assert shell.minimumSizeHint().width() <= size[0]
        assert shell.minimumSizeHint().height() <= size[1]
        assert window.laser_selection_controls.isVisible()
        assert not clipped_widgets(shell)
    finally:
        shell.take_tab(window)
        shell.close()
