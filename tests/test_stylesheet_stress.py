"""Offscreen guards for application repolishing and repeated tab lifetimes."""

from __future__ import annotations

import gc
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.icon import build_app_icon
from GUI.main import StudioWindowController
from GUI.main_window import MainWindow
from GUI.theme import apply_theme, build_application

FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"


def process_deletes(app):
    for _ in range(3):
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def test_same_theme_skips_repolishing_and_switches_change_stylesheet(app, monkeypatch):
    apply_theme(app, "dark")
    sheets = []
    palettes = []
    set_sheet = app.setStyleSheet
    set_palette = app.setPalette

    def record_sheet(sheet):
        sheets.append(sheet)
        set_sheet(sheet)

    def record_palette(palette):
        palettes.append(palette)
        set_palette(palette)

    monkeypatch.setattr(app, "setStyleSheet", record_sheet)
    monkeypatch.setattr(app, "setPalette", record_palette)
    apply_theme(app, "light")
    light = app.styleSheet()
    apply_theme(app, "light")
    assert sheets == [light]
    assert len(palettes) == 1
    assert app.property("upbrpy.theme_mode") == "light"
    assert app.property("upbrpy.resolved_theme") == "light"
    apply_theme(app, "dark")
    dark = app.styleSheet()
    assert dark != light
    apply_theme(app, "light")
    assert app.styleSheet() == light
    assert sheets == [light, dark, light]


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("setter", ["setPalette", "setStyleSheet"])
@pytest.mark.parametrize("raises", [False, True])
def test_repolishing_pauses_gc_and_restores_its_state(app, monkeypatch, enabled, setter, raises):
    apply_theme(app, "light")
    original = getattr(app, setter)
    observed = []

    def record(value):
        observed.append(gc.isenabled())
        if raises:
            raise RuntimeError("setter failed")
        original(value)

    was_enabled = gc.isenabled()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(app, setter, record)
            gc.enable() if enabled else gc.disable()
            if raises:
                with pytest.raises(RuntimeError, match="setter failed"):
                    apply_theme(app, "dark")
            else:
                apply_theme(app, "dark")
            assert observed == [False]
            assert gc.isenabled() is enabled
    finally:
        gc.enable() if was_enabled else gc.disable()
        apply_theme(app, "light")


@pytest.fixture
def controller(app, monkeypatch):
    settings = QtCore.QSettings("UPbRPy", "Studio")
    settings.clear()
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: pytest.fail(str(args[1:])))
    monkeypatch.setattr(MainWindow, "request_close", lambda self: True)
    controller = StudioWindowController(theme_mode="light", app_icon=build_app_icon())
    yield controller
    for shell in list(controller.windows.values()):
        shell.close()
    process_deletes(app)
    settings.clear()
    apply_theme(app, "light")


@pytest.mark.slow
def test_theme_switches_with_loaded_and_deferred_deleted_tabs(controller, app):
    shell = controller.create_window(show=False)
    shell.resize(1280, 800)
    shell.show()
    process_deletes(app)
    for round_index in range(10):
        # Retire the previous spare between rounds to bound repolishing cost.
        if shell.tabs.count() > 1:
            assert shell.close_tab(shell.tabs.count() - 1)
            process_deletes(app)
        shell.add_tab()
        reduction = shell.add_tab()
        reduction.load_experiment_directory(str(FIXTURE), background=False)
        assert reduction.session.loaded and reduction._active_worker is None
        mode = "dark" if round_index % 2 else "light"
        shell.active_reduction.apply_theme_mode(mode, persist=False)
        gc.collect()
        assert shell.close_tab(shell.tabs.indexOf(reduction))
        del reduction
        process_deletes(app)

    assert shell.tabs.count() == 2
    assert shell.active_reduction is not None
    actions = shell.menuBar().actions()
    assert [action.text() for action in actions] == [
        "File", "Workflow", "Calibration", "Results", "View", "Window", "Help",
    ]
    sources = shell.active_reduction._studio_menus
    assert [action.text() for action in actions] == [source.text() for source in sources]
    assert all(action.menu()._source is source.menu() for action, source in zip(actions, sources))
    assert app.property("upbrpy.resolved_theme") == mode
