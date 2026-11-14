"""Final-review database buttons share menu actions and confirmation flows."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from urllib.parse import urlsplit

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

import GUI.database as gui_database
from UPbRPy.pipeline import PipelineStage
from synthetic_bundles import write_raw_directory
from test_database_gui import app, controller, dialogs  # noqa: F401
from test_small_screens import clipped_widgets, process


ORIGIN = {
    "server": "https://lab.example.org",
    "session_id": "session-123",
    "session_key": "Synthetic_20Mar25",
    "run_id": "run-previous",
}


@pytest.fixture
def window(controller, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    monkeypatch.setattr("UPbRPy.io.auto_select_baseline_peak", lambda experiment, *a, **k: experiment.assign(
        baseline_start_times=0.0, baseline_end_times=4.0,
        plateau_start_times=8.0, plateau_end_times=16.0,
        signal_start_times=6.0, signal_end_times=18.0,
    ))
    raw = tmp_path / "Apatite_synthetic"
    write_raw_directory(raw)
    shell = controller.create_window(show=False)
    window = shell.active_reduction
    window.load_experiment_directory(str(raw), background=False)
    assert window.session.loaded
    return window


def set_stage(window, stage):
    window.workflow_step_list.setCurrentRow(window.workflow_preset.stage_sequence.index(stage))


def database_review(window, origin=None):
    window.session.origin = dict(ORIGIN if origin is None else origin)
    window.update_project_window_title()
    set_stage(window, PipelineStage.FINALIZE)


def panel_buttons(window):
    return (*window.step_database_buttons, *window.results_database_buttons)


def test_buttons_only_at_database_final_review(window):
    # The database status strip and the Submit to database step replaced these
    # panel buttons (2026-11-13); they stay hidden at every stage.
    for origin in (None, ORIGIN):
        window.session.origin = dict(origin) if origin else None
        window.update_project_window_title()
        window.inspector_tabs.setCurrentIndex(2)  # Results
        for stage in window.workflow_preset.stage_sequence:
            set_stage(window, stage)
            assert all(not button.isVisibleTo(window) for button in panel_buttons(window))


@pytest.mark.parametrize("index", [0, 1], ids=["submit", "prefer"])
def test_button_state_and_tooltip_follow_action(window, index):
    database_review(window)
    action = (window.submit_to_database_action, window.prefer_run_action)[index]
    for enabled in (False, True):
        action.setEnabled(enabled)
        action.setToolTip(f"Updated reason: {enabled}")
        for buttons in (window.step_database_buttons, window.results_database_buttons):
            assert buttons[index].isEnabled() == enabled
            assert action.toolTip() in buttons[index].toolTip()
            if index == 0:
                shortcut = QtGui.QKeySequence("Ctrl+Shift+D").toString(QtGui.QKeySequence.SequenceFormat.NativeText)
                assert shortcut in buttons[index].toolTip()


def test_first_submit_confirmation_has_no_superseded_run(window, monkeypatch):
    origin = {key: value for key, value in ORIGIN.items() if key != "run_id"}
    database_review(window, origin)
    assert all(not buttons[1].isEnabled() for buttons in (
        window.step_database_buttons, window.results_database_buttons,
    ))
    question = Mock(return_value=QtWidgets.QMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", question)
    window.submit_to_database_action.trigger()
    assert question.call_args.args[2] == (
        f"Submit this reduction to session {origin['session_key']} on {origin['server']} as a new run?"
    )


@pytest.mark.parametrize("panel", ["step_database_buttons", "results_database_buttons"])
@pytest.mark.parametrize("use_key", [True, False], ids=["session-key", "session-id"])
@pytest.mark.parametrize("operation", ["submit", "prefer"])
def test_confirmation_cancel_then_yes(window, dialogs, monkeypatch, panel, use_key, operation):
    origin = dict(ORIGIN)
    if not use_key:
        origin.pop("session_key")
    database_review(window, origin)
    bridge = window.database()
    export = Mock()
    upload = Mock(return_value=SimpleNamespace(run_id="run-new", counts={}, quarantine_items=[]))
    prefer = Mock(return_value={})
    monkeypatch.setattr(gui_database, "client", lambda: SimpleNamespace(submit_reduction=upload, prefer_run=prefer))
    monkeypatch.setattr("GUI.services.ensure_shared_pipeline", Mock(return_value=object()))
    monkeypatch.setattr("UPbRPy.reproducibility.export_reduction_bundle", export)
    monkeypatch.setattr("UPbRPy.project.write_reduction_project", Mock())
    monkeypatch.setattr(window, "apply_pending_project_edits", Mock())
    monkeypatch.setattr(window, "project_workspace_state", lambda: {})
    monkeypatch.setattr(window, "run_background", lambda title, fn, done: done(fn()))
    authorize = Mock(side_effect=lambda server, code, then, title: then())
    monkeypatch.setattr(bridge, "_authorize", authorize)
    monkeypatch.setattr(bridge, "run", lambda fn, done, **kwargs: done(fn()))
    log = Mock()
    monkeypatch.setattr(window, "append_log", log)
    questions = []
    answer = QtWidgets.QMessageBox.StandardButton.Cancel

    def question(parent, title, text, buttons, default):
        questions.append(text)
        assert parent is window
        assert buttons == (QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel)
        assert default == QtWidgets.QMessageBox.StandardButton.Cancel
        return answer

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", question)
    button = getattr(window, panel)[operation == "prefer"]
    button.click()
    export.assert_not_called()
    upload.assert_not_called()
    prefer.assert_not_called()
    authorize.assert_not_called()
    log.assert_not_called()
    window.apply_pending_project_edits.assert_not_called()
    label = origin.get("session_key") or origin["session_id"]
    if operation == "submit":
        expected = (f"Submit this reduction to session {label} on {origin['server']} as a new run?"
                    "\n\nIt will supersede the run this reduction submitted earlier "
                    f"(id {origin['run_id'][:8]}).")
    else:
        host = urlsplit(origin["server"]).netloc or origin["server"]
        expected = (f"Make the run this reduction submitted (id {origin['run_id'][:8]}) the preferred run "
                    f"of session {label} on {host}?")
    assert questions == [expected]
    answer = QtWidgets.QMessageBox.StandardButton.Yes
    button.click()
    assert questions == [expected, expected]
    if operation == "submit":
        export.assert_called_once()
        upload.assert_called_once()
        assert upload.call_args.kwargs["supersedes"] == origin["run_id"]
        prefer.assert_not_called()
    else:
        prefer.assert_called_once_with(origin["server"], origin["session_id"], origin["run_id"])
        export.assert_not_called()
        upload.assert_not_called()


@pytest.mark.parametrize("use_key", [True, False])
def test_submitted_and_preferred_status_name_session(window, dialogs, tmp_path, use_key):
    origin = dict(ORIGIN)
    if not use_key:
        origin.pop("session_key")
    database_review(window, origin)
    scratch = tmp_path / "export"
    scratch.mkdir()
    result = SimpleNamespace(run_id="run-created", counts={"spots": 3}, quarantine_items=[])
    window.database()._submitted(window, result, scratch)
    label = origin.get("session_key") or origin["session_id"]
    assert window.statusBar().currentMessage() == f"Submitted to {label} as a new run."
    assert window.session.origin["run_id"] == "run-created"
    assert not scratch.exists()
    assert any(kind == "box" and "id run-crea" in text for kind, title, text in dialogs)
    window.database()._preferred(window, "run-created")
    assert window.statusBar().currentMessage() == f"The submitted run is now preferred for session {label}."


def test_submit_shortcut_is_unique_and_routes_to_active_tab(window, app, monkeypatch):
    shell = window.window()
    submit_key = QtGui.QKeySequence("Ctrl+Shift+D")
    assert window.submit_to_database_action.shortcut() == submit_key
    assert [action for action in window.findChildren(QtGui.QAction) if submit_key in action.shortcuts()] == [window.submit_to_database_action]
    second = shell.add_tab()
    submitted = []
    for reduction in (window, second):
        monkeypatch.setattr(reduction, "database", lambda: SimpleNamespace(submit=submitted.append))
    messages = []
    previous = QtCore.qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    try:
        shell.show()
        shell.activateWindow()
        for reduction in (second, window, second):
            shell.tabs.setCurrentWidget(reduction)
            reduction.trace_plot.setFocus()
            process(app)
            reduction.submit_to_database_action.setEnabled(True)
            QtTest.QTest.keyClick(reduction.trace_plot, QtCore.Qt.Key.Key_D,
                                 QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier)
            process(app)
        assert submitted == [second, window, second]
        assert not any("ambiguous" in message.lower() for message in messages), messages
    finally:
        QtCore.qInstallMessageHandler(previous)


@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_database_review_panels_fit_laptop(window, app, size):
    database_review(window)
    shell = window.window()
    shell.show()
    shell.resize(*size)
    window.apply_default_layout()
    window.inspector_tabs.setCurrentIndex(2)
    process(app)
    assert shell.size() == QtCore.QSize(*size)
    # The replaced panel buttons are hidden; the strip and Submit page have
    # their own fit test (test_database_panel.py).
    assert all(not button.isVisibleTo(window) for button in panel_buttons(window))
    assert not clipped_widgets(shell)
    scroll = window.inspector_tabs.currentWidget()
    assert scroll.horizontalScrollBar().maximum() == 0
