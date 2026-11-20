"""Check the shared standards checkout without adopting or editing values."""

from dataclasses import dataclass
from importlib.resources import files
import os
import errno
import hashlib
import io
import json
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import subprocess
import tomllib
import urllib.error
from urllib.request import Request
import zipfile
import xml.etree.ElementTree as ET

from UPbRPy.standards import reference_library_info
from UPbRPy.tls import urlopen, is_certificate_error, certificate_error_message


STANDARDS_PAGE = "https://github.com/duserzym/upb-standards"
UPSTREAM_REF = "refs/upb-standards/checked"
COMMITS_FEED = "https://github.com/duserzym/upb-standards/commits/main.atom"
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MINERALS = {"apatite", "carbonate", "glass", "hematite", "rutile", "titanite", "zircon"}


@dataclass(frozen=True)
class StandardsCheck:
    message: str
    warning: bool = False
    digest: str = ""
    changed: tuple[str, ...] = ()
    details: str = ""
    snapshot_path: str = ""


def _read_url(url: str, *, accept: str, limit: int):
    request = Request(url, headers={"User-Agent": "UPbRPy-Studio-standards", "Accept": accept})
    with urlopen(request, timeout=20) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("GitHub standards response exceeds the size limit")
        return data, response.headers


def _snapshot_files(data: bytes, commit: str) -> dict[str, bytes]:
    """Validate the whole ZIP, selecting only reference TOML and Markdown files."""
    selected = {}
    seen = set()
    total = 0
    prefix = f"upb-standards-{commit}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > 4096:
            raise ValueError("Too many files in the standards ZIP")
        for member in members:
            path = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                    or ":" in member.filename or not path.parts or path.parts[0] != prefix
                    or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError(f"Unsafe path or file type in standards ZIP: {member.filename}")
            key = member.filename.rstrip("/").casefold()
            if key in seen:
                raise ValueError("Duplicate path in standards ZIP")
            seen.add(key)
            total += member.file_size
            if total > MAX_EXPANDED:
                raise ValueError("Expanded standards ZIP exceeds the size limit")
            if member.is_dir():
                continue
            relative = PurePosixPath(*path.parts[1:])
            name = relative.as_posix()
            expected = name in {"library.toml", "sources.toml"} or (
                len(relative.parts) == 2 and relative.parts[0] in MINERALS
                and relative.suffix == ".toml"
            ) or (relative.suffix == ".md" and not any(p.startswith(".") for p in relative.parts))
            # A repository ZIP also contains CI/configuration/license files.
            # They are never extracted or executed.
            if expected:
                selected[name] = archive.read(member)
                if relative.suffix == ".toml":
                    tomllib.loads(selected[name].decode("utf-8"))
    if not {"library.toml", "sources.toml"} <= selected.keys():
        raise ValueError("Standards ZIP is missing library.toml or sources.toml")
    manifest = tomllib.loads(selected["library.toml"].decode("utf-8"))
    materials = manifest.get("material_files")
    if (manifest.get("sources") != "sources.toml" or not isinstance(materials, list)
            or not all(isinstance(name, str) and name in selected
                       and len(PurePosixPath(name).parts) == 2
                       and PurePosixPath(name).parts[0] in MINERALS
                       and name.endswith(".toml") for name in materials)):
        raise ValueError("Standards manifest refers outside the data snapshot")
    return selected


def _app_data_snapshots() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return root / "UPbRPy/standards-snapshots"


def _install_snapshot(checkout: Path, commit: str, commit_date: str,
                      fetched_at: str, selected: dict[str, bytes]) -> Path:
    """Publish a complete snapshot with one rename; never replace existing values."""
    target = checkout
    if checkout.exists() or checkout.is_symlink():
        target = checkout.parent / (checkout.name + "-snapshots") / commit
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix=".standards-", dir=target.parent)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS) and not isinstance(exc, PermissionError):
            raise
        target = _app_data_snapshots() / commit
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix=".standards-", dir=target.parent)
    with scratch as temporary:
        stage = Path(temporary) / "snapshot"
        stage.mkdir()
        for name, data in selected.items():
            file = stage / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        metadata = [f"repository = {json.dumps(STANDARDS_PAGE)}",
                    f"commit = {json.dumps(commit)}", f"commit_date = {json.dumps(commit_date)}",
                    f"fetched_at = {json.dumps(fetched_at)}",
                    "files = " + json.dumps(sorted(selected)), "", "[sha256]"]
        metadata.extend(f"{json.dumps(name)} = {json.dumps(hashlib.sha256(data).hexdigest())}"
                        for name, data in sorted(selected.items()))
        (stage / "UPSTREAM.toml").write_text("\n".join(metadata) + "\n", encoding="utf-8")
        info = reference_library_info(stage, overlay=None)
        if target.exists() or target.is_symlink():
            if (target.is_symlink() or not (target / "UPSTREAM.toml").is_file()
                    or reference_library_info(target, overlay=None) != info):
                raise ValueError(f"Existing standards snapshot differs; left untouched: {target}")
        else:
            stage.rename(target)
    return target


def _existing_snapshot(checkout: Path, commit: str) -> Path | None:
    """Reuse a snapshot already fetched for this commit (no download per launch)."""
    for candidate in (checkout, checkout.parent / (checkout.name + "-snapshots") / commit,
                      _app_data_snapshots() / commit):
        metadata = candidate / "UPSTREAM.toml"
        try:
            if candidate.is_symlink() or not metadata.is_file():
                continue
            with metadata.open("rb") as stream:
                if tomllib.load(stream).get("commit") == commit:
                    reference_library_info(candidate, overlay=None)
                    return candidate
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            continue
    return None


def _check_snapshot(active_path, active_info: dict, checkout: Path) -> StandardsCheck:
    digest = active_info["sha256"]
    try:
        data, _ = _read_url(COMMITS_FEED, accept="application/atom+xml", limit=256 * 1024)
        entry = ET.fromstring(data).find("{http://www.w3.org/2005/Atom}entry")
        identifier = entry.findtext("{http://www.w3.org/2005/Atom}id", "") if entry is not None else ""
        commit = identifier.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("GitHub did not return a valid standards commit SHA")
        commit_date = entry.findtext("{http://www.w3.org/2005/Atom}updated", "")
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        snapshot = _existing_snapshot(checkout, commit)
        if snapshot is None:
            data, _ = _read_url(f"https://codeload.github.com/duserzym/upb-standards/zip/{commit}",
                                accept="application/zip", limit=MAX_DOWNLOAD)
            selected = _snapshot_files(data, commit)
            snapshot = _install_snapshot(checkout, commit, commit_date, fetched_at, selected)
        shared = reference_library_info(snapshot, overlay=None)
        # The same content digest used by the Git path ignores metadata and
        # documentation changes, so newer documentation alone is not a warning.
        differs = digest != shared["sha256"]
        status = ("Warning: Studio's configured library on disk differs from GitHub main."
                  if differs else "Standards match GitHub main.")
        return StandardsCheck(
            f"Fetched GitHub main {commit[:12]} on {fetched_at}. "
            + (f"Commit date: {commit_date}. " if commit_date else "") + status,
            differs, digest, snapshot_path=str(snapshot),
        )
    except Exception as exc:
        if is_certificate_error(exc):
            message = certificate_error_message("GitHub")
        elif isinstance(exc, urllib.error.HTTPError):
            excerpt = exc.read(240).decode("utf-8", errors="replace")
            message = f"GitHub standards check failed: HTTP {exc.code} {exc.reason}: {excerpt}"
        elif isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
            message = "GitHub standards check unavailable. Check your internet connection and try again."
        else:
            message = f"GitHub standards snapshot could not be checked: {exc}"
        return StandardsCheck(message, True, digest, details=str(exc))


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    if not (checkout / ".git").exists() or shutil.which("git") is None:
        return _check_snapshot(active_path, active_info, checkout)

    online = True
    details = ""
    try:
        _git(checkout, "fetch", "--quiet", "--no-tags", "--no-prune",
             "--no-write-fetch-head", "--no-auto-maintenance", "--refmap=", "origin",
             f"+refs/heads/main:{UPSTREAM_REF}")
        base = _git(checkout, "rev-parse", UPSTREAM_REF).strip()
        status = "Standards match GitHub main."
    except (OSError, subprocess.SubprocessError) as exc:
        online = False
        details = (getattr(exc, "stderr", "") or str(exc)).strip()
        try:
            base = _git(checkout, "rev-parse", "--verify", UPSTREAM_REF).strip()
        except (OSError, subprocess.SubprocessError):
            base = "HEAD"
        status = (certificate_error_message("GitHub") if is_certificate_error(details) else
                  "GitHub standards check unavailable. Check your connection and Git access.")

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
                          digest, tuple(sorted(changed)), details=details)
