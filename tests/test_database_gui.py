"""Studio's File > Database actions, offscreen, against the fake UPbDB."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

import GUI.database as gui_database  # noqa: E402
from UPbRPy.integration import upbdb as db  # noqa: E402

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
        # An empty window has nothing to submit yet (c6af2a0 put this first).
        assert window.submit_to_database_action.toolTip() == "Load a session first"


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
    from fake_upbdb import FakeUpbdb, FakeState

    gui_database.database_availability.cache_clear()
    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    store = db.MemoryTokenStore()
    previous = db.set_token_store(store)
    monkeypatch.setattr("UPbRPy.reproducibility.export_reduction_bundle", fake_export)
    bundle = write_session_bundle(tmp_path / "server", session_id=SESSION_ID)
    state = FakeState(sessions={SESSION_ID: bundle})
    try:
        with FakeUpbdb(state) as fake:
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
            assert any(kind == "box" and "(id run-1)" in text for kind, _t, text in dialogs)
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
    from fake_upbdb import FakeUpbdb, FakeState

    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    previous = db.set_token_store(db.MemoryTokenStore())
    asked = []
    controller.database.sign_in = lambda server, then: asked.append(server)
    bundle = write_session_bundle(tmp_path / "server", session_id=SESSION_ID)
    try:
        with FakeUpbdb(FakeState(sessions={SESSION_ID: bundle})) as fake:
            controller.open_target(f"upbr://session/{SESSION_ID}?server={fake.url}")
            assert asked == [fake.url]
    finally:
        db.set_token_store(previous)


def test_orcid_callback_completes_pending_sign_in(controller, dialogs, monkeypatch):
    pytest.importorskip("requests")
    from fake_upbdb import FakeUpbdb

    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    store = db.MemoryTokenStore()
    previous = db.set_token_store(store)
    resumed = []
    try:
        with FakeUpbdb() as fake:
            # What SignInDialog leaves behind after opening the browser:
            controller.database.pending_sign_in = (fake.url, lambda: resumed.append(True), "")
            code = fake.state.issue_code()
            controller.open_target(f"upbr://auth/callback?code={code}")
            assert wait_for(lambda: resumed), dialogs
            assert store.get(fake.url) in fake.state.tokens
    finally:
        db.set_token_store(previous)


def test_browser_sign_in_needs_its_own_state(controller, dialogs, monkeypatch):
    pytest.importorskip("requests")
    from fake_upbdb import FakeUpbdb

    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    store = db.MemoryTokenStore()
    previous = db.set_token_store(store)
    resumed = []
    state = "abcdefghijklmnop_12345"
    try:
        with FakeUpbdb() as fake:
            controller.database.pending_sign_in = (fake.url, lambda: resumed.append(True), state)
            code = fake.state.issue_code()
            controller.open_target(f"upbr://auth/callback?code={code}&state=other_state_value_1234")
            assert not resumed and store.get(fake.url) is None
            controller.open_target(f"upbr://auth/callback?code={code}&state={state}")
            assert wait_for(lambda: resumed), dialogs
            assert store.get(fake.url) in fake.state.tokens
    finally:
        db.set_token_store(previous)


def test_sign_in_dialog_opens_the_connect_page(app, monkeypatch):
    opened = []
    monkeypatch.setattr(gui_database.QtGui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    dialog = gui_database.SignInDialog(None, "https://lab.example.org")
    dialog.browser_button.click()
    assert dialog.browser_started
    assert opened and opened[0].startswith("https://lab.example.org/app/#/connect?app=upbrpy&callback=upbr")
    assert f"state={dialog.state}" in opened[0]


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
    assert urls == (["upbdb://run/run-1"] if via == "upblab" else [])
    assert window.session.origin["run_id"] == "run-1" and not scratch.exists()
    assert all(kind != "warning" for kind, _title, _text in dialogs)
    assert any("Could not open" in line for line in logs) == (via == "upblab" and outcome is not True)


@pytest.fixture
def local_link_project(controller, tmp_path, monkeypatch, mock_client):
    """A local reduction and a byte-identical server export; no transport."""
    from datetime import datetime
    from UPbRPy.session_bundle import unpack_session_bundle
    from synthetic_bundles import zip_directory

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 13, tzinfo=tz)

    # A normal save updates saved_utc. Hold the clock fixed so the entire
    # saved documents can be compared with only origin removed.
    monkeypatch.setattr("UPbRPy.project.datetime", FixedDateTime)
    fake, calls = mock_client
    server_bundle = write_session_bundle(tmp_path / "server", server="https://lab.example.org")
    server_project = server_bundle / "Synthetic_20Mar25.upbr"
    server_document = json.loads(server_project.read_text())
    server_document["origin"]["archive_version"] = "1"
    server_project.write_text(json.dumps(server_document))
    raw = tmp_path / "local" / "raw"
    shutil.copytree(server_bundle / "raw", raw)
    project = raw.parent / "Synthetic_20Mar25.upbr"
    document = json.loads((server_bundle / project.name).read_text())
    document.pop("origin")
    project.write_text(json.dumps(document))
    window = controller.create_window(show=False).active_reduction
    window.load_project_file(project, background=False)
    assert window.session.loaded and window.session.origin is None
    window.session.experiment.loc[0, "baseline_start_times"] = 0.25
    window.show_spot_signal("AFB_3")
    window.save_project()
    directories = []
    fetched_origins = []

    def fetch(server, session_id, destination):
        assert server == "https://lab.example.org" and session_id == SESSION_ID
        assert destination != controller.database.cache_root
        directories.append(Path(destination))
        bundle = unpack_session_bundle(zip_directory(server_bundle), destination, server=server)
        fetched_origins.append(json.loads(bundle.project_file.read_text())["origin"])
        return bundle

    fake.fetch_session_bundle = fetch
    fake.list_sessions = lambda server: calls.append(("list", server)) or [
        {"id": "wrong", "session_key": "Unrelated"},
        {"id": SESSION_ID, "session_key": "Synthetic_20Mar25"},
    ]
    monkeypatch.setattr(controller.database, "choose_server", lambda: "https://lab.example.org")

    def run(function, success, **kwargs):
        try:
            result = function()
        except Exception as exc:
            kwargs.get("on_failure", lambda message: controller.database.show_error(kwargs["title"], message))(exc)
        else:
            success(result)

    monkeypatch.setattr(controller.database, "run", run)

    def pick(dialog):
        assert dialog.table.currentRow() == 1
        dialog.accept()
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui_database.SessionPickerDialog, "exec", pick)
    return SimpleNamespace(window=window, server_bundle=server_bundle, project=project,
                           directories=directories, fetched_origins=fetched_origins,
                           fake=fake, calls=calls)


def test_link_local_project_preserves_decisions_and_submits(
    controller, dialogs, monkeypatch, local_link_project,
):
    case = local_link_project
    window = case.window
    before = json.loads(case.project.read_text())
    pipeline = window.session.pipeline
    directory = window.session.directory
    assert not window.has_unsaved_changes()
    assert window.link_database_session_action.isEnabled()
    assert not window.submit_to_database_action.isEnabled()
    assert "File > Database > Link to Database Session…" in window.submit_to_database_action.toolTip()
    assert "File > Database > Link to Database Session…" in gui_database.ORIGIN_REQUIRED
    dirty_at_save = []
    save_project = window.save_project

    def save():
        dirty_at_save.append(window.has_unsaved_changes())
        save_project()

    monkeypatch.setattr(window, "save_project", save)
    window.link_database_session_action.trigger()
    origin = window.session.origin
    assert origin == case.fetched_origins[0]
    assert origin["export_sha256"] and origin["exported_at"]
    assert pipeline.origin == origin and window.session.pipeline is pipeline
    assert window.session.directory == directory
    assert dirty_at_save == [True]
    assert not window.has_unsaved_changes()
    assert window.submit_to_database_action.isEnabled()
    assert not window.link_database_session_action.isEnabled()
    assert case.directories and all(not path.exists() for path in case.directories)
    assert not controller.database.cache_root.exists()
    assert "Linked to database session Synthetic_20Mar25" in window.log_view.toPlainText()
    saved = json.loads(case.project.read_text())
    assert saved.pop("origin") == origin
    assert saved == before
    assert not window.has_unsaved_changes()

    def submit(server, session_id, folder, **kwargs):
        import csv

        assert server == origin["server"] and session_id == SESSION_ID
        manifest = json.loads((folder / "reduction_manifest.json").read_text())
        # The real importer uses this same hash membership check; filenames
        # and local paths do not affect matching to session raw assets.
        stored = {sha256(path) for path in (case.server_bundle / "raw").glob("*.csv")}
        assert manifest["input_files"]
        assert all(item["sha256"] in stored for item in manifest["input_files"])
        assert manifest["metadata"]["origin"] == origin
        with (folder / "spot_mean_results.csv").open() as stream:
            assert {row["spot_names"] for row in csv.DictReader(stream)} == set(window.session.spot_names)
        case.calls.append(("submit", session_id))
        return SimpleNamespace(run_id="linked-run", counts={}, quarantine_items=[])

    case.fake.submit_reduction = submit

    def export(pipeline, folder, **kwargs):
        fake_export(pipeline, folder, **kwargs)
        (folder / "spot_mean_results.csv").write_text("spot_names\n" + "\n".join(window.session.spot_names) + "\n")

    monkeypatch.setattr("UPbRPy.reproducibility.export_reduction_bundle", export)
    monkeypatch.setattr(window, "run_background", lambda label, calculation, success: success(calculation()))
    window.submit_to_database_action.trigger()
    assert ("submit", SESSION_ID) in case.calls
    assert window.session.origin["run_id"] == "linked-run"
    assert not window.has_unsaved_changes()
    assert all(kind != "warning" for kind, _title, _text in dialogs)


@pytest.mark.parametrize("mismatch", ["hash", "label", "missing", "manifest", "count"])
def test_link_refuses_differences_without_changing_project(
    controller, dialogs, local_link_project, mismatch,
):
    case = local_link_project
    window = case.window
    raw_file = case.server_bundle / "raw" / "Synthetic_1.csv"
    manifest = case.server_bundle / "session_manifest.toml"
    old_hash = sha256(raw_file)
    if mismatch == "hash":
        raw_file.write_text(raw_file.read_text() + "\n")
        manifest.write_text(manifest.read_text().replace(old_hash, sha256(raw_file)))
    elif mismatch == "label":
        raw_file.write_text(raw_file.read_text().replace("NIST612_1", "Other_1"))
        manifest.write_text(manifest.read_text().replace("NIST612_1", "Other_1").replace(old_hash, sha256(raw_file)))
    elif mismatch == "missing":
        raw_file.unlink()
    elif mismatch == "count":
        manifest.write_text(manifest.read_text() + '\n[[acquisition]]\nlabel = "NIST612_1"\n')
    else:
        manifest.write_text(manifest.read_text().replace(old_hash, "0" * 64))
    before = case.project.read_bytes()
    fingerprint = window.scientific_project_fingerprint()
    pipeline_origin = window.session.pipeline.origin
    controller.database.link_session(window)
    assert window.session.origin is None and window.session.pipeline.origin == pipeline_origin
    assert case.project.read_bytes() == before
    assert window.scientific_project_fingerprint() == fingerprint
    assert not window.has_unsaved_changes()
    assert not window.submit_to_database_action.isEnabled()
    assert window.link_database_session_action.isEnabled()
    assert case.directories and all(not path.exists() for path in case.directories)
    message = "\n".join(text for kind, _title, text in dialogs if kind == "warning")
    if mismatch == "label":
        assert "labels" in message and "NIST612_1" in message and "Other_1" in message
    elif mismatch == "count":
        assert "acquisition counts: 4 local, 5 database" in message
    else:
        assert "Synthetic_1.csv" in message
        assert ("SHA-256" in message if mismatch != "missing" else "missing" in message.lower())


def test_link_uses_existing_authorization_and_guards_blank_or_linked_projects(
    controller, dialogs, monkeypatch, local_link_project,
):
    case = local_link_project
    authorized = []
    case.fake.is_signed_in = lambda server: bool(authorized)

    def sign_in(server, then):
        authorized.append(server)
        then()

    controller.database.sign_in = sign_in
    controller.database.link_session(case.window)
    assert authorized == ["https://lab.example.org"]
    assert case.window.session.origin
    listed = list(case.calls)
    controller.database.link_session(case.window)
    blank = controller.create_window(show=False).active_reduction
    assert not blank.link_database_session_action.isEnabled()
    controller.database.link_session(blank)
    assert listed == case.calls


def test_link_action_disabled_without_database_extra(controller, monkeypatch):
    monkeypatch.setattr(gui_database, "database_availability", lambda: (False, "Needs [database] extra."))
    window = controller.create_window(show=False).active_reduction
    assert not window.link_database_session_action.isEnabled()
    assert "[database] extra" in window.link_database_session_action.toolTip()


def test_link_reports_label_only_fallback_and_server_hash_rejection(
    controller, dialogs, local_link_project,
):
    from synthetic_bundles import manifest_toml

    case = local_link_project
    acquisitions = case.window.session.qtegra.acquisitions
    raw = case.server_bundle / "raw"
    for path in raw.iterdir():
        path.unlink()
    rich = raw / "combined.csv"
    labels = [acq.label for acq in acquisitions]
    lines = [",,,," + ",".join(f"Sample {i + 1}" for i in range(len(labels))) + ",",
             ",,,," + ",".join(labels) + ","]
    for channel_index, channel in enumerate(acquisitions[0].channel_names):
        for sweep in range(acquisitions[0].n_sweeps):
            for kind in ("Y", "Time"):
                values = [acq.y[sweep, channel_index] if kind == "Y" else acq.sweep_time[sweep]
                          for acq in acquisitions]
                lines.append(f"MainRuns,{sweep},{channel},{kind}," + ",".join(map(str, values)) + ",")
    rich.write_text("\n".join(lines) + "\n")
    (case.server_bundle / "session_manifest.toml").write_text(manifest_toml(
        session_id=SESSION_ID, session_key=case.server_bundle.name,
        raw_paths=[rich] * len(labels), labels=labels,
    ))
    controller.database.link_session(case.window)
    assert case.window.session.origin
    message = "\n".join(text for _kind, _title, text in dialogs)
    assert "labels and counts only" in message
    assert "different export layouts" in message
    assert "Submission will be rejected because no input hash belongs" in message
    assert all(not path.exists() for path in case.directories)


def test_link_cleans_temporary_directory_when_fetch_fails(
    controller, dialogs, local_link_project,
):
    case = local_link_project

    def failed_fetch(server, session_id, destination):
        case.directories.append(destination)
        (destination / "partial-download").write_bytes(b"partial")
        raise ValueError("Export could not be downloaded.")

    case.fake.fetch_session_bundle = failed_fetch
    controller.database.link_session(case.window)
    assert not case.window.session.origin
    assert all(not path.exists() for path in case.directories)
    assert any("Export could not be downloaded" in message for _kind, _title, message in dialogs)


def test_link_accepts_renamed_identical_inputs(controller, dialogs, local_link_project):
    case = local_link_project
    for path in (case.server_bundle / "raw").iterdir():
        name = "renamed_" + path.name
        manifest = case.server_bundle / "session_manifest.toml"
        manifest.write_text(manifest.read_text().replace(path.name, name))
        path.rename(path.with_name(name))
    controller.database.link_session(case.window)
    assert case.window.session.origin
    assert "Verified SHA-256 for 4 raw file(s)" in dialogs[-1][2]


def test_link_unnamed_reduction_stays_modified_until_save_as(controller, dialogs, local_link_project):
    case = local_link_project
    case.window.project_path = None
    case.fake.list_sessions = lambda server: [
        {"id": "wrong", "session_key": "Unrelated"},
        {"id": SESSION_ID, "session_key": "Synthetic_1"},
    ]
    before = case.project.read_bytes()
    controller.database.link_session(case.window)
    assert case.window.session.origin
    assert case.window.has_unsaved_changes()
    assert case.project.read_bytes() == before
    assert "Save the project to keep this link" in dialogs[-1][2]
    target = case.project.with_name("linked.upbr")
    case.window.save_project(target)
    assert json.loads(target.read_text())["origin"] == case.window.session.origin
    assert not case.window.has_unsaved_changes()
