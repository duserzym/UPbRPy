"""Private-library composition with invented materials and working values only."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tomllib
import zipfile
from xml.etree import ElementTree as ET

import pandas as pd
import pytest

from UPbRPy.standards import (
    build_standard_archive, default_lab_overlay_path, load_standard_measurements,
    reference_library_info, write_merged_library,
)


BASE_INFO = {
    "version": "format-2+sha256.722f368e2b52",
    "sha256": "722f368e2b52e84e2368c915616c853588448a220b13bf1613dd9de092b4fc6e",
    "repository": "https://github.com/duserzym/upb-standards",
    "revision": "e00070088b5e0f70edb28ad1c87147e9a1008966",
    "commit_date": "2026-11-16T15:20:44-07:00",
}


def library(root, *, name="LABX", aliases=(), source="labx_working", overlay=True,
            replaces=(), manifest_extra="", value=0.0123):
    root.mkdir(parents=True)
    manifest = ('format_version = 2\nsources = "sources.toml"\n'
                'material_files = ["material.toml"]\n')
    if overlay:
        manifest += 'lab = "Example lab"\nreplaces = ' + json.dumps(list(replaces)) + '\n'
    (root / "library.toml").write_text(manifest + manifest_extra, encoding="utf-8")
    status = "internal_unpublished" if overlay else "recommended"
    (root / "sources.toml").write_text(f'''format_version = 2
[sources.{source}]
display = "Invented {source} working record"
kind = "lab_compilation"
status = "{status}"
no_doi_reason = "Invented working assignment retained privately."
''', encoding="utf-8")
    table = f'standards.{json.dumps(name)}'
    (root / "material.toml").write_text(f'''format_version = 2
material = "rutile"
[{table}]
aliases = {json.dumps(list(aliases))}
[[{table}.measurements]]
role = "radiogenic_ratio"
unit = "ratio"
status = "{status}"
source = "{source}"
locator = "Invented worksheet"
notes = "Invented {{quantity}} assignment."
[{table}.measurements.values]
"206Pb/238U*" = {{ value = {value}, uncertainty = 0.0002, uncertainty_type = "2sd" }}
"207Pb/206Pb*" = 0.0567
[[{table}.measurements]]
role = "initial_ratio"
unit = "ratio"
status = "{status}"
source = "{source}"
[{table}.measurements.values]
"207Pb/206Pb_i" = 0.89
''', encoding="utf-8")
    return root


@pytest.fixture
def overlay(tmp_path):
    return library(tmp_path / "overlay", aliases=("Lab-X",))


def test_adds_internal_material_and_preserves_base(overlay):
    base = load_standard_measurements(overlay=None)
    merged = load_standard_measurements(overlay=overlay)
    pd.testing.assert_frame_equal(merged.iloc[:len(base)], base)
    archive = build_standard_archive(merged)
    assert archive.loc["LABX", "material"] == "rutile"
    assert archive.loc["LABX", "206Pb/238U*"] == 0.0123
    assert archive.loc["LABX", "207Pb/206Pb_i"] == 0.89
    assert set(merged.loc[merged.standard.eq("LABX"), "status"]) == {"internal_unpublished"}
    digest = hashlib.sha256()
    for name in ("library.toml", "sources.toml", "material.toml"):
        digest.update(name.encode() + b"\0")
        digest.update((overlay / name).read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    assert merged.attrs["overlay"] == dict(path=str(overlay), lab="Example lab",
        sha256=digest.hexdigest(), standards=["LABX"], replaces=[])
    assert archive.attrs["overlay"] == merged.attrs["overlay"]
    assert "labx_working" in merged.attrs["sources"]


@pytest.mark.parametrize("name,aliases,replaces", [
    ("LAB-OLD", (), "LAB-OLD"),
    ("labbase", (), "LABBASE"),
    ("LABX", ("Lab old",), "LabBase"),
])
def test_normalized_names_and_aliases_require_replacement(tmp_path, name, aliases, replaces):
    base = library(tmp_path / "base", name="LABBASE", aliases=("lab_old",),
                   source="base_source", overlay=False)
    extra = library(tmp_path / "extra", name=name, aliases=aliases)
    with pytest.raises(ValueError, match="clashes.*LABBASE") as error:
        load_standard_measurements(base, overlay=extra)
    assert name in str(error.value)
    manifest = extra / "library.toml"
    manifest.write_text(manifest.read_text().replace('replaces = []', 'replaces = ' + json.dumps([replaces])))
    merged = load_standard_measurements(base, overlay=extra)
    assert merged.standard.unique().tolist() == [name]
    assert set(merged.source) == {"Invented labx_working working record"}


@pytest.mark.parametrize("name,replaces,base_name", [("krag", "krag", "KNW"), ("knw", "KNW", "KNW")])
def test_bundled_alias_collision(tmp_path, name, replaces, base_name):
    extra = library(tmp_path / "extra", name=name)
    with pytest.raises(ValueError, match="clashes"):
        load_standard_measurements(overlay=extra)
    manifest = extra / "library.toml"
    manifest.write_text(manifest.read_text().replace('replaces = []', 'replaces = ' + json.dumps([replaces])))
    merged = load_standard_measurements(overlay=extra)
    assert base_name not in set(merged.standard)
    assert name in set(merged.standard)


@pytest.mark.parametrize("case", ["stale", "source", "unknown", "main_lab", "main_replaces", "bad_lab", "bad_replaces", "invalid_status", "invalid_unit"])
def test_rejects_invalid_overlay_and_base_metadata(tmp_path, case):
    base = library(tmp_path / "base", name="LABBASE", source="base_source", overlay=False)
    extra = library(tmp_path / "extra", source="base_source" if case == "source" else "labx_working",
                    replaces=("LABSTALE",) if case == "stale" else ())
    manifest = extra / "library.toml"
    if case in {"main_lab", "main_replaces"}:
        manifest = base / "library.toml"
        manifest.write_text(manifest.read_text() + ('lab = "Example"\n' if case == "main_lab" else 'replaces = []\n'))
    elif case == "unknown":
        manifest.write_text(manifest.read_text() + 'unexpected = true\n')
    elif case == "bad_lab":
        manifest.write_text(manifest.read_text().replace('lab = "Example lab"', 'lab = 7'))
    elif case == "bad_replaces":
        manifest.write_text(manifest.read_text().replace('replaces = []', 'replaces = "LABBASE"'))
    elif case in {"invalid_status", "invalid_unit"}:
        material = extra / "material.toml"
        material.write_text(material.read_text().replace('internal_unpublished', 'unknown_status') if case == "invalid_status"
                            else material.read_text().replace('unit = "ratio"', 'unit = "Ma"'))
    with pytest.raises(ValueError):
        load_standard_measurements(base, overlay=extra)
    if case.startswith("main_"):
        with pytest.raises(ValueError, match="unsupported fields"):
            load_standard_measurements(base, overlay=None)


@pytest.mark.parametrize("disabled", ["none", "", " NONE "])
def test_environment_disable(monkeypatch, disabled):
    monkeypatch.setenv("UPBRPY_LAB_STANDARDS", disabled)
    assert default_lab_overlay_path() is None
    assert load_standard_measurements().attrs["overlay"] == {}


def test_environment_path_and_home(monkeypatch, tmp_path, overlay):
    monkeypatch.setenv("UPBRPY_LAB_STANDARDS", str(overlay / "library.toml"))
    assert default_lab_overlay_path() == overlay / "library.toml"
    assert "LABX" in set(load_standard_measurements().standard)
    monkeypatch.delenv("UPBRPY_LAB_STANDARDS")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_lab_overlay_path() is None
    default = library(tmp_path / "UPbRPy" / "lab-standards")
    assert default_lab_overlay_path() == default
    assert "LABX" in set(load_standard_measurements().standard)


def test_provenance_without_overlay_is_unchanged(overlay):
    assert reference_library_info(overlay=None) == BASE_INFO
    info = reference_library_info(overlay=overlay)
    digest = load_standard_measurements(overlay=overlay).attrs["overlay"]["sha256"]
    assert info == {**BASE_INFO, "version": BASE_INFO["version"] + "+overlay.Example lab." + digest[:12],
                    "overlay_sha256": digest, "overlay_path": str(overlay), "overlay_lab": "Example lab"}


def test_merged_library_round_trip_and_marker(tmp_path, overlay):
    expected = build_standard_archive(load_standard_measurements(overlay=overlay))
    target = tmp_path / "merged"
    assert write_merged_library(target, overlay=overlay) == target
    standalone = load_standard_measurements(target, overlay=None)
    actual = build_standard_archive(standalone)
    # Older format-2 readers accept these original statuses only.
    from UPbRPy.standards import _load_standard_measurements
    on_disk = _load_standard_measurements(target)
    assert set(on_disk.status) <= {"provisional", "recommended"}
    assert set(entry.get("status", "recommended") for entry in standalone.attrs["sources"].values()) <= {"provisional", "recommended"}
    from UPbRPy.standards import _internal_reference_note
    internal = standalone.loc[standalone.standard.eq("LABX")]
    source = standalone.attrs["sources"]["labx_working"]
    label = _internal_reference_note(internal, source)
    assert "internal, unpublished laboratory reference value" in label
    assert "Invented working assignment retained privately." in label
    assert "no DOI" not in label
    pd.testing.assert_frame_equal(actual, expected)
    assert actual.attrs["standard_aliases"] == expected.attrs["standard_aliases"]
    assert "overlay.Example lab." in reference_library_info(target, overlay=None)["version"]
    write_merged_library(target, overlay=overlay)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("untouched")
    with pytest.raises(ValueError, match="non-empty target"):
        write_merged_library(foreign, overlay=overlay)
    assert (foreign / "keep.txt").read_text() == "untouched"
    with pytest.raises(ValueError, match="input library"):
        write_merged_library(overlay, overlay=overlay)


@pytest.mark.parametrize("manifest_path", [False, True])
def test_merged_library_same_automatic_overlay(tmp_path, monkeypatch, manifest_path):
    base = library(tmp_path / "base", name="LABBASE", source="base_source", overlay=False)
    extra = library(tmp_path / "overlay", aliases=("Lab-X",))
    original = load_standard_measurements(base, overlay=extra)
    target = write_merged_library(tmp_path / "merged", base, extra)
    marker = tomllib.loads((target / ".upbrpy-merged-library").read_text())
    assert marker == {"format": 1, "lab": "Example lab",
                      "sha256": original.attrs["overlay"]["sha256"],
                      "standards": ["LABX"], "internal_unpublished": ["LABX"]}
    monkeypatch.setenv("UPBRPY_LAB_STANDARDS", str(extra))
    path = target / "library.toml" if manifest_path else target
    loaded = load_standard_measurements(path)
    assert loaded.standard.tolist() == original.standard.tolist()
    assert set(loaded.loc[loaded.standard.eq("LABX"), "status"]) == {"internal_unpublished"}
    assert loaded.attrs["overlay"] == original.attrs["overlay"]
    assert loaded.attrs["merged_library"] == {**marker, "overlay_action": "same"}
    archive = build_standard_archive(loaded)
    assert archive.attrs["merged_library"] == loaded.attrs["merged_library"]
    assert reference_library_info(path) == loaded.attrs["reference_library"]
    version = loaded.attrs["reference_library"]["version"]
    assert version.count("+overlay.") == 1
    assert version.endswith(f"+overlay.Example lab.{marker['sha256'][:12]}")
    assert f"+sha256.{loaded.attrs['reference_library']['sha256'][:12]}" in version
    without_overlay = load_standard_measurements(path, overlay=None)
    assert without_overlay.attrs["merged_library"]["overlay_action"] == "disabled"
    assert set(without_overlay.loc[without_overlay.standard.eq("LABX"), "status"]) == {"internal_unpublished"}
    assert reference_library_info(path, overlay=None) == without_overlay.attrs["reference_library"]


def test_merged_library_changed_overlay_and_unrelated_clashes(tmp_path):
    base = library(tmp_path / "base", name="LABBASE", source="base_source", overlay=False)
    extra = library(tmp_path / "overlay", aliases=("Lab-X",))
    target = write_merged_library(tmp_path / "merged", base, extra)
    material = extra / "material.toml"
    material.write_text(material.read_text().replace("0.0123", "0.0456"))
    source_file = extra / "sources.toml"
    source_file.write_text(source_file.read_text().replace("Invented working assignment", "Invented updated assignment"))
    loaded = load_standard_measurements(target, overlay=extra)
    assert loaded.standard.unique().tolist() == ["LABBASE", "LABX"]
    assert build_standard_archive(loaded).loc["LABX", "206Pb/238U*"] == 0.0456
    assert "updated assignment" in loaded.attrs["sources"]["labx_working"]["no_doi_reason"]
    assert loaded.attrs["merged_library"]["overlay_action"] == "updated"
    expected = load_standard_measurements(base, overlay=extra)
    assert loaded.attrs["overlay"] == expected.attrs["overlay"]
    version = loaded.attrs["reference_library"]["version"]
    assert version.count("+overlay.") == 1
    assert version.endswith(f"+overlay.Example lab.{expected.attrs['overlay']['sha256'][:12]}")
    material.write_text(material.read_text().replace('["Lab-X"]', '["Lab-X", "LABBASE"]'))
    with pytest.raises(ValueError, match="clashes.*LABBASE"):
        load_standard_measurements(target, overlay=extra)
    material.write_text(material.read_text().replace('["Lab-X", "LABBASE"]', '["Lab-X"]'))
    source_file.write_text(source_file.read_text().replace("labx_working", "base_source"))
    material.write_text(material.read_text().replace("labx_working", "base_source"))
    with pytest.raises(ValueError, match="source IDs clash.*base_source"):
        load_standard_measurements(target, overlay=extra)


def test_merged_library_legacy_text_marker(tmp_path):
    base = library(tmp_path / "base", name="LABBASE", source="base_source", overlay=False)
    extra = library(tmp_path / "overlay")
    target = write_merged_library(tmp_path / "merged", base, extra)
    marker = target / ".upbrpy-merged-library"
    marker.write_text("UPbRPy merged reference library, format 2\n")
    loaded = load_standard_measurements(target, overlay=None)
    assert loaded.attrs["merged_library"] == {"overlay_action": "legacy"}
    assert loaded.attrs["overlay"] == {}
    assert set(loaded.loc[loaded.standard.eq("LABX"), "status"]) == {"provisional"}
    with pytest.raises(ValueError, match="clashes.*LABX"):
        load_standard_measurements(target, overlay=extra)
    write_merged_library(target, base, extra)
    assert tomllib.loads(marker.read_text())["format"] == 1


def test_writer_replacements_and_manifest_inputs(tmp_path):
    base = library(tmp_path / "base", name="LABBASE", source="base_source", overlay=False)
    extra = library(tmp_path / "extra", name="lab-base", replaces=("LABBASE",))
    merged = write_merged_library(tmp_path / "merged", base / "library.toml", extra / "library.toml")
    archive = build_standard_archive(load_standard_measurements(merged, overlay=None))
    assert archive.index.tolist() == ["lab-base"]
    assert archive.loc["lab-base", "206Pb/238U*"] == 0.0123


def test_internal_citations_exports_and_recorded_provenance(tmp_path, overlay, monkeypatch):
    from dataclasses import replace
    from test_export_table import synthetic_pipeline
    from UPbRPy.export_table import build_export_table, build_footnotes, write_export_xlsx
    from UPbRPy.supplement import SupplementSession, _reference_rows, _library, write_supplement_xlsx

    monkeypatch.setenv("UPBRPY_LAB_STANDARDS", str(overlay))
    pipeline = synthetic_pipeline()
    archive = build_standard_archive(load_standard_measurements())
    pipeline.base_standard_archive = archive
    pipeline.standard_archive = archive
    pipeline.config = replace(pipeline.config, primary_68_standard="LABX", primary_76_standard="LABX", primary_82_standard="LABX")
    # Cached synthetic results need no numerical reduction for source/citation checks.
    table = build_export_table(pipeline)
    notes = build_footnotes(table, pipeline)
    note = next(n.text for n in notes if n.id == "standards")
    assert "internal, unpublished laboratory reference value" in note
    assert "Invented working assignment retained privately." in note
    lab_citation = note.split("Secondary standard")[0]
    assert "no DOI" not in lab_citation and ".doi" not in lab_citation
    assert "+overlay.Example lab." in next(n.text for n in notes if n.id == "software")
    session = SupplementSession(table, tuple(notes))
    citations = [row[-1] for row in _reference_rows(session) if row[0] == "LABX"]
    assert citations and all("internal, unpublished" in text and "no DOI" not in text for text in citations[:3])
    assert "Example lab" in _library(session)
    for path in (write_export_xlsx(table, tmp_path / "export.xlsx"),
                 write_supplement_xlsx([session], tmp_path / "supplement.xlsx")):
        with zipfile.ZipFile(path) as book:
            shared = ET.fromstring(book.read("xl/sharedStrings.xml"))
            strings = ["".join(item.itertext()) for item in shared]
            assert any("internal, unpublished laboratory reference value" in text for text in strings)
            assert any("+overlay.Example lab." in text for text in strings)
            if path.name == "export.xlsx":
                sheet = book.read("xl/worksheets/sheet2.xml").decode()
                assert any(f">{i}<" in sheet for i, text in enumerate(strings) if "internal, unpublished" in text)
    digest = archive.attrs["overlay"]["sha256"]
    # The run reports the library loaded for calibration, even if the environment changes.
    monkeypatch.setenv("UPBRPY_LAB_STANDARDS", "none")
    manifest = pipeline.manifest()
    assert manifest["overlay_sha256"] == digest
    assert manifest["overlay_lab"] == "Example lab"
    assert manifest["overlay_path"] == str(overlay)


@pytest.mark.parametrize("explicit", [False, True])
def test_gui_broken_overlay_falls_back_and_reports(tmp_path, monkeypatch, explicit):
    from GUI import services
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "library.toml").write_text("invalid = [")
    monkeypatch.setenv("UPBRPY_LAB_STANDARDS", str(broken))
    monkeypatch.setattr(services, "_active_standard_archive", services._active_standard_archive.copy())
    monkeypatch.setattr(services, "_active_standard_archive_path", services._active_standard_archive_path)
    path = services.DEFAULT_REFERENCE_LIBRARY_PATH if explicit else None
    archive = services.load_standard_archive_from_path(path)
    assert archive.equals(build_standard_archive(load_standard_measurements(overlay=None)))
    assert archive.attrs["overlay"] == {}
    assert "overlay could not be loaded" in archive.attrs["overlay_error"]
    assert "overlay_sha256" not in archive.attrs["reference_library"]
    with pytest.raises(ValueError):
        load_standard_measurements()


def test_import_defaults_and_gui_startup_with_broken_overlay(tmp_path, overlay):
    env = {**os.environ, "UPBRPY_LAB_STANDARDS": str(overlay), "PYTHONDONTWRITEBYTECODE": "1"}
    python = str(Path.cwd() / ".venv/bin/python")
    good = subprocess.run([python, "-c", 'from UPbRPy.standards import standard_archive; assert "LABX" in standard_archive.index'], env=env, capture_output=True, text=True)
    assert good.returncode == 0, good.stderr
    (overlay / "library.toml").write_text("invalid = [")
    bad = subprocess.run([python, "-c", "import UPbRPy.standards"], env=env, capture_output=True, text=True)
    assert bad.returncode != 0
    gui = subprocess.run([python, "-c", 'from GUI.services import load_standard_archive_from_path; a = load_standard_archive_from_path(None); assert a.attrs["overlay_error"]'], env=env, capture_output=True, text=True)
    assert gui.returncode == 0, gui.stderr
