"""Database queues retain prepared calls and report failures without dialogs."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

import GUI.database as gui
from GUI.state import ReductionSession
from UPbRPy.integration import upbdb as db
from test_database_gui import app, wait_for  # noqa: F401

SERVER = "https://lab.example.org"
ORIGIN = {"server": SERVER, "session_id": "session-1", "session_key": "Synthetic"}
RESULT = db.ImportResult("run-1", {"spots": 3})


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.session = ReductionSession(directory="/tmp/raw", experiment=pd.DataFrame({"a": [1]}),
                                        total_data=pd.DataFrame({"a": [1]}))
        self.project_path = None
        self.workflow_preset = SimpleNamespace(supports_depth_profiles=False)
        self._active_worker = None
        self.logs = []
        self.apply_pending_project_edits = Mock()
        self.project_workspace_state = lambda: {}
        self.current_workflow_stage = lambda: None
        self.update_project_window_title = Mock()
        self.save_project = Mock()
        self.has_unsaved_changes = lambda: False
        self.append_log = self.logs.append


@pytest.fixture
def case(app, monkeypatch, tmp_path):
    window = Window()
    links = gui.DatabaseLinks(open_bundle=lambda bundle: None, open_project=lambda path: None,
                              parent_widget=lambda: window,
                              settings=QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.IniFormat))
    events = []
    links.operation_changed.connect(lambda session, op, state, value: events.append((op, state, value)))
    monkeypatch.setattr(gui, "database_availability", lambda: (True, ""))
    monkeypatch.setattr(links, "_authorize", lambda server, code, then, title: then())
    monkeypatch.setattr(links, "choose_server", lambda: SERVER)
    monkeypatch.setattr(gui, "_session_key_candidates", lambda window: ["Synthetic"])
    monkeypatch.setattr(gui, "_verified_session_origin", lambda *args: (ORIGIN, "Verified raw files."))
    monkeypatch.setattr(gui, "notify_lab", Mock())
    monkeypatch.setattr(gui.random, "uniform", lambda a, b: 1)
    monkeypatch.setattr("GUI.services.ensure_shared_pipeline", lambda *args: object())
    exported = Mock(side_effect=lambda pipeline, folder, **kwargs: Path(folder).mkdir(parents=True))
    monkeypatch.setattr("UPbRPy.reproducibility.export_reduction_bundle", exported)
    monkeypatch.setattr("UPbRPy.project.write_reduction_project", Mock())
    picker = Mock()

    def pick(dialog):
        picker()
        dialog.accept()
        return QtWidgets.QDialog.Accepted

    monkeypatch.setattr(gui.SessionPickerDialog, "exec", pick)
    questions = Mock(return_value=QtWidgets.QMessageBox.Yes)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", questions)
    for name in ("information", "warning", "critical", "exec"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, Mock(side_effect=AssertionError("Unexpected message box")))
    fake = SimpleNamespace(list_sessions=Mock(return_value=[{"id": "session-1", "session_key": "Synthetic"}]),
                           fetch_session_bundle=Mock(return_value=SimpleNamespace(session_id="session-1")),
                           submit_reduction=Mock(return_value=RESULT), prefer_run=Mock(return_value={}))
    monkeypatch.setattr(gui, "client", lambda: fake)
    yield SimpleNamespace(window=window, links=links, events=events, fake=fake,
                          export=exported, questions=questions, picker=picker)
    links.stop_window(window)
    assert wait_for(lambda: not links._workers)
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def start(case, operation):
    if operation != "link":
        case.window.session.origin = dict(ORIGIN)
    if operation == "prefer":
        case.window.session.origin["run_id"] = "run-1"
    getattr(case.links, "link_session" if operation == "link" else operation)(case.window)


def queued(case):
    assert wait_for(lambda: case.events and case.events[-1][1] == "queued")
    action = next(iter(case.links._actions.values()))
    assert action.timer.isActive()
    assert action.timer.thread() == QtCore.QThread.currentThread()
    assert "Server unreachable:" in case.events[-1][2]
    assert "Will retry in 5 s" in case.events[-1][2]
    assert "open tab only" in case.events[-1][2]
    return action


@pytest.mark.parametrize("phase", ["list", "verify", "submit", "prefer"])
def test_unreachable_retries_same_call_twice_then_succeeds(case, phase):
    method = {"list": "list_sessions", "verify": "fetch_session_bundle",
              "submit": "submit_reduction", "prefer": "prefer_run"}[phase]
    call = getattr(case.fake, method)
    success = call.return_value
    call.side_effect = [db.ConnectionFailed("offline"), db.ConnectionFailed("offline"), success]
    operation = "link" if phase in {"list", "verify"} else phase
    if phase == "submit":
        # Export uses Studio's guarded scientific worker exactly once; upload
        # retries use the database pool without locking the reduction controls.
        case.window.run_background = Mock(side_effect=lambda label, fn, done: done(fn()))
    start(case, operation)
    action = queued(case)
    key = action.key
    assert uuid.UUID(key)
    folder = call.call_args.args[2] if phase == "submit" else None
    scratch = action.scratch
    case.links.retry_operation(case.window, operation)
    assert wait_for(lambda: call.call_count == 2 and action.timer.isActive())
    assert "Will retry in 10 s" in case.events[-1][2]
    # Exercise the automatic timer too, without sleeping through the backoff.
    action.deadline = time.monotonic() - 1
    assert wait_for(lambda: case.events[-1][1] == "done")
    assert call.call_count == 3
    assert not case.links._actions
    if phase != "list":
        assert case.fake.list_sessions.call_count == (1 if operation == "link" else 0)
    if phase == "verify":
        assert case.picker.call_count == 1
        assert all(c.args[:2] == (SERVER, "session-1") for c in call.call_args_list)
        case.window.save_project.assert_not_called()  # unnamed project
        assert case.window.session.origin == ORIGIN
    if phase in {"submit", "prefer"}:
        assert {c.kwargs["idempotency_key"] for c in call.call_args_list} == {key}
        case.questions.assert_called_once()
    if phase == "submit":
        assert all(c.args[2] == folder for c in call.call_args_list)
        case.export.assert_called_once()
        case.window.run_background.assert_called_once()
        assert not scratch.exists()
        assert case.window.session.origin["run_id"] == "run-1"
        gui.notify_lab.assert_called_once()


@pytest.mark.parametrize("phase", ["list", "verify", "submit"])
def test_server_answer_stops_retry_and_keeps_prepared_work(case, phase):
    method = {"list": "list_sessions", "verify": "fetch_session_bundle", "submit": "submit_reduction"}[phase]
    getattr(case.fake, method).side_effect = db.ServerError("server failed", status=503, details="maintenance")
    operation = "submit" if phase == "submit" else "link"
    start(case, operation)
    assert wait_for(lambda: case.events and case.events[-1][1] == "error")
    assert "lab.example.org answered: 503 maintenance" in case.events[-1][2]
    assert "administrator" in case.events[-1][2]
    action = next(iter(case.links._actions.values()))
    assert not action.timer.isActive()
    if phase == "submit":
        assert action.scratch.exists()
    case.links.stop_operation(case.window, operation)
    assert not case.links._actions


def test_409_retries_after_server_delay_with_same_key(case):
    case.fake.submit_reduction.side_effect = [
        db.RequestRejected("busy", status=409, code="idempotency_in_progress", retry_after=17), RESULT]
    start(case, "submit")
    assert wait_for(lambda: case.events and case.events[-1][1] == "queued")
    action = next(iter(case.links._actions.values()))
    assert "answered: 409" in case.events[-1][2]
    assert 16 < action.deadline - time.monotonic() <= 17
    case.links.retry_operation(case.window, "submit")
    assert wait_for(lambda: case.events[-1][1] == "done")
    assert len({c.kwargs["idempotency_key"] for c in case.fake.submit_reduction.call_args_list}) == 1


def test_stop_keeps_scratch_until_inflight_upload_settles_and_ignores_success(case):
    entered, release = threading.Event(), threading.Event()

    def upload(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return RESULT

    case.fake.submit_reduction.side_effect = upload
    start(case, "submit")
    assert wait_for(entered.is_set)
    action = next(iter(case.links._actions.values()))
    scratch = action.scratch
    try:
        case.links.stop_window(case.window)
        assert not action.timer.isActive()
        assert scratch.exists()
    finally:
        release.set()
    assert wait_for(lambda: not case.links._actions)
    assert not scratch.exists()
    assert "run_id" not in case.window.session.origin
    gui.notify_lab.assert_not_called()


def test_worker_preserves_exception_and_delivers_on_gui_thread(case):
    failure = db.PermissionDenied("forbidden", status=403)
    threads, errors = [], []

    def fail():
        threads.append(QtCore.QThread.currentThread())
        raise failure

    case.links.run(fail, lambda result: pytest.fail("unexpected success"), title="Test",
                   on_failure=lambda exc: errors.append((exc, QtCore.QThread.currentThread())))
    assert wait_for(lambda: errors)
    assert errors == [(failure, QtCore.QThread.currentThread())]
    assert threads[0] != QtCore.QThread.currentThread()
    case.window.run_background = lambda label, fn, done: case.links.run(
        fn, done, title=label, on_failure=lambda error: None)
    case.export.side_effect = ValueError("Cannot export this bundle")
    start(case, "submit")
    assert wait_for(lambda: case.events and case.events[-1][1] == "error")
    assert case.events[-1][2] == "Cannot export this bundle"
    case.fake.submit_reduction.assert_not_called()


def test_submit_confirmation_cannot_stack_or_repeat_on_retry(case, monkeypatch):
    def question(*args):
        case.links.submit(case.window)
        case.links.prefer(case.window)
        case.links.link_session(case.window)
        return QtWidgets.QMessageBox.Yes

    case.questions.side_effect = question
    case.fake.submit_reduction.side_effect = db.ConnectionFailed("offline")
    start(case, "submit")
    queued(case)
    case.links.submit(case.window)
    case.questions.assert_called_once()
    case.links.retry_operation(case.window, "submit")
    assert wait_for(lambda: case.fake.submit_reduction.call_count == 2)
    case.questions.assert_called_once()
    assert wait_for(lambda: not next(iter(case.links._actions.values())).in_flight)
    case.links.stop_window(case.window)
    case.window.session.origin = None
    page = [{"id": "session-1", "session_key": "Synthetic"}]
    case.fake.list_sessions.side_effect = [db.ConnectionFailed("offline"), page]
    start(case, "link")
    action = queued(case)

    def another_dialog():
        action.retry_now()
        assert wait_for(lambda: not action.in_flight)
        assert action.valid()  # A guarded picker waits instead of canceling.
        case.picker.assert_not_called()

    case.links._flow_dialog(case.window, another_dialog)
    assert wait_for(lambda: case.events[-1][1] == "done")
    assert case.fake.list_sessions.call_count == 2
    case.picker.assert_called_once()


def test_backoff_caps_and_session_replacement_stops_timer(case):
    case.fake.list_sessions.side_effect = db.ConnectionFailed("offline")
    start(case, "link")
    action = queued(case)
    action.failures = 20
    case.links.retry_operation(case.window, "link")
    assert wait_for(lambda: action.timer.isActive())
    assert 299 < action.deadline - time.monotonic() <= 300
    case.window.session = ReductionSession()
    action._tick()
    assert not action.timer.isActive()
    assert not case.links._actions


def test_stop_detaches_browser_continuation_and_never_submits_later(case, monkeypatch):
    def authorize(server, code, then, title):
        case.links.pending_sign_in = (server, then, "browser-state")

    monkeypatch.setattr(case.links, "_authorize", authorize)
    start(case, "submit")
    assert wait_for(lambda: case.links.pending_sign_in is not None)
    action = next(iter(case.links._actions.values()))
    resume = case.links.pending_sign_in[1]
    scratch = action.scratch
    case.links.stop_operation(case.window, "submit")
    assert case.links.pending_sign_in == (SERVER, None, "browser-state")
    resume()
    case.fake.submit_reduction.assert_not_called()
    assert not scratch.exists()


@pytest.mark.parametrize("error, next_step", [
    (db.AuthenticationRequired("expired", status=401, details="token expired"), "Sign in again"),
    (db.PermissionDenied("denied", status=403, details="not shared"), "administrator"),
    (db.NotFound("missing", status=404, details="no session"), "administrator"),
    (db.RequestRejected("invalid", status=422, details="unknown hashes"), "Check the bundle"),
])
def test_answered_errors_offer_next_step_without_retry(case, error, next_step):
    case.fake.list_sessions.side_effect = error
    start(case, "link")
    assert wait_for(lambda: case.events and case.events[-1][1] == "error")
    assert f"answered: {error.status}" in case.events[-1][2]
    assert next_step in case.events[-1][2]
    assert not next(iter(case.links._actions.values())).timer.isActive()
