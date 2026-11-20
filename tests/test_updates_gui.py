"""Shared controls, Studio standards and update cards at laptop sizes offscreen."""

from dataclasses import replace
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import apply_theme, build_application
from GUI.update_widgets import CheckUpdatesButton, UpdatesDialog, VersionChip, WhatsNewCard
from GUI.updater import Updater
from GUI.labupdate import qt
from GUI.labupdate.manifest import Release
from GUI.labupdate.state import UpdateState, button_view
from GUI.update_config import update_config


@pytest.fixture(scope="module")
def app():
    from GUI.gc_policy import install, uninstall
    application = build_application([], "light")
    already_installed = hasattr(application, "_upbrpy_gc_policy")
    install(application)
    yield application
    if not already_installed:
        uninstall(application)


@pytest.fixture
def prefs(app, tmp_path):
    return QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat)


def pump(app):
    for _ in range(12):
        app.processEvents()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1440, 900), (1280, 800)])
def test_shared_controls_fit_studio(app, prefs, theme, size, tmp_path):
    apply_theme(app, theme)
    updater = Updater(preview="ready", version="0.1.0", settings=prefs)
    shell = StudioWindow(theme_mode=theme, reduction_factory=lambda: MainWindow(theme_mode=theme, updater=updater))
    window = shell.active_reduction
    shell.resize(*size)
    shell.show()
    dialog = None
    try:
        pump(app)
        chip, button, canvas = window.version_chip, window.check_updates_button, window.plots_stack
        assert isinstance(chip, qt.VersionChip) and isinstance(button, qt.CheckUpdatesButton)
        assert button.size() == QtCore.QSize(21, 21)
        assert isinstance(button.whats_new_card, WhatsNewCard)
        assert chip.text() == "v0.1.0"
        for page in range(canvas.count()):
            canvas.setCurrentIndex(page)
            pump(app)
            assert canvas.rect().contains(chip.geometry())
            assert canvas.width() - chip.geometry().right() - 1 == 10
            assert canvas.height() - chip.geometry().bottom() - 1 == 10
            assert canvas.childAt(chip.geometry().center()) is chip
        popup = button.popover
        popup.show()
        pump(app)
        assert popup.status.text() == "v2.0.0 is ready"
        assert popup.notes.text() == "Release notes"
        assert popup.action.text() == "Restart to update"
        assert button.screen().availableGeometry().contains(popup.frameGeometry())
        popup.hide()
        dialog = chip.open_dialog()
        pump(app)
        assert dialog.heading.text() == "UPbRPy Studio v0.1.0"
        assert dialog.status.text() == popup.status.text()
        assert dialog.notes.text() == "Release notes"
        assert dialog.notes_link.isVisible() and dialog.standards_status.isVisible()
        assert not hasattr(dialog, "channel_combo")
        assert dialog.width() <= size[0] and dialog.height() <= size[1]
        for widget in dialog.findChildren(QtWidgets.QWidget):
            if widget.isVisibleTo(dialog) and not widget.isWindow():
                assert dialog.rect().contains(QtCore.QRect(widget.mapTo(dialog, QtCore.QPoint()), widget.size()))
        assert dialog.grab().save(str(tmp_path / f"updates-{theme}-{size[0]}.png"))
    finally:
        if dialog:
            dialog.close()
        updater.shutdown()
        window.has_unsaved_changes = lambda: False
        shell.close()
        pump(app)
        apply_theme(app, "light")


@pytest.mark.parametrize("status", sorted(qt.STATES))
def test_canonical_state_ui_and_details(app, prefs, status):
    updater = Updater(preview=status, version="0.1.0", settings=prefs)
    button = CheckUpdatesButton(updater)
    dialog = UpdatesDialog(updater)
    try:
        button.show()
        dialog.show()
        button.popover.show()
        pump(app)
        view = button_view(updater.state, updater.version)
        assert button.popover.status.text() == dialog.status.text() == view.text
        assert button.property("busy") == view.busy
        assert button.property("badge") == view.badge
        assert button._rotation_timer.isActive() == view.busy
        assert button.popover.notes.isVisible() == (status in {"available", "ready"})
        assert dialog.notes.isVisible() == (status in {"available", "ready"})
        updater._state = UpdateState("error", message="raw " * 100)
        updater.stateChanged.emit(updater.state)
        assert len(button.popover.details.text().removeprefix("Details: ")) <= 120
        assert len(dialog.details.text().removeprefix("Details: ")) <= 120
    finally:
        button.close()
        dialog.close()
        updater.shutdown()
        pump(app)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_whats_new_startup_reopen_and_dismissal(app, prefs, theme, monkeypatch):
    apply_theme(app, theme)
    prefs.setValue("updates/last_seen_version", "0.1.4")
    updater = Updater(version="0.1.5", settings=prefs)
    window = QtWidgets.QWidget()
    window.resize(1280, 800)
    button = CheckUpdatesButton(updater, parent=window)
    button.move(1220, 755)
    window.show()
    try:
        updater.start()
        pump(app)
        card = button.whats_new_card
        assert card.isVisible() and card.heading.text() == "What's new in v0.1.5"
        assert card.notes.text() == updater.current_notes()
        assert card.notes.height() >= card.notes.heightForWidth(card.notes.width())
        assert card.grab().toImage().pixelColor(8, 50).alpha() == 255
        assert window.screen().availableGeometry().contains(card.frameGeometry())
        QtTest.QTest.keyClick(card, QtCore.Qt.Key.Key_Escape)
        assert not card.isVisible()
        popup = button.popover
        popup.show()
        popup.whats_new_link.linkActivated.emit("new")
        assert card.isVisible() and not popup.isVisible()
        close = next(w for w in card.findChildren(QtWidgets.QToolButton) if w.text() == "✕")
        close.click()
        assert not card.isVisible()
        updater.show_whats_new()
        assert card.isVisible()
    finally:
        button.whats_new_card.hide()
        window.close()
        updater.shutdown()
        pump(app)
        apply_theme(app, "light")


def test_notes_clamping_links_and_actions(app, prefs, monkeypatch):
    updater = Updater(preview="available", version="0.1.0", settings=prefs)
    prefs.setValue("updates/auto_download", False)
    button = CheckUpdatesButton(updater)
    popup = button.popover
    opened = []
    actions = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    monkeypatch.setattr(updater, "download", lambda: actions.append("download"))
    monkeypatch.setattr(updater, "install", lambda: actions.append("install"))
    notes = "\n".join([f"- Line {i}" for i in range(10)])
    updater._state = replace(updater.state, info=Release("0.2.0", notes, None, "https://example.org/app", ""))
    try:
        button.show()
        updater.stateChanged.emit(updater.state)
        popup.show()
        pump(app)
        assert popup.notes.text() == notes
        assert popup.notes.height() == popup.notes.fontMetrics().lineSpacing() * 4
        assert not popup.notes.grab().isNull()
        popup.notes_link.linkActivated.emit("release")
        assert opened == ["https://github.com/duserzym/UPbRPy/releases/tag/v0.2.0"]
        popup.action.click()
        updater._state = replace(updater.state, status="ready")
        updater.stateChanged.emit(updater.state)
        popup.action.click()
        assert actions == ["download", "install"]
        updater._state = replace(updater.state, status="available")
        updater.set_auto_download(True)
        assert popup.action.isHidden()
        updater.set_auto_download(False)
        assert not popup.action.isHidden() and popup.action.text() == "Download"
        QtTest.QTest.keyClick(popup, QtCore.Qt.Key.Key_Escape)
        assert not popup.isVisible()
    finally:
        button.close()
        updater.shutdown()


def test_controller_shares_updater_and_dialog_follows_tab(app, prefs, monkeypatch):
    from GUI.main import StudioWindowController
    monkeypatch.setenv("UPBRPY_UPDATE_PREVIEW", "ready")
    controller = StudioWindowController(theme_mode="light", app_icon=None)
    source = controller.create_window(show=False)
    reduction = source.add_tab()
    calls = []
    monkeypatch.setattr(controller.updater, "check", lambda manual=False: calls.append(manual))
    target = None
    try:
        assert reduction.updater is source.tabs.widget(0).updater is controller.updater
        chip = reduction.version_chip
        first = chip.open_dialog(manual=True)
        assert calls == [True]
        target = controller.move_tab_to_new_window(source, reduction)
        assert chip.open_dialog() is first and first.parentWidget() is target
        first.close()
        first.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        assert chip._dialog is None
        chip.open_dialog().close()
    finally:
        source.close()
        if target:
            target.close()
        controller.updater.shutdown()
        pump(app)


def test_standards_still_independent(app, prefs, monkeypatch):
    from GUI.standards_updates import StandardsCheck
    updater = Updater(settings=prefs, opener=lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("offline")))
    checks = []
    monkeypatch.setattr(updater, "check_standards", lambda: checks.append(True))
    dialog = UpdatesDialog(updater)
    try:
        updater.start_scheduler()
        assert updater._standards_timer.isActive()
        updater._standards_timer.timeout.emit()
        assert checks == [True] and updater._standards_timer.interval() == 6 * 60 * 60 * 1000
        updater.check(manual=True)
        assert checks == [True, True]
        updater._standards_result(StandardsCheck("Standards match.", digest="previous"))
        updater._standards_result(StandardsCheck("Standards match.", digest="updated"))
        assert updater.standards_warning
        assert "changed since you last reviewed it" in dialog.standards_status.text()
        dialog.standards_ack.click()
        assert not updater.standards_warning
    finally:
        updater.shutdown()
        dialog.close()
