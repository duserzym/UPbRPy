from __future__ import annotations

import re
import weakref
from copy import deepcopy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from GUI.state import ReductionSession
from GUI.workflows import (
    WorkflowPreset,
    format_import_defaults,
    infer_label_separator,
    get_workflow_preset,
    infer_workflow_preset,
    resolve_reference_standards,
)
from UPbRPy.calibration import evaluate_correction_result
from UPbRPy.concentration import resolve_element_concentration
from UPbRPy.export_table import ExportSettings, build_export_table
from UPbRPy.io import read_experiment_data, resolve_session_manifest, autopick_signal_regions
from UPbRPy.qtegra import QtegraSession, open_qtegra
from UPbRPy.spot_edits import restore_spot_edits, spot_edit_metadata
from UPbRPy.session_manifest import find_session_manifest, raw_directory_for
from UPbRPy.reproducibility import selection_export_frame
from UPbRPy.pipeline import (
    FitSettings,
    PipelineConfig,
    PipelineStage,
    ReductionPipeline,
    pipeline_config_from_dict,
    config_invalidation_stage,
)
from UPbRPy.project import (
    completed_project_stages,
    project_origin,
    read_reduction_project,
    resolve_project_path,
    restore_project_selections,
)
from UPbRPy.results import ReductionDecision
from UPbRPy.standards import (
    build_standard_archive,
    load_standard_measurements,
    standard_archive,
)
from UPbRPy.uncertainty import UncertaintySettings, inverse_standard_deviation_weights


DEFAULT_REFERENCE_LIBRARY_PATH = str(files("UPbRPy") / "reference_materials")
_active_standard_archive = standard_archive
_active_standard_archive_path = DEFAULT_REFERENCE_LIBRARY_PATH


FIT_METHOD_LABELS: dict[str, str] = {
    "mean": "Pointwise mean",
    "interpolated": "Interpolated",
    "linear": "Linear",
    "exponential": "Exponential",
    "cubic_spline": "Cubic spline",
    "running_mean": "Running mean",
    "polynomial": "Polynomial",
}

DOWNHOLE_FIT_METHODS: tuple[str, ...] = (
    "mean",
    "interpolated",
    "linear",
    "exponential",
    "cubic_spline",
    "running_mean",
    "polynomial",
)

SESSION_FIT_METHODS: tuple[str, ...] = (
    "mean",
    "interpolated",
    "linear",
    "exponential",
    "cubic_spline",
    "running_mean",
    "polynomial",
)


@dataclass(frozen=True)
class RestoredProject:
    """A fully recalculated GUI session and its saved workspace context."""

    session: ReductionSession
    preset: WorkflowPreset
    project_path: Path
    reference_library_path: Path | None
    resume_stage: PipelineStage
    completed_stages: tuple[PipelineStage, ...]
    workspace: dict[str, object]


def fit_method_label(method: str) -> str:
    return FIT_METHOD_LABELS.get(method, method.replace("_", " ").title())


def session_fit_weights(
    fractions: np.ndarray,
    stds: np.ndarray,
) -> np.ndarray:
    """Return core inverse-standard-deviation weights for session fitting.

    ``fractions`` is retained in the adapter signature because older GUI
    callers supplied it, although weighting depends only on the reported
    standard deviations.
    """
    del fractions
    return inverse_standard_deviation_weights(stds)


# Temporary compatibility for third-party code that imported the old GUI
# helper. New code should use ``session_fit_weights`` or the core function.
_session_fit_weights = session_fit_weights


def core_fit_method(method: str) -> str:
    """Translate legacy GUI labels to shared calibration-model names."""
    return {
        "interpolated": "interpolate",
        "cubic_spline": "spline",
    }.get(method, method)


def gui_fit_method(method: object) -> str:
    """Translate a saved core method name back to its GUI control value."""
    return {
        "interpolate": "interpolated",
        "spline": "cubic_spline",
    }.get(str(method), str(method))


def core_fit_options(
    options: Mapping[str, float | int | str],
) -> dict[str, object]:
    """Translate GUI control names to shared calibration-model options."""
    translated: dict[str, object] = {}
    if "polynomial_degree" in options:
        translated["degree"] = int(options["polynomial_degree"])
    if "running_mean_window" in options:
        translated["running_mean_window"] = int(
            options["running_mean_window"]
        )
    if "exponential_initial_decay" in options:
        translated["exponential_initial_decay"] = float(
            options["exponential_initial_decay"]
        )
    return translated


def gui_fit_options(
    options: Mapping[str, object],
    *,
    exponential_default: float,
) -> dict[str, float | int | str]:
    """Translate saved core model options back to editable GUI fields."""
    return {
        "running_mean_window": int(
            options.get("running_mean_window", 5)
        ),
        "polynomial_degree": int(options.get("degree", 2)),
        "exponential_initial_decay": float(
            options.get(
                "exponential_initial_decay",
                exponential_default,
            )
        ),
    }


def pipeline_config_from_session(
    session: ReductionSession,
    preset: WorkflowPreset,
) -> PipelineConfig:
    """Build the shared scientific configuration represented by GUI controls."""
    retained_config = (
        session.pipeline.config
        if session.pipeline is not None
        else PipelineConfig()
    )
    selection_options = dict(session.import_settings)
    # These values have dedicated PipelineConfig fields and must not be
    # duplicated inside method-specific keyword arguments.
    selection_options.pop("primary_channel", None)
    selection_options.pop("selection_method", None)
    detector_calibration = selection_options.pop(
        "detector_calibration", retained_config.detector_calibration,
    )
    if isinstance(detector_calibration, str):
        detector_calibration = {"mode": detector_calibration}
    common_pb_options = dict(retained_config.common_pb_options)
    if session.common_pb_uncertainty_preset_name == "none":
        common_pb_options.pop("common_pb_uncertainty_preset_name", None)
    else:
        common_pb_options["common_pb_uncertainty_preset_name"] = (
            session.common_pb_uncertainty_preset_name
        )
    return PipelineConfig(
        workflow_key=preset.key,
        material=preset.internal_material,
        primary_channel=session.primary_channel,
        internal_standard_channel=session.internal_standard_channel,
        reference_overrides=deepcopy(session.reference_overrides),
        selection_method=str(
            session.import_settings.get(
                "selection_method",
                preset.read_kwargs.get("selection_method", "threshold"),
            )
        ),
        selection_options=selection_options,
        detector_calibration=dict(detector_calibration or {}),
        concentration_standards=(
            session.concentration_standards
            or preset.concentration_standards
        ),
        primary_68_standard=(
            session.primary_68_standard or preset.primary_standard
        ),
        primary_76_standard=(
            session.primary_76_standard
            or preset.pb_ratio_standard
            or preset.primary_standard
        ),
        primary_82_standard=(
            session.primary_82_standard
            or preset.th_ratio_standard
            or preset.pb_ratio_standard
            or preset.primary_standard
        ),
        secondary_standards=(
            session.secondary_standards or preset.secondary_standards
        ),
        ratio_names=retained_config.ratio_names,
        ratio_aggregation=session.ratio_aggregation,
        common_pb_calibration=preset.common_pb_calibration,
        downhole=FitSettings(
            method=core_fit_method(session.downhole_fit_method),
            options=core_fit_options(session.downhole_fit_options),
            outlier_sigma=session.downhole_outlier_sigma,
        ),
        session=FitSettings(
            method=core_fit_method(session.session_fit_method),
            options=core_fit_options(session.session_fit_options),
        ),
        decision=ReductionDecision(
            common_pb_method=session.common_pb_method,
            include_calibration_uncertainty=(
                session.include_calibration_uncertainty
            ),
            age_transition_ma=session.age_transition_ma,
        ),
        automatic_flags=retained_config.automatic_flags,
        depth_calibration=retained_config.depth_calibration,
        export_settings=retained_config.export_settings,
        common_pb_options=common_pb_options,
        uncertainty=UncertaintySettings(
            propagate_baseline=True,
            propagate_decay_constants=session.propagate_decay_constants,
            propagate_u238_u235=session.propagate_u238_u235,
            long_term_reproducibility_mode=(
                session.long_term_reproducibility_mode
            ),
            long_term_reproducibility_relative_1s=(
                session.long_term_reproducibility_relative_1s
            ),
        ),
    )


def raw_input_files(directory: str | Path | QtegraSession) -> tuple[Path, ...]:
    """Return source files from a retained session or header-only discovery.

    Loaded Studio sessions use their exact sources without reading files again.
    Path callers retain the header-only discovery used before Qtegra sessions
    were retained by the GUI.
    """
    if isinstance(directory, QtegraSession):
        return tuple(source.path for source in directory.sources)
    from UPbRPy.qtegra import detect_format

    source = Path(directory).expanduser()
    if source.is_file():
        return (source,)
    source = raw_directory_for(source)

    def csv_formats(folder):
        formats = {}
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() == ".csv":
                try:
                    formats[path] = detect_format(path)
                except ValueError:
                    pass
        return formats

    formats = csv_formats(source)
    files = [path for path, kind in formats.items() if kind in {"rich", "spot"}]
    if "rich" in formats.values():
        for child in source.iterdir():
            if child.is_dir():
                files.extend(
                    path for path, kind in csv_formats(child).items() if kind == "spot"
                )
    return tuple(sorted(files))


_REDUCTION_CACHE_STAGES = {
    "baseline_subtracted_total_data": PipelineStage.BASELINE,
    "normalized_plateau_data": PipelineStage.NORMALIZATION,
    "concentration_results": PipelineStage.CONCENTRATION,
    "plateau_data_with_raw_ratios": PipelineStage.RATIOS,
    "signal_data_with_raw_ratios": PipelineStage.RATIOS,
    "downhole_results": PipelineStage.DOWNHOLE,
    "session_correction_results": PipelineStage.SESSION,
    "spot_mean_results": PipelineStage.SPOT_MEANS,
    "depth_profile_results": PipelineStage.DEPTH_PROFILE,
    "finalized_results": PipelineStage.FINALIZE,
    "results_pb204_source": PipelineStage.FINALIZE,
    "results_pb204": PipelineStage.FINALIZE,
}


def ensure_shared_pipeline(
    session: ReductionSession,
    preset: WorkflowPreset,
    *,
    source_experiment_unchanged: bool = False,
) -> ReductionPipeline:
    """Synchronize GUI choices; skip source hashing only for a verified snapshot.

    ``source_experiment_unchanged`` requires identity and exact contents to
    have been checked against the source synchronized by a stage worker.
    """
    config = pipeline_config_from_session(session, preset)
    if session.pipeline is None:
        input_files = (
            raw_input_files(session.qtegra or session.directory)
            if session.qtegra is not None or (
                session.directory and Path(session.directory).exists()
            )
            else ()
        )
        session.pipeline = ReductionPipeline(
            session.experiment,
            session.total_data,
            session.channels,
            current_standard_archive(),
            config,
            source_directory=session.directory or None,
            reference_library_path=current_standard_archive_path(),
            input_files=input_files,
            build_diagnostic_plots=False,
            origin=session.origin,
        )
    else:
        pipeline = session.pipeline
        pipeline.origin = None if session.origin is None else dict(session.origin)
        if pipeline.config != config:
            pipeline.update_config(
                config,
                invalidate_from=config_invalidation_stage(
                    pipeline.config,
                    config,
                ),
            )
        verified_source = (source_experiment_unchanged
                           and pipeline._source_experiment is session.experiment)
        if getattr(pipeline, "_source_total_data", None) is not session.total_data:
            pipeline.update_inputs(session.experiment, session.total_data)
        elif not verified_source and not pipeline.matches_source_experiment(session.experiment):
            pipeline.update_experiment(session.experiment)
        if pipeline.base_standard_archive is not current_standard_archive():
            pipeline.standard_archive = current_standard_archive()
            pipeline.reference_library_path = Path(
                current_standard_archive_path()
            )
            pipeline.cache.invalidate(PipelineStage.NORMALIZATION)
    # Widgets still consume these compatibility keys directly. Drop products
    # invalidated by selection, settings, or reference-library changes.
    for key, stage in _REDUCTION_CACHE_STAGES.items():
        if not session.pipeline.cache.has(stage):
            session.reduction_cache.pop(key, None)
    return session.pipeline


def pipeline_for_database_submission(session, preset):
    """Use raw acquisition labels in database result tables and keep UI provenance.

    Recalculate a private pipeline only when names differ. This adds no network
    calls and leaves the session, regular exports and saved project untouched.
    """
    pipeline = ensure_shared_pipeline(session, preset)
    renames = session.experiment.attrs.get("spot_renames", {})
    if not renames:
        return pipeline
    experiment = session.experiment.copy(deep=True)
    total_data = session.total_data.copy()
    experiment["spot_names"] = experiment["spot_names"].replace(renames)
    total_data["spot_name"] = total_data["spot_name"].replace(renames)
    # The submitted .upbr carries UI renames. This private pipeline already
    # uses raw identities, so its replay notebook must not rename them again.
    experiment.attrs["spot_renames"] = {}
    experiment.attrs["sample_overrides"] = {
        renames.get(name, name): value for name, value in experiment.attrs.get("sample_overrides", {}).items()}
    experiment.attrs["calculation_excluded_spots"] = [renames.get(name, name) for name in experiment.attrs.get("calculation_excluded_spots", [])]
    return ReductionPipeline(
        experiment, total_data, session.channels, pipeline.base_standard_archive, pipeline.config,
        source_directory=pipeline.source_directory, reference_library_path=pipeline.reference_library_path,
        input_files=pipeline.input_files, build_diagnostic_plots=False, origin=pipeline.origin,
    )


# ── Isotope channel parsing ────────────────────────────────────────────────

_MASS_RE_SVC = re.compile(r'^(\d+)\s*([A-Za-z]+)$')


def _parse_channel_name(name: str) -> tuple[str, str] | None:
    """Return (Element_Capitalized, mass_str) for '238U' or 'U238', else None."""
    s = name.strip()
    m = _MASS_RE_SVC.match(s)
    if m:
        return m.group(2).capitalize(), m.group(1)
    m2 = re.match(r'^([A-Za-z]+)\s*(\d+)$', s)
    if m2:
        return m2.group(1).capitalize(), m2.group(2)
    return None


# ── Discrete element colour palette (tab10-inspired) ─────────────────────────
#
# Two variants optimised for dark and light backgrounds.  All isotopes of
# the same element share the same base colour; visual distinction between
# isotopes is provided by the chip border-style in the UI widget.

_PALETTE_DARK: list[str] = [
    "#5590f5",  # 0  vivid blue       U
    "#f5c040",  # 1  gold             Pb
    "#f07840",  # 2  orange           Th
    "#b864f0",  # 3  violet           Hf
    "#40c870",  # 4  green            Si
    "#d04848",  # 5  red              Fe
    "#40c8d8",  # 6  cyan             Ti / Zr
    "#f060a8",  # 7  hot-pink         Sr / Na
    "#b8d840",  # 8  lime             Ca / Al
    "#f0a040",  # 9  amber            Mn / P
    "#7090d8",  # 10 steel-blue       Nb / Ta
    "#d090f0",  # 11 lavender         REE / misc
]

_PALETTE_LIGHT: list[str] = [
    "#1040c0",  # 0 deep blue
    "#806010",  # 1 dark gold
    "#a04010",  # 2 burnt orange
    "#5810a0",  # 3 deep purple
    "#0a6030",  # 4 forest green
    "#900000",  # 5 crimson
    "#0a6878",  # 6 dark teal
    "#880860",  # 7 deep magenta
    "#506818",  # 8 olive
    "#804808",  # 9 dark amber
    "#183878",  # 10 dark steel-blue
    "#600878",  # 11 dark lavender
]

# Fixed palette-index per known element.  Unrecognised elements are assigned
# indices dynamically by cycling through the palette.
_ELEM_PALETTE_IDX: dict[str, int] = {
    # Geochronology – must be maximally distinct
    "U": 0, "Pb": 1, "Th": 2, "Hf": 3,
    # Internal standards
    "Si": 4, "Ca": 8, "Al": 8, "Mg": 4,
    "Ti": 6, "Fe": 5, "Mn": 9, "P": 9, "Na": 7, "K": 7,
    # REE + Y: cycle 4-11 to stay clear of geochronology colours (0-3)
    "Y": 4, "La": 5, "Ce": 6, "Pr": 7, "Nd": 8, "Sm": 9, "Eu": 10,
    "Gd": 11, "Tb": 4, "Dy": 5, "Ho": 6, "Er": 7, "Tm": 8, "Yb": 9, "Lu": 10,
    # HFSE
    "Zr": 6, "Nb": 10, "Ta": 10,
    # Alkali / alkaline earth
    "Sr": 7, "Ba": 9, "Rb": 2, "Li": 11, "Cs": 5,
    # Others
    "Hg": 6, "Bi": 3,
}

# Channel section definitions for the isotope grid UI.
# Each entry is (label, ordered_element_list | None for catch-all).
CHANNEL_SECTIONS: list[tuple[str, list[str] | None]] = [
    ("Geochronology",
     ["U", "Pb", "Th", "Hf"]),
    ("Internal Standards",
     ["Si", "Ca", "Al", "Ti", "Mg", "Fe", "Mn", "P", "Na", "K"]),
    ("REE + Y",
     ["Y", "La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd",
      "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu"]),
    ("HFSE",
     ["Zr", "Nb", "Ta"]),
    ("Alkali / Alkaline Earth",
     ["Sr", "Ba", "Rb", "Na", "K", "Li", "Cs"]),
    ("Other",
     None),
]

# Regex for stripping trailing spot-number suffixes.
_SPOT_SUFFIX_SEP = re.compile(r'[\s\-_\.]+\d+[a-zA-Z]{0,2}$')
_SPOT_SUFFIX_NOSEP = re.compile(r'(?<=[a-zA-Z])\d+[a-zA-Z]{0,1}$')


def spot_base_name(name: str) -> str:
    """Strip trailing spot-number suffixes: 'GJ1_001' -> 'GJ1'."""
    s = name.strip()
    stripped = _SPOT_SUFFIX_SEP.sub('', s)
    if stripped and stripped != s:
        return stripped.strip()
    stripped = _SPOT_SUFFIX_NOSEP.sub('', s)
    if stripped and stripped != s:
        return stripped.strip()
    return s


def default_visible_channels(channels: Iterable[str], primary_channel: str) -> list[str]:
    """Choose a compact, geochronologically useful initial trace set."""
    available = [str(channel) for channel in channels]
    priority = [
        primary_channel,
        "206Pb",
        "207Pb",
        "238U",
        "208Pb",
        "232Th",
    ]
    ordered = [
        channel
        for channel in priority
        if channel in available
    ]
    ordered.extend(
        channel for channel in available if channel not in ordered
    )
    return ordered[: min(4, len(ordered))]


def build_channel_colors(
    channels: Iterable[str],
    theme_name: str = "dark",
) -> dict[str, str]:
    """Assign discrete palette colours to channels.

    All isotopes of the same element receive the same base colour.  Visual
    distinction between isotopes is provided by the chip border-style in the
    IsotopeGridWidget.
    """
    palette = _PALETTE_DARK if (theme_name == "dark") else _PALETTE_LIGHT
    channel_list = [str(ch) for ch in channels]

    # Dynamic assignment for elements not in the fixed map.
    _dyn_color: dict[str, str] = {}
    _dyn_idx: list[int] = [0]

    def _elem_color(elem: str) -> str:
        if elem in _ELEM_PALETTE_IDX:
            return palette[_ELEM_PALETTE_IDX[elem] % len(palette)]
        if elem not in _dyn_color:
            _dyn_color[elem] = palette[_dyn_idx[0] % len(palette)]
            _dyn_idx[0] += 1
        return _dyn_color[elem]

    colors: dict[str, str] = {}
    unparsed_idx = 0
    for ch in channel_list:
        parsed = _parse_channel_name(ch)
        if parsed:
            elem, _ = parsed
            colors[ch] = _elem_color(elem)
        else:
            # Unnamed channels: assign from the end of the palette backwards.
            colors[ch] = palette[(len(palette) - 1 - unparsed_idx) % len(palette)]
            unparsed_idx += 1

    return colors


def default_reference_material_library_path() -> str:
    return DEFAULT_REFERENCE_LIBRARY_PATH


def current_standard_archive() -> pd.DataFrame:
    return _active_standard_archive


def current_standard_archive_path() -> str:
    return _active_standard_archive_path


def load_standard_archive_from_path(path: str | None) -> pd.DataFrame:
    """Load a TOML library directory or manifest."""
    global _active_standard_archive
    global _active_standard_archive_path

    target_path = Path(path).expanduser().resolve() if path else Path(DEFAULT_REFERENCE_LIBRARY_PATH)
    if target_path.suffix.lower() == ".csv":
        raise ValueError("Select a TOML library directory or library.toml manifest.")
    try:
        loaded_archive = build_standard_archive(load_standard_measurements(target_path))
    except (OSError, ValueError) as exc:
        # A base-library failure still propagates. Only a usable base permits
        # overlay fallback, and its identity must describe the values in use.
        loaded_archive = build_standard_archive(load_standard_measurements(target_path, overlay=None))
        loaded_archive.attrs["overlay_error"] = f"Lab reference-material overlay could not be loaded; using the base library: {exc}"
    # Other tabs may have cached reductions using the same archive. Keep
    # that identity when opening or restoring a tab with the same library.
    if (
        str(target_path) != _active_standard_archive_path
        or not loaded_archive.equals(_active_standard_archive)
        or loaded_archive.attrs != _active_standard_archive.attrs
    ):
        _active_standard_archive = loaded_archive
        _active_standard_archive_path = str(target_path)
    return _active_standard_archive


def bundle_origin(directory: str | Path) -> dict[str, str] | None:
    """Origin of a cached session bundle's ``raw/`` folder, if known.

    Used when a bundle is opened as a folder rather than through its
    ``.upbr``; needs the server recorded when the bundle was fetched.
    """
    from UPbRPy.session_bundle import read_bundle_record
    from UPbRPy.project import normalize_origin

    folder = Path(directory)
    if folder.is_file():
        folder = folder.parent
    manifest = find_session_manifest(folder)
    if manifest is None:
        return None
    record = read_bundle_record(manifest.parent)
    origin = {
        "server": record.get("server", ""),
        "session_id": record.get("session_id", ""),
        "session_key": record.get("session_key", ""),
        "export_sha256": record.get("export_sha256", ""),
    }
    try:
        return normalize_origin(origin)
    except ValueError:
        return None


def normalize_import_settings(
    values: Mapping[str, object],
) -> dict[str, object]:
    """Keep ``read_experiment_data`` keywords; nest detector options.

    Project ``selection_options`` are the reader's keyword arguments with
    detector options in a nested ``selection_options`` mapping. Keys the
    reader does not take (for example ``plateau_width_mode`` written one
    level too high) are moved into that nested mapping instead of failing.
    """
    import inspect

    accepted = set(inspect.signature(read_experiment_data).parameters)
    accepted.add("primary_channel")
    result: dict[str, object] = {}
    nested: dict[str, object] = {}
    for key, value in dict(values).items():
        if key == "selection_options" and isinstance(value, Mapping):
            nested.update(dict(value))
        elif key in accepted:
            result[key] = value
        else:
            nested.setdefault(key, value)
    if nested:
        result["selection_options"] = nested
    return result


def measured_reference_materials(samples: Iterable[str]) -> list[str]:
    """Measured sample names that the reference library knows (aliases and
    typography resolved), in measurement order, Iolite duplicates excluded."""
    # Import from the defining module: ``from UPbRPy import standard_archive``
    # goes through the package's lazy legacy API, which loads UPbRPy.UPbRPy
    # (and matplotlib), and that module is not in the packaged app.
    from UPbRPy.standards import standard_archive
    from UPbRPy.standards import canonicalize_reference_material_samples

    archive = standard_archive
    names = [str(s) for s in samples]
    canonical = canonicalize_reference_material_samples(pd.DataFrame({"sample": names}), archive)["sample"]
    known = set(archive.index.astype(str))
    return list(dict.fromkeys(
        str(c) for c in canonical if str(c) in known and not str(c).upper().startswith("IOLITE_")
    ))


def load_session(
    directory: str,
    primary_channel: str | None = None,
    preset: WorkflowPreset | None = None,
    selection_method: str | None = None,
    import_settings: Mapping[str, object] | None = None,
    *,
    restored_selections: Sequence[Mapping[str, object]] | None = None,
) -> ReductionSession:
    """Open Qtegra data with preset or restored import settings.

    ``restored_selections`` allows the reader to omit redundant automatic
    picks when the caller will overlay complete saved selection records.
    """
    workflow_preset = preset or infer_workflow_preset(directory)
    effective_primary = primary_channel or workflow_preset.primary_channel
    # Presets contain nested selection-options dictionaries. A shallow copy
    # would let an interactive session edit the global mineral defaults when
    # the user changes plateau duration or sigma settings.
    read_kwargs = deepcopy(dict(workflow_preset.read_kwargs))
    explicit = normalize_import_settings(import_settings or {})
    manifest = resolve_session_manifest(directory, explicit.get("session_manifest"))
    if manifest is not None:
        # A session manifest names the label separator of this session; a
        # mineral preset's default must not override it (explicit project
        # settings still do).
        for key in ("sep", "sample_aliases"):
            if key not in explicit:
                read_kwargs.pop(key, None)
    read_kwargs.update(deepcopy(explicit))
    read_kwargs.pop("primary_channel", None)
    if selection_method is not None:
        read_kwargs["selection_method"] = selection_method
    qtegra = open_qtegra(
        raw_directory_for(directory) if manifest is not None else directory,
    )
    if manifest is None and "sep" not in explicit:
        # The preset's separator is only a fallback: labels such as
        # ``MADUCSB_778`` must group by sample even under a preset written
        # for ``MADUCSB 778``.
        sep = infer_label_separator(qtegra.labels, read_kwargs.get("sep"))
        if sep is not None:
            read_kwargs["sep"] = sep
    experiment, channels, total_data = read_experiment_data(
        qtegra,
        primary_channel=effective_primary,
        restored_selections=restored_selections,
        **{**read_kwargs, "session_manifest": manifest or False},
    )
    if "plateau_filter_method" not in experiment.columns:
        experiment["plateau_filter_method"] = (
            workflow_preset.plateau_filter_method
        )
    if "plateau_filter_channel" not in experiment.columns:
        experiment["plateau_filter_channel"] = effective_primary
    if MANUAL_SELECTION_COLUMN not in experiment.columns:
        experiment[MANUAL_SELECTION_COLUMN] = False
    clean_channels = [str(channel) for channel in channels if str(channel)]
    resolved_primary = effective_primary if effective_primary in clean_channels else clean_channels[0]
    selected_spot = str(experiment["spot_names"].iloc[0]) if not experiment.empty else None
    measured_samples = list(experiment["sample"].astype(str).unique()) if "sample" in experiment else []
    # Reference materials: the session manifest's plan (UPbDB) wins, else
    # the preset, preferring its preferred primaries that this session measured.
    standards: dict[str, object] = {
        "concentration_standards": workflow_preset.concentration_standards,
        **resolve_reference_standards(
            workflow_preset,
            measured_samples,
            measured_reference_materials(measured_samples),
        ),
    }
    if manifest is not None:
        standards.update(manifest.standards_config())
    session = ReductionSession(
        directory=str(directory),
        qtegra=qtegra,
        experiment=experiment,
        total_data=total_data,
        channels=clean_channels,
        primary_channel=resolved_primary,
        workflow_key=workflow_preset.key,
        workflow_title=workflow_preset.title,
        **standards,
        selected_spot=selected_spot,
        visible_channels=default_visible_channels(clean_channels, resolved_primary),
        import_settings={"primary_channel": resolved_primary, **read_kwargs},
        plot_mode="raw",
        reduction_cache={},
    )
    session.origin = bundle_origin(directory)
    ensure_shared_pipeline(session, workflow_preset)
    return session


def reimport_detector_calibration(
    session: ReductionSession,
    detector_calibration: Mapping[str, object],
) -> ReductionSession:
    """Rebuild sweeps from retained Qtegra readings, preserving reviewed windows.

    Returns a new GUI session and pipeline; the worker never mutates the active
    session. Only small settings and selection tables are copied.
    """
    if session.qtegra is None:
        raise ValueError("Load a Qtegra session before changing detector calibration.")
    settings = deepcopy(session.import_settings)
    settings["detector_calibration"] = deepcopy(dict(detector_calibration))
    read_kwargs = dict(settings)
    read_kwargs.pop("primary_channel", None)
    experiment, channels, total_data = read_experiment_data(
        session.qtegra, primary_channel=session.primary_channel, **read_kwargs,
    )
    experiment, total_data = restore_spot_edits(experiment, total_data, spot_edit_metadata(session.experiment))
    for key in ("studio_empty_spot_folders", "studio_collapsed_spot_folders"):
        if key in session.experiment.attrs:
            experiment.attrs[key] = deepcopy(session.experiment.attrs[key])
    experiment = restore_project_selections(
        experiment, selection_export_frame(session.experiment).to_dict("records"),
    )
    updated = replace(
        session, experiment=experiment, total_data=total_data, channels=list(channels),
        import_settings=settings, reduction_cache={},
    )
    preset = get_workflow_preset(session.workflow_key)
    config = pipeline_config_from_session(updated, preset)
    updated.pipeline = ReductionPipeline(
        experiment, total_data, channels, current_standard_archive(), config,
        source_directory=session.directory,
        reference_library_path=current_standard_archive_path(),
        input_files=raw_input_files(session.qtegra), build_diagnostic_plots=False,
        origin=session.origin,
    )
    return updated


def populate_recalculated_cache(
    session: ReductionSession,
    preset: WorkflowPreset,
    completed_stages: Iterable[PipelineStage],
    *, export_settings: ExportSettings | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Populate GUI compatibility keys from recalculated shared-core stages."""
    completed = set(completed_stages)
    for stage in PipelineStage:
        if stage not in completed:
            continue
        if progress is not None and stage not in {PipelineStage.IMPORT, PipelineStage.SELECTION}:
            progress(stage.value.replace("_", " ").capitalize())
        if stage == PipelineStage.BASELINE:
            ensure_baseline_subtracted(session)
        elif stage == PipelineStage.NORMALIZATION:
            ensure_normalized_plateau(session, preset)
        elif stage == PipelineStage.CONCENTRATION:
            ensure_concentration_results(session, preset)
        elif stage == PipelineStage.RATIOS:
            ensure_raw_ratios(session)
            ensure_signal_raw_ratios(session)
        elif stage == PipelineStage.DOWNHOLE:
            ensure_downhole_results(session, preset)
        elif stage == PipelineStage.SESSION:
            ensure_session_corrected(session, preset)
        elif stage == PipelineStage.SPOT_MEANS:
            ensure_spot_means(session, preset)
        elif stage == PipelineStage.DEPTH_PROFILE:
            ensure_depth_profile(session, preset)
        elif stage in {PipelineStage.FINALIZE, PipelineStage.EXPORT}:
            ensure_finalized_results(session, preset)
            ensure_results_pb204(session, preset, export_settings or ExportSettings())


def restore_session_from_project(
    project_file: str | Path, *, progress: Callable[[str], None] | None = None,
) -> RestoredProject:
    """Re-import and recalculate a project, reporting stage names if requested."""
    if progress is not None:
        progress("Reading data")
    project_path = Path(project_file).expanduser().resolve(strict=False)
    document = read_reduction_project(project_path)
    paths = dict(document["paths"])
    pipeline_values = dict(document["pipeline"])
    workspace = dict(document.get("workspace", {}))
    config = pipeline_config_from_dict(
        dict(pipeline_values["config"]),
        saved_version=(document.get("saved_by") or {}).get("version"),
        saved_schema=pipeline_values.get("settings_schema"),
    )
    preset = get_workflow_preset(config.workflow_key)

    source_directory = resolve_project_path(
        project_path,
        paths.get("source_directory"),
        label="the raw experiment directory",
    )
    reference_path = resolve_project_path(
        project_path,
        paths.get("reference_library"),
        label="the reference material library",
        required=False,
    )
    load_standard_archive_from_path(
        None if reference_path is None else str(reference_path)
    )

    restored_import_settings = normalize_import_settings(
        config.selection_options
    )
    restored_import_settings.pop("primary_channel", None)
    restored_import_settings.pop("selection_method", None)
    restored_import_settings["detector_calibration"] = config.detector_calibration
    session = load_session(
        str(source_directory),
        primary_channel=config.primary_channel,
        preset=preset,
        selection_method=config.selection_method,
        import_settings=restored_import_settings,
        restored_selections=document["selections"],
    )
    session.experiment, session.total_data = restore_spot_edits(
        session.experiment, session.total_data, document.get("spot_edits", {}),
    )
    if document["selections"]:
        auto_picks = session.experiment.copy()
        session.experiment = restore_project_selections(
            session.experiment,
            document["selections"],
            strict_spot_names=True,
        )
        if not any(MANUAL_SELECTION_COLUMN in record for record in document["selections"]):
            session.experiment[MANUAL_SELECTION_COLUMN] = infer_manual_selections(
                auto_picks, session.experiment
            )
    # else: a project fresh from a session bundle has no reviewed windows
    # yet; the auto-picked windows of this import are the starting point.
    session.origin = project_origin(document) or session.origin
    session.import_settings = {
        "primary_channel": config.primary_channel,
        **restored_import_settings,
        "selection_method": config.selection_method,
    }
    session.workflow_key = preset.key
    session.workflow_title = preset.title
    session.primary_channel = config.primary_channel
    session.internal_standard_channel = config.internal_standard_channel
    session.reference_overrides = deepcopy(config.reference_overrides)
    session.concentration_standards = config.concentration_standards
    session.primary_68_standard = config.primary_68_standard
    session.primary_76_standard = config.primary_76_standard
    session.primary_82_standard = config.primary_82_standard
    session.secondary_standards = config.secondary_standards
    session.ratio_aggregation = config.ratio_aggregation
    session.downhole_fit_method = gui_fit_method(config.downhole.method)
    session.downhole_fit_options = gui_fit_options(
        config.downhole.options,
        exponential_default=0.1,
    )
    session.downhole_outlier_sigma = config.downhole.outlier_sigma
    session.session_fit_method = gui_fit_method(config.session.method)
    session.session_fit_options = gui_fit_options(
        config.session.options,
        exponential_default=0.001,
    )
    session.common_pb_method = config.decision.common_pb_method
    session.common_pb_uncertainty_preset_name = str(
        config.common_pb_options.get(
            "common_pb_uncertainty_preset_name",
            "none",
        )
    )
    session.include_calibration_uncertainty = (
        config.decision.include_calibration_uncertainty
    )
    session.propagate_decay_constants = (
        config.uncertainty.propagate_decay_constants
    )
    session.propagate_u238_u235 = config.uncertainty.propagate_u238_u235
    session.long_term_reproducibility_mode = (
        config.uncertainty.long_term_reproducibility_mode
    )
    session.long_term_reproducibility_relative_1s = (
        config.uncertainty.long_term_reproducibility_relative_1s
    )
    session.age_transition_ma = config.decision.age_transition_ma

    visible_channels = workspace.get("visible_channels")
    restored_channels = []
    if isinstance(visible_channels, list):
        restored_channels = [
            str(channel)
            for channel in visible_channels
            if str(channel) in session.channels
        ]
    # Database bundles and projects saved with no usable channels need folder-open defaults.
    session.visible_channels = restored_channels or default_visible_channels(
        session.channels, session.primary_channel,
    )
    selected_spot = str(workspace.get("selected_spot", "") or "")
    if selected_spot in session.spot_names:
        session.selected_spot = selected_spot
    session.plot_mode = str(workspace.get("plot_mode", "raw"))

    input_files = raw_input_files(session.qtegra or source_directory)
    session.pipeline = ReductionPipeline(
        session.experiment,
        session.total_data,
        session.channels,
        current_standard_archive(),
        config,
        source_directory=source_directory,
        reference_library_path=reference_path,
        input_files=input_files,
        build_diagnostic_plots=False,
        origin=session.origin,
    )
    completed = completed_project_stages(
        pipeline_values.get("completed_stages", ())
    )
    populate_recalculated_cache(session, preset, completed,
        export_settings=ExportSettings.from_dict(workspace.get("export_settings", {})),
        progress=progress)
    resume_stage = PipelineStage(
        pipeline_values.get("resume_stage", PipelineStage.IMPORT.value)
    )
    return RestoredProject(
        session=session,
        preset=preset,
        project_path=project_path,
        reference_library_path=reference_path,
        resume_stage=resume_stage,
        completed_stages=completed,
        workspace=workspace,
    )


def unlocked_workflow_rows(stage_sequence, completed, loaded) -> list[bool]:
    """Unlock the first row and rows whose preceding stage is complete."""
    completed = set(completed)
    return [
        index == 0 or bool(loaded and stage_sequence[index - 1] in completed)
        for index in range(len(stage_sequence))
    ]


def clear_reduction_cache(session: ReductionSession) -> None:
    """Discard calculated products while preserving import and selections."""
    session.reduction_cache.clear()
    _downhole_plot_cache.pop(id(session), None)
    _session_plot_cache.pop(id(session), None)
    if session.pipeline is not None:
        session.pipeline.update_inputs(session.experiment, session.total_data)
        session.pipeline.cache.invalidate(PipelineStage.BASELINE)


MANUAL_SELECTION_COLUMN = "selection_manual"
SELECTION_WINDOW_COLUMNS = (
    "baseline_start_times",
    "baseline_end_times",
    "plateau_start_times",
    "plateau_end_times",
    "signal_start_times",
    "signal_end_times",
)


def infer_manual_selections(
    auto_picks: pd.DataFrame,
    restored: pd.DataFrame,
    *,
    tolerance: float = 1e-6,
) -> pd.Series:
    """Flag spots whose restored windows differ from a fresh auto-pick.

    Projects saved before ``selection_manual`` existed do not record which
    windows were edited by hand. ``auto_picks`` is the same import picked
    with the project's saved settings; any window that differs from it is
    treated as a manual edit, so a later batch re-pick leaves it alone.
    """
    auto = auto_picks.set_index(auto_picks["spot_names"].astype(str))
    names = restored["spot_names"].astype(str)
    manual = pd.Series(False, index=restored.index)
    for column in SELECTION_WINDOW_COLUMNS:
        if column not in restored.columns or column not in auto.columns:
            continue
        expected = names.map(auto[column]).to_numpy(dtype=float)
        actual = pd.to_numeric(restored[column], errors="coerce").to_numpy(dtype=float)
        both_missing = np.isnan(expected) & np.isnan(actual)
        manual |= ~(both_missing | (np.abs(actual - expected) <= tolerance))
    return manual


def update_selection_region(
    session: ReductionSession,
    spot_name: str,
    *,
    baseline: tuple[float, float] | None = None,
    plateau: tuple[float, float] | None = None,
    signal: tuple[float, float] | None = None,
    refresh_pipeline: bool = True,
    manual: bool | None = None,
) -> None:
    """Save reviewed windows while requiring signal bounds to contain plateau.

    ``manual=True`` marks the spot as edited by hand, so a batch re-pick
    that is told to keep manual edits skips it; ``False`` clears the mark
    (undo and redo restore it); ``None`` leaves it unchanged.
    """
    if not session.loaded:
        return
    row_mask = session.experiment["spot_names"].eq(spot_name)
    row = session.experiment[row_mask]
    if row.empty:
        raise KeyError(f"Unknown spot {spot_name!r}")

    effective_plateau = (
        tuple(sorted(float(value) for value in plateau))
        if plateau is not None
        else (
            float(row["plateau_start_times"].iloc[0]),
            float(row["plateau_end_times"].iloc[0]),
        )
    )
    if signal is not None:
        effective_signal = tuple(sorted(float(value) for value in signal))
    elif {
        "signal_start_times",
        "signal_end_times",
    }.issubset(row.columns):
        saved_start = row["signal_start_times"].iloc[0]
        saved_end = row["signal_end_times"].iloc[0]
        effective_signal = (
            tuple(sorted((float(saved_start), float(saved_end))))
            if pd.notna(saved_start) and pd.notna(saved_end)
            else effective_plateau
        )
    else:
        effective_signal = effective_plateau
    if (
        effective_signal[0] > effective_plateau[0]
        or effective_signal[1] < effective_plateau[1]
    ):
        raise ValueError(
            "Signal start/end must enclose the complete plateau window."
        )

    if baseline is not None:
        start_time, end_time = sorted(float(value) for value in baseline)
        session.experiment.loc[row_mask, "baseline_start_times"] = start_time
        session.experiment.loc[row_mask, "baseline_end_times"] = end_time
    if plateau is not None:
        session.experiment.loc[row_mask, "plateau_start_times"] = (
            effective_plateau[0]
        )
        session.experiment.loc[row_mask, "plateau_end_times"] = (
            effective_plateau[1]
        )
    if signal is not None:
        session.experiment.loc[row_mask, "signal_start_times"] = (
            effective_signal[0]
        )
        session.experiment.loc[row_mask, "signal_end_times"] = (
            effective_signal[1]
        )
    if manual is not None:
        if MANUAL_SELECTION_COLUMN not in session.experiment:
            session.experiment[MANUAL_SELECTION_COLUMN] = False
        session.experiment.loc[row_mask, MANUAL_SELECTION_COLUMN] = bool(manual)
    if refresh_pipeline:
        clear_reduction_cache(session)


def autopick_selection_regions(
    session: ReductionSession,
    spot_name: str,
    *,
    spot_data: pd.DataFrame | None = None,
    refresh_pipeline: bool = True,
) -> dict[str, tuple[float, float] | None]:
    """Re-pick one spot with the session's current selection parameters."""
    if not session.loaded:
        return {"baseline": None, "plateau": None}
    if spot_data is None:
        spot_data = get_spot_data(session, spot_name)
    if spot_data.empty:
        return {"baseline": None, "plateau": None}

    selection_channel = "29Si" if "NIST" in spot_name and "29Si" in spot_data else str(
        session.import_settings.get("primary_channel", session.primary_channel)
    )
    if selection_channel not in spot_data:
        selection_channel = session.primary_channel if session.primary_channel in spot_data else str(spot_data.columns[1])

    selection_options = session.import_settings.get("selection_options", {})
    if not isinstance(selection_options, Mapping):
        selection_options = {}
    selection_options = dict(selection_options)
    option_duration_limit = selection_options.pop(
        "signal_duration_limit_seconds", None
    )
    duration_limit = session.import_settings.get(
        "signal_duration_limit_seconds", option_duration_limit
    )
    selection = autopick_signal_regions(
        spot_data[selection_channel].to_numpy(dtype=float),
        time=spot_data["Time"].to_numpy(dtype=float),
        method=str(session.import_settings.get("selection_method", "threshold")),
        baseline_sweep_n=int(session.import_settings.get("baseline_sweep_n", 10)),
        peak_sweep_n=int(session.import_settings.get("peak_sweep_n", 20)),
        threshold=float(session.import_settings.get("threshold", 0.1)),
        filter_percentile=float(session.import_settings.get("filter_percentile", 70)),
        baseline_offset=int(session.import_settings.get("baseline_offset", 2)),
        plateau_offset=int(session.import_settings.get("plateau_offset", 2)),
        signal_duration_limit_seconds=duration_limit,
        **selection_options,
    )

    max_index = max(0, len(spot_data) - 1)
    baseline = (
        float(spot_data.iloc[min(selection.baseline_start, max_index)]["Time"]),
        float(spot_data.iloc[min(selection.baseline_end, max_index)]["Time"]),
    )
    plateau = (
        float(spot_data.iloc[min(selection.plateau_start, max_index)]["Time"]),
        float(spot_data.iloc[min(selection.plateau_end, max_index)]["Time"]),
    )
    signal = (
        float(spot_data.iloc[min(selection.signal_start, max_index)]["Time"]),
        float(spot_data.iloc[min(selection.signal_end, max_index)]["Time"]),
    )
    # Install the complete proposed selection atomically. Updating the
    # plateau first can reject an otherwise valid re-pick when the old signal
    # interval does not contain the newly detected plateau.
    update_selection_region(
        session,
        spot_name,
        baseline=baseline,
        plateau=plateau,
        signal=signal,
        refresh_pipeline=False,
    )
    row_mask = session.experiment["spot_names"] == spot_name
    session.experiment.loc[row_mask, "selection_method"] = selection.method
    session.experiment.loc[row_mask, "selection_channel"] = selection_channel
    session.experiment.loc[row_mask, "selection_error"] = None
    session.experiment.loc[row_mask, "selection_detection_index"] = selection.detection_index
    if "sigma_threshold" in selection.diagnostics:
        session.experiment.loc[row_mask, "selection_sigma_threshold"] = (
            selection.diagnostics["sigma_threshold"]
        )
    session.experiment.loc[row_mask, "selection_signal_end_index"] = selection.signal_end
    session.experiment.loc[row_mask, "selection_unrestricted_signal_end_index"] = (
        selection.diagnostics["signal_end_unrestricted_index"]
    )
    session.experiment.loc[row_mask, "selection_signal_duration_limit_seconds"] = (
        session.import_settings.get("signal_duration_limit_seconds")
    )
    session.experiment.loc[row_mask, "selection_signal_duration_limit_applied"] = (
        selection.diagnostics["signal_duration_limit_applied"]
    )
    session.experiment.loc[row_mask, "selection_washout_start_index"] = (
        selection.washout_start
    )
    session.experiment.loc[row_mask, "selection_post_baseline_start_index"] = (
        selection.washout_start
    )
    session.experiment.loc[row_mask, "selection_falling_edge_index"] = (
        selection.diagnostics.get("signal_end_falling_edge_start", -1)
    )
    session.experiment.loc[row_mask, MANUAL_SELECTION_COLUMN] = False
    if refresh_pipeline:
        clear_reduction_cache(session)
    return {"baseline": baseline, "plateau": plateau, "signal": signal}


def manual_selection_spots(session: ReductionSession) -> set[str]:
    """Return the spots whose windows were edited by hand."""
    if not session.loaded or MANUAL_SELECTION_COLUMN not in session.experiment:
        return set()
    flags = session.experiment[MANUAL_SELECTION_COLUMN].fillna(False).astype(bool)
    return set(session.experiment.loc[flags, "spot_names"].astype(str))


def detected_on_peak_summary(session: ReductionSession) -> dict[str, float] | None:
    """Median and range of the detected on-peak durations, in seconds.

    Each duration runs from the spot's signal start to the first sweep of
    its falling edge (``selection_falling_edge_index``).
    """
    column = "selection_falling_edge_index"
    experiment = session.experiment if session.loaded else None
    if experiment is None or column not in experiment or "signal_start_times" not in experiment:
        return None
    edges = pd.to_numeric(experiment[column], errors="coerce")
    starts = pd.to_numeric(experiment["signal_start_times"], errors="coerce")
    times = session.total_data.groupby("spot_name", sort=False)["Time"]
    durations = []
    for name, edge, start in zip(experiment["spot_names"].astype(str), edges, starts):
        if not np.isfinite(edge) or edge < 0 or not np.isfinite(start):
            continue
        try:
            spot_times = times.get_group(name).to_numpy(dtype=float)
        except KeyError:
            continue
        if int(edge) < spot_times.size:
            durations.append(spot_times[int(edge)] - start)
    values = pd.Series(durations, dtype=float)
    values = values[values > 0]
    if values.empty:
        return None
    return {
        "median": float(values.median()),
        "p10": float(values.quantile(0.1)),
        "p90": float(values.quantile(0.9)),
        "count": int(values.size),
    }


def autopick_all_selection_regions(
    session: ReductionSession,
    spot_names: Iterable[str] | None = None,
    *,
    overwrite_manual: bool = True,
) -> dict[str, object]:
    """Re-pick spots and refresh the shared pipeline only once.

    ``spot_names`` limits the re-pick to those spots (default: every spot).
    With ``overwrite_manual=False`` spots edited by hand are skipped and
    listed under ``skipped_manual``. Grouping the sweep table before
    selection avoids repeatedly scanning the complete session. Individual
    failures are retained in the experiment table and returned to the GUI,
    while valid spots remain usable.
    """
    if not session.loaded:
        return {"selected_count": 0, "failures": {}, "skipped_manual": []}

    spot_frames = {
        str(spot_name): frame.reset_index(drop=True)
        for spot_name, frame in session.total_data.groupby(
            "spot_name", sort=False
        )
    }
    all_names = session.experiment["spot_names"].astype(str).tolist()
    if spot_names is not None:
        wanted = {str(name) for name in spot_names}
        all_names = [name for name in all_names if name in wanted]
    manual = set() if overwrite_manual else manual_selection_spots(session)
    skipped = [name for name in all_names if name in manual]
    spot_names = [name for name in all_names if name not in manual]
    failures: dict[str, str] = {}
    selected_count = 0
    for spot_name in spot_names:
        try:
            result = autopick_selection_regions(
                session,
                spot_name,
                spot_data=spot_frames.get(spot_name),
                refresh_pipeline=False,
            )
            if result.get("baseline") and result.get("plateau"):
                selected_count += 1
        except Exception as exc:
            message = str(exc)
            failures[spot_name] = message
            row_mask = session.experiment["spot_names"].astype(str).eq(
                spot_name
            )
            session.experiment.loc[row_mask, "selection_error"] = message

    clear_reduction_cache(session)
    return {"selected_count": selected_count, "failures": failures, "skipped_manual": skipped}


_spot_row_positions: dict[int, tuple[weakref.ReferenceType, dict]] = {}


def _rows_by_spot(frame: pd.DataFrame) -> dict:
    key = id(frame)
    cached = _spot_row_positions.get(key)
    if cached is not None and cached[0]() is frame:
        return cached[1]

    def release(reference):
        current = _spot_row_positions.get(key)
        if current is not None and current[0] is reference:
            _spot_row_positions.pop(key, None)

    positions = frame.groupby("spot_name", sort=False, observed=True).indices
    _spot_row_positions[key] = (weakref.ref(frame, release), positions)
    return positions


def get_spot_data(session: ReductionSession, spot_name: str | None) -> pd.DataFrame:
    if not session.loaded or not spot_name:
        return pd.DataFrame()
    positions = _rows_by_spot(session.total_data).get(spot_name, [])
    return session.total_data.iloc[positions].reset_index(drop=True)


def extract_spot_frame(frame: pd.DataFrame, spot_name: str | None) -> pd.DataFrame:
    if frame.empty or not spot_name or "spot_name" not in frame:
        return pd.DataFrame()
    return frame[frame["spot_name"] == spot_name].reset_index(drop=True)


def get_selection_regions(session: ReductionSession, spot_name: str | None) -> dict[str, tuple[float, float] | None]:
    if not session.loaded or not spot_name:
        return {"baseline": None, "plateau": None, "signal": None}
    row = session.experiment[session.experiment["spot_names"] == spot_name]
    if row.empty:
        return {"baseline": None, "plateau": None, "signal": None}

    def _pair(start_key: str, end_key: str) -> tuple[float, float] | None:
        if start_key not in row.columns or end_key not in row.columns:
            return None
        start_value = row[start_key].iloc[0]
        end_value = row[end_key].iloc[0]
        if pd.isna(start_value) or pd.isna(end_value):
            return None
        return float(start_value), float(end_value)

    return {
        "baseline": _pair("baseline_start_times", "baseline_end_times"),
        "plateau": _pair("plateau_start_times", "plateau_end_times"),
        "signal": _pair("signal_start_times", "signal_end_times"),
    }


def selection_quality(session: ReductionSession, spot_name: str | None) -> dict[str, Any]:
    if not session.loaded or not spot_name:
        return {"baseline_points": 0, "plateau_points": 0, "warnings": ["Load a session to inspect selections."], "status": "idle"}

    spot_data = get_spot_data(session, spot_name)
    regions = get_selection_regions(session, spot_name)
    baseline_frame = slice_region(spot_data, regions["baseline"])
    plateau_frame = slice_region(spot_data, regions["plateau"])
    warnings: list[str] = []

    if len(baseline_frame) < 5:
        warnings.append("baseline window has fewer than 5 points")
    if len(plateau_frame) < 5:
        warnings.append("plateau window has fewer than 5 points")

    baseline_region = regions["baseline"]
    plateau_region = regions["plateau"]
    signal_region = regions["signal"]
    if baseline_region and plateau_region:
        overlap_start = max(baseline_region[0], plateau_region[0])
        overlap_end = min(baseline_region[1], plateau_region[1])
        if overlap_start < overlap_end:
            warnings.append("baseline and plateau windows overlap")
    if plateau_region and signal_region:
        if (
            signal_region[0] > plateau_region[0]
            or signal_region[1] < plateau_region[1]
        ):
            warnings.append("signal limits do not enclose the plateau")

    status = "ready" if not warnings else "review"
    return {
        "baseline_points": len(baseline_frame),
        "plateau_points": len(plateau_frame),
        "warnings": warnings,
        "status": status,
    }


def ensure_baseline_subtracted(session: ReductionSession) -> pd.DataFrame:
    """Return baseline-subtracted sweeps from the shared reduction pipeline."""
    cache_key = "baseline_subtracted_total_data"
    if not session.loaded:
        return pd.DataFrame()
    preset = get_workflow_preset(session.workflow_key)
    baseline_subtracted = ensure_shared_pipeline(session, preset).run_baseline()
    session.reduction_cache[cache_key] = baseline_subtracted
    return baseline_subtracted


def ensure_normalized_plateau(session: ReductionSession, preset: WorkflowPreset) -> pd.DataFrame:
    """Return internally normalized plateau data from the shared pipeline."""
    cache_key = "normalized_plateau_data"
    if not session.loaded:
        return pd.DataFrame()
    normalized = ensure_shared_pipeline(session, preset).run_normalization()
    session.reduction_cache[cache_key] = normalized
    return normalized


def ensure_concentration_results(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    """Return concentration calibration and result products from the core."""
    cache_key = "concentration_results"
    if not preset.concentration_standards:
        raise ValueError(f"{preset.title} does not yet define concentration standards for the GUI workflow.")
    pipeline = ensure_shared_pipeline(session, preset)
    results = pipeline.run_concentration()
    session.reduction_cache["normalized_plateau_data"] = (
        pipeline.run_normalization()
    )
    session.reduction_cache[cache_key] = results
    return results


def ensure_raw_ratios(session: ReductionSession) -> pd.DataFrame:
    """Return plateau ratios calculated by the shared pipeline."""
    cache_key = "plateau_data_with_raw_ratios"
    if not session.loaded:
        return pd.DataFrame()
    preset = get_workflow_preset(session.workflow_key)
    ratios = ensure_shared_pipeline(session, preset).run_ratios()["plateau"]
    session.reduction_cache[cache_key] = ratios
    return ratios


def ensure_signal_raw_ratios(session: ReductionSession) -> pd.DataFrame:
    """Return full-signal ratios aligned to each spot's saved signal start."""
    cache_key = "signal_data_with_raw_ratios"
    if not session.loaded:
        return pd.DataFrame()
    preset = get_workflow_preset(session.workflow_key)
    ratios = ensure_shared_pipeline(session, preset).run_ratios()["signal"]
    session.reduction_cache[cache_key] = ratios
    return ratios


def ensure_downhole_results(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    """Return shared downhole models and corrected plateau/signal ratios."""
    cache_key = "downhole_results"
    if not preset.primary_standard:
        raise ValueError(f"{preset.title} does not yet define a primary standard for downhole correction.")
    pipeline = ensure_shared_pipeline(session, preset)
    core_results = pipeline.run_downhole()
    results = {
        **core_results,
        # Compatibility aliases keep existing widgets working while new stages
        # consume the explicit plateau/signal products.
        "corrected_ratios": core_results["plateau_corrected"],
        "fit_method": session.downhole_fit_method,
        "fit_label": fit_method_label(session.downhole_fit_method),
        "fit_options": dict(session.downhole_fit_options),
    }
    ratios = pipeline.run_ratios()
    session.reduction_cache["plateau_data_with_raw_ratios"] = ratios["plateau"]
    session.reduction_cache["signal_data_with_raw_ratios"] = ratios["signal"]
    session.reduction_cache[cache_key] = results
    return results


def ensure_session_corrected(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    """Return shared session models and fully corrected ratio products."""
    cache_key = "session_correction_results"
    pipeline = ensure_shared_pipeline(session, preset)
    core_results = pipeline.run_session()
    results = {
        **core_results,
        "corrected_ratios": core_results["plateau_corrected"],
        "fit_method": session.session_fit_method,
        "fit_label": fit_method_label(session.session_fit_method),
        "fit_options": dict(session.session_fit_options),
    }
    ensure_downhole_results(session, preset)
    session.reduction_cache[cache_key] = results
    return results


def ensure_spot_means(
    session: ReductionSession,
    preset: WorkflowPreset,
) -> pd.DataFrame:
    """Return final corrected spot means before optional common-Pb decisions."""
    cache_key = "spot_mean_results"
    result = ensure_shared_pipeline(session, preset).run_spot_means()
    ensure_session_corrected(session, preset)
    session.reduction_cache[cache_key] = result
    return result


def ensure_depth_profile(
    session: ReductionSession,
    preset: WorkflowPreset,
) -> pd.DataFrame:
    """Return fully corrected sweep-wise depth-profile results."""
    cache_key = "depth_profile_results"
    result = ensure_shared_pipeline(session, preset).run_depth_profile()
    ensure_session_corrected(session, preset)
    session.reduction_cache[cache_key] = result
    return result


def ensure_finalized_results(
    session: ReductionSession,
    preset: WorkflowPreset,
):
    """Return final decisions and both legacy and per-system RM summaries."""
    cache_key = "finalized_results"
    pipeline = ensure_shared_pipeline(session, preset)
    result = pipeline.run_finalize()
    ensure_spot_means(session, preset)
    session.reduction_cache[cache_key] = result
    return result


def ensure_results_pb204(
    session: ReductionSession,
    preset: WorkflowPreset,
    settings: ExportSettings,
) -> pd.DataFrame:
    """Cache export-derived mass-204 display values once per finalized object.

    Call from the finalization worker. Review clicks only read this cache;
    export choices and review exclusions do not change these quantities.
    """
    pipeline = ensure_shared_pipeline(session, preset)
    finalized = pipeline.run_finalize()
    cache = session.reduction_cache
    if cache.get("results_pb204_source") is finalized and "results_pb204" in cache:
        return cache["results_pb204"]
    try:
        table = build_export_table(pipeline, settings)
    except Exception as exc:  # the results must still show without these columns
        names = finalized.selected_results["spot_name"] if "spot_name" in finalized.selected_results else []
        frame = pd.DataFrame({"spot_name": list(names), "204Pb": np.nan, "206Pb/204Pb": np.nan,
                              "206Pb/204Pb_1s": np.nan, "204Pb_status": f"unavailable: {exc}"})
        cache["results_pb204_source"] = finalized
        cache["results_pb204"] = frame
        return frame
    frame = table.rows[["spot_name", "204Pb", "206Pb/204Pb", "206Pb/204Pb_1s"]].copy()
    context = table.context["total_pb"]
    correction = context["hg_correction"]
    omitted = frame.spot_name.isin(context["omitted_204_spots"])
    frame["204Pb_status"] = "reported"
    frame.loc[omitted, "204Pb_status"] = "not detected"
    if correction != "per-spot baseline 204Hg/202Hg":
        frame["204Pb_status"] = correction
    frame.loc[frame["204Pb_status"].ne("reported"), "204Pb"] = np.nan
    cache["results_pb204_source"] = finalized
    cache["results_pb204"] = frame
    return frame


def reference_material_summary(session: ReductionSession, preset: WorkflowPreset) -> str:
    if not session.loaded or "sample" not in session.total_data:
        return ""
    counts = session.total_data["sample"].value_counts()
    parts: list[str] = []
    primary_standard = (
        session.primary_68_standard or preset.primary_standard
    )
    concentration_standards = (
        session.concentration_standards
        or preset.concentration_standards
    )
    if primary_standard:
        parts.append(
            f"{primary_standard}: "
            f"{int(counts.get(primary_standard, 0))} primary rows"
        )
    if concentration_standards:
        standard_counts = [
            f"{name} {int(counts.get(name, 0))}"
            for name in concentration_standards
        ]
        parts.append("Glasses " + ", ".join(standard_counts))
    return " | ".join(parts)


def _require_samples(session: ReductionSession, sample_names: tuple[str, ...], label: str) -> None:
    available = set(session.total_data["sample"].dropna().astype(str).unique().tolist()) if session.loaded else set()
    missing = [sample_name for sample_name in sample_names if sample_name not in available]
    if missing:
        raise ValueError(f"Missing {label} in the loaded session: {', '.join(missing)}")


def project_summary(session: ReductionSession) -> dict[str, str]:
    if not session.loaded:
        return {
            "name": "No experiment loaded",
            "spots": "Import a Thermo iCAP export directory",
            "mode": "Choose a notebook-derived mineral workflow",
            "engine": "Shared UPbRPy core",
        }
    project_name = Path(session.directory).name or session.directory
    return {
        "name": project_name,
        "spots": f"{len(session.spot_names)} spots · {len(session.channels)} channels",
        "mode": f"{session.workflow_title} workflow · depth profile + spot",
        "engine": f"Primary channel: {session.primary_channel}",
    }


def spot_metrics(session: ReductionSession, spot_name: str | None) -> dict[str, str]:
    if not session.loaded or not spot_name:
        return {
            "Active spot": "Preview",
            "Selection span": "34.8 s",
            "206Pb/238U": "0.09142 ± preview",
            "Primary signal": "1.95 kcps",
        }

    spot_data = get_spot_data(session, spot_name)
    regions = get_selection_regions(session, spot_name)
    plateau = regions["plateau"]
    baseline = regions["baseline"]

    plateau_data = slice_region(spot_data, plateau)
    baseline_data = slice_region(spot_data, baseline)

    selection_span = f"{plateau[1] - plateau[0]:.1f} s" if plateau else "Not set"
    ratio = "Unavailable"
    if not plateau_data.empty and {"206Pb", "238U"}.issubset(plateau_data.columns):
        denominator = float(plateau_data["238U"].mean())
        if denominator != 0:
            ratio = f"{float(plateau_data['206Pb'].mean()) / denominator:.5f}"

    primary_signal = "Unavailable"
    if session.primary_channel in spot_data:
        primary_source = plateau_data if not plateau_data.empty else spot_data
        primary_signal = f"{float(primary_source[session.primary_channel].mean()) / 1000:.2f} kcps"

    if not baseline_data.empty and session.primary_channel in baseline_data:
        baseline_value = float(baseline_data[session.primary_channel].mean())
        primary_signal = f"{primary_signal} · BL {baseline_value / 1000:.2f} kcps"

    return {
        "Active spot": spot_name,
        "Selection span": selection_span,
        "206Pb/238U": ratio,
        "Primary signal": primary_signal,
    }


def slice_region(frame: pd.DataFrame, region: tuple[float, float] | None) -> pd.DataFrame:
    if frame.empty or region is None:
        return pd.DataFrame()
    start_time, end_time = region
    return frame[(frame["Time"] >= start_time) & (frame["Time"] <= end_time)].reset_index(drop=True)


# ── Diagnostic plot data helpers ──────────────────────────────────────────

def _fractionation_standard(session: ReductionSession, preset: WorkflowPreset, ratio_name: str) -> str:
    if preset.common_pb_calibration == "tera_wasserburg":
        if ratio_name == "207Pb/206Pb":
            return session.primary_76_standard or preset.pb_ratio_standard
        if ratio_name == "208Pb/232Th":
            return session.primary_82_standard or preset.th_ratio_standard
    return session.primary_68_standard or preset.primary_standard


# Kept outside ReductionSession so view caches never enter stage snapshots.
_downhole_plot_cache = {}
_session_plot_cache = {}


def _remember_diagnostic_plot(session, kind, key, sources, data):
    cache = _downhole_plot_cache if kind == "downhole" else _session_plot_cache
    names = ("signal_data_with_raw_ratios", "downhole_results") if kind == "downhole" else (
        "session_correction_results", "downhole_results")
    if any(session.reduction_cache.get(name) is not source
           for name, source in zip(names, sources)):
        return
    identity = id(session)

    def release(reference):
        cached = cache.get(identity)
        if cached is not None and cached[0] is reference:
            cache.pop(identity, None)

    cache[identity] = (weakref.ref(session, release), key, sources, data)


def get_downhole_plot_data(
    session: ReductionSession,
    preset: WorkflowPreset,
    ratio_name: str = "206Pb/238U",
) -> dict | None:
    """Return a dict of arrays needed to draw the downhole fractionation diagnostic.

    Returns None if the prerequisite cache entries are absent.
    """
    if session.loaded:
        ensure_shared_pipeline(session, preset)
    raw_ratios = session.reduction_cache.get("signal_data_with_raw_ratios")
    downhole_results = session.reduction_cache.get("downhole_results")
    if not isinstance(raw_ratios, pd.DataFrame) or raw_ratios.empty:
        return None
    primary_standard = _fractionation_standard(session, preset, ratio_name)
    if not primary_standard:
        return None
    if ratio_name not in raw_ratios.columns:
        return None
    try:
        archive = current_standard_archive()
        known_ratio = float(archive.loc[primary_standard, ratio_name + "*"])
    except Exception:
        return None

    key = (ratio_name, primary_standard, known_ratio, session.downhole_fit_method)
    sources = (raw_ratios, downhole_results)
    cached = _downhole_plot_cache.get(id(session))
    if cached is not None and cached[0]() is session and cached[1] == key and all(
        old is new for old, new in zip(cached[2], sources)
    ):
        return cached[3]
    # Plotting consumes columns only, not deep-copied reduction provenance.
    rows = pd.DataFrame(raw_ratios, copy=False)
    ps_rows = rows[rows["sample"] == primary_standard]
    if ps_rows.empty:
        return None

    spot_times: list[np.ndarray] = []
    spot_ratios: list[np.ndarray] = []
    spot_names: list[str] = []
    for spot_name, this_spot in ps_rows.groupby("spot_name", sort=False, observed=True):
        t = this_spot["Time"].values.astype(float)
        r = this_spot[ratio_name].values.astype(float)
        valid = np.isfinite(r) & (r > 0)
        if valid.sum() < 2:
            continue
        rel_t = t - t[0]
        spot_times.append(rel_t[valid])
        spot_ratios.append(r[valid])
        spot_names.append(str(spot_name))

    if not spot_times:
        return None

    max_t = float(max(t[-1] for t in spot_times))
    n_pts = max(len(t) for t in spot_times)
    unified_time = np.linspace(0.0, max_t, n_pts)
    stacked = np.array(
        [
            np.interp(unified_time, time, ratio)
            for time, ratio in zip(spot_times, spot_ratios)
        ]
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_frac = known_ratio / np.nanmean(stacked, axis=0)
    fit_curve = mean_frac
    fit_label = fit_method_label(session.downhole_fit_method)
    std_frac = float("nan")
    if isinstance(downhole_results, dict):
        correction_results = downhole_results.get("correction_results")
        if isinstance(correction_results, dict) and ratio_name in correction_results:
            ratio_result = correction_results[ratio_name]
            metadata = ratio_result.get("metadata", {})
            model_time = np.asarray(
                metadata.get(
                    "time",
                    ratio_result.get("fit_prediction_x", unified_time),
                ),
                dtype=float,
            )
            model_values = np.asarray(
                metadata.get("fitted_fractionation", []),
                dtype=float,
            )
            if model_time.size and model_values.size == model_time.size:
                unified_time = model_time
                fit_curve = model_values
            else:
                fit_curve = evaluate_correction_result(
                    ratio_result,
                    unified_time,
                )
            std_frac = float(
                metadata.get(
                    "residual_standard_deviation",
                    ratio_result.get(
                        "fit_residual_standard_deviation",
                        float("nan"),
                    ),
                )
            )
            fit_label = str(ratio_result.get("model", fit_label))

    data = {
        "ratio_name": ratio_name,
        "known_ratio": known_ratio,
        "primary_standard": primary_standard,
        "spot_names": spot_names,
        "spot_times": spot_times,
        "spot_ratios": spot_ratios,
        "unified_time": unified_time,
        "mean_frac": fit_curve,
        "fit_label": fit_label,
        "std_frac": std_frac,
    }
    _remember_diagnostic_plot(session, "downhole", key, sources, data)
    return data


def get_session_plot_data(
    session: ReductionSession,
    preset: WorkflowPreset,
    ratio_name: str = "206Pb/238U",
) -> dict | None:
    """Return a dict of arrays for drawing the session fractionation diagnostic."""
    if session.loaded:
        ensure_shared_pipeline(session, preset)
    session_results = session.reduction_cache.get("session_correction_results")
    downhole_results = session.reduction_cache.get("downhole_results")
    if not isinstance(session_results, dict) or not isinstance(downhole_results, dict):
        return None

    primary_standard = _fractionation_standard(session, preset, ratio_name)
    if not primary_standard:
        return None
    try:
        archive = current_standard_archive()
        known_ratio = float(archive.loc[primary_standard, ratio_name + "*"])
    except Exception:
        return None

    ratio_results = session_results.get("correction_results", {}).get(ratio_name)
    if not ratio_results:
        return None
    key = (ratio_name, primary_standard, known_ratio)
    sources = (session_results, downhole_results)
    cached = _session_plot_cache.get(id(session))
    if cached is not None and cached[0]() is session and cached[1] == key and all(
        old is new for old, new in zip(cached[2], sources)
    ):
        return cached[3]
    observations = ratio_results.get("fit_observations", {})
    t_arr = np.asarray(observations.get("time", []), dtype=float)
    f_arr = np.asarray(observations.get("fractionation", []), dtype=float)
    e_arr = np.asarray(observations.get("std", []), dtype=float)
    if t_arr.size < 1 or f_arr.size != t_arr.size:
        return None
    if e_arr.size != t_arr.size:
        e_arr = np.full(t_arr.size, np.nan)
    fit_times = np.asarray(
        ratio_results.get(
            "fit_prediction_x",
            np.linspace(float(np.nanmin(t_arr)), float(np.nanmax(t_arr)), 200),
        ),
        dtype=float,
    )
    fit_vals = evaluate_correction_result(ratio_results, fit_times)
    p_std = float(
        ratio_results.get(
            "fit_residual_standard_deviation",
            ratio_results.get("std", 0.0),
        )
    )

    # run_session passes the primary-standard rows of plateau_corrected to
    # calc_session_nominal_fractionation, which groups with sort=False and
    # takes Time.mean(). Match those times, including partial old caches.
    dh_corrected = downhole_results.get("plateau_corrected")
    spot_names_out = [f"standard {index + 1}" for index in range(t_arr.size)]
    spot_samples = [primary_standard] * t_arr.size
    if isinstance(dh_corrected, pd.DataFrame):
        rows = pd.DataFrame(dh_corrected, copy=False)
        selected = rows[rows["sample"].eq(primary_standard)]
        groups = selected.groupby("spot_name", sort=False).agg(
            time=("Time", "mean"), sample=("sample", "first")
        )
        group_times = groups["time"].to_numpy(dtype=float)
        matches = {}
        if group_times.shape == t_arr.shape and np.all(
            np.isclose(group_times, t_arr, rtol=1e-9, atol=1e-6)
        ):
            matches = dict(enumerate(range(t_arr.size)))
        else:
            order = np.argsort(group_times)
            sorted_times = group_times[order]
            for index, time in enumerate(t_arr):
                if not np.isfinite(time):
                    continue
                tolerance = 1e-6 + 1e-9 * abs(time)
                lo = np.searchsorted(sorted_times, time - tolerance, side="left")
                hi = np.searchsorted(sorted_times, time + tolerance, side="right")
                candidates = order[lo:hi]
                candidates = candidates[np.isclose(group_times[candidates], time,
                                                    rtol=1e-9, atol=1e-6)]
                if candidates.size == 1:
                    matches[index] = int(candidates[0])
            # A group cannot identify two observations unambiguously.
            used = list(matches.values())
            counts = pd.Series(used).value_counts()
            matches = {i: group for i, group in matches.items() if counts[group] == 1}
        for index, group in matches.items():
            spot_names_out[index] = str(groups.index[group])
            spot_samples[index] = str(groups.iloc[group]["sample"])

    experiment = session.experiment
    measurement_names = session.spot_names
    starts = {}
    if "spot_names" in experiment and "spot_starting_times" in experiment:
        measurement_names = experiment.sort_values("spot_starting_times", kind="stable")["spot_names"].astype(str).tolist()
        starts = dict(zip(experiment["spot_names"].astype(str), experiment["spot_starting_times"]))
    spot_indices = {name: index + 1 for index, name in enumerate(measurement_names)}
    weights = np.asarray(observations.get("weights", []), dtype=float)
    residuals = np.asarray(ratio_results.get("fit_residuals", []), dtype=float)

    data = {
        "ratio_name": ratio_name,
        "known_ratio": known_ratio,
        "primary_standard": primary_standard,
        "spot_names": spot_names_out,
        "spot_samples": spot_samples,
        "spot_order": np.array([spot_indices.get(name, np.nan) for name in spot_names_out]),
        "total_spots": len(measurement_names),
        "spot_start_times": np.array([starts.get(name, np.nan) for name in spot_names_out]),
        "spot_weights": weights if weights.shape == t_arr.shape else np.full(t_arr.shape, np.nan),
        "spot_residuals": residuals if residuals.shape == t_arr.shape else np.full(t_arr.shape, np.nan),
        "spot_mean_times": t_arr,
        "spot_mean_fracs": f_arr,
        "spot_frac_errs": e_arr,
        "fit_times": fit_times,
        "fit_vals": fit_vals,
        "fit_std": p_std,
        "fit_label": str(
            ratio_results.get(
                "model",
                fit_method_label(session.session_fit_method),
            )
        ),
        # ``fit_std`` is the residual standard deviation of the calibrated
        # standard-spot factors.  The GUI chooses whether to show ±1σ or ±2σ.
        "show_uncertainty": True,
    }
    _remember_diagnostic_plot(session, "session", key, sources, data)
    return data


def get_standard_bias_data(
    session: ReductionSession,
    preset: WorkflowPreset,
    state: Mapping[str, object] | None = None,
) -> dict | None:
    """Return the reference-material bias table, its summary and axis labels.

    ``state`` holds the bias controls (``date``, ``quantity``, ``log_x``).
    Returns ``None`` until the session has finalized results.
    """
    from UPbRPy.bias import (
        bias_quantities,
        project_unknowns,
        bias_date_description,
        bias_summary,
        calibrated_bias_date,
        spot_signal_quantities,
        standard_bias_table,
    )

    if not session.loaded or "finalized_results" not in session.reduction_cache:
        return None
    state = dict(state or {})
    date = str(state.get("date", "calibrated"))
    quantity = str(state.get("quantity", "U"))
    log_x = bool(state.get("log_x", False))
    pipeline = ensure_shared_pipeline(session, preset)
    if date == "calibrated":
        date = calibrated_bias_date(pipeline.config.common_pb_calibration)
    description = bias_date_description(date, pipeline.config.common_pb_calibration)
    finalized = pipeline.run_finalize()
    quantities = bias_quantities(finalized.parallel_results, standard_archive=pipeline.standard_archive)
    if quantity not in quantities:
        quantity = "U" if "U" in quantities else next(iter(quantities))
    signals = spot_signal_quantities(pipeline.experiment, pipeline.run_baseline())
    table = standard_bias_table(
        finalized.parallel_results,
        pipeline.standard_archive,
        date=date,
        quantity=quantity,
        signal_quantities=signals,
        uncertainty=(
            "with_calibration"
            if session.include_calibration_uncertainty
            else "analytical"
        ),
    )
    summary = bias_summary(table, log_x=log_x)
    from UPbRPy.radiation_damage import ALPHA_DOSE_TOOLTIP
    return {
        "quantities": quantities,
        "unknowns": project_unknowns(finalized.parallel_results, pipeline.standard_archive, summary["trend"],
                                     date=date, quantity=quantity, log_x=log_x, signal_quantities=signals),
        "x_tooltip": ALPHA_DOSE_TOOLTIP if quantity == "alpha_dose" else "",
        "table": table,
        "summary": summary,
        "log_x": log_x,
        "x_label": str(quantities.get(quantity, quantity)),
        "y_label": description["axis"],
        "date": date,
        "date_tooltip": description["tooltip"],
    }


def get_calibration_slope_data(
    session: ReductionSession,
    preset: WorkflowPreset,
) -> dict | None:
    """Return a dict of arrays for drawing the concentration calibration slopes."""
    pipeline = ensure_shared_pipeline(session, preset) if session.loaded else None
    conc_results = session.reduction_cache.get("concentration_results")
    if not isinstance(conc_results, dict):
        return None
    calibration = conc_results.get("calibration")
    if not isinstance(calibration, pd.Series) or calibration.empty:
        return None

    normalized = session.reduction_cache.get("normalized_plateau_data")
    if not isinstance(normalized, pd.DataFrame) or normalized.empty:
        return None
    standards = conc_results.get("active_standards", preset.concentration_standards)
    calibrants = normalized[normalized["sample"].isin(standards)]
    if calibrants.empty:
        return None

    archive = pipeline.standard_archive if pipeline is not None else current_standard_archive()
    channels = [channel for channel in calibration.index if channel in calibrants]
    groups = calibrants.groupby("spot_name", sort=False)
    means = groups[channels].mean()
    samples = groups["sample"].first().astype(str)
    channel_data: dict[str, dict] = {}
    for channel in channels:
        concentrations = {}
        for sample in samples.unique():
            try:
                concentrations[sample], _ = resolve_element_concentration(archive, sample, channel)
            except (KeyError, ValueError):
                continue
        ppm = samples.map(concentrations).to_numpy(dtype=float)
        cps = means[channel].to_numpy(dtype=float)
        valid = np.isfinite(ppm) & (ppm > 0) & np.isfinite(cps)
        if valid.any():
            channel_data[channel] = {
                "cps": cps[valid],
                "ppm": ppm[valid],
                "slope": float(calibration[channel]),
                "labels": samples[valid].tolist(),
                "spot_names": means.index[valid].astype(str).tolist(),
            }

    if not channel_data:
        return None
    return {
        "channels": [c for c in channels if c in channel_data],
        "channel_data": channel_data,
    }


def build_notes(session: ReductionSession, spot_name: str | None, preset: WorkflowPreset, current_step: str | None = None) -> str:
    if not session.loaded:
        return (
            f"{preset.description} Import defaults: {format_import_defaults(preset)} "
            "The GUI shell stays separate from the UPbRPy reduction engine so notebook behavior remains backward-compatible."
        )

    spot_data = get_spot_data(session, spot_name)
    sample_names = sorted(spot_data["sample"].dropna().unique().tolist()) if "sample" in spot_data else []
    sample_label = ", ".join(sample_names[:3]) if sample_names else "Unknown sample"
    import_defaults = format_import_defaults(preset)
    next_step = f" Focus step: {current_step}." if current_step else ""
    return (
        f"Loaded {len(session.spot_names)} spots from {Path(session.directory).name}. "
        f"Viewing {spot_name} from {sample_label}. Using the {preset.title.lower()} notebook sequence with {import_defaults} "
        f"{preset.special_note}{next_step} The spot trace view is backed by pyqtgraph while reductions continue to come from the shared UPbRPy source functions."
    )
