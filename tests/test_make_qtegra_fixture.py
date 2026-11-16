"""The demo generator preserves instrument cells and defaults to RM-only data."""

from hashlib import sha256
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/qtegra/Zircon_demo"
spec = importlib.util.spec_from_file_location("make_qtegra_fixture", ROOT / "scripts/make_qtegra_fixture.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_default_fixture_reproduces_every_byte(tmp_path):
    out = tmp_path / "demo"
    assert fixture.main([str(FIXTURE), "--out", str(out)]) == 0
    expected = {p.relative_to(FIXTURE): p.read_bytes() for p in FIXTURE.rglob("*.csv")}
    actual = {p.relative_to(out): p.read_bytes() for p in out.rglob("*.csv")}
    assert actual == expected
    # A second run must also be deterministic, without leaving stale exports.
    assert fixture.main([str(FIXTURE), "--out", str(out)]) == 0
    assert {p.relative_to(out): p.read_bytes() for p in out.rglob("*.csv")} == expected


def test_fixture_selection_and_metadata():
    rich = FIXTURE / "Zircon_demo_Rich.csv"
    bom, lines, newline = fixture.read_lines(rich)
    assert bom == "\ufeff" and newline == "\r\n"
    assert lines[1].split(",")[4:-1] == list(fixture.DEFAULT_LABELS)
    assert all(label.rsplit("_", 1)[0] in fixture.KNOWN_REFERENCE_MATERIALS
               for label in fixture.DEFAULT_LABELS)
    assert {line.split(",")[0] for line in lines[2:]} == {
        "MainRuns", "TuneSettings", "StatusReport", "Info", "StartTime",
        "StopTime", "BrigidMS", "PMIAnalyteLaser",
    }
    for path in FIXTURE.rglob("*.csv"):
        raw = path.read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b"")
    # Source-file BOMs are preserved: Rich has one, the per-spot exports do not.
    assert all(not p.read_bytes().startswith(b"\xef\xbb\xbf")
               for p in (FIXTURE / "zircon_demo").glob("*.csv"))


def test_selected_labels_keep_acquisition_order_and_neutral_names(tmp_path):
    out = tmp_path / "demo"
    assert fixture.main([str(FIXTURE), "--out", str(out), "--name", "RM_example",
                         "--rich", "Zircon_demo_Rich.csv",
                         "--labels", "PL_837,NIST612_896", "--channels", "206Pb,238U"]) == 0
    _, lines, _ = fixture.read_lines(out / "RM_example_Rich.csv")
    assert lines[1].split(",")[4:-1] == ["NIST612_896", "PL_837"]
    assert {p.name for p in (out / "rm_example").iterdir()} == {"RM_example_1.csv", "RM_example_2.csv"}
    assert {line.split(",")[2] for line in lines if line.startswith("MainRuns,")} == {"206Pb", "238U"}
    assert (out / "rm_example/RM_example_2.csv").read_bytes().startswith(b"PL_837:")


@pytest.mark.parametrize("options", [
    ["--labels", "SMP1_1"],
    ["--labels", "NIST612_896,NIST612_896"],
    ["--labels", ""],
    ["--labels", "91500_999999"],
    ["--channels", "missing"],
    ["--name", "../unsafe"],
])
def test_invalid_selection_does_not_write(tmp_path, options):
    out = tmp_path / "demo"
    with pytest.raises(SystemExit) as error:
        fixture.main([str(FIXTURE), "--out", str(out), *options])
    assert error.value.code == 2
    assert not out.exists()


def test_non_reference_requires_explicit_opt_in(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    # Synthetic sample label; the measurements remain reference-material data.
    rich = (FIXTURE / "Zircon_demo_Rich.csv").read_bytes().replace(b"NIST612_896", b"SMP1_1")
    (source / "session_Rich.csv").write_bytes(rich)
    spot = (FIXTURE / "zircon_demo/Zircon_demo_1.csv").read_bytes().replace(b"NIST612_896", b"SMP1_1")
    (source / "spot.csv").write_bytes(spot)
    out = tmp_path / "out"
    args = [str(source), "--out", str(out), "--labels", "SMP1_1"]
    with pytest.raises(SystemExit) as error:
        fixture.main(args)
    assert error.value.code == 2 and not out.exists()
    assert fixture.main([*args, "--allow-non-reference"]) == 0
    assert (out / "zircon_demo/Zircon_demo_1.csv").read_bytes() == spot


def test_missing_source_and_ambiguous_rich_are_rejected(tmp_path):
    with pytest.raises(SystemExit):
        fixture.main([])
    with pytest.raises(SystemExit):
        fixture.main([str(tmp_path / "missing")])
    for name in ("first_Rich.csv", "second_Rich.csv"):
        (tmp_path / name).write_bytes((FIXTURE / "Zircon_demo_Rich.csv").read_bytes())
    with pytest.raises(SystemExit):
        fixture.main([str(tmp_path)])


def test_source_and_unrelated_destination_are_protected(tmp_path):
    with pytest.raises(SystemExit):
        fixture.main([str(FIXTURE), "--out", str(FIXTURE / "nested")])
    (tmp_path / "keep.txt").write_text("keep")
    with pytest.raises(SystemExit):
        fixture.main([str(FIXTURE), "--out", str(tmp_path)])
    assert (tmp_path / "keep.txt").read_text() == "keep"


def test_original_eight_reference_spots_are_unchanged():
    lines = (FIXTURE / "Zircon_demo_Rich.csv").read_bytes().splitlines(keepends=True)
    columns = b"".join(b",".join(line.rstrip(b"\r\n").split(b",")[:12]) + b"\r\n" for line in lines)
    assert sha256(columns).hexdigest() == "d4d5473ecbfdf0ae1fa83e2137187ea39d9b68c56b87b7985666319498c30359"
    expected = [
        "8f5b9d98b7f5870cea97ca69c62e10dd11cea082fa11b5e580a8e0fc5d670a5f",
        "edcbe2173010d1b8b5064097f27cda3da096361a11c8ed724792b105a8652410",
        "81d34c62dad96026598fb6d87c5b446a83b16d7b3260b2ebcb956603027ee0cc",
        "193f53e14a2ec8953a080d4b852fa7ae79b7bc92139954829d5b6b4a220ac0ae",
        "3109ac133e6ac6ae42d677d4775369ba2b9528eb549be0a6f2e297c9f6c72546",
        "d27b9c31f4c9eaffe02685ebe9e948c22da92f687749f3027817cf6876a132db",
        "d7eac91eaf0fff4c570c1d14a24f5fc173fddd3fee8cec1058bc769ac156c12b",
        "a4506a4b638127046383312e318f249c5da9279bf82c24e44d4cc6a8ddde9e3a",
    ]
    for index, digest in enumerate(expected, 1):
        assert sha256((FIXTURE / "zircon_demo" / f"Zircon_demo_{index}.csv").read_bytes()).hexdigest() == digest
