"""Offline Qt worker lifecycle, scheduler preferences and compact light/dark controls."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.labupdate import qt
from GUI.labupdate.state import STATES
from test_labupdate_install import config, make_archive, old_app
from GUI.labupdate.config import platform_key
import json


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump_until(app, condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        QtTest.QTest.qWait(5)
    assert condition()


def test_check_download_ready_install(app, tmp_path, monkeypatch):
    archive, release = make_archive(tmp_path)
    manifest = tmp_path / "latest.json"
    manifest.write_text(json.dumps({"version": release.version, "notes": "Release notes", "platforms": {
        platform_key(): {"url": release.url, "signature": release.signature}}}))
    root, calls, commands = old_app(tmp_path), [], []
    def opener(request, *, timeout):
        calls.append(request.full_url)
        return urllib.request.urlopen(request, timeout=timeout)
    updater = qt.Updater(config(feed_url=manifest.as_uri()), "1.0.0", root=root, opener=opener, runner=commands.append)
    updater.prefs.setValue("updates/auto_download", False)
    statuses = []
    updater.stateChanged.connect(lambda state: statuses.append(state.status))
    quits = []
    monkeypatch.setattr(QtCore.QCoreApplication, "quit", lambda: quits.append(True))
    try:
        updater.check(manual=True)
        pump_until(app, lambda: updater.state.status == "available" and not updater._jobs)
        assert updater.state.info.version == "2.0.0"
        updater.download()
        pump_until(app, lambda: updater.state.status == "ready" and not updater._jobs)
        assert updater.state.path.read_bytes() == archive.read_bytes()
        assert {"checking", "available", "downloading", "ready"} <= set(statuses)
        assert calls == [manifest.as_uri(), archive.as_uri()]
        assert updater.prefs.value("updates/last_check")
        updater.install()
        pump_until(app, lambda: bool(quits) and not updater._jobs)
        assert commands and Path(commands[0][-1]).is_file()
    finally:
        updater.shutdown()
        for command in commands:
            shutil.rmtree(Path(command[-1]).parent, ignore_errors=True)


def test_auto_download_and_scheduler(app, tmp_path):
    archive, release = make_archive(tmp_path)
    path = tmp_path / "latest.json"
    path.write_text(json.dumps({"version": "2.0.0", "platforms": {platform_key(): {"url": archive.as_uri(), "signature": release.signature}}}))
    updater = qt.Updater(config(feed_url=path.as_uri()), "1.0.0", root=old_app(tmp_path))
    updater.prefs.setValue("updates/auto_download", True)
    try:
        updater.start_scheduler()
        assert updater._initial_timer.interval() == 8000 and updater._initial_timer.isActive()
        assert updater._timer.interval() == 6 * 60 * 60 * 1000
        updater.check()
        pump_until(app, lambda: updater.state.status == "ready" and not updater._jobs)
        assert updater.auto_download
        updater.set_auto_download(False)
        assert not updater.auto_download
    finally:
        updater.shutdown()
    assert not updater._timer.isActive()


def test_shutdown_joins_worker(app, tmp_path):
    def opener(request, *, timeout):
        time.sleep(0.03)
        raise urllib.error.URLError("offline")
    updater = qt.Updater(config(), "1.0.0", opener=opener, root=old_app(tmp_path))
    updater.check(manual=True)
    workers = list(updater._jobs)
    updater.shutdown()
    assert all(not worker.isRunning() for worker in workers)
    assert not any(worker in qt._RUNNING for worker in workers)


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("status", sorted(STATES))
def test_controls_render_without_overflow(app, tmp_path, dark, status):
    original = app.palette()
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#202020" if dark else "#fafafa"))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#eeeeee" if dark else "#202020"))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, palette.color(QtGui.QPalette.ColorRole.WindowText))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor("#303030" if dark else "#f0f0f0"))
    palette.setColor(QtGui.QPalette.ColorRole.Base, palette.color(QtGui.QPalette.ColorRole.Window))
    palette.setColor(QtGui.QPalette.ColorRole.Text, palette.color(QtGui.QPalette.ColorRole.WindowText))
    app.setPalette(palette)
    updater = qt.Updater(config(), "1.0.0", preview=status)
    window = QtWidgets.QWidget()
    window.resize(1280, 800)
    button = qt.CheckUpdatesButton(updater, parent=window)
    button.move(10, 760)
    chip = qt.VersionChip(updater)
    chip.anchor_to(window)
    try:
        window.show()
        app.processEvents()
        assert window.rect().contains(button.geometry()) and window.rect().contains(chip.geometry())
        assert button.property("badge") == (status in {"available", "ready", "unsupported"})
        button.popover.show()
        app.processEvents()
        screen = button.screen().availableGeometry()
        assert screen.contains(button.popover.frameGeometry())
        assert button.popover.height() <= 800 and button.popover.width() <= 1280
        assert button.popover.status.heightForWidth(button.popover.status.width()) <= button.popover.status.height()
        image = window.grab().toImage()
        assert image.width() == 1280 and image.height() == 800
        assert not button.grab().isNull() and not chip.grab().isNull()
        image.save(str(tmp_path / f"updates-{status}-{'dark' if dark else 'light'}.png"))
        assert button._rotation_timer.isActive() == (status in {"checking", "downloading", "installing"})
    finally:
        updater.shutdown()
        window.close()
        app.setPalette(original)


def test_preview_never_fetches(app):
    updater = qt.Updater(config(), "1.0.0", preview="available", opener=lambda *a, **kw: pytest.fail("Preview fetched network"))
    try:
        updater.start_scheduler()
        updater.download()
        updater.check(manual=True)
        pump_until(app, lambda: updater.state.status == "available")
        assert not updater._jobs and not updater._timer.isActive()
    finally:
        updater.shutdown()
