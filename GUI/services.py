from __future__ import annotations

import re
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from GUI.state import ReductionSession
from GUI.workflows import (
    WorkflowPreset,
    format_import_defaults,
    get_workflow_preset,
    infer_workflow_preset,
)
from UPbRPy.calibration import evaluate_correction_result
from UPbRPy.constants import standard_archive_legacy
from UPbRPy.io import read_experiment_data, resolve_session_manifest, select_signal_regions
from UPbRPy.qtegra import QtegraSession, open_qtegra
from UPbRPy.session_manifest import find_session_manifest, raw_directory_for
from UPbRPy.reproducibility import selection_export_frame
from UPbRPy.pipeline import (
    FitSettings,
    PipelineConfig,
    PipelineStage,
    ReductionPipeline,
    pipeline_config_from_dict,
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
_active_standard_archive = standard_archive.combine_first(
    standard_archive_legacy
)
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


def config_invalidation_stage(
    previous: PipelineConfig,
    current: PipelineConfig,
) -> PipelineStage:
    """Return the earliest stage affected by a configuration change."""
    if previous.detector_calibration != current.detector_calibration:
        return PipelineStage.BASELINE
    if (
        previous.material != current.material
        or previous.primary_channel != current.primary_channel
    ):
        return PipelineStage.NORMALIZATION
    if previous.concentration_standards != current.concentration_standards:
        return PipelineStage.CONCENTRATION
    if (
        previous.primary_68_standard != current.primary_68_standard
        or previous.primary_76_standard != current.primary_76_standard
        or previous.primary_82_standard != current.primary_82_standard
        or previous.ratio_names != current.ratio_names
        or previous.ratio_aggregation != current.ratio_aggregation
        or previous.common_pb_calibration != current.common_pb_calibration
        or previous.downhole != current.downhole
    ):
        return PipelineStage.DOWNHOLE
    if previous.session != current.session:
        return PipelineStage.SESSION
    if previous.uncertainty != current.uncertainty:
        # Both the spot decision branch and the independently cached depth
        # product carry these reporting components.
        return PipelineStage.SESSION
    if (
        previous.decision != current.decision
        or previous.automatic_flags != current.automatic_flags
        or previous.common_pb_options != current.common_pb_options
    ):
        return PipelineStage.FINALIZE
    return PipelineStage.FINALIZE


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
    elif not files:
        files = [path for path, kind in formats.items() if kind == "upbr"]
    return tuple(sorted(files))


def ensure_shared_pipeline(
    session: ReductionSession,
    preset: WorkflowPreset,
) -> ReductionPipeline:
    """Return the session's core pipeline, synchronizing current GUI choices."""
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
        if pipeline.experiment is not session.experiment:
            pipeline.update_experiment(session.experiment)
        if pipeline.standard_archive is not current_standard_archive():
            pipeline.standard_archive = current_standard_archive()
            pipeline.reference_library_path = Path(
                current_standard_archive_path()
            )
            pipeline.cache.invalidate(PipelineStage.NORMALIZATION)
    return session.pipeline


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
    """Load the primary TOML library or an explicitly selected legacy CSV."""
    global _active_standard_archive
    global _active_standard_archive_path

    if not path:
        _active_standard_archive = standard_archive.combine_first(
            standard_archive_legacy
        )
        _active_standard_archive_path = DEFAULT_REFERENCE_LIBRARY_PATH
        return _active_standard_archive

    target_path = Path(path).expanduser().resolve()
    if target_path.suffix.lower() == ".csv":
        loaded_archive = pd.read_csv(target_path, index_col=0)
    else:
        measurements = load_standard_measurements(target_path)
        loaded_archive = build_standard_archive(measurements).combine_first(
            standard_archive_legacy
        )
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


def load_session(
    directory: str,
    primary_channel: str | None = None,
    preset: WorkflowPreset | None = None,
    selection_method: str | None = None,
    import_settings: Mapping[str, object] | None = None,
) -> ReductionSession:
    """Open a Qtegra file or folder once and import with preset/restored settings."""
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
        cycle_time_s=read_kwargs.get("cycle_time_s"),
    )
    experiment, channels, total_data = read_experiment_data(
        qtegra,
        primary_channel=effective_primary,
        **{**read_kwargs, "session_manifest": manifest or False},
    )
    if "plateau_filter_method" not in experiment.columns:
        experiment["plateau_filter_method"] = (
            workflow_preset.plateau_filter_method
        )
    if "plateau_filter_channel" not in experiment.columns:
        experiment["plateau_filter_channel"] = effective_primary
    clean_channels = [str(channel) for channel in channels if str(channel)]
    resolved_primary = effective_primary if effective_primary in clean_channels else clean_channels[0]
    selected_spot = str(experiment["spot_names"].iloc[0]) if not experiment.empty else None
    session = ReductionSession(
        directory=str(directory),
        qtegra=qtegra,
        experiment=experiment,
        total_data=total_data,
        channels=clean_channels,
        primary_channel=resolved_primary,
        workflow_key=workflow_preset.key,
        workflow_title=workflow_preset.title,
        concentration_standards=workflow_preset.concentration_standards,
        primary_68_standard=workflow_preset.primary_standard,
        primary_76_standard=(
            workflow_preset.pb_ratio_standard
            or workflow_preset.primary_standard
        ),
        primary_82_standard=(
            workflow_preset.th_ratio_standard
            or workflow_preset.pb_ratio_standard
            or workflow_preset.primary_standard
        ),
        secondary_standards=workflow_preset.secondary_standards,
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
) -> None:
    """Populate GUI compatibility keys from recalculated shared-core stages."""
    completed = set(completed_stages)
    for stage in PipelineStage:
        if stage not in completed:
            continue
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


def restore_session_from_project(project_file: str | Path) -> RestoredProject:
    """Re-import raw data and recalculate a saved lightweight GUI project."""
    project_path = Path(project_file).expanduser().resolve(strict=False)
    document = read_reduction_project(project_path)
    paths = dict(document["paths"])
    pipeline_values = dict(document["pipeline"])
    workspace = dict(document.get("workspace", {}))
    config = pipeline_config_from_dict(
        dict(pipeline_values["config"])
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
    )
    if document["selections"]:
        session.experiment = restore_project_selections(
            session.experiment,
            document["selections"],
            strict_spot_names=True,
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

    visible_channels = workspace.get("visible_channels", ())
    if isinstance(visible_channels, (list, tuple)):
        session.visible_channels = [
            str(channel)
            for channel in visible_channels
            if str(channel) in session.channels
        ]
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
    populate_recalculated_cache(session, preset, completed)
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


def clear_reduction_cache(session: ReductionSession) -> None:
    """Discard calculated products while preserving import and selections."""
    session.reduction_cache.clear()
    if session.pipeline is not None:
        session.pipeline.cache.invalidate(PipelineStage.BASELINE)


def update_selection_region(
    session: ReductionSession,
    spot_name: str,
    *,
    baseline: tuple[float, float] | None = None,
    plateau: tuple[float, float] | None = None,
    signal: tuple[float, float] | None = None,
    refresh_pipeline: bool = True,
) -> None:
    """Save reviewed windows while requiring signal bounds to contain plateau."""
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
    if refresh_pipeline:
        clear_reduction_cache(session)
        if session.pipeline is not None:
            session.pipeline.update_experiment(session.experiment)


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
    selection = select_signal_regions(
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
    if refresh_pipeline:
        clear_reduction_cache(session)
    if refresh_pipeline and session.pipeline is not None:
        # Signal limits are written after the baseline/plateau update above.
        # Refresh once more so the pipeline sees the complete reviewed pick.
        session.pipeline.update_experiment(session.experiment)
    return {"baseline": baseline, "plateau": plateau, "signal": signal}


def autopick_all_selection_regions(
    session: ReductionSession,
) -> dict[str, object]:
    """Re-pick every spot and refresh the shared pipeline only once.

    Grouping the sweep table before selection avoids repeatedly scanning the
    complete session.  Individual failures are retained in the experiment
    table and returned to the GUI, while valid spots remain usable.
    """
    if not session.loaded:
        return {"selected_count": 0, "failures": {}}

    spot_frames = {
        str(spot_name): frame.reset_index(drop=True)
        for spot_name, frame in session.total_data.groupby(
            "spot_name", sort=False
        )
    }
    spot_names = session.experiment["spot_names"].astype(str).tolist()
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
    if session.pipeline is not None:
        session.pipeline.update_experiment(session.experiment)
    return {"selected_count": selected_count, "failures": failures}


def get_spot_data(session: ReductionSession, spot_name: str | None) -> pd.DataFrame:
    if not session.loaded or not spot_name:
        return pd.DataFrame()
    return session.total_data[session.total_data["spot_name"] == spot_name].reset_index(drop=True)


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
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    if not session.loaded:
        return pd.DataFrame()
    preset = get_workflow_preset(session.workflow_key)
    baseline_subtracted = ensure_shared_pipeline(session, preset).run_baseline()
    session.reduction_cache[cache_key] = baseline_subtracted
    return baseline_subtracted


def ensure_normalized_plateau(session: ReductionSession, preset: WorkflowPreset) -> pd.DataFrame:
    """Return internally normalized plateau data from the shared pipeline."""
    cache_key = "normalized_plateau_data"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    if not session.loaded:
        return pd.DataFrame()
    normalized = ensure_shared_pipeline(session, preset).run_normalization()
    session.reduction_cache[cache_key] = normalized
    return normalized


def ensure_concentration_results(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    """Return concentration calibration and result products from the core."""
    cache_key = "concentration_results"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
    if not preset.concentration_standards:
        raise ValueError(f"{preset.title} does not yet define concentration standards for the GUI workflow.")
    results = ensure_shared_pipeline(session, preset).run_concentration()
    session.reduction_cache["normalized_plateau_data"] = (
        ensure_shared_pipeline(session, preset).run_normalization()
    )
    session.reduction_cache[cache_key] = results
    return results


def ensure_raw_ratios(session: ReductionSession) -> pd.DataFrame:
    """Return plateau ratios calculated by the shared pipeline."""
    cache_key = "plateau_data_with_raw_ratios"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    if not session.loaded:
        return pd.DataFrame()
    preset = get_workflow_preset(session.workflow_key)
    ratios = ensure_shared_pipeline(session, preset).run_ratios()["plateau"]
    session.reduction_cache[cache_key] = ratios
    return ratios


def ensure_signal_raw_ratios(session: ReductionSession) -> pd.DataFrame:
    """Return full-signal ratios aligned to each spot's saved signal start."""
    cache_key = "signal_data_with_raw_ratios"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    if not session.loaded:
        return pd.DataFrame()
    preset = get_workflow_preset(session.workflow_key)
    ratios = ensure_shared_pipeline(session, preset).run_ratios()["signal"]
    session.reduction_cache[cache_key] = ratios
    return ratios


def ensure_downhole_results(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    """Return shared downhole models and corrected plateau/signal ratios."""
    cache_key = "downhole_results"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
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
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
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
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
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
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    result = ensure_shared_pipeline(session, preset).run_depth_profile()
    ensure_session_corrected(session, preset)
    session.reduction_cache[cache_key] = result
    return result


def ensure_finalized_results(
    session: ReductionSession,
    preset: WorkflowPreset,
):
    """Return common-Pb calculations, flags, and preferred-age decisions."""
    cache_key = "finalized_results"
    cached = session.reduction_cache.get(cache_key)
    if cached is not None:
        return cached
    result = ensure_shared_pipeline(session, preset).run_finalize()
    ensure_spot_means(session, preset)
    session.reduction_cache[cache_key] = result
    return result


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

def get_downhole_plot_data(
    session: ReductionSession,
    preset: WorkflowPreset,
    ratio_name: str = "206Pb/238U",
) -> dict | None:
    """Return a dict of arrays needed to draw the downhole fractionation diagnostic.

    Returns None if the prerequisite cache entries are absent.
    """
    raw_ratios = session.reduction_cache.get("signal_data_with_raw_ratios")
    downhole_results = session.reduction_cache.get("downhole_results")
    if not isinstance(raw_ratios, pd.DataFrame) or raw_ratios.empty:
        return None
    primary_standard = (
        session.primary_68_standard or preset.primary_standard
    )
    if not primary_standard:
        return None
    if ratio_name not in raw_ratios.columns:
        return None
    try:
        archive = current_standard_archive()
        known_ratio = float(archive.loc[primary_standard, ratio_name + "*"])
    except Exception:
        return None

    ps_rows = raw_ratios[raw_ratios["sample"] == primary_standard]
    if ps_rows.empty:
        return None

    spot_times: list[np.ndarray] = []
    spot_ratios: list[np.ndarray] = []
    spot_names: list[str] = []
    for spot_name in ps_rows["spot_name"].unique():
        this_spot = ps_rows[ps_rows["spot_name"] == spot_name]
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

    return {
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


def get_session_plot_data(
    session: ReductionSession,
    preset: WorkflowPreset,
    ratio_name: str = "206Pb/238U",
) -> dict | None:
    """Return a dict of arrays for drawing the session fractionation diagnostic."""
    session_results = session.reduction_cache.get("session_correction_results")
    downhole_results = session.reduction_cache.get("downhole_results")
    if not isinstance(session_results, dict) or not isinstance(downhole_results, dict):
        return None

    primary_standard = (
        session.primary_68_standard or preset.primary_standard
    )
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

    dh_corrected = downhole_results.get("plateau_corrected")
    spot_names_out: list[str] = []
    if isinstance(dh_corrected, pd.DataFrame):
        selected = dh_corrected[dh_corrected["sample"].eq(primary_standard)]
        spot_names_out = [
            str(name)
            for name in selected.groupby("spot_name", sort=False).groups
        ]
    if len(spot_names_out) != len(t_arr):
        spot_names_out = [f"standard {index + 1}" for index in range(len(t_arr))]

    return {
        "ratio_name": ratio_name,
        "known_ratio": known_ratio,
        "primary_standard": primary_standard,
        "spot_names": spot_names_out,
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


def get_calibration_slope_data(
    session: ReductionSession,
    preset: WorkflowPreset,
) -> dict | None:
    """Return a dict of arrays for drawing the concentration calibration slopes."""
    conc_results = session.reduction_cache.get("concentration_results")
    if not isinstance(conc_results, dict):
        return None
    calibration = conc_results.get("calibration")
    if not isinstance(calibration, pd.Series) or calibration.empty:
        return None

    normalized = session.reduction_cache.get("normalized_plateau_data")
    if not isinstance(normalized, pd.DataFrame) or normalized.empty:
        return None
    if not preset.concentration_standards:
        return None

    nist_data = normalized[normalized["sample"].isin(preset.concentration_standards)].reset_index(drop=True)
    if nist_data.empty:
        return None

    try:
        archive = current_standard_archive()
    except Exception:
        return None

    channels = list(calibration.index)
    channel_data: dict[str, dict] = {}
    for channel in channels:
        if channel not in nist_data.columns:
            continue
        cps_vals: list[float] = []
        ppm_vals: list[float] = []
        sample_labels: list[str] = []
        for sn in nist_data["spot_name"].unique():
            this_spot = nist_data[nist_data["spot_name"] == sn]
            sample = str(this_spot["sample"].values[0])
            if sample not in archive.index or channel not in archive.columns:
                continue
            known_ppm = float(archive.loc[sample, channel])
            if not np.isfinite(known_ppm) or known_ppm <= 0:
                continue
            mean_cps = float(this_spot[channel].mean())
            if not np.isfinite(mean_cps):
                continue
            cps_vals.append(mean_cps)
            ppm_vals.append(known_ppm)
            sample_labels.append(sample)
        if len(cps_vals) >= 2:
            channel_data[channel] = {
                "cps": np.array(cps_vals),
                "ppm": np.array(ppm_vals),
                "slope": float(calibration[channel]),
                "labels": sample_labels,
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
