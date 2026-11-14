"""Tell a running UPb Lab that Studio submitted a run (decision Q3, 2026-11-13).

UPb Lab polls ``<app data>/edu.umn.upbstudio/inbox/run-submitted/`` while it is
visible, re-reads the session's runs from the server (it never trusts the
file), refreshes and deletes the notice. When UPb Lab is not running the file
waits there; the next submit for the same session overwrites it. Writing a
file never launches or focuses UPb Lab. This module stays free of Qt.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

APP_IDENTIFIER = "edu.umn.upbstudio"


def inbox_directory() -> Path:
    """Tauri's app_data_dir for UPb Lab, plus ``inbox/run-submitted``."""
    if value := os.environ.get("UPBRPY_LAB_INBOX_DIR"):
        return Path(value).expanduser()
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return root / APP_IDENTIFIER / "inbox" / "run-submitted"


def notice_payload(session_id: str, run_id: str, server: str) -> dict:
    return {
        "v": 1,
        "type": "lab.run.submitted",
        "session_id": str(session_id),
        "run_id": str(run_id),
        "server": str(server),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "studio",
    }


def write_run_submitted(session_id: str, run_id: str, server: str) -> Path:
    """Atomically write ``<session_id>.json``; raises OSError on failure."""
    name = str(session_id)
    if not name or any(char in name for char in "/\\:") or name in {".", ".."}:
        raise ValueError(f"Unsafe session id for a notice file: {name!r}")
    folder = inbox_directory()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.json"
    data = json.dumps(notice_payload(session_id, run_id, server)).encode()
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
