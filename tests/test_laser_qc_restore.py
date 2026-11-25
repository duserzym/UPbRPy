"""Laser restore shares selection validation and replays QC separately."""

from datetime import datetime, timezone
import json
import pickle

import numpy as np
import pandas as pd
import pytest

from GUI import results_cache, restore_process, services
from GUI.workflows import get_workflow_preset
import UPbRPy.io as engine_io
import UPbRPy.project as project
from UPbRPy.laser_guidance import update_laser_qc
from UPbRPy.laser_tie import tie_laser_log
from UPbRPy.laserlog import read_lase
from UPbRPy.pipeline import PipelineStage
from UPbRPy.project import selection_records, write_reduction_project
from UPbRPy.spot_edits import restore_spot_edits
import synthetic_bundles
from synthetic_bundles import write_session_bundle
from test_laser_tie import session_log
from test_laser_selection_gui import app, idle, windows  # shared offscreen fixtures


QC_COLUMNS = ["laser_detected_onset_time", "laser_onset_residual_s", "laser_onset_flag"]


def laser_session(tmp_path, *, source="manifest", method="threshold", laser=True):
    bundle = write_session_bundle(tmp_path, laser=laser and source == "manifest", upbr=False)
    preset = get_workflow_preset("apatite")
    session = services.load_session(
        str(bundle), primary_channel="44Ca", preset=preset,
        import_settings={"threshold": .1, "baseline_sweep_n": 10, "peak_sweep_n": 20,
                         "baseline_offset": 2, "plateau_offset": 2,
                         "selection_options": {"plateau_width_mode": "sweeps"}},
    )
    if laser and source == "tie":
        tie = tie_laser_log(read_lase(session_log(bundle)), session.qtegra)
        services.apply_session_laser_tie(session, tie)
    if method != "threshold":
        session.import_settings["selection_method"] = method
        session.import_settings["selection_fallback_methods"] = ("threshold",)
        services.autopick_all_selection_regions(session)
    return session, preset


def track_picks(monkeypatch, *, max_spots=1, allow_reference=False):
    original = engine_io.auto_select_baseline_peak
    sizes = []

    def tracked(experiment, data, *args, **kwargs):
        sizes.append(data.spot_name.nunique())
        assert sizes[-1] <= max_spots, "Unexpected full selection pass"
        return original(experiment, data, *args, **kwargs)

    def fail(*args, **kwargs):
        pytest.fail("Project reopen ran a reference pass")

    monkeypatch.setattr(engine_io, "auto_select_baseline_peak", tracked)
    if not allow_reference:
        monkeypatch.setattr(engine_io, "update_laser_detected_onsets", fail)
        monkeypatch.setattr(services, "update_laser_detected_onsets", fail)
    return sizes


@pytest.mark.parametrize("source", ["manifest", "tie"])
@pytest.mark.parametrize("method", ["threshold", "laser_guided", "rolling_sigma"])
def test_cold_restore_reuses_onsets_and_preserves_reviewed_windows(tmp_path, monkeypatch, source, method):
    session, preset = laser_session(tmp_path, source=source, method=method)
    # QC must use the stored independent onset, never the reviewed windows.
    session.experiment.loc[:, list(services.SELECTION_WINDOW_COLUMNS)] += .25
    session.experiment["selection_manual"] = [True, False, True, False]
    session.experiment.loc[0, "laser_detected_onset_time"] += 4.
    update_laser_qc(session.experiment)
    expected = session.experiment[QC_COLUMNS].copy()
    records = selection_records(session.experiment)
    pipeline = services.ensure_shared_pipeline(session, preset)
    path = write_reduction_project(pipeline, tmp_path / "qc.upbr", workspace={"review_flags": {"AFB_3": True}})
    document = project.read_reduction_project(path)
    assert len(document["laser_qc"]["detected_onsets"]) == 4
    if method == "laser_guided":
        assert document["laser_qc"]["reference_method"] == "threshold"
    sizes = track_picks(monkeypatch, max_spots=1 if method == "threshold" else 4,
                        allow_reference=method == "laser_guided")
    restored = services.restore_session_from_project(path)
    assert sizes == {"threshold": [1], "rolling_sigma": [4], "laser_guided": [4, 4]}[method]
    actual = restored.session.experiment[QC_COLUMNS].copy()
    actual.attrs = expected.attrs
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    assert restored.session.experiment.attrs["laser"] == session.experiment.attrs["laser"]
    assert selection_records(restored.session.experiment) == records
    assert restored.workspace["review_flags"] == {"AFB_3": True}


@pytest.mark.parametrize("change", ["missing_windows", "missing_diagnostics", "selection_error",
                                    "rolling_sigma", "fallback", "custom_threshold"])
@pytest.mark.parametrize("stored_qc", [True, False])
def test_full_restore_matches_nonlaser_windows_and_numbers(tmp_path, monkeypatch, change, stored_qc):
    session, preset = laser_session(tmp_path, method="rolling_sigma" if change == "rolling_sigma" else "threshold")
    pipeline = services.ensure_shared_pipeline(session, preset)
    pipeline.run_baseline()
    path = write_reduction_project(pipeline, tmp_path / "incomplete.upbr")
    document = project.read_reduction_project(path)
    if change in {"missing_windows", "missing_diagnostics"}:
        columns = ("baseline_start_times", "signal_end_times") if change == "missing_windows" else ("selection_detection_index",)
        for record in document["selections"]:
            for column in columns:
                record.pop(column)
    elif change == "selection_error":
        document["selections"][0]["selection_error"] = "Saved detection failure"
    elif change == "fallback":
        document["pipeline"]["config"]["selection_options"]["selection_fallback_methods"] = ["rolling_sigma"]
    elif change == "custom_threshold":
        def custom(values, **kwargs):
            return engine_io.threshold_onset(values, **kwargs)

        monkeypatch.setitem(engine_io._SELECTORS, "threshold", custom)
    if not stored_qc:
        document.pop("laser_qc")
    project.write_project_document(document, path)
    sizes = track_picks(monkeypatch, max_spots=4)
    timed = services.restore_session_from_project(path).session
    # Keep the same source, project, sample metadata and settings; remove only
    # timing attachment for the ordinary non-laser restore used as reference.
    monkeypatch.setattr("UPbRPy.laser_guidance.add_manifest_laser_timing", lambda *args: None)
    plain = services.restore_session_from_project(path).session
    assert sizes == [4, 4]
    assert selection_records(timed.experiment) == selection_records(plain.experiment)
    assert timed.experiment[list(services.SELECTION_WINDOW_COLUMNS)].notna().all().all()
    pd.testing.assert_frame_equal(timed.pipeline.run_baseline(), plain.pipeline.run_baseline(), check_exact=True)
    if stored_qc:
        assert project.laser_qc_record(timed.experiment) == document["laser_qc"]
    else:
        assert timed.experiment.laser_detected_onset_time.isna().all()
        assert timed.experiment.laser_onset_residual_s.isna().all()
        assert not timed.experiment.attrs["laser"]["qc_onsets_computed"]


def test_laser_fast_template_retains_duration_limit_without_computing_qc(tmp_path, monkeypatch):
    session, preset = laser_session(tmp_path)
    session.import_settings["signal_duration_limit_source"] = "laser"
    services.autopick_all_selection_regions(session)
    records = selection_records(session.experiment)
    sizes = track_picks(monkeypatch)
    experiment, _, _ = engine_io.read_experiment_data(
        session.qtegra, primary_channel="44Ca", session_manifest=f"{session.directory}/session_manifest.toml",
        restored_selections=records, signal_duration_limit_source="laser",
    )
    assert sizes == [1]
    selected = experiment.selection_error.isna() & experiment.signal_start_times.notna()
    assert experiment.loc[selected, "selection_signal_duration_limit_seconds"].eq(10.).all()
    assert experiment.laser_detected_onset_time.isna().all()
    assert not experiment.attrs["laser"]["qc_onsets_computed"]


@pytest.mark.parametrize("source", ["manifest", "tie"])
def test_old_project_stays_uncomputed_until_explicit_action(tmp_path, monkeypatch, source):
    session, preset = laser_session(tmp_path, source=source, method="laser_guided")
    pipeline = services.ensure_shared_pipeline(session, preset)
    path = write_reduction_project(pipeline, tmp_path / "old.upbr")
    document = project.read_reduction_project(path)
    document.pop("laser_qc")
    project.write_project_document(document, path)
    with monkeypatch.context() as blocked:
        track_picks(blocked, max_spots=4, allow_reference=True)
        restored = services.restore_session_from_project(path).session
    assert restored.experiment.laser_detected_onset_time.isna().all()
    assert restored.experiment.laser_onset_residual_s.isna().all()
    assert not restored.experiment.laser_onset_flag.any()
    assert restored.experiment.attrs["laser"]["transport_delay_s"] is None
    assert restored.experiment.attrs["laser"]["qc_onsets_computed"] is False
    before = selection_records(restored.experiment)
    marker = object()
    restored.reduction_cache["baseline_subtracted_total_data"] = marker
    restored.pipeline.cache.set(PipelineStage.BASELINE, marker)
    cached = dict(restored.pipeline.cache.as_mapping())
    services.compute_session_laser_qc(restored)
    assert selection_records(restored.experiment) == before
    assert restored.experiment.laser_detected_onset_time.notna().all()
    assert restored.experiment.attrs["laser"]["qc_onsets_computed"] is True
    assert restored.pipeline.matches_source_experiment(restored.experiment)
    services.ensure_shared_pipeline(restored, preset)
    assert restored.reduction_cache["baseline_subtracted_total_data"] is marker
    assert restored.pipeline.cache.stages() == tuple(cached)
    assert all(restored.pipeline.cache.get(key) is value for key, value in cached.items())
    write_reduction_project(restored.pipeline, path)
    with monkeypatch.context() as blocked:
        track_picks(blocked, max_spots=4, allow_reference=True)
        replay = services.restore_session_from_project(path).session
    np.testing.assert_array_equal(replay.experiment.laser_detected_onset_time,
                                  restored.experiment.laser_detected_onset_time)


def test_qc_restore_follows_renames_and_removals(tmp_path, monkeypatch):
    session, preset = laser_session(tmp_path)
    session.experiment, session.total_data = restore_spot_edits(
        session.experiment, session.total_data,
        {"spot_renames": {"Renamed_1": "AFB_3"}, "removed_spots": ["KD27_4"]},
    )
    update_laser_qc(session.experiment)
    pipeline = services.ensure_shared_pipeline(session, preset)
    path = write_reduction_project(pipeline, tmp_path / "edited.upbr")
    track_picks(monkeypatch, max_spots=4)
    restored = services.restore_session_from_project(path).session
    assert restored.spot_names == session.spot_names
    assert project.laser_qc_record(restored.experiment) == project.laser_qc_record(session.experiment)
    assert restored.experiment.attrs["laser"] == session.experiment.attrs["laser"]


def test_settings_only_laser_project_uses_normal_selection_validation(tmp_path, monkeypatch):
    bundle = write_session_bundle(tmp_path, laser=True, upbr=False)
    manifest = bundle / "session_manifest.toml"
    manifest.write_text("\n".join(line for line in manifest.read_text().splitlines()
                                  if not line.startswith(("laser_on_at", "laser_off_at", "clock_offset_s"))))
    preset = get_workflow_preset("apatite")
    session = services.load_session(str(bundle), primary_channel="44Ca", preset=preset)
    pipeline = services.ensure_shared_pipeline(session, preset)
    path = write_reduction_project(pipeline, tmp_path / "settings-only.upbr")
    assert "laser_qc" not in project.read_reduction_project(path)
    track_picks(monkeypatch, max_spots=4)
    restored = services.restore_session_from_project(path).session
    assert selection_records(restored.experiment) == selection_records(session.experiment)
    assert "laser" not in restored.experiment.attrs


def test_failed_reference_onsets_remain_null_but_computed(tmp_path, monkeypatch):
    session, preset = laser_session(tmp_path)
    records = selection_records(session.experiment)

    def failed(*args, **kwargs):
        raise engine_io.SelectionError("Synthetic detection failure")

    with monkeypatch.context() as failures:
        failures.setattr(engine_io, "autopick_signal_regions", failed)
        services.compute_session_laser_qc(session)
    assert selection_records(session.experiment) == records
    qc = project.laser_qc_record(session.experiment)
    assert qc["computed"] is True
    assert set(qc["detected_onsets"].values()) == {None}
    path = write_reduction_project(services.ensure_shared_pipeline(session, preset), tmp_path / "failed.upbr")
    track_picks(monkeypatch)
    restored = services.restore_session_from_project(path).session
    assert project.laser_qc_record(restored.experiment) == qc


@pytest.mark.parametrize("qc", [[], {}, {"detected_onsets": []},
    {"detected_onsets": {"A": "bad"}}, {"detected_onsets": {"A": float("inf")}},
    {"detected_onsets": {}, "computed": "false"},
    {"detected_onsets": {}, "reference_method": 3},
])
def test_invalid_qc_records_are_rejected(tmp_path, qc):
    session, preset = laser_session(tmp_path)
    document = project.build_reduction_project(services.ensure_shared_pipeline(session, preset), tmp_path / "bad.upbr")
    document["laser_qc"] = qc
    with pytest.raises(ValueError, match="Project laser QC"):
        project.validate_reduction_project(document)


def test_warm_laser_results_still_bypass_cold_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("UPBRPY_RESULTS_CACHE_DIR", str(tmp_path / "cache"))
    session, preset = laser_session(tmp_path)
    path = write_reduction_project(services.ensure_shared_pipeline(session, preset), tmp_path / "warm.upbr")
    request = results_cache.prepare(path)
    assert request is not None
    with results_cache.DependencyRecorder(request) as recorder:
        cold = services.restore_session_from_project(path)
    payload = pickle.dumps(cold, protocol=5)
    results_cache.store(request, payload, recorder.dependencies(cold))
    assert results_cache.load(request) == payload
    sizes = track_picks(monkeypatch)
    monkeypatch.setattr(services, "restore_session_from_project", lambda *a, **k: pytest.fail("Cold restore"))
    monkeypatch.setattr(restore_process.multiprocessing, "get_context", lambda *a: pytest.fail("Spawn"))
    progress = []
    warm = restore_process.RestoreTask().restore(path, progress.append)
    assert sizes == []
    assert progress == ["Loading saved results"]
    assert project.laser_qc_record(warm.session.experiment) == project.laser_qc_record(cold.session.experiment)


def test_nonlaser_bytes_and_one_spot_restore_are_unchanged(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 3, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(project, "datetime", FrozenDateTime)
    session, preset = laser_session(tmp_path, laser=False)
    path = tmp_path / "plain.upbr"
    pipeline = services.ensure_shared_pipeline(session, preset)
    write_reduction_project(pipeline, path)
    before = path.read_bytes()
    assert "laser_qc" not in json.loads(before)
    # The previous writer had no laser-QC block at all.
    with monkeypatch.context() as legacy:
        legacy.setattr(project, "laser_qc_record", lambda experiment: None)
        write_reduction_project(pipeline, path)
    assert path.read_bytes() == before
    calls = []
    original = engine_io.auto_select_baseline_peak

    def tracked(experiment, data, *args, **kwargs):
        calls.append(data.spot_name.nunique())
        return original(experiment, data, *args, **kwargs)

    monkeypatch.setattr(engine_io, "auto_select_baseline_peak", tracked)
    restored = services.restore_session_from_project(path).session
    assert calls == [1]
    write_reduction_project(restored.pipeline, path)
    assert path.read_bytes() == before


def test_laser_cold_restore_keeps_reduction_numbers_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(synthetic_bundles, "CHANNELS", (
        "29Si", "44Ca", "202Hg", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U",
    ))
    session, preset = laser_session(tmp_path)
    session.primary_76_standard = session.primary_82_standard = "MADUCSB"
    session.session_fit_method = "mean"
    pipeline = services.ensure_shared_pipeline(session, preset)
    baseline = pipeline.run_baseline().copy()
    ratios = pipeline.run_ratios()
    finalized = pipeline.run_finalize()
    path = write_reduction_project(pipeline, tmp_path / "reduced.upbr")
    track_picks(monkeypatch)
    restored = services.restore_session_from_project(path).session.pipeline
    pd.testing.assert_frame_equal(restored.run_baseline(), baseline, check_exact=True)
    for key in ratios:
        pd.testing.assert_frame_equal(restored.run_ratios()[key], ratios[key], check_exact=True)
    for name in ("parallel_results", "selected_results"):
        pd.testing.assert_frame_equal(getattr(restored.run_finalize(), name), getattr(finalized, name), check_exact=True)


def test_compute_button_fills_old_project_without_touching_selections_or_cache(windows, app, tmp_path, monkeypatch):
    window = windows()
    path = tmp_path / "old-gui.upbr"
    window.save_project(path)
    document = project.read_reduction_project(path)
    document.pop("laser_qc")
    project.write_project_document(document, path)
    with monkeypatch.context() as blocked:
        track_picks(blocked)
        window = windows(project=path)
    assert "QC onsets are not computed" in window.transport_delay_label.text()
    before = selection_records(window.session.experiment)
    pipeline = window.session.pipeline
    cached = dict(pipeline.cache.as_mapping())
    assert not window.has_unsaved_changes()
    window.compute_laser_qc_button.click()
    assert window._active_worker is not None
    assert not window.compute_laser_qc_button.isEnabled()
    idle(app, window)
    assert window.compute_laser_qc_button.isEnabled()
    assert window.session.experiment.laser_detected_onset_time.notna().all()
    assert "QC onsets are not computed" not in window.transport_delay_label.text()
    assert selection_records(window.session.experiment) == before
    assert all(pipeline.cache.get(key) is value for key, value in cached.items())
    assert window.has_unsaved_changes()
    window.save_project(path)
    assert project.read_reduction_project(path)["laser_qc"]["computed"] is True
