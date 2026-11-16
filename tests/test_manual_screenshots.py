"""Privacy and byte preservation of the documentation screenshot inputs."""

import csv
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.make_manual_screenshots import (
    DEFAULT_SOURCE, alphabetic_number, copy_neutral_source, label_replacements,
)


ROOT = Path(__file__).resolve().parents[1]


def test_labels_follow_source_order_keep_suffixes_and_library_aliases():
    labels = ["Unlisted_S_12", "NIST612_896", "Another_2", "Unlisted_S_13",
              "PL_836", "S513_628", "91500_738", "LTSE_788", "Seiland_1",
              "Plesovice_2", "Unlisted_S_14"]
    mapped = label_replacements(labels)
    assert mapped["Unlisted_S_12"] == "Sample A_S_12"
    assert mapped["Unlisted_S_13"] == "Sample A_S_13"
    assert mapped["Another_2"] == "Sample B_2"
    for label in labels:
        if label.startswith(("NIST", "PL_", "S513", "91500", "LTSE", "Seiland", "Plesovice")):
            assert mapped[label] == label
    assert [alphabetic_number(n) for n in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"]


def test_reference_only_fixture_copy_is_byte_identical(tmp_path):
    target = copy_neutral_source(DEFAULT_SOURCE, tmp_path / "Zircon_demo")
    assert target.name == "Zircon_demo_Rich.csv"
    assert target.read_bytes() == DEFAULT_SOURCE.read_bytes()


def test_every_label_cell_and_spot_first_line_rewritten_only(tmp_path, capsys):
    source = tmp_path / "private-input"
    source.mkdir()
    original = DEFAULT_SOURCE.read_bytes()
    # Use actual fixture measurements, changing label text only for this test.
    before = original.replace(b"S513_628", b"Unlisted_S_12").replace(b"S513_629", b"Unlisted_S_13")
    (source / "input.csv").write_bytes(before)
    companion = source / "companion"
    companion.mkdir()
    files = sorted((DEFAULT_SOURCE.parent / "zircon_demo").glob("*.csv"))
    source_spot = next(p for p in files if p.read_bytes().splitlines()[0].startswith(b"S513_628:"))
    spot_bytes = source_spot.read_bytes().replace(b"S513_628", b"Unlisted_S_12")
    (companion / "private-name.csv").write_bytes(spot_bytes)
    target = copy_neutral_source(source, tmp_path / "Zircon_demo")
    expected = before.replace(b"Unlisted_S_12", b"Sample A_S_12").replace(b"Unlisted_S_13", b"Sample A_S_13")
    assert (target / "Zircon_demo_Rich.csv").read_bytes() == expected
    rewritten_spot, = (target / "zircon_demo").glob("*.csv")
    assert rewritten_spot.name == "Zircon_demo_0001.csv"
    assert rewritten_spot.read_bytes() == spot_bytes.replace(b"Unlisted_S_12", b"Sample A_S_12")
    assert (source / "input.csv").read_bytes() == before
    assert (companion / "private-name.csv").read_bytes() == spot_bytes
    assert capsys.readouterr().out == ""
    rows = list(csv.reader((target / "Zircon_demo_Rich.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert "Sample A_S_12" in rows[1]
    assert any(row[:2] == ["BrigidMS", "Label"] and "Sample A_S_12" in row for row in rows)


def test_reject_ambiguous_rich_inputs_without_printing_names(tmp_path, capsys):
    (tmp_path / "one.csv").write_bytes(DEFAULT_SOURCE.read_bytes())
    (tmp_path / "two.csv").write_bytes(DEFAULT_SOURCE.read_bytes())
    with pytest.raises(ValueError, match="exactly one Rich"):
        copy_neutral_source(tmp_path, tmp_path / "output")
    assert capsys.readouterr().out == ""


def test_isolation_precedes_studio_imports_in_fresh_process(tmp_path):
    # A new interpreter is required: the rest of the GUI suite already owns
    # an application and its isolated settings. Never replace those in-process.
    code = '''
from pathlib import Path
import sys
from scripts.make_manual_screenshots import isolate
assert 'GUI.main_window' not in sys.modules
isolate(Path(sys.argv[1]))
from PySide6 import QtCore
prefs = QtCore.QSettings('UPbRPy', 'Studio')
assert prefs.allKeys() == []
assert str(Path(sys.argv[1])) in prefs.fileName()
assert QtCore.QSettings('Other', 'Application').allKeys() == []
from GUI.results_cache import enabled
assert not enabled()
for name in ('requests', 'keyring'):
    try:
        __import__(name)
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError('Network dependency was imported')
import socket
try:
    socket.create_connection(('upbdb.example.org', 443))
except RuntimeError as error:
    assert 'Network disabled' in str(error)
else:
    raise AssertionError('Network was not blocked')
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scale", [1, 2])
def test_fixture_capture_is_offscreen_and_reports_unavailable_stages(tmp_path, scale):
    from PIL import Image

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/make_manual_screenshots.py"),
         "--out", str(tmp_path), "--scale", str(scale)],
        cwd=ROOT, env={**os.environ, "QT_QPA_PLATFORM": "offscreen",
                       "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((tmp_path / "captures.json").read_text())
    assert manifest["missing_channels"] == ["202Hg"]
    assert "09-spot-concordia" in manifest["unavailable"]
    assert len(manifest["images"]) == 9
    for name in manifest["images"]:
        path = tmp_path / name
        assert path.stat().st_size <= 410_000
        with Image.open(path) as png:
            logical = (1280, 800) if "1280x800" in name else (1440, 900)
            assert png.size == tuple(d * scale for d in logical)
            assert not any(key.lower() in {"comment", "description", "source"} for key in png.info)
