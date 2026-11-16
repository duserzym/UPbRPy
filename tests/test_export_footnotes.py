"""Publication provenance, visibility-dependent numbering and heading text."""

from dataclasses import replace
from datetime import date
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from UPbRPy import io, qtegra
from UPbRPy.export_table import (
    ExportColumn, ExportSettings, Footnote, _git_commit, build_export_table,
    build_footnotes, column_heading, footnotes_markdown, footnotes_text,
)
from UPbRPy.pipeline import FitSettings, PipelineConfig, PipelineStage, ReductionPipeline
from UPbRPy.project import (
    read_reduction_project, resolve_project_path, restore_project_selections,
    write_reduction_project,
)
from UPbRPy.results import FinalizedReduction
from UPbRPy.session_bundle import unpack_session_bundle
from UPbRPy.standards import standard_archive
from synthetic_bundles import write_session_bundle, zip_directory
from test_data_model_docs import DOCS, pipe_table
from test_export_table import synthetic_pipeline


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"


def fixture_pipeline():
    """Use real metadata and concentrations; dates are unavailable without 202Hg.

    The trimmed fixture cannot run the raw-ratio stage. An empty final-date
    frame lets the publication builder exercise its real concentration stages
    without inventing a mercury signal or calculated isotope result.
    """
    experiment, channels, data = io.read_experiment_data(FIXTURE)
    config = PipelineConfig(primary_68_standard="S513", primary_76_standard="S513",
        primary_82_standard="S513", concentration_standards=("NIST610", "NIST612"),
        downhole=FitSettings(method="mean"), session=FitSettings(method="mean"))
    pipeline = ReductionPipeline(experiment, data, channels, standard_archive, config,
        source_directory=FIXTURE, build_diagnostic_plots=False)
    frame = experiment[["sample", "spot_names"]].rename(columns={"spot_names": "spot_name"})
    frame["preferred_date"] = np.nan
    frame["preferred_date_2e"] = np.nan
    frame["preferred_date_method"] = pd.Series(pd.NA, index=frame.index, dtype="string")
    pipeline.cache.set(PipelineStage.FINALIZE, FinalizedReduction(
        frame, frame.copy(), pd.DataFrame(), pd.DataFrame(), config.decision))
    return pipeline


def test_global_notes_then_first_use_and_all_ids_resolve():
    pipeline = synthetic_pipeline()
    table = build_export_table(pipeline)
    notes = build_footnotes(table, pipeline)
    globals_ = ["session", "method", "standards", "corrections", "uncertainty", "constants", "software"]
    expected = list(dict.fromkeys(globals_ + [key for col in table.visible_columns() for key in col.footnotes]))
    assert [n.id for n in notes] == expected
    assert [n.number for n in notes] == list(range(1, len(notes) + 1))
    assert all(key in expected for col in table.visible_columns() for key in col.footnotes)
    assert "grimes2015" not in expected  # No Nb or Yb was measured.
    assert "ti_in_zircon" in expected
    table.settings = replace(table.settings, excluded_columns=("Ti_in_zircon_C", "Pb", "notes"))
    filtered = build_footnotes(table, pipeline)
    assert {"ti_in_zircon", "total_pb", "notes", "glasses"}.isdisjoint(n.id for n in filtered)
    assert [n.number for n in filtered] == list(range(1, len(filtered) + 1))
    # First use follows a reordered visible catalogue, not a fixed registry.
    table.columns = tuple(reversed(table.columns))
    reordered = build_footnotes(table, pipeline)
    assert [n.id for n in reordered] == list(dict.fromkeys(
        globals_ + [key for col in table.visible_columns() for key in col.footnotes]))


def test_missing_fields_settings_and_citations():
    pipeline = synthetic_pipeline()
    table = build_export_table(pipeline)
    notes = {n.id: n for n in build_footnotes(table, pipeline)}
    for field in ("laser_system", "spot_diameter_um", "repetition_rate_hz", "fluence_j_cm2", "laboratory"):
        assert field in notes["session"].missing_fields
    assert "not recorded" in notes["session"].text
    table.settings = ExportSettings(laser_system="Laser", spot_diameter_um="25",
        repetition_rate_hz="5", fluence_j_cm2="3", laboratory="Lab", operator="Operator")
    supplied = {n.id: n for n in build_footnotes(table, pipeline)}["session"]
    assert all(field not in supplied.missing_fields for field in (
        "laser_system", "spot_diameter_um", "repetition_rate_hz", "fluence_j_cm2", "laboratory", "operator"))
    assert "laser system: Laser; spot diameter: 25 µm; repetition rate: 5 Hz; fluence: 3 J/cm²" in supplied.text
    assert "10.1111/ggr.12603" in notes["standards"].text
    assert "10.1111/j.1751-908X.1995.tb00147.x" in notes["standards"].text
    assert "10.1111/j.1751-908X.2011.00120.x" in notes["standards"].text
    assert "10.1103/PhysRevC.4.1889" in notes["constants"].text
    assert "137.818 ± 0.045" in notes["constants"].text
    assert "207Pb/206Pb (S513): 0.0068 % (relative 1s)" in notes["uncertainty"].text
    assert "primary_reference_terms.207Pb/206Pb" not in notes["uncertainty"].missing_fields
    assert "not recorded" in notes["corrections"].text  # Default manifest has no detector factors.
    assert "burn-through" in notes["notes"].text.lower()
    assert "High common Pb" not in notes["notes"].text
    assert "high 204" not in notes["notes"].text
    assert date.today().isoformat() in notes["software"].text


def test_sources_follow_active_values_and_unavailable_library(tmp_path):
    pipeline = synthetic_pipeline()
    pipeline.standard_archive = pipeline.standard_archive.copy()
    pipeline.standard_archive.loc["S513", "206Pb/238U*"] *= 1.1
    table = build_export_table(pipeline)
    notes = {n.id: n for n in build_footnotes(table, pipeline)}
    assert "primary.S513.206Pb/238U.citation" in notes["standards"].missing_fields
    pipeline.reference_library_path = tmp_path / "missing"
    notes = {n.id: n for n in build_footnotes(table, pipeline)}
    assert "primary.S513.207Pb/235U.citation" in notes["standards"].missing_fields


def test_heading_styles_and_text_helpers():
    col = ExportColumn("x", "Dates", "206Pb/238U date ±2s-sys", "Ma", footnotes=("a",))
    notes = [Footnote("a", 3, "A sentence.", ())]
    assert column_heading(col, notes) == "206Pb/238U date ±2s-sys (Ma) [3]"
    assert column_heading(col, notes, "markdown") == "206Pb/238U date ±2s-sys (Ma)<sup>3</sup>"
    assert column_heading(col, notes, "rich") == [
        ("206", True), ("Pb/", False), ("238", True), ("U date ±2s-sys (Ma)", False), ("3", True)]
    mass204 = replace(col, label="206Pb/204Pb ±1s", footnotes=())
    assert ("204", True) in column_heading(mass204, [], "rich")
    element = replace(col, label="10,000(Eu/Eu*)/Yb_N", footnotes=(), unit="")
    assert column_heading(element, [], "rich") == [(element.label, False)]
    assert footnotes_text(notes) == "[3] A sentence."
    assert footnotes_markdown(notes) == "3. A sentence."
    assert footnotes_text([]) == footnotes_markdown([]) == ""
    with pytest.raises(ValueError):
        column_heading(col, notes, "html")
    with pytest.raises(KeyError):
        column_heading(col, [])
    multiple = replace(col, footnotes=("b", "a", "b"))
    assert column_heading(multiple, [*notes, Footnote("b", 1, "Earlier.", ())]).endswith("[1,3]")


def test_publication_labels_and_classification_citations():
    pipeline = synthetic_pipeline()
    table = build_export_table(pipeline)
    columns = {c.key: c for c in table.columns}
    expected = {"La_N": "La", "Lu_N": "Lu", "10000Eu/Eu*/Y": "10,000(Eu/Eu*)/Y",
        "10000Eu/Eu*/Yb_N": "10,000(Eu/Eu*)/Yb_N", "1000(Ce/Nd)_N/Y": "1000(Ce/Nd)_N/Y",
        "U_conc_ppm": "U", "Th_conc_ppm": "Th", "notes": "Notes", "Ti_in_zircon_C": "T"}
    assert {key: columns[key].label for key in expected} == expected
    assert columns["Dy/Yb"].group == "Lu et al. (2016); Pizarro et al. (2020)"
    for col in table.columns:
        table.rows[col.key] = "present"  # Exercise all catalogue footnotes without requiring every isotope.
    text = footnotes_text(build_footnotes(table, pipeline))
    for citation in ("Grimes et al. (2015)", '"Itano \'24" as labelled in UPbR26plus; citation not verified',
                     "Lu et al. (2016, SEG Spec. Publ. 19, 329-347)", "Roberts et al. (2024)", "Loucks et al. (2024)"):
        assert citation in text
    assert "10.1007/s00410-007-0201-0" in text
    assert "aSiO2 = 1 and aTiO2 = 0.6" in text


def test_fixture_acquisition_summary_and_footnotes():
    pipeline = fixture_pipeline()
    summary = pipeline.experiment.attrs["qtegra"]["acquisition_summary"]
    assert json.loads(json.dumps(summary, allow_nan=False)) == summary
    assert summary["first_start"] == "2026-11-04T15:55:37.4924868"
    assert summary["last_start_date"] == "2026-11-04"
    assert summary["users"] == [r"BOISESTATE\ERB5110"]
    assert summary["machine"] == "iCAP RQ"
    assert summary["dwell_s"]["206Pb"] == .06
    assert summary["sweeps_per_acquisition"] == dict(median=72., min=72, max=72)
    assert summary["laser_output_settings_percent"] == ["46"]
    table = build_export_table(pipeline)
    notes = {n.id: n for n in build_footnotes(table, pipeline)}
    assert "Zircon_demo" in notes["session"].text
    assert "BOISESTATE" in notes["session"].text
    assert "NIST610" in notes["total_pb"].text and "NIST612" in notes["total_pb"].text
    assert "204Pb contributed to 0 of 12 analyses" in notes["total_pb"].text
    assert "unavailable because 202Hg was not measured" in notes["corrections"].text
    assert "91Zr, 177Hf 0.001 s" in notes["method"].text
    assert "U-Pb ratios and dates are not reported for reference glasses" in notes["glasses"].text


def test_summary_medians_missing_metadata_and_documented_keys():
    session = qtegra.open_qtegra(FIXTURE)
    session.acquisitions[0].channels = tuple(replace(c, dwell_s=.1 if c.name == "29Si" else c.dwell_s)
                                            for c in session.acquisitions[0].channels)
    session.acquisitions[0].y = session.acquisitions[0].y[:-1]
    session.acquisitions[0].time = session.acquisitions[0].time[:-1]
    summary = io.experiment_from_session(session)[0].attrs["qtegra"]["acquisition_summary"]
    assert summary == io.acquisition_summary(session)
    assert summary["dwell_s"]["29Si"] == .005
    assert summary["sweeps_per_acquisition"] == dict(median=72., min=71, max=72)
    rows = pipe_table(DOCS / "acquisition-model.md", "Acquisition summary")
    assert {row["Name"] for row in rows} == set(summary)
    rich = qtegra.read_rich_csv(FIXTURE / "Zircon_demo_Rich.csv")
    for a in rich.acquisitions:
        a.start = a.stop = a.start_text = a.stop_text = None
    missing = io.experiment_from_session(rich)[0].attrs["qtegra"]["acquisition_summary"]
    assert missing["first_start"] is None and missing["last_stop"] is None
    assert all(value is None for value in missing["dwell_s"].values())
    json.dumps(missing, allow_nan=False)


def test_configured_corrections_and_uncertainty_values():
    pipeline = synthetic_pipeline()
    pipeline.config = replace(pipeline.config, decision=replace(pipeline.config.decision,
        common_pb_method="207", age_transition_ma=800.), uncertainty=replace(pipeline.config.uncertainty,
        propagate_baseline=False, u238_u235=140., u238_u235_1s=.1,
        long_term_reproducibility_mode="user", long_term_reproducibility_relative_1s=.012))
    pipeline.run_finalize().decision = pipeline.config.decision
    pipeline.experiment.attrs["detector_calibration"] = dict(mode="drift", channels={
        "238U": dict(factor=123., qtegra_factor=120., factors=[121., 123., 125.])})
    table = build_export_table(pipeline)
    table.columns += (ExportColumn("test_date", "Common-Pb corrected", "207-method date", footnotes=("preferred",)),)
    table.rows["test_date"] = 1.
    notes = {n.id: n for n in build_footnotes(table, pipeline)}
    assert "is not propagated (disabled in settings)" in notes["corrections"].text
    assert "mode: drift; factors 238U: factor 123, qtegra_factor 120" in notes["corrections"].text
    assert "drift-factor range 121 to 125 (median 123)" in notes["corrections"].text
    assert "propagated baseline-mean uncertainty" not in notes["uncertainty"].text
    assert "Long-term term: 1.2%" in notes["uncertainty"].text
    assert "238U/235U = 140 ± 0.2" in notes["constants"].text
    assert "uses the 207-method" in notes["preferred"].text
    assert "age transition is 800 Ma" in notes["preferred"].text


def test_summary_regenerated_in_project_and_bundle(tmp_path):
    bundle = write_session_bundle(tmp_path / "source")
    experiment, channels, data = io.read_experiment_data(bundle)
    summary = experiment.attrs["qtegra"]["acquisition_summary"]
    pipeline = ReductionPipeline(experiment, data, channels, standard_archive,
        PipelineConfig(primary_channel="44Ca"), source_directory=bundle, build_diagnostic_plots=False)
    target = write_reduction_project(pipeline, bundle / (bundle.name + ".upbr"))
    project = read_reduction_project(target)
    source = resolve_project_path(target, project["paths"]["source_directory"], label="source")
    reopened = io.read_experiment_data(source)[0]
    assert restore_project_selections(reopened, project["selections"]).attrs["qtegra"]["acquisition_summary"] == summary
    unpacked = unpack_session_bundle(zip_directory(bundle), tmp_path / "cache")
    assert unpacked.project_file is not None
    assert io.read_experiment_data(unpacked.path)[0].attrs["qtegra"]["acquisition_summary"] == summary


def test_git_commit_loose_packed_detached_and_worktree(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    digest = "a" * 40
    head = gitdir / "HEAD"
    head.write_text(digest + "\n")
    assert _git_commit(tmp_path) == digest
    head.write_text("ref: refs/heads/main\n")
    assert _git_commit(tmp_path) is None
    (gitdir / "packed-refs").write_text(f"# packed\n{digest} refs/heads/main\n")
    assert _git_commit(tmp_path) == digest
    refs = gitdir / "refs/heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("b" * 40)
    assert _git_commit(tmp_path) == "b" * 40
    worktree = tmp_path / "tree"
    worktree.mkdir()
    metadata = gitdir / "worktrees/tree"
    metadata.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../.git/worktrees/tree\n")
    (metadata / "HEAD").write_text("ref: refs/heads/main\n")
    (metadata / "commondir").write_text("../..\n")
    assert _git_commit(worktree) == "b" * 40
    head.write_text("ref: refs/../../outside\n")
    assert _git_commit(tmp_path) is None
    assert _git_commit(tmp_path / "installed-without-checkout") is None


def test_compact_journal_prose_preserves_values_sources_and_clock(monkeypatch):
    import UPbRPy.export_table as module
    pipeline = fixture_pipeline()
    monkeypatch.setattr(module, '_git_commit', lambda: 'abcdef0123456789' * 2 + '12345678')
    table = build_export_table(pipeline)
    notes = {n.id: n for n in build_footnotes(table, pipeline)}
    standards = notes['standards'].text
    assert standards.count('Primary standard S513') == 1
    assert standards.count('Luo, T., Qing, L.') == 1
    assert '206Pb/238U 0.090955 (561.18 Ma)' in standards
    assert '207Pb/235U 0.73873 (561.66 Ma)' in standards
    assert '207Pb/206Pb 0.058934 (563.68 Ma)' in standards
    assert '208Pb/232Th 0.028154 (561.18 Ma)' in standards
    assert 'Concentration reference glasses NIST610 and NIST612 (Jochum, K.P., Weis, U.' in standards
    assert standards.count('Jochum') == 1
    assert 'acquisition starts: 2026-11-04 15:55 to 16:20 (instrument clock)' in notes['session'].text
    assert notes['session'].text.count('2026-11-04') == 1
    assert 'baseline median duration' in notes['method'].text
    assert re.search(r'median duration \d+\.\d s', notes['method'].text)
    assert 'git commit abcdef012345;' in notes['software'].text
    assert notes['software'].text.count('version') == 1
    assert 'SHA-256' not in notes['software'].text
    # All scheme-specific footnotes now carry the citation only; the shared
    # mass-ratio and decay-restoration definitions occur once.
    for col in table.columns:
        table.rows[col.key] = 'present'
    all_notes = {n.id: n for n in build_footnotes(table, pipeline)}
    text = footnotes_text(all_notes.values())
    assert text.count('Ratios are mass ratios of µg/g values unless subscript N') == 1
    assert text.count('U_i and Th_i restore radioactive decay') == 1
    assert all('Ratios are' not in all_notes[key].text for key in
               ('grimes2015', 'itano24', 'lu_pizarro', 'roberts2024', 'loucks2024'))
