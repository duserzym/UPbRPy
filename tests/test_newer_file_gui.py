"""Newer projects produce one actionable dialog and leave the session intact."""

import json
import os
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets

from GUI import main_window, newer_file
from GUI.main_window import MainWindow
from GUI.theme import build_application
from UPbRPy.pipeline import NewerFileError
from UPbRPy.project import PROJECT_FORMAT_NAME, PROJECT_FORMAT_VERSION


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.mark.parametrize("choice", ["Update Studio…", "Close"])
@pytest.mark.parametrize("callback", [False, True])
def test_refusal_dialog_message_buttons_and_update(app, monkeypatch, choice, callback):
    error = NewerFileError("9.0.0", "0.1.4")
    calls = []
    titles = []
    set_title = QtWidgets.QMessageBox.setWindowTitle

    def record_title(dialog, title):
        titles.append(title)
        set_title(dialog, title)

    def execute(dialog):
        # QMessageBox ignores its native window title on macOS.
        assert titles == ["Newer Studio file"]
        assert dialog.text() == str(error)
        assert not dialog.detailedText()
        assert sorted(button.text() for button in dialog.buttons()) == ["Close", "Update Studio…"]
        next(button for button in dialog.buttons() if button.text() == choice).click()
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", execute)
    monkeypatch.setattr(QtWidgets.QMessageBox, "setWindowTitle", record_title)
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda url: calls.append(url.toString()))
    newer_file.show_newer_file_dialog(None, error,
                                     open_updates=(lambda: calls.append("updates")) if callback else None)
    expected = "updates" if callback else "https://github.com/duserzym/UPbRPy/releases/latest"
    assert calls == ([expected] if choice == "Update Studio…" else [])


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow()
    yield window
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def newer_project(tmp_path):
    path = tmp_path / "future.upbr"
    path.write_text(json.dumps({"format": PROJECT_FORMAT_NAME,
                                "format_version": PROJECT_FORMAT_VERSION + 1,
                                "saved_by": {"version": "9.0.0"}}))
    return path


@pytest.mark.parametrize("background", [False, True])
def test_main_open_refuses_before_changing_session(window, monkeypatch, newer_project, background):
    before = newer_project.read_bytes()
    session = window.session
    shown = []
    updates = []
    monkeypatch.setattr(window.version_chip, "open_dialog", lambda **kw: updates.append(kw))

    def refusal(parent, error, **kwargs):
        shown.append((parent, error))
        kwargs["open_updates"]()

    monkeypatch.setattr(newer_file, "show_newer_file_dialog", refusal)
    monkeypatch.setattr(window, "_handle_action_error", lambda *a: pytest.fail("generic error path"))
    window.load_project_file(newer_project, background=background)
    assert len(shown) == 1 and shown[0][0] is window
    assert shown[0][1].saved_version == "9.0.0"
    assert updates == [{"manual": True}]
    assert window.session is session
    assert window.project_path is None
    assert window._active_worker is None
    assert window.workflow_combo.isEnabled()
    assert newer_project.read_bytes() == before


def test_background_restore_refusal_uses_helper_and_unlocks(window, app, monkeypatch):
    shown = []
    monkeypatch.setattr(newer_file, "show_newer_file_dialog", lambda *a, **kw: shown.append(a))

    def fail():
        raise NewerFileError("9.0.0")

    window.run_background("Opening project", fail, lambda result: pytest.fail("installed refused file"))
    deadline = time.monotonic() + 10
    while window._active_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert window._active_worker is None
    assert len(shown) == 1
    assert window.workflow_combo.isEnabled()
    assert window.issue_banner.isHidden()


def test_tabbed_open_refuses_before_adding_tab(app, monkeypatch, newer_project):
    from GUI.main import StudioWindowController

    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    controller = StudioWindowController(theme_mode="light", app_icon=QtGui.QIcon())
    shell = controller.create_window(show=False)
    shown = []
    monkeypatch.setattr(newer_file, "show_newer_file_dialog", lambda *a, **kw: shown.append(a))
    # Force the content allocator to need another tab if it were called.
    shell.active_reduction.session.directory = "/existing/session"
    count = shell.tabs.count()
    try:
        assert shell.open_project(str(newer_project)) is None
        assert controller.open_project_in_window(str(newer_project)) is None
        assert shell.tabs.count() == count
        assert len(shown) == 2
    finally:
        for index in range(shell.tabs.count()):
            shell.tabs.widget(index).has_unsaved_changes = lambda: False
        shell.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_database_cached_open_uses_refusal_without_network(app, monkeypatch, newer_project):
    from GUI import database

    shown = []
    monkeypatch.setattr(database, "find_project_for_run", lambda *a: newer_project)
    monkeypatch.setattr(database, "show_newer_file_dialog", lambda *a, **kw: shown.append(a))
    links = database.DatabaseLinks(open_bundle=lambda *a: pytest.fail("opened bundle"),
                                   open_project=lambda *a: pytest.fail("opened project"),
                                   parent_widget=lambda: None)
    monkeypatch.setattr(links, "run", lambda *a, **kw: pytest.fail("network call"))
    links.open_run("https://example.test", "run")
    assert len(shown) == 1
    assert isinstance(shown[0][1], NewerFileError)


def test_database_worker_refusal_bypasses_retry_handler(app, monkeypatch):
    from GUI import database

    shown = []
    monkeypatch.setattr(database, "show_newer_file_dialog", lambda *a, **kw: shown.append(a))
    links = database.DatabaseLinks(open_bundle=lambda *a: None, open_project=lambda *a: None,
                                   parent_widget=lambda: None)

    def fail():
        raise NewerFileError("9.0.0")

    links.run(fail, lambda *a: pytest.fail("opened refused project"), title="Open reduction run",
              on_failure=lambda *a: pytest.fail("retried compatibility refusal"))
    deadline = time.monotonic() + 10
    while links._workers and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert not links._workers
    assert len(shown) == 1


def test_sidecar_restore_refuses_without_rewriting(window, monkeypatch, newer_project):
    sidecar = main_window.selection_autosave_path("/example/source")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(newer_project.read_bytes())
    before = sidecar.read_bytes()
    shown = []
    monkeypatch.setattr(newer_file, "show_newer_file_dialog", lambda *a, **kw: shown.append(a))
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: QtWidgets.QMessageBox.DialogCode.Accepted)
    assert window._restore_selection_sidecar("/example/source", background=False)
    assert len(shown) == 1
    assert window.project_path is None
    assert sidecar.read_bytes() == before
