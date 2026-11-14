"""Studio's run-submitted notice for UPb Lab (decision Q3)."""
import json
import os

import pytest

from GUI import lab_inbox


def test_notice_is_written_atomically_and_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("UPBRPY_LAB_INBOX_DIR", str(tmp_path / "inbox"))
    first = lab_inbox.write_run_submitted("sess-1", "run-a", "https://lab.example")
    second = lab_inbox.write_run_submitted("sess-1", "run-b", "https://lab.example")
    assert first == second == tmp_path / "inbox" / "sess-1.json"
    payload = json.loads(second.read_text())
    assert payload["v"] == 1 and payload["type"] == "lab.run.submitted"
    assert payload["run_id"] == "run-b" and payload["session_id"] == "sess-1"
    assert payload["server"] == "https://lab.example" and payload["source"] == "studio"
    assert payload["at"].endswith("+00:00")
    assert sorted(os.listdir(tmp_path / "inbox")) == ["sess-1.json"]


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a\\\\b", "..", "c:d"])
def test_unsafe_session_ids_are_refused(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("UPBRPY_LAB_INBOX_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        lab_inbox.write_run_submitted(bad, "run", "https://lab.example")


def test_default_folder_is_upb_labs_app_data(monkeypatch):
    monkeypatch.delenv("UPBRPY_LAB_INBOX_DIR")
    monkeypatch.setattr(lab_inbox.sys, "platform", "darwin")
    path = lab_inbox.inbox_directory()
    assert str(path).endswith("Library/Application Support/edu.umn.upbstudio/inbox/run-submitted")


def test_submit_leaves_notice_and_failure_is_only_logged(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from GUI.database import notify_lab

    logs = []
    window = SimpleNamespace(session=SimpleNamespace(origin={"session_id": "s-9", "server": "https://lab.example"}),
                             append_log=logs.append)
    monkeypatch.setenv("UPBRPY_LAB_INBOX_DIR", str(tmp_path / "inbox"))
    notify_lab(window, "run-9")
    assert json.loads((tmp_path / "inbox" / "s-9.json").read_text())["run_id"] == "run-9"
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("UPBRPY_LAB_INBOX_DIR", str(blocker / "inbox"))
    notify_lab(window, "run-10")
    assert logs and "Could not leave a notice for UPb Lab" in logs[0]
