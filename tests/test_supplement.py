"""SI workbook content and layout through standard-library XLSX inspection."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
import json
import re
import zipfile
from xml.etree import ElementTree as ET

import numpy as np
import pytest

from UPbRPy.pipeline import package_version
from UPbRPy.export_table import ExportSettings, build_footnotes
from UPbRPy.reproducibility import export_reduction_bundle
from UPbRPy.supplement import (
    MethodsEntry, SampleEntry, SupplementInfo, SupplementSession,
    session_display_name, supplement_info_from_settings, supplement_session,
    write_supplement_xlsx,
)
from test_export_table import synthetic_pipeline


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SHEETS = ["README", "Table S1 Methods", "Table S2 Sample data", "Table S3 Standard data", "Table S4 QC"]


class WorkbookXML:
    def __init__(self, path):
        with zipfile.ZipFile(path) as archive:
            self.xml = {name: archive.read(name) for name in archive.namelist() if name.endswith(".xml")}
        self.shared = ET.fromstring(self.xml["xl/sharedStrings.xml"])
        self.strings = ["".join(si.itertext()) for si in self.shared]
        self.sheets = [ET.fromstring(self.xml[f"xl/worksheets/sheet{i}.xml"]) for i in range(1, 6)]

    def value(self, cell):
        value = cell.find("s:v", NS)
        if value is None:
            return ""
        return self.strings[int(value.text)] if cell.get("t") == "s" else value.text

    def rows(self, sheet):
        return [{re.sub(r"\d", "", c.get("r")): self.value(c) for c in row}
                for row in self.sheets[sheet].findall("s:sheetData/s:row", NS)]


def sessions():
    pipeline = synthetic_pipeline()
    first = supplement_session(pipeline, excluded_spots=("B_3",), label="Session A")
    second = supplement_session(synthetic_pipeline(), label="Session B")
    for session, start in ((first, "2026-01-01T10:00:00"), (second, "2026-02-01T10:00:00")):
        session.table.context["experiment_metadata"]["qtegra"] = {"acquisition_summary": {
            "first_start": start, "last_start": start.replace("10:00", "12:00"),
            "machine": "Instrument", "users": ["Operator"], "tune_names": ["Tune"],
            "laser_output_settings_percent": ["30"],
            "dwell_s": {c: .01 for c in session.table.context["channels"]},
            "sweeps_per_acquisition": {"median": 8, "min": 8, "max": 8}}}
    return pipeline, first, second


def test_workbook_structure_and_content(tmp_path):
    _, first, second = sessions()
    info = SupplementInfo(citation="Study citation", export_date=date(2026, 3, 1),
        samples=(SampleEntry("Sample B", ("b",), "Unit", "Interpret with care"),
                 SampleEntry("Sample C")),
        methods=(MethodsEntry("Laboratory and sample preparation", "Preparation", "Polished", "lab record"),))
    target = write_supplement_xlsx([second, first], tmp_path / "supplement", info)
    assert target.suffix == ".xlsx"
    xml = WorkbookXML(target)
    workbook = ET.fromstring(xml.xml["xl/workbook.xml"])
    assert [s.get("name") for s in workbook.find("s:sheets", NS)] == SHEETS
    sample = xml.sheets[2]
    assert sample.find(".//s:pane", NS).attrib == {
        "xSplit": "3", "ySplit": "3", "topLeftCell": "D4", "activePane": "bottomRight", "state": "frozen"}
    assert any(m.get("ref").endswith("2") for m in sample.findall("s:mergeCells/s:mergeCell", NS))
    assert xml.shared.find('.//s:vertAlign[@val="superscript"]', NS) is not None
    assert sample.find('.//s:col[@outlineLevel="1"]', NS) is not None
    for i, sheet in enumerate(xml.sheets):
        setup = sheet.find("s:pageSetup", NS)
        assert setup.get("orientation") == ("portrait" if i == 0 else "landscape")
        assert setup.get("paperSize") == "1"
        if i in (2, 3):  # wide data tables: fixed scale, identifier columns repeated
            assert setup.get("scale") == "55"
            assert sheet.find(".//s:pageSetUpPr", NS) is None or sheet.find(".//s:pageSetUpPr", NS).get("fitToPage") != "1"
        else:
            assert setup.get("fitToWidth", "1") == "1"
            assert sheet.find(".//s:pageSetUpPr", NS).get("fitToPage") == "1"
        assert "&A — page &P of &N" == sheet.find("s:headerFooter/s:oddFooter", NS).text
        assert sheet.find(".//s:pane", NS).get("state") == "frozen"
    assert "_xlnm.Print_Titles" in xml.xml["xl/workbook.xml"].decode()
    assert "'Table S2 Sample data'!$A:$C" in xml.xml["xl/workbook.xml"].decode()
    readme = " ".join(v for row in xml.rows(0) for v in row.values())
    assert package_version() in readme and "no analyses in these sessions" in readme
    assert "Study citation" in readme and "2026-03-01" in readme
    assert "omitted: 4" in readme
    assert "repository:" in readme and "revision:" in readme and "commit_date:" in readme
    assert not any(re.search(r"\bA_[26]\b", text) for text in xml.strings)
    # Sample identifiers are only ever written after selection. Check all XML,
    # including shared strings, for a distinctive unlisted sample below too.
    sample_rows = xml.rows(2)
    note = next(r for r in sample_rows if r.get("B") == "Sample B: Interpret with care")
    assert note["A"].isdigit()
    assert any(f"Note {note['A']}: Interpret with care" in r.get("A", "") for r in sample_rows)
    assert any("±2s-sys not combined across sessions" in r.get("A", "") for r in sample_rows)
    analysis = [r for r in sample_rows if r.get("B") in ("Session A", "Session B")]
    assert [r["B"] for r in analysis] == ["Session A"] * 2 + ["Session B"] * 2
    assert all(r["A"] == "Sample B" for r in analysis)
    assert [r["C"] for r in analysis[:2]] == ["B_0", "B_3"]
    assert sum(r.get("A") in ("Session A", "Session B") for r in xml.rows(4)) == 2 * 4 * 2
    assert any(r.get("C") == "primary, calibration; not independent" for r in xml.rows(4))
    methods = xml.rows(1)
    assert any(r.get("B") == "Preferred-date rule" and "1000" in r.get("C", "") for r in methods)
    assert any(r.get("B") == "Laboratory" and r.get("C") == "not recorded" for r in methods)
    assert any(r.get("B") == "Dwell 10 ms" for r in methods)
    assert any(r.get("B") == "Total dwell per sweep (s)" and r.get("C") == "0.09" for r in methods)
    assert any(re.fullmatch(r"C\d+:D\d+", m.get("ref")) for m in xml.sheets[1].findall("s:mergeCells/s:mergeCell", NS))
    assert not any("Reduced with UPbRPy" in v for r in sample_rows for v in r.values())
    assert any("see Table S1" in r.get("A", "") for r in sample_rows)


def test_filter_never_serializes_unlisted_sample_and_keeps_qc(tmp_path):
    _, first, second = sessions()
    for session in (first, second):
        mask = session.table.rows["sample"].eq("A")
        session.table.rows.loc[mask, "sample"] = "Unlisted sample"
        session.table.rows.loc[mask, "spot_name"] = ["Unlisted sample 1", "Unlisted sample 2"]
    info = SupplementInfo(samples=(SampleEntry("Sample B", aliases=("B",)),))
    xml = WorkbookXML(write_supplement_xlsx([first, second], tmp_path / "selected.xlsx", info))
    assert all(b"Unlisted sample" not in data for data in xml.xml.values())
    standard_rows = [r for r in xml.rows(3) if r.get("B") in ("Session A", "Session B")]
    assert len(standard_rows) == 6
    assert {r["A"] for r in standard_rows} == {"S513", "91500", "NIST612"}
    headings = next(r for r in xml.rows(3) if r.get("A") == "Sample")
    temperature = next(k for k, v in headings.items() if v.startswith("T (°C)"))
    assert all(not r.get(temperature) for r in standard_rows if r["A"] == "NIST612")


def test_merged_notes_precision_and_single_session_summary(tmp_path):
    _, first, second = sessions()
    first.table.rows.loc[first.table.rows.spot_name.eq("B_0"), "206Pb/238U"] = .1234567890123
    second = replace(second, footnotes=tuple(replace(n, text="Alternate total-Pb definition.") if n.id == "total_pb" else n for n in second.footnotes))
    xml = WorkbookXML(write_supplement_xlsx([first, second], tmp_path / "merged.xlsx"))
    notes = [r.get("B", "") for r in xml.rows(2) if r.get("A", "").isdigit()]
    pb = next(n for n in notes if "Alternate total-Pb definition." in n)
    assert pb.startswith("Session A:") and "Session B: Alternate" in pb
    assert sum("Ti-in-zircon temperatures use" in n for n in notes) == 1
    assert b"0.1234567890123" in xml.xml["xl/worksheets/sheet3.xml"]
    one = WorkbookXML(write_supplement_xlsx([first], tmp_path / "one.xlsx", SupplementInfo(paper="a4")))
    assert any("±2s-sys =" in r.get("A", "") for r in one.rows(2))
    assert all(sheet.find("s:pageSetup", NS).get("paperSize") == "9" for sheet in one.sheets)
    # A within-2s result must correspond to the displayed numerical test.
    for row in one.rows(4):
        if row.get("A") != "Session A":
            continue
        if row.get("L") and row.get("G"):
            limit = np.hypot(float(row["G"]), float(row.get("K") or 0))
            assert row["N"] == ("yes" if abs(float(row["L"])) <= limit else "no")
    pbpb = next(r for r in one.rows(4) if r.get("B") == "91500" and r.get("D") == "207Pb/206Pb")
    assert float(pbpb["K"]) == pytest.approx(.6)  # Recorded 0.3 Ma at 1s.
    thpb = next(r for r in one.rows(4) if r.get("B") == "S513" and r.get("D") == "208Pb/232Th")
    assert not thpb.get("K")  # Do not transfer the error of a different date.


def test_sample_summary_dispersed_mixed_and_alias_pooling(tmp_path):
    _, first, _ = sessions()
    rows = first.table.rows
    # Aliases can pool several original sample names within one session.
    info = SupplementInfo(samples=(SampleEntry("Combined", ("A", "B")),))
    good = WorkbookXML(write_supplement_xlsx([first], tmp_path / "good.xlsx", info))
    assert any("n = 4 analyses" in r.get("A", "") and "weighted mean" in r["A"] for r in good.rows(2))
    rows.loc[rows.spot_name.eq("A_2"), "preferred_date"] = 1200
    dispersed = WorkbookXML(write_supplement_xlsx([first], tmp_path / "dispersed.xlsx", info))
    assert any("dispersed: MSWD" in r.get("A", "") and "no weighted mean" in r["A"] for r in dispersed.rows(2))
    rows.loc[rows.spot_name.eq("A_2"), "preferred_date_method"] = "207Pb/206Pb"
    mixed = WorkbookXML(write_supplement_xlsx([first], tmp_path / "mixed.xlsx", info))
    assert any("mixed methods" in r.get("A", "") and "no weighted mean" in r["A"] for r in mixed.rows(2))
    rows.loc[rows.role.eq("unknown"), "excluded"] = True
    empty = WorkbookXML(write_supplement_xlsx([first], tmp_path / "excluded.xlsx", info))
    assert any("MSWD = not available, n = 0; no weighted mean" in r.get("A", "") for r in empty.rows(2))


def test_union_columns_keep_publication_order_and_trace_outline(tmp_path):
    _, first, second = sessions()
    second.table.settings = replace(second.table.settings, optional_columns=("204Pb",))
    for session in (first, second):
        session.table.settings = replace(session.table.settings,
            excluded_columns=tuple(c.key for c in session.table.columns if c.group == "Concentrations"))
    xml = WorkbookXML(write_supplement_xlsx([first, second], tmp_path / "union.xlsx"))
    heading = next(r for r in xml.rows(2) if r.get("A") == "Sample")
    values = list(heading.values())
    assert next(i for i, v in enumerate(values) if v.startswith("204Pb (µg/g)")) < next(i for i, v in enumerate(values) if v.startswith("208Pb/232Th"))
    key = next(k for k, v in heading.items() if v.startswith("T (°C)"))
    col_index = 0
    for char in key:
        col_index = col_index * 26 + ord(char) - ord("A") + 1
    columns = xml.sheets[2].findall("s:cols/s:col", NS)
    style = next(c for c in columns if int(c.get("min")) <= col_index <= int(c.get("max")))
    assert style.get("outlineLevel") == "1" and style.get("hidden") is None


def test_session_names_and_existing_footnotes():
    context = {"session_name": "raw", "manifest": {"metadata": {"origin": {"session_key": "Origin"}},
        "input_files": [{"path": "/input/Folder/file.csv"}]},
        "experiment_metadata": {"qtegra": {"sources": [{"format": "rich", "path": "/input/RichSession_Rich.csv"}]}}}
    assert session_display_name(context) == "Origin"
    context["manifest"]["metadata"] = {}
    assert session_display_name(context) == "RichSession"
    context["experiment_metadata"] = {}
    assert session_display_name(context) == "Folder"
    assert session_display_name({"session_name": "raw"}) == "not recorded"
    pipeline = synthetic_pipeline()
    session = supplement_session(pipeline)
    assert session.footnotes == tuple(build_footnotes(session.table, pipeline))
    assert next(n.text for n in session.footnotes if n.id == "session").startswith("Session: not recorded;")


def test_settings_mapping_citation_roundtrip_and_bundle(tmp_path):
    settings = ExportSettings(laboratory="Lab", laser_system="Laser", spot_diameter_um="25",
        repetition_rate_hz="5", fluence_j_cm2="3", operator="Operator",
        table_title="Study title", citation="Study citation")
    assert ExportSettings.from_dict(json.loads(json.dumps(settings.to_dict()))) == settings
    info = supplement_info_from_settings(settings)
    assert info.title == settings.table_title and info.citation == settings.citation
    assert len(info.methods) == 6 and all(m.source == "export settings" for m in info.methods)
    pipeline = synthetic_pipeline()
    bundle = export_reduction_bundle(pipeline, tmp_path, formats=("si",), export_settings=settings,
        file_stem="study", include_audit_files=False)
    assert [p.name for p in bundle.files] == ["study_SI.xlsx"]
    assert "Study citation" in WorkbookXML(bundle.files[0]).strings[1]


@pytest.mark.parametrize("factory", [
    lambda: MethodsEntry(1, "Parameter", "Value"),
    lambda: MethodsEntry("", "Parameter", "Value"),
    lambda: SampleEntry("Sample", aliases="alias"),
    lambda: SampleEntry("Sample", note=2),
    lambda: SupplementInfo(samples=("Sample",)),
    lambda: SupplementInfo(methods=("Method",)),
    lambda: SupplementInfo(notes="note"),
    lambda: SupplementInfo(paper="legal"),
    lambda: SupplementInfo(export_date=datetime(2026, 1, 1)),
    lambda: SupplementInfo(samples=(SampleEntry("Sample"), SampleEntry("Other", ("sample",)))),
    lambda: SupplementSession("table", ()),
    lambda: ExportSettings(citation=123),
])
def test_validation(factory):
    with pytest.raises(ValueError):
        factory()


def test_frozen_records_and_writer_validation(tmp_path):
    with pytest.raises(FrozenInstanceError):
        SampleEntry("Sample").name = "Other"
    with pytest.raises(ValueError, match="nonempty"):
        write_supplement_xlsx([], tmp_path / "empty.xlsx")
    with pytest.raises(ValueError, match="SupplementSession"):
        write_supplement_xlsx(["session"], tmp_path / "bad.xlsx")


def test_gui_si_choice_and_citation_preservation(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    from GUI.export_dialog import ExportResultsDialog
    from GUI.theme import build_application
    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    session = supplement_session(synthetic_pipeline(), ExportSettings(citation="Study citation"))
    dialog = ExportResultsDialog(session.table, folder=tmp_path)
    try:
        assert dialog.selected_formats() == ("xlsx", "csv", "md")
        assert dialog.format_checks["si"].text() == "SI workbook (.xlsx)"
        dialog.format_checks["si"].setChecked(True)
        assert dialog.selected_formats() == ("xlsx", "csv", "md", "si")
        assert dialog.export_settings().citation == "Study citation"
        assert dialog.minimumSizeHint().width() <= 1200
    finally:
        dialog.close()
        dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_supplied_method_entry_replaces_recorded_row(tmp_path):
    _, first, second = sessions()
    info = SupplementInfo(methods=(MethodsEntry("Laboratory and sample preparation", "Laboratory",
                                                "Example laboratory", "supplied"),))
    xml = WorkbookXML(write_supplement_xlsx([first, second], tmp_path / "si.xlsx", info))
    laboratory = [r for r in xml.rows(1) if r.get("B") == "Laboratory"]
    assert len(laboratory) == 1 and laboratory[0]["C"] == "Example laboratory"
    titles = [r.get("A", "") for r in xml.rows(2)]
    assert not any("1 sessions" in text for text in titles)


def test_instrument_login_never_reaches_workbook(tmp_path):
    login = "EXAMPLEDOMAIN\\op1234"
    _, first, second = sessions()
    for session in (first, second):
        session.table.context["experiment_metadata"]["qtegra"]["acquisition_summary"]["users"] = [login]
    info = SupplementInfo(notes=(f"Acquired under {login}.",),
                          methods=(MethodsEntry("ICP-MS", "Account", "op1234", "instrument log"),))
    target = write_supplement_xlsx([first, second], tmp_path / "si.xlsx", info)
    with zipfile.ZipFile(target) as archive:
        text = "".join(archive.read(n).decode() for n in archive.namelist() if n.endswith(".xml"))
    assert "op1234" not in text and "EXAMPLEDOMAIN" not in text
    xml = WorkbookXML(target)
    assert not [r for r in xml.rows(1) if r.get("B") == "Operator"]
    staffed = replace(first, table=replace(first.table, settings=replace(first.table.settings, operator="Laboratory staff")))
    xml = WorkbookXML(write_supplement_xlsx([staffed], tmp_path / "staffed.xlsx"))
    assert [r["C"] for r in xml.rows(1) if r.get("B") == "Operator"] == ["Laboratory staff"]
