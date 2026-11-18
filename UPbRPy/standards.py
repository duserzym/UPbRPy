"""Source-traceable reference-material measurements and derived archives.

The bundled TOML data is a pinned snapshot of duserzym/upb-standards.
This module validates source documents and constructs a wide calibration
view, deriving reciprocal and linked ratios only when no direct value exists.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import os
import warnings

import numpy as np
import pandas as pd

try:  # Python 3.11+; UPbRPy's declared runtime is Python 3.12+.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility for old dev envs
    import tomli as tomllib

from .constants import common_U238_U235, lambda_235, lambda_238


# Decay constant used by the source workbooks for concordant Th-Pb ratios.
# It is kept local because the current numerical reduction does not otherwise
# perform Th-Pb age calculations.
LAMBDA_232 = 4.94752e-11
ARCHIVE_FORMAT_VERSION = 2
REFERENCE_MATERIALS_DIRECTORY = "reference_materials"
MANIFEST_FILENAME = "library.toml"
_AUTO_OVERLAY = object()
_MERGED_MARKER = ".upbrpy-merged-library"
_MERGED_MARKER_TEXT = "UPbRPy merged reference library, format 2\n"
INTERNAL_REFERENCE_LABEL = "internal, unpublished laboratory reference value"


def default_lab_overlay_path() -> Path | None:
    """Resolve the private lab library, or None when absent or disabled."""
    configured = os.environ.get("UPBRPY_LAB_STANDARDS")
    if configured is not None:
        if configured.strip().casefold() in {"", "none"}:
            return None
        return Path(configured).expanduser()
    candidate = Path.home() / "UPbRPy" / "lab-standards"
    return candidate if (candidate / MANIFEST_FILENAME).is_file() else None


def _library_root(path):
    if path is None:
        return files(__package__) / REFERENCE_MATERIALS_DIRECTORY
    path = Path(path).expanduser()
    return path.parent if path.name == MANIFEST_FILENAME else path


def _standard_key(value):
    """Use the same typography-insensitive key for aliases and overlay clashes."""
    return "".join(character for character in str(value).casefold() if character.isalnum())


REQUIRED_COLUMNS = (
    "standard",
    "aliases",
    "material",
    "quantity",
    "value",
    "unit",
    "uncertainty",
    "uncertainty_type",
    "role",
    "status",
    "source",
    "source_locator",
    "notes",
)

VALID_ROLES = {
    "concentration",
    "delta_value",
    "epsilon_value",
    "initial_ratio",
    "isotope_ratio",
    "radiogenic_ratio",
    "total_ratio",
    "reference_age",
}

VALID_STATUSES = {"recommended", "provisional", "internal_unpublished"}
VALID_UNCERTAINTY_TYPES = {
    "",
    "1sd",
    "2sd",
    "expanded_95",
    "reported_unspecified",
}
ROLE_UNITS = {
    "concentration": "ug/g",
    "delta_value": "permil",
    "epsilon_value": "dimensionless",
    "initial_ratio": "ratio",
    "isotope_ratio": "ratio",
    "radiogenic_ratio": "ratio",
    "total_ratio": "ratio",
    "reference_age": "Ma",
}

_MATERIAL_KEYS = {"format_version", "material", "standards"}
_STANDARD_KEYS = {"aliases", "description", "origin", "measurements"}
_MEASUREMENT_KEYS = {
    "role",
    "unit",
    "status",
    "source",
    "locator",
    "notes",
    "values",
}
_VALUE_KEYS = {
    "value",
    "uncertainty",
    "uncertainty_type",
    "status",
    "source",
    "locator",
    "notes",
}
_SOURCE_KEYS = {
    "display", "kind", "doi", "no_doi_reason", "status", "file", "url", "notes"
}


def _read_toml(resource) -> dict:
    """Read TOML from a filesystem path or importlib resource."""
    with resource.open("rb") as stream:
        return tomllib.load(stream)


def _resolve_resource_case_insensitive(root, relative_path: str):
    """Resolve a manifest path without depending on filesystem case rules.

    Exact spelling is preferred. If it does not exist, each path component is
    matched by :meth:`str.casefold`. Ambiguous case-only duplicates are
    rejected instead of selecting a platform-dependent file.
    """
    resource = root
    for component in Path(relative_path).parts:
        exact = resource / component
        if exact.is_file() or exact.is_dir():
            resource = exact
            continue

        matches = [
            child
            for child in resource.iterdir()
            if child.name.casefold() == component.casefold()
        ]
        if not matches:
            raise FileNotFoundError(
                f"Reference-material resource {relative_path!r} was not found."
            )
        if len(matches) > 1:
            names = ", ".join(sorted(child.name for child in matches))
            raise ValueError(
                f"Reference-material path {relative_path!r} is ambiguous: {names}."
            )
        resource = matches[0]
    return resource


def _reject_unknown_keys(mapping: dict, allowed: set[str], context: str) -> None:
    """Raise a focused error for misspelled or unsupported TOML fields."""
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(
            f"{context} contains unsupported fields: " + ", ".join(unknown)
        )


def _validate_format_version(document: dict, context: str) -> None:
    """Require the current archive format version in every TOML document."""
    if document.get("format_version") != ARCHIVE_FORMAT_VERSION:
        raise ValueError(
            f"{context} must declare format_version = {ARCHIVE_FORMAT_VERSION}."
        )


def _load_source_registry(root, filename: str) -> dict[str, dict]:
    """Load and validate publication metadata shared by material records."""
    document = _read_toml(_resolve_resource_case_insensitive(root, filename))
    _validate_format_version(document, filename)
    _reject_unknown_keys(document, {"format_version", "sources"}, filename)
    registry = document.get("sources")
    if not isinstance(registry, dict) or not registry:
        raise ValueError(f"{filename} must contain a non-empty [sources] table.")

    displays = []
    for source_id, metadata in registry.items():
        if not isinstance(metadata, dict):
            raise ValueError(f"Source {source_id!r} must be a TOML table.")
        _reject_unknown_keys(metadata, _SOURCE_KEYS, f"Source {source_id!r}")
        display = metadata.get("display")
        if not isinstance(display, str) or not display.strip():
            raise ValueError(f"Source {source_id!r} requires a display citation.")
        kind = metadata.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"Source {source_id!r} requires a kind.")
        for field in ("doi", "no_doi_reason"):
            if field in metadata and (
                not isinstance(metadata[field], str) or not metadata[field].strip()
            ):
                raise ValueError(f"Source {source_id!r} {field} must be nonblank text.")
        if "status" in metadata and (
            not isinstance(metadata["status"], str)
            or metadata["status"] not in VALID_STATUSES
        ):
            raise ValueError(f"Source {source_id!r} has an unsupported status.")
        displays.append(display.strip())
    if len(displays) != len(set(displays)):
        raise ValueError("Source display citations must be unique.")
    return registry


def check_reference_sources(path: str | Path | None = None) -> list[str]:
    """Audit source references in a TOML library directory or manifest.

    Raise ValueError for malformed source metadata or unknown measurement
    source IDs, reporting all unresolved measurements together. Return the
    sorted IDs lacking both a DOI and a no_doi_reason, and emit a UserWarning
    when that list is nonempty. The default is the packaged library. This
    audit does not look up DOIs or validate measurement values.
    """
    if path is None:
        root = files(__package__) / REFERENCE_MATERIALS_DIRECTORY
    else:
        source_path = Path(path).expanduser()
        root = source_path.parent if source_path.name == MANIFEST_FILENAME else source_path
    manifest = _read_toml(_resolve_resource_case_insensitive(root, MANIFEST_FILENAME))
    _validate_format_version(manifest, MANIFEST_FILENAME)
    sources = _load_source_registry(root, manifest["sources"])
    unknown = []
    for filename in manifest["material_files"]:
        document = _read_toml(_resolve_resource_case_insensitive(root, filename))
        for standard, definition in document.get("standards", {}).items():
            for group in definition.get("measurements", []):
                for quantity, value in group.get("values", {}).items():
                    source_id = (
                        value.get("source", group.get("source"))
                        if isinstance(value, dict) else group.get("source")
                    )
                    if source_id not in sources:
                        unknown.append(
                            f"{filename}: {standard}/{quantity} -> {source_id!r}"
                        )
    if unknown:
        raise ValueError("Measurements cite unknown source IDs:\n" + "\n".join(unknown))
    missing = sorted(
        source_id for source_id, metadata in sources.items()
        if not metadata.get("doi") and not metadata.get("no_doi_reason")
    )
    if missing:
        warnings.warn(
            "Sources without doi or no_doi_reason: " + ", ".join(missing),
            UserWarning,
            stacklevel=2,
        )
    return missing


def _measurement_record(
    *,
    standard: str,
    aliases: list[str],
    material: str,
    quantity: str,
    group: dict,
    raw_value,
    sources: dict[str, dict],
) -> dict:
    """Expand one compact TOML value into one normalized measurement row."""
    if isinstance(raw_value, bool):
        raise ValueError(f"{standard}/{quantity} must have a numeric value.")
    if isinstance(raw_value, (int, float)):
        value_metadata = {"value": raw_value}
    elif isinstance(raw_value, dict):
        _reject_unknown_keys(
            raw_value,
            _VALUE_KEYS,
            f"Value {standard}/{quantity}",
        )
        value_metadata = raw_value
    else:
        raise ValueError(f"{standard}/{quantity} must be numeric or a TOML table.")

    if "value" not in value_metadata:
        raise ValueError(f"{standard}/{quantity} is missing its numeric value.")
    value = value_metadata["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{standard}/{quantity} must have a numeric value.")

    def resolved(field: str, default=""):
        """Resolve value-level metadata before falling back to its group."""
        return value_metadata.get(field, group.get(field, default))

    source_id = resolved("source")
    if source_id not in sources:
        raise ValueError(
            f"{standard}/{quantity} refers to unknown source {source_id!r}."
        )
    notes = str(resolved("notes")).replace("{quantity}", quantity)
    uncertainty = value_metadata.get("uncertainty", "")
    return {
        "standard": standard,
        "aliases": ";".join(aliases),
        "material": material,
        "quantity": quantity,
        "value": value,
        "unit": resolved("unit"),
        "uncertainty": uncertainty,
        "uncertainty_type": value_metadata.get("uncertainty_type", ""),
        "role": resolved("role"),
        "status": resolved("status"),
        "source": sources[source_id]["display"],
        "source_locator": resolved("locator"),
        "notes": notes,
    }


def _load_toml_measurements(root, *, is_overlay=False) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Expand the manifest-driven TOML library into normalized rows."""
    manifest_resource = _resolve_resource_case_insensitive(root, MANIFEST_FILENAME)
    manifest = _read_toml(manifest_resource)
    _validate_format_version(manifest, MANIFEST_FILENAME)
    allowed_manifest_keys = {
        "format_version",
        "version",
        "sources",
        "material_files",
    }
    if is_overlay:
        allowed_manifest_keys |= {"lab", "replaces"}
        if not isinstance(manifest.get("lab", ""), str):
            raise ValueError("Overlay lab must be a string.")
        replaces = manifest.get("replaces", [])
        if not isinstance(replaces, list) or not all(
            isinstance(name, str) and name.strip() for name in replaces
        ):
            raise ValueError("Overlay replaces must be a list of standard names.")
    _reject_unknown_keys(manifest, allowed_manifest_keys, MANIFEST_FILENAME)
    library_version = manifest.get("version")
    if library_version is not None and not isinstance(library_version, str):
        raise ValueError(f"{MANIFEST_FILENAME} version must be a string.")

    source_filename = manifest.get("sources")
    material_files = manifest.get("material_files")
    if not isinstance(source_filename, str) or not source_filename:
        raise ValueError(f"{MANIFEST_FILENAME} requires a sources filename.")
    if not isinstance(material_files, list) or not material_files:
        raise ValueError(f"{MANIFEST_FILENAME} requires material_files.")
    sources = _load_source_registry(root, source_filename)

    records = []
    seen_standards = set()
    for filename in material_files:
        if not isinstance(filename, str) or not filename.lower().endswith(".toml"):
            raise ValueError("Every material_files entry must be a TOML filename.")
        document = _read_toml(
            _resolve_resource_case_insensitive(root, filename)
        )
        _validate_format_version(document, filename)
        _reject_unknown_keys(document, _MATERIAL_KEYS, filename)

        material = document.get("material")
        standards = document.get("standards")
        if not isinstance(material, str) or not material:
            raise ValueError(f"{filename} requires a material name.")
        if not isinstance(standards, dict) or not standards:
            raise ValueError(f"{filename} requires a non-empty [standards] table.")

        for standard, definition in standards.items():
            if standard in seen_standards:
                raise ValueError(
                    f"Standard {standard!r} is defined in multiple TOML files."
                )
            seen_standards.add(standard)
            if not isinstance(definition, dict):
                raise ValueError(f"Standard {standard!r} must be a TOML table.")
            _reject_unknown_keys(
                definition,
                _STANDARD_KEYS,
                f"Standard {standard!r}",
            )
            aliases = definition.get("aliases", [])
            description = definition.get("description", "")
            origin = definition.get("origin", "")
            groups = definition.get("measurements")
            if not isinstance(aliases, list) or not all(
                isinstance(alias, str) and alias.strip() for alias in aliases
            ):
                raise ValueError(f"Standard {standard!r} aliases must be strings.")
            if not isinstance(description, str):
                raise ValueError(f"Standard {standard!r} description must be text.")
            if not isinstance(origin, str):
                raise ValueError(f"Standard {standard!r} origin must be text.")
            if not isinstance(groups, list) or not groups:
                raise ValueError(f"Standard {standard!r} requires measurements.")

            for group in groups:
                if not isinstance(group, dict):
                    raise ValueError(
                        f"Standard {standard!r} measurements must be TOML tables."
                    )
                _reject_unknown_keys(
                    group,
                    _MEASUREMENT_KEYS,
                    f"Measurement group for {standard!r}",
                )
                required_group_fields = {"role", "unit", "status", "source", "values"}
                missing = sorted(required_group_fields - set(group))
                if missing:
                    raise ValueError(
                        f"Measurement group for {standard!r} is missing: "
                        + ", ".join(missing)
                    )
                values = group["values"]
                if not isinstance(values, dict) or not values:
                    raise ValueError(
                        f"Measurement group for {standard!r} requires values."
                    )
                for quantity, raw_value in values.items():
                    records.append(
                        _measurement_record(
                            standard=standard,
                            aliases=aliases,
                            material=material,
                            quantity=quantity,
                            group=group,
                            raw_value=raw_value,
                            sources=sources,
                        )
                    )

    measurements = pd.DataFrame.from_records(records, columns=REQUIRED_COLUMNS)
    return measurements, sources


def _load_csv_measurements(path) -> pd.DataFrame:
    """Load the former long-form CSV representation for explicit migrations."""
    return pd.read_csv(path, keep_default_na=False)


def _base_reference_library_info(path: str | Path | None = None) -> dict[str, str]:
    """Identify a reference-material library for provenance records.

    Returns ``version``, a human-readable string, and ``sha256``, a digest of
    ``library.toml``, the source registry and every material file in manifest
    order with line endings normalized to LF (or of the file itself for a
    legacy CSV). ``version`` is the
    manifest's optional ``version`` key, else ``format-<n>``, always followed
    by ``+sha256.<first 12 hex digits>`` so two edited copies of the same
    nominal version stay distinguishable. Snapshots also return ``repository``,
    ``revision`` (the upstream commit) and ``commit_date`` from UPSTREAM.toml.
    """
    import hashlib

    digest = hashlib.sha256()
    if path is not None and Path(path).suffix.lower() == ".csv":
        digest.update(Path(path).expanduser().read_bytes())
        label = "legacy-csv"
    else:
        if path is None:
            root = files(__package__) / REFERENCE_MATERIALS_DIRECTORY
        else:
            source_path = Path(path).expanduser()
            root = (
                source_path.parent
                if source_path.name == MANIFEST_FILENAME
                else source_path
            )
        manifest_resource = _resolve_resource_case_insensitive(
            root, MANIFEST_FILENAME
        )
        manifest = _read_toml(manifest_resource)
        names = [MANIFEST_FILENAME]
        if isinstance(manifest.get("sources"), str):
            names.append(manifest["sources"])
        names.extend(
            str(name) for name in manifest.get("material_files", ()) or ()
        )
        for name in names:
            resource = (
                manifest_resource
                if name == MANIFEST_FILENAME
                else _resolve_resource_case_insensitive(root, name)
            )
            digest.update(name.encode("utf-8") + b"\0")
            # Text is hashed with LF line endings so a Windows checkout
            # (core.autocrlf) reports the same library as macOS.
            digest.update(resource.read_bytes().replace(b"\r\n", b"\n"))
            digest.update(b"\0")
        declared = manifest.get("version")
        label = (
            str(declared)
            if isinstance(declared, str) and declared
            else f"format-{manifest.get('format_version', '?')}"
        )
    hexdigest = digest.hexdigest()
    info = {"version": f"{label}+sha256.{hexdigest[:12]}", "sha256": hexdigest}
    if label != "legacy-csv" and (root / "UPSTREAM.toml").is_file():
        upstream = _read_toml(root / "UPSTREAM.toml")
        info["repository"] = upstream["repository"]
        info["revision"] = upstream["commit"]
        info["commit_date"] = upstream["commit_date"]
    return info


def _load_standard_measurements(path: str | Path | None = None, *, is_overlay=False) -> pd.DataFrame:
    """Load and validate normalized reference-material measurements.

    Parameters
    ----------
    path : path-like, optional
        Alternative TOML library directory, ``library.toml`` manifest, or
        former long-form CSV file. By default, the packaged
        ``reference_materials`` TOML directory is loaded.

    Returns
    -------
    pandas.DataFrame
        One row per source value. Numeric fields are converted to floating
        point while textual fields retain empty strings rather than ``NaN``.

    Notes
    -----
    ``uncertainty`` is intentionally separate from ``value`` and must be read
    together with ``uncertainty_type`` (for example ``1sd``, ``2sd``, or
    ``expanded_95``). Missing uncertainties remain ``NaN``; they are not
    silently interpreted as zero.
    """
    sources = {}
    if path is None:
        source_root = files(__package__) / REFERENCE_MATERIALS_DIRECTORY
        measurements, sources = _load_toml_measurements(source_root, is_overlay=is_overlay)
    else:
        source_path = Path(path).expanduser()
        if source_path.suffix.lower() == ".csv":
            measurements = _load_csv_measurements(source_path)
        else:
            source_root = (
                source_path.parent
                if source_path.name == MANIFEST_FILENAME
                else source_path
            )
            measurements, sources = _load_toml_measurements(source_root, is_overlay=is_overlay)

    missing_columns = set(REQUIRED_COLUMNS) - set(measurements.columns)
    if missing_columns:
        raise ValueError(
            "standards v2 file is missing required columns: "
            + ", ".join(sorted(missing_columns))
        )

    measurements = measurements.loc[:, REQUIRED_COLUMNS].copy()
    for column in REQUIRED_COLUMNS:
        if column not in {"value", "uncertainty"}:
            measurements[column] = measurements[column].astype(str).str.strip()

    measurements["value"] = pd.to_numeric(measurements["value"], errors="coerce")
    measurements["uncertainty"] = pd.to_numeric(
        measurements["uncertainty"], errors="coerce"
    )

    required_text = (
        "standard",
        "material",
        "quantity",
        "unit",
        "role",
        "status",
        "source",
    )
    empty_text = [
        column for column in required_text if measurements[column].eq("").any()
    ]
    if empty_text:
        raise ValueError(
            "standards v2 contains empty required values in: "
            + ", ".join(empty_text)
        )
    if measurements["value"].isna().any():
        bad_rows = measurements.index[measurements["value"].isna()].tolist()
        raise ValueError(f"standards v2 contains nonnumeric values in rows {bad_rows}.")

    invalid_roles = sorted(set(measurements["role"]) - VALID_ROLES)
    if invalid_roles:
        raise ValueError(
            "standards v2 contains unsupported roles: " + ", ".join(invalid_roles)
        )

    invalid_statuses = sorted(set(measurements["status"]) - VALID_STATUSES)
    if invalid_statuses:
        raise ValueError(
            "standards v2 contains unsupported statuses: "
            + ", ".join(invalid_statuses)
        )

    invalid_uncertainty_types = sorted(
        set(measurements["uncertainty_type"]) - VALID_UNCERTAINTY_TYPES
    )
    if invalid_uncertainty_types:
        raise ValueError(
            "standards v2 contains unsupported uncertainty types: "
            + ", ".join(invalid_uncertainty_types)
        )

    missing_uncertainty_type = measurements["uncertainty"].notna() & measurements[
        "uncertainty_type"
    ].eq("")
    orphan_uncertainty_type = measurements["uncertainty"].isna() & measurements[
        "uncertainty_type"
    ].ne("")
    if missing_uncertainty_type.any() or orphan_uncertainty_type.any():
        raise ValueError(
            "standards v2 uncertainty and uncertainty_type must either both be "
            "provided or both be empty."
        )

    expected_units = measurements["role"].map(ROLE_UNITS)
    wrong_units = measurements["unit"].ne(expected_units)
    if wrong_units.any():
        bad_pairs = sorted(
            {
                f"{role}/{unit}"
                for role, unit in measurements.loc[
                    wrong_units, ["role", "unit"]
                ].itertuples(index=False, name=None)
            }
        )
        raise ValueError(
            "standards v2 contains units inconsistent with their roles: "
            + ", ".join(bad_pairs)
        )

    duplicate_mask = measurements.duplicated(
        subset=["standard", "quantity"], keep=False
    )
    if duplicate_mask.any():
        duplicates = measurements.loc[
            duplicate_mask, ["standard", "quantity"]
        ].drop_duplicates()
        labels = [f"{row.standard}/{row.quantity}" for row in duplicates.itertuples()]
        raise ValueError(
            "standards v2 contains duplicate standard/quantity rows: "
            + ", ".join(labels)
        )

    mixed_materials = measurements.groupby("standard")["material"].nunique()
    mixed_materials = mixed_materials[mixed_materials > 1].index.tolist()
    if mixed_materials:
        raise ValueError(
            "standards v2 assigns multiple materials to: "
            + ", ".join(mixed_materials)
        )

    measurements.attrs["sources"] = sources
    return measurements


def _overlay_path(overlay):
    return default_lab_overlay_path() if overlay is _AUTO_OVERLAY else overlay


def _merged_library_marker(path):
    """Read writer metadata, accepting the original text marker without it."""
    if path is not None and Path(path).suffix.lower() == ".csv":
        return None
    marker = _library_root(path) / _MERGED_MARKER
    if not marker.is_file():
        return None
    text = marker.read_text(encoding="utf-8")
    if text == _MERGED_MARKER_TEXT:
        return {}
    try:
        metadata = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid merged-library marker: {exc}") from exc
    fields = {"format", "lab", "sha256", "standards", "internal_unpublished"}
    if (set(metadata) != fields or type(metadata.get("format")) is not int
            or metadata["format"] != 1
            or not isinstance(metadata.get("lab"), str)
            or not isinstance(metadata.get("sha256"), str)
            or (metadata["sha256"] and (len(metadata["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in metadata["sha256"])))
            or any(not isinstance(metadata.get(key), list)
                   or not all(isinstance(name, str) and name.strip() for name in metadata[key])
                   for key in ("standards", "internal_unpublished"))):
        raise ValueError("Invalid merged-library marker metadata.")
    return metadata


def _with_overlay_info(info, overlay):
    info = dict(info)
    if overlay:
        info["version"] += f"+overlay.{overlay['lab']}.{overlay['sha256'][:12]}"
        info.update(overlay_sha256=overlay["sha256"], overlay_path=overlay["path"],
                    overlay_lab=overlay["lab"])
    return info


def load_standard_measurements(
    path: str | Path | None = None, *, overlay=_AUTO_OVERLAY,
) -> pd.DataFrame:
    """Load validated base measurements with an optional private TOML overlay.

    ``path`` selects a TOML directory, manifest or legacy normalized CSV.
    Omit ``overlay`` to use :func:`default_lab_overlay_path`; pass None to
    disable it, or a directory/manifest path to select it explicitly. Overlay
    names and aliases cannot collide with the base unless ``replaces`` names
    that base standard (or its unambiguous alias). Source IDs must be unique.
    The returned attrs contain ``sources``, ``overlay`` and ``reference_library``.
    A merged-library marker restores internal record statuses and permits its
    recorded overlay standards to be refreshed. ``merged_library.overlay_action``
    in attrs records ``same``, ``updated``, ``disabled`` or ``legacy``.
    """
    measurements = _load_standard_measurements(path)
    marker = _merged_library_marker(path)
    if marker:
        internal = (measurements.standard.isin(marker["internal_unpublished"])
                    & measurements.status.eq("provisional")
                    & measurements.notes.str.startswith(INTERNAL_REFERENCE_LABEL))
        measurements.loc[internal, "status"] = "internal_unpublished"
    active = _overlay_path(overlay)
    metadata = {}
    action = "legacy" if marker == {} else "disabled"
    info = _base_reference_library_info(path)
    if marker and marker["sha256"]:
        metadata = dict(path=str(Path(_library_root(path)).resolve()), lab=marker["lab"],
                        sha256=marker["sha256"], standards=marker["standards"], replaces=[])
        # The writer already included this overlay in the manifest version.
        suffix = f"+overlay.{marker['lab']}.{marker['sha256'][:12]}"
        digest_suffix = f"+sha256.{info['sha256'][:12]}"
        info["version"] = info["version"].removesuffix(digest_suffix).removesuffix(suffix) + digest_suffix
    if active is not None:
        root = _library_root(active)
        extra = _load_standard_measurements(root, is_overlay=True)
        manifest = _read_toml(_resolve_resource_case_insensitive(root, MANIFEST_FILENAME))
        digest = _base_reference_library_info(root)["sha256"]
        same = bool(marker and digest == marker["sha256"])
        metadata = dict(path=str(Path(root).resolve()), lab=manifest.get("lab", ""),
                        sha256=digest, standards=extra.standard.drop_duplicates().tolist(),
                        replaces=list(manifest.get("replaces", [])))
        if same:
            action = "same"
        else:
            measurements = _apply_overlay(measurements, extra, manifest, marker)
            action = "updated"
    measurements.attrs["overlay"] = metadata
    if marker is not None:
        measurements.attrs["merged_library"] = {**marker, "overlay_action": action}
    measurements.attrs["reference_library"] = _with_overlay_info(info, metadata)
    return measurements


def _apply_overlay(measurements, extra, manifest, marker):
    """Compose an overlay, allowing replacement of its recorded merged copies."""
    base_names = {}
    for name, aliases in measurements[["standard", "aliases"]].drop_duplicates().itertuples(index=False, name=None):
        for spelling in (name, *aliases.split(";")):
            if _standard_key(spelling):
                base_names.setdefault(_standard_key(spelling), set()).add(name)
    current_keys = {_standard_key(name) for name in extra.standard.unique()}
    merged_copies = {name for name in (marker or {}).get("standards", [])
                     if _standard_key(name) in current_keys}
    replaced = set(merged_copies)
    for name in manifest.get("replaces", []):
        matches = base_names.get(_standard_key(name), set())
        if len(matches) != 1:
            raise ValueError(f"Overlay replaces entry {name!r} is unknown or ambiguous in the base library.")
        replaced.update(matches)
    for name, aliases in extra[["standard", "aliases"]].drop_duplicates().itertuples(index=False, name=None):
        for spelling in (name, *aliases.split(";")):
            for base_name in base_names.get(_standard_key(spelling), ()):
                if base_name not in replaced:
                    raise ValueError(f"Overlay standard {name!r} ({spelling!r}) clashes with base standard {base_name!r}; list it in replaces.")
    sources = measurements.attrs["sources"]
    remaining = measurements.loc[~measurements.standard.isin(replaced)]
    # Only discard registry entries belonging to refreshed merged copies.
    # A source still referenced by another base material must keep clashing.
    old_citations = set(measurements.loc[measurements.standard.isin(merged_copies), "source"])
    unused = old_citations - set(remaining.source)
    sources = {key: entry for key, entry in sources.items() if entry["display"] not in unused}
    extra_sources = extra.attrs["sources"]
    clashes = sources.keys() & extra_sources.keys()
    if clashes:
        raise ValueError("Overlay source IDs clash with base source IDs: " + ", ".join(sorted(clashes)))
    merged_sources = {**sources, **extra_sources}
    displays = [entry["display"].strip() for entry in merged_sources.values()]
    if len(displays) != len(set(displays)):
        raise ValueError("Source display citations must be unique across base and overlay.")
    measurements = pd.concat([remaining, extra], ignore_index=True)
    measurements.attrs["sources"] = merged_sources
    return measurements


def reference_library_info(
    path: str | Path | None = None, *, overlay=_AUTO_OVERLAY,
) -> dict[str, str]:
    """Return the library version and digest, including validated overlay identity.

    Without an overlay this is the original base-library provenance dictionary.
    An overlay appends ``+overlay.<lab>.<sha12>`` to the version and adds
    ``overlay_sha256``, ``overlay_path`` and ``overlay_lab``. The base digest
    retains its original meaning. ``overlay=None`` disables the overlay.
    """
    active = _overlay_path(overlay)
    if active is None and _merged_library_marker(path) is None:
        return _base_reference_library_info(path)
    return dict(load_standard_measurements(path, overlay=active).attrs["reference_library"])


def write_merged_library(target: str | Path, base=None, overlay=_AUTO_OVERLAY) -> Path:
    """Write a standalone TOML library for Studio versions without overlays.

    Refuse a nonempty target without this writer's marker. Validate all input
    and generated TOML before replacing a previously generated directory.
    Internal records use the legacy ``provisional`` status on disk, retaining
    their explicit internal/unpublished label in notes and source metadata.
    Their calibration values and aliases are unchanged.
    """
    import json
    import shutil
    import tempfile

    target = Path(target).expanduser().absolute()
    active = _overlay_path(overlay)
    for source in (base, active):
        if source is not None and Path(_library_root(source)).resolve().is_relative_to(target.resolve()):
            raise ValueError("Merged target must not contain an input library.")
    if target.is_symlink():
        raise ValueError("Merged target must not be a symlink.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        if not target.is_dir() or _merged_library_marker(target) is None:
            raise ValueError("Refusing a non-empty target not written by write_merged_library.")
    measurements = load_standard_measurements(base, overlay=active)
    sources = measurements.attrs["sources"]
    if not sources:
        raise ValueError("A merged TOML library requires a source registry.")
    source_ids = {entry["display"].strip(): key for key, entry in sources.items()}
    def quoted(value):
        return json.dumps(str(value), ensure_ascii=False)
    def scalar(value):
        return quoted(value) if isinstance(value, str) else repr(value)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".upbrpy-merged-", dir=target.parent) as temporary:
        staged = Path(temporary) / "library"
        staged.mkdir()
        text = [f"format_version = {ARCHIVE_FORMAT_VERSION}"]
        for source_id, original in sources.items():
            entry = dict(original)
            if entry.get("status") == "internal_unpublished":
                entry["status"] = "provisional"
                for field in ("notes", "no_doi_reason"):
                    original_text = entry.get(field, "")
                    entry[field] = INTERNAL_REFERENCE_LABEL + ("; " + original_text if original_text else "")
            text += [f"\n[sources.{quoted(source_id)}]"]
            text += [f"{key} = {quoted(value)}" for key, value in entry.items()]
        (staged / "sources.toml").write_text("\n".join(text) + "\n", encoding="utf-8")
        names = []
        for index, (name, records) in enumerate(measurements.groupby("standard", sort=False)):
            filename = f"material-{index:04d}.toml"
            names.append(filename)
            first = records.iloc[0]
            table = f"standards.{quoted(name)}"
            aliases = first.aliases.split(";") if first.aliases else []
            text = [f"format_version = {ARCHIVE_FORMAT_VERSION}", f"material = {quoted(first.material)}",
                    f"[{table}]", "aliases = [" + ", ".join(map(quoted, aliases)) + "]"]
            for record in records.itertuples(index=False):
                text += [f"\n[[{table}.measurements]]"]
                internal = record.status == "internal_unpublished"
                for key in ("role", "unit", "status", "notes"):
                    field_value = getattr(record, key)
                    if internal and key == "status":
                        field_value = "provisional"
                    elif internal and key == "notes":
                        field_value = INTERNAL_REFERENCE_LABEL + ("; " + field_value if field_value else "")
                    text += [f"{key} = {quoted(field_value)}"]
                text += [f"source = {quoted(source_ids[record.source])}", f"locator = {quoted(record.source_locator)}",
                         f"[{table}.measurements.values]"]
                value = "value = " + scalar(record.value)
                if pd.notna(record.uncertainty):
                    value += f", uncertainty = {scalar(record.uncertainty)}, uncertainty_type = {quoted(record.uncertainty_type)}"
                text += [f"{quoted(record.quantity)} = {{ {value} }}"]
            (staged / filename).write_text("\n".join(text) + "\n", encoding="utf-8")
        version = measurements.attrs["reference_library"]["version"]
        manifest = [f"format_version = {ARCHIVE_FORMAT_VERSION}", f"version = {quoted(version)}",
                    'sources = "sources.toml"', "material_files = [" + ", ".join(map(quoted, names)) + "]"]
        (staged / MANIFEST_FILENAME).write_text("\n".join(manifest) + "\n", encoding="utf-8")
        overlay_info = measurements.attrs["overlay"]
        marker = ["format = 1", f"lab = {quoted(overlay_info.get('lab', ''))}",
                  f"sha256 = {quoted(overlay_info.get('sha256', ''))}"]
        for key, values in (
            ("standards", overlay_info.get("standards", [])),
            ("internal_unpublished", measurements.loc[
                measurements.status.eq("internal_unpublished"), "standard"].drop_duplicates().tolist()),
        ):
            marker += [f"{key} = [" + ", ".join(map(quoted, values)) + "]"]
        (staged / _MERGED_MARKER).write_text("\n".join(marker) + "\n", encoding="utf-8")
        load_standard_measurements(staged, overlay=None)
        if target.exists():
            shutil.rmtree(target)
        staged.rename(target)
    return target


def _internal_reference_note(records, entry):
    """Describe an internal source without implying a missing publication DOI."""
    internal = (
        entry.get("status") == "internal_unpublished"
        or str(entry.get("notes", "")).startswith(INTERNAL_REFERENCE_LABEL)
        or ("status" in records and records.status.eq("internal_unpublished").any())
        or ("notes" in records and records.notes.str.startswith(INTERNAL_REFERENCE_LABEL).any())
    )
    if not internal:
        return ""
    reason = entry.get("no_doi_reason", "")
    if reason.startswith(INTERNAL_REFERENCE_LABEL):
        reason = reason.removeprefix(INTERNAL_REFERENCE_LABEL).lstrip("; ")
    return INTERNAL_REFERENCE_LABEL + ("; " + reason if reason else "")


def _fill_if_missing(
    archive: pd.DataFrame,
    standard: str,
    quantity: str,
    value,
) -> None:
    """Assign a finite derived value without replacing a documented value."""
    if value is None or not np.isfinite(value):
        return
    if quantity not in archive.columns or pd.isna(archive.loc[standard, quantity]):
        archive.loc[standard, quantity] = float(value)


def _derive_initial_ratios(archive: pd.DataFrame, standard: str) -> None:
    """Derive common-Pb reciprocals and cross-ratios from canonical inputs."""
    row = archive.loc[standard]
    ratio_64 = row.get("206Pb/204Pb_i", np.nan)
    ratio_74 = row.get("207Pb/204Pb_i", np.nan)
    ratio_84 = row.get("208Pb/204Pb_i", np.nan)

    if np.isfinite(ratio_64) and ratio_64 != 0:
        _fill_if_missing(archive, standard, "204Pb/206Pb_i", 1 / ratio_64)
    if np.isfinite(ratio_74) and ratio_74 != 0:
        _fill_if_missing(archive, standard, "204Pb/207Pb_i", 1 / ratio_74)
    if np.isfinite(ratio_84) and ratio_84 != 0:
        _fill_if_missing(archive, standard, "204Pb/208Pb_i", 1 / ratio_84)
    if np.isfinite(ratio_64) and np.isfinite(ratio_74) and ratio_74 != 0:
        _fill_if_missing(archive, standard, "206Pb/207Pb_i", ratio_64 / ratio_74)
        _fill_if_missing(archive, standard, "207Pb/206Pb_i", ratio_74 / ratio_64)
    if np.isfinite(ratio_64) and np.isfinite(ratio_84) and ratio_84 != 0:
        _fill_if_missing(archive, standard, "206Pb/208Pb_i", ratio_64 / ratio_84)


def _ratio_76_from_age(age_ma: float) -> float:
    """Return concordant radiogenic ``207Pb/206Pb`` for an age in Ma."""
    age_years = float(age_ma) * 1e6
    ratio_75 = np.expm1(lambda_235 * age_years)
    ratio_68 = np.expm1(lambda_238 * age_years)
    return ratio_75 / ratio_68 / common_U238_U235


def _finite_or_fallback(value, fallback) -> float:
    """Return ``value`` when finite, otherwise a finite fallback or ``NaN``."""
    if value is not None and np.isfinite(value):
        return float(value)
    if fallback is not None and np.isfinite(fallback):
        return float(fallback)
    return np.nan


def _derive_radiogenic_ratios(archive: pd.DataFrame, standard: str) -> None:
    """Fill radiogenic ratios from reported system ages and algebraic links."""
    row = archive.loc[standard]
    concordia_age = row.get("concordia_age", np.nan)
    system_age_68 = row.get("206Pb/238U_age", np.nan)
    system_age_75 = row.get("207Pb/235U_age", np.nan)
    system_age_76 = row.get("207Pb/206Pb_age", np.nan)

    # The system-age columns may exist because another standard reports one,
    # yet contain NaN for this standard. Series.get() would not fall back in
    # that case, so make the finite-value precedence explicit.
    age_68 = _finite_or_fallback(system_age_68, concordia_age)
    age_75 = _finite_or_fallback(system_age_75, concordia_age)

    if np.isfinite(age_68):
        _fill_if_missing(
            archive,
            standard,
            "206Pb/238U*",
            np.expm1(lambda_238 * float(age_68) * 1e6),
        )
    if np.isfinite(age_75):
        _fill_if_missing(
            archive,
            standard,
            "207Pb/235U*",
            np.expm1(lambda_235 * float(age_75) * 1e6),
        )
    # A separately reported 207Pb/206Pb age is an independent source value.
    # Otherwise wait until after the algebraic link below: two documented
    # U-Pb ratios should take precedence over a general accepted age.
    if np.isfinite(system_age_76):
        _fill_if_missing(
            archive,
            standard,
            "207Pb/206Pb*",
            _ratio_76_from_age(system_age_76),
        )
    if np.isfinite(concordia_age):
        _fill_if_missing(
            archive,
            standard,
            "208Pb/232Th*",
            np.expm1(LAMBDA_232 * float(concordia_age) * 1e6),
        )

    # Re-read the row after age-based values have been inserted.
    row = archive.loc[standard]
    ratio_68 = row.get("206Pb/238U*", np.nan)
    ratio_75 = row.get("207Pb/235U*", np.nan)
    ratio_76 = row.get("207Pb/206Pb*", np.nan)

    if np.isfinite(ratio_68) and np.isfinite(ratio_75) and ratio_68 != 0:
        _fill_if_missing(
            archive,
            standard,
            "207Pb/206Pb*",
            ratio_75 / ratio_68 / common_U238_U235,
        )
    elif np.isfinite(concordia_age):
        _fill_if_missing(
            archive,
            standard,
            "207Pb/206Pb*",
            _ratio_76_from_age(concordia_age),
        )
    if np.isfinite(ratio_68) and np.isfinite(ratio_76):
        _fill_if_missing(
            archive,
            standard,
            "207Pb/235U*",
            ratio_76 * ratio_68 * common_U238_U235,
        )
    if np.isfinite(ratio_75) and np.isfinite(ratio_76) and ratio_76 != 0:
        _fill_if_missing(
            archive,
            standard,
            "206Pb/238U*",
            ratio_75 / ratio_76 / common_U238_U235,
        )

    row = archive.loc[standard]
    ratio_68 = row.get("206Pb/238U*", np.nan)
    ratio_76 = row.get("207Pb/206Pb*", np.nan)
    if np.isfinite(ratio_68) and ratio_68 != 0:
        _fill_if_missing(archive, standard, "238U/206Pb*", 1 / ratio_68)
    if np.isfinite(ratio_76) and ratio_76 != 0:
        _fill_if_missing(archive, standard, "206Pb/207Pb*", 1 / ratio_76)


def _derive_total_ratios(archive: pd.DataFrame, standard: str) -> None:
    """Fill only exact reciprocal forms of documented total ratios."""
    row = archive.loc[standard]
    ratio_68 = row.get("206Pb/238U_total", np.nan)
    ratio_76 = row.get("207Pb/206Pb_total", np.nan)
    if np.isfinite(ratio_68) and ratio_68 != 0:
        _fill_if_missing(archive, standard, "238U/206Pb_total", 1 / ratio_68)
    if np.isfinite(ratio_76) and ratio_76 != 0:
        _fill_if_missing(archive, standard, "206Pb/207Pb_total", 1 / ratio_76)


def build_standard_archive(
    measurements: pd.DataFrame | None = None,
    *,
    include_derived: bool = True,
) -> pd.DataFrame:
    """Pivot normalized measurements into the wide calibration interface.

    Documented inputs always take precedence. When ``include_derived`` is true,
    the function calculates reciprocals, cross-ratios, and ratios implied by
    accepted ages. This recreates the convenient interface expected by the
    concentration and fractionation routines without storing those calculated
    values as if they were independent reference measurements.
    """
    if measurements is None:
        measurements = load_standard_measurements()
    else:
        measurements = measurements.copy()

    standard_order = measurements["standard"].drop_duplicates().tolist()
    quantity_order = measurements["quantity"].drop_duplicates().tolist()
    material = (
        measurements.drop_duplicates("standard", keep="first")
        .set_index("standard")["material"]
        .reindex(standard_order)
        .rename("material")
    )
    documented = measurements.pivot(
        index="standard",
        columns="quantity",
        values="value",
    ).reindex(index=standard_order, columns=quantity_order)
    documented.index.name = "Standard"
    documented.columns.name = None
    archive = pd.concat([material, documented], axis="columns")
    archive.index.name = "Standard"

    if include_derived:
        # Allocate all possible derived columns together. This avoids repeatedly
        # fragmenting a wide DataFrame as the individual standards are filled.
        possible_derived = (
            "204Pb/206Pb_i",
            "204Pb/207Pb_i",
            "204Pb/208Pb_i",
            "206Pb/207Pb_i",
            "207Pb/206Pb_i",
            "206Pb/208Pb_i",
            "206Pb/238U*",
            "207Pb/235U*",
            "207Pb/206Pb*",
            "208Pb/232Th*",
            "238U/206Pb*",
            "206Pb/207Pb*",
            "238U/206Pb_total",
            "206Pb/207Pb_total",
        )
        missing_derived = [
            quantity for quantity in possible_derived if quantity not in archive
        ]
        if missing_derived:
            archive = archive.reindex(columns=[*archive.columns, *missing_derived])
        for standard in archive.index:
            _derive_initial_ratios(archive, standard)
            _derive_radiogenic_ratios(archive, standard)
            _derive_total_ratios(archive, standard)

    # The normalized table is exported separately as ``standard_measurements``.
    # Do not attach that DataFrame to another DataFrame's ``attrs``: pandas
    # compares attrs while concatenating or formatting slices, and equality of
    # DataFrames has an ambiguous truth value. Scalar provenance keeps normal
    # DataFrame operations safe and avoids carrying a second table through
    # every archive slice.
    archive.attrs["source_library"] = "TOML"
    archive.attrs["overlay"] = dict(measurements.attrs.get("overlay", {}))
    if "merged_library" in measurements.attrs:
        archive.attrs["merged_library"] = dict(measurements.attrs["merged_library"])
    if "reference_library" in measurements.attrs:
        archive.attrs["reference_library"] = dict(measurements.attrs["reference_library"])
    archive.attrs["measurement_count"] = int(len(measurements))
    archive.attrs["derived_values_included"] = bool(include_derived)
    if "aliases" in measurements:
        archive.attrs["standard_aliases"] = {
            name: tuple(aliases.split(";"))
            for name, aliases in measurements[["standard", "aliases"]].drop_duplicates().itertuples(index=False)
            if isinstance(aliases, str) and aliases
        }
    return archive


standard_measurements = load_standard_measurements()
standard_sources = standard_measurements.attrs["sources"]
standard_archive = build_standard_archive(standard_measurements)
# Compatibility alias for notebooks written while the TOML archive was opt-in.
standard_archive_v2 = standard_archive


def canonicalize_reference_material_samples(data, archive):
    """Return a copy with unambiguous reference-material labels canonicalized.

    Instrument files and reference libraries commonly differ only by case,
    spaces, underscores, or hyphens (for example ``LAB-X`` versus ``LABX``).
    Matching on an alphanumeric case-folded key keeps calibration lookup
    independent of filename typography while leaving unknown sample labels
    untouched. Canonical names take priority over aliases. Ambiguous aliases
    and ambiguous canonical spellings are deliberately not substituted.
    """
    if not isinstance(data, pd.DataFrame):
        raise TypeError("data must be a pandas DataFrame")
    if not isinstance(archive, pd.DataFrame):
        raise TypeError("archive must be a pandas DataFrame")
    if "sample" not in data.columns:
        return data.copy()

    key = _standard_key

    candidates = {}
    ambiguous = set()
    for name in archive.index.astype(str):
        normalized = key(name)
        if normalized in candidates and candidates[normalized] != name:
            ambiguous.add(normalized)
        else:
            candidates[normalized] = name
    canonical_keys = set(candidates)
    for name, aliases in archive.attrs.get("standard_aliases", {}).items():
        if name not in archive.index:
            continue
        for alias in aliases:
            normalized = key(alias)
            if not normalized or normalized in canonical_keys:
                continue
            if normalized in candidates and candidates[normalized] != name:
                ambiguous.add(normalized)
            else:
                candidates[normalized] = name
    for normalized in ambiguous:
        candidates.pop(normalized, None)

    result = data.copy()
    original = result["sample"].astype(str)
    replacement = original.map(lambda value: candidates.get(key(value), value))
    result["sample"] = replacement
    changed = {
        before: after
        for before, after in zip(original, replacement)
        if before != after
    }
    result.attrs = dict(data.attrs)
    result.attrs["reference_material_sample_aliases"] = changed
    return result


__all__ = [
    "ARCHIVE_FORMAT_VERSION",
    "reference_library_info",
    "default_lab_overlay_path",
    "write_merged_library",
    "LAMBDA_232",
    "MANIFEST_FILENAME",
    "REFERENCE_MATERIALS_DIRECTORY",
    "REQUIRED_COLUMNS",
    "ROLE_UNITS",
    "VALID_ROLES",
    "VALID_STATUSES",
    "VALID_UNCERTAINTY_TYPES",
    "build_standard_archive",
    "canonicalize_reference_material_samples",
    "check_reference_sources",
    "load_standard_measurements",
    "standard_archive",
    "standard_archive_v2",
    "standard_measurements",
    "standard_sources",
]
