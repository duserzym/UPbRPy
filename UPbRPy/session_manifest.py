"""Read the ``session_manifest.toml`` of a database session bundle.

A session bundle (``upblab/docs/WORKFLOW.md`` section 3.2) is a folder::

    <session_key>/
      raw/                    original Qtegra CSVs, byte-identical
      session_manifest.toml   this file
      <session_key>.upbr      a pre-filled project
      README.md

The manifest tells the reader how to split spot labels (``sep``), which label
prefixes are aliases of library names (``[samples]``), which reference
materials the session planned (``[reference_materials]``), and which database
records every acquisition belongs to (``[[acquisition]]``)::

    [session]
    id = "0192f0c4-…"          # session UUID
    key = "Apatite_20Mar25"
    sep = "_"
    material = "apatite"       # optional
    instrument = "iCAP TQ"
    method = "Apatite U-Pb"
    operator = "Y. Zhang"

    [samples]                  # alias = library or sample name
    AFB = "AFB1"

    [reference_materials]      # role = name(s); see ROLE_FIELDS
    primary_68 = "MADUCSB"
    concentration = ["NIST612"]
    secondary = ["OL2", "BR5"]

    [[acquisition]]
    label = "MADUCSB_122"
    acquisition_id = "…"
    spot_id = "…"
    atlas_spot_id = "…"
    grain_id = "…"
    sample_id = "…"
    sample_label = "MADUCSB"   # optional per-acquisition sample correction
    started_at = 2025-03-20T21:17:18
    sha256 = "…"
    kind = "primary_standard"
    filename = "Apatite_20Mar25_Zhang_9.csv"   # optional

``[reference_materials]`` may instead hold ``[[reference_materials.plan]]``
rows with ``rm_name``, ``role`` and ``planned_bracket_every``, the shape of
UPbDB's ``ReferenceMaterialPlan``. The module is plain Python: no Qt, no
HTTP, no database.

Version 2 adds optional ``[[laser_log]]`` and ``[[laser_setting]]`` tables
and acquisition keys prefixed with ``laser_`` (log hash, scan position,
on/off times, settings, stage coordinates and link method). Laser times are
normalised to UTC; adding the matching log's ``clock_offset_s`` places them
on the acquisition clock. Invalid laser values are dropped with warnings,
and unknown keys are ignored. Stage coordinates are read for display/QC and
excluded from exports unless opted in; export support is a later stage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
from pathlib import Path
import re

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


SESSION_MANIFEST_FILENAME = "session_manifest.toml"
RAW_DIRECTORY_NAME = "raw"
SESSION_BUNDLE_FORMATS = (
    "upbdb-session-bundle",
    "laicpmsdb-session-bundle",  # written before the 2026-11-15 rename
)

# Plan role -> PipelineConfig field. Scalar fields take the first name.
ROLE_FIELDS = {
    "primary": "primary_68_standard",
    "primary_68": "primary_68_standard",
    "primary_76": "primary_76_standard",
    "primary_82": "primary_82_standard",
    "concentration": "concentration_standards",
    "secondary": "secondary_standards",
    "validation": "secondary_standards",
}
_LIST_FIELDS = {"concentration_standards", "secondary_standards"}


class SessionManifestError(ValueError):
    """Raised when a session manifest cannot be read or is malformed."""


@dataclass(frozen=True)
class SessionLaserLog:
    """One laser log's identity, linking summary and clock correction."""

    id: str = ""
    file_name: str = ""
    sha256: str = ""
    system_type: str = ""
    serial_number: str = ""
    software_version: str = ""
    time_zone: str = ""
    laser_instrument: str = ""
    spot_key: str = ""
    has_laser_times: bool = False
    clock_offset_s: float | None = None
    clock_offset_spread_s: float | None = None
    offset_pairs: int = 0
    linked: int = 0


@dataclass(frozen=True)
class SessionLaserSetting:
    """One distinct ablation setting combination within a laser log."""

    log_sha256: str = ""
    fluence_j_cm2: float | None = None
    rep_rate_hz: float | None = None
    spot_um: float | None = None
    output_pct: float | None = None
    scan_speed_um_s: float | None = None
    spot_shape: str = ""
    shot_count: int | None = None
    acquisitions: int = 0


@dataclass(frozen=True)
class AcquisitionLaser:
    """Laser firing metadata linked to one acquisition; times are UTC."""

    log_sha256: str = ""
    scan_position: int | None = None
    laser_on_at: datetime | None = None
    laser_off_at: datetime | None = None
    fluence_j_cm2: float | None = None
    rep_rate_hz: float | None = None
    spot_um: float | None = None
    output_pct: float | None = None
    spot_shape: str = ""
    shot_count: int | None = None
    stage_um: tuple[float, float, float] | None = None
    link_method: str = ""

    @property
    def duration_s(self) -> float | None:
        """Return firing duration in seconds when both endpoints exist."""
        if self.laser_on_at is None or self.laser_off_at is None:
            return None
        return (self.laser_off_at - self.laser_on_at).total_seconds()


@dataclass(frozen=True)
class SessionAcquisition:
    """One ``[[acquisition]]`` row: a spot label and its database records."""

    label: str
    acquisition_id: str = ""
    spot_id: str = ""
    grain_id: str = ""
    sample_id: str = ""
    started_at: str = ""
    sha256: str = ""
    kind: str = ""
    filename: str = ""
    sample_label: str = ""
    atlas_spot_id: str = ""
    laser: AcquisitionLaser | None = None


@dataclass(frozen=True)
class SessionManifest:
    """Parsed contents of a ``session_manifest.toml``."""

    path: Path
    session_id: str = ""
    session_key: str = ""
    sep: str | None = None
    material: str = ""
    instrument: str = ""
    method: str = ""
    operator: str = ""
    sample_aliases: dict[str, str] = field(default_factory=dict)
    reference_materials: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    acquisitions: tuple[SessionAcquisition, ...] = ()
    format_version: int = 1
    laser_logs: tuple[SessionLaserLog, ...] = ()
    laser_settings: tuple[SessionLaserSetting, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def has_laser_data(self) -> bool:
        """Whether a log or acquisition carries laser metadata."""
        return bool(self.laser_logs) or any(
            row.laser is not None for row in self.acquisitions
        )

    def laser_log(self, sha256: str) -> SessionLaserLog | None:
        """Return the laser log with the given SHA-256, if listed."""
        if not sha256:
            return None
        for log in self.laser_logs:
            if log.sha256.casefold() == sha256.casefold():
                return log
        return None

    def acquisition_laser_window(
        self, label: str,
    ) -> tuple[float, float | None] | None:
        """Return firing endpoints in seconds after acquisition start.

        Laser times are shifted by the log's clock offset. ``started_at``
        is parsed from its stored ISO text; a naive start is treated as UTC.
        Missing or unparseable start/on times or clock offsets give None.
        """
        row = self.acquisition(label)
        if row is None or row.laser is None or row.laser.laser_on_at is None:
            return None
        log = self.laser_log(row.laser.log_sha256)
        if log is None or log.clock_offset_s is None:
            return None
        try:
            start = _utc_datetime(row.started_at)
        except (ValueError, OverflowError):
            return None
        on = (row.laser.laser_on_at - start).total_seconds() + log.clock_offset_s
        off = row.laser.laser_off_at
        return on, (
            None if off is None else (off - start).total_seconds() + log.clock_offset_s
        )

    @property
    def bundle_directory(self) -> Path:
        """The folder holding the manifest (the session bundle root)."""
        return self.path.parent

    @property
    def raw_directory(self) -> Path:
        """The ``raw/`` folder beside the manifest."""
        return self.bundle_directory / RAW_DIRECTORY_NAME

    def acquisition(self, label: str) -> SessionAcquisition | None:
        """Return the acquisition row for a spot label, if listed."""
        for row in self.acquisitions:
            if row.label == label:
                return row
        return None

    def standards_config(self) -> dict[str, object]:
        """Return PipelineConfig standard fields implied by the RM plan."""
        values: dict[str, object] = {}
        for role, names in self.reference_materials.items():
            target = ROLE_FIELDS.get(role)
            if target is None or not names:
                continue
            if target in _LIST_FIELDS:
                existing = list(values.get(target, ()))
                existing.extend(n for n in names if n not in existing)
                values[target] = tuple(existing)
            else:
                values.setdefault(target, names[0])
        return values

    def acquisition_samples(self) -> dict[str, str]:
        """Return spot labels mapped to non-empty per-acquisition samples."""
        return {
            row.label: row.sample_label
            for row in self.acquisitions
            if row.sample_label
        }


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def _laser_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _laser_sha256(value: object) -> str:
    text = _laser_text(value)
    if re.fullmatch(r"[0-9a-fA-F]{64}", text) is None:
        raise ValueError
    return text


def _laser_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _laser_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _laser_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError
    return value


def _utc_datetime(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _laser_stage(value: object) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError
    return (_laser_float(value[0]), _laser_float(value[1]), _laser_float(value[2]))


class _LaserRow:
    """Read optional laser values, keeping failures local to one key."""

    def __init__(self, row: Mapping, where: str, warnings: list[str]):
        self.row = row
        self.where = where
        self.warnings = warnings

    def value(self, key: str, parser: Callable, default=None):
        if key not in self.row:
            return default
        try:
            return parser(self.row[key])
        except (ValueError, TypeError, OverflowError):
            self.warnings.append(f"{self.where}: invalid {key}; ignored.")
            return default


def _laser_rows(document: Mapping, key: str, warnings: list[str]) -> list[_LaserRow]:
    if key not in document:
        return []
    rows = document[key]
    if not isinstance(rows, list):
        warnings.append(f"{key}: expected an array of tables; ignored.")
        return []
    result = []
    for index, row in enumerate(rows, start=1):
        where = f"{key} row {index}"
        if not isinstance(row, Mapping):
            warnings.append(f"{where}: expected a table; ignored.")
        else:
            result.append(_LaserRow(row, where, warnings))
    return result


def _laser_log(row: _LaserRow) -> SessionLaserLog:
    return SessionLaserLog(
        **{
            key: row.value(key, _laser_text, "")
            for key in (
                "id", "file_name", "system_type", "serial_number",
                "software_version", "time_zone", "laser_instrument", "spot_key",
            )
        },
        sha256=row.value("sha256", _laser_sha256, ""),
        has_laser_times=row.value("has_laser_times", _laser_bool, False),
        clock_offset_s=row.value("clock_offset_s", _laser_float),
        clock_offset_spread_s=row.value("clock_offset_spread_s", _laser_float),
        offset_pairs=row.value("offset_pairs", _laser_int, 0),
        linked=row.value("linked", _laser_int, 0),
    )


def _laser_setting(row: _LaserRow) -> SessionLaserSetting:
    return SessionLaserSetting(
        log_sha256=row.value("log_sha256", _laser_sha256, ""),
        **{
            key: row.value(key, _laser_float)
            for key in (
                "fluence_j_cm2", "rep_rate_hz", "spot_um", "output_pct",
                "scan_speed_um_s",
            )
        },
        spot_shape=row.value("spot_shape", _laser_text, ""),
        shot_count=row.value("shot_count", _laser_int),
        acquisitions=row.value("acquisitions", _laser_int, 0),
    )


def _acquisition_laser(row: _LaserRow) -> AcquisitionLaser | None:
    keys = {
        "laser_log_sha256", "laser_scan_position", "laser_on_at", "laser_off_at",
        "laser_fluence_j_cm2", "laser_rep_rate_hz", "laser_spot_um",
        "laser_output_pct", "laser_spot_shape", "laser_shot_count",
        "laser_stage_um", "laser_link_method",
    }
    if not keys.intersection(row.row):
        return None
    return AcquisitionLaser(
        log_sha256=row.value("laser_log_sha256", _laser_sha256, ""),
        scan_position=row.value("laser_scan_position", _laser_int),
        laser_on_at=row.value("laser_on_at", _utc_datetime),
        laser_off_at=row.value("laser_off_at", _utc_datetime),
        **{
            key: row.value(f"laser_{key}", _laser_float)
            for key in ("fluence_j_cm2", "rep_rate_hz", "spot_um", "output_pct")
        },
        spot_shape=row.value("laser_spot_shape", _laser_text, ""),
        shot_count=row.value("laser_shot_count", _laser_int),
        stage_um=row.value("laser_stage_um", _laser_stage),
        link_method=row.value("laser_link_method", _laser_text, ""),
    )


def _names(value: object, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(v.strip() for v in value if v.strip())
    raise SessionManifestError(
        f"{where} must be a name or a list of names."
    )


def _reference_materials(table: object) -> dict[str, tuple[str, ...]]:
    if table is None:
        return {}
    if not isinstance(table, Mapping):
        raise SessionManifestError("[reference_materials] must be a table.")
    plan: dict[str, list[str]] = {}

    def add(role: str, names: tuple[str, ...]) -> None:
        bucket = plan.setdefault(role, [])
        bucket.extend(name for name in names if name not in bucket)

    for key, value in table.items():
        if key == "plan":
            if not isinstance(value, list):
                raise SessionManifestError(
                    "[[reference_materials.plan]] must be an array of tables."
                )
            for row in value:
                if not isinstance(row, Mapping):
                    raise SessionManifestError(
                        "Each [[reference_materials.plan]] row must be a table."
                    )
                role = _text(row.get("role"))
                name = _text(row.get("rm_name"))
                if role and name:
                    add(role, (name,))
            continue
        if key == "library_version" or isinstance(value, (int, float, bool)):
            continue
        add(str(key), _names(value, f"[reference_materials] {key}"))
    return {role: tuple(names) for role, names in plan.items()}


def parse_session_manifest(
    document: Mapping[str, object],
    path: str | Path,
) -> SessionManifest:
    """Validate a parsed TOML document and return a :class:`SessionManifest`."""
    if "format" in document and document["format"] not in SESSION_BUNDLE_FORMATS:
        raise SessionManifestError(
            f"Unsupported session bundle format {document['format']}. "
            "Download the session again from UPbDB, or open its Qtegra export "
            "folder directly."
        )
    session = document.get("session", {})
    if not isinstance(session, Mapping):
        raise SessionManifestError("[session] must be a table.")
    sep = session.get("sep")
    if sep is not None and (not isinstance(sep, str) or sep == ""):
        raise SessionManifestError("[session] sep must be a non-empty string.")

    samples = document.get("samples", {})
    if not isinstance(samples, Mapping):
        raise SessionManifestError("[samples] must be a table.")
    aliases = {
        str(alias).strip(): str(target).strip()
        for alias, target in samples.items()
        if isinstance(target, str) and str(alias).strip() and target.strip()
    }

    rows = document.get("acquisition", [])
    if not isinstance(rows, list):
        raise SessionManifestError("[[acquisition]] must be an array of tables.")
    acquisitions = []
    warnings: list[str] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping) or not _text(row.get("label")):
            raise SessionManifestError(
                f"[[acquisition]] row {index} needs a label."
            )
        acquisitions.append(
            SessionAcquisition(
                **{
                    name: _text(row.get(name))
                    for name in (
                        "label", "acquisition_id", "spot_id", "grain_id",
                        "sample_id", "started_at", "sha256", "kind", "filename",
                        "sample_label", "atlas_spot_id",
                    )
                },
                laser=_acquisition_laser(_LaserRow(
                    row, f"acquisition row {index} ({_text(row.get('label'))})", warnings,
                )),
            )
        )

    laser_logs = tuple(
        _laser_log(row) for row in _laser_rows(document, "laser_log", warnings)
    )
    laser_settings = tuple(
        _laser_setting(row) for row in _laser_rows(document, "laser_setting", warnings)
    )
    format_version = _LaserRow(document, "manifest", warnings).value(
        "format_version", _laser_int, 1,
    )
    return SessionManifest(
        path=Path(path),
        session_id=_text(session.get("id")),
        session_key=_text(session.get("key")),
        sep=sep,
        material=_text(session.get("material")),
        instrument=_text(session.get("instrument")),
        method=_text(session.get("method")),
        operator=_text(session.get("operator")),
        sample_aliases=aliases,
        reference_materials=_reference_materials(
            document.get("reference_materials")
        ),
        acquisitions=tuple(acquisitions),
        format_version=format_version,
        laser_logs=laser_logs,
        laser_settings=laser_settings,
        warnings=tuple(warnings),
    )


def read_session_manifest(path: str | Path) -> SessionManifest:
    """Read and validate one ``session_manifest.toml`` file."""
    target = Path(path).expanduser()
    try:
        with target.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise SessionManifestError(
            f"{target.name} is not valid TOML: {exc}"
        ) from exc
    return parse_session_manifest(document, target)


def find_session_manifest(directory: str | Path) -> Path | None:
    """Return the manifest that describes a raw-data or bundle directory.

    ``directory`` may be the bundle root (manifest inside it) or the ``raw/``
    folder (manifest beside it, in the parent). Only a parent manifest that
    names this folder ``raw`` is accepted, so an unrelated manifest two levels
    up is never picked up.
    """
    folder = Path(directory).expanduser()
    inside = folder / SESSION_MANIFEST_FILENAME
    if inside.is_file():
        return inside
    beside = folder.parent / SESSION_MANIFEST_FILENAME
    if folder.name == RAW_DIRECTORY_NAME and beside.is_file():
        return beside
    return None


def raw_directory_for(directory: str | Path) -> Path:
    """Map a bundle root to its ``raw/`` folder; other paths pass through."""
    folder = Path(directory).expanduser()
    if (folder / SESSION_MANIFEST_FILENAME).is_file() and (
        folder / RAW_DIRECTORY_NAME
    ).is_dir():
        return folder / RAW_DIRECTORY_NAME
    return folder


__all__ = [
    "SESSION_MANIFEST_FILENAME",
    "RAW_DIRECTORY_NAME",
    "SESSION_BUNDLE_FORMATS",
    "ROLE_FIELDS",
    "SessionManifestError",
    "SessionLaserLog",
    "SessionLaserSetting",
    "AcquisitionLaser",
    "SessionAcquisition",
    "SessionManifest",
    "parse_session_manifest",
    "read_session_manifest",
    "find_session_manifest",
    "raw_directory_for",
]
