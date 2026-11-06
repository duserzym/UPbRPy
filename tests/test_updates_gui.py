"""Offscreen update overlays and dialogs at laptop sizes in both themes."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import apply_theme, build_application
from GUI.update_widgets import UpdatesDialog, VersionChip
from GUI.updater import Updater
from GUI.updates import UpdateState


@pytest.fixture(scope="module")
def app():
    return build_application([], "light")


def pump(app):
    for _ in range(12):
        app.processEvents()


@pytest.fixture
def prefs(app):
    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    yield settings
    settings.clear()
    for key, value in saved.items():
        settings.setValue(key, value)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1440, 900), (1280, 800)])
def test_updates_fit_canvas_and_laptop(app, prefs, theme, size):
    apply_theme(app, theme)
    updater = Updater(preview="ready", version="0.1.0")
    shell = StudioWindow(theme_mode=theme, reduction_factory=lambda: MainWindow(theme_mode=theme, updater=updater))
    window = shell.active_reduction
    shell.show()
    dialog = None
    try:
        fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
        window.load_experiment_directory(str(fixture), background=False)
        shell.resize(*size)
        window.apply_default_layout()
        pump(app)
        assert (shell.width(), shell.height()) == size
        chip, canvas = window.version_chip, window.plots_stack
        assert chip.objectName() == "VersionChip" and chip.text() == "Update ready, restart"
        for page in range(canvas.count()):
            canvas.setCurrentIndex(page)
            pump(app)
            assert chip.isVisible() and canvas.rect().contains(chip.geometry())
            assert canvas.width() - chip.geometry().right() - 1 == 10
            assert canvas.height() - chip.geometry().bottom() - 1 == 10
            assert canvas.childAt(chip.geometry().center()) is chip
        canvas.setCurrentIndex(0)
        pump(app)
        output = Path(__file__).resolve().parents[1] / "docs/screenshots"
        if os.environ.get("UPBRPY_SCREENSHOTS") == "1":
            output.mkdir(exist_ok=True)
            assert shell.grab().save(str(output / f"studio_updates_chip_{theme}_{size[0]}x{size[1]}.png"))
        chip.click()
        dialog = chip._dialog
        pump(app)
        assert dialog.isVisible() and dialog.objectName() == "UpdatesDialog"
        assert dialog.heading.text() == "UPbRPy Studio v0.1.0"
        assert dialog.status.text() == "Version 0.2.0 is ready to install"
        assert dialog.notes.toPlainText() == "Publication export, faster canvas"
        assert dialog.restart_button.isVisible() and dialog.check_button.isEnabled()
        assert dialog.auto_checkbox.isChecked()
        assert dialog.channel_combo.itemText(1) == "Beta (pre-releases)"
        assert dialog.width() <= shell.width() and dialog.height() <= shell.height()
        assert shell.frameGeometry().contains(dialog.frameGeometry())
        for widget in dialog.findChildren(QtWidgets.QWidget):
            if widget.isVisibleTo(dialog) and not widget.isWindow():
                top = widget.mapTo(dialog, QtCore.QPoint())
                assert dialog.rect().contains(QtCore.QRect(top, widget.size()))
        if os.environ.get("UPBRPY_SCREENSHOTS") == "1":
            assert dialog.grab().save(str(output / f"studio_updates_dialog_{theme}_{size[0]}x{size[1]}.png"))
    finally:
        if dialog is not None:
            dialog.close()
        window.has_unsaved_changes = lambda: False
        shell.close()
        pump(app)
        apply_theme(app, "light")


@pytest.mark.parametrize("state,chip_text,status_text", [
    ("none", "v0.1.0", "You're up to date"),
    ("available", "Update available", "Version 0.2.0 is available"),
    ("downloading", "Downloading 43%", "Downloading 0.2.0, 43%"),
    ("ready", "Update ready, restart", "Version 0.2.0 is ready to install"),
    ("error", "Check failed", "Update check failed:"),
    ("unsupported", "v0.1.0", "Running from a checkout:"),
])
def test_preview_states_never_use_io(app, prefs, state, chip_text, status_text):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview performed I/O")

    updater = Updater(preview=state, version="0.1.0", opener=forbidden, runner=forbidden)
    updater.start_scheduler()
    updater.check(manual=True)
    updater.download()
    updater.install()
    assert not updater._timer.isActive() and not updater._jobs
    chip = VersionChip(updater)
    dialog = UpdatesDialog(updater)
    try:
        dialog.show()
        pump(app)
        assert chip.text() == chip_text
        assert status_text in dialog.status.text()
        assert dialog.check_button.isEnabled() == (state != "downloading")
        assert dialog.page_button.isVisible() == (state == "unsupported")
    finally:
        chip.deleteLater()
        dialog.close()
        dialog.deleteLater()
        pump(app)


def test_preferences_manual_feedback_and_theme_changes(app, prefs, monkeypatch):
    updater = Updater(preview="available", version="0.1.0")
    chip = VersionChip(updater)
    dialog = UpdatesDialog(updater)
    try:
        dialog.show()
        pump(app)
        dialog.auto_checkbox.setChecked(False)
        assert not updater.auto_download and dialog.download_button.isVisible()
        dialog.channel_combo.setCurrentIndex(1)
        assert updater.channel == "beta"
        assert Updater(preview="none").channel == "beta"
        apply_theme(app, "dark")
        assert "#e7ecf4" in chip.styleSheet() and "#e7ecf4" in dialog.styleSheet()
        apply_theme(app, "light")
        assert "#17202e" in chip.styleSheet()
        updater._state = UpdateState("none", manual=True)
        updater.stateChanged.emit(updater.state)
        assert chip.text() == "Up to date" and chip._none_timer.interval() == 5000
        chip._none_timer.timeout.emit()
        assert chip.text() == "v0.1.0"
        updater._state = UpdateState("checking")
        updater.stateChanged.emit(updater.state)
        assert chip.text() == "Checking" and not dialog.check_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        chip.deleteLater()
        pump(app)


def test_controller_shares_updater_and_help_checks(app, prefs, monkeypatch):
    from GUI.main import StudioWindowController
    monkeypatch.setenv("UPBRPY_UPDATE_PREVIEW", "ready")
    controller = StudioWindowController(theme_mode="light", app_icon=None)
    shell = controller.create_window(show=False)
    second = shell.add_tab()
    calls = []
    monkeypatch.setattr(controller.updater, "check", lambda manual=False: calls.append(manual))
    try:
        assert second.updater is shell.tabs.widget(0).updater is controller.updater
        help_menu = next(action.menu() for action in second._studio_menus if action.text() == "Help")
        action = next(action for action in help_menu.actions() if action.text() == "Check for updates")
        action.trigger()
        assert calls == [True] and second.version_chip._dialog.isVisible()
        second.version_chip._dialog.close()
    finally:
        shell.close()
        pump(app)


def test_updates_dialog_follows_a_moved_tab(app, prefs, monkeypatch):
    from GUI.main import StudioWindowController
    monkeypatch.setenv("UPBRPY_UPDATE_PREVIEW", "ready")
    controller = StudioWindowController(theme_mode="light", app_icon=None)
    source = controller.create_window(show=False)
    reduction = source.add_tab()
    chip = reduction.version_chip
    first = chip.open_dialog()
    target = controller.move_tab_to_new_window(source, reduction)
    try:
        assert chip.open_dialog() is first
        assert first.parentWidget() is target
        source.close()
        pump(app)
        first.close()
        first.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        assert chip._dialog is None
        second = chip.open_dialog()
        assert second.isVisible() and second.parentWidget() is target
        second.close()
    finally:
        if source in controller.windows.values():
            source.close()
        target.close()
        pump(app)
