"""Project restores retain useful trace defaults when no saved channels survive."""

from __future__ import annotations

import json
import os

import pytest

from GUI.services import (
    default_visible_channels,
    load_session,
    restore_session_from_project,
)
from synthetic_bundles import write_session_bundle


EMPTY_WORKSPACES = [
    pytest.param({}, id="missing"),
    pytest.param({"visible_channels": []}, id="empty"),
    pytest.param({"visible_channels": ["NotAChannel"]}, id="unavailable"),
    pytest.param({"visible_channels": None}, id="null"),
    pytest.param({"visible_channels": "206Pb"}, id="string"),
    pytest.param({"visible_channels": {"206Pb": True}}, id="mapping"),
    pytest.param({"visible_channels": 42}, id="number"),
]


def project_with_workspace(tmp_path, workspace):
    bundle = write_session_bundle(tmp_path)
    project = bundle / f"{bundle.name}.upbr"
    document = json.loads(project.read_text(encoding="utf-8"))
    document["workspace"] = workspace
    project.write_text(json.dumps(document), encoding="utf-8")
    return project


@pytest.mark.parametrize("workspace", EMPTY_WORKSPACES)
def test_project_without_usable_channels_matches_folder_defaults(tmp_path, workspace):
    project = project_with_workspace(tmp_path, workspace)
    restored = restore_session_from_project(project)
    session = restored.session
    folder_session = load_session(
        session.directory,
        primary_channel=session.primary_channel,
        preset=restored.preset,
    )

    expected = default_visible_channels(session.channels, session.primary_channel)
    assert expected
    assert session.visible_channels == expected
    assert session.visible_channels == folder_session.visible_channels


@pytest.mark.parametrize("saved", [
    pytest.param(["238U", "44Ca"], id="two-real-channels"),
    pytest.param(["NotAChannel", "238U", "44Ca", "Missing"], id="filtered-order"),
])
def test_project_restores_nonempty_saved_channels_in_order(tmp_path, saved):
    project = project_with_workspace(tmp_path, {"visible_channels": saved})

    session = restore_session_from_project(project).session

    assert session.visible_channels == ["238U", "44Ca"]


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from GUI.theme import build_application

    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.mark.parametrize("workspace", EMPTY_WORKSPACES[:3])
def test_project_defaults_activate_chips_and_trace_toggle(app, tmp_path, monkeypatch, workspace):
    from PySide6 import QtCore, QtWidgets
    from GUI.main_window import MainWindow

    project = project_with_workspace(tmp_path, workspace)
    settings = QtCore.QSettings("UPbRPy", "Studio")
    settings.clear()
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "critical",
        lambda *args: pytest.fail(str(args[1:])),
    )
    window = MainWindow(theme_mode="light")
    try:
        window.load_project_file(project, background=False)
        app.processEvents()
        assert window.session.loaded
        defaults = default_visible_channels(
            window.session.channels, window.session.primary_channel,
        )
        # The grid reports checked channels in instrument order.
        assert set(window.checked_channels()) == set(defaults)
        for channel, chip in window.isotope_grid._buttons.items():
            assert chip.tile_active == (channel in defaults)

        def trace_channels():
            return {
                item.name()
                for item in window.trace_plot.plot_item.listDataItems()
                if item.name() in window.session.channels
            }

        assert trace_channels() == set(defaults)
        channel = defaults[0]
        chip = window.isotope_grid._buttons[channel]
        chip.doubleClicked.emit()
        app.processEvents()
        assert channel not in window.checked_channels()
        assert not chip.tile_active
        assert channel not in trace_channels()

        chip.doubleClicked.emit()
        app.processEvents()
        assert set(window.checked_channels()) == set(defaults)
        assert chip.tile_active
        assert trace_channels() == set(defaults)
        curve = next(
            item for item in window.trace_plot.plot_item.listDataItems()
            if item.name() == channel
        )
        x_values, y_values = curve.getData()
        assert len(x_values) == len(y_values) > 0
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
        settings.clear()
