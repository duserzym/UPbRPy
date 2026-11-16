"""Offscreen update overlays and dialogs at laptop sizes in both themes."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import apply_theme, build_application
from GUI.update_widgets import CheckUpdatesButton, UpdatesDialog, VersionChip
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
        fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
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
        output = Path(__file__).resolve().parents[1] / "docs/assets/screenshots"
        if os.environ.get("UPBRPY_SCREENSHOTS") == "1":
            output.mkdir(parents=True, exist_ok=True)
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
    ("none", "Up to date", "You're up to date"),
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
    # Manual preview checks now show a short checking phase before the scenario.
    QtTest.QTest.qWait(700)
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


def test_standards_check_and_warning_are_independent_of_app_updates(app, prefs, monkeypatch):
    from GUI.standards_updates import StandardsCheck

    updater = Updater(settings=prefs)
    monkeypatch.setattr(updater, "_support_reason", lambda: "Checkout build")
    checks = []
    monkeypatch.setattr(updater, "check_standards", lambda: checks.append(True))
    chip = VersionChip(updater)
    dialog = UpdatesDialog(updater)
    try:
        updater.start_scheduler()
        assert not updater._timer.isActive()
        assert updater._standards_timer.isActive()
        assert updater._standards_timer.interval() == 8000
        updater._standards_timer.timeout.emit()
        assert checks == [True]
        assert updater._standards_timer.interval() == 6 * 60 * 60 * 1000

        updater._standards_result(StandardsCheck("Standards match.", digest="previous"))
        updater._standards_result(StandardsCheck("Standards match.", digest="updated"))
        assert "Standards warning" in chip.text()
        assert "changed since you last reviewed it" in dialog.standards_status.text()
        dialog.standards_ack.click()
        assert not updater.standards_warning
    finally:
        updater._shutdown()
        chip.deleteLater()
        dialog.deleteLater()
        pump(app)


@pytest.mark.parametrize("scenario", ["idle", "none", "available", "ready", "error", "unsupported", "checking", "downloading"])
def test_manual_preview_outcomes(app, prefs, scenario):
    updater = Updater(preview=scenario, version="0.1.0", settings=prefs)
    initial = updater.state
    updater.check(manual=True)
    if scenario in {"checking", "downloading"}:
        assert updater.state is initial
    else:
        assert updater.state.status == "checking" and updater.state.manual
        assert updater._preview_timer.interval() == 600
        updater.check(manual=True)
        QtTest.QTest.qWait(700)
        assert updater.state.status == ("none" if scenario == "idle" else scenario)
        assert updater.state.info == initial.info
    updater._shutdown()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1512, 982), (1280, 800), (760, 620)])
def test_check_updates_button_geometry_and_dismissal(app, prefs, theme, size, monkeypatch):
    apply_theme(app, theme)
    updater = Updater(preview="available", version="0.1.0", settings=prefs)
    shell = StudioWindow(theme_mode=theme, reduction_factory=lambda: MainWindow(theme_mode=theme, updater=updater))
    window = shell.active_reduction
    button = window.check_updates_button
    popup = button.popover
    checks = []
    monkeypatch.setattr(updater, "check", lambda manual=False: checks.append(manual))
    try:
        shell.resize(*size)
        shell.show()
        pump(app)
        assert (shell.width(), shell.height()) == size
        assert button.objectName() == "CheckUpdatesButton"
        assert button.accessibleName() == "Check for updates"
        assert button.toolTip().startswith("Check for updates")
        assert button.property("badge") is True
        window.capabilities_badge.setText("Rich input")
        window.capabilities_badge.show()
        pump(app)
        assert button.geometry().left() > window.capabilities_badge.geometry().right()
        rect = QtCore.QRect(button.mapTo(shell, QtCore.QPoint()), button.size())
        assert shell.rect().contains(rect)
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        pump(app)
        assert checks == [True] and popup.isVisible()
        assert popup.grab().toImage().pixelColor(8, 40).alpha() == 255
        assert "Update available: v0.2.0" in popup.status.text()
        assert not popup.action.isVisible()  # automatic download is on
        prefs.setValue("updates/auto_download", False)
        updater.stateChanged.emit(updater.state)
        assert popup.action.isVisible() and popup.action.text() == "Download"
        bounds = QtCore.QRect(shell.mapToGlobal(QtCore.QPoint()), shell.size()).intersected(shell.screen().availableGeometry())
        assert bounds.contains(popup.frameGeometry())
        assert popup.geometry().bottom() < button.mapToGlobal(QtCore.QPoint()).y()
        assert button.iconSize() == QtCore.QSize(16, 16)
        QtTest.QTest.keyClick(popup, QtCore.Qt.Key.Key_Escape)
        assert not popup.isVisible()
        button.click()
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        assert not popup.isVisible() and len(checks) == 2
        button.click()
        QtTest.QTest.mouseClick(shell, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(10, 10))
        assert not popup.isVisible()
    finally:
        popup.hide()
        updater._shutdown()
        window.has_unsaved_changes = lambda: False
        shell.close()
        shell.deleteLater()
        pump(app)
        apply_theme(app, "light")


def test_check_updates_actions_and_busy_state(app, prefs, monkeypatch):
    updater = Updater(preview="checking", version="0.1.0", settings=prefs)
    settings_calls, actions = [], []
    button = CheckUpdatesButton(updater, lambda: settings_calls.append(True))
    popup = button.popover
    monkeypatch.setattr(updater, "check", lambda **kw: pytest.fail("Duplicate check"))
    monkeypatch.setattr(updater, "download", lambda: actions.append("download"))
    monkeypatch.setattr(updater, "install", lambda: actions.append("install"))
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda url: actions.append(url.toString()))
    button.show()
    try:
        button.click()
        assert popup.status.text() == "Checking for updates…" and button.property("busy")
        assert button._rotation_timer.isActive()
        for status, action in [("downloading", None), ("ready", "install"), ("unsupported", "page"), ("available", "download")]:
            prefs.setValue("updates/auto_download", False)
            updater._state = UpdateState(status, info={"version": "0.2.0"}, progress=43, total=100)
            updater.stateChanged.emit(updater.state)
            if action:
                popup.action.click()
            else:
                assert popup.status.text() == "Downloading v0.2.0… 43%"
        assert actions == ["install", "https://github.com/duserzym/UPbRPy/releases/latest", "download"]
        assert not button._rotation_timer.isActive()
        popup.settings_link.linkActivated.emit("settings")
        assert settings_calls == [True] and not popup.isVisible()
        updater._state = UpdateState("none")
        updater.stateChanged.emit(updater.state)
        assert not button.property("badge") and not button.property("busy")
        assert popup.status.text() == "Up to date (v0.1.0)"
        updater._state = UpdateState("error", message="BASE64 invalid")
        updater.stateChanged.emit(updater.state)
        assert popup.status.text() == "Updates are not configured for this build yet."
        apply_theme(app, "dark")
        assert "#e7ecf4" in popup.styleSheet()
    finally:
        button.close()
        button.deleteLater()
        updater._shutdown()
        pump(app)
        apply_theme(app, "light")


def test_check_updates_reduced_motion(app, prefs):
    class NoAnimationStyle(QtWidgets.QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, returnData=None):
            if hint == QtWidgets.QStyle.StyleHint.SH_Widget_Animate:
                return 0
            return super().styleHint(hint, option, widget, returnData)

    updater = Updater(preview="checking", settings=prefs)
    button = CheckUpdatesButton(updater, lambda: None)
    style = NoAnimationStyle()
    button.setStyle(style)
    button.show()
    button.click()
    try:
        assert button.property("busy") and not button._rotation_timer.isActive()
        assert button.popover.status.text() == "Checking for updates…"
    finally:
        button.close()
        button.deleteLater()
        pump(app)
