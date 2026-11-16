"""Check the shared standards checkout without adopting or editing values."""

from dataclasses import dataclass
from importlib.resources import files
import os
from pathlib import Path
import subprocess
import tomllib

from UPbRPy.standards import reference_library_info


STANDARDS_PAGE = "https://github.com/duserzym/upb-standards"
UPSTREAM_REF = "refs/upb-standards/checked"


@dataclass(frozen=True)
class StandardsCheck:
    message: str
    warning: bool = False
    digest: str = ""
    changed: tuple[str, ...] = ()


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
        timeout=12,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
             "GH_PROMPT_DISABLED": "1"},
    ).stdout


def library_revision(path: str | None) -> str:
    """Return the snapshot revision or a shared checkout's HEAD and edit marker."""
    try:
        root = Path(path) if path else files("UPbRPy") / "reference_materials"
        if root.name == "library.toml":
            root = root.parent
        metadata = root / "UPSTREAM.toml"
        if metadata.is_file():
            with metadata.open("rb") as stream:
                return tomllib.load(stream)["commit"][:12]
        if path is None:
            return ""
        revision = _git(root, "rev-parse", "HEAD").strip()
        changed = _git(root, "status", "--porcelain", "--", "*.toml").strip()
        return revision[:12] + (" (modified)" if changed else "")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def check_standards(active_path: str | None, checkout: Path) -> StandardsCheck:
    """Compare the active TOML library and shared checkout with GitHub main."""
    try:
        active_info = reference_library_info(active_path)
        digest = active_info["sha256"]
    except (OSError, ValueError):
        return StandardsCheck("Warning: the active standards library could not be read.", True)
    if not (checkout / ".git").exists():
        return StandardsCheck("Shared standards checkout is missing. " + str(checkout), digest=digest)

    online = True
    try:
        _git(checkout, "fetch", "--quiet", "--no-tags", "--no-prune",
             "--no-write-fetch-head", "--no-auto-maintenance", "--refmap=", "origin",
             f"+refs/heads/main:{UPSTREAM_REF}")
        base = _git(checkout, "rev-parse", UPSTREAM_REF).strip()
        status = "Standards match GitHub main."
    except (OSError, subprocess.SubprocessError):
        online = False
        try:
            base = _git(checkout, "rev-parse", "--verify", UPSTREAM_REF).strip()
        except (OSError, subprocess.SubprocessError):
            base = "HEAD"
        status = "GitHub standards check unavailable. Check your connection and Git access."

    try:
        changed = set(_git(checkout, "diff", "--no-renames", "--name-only", "-z",
                           base, "--", "*.toml").split("\0"))
        changed.update(_git(checkout, "ls-files", "--others", "--exclude-standard",
                            "-z", "--", "*.toml").split("\0"))
        changed.discard("")
        shared_digest = reference_library_info(checkout)["sha256"]
    except (OSError, ValueError, subprocess.SubprocessError):
        return StandardsCheck("Warning: the shared standards checkout could not be compared.", True, digest)

    messages = []
    snapshot_changed = False
    if revision := active_info.get("revision"):
        messages.append(f"Using upb-standards snapshot {revision[:12]}.")
        if base != "HEAD":
            messages.append(f"{'GitHub main' if online else 'Last checked revision'}: {base[:12]}.")
        # Compare the pinned revision itself when its Git object is available.
        # Documentation-only upstream commits do not require a data warning.
        try:
            newer = _git(checkout, "diff", "--name-only", revision, base,
                         "--", "library.toml", "sources.toml", "*/*.toml").strip()
        except (OSError, subprocess.SubprocessError):
            newer = ""
        snapshot_changed = bool(newer)
        if snapshot_changed:
            messages.append("Warning: standards data differs from the bundled snapshot revision.")

    if changed:
        target = "GitHub main" if online else (
            "the local Git revision" if base == "HEAD" else "the last checked GitHub revision")
        messages.append(f"Warning: {len(changed)} standards TOML file(s) differ from {target}.")
    if digest != shared_digest:
        messages.append("Warning: Studio's configured library on disk differs from the shared checkout.")
    if (not changed and digest == shared_digest and not snapshot_changed) or not online:
        messages.append(status)
    return StandardsCheck(" ".join(messages), bool(changed) or digest != shared_digest or snapshot_changed,
                          digest, tuple(sorted(changed)))
