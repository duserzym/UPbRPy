"""Optional GUI tests for independent multi-session window management."""

from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtGui  # noqa: E402

from GUI.icon import build_app_icon  # noqa: E402
from GUI.main import StudioWindowController  # noqa: E402
from GUI.theme import build_application  # noqa: E402


def test_window_controller_keeps_reduction_sessions_independent():
    """Changing one open reduction must not mutate another window's state."""
    application = build_application([], theme_mode="light")
    controller = StudioWindowController(
        theme_mode="light",
        app_icon=build_app_icon(),
    )
    first = controller.create_window(show=False)
    second = controller.create_window(show=False)

    first.session.primary_channel = "29Si"
    second.session.primary_channel = "44Ca"

    assert first.session is not second.session
    assert first.session.primary_channel == "29Si"
    assert second.session.primary_channel == "44Ca"
    assert len(controller.windows) == 2

    action_texts = {
        action.text()
        for action in first.findChildren(QtGui.QAction)
    }
    assert {
        "New Reduction Window",
        "Open Project in New Window…",
        "Open Experiment Folder in New Window…",
        "Next Reduction Window",
    } <= action_texts

    first.close()
    second.close()
    application.sendPostedEvents()
    application.processEvents()
