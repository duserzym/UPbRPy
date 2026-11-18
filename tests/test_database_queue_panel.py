"""Retry controls and accepted tab closure use the retained database action."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtWidgets

import GUI.database as gui
from UPbRPy.integration import upbdb as db
from test_database_gui import app, controller, wait_for  # noqa: F401
from test_database_submit_buttons import window  # noqa: F401


def test_inline_retry_stop_and_close_tab(window, monkeypatch):
    panel = window.database_panel
    panel.select_step()
    links = window.database()
    calls = Mock(side_effect=db.ConnectionFailed("offline"))
    monkeypatch.setattr(gui, "client", lambda: SimpleNamespace(list_sessions=calls))
    monkeypatch.setattr(links, "choose_server", lambda: "https://lab.example.org")
    monkeypatch.setattr(links, "_authorize", lambda server, code, then, title: then())
    for name in ("question", "information", "warning", "critical", "exec"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, Mock(side_effect=AssertionError("No message box")))
    panel.link.primary.click()
    assert wait_for(lambda: panel.link.primary.text() == "Retry now")
    action = links._actions[id(window), "link"]
    assert "unreachable" in panel.link.status.text()
    assert "Queued: will link when lab.example.org answers" in panel.link.detail.toolTip()
    assert panel.link.secondary.text() == "Stop"
    assert panel.link.secondary.isEnabled()
    panel.link.primary.click()
    assert wait_for(lambda: calls.call_count == 2 and action.timer.isActive())
    panel.link.secondary.click()
    assert not action.timer.isActive()
    assert not links._actions
    panel.link.primary.click()
    assert wait_for(lambda: calls.call_count == 3)
    assert wait_for(lambda: panel.link.primary.text() == "Retry now")
    action = links._actions[id(window), "link"]
    monkeypatch.setattr(window, "has_unsaved_changes", lambda: False)
    shell = window.window()
    assert shell.close_tab(shell.tabs.indexOf(window))
    assert not action.timer.isActive()
    assert not links._actions
