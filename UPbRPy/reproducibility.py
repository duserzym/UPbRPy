"""Reproducible notebook and publication-bundle exports for UPbRPy."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import datetime, timezone
import json
from uuid import uuid4
from pathlib import Path
from typing import Any

import pandas as pd

from .pipeline import ReductionPipeline
from .results import build_publication_tables, export_publication_tables
from .export_table import (
    ExportSettings, ExportTable, ExportColumn, build_export_table,
    write_export_xlsx, write_export_csv, write_export_markdown,
)


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


def selection_export_frame(experiment: pd.DataFrame) -> pd.DataFrame:
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
    return experiment.loc[:, columns].copy()


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
) -> dict[str, Any]:
    """Create a complete notebook representation of GUI reduction choices.

    Reviewed window coordinates are embedded directly in the notebook. The
    notebook therefore replays manual GUI edits rather than merely repeating
    the original auto-pick settings.
    """
    config_json = json.dumps(pipeline.config.to_dict(), indent=2)
    selections_json = selection_export_frame(pipeline.experiment).to_json(
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
            "from UPbRPy import (\n"
            "    ReductionPipeline,\n"
            "    build_publication_tables,\n"
            "    build_standard_archive,\n"
            "    load_standard_measurements,\n"
            "    pipeline_config_from_dict,\n"
            "    export_publication_tables,\n"
            "    read_experiment_data,\n"
            "    standard_archive,\n"
            "    standard_archive_legacy,\n"
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
            f"config = pipeline_config_from_dict(json.loads({config_json!r}))\n"
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
            "    if reference_library_path.suffix.lower() == '.csv':\n"
            "        archive = pd.read_csv(reference_library_path, index_col=0)\n"
            "    else:\n"
            "        archive = build_standard_archive(\n"
            "            load_standard_measurements(reference_library_path)\n"
            "        ).combine_first(standard_archive_legacy)\n"
            "else:\n"
            "    archive = standard_archive.combine_first(\n"
            "        standard_archive_legacy\n"
            "    )\n"
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
    formats: Iterable[str] = ("xlsx", "csv", "md"),
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
    if set(formats) - {"xlsx", "csv", "md"}:
        raise ValueError("formats must contain only xlsx, csv, md")
    settings = export_settings or (table.settings if table is not None else pipeline.config.export_settings) or ExportSettings()
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
        for fmt in formats:
            path = root / (stem + "." + fmt)
            written = {"xlsx": write_export_xlsx, "csv": write_export_csv, "md": write_export_markdown}[fmt](table, path)
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
    manifest = pipeline.manifest(hash_inputs=hash_inputs, metadata={"export_settings": settings.to_dict(),
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
    )
    workbook = root / "UPbRPy_publication_results.xlsx"
    try:
        files.append(export_publication_tables(tables, workbook))
        files.extend(sorted(workbook.with_suffix("").glob("*.csv")))
    except ImportError as exc:
        warnings.append(str(exc))
        for name, table in tables.items():
            target = root / f"{name.lower().replace(' ', '_')}.csv"
            table.to_csv(target, index=False)
            files.append(target)

    selections_path = root / "selection_windows.csv"
    selection_export_frame(pipeline.experiment).to_csv(
        selections_path,
        index=False,
    )
    files.append(selections_path)

    spot_path = root / "spot_mean_results.csv"
    pipeline.run_spot_means().to_csv(spot_path, index=False)
    files.append(spot_path)

    if include_depth_profile:
        depth_path = root / "depth_profile_results.csv"
        depth_profile.to_csv(depth_path, index=False)
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
        include_concentrations=include_concentrations,
        include_depth_concentrations=include_depth_concentrations,
    )
    files.append(notebook_path)
    return ExportBundle(root, tuple(files), tuple(warnings))


__all__ = [
    "SELECTION_EXPORT_COLUMNS",
    "ExportBundle",
    "selection_export_frame",
    "build_reproducibility_notebook",
    "write_reproducibility_notebook",
    "export_reduction_bundle",
]
