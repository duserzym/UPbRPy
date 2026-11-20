"""Unpack session-bundle zips into Studio's session cache.

A session bundle (``upblab/docs/WORKFLOW.md`` section 3.2) arrives either
from a UPbDB server (:mod:`UPbRPy.integration.upbdb`) or as a zip a
colleague sent by hand. Both routes end here, so the files on disk are the
same however the bundle came. No HTTP, no Qt.

The cache is one folder per session, ``~/UPbRPy/sessions/<session_key>/``.
Each folder records what it was unpacked from in ``.upbrpy_bundle.json``.
``export_sha256`` is the SHA-256 of ``session_manifest.toml`` (the server
writes the same value into ``origin.export_sha256`` and the
``X-Export-SHA256`` header); the manifest changes only when the session
does, so an equal hash means the cached folder is current and is kept as it
is, the user's saved project included. A different hash moves the old folder
aside (``<key>.superseded-<UTC time>``) rather than overwriting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile
import zlib

from .session_manifest import (
    RAW_DIRECTORY_NAME,
    SESSION_MANIFEST_FILENAME,
    SessionManifestError,
    read_session_manifest,
)


BUNDLE_RECORD_FILENAME = ".upbrpy_bundle.json"


class SessionBundleError(ValueError):
    """Raised when a zip is not a usable session bundle."""


@dataclass(frozen=True)
class SessionBundle:
    """A session bundle unpacked in the cache.

    The object is path-like (``os.fspath(bundle)`` is the folder).
    """

    path: Path
    session_key: str
    session_id: str
    export_sha256: str
    zip_sha256: str
    project_file: Path | None
    reused: bool = False
    superseded: Path | None = None

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    @property
    def raw_directory(self) -> Path:
        return self.path / RAW_DIRECTORY_NAME


@dataclass(frozen=True)
class RunProject:
    """A downloaded run project installed beside its session's raw files."""

    path: Path
    notes: tuple[str, ...]
    created: bool


def pick_session_run(runs: list[dict]) -> dict | None:
    """Choose a preferred complete run, otherwise the newest complete run."""
    complete = [run for run in runs if run.get("status") in {"completed", "complete"}]
    preferred = [run for run in complete if run.get("is_preferred_for_session")]

    def started(run):
        try:
            value = datetime.fromisoformat(str(run.get("started_at", "")))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError):
            return float("-inf")

    return max(preferred or complete, key=started, default=None)


def install_run_project(
    folder: str | Path, run_id: str, data: bytes, *, server: str,
    session_id: str, session_key: str, export_sha256: str,
) -> RunProject:
    """Validate and rebase a run project, preserving an existing local copy."""
    from .project import (
        resolve_project_path, validate_reduction_project, write_project_document,
    )
    from .pipeline import NewerFileError

    folder = Path(folder).expanduser().resolve()
    # Both identifiers become filename components, never paths supplied by a server.
    for value in (session_key, run_id):
        if not value or value in {".", ".."} or any(c in value for c in ("/", "\\", "\x00")):
            raise SessionBundleError("The run project has an invalid session key or run id.")
    target = folder / f"{session_key}.run-{run_id}.upbr"
    if target.exists():
        return RunProject(target, (), False)
    try:
        document = json.loads(data)
        if not isinstance(document, dict):
            raise ValueError("The project root must be a JSON object.")
        document = validate_reduction_project(document)
    except NewerFileError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        raise SessionBundleError(f"Invalid saved UPbRPy reduction project: {exc}") from exc
    notes = []
    paths = dict(document["paths"])
    paths["source_directory"] = {"relative": "raw", "absolute": str(folder / "raw")}
    reference = paths.get("reference_library")
    if reference is not None:
        try:
            resolve_project_path(target, reference, label="reference library")
        except (OSError, ValueError, TypeError, AttributeError):
            paths["reference_library"] = None
            notes.append("The run's reference library could not be found; using Studio's built-in library.")
    document["paths"] = paths
    origin = dict(document.get("origin") or {})
    if origin.get("export_sha256") and origin["export_sha256"] != export_sha256:
        notes.append("This run was reduced from an earlier export of the session and now points at the current raw files.")
    origin.update(server=server.rstrip("/"), session_id=session_id, session_key=session_key,
                  run_id=run_id, export_sha256=export_sha256)
    document["origin"] = origin
    document["format_version"] = max(2, int(document["format_version"]))
    try:
        write_project_document(document, target)
    except (ValueError, TypeError) as exc:
        raise SessionBundleError(f"Invalid saved UPbRPy reduction project: {exc}") from exc
    return RunProject(target, tuple(notes), True)


def holds_reduced_work(folder: str | Path) -> bool:
    """Whether a cached folder contains selections or a project linked to a run."""
    for project in Path(folder).glob("*.upbr"):
        try:
            document = json.loads(project.read_text(encoding="utf-8"))
            origin = document.get("origin") or {}
            if document.get("selections") or (isinstance(origin, dict) and origin.get("run_id")):
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def default_cache_root() -> Path:
    """``~/UPbRPy/sessions``, or ``$UPBRPY_SESSION_CACHE`` when set."""
    configured = os.environ.get("UPBRPY_SESSION_CACHE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "UPbRPy" / "sessions"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_members(archive: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    """Return the single top-level folder and the file members, or raise."""
    tops: set[str] = set()
    members = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if (
            not parts
            or name.startswith("/")
            or ":" in parts[0]
            or any(part in ("..", "") for part in parts)
        ):
            raise SessionBundleError(f"Unsafe path in bundle: {info.filename!r}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise SessionBundleError(f"Symbolic link in bundle: {info.filename!r}")
        if parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
            continue
        tops.add(parts[0])
        if not info.is_dir():
            if len(parts) < 2:
                raise SessionBundleError(
                    "A session bundle holds one folder, <session_key>/; "
                    f"found the loose file {name!r}."
                )
            members.append(info)
    if len(tops) != 1:
        raise SessionBundleError(
            "A session bundle holds exactly one folder, <session_key>/; "
            f"found {len(tops)}."
        )
    return tops.pop(), members


def _verify_unpacked(folder: Path) -> tuple[str, str, str]:
    """Check the manifest and raw hashes; return (key, id, export_sha256)."""
    manifest_path = folder / SESSION_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SessionBundleError(
            f"The bundle has no {SESSION_MANIFEST_FILENAME}."
        )
    if not (folder / RAW_DIRECTORY_NAME).is_dir():
        raise SessionBundleError("The bundle has no raw/ folder.")
    try:
        manifest = read_session_manifest(manifest_path)
    except SessionManifestError as exc:
        raise SessionBundleError(str(exc)) from exc
    verified: dict[str, str] = {}
    for row in manifest.acquisitions:
        if not (row.filename and row.sha256):
            continue
        expected = row.sha256.lower()
        if verified.get(row.filename) == expected:
            continue
        raw_file = folder / RAW_DIRECTORY_NAME / row.filename
        if not raw_file.is_file():
            raise SessionBundleError(
                f"raw/{row.filename} ({row.label}) is listed in the manifest "
                "but missing from the bundle."
            )
        actual = verified.get(row.filename) or _sha256_file(raw_file)
        if actual != expected:
            raise SessionBundleError(
                f"raw/{row.filename} ({row.label}) does not match the SHA-256 "
                "recorded in the manifest; the download may be damaged."
            )
        verified[row.filename] = actual
    return (
        manifest.session_key or folder.name,
        manifest.session_id,
        _sha256_file(manifest_path),
    )


def _update_project_origin(
    project_file: Path,
    *,
    server: str | None,
    session_id: str,
    session_key: str,
    export_sha256: str,
) -> None:
    """Fill the bundled project's origin with what this client knows.

    ``server`` is the URL Studio actually used (its keychain token belongs to
    that URL), so it replaces a server-side guess; other fields are filled
    only when missing.
    """
    try:
        document = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(document, dict):
        return
    from .pipeline import NewerFileError
    from .project import validate_reduction_project

    try:
        validate_reduction_project(document)
    except NewerFileError:
        # Preserve incompatible bundled files byte-for-byte. The project
        # opener presents the refusal after the bundle is available.
        return
    except (ValueError, TypeError):
        pass
    origin = document.get("origin")
    if not isinstance(origin, dict):
        if not server or not session_id:
            return
        origin = {}
    if server:
        origin["server"] = server.rstrip("/")
    for key, value in (
        ("session_id", session_id),
        ("session_key", session_key),
        ("export_sha256", export_sha256),
    ):
        if value and not origin.get(key):
            origin[key] = value
    document["origin"] = origin
    if int(document.get("format_version", 1) or 1) < 2:
        document["format_version"] = 2
    project_file.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def find_bundle_project(folder: Path, session_key: str) -> Path | None:
    """The bundle's ``<session_key>.upbr``, else its first ``.upbr``."""
    preferred = folder / f"{session_key}.upbr"
    if preferred.is_file():
        return preferred
    found = sorted(folder.glob("*.upbr"))
    return found[0] if found else None


def read_bundle_record(folder: str | Path) -> dict:
    """Return the ``.upbrpy_bundle.json`` of a cached bundle, or ``{}``."""
    try:
        record = json.loads(
            (Path(folder) / BUNDLE_RECORD_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def unpack_session_bundle(
    source: str | Path | bytes,
    cache_root: str | Path | None = None,
    *,
    server: str | None = None,
    expected_export_sha256: str | None = None,
    zip_sha256: str | None = None,
) -> SessionBundle:
    """Verify a session-bundle zip and unpack it to ``cache_root/<key>/``.

    ``source`` is a zip path or the zip bytes. ``expected_export_sha256``
    (the server's ``X-Export-SHA256``) is checked against the manifest.
    A streaming caller may supply the already-computed zip_sha256.
    """
    archive_source = io.BytesIO(source) if isinstance(source, bytes) else Path(source)
    zip_sha256 = zip_sha256 or (sha256_bytes(source) if isinstance(source, bytes)
                                else _sha256_file(Path(source)))
    root = Path(cache_root).expanduser() if cache_root else default_cache_root()
    try:
        archive = zipfile.ZipFile(archive_source)
    except zipfile.BadZipFile as exc:
        raise SessionBundleError("The download is not a zip file.") from exc
    with archive:
        top, members = _safe_members(archive)
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=root))
        try:
            for info in members:
                parts = PurePosixPath(info.filename.replace("\\", "/")).parts
                target = staging.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with archive.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
                except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
                    raise SessionBundleError(f"The zip is damaged at {info.filename!r}.") from exc
            unpacked = staging / top
            session_key, session_id, export_sha256 = _verify_unpacked(unpacked)
            if (
                expected_export_sha256
                and expected_export_sha256.lower() != export_sha256
            ):
                raise SessionBundleError(
                    "The session manifest does not match the hash the server "
                    "announced (X-Export-SHA256); try the download again."
                )
            return _install(
                unpacked,
                root / top,
                server=server,
                session_key=session_key,
                session_id=session_id,
                export_sha256=export_sha256,
                zip_sha256=zip_sha256,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _install(
    unpacked: Path,
    destination: Path,
    *,
    server: str | None,
    session_key: str,
    session_id: str,
    export_sha256: str,
    zip_sha256: str,
) -> SessionBundle:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    record = {
        "session_id": session_id,
        "session_key": session_key,
        "server": (server or "").rstrip("/"),
        "export_sha256": export_sha256,
        "zip_sha256": zip_sha256,
        "fetched_at": now.isoformat().replace("+00:00", "Z"),
    }
    superseded: Path | None = None
    if destination.exists():
        previous = read_bundle_record(destination)
        intact = False
        if previous.get("export_sha256") == export_sha256:
            try:
                intact = _verify_unpacked(destination)[2] == export_sha256
            except (OSError, ValueError):
                pass
        if intact:
            previous.update(
                fetched_at=record["fetched_at"],
                server=record["server"] or previous.get("server", ""),
            )
            (destination / BUNDLE_RECORD_FILENAME).write_text(
                json.dumps(previous, indent=2) + "\n", encoding="utf-8"
            )
            return SessionBundle(
                path=destination,
                session_key=session_key,
                session_id=session_id,
                export_sha256=export_sha256,
                zip_sha256=str(previous.get("zip_sha256", zip_sha256)),
                project_file=find_bundle_project(destination, session_key),
                reused=True,
            )
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        superseded = destination.with_name(
            f"{destination.name}.superseded-{stamp}"
        )
        destination.rename(superseded)
    project = find_bundle_project(unpacked, session_key)
    if project is not None:
        _update_project_origin(
            project,
            server=server,
            session_id=session_id,
            session_key=session_key,
            export_sha256=export_sha256,
        )
    (unpacked / BUNDLE_RECORD_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    shutil.move(os.fspath(unpacked), os.fspath(destination))
    return SessionBundle(
        path=destination,
        session_key=session_key,
        session_id=session_id,
        export_sha256=export_sha256,
        zip_sha256=zip_sha256,
        project_file=find_bundle_project(destination, session_key),
        superseded=superseded,
    )


def find_cached_session(
    session_id: str,
    cache_root: str | Path | None = None,
    *, server: str | None = None,
) -> Path | None:
    """Find an active bundle by its small record, optionally scoped to a server."""
    root = Path(cache_root).expanduser() if cache_root else default_cache_root()
    if not root.is_dir():
        return None
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and ".superseded-" not in folder.name:
            record = read_bundle_record(folder)
            if (record.get("session_id") == session_id
                    and (server is None or record.get("server", "").rstrip("/") == server.rstrip("/"))):
                return folder
    return None


def find_project_for_run(
    run_id: str,
    cache_root: str | Path | None = None,
) -> Path | None:
    """Return a cached ``.upbr`` whose ``origin.run_id`` is ``run_id``."""
    root = Path(cache_root).expanduser() if cache_root else default_cache_root()
    if not root.is_dir():
        return None
    for project in sorted(root.glob("*/*.upbr"),
                          key=lambda path: (".superseded-" in path.parent.name, path)):
        try:
            origin = json.loads(project.read_text(encoding="utf-8")).get("origin")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(origin, dict) and origin.get("run_id") == run_id:
            return project
    return None


__all__ = [
    "BUNDLE_RECORD_FILENAME",
    "SessionBundleError",
    "SessionBundle",
    "RunProject",
    "pick_session_run",
    "install_run_project",
    "holds_reduced_work",
    "default_cache_root",
    "read_bundle_record",
    "unpack_session_bundle",
    "find_bundle_project",
    "find_cached_session",
    "find_project_for_run",
    "sha256_bytes",
]
