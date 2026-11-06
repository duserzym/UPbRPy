"""Check the published input contract against readers and public dataclasses."""

from dataclasses import fields
from pathlib import Path
import re

import pytest

from UPbRPy import qtegra
from qtegra_helpers import write_y_only_csv
from test_qtegra_reduced import reduced


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs/data-model"
RICH = ROOT / "tests/fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"


def pipe_table(path, heading):
    """Read the first pipe table below an exact stable Markdown heading."""
    text = path.read_text(encoding="utf-8")
    section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    lines = section.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("|"))
    rows = []
    for line in lines[first:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip().strip("`") for cell in line.strip("|").split("|")])
    assert all(re.fullmatch(r":?-+:?", cell) for cell in rows[1])
    assert all(len(row) == len(rows[0]) for row in rows[2:])
    return [dict(zip(rows[0], row)) for row in rows[2:]]


CONTRACT = pipe_table(DOCS / "rich-csv.md", "Header and row-group contract")


def row_name(line):
    """Map fixture keys to the public table's row names."""
    group, key, subkey, quantity = line.decode("utf-8-sig").split(",", 4)[:4]
    if group == "MainRuns":
        return f"MainRuns {quantity}"
    if group in {"BrigidMS", "PMIAnalyteLaser", "Info"}:
        return " ".join(part for part in (group, key, subkey) if part)
    return group


def without_row(tmp_path, name):
    """Remove a documented header/group/key without changing fixture bytes."""
    if name.startswith("MainRuns "):
        return reduced(tmp_path, drop=(name.removeprefix("MainRuns "),))
    if name in {"TuneSettings", "StatusReport", "StartTime", "StopTime"}:
        return reduced(tmp_path, drop=(name,))
    lines = RICH.read_bytes().splitlines(keepends=True)
    if name.startswith("Header row "):
        del lines[int(name.removeprefix("Header row ")) - 1]
    else:
        assert any(row_name(line) == name for line in lines[2:]), name
        lines = lines[:2] + [line for line in lines[2:] if row_name(line) != name]
    target = tmp_path / "without-row.csv"
    target.write_bytes(b"".join(lines))
    return target


def test_row_contract_covers_fixture():
    names = [row["Name"] for row in CONTRACT]
    assert len(names) == len(set(names))
    expected = {"Header row 1", "Header row 2"} | {
        row_name(line) for line in RICH.read_bytes().splitlines()[2:]
    }
    assert set(names) == expected
    assert {row["Required/optional"] for row in CONTRACT} == {
        "Required", "Optional", "One of",
    }


@pytest.mark.parametrize("row", [
    pytest.param(row, id=row["Name"])
    for row in CONTRACT
])
def test_documented_row_requirement(tmp_path, row):
    source = without_row(tmp_path, row["Name"])
    if row["Required/optional"] == "Required":
        with pytest.raises(ValueError):
            qtegra.read_rich_csv(source)
    else:
        session = qtegra.read_rich_csv(source)
        assert len(session.acquisitions) == 12
        assert session.labels[0] == "NIST612_896"
        assert len(session.channel_names) == 9


def test_either_timing_source_alone_is_sufficient(tmp_path):
    conditional = {
        row["Name"] for row in CONTRACT if row["Required/optional"] == "One of"
    }
    assert conditional == {"MainRuns Time", "StartTime"}
    for timing in conditional:
        source = reduced(tmp_path, keep=("Y", timing.removeprefix("MainRuns ")))
        assert len(qtegra.read_rich_csv(source).acquisitions) == 12
    source = write_y_only_csv(RICH, tmp_path / "neither-time-source.csv")
    with pytest.raises(qtegra.UnsupportedQtegraFormat, match="no time information"):
        qtegra.read_rich_csv(source)


def test_capabilities_are_documented():
    rows = pipe_table(DOCS / "acquisition-model.md", "Capabilities")
    assert {row["Capability"] for row in rows} == qtegra.CAPABILITIES


@pytest.mark.parametrize("model", [
    qtegra.QtegraSession, qtegra.Acquisition, qtegra.Channel, qtegra.SourceFile,
])
def test_public_dataclass_fields_are_documented(model):
    rows = pipe_table(DOCS / "acquisition-model.md", f"{model.__name__} fields")
    assert {row["Name"] for row in rows} == {
        field.name for field in fields(model) if not field.name.startswith("_")
    }


def test_export_table_catalogue_and_settings_are_documented():
    from UPbRPy.export_table import ExportSettings, _catalogue, _ELEMENT_MASSES
    path = DOCS / "export-table.md"
    rows = pipe_table(path, "Columns")
    columns = _catalogue(sorted(_ELEMENT_MASSES, key=_ELEMENT_MASSES.get))
    assert {row["Key"] for row in rows} == {col.key for col in columns}
    indexed = {row["Key"]: row for row in rows}
    for col in columns:
        row = indexed[col.key]
        assert row["Group"] == col.group
        assert row["Label"] == col.label
        assert row["Unit"] == col.unit
        assert row["Default/optional"] == ("Optional" if col.optional else "Default")
        assert row["Footnote ids"] == (", ".join(col.footnotes) or "—")
        assert row["Source quantity"]
    settings = pipe_table(path, "ExportSettings fields")
    assert {row["Name"] for row in settings} == {field.name for field in fields(ExportSettings)}
