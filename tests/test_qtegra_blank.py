"""Blank analyses are skipped before selection, reduction and export."""

import csv
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from UPbRPy import io, qtegra
from UPbRPy.export_table import build_export_table
from UPbRPy.pipeline import FitSettings, PipelineConfig, ReductionPipeline
from UPbRPy.reproducibility import export_reduction_bundle
from UPbRPy.standards import standard_archive
from synthetic_bundles import qtegra_csv, write_raw_directory
from test_export_table import synthetic_pipeline


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
WARNING = "Skipped blank analysis 'nothing' (no sweep rows)"


def blank_rich(tmp_path, position=1):
    """Insert a metadata-only column and synthetic 202Hg into a small Rich copy."""
    with RICH.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    # The trimmed fixture omits 202Hg, which the full ratio pipeline requires.
    for row in list(rows[2:]):
        if row[0] == "MainRuns" and row[2] == "204Hg" and row[3] in {"Y", "Time"}:
            extra = row.copy()
            extra[2] = "202Hg"
            if row[3] == "Y":
                extra[4:-1] = ["1.0" if cell else "" for cell in row[4:-1]]
            rows.append(extra)
    for index, row in enumerate(rows):
        value = "" if row[0] == "MainRuns" else row[4]
        if index == 0:
            value = "Sample blank"
        elif index == 1 or (row[0] == "BrigidMS" and row[1] == "Label"):
            value = "nothing"
        row.insert(4 + position, value)
    rows[0][4:-1] = [f"Sample {index}" for index in range(1, len(rows[0]) - 4)]
    target = tmp_path / "zircon_blank_rich.csv"
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)
    return target


@pytest.mark.parametrize("position", [0, 1, 12])
def test_rich_skips_blank_without_shifting_metadata(tmp_path, position):
    source = blank_rich(tmp_path, position)
    expected = qtegra.read_rich_csv(RICH)
    session = qtegra.open_qtegra(source)
    assert session.labels == expected.labels
    assert session.warnings == [WARNING]
    assert session.sources[0].path == source
    for index, (actual, original) in enumerate(zip(session.acquisitions, expected.acquisitions)):
        assert actual.sequence_index == index + 1 + (index >= position)
        assert actual.start_text == original.start_text
        assert actual.stop_text == original.stop_text
        assert actual.sequence_fields == original.sequence_fields
        for channel in original.channel_names:
            np.testing.assert_array_equal(
                actual.y[:, actual.channel_index(channel)],
                original.y[:, original.channel_index(channel)],
            )
    experiment, _, data = io.read_experiment_data(source)
    assert set(experiment.spot_names) == set(expected.labels)
    assert set(data.spot_name) == set(expected.labels)
    assert experiment.attrs["qtegra"]["warnings"] == [WARNING]


@pytest.mark.parametrize("empty_rows", [False, True])
def test_per_spot_skips_blank_and_preserves_sources(tmp_path, empty_rows):
    paths = write_raw_directory(tmp_path)
    blank = tmp_path / "blank.csv"
    text = qtegra_csv("nothing", datetime(2025, 3, 20, 21, 0, 30), sweeps=0)
    if empty_rows:
        text += ",,,,,\n" * 3
    blank.write_text(text, encoding="utf-8")
    assert qtegra.read_spot_file(blank).n_sweeps == 0
    for session in (qtegra.open_qtegra(tmp_path), qtegra.read_spot_folder(tmp_path)):
        assert "nothing" not in session.labels
        assert len(session.acquisitions) == len(paths)
        assert session.warnings == [WARNING]
        assert {source.path for source in session.sources} == {*paths, blank}
        assert [a.sequence_index for a in session.acquisitions] == [1, 3, 4, 5]
    experiment, _, data = io.read_experiment_data(tmp_path)
    assert "nothing" not in set(experiment.spot_names) | set(data.spot_name)
    assert experiment.attrs["qtegra"]["warnings"] == [WARNING]
    session = qtegra.open_qtegra(blank)
    assert session.acquisitions == [] and session.warnings == [WARNING]
    with pytest.raises(ValueError, match="empty QtegraSession"):
        io.read_experiment_data(blank)


def test_blank_rich_full_pipeline_export_retains_warning(tmp_path):
    source = blank_rich(tmp_path)
    experiment, channels, data = io.read_experiment_data(source)
    config = PipelineConfig(primary_68_standard="S513", primary_76_standard="S513",
        primary_82_standard="S513", concentration_standards=("NIST612", "NIST610"),
        session=FitSettings(method="mean"))
    pipeline = ReductionPipeline(experiment, data, channels, standard_archive, config,
        source_directory=source, input_files=(source,), build_diagnostic_plots=False)
    table = build_export_table(pipeline)
    assert len(table.rows) == 12
    assert "nothing" not in set(table.rows.spot_name)
    assert table.context["experiment_metadata"]["qtegra"]["warnings"] == [WARNING]
    assert np.isfinite(table.rows.loc[table.rows.role.ne("glass"), "206Pb/238U_age"]).all()
    bundle = export_reduction_bundle(pipeline, tmp_path / "export", table=table,
        formats=("csv",), include_depth_profile=False)
    assert not bundle.warnings
    manifest = json.loads((bundle.directory / "reduction_manifest.json").read_text())
    assert manifest["metadata"]["qtegra"]["warnings"] == [WARNING]


@pytest.mark.parametrize("cached_normalization", [False, True])
def test_total_pb_leaves_missing_raw_spot_blank(cached_normalization):
    pipeline = synthetic_pipeline()
    if cached_normalization:
        pipeline.run_normalization()
    pipeline.total_data = pipeline.total_data.loc[pipeline.total_data.spot_name.ne("B_0")]
    rows = build_export_table(pipeline).rows.set_index("spot_name")
    assert pd.isna(rows.loc["B_0", ["Pb", "204Pb", "206Pb", "207Pb", "208Pb"]]).all()
    assert np.isfinite(rows.loc["A_2", "Pb"])
