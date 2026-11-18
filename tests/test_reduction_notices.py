"""Reference gaps remain recoverable and picking does not control calibration."""

from dataclasses import replace
import json
import pickle

import numpy as np
import pandas as pd
import pytest

from UPbRPy.concentration import (
    calc_concentration, calc_concentration_calibration_factor,
    calc_concentration_depth_profile, channel_for_element,
    internal_normalization, resolve_element_concentration,
)
from UPbRPy.notices import ReductionError, ReductionNotice
from UPbRPy.pipeline import (
    PipelineConfig, PipelineStage, ReductionPipeline, config_invalidation_stage,
    pipeline_config_from_dict,
)
from UPbRPy.project import read_reduction_project, write_reduction_project


def example_pipeline(primary="29Si", archive=None):
    samples = ["S513", "Glass", "unknown"]
    experiment = pd.DataFrame({
        "spot_names": [name + "_1" for name in samples], "sample": samples,
        "baseline_start_times": [0., 10., 20.], "baseline_end_times": [1., 11., 21.],
        "plateau_start_times": [2., 12., 22.], "plateau_end_times": [5., 15., 25.],
        "signal_start_times": [2., 12., 22.], "signal_end_times": [5., 15., 25.],
    })
    data = pd.DataFrame({
        "Time": [2., 3., 12., 13., 22., 23.],
        "spot_name": [name + "_1" for name in samples for _ in range(2)],
        "sample": [name for name in samples for _ in range(2)],
        "29Si": [100., 120.] * 3, "31P": [4., 6., 10., 14., 2., 4.],
        "238U": [2., 4., 6., 8., 3., 5.],
    })
    if archive is None:
        archive = pd.DataFrame({"material": ["zircon", "glass"],
                                "29Si": [151443., 200000.], "31P": [np.nan, 12.],
                                "238U": [3., 7.]}, index=["S513", "Glass"])
    pipeline = ReductionPipeline(
        experiment, data, ["29Si", "31P", "238U"], archive,
        PipelineConfig(primary_channel=primary, concentration_standards=("S513", "Glass")),
        build_diagnostic_plots=False,
    )
    pipeline.cache.set(PipelineStage.BASELINE, data)
    return pipeline


@pytest.mark.parametrize("picking_channel", ["31P", "238U"])
def test_picking_channel_does_not_control_normalization_or_concentrations(picking_channel):
    expected = example_pipeline()
    actual = example_pipeline(picking_channel)
    pd.testing.assert_frame_equal(actual.run_normalization(), expected.run_normalization(), check_exact=True)
    pd.testing.assert_series_equal(actual.run_concentration()["calibration"],
                                   expected.run_concentration()["calibration"], check_exact=True)
    assert actual.resolve_internal_standard_channel() == "29Si"


def test_depth_concentration_uses_material_internal_channel(monkeypatch):
    import UPbRPy.isoplot as isoplot
    import UPbRPy.reduction as reduction

    monkeypatch.setattr(reduction, "calc_depth_profile", lambda *args: pd.DataFrame())
    monkeypatch.setattr(isoplot, "construct_final_depthpro_df", lambda concentrations, *args, **kwargs: concentrations)
    expected = example_pipeline()
    actual = example_pipeline("31P")
    for pipeline in (actual, expected):
        pipeline.cache.set(PipelineStage.SESSION, {"signal_corrected": pd.DataFrame(), "correction_results": {}})
    pd.testing.assert_frame_equal(actual.run_depth_profile(), expected.run_depth_profile(), check_exact=True)


@pytest.mark.parametrize("material,default", [("zircon", "29Si"), ("titanite", "29Si"),
                                               ("apatite", "44Ca"), ("rutile", "49Ti")])
def test_internal_channel_material_defaults_and_measured_mass(material, default):
    pipeline = example_pipeline()
    pipeline.config = replace(pipeline.config, material=material)
    pipeline.channels = (default, "31P")
    assert pipeline.resolve_internal_standard_channel() == default
    pipeline.config = replace(pipeline.config, internal_standard_channel="31P")
    assert pipeline.resolve_internal_standard_channel() == "31P"


def test_partial_reference_values_use_remaining_calibrants_and_keep_other_elements():
    pipeline = example_pipeline()
    data = pipeline.total_data.query("sample != 'unknown'")
    archive = pipeline.standard_archive
    complete = archive.copy()
    complete.loc["S513", "31P"] = 5.
    expected, _ = calc_concentration_calibration_factor(data, pipeline.channels, complete, build_plot=False)
    notices = []
    actual, _ = calc_concentration_calibration_factor(data, pipeline.channels, archive, build_plot=False, notices=notices)
    assert actual["238U"] == expected["238U"]
    assert actual["31P"] == 1.  # Glass spot mean 12 and reference 12.
    assert len(notices) == 1
    assert notices[0] == ReductionNotice("concentration", "missing_reference_value",
        "No P value for S513: P not calibrated with it.", "S513", "31P", ("choose_standard", "enter_value"))
    assert actual.attrs["reference_channels"]["31P"] == {"Glass": "31P"}


@pytest.mark.parametrize("channel,element", [("31P", "P"), ("202Hg", "Hg")])
def test_no_calibrant_has_element_omits_results_and_records_one_notice(channel, element):
    pipeline = example_pipeline()
    archive = pipeline.standard_archive.copy()
    data = pipeline.total_data.rename(columns={"31P": channel})
    archive = archive.rename(columns={"31P": channel})
    archive[channel] = np.nan
    standards = {"S513": "NIST610", "Glass": "NIST612"}
    archive = archive.rename(index=standards)
    data["sample"] = data["sample"].replace(standards)
    channels = ["29Si", channel, "238U"]
    if element == "Hg":
        data["204Hg"] = data[channel] / 3
        channels.append("204Hg")
    notices = []
    factors, _ = calc_concentration_calibration_factor(
        data.query("sample != 'unknown'"), channels, archive,
        build_plot=False, notices=notices,
    )
    assert channel not in factors.index
    assert channel not in factors.attrs["reference_channels"]
    result = calc_concentration(data.query("sample == 'unknown'"), factors)
    assert not any(column.startswith(element) for column in result.columns)
    normalized = internal_normalization(pipeline.experiment, channels, data, archive)
    depth = calc_concentration_depth_profile(normalized, factors)
    assert not any(column.startswith(element) for column in depth.columns)
    assert len(notices) == 1
    assert notices[0] == ReductionNotice(
        "concentration", "element_not_calibrated",
        f"No {element} value in NIST610, NIST612: {element} not calibrated.",
        "NIST610", channel, ("choose_standard", "enter_value"), level="info",
    )
    assert np.isfinite(result["U"]).all()


def test_missing_internal_reference_skips_standard_and_preserves_unknown_default():
    pipeline = example_pipeline()
    archive = pipeline.standard_archive.copy()
    archive.loc["S513", "29Si"] = np.nan
    notices = []
    actual = internal_normalization(pipeline.experiment, pipeline.channels, pipeline.total_data,
                                    archive, notices=notices)
    assert set(actual["sample"]) == {"Glass", "unknown"}
    assert np.allclose(actual.query("sample == 'unknown'")["29Si"], 151443.)
    assert len(notices) == 1
    assert notices[0].message == "No Si value for S513: its analyses are not normalized."


@pytest.mark.parametrize("other_material", ["apatite", "rutile", "titanite"])
def test_normalization_material_filter_uses_private_overrides(other_material):
    pipeline = example_pipeline()
    pipeline.base_standard_archive.loc["S513", "material"] = other_material
    pipeline.config = replace(pipeline.config, reference_overrides={
        "S513": {"29Si": 10.}, "Glass": {"29Si": 123.},
    })
    normalized = pipeline.run_normalization()
    assert np.allclose(normalized.query("sample == 'S513'")["29Si"], 151443.)
    assert np.allclose(normalized.query("sample == 'unknown'")["29Si"], 151443.)
    assert np.allclose(normalized.query("sample == 'Glass'")["29Si"], 123.)
    assert not pipeline.notices


def test_overrides_are_private_and_invalidate_only_concentration_branch():
    pipeline = example_pipeline()
    shared = pipeline.base_standard_archive.copy(deep=True)
    baseline = pipeline.cache.get(PipelineStage.BASELINE)
    pipeline.run_concentration()
    fits = object()
    for stage in (PipelineStage.RATIOS, PipelineStage.DOWNHOLE, PipelineStage.SESSION,
                  PipelineStage.DEPTH_PROFILE, PipelineStage.FINALIZE):
        pipeline.cache.set(stage, fits)
    config = replace(pipeline.config, reference_overrides={"S513": {"31P": 5.}})
    pipeline.update_config(config, invalidate_from=config_invalidation_stage(pipeline.config, config))
    assert pipeline.cache.get(PipelineStage.BASELINE) is baseline
    assert pipeline.cache.get(PipelineStage.SESSION) is fits
    assert not pipeline.cache.has(PipelineStage.NORMALIZATION)
    assert not pipeline.cache.has(PipelineStage.DEPTH_PROFILE)
    assert not pipeline.cache.has(PipelineStage.FINALIZE)
    pipeline.run_concentration()
    assert not pipeline.notices
    assert pipeline.standard_archive.loc["S513", "31P"] == 5.
    pd.testing.assert_frame_equal(pipeline.base_standard_archive, shared)
    assert np.isnan(example_pipeline().standard_archive.loc["S513", "31P"])


@pytest.mark.parametrize("change", [
    {"reference_overrides": {"S513": {"31P": 5.}}},
    {"internal_standard_channel": "29Si"},
])
def test_reference_config_invalidation_chooses_earliest_stage_and_honors_caller(change):
    pipeline = example_pipeline()
    previous = pipeline.config
    config = replace(previous, **change)
    assert config_invalidation_stage(previous, config) == PipelineStage.NORMALIZATION
    assert config_invalidation_stage(previous, replace(config, detector_calibration={"238U": 1.1})) == PipelineStage.BASELINE
    assert config_invalidation_stage(previous, replace(config, concentration_standards=("Glass",))) == PipelineStage.NORMALIZATION
    assert config_invalidation_stage(previous, replace(previous, concentration_standards=("Glass",))) == PipelineStage.CONCENTRATION
    assert config_invalidation_stage(previous, replace(previous, primary_channel="31P")) == PipelineStage.NORMALIZATION
    pipeline.run_normalization()
    pipeline.update_config(config, invalidate_from=PipelineStage.BASELINE)
    assert not pipeline.cache.has(PipelineStage.BASELINE)
    assert not pipeline.cache.has(PipelineStage.NORMALIZATION)


def test_notices_recompute_by_stage_without_duplicates():
    pipeline = example_pipeline()
    pipeline.run_concentration()
    other = ReductionNotice("session", "test", "Session note.")
    pipeline.notices.append(other)
    pipeline.cache.invalidate(PipelineStage.NORMALIZATION)
    pipeline.run_normalization()
    assert len(pipeline.notices) == 2
    pipeline.run_concentration()
    assert len(pipeline.notices) == 2
    assert other in pipeline.notices


def test_project_round_trip_and_old_config_defaults(tmp_path):
    pipeline = example_pipeline()
    pipeline.config = replace(pipeline.config, internal_standard_channel="29Si",
                              reference_overrides={"S513": {"31P": 123.4}})
    pipeline.source_directory = tmp_path
    project = write_reduction_project(pipeline, tmp_path / "values.upbr")
    document = read_reduction_project(project)
    assert pipeline_config_from_dict(document["pipeline"]["config"]) == pipeline.config
    for key in ("internal_standard_channel", "reference_overrides"):
        document["pipeline"]["config"].pop(key)
    project.write_text(json.dumps(document))
    config = pipeline_config_from_dict(read_reduction_project(project)["pipeline"]["config"])
    assert config.internal_standard_channel is None
    assert config.reference_overrides == {}


def test_reduction_errors_are_actionable_and_survive_worker_transport():
    with pytest.raises(ReductionError) as caught:
        channel_for_element(["31P"], "29Si")
    error = pickle.loads(pickle.dumps(caught.value))
    assert str(error).startswith("No measured Si")
    assert error.stage == "normalization"
    assert "Choose" in error.next_step
    with pytest.raises(ReductionError, match="Unknown concentration standard Missing"):
        resolve_element_concentration(example_pipeline().standard_archive, "Missing", "31P")
    with pytest.raises(ReductionError, match="No Pb/U primary standard is configured"):
        example_pipeline().require_samples((), "Pb/U primary standard")


def test_empty_selections_have_a_next_step():
    pipeline = example_pipeline()
    pipeline.experiment["plateau_end_times"] = pipeline.experiment["plateau_start_times"]
    with pytest.raises(ReductionError, match="No sweeps") as caught:
        pipeline.run_normalization()
    assert "Review" in caught.value.next_step
