"""Studio import, detector controls and project replay on trimmed Qtegra exports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from qtegra_helpers import write_y_only_csv
from GUI import services
from GUI.launch import parse_open_target
from GUI.main_window import MainWindow
from GUI.theme import build_application
from GUI.workflows import infer_workflow_preset
from UPbRPy.pipeline import PipelineStage
from UPbRPy.reproducibility import build_reproducibility_notebook, selection_export_frame


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
RICH = FIXTURE / "Zircon_demo_Rich.csv"
FORMATS = [(RICH, "rich", "Rich"), (FIXTURE / "zircon_demo", "spot_folder", "Per-spot folder"),
           (FIXTURE, "rich+spot_folder", "Rich + per-spot")]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture
def window(app, monkeypatch):
    QtCore.QSettings("UPbRPy", "Studio").clear()
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    window = MainWindow(theme_mode="light")
    window.show()
    yield window
    assert wait_for(app, lambda: window._active_worker is None)
    window.has_unsaved_changes = lambda: False
    window.close()
    app.processEvents()
    QtCore.QSettings("UPbRPy", "Studio").clear()
    assert not errors, errors


def wait_for(app, condition):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("source,format_name,badge", FORMATS, ids=[v[1] for v in FORMATS])
def test_service_opens_source_once(source, format_name, badge, monkeypatch):
    opened = []
    reader = services.open_qtegra

    def record(path, **kwargs):
        result = reader(path, **kwargs)
        opened.append(result)
        return result

    monkeypatch.setattr(services, "open_qtegra", record)
    monkeypatch.setattr("UPbRPy.io.open_qtegra", lambda *a, **k: pytest.fail("Reader reopened sweeps"))
    session = services.load_session(str(source))
    assert session.loaded and len(session.spot_names) == 12
    assert len(opened) == 1 and session.qtegra is opened[0]
    assert session.qtegra.format == format_name
    assert session.pipeline.input_files == tuple(s.path for s in session.qtegra.sources)
    assert sorted(services.raw_input_files(source)) == sorted(services.raw_input_files(session.qtegra))


@pytest.mark.parametrize("source,format_name,badge", FORMATS, ids=[v[1] for v in FORMATS])
def test_window_formats_and_capabilities(window, app, source, format_name, badge):
    window.load_experiment_directory(str(source), background=False)
    app.processEvents()
    assert window.session.loaded
    assert window.capabilities_badge.isVisible()
    assert window.capabilities_badge.text() == badge
    tooltip = window.capabilities_badge.toolTip()
    for item in window.session.qtegra.sources:
        assert str(item.path) in tooltip
    for warning in window.session.qtegra.warnings:
        assert warning in tooltip
    assert all(capability in tooltip for capability in window.session.qtegra.capabilities)
    if format_name == "rich+spot_folder":
        assert "+7 h" in tooltip
    panel = window.detector_calibration_panel
    capable = format_name.startswith("rich")
    assert panel.isEnabled() == capable
    if capable:
        assert panel.channel_combo.count() and panel.channel_combo.currentText() == "238U"
        assert panel.table.rowCount() == panel.channel_combo.count()
        assert len(panel.plot.listDataItems()) == 2
        assert len([item for item in panel.plot.items() if isinstance(item, pg.InfiniteLine)]) >= 1
    else:
        assert panel.reason_label.text() == "Needs a Rich export: analog and counter readings"
        assert not panel.apply_button.isEnabled()
    assert source.name in window.windowTitle()


def apply_mode(window, app, mode):
    panel = window.detector_calibration_panel
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData(mode))
    assert panel.apply_button.isEnabled()
    panel.apply_button.click()
    assert window._active_worker is not None
    assert wait_for(app, lambda: window._active_worker is None)
    assert window.session.import_settings["detector_calibration"]["mode"] == mode


def test_fractionation_is_first_and_default_calibration_tab(window, app):
    window.load_experiment_directory(str(RICH), background=False)
    window.inspector_tabs.setCurrentWidget(window.calibration_tabs)
    app.processEvents()
    assert [window.calibration_tabs.tabText(i) for i in range(2)] == ["Fractionation", "Detector"]
    assert window.calibration_tabs.currentIndex() == 0


@pytest.mark.parametrize("mode", ["qtegra", "constant", "drift"])
def test_detector_layout_and_table_at_1280x800(window, app, mode):
    window.load_experiment_directory(str(FIXTURE), background=False)
    window.resize(1280, 800)
    window.apply_default_layout()
    window.inspector_tabs.setCurrentWidget(window.calibration_tabs)
    window.calibration_tabs.setCurrentIndex(1)
    panel = window.detector_calibration_panel
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData(mode))
    for _ in range(8):
        app.processEvents()
    assert (window.width(), window.height()) == (1280, 800)
    assert panel.layout().itemAt(0).widget().y() <= 24
    assert panel.basis_combo.y() - panel.mode_combo.geometry().bottom() <= 8
    assert panel.channel_combo.y() - panel.basis_combo.geometry().bottom() <= 8
    assert panel.plot.height() >= 220
    assert panel.plot_caption.y() >= panel.plot.geometry().bottom()
    assert panel.table.y() >= panel.plot_caption.geometry().bottom()

    table = panel.table
    assert not table.horizontalScrollBar().isVisible()
    assert table.horizontalScrollBar().maximum() == 0
    header = table.horizontalHeader()
    assert header.sectionResizeMode(2) == QtWidgets.QHeaderView.ResizeMode.Stretch
    assert header.sectionViewportPosition(3) + header.sectionSize(3) <= table.viewport().width()
    for column in range(4):
        assert table.columnWidth(column) >= table.sizeHintForColumn(column)
    for row in range(table.rowCount()):
        assert re.fullmatch(r"\d{1,3}(?:,\d{3})+ ± \d{1,3}(?:,\d{3})*", table.item(row, 2).text())
        if table.item(row, 3).text() != "—":
            assert re.fullmatch(r"[+-]\d+\.\d", table.item(row, 3).text())
    for channel in ("238U", "29Si"):
        panel.channel_combo.setCurrentText(channel)
        selected = table.selectionModel().selectedRows()
        assert len(selected) == 1
        assert table.item(selected[0].row(), 0).text() == channel


@pytest.mark.parametrize("mode,label", [("constant", "Session constant"), ("drift", "Time drift"),
                                        ("qtegra", "Qtegra factors")])
def test_apply_reports_result_for_plotted_channel(window, app, mode, label):
    window.load_experiment_directory(str(FIXTURE), background=False)
    panel = window.detector_calibration_panel
    panel.channel_combo.setCurrentText("29Si")
    apply_mode(window, app, mode)
    message = window.statusBar().currentMessage()
    if mode == "qtegra":
        assert message == "Detector calibration: Qtegra factors"
    else:
        row = panel.table.selectionModel().selectedRows()[0].row()
        factor = panel.table.item(row, 2).text().split(" ± ")[0]
        difference = panel.table.item(row, 3).text()
        assert message == f"Applied detector cross-calibration ({label}): 29Si {factor}, {difference} % vs Qtegra"


def test_apply_preserves_windows_and_invalidates_both_caches(window, app, monkeypatch):
    window.load_experiment_directory(str(RICH), background=False)
    original = window.session
    raw_y = [a.y.copy() for a in original.qtegra.acquisitions]
    # Deliberately reorder rows and review a window to exercise matching by name.
    original.experiment = original.experiment.iloc[::-1].reset_index(drop=True)
    original.experiment.loc[0, "plateau_start_times"] += 0.25
    selections = selection_export_frame(original.experiment).copy()
    original.reduction_cache["baseline_subtracted_total_data"] = object()
    original.pipeline.cache.set(PipelineStage.BASELINE, object())
    original.selected_spot = str(original.experiment.spot_names.iloc[0])
    window._select_spot_in_tree(original.selected_spot)
    monkeypatch.setattr(services, "open_qtegra", lambda *a, **k: pytest.fail("Re-import reopened source"))
    apply_mode(window, app, "constant")
    updated = window.session
    assert updated.qtegra is original.qtegra
    assert updated.pipeline.config.detector_calibration == updated.import_settings["detector_calibration"]
    assert updated.pipeline.experiment.attrs["detector_calibration"]["mode"] == "constant"
    pd.testing.assert_frame_equal(
        selection_export_frame(updated.experiment).sort_values("spot_names").reset_index(drop=True),
        selections.sort_values("spot_names").reset_index(drop=True),
    )
    assert updated.selected_spot == original.selected_spot
    assert not updated.reduction_cache
    assert updated.pipeline.cache.get(PipelineStage.BASELINE) is None
    assert not updated.total_data.equals(original.total_data)
    for before, acquisition in zip(raw_y, original.qtegra.acquisitions):
        np.testing.assert_array_equal(acquisition.y, before)
    apply_mode(window, app, "qtegra")
    pd.testing.assert_frame_equal(window.session.total_data, original.total_data)


def test_primary_basis_uses_current_206_238_standard(window, app):
    window.load_experiment_directory(str(RICH), background=False)
    window.on_primary_68_standard_changed("S513")
    panel = window.detector_calibration_panel
    assert panel.basis_combo.itemText(1) == "Primary standard (S513)"
    panel.basis_combo.setCurrentIndex(1)
    assert panel.channel_combo.currentText() == "238U"
    fit = panel._drift.channels["238U"]
    assert fit.acquisition_labels and all(label.startswith("S513_") for label in fit.acquisition_labels)
    apply_mode(window, app, "drift")
    choice = window.session.import_settings["detector_calibration"]
    assert choice["on"] == "primary_standard" and choice["primary_standard"] == "S513"
    assert window.session.pipeline.config.detector_calibration == choice


@pytest.mark.parametrize("mode", ["constant", "drift"])
def test_rich_project_round_trip(window, app, tmp_path, mode):
    window.load_experiment_directory(str(RICH), background=False)
    window.session.experiment.loc[0, "plateau_start_times"] += 0.125
    apply_mode(window, app, mode)
    before = window.session
    path = tmp_path / "rich.upbr"
    window.save_project(path)
    assert path.is_file()
    saved = json.loads(path.read_text())
    assert saved["pipeline"]["config"]["detector_calibration"]["mode"] == mode
    restored = services.restore_session_from_project(path)
    assert Path(restored.session.directory) == RICH
    pd.testing.assert_frame_equal(restored.session.total_data, before.total_data)
    pd.testing.assert_frame_equal(selection_export_frame(restored.session.experiment), selection_export_frame(before.experiment))
    window.load_project_file(str(path), background=False)
    assert window.session.qtegra.format == "rich"
    assert window.detector_calibration_panel.mode_combo.currentData() == mode
    assert window.session.pipeline.config.detector_calibration["mode"] == mode


def test_csv_dialog_and_launch_route_to_existing_loader(window, monkeypatch):
    calls = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *args: calls.append(args) or (str(RICH), ""))
    opened = []
    monkeypatch.setattr(window, "load_experiment_directory", lambda path: opened.append(path))
    window.choose_qtegra_file()
    assert opened == [str(RICH)]
    assert calls[0][-1] == "Qtegra exports (*.csv);;All files (*)"
    target = parse_open_target(RICH)
    assert target.kind == "directory" and target.path == RICH.resolve()
    assert parse_open_target(RICH.as_uri()).path == RICH.resolve()


def test_file_source_manifest_origin_and_workflow(tmp_path):
    from synthetic_bundles import write_session_bundle

    root = write_session_bundle(tmp_path)
    source = root / "raw" / "export_Rich.csv"
    source.write_bytes(RICH.read_bytes().replace(b"S513_", b"MAD_UCSB_"))
    (root / ".upbrpy_bundle.json").write_text(json.dumps({
        "server": "http://127.0.0.1:1", "session_id": "fixture-session", "session_key": root.name,
    }))
    session = services.load_session(str(source), primary_channel="29Si")
    assert session.workflow_key == infer_workflow_preset(str(source)).key == "apatite"
    assert session.origin and session.origin == services.bundle_origin(root / "raw")
    assert session.experiment.attrs["qtegra"]["format"] == "rich"
    assert set(session.experiment.loc[session.experiment.spot_names.str.startswith("MAD_UCSB_"), "sample"]) == {"MADUCSB"}


@pytest.mark.parametrize("source", [RICH, FIXTURE])
def test_notebook_replays_exact_file_provenance(source):
    session = services.load_session(str(source))
    notebook = build_reproducibility_notebook(session.pipeline)
    namespace = {}
    for cell in notebook["cells"][:7]:
        if cell["cell_type"] == "code":
            exec("".join(cell["source"]), namespace)
    assert namespace["pipeline"].input_files == session.pipeline.input_files
    assert namespace["pipeline"].config.detector_calibration == session.pipeline.config.detector_calibration


@pytest.mark.parametrize("background", [False, True])
def test_open_qtegra_file_shows_missing_time_error(window, app, monkeypatch, tmp_path, background):
    source = write_y_only_csv(RICH, tmp_path / "Y_only.csv")
    messages = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: messages.append(args[1:]))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *args: (str(source), ""))
    if not background:
        loader = window.load_experiment_directory
        monkeypatch.setattr(window, "load_experiment_directory", lambda path: loader(path, background=False))
    window.choose_qtegra_file()
    assert wait_for(app, lambda: window._active_worker is None)
    assert not messages
    assert not window.issue_banner.isHidden()
    message = window.issue_banner.message.text()
    assert window.issue_banner.stage == "import"
    assert message.startswith("Import: Unsupported Qtegra export: no time information")
    assert source.name in message
    assert "Export the Rich single CSV" in message
    assert not window.session.loaded
