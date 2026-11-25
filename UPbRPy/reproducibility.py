"""Reproducible notebook and publication-bundle exports for UPbRPy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Iterable
from datetime import datetime, timezone
import json
from uuid import uuid4
from pathlib import Path
from typing import Any

import pandas as pd

from .pipeline import ReductionPipeline
from .spot_edits import spot_edit_metadata
from .results import build_publication_tables, export_publication_tables
from .export_table import (
    ExportSettings, ExportTable, ExportColumn, build_export_table,
    write_export_xlsx, write_export_csv, write_export_markdown,
    build_footnotes, _table_footnotes, update_export_settings, _laser_columns,
)
from .laser_exports import (
    laser_export_frame, pipeline_laser_export_data, laser_export_prefill,
    without_stage_coordinates, export_manifest,
)
from .supplement import SupplementSession, supplement_info_from_settings, write_supplement_xlsx


SELECTION_EXPORT_COLUMNS = (
    "spot_names",
    "baseline_start_times",
    "baseline_end_times",
    "plateau_start_times",
    "plateau_end_times",
    "signal_start_times",
    "signal_end_times",
    "selection_method",
    "selection_channel",
    "plateau_filter_method",
    "plateau_filter_channel",
)


@dataclass(frozen=True)
class ExportBundle:
    """Files and non-fatal warnings produced by a reduction export."""

    directory: Path
    files: tuple[Path, ...]
    warnings: tuple[str, ...] = ()


def selection_export_frame(experiment: pd.DataFrame, *, include_stage_coordinates=False) -> pd.DataFrame:
    """Return the reviewed selection decisions needed to replay a session."""
    columns = [
        column
        for column in SELECTION_EXPORT_COLUMNS
        if column in experiment.columns
    ]
    columns.extend(
        column for column in experiment.columns
        if column not in columns and str(column).startswith("selection_")
    )
    frame = experiment.loc[:, columns].copy()
    return frame if include_stage_coordinates else without_stage_coordinates(frame)


def notebook_code_cell(source: str) -> dict[str, Any]:
    """Build one unexecuted Jupyter code cell without requiring nbformat."""
    return {
        "cell_type": "code",
        "id": uuid4().hex,
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


def notebook_markdown_cell(source: str) -> dict[str, Any]:
    """Build one Jupyter Markdown cell without requiring nbformat."""
    return {
        "cell_type": "markdown",
        "id": uuid4().hex,
        "metadata": {},
        "source": source.splitlines(keepends=True),
    }


def build_reproducibility_notebook(
    pipeline: ReductionPipeline,
    *,
    title: str = "UPbRPy reproducible reduction",
    include_concentrations: bool = True,
    include_depth_concentrations: bool = True,
    export_settings: ExportSettings | None = None,
) -> dict[str, Any]:
    """Create a complete notebook representation of GUI reduction choices.

    Reviewed window coordinates are embedded directly in the notebook. The
    notebook therefore replays manual GUI edits rather than merely repeating
    the original auto-pick settings.
    """
    settings = export_settings or pipeline.config.export_settings or ExportSettings()
    data = pipeline_laser_export_data(pipeline)
    config_record = pipeline.config.to_dict()
    if not settings.include_stage_coordinates:
        config_record = without_stage_coordinates(config_record)
    config_json = json.dumps(config_record, indent=2)
    manifest = export_manifest(pipeline, settings, laser_data=data)
    edits_json = json.dumps(spot_edit_metadata(pipeline.experiment))
    selections_json = selection_export_frame(pipeline.experiment,
        include_stage_coordinates=settings.include_stage_coordinates).to_json(
        orient="records",
        double_precision=15,
    )
    source_directory = (
        ""
        if pipeline.source_directory is None
        else str(pipeline.source_directory)
    )
    reference_path = (
        ""
        if pipeline.reference_library_path is None
        else str(pipeline.reference_library_path)
    )
    generated = datetime.now(timezone.utc).isoformat()

    cells = [
        notebook_markdown_cell(
            f"# {title}\n\n"
            f"Generated from UPbRPy Studio at `{generated}`. This notebook "
            "contains the exact scientific settings and reviewed baseline, "
            "plateau, signal-start, and signal-end coordinates used by the "
            "GUI. Re-running it uses the same shared Python pipeline."
        ),
        notebook_code_cell(
            "from pathlib import Path\n"
            "import json\n"
            "import pandas as pd\n"
            "from IPython.display import display\n"
            "from UPbRPy.spot_edits import restore_spot_edits\n"
            "from UPbRPy import (\n"
            "    ReductionPipeline,\n"
            "    build_publication_tables,\n"
            "    build_standard_archive,\n"
            "    load_standard_measurements,\n"
            "    pipeline_config_from_dict,\n"
            "    export_publication_tables,\n"
            "    read_experiment_data,\n"
            "    standard_archive,\n"
            ")\n"
        ),
        notebook_code_cell(
            f"source_directory = Path({source_directory!r})\n"
            f"reference_library_text = {reference_path!r}\n"
            "reference_library_path = (\n"
            "    Path(reference_library_text)\n"
            "    if reference_library_text\n"
            "    else None\n"
            ")\n"
            f"config = pipeline_config_from_dict(json.loads({config_json!r}),\n"
            f"    saved_version={manifest['upbrpy_version']!r},\n"
            f"    saved_schema={manifest['settings_schema']!r})\n"
            "read_options = dict(config.selection_options)\n"
            "read_options.pop('primary_channel', None)\n"
            "read_options.setdefault('selection_method', config.selection_method)\n"
            "read_options['detector_calibration'] = config.detector_calibration\n"
            "experiment, channels, total_data = read_experiment_data(\n"
            "    source_directory,\n"
            "    primary_channel=config.primary_channel,\n"
            "    **read_options,\n"
            ")\n"
        ),
        notebook_markdown_cell(
            "## Restore reviewed selection windows\n\n"
            "The saved coordinates below supersede newly auto-picked values "
            "for matching spot names."
        ),
        notebook_code_cell(
            f"spot_edits = json.loads({edits_json!r})\n"
            "experiment, total_data = restore_spot_edits(experiment, total_data, spot_edits)\n"
        ),
        notebook_code_cell(
            f"selection_records = json.loads({selections_json!r})\n"
            "saved_selections = pd.DataFrame.from_records(selection_records)\n"
            "saved_selections = saved_selections.set_index('spot_names')\n"
            "experiment = experiment.set_index('spot_names')\n"
            "for column in saved_selections.columns:\n"
            "    experiment.loc[saved_selections.index, column] = saved_selections[column]\n"
            "experiment = experiment.reset_index()\n"
        ),
        notebook_code_cell(
            "if reference_library_path is not None:\n"
            "    if not reference_library_path.exists():\n"
            "        raise FileNotFoundError(reference_library_path)\n"
            "    archive = build_standard_archive(\n"
            "        load_standard_measurements(reference_library_path)\n"
            "    )\n"
            "else:\n"
            "    archive = standard_archive\n"
            "pipeline = ReductionPipeline(\n"
            "    experiment,\n"
            "    total_data,\n"
            "    channels,\n"
            "    archive,\n"
            "    config,\n"
            "    source_directory=source_directory,\n"
            "    reference_library_path=reference_library_path,\n"
            "    input_files=tuple(Path(source['path']) for source in\n"
            "        experiment.attrs.get('qtegra', {}).get('sources', [])),\n"
            ")\n"
        ),
        notebook_markdown_cell("## Baseline, normalization, and concentration"),
        notebook_code_cell(
            "baseline_subtracted = pipeline.run_baseline()\n"
            "normalized_plateau = pipeline.run_normalization()\n"
            "concentration_results = pipeline.run_concentration()\n"
            "display(concentration_results['final_concentration_df'].head())\n"
        ),
        notebook_markdown_cell("## Isotope ratios and fractionation corrections"),
        notebook_code_cell(
            "raw_ratios = pipeline.run_ratios()\n"
            "downhole_results = pipeline.run_downhole()\n"
            "for ratio_result in downhole_results['correction_results'].values():\n"
            "    display(ratio_result['plot'])\n"
            "session_results = pipeline.run_session()\n"
            "for ratio_result in session_results['correction_results'].values():\n"
            "    display(ratio_result['plot'])\n"
        ),
        notebook_markdown_cell(
            "## Spot means, depth profiles, common Pb, and decisions"
        ),
        notebook_code_cell(
            "spot_means = pipeline.run_spot_means()\n"
            "depth_profile = pipeline.run_depth_profile()\n"
            "finalized = pipeline.run_finalize()\n"
            "display(finalized.selected_results)\n"
            "display(finalized.secondary_standard_summary)\n"
        ),
        notebook_markdown_cell(
            "## Horstwood-style publication export\n\n"
            "This writes the concise result, Horstwood Table 4-style, depth, "
            "QC, methods, and full audit worksheets. Set either concentration "
            "option to `False` when dates should be exported alone."
        ),
        notebook_code_cell(
            "manifest = pipeline.manifest(hash_inputs=False)\n"
            "publication_tables = build_publication_tables(\n"
            "    finalized,\n"
            "    metadata={\n"
            "        'workflow_key': config.workflow_key,\n"
            "        'package_version': manifest['upbrpy_version'],\n"
            "        'pipeline_format_version': manifest['format_version'],\n"
            "        'detector_calibration': manifest['detector_calibration'],\n"
            "        'source_directory': str(source_directory),\n"
            "        'reference_library_path': str(reference_library_path or ''),\n"
            "    },\n"
            "    depth_profile=depth_profile,\n"
            f"    include_concentrations={bool(include_concentrations)!r},\n"
            f"    include_depth_concentrations={bool(include_depth_concentrations)!r},\n"
            ")\n"
            "publication_workbook = export_publication_tables(\n"
            "    publication_tables,\n"
            "    source_directory.parent / f'{source_directory.name}_Table4.xlsx',\n"
            ")\n"
            "publication_workbook\n"
        ),
        notebook_markdown_cell("## Saved provenance"),
        notebook_code_cell(
            "display(pd.json_normalize(manifest, sep='.').T)\n"
        ),
    ]
    if data.source:
        from dataclasses import asdict
        payload = asdict(data)
        if not settings.include_stage_coordinates:
            payload = without_stage_coordinates(payload)
        payload_json = json.dumps(payload, ensure_ascii=False)
        laser_json = json.dumps(manifest["laser"], ensure_ascii=False)
        settings_json = json.dumps(settings.to_dict(), ensure_ascii=False)
        cells.insert(1, notebook_code_cell(
            "from UPbRPy.laser_exports import LaserExportData\n"
            "from UPbRPy.export_table import ExportSettings\n"
            f"laser_export_data = LaserExportData(**json.loads({payload_json!r}))\n"
            f"export_settings = ExportSettings.from_dict(json.loads({settings_json!r}))\n"
        ))
        # Import json before the laser cell, while keeping original cell order.
        cells[1]["source"].insert(0, "import json\n")
        publication = next(cell for cell in cells if "manifest = pipeline.manifest" in "".join(cell["source"]))
        code = "".join(publication["source"])
        code = code.replace("publication_tables = build_publication_tables(",
            f"manifest['laser'] = json.loads({laser_json!r})\npublication_tables = build_publication_tables(")
        code = code.replace("    finalized,", "    finalized,\n    export_settings=export_settings, laser_data=laser_export_data,")
        code = code.replace("    publication_tables,", "    publication_tables,\n    export_settings=export_settings,")
        code = code.replace("    source_directory.parent /", "    output_path=source_directory.parent /")
        publication["source"] = code.splitlines(keepends=True)
        # Standalone ties must be available before the import/selection call.
        tie = getattr(pipeline, "laser_tie", None)
        if tie is not None:
            tie_record = tie.to_dict()
            if not settings.include_stage_coordinates:
                tie_record = without_stage_coordinates(tie_record)
            tie_json = json.dumps(tie_record, ensure_ascii=False)
            cells[1]["source"] += [
                "from UPbRPy.laser_tie import LaserTie, LaserTieRow\n",
                f"laser_tie_record = json.loads({tie_json!r})\n",
                "laser_tie_record['rows'] = {label: LaserTieRow(**row) for label, row in laser_tie_record['rows'].items()}\n",
                "laser_tie = LaserTie(**laser_tie_record)\n",
            ]
            read_cell = next(cell for cell in cells if "read_options = dict" in "".join(cell["source"]))
            code = "".join(read_cell["source"]).replace("    **read_options,",
                "    laser_tie=laser_tie, laser_tie_confirmed=True,\n    **read_options,")
            read_cell["source"] = code.splitlines(keepends=True)
        pipeline_cell = next(cell for cell in cells if "pipeline = ReductionPipeline(" in "".join(cell["source"]))
        pipeline_cell["source"].append("pipeline.laser_export_data = laser_export_data\n")
        if tie is not None:
            pipeline_cell["source"].append("pipeline.laser_tie = laser_tie\n")
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "pygments_lexer": "ipython3"},
            "upbrpy": {
                "pipeline_format_version": pipeline.manifest()[
                    "format_version"
                ]
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_reproducibility_notebook(
    pipeline: ReductionPipeline,
    output_path: str | Path,
    *,
    include_concentrations: bool = True,
    include_depth_concentrations: bool = True,
    export_settings: ExportSettings | None = None,
) -> Path:
    """Write a standards-compliant, unexecuted Jupyter notebook."""
    target = Path(output_path)
    if target.suffix.lower() != ".ipynb":
        target = target.with_suffix(".ipynb")
    target.parent.mkdir(parents=True, exist_ok=True)
    notebook = build_reproducibility_notebook(
        pipeline,
        include_concentrations=include_concentrations,
        include_depth_concentrations=include_depth_concentrations,
        export_settings=export_settings,
    )
    target.write_text(
        json.dumps(notebook, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def export_reduction_bundle(
    pipeline: ReductionPipeline,
    output_directory: str | Path,
    *,
    include_depth_profile: bool = True,
    hash_inputs: bool = True,
    include_concentrations: bool = True,
    include_depth_concentrations: bool = True,
    export_settings: ExportSettings | None = None,
    formats: Iterable[str] = ("xlsx", "csv", "md", "si"),
    excluded_spots: Iterable[str] = (),
    file_stem: str | None = None,
    include_audit_files: bool = True,
    table: ExportTable | None = None,
    isochron_fits: Iterable[dict] = (),
) -> ExportBundle:
    """Export publication tables, audit data, provenance, and a notebook.

    ``isochron_fits`` are records from :func:`UPbRPy.regression.fit_record`
    (with Studio's ``label``, ``view`` and ``items`` keys); when given they are
    written as ``<stem>_isochron_fits.csv``/``.md`` with footnotes and stored
    in the manifest metadata.

    Input files are hashed by default (``hash_inputs=True``): the SHA-256 of
    every raw CSV is what lets a database match the bundle to the session it
    came from (``upblab/docs/WORKFLOW.md`` section 3.3).
    """
    formats = tuple(dict.fromkeys(formats))
    excluded_spots = tuple(excluded_spots)
    if set(formats) - {"xlsx", "csv", "md", "si"}:
        raise ValueError("formats must contain only xlsx, csv, md, si")
    settings = export_settings or (table.settings if table is not None else pipeline.config.export_settings) or ExportSettings()
    laser_data = pipeline_laser_export_data(pipeline)
    if laser_data.source:
        labels = pipeline.run_finalize().parallel_results.get("spot_name", pd.Series(dtype=str))
        prefill, _ = laser_export_prefill(laser_data, labels, existing=settings.to_dict())
        settings = replace(settings, **prefill)
    stem = file_stem or f"{Path(pipeline.source_directory).stem if pipeline.source_directory else 'session'}_results_table"
    if not stem.strip() or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("file_stem must be a single file name")
    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    warnings: list[str] = []

    finalized = pipeline.run_finalize()
    if formats and (table is not None or not finalized.parallel_results.empty):
        if table is None:
            if "preferred_date" in finalized.parallel_results:
                table = build_export_table(pipeline, settings, excluded_spots=excluded_spots)
            else:
                # Older cached audit frames predate the publication quantity
                # model. Preserve export compatibility without inventing dates.
                frame = finalized.parallel_results.copy()
                frame["role"], frame["notes"] = "unknown", ""
                frame["excluded"] = frame.spot_name.isin(tuple(excluded_spots))
                frame.loc[frame.excluded, "notes"] = "excluded"
                columns = (ExportColumn("spot_name", "Analysis", "Analysis"),
                           ExportColumn("notes", "Analysis", "Notes"))
                table = ExportTable(frame, columns, pd.DataFrame(), pd.DataFrame(), settings,
                    {"session_name": stem.removesuffix("_results_table"), "legacy_audit": True})
        if table.settings != settings and not table.context.get("legacy_audit"):
            table = update_export_settings(table, settings)
        if table.context.get("legacy_audit"):
            table = replace(table, settings=settings, context=dict(table.context))
            table.rows, table.columns = _laser_columns(table.rows, table.columns, laser_data, settings)
            if laser_data.source:
                table.context["laser_export_data"] = laser_data
        supplement = None
        if "xlsx" in formats or "si" in formats:
            notes = _table_footnotes(table) if table.context.get("legacy_audit") else build_footnotes(table, pipeline)
            supplement = SupplementSession(table, tuple(notes))
        for fmt in formats:
            if fmt == "si":
                files.append(write_supplement_xlsx([supplement],
                    root / (stem + "_SI.xlsx"), supplement_info_from_settings(settings)))
                continue
            path = root / (stem + "." + fmt)
            if fmt == "xlsx":
                written = write_export_xlsx(table, path, supplement=supplement)
            else:
                written = {"csv": write_export_csv, "md": write_export_markdown}[fmt](table, path)
            files.extend(written if isinstance(written, list) else [written])
    isochron_fits = [dict(record) for record in isochron_fits]
    if isochron_fits:
        from .regression import write_isochron_fits

        files.extend(write_isochron_fits(isochron_fits, root, f"{stem}_isochron_fits"))
    if not include_audit_files:
        return ExportBundle(root, tuple(files), tuple(warnings))
    depth_profile = (
        pipeline.run_depth_profile() if include_depth_profile else None
    )
    manifest = export_manifest(pipeline, settings, laser_data=laser_data, hash_inputs=hash_inputs, metadata={"export_settings": settings.to_dict(),
        "excluded_spots": list(excluded_spots), "isochron_fits": isochron_fits})
    tables = build_publication_tables(
        finalized,
        metadata={
            "workflow_key": pipeline.config.workflow_key,
            "package_version": manifest["upbrpy_version"],
            "pipeline_format_version": manifest["format_version"],
            "detector_calibration": manifest["detector_calibration"],
            "source_directory": str(pipeline.source_directory or ""),
            "reference_library_path": str(
                pipeline.reference_library_path or ""
            ),
        },
        depth_profile=depth_profile,
        include_concentrations=include_concentrations,
        include_depth_concentrations=include_depth_concentrations,
        export_settings=settings, laser_data=laser_data,
    )
    workbook = root / "UPbRPy_publication_results.xlsx"
    try:
        files.append(export_publication_tables(tables, workbook, export_settings=settings))
        files.extend(sorted(workbook.with_suffix("").glob("*.csv")))
    except ImportError as exc:
        warnings.append(str(exc))
        for name, table in tables.items():
            target = root / f"{name.lower().replace(' ', '_')}.csv"
            table.to_csv(target, index=False)
            files.append(target)

    selections_path = root / "selection_windows.csv"
    selection_export_frame(pipeline.experiment,
        include_stage_coordinates=settings.include_stage_coordinates).to_csv(
        selections_path,
        index=False,
    )
    files.append(selections_path)

    spot_path = root / "spot_mean_results.csv"
    laser_export_frame(pipeline.run_spot_means(), laser_data,
        include_stage_coordinates=settings.include_stage_coordinates, headings=True).to_csv(spot_path, index=False)
    files.append(spot_path)

    if include_depth_profile:
        depth_path = root / "depth_profile_results.csv"
        depth_export = depth_profile if settings.include_stage_coordinates else without_stage_coordinates(depth_profile)
        depth_export.to_csv(depth_path, index=False)
        files.append(depth_path)

    manifest_path = root / "reduction_manifest.json"
    manifest_path.write_text(
        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    files.append(manifest_path)

    notebook_path = write_reproducibility_notebook(
        pipeline,
        root / "UPbRPy_reproducible_workflow.ipynb",
        export_settings=settings,
        include_concentrations=include_concentrations,
        include_depth_concentrations=include_depth_concentrations,
    )
    files.append(notebook_path)
    return ExportBundle(root, tuple(files), tuple(warnings))


def write_export_project(pipeline, output_path, *, export_settings=None, **project_options):
    """Write an export copy of a project, applying the coordinate opt-in."""
    from .project import write_reduction_project
    settings = export_settings or getattr(getattr(pipeline, "config", None), "export_settings", None) or ExportSettings()
    return write_reduction_project(pipeline, output_path,
        include_stage_coordinates=settings.include_stage_coordinates, **project_options)


__all__ = [
    "SELECTION_EXPORT_COLUMNS",
    "ExportBundle",
    "selection_export_frame",
    "build_reproducibility_notebook",
    "write_reproducibility_notebook",
    "export_reduction_bundle",
    "write_export_project",
]
