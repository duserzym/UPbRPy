"""Synthetic laser export metadata and coordinate opt-in contracts."""

from dataclasses import replace
import json
from hashlib import sha256
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from UPbRPy.export_table import (
    ExportSettings, build_export_table, build_footnotes, update_export_settings,
    write_export_csv, write_export_xlsx, write_export_markdown,
)
from UPbRPy.laser_exports import (
    LASER_COLUMNS, STAGE_COLUMNS, LaserExportData, laser_export_data,
    laser_export_prefill, export_manifest,
)
from UPbRPy.laser_tie import LaserTie, LaserTieRow
from UPbRPy.pipeline import PipelineStage
from UPbRPy.project import write_reduction_project, read_reduction_project
from UPbRPy.reproducibility import (
    export_reduction_bundle, build_reproducibility_notebook, write_export_project,
)
from UPbRPy.session_manifest import (
    SessionManifest, SessionAcquisition, AcquisitionLaser, SessionLaserLog,
)
from test_export_table import synthetic_pipeline


STAGE = (12345.678, -2345.125, 345.5)


def laser_pipeline(source="lase", *, varying=False):
    pipeline = synthetic_pipeline()
    labels = pipeline.experiment.spot_names.tolist()
    rows = {label: LaserTieRow(status="linked", fluence_j_cm2=2.5 + (i % 2 if varying else 0),
        rep_rate_hz=10. + (10 * (i % 2) if varying else 0),
        spot_um=20. + (20 * (i % 2) if varying else 0),
        spot_shape="Circle", shot_count=100, output_pct=25., stage_um=STAGE)
        for i, label in enumerate(labels)}
    tie = LaserTie(sha256="a" * 64, file_name="synthetic.lase", system_type="Chromium",
        laser_instrument="Synthetic laser", spot_key="position", threshold_s=30.,
        has_laser_times=False, clock_offset_s=None, clock_offset_spread_s=None,
        pairs=0, counts={"linked": len(rows)}, warnings=(), rows=rows)
    if source == "lase":
        pipeline.laser_tie = tie
    else:
        log = SessionLaserLog(file_name=tie.file_name, sha256=tie.sha256,
            system_type=tie.system_type, laser_instrument=tie.laser_instrument,
            spot_key="position", serial_number="SYNTHETIC-DO-NOT-PREFILL",
            software_version="SYNTHETIC-VERSION")
        acquisitions = tuple(SessionAcquisition(label=label, laser=AcquisitionLaser(
            log_sha256=tie.sha256, **{key: getattr(row, key) for key in (
                "fluence_j_cm2", "rep_rate_hz", "spot_um", "spot_shape", "shot_count", "output_pct", "stage_um")}))
            for label, row in rows.items())
        pipeline.session_manifest = SessionManifest(Path("synthetic/session_manifest.toml"),
            format_version=2, acquisitions=acquisitions, laser_logs=(log,))
    pipeline.cache.set(PipelineStage.SPOT_MEANS, pipeline.run_finalize().parallel_results.copy())
    pipeline.cache.set(PipelineStage.DEPTH_PROFILE, pipeline.run_finalize().parallel_results.copy())
    return pipeline


def data_for(pipeline):
    return laser_export_data(pipeline.experiment, manifest=getattr(pipeline, "session_manifest", None),
                             laser_tie=getattr(pipeline, "laser_tie", None))


def assert_no_coordinates(paths):
    """Inspect every text file and every XLSX member, including numeric cells."""
    for path in paths:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                blobs = [archive.read(name) for name in archive.namelist()]
        else:
            blobs = [path.read_bytes()]
        for blob in blobs:
            for value in STAGE:
                assert str(value).encode() not in blob, path
            for key in (b'stage_um', b'laser_stage', b'Stage x', b'Stage y', b'Stage z'):
                assert key not in blob, (path, key)


@pytest.mark.parametrize("source", ["lase", "manifest"])
def test_default_bundle_has_no_coordinates_in_any_file(tmp_path, source):
    pipeline = laser_pipeline(source, varying=True)
    bundle = export_reduction_bundle(pipeline, tmp_path / "export")
    assert not bundle.warnings
    assert set(bundle.files) == {p for p in bundle.directory.rglob("*") if p.is_file()}
    assert_no_coordinates(bundle.files)
    assert STAGE == next(iter(data_for(pipeline).spots.values()))["stage_um"]


@pytest.mark.parametrize("source", ["lase", "manifest"])
def test_stage_opt_in_table_bundle_and_toggle(tmp_path, source):
    pipeline = laser_pipeline(source)
    original = build_export_table(pipeline)
    enabled = replace(original.settings, include_stage_coordinates=True)
    table = update_export_settings(original, enabled)
    for key, value in zip(STAGE_COLUMNS, STAGE):
        assert table.rows[key].eq(value).all()
        assert key in {c.key for c in table.visible_columns()}
    assert not set(STAGE_COLUMNS).intersection(original.rows)
    off = update_export_settings(table, replace(enabled, include_stage_coordinates=False))
    assert not set(STAGE_COLUMNS).intersection(off.rows)
    assert "stage_um" not in off.context["manifest"]["laser"]
    bundle = export_reduction_bundle(pipeline, tmp_path / "on", export_settings=enabled, table=table)
    audit = pd.read_csv(bundle.directory / "spot_mean_results.csv")
    assert tuple(audit.loc[0, list(STAGE_COLUMNS)]) == STAGE
    manifest = json.loads((bundle.directory / "reduction_manifest.json").read_text())
    assert list(manifest["laser"]["stage_um"].values())[0] == list(STAGE)
    # Reusing an opted-in preview must not leak into an opted-out bundle.
    bundle = export_reduction_bundle(pipeline, tmp_path / "off", export_settings=off.settings, table=table)
    assert_no_coordinates(bundle.files)


@pytest.mark.parametrize("source", ["lase", "manifest"])
def test_prefill_single_range_and_analyst_wins(source):
    pipeline = laser_pipeline(source)
    data = data_for(pipeline)
    values, varies = laser_export_prefill(data)
    assert values == dict(laser_system="Synthetic laser", spot_diameter_um="20",
                          repetition_rate_hz="10", fluence_j_cm2="2.5")
    assert not any(varies.values())
    table = build_export_table(pipeline)
    assert table.settings.spot_diameter_um == "20"
    assert not any("laser_" + key in table.rows for key, _, _ in LASER_COLUMNS)
    existing = dict(laser_system="Analyst system", spot_diameter_um="analyst spot",
                    repetition_rate_hz="analyst rep", fluence_j_cm2="analyst fluence")
    assert laser_export_prefill(data, existing=existing)[0] == existing
    assert build_export_table(pipeline, ExportSettings(**existing)).settings.laser_system == "Analyst system"
    varying = laser_pipeline(source, varying=True)
    table = build_export_table(varying)
    assert (table.settings.spot_diameter_um, table.settings.repetition_rate_hz,
            table.settings.fluence_j_cm2) == ("20–40", "10–20", "2.5–3.5")
    for key, _, _ in LASER_COLUMNS:
        assert "laser_" + key in table.rows
        assert len(table.rows["laser_" + key].unique()) == 2
    notes = " ".join(note.text for note in build_footnotes(table, varying))
    assert all(text in notes for text in ("20–40 µm", "10–20 Hz", "2.5–3.5 J/cm²"))
    # A subset with a single setting must not inherit a session-wide range.
    subset = varying.experiment.spot_names.iloc[::2]
    assert laser_export_prefill(data_for(varying), subset)[0]["spot_diameter_um"] == "20"


def test_unlinked_missing_renamed_labels_and_instrument_fallback():
    pipeline = laser_pipeline()
    tie = pipeline.laser_tie
    raw = pipeline.experiment.spot_names.iloc[0]
    pipeline.experiment.loc[0, "spot_names"] = "renamed_1"
    pipeline.experiment.attrs["spot_renames"] = {"renamed_1": raw}
    tie = replace(tie, laser_instrument="", rows={**tie.rows,
        "unlinked_1": LaserTieRow(status="doubtful", spot_um=999., stage_um=STAGE)})
    data = laser_export_data(pipeline.experiment, laser_tie=tie)
    assert data.spots["renamed_1"]["spot_um"] == 20.
    assert "unlinked_1" not in data.spots
    assert laser_export_prefill(data)[0]["laser_system"] == "Chromium"
    missing = LaserExportData("lase", data.logs, {"a": {"spot_um": None}, "b": {"spot_um": 20.}})
    assert laser_export_prefill(missing)[0]["spot_diameter_um"] == "20"


@pytest.mark.parametrize("source", ["lase", "manifest"])
def test_laser_manifest_settings_counts_qc_and_no_identifiers(source):
    pipeline = laser_pipeline(source, varying=True)
    pipeline.experiment.attrs["laser"] = dict(transport_delay_s=1.25, qc_threshold_s=2.5)
    pipeline.experiment["laser_onset_flag"] = [True, False, False, False, False, False, False]
    pipeline.experiment["selection_method"] = "laser_guided"
    pipeline.config = replace(pipeline.config, selection_options={"signal_duration_limit_source": "laser"})
    manifest = export_manifest(pipeline, ExportSettings())
    laser = manifest["laser"]
    assert laser["source"] == source
    assert laser["transport_delay_s"] == 1.25 and laser["qc_threshold_s"] == 2.5
    assert laser["flagged_count"] == 1 and laser["laser_guided"] is True
    assert laser["signal_duration_limit_source"] == "laser"
    assert len(laser["ablation_settings"]) == 2
    assert sum(row["acquisitions"] for row in laser["ablation_settings"]) == 7
    log = laser["logs"][0]
    assert log["sha256"] == "a" * 64 and log["file_name"] == "synthetic.lase"
    assert log["clock_offset_s"] is None and log["pairs"] == 0
    assert "serial_number" not in log and "software_version" not in log
    assert "stage" not in json.dumps(laser).lower()


def test_settings_old_dict_and_bool_validation():
    assert ExportSettings.from_dict({}).include_stage_coordinates is False
    assert "include_stage_coordinates" not in ExportSettings().to_dict()
    from UPbRPy.pipeline import PipelineConfig, pipeline_config_from_dict
    default_config = PipelineConfig(export_settings=ExportSettings()).to_dict()
    assert "include_stage_coordinates" not in default_config["export_settings"]
    enabled = ExportSettings(include_stage_coordinates=True)
    assert enabled != ExportSettings()
    assert replace(enabled, operator="Synthetic").include_stage_coordinates is True
    restored = pipeline_config_from_dict(PipelineConfig(export_settings=enabled).to_dict())
    assert restored.export_settings.include_stage_coordinates is True
    for invalid in (0, 1, None, "false"):
        with pytest.raises(ValueError, match="include_stage_coordinates"):
            ExportSettings(include_stage_coordinates=invalid)


@pytest.mark.parametrize("include_stage", [False, True])
def test_coordinate_option_round_trips_through_saved_project(tmp_path, include_stage):
    from UPbRPy.pipeline import pipeline_config_from_dict
    pipeline = synthetic_pipeline()
    pipeline.source_directory = tmp_path
    settings = ExportSettings(include_stage_coordinates=include_stage)
    pipeline.config = replace(pipeline.config, export_settings=settings)
    path = write_reduction_project(pipeline, tmp_path / "synthetic.upbr")
    saved = read_reduction_project(path)["pipeline"]
    record = saved["config"]["export_settings"]
    if include_stage:
        assert record["include_stage_coordinates"] is True
    else:
        assert "include_stage_coordinates" not in record
    restored = pipeline_config_from_dict(saved["config"], saved_schema=saved["settings_schema"])
    assert restored.export_settings == settings
    assert restored.export_settings.include_stage_coordinates is include_stage


def test_no_laser_prefill_tables_and_manifest_unchanged(tmp_path, monkeypatch):
    pipeline = synthetic_pipeline()
    values, varies = laser_export_prefill(LaserExportData())
    assert values == dict.fromkeys(("laser_system", "spot_diameter_um", "repetition_rate_hz", "fluence_j_cm2"), "")
    assert not any(varies.values())
    original = pipeline.manifest()
    monkeypatch.setattr(pipeline, "manifest", lambda **kwargs: original)
    assert json.dumps(export_manifest(pipeline, ExportSettings()), indent=2) == json.dumps(original, indent=2)
    table = build_export_table(pipeline)
    baseline = build_export_table(pipeline, ExportSettings.from_dict({}))
    pd.testing.assert_frame_equal(table.rows, baseline.rows, check_exact=True)
    assert table.columns == baseline.columns
    for writer, suffix in ((write_export_csv, ".csv"), (write_export_markdown, ".md")):
        a, b = tmp_path / ("a" + suffix), tmp_path / ("b" + suffix)
        writer(table, a)
        writer(baseline, b)
        assert a.read_bytes() == b.read_bytes()
        if suffix == ".csv":
            # Captured before L3 export changes from this synthetic pipeline.
            assert sha256(a.read_bytes()).hexdigest() == "22c96c6c77eb41d6a2301c65b7de17ee96d7a61677f0b2990262f84f6076a1aa"


@pytest.mark.parametrize("include_stage", [False, True])
def test_notebook_cells_compile_and_preserve_ablation_data(tmp_path, include_stage):
    pipeline = laser_pipeline(varying=True)
    settings = ExportSettings(include_stage_coordinates=include_stage)
    notebook = build_reproducibility_notebook(pipeline, export_settings=settings)
    namespace = {}
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            code = "".join(cell["source"])
            compile(code, "<laser notebook>", "exec")
            if "laser_export_data = LaserExportData(" in code:
                exec(code, namespace)
    assert len(namespace["laser_export_data"].spots) == 7
    assert namespace["laser_tie"].rows
    from UPbRPy.results import build_publication_tables, export_publication_tables
    namespace.update(pipeline=pipeline, config=pipeline.config,
        finalized=pipeline.run_finalize(), depth_profile=pipeline.run_depth_profile(),
        source_directory=tmp_path / "synthetic", reference_library_path=None,
        build_publication_tables=build_publication_tables, export_publication_tables=export_publication_tables)
    publication = next(cell for cell in notebook["cells"]
                       if "manifest = pipeline.manifest" in "".join(cell["source"]))
    exec("".join(publication["source"]), namespace)
    assert namespace["publication_workbook"].is_file()
    if not include_stage:
        assert_no_coordinates([namespace["publication_workbook"],
            *namespace["publication_workbook"].with_suffix("").glob("*.csv")])
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(notebook))
    if not include_stage:
        assert_no_coordinates([path])


def test_exported_project_filters_tie_without_changing_saved_work(tmp_path):
    pipeline = laser_pipeline()
    pipeline.source_directory = tmp_path
    saved = write_reduction_project(pipeline, tmp_path / "working.upbr")
    assert read_reduction_project(saved)["laser_log"]["rows"]["B_0"]["stage_um"] == list(STAGE)
    exported = write_export_project(pipeline, tmp_path / "export.upbr")
    assert_no_coordinates([exported])
    restored_tie = LaserTie.from_dict(read_reduction_project(exported)["laser_log"])
    assert restored_tie.rows["B_0"].stage_um is None
    enabled = write_export_project(pipeline, tmp_path / "on.upbr",
        export_settings=ExportSettings(include_stage_coordinates=True))
    assert read_reduction_project(enabled)["laser_log"]["rows"]["B_0"]["stage_um"] == list(STAGE)
    assert next(iter(pipeline.laser_tie.rows.values())).stage_um == STAGE


def test_dialog_prefill_editable_and_stage_checkbox_offscreen(tmp_path):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from GUI.export_dialog import ExportResultsDialog
    from GUI.theme import build_application
    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    pipeline = laser_pipeline(varying=True)
    dialog = ExportResultsDialog(build_export_table(pipeline), folder=tmp_path)
    try:
        dialog.resize(1180, 720)
        app.processEvents()
        assert dialog.text_fields["spot_diameter_um"].text() == "20–40"
        assert not dialog.text_fields["laser_system"].isReadOnly()
        assert not dialog.stage_coordinates_check.isChecked()
        dialog.stage_coordinates_check.setChecked(True)
        dialog.text_fields["spot_diameter_um"].setText("Analyst value")
        dialog.refresh_preview()
        assert dialog.export_settings().spot_diameter_um == "Analyst value"
        assert set(STAGE_COLUMNS).issubset(dialog.table.rows)
        assert dialog.minimumSizeHint().width() <= 1280
        assert dialog.minimumSizeHint().height() <= 800
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_manifest_file_discovery_and_time_provenance(tmp_path):
    pipeline = laser_pipeline("manifest")
    del pipeline.session_manifest
    pipeline.source_directory = tmp_path
    (tmp_path / "session_manifest.toml").write_text('''
format = "upbdb-session-bundle"
format_version = 2
[[laser_log]]
sha256 = "''' + "a" * 64 + '''"
file_name = "synthetic.lase"
system_type = "Chromium"
clock_offset_s = 3600.0
clock_offset_spread_s = 0.5
offset_pairs = 12
has_laser_times = true
[[acquisition]]
label = "B_0"
laser_log_sha256 = "''' + "a" * 64 + '''"
laser_spot_um = 20.0
laser_rep_rate_hz = 10.0
laser_fluence_j_cm2 = 2.5
laser_stage_um = [12345.678, -2345.125, 345.5]
''')
    table = build_export_table(pipeline)
    assert table.settings.laser_system == "Chromium"
    assert table.settings.spot_diameter_um == "20"
    laser = export_manifest(pipeline, table.settings)["laser"]
    assert laser["logs"][0]["clock_offset_s"] == 3600.
    assert laser["logs"][0]["clock_offset_spread_s"] == .5
    assert laser["logs"][0]["pairs"] == 12


def test_defensive_filtering_of_cached_and_nested_coordinate_fields(tmp_path):
    from UPbRPy.results import build_publication_tables, export_publication_tables
    from UPbRPy.reproducibility import selection_export_frame
    from UPbRPy.laser_exports import without_stage_coordinates
    pipeline = laser_pipeline()
    experiment = pipeline.experiment
    experiment["selection_stage_um"] = [STAGE] * len(experiment)
    assert "selection_stage_um" not in selection_export_frame(experiment)
    finalized = pipeline.run_finalize()
    finalized.parallel_results["laser_stage_um"] = [STAGE] * len(experiment)
    finalized.selected_results["Stage x (µm)"] = STAGE[0]
    tables = build_publication_tables(finalized)
    assert "laser_stage_um" not in tables["Parallel audit"]
    assert "Stage x (µm)" not in tables["Results"]
    # Protect callers that pass already prepared frames straight to the writer.
    tables["Parallel audit"]["laser_stage_um"] = [STAGE] * len(experiment)
    tables["Parallel audit"]["metadata"] = [{"laser": {"stage_um": STAGE}}] * len(experiment)
    target = export_publication_tables(tables, tmp_path / "direct.xlsx")
    assert_no_coordinates([target, *target.with_suffix("").glob("*.csv")])
    assert without_stage_coordinates({"completed_stages": ["finalize"], "stage_um": STAGE}) == {"completed_stages": ["finalize"]}


def test_database_archive_filters_project_copy_and_keeps_working_project(tmp_path):
    from UPbRPy.integration.upbdb import zip_reduction_bundle
    pipeline = laser_pipeline()
    pipeline.source_directory = tmp_path / "synthetic"
    bundle = export_reduction_bundle(pipeline, tmp_path / "reduction")
    project = write_export_project(pipeline, bundle.directory / "synthetic.upbr")
    assert_no_coordinates([*bundle.files, project])
    archive = zip_reduction_bundle(bundle.directory, tmp_path / "payload.zip")
    assert_no_coordinates([archive])
    # Packaging and export perform no HTTP calls or fitting.
    assert next(iter(pipeline.laser_tie.rows.values())).stage_um == STAGE


def test_old_saved_nondefault_settings_keep_legacy_manifest_bytes(monkeypatch):
    pipeline = synthetic_pipeline()
    settings = ExportSettings.from_dict({"operator": "Synthetic analyst", "optional_columns": ["204Pb"]})
    pipeline.config = replace(pipeline.config, export_settings=settings)
    legacy = pipeline.manifest(metadata={"export_settings": settings.to_dict()})
    monkeypatch.setattr(pipeline, "manifest", lambda **kwargs: legacy)
    actual = export_manifest(pipeline, settings)
    assert json.dumps(actual, indent=2) == json.dumps(legacy, indent=2)
    assert "include_stage_coordinates" not in actual["metadata"]["export_settings"]


def test_log_hash_matching_is_case_insensitive():
    pipeline = laser_pipeline("manifest")
    manifest = pipeline.session_manifest
    manifest = replace(manifest, laser_logs=(replace(manifest.laser_logs[0], sha256="A" * 64),))
    data = laser_export_data(pipeline.experiment, manifest=manifest)
    assert laser_export_prefill(data)[0]["laser_system"] == "Synthetic laser"


def test_legacy_cached_export_and_notebook_audit_report_ranges(tmp_path):
    from UPbRPy.results import build_publication_tables
    pipeline = laser_pipeline(varying=True)
    finalized = pipeline.run_finalize()
    finalized = replace(finalized, parallel_results=finalized.parallel_results.drop(columns=["preferred_date"]))
    pipeline.cache.set(PipelineStage.FINALIZE, finalized)
    bundle = export_reduction_bundle(pipeline, tmp_path / "legacy", formats=("csv",))
    note = (bundle.directory / "session_results_table_footnotes.txt").read_text(encoding="utf-8-sig")
    assert all(value in note for value in ("20–40 µm", "10–20 Hz", "2.5–3.5 J/cm²"))
    audit = build_publication_tables(finalized, laser_data=data_for(pipeline))
    methods = audit["Methods"].set_index("setting")["value"]
    assert methods["spot_diameter_um"] == "20–40"
    assert audit["Table notes"].note.str.contains("20–40 µm").any()


def test_position_values_quoted_in_log_warnings_do_not_leak(tmp_path):
    pipeline = laser_pipeline()
    pipeline.source_directory = tmp_path / "synthetic"
    warning = "Unparseable Position.Start: 12345.678, -2345.125, 345.5"
    pipeline.laser_tie = replace(pipeline.laser_tie, warnings=(warning,))
    bundle = export_reduction_bundle(pipeline, tmp_path / "export", formats=("csv",))
    project = write_export_project(pipeline, bundle.directory / "synthetic.upbr")
    assert_no_coordinates([*bundle.files, project])
    assert pipeline.laser_tie.warnings == (warning,)


def test_dialog_retains_unchanged_legacy_settings_provenance():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from GUI.export_dialog import ExportResultsDialog
    from GUI.theme import build_application
    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    settings = ExportSettings.from_dict({"operator": "Synthetic analyst"})
    pipeline = synthetic_pipeline()
    dialog = ExportResultsDialog(build_export_table(pipeline, settings))
    try:
        assert dialog.export_settings() == settings
        manifest = export_manifest(pipeline, dialog.export_settings(), metadata={"export_settings": settings.to_dict()})
        assert "include_stage_coordinates" not in manifest["metadata"]["export_settings"]
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
