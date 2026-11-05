"""Read the ``session_manifest.toml`` of a database session bundle.

A session bundle (``upbstudio/docs/WORKFLOW.md`` section 3.2) is a folder::

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
    grain_id = "…"
    sample_id = "…"
    sample_label = "MADUCSB"   # optional per-acquisition sample correction
    started_at = 2025-03-20T21:17:18
    sha256 = "…"
    kind = "primary_standard"
    filename = "Apatite_20Mar25_Zhang_9.csv"   # optional

``[reference_materials]`` may instead hold ``[[reference_materials.plan]]``
rows with ``rm_name``, ``role`` and ``planned_bracket_every``, the shape of
laicpmsdb's ``ReferenceMaterialPlan``. The module is plain Python: no Qt, no
HTTP, no database.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


SESSION_MANIFEST_FILENAME = "session_manifest.toml"
RAW_DIRECTORY_NAME = "raw"

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
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping) or not _text(row.get("label")):
            raise SessionManifestError(
                f"[[acquisition]] row {index} needs a label."
            )
        acquisitions.append(
            SessionAcquisition(
                **{
                    name: _text(row.get(name))
                    for name in SessionAcquisition.__dataclass_fields__
                }
            )
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
    "ROLE_FIELDS",
    "SessionManifestError",
    "SessionAcquisition",
    "SessionManifest",
    "parse_session_manifest",
    "read_session_manifest",
    "find_session_manifest",
    "raw_directory_for",
]
