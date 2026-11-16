"""The spot tree uses exact reference names and library aliases."""

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.services import current_standard_archive
from GUI.theme import build_application

RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow()
    window.load_experiment_directory(str(RICH), background=False, offer_restore=False)
    assert window.session.loaded
    yield window
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_unknown_names_are_not_reference_materials(window):
    unknowns = ["PL7-QX1", "PL7-QX1 M", "AB1-CD1", "Plateau", "PLX", ""]
    assert [name for name in unknowns if window._is_reference_material(name)] == []


def test_library_names_typography_and_real_aliases_are_reference_materials(window):
    aliases = current_standard_archive().attrs["standard_aliases"]
    assert "Plesovice" in aliases["PL"]
    assert "SLZC" in aliases["LTSE"]
    assert "NIST SRM 612" in aliases["NIST612"]
    names = ["S513", "LTSE", "Seiland", "91500", "NIST610", "NIST 612", "NIST_612",
             "Plesovice", "Plešovice", "SLZC", "NIST SRM 610", "NIST SRM 612", "BIR-1G"]
    names.extend(alias for values in aliases.values() for alias in values)
    assert [name for name in names if not window._is_reference_material(name)] == []


def test_configured_standards_match_exact_normalized_names(window):
    session = window.session
    for field in ("primary_68_standard", "primary_76_standard", "primary_82_standard",
                  "secondary_standards", "concentration_standards"):
        original = getattr(session, field)
        setattr(session, field, ("Custom RM",) if isinstance(original, tuple) else "Custom RM")
        try:
            assert window._is_reference_material("CUSTOM_RM")
            assert not window._is_reference_material("Custom RM unknown")
        finally:
            setattr(session, field, original)
    preset = window.workflow_preset
    for field in ("primary_standard", "pb_ratio_standard", "th_ratio_standard",
                  "secondary_standards", "concentration_standards"):
        value = ("Preset RM",) if isinstance(getattr(preset, field), tuple) else "Preset RM"
        window.workflow_preset = replace(preset, **{field: value})
        try:
            assert window._is_reference_material("PRESET_RM")
            assert not window._is_reference_material("Preset RM unknown")
        finally:
            window.workflow_preset = preset


def test_tree_separates_unknowns_and_caches_each_sample_per_population(window, monkeypatch):
    experiment = window.session.experiment
    names = experiment["spot_names"].astype(str).tolist()
    sample_map = {name: "PL7-QX1" if index % 2 else "91500"
                  for index, name in enumerate(names)}
    experiment["sample"] = experiment["spot_names"].map(sample_map)
    data = window.session.total_data
    data["sample"] = data["spot_name"].map(sample_map)
    calls = Counter()
    original = window._is_reference_material

    def classify(sample):
        calls[sample] += 1
        return original(sample)

    monkeypatch.setattr(window, "_is_reference_material", classify)
    for population in (1, 2):
        window._populate_spot_tree()
        roots = {window.spot_list.topLevelItem(i).text(0): window.spot_list.topLevelItem(i)
                 for i in range(window.spot_list.topLevelItemCount())}
        for root_name, sample in (("Reference Materials", "91500"), ("Unknowns", "PL7-QX1")):
            root = roots[root_name]
            assert root.childCount() == 1
            folder = root.child(0)
            assert folder.text(0) == sample
            expected = {name for name, value in sample_map.items() if value == sample}
            assert {folder.child(i).data(0, QtCore.Qt.ItemDataRole.UserRole)
                    for i in range(folder.childCount())} == expected
        assert calls == {"91500": population, "PL7-QX1": population}
