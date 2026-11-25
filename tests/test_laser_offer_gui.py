"""Fresh-session laser offers, with offscreen dialogs and mocked HTTP."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from PySide6 import QtCore, QtWidgets

from GUI import services
from GUI.laser_log_dialog import LaserLogDialog, LaserOfferDialog
from UPbRPy.integration import upbdb as db
from UPbRPy.laser_tie import tie_laser_log
from UPbRPy.laserlog import read_lase
from UPbRPy.pipeline import PipelineStage
from synthetic_bundles import write_session_bundle
from test_laser_selection_gui import app, windows, idle
from test_laser_tie import session_log
from test_upbdb_laser import laser_remote, SERVER


@pytest.fixture(autouse=True)
def no_tims_queries(monkeypatch):
    from GUI.tims import TimsController

    monkeypatch.setattr(TimsController, "sync", lambda self: None)


def wait_until(app, condition):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.005)
    pytest.fail("Laser offer worker did not finish")


def fresh(window, tmp_path, *, local=True, laser=False, origin=None):
    folder = write_session_bundle(tmp_path / "fresh", laser=laser, upbr=False)
    if local:
        session_log(folder)
    window.load_experiment_directory(str(folder), primary_channel="44Ca", background=False, offer_restore=False)
    window.session.origin = origin
    return folder


def shown(window):
    offer = window._laser_offer
    return offer is not None and hasattr(offer, "dialog") and offer.dialog.isVisible()


def test_nearby_offer_once_decline_saved_and_restore_never_asks(windows, app, tmp_path):
    window = windows(laser=False)
    fresh(window, tmp_path)
    before = window.session.experiment.copy()
    wait_until(app, lambda: shown(window))
    dialog = window._laser_offer.dialog
    assert "synthetic.lase" in dialog.text()
    assert "reduction numbers" in dialog.text()
    assert dialog.width() <= 1280 and dialog.height() <= 800
    assert dialog.minimumSizeHint().width() <= 1280
    dialog.not_now_button.click()
    assert window.session.laser_offer_declined
    assert window.project_workspace_state()["laser_offer_declined"] is True
    window._laser_offer.start()
    assert not shown(window)
    pd.testing.assert_frame_equal(window.session.experiment, before)
    path = tmp_path / "declined.upbr"
    window.save_project(path)
    assert json.loads(path.read_text())["workspace"]["laser_offer_declined"] is True
    reopened = windows(project=path)
    assert reopened.session.laser_offer_declined and reopened._laser_offer is None
    # A restored project never checks, even when it has never declined.
    document = json.loads(path.read_text())
    del document["workspace"]["laser_offer_declined"]
    path.write_text(json.dumps(document))
    reopened = windows(project=path)
    assert not reopened.session.laser_offer_declined and reopened._laser_offer is None
    assert reopened.load_laser_log_action.isEnabled()


def test_offer_import_uses_existing_preview_and_preserves_default_values(windows, app, tmp_path, monkeypatch):
    window = windows(laser=False)
    folder = fresh(window, tmp_path)
    before = window.session.experiment.copy()
    data = window.session.total_data.copy()
    wait_until(app, lambda: shown(window))
    previews = []

    def accept(dialog):
        previews.append(dialog)
        assert dialog.key_combo.currentData() == "position"
        assert dialog.threshold_spin.value() == 30.
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(LaserLogDialog, "exec", accept)
    window._laser_offer.dialog.import_button.click()
    assert len(previews) == 1 and window.session.laser_tie is not None
    assert window.session.experiment.attrs["laser"]["source"] == "lase"
    assert "laser_offer_declined" not in window.project_workspace_state()
    pd.testing.assert_frame_equal(window.session.experiment[before.columns], before)
    pd.testing.assert_frame_equal(window.session.total_data, data)
    assert window.selection_method == "threshold"


@pytest.mark.parametrize("laser,local", [(True, True), (False, False)])
def test_no_offer_with_manifest_data_or_no_source(windows, app, tmp_path, monkeypatch, laser, local):
    window = windows(laser=False)
    monkeypatch.setattr(db, "get_session", lambda *a: pytest.fail("Unexpected database request"))
    fresh(window, tmp_path, laser=laser, local=local)
    app.processEvents()
    assert not shown(window)


def test_not_signed_in_makes_no_database_request(windows, app, tmp_path, laser_remote):
    window = windows(laser=False)
    db.store_token("https://another.example.org", "other-account")
    fresh(window, tmp_path, origin={"server": SERVER, "session_id": "s"})
    wait_until(app, lambda: shown(window))
    assert not laser_remote.calls
    window._laser_offer.dialog.not_now_button.click()


def test_database_priority_paged_import_and_reopen(windows, app, tmp_path, laser_remote):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    fresh(window, tmp_path, origin={"server": SERVER, "session_id": "s"})
    remote = laser_remote
    remote.logs = [{"id": "log", "file_name": "tied.lase", "has_laser_times": True,
                    "spot_key": "position", "clock_offset_s": 3600.}]
    manifest = services.resolve_session_manifest(window.session.directory, None)
    for index, row in enumerate(manifest.acquisitions):
        start = datetime.fromisoformat(row.started_at)
        remote.rows.append({"acquisition_id": str(index), "label": row.label, "log_id": "log",
            "laser_on_at": (start - timedelta(seconds=3596)).isoformat(),
            "laser_off_at": (start - timedelta(seconds=3586)).isoformat(), "stage_um": [1, 2, 3]})
    # Thousands of rows require pages, not one request per acquisition.
    remote.rows += [{"acquisition_id": str(i), "label": f"unused_{i}", "log_id": None} for i in range(4, 2504)]
    before = window.session.experiment.copy()
    cached = object()
    window.session.reduction_cache["baseline_subtracted_total_data"] = cached
    window.session.pipeline.cache.set(PipelineStage.BASELINE, cached)
    wait_until(app, lambda: shown(window))
    assert "tied in UPbDB" in window._laser_offer.dialog.text()
    assert len(remote.calls) == 2  # Detail and a one-row compatibility probe.
    began = time.perf_counter()
    window._laser_offer.dialog.import_button.click()
    wait_until(app, lambda: window.session.laser_tie is not None)
    idle(app, window)
    elapsed = time.perf_counter() - began
    assert window.session.experiment.attrs["laser"]["source"] == "database"
    assert window.session.experiment.laser_on_time.eq(window.session.experiment.spot_starting_times + 4.).all()
    assert len(remote.calls) == 8  # 2 availability + 6 acquisition pages.
    print(f"Studio laser import: before=0, after={len(remote.calls)} requests; {elapsed:.4f} s mocked")
    pd.testing.assert_frame_equal(window.session.experiment[before.columns], before)
    services.ensure_shared_pipeline(window.session, window.workflow_preset)
    assert window.session.reduction_cache["baseline_subtracted_total_data"] is cached
    assert window.session.pipeline.cache.get(PipelineStage.BASELINE) is cached
    assert "stage_um" not in json.dumps(window.session.pipeline.manifest())
    # Test values are objects only to check identity; do not serialize them.
    window.session.reduction_cache.clear()
    window.session.pipeline.cache.clear()
    assert not any("stage" in c for c in window.session.experiment)
    path = tmp_path / "database-laser.upbr"
    window.save_project(path)
    reopened = windows(project=path)
    assert reopened.session.experiment.attrs["laser"]["source"] == "database"
    pd.testing.assert_series_equal(reopened.session.experiment.laser_on_time, window.session.experiment.laser_on_time)
    assert len(remote.calls) == 8


@pytest.mark.parametrize("older", ["fields", "route"])
def test_older_server_skipped_silently(windows, app, tmp_path, laser_remote, older):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    if older == "fields":
        laser_remote.detail = {}
    else:
        laser_remote.laser_status = 404
    fresh(window, tmp_path, local=False, origin={"server": SERVER, "session_id": "s"})
    wait_until(app, lambda: bool(laser_remote.calls))
    links = window.database()
    wait_until(app, lambda: not links._workers)
    assert not shown(window)
    assert len(laser_remote.calls) == (1 if older == "fields" else 2)


def test_late_database_reply_does_not_offer_after_manual_log(windows, app, tmp_path, laser_remote, monkeypatch):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    folder = fresh(window, tmp_path, origin={"server": SERVER, "session_id": "s"})
    laser_remote.logs = [{"id": "log", "has_laser_times": True}]
    callbacks = []
    monkeypatch.setattr(window.database(), "run", lambda function, success, **kw: callbacks.append((function, success)))
    app.processEvents()
    assert len(callbacks) == 1
    window.apply_laser_log(tie_laser_log(read_lase(folder / "synthetic.lase"), window.session.qtegra))
    function, success = callbacks.pop()
    success(function())
    assert not shown(window)


@pytest.mark.parametrize("stored", ["manifest", "tie"])
def test_signed_in_existing_laser_data_does_not_check_database(windows, app, tmp_path, laser_remote, stored):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    folder = fresh(window, tmp_path, laser=stored == "manifest",
                   origin={"server": SERVER, "session_id": "s"})
    if stored == "tie":
        window.apply_laser_log(tie_laser_log(read_lase(folder / "synthetic.lase"), window.session.qtegra))
    app.processEvents()
    assert not laser_remote.calls and not shown(window)


def test_database_without_times_falls_back_to_nearby_log(windows, app, tmp_path, laser_remote):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    laser_remote.detail["laser_has_times"] = False
    fresh(window, tmp_path, origin={"server": SERVER, "session_id": "s"})
    wait_until(app, lambda: shown(window))
    assert "synthetic.lase" in window._laser_offer.dialog.text()
    assert len(laser_remote.calls) == 1
    window._laser_offer.dialog.not_now_button.click()


def test_file_menu_can_retry_declined_database_offer(windows, app, tmp_path, laser_remote):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    laser_remote.logs = [{"id": "log", "has_laser_times": True}]
    fresh(window, tmp_path, local=False, origin={"server": SERVER, "session_id": "s"})
    wait_until(app, lambda: shown(window))
    window._laser_offer.dialog.not_now_button.click()
    assert window.session.laser_offer_declined
    window.choose_laser_log()
    wait_until(app, lambda: shown(window))
    assert "tied in UPbDB" in window._laser_offer.dialog.text()
    assert len(laser_remote.calls) == 4
    assert all("If-None-Match" in call[2]["headers"] for call in laser_remote.calls[2:])
    window._laser_offer.dialog.not_now_button.click()


def test_database_bundle_empty_export_project_is_fresh(windows, app, tmp_path):
    from GUI.laser_offer import is_fresh_bundle

    window = windows(laser=False)
    folder = write_session_bundle(tmp_path / "export", upbr=True)
    session_log(folder)
    project = next(folder.glob("*.upbr"))
    document = json.loads(project.read_text())
    document["pipeline"]["completed_stages"] = []
    document["pipeline"]["resume_stage"] = "import"
    project.write_text(json.dumps(document))
    bundle = SimpleNamespace(project_file=project, raw_directory=folder / "raw")
    assert is_fresh_bundle(bundle)
    window._open_session_bundle_here(bundle)
    wait_until(app, lambda: shown(window))
    window._laser_offer.dialog.not_now_button.click()
    idle(app, window)
    window.save_project(project)
    assert not is_fresh_bundle(bundle)
    reopened = windows(project=project)
    assert reopened._laser_offer is None


def test_application_bundle_opener_marks_fresh_export(windows, app, tmp_path):
    from GUI.main import StudioWindowController

    window = windows(laser=False)
    folder = write_session_bundle(tmp_path / "export", upbr=True)
    project = next(folder.glob("*.upbr"))
    document = json.loads(project.read_text())
    document["pipeline"]["completed_stages"] = []
    project.write_text(json.dumps(document))
    calls = []
    fake_window = SimpleNamespace(_offer_laser_for_fresh_bundle=calls.append, append_log=lambda text: None)
    controller = SimpleNamespace(open_project_in_window=lambda path: fake_window)
    bundle = SimpleNamespace(project_file=project, reused=False, session_key=folder.name, path=folder)
    assert StudioWindowController.open_session_bundle(controller, bundle) is fake_window
    assert calls == [project]


def test_local_folder_without_manifest_uses_database_time_zone(windows, app, tmp_path, laser_remote):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    folder = fresh(window, tmp_path, local=False, origin={"server": SERVER, "session_id": "s"})
    (folder / "session_manifest.toml").unlink()
    laser_remote.detail["timezone"] = "America/Denver"
    laser_remote.logs = [{"id": "log", "clock_offset_s": 3600., "has_laser_times": True}]
    for i, acquisition in enumerate(window.session.qtegra.acquisitions):
        instant = acquisition.start.replace(tzinfo=ZoneInfo("America/Denver"))
        laser_remote.rows.append({"acquisition_id": str(i), "label": acquisition.label, "log_id": "log",
            "laser_on_at": (instant.astimezone(timezone.utc) - timedelta(seconds=3596)).isoformat(),
            "laser_off_at": (instant.astimezone(timezone.utc) - timedelta(seconds=3586)).isoformat()})
    before = window.session.experiment.copy()
    wait_until(app, lambda: shown(window))
    window._laser_offer.dialog.import_button.click()
    wait_until(app, lambda: window.session.laser_tie is not None)
    idle(app, window)
    assert window.session.experiment.laser_on_time.eq(window.session.experiment.spot_starting_times + 4.).all()
    pd.testing.assert_frame_equal(window.session.experiment[before.columns], before)
    assert len(laser_remote.calls) == 3  # Same detail/probe/page budget, no extra request.


@pytest.mark.parametrize("time_zone", [None, "Invalid/Zone"])
def test_missing_or_invalid_local_zone_suppresses_offer(windows, app, tmp_path, laser_remote, monkeypatch, time_zone):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    folder = fresh(window, tmp_path, origin={"server": SERVER, "session_id": "s"})
    (folder / "session_manifest.toml").unlink()
    laser_remote.detail["timezone"] = time_zone
    messages = []
    monkeypatch.setattr(window, "append_log", messages.append)
    wait_until(app, lambda: bool(laser_remote.calls))
    wait_until(app, lambda: not window.database()._workers)
    assert not shown(window) and len(laser_remote.calls) == 1
    assert len(messages) == 1 and "valid session time zone" in messages[0]
    assert window.session.laser_tie is None


def test_database_clock_mismatch_does_not_apply_and_shows_reason(windows, app, tmp_path, laser_remote):
    window = windows(laser=False)
    db.store_token(SERVER, "account-a")
    fresh(window, tmp_path, local=False, origin={"server": SERVER, "session_id": "s"})
    laser_remote.logs = [{"id": "log", "clock_offset_s": 0., "has_laser_times": True}]
    manifest = services.resolve_session_manifest(window.session.directory, None)
    for i, row in enumerate(manifest.acquisitions):
        start = datetime.fromisoformat(row.started_at)
        laser_remote.rows.append({"acquisition_id": str(i), "label": row.label, "log_id": "log",
            "laser_on_at": (start + timedelta(hours=7 if i < 3 else 0)).isoformat()})
    before = window.session.experiment.copy()
    wait_until(app, lambda: shown(window))
    window._laser_offer.dialog.import_button.click()
    wait_until(app, lambda: "clock or zone mismatch" in window.statusBar().currentMessage())
    idle(app, window)
    assert "dropped 3 of 4" in window.statusBar().currentMessage()
    assert window.session.laser_tie is None and not window.session.laser_data_available
    pd.testing.assert_frame_equal(window.session.experiment, before)
