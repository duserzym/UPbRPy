"""Studio's File > Database actions, offscreen, against the fake laicpmsdb."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

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
def controller(app, tmp_path, monkeypatch):
    from GUI.icon import build_app_icon
    from GUI.main import StudioWindowController

    # No live standards or update checks: a git fetch thread still running
    # when a later test collects this controller makes Qt abort.
    monkeypatch.setenv("UPBRPY_UPDATE_PREVIEW", "none")
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


@pytest.fixture
def mock_client(controller, monkeypatch):
    """Execute database workers locally, with no transport or token store."""
    calls = []
    fake = SimpleNamespace(
        DatabaseError=ValueError,
        normalize_server=lambda server: server.rstrip("/"),
        is_signed_in=lambda server: True,
        exchange_code=lambda server, code: calls.append(("exchange", code)),
        get_run=lambda server, run: calls.append(("run", run)) or {"session_id": SESSION_ID},
        fetch_session_bundle=lambda *args: pytest.fail("Unexpected bundle fetch"),
    )
    monkeypatch.setattr(gui_database, "client", lambda: fake)
    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    monkeypatch.setattr(controller.database, "run", lambda function, success, **kwargs: success(function()))
    controller.database.sign_in = lambda *args: pytest.fail("Unexpected sign-in")
    return fake, calls


def loaded_bundle(controller, tmp_path):
    bundle = write_session_bundle(tmp_path / "source", server="https://lab.example.org")
    shell = controller.create_window(show=False)
    window = shell.active_reduction
    window.load_project_file(bundle / "Synthetic_20Mar25.upbr", background=False)
    return shell, window


@pytest.mark.parametrize("route", ["session", "run", "run-session", "run-cached"])
def test_open_link_activates_existing_tab_without_fetch(
    controller, dialogs, tmp_path, monkeypatch, mock_client, route,
):
    _fake, calls = mock_client
    shell, window = loaded_bundle(controller, tmp_path)
    window.spot_tree_filter.setText("NIST")
    if route == "run":
        window.session.origin["run_id"] = "run-1"
    if route == "run-cached":
        monkeypatch.setattr(gui_database, "find_project_for_run", lambda *args: window.project_path)
    shell.add_tab()
    other = controller.create_window(show=False)
    shown = []
    monkeypatch.setattr(controller, "_show_window", shown.append)
    kind, identifier = ("session", SESSION_ID) if route == "session" else ("run", "run-1")
    controller.open_target(
        f"upbr://{kind}/{identifier}?server=https://lab.example.org"
        "&code=c&acquisition=acq-2&via=upblab"
    )
    assert shell.active_reduction is window and controller.active_window() is shell
    assert shown == [shell] and shell.tabs.count() == 2 and other.tabs.count() == 1
    assert window.current_spot_name() == "AFB_3"
    assert not window.spot_tree_filter.text() and not window.spot_list.currentItem().isHidden()
    assert window.session.selected_spot == "AFB_3" and window.session.plot_mode == "raw"
    assert window.trace_plot._spot_name_label.text() == "AFB_3"
    assert window.session.origin["opened_via"] == "upblab"
    assert not dialogs
    assert calls == ([("exchange", "c"), ("run", "run-1")] if route == "run-session" else [])


@pytest.mark.parametrize("route", ["session", "run", "cached-run", "raw"])
def test_new_opener_selects_analysis_after_background_load(
    controller, dialogs, tmp_path, monkeypatch, mock_client, route,
):
    from UPbRPy.session_bundle import BUNDLE_RECORD_FILENAME, SessionBundle

    fake, calls = mock_client
    bundle = write_session_bundle(tmp_path / "source", server="https://lab.example.org", upbr=route != "raw")
    project = bundle / "Synthetic_20Mar25.upbr" if route != "raw" else None
    if route == "raw":
        (bundle / BUNDLE_RECORD_FILENAME).write_text(json.dumps({
            "server": "https://lab.example.org", "session_id": SESSION_ID, "session_key": bundle.name,
        }))
    monkeypatch.setattr(fake, "fetch_session_bundle", lambda *args: calls.append(("fetch", SESSION_ID)) or SessionBundle(
        path=bundle, session_key=bundle.name, session_id=SESSION_ID,
        export_sha256="", zip_sha256="", project_file=project,
    ))
    if route == "cached-run":
        monkeypatch.setattr(gui_database, "find_project_for_run", lambda *args: project)
    kind, identifier = ("session", SESSION_ID) if route in {"session", "raw"} else ("run", "run-1")
    controller.open_target(
        f"upbr://{kind}/{identifier}?server=https://lab.example.org"
        "&code=c&acquisition=acq-2&via=upblab"
    )
    window = controller.active_window().active_reduction
    assert wait_for(lambda: window.session.loaded and window._active_worker is None
                    and window.session.origin.get("opened_via") == "upblab"), dialogs
    assert window.current_spot_name() == "AFB_3" and window.session.plot_mode == "raw"
    assert window.trace_plot._spot_name_label.text() == "AFB_3"
    assert not dialogs
    assert calls[0] == ("exchange", "c")
    if route != "cached-run":
        assert ("fetch", SESSION_ID) in calls
    # The opener flag is saved through the existing origin mechanism.
    if project is None:
        project = bundle / "saved.upbr"
        window.save_project(project)
    else:
        assert "opened_via" not in json.loads(project.read_text())["origin"]
        window.save_project()
    assert json.loads(project.read_text())["origin"]["opened_via"] == "upblab"


def test_unknown_acquisition_only_reports_status(controller, dialogs, tmp_path, mock_client):
    _shell, window = loaded_bundle(controller, tmp_path)
    controller.open_target(f"upbr://session/{SESSION_ID}?server=https://lab.example.org&acquisition=missing")
    assert "not in this project" in window.statusBar().currentMessage()
    assert not dialogs


@pytest.mark.parametrize("cached", [False, True])
def test_old_run_without_server_keeps_cache_behavior(
    controller, dialogs, tmp_path, monkeypatch, mock_client, cached,
):
    if cached:
        bundle = write_session_bundle(tmp_path / "source")
        monkeypatch.setattr(gui_database, "find_project_for_run", lambda *args: bundle / "Synthetic_20Mar25.upbr")
    controller.open_target("upbr://run/run-1")
    if cached:
        window = controller.active_window().active_reduction
        assert wait_for(lambda: window.session.loaded and window._active_worker is None)
        assert not dialogs
    else:
        assert len(dialogs) == 1 and "link names no server" in dialogs[0][2]
    assert not mock_client[1]


@pytest.mark.parametrize("via, outcome", [("", True), ("elsewhere", True), ("upblab", True), ("upblab", False), ("upblab", RuntimeError("no handler"))])
def test_submitted_returns_to_upblab_only_for_opener(
    controller, dialogs, tmp_path, monkeypatch, mock_client, via, outcome,
):
    _shell, window = loaded_bundle(controller, tmp_path)
    if via:
        window.session.origin["opened_via"] = via
    urls = []
    logs = []
    monkeypatch.setattr(window, "append_log", logs.append)

    def open_url(url):
        urls.append(url.toString())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", open_url)
    scratch = tmp_path / "submission"
    scratch.mkdir()
    result = SimpleNamespace(run_id="run-1", counts={}, quarantine_items=[])
    fake, calls = mock_client
    fake.submit_reduction = lambda server, session, folder, **kwargs: calls.append(("submit", session)) or result
    monkeypatch.setattr("UPbRPy.reproducibility.export_reduction_bundle", fake_export)
    monkeypatch.setattr(gui_database.tempfile, "mkdtemp", lambda **kwargs: str(scratch))
    monkeypatch.setattr(window, "run_background", lambda label, calculation, success: success(calculation()))
    controller.database.submit(window)
    assert calls == [("submit", SESSION_ID)]
    assert urls == (["laicpmsdb://run/run-1"] if via == "upblab" else [])
    assert window.session.origin["run_id"] == "run-1" and not scratch.exists()
    assert all(kind != "warning" for kind, _title, _text in dialogs)
    assert any("Could not open" in line for line in logs) == (via == "upblab" and outcome is not True)
