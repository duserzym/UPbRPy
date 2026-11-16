"""Tests for the shared notebook/GUI reduction orchestration layer."""

from __future__ import annotations

from dataclasses import replace
import math

import pandas as pd

from UPbRPy.pipeline import (
    PIPELINE_STAGE_BY_ID,
    FitSettings,
    PipelineCache,
    PipelineConfig,
    PipelineStage,
    ReductionPipeline,
    pipeline_config_from_dict,
    pipeline_descendants,
)
from UPbRPy.reproducibility import build_reproducibility_notebook


def test_stage_graph_captures_branching_dependencies():
    descendants = set(pipeline_descendants(PipelineStage.BASELINE))

    assert PipelineStage.NORMALIZATION in descendants
    assert PipelineStage.CONCENTRATION in descendants
    assert PipelineStage.RATIOS in descendants
    assert PipelineStage.FINALIZE in descendants
    assert PipelineStage.IMPORT not in descendants
    assert PIPELINE_STAGE_BY_ID[PipelineStage.DEPTH_PROFILE].optional


def test_pipeline_cache_invalidates_only_downstream_results():
    cache = PipelineCache()
    for stage in (
        PipelineStage.IMPORT,
        PipelineStage.SELECTION,
        PipelineStage.BASELINE,
        PipelineStage.NORMALIZATION,
        PipelineStage.CONCENTRATION,
        PipelineStage.RATIOS,
        PipelineStage.DOWNHOLE,
    ):
        cache.set(stage, stage.value)

    removed = cache.invalidate(PipelineStage.NORMALIZATION)

    assert set(removed) == {
        PipelineStage.NORMALIZATION,
        PipelineStage.CONCENTRATION,
    }
    assert cache.has(PipelineStage.BASELINE)
    assert cache.has(PipelineStage.RATIOS)
    assert cache.has(PipelineStage.DOWNHOLE)


def test_pipeline_config_serializes_models_and_decisions():
    config = PipelineConfig(
        workflow_key="apatite",
        material="apatite",
        primary_channel="44Ca",
        ratio_aggregation="logratio",
        concentration_standards=("NIST610", "NIST612"),
        primary_68_standard="MAD",
        primary_76_standard="NIST610",
        primary_82_standard="NIST610",
        common_pb_calibration="tera_wasserburg",
        downhole=FitSettings(
            method="mean",
            options={"degree": 1},
            outlier_sigma=5,
        ),
    )

    serialized = config.to_dict()

    assert serialized["common_pb_calibration"] == "tera_wasserburg"
    assert serialized["ratio_aggregation"] == "logratio"
    assert serialized["primary_68_standard"] == "MAD"
    assert serialized["downhole"]["method"] == "mean"
    assert serialized["downhole"]["outlier_sigma"] == 5
    assert serialized["decision"]["common_pb_method"] == "none"


def test_pipeline_config_round_trip_preserves_nested_settings():
    config = PipelineConfig(
        workflow_key="apatite",
        common_pb_calibration="tera_wasserburg",
        concentration_standards=("NIST610", "NIST612"),
        primary_68_standard="MADUCSB",
        primary_76_standard="NIST610",
        primary_82_standard="NIST610",
        downhole=FitSettings(
            method="running_mean",
            options={"running_mean_window": 7},
            outlier_sigma=4.5,
        ),
    )

    restored = pipeline_config_from_dict(config.to_dict())

    assert restored == config


def test_pipeline_config_restores_importable_custom_model():
    config = PipelineConfig(
        downhole=FitSettings(method=math.exp),
    )

    restored = pipeline_config_from_dict(config.to_dict())

    assert restored.downhole.method is math.exp


def test_pipeline_manifest_is_lightweight_and_records_completed_stages(tmp_path):
    input_path = tmp_path / "spot.csv"
    input_path.write_text("Time,206Pb\n0,1\n")
    experiment = pd.DataFrame(
        {
            "spot_names": ["spot-1"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [1.0],
            "plateau_start_times": [2.0],
            "plateau_end_times": [3.0],
        }
    )
    total_data = pd.DataFrame(
        {
            "spot_name": ["spot-1"],
            "sample": ["unknown"],
            "Time": [0.0],
            "206Pb": [1.0],
        }
    )
    pipeline = ReductionPipeline(
        experiment,
        total_data,
        ["206Pb"],
        pd.DataFrame(),
        PipelineConfig(),
        source_directory=tmp_path,
        input_files=(input_path,),
    )

    manifest = pipeline.manifest(hash_inputs=True, metadata={"operator": "test"})

    assert manifest["format_version"] == 2
    assert manifest["completed_stages"] == ["import", "selection"]
    assert manifest["input_files"][0]["sha256"]
    assert manifest["metadata"]["operator"] == "test"
    assert "total_data" not in manifest


def test_updating_selection_invalidates_calculated_stages():
    experiment = pd.DataFrame({"spot_names": ["spot-1"]})
    pipeline = ReductionPipeline(
        experiment,
        pd.DataFrame({"sample": ["unknown"]}),
        [],
        pd.DataFrame(),
        PipelineConfig(),
    )
    pipeline.cache.set(PipelineStage.BASELINE, "baseline")
    pipeline.cache.set(PipelineStage.RATIOS, "ratios")
    pipeline.cache.set(PipelineStage.NORMALIZATION, "normalization")

    pipeline.update_experiment(experiment.copy())

    assert pipeline.cache.has(PipelineStage.IMPORT)
    assert pipeline.cache.has(PipelineStage.SELECTION)
    assert not pipeline.cache.has(PipelineStage.BASELINE)
    assert not pipeline.cache.has(PipelineStage.RATIOS)
    assert not pipeline.cache.has(PipelineStage.NORMALIZATION)


def test_updating_session_settings_can_preserve_upstream_results():
    pipeline = ReductionPipeline(
        pd.DataFrame({"spot_names": ["spot-1"]}),
        pd.DataFrame({"sample": ["unknown"]}),
        [],
        pd.DataFrame(),
        PipelineConfig(),
    )
    pipeline.cache.set(PipelineStage.RATIOS, "ratios")
    pipeline.cache.set(PipelineStage.DOWNHOLE, "downhole")
    pipeline.cache.set(PipelineStage.SESSION, "session")
    updated = replace(
        pipeline.config,
        session=FitSettings(method="linear"),
    )

    pipeline.update_config(updated, invalidate_from=PipelineStage.SESSION)

    assert pipeline.cache.get(PipelineStage.RATIOS) == "ratios"
    assert pipeline.cache.get(PipelineStage.DOWNHOLE) == "downhole"
    assert not pipeline.cache.has(PipelineStage.SESSION)


def test_reproducibility_notebook_embeds_reviewed_selection():
    experiment = pd.DataFrame(
        {
            "spot_names": ["sample_1"],
            "baseline_start_times": [1.25],
            "baseline_end_times": [2.5],
            "plateau_start_times": [3.75],
            "plateau_end_times": [8.0],
            "signal_start_times": [3.0],
            "signal_end_times": [9.0],
        }
    )
    total_data = pd.DataFrame(
        {
            "spot_name": ["sample_1"],
            "sample": ["sample"],
            "Time": [1.25],
        }
    )
    pipeline = ReductionPipeline(
        experiment,
        total_data,
        [],
        pd.DataFrame(),
        PipelineConfig(),
    )

    notebook = build_reproducibility_notebook(
        pipeline,
        include_concentrations=False,
        include_depth_concentrations=False,
    )
    source = "".join(
        "".join(cell["source"])
        for cell in notebook["cells"]
    )

    assert notebook["nbformat"] == 4
    assert '"baseline_start_times":1.25' in source
    assert "pipeline.run_depth_profile()" in source
    assert "standard_archive_legacy" not in source
    assert "load_standard_measurements(reference_library_path)" in source
    assert "archive = standard_archive" in source
    assert "include_concentrations=False" in source
    assert "include_depth_concentrations=False" in source
