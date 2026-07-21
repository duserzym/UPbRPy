"""Source-traceable reference-material measurements and derived archives.

The historical :mod:`UPbRPy.constants` archive is a convenient wide table, but
it mixes primary values with reciprocals and other calculated ratios. The
primary library instead uses readable TOML files grouped by material,
standard, and measurement type. This module expands and validates those source
documents, then constructs a backwards-friendly wide view on demand.

The TOML archive is the package's primary reference-material interface. The
historical CSV remains available as ``standard_archive_legacy`` for comparison
and migration work.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

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

VALID_STATUSES = {"recommended", "provisional"}
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
_SOURCE_KEYS = {"display", "kind", "doi", "file", "url", "notes"}


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
    """Load and validate the shared source registry."""
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
        displays.append(display.strip())
    if len(displays) != len(set(displays)):
        raise ValueError("Source display citations must be unique.")
    return registry


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


def _load_toml_measurements(root) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Expand the manifest-driven TOML library into normalized rows."""
    manifest_resource = _resolve_resource_case_insensitive(root, MANIFEST_FILENAME)
    manifest = _read_toml(manifest_resource)
    _validate_format_version(manifest, MANIFEST_FILENAME)
    allowed_manifest_keys = {"format_version", "sources", "material_files"}
    _reject_unknown_keys(manifest, allowed_manifest_keys, MANIFEST_FILENAME)

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


def load_standard_measurements(path: str | Path | None = None) -> pd.DataFrame:
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
        measurements, sources = _load_toml_measurements(source_root)
    else:
        source_path = Path(path)
        if source_path.suffix.lower() == ".csv":
            measurements = _load_csv_measurements(source_path)
        else:
            source_root = (
                source_path.parent
                if source_path.name == MANIFEST_FILENAME
                else source_path
            )
            measurements, sources = _load_toml_measurements(source_root)

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
    archive.attrs["measurement_count"] = int(len(measurements))
    archive.attrs["derived_values_included"] = bool(include_derived)
    return archive


standard_measurements = load_standard_measurements()
standard_sources = standard_measurements.attrs["sources"]
standard_archive = build_standard_archive(standard_measurements)
# Compatibility alias for notebooks written while the TOML archive was opt-in.
standard_archive_v2 = standard_archive


__all__ = [
    "ARCHIVE_FORMAT_VERSION",
    "LAMBDA_232",
    "MANIFEST_FILENAME",
    "REFERENCE_MATERIALS_DIRECTORY",
    "REQUIRED_COLUMNS",
    "ROLE_UNITS",
    "VALID_ROLES",
    "VALID_STATUSES",
    "VALID_UNCERTAINTY_TYPES",
    "build_standard_archive",
    "load_standard_measurements",
    "standard_archive",
    "standard_archive_v2",
    "standard_measurements",
    "standard_sources",
]
