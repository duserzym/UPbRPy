"""Version, release, installation-path and update-state rules (standard library only)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import metadata
import os
from pathlib import Path
import platform
import re
import sys
import tomllib


CHANNELS = ("stable", "beta")


def friendly_update_error(raw: str) -> str:
    """Translate updater diagnostics into a short message for the user."""
    message = raw.strip()
    lower = message.lower()
    if any(word in lower for word in ("pubkey", "public key", "base64", "decode", "not configured")):
        return "Updates are not configured for this build yet."
    if any(word in lower for word in ("404", "release json", "could not fetch a valid release",
                                      "not found", "no assets")):
        return "No release has been published yet."
    if any(word in lower for word in ("error sending request", "dns", "connect", "timed out",
                                      "timeout", "offline", "network", "failed to fetch",
                                      "enotfound", "urlerror")):
        return "Can't reach the update server. Check your internet connection and try again."
    return "The update check failed: " + (message[:117] + "…" if len(message) > 120 else message)


@dataclass(frozen=True)
class CheckButtonView:
    """Text and actions derived from the shared updater state."""

    text: str
    action: str | None
    badge: bool
    busy: bool


def check_button_view(state: UpdateState, version: str, auto_download: bool = True) -> CheckButtonView:
    """Describe the compact update popover without changing updater state."""
    target = "v" + (state.info or {}).get("version", "").removeprefix("v")
    current = "v" + version.removeprefix("v")
    percent = min(100, max(0, int(100 * state.progress / state.total))) if state.total else 0
    text = {
        "idle": "Check for a new version of Studio.",
        "checking": "Checking for updates…",
        "none": f"Up to date ({current})",
        "available": f"Update available: {target}\nCurrent version: {current}",
        "downloading": f"Downloading {target}… {percent}%",
        "ready": f"{target} is ready",
        "installing": "Installing and restarting…",
        "error": friendly_update_error(state.message),
        "unsupported": (f"Update available: {target}\nCurrent version: {current}"
                        if state.info else state.message),
    }[state.status]
    action = ("download" if state.status == "available" and not auto_download else
              "restart" if state.status == "ready" else
              "page" if state.status == "unsupported" and state.info else None)
    return CheckButtonView(text, action,
                           state.status in {"available", "ready"}
                           or (state.status == "unsupported" and bool(state.info)),
                           state.status in {"checking", "downloading"})


def parse_version(s: str) -> tuple[int, int, int, int, int]:
    """Compare X.Y.Z releases, beta and release candidates, optionally prefixed by v."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:(b|rc)(\d+))?", s.strip())
    if match is None:
        raise ValueError(f"Invalid version: {s!r}")
    major, minor, patch, stage, number = match.groups()
    return int(major), int(minor), int(patch), {"b": 0, "rc": 1, None: 2}[stage], int(number or 0)


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 according to the ordering of two versions."""
    left, right = parse_version(a), parse_version(b)
    return (left > right) - (left < right)


def select_release(releases: list[dict], channel: str) -> dict | None:
    """Select the newest supported version, ignoring drafts and malformed tags."""
    if channel not in CHANNELS:
        raise ValueError(f"Unknown update channel: {channel}")
    candidates = []
    for release in releases:
        if release.get("draft"):
            continue
        try:
            version = parse_version(release.get("tag_name", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if channel == "stable" and (release.get("prerelease") or version[3] < 2):
            continue
        candidates.append((version, release))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def platform_asset_name(version: str, system=None, machine=None) -> str:
    """Use the same platform labels as scripts.build_desktop.platform_label."""
    system = system or platform.system()
    machine = machine or platform.machine()
    system = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(system, system)
    machine = {"arm64": "arm64", "aarch64": "arm64", "AMD64": "x86_64", "x86_64": "x86_64"}.get(machine, machine)
    return f"UPbRPy-Studio-{version.removeprefix('v')}-{system}-{machine}.zip"


def find_asset(release: dict, name: str) -> dict | None:
    """Find an exact asset name in a GitHub release."""
    return next((asset for asset in release.get("assets", []) if asset.get("name") == name), None)


def parse_sha256sums(text: str) -> dict[str, str]:
    """Read sha256sum text (both binary and text markers), ignoring invalid lines."""
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *](.+)", line)
        if match:
            result[match[2]] = match[1].lower()
    return result


def installed_version() -> str:
    """Read installed package metadata, or the source checkout's pyproject.toml."""
    try:
        return metadata.version("UPbRPy")
    except metadata.PackageNotFoundError:
        project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        return tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]


def is_frozen() -> bool:
    """Whether this process is a packaged application."""
    return bool(getattr(sys, "frozen", False))


def install_root() -> Path | None:
    """Return the running macOS app bundle or Windows application directory."""
    if not is_frozen():
        return None
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        return next((path for path in executable.parents if path.suffix == ".app"), None)
    if sys.platform == "win32":
        return executable.parent
    return None


def can_replace(root) -> bool:
    """Replacement requires write access to the application and its parent."""
    return bool(root and Path(root).is_dir() and os.access(root, os.W_OK)
                and os.access(Path(root).parent, os.W_OK))


@dataclass(frozen=True)
class UpdateState:
    """Reducer value; info is the selected GitHub release with a version field."""

    status: str = "idle"
    info: dict | None = None
    progress: int = 0
    total: int = 0
    path: Path | None = None
    message: str = ""
    manual: bool = False


def transition(state: UpdateState, event) -> UpdateState:
    """Apply a string or (name, *arguments) event; illegal events are ignored.

    check_started accepts an optional manual boolean, found accepts a release,
    progress accepts bytes received and total, and downloaded accepts a Path.
    failed and unsupported accept a message.
    """
    name, *args = (event,) if isinstance(event, str) else event
    if name == "check_started":
        manual = bool(args and args[0])
        if state.status in {"checking", "downloading", "installing"}:
            return state
        if state.status == "ready" and not manual:
            return state
        return UpdateState("checking", manual=manual)
    if name == "unsupported" and state.status != "installing":
        return replace(state, status="unsupported", message=str(args[0]))
    if name == "no_update" and state.status == "checking":
        return UpdateState("none", manual=state.manual)
    if name == "found" and state.status == "checking":
        return UpdateState("available", info=args[0], manual=state.manual)
    if name == "download_started" and state.status == "available":
        return replace(state, status="downloading", progress=0, total=0)
    if name == "progress" and state.status == "downloading":
        return replace(state, progress=args[0], total=args[1])
    if name == "downloaded" and state.status == "downloading":
        return replace(state, status="ready", path=Path(args[0]))
    if name == "install_started" and state.status == "ready":
        return replace(state, status="installing")
    if name == "failed" and state.status in {"checking", "downloading", "installing"}:
        return replace(state, status="error", path=None, message=str(args[0]))
    return state
