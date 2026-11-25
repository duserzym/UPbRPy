"""Lightweight, replayable UPbRPy reduction project files.

A ``.upbr`` project records paths, scientific settings, reviewed selection
windows, and small GUI workspace choices.  It intentionally does not embed raw
measurements or calculated tables.  Opening the file therefore re-imports the
source data and recalculates the stages that had been completed when the
project was saved.

Format version 2 adds one optional block, ``origin``, naming the database
session a project was exported from (``upblab/docs/WORKFLOW.md`` section
3.2)::

    "origin": {
        "server": "https://lab.example.org",
        "session_id": "0192…",           # session UUID
        "session_key": "Apatite_20Mar25",
        "export_sha256": "…",            # SHA-256 of session_manifest.toml
        "exported_at": "2026-11-03T…Z",
        "run_id": "0193…"                # after Submit to database
    }

Version 1 files load unchanged; they simply have no origin. The optional
top-level ``saved_by`` records Studio's app name, package version and source
commit. ``pipeline.settings_schema`` records the scientific settings schema;
neither is part of ``pipeline.config``. Unknown settings or a newer schema
are refused with an update message rather than discarded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import numpy as np
import pandas as pd

from .spot_edits import spot_edit_metadata

from .pipeline import (
    NewerFileError, PipelineStage, ReductionPipeline, SETTINGS_SCHEMA_VERSION,
    _validate_saved_settings, package_version, source_revision,
)
from .reproducibility import selection_export_frame


PROJECT_FILE_EXTENSION = ".upbr"
PROJECT_FORMAT_NAME = "UPbRPy reduction project"
PROJECT_FORMAT_VERSION = 2
ORIGIN_REQUIRED_KEYS = ("server", "session_id")
ORIGIN_KEYS = (
    "server",
    "session_id",
    "session_key",
    "export_sha256",
    "exported_at",
    "run_id",
)


def json_safe_value(value: object) -> object:
    """Return a JSON-compatible representation of common scientific values."""
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): json_safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe_value(item) for item in value]
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return json_safe_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def project_path_record(
    value: str | Path | None,
    project_file: str | Path,
) -> dict[str, str] | None:
    """Describe one dependency with portable and original path fallbacks."""
    if value is None or not str(value):
        return None
    target = Path(value).expanduser().resolve(strict=False)
    project_directory = Path(project_file).expanduser().resolve(
        strict=False
    ).parent
    try:
        relative = os.path.relpath(target, start=project_directory)
    except ValueError:
        relative = ""
    return {
        "relative": relative,
        "absolute": str(target),
    }


def resolve_project_path(
    project_file: str | Path,
    record: Mapping[str, object] | str | None,
    *,
    label: str,
    required: bool = True,
) -> Path | None:
    """Resolve a saved dependency, preferring its project-relative location."""
    if record is None:
        if required:
            raise FileNotFoundError(f"The project does not define {label}.")
        return None

    project_directory = Path(project_file).expanduser().resolve(
        strict=False
    ).parent
    candidates: list[Path] = []
    if isinstance(record, str):
        candidates.append(Path(record).expanduser())
    else:
        relative = str(record.get("relative", "") or "")
        absolute = str(record.get("absolute", "") or "")
        if relative:
            candidates.append(project_directory / relative)
        if absolute:
            candidates.append(Path(absolute).expanduser())

    unique_candidates: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in unique_candidates:
            unique_candidates.append(resolved)
        if resolved.exists():
            return resolved

    if not required and not unique_candidates:
        return None
    attempted = ", ".join(str(path) for path in unique_candidates) or "none"
    raise FileNotFoundError(
        f"Cannot find {label}. Paths tried: {attempted}. "
        "Move the project beside its data again or edit the path in the "
        "human-readable .upbr file."
    )


def normalize_origin(
    origin: Mapping[str, object] | None,
) -> dict[str, str] | None:
    """Validate an ``origin`` block and return it with string values.

    Unknown keys are kept (a newer server may add some) but must be JSON
    scalars; ``server`` and ``session_id`` are required, the rest optional.
    Empty optional values are dropped.
    """
    if origin is None:
        return None
    if not isinstance(origin, Mapping):
        raise ValueError("Project origin must be a JSON object.")
    cleaned: dict[str, str] = {}
    for key, value in origin.items():
        if value is None or value == "":
            continue
        if isinstance(value, (Mapping, list, tuple)):
            raise ValueError(f"Project origin field {key!r} must be a string.")
        cleaned[str(key)] = str(value)
    missing = [key for key in ORIGIN_REQUIRED_KEYS if not cleaned.get(key)]
    if missing:
        raise ValueError(
            "Project origin is missing " + ", ".join(missing) + "."
        )
    cleaned["server"] = cleaned["server"].rstrip("/")
    return cleaned


def project_origin(document: Mapping[str, object]) -> dict[str, str] | None:
    """Return the validated database origin of a project document, if any."""
    return normalize_origin(document.get("origin"))


def selection_records(experiment: pd.DataFrame) -> list[dict[str, object]]:
    """Return JSON-safe reviewed selection decisions for every spot."""
    frame = selection_export_frame(experiment)
    records = frame.to_dict(orient="records")
    return [
        {
            str(key): json_safe_value(value)
            for key, value in record.items()
        }
        for record in records
    ]


def completed_project_stages(
    values: Sequence[str | PipelineStage],
) -> tuple[PipelineStage, ...]:
    """Validate and order the stage identifiers stored in a project."""
    requested = {PipelineStage(value) for value in values}
    return tuple(
        stage
        for stage in PipelineStage
        if stage in requested
    )


def default_resume_stage(pipeline: ReductionPipeline) -> PipelineStage:
    """Return the furthest calculated stage suitable for resuming the GUI."""
    completed = pipeline.cache.stages()
    if not completed:
        return PipelineStage.IMPORT
    return completed[-1]


def build_reduction_project(
    pipeline: ReductionPipeline,
    project_file: str | Path,
    *,
    resume_stage: PipelineStage | str | None = None,
    workspace: Mapping[str, object] | None = None,
    origin: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the lightweight document written inside a ``.upbr`` file.

    ``origin`` defaults to ``pipeline.origin``; it is written only when set.
    """
    if pipeline.source_directory is None:
        raise ValueError(
            "A project can only be saved after an experiment directory is "
            "loaded."
        )
    resumed = (
        default_resume_stage(pipeline)
        if resume_stage is None
        else PipelineStage(resume_stage)
    )
    resolved_origin = normalize_origin(
        origin if origin is not None else getattr(pipeline, "origin", None)
    )
    document: dict[str, object] = {
        "format": PROJECT_FORMAT_NAME,
        "format_version": PROJECT_FORMAT_VERSION,
        "saved_utc": datetime.now(timezone.utc).isoformat(),
        "saved_by": {
            "app": "UPbRPy Studio",
            "version": package_version(),
            "commit": (source_revision() or {}).get("commit"),
        },
        "paths": {
            "source_directory": project_path_record(
                pipeline.source_directory,
                project_file,
            ),
            "reference_library": project_path_record(
                pipeline.reference_library_path,
                project_file,
            ),
        },
        "pipeline": {
            "settings_schema": SETTINGS_SCHEMA_VERSION,
            "config": pipeline.config.to_dict(),
            "completed_stages": [
                stage.value for stage in pipeline.cache.stages()
            ],
            "resume_stage": resumed.value,
        },
        "selections": selection_records(pipeline.experiment),
        "spot_edits": spot_edit_metadata(pipeline.experiment),
        "workspace": json_safe_value(dict(workspace or {})),
    }
    if resolved_origin:
        document["origin"] = resolved_origin
    tie = getattr(pipeline, "laser_tie", None)
    if tie is not None:
        document["laser_log"] = tie.to_dict()
    return document


def validate_reduction_project(
    document: Mapping[str, object],
) -> dict[str, object]:
    """Validate a parsed project and return an ordinary mutable dictionary."""
    if document.get("format") != PROJECT_FORMAT_NAME:
        raise ValueError("This file is not an UPbRPy reduction project.")
    version = int(document.get("format_version", 0))
    saved_by = document.get("saved_by")
    saved_version = saved_by.get("version") if isinstance(saved_by, Mapping) else None
    if version > PROJECT_FORMAT_VERSION:
        pipeline = document.get("pipeline")
        saved_schema = pipeline.get("settings_schema") if isinstance(pipeline, Mapping) else None
        raise NewerFileError(saved_version, saved_schema=saved_schema)
    if version < 1:
        raise ValueError("The project format version is missing or invalid.")

    paths = document.get("paths")
    pipeline = document.get("pipeline")
    selections = document.get("selections")
    workspace = document.get("workspace", {})
    if not isinstance(paths, Mapping):
        raise ValueError("Project paths are missing or invalid.")
    if not isinstance(pipeline, Mapping):
        raise ValueError("Project pipeline settings are missing or invalid.")
    if not isinstance(pipeline.get("config"), Mapping):
        raise ValueError("Project reduction configuration is missing.")
    _validate_saved_settings(pipeline["config"], saved_version=saved_version,
                             saved_schema=pipeline.get("settings_schema"))
    if not isinstance(selections, list):
        raise ValueError("Project selection records are missing or invalid.")
    if not isinstance(workspace, Mapping):
        raise ValueError("Project workspace settings are invalid.")

    stage_values = pipeline.get("completed_stages", ())
    if not isinstance(stage_values, (list, tuple)):
        raise ValueError("Project completed stages must be a list.")
    completed_project_stages(stage_values)
    PipelineStage(pipeline.get("resume_stage", PipelineStage.IMPORT.value))
    result = dict(document)
    if "origin" in result:
        result["origin"] = normalize_origin(result["origin"])
        if result["origin"] is None:
            result.pop("origin")
    return result


def read_reduction_project(project_file: str | Path) -> dict[str, object]:
    """Read and validate a human-readable ``.upbr`` project file."""
    path = Path(project_file).expanduser()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path.name} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno})."
        ) from exc
    if not isinstance(document, Mapping):
        raise ValueError("The project root must be a JSON object.")
    return validate_reduction_project(document)


def write_reduction_project(
    pipeline: ReductionPipeline,
    project_file: str | Path,
    *,
    resume_stage: PipelineStage | str | None = None,
    workspace: Mapping[str, object] | None = None,
    origin: Mapping[str, object] | None = None,
) -> Path:
    """Atomically write a lightweight, human-readable project document."""
    target = Path(project_file).expanduser()
    if target.suffix.lower() != PROJECT_FILE_EXTENSION:
        target = target.with_suffix(PROJECT_FILE_EXTENSION)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = build_reduction_project(
        pipeline,
        target,
        resume_stage=resume_stage,
        workspace=workspace,
        origin=origin,
    )
    return write_project_document(document, target)


def write_project_document(
    document: Mapping[str, object],
    project_file: str | Path,
) -> Path:
    """Atomically write an already built project document as JSON.

    Used to update one field, such as ``origin.run_id`` after a submit,
    without rebuilding the document from a live pipeline.
    """
    target = Path(project_file).expanduser()
    validate_reduction_project(document)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return target.resolve(strict=False)


def _complete_threshold_selections(experiment, records, method, fallbacks):
    """Require full coverage, including diagnostics and the saved manual flag."""
    if not records or method != 'threshold' or fallbacks:
        return False
    required = {
        'spot_names', 'baseline_start_times', 'baseline_end_times',
        'plateau_start_times', 'plateau_end_times', 'signal_start_times',
        'signal_end_times', 'selection_error', 'selection_method',
        'selection_channel', 'selection_detection_index',
        'selection_signal_end_index', 'selection_unrestricted_signal_end_index',
        'selection_signal_duration_limit_seconds',
        'selection_signal_duration_limit_applied', 'selection_washout_start_index',
        'selection_post_baseline_start_index', 'selection_falling_edge_index',
        'selection_manual',
    }
    if any(
        not required.issubset(record) or record['selection_error'] is not None
        for record in records
    ):
        return False
    names = [record['spot_names'] for record in records]
    return len(names) == len(set(names)) and set(names) == set(experiment['spot_names'])


def _restore_selection_inputs_valid(total_data, primary_channel, selection_channels):
    """Keep full selection when a skipped spot could raise an input error."""
    channels = [primary_channel, *(str(value).strip() for value in (selection_channels or {}).values())]
    if any(channel not in total_data for channel in channels):
        return False
    times = total_data['Time']
    return bool(
        np.isfinite(times).all()
        and not total_data.groupby('spot_name', sort=False)['Time'].diff().le(0).any()
    )


def restore_project_selections(
    experiment: pd.DataFrame,
    records: Sequence[Mapping[str, object]],
    *,
    strict_spot_names: bool = True,
) -> pd.DataFrame:
    """Overlay saved reviewed windows on freshly imported spot metadata.

    Strict spot matching prevents a changed raw-data directory from silently
    producing a reduction different from the session that was saved.
    """
    if "spot_names" not in experiment.columns:
        raise KeyError("Imported experiment must contain a spot_names column.")
    saved = pd.DataFrame.from_records(records)
    if saved.empty or "spot_names" not in saved.columns:
        raise ValueError("The project contains no usable spot selections.")
    if saved["spot_names"].duplicated().any():
        raise ValueError("Project selection records contain duplicate spots.")
    if experiment["spot_names"].duplicated().any():
        raise ValueError("Imported experiment contains duplicate spot names.")

    imported_names = set(experiment["spot_names"].astype(str))
    saved_names = set(saved["spot_names"].astype(str))
    if strict_spot_names and imported_names != saved_names:
        missing = sorted(saved_names - imported_names)
        added = sorted(imported_names - saved_names)
        details = []
        if missing:
            details.append("missing from data: " + ", ".join(missing[:8]))
        if added:
            details.append("new in data: " + ", ".join(added[:8]))
        raise ValueError(
            "The raw-data spot list has changed since this project was "
            "saved (" + "; ".join(details) + ")."
        )

    restored = experiment.copy()
    saved = saved.set_index(saved["spot_names"].astype(str))
    imported_key = restored["spot_names"].astype(str)
    common_names = imported_names & saved_names
    for column in saved.columns:
        if column == "spot_names":
            continue
        values = imported_key.map(saved[column])
        mask = imported_key.isin(common_names)
        restored.loc[mask, column] = values.loc[mask].to_numpy()
        saved_values = saved[column].dropna()
        if len(saved_values) and saved_values.map(lambda value: isinstance(value, (bool, np.bool_))).all():
            # Keep flag columns boolean after the masked overlay.
            restored[column] = restored[column].where(restored[column].notna(), False).astype(bool)
    return restored


__all__ = [
    "PROJECT_FILE_EXTENSION",
    "PROJECT_FORMAT_NAME",
    "PROJECT_FORMAT_VERSION",
    "ORIGIN_KEYS",
    "json_safe_value",
    "normalize_origin",
    "project_origin",
    "write_project_document",
    "project_path_record",
    "resolve_project_path",
    "selection_records",
    "completed_project_stages",
    "default_resume_stage",
    "build_reduction_project",
    "validate_reduction_project",
    "read_reduction_project",
    "write_reduction_project",
    "restore_project_selections",
]
