"""Document tabs, menu routing, close guards and launch handoff in Studio."""

from __future__ import annotations

import os
from pathlib import Path
import time
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.icon import build_app_icon
from GUI.launch import SingleInstance
from GUI.main import StudioWindowController
from GUI.main_window import MainWindow
from GUI.theme import build_application

FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
RICH = FIXTURE / "Zircon_04Nov26_slice_Rich.csv"
SPOTS = FIXTURE / "zircon_04Nov26"


def process(app):
    for _ in range(5):
        app.processEvents()


def wait_for(app, condition):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        QtTest.QTest.qWait(10)
    pytest.fail("Timed out waiting for Studio")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture
def controller(app, monkeypatch):
    settings = QtCore.QSettings("UPbRPy", "Studio")
    settings.clear()
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args[1:])))
    controller = StudioWindowController(theme_mode="light", app_icon=build_app_icon())
    yield controller
    for shell in list(controller.windows.values()):
        for index in range(shell.tabs.count()):
            reduction = shell.tabs.widget(index)
            wait_for(app, lambda: reduction._active_worker is None)
            reduction.has_unsaved_changes = lambda: False
        shell.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    process(app)
    settings.clear()


@pytest.fixture
def loaded(controller, app):
    shell = controller.create_window(show=False)
    shell.resize(1280, 800)
    shell.show()
    first = shell.active_reduction
    first.load_experiment_directory(str(RICH), background=False)
    second = shell.add_tab()
    second.load_experiment_directory(str(SPOTS), background=False)
    process(app)
    return shell, first, second


def test_two_formats_titles_dirty_marker_and_active_panels(loaded, app, tmp_path):
    shell, rich, spots = loaded
    assert shell.tabs.count() == 2
    assert not rich.isWindow() and not spots.isWindow()
    assert shell.tabs.documentMode() and shell.tabs.tabsClosable() and shell.tabs.isMovable()
    assert shell.tabs.elideMode() == QtCore.Qt.TextElideMode.ElideRight
    assert shell.tabs.tabText(0) == f"• {RICH.name}"
    assert shell.tabs.tabText(1) == f"• {SPOTS.name}"
    assert shell.tabs.tabToolTip(0) == str(RICH.resolve())
    assert rich.session is not spots.session
    assert rich.detector_calibration_panel.isEnabled()
    assert not spots.detector_calibration_panel.isEnabled()
    rich.session.origin = {"server": "https://lab.example.org", "session_key": "rich-session", "session_id": "123"}
    rich.update_project_window_title()
    spots.update_project_window_title()
    assert rich.database_origin_label.text() and not spots.database_origin_label.text()
    shell.tabs.setCurrentIndex(0)
    rich.inspector_tabs.setCurrentWidget(rich.calibration_tabs)
    rich.calibration_tabs.setCurrentIndex(1)
    process(app)
    assert rich.detector_calibration_panel.isVisible()
    assert not spots.detector_calibration_panel.isVisible()
    assert rich.database_origin_label.isVisible()
    assert "database lab.example.org" in shell.windowTitle()
    shell.tabs.setCurrentIndex(1)
    assert "database" not in shell.windowTitle()
    project = tmp_path / "saved-project.upbr"
    spots.save_project(project)
    assert project.is_file()
    assert shell.tabs.tabText(1) == "saved-project"
    assert shell.tabs.tabToolTip(1) == str(project)
    spots._workflow_step_allows_region_edits = lambda: True
    spots.on_plot_region_changed("plateau", (8.0, 12.0))
    assert shell.tabs.tabText(1) == "• saved-project"


def test_shortcuts_route_only_to_active_tab_and_cycle_from_table(loaded, app, monkeypatch):
    shell, first, second = loaded
    saved = []
    monkeypatch.setattr(first, "save_project", lambda: saved.append(first))
    monkeypatch.setattr(second, "save_project", lambda: saved.append(second))
    messages = []
    previous = QtCore.qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    try:
        shell.activateWindow()
        second.plots_stack.setCurrentWidget(second.results_table)
        table = second.results_table.results_table
        table.setFocus()
        process(app)
        QtTest.QTest.keyClick(table, QtCore.Qt.Key.Key_S, QtCore.Qt.KeyboardModifier.ControlModifier)
        process(app)
        assert saved == [second]
        QtTest.QTest.keyClick(table, QtCore.Qt.Key.Key_Tab, QtCore.Qt.KeyboardModifier.ControlModifier)
        process(app)
        assert shell.active_reduction is first
        first.trace_plot.setFocus()
        QtTest.QTest.keyClick(first.trace_plot, QtCore.Qt.Key.Key_Tab,
                             QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier)
        process(app)
        assert shell.active_reduction is second
        shell.tabs.setCurrentIndex(0)
        QtTest.QTest.keyClick(first, QtCore.Qt.Key.Key_S, QtCore.Qt.KeyboardModifier.ControlModifier)
        assert saved == [second, first]
        if sys.platform == "darwin":
            QtTest.QTest.keyClick(first, QtCore.Qt.Key.Key_Tab, QtCore.Qt.KeyboardModifier.MetaModifier)
            assert shell.active_reduction is second
            QtTest.QTest.keyClick(second, QtCore.Qt.Key.Key_Tab,
                                 QtCore.Qt.KeyboardModifier.MetaModifier | QtCore.Qt.KeyboardModifier.ShiftModifier)
            assert shell.active_reduction is first
        assert not any("ambiguous" in message.lower() for message in messages), messages
    finally:
        QtCore.qInstallMessageHandler(previous)
    assert first.menuBar().isHidden() and second.menuBar().isHidden()
    assert [a.text() for a in shell.menuBar().actions()] == ["File", "Workflow", "Calibration", "Results", "View", "Window", "Help"]
    assert not first.menuBar().actions() and not second.menuBar().actions()
    assert shell.menuBar().actions() == first._studio_menus


def test_close_tab_cancel_discard_and_release(loaded, app, monkeypatch):
    shell, first, second = loaded
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.StandardButton.Cancel)
    assert not shell.close_tab(1)
    assert shell.tabs.count() == 2 and shell.active_reduction is second
    destroyed = []
    second.destroyed.connect(lambda: destroyed.append(True))
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.StandardButton.Discard)
    assert shell.close_tab(1)
    assert shell.tabs.count() == 1 and shell.active_reduction is first
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert destroyed == [True]
    assert not first.move_tab_action.isEnabled()


def test_window_close_cancel_preserves_all_tabs(loaded, app, monkeypatch):
    shell, first, second = loaded
    first.has_unsaved_changes = lambda: False
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.StandardButton.Cancel)
    assert not shell.close()
    assert shell.isVisible() and shell.tabs.count() == 2
    assert shell.tabs.widget(0) is first and shell.tabs.widget(1) is second


def test_close_save_and_busy_guard(loaded, monkeypatch):
    shell, first, second = loaded
    saved = []
    def save():
        saved.append(second)
        second.has_unsaved_changes = lambda: False
    monkeypatch.setattr(second, "save_project", save)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.StandardButton.Save)
    second._active_worker = object()
    second._busy_label = "Importing"
    notices = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *args: notices.append(args))
    assert not shell.close_tab(1) and notices and not saved
    second._active_worker = None
    assert shell.close_tab(1) and saved == [second]


def test_move_tab_keeps_reduction_session_and_menu_routing(loaded, controller, app, monkeypatch):
    shell, first, second = loaded
    session = second.session
    assert second.move_tab_action.isEnabled()
    second.move_to_new_window_requested.emit()
    process(app)
    target = controller.active_window()
    assert target is not shell and len(controller.windows) == 2
    assert target.active_reduction is second and second.session is session
    assert shell.active_reduction is first and shell.tabs.count() == 1
    assert not second.move_tab_action.isEnabled()
    saved = []
    monkeypatch.setattr(second, "save_project", lambda: saved.append(second))
    target.activateWindow()
    process(app)
    QtTest.QTest.keyClick(second, QtCore.Qt.Key.Key_S, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert saved == [second]
    second.new_tab_requested.emit()
    assert target.tabs.count() == 2 and shell.tabs.count() == 1


def test_last_tab_close_leaves_empty_reduction(controller):
    shell = controller.create_window(show=False)
    old = shell.active_reduction
    assert shell.close_tab(0)
    assert shell.tabs.count() == 1 and shell.active_reduction is not old
    assert not shell.active_reduction.session.directory
    assert shell.tabs.tabText(0) == "Untitled"


def test_file_open_reuses_only_active_empty_idle_tab(controller, app, monkeypatch):
    shell = controller.create_window(show=False)
    initial = shell.active_reduction
    monkeypatch.setattr(initial, "select_qtegra_file", lambda: str(RICH))
    initial.choose_qtegra_file()
    assert shell.tabs.count() == 1 and shell.active_reduction is initial
    wait_for(app, lambda: initial._active_worker is None)
    assert initial.session.loaded
    initial.choose_qtegra_file()
    second = shell.active_reduction
    assert shell.tabs.count() == 2 and second is not initial
    wait_for(app, lambda: second._active_worker is None)
    idle = shell.add_tab()
    busy = shell.add_tab()
    busy._active_worker = object()
    busy.open_source_requested.emit(str(SPOTS))
    busy._active_worker = None
    third = shell.active_reduction
    assert third not in (idle, busy)
    wait_for(app, lambda: third._active_worker is None)
    opened = []
    monkeypatch.setattr(MainWindow, "load_project_file", lambda self, path: opened.append((self, path)))
    shell.tabs.setCurrentWidget(idle)
    monkeypatch.setattr(idle, "select_project_file", lambda: "/tmp/example.upbr")
    idle.choose_project_file()
    assert opened == [(idle, "/tmp/example.upbr")]


def test_open_target_and_single_instance_handoff_add_tabs(controller, app, monkeypatch):
    shell = controller.create_window(show=False)
    controller.open_target(str(RICH))
    first = shell.active_reduction
    wait_for(app, lambda: first._active_worker is None)
    instance = SingleInstance()
    instance.received.connect(controller.open_target)
    # Exercise the real --open entry point with the transport replaced. The
    # socket/subprocess integration itself is covered in test_launch.py.
    def send(value):
        instance.received.emit(value)
        return True
    monkeypatch.setattr(instance, "send", send)
    monkeypatch.setattr("GUI.main.SingleInstance", lambda: instance)
    from GUI.main import main

    assert main(["--open", str(SPOTS)]) == 0
    assert shell.tabs.count() == 2
    second = shell.active_reduction
    wait_for(app, lambda: second._active_worker is None)
    assert len(controller.windows) == 1
    assert first.session.qtegra.format == "rich"
    assert second.session.qtegra.format == "spot_folder"


def test_new_content_uses_most_recent_shell(controller, app):
    first = controller.create_window()
    second = controller.create_window()
    first.activateWindow()
    process(app)
    controller.open_target(str(RICH))
    reduction = first.active_reduction
    wait_for(app, lambda: reduction._active_worker is None)
    assert reduction.session.loaded and not second.active_reduction.session.loaded


def test_new_close_and_new_window_shortcuts(controller, app):
    shell = controller.create_window()
    shell.activateWindow()
    process(app)
    QtTest.QTest.keyClick(shell.active_reduction, QtCore.Qt.Key.Key_T, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert shell.tabs.count() == 2
    QtTest.QTest.keyClick(shell.active_reduction, QtCore.Qt.Key.Key_N, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert shell.tabs.count() == 3
    QtTest.QTest.keyClick(shell.active_reduction, QtCore.Qt.Key.Key_W, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert shell.tabs.count() == 2
    QtTest.QTest.keyClick(shell.active_reduction, QtCore.Qt.Key.Key_N,
                         QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier)
    process(app)
    assert len(controller.windows) == 2
    assert controller.active_window().tabs.count() == 1
