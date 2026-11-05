"""Studio groups corrected acquisitions by sample and marks their provenance."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.main_window import MainWindow
from GUI.services import reimport_detector_calibration
from GUI.theme import build_application
from synthetic_bundles import write_session_bundle


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def spot_items(window):
    root = window.spot_list.invisibleRootItem()
    return {
        item.data(0, QtCore.Qt.ItemDataRole.UserRole): item
        for i in range(root.childCount())
        for section in [root.child(i)]
        for j in range(section.childCount())
        for folder in [section.child(j)]
        for k in range(folder.childCount())
        for item in [folder.child(k)]
    }


def assert_corrected_tree(window):
    items = spot_items(window)
    corrected = items["KD27_4"]
    assert corrected.text(0) == "KD27_4 · DB"
    assert corrected.parent().text(0) == "SMP2"
    assert corrected.toolTip(0) == "KD27_4\nSample SMP2 from laicpmsdb (label sample KD27)"
    for label in ("AFB_3", "MAD_UCSB_2", "NIST612_1"):
        assert items[label].text(0) == label
        assert items[label].toolTip(0) == label
    assert items["AFB_3"].parent().text(0) == "AFB1"
    window.spot_list.setCurrentItem(corrected)
    assert window.session.selected_spot == "KD27_4"
    assert window.session.experiment.attrs["sample_overrides"]["KD27_4"]["sample"] == "SMP2"
    assert window.session.pipeline.manifest()["sample_overrides"] == window.session.experiment.attrs["sample_overrides"]


def test_marker_grouping_reimport_and_project_reopen(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args[1:])))
    settings = QtCore.QSettings("UPbRPy", "Studio")
    settings.clear()
    bundle = write_session_bundle(tmp_path, sample_labels={
        "KD27_4": "SMP2", "AFB_3": "AFB", "NIST612_1": "nist612",
    })
    window = MainWindow(theme_mode="light")
    try:
        window.load_experiment_directory(str(bundle), primary_channel="44Ca", background=False)
        assert window.session.loaded
        assert_corrected_tree(window)
        # The text badge must not become part of a spot's navigation identity.
        item = spot_items(window)["KD27_4"]
        window._on_spot_tree_item_renamed(item, 0)
        assert item.data(0, QtCore.Qt.ItemDataRole.UserRole) == "KD27_4"

        reimported = reimport_detector_calibration(window.session, {"mode": "qtegra"})
        window.install_loaded_session(reimported, str(bundle))
        assert_corrected_tree(window)
        assert reimported.total_data.loc[reimported.total_data["spot_name"].eq("KD27_4"), "sample"].eq("SMP2").all()

        project = tmp_path / "reviewed.upbr"
        window.save_project(project)
        assert project.is_file()
        window.load_project_file(project, background=False)
        assert_corrected_tree(window)

        old_bundle = write_session_bundle(tmp_path / "old")
        window.load_experiment_directory(str(old_bundle), primary_channel="44Ca", background=False)
        assert all(" · DB" not in item.text(0) and "laicpmsdb" not in item.toolTip(0) for item in spot_items(window).values())
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
        settings.clear()
