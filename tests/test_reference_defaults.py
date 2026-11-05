"""Studio's default reference materials: S513 is the zircon primary whenever measured."""

from pathlib import Path

import pytest

from GUI.workflows import WORKFLOW_PRESETS, resolve_reference_standards

FIXTURE = Path(__file__).parent / "fixtures" / "qtegra" / "Zircon_04Nov26_slice"


def test_zircon_prefers_s513_when_measured():
    chosen = resolve_reference_standards(WORKFLOW_PRESETS["zircon"], ["NIST612", "s513", "PL", "91500"])
    assert chosen["primary_68_standard"] == "S513"
    assert chosen["primary_76_standard"] == "S513"
    assert chosen["primary_82_standard"] == "S513"
    assert "PL" in chosen["secondary_standards"]
    assert "S513" not in chosen["secondary_standards"]


def test_zircon_falls_back_to_pl():
    chosen = resolve_reference_standards(WORKFLOW_PRESETS["zircon"], ["NIST612", "PL", "91500"])
    assert (chosen["primary_68_standard"], chosen["primary_76_standard"], chosen["primary_82_standard"]) == (
        "PL", "PL", "PL")
    assert "PL" not in chosen["secondary_standards"]


def test_other_presets_unchanged():
    apatite = WORKFLOW_PRESETS["apatite"]
    chosen = resolve_reference_standards(apatite, ["MADUCSB", "NIST610"])
    assert chosen["primary_68_standard"] == apatite.primary_standard
    assert chosen["primary_76_standard"] == apatite.pb_ratio_standard
    assert chosen["secondary_standards"] == apatite.secondary_standards


def test_load_session_picks_s513_from_the_rich_fixture():
    pytest.importorskip("PySide6")
    from GUI.services import load_session

    session = load_session(str(FIXTURE / "Zircon_04Nov26_slice_Rich.csv"), preset=WORKFLOW_PRESETS["zircon"])
    assert session.primary_68_standard == "S513"
    assert session.primary_82_standard == "S513"
