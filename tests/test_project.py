"""Tests for lightweight, replayable ``.upbr`` project documents."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import GUI.services as gui_services
from GUI.state import ReductionSession
from GUI.workflows import get_workflow_preset
from UPbRPy.pipeline import (
    FitSettings,
    PipelineConfig,
    PipelineStage,
    ReductionPipeline,
)
from UPbRPy.project import (
    PROJECT_FORMAT_VERSION,
    build_reduction_project,
    read_reduction_project,
    resolve_project_path,
    restore_project_selections,
    write_reduction_project,
)


def example_pipeline(
    source_directory: Path,
    reference_library: Path,
) -> ReductionPipeline:
    """Return a minimal imported pipeline with reviewed signal windows."""
    experiment = pd.DataFrame(
        {
            "spot_names": ["MAD_1", "MA1_2"],
            "baseline_start_times": [0.0, 10.0],
            "baseline_end_times": [1.0, 11.0],
            "plateau_start_times": [3.0, 13.0],
            "plateau_end_times": [8.0, 18.0],
            "signal_start_times": [2.5, 12.5],
            "signal_end_times": [9.0, 19.0],
            "selection_method": ["threshold", "threshold"],
            "selection_channel": ["44Ca", "44Ca"],
            "plateau_filter_method": ["none", "excel_2sigma"],
            "plateau_filter_channel": ["44Ca", "44Ca"],
        }
    )
    total_data = pd.DataFrame(
        {
            "spot_name": ["MAD_1", "MA1_2"],
            "sample": ["MAD", "MA1"],
            "Time": [0.0, 10.0],
            "44Ca": [1.0, 2.0],
        }
    )
    config = PipelineConfig(
        workflow_key="apatite",
        material="apatite",
        primary_channel="44Ca",
        selection_method="threshold",
        selection_options={"baseline_sweep_n": 10, "threshold": 0.1},
        primary_68_standard="MAD",
        primary_76_standard="NIST610",
        primary_82_standard="NIST610",
        common_pb_calibration="tera_wasserburg",
        downhole=FitSettings(method="mean", outlier_sigma=5.0),
    )
    return ReductionPipeline(
        experiment,
        total_data,
        ["44Ca"],
        pd.DataFrame(),
        config,
        source_directory=source_directory,
        reference_library_path=reference_library,
    )


def test_project_is_lightweight_and_records_replay_inputs(tmp_path):
    data_directory = tmp_path / "raw"
    data_directory.mkdir()
    (data_directory / "MAD_1.csv").write_text(
        "Time,44Ca\n0,1\n",
        encoding="utf-8",
    )
    library = tmp_path / "library.toml"
    library.write_text("format_version = 1\n", encoding="utf-8")
    pipeline = example_pipeline(data_directory, library)
    pipeline.cache.set(PipelineStage.BASELINE, pd.DataFrame({"value": [1]}))

    project_path = write_reduction_project(
        pipeline,
        tmp_path / "apatite_reduction",
        resume_stage=PipelineStage.BASELINE,
        workspace={
            "selected_spot": "MA1_2",
            "visible_channels": ["44Ca"],
        },
    )
    document = read_reduction_project(project_path)

    assert project_path.suffix == ".upbr"
    assert document["format_version"] == PROJECT_FORMAT_VERSION
    assert document["pipeline"]["resume_stage"] == "baseline"
    assert document["pipeline"]["completed_stages"] == [
        "import",
        "selection",
        "baseline",
    ]
    assert document["pipeline"]["config"]["primary_channel"] == "44Ca"
    assert document["selections"][1]["signal_end_times"] == 19.0
    assert (
        document["selections"][1]["plateau_filter_method"]
        == "excel_2sigma"
    )
    assert document["workspace"]["selected_spot"] == "MA1_2"
    serialized = project_path.read_text(encoding="utf-8")
    assert "total_data" not in serialized
    assert "MAD_1.csv" not in serialized
    assert project_path.stat().st_size < 20_000


def test_project_paths_prefer_portable_relative_location(tmp_path):
    original_root = tmp_path / "original"
    original_root.mkdir()
    data_directory = original_root / "data"
    data_directory.mkdir()
    library = original_root / "library.toml"
    library.write_text("format_version = 1\n", encoding="utf-8")
    project_path = original_root / "session.upbr"
    document = build_reduction_project(
        example_pipeline(data_directory, library),
        project_path,
    )
    project_path.write_text(json.dumps(document), encoding="utf-8")

    moved_root = tmp_path / "moved"
    original_root.rename(moved_root)
    moved_project = moved_root / "session.upbr"
    loaded = read_reduction_project(moved_project)

    assert resolve_project_path(
        moved_project,
        loaded["paths"]["source_directory"],
        label="raw data",
    ) == moved_root / "data"
    assert resolve_project_path(
        moved_project,
        loaded["paths"]["reference_library"],
        label="reference library",
    ) == moved_root / "library.toml"


def test_restoring_selections_requires_unchanged_spot_list():
    imported = pd.DataFrame(
        {
            "spot_names": ["MAD_1", "MA1_2"],
            "signal_start_times": [0.0, 0.0],
        }
    )
    records = [
        {"spot_names": "MAD_1", "signal_start_times": 2.5},
        {"spot_names": "MA1_2", "signal_start_times": 12.5},
    ]

    restored = restore_project_selections(imported, records)

    assert restored["signal_start_times"].tolist() == [2.5, 12.5]
    with pytest.raises(ValueError, match="spot list has changed"):
        restore_project_selections(
            imported.iloc[:1].copy(),
            records,
        )


def test_project_rejects_newer_unknown_format(tmp_path):
    project_path = tmp_path / "future.upbr"
    project_path.write_text(
        json.dumps(
            {
                "format": "UPbRPy reduction project",
                "format_version": PROJECT_FORMAT_VERSION + 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Update Studio to open it"):
        read_reduction_project(project_path)


def test_gui_restore_keeps_saved_config_and_reviewed_windows(
    tmp_path,
    monkeypatch,
):
    """Opening must not immediately replace the restored core configuration."""
    data_directory = tmp_path / "raw"
    data_directory.mkdir()
    (data_directory / "MA1_2.csv").write_text(
        "Time 44Ca\n0 2\n",
        encoding="utf-8",
    )
    library = tmp_path / "library.toml"
    library.write_text("format_version = 1\n", encoding="utf-8")
    preset = get_workflow_preset("apatite")
    experiment = pd.DataFrame(
        {
            "spot_names": ["MA1_2"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [1.0],
            "plateau_start_times": [3.0],
            "plateau_end_times": [8.0],
            "signal_start_times": [2.5],
            "signal_end_times": [9.0],
        }
    )
    total_data = pd.DataFrame(
        {
            "spot_name": ["MA1_2"],
            "sample": ["MA1"],
            "Time": [0.0],
            "44Ca": [2.0],
        }
    )
    session_for_config = ReductionSession(
        directory=str(data_directory),
        experiment=experiment,
        total_data=total_data,
        channels=["44Ca"],
        primary_channel="44Ca",
        workflow_key="apatite",
        workflow_title="Apatite",
        ratio_aggregation="logratio",
        concentration_standards=preset.concentration_standards,
        primary_68_standard="MAD",
        primary_76_standard="NIST610",
        primary_82_standard="NIST610",
        import_settings={
            "primary_channel": "44Ca",
            **preset.read_kwargs,
        },
    )
    config = gui_services.pipeline_config_from_session(
        session_for_config,
        preset,
    )
    pipeline = ReductionPipeline(
        experiment,
        total_data,
        ["44Ca"],
        pd.DataFrame(),
        config,
        source_directory=data_directory,
        reference_library_path=library,
    )
    project_path = write_reduction_project(
        pipeline,
        tmp_path / "session.upbr",
        workspace={"selected_spot": "MA1_2"},
    )

    freshly_imported = experiment.copy()
    freshly_imported["signal_start_times"] = 4.0
    freshly_imported["signal_end_times"] = 7.0

    def fake_load_session(*_args, **_kwargs):
        return ReductionSession(
            directory=str(data_directory),
            experiment=freshly_imported.copy(),
            total_data=total_data.copy(),
            channels=["44Ca"],
            primary_channel="44Ca",
            workflow_key="apatite",
            workflow_title="Apatite",
            selected_spot="MA1_2",
            visible_channels=["44Ca"],
        )

    monkeypatch.setattr(gui_services, "load_session", fake_load_session)
    monkeypatch.setattr(
        gui_services,
        "load_standard_archive_from_path",
        lambda _path: gui_services.current_standard_archive(),
    )

    restored = gui_services.restore_session_from_project(project_path)
    shared = gui_services.ensure_shared_pipeline(
        restored.session,
        restored.preset,
    )

    assert shared is restored.session.pipeline
    assert shared.config == config
    assert restored.session.ratio_aggregation == "logratio"
    assert restored.session.experiment.loc[0, "signal_start_times"] == 2.5
    assert restored.session.experiment.loc[0, "signal_end_times"] == 9.0
    assert shared.cache.stages() == (
        PipelineStage.IMPORT,
        PipelineStage.SELECTION,
    )
