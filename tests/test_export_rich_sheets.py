"""Shared SI sheets and the default publication products, using synthetic data."""
from dataclasses import replace
import hashlib
import zipfile
from xml.etree import ElementTree as ET

import pytest
import xlsxwriter

from UPbRPy.export_table import ExportSettings, RICH_SHEETS, build_export_table, matching_column_preset
from UPbRPy.pipeline import package_version
from UPbRPy.reproducibility import export_reduction_bundle
from UPbRPy.supplement import SUPPLEMENT_SHEETS, supplement_session, write_supplement_sheets, write_supplement_xlsx
from test_export_table import synthetic_pipeline


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SI_NAMES = ["README", "Table S1 Methods", "Table S2 Sample data", "Table S3 Standard data", "Table S4 QC"]
RICH_NAMES = dict(zip(RICH_SHEETS, (SI_NAMES[i] for i in (0, 1, 3, 4))))
SI_CONTENTS = list(zip(SI_NAMES, (
    "Citation, provenance, sessions, samples and conventions",
    "Instrumental methods, reference values and uncertainty propagation",
    "Listed samples, including excluded analyses",
    "All reference-material and concentration-glass analyses",
    "Per-session analytical weighted means against accepted dates",
)))


def read_workbook(path):
    """Return ordered sheet names, XML and decoded cell text without openpyxl."""
    with zipfile.ZipFile(path) as archive:
        strings = ["".join(si.itertext()) for si in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        names = [sheet.get("name") for sheet in ET.fromstring(archive.read("xl/workbook.xml")).find("s:sheets", NS)]
        result = {}
        for i, name in enumerate(names, 1):
            sheet = ET.fromstring(archive.read(f"xl/worksheets/sheet{i}.xml"))
            rows = []
            for row in sheet.findall("s:sheetData/s:row", NS):
                values = {}
                for cell in row:
                    value = cell.find("s:v", NS)
                    values[cell.get("r")] = (strings[int(value.text)] if cell.get("t") == "s"
                                              else value.text) if value is not None else ""
                rows.append(values)
            result[name] = (sheet, rows)
        return result


def column_a(book, name):
    return [next((value for key, value in row.items() if key[0] == "A" and key[1:].isdigit()), "")
            for row in book[name][1]]


def all_text(book, name):
    return " ".join(value for row in book[name][1] for value in row.values())


def readme_contents(book):
    labels = column_a(book, "README")
    start, end = labels.index("Sheet name") + 1, labels.index("Sessions")
    return [(label, next(value for cell, value in row.items() if cell[0] == "B" and cell[1:].isdigit()))
            for label, row in zip(labels[start:end], book["README"][1][start:end]) if label]


def test_si_matches_pre_refactor_content(tmp_path):
    # Captured from the original writer on synthetic_pipeline(), before factoring
    # the builder. Ignore the export date and environment-dependent software line.
    golden = [
        ("A1:F27", "fcc90ac84591359008b0171e3e69db0702db115ef2e9c1f63b9a5eafcd13ac1e"),
        ("A1:H80", "f7a2052d4179fdd21b16a971503e7853abc8f6f29c91d9d73492b96c3a02db44"),
        ("A1:AR20", "362306e625a5db27125e62fce3264b00d6b613269dd5a6171a23d8f0b45faecd"),
        ("A1:AR23", "1ec558f290c00c41e02c582c3823f6a5dbc733cfcff16479fc281a68e3c6653f"),
        ("A1:N16", "bc34fd349799826e36e583c6cd6c50840d6d88aae01bcaf6165ba27bf3486ace"),
    ]
    book = read_workbook(write_supplement_xlsx([supplement_session(synthetic_pipeline())], tmp_path / "si.xlsx"))
    assert list(book) == SI_NAMES
    for name, (dimension, digest) in zip(SI_NAMES, golden):
        assert book[name][0].find("s:dimension", NS).get("ref") == dimension
        values = ["Export date: <date>" if value.startswith("Export date:") else
                  "All sessions: <software provenance>" if value.startswith("All sessions: Reduced with UPbRPy")
                  else value for value in column_a(book, name)]
        assert hashlib.sha256("\n".join(values).encode()).hexdigest() == digest, name


def test_builder_orders_selected_sheets_and_reuses_state(tmp_path, monkeypatch):
    import UPbRPy.supplement as module

    session = supplement_session(synthetic_pipeline())
    original = module._Workbook
    states = []

    def create(*args):
        state = original(*args)
        states.append(state)
        return state

    monkeypatch.setattr(module, "_Workbook", create)
    target = tmp_path / "selected.xlsx"
    with xlsxwriter.Workbook(target) as workbook:
        write_supplement_sheets(workbook, [session], sheets=("methods", "readme"))
        workbook.add_worksheet("Table")
        style_count = len(states[0].styles)
        write_supplement_sheets(workbook, [session], sheets=("qc", "standards"))
        assert len(states) == 1
        assert len(states[0].styles) >= style_count
        assert workbook.formats[0].font_name == "Calibri"
        assert workbook.formats[0].font_size == 10
    assert SUPPLEMENT_SHEETS == ("readme", "methods", "samples", "standards", "qc")
    assert list(read_workbook(target)) == [SI_NAMES[0], SI_NAMES[1], "Table", SI_NAMES[3], SI_NAMES[4]]
    with xlsxwriter.Workbook(tmp_path / "invalid.xlsx") as workbook:
        with pytest.raises(ValueError, match="sheets"):
            write_supplement_sheets(workbook, [session], sheets=("unknown",))


def test_rich_settings_roundtrip_and_presets():
    assert ExportSettings.from_dict({"laboratory": "Legacy lab"}).excluded_sheets == ()
    settings = ExportSettings.from_dict({"excluded_sheets": ["qc", "qc"]})
    assert settings.excluded_sheets == ("qc",)
    assert settings.to_dict()["excluded_sheets"] == ["qc"]
    assert ExportSettings.from_dict(settings.to_dict()) == settings
    table = build_export_table(synthetic_pipeline())
    assert matching_column_preset(table, table.settings) == "publication"
    assert matching_column_preset(table, replace(table.settings, excluded_sheets=("qc",))) is None
    for value in (("unknown",), ("samples",), "qc", (1,)):
        with pytest.raises(ValueError, match="excluded_sheets"):
            ExportSettings(excluded_sheets=value)


@pytest.mark.parametrize("excluded", [(), *( (key,) for key in RICH_SHEETS), RICH_SHEETS])
def test_default_bundle_rich_workbooks(tmp_path, excluded):
    pipeline = synthetic_pipeline()
    pipeline.experiment.attrs["session_name"] = "Synthetic session"
    bundle = export_reduction_bundle(pipeline, tmp_path, include_audit_files=False,
                                     export_settings=ExportSettings(excluded_sheets=excluded), file_stem="results")
    assert {"results.xlsx", "results_SI.xlsx", "results.csv", "results.md"} <= {p.name for p in bundle.files}
    book = read_workbook(tmp_path / "results.xlsx")
    si = read_workbook(tmp_path / "results_SI.xlsx")
    expected = [RICH_NAMES[k] for k in ("readme", "methods") if k not in excluded]
    expected += ["Table"] + [RICH_NAMES[k] for k in ("standards", "qc") if k not in excluded]
    assert list(book) == expected + ["Reference materials"]
    assert list(si) == SI_NAMES
    assert readme_contents(si) == SI_CONTENTS
    if "readme" not in excluded:
        contents = readme_contents(book)
        assert [name for name, _ in contents] == list(book)
        descriptions = dict(SI_CONTENTS, **{
            "Table": "Publication table: listed samples and reference materials with footnotes",
            "Reference materials": "Per-system reference-material weighted means against accepted dates",
        })
        assert contents == [(name, descriptions[name]) for name in book]
        main_labels, si_labels = column_a(book, "README"), column_a(si, "README")
        assert main_labels[:main_labels.index("Contents")] == si_labels[:si_labels.index("Contents")]
        assert main_labels[main_labels.index("Sessions"):] == si_labels[si_labels.index("Sessions"):]
    for key in RICH_SHEETS:
        if key != "readme" and key not in excluded:
            assert column_a(book, RICH_NAMES[key]) == column_a(si, RICH_NAMES[key])
    readme = all_text(si, "README")
    assert package_version() in readme and "Reduced with UPbRPy" in readme
    assert "Sessions" in readme and "Label" in readme
    session_rows = [row for row in si["README"][1] if "Synthetic session" in row.values()]
    assert len(session_rows) == 1
    assert list(session_rows[0].values()) == ["Synthetic session", "not recorded", "4", "2", "1", "0"]
    methods = all_text(si, "Table S1 Methods")
    assert "lambda238 = 1.55125e-10" in methods and "lambda235 = 9.8485e-10" in methods
    assert "lambda232 = 4.9475e-11" in methods
    assert "Preferred-date rule" in methods and "1000 Ma" in methods
    assert "Ti-in-zircon" in methods and "doi:10.1007/s00410-007-0201-0" in methods
    assert "S513" in methods and "doi:10.1111/ggr.12603" in methods


def test_bundle_shares_one_supplement_session(tmp_path, monkeypatch):
    import UPbRPy.reproducibility as module

    seen = []
    xlsx_writer, si_writer = module.write_export_xlsx, module.write_supplement_xlsx

    def xlsx(*args, supplement):
        seen.append(supplement)
        return xlsx_writer(*args, supplement=supplement)

    def si(sessions, *args):
        seen.extend(sessions)
        return si_writer(sessions, *args)

    monkeypatch.setattr(module, "write_export_xlsx", xlsx)
    monkeypatch.setattr(module, "write_supplement_xlsx", si)
    export_reduction_bundle(synthetic_pipeline(), tmp_path, include_audit_files=False)
    assert len(seen) == 2 and seen[0] is seen[1]
