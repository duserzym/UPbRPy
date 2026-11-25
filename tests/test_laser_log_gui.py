"""Standalone laser preview, selections and saved projects, offscreen."""

import json
from pathlib import Path

import pandas as pd
import pytest

from PySide6 import QtWidgets
from GUI import services
from GUI.laser_log_dialog import LaserLogDialog, laser_log_start_path
from UPbRPy.laser_tie import tie_laser_log
from UPbRPy.laserlog import read_lase
from UPbRPy.pipeline import PipelineStage
from UPbRPy.reproducibility import selection_export_frame
from test_laser_selection_gui import app, windows, idle
from test_laser_tie import session_log


def make_tie(window, *, times=True):
    path = session_log(Path(window.session.directory), times=times)
    return tie_laser_log(read_lase(path), window.session.qtegra)


def test_dialog_summary_key_threshold_and_confirmation(windows, app):
    window = windows(laser=False)
    tie = make_tie(window)
    dialog = LaserLogDialog(read_lase(Path(window.session.directory) / tie.file_name), window.session.qtegra)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.width() <= 1280 and dialog.height() <= 800
        assert dialog.minimumSizeHint().width() <= 1280
        assert "Linked: 4" in dialog.summary_label.text()
        assert "3596" in dialog.summary_label.text()
        dialog.key_combo.setCurrentIndex(1)
        assert dialog.tie.spot_key == "description"
        dialog.threshold_spin.setValue(12)
        assert dialog.tie.threshold_s == 12
        assert dialog.apply_button.isEnabled()
    finally:
        dialog.close()
    path = session_log(Path(window.session.directory), times=False)
    dialog = LaserLogDialog(read_lase(path), window.session.qtegra)
    try:
        assert dialog.tie.requires_confirmation
        assert not dialog.apply_button.isEnabled()
        dialog.accept()
        assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted
        dialog.confirm_check.setChecked(True)
        assert dialog.apply_button.isEnabled()
        dialog.accept()
        assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_nearby_file_preselection(windows, tmp_path):
    window = windows(laser=False)
    folder = Path(window.session.directory)
    assert laser_log_start_path(str(folder)) == str(folder)
    path = session_log(folder.parent)
    assert laser_log_start_path(str(folder)) == str(path)
    local = session_log(folder)
    assert laser_log_start_path(str(folder)) == str(local)
    (folder / "another.lase").write_text("synthetic")
    (folder.parent / "another.lase").write_text("synthetic")
    assert laser_log_start_path(str(folder)) == str(folder)


def test_apply_remove_preserves_picks_and_numbers(windows, monkeypatch):
    window = windows(laser=False)
    before = window.session.experiment.copy()
    cache_value = object()
    window.session.reduction_cache["baseline_subtracted_total_data"] = cache_value
    window.session.pipeline.cache.set(PipelineStage.BASELINE, cache_value)
    tie = make_tie(window)
    monkeypatch.setattr(services, "autopick_signal_regions", lambda *a, **k: pytest.fail("Re-picked"))
    monkeypatch.setattr(services, "read_experiment_data", lambda *a, **k: pytest.fail("Re-imported"))
    window.apply_laser_log(tie)
    assert window.session.laser_data_available and window._has_laser_times()
    assert window.session.laser_tie == tie
    assert window.session.experiment.attrs["laser"]["source"] == "lase"
    assert window.session.experiment.attrs["laser"]["transport_delay_s"] == 6.
    assert window.selection_method_combo.findData("laser_guided") >= 0
    assert window.spot_list.columnCount() == 2
    analytical = window.session.experiment[before.columns].copy()
    analytical.attrs = before.attrs
    pd.testing.assert_frame_equal(analytical, before, check_flags=False)
    assert not any("stage" in c for c in window.session.experiment)
    # Synchronizing for Save must retain all numerical products.
    services.ensure_shared_pipeline(window.session, window.workflow_preset)
    assert window.session.reduction_cache["baseline_subtracted_total_data"] is cache_value
    assert "stage_um" not in json.dumps(window.session.pipeline.manifest())
    window.remove_laser_log()
    assert window.session.laser_tie is None and not window._has_laser_times()
    pd.testing.assert_frame_equal(window.session.experiment, before)


def test_settings_only_service_and_gui_confirm(windows):
    window = windows(laser=False)
    tie = make_tie(window, times=False)
    with pytest.raises(ValueError, match="Confirm"):
        window.apply_laser_log(tie)
    window.apply_laser_log(tie, confirmed=True)
    assert window.session.laser_data_available
    assert not window._has_laser_times()
    assert window.spot_list.columnCount() == 2
    assert window.selection_method_combo.findData("laser_guided") == -1


@pytest.mark.parametrize("setting", ["method", "duration"])
def test_guided_apply_cancel_confirm_undo(windows, app, setting):
    window = windows()
    tie = make_tie(window)
    if setting == "method":
        window.select_selection_method("laser_guided")
        window.pending_change_bar.apply_button.click()
        idle(app, window)
    else:
        window.signal_duration_source_combo.setCurrentIndex(1)
    before = window.session.experiment.copy()
    window.apply_laser_log(tie)
    assert window.change_guard.pending
    assert window.session.laser_tie is None
    window.pending_change_bar.cancel_button.click()
    pd.testing.assert_frame_equal(window.session.experiment, before)
    window.apply_laser_log(tie)
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    assert window.session.laser_tie == tie
    assert window.change_guard.record is not None
    if setting == "method":
        assert window.session.experiment.selection_method.eq("laser_guided").all()
    else:
        assert window.session.experiment.selection_signal_duration_limit_seconds.eq(10.).all()
    window.change_guard.undo()
    assert window.session.laser_tie is None
    pd.testing.assert_frame_equal(window.session.experiment, before)


def test_project_saved_tie_survives_moved_or_changed_file(windows, tmp_path, monkeypatch):
    window = windows(laser=False)
    tie = make_tie(window)
    window.apply_laser_log(tie)
    path = tmp_path / "standalone.upbr"
    window.save_project(path)
    document = json.loads(path.read_text())
    assert document["laser_log"] == tie.to_dict()
    assert "laser_log" not in document["pipeline"]["config"]
    laser_path = Path(window.session.directory) / tie.file_name
    laser_path.unlink()
    reopened = windows(project=path)
    assert reopened.session.laser_tie == tie
    pd.testing.assert_series_equal(reopened.session.experiment.laser_on_time,
                                   window.session.experiment.laser_on_time)
    assert reopened.session.experiment.attrs["laser"]["source"] == "lase"
    laser_path.write_text("different synthetic content")
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    reopened = windows(project=path)
    assert reopened.session.laser_tie == tie and len(warnings) == 1
    assert "Keeping" in warnings[0]
    reopened.remove_laser_log()
    reopened.save_project(path)
    assert "laser_log" not in json.loads(path.read_text())


def test_manifest_replacement_requires_consent_and_remove_restores(windows, monkeypatch):
    window = windows()
    tie = make_tie(window)
    before = window.session.experiment.laser_on_time.copy()
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *a: (
        str(Path(window.session.directory) / tie.file_name), ""))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a: QtWidgets.QMessageBox.StandardButton.No)
    monkeypatch.setattr(LaserLogDialog, "exec", lambda self: pytest.fail("Consent not given"))
    window.choose_laser_log()
    assert window.session.laser_tie is None
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a: QtWidgets.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(LaserLogDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    window.choose_laser_log()
    assert window.session.laser_tie == tie
    window.remove_laser_log()
    pd.testing.assert_series_equal(window.session.experiment.laser_on_time, before)
    assert window.session.experiment.attrs["laser"]["source"] == "manifest"


def test_tie_survives_reimport_and_spot_rename(windows, app):
    window = windows()
    tie = make_tie(window)
    before = window.session.experiment.laser_on_time.copy()
    original = window.session.experiment.spot_names.iloc[0]
    start = window.session.experiment.spot_starting_times.iloc[0]
    window.session.experiment.loc[0, "spot_names"] = "Renamed"
    window.session.experiment.attrs["spot_renames"] = {"Renamed": original}
    window.session.total_data["spot_name"] = window.session.total_data.spot_name.replace({original: "Renamed"})
    window.apply_laser_log(tie)
    assert window.session.experiment.laser_on_time.iloc[0] == start
    window.remove_laser_log()
    pd.testing.assert_series_equal(window.session.experiment.laser_on_time, before)
    window.apply_laser_log(tie)
    updated = services.reimport_detector_calibration(window.session, {"mode": "qtegra"})
    assert updated.laser_tie == tie
    assert updated.experiment.laser_on_time.iloc[0] == start
    window.load_experiment_directory(window.session.directory, primary_channel="44Ca",
                                      background=False, offer_restore=False)
    idle(app, window)
    assert window.session.laser_tie == tie
    assert window.session.experiment.attrs["laser"]["source"] == "lase"


def test_manual_windows_use_independent_transport_delay(windows):
    window = windows(laser=False)
    tie = make_tie(window)
    window.session.experiment.loc[0, "signal_start_times"] += 20.
    window.session.experiment.loc[0, "selection_manual"] = True
    before = window.session.experiment.signal_start_times.copy()
    window.apply_laser_log(tie)
    assert window.session.experiment.attrs["laser"]["n_pairs"] == 4
    assert window.session.experiment.attrs["laser"]["transport_delay_s"] == 6.
    pd.testing.assert_series_equal(window.session.experiment.signal_start_times, before)


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("stored_tie", [False, True])
def test_apply_tie_to_reviewed_session_preserves_selections_and_cache(
    windows, tmp_path, monkeypatch, restored, stored_tie,
):
    window = windows(laser=False)
    tie = make_tie(window)
    if stored_tie:
        window.apply_laser_log(tie)
    experiment = window.session.experiment
    experiment.loc[:, list(services.SELECTION_WINDOW_COLUMNS)] += 1.25
    experiment["selection_manual"] = True
    reviewed = selection_export_frame(experiment).to_json().encode()
    if restored:
        path = tmp_path / "reviewed.upbr"
        window.save_project(path)
        window = windows(project=path)
        assert selection_export_frame(window.session.experiment).to_json().encode() == reviewed
        if stored_tie:
            # The reader must bypass its saved-selection shortcut with a tie.
            assert window.session.experiment.attrs["laser"]["n_pairs"] == 4
            assert window.session.experiment.attrs["laser"]["transport_delay_s"] == 6.
            assert window.session.experiment.laser_detected_onset_time.eq(
                window.session.experiment.spot_starting_times + 6.
            ).all()
    session = window.session
    before = session.experiment.drop(columns=[c for c in session.experiment if c.startswith("laser_")]).copy()
    sweeps = session.total_data.copy()
    cache_value = object()
    session.reduction_cache["baseline_subtracted_total_data"] = cache_value
    session.pipeline.cache.set(PipelineStage.BASELINE, cache_value)
    # Replacing timing must recompute even if a saved/reference onset exists.
    if "laser_detected_onset_time" in session.experiment:
        session.experiment["laser_detected_onset_time"] = -100.
    monkeypatch.setattr(services, "read_experiment_data", lambda *a, **k: pytest.fail("Re-imported"))
    monkeypatch.setattr(services, "autopick_signal_regions", lambda *a, **k: pytest.fail("Changed selections"))
    services.apply_session_laser_tie(session, tie)
    assert session.experiment.attrs["laser"]["n_pairs"] == 4
    assert session.experiment.attrs["laser"]["transport_delay_s"] == 6.
    assert session.experiment.laser_detected_onset_time.eq(session.experiment.spot_starting_times + 6.).all()
    assert session.experiment.laser_onset_residual_s.eq(0.).all()
    assert selection_export_frame(session.experiment).to_json().encode() == reviewed
    analytical = session.experiment[before.columns].copy()
    analytical.attrs = before.attrs
    pd.testing.assert_frame_equal(analytical, before, check_exact=True)
    pd.testing.assert_frame_equal(session.total_data, sweeps, check_exact=True)
    assert session.reduction_cache["baseline_subtracted_total_data"] is cache_value
    assert session.pipeline.cache.get(PipelineStage.BASELINE) is cache_value
