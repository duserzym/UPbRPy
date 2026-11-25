"""Saved settings are either understood in full or refused before loading."""

import json
import pickle
from dataclasses import fields
from pathlib import Path

import pandas as pd
import pytest

from UPbRPy import pipeline as engine
from UPbRPy.depth import LinearDepthCalibration
from UPbRPy.export_table import ExportSettings
from UPbRPy.pipeline import (
    SETTINGS_SCHEMA_VERSION, AutomaticFlagSettings, FitSettings, NewerFileError,
    PipelineConfig, ReductionDecision, ReductionPipeline, UncertaintySettings,
    pipeline_config_from_dict,
)
from UPbRPy.project import (
    PROJECT_FORMAT_VERSION, build_reduction_project, read_reduction_project,
    validate_reduction_project, write_reduction_project,
)


@pytest.fixture
def pipeline(tmp_path):
    return ReductionPipeline(
        pd.DataFrame({"spot_names": []}), pd.DataFrame(), [], pd.DataFrame(),
        PipelineConfig(), source_directory=tmp_path,
    )


def plain_message(saved_version, current="0.1.4"):
    saved = f"UPbRPy Studio {saved_version}" if saved_version else "a newer UPbRPy Studio"
    return f"Saved by {saved}; you have {current}. Update Studio to open it."


@pytest.mark.parametrize("saved_version", [None, "9.0.0"])
@pytest.mark.parametrize("prefix", [
    "", "downhole", "session", "decision", "automatic_flags", "uncertainty",
    "export_settings", "depth_calibration",
])
def test_unknown_settings_are_refused_before_any_construction(monkeypatch, saved_version, prefix):
    monkeypatch.setattr(engine, "package_version", lambda: "0.1.4")
    values = {"unknown": 123} if not prefix else {prefix: {"unknown": 123}}
    # The invalid method would fail resolution if any construction ran first.
    values.setdefault("downhole", {})["method"] = "nonexistent.module.method"
    with pytest.raises(NewerFileError) as caught:
        pipeline_config_from_dict(values, saved_version=saved_version)
    error = caught.value
    assert str(error) == plain_message(saved_version)
    assert error.saved_version == saved_version
    assert error.current_version == "0.1.4"
    assert error.unknown_settings == ((f"{prefix}.unknown" if prefix else "unknown"),)
    assert error.saved_schema is None


def test_all_unknown_names_are_reported(monkeypatch):
    monkeypatch.setattr(engine, "package_version", lambda: "0.1.4")
    with pytest.raises(NewerFileError) as caught:
        pipeline_config_from_dict({"new_field": 1, "downhole": {"new_field": 2}})
    assert caught.value.unknown_settings == ("downhole.new_field", "new_field")


@pytest.mark.parametrize("saved_version", [None, "9.0.0"])
def test_newer_schema_and_format(pipeline, tmp_path, monkeypatch, saved_version):
    monkeypatch.setattr(engine, "package_version", lambda: "0.1.4")
    schema = SETTINGS_SCHEMA_VERSION + 1
    with pytest.raises(NewerFileError) as caught:
        pipeline_config_from_dict({}, saved_version=saved_version, saved_schema=schema)
    assert str(caught.value) == plain_message(saved_version)
    assert caught.value.saved_schema == schema
    assert caught.value.unknown_settings == ()
    document = build_reduction_project(pipeline, tmp_path / "project.upbr")
    document["saved_by"] = {"version": saved_version}
    document["pipeline"]["settings_schema"] = schema
    for newer_format in (False, True):
        if newer_format:
            document["format_version"] = PROJECT_FORMAT_VERSION + 1
            document["pipeline"]["settings_schema"] = SETTINGS_SCHEMA_VERSION
        with pytest.raises(NewerFileError) as caught:
            validate_reduction_project(document)
        assert str(caught.value) == plain_message(saved_version)
        assert caught.value.saved_schema == document["pipeline"]["settings_schema"]


def test_saved_metadata_round_trip_and_manifest(pipeline, tmp_path):
    path = write_reduction_project(pipeline, tmp_path / "project.upbr")
    document = read_reduction_project(path)
    assert document["saved_by"] == {
        "app": "UPbRPy Studio", "version": engine.package_version(),
        "commit": (engine.source_revision() or {}).get("commit"),
    }
    assert document["pipeline"]["settings_schema"] == SETTINGS_SCHEMA_VERSION
    assert document["pipeline"]["config"] == pipeline.config.to_dict()
    assert not {"saved_by", "settings_schema"} & document["pipeline"]["config"].keys()
    assert pipeline_config_from_dict(document["pipeline"]["config"]) == pipeline.config
    assert pipeline.manifest()["settings_schema"] == SETTINGS_SCHEMA_VERSION


def test_old_config_defaults_and_depth_discriminator():
    config = PipelineConfig().to_dict()
    del config["internal_standard_channel"]
    del config["reference_overrides"]
    restored = pipeline_config_from_dict(config)
    assert restored.internal_standard_channel is None
    assert restored.reference_overrides == {}
    calibration = LinearDepthCalibration(.01, 10)
    assert pipeline_config_from_dict({"depth_calibration": calibration.to_dict()}).depth_calibration == calibration
    # Open-ended model options remain accepted; they are not dataclass fields.
    assert pipeline_config_from_dict({"downhole": {"options": {"custom": 1}}}).downhole.options == {"custom": 1}


def test_old_project_restores_without_provenance(tmp_path):
    from GUI.services import ensure_shared_pipeline, load_session, restore_session_from_project
    from GUI.workflows import get_workflow_preset

    raw = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
    preset = get_workflow_preset("zircon")
    session = load_session(str(raw), primary_channel="29Si", preset=preset)
    pipeline = ensure_shared_pipeline(session, preset)
    path = tmp_path / "old.upbr"
    document = build_reduction_project(pipeline, path)
    document.pop("saved_by")
    document["pipeline"].pop("settings_schema")
    document["pipeline"]["config"].pop("internal_standard_channel")
    path.write_text(json.dumps(document))
    restored = restore_session_from_project(path)
    assert restored.session.internal_standard_channel is None
    assert restored.session.spot_names == session.spot_names


def test_refusal_survives_restore_process_serialization():
    from GUI.process_support import _remote_exception

    error = NewerFileError("9.0.0", "0.1.4", ("downhole.future",), 2)
    restored = _remote_exception(type(error).__module__, type(error).__name__,
                                str(error), "remote trace", pickle.dumps(error))
    assert isinstance(restored, NewerFileError)
    assert restored.__dict__ | {"__notes__": []} == error.__dict__ | {"__notes__": []}
    assert str(restored) == str(error)


def test_database_install_preserves_refusal_without_writing(pipeline, tmp_path):
    from UPbRPy.session_bundle import install_run_project

    document = build_reduction_project(pipeline, tmp_path / "project.upbr")
    document["pipeline"]["config"]["future"] = True
    with pytest.raises(NewerFileError):
        install_run_project(tmp_path, "run", json.dumps(document).encode(),
                            server="https://example.test", session_id="session",
                            session_key="session", export_sha256="hash")
    assert not list(tmp_path.glob("*.upbr"))


def test_bundle_origin_does_not_rewrite_newer_project(pipeline, tmp_path):
    from UPbRPy.session_bundle import _update_project_origin

    path = tmp_path / "future.upbr"
    document = build_reduction_project(pipeline, path)
    document["pipeline"]["config"]["future"] = True
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    _update_project_origin(path, server="https://example.test", session_id="session",
                           session_key="session", export_sha256="hash")
    assert path.read_bytes() == before


def test_settings_schema_field_snapshot():
    snapshot = {
        "PipelineConfig": "automatic_flags common_pb_calibration common_pb_options concentration_standards decision depth_calibration detector_calibration downhole export_settings internal_standard_channel material primary_68_standard primary_76_standard primary_82_standard primary_channel ratio_aggregation ratio_names reference_overrides secondary_standards selection_method selection_options session uncertainty workflow_key",
        "FitSettings": "method options outlier_sigma",
        "ReductionDecision": "age_transition_ma common_pb_method include_calibration_uncertainty",
        "AutomaticFlagSettings": "flag_session_extrapolation maximum_discordance_percent maximum_primary_signal_rsd_percent maximum_ratio_rsd_percent maximum_relative_2e_percent maximum_u_mean_intercept_difference_percent minimum_204pb_detection_sigma minimum_radiogenic_206_fraction",
        "UncertaintySettings": "lambda_232_1s lambda_235_1s lambda_238_1s long_term_reproducibility_mode long_term_reproducibility_relative_1s propagate_baseline propagate_decay_constants propagate_u238_u235 u238_u235 u238_u235_1s",
        "LinearDepthCalibration": "coefficient_covariance depth_per_pulse_um force_zero geometry_1s_um intercept_um pulse_range repetition_rate_1s_hz repetition_rate_hz residual_1s_um",
        "ExportSettings": "a_sio2 a_tio2 citation csv_full_precision discordance_definition discordance_threshold_percent excluded_columns excluded_sheets fluence_j_cm2 include_stage_coordinates laboratory laser_system operator optional_columns pressure_gpa repetition_rate_hz rutile_calibration spot_diameter_um table_title",
    }
    classes = (PipelineConfig, FitSettings, ReductionDecision, AutomaticFlagSettings,
               UncertaintySettings, LinearDepthCalibration, ExportSettings)
    actual = {cls.__name__: sorted(item.name for item in fields(cls)) for cls in classes}
    assert SETTINGS_SCHEMA_VERSION == 1 and actual == {k: v.split() for k, v in snapshot.items()}, (
        "Saved settings changed: bump SETTINGS_SCHEMA_VERSION and update this snapshot"
    )
