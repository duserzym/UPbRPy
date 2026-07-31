"""Lightweight, replayable UPbRPy reduction project files.

A ``.upbr`` project records paths, scientific settings, reviewed selection
windows, and small GUI workspace choices.  It intentionally does not embed raw
measurements or calculated tables.  Opening the file therefore re-imports the
source data and recalculates the stages that had been completed when the
project was saved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from .pipeline import PipelineStage, ReductionPipeline
from .reproducibility import selection_export_frame


PROJECT_FILE_EXTENSION = ".upbr"
PROJECT_FORMAT_NAME = "UPbRPy reduction project"
PROJECT_FORMAT_VERSION = 1


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
) -> dict[str, object]:
    """Build the lightweight document written inside a ``.upbr`` file."""
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
    return {
        "format": PROJECT_FORMAT_NAME,
        "format_version": PROJECT_FORMAT_VERSION,
        "saved_utc": datetime.now(timezone.utc).isoformat(),
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
            "config": pipeline.config.to_dict(),
            "completed_stages": [
                stage.value for stage in pipeline.cache.stages()
            ],
            "resume_stage": resumed.value,
        },
        "selections": selection_records(pipeline.experiment),
        "workspace": json_safe_value(dict(workspace or {})),
    }


def validate_reduction_project(
    document: Mapping[str, object],
) -> dict[str, object]:
    """Validate a parsed project and return an ordinary mutable dictionary."""
    if document.get("format") != PROJECT_FORMAT_NAME:
        raise ValueError("This file is not an UPbRPy reduction project.")
    version = int(document.get("format_version", 0))
    if version > PROJECT_FORMAT_VERSION:
        raise ValueError(
            f"Project format {version} is newer than this UPbRPy build "
            f"(supports through {PROJECT_FORMAT_VERSION})."
        )
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
    if not isinstance(selections, list):
        raise ValueError("Project selection records are missing or invalid.")
    if not isinstance(workspace, Mapping):
        raise ValueError("Project workspace settings are invalid.")

    stage_values = pipeline.get("completed_stages", ())
    if not isinstance(stage_values, (list, tuple)):
        raise ValueError("Project completed stages must be a list.")
    completed_project_stages(stage_values)
    PipelineStage(pipeline.get("resume_stage", PipelineStage.IMPORT.value))
    return dict(document)


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
    )
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
    return restored


__all__ = [
    "PROJECT_FILE_EXTENSION",
    "PROJECT_FORMAT_NAME",
    "PROJECT_FORMAT_VERSION",
    "json_safe_value",
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
