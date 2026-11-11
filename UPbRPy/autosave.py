"""Locations for recoverable selection edits from unfiled sessions."""

import hashlib
import os
from pathlib import Path


def default_autosave_dir() -> Path:
    """Return ``$UPBRPY_AUTOSAVE_DIR`` or ``~/UPbRPy/autosave``."""
    configured = os.environ.get("UPBRPY_AUTOSAVE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "UPbRPy" / "autosave"


def selection_autosave_path(source: str | Path) -> Path:
    """Return the sidecar path for a resolved CSV or folder source."""
    resolved = Path(source).expanduser().resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:10]
    return default_autosave_dir() / f"{resolved.stem}-{digest}.upbr"
