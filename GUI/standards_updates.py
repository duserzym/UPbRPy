"""Check the shared standards checkout without adopting or editing values."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

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


def check_standards(active_path: str | None, checkout: Path) -> StandardsCheck:
    """Compare the active TOML library and shared checkout with GitHub main."""
    try:
        digest = reference_library_info(active_path)["sha256"]
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
    if changed:
        target = "GitHub main" if online else (
            "the local Git revision" if base == "HEAD" else "the last checked GitHub revision")
        messages.append(f"Warning: {len(changed)} standards TOML file(s) differ from {target}.")
    if digest != shared_digest:
        messages.append("Warning: Studio's configured library on disk differs from the shared checkout.")
    if not messages or not online:
        messages.append(status)
    return StandardsCheck(" ".join(messages), bool(changed) or digest != shared_digest,
                          digest, tuple(sorted(changed)))
