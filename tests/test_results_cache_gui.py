"""Save/open wiring and the manual cache bypass, without native windows."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtWidgets
from GUI import main_window
from GUI.theme import build_application
from test_restore_process import fixture_project


@pytest.fixture
def window(monkeypatch):
    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda *a: None)
    window = main_window.MainWindow(theme_mode="light")
    yield window, app
    window.has_unsaved_changes = lambda: False
    window.close()
    app.processEvents()


def test_recalculate_action_and_explicit_save_only(window, fixture_project, monkeypatch):
    win, app = window
    scheduled = []
    cancelled = []
    warmer = main_window.results_cache_warmer
    monkeypatch.setattr(warmer, "schedule", scheduled.append)
    monkeypatch.setattr(warmer, "cancel", lambda: cancelled.append(True))
    assert not win.recalculate_project_action.isEnabled()
    win.resize(1280, 800)
    win.load_project_file(fixture_project, background=False)
    assert cancelled and win.recalculate_project_action.isEnabled()
    assert win.size().width() <= 1280 and win.size().height() <= 800
    win._write_project(fixture_project, sidecar=False, autosave=True)
    assert scheduled == []
    win._write_project(fixture_project, sidecar=False)
    assert scheduled == [fixture_project]
    calls = []
    monkeypatch.setattr(win, "load_project_file", lambda path, **kw: calls.append((path, kw)))
    win.recalculate_project_action.trigger()
    assert calls == [(fixture_project, {"use_cache": False})]
    cancelled.clear()
    win._cancel_restore()
    assert cancelled == [True]


def test_recalculate_asks_before_discarding_unsaved_changes(window, fixture_project, monkeypatch):
    from PySide6 import QtWidgets

    win, _app = window
    win.load_project_file(fixture_project, background=False)
    calls = []
    monkeypatch.setattr(win, "load_project_file", lambda path, **kw: calls.append((path, kw)))
    monkeypatch.setattr(win, "has_unsaved_changes", lambda: True)
    answers = [QtWidgets.QMessageBox.StandardButton.Cancel, QtWidgets.QMessageBox.StandardButton.Discard]
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: answers.pop(0))
    win.recalculate_project_action.trigger()
    assert calls == []
    win.recalculate_project_action.trigger()
    assert calls == [(fixture_project, {"use_cache": False})]
