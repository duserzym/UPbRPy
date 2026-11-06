"""Studio's File > Database actions, offscreen, against the fake laicpmsdb."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtCore, QtWidgets  # noqa: E402

import GUI.database as gui_database  # noqa: E402
from UPbRPy.integration import laicpmsdb as db  # noqa: E402

from synthetic_bundles import sha256, write_session_bundle  # noqa: E402


SESSION_ID = "0192f0c4-0000-7000-8000-000000000001"


@pytest.fixture(scope="module")
def app():
    from GUI.theme import build_application

    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture()
def dialogs(monkeypatch):
    """Record message boxes instead of blocking on them."""
    shown = []

    def record(kind):
        def show(parent, title, text, *args, **kwargs):
            shown.append((kind, title, str(text)))
            return (QtWidgets.QMessageBox.StandardButton.Yes if kind == "question"
                    else QtWidgets.QMessageBox.StandardButton.Ok)
        return staticmethod(show)

    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QtWidgets.QMessageBox, kind, record(kind))
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "exec",
        lambda self: shown.append(("box", self.windowTitle(), self.text() + "\n" + self.detailedText())) or 0,
    )
    return shown


def wait_for(condition, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def controller(app, tmp_path):
    from GUI.icon import build_app_icon
    from GUI.main import StudioWindowController

    controller = StudioWindowController(theme_mode="light", app_icon=build_app_icon())
    controller.database.settings = QtCore.QSettings(
        str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat
    )
    controller.database.cache_root = tmp_path / "cache"
    yield controller
    for window in list(controller.windows.values()):
        for index in range(window.tabs.count()):
            window.tabs.widget(index).has_unsaved_changes = lambda: False  # no save prompt
        window.close()
    QtWidgets.QApplication.processEvents()


def test_actions_disabled_with_reason_without_extra(controller, monkeypatch):
    gui_database.database_availability.cache_clear()
    monkeypatch.setattr(db, "availability", lambda: (False, "Database mode needs the optional [database] extra."))
    try:
        window = controller.create_window(show=False).active_reduction
        for action in (
            window.open_from_database_action,
            window.submit_to_database_action,
            window.prefer_run_action,
        ):
            assert not action.isEnabled()
            assert "[database] extra" in action.toolTip()
    finally:
        gui_database.database_availability.cache_clear()


def test_standalone_window_has_no_origin_and_submit_disabled(controller):
    gui_database.database_availability.cache_clear()
    window = controller.create_window(show=False).active_reduction
    assert window.windowTitle() == "UPbRPy Studio — Untitled"
    assert not window.database_origin_label.isVisible()
    assert not window.submit_to_database_action.isEnabled()
    if gui_database.database_availability()[0]:
        assert window.open_from_database_action.isEnabled()
        assert "did not come from a database" in window.submit_to_database_action.toolTip()


def fake_export(pipeline, folder, **kwargs):
    """Stand-in for export_reduction_bundle: the manifest the server checks."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = pipeline.manifest(hash_inputs=kwargs.get("hash_inputs", True))
    (folder / "reduction_manifest.json").write_text(json.dumps(manifest, default=str))
    (folder / "spot_mean_results.csv").write_text("spot_names\nA\n")
    return folder


def test_link_open_submit_prefer_round_trip(controller, dialogs, tmp_path, monkeypatch):
    pytest.importorskip("requests")
    from fake_laicpmsdb import FakeLaicpmsdb, FakeState

    gui_database.database_availability.cache_clear()
    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    store = db.MemoryTokenStore()
    previous = db.set_token_store(store)
    monkeypatch.setattr("UPbRPy.reproducibility.export_reduction_bundle", fake_export)
    bundle = write_session_bundle(tmp_path / "server", session_id=SESSION_ID)
    state = FakeState(sessions={SESSION_ID: bundle})
    try:
        with FakeLaicpmsdb(state) as fake:
            window = controller.create_window(show=False).active_reduction
            code = state.issue_code()
            controller.open_target(f"upbr://session/{SESSION_ID}?server={fake.url}&code={code}")
            assert wait_for(lambda: window.session.loaded and window._active_worker is None), dialogs

            origin = window.session.origin
            assert origin["server"] == fake.url and origin["session_id"] == SESSION_ID
            assert "database 127.0.0.1" in window.windowTitle()
            assert window.database_origin_label.text().startswith("Database: Synthetic_20Mar25")
            project = Path(window.project_path)
            assert project == tmp_path / "cache" / "Synthetic_20Mar25" / "Synthetic_20Mar25.upbr"
            # The manifest's sep "_" and aliases won over the apatite preset's sep " ".
            samples = dict(zip(window.session.experiment["spot_names"], window.session.experiment["sample"]))
            assert samples["MAD_UCSB_2"] == "MADUCSB" and samples["AFB_3"] == "AFB1"
            assert window.workflow_preset.key == "apatite"
            assert window.submit_to_database_action.isEnabled()
            assert not window.prefer_run_action.isEnabled()

            window.submit_to_database_action.trigger()
            assert wait_for(lambda: state.imports and (window.session.origin or {}).get("run_id")), dialogs
            _session, supersedes, data, manifest = state.imports[-1]
            assert supersedes is None
            raw_hashes = {sha256(p) for p in (project.parent / "raw").glob("*.csv")}
            assert {f["sha256"] for f in manifest["input_files"]} == raw_hashes
            assert manifest["metadata"]["origin"]["session_id"] == SESSION_ID
            assert window.session.origin["run_id"] == "run-1"
            saved = json.loads(project.read_text())
            assert saved["origin"]["run_id"] == "run-1"
            assert any(kind == "box" and "Run run-1" in text for kind, _t, text in dialogs)
            assert window.prefer_run_action.isEnabled()

            window.prefer_run_action.trigger()
            assert wait_for(lambda: state.preferred.get(SESSION_ID) == "run-1"), dialogs

            # A resubmission names the run it supersedes.
            window.submit_to_database_action.trigger()
            assert wait_for(lambda: len(state.imports) == 2), dialogs
            assert state.imports[-1][1] == "run-1"
            assert wait_for(lambda: window.session.origin["run_id"] == "run-2")
    finally:
        db.set_token_store(previous)


def test_link_without_code_or_token_asks_to_sign_in(controller, dialogs, tmp_path, monkeypatch):
    pytest.importorskip("requests")
    from fake_laicpmsdb import FakeLaicpmsdb, FakeState

    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    previous = db.set_token_store(db.MemoryTokenStore())
    asked = []
    controller.database.sign_in = lambda server, then: asked.append(server)
    bundle = write_session_bundle(tmp_path / "server", session_id=SESSION_ID)
    try:
        with FakeLaicpmsdb(FakeState(sessions={SESSION_ID: bundle})) as fake:
            controller.open_target(f"upbr://session/{SESSION_ID}?server={fake.url}")
            assert asked == [fake.url]
    finally:
        db.set_token_store(previous)


def test_orcid_callback_completes_pending_sign_in(controller, dialogs, monkeypatch):
    pytest.importorskip("requests")
    from fake_laicpmsdb import FakeLaicpmsdb

    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    store = db.MemoryTokenStore()
    previous = db.set_token_store(store)
    resumed = []
    try:
        with FakeLaicpmsdb() as fake:
            # What SignInDialog leaves behind after opening the browser:
            controller.database.pending_sign_in = (fake.url, lambda: resumed.append(True))
            code = fake.state.issue_code()
            controller.open_target(f"upbr://auth/callback?code={code}")
            assert wait_for(lambda: resumed), dialogs
            assert store.get(fake.url) in fake.state.tokens
    finally:
        db.set_token_store(previous)


def test_session_picker_filters_and_returns_id(app):
    dialog = gui_database.SessionPickerDialog(
        [
            {"id": "a", "session_key": "Apatite_20Mar25", "material": "apatite"},
            {"id": "z", "session_key": "Zircon_28Mar25_1", "material": "zircon"},
        ],
        "https://lab.example.org",
    )
    dialog.filter_edit.setText("zircon")
    assert dialog.table.isRowHidden(0) and not dialog.table.isRowHidden(1)
    dialog.table.selectRow(1)
    dialog.accept()
    assert dialog.session_id == "z"


def test_unknown_import_keys_are_nested_under_selection_options():
    from GUI.services import normalize_import_settings

    assert normalize_import_settings(
        {"baseline_sweep_n": 15, "sep": "_", "plateau_width_mode": "signal",
         "selection_options": {"sigma_threshold": 3}}
    ) == {
        "baseline_sweep_n": 15,
        "sep": "_",
        "selection_options": {"sigma_threshold": 3, "plateau_width_mode": "signal"},
    }
