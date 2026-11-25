"""Offline laser upload bytes, request identities and confirmed server ties."""

import hashlib
from collections import Counter
import time
from types import SimpleNamespace

import pytest
from PySide6 import QtWidgets

from GUI.laser_log_dialog import LaserUploadDialog, LaserOfferDialog, LaserLogDialog
from GUI.laser_upload import system_zone
from UPbRPy.integration import upbdb as db
from test_laser_log_gui import make_tie
from test_laser_selection_gui import app, windows
from test_laser_offer_gui import wait_until, fresh, shown, no_tims_queries
from test_upbdb_retry import Reply, SERVER


@pytest.fixture
def remote(monkeypatch):
    previous = db.set_token_store(db.MemoryTokenStore())
    monkeypatch.setattr(db, "_laser_upload_unsupported", set())
    monkeypatch.setattr(db, "availability", lambda: (True, ""))
    state = SimpleNamespace(calls=[], status=201, tie_status=200,
        preview_rows=[{"label": f"row-{i}", "status": "linked" if i < 4998 else "doubtful" if i == 4998 else "unlinked"} for i in range(5000)],
        account={"groups": [{"lab_id": "lab-a", "name": "Lab A"}]}, revision="a" * 64)

    def request(method, url, **kw):
        state.calls.append((method, url, kw))
        if url.endswith("accounts/me"):
            return Reply(payload=state.account)
        if url.endswith("sessions/s"):
            return Reply(payload={"timezone": "America/Boise", "laser_log_count": 0})
        if url.endswith("tie/preview"):
            return Reply(payload={"revision": state.revision, "sessions": [{"id": "s",
                "counts": dict(Counter(row["status"] for row in state.preview_rows)), "warnings": []}],
                "rows": state.preview_rows[:kw["params"]["limit"]]})
        if url.endswith("/tie"):
            return Reply(state.tie_status, {"detail": "Stale preview"} if state.tie_status == 409 else {"sessions": []})
        return Reply(state.status, {"id": "log-1"} if state.status in (200, 201) else
                     {"detail": {401: "Token expired",
                                 413: "Laser logs must be at most 20 MB.",
                                 422: "Choose a lab when you belong to several labs.",
                                 404: "Not found", 405: "Method not allowed"}[state.status]})

    # Include exception types so error translation is exercised normally.
    class TransportFailure(Exception):
        pass

    exceptions = SimpleNamespace(Timeout=TransportFailure, RequestException=TransportFailure)
    monkeypatch.setattr(db, "_requests", lambda: SimpleNamespace(request=request, exceptions=exceptions))
    yield state
    db.set_token_store(previous)


def signed(remote):
    db.store_token(SERVER, "account-a")


@pytest.mark.parametrize("status", [201, 200])
def test_upload_original_bytes_stable_key_and_body(remote, tmp_path, status):
    signed(remote)
    remote.status = status
    path = tmp_path / "synthetic.lase"
    data = b"\xef\xbb\xbf[SystemInfo]\r\nSystemType=Synthetic\r\n"
    path.write_bytes(data)
    results = [db.upload_laser_log(SERVER, path, lab_id="lab-a", time_zone="UTC") for _ in range(2)]
    assert [r["id"] for r in results] == ["log-1", "log-1"]
    assert all(r["status_code"] == status for r in results)
    assert len(remote.calls) == 2
    first, second = [c[2] for c in remote.calls]
    assert first["data"] == second["data"] and data in first["data"]
    assert first["headers"]["Idempotency-Key"] == second["headers"]["Idempotency-Key"]
    assert hashlib.sha256(data).hexdigest() in first["headers"]["Idempotency-Key"]
    assert b'name="file"' in first["data"] and b'name="lab_id"' in first["data"]
    assert first["timeout"] == (db.CONNECT_TIMEOUT, db.READ_TIMEOUT)
    assert first["headers"]["Accept-Encoding"] == "gzip"


@pytest.mark.parametrize("status", [413, 422, 404, 405])
def test_upload_errors_and_route_memory(remote, tmp_path, status):
    signed(remote)
    path = tmp_path / "synthetic.lase"
    path.write_bytes(b"synthetic")
    remote.status = status
    with pytest.raises(db.DatabaseError) as failure:
        db.upload_laser_log(SERVER, path)
    assert failure.value.status == status
    assert f"HTTP {status}" in str(failure.value) and "Reply:" in str(failure.value)
    allowed, reason = db.laser_upload_availability(SERVER)
    assert allowed == (status not in (404, 405))
    if not allowed:
        assert reason == "This UPbDB server cannot receive laser logs"
    assert len(remote.calls) == 1
    db.store_token("https://other.example.org", "other")
    assert db.laser_upload_availability("https://other.example.org")[0]


def test_signed_out_no_request(remote, tmp_path):
    with pytest.raises(db.AuthenticationRequired):
        db.upload_laser_log(SERVER, tmp_path / "absent.lase")
    assert not db.laser_upload_availability(SERVER)[0]
    assert not remote.calls


def loaded(windows, remote, *, linked=True):
    window = windows(laser=False)
    window.apply_laser_log(make_tie(window))
    window.session.origin = {"server": SERVER, "session_id": "s"} if linked else None
    window.database().settings.setValue("database/last_server", SERVER)
    return window


def upload_dialog(window, app):
    window.upload_laser_log()
    flow = window._laser_upload
    wait_until(app, lambda: hasattr(flow, "dialog") and flow.dialog.isVisible())
    return flow


def uploaded(flow, app):
    flow.dialog.accept()
    wait_until(app, lambda: hasattr(flow, "log_id"))


def test_confirmation_upload_then_preview_and_confirmed_tie(windows, remote, app):
    signed(remote)
    window = loaded(windows, remote)
    before = window.session.experiment.copy()
    began = time.perf_counter()
    flow = upload_dialog(window, app)
    assert len(remote.calls) == 2  # account and linked session zone, no POST
    assert flow.dialog.zone_edit.text() == "America/Boise"
    uploaded(flow, app)
    assert flow.dialog.text().startswith("Uploaded")
    assert len(remote.calls) == 3
    flow.tie_button.click()
    wait_until(app, lambda: hasattr(flow, "revision"))
    assert "Linked: 4998" in flow.dialog.text()
    assert remote.calls[-1][2]["params"] == {"limit": 1}
    assert not any(c[1].endswith("/tie") for c in remote.calls)
    flow.dialog.button(QtWidgets.QMessageBox.StandardButton.Yes).click()
    wait_until(app, lambda: len(remote.calls) == 5 and not flow.busy)
    body = remote.calls[-1][2]["json"]
    assert body == {"session_ids": ["s"], "spot_key": "position", "threshold_s": 30, "revision": "a" * 64}
    import pandas as pd
    pd.testing.assert_frame_equal(window.session.experiment, before)
    print(f"Laser upload baseline: 0 requests; confirmed upload+tie: {len(remote.calls)} requests, "
          f"{time.perf_counter() - began:.3f}s; preview summarizes 5000 rows with 1 row requested")


def test_duplicate_no_silent_tie_and_cancelled_preview(windows, remote, app):
    signed(remote)
    remote.status = 200
    window = loaded(windows, remote)
    flow = upload_dialog(window, app)
    uploaded(flow, app)
    assert flow.dialog.text().startswith("Already in UPbDB") and "log-1" in flow.dialog.text()
    assert not any("/tie" in c[1] for c in remote.calls)
    flow.tie_button.click()
    wait_until(app, lambda: hasattr(flow, "revision"))
    flow.dialog.button(QtWidgets.QMessageBox.StandardButton.Cancel).click()
    app.processEvents()
    assert not any(c[1].endswith("/tie") for c in remote.calls)


def test_stale_preview_refresh_needs_another_confirmation(windows, remote, app):
    signed(remote)
    window = loaded(windows, remote)
    flow = upload_dialog(window, app)
    uploaded(flow, app)
    flow.tie_button.click()
    wait_until(app, lambda: hasattr(flow, "revision"))
    remote.tie_status = 409
    flow.dialog.button(QtWidgets.QMessageBox.StandardButton.Yes).click()
    wait_until(app, lambda: "stale" in flow.dialog.text())
    assert len([c for c in remote.calls if c[1].endswith("/tie")]) == 1
    remote.revision = "b" * 64
    next(b for b in flow.dialog.buttons() if b.text() == "Refresh preview").click()
    wait_until(app, lambda: flow.revision == "b" * 64)
    assert len([c for c in remote.calls if c[1].endswith("/tie")]) == 1
    flow.dialog.button(QtWidgets.QMessageBox.StandardButton.Cancel).click()


def test_cancel_upload_and_unlinked_result(windows, remote, app):
    signed(remote)
    window = loaded(windows, remote, linked=False)
    flow = upload_dialog(window, app)
    flow.dialog.reject()
    assert not any(c[0] == "POST" for c in remote.calls)
    flow = upload_dialog(window, app)
    uploaded(flow, app)
    assert flow.tie_button is None and "UPb Lab" in flow.dialog.informativeText()
    flow.dialog.close()


def test_hash_checked_before_dialog_and_again_before_send(windows, remote, app, monkeypatch):
    signed(remote)
    window = loaded(windows, remote)
    errors = []
    monkeypatch.setattr(window, "_handle_action_error", lambda title, error: errors.append(str(error)))
    flow = upload_dialog(window, app)
    flow.path.write_bytes(b"changed synthetic file")
    flow.dialog.accept()
    wait_until(app, lambda: bool(errors))
    assert "has changed" in errors[0] and not any(c[0] == "POST" for c in remote.calls)
    remote.calls.clear()
    errors.clear()
    window.upload_laser_log()
    wait_until(app, lambda: bool(errors))
    assert not remote.calls


def test_availability_no_requests(windows, remote, monkeypatch):
    window = windows(laser=False)
    assert not window.upload_laser_log_action.isVisible()
    window.apply_laser_log(make_tie(window))
    window.session.origin = {"server": SERVER, "session_id": "s"}
    window._sync_laser_controls()
    assert window.upload_laser_log_action.isVisible()
    assert not window.upload_laser_log_action.isEnabled()
    assert "Sign in" in window.upload_laser_log_action.toolTip()
    window.upload_laser_log()
    assert not remote.calls
    signed(remote)
    monkeypatch.setattr(db, "availability", lambda: (False, "database extra missing"))
    window._sync_laser_controls()
    assert "extra missing" in window.upload_laser_log_action.toolTip()
    assert not remote.calls


@pytest.mark.parametrize("labs", [[], ["a"], ["a", "b"]])
def test_dialog_zone_labs_and_laptop_fit(app, labs):
    account = {"groups": [{"lab_id": lab, "name": lab} for lab in labs for _ in range(2)]}
    dialog = LaserUploadDialog("synthetic.lase", 1234, SERVER, account, "UTC")
    try:
        dialog.show()
        app.processEvents()
        assert dialog.width() <= 1280 and dialog.height() <= 800
        assert dialog.minimumSizeHint().width() <= 1280
        assert dialog.lab_combo.count() == len(labs)
        assert dialog.lab_combo.isVisible() == (len(labs) > 1)
        assert dialog.upload_button.isEnabled() == bool(labs)
        dialog.zone_edit.setText("not/an/IANA/zone")
        dialog.accept()
        assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted
        assert not dialog.upload_button.isEnabled()
    finally:
        dialog.close()


def test_import_offer_upload_checkbox(windows, remote, app, tmp_path, monkeypatch):
    signed(remote)
    window = windows(laser=False)
    window.database().settings.setValue("database/last_server", SERVER)
    fresh(window, tmp_path)
    wait_until(app, lambda: shown(window))
    dialog = window._laser_offer.dialog
    assert dialog.upload_check.isVisible()
    dialog.upload_check.setChecked(True)
    monkeypatch.setattr(LaserLogDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    dialog.import_button.click()
    wait_until(app, lambda: hasattr(window, "_laser_upload") and hasattr(window._laser_upload, "dialog"))
    assert isinstance(window._laser_upload.dialog, LaserUploadDialog)
    assert not any(c[0] == "POST" for c in remote.calls)
    window._laser_upload.dialog.reject()


@pytest.mark.parametrize("status", [404, 405])
def test_failed_route_disables_menu_without_probing(windows, remote, app, monkeypatch, status):
    signed(remote)
    window = loaded(windows, remote)
    errors = []
    monkeypatch.setattr(window, "_handle_action_error", lambda *args: errors.append(args))
    remote.status = status
    flow = upload_dialog(window, app)
    flow.dialog.accept()
    wait_until(app, lambda: bool(errors))
    assert not window.upload_laser_log_action.isEnabled()
    assert window.upload_laser_log_action.toolTip() == "This UPbDB server cannot receive laser logs"
    count = len(remote.calls)
    window._sync_laser_controls()
    window.upload_laser_log()
    assert len(remote.calls) == count


@pytest.mark.parametrize("change", ["sign_out", "replace_session"])
def test_pending_confirmation_invalidated(windows, remote, app, change):
    signed(remote)
    window = loaded(windows, remote)
    flow = upload_dialog(window, app)
    if change == "sign_out":
        db.forget_token(SERVER)
    else:
        window.session.origin = {"server": SERVER, "session_id": "different"}
    flow.dialog.accept()
    app.processEvents()
    assert not any(c[0] == "POST" for c in remote.calls)


def test_oversize_and_invalid_zone_are_local_errors(remote, tmp_path):
    signed(remote)
    path = tmp_path / "synthetic.lase"
    with path.open("wb") as file:
        file.truncate(db.MAX_LASER_LOG_BYTES + 1)
    with pytest.raises(db.RequestRejected, match="20 MB"):
        db.upload_laser_log(SERVER, path)
    path.write_bytes(b"synthetic")
    with pytest.raises(db.RequestRejected, match="IANA"):
        db.upload_laser_log(SERVER, path, time_zone="Not/a/zone")
    assert not remote.calls


def test_upload_options_have_distinct_retry_identity(remote, tmp_path):
    signed(remote)
    path = tmp_path / "synthetic.lase"
    path.write_bytes(b"synthetic")
    db.upload_laser_log(SERVER, path, lab_id="a")
    db.upload_laser_log(SERVER, path, lab_id="b")
    assert len({call[2]["headers"]["Idempotency-Key"] for call in remote.calls}) == 2


def test_offer_checkbox_hidden_when_signed_out_and_for_database(app):
    for allowed in (False, True):
        dialog = LaserOfferDialog("synthetic.lase", allow_upload=allowed)
        dialog.show()
        app.processEvents()
        assert dialog.upload_check.isVisible() == allowed
        assert dialog.width() <= 1280 and dialog.height() <= 800
        dialog.close()



def test_expired_sign_in_still_shows_error(windows, remote, app, monkeypatch):
    signed(remote)
    window = loaded(windows, remote)
    errors = []
    monkeypatch.setattr(window, "_handle_action_error", lambda title, error: errors.append(str(error)))
    flow = upload_dialog(window, app)
    remote.status = 401
    flow.dialog.accept()
    wait_until(app, lambda: bool(errors))
    assert "HTTP 401" in errors[0] and "Sign in" in errors[0]
    assert not window.upload_laser_log_action.isEnabled()


def test_import_upload_waits_for_repick_worker(windows, remote, app, monkeypatch):
    window = loaded(windows, remote)
    offered = []
    monkeypatch.setattr(window, "upload_laser_log", lambda: offered.append(True))
    window._laser_upload_after_apply = window.session.laser_tie.sha256
    window._install_laser_tie_session(window.session)
    window._active_worker = object()
    app.processEvents()
    assert not offered
    window._active_worker = None
    wait_until(app, lambda: bool(offered))
    assert offered == [True] and not remote.calls


def test_recorded_zone_preferred_without_session_read(windows, remote, app):
    signed(remote)
    window = loaded(windows, remote)
    window.session.import_settings["time_zone"] = "Pacific/Auckland"
    flow = upload_dialog(window, app)
    assert flow.dialog.zone_edit.text() == "Pacific/Auckland"
    assert len(remote.calls) == 1
    flow.dialog.reject()


def test_system_iana_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Boise")
    assert system_zone() == "America/Boise"
