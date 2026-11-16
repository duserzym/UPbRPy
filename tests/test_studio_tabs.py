"""Document tabs, menu routing, close guards and launch handoff in Studio."""

from __future__ import annotations

import gc
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

FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
RICH = FIXTURE / "Zircon_demo_Rich.csv"
SPOTS = FIXTURE / "zircon_demo"


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
    # The document tests do not need live background update checks.
    monkeypatch.setenv("UPBRPY_UPDATE_PREVIEW", "none")
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


def test_tab_dot_follows_unsaved_calibration_text_and_review_options(loaded, tmp_path):
    shell, first, second = loaded
    first.save_project(tmp_path / "first.upbr")
    second.save_project(tmp_path / "second.upbr")
    second.secondary_standards_edit.setFocus()
    second.secondary_standards_edit.selectAll()
    QtTest.QTest.keyClicks(second.secondary_standards_edit, "Example")
    assert second.session.secondary_standards != ("Example",)
    assert shell.tabs.tabText(1) == "• second"
    assert shell.tabs.tabBar().accessibleTabName(1) == "second, unsaved changes"
    assert shell.tabs.tabText(0) == "first"
    second.save_project()
    assert second.session.secondary_standards == ("Example",)
    assert shell.tabs.tabText(1) == "second"
    second.results_table._glass_toggled(True)
    assert shell.tabs.tabText(1) == "• second"
    second.results_table._glass_toggled(False)
    assert shell.tabs.tabText(1) == "second"


def test_spot_review_flag_survives_project_reopen(loaded, tmp_path):
    _, rich, spots = loaded
    name = rich.current_spot_name()
    reason = "Suspected mixed ablation"
    path = tmp_path / "flagged.upbr"
    rich.save_project(path)
    assert not rich.has_unsaved_changes()
    rich._set_spot_review_flag(name, reason)
    assert rich.has_unsaved_changes()
    assert reason in rich.spot_list.currentItem().toolTip(0)
    assert not rich.spot_list.currentItem().icon(0).isNull()
    assert reason in rich._compose_notes(name)
    assert not spots.project_workspace_state()["review_flags"]
    rich.save_project()
    rich.load_project_file(str(path), background=False)
    assert rich.project_workspace_state()["review_flags"] == {name: reason}
    assert reason in rich.spot_list.currentItem().toolTip(0)
    assert not rich.has_unsaved_changes()
    rich._set_spot_review_flag(name, "")
    assert reason not in rich.spot_list.currentItem().toolTip(0)
    assert rich.has_unsaved_changes()
    rich.load_project_file(str(path), background=False)
    rich.load_experiment_directory(str(RICH), background=False)
    assert rich.project_workspace_state()["review_flags"] == {}


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
    assert [a.text() for a in shell.menuBar().actions()] == [a.text() for a in first._studio_menus]
    assert all(a.menu() is not original.menu()
               for a, original in zip(shell.menuBar().actions(), first._studio_menus))


def assert_menu_mirrors(shell, reduction):
    def check(mirror, source):
        assert isinstance(mirror, QtWidgets.QMenu) and mirror is not source
        parent = mirror.parent()
        while parent is not None and parent is not shell:
            parent = parent.parent()
        assert parent is shell
        assert mirror.title() == source.title()
        assert mirror.objectName() == source.objectName()
        assert mirror.toolTipsVisible() == source.toolTipsVisible()
        assert mirror.menuAction().isEnabled() == source.menuAction().isEnabled()
        assert mirror.menuAction().isVisible() == source.menuAction().isVisible()
        assert len(mirror.actions()) == len(source.actions())
        for mirrored, original in zip(mirror.actions(), source.actions()):
            if original.menu() is not None:
                check(mirrored.menu(), original.menu())
            else:
                # Also preserves separators, shortcuts and macOS menu roles.
                assert mirrored is original

    actions = shell.menuBar().actions()
    assert [a.text() for a in actions] == ["File", "Workflow", "Calibration", "Results", "View", "Window", "Help"]
    for action, original in zip(actions, reduction._studio_menus):
        check(action.menu(), original.menu())


def test_shell_owns_menus_and_database_actions_follow_active_tab(loaded, monkeypatch):
    from types import SimpleNamespace

    shell, first, second = loaded
    submitted = []
    for reduction in (first, second, first):
        monkeypatch.setattr(reduction, "database", lambda: SimpleNamespace(submit=submitted.append))
        shell.tabs.setCurrentWidget(reduction)
        shell._activate_tab(shell.tabs.currentIndex())
        assert_menu_mirrors(shell, reduction)
        file_menu = shell.menuBar().actions()[0].menu()
        database = next(a.menu() for a in file_menu.actions() if a.text() == "Database")
        assert any(a is reduction.submit_to_database_action for a in database.actions())
        assert any(a is reduction.prefer_run_action for a in database.actions())
        submit = next(a for a in database.actions() if a is reduction.submit_to_database_action)
        submit.setEnabled(True)
        submit.trigger()
        gc.collect()
    assert submitted == [first, second, first]


def test_mirror_refreshes_dynamic_menus_and_tracks_menu_state(loaded):
    shell, first, second = loaded
    source = second._studio_menus[1].menu()
    dynamic = source.addMenu("Dynamic presets")
    dynamic.setObjectName("DynamicPresets")
    dynamic.setToolTipsVisible(True)
    dynamic.setIcon(second.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileIcon))
    opened = []

    def populate():
        dynamic.clear()
        opened.append(True)
        dynamic.addAction(f"Preset {len(opened)}")
        nested = dynamic.addMenu("Nested")
        nested.addAction("Initial")
        nested.aboutToShow.connect(lambda: nested.addAction("Added on opening"))

    dynamic.aboutToShow.connect(populate)
    shell._activate_tab(1)
    workflow = shell.menuBar().actions()[1].menu()
    mirror = next(a.menu() for a in workflow.actions() if a.text() == dynamic.title())
    for count in (1, 2):
        mirror.aboutToShow.emit()
        assert opened == [True] * count
        assert mirror.actions()[0] is dynamic.actions()[0]
        assert mirror.actions()[0].text() == f"Preset {count}"
        nested_mirror = mirror.actions()[1].menu()
        nested_mirror.aboutToShow.emit()
        assert [a.text() for a in nested_mirror.actions()] == ["Initial", "Added on opening"]
        assert nested_mirror.actions()[1] is dynamic.actions()[1].menu().actions()[1]
        assert_menu_mirrors(shell, second)
    assert mirror.icon().cacheKey() == dynamic.icon().cacheKey()
    for menu, mirrored in ((source, workflow), (dynamic, mirror)):
        menu.menuAction().setEnabled(False)
        menu.menuAction().setVisible(False)
        menu.setTitle("Updated menu")
        assert not mirrored.menuAction().isEnabled()
        assert not mirrored.menuAction().isVisible()
        assert mirrored.title() == "Updated menu"
        menu.menuAction().setEnabled(True)
        menu.menuAction().setVisible(True)
    source.addAction("Added after construction")
    workflow.aboutToShow.emit()
    assert workflow.actions()[-1] is source.actions()[-1]
    dynamic.clear()
    workflow.aboutToShow.emit()
    mirror = next(a.menu() for a in workflow.actions() if a.text() == dynamic.title())
    assert not mirror.actions()


def test_closing_active_tab_disposes_mirrors_and_routes_remaining_tab(loaded, app, monkeypatch):
    shell, first, second = loaded
    old_menus = [a.menu() for a in shell.menuBar().actions()]
    destroyed = []
    for menu in old_menus:
        menu.destroyed.connect(lambda: destroyed.append(True))
    monkeypatch.setattr(second, "request_close", lambda: True)
    assert shell.close_tab(1)
    assert shell.active_reduction is first
    assert_menu_mirrors(shell, first)
    assert all(menu._source is None and not menu.actions() for menu in old_menus)
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert len(destroyed) == len(old_menus)
    process(app)
    assert_menu_mirrors(shell, first)


def test_reduction_menu_bar_is_not_native_before_it_joins_the_shell(controller):
    # On macOS a native menu bar reparented into the shell claims the shell's
    # window; destroying it afterwards leaves macOS with only the app menu.
    shell = controller.create_window(show=False)
    reduction = controller._create_reduction()
    seen = []
    original = reduction.setParent

    def record(*args):
        seen.append(reduction.menuBar().isNativeMenuBar())
        return original(*args)

    reduction.setParent = record
    shell.add_tab(reduction)
    assert seen and seen[0] is False
    assert_menu_mirrors(shell, reduction)


def test_taking_last_tab_empties_shell_menu_bar(controller):
    shell = controller.create_window(show=False)
    reduction = shell.active_reduction
    old_menus = [a.menu() for a in shell.menuBar().actions()]
    assert shell.take_tab(reduction) is reduction
    assert shell.active_reduction is None and not shell.menuBar().actions()
    assert all(menu._source is None and not menu.actions() for menu in old_menus)
    # The original menus remain usable when the reduction moves to a shell.
    shell.add_tab(reduction)
    assert_menu_mirrors(shell, reduction)


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


def test_app_quit_prompts_for_each_window_before_closing_any(
    loaded, controller, app, tmp_path, monkeypatch
):
    shell, first, second = loaded
    other = controller.move_tab_to_new_window(shell, second)
    first.save_project(tmp_path / "first.upbr")
    second.save_project(tmp_path / "second.upbr")
    first._set_spot_review_flag(first.current_spot_name(), "Keep this note")
    second._set_spot_review_flag(second.current_spot_name(), "Discard this note")
    before = first.project_path.read_bytes(), second.project_path.read_bytes()
    buttons = QtWidgets.QMessageBox.StandardButton
    choices = iter([buttons.Discard, buttons.Cancel])
    prompts = []

    def choose(dialog):
        prompts.append(dialog.text())
        assert dialog.button(buttons.Discard).text() == "Don't Save"
        assert dialog.defaultButton() is dialog.button(buttons.Save)
        assert dialog.escapeButton() is dialog.button(buttons.Cancel)
        return next(choices)

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", choose)
    app.installEventFilter(controller)
    try:
        event = QtCore.QEvent(QtCore.QEvent.Type.Quit)
        assert app.sendEvent(app, event)
        assert not event.isAccepted()
    finally:
        app.removeEventFilter(controller)
    assert shell.active_reduction is first and other.active_reduction is second
    assert shell.isVisible() and other.isVisible()
    assert first.has_unsaved_changes() and second.has_unsaved_changes()
    assert "first" in prompts[0] and "second" in prompts[1]
    assert (first.project_path.read_bytes(), second.project_path.read_bytes()) == before

    paths = first.project_path, second.project_path
    choices = iter([buttons.Save, buttons.Discard])
    event = QtCore.QEvent(QtCore.QEvent.Type.Quit)
    assert not controller.eventFilter(app, event)
    assert shell.tabs.count() == other.tabs.count() == 0
    assert not shell.isVisible() and not other.isVisible()
    assert paths[0].read_bytes() != before[0]
    assert paths[1].read_bytes() == before[1]


def test_quit_stays_open_when_save_as_is_cancelled(loaded, monkeypatch):
    shell, first, _ = loaded
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    assert not shell.close()
    assert shell.isVisible() and shell.tabs.count() == 2
    assert first.has_unsaved_changes()


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


def test_cmd_w_on_the_last_tab_keeps_the_window_open(controller, app):
    # Cmd+W (Ctrl+W in Qt terms) closes a tab, never the window or the app.
    shell = controller.create_window()
    shell.activateWindow()
    process(app)
    closed = []
    app.lastWindowClosed.connect(lambda: closed.append(True))
    first = shell.active_reduction
    QtTest.QTest.keyClick(first, QtCore.Qt.Key.Key_W, QtCore.Qt.KeyboardModifier.ControlModifier)
    process(app)
    assert shell.isVisible() and shell.tabs.count() == 1
    assert shell.active_reduction is not first and not shell.active_reduction.session.loaded
    assert len(controller.windows) == 1 and closed == []


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
