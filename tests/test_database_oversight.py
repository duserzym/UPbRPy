"""Offscreen consent flows use the real client against the fake server."""

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

import GUI.database as gui
from UPbRPy.integration import upbdb as db
from test_database_gui import app, wait_for  # noqa: F401
from test_database_retry import case, start  # noqa: F401
from test_upbdb_oversight import OWNER, SESSION_ID, oversight_server, write_reduction  # noqa: F401


YES = QtWidgets.QMessageBox.StandardButton.Yes
CANCEL = QtWidgets.QMessageBox.StandardButton.Cancel
SUBMIT_QUESTION = (
    f"This session belongs to {OWNER}. Submit this run on their behalf? "
    "It will be recorded as your run on their session."
)
PREFER_QUESTION = f"Also mark it as the preferred run for {OWNER}'s session?"


@pytest.fixture
def live(case, oversight_server, monkeypatch):
    remote = oversight_server
    case.window.session.origin = {"server": remote.server.url, "session_id": SESSION_ID,
                                  "session_key": "Synthetic"}
    monkeypatch.setattr(gui, "client", lambda: db)
    case.export.side_effect = lambda pipeline, folder, **kwargs: write_reduction(folder, remote.source / "raw")
    case.remote = remote
    return case


def settled(case):
    assert wait_for(lambda: not case.links._actions and not case.links._workers)


def questions(case):
    return [c.args[2] for c in case.questions.call_args_list]


def test_confirm_submit_reuses_key_and_never_prefers(live):
    threads = []

    def confirm(*args):
        threads.append(QtCore.QThread.currentThread())
        if args[2] == SUBMIT_QUESTION:
            assert len(live.remote.state.calls) == 1
            assert not live.remote.state.runs
            live.links.retry_operation(live.window, "submit")  # nested modal event loop
            live.links.submit(live.window)
            assert len(live.remote.state.calls) == 1
        return YES

    live.questions.side_effect = confirm
    live.links.submit(live.window)
    settled(live)
    calls = live.remote.state.calls
    assert questions(live)[1:] == [SUBMIT_QUESTION]
    assert threads == [QtCore.QThread.currentThread()] * 2
    assert [c.headers.get("X-UPbDB-Oversight") for c in calls] == [None, "on-behalf"]
    assert len({c.headers["Idempotency-Key"] for c in calls}) == 1
    assert calls[0].body == calls[1].body
    assert len(live.remote.state.runs) == len(live.remote.state.imports) == 1
    assert not live.remote.state.preferred
    assert live.window.session.origin["run_id"] == "run-1"
    live.export.assert_called_once()


def test_cancel_submit_sends_no_second_request(live):
    live.questions.side_effect = [YES, CANCEL]
    live.links.submit(live.window)
    settled(live)
    assert questions(live)[1:] == [SUBMIT_QUESTION]
    assert len(live.remote.state.calls) == 1
    assert not live.remote.state.runs
    assert "run_id" not in live.window.session.origin
    live.links.retry_operation(live.window, "submit")
    assert len(live.remote.state.calls) == 1


@pytest.mark.parametrize("confirm", [YES, CANCEL])
@pytest.mark.parametrize("after_submit", [False, True], ids=["standalone", "after-on-behalf-submit"])
def test_prefer_requires_its_own_owner_confirmation(live, confirm, after_submit):
    if after_submit:
        live.links.submit(live.window)
        settled(live)
        assert not live.remote.state.preferred
    else:
        live.remote.state.runs["run-1"] = SESSION_ID
        live.window.session.origin["run_id"] = "run-1"
    live.remote.state.calls.clear()
    live.questions.reset_mock()

    def answer(parent, title, text, *args):
        if text == PREFER_QUESTION:
            assert not live.remote.state.preferred
            assert len(live.remote.state.calls) == 1  # rejected discovery request only
            return confirm
        assert not live.remote.state.calls
        return YES

    live.questions.side_effect = answer
    live.links.prefer(live.window)
    settled(live)
    assert questions(live)[1:] == [PREFER_QUESTION]
    calls = live.remote.state.calls
    assert len(calls) == (2 if confirm == YES else 1)
    assert calls[0].headers.get("X-UPbDB-Oversight") is None
    assert live.remote.state.preferred == ({SESSION_ID: "run-1"} if confirm == YES else {})
    if confirm == YES:
        assert calls[1].headers["X-UPbDB-Oversight"] == "on-behalf"
        assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]


@pytest.mark.parametrize("role", ["Write", "Admin", "owner"])
def test_ordinary_access_needs_no_oversight_prompt(live, role):
    if role == "owner":
        live.remote.state.session_owners[SESSION_ID] = live.remote.state.username
    else:
        live.remote.state.explicit_roles[SESSION_ID] = role
    live.links.submit(live.window)
    settled(live)
    live.links.prefer(live.window)
    settled(live)
    assert len(questions(live)) == 2  # ordinary Submit and Prefer questions
    assert SUBMIT_QUESTION not in questions(live) and PREFER_QUESTION not in questions(live)
    assert len(live.remote.state.calls) == 2
    assert all("X-UPbDB-Oversight" not in c.headers for c in live.remote.state.calls)
    assert live.remote.state.preferred == {SESSION_ID: "run-1"}
    print(f"Ordinary {role}: 2 requests (submit + prefer), "
          f"{sum(live.remote.timings) * 1000:.2f} ms; no per-row requests.")


def test_lost_confirmed_response_replays_same_run_without_another_prompt(live):
    original = live.remote.adapter.request
    lost = False

    def request(*args, **kwargs):
        nonlocal lost
        reply = original(*args, **kwargs)
        if reply.status_code == 201 and not lost:
            lost = True
            raise TimeoutError("Response lost after run was created")
        return reply

    live.remote.adapter.request = request
    live.links.submit(live.window)
    assert wait_for(lambda: live.events and live.events[-1][1] == "queued")
    assert len(live.remote.state.runs) == 1
    live.links.retry_operation(live.window, "submit")
    settled(live)
    calls = live.remote.state.calls
    assert len(calls) == 3
    assert len({c.headers["Idempotency-Key"] for c in calls}) == 1
    assert len({c.body for c in calls}) == 1
    assert len(live.remote.state.runs) == len(live.remote.state.imports) == 1
    assert questions(live)[1:] == [SUBMIT_QUESTION]


def test_consent_does_not_apply_to_next_submit(live):
    for _ in range(2):
        live.links.submit(live.window)
        settled(live)
    assert questions(live).count(SUBMIT_QUESTION) == 2
    calls = live.remote.state.calls
    assert [c.headers.get("X-UPbDB-Oversight") for c in calls] == [None, "on-behalf"] * 2
    assert calls[0].headers["Idempotency-Key"] != calls[2].headers["Idempotency-Key"]
    assert len(live.remote.state.runs) == 2
    assert live.remote.state.imports[1][1] == "run-1"


def test_repeated_oversight_error_never_loops(case):
    case.fake.submit_reduction.side_effect = db.OversightConfirmationRequired(OWNER)
    start(case, "submit")
    assert wait_for(lambda: case.events and case.events[-1][1] == "error")
    assert case.fake.submit_reduction.call_count == 2
    assert questions(case)[1:] == [SUBMIT_QUESTION]
    action = next(iter(case.links._actions.values()))
    assert not action.timer.isActive()
    assert action.on_behalf
