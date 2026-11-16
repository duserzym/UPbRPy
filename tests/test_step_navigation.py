"""Rail geometry, stage actions and keyboard navigation on Qtegra data."""

import os
from pathlib import Path
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtTest, QtWidgets

from GUI import services
from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application
from UPbRPy.pipeline import PipelineStage


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def pump(app):
    for _ in range(10):
        app.processEvents()


def finish_work(window, app):
    deadline = time.monotonic() + 20
    while window._active_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert window._active_worker is None, "Background task did not finish"
    pump(app)


@pytest.fixture
def window(app, monkeypatch):
    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    window = MainWindow(theme_mode="light")
    window.resize(1280, 800)
    window.show()
    window.apply_default_layout()
    window.load_experiment_directory(str(FIXTURE), background=False)
    pump(app)
    window._test_errors = errors
    try:
        yield window
    finally:
        finish_work(window, app)
        window.has_unsaved_changes = lambda: False
        window.close()
        pump(app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)


def run_primary(window, app):
    window.step_primary_button.click()
    finish_work(window, app)
    assert not window._test_errors


def reach_concentration(window, app):
    for row in range(4):
        assert window.workflow_step_list.currentRow() == row
        run_primary(window, app)
        assert window.step_next_button.isEnabled()
        window.step_next_button.click()
        pump(app)
    assert window.workflow_step_list.currentRow() == 4


def assert_inside(widget, ancestor):
    assert widget.isVisibleTo(ancestor), widget.objectName()
    rect = QtCore.QRect(widget.mapTo(ancestor, QtCore.QPoint()), widget.size())
    assert ancestor.rect().contains(rect), (widget.objectName(), rect, ancestor.rect())


def assert_rail_layout(window):
    rail = window.sidebar_panel
    # All visible rail descendants fit their immediate parent and the rail.
    for widget in rail.findChildren(QtWidgets.QWidget):
        if widget.isVisibleTo(rail) and widget.width() and widget.height():
            assert_inside(widget, widget.parentWidget())
            assert_inside(widget, rail)
    step_list = window.workflow_step_list
    assert step_list.count() == 10
    assert not step_list.verticalScrollBar().isVisible()
    assert step_list.verticalScrollBar().maximum() == 0
    for index in range(10):
        assert step_list.viewport().rect().contains(step_list.visualItemRect(step_list.item(index)))
    # Check siblings too: widgets can be inside the rail yet overlap each other.
    previous_bottom = -1
    layout = rail.layout()
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.isEmpty():
            continue
        assert item.geometry().top() > previous_bottom
        previous_bottom = item.geometry().bottom()
    assert step_list.geometry().bottom() < window.step_actions_block.y()
    for widget in (window.step_primary_button, *window.step_secondary_buttons,
                   window.step_back_button, window.step_next_button,
                   window.save_project_button, window.export_results_button, window.import_button):
        assert_inside(widget, rail)
    primary = window.step_primary_button
    assert primary.text() == window._workflow_action_specs()[0][0]
    assert "…" not in primary.text()
    assert primary.height() >= primary.heightForWidth(primary.width())
    text_rect = primary._text_rect(primary.width(), primary.height())
    rendered = primary.fontMetrics().boundingRect(text_rect, QtCore.Qt.TextFlag.TextWordWrap, primary.text())
    assert rendered.height() <= text_rect.height()
    assert rendered.width() <= text_rect.width()
    for secondary in window.step_secondary_buttons:
        assert secondary.toolTip() == secondary.text()
        assert secondary.height() >= secondary.heightForWidth(secondary.width())
    assert abs(window.step_back_button.width() - window.step_next_button.width()) <= 1
    caption = window.step_actions_caption
    assert not caption.findChildren(QtWidgets.QPushButton)
    assert caption.height() <= 28
    assert window.step_actions_title.toolTip() == window.step_actions_block.toolTip()
    assert window.workflow_status_label.toolTip() == window.step_actions_block.toolTip()
    assert window.workflow_hint_body.toolTip() == window.step_actions_block.toolTip()


@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
@pytest.mark.parametrize("in_studio", [False, True], ids=["standalone", "studio-tab"])
def test_all_stage_actions_fit_rail(window, app, size, in_studio):
    shell = None
    target = window
    if in_studio:
        shell = StudioWindow(theme_mode="light")
        shell.show()
        target = shell.active_reduction
        target.load_experiment_directory(str(FIXTURE), background=False)
    try:
        (shell or target).resize(*size)
        target.apply_default_layout()
        pump(app)
        assert not target.autopick_settings_toggle.isChecked()
        assert (shell or target).size() == QtCore.QSize(*size)
        for row in range(10):
            # Inspect each stage's actions without pretending to calculate
            # products unavailable in this channel-limited fixture.
            target.workflow_step_list.setCurrentRow(row)
            pump(app)
            assert_rail_layout(target)
            if row == 4:
                primary = target.step_primary_button
                assert primary.height() <= 2 * primary.fontMetrics().lineSpacing() + 16
        assert not target.step_next_button.isEnabled()
        assert "last step" in target.step_next_button.toolTip()
    finally:
        if shell:
            target.has_unsaved_changes = lambda: False
            shell.close()
            pump(app)


def test_concentration_unlocks_next_and_transfers_accent(window, app):
    reach_concentration(window, app)
    primary = window.step_primary_button
    assert primary.property("accent") is True
    assert not window.step_next_button.isEnabled()
    assert primary.text() in window.step_next_button.toolTip()
    assert "6 Ratios" in window.step_next_button.toolTip()
    window.go_to_workflow_step(1)
    assert window.workflow_step_list.currentRow() == 4
    run_primary(window, app)
    assert window.step_next_button.isEnabled()
    assert window.step_next_button.property("accent") is True
    assert primary.property("accent") is False
    assert primary.isEnabled()
    assert window.statusBar().currentMessage() == "Calibrating concentrations done"
    window.step_next_button.click()
    assert window.workflow_step_list.currentRow() == 5
    window.step_back_button.click()
    assert window.workflow_step_list.currentRow() == 4
    assert window.step_next_button.property("accent") is True
    window.on_selection_threshold_percent_changed(110)
    assert not window.step_next_button.isEnabled()
    assert window.step_primary_button.property("accent") is True
    assert "6 Ratios" in window.step_next_button.toolTip()


@pytest.mark.parametrize("editor_kind", ["line", "spin"])
def test_navigation_shortcuts_respect_text_editors(window, app, editor_kind):
    reach_concentration(window, app)
    run_primary(window, app)
    window.activateWindow()
    window.workflow_step_list.setFocus()
    pump(app)
    assert window.workflow_step_list.hasFocus()
    QtTest.QTest.keyClick(window.workflow_step_list, QtCore.Qt.Key.Key_Right, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert window.workflow_step_list.currentRow() == 5
    QtTest.QTest.keyClick(window.workflow_step_list, QtCore.Qt.Key.Key_Left, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert window.workflow_step_list.currentRow() == 4
    if editor_kind == "line":
        editor = QtWidgets.QLineEdit("Editable selection name", window)
    else:
        editor = QtWidgets.QSpinBox(window)
        editor.setRange(0, 100000)
        editor.setValue(12345)
    editor.setGeometry(300, 200, 220, 40)
    editor.show()
    editor.setFocus()
    pump(app)
    assert editor.hasFocus()
    for key in (QtCore.Qt.Key.Key_Right, QtCore.Qt.Key.Key_Left):
        QtTest.QTest.keyClick(editor, key, QtCore.Qt.KeyboardModifier.ControlModifier)
        assert window.workflow_step_list.currentRow() == 4
    editor.hide()
    window.workflow_step_list.setFocus()
    pump(app)
    QtTest.QTest.keyClick(window.workflow_step_list, QtCore.Qt.Key.Key_BracketRight, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert window.workflow_step_list.currentRow() == 5
    QtTest.QTest.keyClick(window.workflow_step_list, QtCore.Qt.Key.Key_BracketLeft, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert window.workflow_step_list.currentRow() == 4


@pytest.mark.parametrize("outcome", ["success", "specific", "failure", "display-failure"])
def test_background_finish_refreshes_navigation_and_status(window, app, outcome):
    reach_concentration(window, app)
    queued = []
    class DeferredPool:
        def start(self, worker):
            queued.append(worker)
    window.thread_pool = DeferredPool()

    def calculate():
        if outcome == "failure":
            raise ValueError("deliberate calculation failure")
        return services.ensure_concentration_results(window.session, window.workflow_preset)

    def display(result):
        if outcome == "display-failure":
            raise ValueError("deliberate display failure")
        if outcome == "specific":
            window.statusBar().showMessage("Calibrated the selected standard")

    window.run_background("Calibrating concentrations", calculate, display)
    assert not window.step_actions_block.isEnabled()
    assert not window.workflow_step_list.isEnabled()
    window.go_to_workflow_step(-1)
    assert window.workflow_step_list.currentRow() == 4
    queued.pop().run()
    pump(app)
    assert window._active_worker is None
    assert window.step_actions_block.isEnabled()
    assert window.workflow_step_list.isEnabled()
    failed = outcome in {"failure", "display-failure"}
    assert bool(window._test_errors) == failed
    expected = ("Calibrating concentrations failed" if failed else
                "Calibrated the selected standard" if outcome == "specific" else
                "Calibrating concentrations done")
    assert window.statusBar().currentMessage() == expected
    assert window.step_next_button.isEnabled() == (outcome != "failure")
    assert window.step_primary_button.property("accent") == (outcome == "failure")


def test_walk_workflow_as_far_as_fixture_allows(window, app):
    for row, stage in enumerate(window.workflow_preset.stage_sequence):
        assert window.workflow_step_list.currentRow() == row
        if stage == PipelineStage.RATIOS and "202Hg" not in window.session.channels:
            pytest.skip("Reached step 6; Qtegra slice lacks 202Hg for Hg correction, so ratios and steps 7–10 cannot run. S513 standards are present.")
        run_primary(window, app)
        assert stage in window.session.pipeline.cache.stages()
        if row < 9:
            assert window.step_next_button.isEnabled()
            window.step_next_button.click()
            pump(app)
    assert not window.step_next_button.isEnabled()


def test_unlock_rule_uses_immediate_predecessor():
    sequence = [PipelineStage.IMPORT, PipelineStage.SELECTION, PipelineStage.BASELINE, PipelineStage.NORMALIZATION]
    assert services.unlocked_workflow_rows(sequence, [], False) == [True, False, False, False]
    assert services.unlocked_workflow_rows(sequence, sequence, False) == [True, False, False, False]
    assert services.unlocked_workflow_rows(sequence, [PipelineStage.BASELINE], True) == [True, False, False, True]
    assert services.unlocked_workflow_rows([], [], True) == []
