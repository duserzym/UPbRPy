"""Rich-first import actions, companion discovery, local drops and recents."""

import json
from pathlib import Path
import shutil

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application
from UPbRPy.qtegra import companion_folder, open_qtegra

ROOT = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
RICH = ROOT / "Zircon_demo_Rich.csv"
SPOTS = ROOT / "zircon_demo"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture
def window(app, monkeypatch, qt_settings_folder):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    QtCore.QSettings("UPbRPy", "Studio").clear()
    window = MainWindow()
    yield window
    if isValid(window):
        window.has_unsaved_changes = lambda: False
        window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def rich_alone(tmp_path):
    folder = tmp_path / "dir"
    folder.mkdir()
    target = folder / RICH.name
    shutil.copyfile(RICH, target)
    return target


def test_import_actions_shortcuts_and_choosers(window, monkeypatch, tmp_path):
    source = rich_alone(tmp_path)
    calls, opened = [], []
    monkeypatch.setattr(window, "load_experiment_directory", opened.append)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *args: calls.append(("file", args)) or (str(source), ""))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        lambda *args: calls.append(("folder", args)) or str(source.parent))
    assert window.import_rich_action.text() == "Import Rich CSV…"
    assert window.import_rich_action.shortcut() == QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Open)
    window.import_rich_action.trigger()
    assert [kind for kind, _ in calls] == ["file"]
    assert calls[0][1][-1] == "Qtegra Rich CSV (*.csv);;All files (*)"
    assert opened == [str(source)]
    folder_action = next(a for a in window.findChildren(QtGui.QAction) if a.text() == "Import folder…")
    assert folder_action.shortcut() == QtGui.QKeySequence("Ctrl+Shift+O")
    folder_action.trigger()
    assert [kind for kind, _ in calls] == ["file", "folder"]
    assert opened[-1] == str(source.parent)
    project = next(a for a in window.findChildren(QtGui.QAction) if a.text() == "Open Project…")
    assert project.shortcut() == QtGui.QKeySequence("Ctrl+Alt+O")
    assert window.import_button.text() == window.step_primary_button.text() == "Import Rich CSV…"
    for button in (window.import_button, window.empty_import_button, window.step_primary_button):
        button.click()
    assert [kind for kind, _ in calls][-3:] == ["file"] * 3


def test_file_and_folder_imports_match_without_companions(window, tmp_path, monkeypatch):
    source = rich_alone(tmp_path)
    loader = window.load_experiment_directory
    monkeypatch.setattr(window, "load_experiment_directory", lambda path: loader(path, background=False))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *args: (str(source), ""))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *args: str(source.parent))
    window.import_rich_csv()
    first = window.session
    window.choose_experiment_directory()
    second = window.session
    assert first.loaded and second.loaded
    assert first.spot_names == second.spot_names
    assert first.channels == second.channels
    for channel in ["238U", "206Pb", "29Si"]:
        np.testing.assert_array_equal(first.total_data[channel], second.total_data[channel])
    assert companion_folder(source) is None


@pytest.mark.parametrize("nested", [False, True])
def test_companion_folder_matches_engine_discovery(tmp_path, nested):
    source = rich_alone(tmp_path)
    target = source.parent / "spots" if nested else source.parent
    target.mkdir(exist_ok=True)
    for spot in SPOTS.glob("*.csv"):
        shutil.copyfile(spot, target / spot.name)
    assert companion_folder(source) == target
    assert open_qtegra(source.parent).format == "rich+spot_folder"
    assert open_qtegra(source).format == "rich"
    other = source.parent / "second"
    other.mkdir()
    spot = next(SPOTS.glob("*.csv"))
    shutil.copyfile(spot, other / spot.name)
    with pytest.raises(ValueError, match="Ambiguous"):
        companion_folder(source)
    with pytest.raises(ValueError, match="Ambiguous"):
        open_qtegra(source.parent)


@pytest.mark.parametrize("include", [True, False])
def test_companion_offer_routes_and_remembers(window, monkeypatch, include):
    prompts, opened = [], []

    def answer(dialog):
        prompts.append(dialog.text())
        text = "Include" if include else "Rich CSV only"
        dialog._test_clicked = next(button for button in dialog.buttons() if button.text() == text)
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", answer)
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton", lambda dialog: dialog._test_clicked)
    monkeypatch.setattr(window, "load_experiment_directory", opened.append)
    window.import_csv_path(str(RICH))
    window.import_csv_path(str(RICH))
    assert len(prompts) == 1
    assert prompts[0] == f"Include the per-spot files in {SPOTS} (dwell time, cross-calibration)?"
    assert opened == [str(RICH.parent if include else RICH)] * 2
    assert "companion" not in window.settings.allKeys()


def test_non_rich_detection_preserves_loader(window, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(window, "load_experiment_directory", opened.append)
    spot = next(SPOTS.glob("*.csv"))
    window.import_csv_path(str(spot))
    assert window.statusBar().currentMessage() == "Detected a per-spot Qtegra CSV, not a Rich export"
    assert opened == [str(spot)]
    assert companion_folder(spot) is None
    unknown = tmp_path / "unknown.csv"
    unknown.write_text("something,else\n")
    window.import_csv_path(str(unknown))
    assert "unknown CSV format" in window.log_view.toPlainText()
    assert opened[-1] == str(unknown)
    y_only = tmp_path / "Y_only.csv"
    y_only.write_text("".join(line for line in RICH.read_text(encoding="utf-8-sig").splitlines(True)
                              if not line.startswith("StartTime,") and
                              not (line.startswith("MainRuns,") and ",Time," in line)))
    window.import_csv_path(str(y_only))
    assert "Y-only UPbR CSV, not a Rich export" in window.log_view.toPlainText()
    assert opened[-1] == str(y_only)


def drop(widget, paths):
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(path)) for path in paths])
    event = QtGui.QDropEvent(QtCore.QPointF(40, 40), QtCore.Qt.DropAction.CopyAction,
                            mime, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)
    return event


@pytest.mark.parametrize("studio", [False, True])
def test_local_drops_route_to_loaders(window, app, monkeypatch, tmp_path, studio):
    source = rich_alone(tmp_path)
    shell = StudioWindow(reduction_factory=lambda: window) if studio else window
    sources, projects, bundles = [], [], []
    monkeypatch.setattr(window, "load_experiment_directory", sources.append)
    monkeypatch.setattr(window, "load_project_file", projects.append)
    database = type("Database", (), {"cache_root": None,
        "run": lambda self, calculation, callback, **kwargs: bundles.append(kwargs["title"])})()
    monkeypatch.setattr(window, "database", lambda: database)
    try:
        assert drop(shell, [source.parent, source]).isAccepted()
        assert sources == [str(source.parent), str(source)]
        assert "Detected a Rich" in window.log_view.toPlainText()
        project = tmp_path / "project.upbr"
        project.touch()
        assert drop(shell, [project]).isAccepted()
        assert projects == [str(project)]
        bundle = tmp_path / "bundle.zip"
        bundle.touch()
        assert drop(shell, [bundle]).isAccepted()
        assert bundles == ["Open session bundle"]
        ignored = tmp_path / "ignore.txt"
        ignored.touch()
        assert not drop(shell, [ignored]).isAccepted()
    finally:
        if studio:
            shell.close()
            app.processEvents()


def test_recent_sources_are_isolated_recorded_bounded_and_clear(window, tmp_path, qt_settings_folder, monkeypatch):
    source = rich_alone(tmp_path)
    window.load_experiment_directory(str(source), background=False)
    assert window._recent_sources() == [{"kind": "csv", "path": str(source)}]
    project = tmp_path / "saved.upbr"
    window.save_project(project)
    assert window._recent_sources()[0] == {"kind": "project", "path": str(project)}
    window.load_experiment_directory(str(source.parent), background=False)
    assert window._recent_sources()[0] == {"kind": "folder", "path": str(source.parent)}
    assert Path(window.settings.fileName()).is_relative_to(qt_settings_folder)
    for i in range(12):
        window._remember_source(tmp_path / f"missing{i}.csv", "csv")
    window._rebuild_recent_sources()
    records = window._recent_sources()
    assert len(records) == 10
    assert records[0]["path"].endswith("missing11.csv")
    assert records[-1]["path"].endswith("missing2.csv")
    actions = window.recent_sources_menu.actions()
    assert all(not action.isEnabled() for action in actions[:-2])
    window._remember_source(source, "csv")
    window._remember_source(source, "csv")
    assert len(window._recent_sources()) == 10
    opened = []
    monkeypatch.setattr(window, "open_local_path", opened.append)
    window.recent_sources_menu.actions()[0].trigger()
    assert opened == [str(source)]
    stored = json.loads(window.settings.value("recent_sources_v1"))
    assert stored == window._recent_sources()
    window.recent_sources_menu.actions()[-1].trigger()
    assert window._recent_sources() == []
