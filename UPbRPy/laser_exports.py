"""Laser metadata shared by publication, audit and database exports."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
import re

import pandas as pd

from .laser_guidance import DEFAULT_LASER_QC_THRESHOLD_S


ABLATION_FIELDS = ("fluence_j_cm2", "rep_rate_hz", "spot_um", "spot_shape", "shot_count", "output_pct")
LASER_COLUMNS = (
    ("spot_um", "Spot diameter", "µm"),
    ("rep_rate_hz", "Rep. rate", "Hz"),
    ("fluence_j_cm2", "Fluence", "J/cm²"),
)
STAGE_COLUMNS = tuple(f"Stage {axis} (µm)" for axis in "xyz")
_PREFILL = dict(spot_diameter_um="spot_um", repetition_rate_hz="rep_rate_hz", fluence_j_cm2="fluence_j_cm2")


@dataclass(frozen=True)
class LaserExportData:
    """Export metadata keyed by current experiment labels; never a raw log."""

    source: str = ""
    logs: tuple[dict, ...] = ()
    spots: dict[str, dict] = field(default_factory=dict)


def laser_export_data(experiment, *, manifest=None, laser_tie=None) -> LaserExportData:
    """Read linked ablation settings and stage from either source, respecting renames.

    A loaded tie takes precedence over the bundle. No file is read or changed.
    """
    renames = experiment.attrs.get("spot_renames", {})
    if laser_tie is not None:
        logs = laser_tie.logs or ({name: getattr(laser_tie, name) for name in (
            "sha256", "file_name", "system_type", "laser_instrument", "spot_key",
            "clock_offset_s", "clock_offset_spread_s", "pairs")},)
        raw = {label: row for label, row in laser_tie.rows.items() if row.status == "linked"}
        source = laser_tie.source
    elif manifest is not None and manifest.has_laser_data:
        logs = tuple({**{name: getattr(log, name) for name in (
            "sha256", "file_name", "system_type", "laser_instrument", "spot_key",
            "clock_offset_s", "clock_offset_spread_s")}, "pairs": log.offset_pairs}
            for log in manifest.laser_logs)
        raw = {row.label: row.laser for row in manifest.acquisitions if row.laser is not None}
        source = "manifest"
    else:
        return LaserExportData()
    spots = {}
    for label in experiment.spot_names:
        row = raw.get(renames.get(label, label))
        if row is not None:
            spots[label] = {name: getattr(row, name) for name in (*ABLATION_FIELDS, "stage_um")}
            spots[label]["log_sha256"] = (laser_tie.sha256 if laser_tie is not None else row.log_sha256)
    return LaserExportData(source, logs, spots)


def pipeline_laser_export_data(pipeline) -> LaserExportData:
    """Resolve the bundle or loaded tie used by a pipeline for export."""
    recorded = getattr(pipeline, "laser_export_data", None)
    if recorded is not None:
        return recorded
    tie = getattr(pipeline, "laser_tie", None)
    manifest = getattr(pipeline, "session_manifest", None)
    if tie is None and manifest is None and pipeline.source_directory is not None:
        from .io import resolve_session_manifest
        manifest = resolve_session_manifest(pipeline.source_directory, None)
    return laser_export_data(pipeline.experiment, manifest=manifest, laser_tie=tie)


def _plain_number(value):
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def laser_export_prefill(data: LaserExportData, spot_names=None, *, existing=None) -> tuple[dict, dict]:
    """Return four strings and variation flags, preserving nonempty analyst text.

    Only linked values among the requested exported labels contribute ranges.
    Missing values are not interpreted as zero.
    """
    labels = data.spots if spot_names is None else spot_names
    spots = [data.spots[label] for label in labels if label in data.spots]
    hashes = {str(row.get("log_sha256", "")).casefold() for row in spots}
    systems = list(dict.fromkeys(log["laser_instrument"] or log["system_type"]
        for log in data.logs if not hashes or log["sha256"].casefold() in hashes))
    systems = [value for value in systems if value]
    values = {"laser_system": ", ".join(systems)}
    varies = {"laser_system": len(systems) > 1}
    for name, key in _PREFILL.items():
        distinct = sorted({row[key] for row in spots if row.get(key) is not None})
        varies[name] = len(distinct) > 1
        values[name] = (f"{_plain_number(distinct[0])}–{_plain_number(distinct[-1])}" if varies[name]
                        else _plain_number(distinct[0]) if distinct else "")
    for key, value in (existing or {}).items():
        if key in values and value:
            values[key] = value
    return values, varies


def is_stage_coordinate_key(key) -> bool:
    """Recognize coordinate fields without removing workflow stage provenance."""
    text = str(key).lower().replace(" ", "_")
    return bool(re.search(r"(?:^|_)stage_(?:um|[xyz](?:_um|_\(µm\))?)$", text))


def without_stage_coordinates(value):
    """Copy structured export data with coordinate fields removed recursively."""
    if isinstance(value, pd.DataFrame):
        result = value.drop(columns=[c for c in value if is_stage_coordinate_key(c)]).copy()
        for column in result.columns[result.dtypes.map(pd.api.types.is_object_dtype)]:
            result[column] = result[column].map(without_stage_coordinates)
        result.attrs = without_stage_coordinates(value.attrs)
        return result
    if isinstance(value, Mapping):
        result = {key: without_stage_coordinates(item) for key, item in value.items()
                  if not is_stage_coordinate_key(key)}
        if {"rows", "sha256", "spot_key", "has_laser_times"}.issubset(value):
            # Parser warnings can quote malformed position strings verbatim.
            # They stay in the working tie, but are unnecessary for replay.
            result["warnings"] = []
        return result
    if isinstance(value, (list, tuple)):
        return type(value)(without_stage_coordinates(item) for item in value)
    return value


def laser_export_frame(frame, data: LaserExportData, *, include_stage_coordinates=False,
                       label_column="spot_name", headings=False):
    """Add varying ablation columns and optional coordinates to per-spot data."""
    result = frame.copy() if include_stage_coordinates else without_stage_coordinates(frame)
    if label_column not in result or not data.source:
        return result
    labels = result[label_column]
    _, varies = laser_export_prefill(data, labels)
    for name, key in _PREFILL.items():
        if varies[name]:
            _, label, unit = next(c for c in LASER_COLUMNS if c[0] == key)
            column = f"{label} ({unit})" if headings else "laser_" + key
            result[column] = labels.map({label: row.get(key) for label, row in data.spots.items()})
    if include_stage_coordinates:
        for axis, column in enumerate(STAGE_COLUMNS):
            result[column] = labels.map({label: row["stage_um"][axis] for label, row in data.spots.items()
                                        if row.get("stage_um") is not None})
    return result


def laser_export_manifest(pipeline, data: LaserExportData, *, include_stage_coordinates=False, spot_names=None):
    """Summarize logs, selection QC and distinct settings of exported spots."""
    if not data.source:
        return None
    experiment = pipeline.experiment
    labels = list(experiment.spot_names if spot_names is None else spot_names)
    spots = {label: data.spots[label] for label in labels if label in data.spots}
    combinations = Counter(tuple(row.get(key) for key in ABLATION_FIELDS) for row in spots.values())
    qc = experiment.attrs.get("laser", {})
    options = {**experiment.attrs.get("selection_parameters", {}), **pipeline.config.selection_options}
    nested_options = options.get("selection_options") or {}
    threshold_s = options.get("laser_qc_threshold_s", nested_options.get(
        "laser_qc_threshold_s", DEFAULT_LASER_QC_THRESHOLD_S))
    record = dict(source=data.source, logs=list(data.logs),
        transport_delay_s=qc.get("transport_delay_s"),
        qc_threshold_s=qc.get("qc_threshold_s", threshold_s),
        flagged_count=int(experiment.loc[experiment.spot_names.isin(labels)].get(
            "laser_onset_flag", pd.Series(dtype=bool)).fillna(False).sum()),
        signal_duration_limit_source=options.get("signal_duration_limit_source", "fixed"),
        laser_guided=bool(experiment.get("selection_method", pd.Series(dtype=str)).eq("laser_guided").any()),
        ablation_settings=[{**dict(zip(ABLATION_FIELDS, combo)), "acquisitions": count}
                           for combo, count in combinations.items()])
    if include_stage_coordinates:
        record["stage_um"] = {label: row["stage_um"] for label, row in spots.items() if row.get("stage_um") is not None}
    return record


def export_manifest(pipeline, settings, *, laser_data=None, spot_names=None, **kwargs):
    """Build export provenance without exposing coordinates by default."""
    record = pipeline.manifest(**kwargs)
    if not settings.include_stage_coordinates:
        record = without_stage_coordinates(record)
    laser = laser_export_manifest(pipeline, laser_data or pipeline_laser_export_data(pipeline),
        include_stage_coordinates=settings.include_stage_coordinates, spot_names=spot_names)
    if laser is not None:
        record["laser"] = laser
    return record
