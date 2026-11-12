"""Session controls recover after a blank tab finishes loading."""

from pathlib import Path
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtTest, QtWidgets

from GUI import main_window
from GUI.studio_window import StudioWindow
from GUI.theme import build_application

RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def wait_idle(app, reduction):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        app.processEvents()
        if reduction._active_worker is None:
            return
        QtTest.QTest.qWait(10)
    pytest.fail("Timed out waiting for the session worker")


@pytest.fixture
def shell(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args[1:])))
    settings = QtCore.QSettings("UPbRPy", "Studio")
    settings.clear()
    shell = StudioWindow()
    shell.resize(1280, 800)
    shell.show()
    app.processEvents()
    yield shell
    for index in range(shell.tabs.count()):
        reduction = shell.tabs.widget(index)
        wait_idle(app, reduction)
        reduction.has_unsaved_changes = lambda: False
    shell.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    settings.clear()


def assert_released(reduction):
    assert reduction.session.loaded
    controls = [reduction.results_table, reduction.results_table.tabs, reduction.fit_card,
                reduction.save_project_button, reduction.export_results_button,
                reduction.repick_all_spots_button, *reduction._session_actions]
    for control in controls:
        assert control.isEnabled(), control
        assert not control.property("blankDisabled"), control
        assert control.toolTip() != "Load a session first", control
    tabs = reduction.results_table.tabs
    tabs.setCurrentIndex(0)
    tabs.setCurrentWidget(reduction.results_table.spot_concordia_page)
    assert tabs.currentWidget() is reduction.results_table.spot_concordia_page


def test_interactive_load_releases_blank_controls(shell, app, monkeypatch):
    reduction = shell.active_reduction
    assert not reduction.session.loaded
    assert not reduction.results_table.isEnabled()
    tooltip = reduction.results_table.property("loadedToolTip") or ""
    during_install = []
    install = reduction.install_loaded_session

    def record_install(*args, **kwargs):
        install(*args, **kwargs)
        during_install.append((reduction._active_worker is not None,
                               reduction.results_table.property("blankDisabled"),
                               reduction.results_table.toolTip()))

    monkeypatch.setattr(reduction, "install_loaded_session", record_install)
    reduction.load_experiment_directory(str(RICH), background=True, offer_restore=False)
    wait_idle(app, reduction)
    assert during_install == [(True, True, "Load a session first")]
    assert_released(reduction)
    assert reduction.results_table.toolTip() == tooltip


@pytest.mark.parametrize("open_method", ["add_tab", "open_source"])
def test_second_interactive_tab_releases_blank_controls(shell, app, open_method):
    first = shell.active_reduction
    first.load_experiment_directory(str(RICH), background=True, offer_restore=False)
    wait_idle(app, first)
    if open_method == "add_tab":
        second = shell.add_tab()
        second.load_experiment_directory(str(RICH), background=True, offer_restore=False)
    else:
        second = shell.open_source(str(RICH))
    wait_idle(app, second)
    assert shell.tabs.count() == 2
    assert shell.active_reduction is second
    assert_released(second)
    assert_released(first)


@pytest.mark.parametrize("route", ["foreground", "project", "project_foreground",
                                  "bundle_raw", "bundle_project", "reload"])
def test_other_session_install_routes_release_controls(shell, app, tmp_path, route):
    reduction = shell.active_reduction
    if route in {"project", "project_foreground", "bundle_project"}:
        reduction.load_experiment_directory(str(RICH), background=False, offer_restore=False)
        project = tmp_path / "session.upbr"
        reduction.save_project(project)
        reduction = shell.add_tab()
        if route == "bundle_project":
            reduction._open_session_bundle_here(SimpleNamespace(project_file=project))
        else:
            reduction.load_project_file(project, background=route == "project")
    elif route == "bundle_raw":
        reduction._open_session_bundle_here(SimpleNamespace(project_file=None, raw_directory=RICH))
    else:
        reduction.load_experiment_directory(str(RICH), background=False, offer_restore=False)
        if route == "reload":
            reduction.has_unsaved_changes = lambda: False
            reduction.reload_current_directory()
    wait_idle(app, reduction)
    assert_released(reduction)
