"""Initial links do their entire network chain while GUI construction is busy."""
import json
import threading
import time

import pytest
from PySide6 import QtCore, QtWidgets

from GUI import database
from GUI.launch import parse_open_target
from UPbRPy.integration import upbdb as db
from UPbRPy.pipeline import NewerFileError
from fake_upbdb import FakeState, InProcessUpbdb
from synthetic_bundles import write_session_bundle


@pytest.fixture
def startup(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    source = write_session_bundle(tmp_path / "source", session_id="session")
    project = (source / f"{source.name}.upbr").read_bytes()
    fake = InProcessUpbdb(FakeState(sessions={"session": source}, runs={"run": "session"},
                                  run_projects={"run": project}, preferred={"session": "run"}))
    previous = db.set_token_store(db.MemoryTokenStore())
    monkeypatch.setattr(db.tls, "request", fake.request)
    monkeypatch.setattr(db, "_requests", lambda: fake.transport(db.tls.request))
    monkeypatch.setattr(database, "database_availability", lambda: (True, ""))
    opened, contexts, errors, signins = [], [], [], []
    links = database.DatabaseLinks(
        open_bundle=lambda bundle: opened.append(bundle) or bundle,
        open_project=lambda path: opened.append(path) or path,
        parent_widget=lambda: None,
        settings=QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat))
    links.cache_root = tmp_path / "cache"
    monkeypatch.setattr(links, "_opened", lambda window, **context: contexts.append(context))
    monkeypatch.setattr(links, "show_error", lambda title, error: errors.append((title, error)))
    links.sign_in = lambda server, then: signins.append(then)
    yield app, fake, links, opened, contexts, errors, signins
    deadline = time.monotonic() + 10
    while links._workers and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not links._workers
    db.set_token_store(previous)


def drain(app, condition):
    deadline = time.monotonic() + 15
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert condition()


@pytest.mark.parametrize("route", ["run/run", "session/session", "session/session?fresh=1"])
def test_initial_network_finishes_before_gui_callbacks(startup, monkeypatch, route):
    app, fake, links, opened, contexts, errors, signins = startup
    finished = threading.Event()
    request = fake.request

    def tracked(method, url, **kwargs):
        reply = request(method, url, **kwargs)
        if url.endswith("/project") or ("fresh=1" in route and url.endswith("/export/upbrpy")):
            finished.set()
        return reply

    monkeypatch.setattr(db.tls, "request", tracked)
    code = fake.state.issue_code()
    separator = "&" if "?" in route else "?"
    target = parse_open_target(f"upbr://{route}{separator}server={fake.url}&code={code}"
                               "&acquisition=acq-1&via=upblab")
    queued = []
    assert links.start_initial_target(target, deliver=queued.append)
    # Deliberately keep the GUI thread busy, as the window constructor does.
    assert finished.wait(15)
    assert opened == contexts == []
    drain(app, lambda: bool(queued))
    assert opened == contexts == []
    queued.pop()()  # The first window has finished building.
    drain(app, lambda: bool(opened))
    assert len(opened) == 1 and not errors and not signins
    assert contexts[0]["acquisition"] == "acq-1" and contexts[0]["via"] == "upblab"
    assert sum(c.path == "/v1/auth/exchange" for c in fake.state.calls) == 1
    if route == "session/session":
        assert "preferred run" in contexts[0]["messages"][0]


def test_initial_expired_code_signin_does_not_exchange_twice(startup):
    app, fake, links, opened, contexts, errors, signins = startup
    target = parse_open_target(f"upbr://run/run?server={fake.url}&code=expired")
    assert links.start_initial_target(target)
    drain(app, lambda: bool(signins))
    assert not opened and not errors
    db.store_token(fake.url, fake.state.issue_token())
    signins.pop()()
    drain(app, lambda: bool(opened))
    assert sum(c.path == "/v1/auth/exchange" for c in fake.state.calls) == 1


def test_initial_newer_project_is_refused(startup):
    app, fake, links, opened, contexts, errors, signins = startup
    document = json.loads(fake.state.run_projects["run"])
    document["pipeline"]["config"]["future_scientific_option"] = True
    fake.state.run_projects["run"] = json.dumps(document).encode()
    code = fake.state.issue_code()
    queued = []
    links.start_initial_target(
        parse_open_target(f"upbr://run/run?server={fake.url}&code={code}"), deliver=queued.append)
    drain(app, lambda: bool(queued))
    assert not errors
    queued.pop()()
    assert not opened and not contexts and not signins
    assert errors[0][0] == "Newer Studio file"
    assert isinstance(errors[0][1], NewerFileError)


def test_initial_result_activates_a_window_opened_in_the_meantime(startup, monkeypatch):
    app, fake, links, opened, contexts, errors, signins = startup
    code = fake.state.issue_code()
    existing = object()
    monkeypatch.setattr(links, "activate_project", lambda **identity: existing)
    links.start_initial_target(parse_open_target(f"upbr://run/run?server={fake.url}&code={code}&via=upblab"))
    drain(app, lambda: bool(contexts))
    assert not opened and not errors
    assert contexts[0]["via"] == "upblab"
