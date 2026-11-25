"""Offline laser route pagination, revalidation and saved tie conversion."""

from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from UPbRPy.integration import upbdb as db
from UPbRPy.laser_tie import LaserTie
from test_upbdb_retry import Reply, SERVER


@pytest.fixture
def laser_remote(monkeypatch):
    previous = db.set_token_store(db.MemoryTokenStore())
    monkeypatch.setattr(db, "_laser_responses", db.OrderedDict())
    state = SimpleNamespace(detail={"laser_log_count": 1, "laser_has_times": True, "timezone": "UTC"},
                            rows=[], logs=[], laser_status=200, calls=[])

    def request(method, url, **kwargs):
        state.calls.append((method, url, kwargs))
        params = kwargs.get("params") or {}
        tag = '"' + url + json.dumps(params, sort_keys=True) + '"'
        if url.endswith("/laser") and state.laser_status != 200:
            return Reply(state.laser_status, {"detail": "Laser route unavailable"})
        if kwargs["headers"].get("If-None-Match") == tag:
            return Reply(304, headers={"ETag": tag})
        if not url.endswith("/laser"):
            return Reply(payload=state.detail, headers={"ETag": tag})
        start, log_start = int(params.get("after", 0)), int(params.get("logs_after", 0))
        size, log_size = params["limit"], params["logs_limit"]
        following = str(start + size) if start + size < len(state.rows) else None
        logs_following = str(log_start + log_size) if log_start + log_size < len(state.logs) else None
        return Reply(payload={"rows": state.rows[start:start + size],
                              "logs": state.logs[log_start:log_start + log_size],
                              "logs_next_cursor": logs_following},
                     headers={"ETag": tag, **({"X-Next-Cursor": following} if following else {})})

    monkeypatch.setattr(db, "_requests", lambda: SimpleNamespace(request=request))
    yield state
    db.forget_token(SERVER)
    db.set_token_store(previous)


def test_large_laser_pages_revalidate_and_clear_on_signout(laser_remote, monkeypatch):
    remote = laser_remote
    remote.rows = [{"acquisition_id": str(i), "label": f"A_{i}"} for i in range(5000)]
    remote.logs = [{"id": str(i)} for i in range(1200)]
    with pytest.raises(db.AuthenticationRequired):
        db.get_session(SERVER, "s")
    assert not remote.calls
    db.store_token(SERVER, "account-a")
    began = time.perf_counter()
    rows, logs, cursor, logs_cursor = {}, {}, None, None
    for _ in range(10):
        page = db.get_session_laser(SERVER, "s", cursor=cursor, logs_cursor=logs_cursor)
        rows.update((row["acquisition_id"], row) for row in page.rows)
        logs.update((log["id"], log) for log in page.logs)
        cursor = page.next_cursor or cursor
        logs_cursor = page.logs_next_cursor or logs_cursor
        if page.next_cursor is None and page.logs_next_cursor is None:
            break
    elapsed = time.perf_counter() - began
    assert list(rows.values()) == remote.rows and list(logs.values()) == remote.logs
    assert len(remote.calls) == 10
    print(f"Laser requests: before=0, after=10 for 5000 acquisitions / 1200 logs; {elapsed:.4f} s mocked")
    first = db.get_session_laser(SERVER, "s")
    assert len(first.rows) == 500
    assert "If-None-Match" in remote.calls[-1][2]["headers"]
    assert all(call[2]["timeout"] == (db.CONNECT_TIMEOUT, db.READ_TIMEOUT) for call in remote.calls)
    assert all(call[2]["headers"]["Accept-Encoding"] == "gzip" for call in remote.calls)
    db.get_session(SERVER, "s")
    db.get_session(SERVER, "s")
    assert "If-None-Match" in remote.calls[-1][2]["headers"]
    db.store_token(SERVER, "account-b")
    db.get_session_laser(SERVER, "s")
    assert "If-None-Match" not in remote.calls[-1][2]["headers"]
    db.forget_token(SERVER)
    assert not db._laser_responses
    db.store_token(SERVER, "account-a")
    db.get_session_laser(SERVER, "s")
    assert "If-None-Match" not in remote.calls[-1][2]["headers"]
    monkeypatch.setattr(db, "_requests", lambda: SimpleNamespace(
        request=lambda *args, **kwargs: Reply(payload=["unexpected"])))
    with pytest.raises(db.ServerError, match="HTTP 200.*unexpected"):
        db.get_session(SERVER, "s")


def test_database_ties_use_saved_offsets_without_threshold():
    start = datetime(2030, 1, 2, tzinfo=timezone.utc)
    logs = [{"id": "one", "file_name": "one.lase", "spot_key": "position", "clock_offset_s": 3600.},
            {"id": "two", "file_name": "two.lase", "spot_key": "description", "clock_offset_s": 7200.}]
    rows = [{"label": "A_1", "log_id": "one", "method": "manual",
             "laser_on_at": (start - timedelta(seconds=3500)).isoformat(),
             "laser_off_at": (start - timedelta(seconds=3490)).isoformat(),
             "stage_um": [1, 2, 3]},
            {"label": "A_2", "log_id": "two", "method": "time",
             "laser_off_at": (start - timedelta(seconds=7190)).isoformat()},
            {"label": "A_3", "log_id": None}]
    tie = db.session_laser_tie(rows, logs, {"A_1": start, "A_2": start, "A_3": start})
    assert tie.rows["A_1"].laser_on == 100.  # A trusted manual tie, more than 30 s.
    assert tie.rows["A_2"].laser_on is None and tie.rows["A_2"].laser_off == 10.
    assert tie.source == "database" and tie.counts["linked"] == 2
    assert LaserTie.from_dict(json.loads(json.dumps(tie.to_dict()))) == tie
    experiment = pd.DataFrame({"spot_names": ["A_1", "A_2", "A_3"], "spot_starting_times": [0., 60., 120.]})
    tie.apply(experiment)
    assert experiment.laser_on_time.iloc[0] == 100.
    assert experiment.laser_off_time.iloc[1] == 70.
    assert experiment.attrs["laser"]["source"] == "database"
    assert not any("stage" in column for column in experiment)
    assert "stage" not in json.dumps(experiment.attrs)


@pytest.mark.parametrize("local,instant", [
    (datetime(2026, 1, 2, 10), datetime(2026, 1, 2, 17, tzinfo=timezone.utc)),
    (datetime(2026, 7, 2, 10), datetime(2026, 7, 2, 16, tzinfo=timezone.utc)),
    (datetime(2026, 3, 8, 1, 59), datetime(2026, 3, 8, 8, 59, tzinfo=timezone.utc)),
    (datetime(2026, 3, 8, 3, 1), datetime(2026, 3, 8, 9, 1, tzinfo=timezone.utc)),
    # An ambiguous naive value always uses fold=0, even if its fold was set.
    (datetime(2026, 11, 1, 1, 30, fold=1), datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)),
])
def test_local_database_starts_match_aware_instants_across_dst(local, instant):
    logs = [{"id": "log", "clock_offset_s": 3600.}]
    rows = [{"label": "A", "log_id": "log",
             "laser_on_at": (instant - timedelta(seconds=3596)).isoformat(),
             "laser_off_at": (instant - timedelta(seconds=3586)).isoformat()}]
    naive = db.session_laser_tie(rows, logs, {"A": local}, time_zone="America/Denver")
    aware = db.session_laser_tie(rows, logs, {"A": instant}, time_zone="Invalid/Zone")
    assert naive.windows() == aware.windows() == {"A": (4., 14.)}


@pytest.mark.parametrize("time_zone", [None, "", "Invalid/Zone"])
def test_naive_database_starts_require_valid_zone(time_zone):
    with pytest.raises(ValueError, match="naive acquisition starts need a valid session time zone"):
        db.session_laser_tie([], [], {"A": datetime(2026, 1, 2, 10)}, time_zone=time_zone)


def test_database_clock_guard_counts_drops_and_rejects_a_majority():
    start = datetime(2030, 1, 2, tzinfo=timezone.utc)
    logs = [{"id": "log", "clock_offset_s": 0.}]

    def row(label, delta):
        return {"label": label, "log_id": "log",
                "laser_on_at": (start + timedelta(seconds=delta)).isoformat()}

    rows = [row("A", 3600.), row("B", -3601.)]
    tie = db.session_laser_tie(rows, logs, {"A": start, "B": start})
    assert tie.windows() == {"A": (3600., None)}  # Exactly one hour is permitted.
    assert tie.counts == {"linked": 1, "doubtful": 0, "unlinked": 0, "clock_mismatch": 1}
    assert "dropped 1 of 2" in tie.warnings[0]
    rows.append(row("C", 3601.))
    # Unlinked acquisition rows do not dilute the mismatch fraction.
    rows.extend({"label": f"unused_{i}", "log_id": None} for i in range(1000))
    with pytest.raises(ValueError, match="clock or zone mismatch: dropped 2 of 3.*not applied"):
        db.session_laser_tie(rows, logs, {"A": start, "B": start, "C": start})
