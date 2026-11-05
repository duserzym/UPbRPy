"""Independent detector fits and import/report wiring on the Qtegra slice."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from UPbRPy import qtegra
from UPbRPy.calibration import (
    ChannelCrossCalibration,
    CrossCalibration,
    apply_cross_calibration,
    fit_cross_calibration,
)
from UPbRPy.io import read_experiment_data
from UPbRPy.pipeline import PipelineConfig, ReductionPipeline, pipeline_config_from_dict
from UPbRPy.reproducibility import build_reproducibility_notebook, export_reduction_bundle
from UPbRPy.results import FinalizedReduction, ReductionDecision, build_publication_tables


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
RICH = FIXTURE / "Zircon_04Nov26_slice_Rich.csv"
UPBR = FIXTURE / "Zircon_04Nov26_slice_UPbR.csv"
FOLDER = FIXTURE / "zircon_04Nov26"
WINDOW = (1e5, 2.4e6)


@pytest.fixture(scope="module")
def rich():
    return qtegra.read_rich_csv(RICH)


def independent_points(session, channel, window=WINDOW, primary_standard=None):
    """Select points directly from source arrays without calibration helpers."""
    points = []
    for acquisition in session.acquisitions:
        if (primary_standard is not None
                and acquisition.label.split("_")[0].casefold() != primary_standard.casefold()):
            continue
        index = acquisition.channel_names.index(channel)
        analog = acquisition.analog[:, index]
        counter = acquisition.counter[:, index]
        keep = (np.isfinite(counter) & (counter != -33)
                & (counter >= window[0]) & (counter <= window[1])
                & np.isfinite(analog) & (analog > 0))
        if keep.any():
            points.append((acquisition, index, keep, analog[keep], counter[keep]))
    return points


def independent_constant(points):
    analog = np.concatenate([p[3] for p in points])
    counter = np.concatenate([p[4] for p in points])
    design = np.stack((analog, np.ones_like(analog)), axis=1)
    # Normal equations are independent of the implementation's lstsq solver.
    coefficients = np.linalg.solve(design.T @ design, design.T @ counter)
    residuals = counter - design @ coefficients
    covariance = np.linalg.inv(design.T @ design) * (residuals @ residuals) / (len(analog) - 2)
    return coefficients, np.sqrt(np.diag(covariance)), len(analog)


@pytest.mark.parametrize("channel", ["29Si", "238U", "206Pb"])
@pytest.mark.parametrize("primary_standard,window", [
    (None, WINDOW), ("nIsT612", (0, 2.4e6)),
])
def test_constant_matches_independent_coefficients_and_errors(rich, channel, primary_standard, window):
    # NIST612 Pb and U never reach the default 1e5 lower bound. A wider
    # window permits independent OLS checks on those primary-only channels.
    points = independent_points(rich, channel, window, primary_standard)
    coefficients, errors, n_points = independent_constant(points)
    calibration = fit_cross_calibration(
        rich, channel, window=window,
        on="all" if primary_standard is None else "primary_standard",
        primary_standard=primary_standard,
    )
    fit = calibration.channels[channel]
    np.testing.assert_allclose([fit.factor, fit.intercept], coefficients, rtol=1e-10)
    np.testing.assert_allclose([fit.factor_se, fit.intercept_se], errors, rtol=1e-10)
    assert fit.n_points == n_points
    assert fit.n_acquisitions == len(points)
    assert fit.acquisition_labels == [p[0].label for p in points]
    if primary_standard:
        assert fit.acquisition_labels == ["NIST612_896", "NIST612_897", "NIST612_898"]
    np.testing.assert_array_equal(fit.factor_at([-1, 100, 1e9]), np.full(3, fit.factor))


def test_primary_standard_default_window_omits_or_rejects_sparse_channels(rich):
    calibration = fit_cross_calibration(rich, on="primary_standard", primary_standard="NIST612")
    assert list(calibration.channels) == ["29Si"]
    points = independent_points(rich, "29Si", primary_standard="NIST612")
    coefficients, errors, _ = independent_constant(points)
    fit = calibration.channels["29Si"]
    np.testing.assert_allclose([fit.factor, fit.intercept], coefficients, rtol=1e-10)
    np.testing.assert_allclose([fit.factor_se, fit.intercept_se], errors, rtol=1e-10)
    for channel in ("238U", "206Pb"):
        assert any(channel in warning and "fewer than 3" in warning for warning in calibration.warnings)
        with pytest.raises(ValueError, match=f"{channel}.*fewer than 3"):
            fit_cross_calibration(
                rich, channel, on="primary_standard", primary_standard="NIST612",
            )


def test_drift_matches_independent_slopes_kernel_errors_and_channel_times(rich):
    session = deepcopy(rich)
    session.acquisitions.reverse()  # The origin must be the earliest start.
    channel = "238U"
    bandwidth = 67.0
    fit = fit_cross_calibration(session, channel, mode="drift", smoothing_s=bandwidth).channels[channel]
    points = independent_points(session, channel)
    coefficients, _, _ = independent_constant(points)
    origin = min(a.start for a in session.acquisitions)
    observations = []
    for acquisition, index, keep, analog, counter in points:
        if len(analog) < 5:
            continue
        slope = np.sum(analog * (counter - coefficients[1])) / np.sum(analog**2)
        residuals = counter - (analog * slope + coefficients[1])
        error = np.sqrt(np.sum(residuals**2) / (len(analog) - 1) / np.sum(analog**2))
        mid = np.mean(acquisition.time[keep, index]) + (acquisition.start - origin).total_seconds()
        observations.append((mid, slope, error, acquisition.label))
    observations.sort()
    times = np.array([p[0] for p in observations])
    raw = np.array([p[1] for p in observations])
    errors = np.array([p[2] for p in observations])
    kernel = np.exp(-((times[:, None] - times[None, :]) ** 2) / (2 * bandwidth**2))
    weights = kernel / errors**2
    expected = (weights @ raw) / weights.sum(axis=1)
    expected_se = np.sqrt(np.sum(weights**2 * errors**2, axis=1)) / weights.sum(axis=1)
    for array in (fit.times_s, fit.factors, fit.factors_se, fit.raw_factors):
        assert array.shape == (len(observations),)
    np.testing.assert_allclose(fit.times_s, times, rtol=1e-14)
    np.testing.assert_allclose(fit.raw_factors, raw, rtol=1e-10)
    np.testing.assert_allclose(fit.factors, expected, rtol=1e-10)
    np.testing.assert_allclose(fit.factors_se, expected_se, rtol=1e-10)
    assert fit.acquisition_labels == [p[3] for p in observations]
    assert fit.n_acquisitions == len(observations)
    np.testing.assert_allclose(fit.factor, np.average(raw, weights=1/errors**2), rtol=1e-10)
    np.testing.assert_allclose(fit.factor_se, np.sqrt(1 / np.sum(1/errors**2)), rtol=1e-10)
    query = [times[0] - 100, (times[0] + times[1]) / 2, times[-1] + 100]
    np.testing.assert_allclose(fit.factor_at(query), [expected[0], expected[:2].mean(), expected[-1]])


def test_one_acquisition_drift_equals_ols_with_fixed_intercept(rich):
    session = replace(rich, acquisitions=[rich.acquisitions[3]])
    constant = fit_cross_calibration(session, "238U").channels["238U"]
    drift = fit_cross_calibration(session, "238U", mode="drift").channels["238U"]
    np.testing.assert_allclose(drift.raw_factors, [constant.factor], rtol=1e-12)
    np.testing.assert_allclose(drift.factors, [constant.factor], rtol=1e-12)
    assert drift.intercept == constant.intercept
    assert drift.acquisition_labels == [session.labels[0]]


def synthetic_session(slope=60000.0, intercept=0.0):
    acquisitions = []
    for position in range(3):
        analog = np.arange(2.0, 14.0)[:, None]
        counter = analog * slope + intercept
        counter[-1] = -33
        y = np.where(counter == -33, analog * 70000.0, counter)
        acquisitions.append(qtegra.Acquisition(
            label=f"STD_{position}", sequence_index=position + 1,
            channels=(qtegra.Channel("29Si"),), analog=analog, counter=counter,
            y=y, time=np.arange(len(analog), dtype=float) + 0.25,
            start=datetime(2026, 11, 4) + timedelta(seconds=position * 150),
        ))
    return qtegra.QtegraSession(acquisitions, "rich", frozenset({"analog_counter"}), ())


@pytest.mark.parametrize("intercept", [0, 17000])
def test_synthetically_constant_slopes_remain_constant_and_finite(intercept):
    session = synthetic_session(intercept=intercept)
    fit = fit_cross_calibration(session, mode="drift").channels["29Si"]
    np.testing.assert_allclose(fit.raw_factors, 60000, rtol=1e-12)
    np.testing.assert_allclose(fit.factors, 60000, rtol=1e-12)
    assert np.isfinite(fit.factor_se)
    assert np.isfinite(fit.factors_se).all()


def test_drift_uses_start_offsets_and_shared_times_when_starts_are_missing():
    session = synthetic_session()
    for i, acquisition in enumerate(session.acquisitions):
        acquisition.start = None
        acquisition.start_offset_s = 100 + 150 * i
    fit = fit_cross_calibration(session, mode="drift").channels["29Si"]
    np.testing.assert_allclose(fit.times_s, np.arange(3) * 150 + 5.25)


def test_drift_skips_short_acquisitions_and_falls_back_to_constant():
    session = synthetic_session()
    for acquisition in session.acquisitions:
        acquisition.counter[4:-1] = -33
    constant = fit_cross_calibration(session).channels["29Si"]
    calibration = fit_cross_calibration(session, mode="drift")
    fit = calibration.channels["29Si"]
    assert fit.factor == constant.factor
    assert fit.factor_se == constant.factor_se
    assert fit.times_s is None
    assert any("29Si" in warning and "constant fit" in warning for warning in calibration.warnings)
    session.acquisitions[1] = synthetic_session().acquisitions[1]
    fit = fit_cross_calibration(session, mode="drift").channels["29Si"]
    assert fit.acquisition_labels == ["STD_1"]
    assert fit.n_acquisitions == 1


def test_point_mask_excludes_invalid_counter_and_nonpositive_analog():
    session = synthetic_session()
    acquisition = session.acquisitions[0]
    acquisition.counter[:4, 0] = [np.nan, np.inf, -33, WINDOW[0] - 1]
    acquisition.analog[4:8, 0] = [np.nan, np.inf, 0, -1]
    points = independent_points(session, "29Si")
    coefficients, errors, n = independent_constant(points)
    fit = fit_cross_calibration(session, "29Si").channels["29Si"]
    np.testing.assert_allclose([fit.factor, fit.intercept], coefficients, atol=1e-8, rtol=1e-10)
    assert fit.n_points == n


@pytest.mark.parametrize("mode", ["constant", "drift"])
def test_apply_only_replaces_calibrated_saturated_cells_and_deep_copies(rich, mode):
    session = deepcopy(rich)
    # Even an exported Y that differs from its valid counter must be retained.
    session.acquisitions[0].y[0, 0] += 123
    before = deepcopy(session)
    calibration = fit_cross_calibration(session, ["29Si", "238U", "206Pb"], mode=mode)
    calibrated = apply_cross_calibration(session, calibration)
    origin = min(a.start for a in session.acquisitions)
    changed = 0
    for original, snapshot, applied in zip(session.acquisitions, before.acquisitions, calibrated.acquisitions):
        expected = original.y.copy()
        for name, fit in calibration.channels.items():
            index = original.channel_index(name)
            mask = (original.counter[:, index] == -33) & np.isfinite(original.analog[:, index])
            times = (original.start - origin).total_seconds() + original.time[:, index]
            expected[mask, index] = original.analog[mask, index] * fit.factor_at(times[mask])
            changed += np.count_nonzero(mask)
        np.testing.assert_array_equal(applied.y, expected)
        np.testing.assert_array_equal(original.y, snapshot.y)
        for key in ("analog", "counter", "time"):
            np.testing.assert_array_equal(getattr(applied, key), getattr(original, key))
            assert not np.shares_memory(getattr(applied, key), getattr(original, key))
        assert not np.shares_memory(applied.y, original.y)
        assert applied.sequence_fields == original.sequence_fields
    assert changed > 0
    assert session.warnings == before.warnings
    assert calibrated.warnings[:-1] == before.warnings
    assert "29Si" in calibrated.warnings[-1] and mode in calibrated.warnings[-1]
    assert vars(calibrated).keys() == vars(session).keys()


def test_apply_saturated_finite_analog_including_zero_and_negative():
    session = synthetic_session()
    acquisition = session.acquisitions[0]
    acquisition.counter[-4:, 0] = -33
    acquisition.analog[-4:, 0] = [np.nan, np.inf, 0, -2]
    fit = fit_cross_calibration(session)
    applied = apply_cross_calibration(session, fit).acquisitions[0]
    np.testing.assert_array_equal(applied.y[-4:-2], acquisition.y[-4:-2])
    np.testing.assert_allclose(applied.y[-2:, 0], [0, -2 * fit.channels["29Si"].factor])


def test_qtegra_mode_is_an_unchanged_deep_copy_and_factors_prefer_exported_xcal(rich):
    recovered = qtegra.recover_xcal(rich)
    calibration = fit_cross_calibration(rich, mode="qtegra")
    assert set(calibration.channels) == set(recovered)
    copied = apply_cross_calibration(rich, calibration)
    for original, applied in zip(rich.acquisitions, copied.acquisitions):
        np.testing.assert_array_equal(applied.y, original.y)
        assert not np.shares_memory(applied.y, original.y)
    assert copied.warnings == rich.warnings
    fit = fit_cross_calibration(rich, ["29Si", "206Pb"])
    assert fit.channels["29Si"].qtegra_factor == recovered["29Si"]
    assert fit.channels["206Pb"].qtegra_factor is None
    merged = qtegra.open_qtegra(FIXTURE)
    # Deliberately distinguish the exported value from the recoverable value.
    for acquisition in merged.acquisitions:
        acquisition.channels = tuple(replace(c, xcal=12345) if c.name == "29Si" else c
                                     for c in acquisition.channels)
    assert fit_cross_calibration(merged, "29Si").channels["29Si"].qtegra_factor == 12345


@pytest.mark.parametrize("source,format_name", [(UPBR, "upbr"), (FOLDER, "spot_folder")])
@pytest.mark.parametrize("mode", ["constant", "drift"])
def test_degraded_formats_reject_non_qtegra_calibration(source, format_name, mode):
    session = qtegra.open_qtegra(source)
    message = f"{format_name}.*analog_counter"
    with pytest.raises(ValueError, match=message):
        fit_cross_calibration(session, mode=mode)
    with pytest.raises(ValueError, match=message):
        read_experiment_data(source, detector_calibration=mode)
    copied = apply_cross_calibration(session, fit_cross_calibration(session, mode="qtegra"))
    np.testing.assert_array_equal(copied.acquisitions[0].y, session.acquisitions[0].y)


@pytest.mark.parametrize("options", ["constant", {"mode": "constant", "channels": ["29Si", "238U"]}])
def test_read_experiment_calibrates_only_saturated_cells_and_stores_record(rich, options):
    original_experiment, channels, original_data = read_experiment_data(rich)
    experiment, actual_channels, data = read_experiment_data(rich, detector_calibration=options)
    record = experiment.attrs["detector_calibration"]
    assert record["mode"] == "constant"
    assert channels == actual_channels
    if isinstance(options, dict):
        assert set(record["channels"]) == set(options["channels"])
    expected = original_data.copy()
    for acquisition in rich.acquisitions:
        rows = expected.index[expected.spot_name == acquisition.label]
        for channel, fit in record["channels"].items():
            index = acquisition.channel_index(channel)
            keep = (acquisition.counter[:, index] == -33) & np.isfinite(acquisition.analog[:, index])
            expected.loc[rows[keep], channel] = acquisition.analog[keep, index] * fit["factor"]
    pd.testing.assert_frame_equal(data, expected, check_exact=True)
    assert np.any(data[channels].to_numpy() != original_data[channels].to_numpy())
    assert original_experiment.attrs["detector_calibration"] == {"mode": "qtegra"}
    assert json.loads(json.dumps(record, allow_nan=False)) == record


@pytest.mark.parametrize("option", [None, "qtegra", {}, {"mode": "qtegra"}])
def test_default_read_path_is_identical_to_explicit_qtegra(rich, option):
    expected, channels, data = read_experiment_data(rich)
    actual, actual_channels, actual_data = read_experiment_data(rich, detector_calibration=option)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    pd.testing.assert_frame_equal(actual_data, data, check_exact=True)
    assert actual.attrs == expected.attrs
    assert actual_channels == channels


@pytest.mark.parametrize("mode", ["qtegra", "constant", "drift"])
def test_to_dict_is_strict_json_and_contains_settings_and_diagnostics(rich, mode):
    calibration = fit_cross_calibration(rich, mode=mode, smoothing_s=500)
    record = calibration.to_dict()
    assert json.loads(json.dumps(record, allow_nan=False)) == record
    assert record["on"] == "all"
    assert record["window"] == list(WINDOW)
    assert record["primary_standard"] is None
    assert record["smoothing_s"] == 500
    assert record["warnings"] == calibration.warnings
    for name, channel in record["channels"].items():
        fit = calibration.channels[name]
        assert channel["factor"] == fit.factor
        assert channel["n_points"] == fit.n_points
        expected = None if fit.qtegra_factor is None else fit.factor / fit.qtegra_factor - 1
        assert channel["relative_difference_from_qtegra"] == expected
        if mode == "drift":
            assert len(channel["times_s"]) == fit.n_acquisitions
            assert channel["acquisition_labels"] == fit.acquisition_labels


@pytest.mark.parametrize("kwargs,match", [
    ({"mode": "unknown"}, "mode"),
    ({"on": "unknown"}, "on"),
    ({"on": "primary_standard"}, "primary_standard"),
    ({"window": (2, 1)}, "window"),
    ({"smoothing_s": 0}, "smoothing_s"),
    ({"smoothing_s": np.nan}, "smoothing_s"),
    ({"channel": "missing"}, "missing"),
    ({"channel": "204Hg"}, "fewer than 3"),
])
def test_invalid_fit_settings_raise(rich, kwargs, match):
    with pytest.raises(ValueError, match=match):
        fit_cross_calibration(rich, **kwargs)


def test_config_round_trip_manifest_and_experiment_attrs(rich):
    options = {"mode": "drift", "channels": ["29Si"], "window": [1e5, 2.4e6],
               "on": "all", "smoothing_s": 90, "primary_standard": "NIST612"}
    config = PipelineConfig(detector_calibration=options)
    restored = pipeline_config_from_dict(json.loads(json.dumps(config.to_dict())))
    assert restored == config
    assert pipeline_config_from_dict({}).detector_calibration == {}
    experiment, channels, data = read_experiment_data(rich, detector_calibration=options)
    # The manifest must report applied data even if configured choices differ.
    pipeline = ReductionPipeline(experiment, data, channels, pd.DataFrame(), PipelineConfig())
    record = experiment.attrs["detector_calibration"]
    assert pipeline.experiment.attrs["detector_calibration"] == record
    assert pipeline.experiment.attrs["qtegra"] == experiment.attrs["qtegra"]
    assert pipeline.manifest()["detector_calibration"] == record
    pipeline.update_experiment(experiment.copy())
    assert pipeline.manifest()["detector_calibration"] == record
    del pipeline.experiment.attrs["detector_calibration"]
    assert pipeline.manifest()["detector_calibration"] == {"mode": "qtegra"}


def test_publication_methods_and_export_use_applied_factors(rich, tmp_path, monkeypatch):
    experiment, channels, data = read_experiment_data(rich, detector_calibration={
        "mode": "constant", "channels": ["29Si"],
    })
    pipeline = ReductionPipeline(experiment, data, channels, pd.DataFrame(), PipelineConfig(),
                                 source_directory=RICH)
    finalized = FinalizedReduction(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                                   pd.DataFrame(), ReductionDecision())
    monkeypatch.setattr(pipeline, "run_session", lambda: {"plateau_corrected": pd.DataFrame()})
    monkeypatch.setattr(pipeline, "run_spot_means", pd.DataFrame)
    monkeypatch.setattr(pipeline, "run_baseline", pd.DataFrame)
    monkeypatch.setattr("UPbRPy.pipeline.finalize_reduction_results", lambda *a, **kw: finalized)
    pipeline.run_finalize()
    tables = build_publication_tables(finalized)
    methods = tables["Methods"].set_index("setting")["value"]
    assert methods["detector_calibration.mode"] == "constant"
    assert methods["detector_calibration.29Si.factor"] == experiment.attrs["detector_calibration"]["channels"]["29Si"]["factor"]
    export_reduction_bundle(pipeline, tmp_path, include_depth_profile=False)
    exported = pd.read_csv(tmp_path / "UPbRPy_publication_results/methods.csv").set_index("setting")["value"]
    assert exported["detector_calibration.mode"] == "constant"
    assert float(exported["detector_calibration.29Si.factor"]) == methods["detector_calibration.29Si.factor"]
    notebook = build_reproducibility_notebook(pipeline)
    source = "".join("".join(cell["source"]) for cell in notebook["cells"])
    assert "read_options['detector_calibration'] = config.detector_calibration" in source
    assert "'detector_calibration': manifest['detector_calibration']" in source


def test_gui_project_round_trip_preserves_detector_choice_and_old_configs(tmp_path):
    from GUI.services import load_session, pipeline_config_from_session, restore_session_from_project
    from GUI.workflows import get_workflow_preset
    from UPbRPy.project import write_reduction_project

    options = {"mode": "constant", "channels": ["29Si"]}
    preset = get_workflow_preset("zircon")
    session = load_session(str(RICH), preset=preset, import_settings={"detector_calibration": options})
    experiment, channels, data = session.experiment, session.channels, session.total_data
    config = pipeline_config_from_session(session, preset)
    pipeline = ReductionPipeline(experiment, data, channels, pd.DataFrame(), config,
                                 source_directory=RICH)
    session.pipeline = pipeline
    assert pipeline_config_from_session(session, preset).detector_calibration == options
    path = write_reduction_project(pipeline, tmp_path / "calibrated.upbr")
    restored = restore_session_from_project(path).session
    assert restored.pipeline.config.detector_calibration == options
    assert restored.experiment.attrs["detector_calibration"] == experiment.attrs["detector_calibration"]
    pd.testing.assert_frame_equal(restored.total_data[channels], data[channels], check_exact=True)
    document = json.loads(path.read_text())
    del document["pipeline"]["config"]["detector_calibration"]
    path.write_text(json.dumps(document))
    restored = restore_session_from_project(path).session
    assert restored.pipeline.config.detector_calibration == {}
    assert restored.experiment.attrs["detector_calibration"] == {"mode": "qtegra"}


def test_public_dataclasses_are_exported():
    from UPbRPy import calibration

    assert {"CrossCalibration", "ChannelCrossCalibration", "fit_cross_calibration",
            "apply_cross_calibration"}.issubset(calibration.__all__)
    fit = ChannelCrossCalibration("29Si", None, 2, 0.1, 0, 0.2, 3, 1)
    assert CrossCalibration("constant", "all", WINDOW, {"29Si": fit}).to_dict()["channels"]["29Si"]["factor"] == 2
