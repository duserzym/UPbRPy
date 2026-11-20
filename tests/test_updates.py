"""Studio's signed-feed adapter, preferences and preserved standards lifecycle."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import urllib.request

import pytest
from PySide6 import QtCore, QtWidgets

from GUI import updater as studio
from GUI.labupdate import qt
from GUI.labupdate.config import platform_key
from GUI.update_config import PUBLIC_KEY, release_notes, update_config
from test_labupdate_install import config, make_archive, old_app
from test_labupdate_qt import pump_until


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def prefs(app, tmp_path):
    return QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat)


@pytest.mark.parametrize("automatic", [False, True])
def test_studio_signed_update_lifecycle(app, prefs, tmp_path, monkeypatch, automatic):
    archive, release = make_archive(tmp_path)
    manifest = tmp_path / "latest.json"
    manifest.write_text(json.dumps({"version": release.version, "notes": "- Print sheets fit Letter.",
        "platforms": {platform_key(): {"url": release.url, "signature": release.signature}}}))
    calls, commands, quits, states = [], [], [], []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        return urllib.request.urlopen(request, timeout=timeout)

    prefs.setValue("updates/channel", "beta")
    prefs.setValue("updates/auto_download", automatic)
    updater = studio.Updater(config=replace(config(feed_url=manifest.as_uri()), opener=opener),
        version="1.0.0", settings=prefs, root=old_app(tmp_path), runner=commands.append)
    monkeypatch.setattr(updater, "check_standards", lambda: None)
    monkeypatch.setattr(QtCore.QCoreApplication, "quit", lambda: quits.append(True))
    updater.stateChanged.connect(lambda state: states.append(state.status))
    try:
        assert isinstance(updater, qt.Updater)
        assert prefs.value("updates/channel") == "stable"
        updater.check(manual=True)
        if not automatic:
            pump_until(app, lambda: updater.state.status == "available" and not updater._jobs)
            assert updater.state.info.notes == "- Print sheets fit Letter."
            updater.download()
        pump_until(app, lambda: updater.state.status == "ready" and not updater._jobs)
        assert updater.state.path.read_bytes() == archive.read_bytes()
        updater.install()
        assert updater.state.status == "installing"
        assert prefs.value("updates/pending_version") == release.version
        assert prefs.value("updates/pending_notes") == "- Print sheets fit Letter."
        pump_until(app, lambda: bool(quits) and not updater._jobs)
        assert {"checking", "available", "downloading", "ready", "installing"} <= set(states)
        assert calls == [manifest.as_uri(), archive.as_uri()]
        script = Path(commands[0][-1])
        assert script.is_file() and "while kill" in script.read_text()
    finally:
        updater.shutdown()
        for command in commands:
            shutil.rmtree(Path(command[-1]).parent, ignore_errors=True)


def test_default_identity_and_opener(app, prefs):
    from UPbRPy import tls
    from GUI.labupdate.minisign import parse_public_key

    cfg = update_config()
    assert cfg.repo == "duserzym/UPbRPy" and cfg.app_name == "UPbRPy Studio"
    assert cfg.feed_url == "https://github.com/duserzym/UPbRPy/releases/latest/download/latest.json"
    assert (cfg.settings_org, cfg.settings_app) == ("UPbRPy", "Studio")
    parse_public_key(PUBLIC_KEY)
    updater = studio.Updater(settings=prefs)
    try:
        assert updater._opener is tls.urlopen
        assert release_notes("0.1.5") and release_notes("0.1.6")
        assert release_notes("../../secret") is None
    finally:
        updater.shutdown()


@pytest.mark.parametrize("previous,bundled,pending,expected", [
    (None, "bundled", "pending", None),
    ("1.0.0", "bundled", "pending", "bundled"),
    ("1.0.0", None, "pending", "pending"),
    ("1.0.0", None, None, "Updated to v2.0.0."),
    ("2.0.0", "bundled", "pending", None),
    ("3.0.0", "bundled", "pending", None),
])
def test_whats_new_after_update_once(app, prefs, previous, bundled, pending, expected, monkeypatch):
    if previous:
        prefs.setValue("updates/last_seen_version", previous)
    if pending:
        prefs.setValue("updates/pending_version", "2.0.0")
        prefs.setValue("updates/pending_notes", pending)
    cfg = replace(update_config(), release_notes=lambda version: bundled)
    updater = studio.Updater(config=cfg, version="2.0.0", settings=prefs)
    monkeypatch.setattr(updater, "check_standards", lambda: None)
    events = []
    updater.whatsNew.connect(lambda *args: events.append(args))
    try:
        updater.start()
        updater.start()
        app.processEvents()
        assert events == ([] if expected is None else [("2.0.0", expected)])
        assert prefs.value("updates/last_seen_version") == "2.0.0"
        assert updater._standards_timer.isActive()
        updater._standards_timer.timeout.emit()
        assert updater._standards_timer.interval() == 6 * 60 * 60 * 1000
    finally:
        updater.shutdown()


def test_no_github_api_paths():
    root = Path(__file__).resolve().parents[1]
    offending = [str(path.relative_to(root)) for folder in ("GUI", "UPbRPy")
                 for path in (root / folder).rglob("*.py") if "api.github.com" in path.read_text()]
    assert not offending
