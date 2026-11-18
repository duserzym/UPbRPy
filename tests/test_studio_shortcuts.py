"""Keyboard shortcuts for the spot-by-spot review loop."""

from pathlib import Path
import time

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from shiboken6 import isValid

from GUI import main_window, services
from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
RICH = FIXTURE / "Zircon_demo_Rich.csv"
CTRL = QtCore.Qt.KeyboardModifier.ControlModifier
SHIFT = QtCore.Qt.KeyboardModifier.ShiftModifier
ALT = QtCore.Qt.KeyboardModifier.AltModifier
NONE = QtCore.Qt.KeyboardModifier.NoModifier
Key = QtCore.Qt.Key


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def wait_for(app, condition, timeout=15):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert condition(), "Timed out"


def idle(app, window):
    wait_for(app, lambda: window._active_worker is None and not window.pending_region_updates
             and not window._selection_recompute_timer.isActive()
             and not window._selection_edit_timer.isActive())


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    window = MainWindow(theme_mode="light")
    window.selection_edit_debounce_ms = 20
    window.selection_recompute_debounce_ms = 40
    window.selection_autosave_debounce_ms = 60
    window.resize(1280, 800)
    window.show()
    window.load_experiment_directory(str(RICH), background=False)
    window.workflow_step_list.setCurrentRow(1)
    window.activateWindow()
    window.spot_list.setFocus()
    app.processEvents()
    idle(app, window)
    yield window
    if isValid(window):
        idle(app, window)
        for timer in (window._selection_edit_timer, window._selection_recompute_timer,
                      window._selection_autosave_timer):
            timer.stop()
        window.has_unsaved_changes = lambda: False
        window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not errors


def press(app, window, key, modifiers=NONE):
    target = QtWidgets.QApplication.focusWidget() or window
    QtTest.QTest.keyClick(target, key, modifiers)
    app.processEvents()


def plateau(window, spot=None):
    return services.get_selection_regions(window.session, spot or window.current_spot_name())["plateau"]


def spot_order(window):
    names = []
    iterator = QtWidgets.QTreeWidgetItemIterator(window.spot_list)
    while iterator.value() is not None:
        item = iterator.value()
        if item.childCount() == 0:
            names.append(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or item.text(0))
        iterator += 1
    return names


def test_next_and_previous_spot_keys(window, app):
    order = spot_order(window)
    start = window.current_spot_name()
    index = order.index(start)
    press(app, window, Key.Key_Down, CTRL)
    assert window.current_spot_name() == order[index + 1]
    press(app, window, Key.Key_Period)
    assert window.current_spot_name() == order[index + 2]
    press(app, window, Key.Key_Comma)
    press(app, window, Key.Key_Up, CTRL)
    assert window.current_spot_name() == start


def test_spot_switch_by_key_keeps_a_queued_edit(window, app):
    spot = window.current_spot_name()
    start, end = plateau(window)
    window.selection_edit_debounce_ms = 10_000
    window.on_plot_region_changed("plateau", (start + 0.4, end))
    press(app, window, Key.Key_Down, CTRL)
    assert window.current_spot_name() != spot
    assert plateau(window, spot)[0] > start


def test_nudge_keys_move_the_active_window_by_one_sweep(window, app):
    spot = window.current_spot_name()
    times = services.get_spot_data(window.session, spot)["Time"].to_numpy(dtype=float)
    start, end = plateau(window)
    press(app, window, Key.Key_P)
    assert window._active_edit_window == "plateau"
    press(app, window, Key.Key_Right, ALT)
    idle(app, window)
    moved = plateau(window, spot)
    later = times[times > start + 1e-9].min()
    assert moved == pytest.approx((later, end))
    press(app, window, Key.Key_Left, ALT | SHIFT)
    idle(app, window)
    assert plateau(window, spot)[1] == pytest.approx(times[times < end - 1e-9].max())
    press(app, window, Key.Key_B)
    assert window._active_edit_window == "baseline"
    assert window.edit_baseline_action.isChecked() and not window.edit_plateau_action.isChecked()
    baseline = services.get_selection_regions(window.session, spot)["baseline"]
    press(app, window, Key.Key_Right, ALT | SHIFT)
    idle(app, window)
    assert services.get_selection_regions(window.session, spot)["baseline"][1] > baseline[1]


def test_undo_redo_and_reset_keys(window, app):
    spot = window.current_spot_name()
    original = plateau(window)
    press(app, window, Key.Key_Right, ALT)
    idle(app, window)
    edited = plateau(window, spot)
    assert edited != original
    press(app, window, Key.Key_Z, CTRL)
    idle(app, window)
    assert plateau(window, spot) == original
    press(app, window, Key.Key_Z, CTRL | SHIFT)
    idle(app, window)
    assert plateau(window, spot) == edited
    press(app, window, Key.Key_R)
    idle(app, window)
    assert plateau(window, spot) == original
    press(app, window, Key.Key_Z, CTRL)
    idle(app, window)
    assert plateau(window, spot) == edited


def test_single_keys_do_not_fire_in_text_fields(window, app):
    spot = window.current_spot_name()
    press(app, window, Key.Key_Right, ALT)
    idle(app, window)
    edited = plateau(window, spot)
    field = window.secondary_standards_edit
    field.setFocus()
    app.processEvents()
    for key in (Key.Key_R, Key.Key_B, Key.Key_T, Key.Key_Period):
        QtTest.QTest.keyClick(field, key)
    app.processEvents()
    assert plateau(window, spot) == edited
    assert window._active_edit_window == "plateau"
    assert window.current_spot_name() == spot
    assert field.text().endswith("rbt.")


def test_cmd_s_applies_queued_edits_and_saves(window, app, tmp_path, monkeypatch):
    target = tmp_path / "keyed.upbr"
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), "")
    )
    spot = window.current_spot_name()
    start, end = plateau(window)
    window.selection_edit_debounce_ms = 10_000
    window.on_plot_region_changed("plateau", (start + 0.4, end))
    press(app, window, Key.Key_S, CTRL)
    assert target.is_file()
    assert window.project_path == target
    assert plateau(window, spot)[0] > start
    assert "Applied and saved selection edits for 1 spot" in window.statusBar().currentMessage()
    # Later saves are silent: no second dialog.
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *args, **kwargs: pytest.fail("asked twice"))
    press(app, window, Key.Key_S, CTRL)
    assert window.statusBar().currentMessage().startswith("Project saved to keyed.upbr")


def test_recalculate_without_saving(window, app):
    spot = window.current_spot_name()
    start, end = plateau(window)
    window.selection_edit_debounce_ms = 10_000
    window.on_plot_region_changed("plateau", (start + 0.4, end))
    press(app, window, Key.Key_Return, CTRL | SHIFT)
    idle(app, window)
    assert plateau(window, spot)[0] > start
    assert window.project_path is None


def test_exclude_current_spot_and_raw_traces(window, app):
    spot = window.current_spot_name()
    press(app, window, Key.Key_E, CTRL)
    assert spot in window.results_table.excluded_spots
    assert window.exclude_spot_action.isChecked()
    press(app, window, Key.Key_E, CTRL)
    assert spot not in window.results_table.excluded_spots
    window.session.plot_mode = "baseline_subtracted"
    press(app, window, Key.Key_T)
    assert window.session.plot_mode == "raw"


def test_selection_keys_are_disabled_on_later_steps(window, app):
    window.step_primary_button.click()
    idle(app, window)
    window.go_to_workflow_step(1)
    app.processEvents()
    assert window.workflow_step_list.currentRow() == 2
    assert not window.reset_spot_action.isEnabled()
    assert not window.edit_plateau_action.isEnabled()
    assert window.next_spot_action.isEnabled()


def test_tab_number_keys(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    shell = StudioWindow(theme_mode="light")
    try:
        shell.show()
        for _ in range(2):
            shell.add_tab()
        assert shell.tabs.count() == 3
        shell.activateWindow()
        app.processEvents()
        shell.active_reduction.tab_index_requested.emit(0)
        assert shell.tabs.currentIndex() == 0
        shell.active_reduction.tab_index_requested.emit(-1)
        assert shell.tabs.currentIndex() == 2
        QtTest.QTest.keyClick(shell, Key.Key_2, CTRL)
        app.processEvents()
        assert shell.tabs.currentIndex() == 1
        QtTest.QTest.keyClick(shell, Key.Key_BracketRight, CTRL | SHIFT)
        app.processEvents()
        assert shell.tabs.currentIndex() == 2
        for index in range(shell.tabs.count()):
            listed = {text for _s, text, _k in shell.tabs.widget(index).keyboard_shortcut_rows()}
            assert {"Next Spot", "Save Project", "Tab 1"} <= listed
    finally:
        for index in range(shell.tabs.count()):
            shell.tabs.widget(index).has_unsaved_changes = lambda: False
        shell.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_shortcut_sheet_lists_every_menu_shortcut_and_fits_a_14_inch_screen(window, app):
    rows = window.keyboard_shortcut_rows()
    actions = {text for _section, text, _keys in rows}
    for expected in ("Save Project", "Undo Selection Edit", "Redo Selection Edit", "Next Spot",
                     "Previous Spot", "Reset Current Spot", "Re-pick All Spots",
                     "Edit Baseline Window", "Edit Plateau Window", "Move Window Start Later",
                     "Exclude Current Spot from Calculations", "Return to Raw Traces",
                     "Recalculate Reduction Stages", "Next Step", "Previous Step", "Next Tab",
                     "Tab 1", "Keyboard Shortcuts", "Hide selected spots"):
        assert expected in actions, expected
    sequences = {}
    for action in window.findChildren(QtGui.QAction):
        for sequence in action.shortcuts():
            key = sequence.toString()
            assert key not in sequences, f"{key} is bound to {sequences[key]!r} and {action.text()!r}"
            sequences[key] = action.text()
    window.show_keyboard_shortcuts()
    app.processEvents()
    sheet = window._shortcut_sheet
    assert sheet.isVisible()
    assert sheet.width() <= 1280 and sheet.height() <= 800 - 80
    table = sheet.findChild(QtWidgets.QTableWidget, "KeyboardShortcutsTable")
    assert table.rowCount() > len(rows)
    sheet.close()


def test_reload_asks_before_discarding_selection_edits(window, app, monkeypatch):
    spot = window.current_spot_name()
    original = plateau(window, spot)
    press(app, window, Key.Key_Right, ALT)
    idle(app, window)
    edited = plateau(window, spot)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *args, **kwargs: pytest.fail("Reload opened a modal question"))
    window.reload_current_directory()
    assert window.change_guard.pending
    assert "Reload" in window.pending_change_bar.message.text()
    assert plateau(window, spot) == edited
    window.pending_change_bar.cancel_button.click()
    assert window.change_guard.pending is None
    assert plateau(window, spot) == edited
    window.reload_current_directory()
    assert window.change_guard.pending
    window.pending_change_bar.apply_button.click()
    idle(app, window)
    assert plateau(window, spot) == original


def test_focus_changes_after_a_window_is_deleted_are_ignored(app, monkeypatch, capfd):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow(theme_mode="light")
    window.show()
    app.processEvents()
    window.has_unsaved_changes = lambda: False
    window.close()
    window.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    capfd.readouterr()
    QtWidgets.QApplication.instance().focusChanged.emit(None, None)
    app.processEvents()
    assert "already deleted" not in capfd.readouterr().err
