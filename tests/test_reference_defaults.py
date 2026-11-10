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


def test_infer_label_separator():
    from GUI.workflows import infer_label_separator

    assert infer_label_separator(["MADUCSB_778", "Tuve-gc1_107", "nothing"], " ") == "_"
    assert infer_label_separator(["MADUCSB 778", "AFB1 3"], "_") == " "
    assert infer_label_separator(["nothing", "blank"], " ") == " "


def test_apatite_secondaries_are_the_measured_reference_materials():
    measured = ["NIST612", "NIST610", "Tuve_gc1", "BR5", "AFB1", "OL2", "MADUCSB"]
    chosen = resolve_reference_standards(
        WORKFLOW_PRESETS["apatite"], measured, ["NIST612", "NIST610", "BR5", "AFB1", "OL2", "MADUCSB"])
    assert chosen["primary_68_standard"] == "MADUCSB"
    assert chosen["primary_76_standard"] == "NIST610"
    assert chosen["secondary_standards"] == ("BR5", "AFB1", "OL2")


def test_load_session_groups_underscore_labels_under_the_apatite_preset():
    pytest.importorskip("PySide6")
    from GUI.services import load_session

    session = load_session(str(FIXTURE / "Zircon_04Nov26_slice_Rich.csv"), primary_channel="29Si",
                           preset=WORKFLOW_PRESETS["apatite"])
    assert session.import_settings["sep"] == "_"
    samples = set(session.experiment["sample"])
    assert {"NIST612", "NIST610", "S513"} <= samples
    assert not any(name.startswith("NIST612_") for name in samples)
    assert "MADJCU" not in session.secondary_standards
