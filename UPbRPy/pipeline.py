"""Shared, GUI-independent orchestration for complete UPbRPy reductions.

The numerical functions in the rest of :mod:`UPbRPy` remain independently
callable.  This module records how those functions are composed into one
reproducible workflow so notebooks and graphical applications do not develop
separate reduction algorithms.
"""

from __future__ import annotations

import hashlib
import math
import pickle
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from .depth import (
    LinearDepthCalibration,
    apply_depth_calibration,
    linear_depth_calibration_from_dict,
)
from .results import (
    AutomaticFlagSettings,
    FinalizedReduction,
    ReductionDecision,
    finalize_reduction_results,
)
from .uncertainty import UncertaintySettings
from .notices import ReductionError, ReductionNotice
from .standards import (
    canonicalize_reference_material_samples,
    reference_library_info,
)

if TYPE_CHECKING:
    from .export_table import ExportSettings

PIPELINE_FORMAT_VERSION = 2


class PipelineStage(str, Enum):
    """Stable identifiers for calculation and review stages."""

    IMPORT = "import"
    SELECTION = "selection"
    BASELINE = "baseline"
    NORMALIZATION = "normalization"
    CONCENTRATION = "concentration"
    RATIOS = "ratios"
    DOWNHOLE = "downhole"
    SESSION = "session"
    SPOT_MEANS = "spot_means"
    DEPTH_PROFILE = "depth_profile"
    FINALIZE = "finalize"
    EXPORT = "export"


@dataclass(frozen=True)
class StageDefinition:
    """Describe one pipeline stage and its direct prerequisites."""

    stage: PipelineStage
    title: str
    prerequisites: tuple[PipelineStage, ...] = ()
    optional: bool = False


PIPELINE_STAGE_DEFINITIONS: tuple[StageDefinition, ...] = (
    StageDefinition(PipelineStage.IMPORT, "Import and classify spots"),
    StageDefinition(
        PipelineStage.SELECTION,
        "Review baseline, plateau, and signal limits",
        (PipelineStage.IMPORT,),
    ),
    StageDefinition(
        PipelineStage.BASELINE,
        "Subtract spot baselines",
        (PipelineStage.SELECTION,),
    ),
    StageDefinition(
        PipelineStage.NORMALIZATION,
        "Apply internal normalization",
        (PipelineStage.BASELINE,),
        optional=True,
    ),
    StageDefinition(
        PipelineStage.CONCENTRATION,
        "Calibrate elemental concentrations",
        (PipelineStage.NORMALIZATION,),
        optional=True,
    ),
    StageDefinition(
        PipelineStage.RATIOS,
        "Build plateau and signal isotope ratios",
        (PipelineStage.BASELINE,),
    ),
    StageDefinition(
        PipelineStage.DOWNHOLE,
        "Model and apply downhole fractionation",
        (PipelineStage.RATIOS,),
    ),
    StageDefinition(
        PipelineStage.SESSION,
        "Model and apply session fractionation",
        (PipelineStage.DOWNHOLE,),
    ),
    StageDefinition(
        PipelineStage.SPOT_MEANS,
        "Calculate spot-mean ratios and ages",
        (PipelineStage.SESSION, PipelineStage.CONCENTRATION),
    ),
    StageDefinition(
        PipelineStage.DEPTH_PROFILE,
        "Calculate sweep-wise depth profiles",
        (PipelineStage.SESSION, PipelineStage.CONCENTRATION),
        optional=True,
    ),
    StageDefinition(
        PipelineStage.FINALIZE,
        "Apply common-Pb, QC, and preferred-age decisions",
        (PipelineStage.SPOT_MEANS,),
    ),
    StageDefinition(
        PipelineStage.EXPORT,
        "Export publication and reproducibility products",
        (PipelineStage.FINALIZE,),
    ),
)

PIPELINE_STAGE_BY_ID = {
    definition.stage: definition for definition in PIPELINE_STAGE_DEFINITIONS
}


def pipeline_descendants(stage: PipelineStage | str) -> tuple[PipelineStage, ...]:
    """Return every stage whose result depends directly or indirectly on ``stage``."""
    resolved = PipelineStage(stage)
    descendants: set[PipelineStage] = set()
    changed = True
    while changed:
        changed = False
        for definition in PIPELINE_STAGE_DEFINITIONS:
            if definition.stage in descendants:
                continue
            if resolved in definition.prerequisites or any(
                prerequisite in descendants
                for prerequisite in definition.prerequisites
            ):
                descendants.add(definition.stage)
                changed = True
    return tuple(
        definition.stage
        for definition in PIPELINE_STAGE_DEFINITIONS
        if definition.stage in descendants
    )


def callable_reference(value: str | Callable[..., object]) -> str:
    """Return a stable display/import reference for a configured model."""
    if isinstance(value, str):
        return value
    module = getattr(value, "__module__", "")
    qualified_name = getattr(
        value,
        "__qualname__",
        getattr(value, "__name__", value.__class__.__name__),
    )
    return f"{module}.{qualified_name}" if module else str(qualified_name)


def resolve_calibration_method(
    value: str | Callable[..., object],
) -> str | Callable[..., object]:
    """Restore an importable custom model recorded by ``callable_reference``.

    Built-in method names contain no module separator and are returned
    unchanged. Dotted values are resolved by importing the longest valid
    module prefix and traversing its remaining attributes. This permits an
    exported notebook to replay module-level functions and static methods.
    """
    if callable(value) or "." not in str(value):
        return value
    parts = str(value).split(".")
    for split_index in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split_index])
        try:
            target: object = import_module(module_name)
        except ImportError:
            continue
        try:
            for attribute in parts[split_index:]:
                target = getattr(target, attribute)
        except AttributeError:
            continue
        if callable(target):
            return target
    raise ValueError(
        f"Custom calibration model {value!r} is not importable. "
        "Define it in an installed module before replaying this pipeline."
    )


@dataclass(frozen=True)
class FitSettings:
    """Configuration for one downhole or session correction model."""

    method: str | Callable[..., object] = "mean"
    options: dict[str, object] = field(default_factory=dict)
    outlier_sigma: float | None = None

    def to_dict(self) -> dict[str, object]:
        """Return JSON/TOML-friendly model settings for provenance."""
        return {
            "method": callable_reference(self.method),
            "options": dict(self.options),
            "outlier_sigma": self.outlier_sigma,
        }


@dataclass(frozen=True)
class PipelineConfig:
    """Complete scientific choices needed to reproduce a reduction.

    The three primary-standard fields may name the same material.  Separate
    names are required for common-Pb-rich workflows where Pb/U, Pb/Pb, and
    Th/Pb fractionation are established by different reference materials.
    """

    workflow_key: str = "zircon"
    material: str = "zircon"
    primary_channel: str = "29Si"
    selection_method: str = "threshold"
    selection_options: dict[str, object] = field(default_factory=dict)
    concentration_standards: tuple[str, ...] = ()
    primary_68_standard: str = ""
    primary_76_standard: str = ""
    primary_82_standard: str = ""
    secondary_standards: tuple[str, ...] = ()
    ratio_names: tuple[str, ...] = (
        "206Pb/238U",
        "207Pb/206Pb",
        "208Pb/232Th",
    )
    ratio_aggregation: str = "arithmetic"
    common_pb_calibration: str = "radiogenic"
    downhole: FitSettings = field(
        default_factory=lambda: FitSettings(
            method="mean",
            outlier_sigma=5.0,
        )
    )
    session: FitSettings = field(
        default_factory=lambda: FitSettings(
            method="polynomial",
            options={"degree": 2},
        )
    )
    decision: ReductionDecision = field(default_factory=ReductionDecision)
    automatic_flags: AutomaticFlagSettings = field(
        default_factory=AutomaticFlagSettings
    )
    common_pb_options: dict[str, object] = field(default_factory=dict)
    uncertainty: UncertaintySettings = field(default_factory=UncertaintySettings)
    depth_calibration: LinearDepthCalibration | None = None
    detector_calibration: dict = field(default_factory=dict)
    export_settings: ExportSettings | None = None
    internal_standard_channel: str | None = None
    reference_overrides: dict[str, dict[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate calibration mode and required configuration invariants."""
        overrides = {}
        from .concentration import element_from_channel
        for standard, values in self.reference_overrides.items():
            overrides[standard] = {}
            for channel, value in values.items():
                if element_from_channel(channel) is None or not math.isfinite(float(value)) or float(value) <= 0:
                    raise ReductionError(
                        f"Invalid reference value for {standard}, {channel}.", "normalization",
                        "Enter a finite concentration greater than zero in µg/g.",
                    )
                overrides[standard][channel] = float(value)
        object.__setattr__(self, "reference_overrides", overrides)
        if self.export_settings is not None:
            from .export_table import ExportSettings

            if not isinstance(self.export_settings, ExportSettings):
                raise TypeError("export_settings must be ExportSettings or None")
        mode = str(self.common_pb_calibration).strip().lower()
        if mode not in {"radiogenic", "tera_wasserburg"}:
            raise ValueError(
                "common_pb_calibration must be 'radiogenic' or "
                "'tera_wasserburg'"
            )
        if not self.ratio_names:
            raise ValueError("ratio_names must contain at least one ratio")
        from .ratios import normalize_ratio_aggregation_method

        object.__setattr__(self, "common_pb_calibration", mode)
        object.__setattr__(
            self,
            "ratio_aggregation",
            normalize_ratio_aggregation_method(self.ratio_aggregation),
        )
        if isinstance(self.depth_calibration, Mapping):
            object.__setattr__(
                self,
                "depth_calibration",
                linear_depth_calibration_from_dict(self.depth_calibration),
            )
        elif (
            self.depth_calibration is not None
            and not isinstance(self.depth_calibration, LinearDepthCalibration)
        ):
            raise TypeError(
                "depth_calibration must be LinearDepthCalibration, a saved "
                "mapping, or None."
            )
        if isinstance(self.uncertainty, Mapping):
            object.__setattr__(
                self,
                "uncertainty",
                UncertaintySettings(**dict(self.uncertainty)),
            )
        elif not isinstance(self.uncertainty, UncertaintySettings):
            raise TypeError("uncertainty must be UncertaintySettings or a mapping")

    @property
    def primary_standard(self) -> str:
        """Return the Pb/U primary standard used by most workflow labels."""
        return self.primary_68_standard

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic, serialization-friendly configuration."""
        return {
            "workflow_key": self.workflow_key,
            "material": self.material,
            "primary_channel": self.primary_channel,
            "internal_standard_channel": self.internal_standard_channel,
            "reference_overrides": {name: dict(values) for name, values in self.reference_overrides.items()},
            "selection_method": self.selection_method,
            "selection_options": dict(self.selection_options),
            "detector_calibration": dict(self.detector_calibration),
            "concentration_standards": list(self.concentration_standards),
            "primary_68_standard": self.primary_68_standard,
            "primary_76_standard": self.primary_76_standard,
            "primary_82_standard": self.primary_82_standard,
            "secondary_standards": list(self.secondary_standards),
            "ratio_names": list(self.ratio_names),
            "ratio_aggregation": self.ratio_aggregation,
            "common_pb_calibration": self.common_pb_calibration,
            "downhole": self.downhole.to_dict(),
            "session": self.session.to_dict(),
            "decision": asdict(self.decision),
            "automatic_flags": asdict(self.automatic_flags),
            "common_pb_options": dict(self.common_pb_options),
            "uncertainty": self.uncertainty.to_dict(),
            "export_settings": (None if self.export_settings is None
                                else self.export_settings.to_dict()),
            "depth_calibration": (
                None
                if self.depth_calibration is None
                else self.depth_calibration.to_dict()
            ),
        }


def config_invalidation_stage(
    previous: PipelineConfig,
    current: PipelineConfig,
) -> PipelineStage:
    """Return the earliest stage affected by a configuration change.

    Check stages in dependency order so earlier changes take precedence when
    reference settings and other configuration fields change together. Callers
    pass the result to ``ReductionPipeline.update_config`` explicitly.
    """
    if previous.detector_calibration != current.detector_calibration:
        return PipelineStage.BASELINE
    if (
        previous.material != current.material
        or previous.primary_channel != current.primary_channel
        or previous.internal_standard_channel != current.internal_standard_channel
        or previous.reference_overrides != current.reference_overrides
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


def pipeline_config_from_dict(values: Mapping[str, object]) -> PipelineConfig:
    """Reconstruct immutable pipeline settings from a saved manifest."""
    data = dict(values)
    data["detector_calibration"] = dict(data.get("detector_calibration", {}))
    data["concentration_standards"] = tuple(
        data.get("concentration_standards", ())
    )
    data["secondary_standards"] = tuple(data.get("secondary_standards", ()))
    data["ratio_names"] = tuple(
        data.get("ratio_names", PipelineConfig().ratio_names)
    )
    downhole_values = dict(data.get("downhole", {}))
    downhole_values["method"] = resolve_calibration_method(
        downhole_values.get("method", "mean")
    )
    session_values = dict(data.get("session", {}))
    session_values["method"] = resolve_calibration_method(
        session_values.get("method", "polynomial")
    )
    data["downhole"] = FitSettings(**downhole_values)
    data["session"] = FitSettings(**session_values)
    data["decision"] = ReductionDecision(**dict(data.get("decision", {})))
    data["automatic_flags"] = AutomaticFlagSettings(
        **dict(data.get("automatic_flags", {}))
    )
    data["uncertainty"] = UncertaintySettings(
        **dict(data.get("uncertainty", {}))
    )
    if data.get("export_settings") is not None:
        from .export_table import ExportSettings

        data["export_settings"] = ExportSettings.from_dict(data["export_settings"])
    depth_calibration = data.get("depth_calibration")
    if isinstance(depth_calibration, Mapping):
        data["depth_calibration"] = linear_depth_calibration_from_dict(
            depth_calibration
        )
    return PipelineConfig(**data)


class PipelineCache:
    """Stage-keyed result cache with dependency-aware invalidation."""

    def __init__(self) -> None:
        """Create an empty stage cache."""
        self._results: dict[PipelineStage, object] = {}

    def get(self, stage: PipelineStage | str, default=None):
        """Return a cached stage result or ``default``."""
        return self._results.get(PipelineStage(stage), default)

    def set(self, stage: PipelineStage | str, result: object) -> object:
        """Store and return a stage result."""
        self._results[PipelineStage(stage)] = result
        return result

    def has(self, stage: PipelineStage | str) -> bool:
        """Return whether a stage currently has a cached result."""
        return PipelineStage(stage) in self._results

    def invalidate(
        self,
        stage: PipelineStage | str,
        *,
        include_stage: bool = True,
    ) -> tuple[PipelineStage, ...]:
        """Drop ``stage`` and every cached result that depends on it."""
        resolved = PipelineStage(stage)
        targets = list(pipeline_descendants(resolved))
        if include_stage:
            targets.insert(0, resolved)
        removed = []
        for target in targets:
            if target in self._results:
                del self._results[target]
                removed.append(target)
        return tuple(removed)

    def clear(self) -> None:
        """Discard every cached stage result."""
        self._results.clear()

    def stages(self) -> tuple[PipelineStage, ...]:
        """Return cached stages in workflow order."""
        return tuple(
            definition.stage
            for definition in PIPELINE_STAGE_DEFINITIONS
            if definition.stage in self._results
        )

    def as_mapping(self) -> Mapping[PipelineStage, object]:
        """Expose a read-only-style snapshot of cached stage results."""
        return dict(self._results)


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Calculate a SHA-256 digest without reading the entire file into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def package_version() -> str:
    """Return the installed UPbRPy version with a source-tree fallback."""
    try:
        return version("UPbRPy")
    except PackageNotFoundError:
        return "0.1.0+source"


def _experiment_fingerprint(experiment: pd.DataFrame) -> bytes:
    """Hash selection contents, including nested object cells and provenance.

    Serialize values rather than pandas' internal blocks so consolidation does
    not change the digest. Unlike hash_pandas_object, this accepts list and
    dict cells as well as arrays containing excluded sweep indices.
    """
    contents = (
        experiment.index.tolist(),
        experiment.columns.tolist(),
        tuple(str(dtype) for dtype in experiment.dtypes),
        experiment.to_numpy(dtype=object).tolist(),
        experiment.attrs,
    )
    return hashlib.sha256(pickle.dumps(contents, protocol=5)).digest()


class ReductionPipeline:
    """Execute and cache one complete UPbRPy reduction.

    Parameters are plain Python objects and pandas frames; the class imports no
    GUI toolkit.  Frontends can therefore run its methods in workers, while
    notebooks can call the identical methods synchronously.
    """

    def __init__(
        self,
        experiment: pd.DataFrame,
        total_data: pd.DataFrame,
        channels: list[str] | tuple[str, ...],
        standard_archive: pd.DataFrame,
        config: PipelineConfig,
        *,
        source_directory: str | Path | None = None,
        reference_library_path: str | Path | None = None,
        input_files: tuple[str | Path, ...] = (),
        build_diagnostic_plots: bool = True,
        origin: Mapping[str, object] | None = None,
    ) -> None:
        """Initialize a pipeline around imported data and immutable settings.

        ``build_diagnostic_plots`` is a frontend concern, not a scientific
        reduction setting. Notebook callers retain Bokeh diagnostics by
        default; native GUIs can disable them and render the returned numeric
        metadata with their own lightweight plotting widgets.

        ``origin`` is the optional database origin of the session (the
        ``origin`` block of a format-2 ``.upbr``); it is provenance only and
        is echoed as ``metadata.origin`` in :meth:`manifest`.
        """
        if not isinstance(experiment, pd.DataFrame):
            raise TypeError("experiment must be a pandas DataFrame")
        if not isinstance(total_data, pd.DataFrame):
            raise TypeError("total_data must be a pandas DataFrame")
        if not isinstance(standard_archive, pd.DataFrame):
            raise TypeError("standard_archive must be a pandas DataFrame")
        self.experiment = canonicalize_reference_material_samples(
            experiment,
            standard_archive,
        )
        self._source_experiment = experiment
        self._source_experiment_fingerprint = _experiment_fingerprint(experiment)
        self.total_data = canonicalize_reference_material_samples(
            total_data,
            standard_archive,
        )
        self.channels = tuple(str(channel) for channel in channels)
        self.base_standard_archive = standard_archive
        self._override_archive_key = None
        self.config = config
        self.source_directory = (
            None if source_directory is None else Path(source_directory)
        )
        self.reference_library_path = (
            None
            if reference_library_path is None
            else Path(reference_library_path)
        )
        self.input_files = tuple(Path(path) for path in input_files)
        self.build_diagnostic_plots = bool(build_diagnostic_plots)
        self.origin = None if origin is None else dict(origin)
        self.notices: list[ReductionNotice] = []
        self.cache = PipelineCache()
        self.cache.set(
            PipelineStage.IMPORT,
            {
                "spot_count": int(len(experiment)),
                "sweep_count": int(len(total_data)),
                "channels": list(self.channels),
            },
        )
        self.cache.set(PipelineStage.SELECTION, self.experiment)

    @property
    def standard_archive(self) -> pd.DataFrame:
        """Return pipeline-local reference values, leaving the shared library intact."""
        from .concentration import element_from_channel

        overrides = self.config.reference_overrides
        if not overrides:
            return self.base_standard_archive
        key = (id(self.base_standard_archive), pickle.dumps(overrides))
        if key != self._override_archive_key:
            archive = self.base_standard_archive.copy(deep=True)
            for standard, values in overrides.items():
                if standard not in archive.index:
                    raise ReductionError(
                        f"Unknown reference standard {standard}.", "normalization",
                        "Choose a standard from the reference material library.", ('choose_standard',),
                    )
                for channel, value in values.items():
                    # Reference values are total-element concentrations, so an
                    # override must also apply to other masses of this element.
                    for existing in archive.columns:
                        if element_from_channel(existing) == element_from_channel(channel):
                            archive.loc[standard, existing] = value
                    archive.loc[standard, channel] = value
            self._effective_standard_archive = archive
            self._override_archive_key = key
        return self._effective_standard_archive

    @standard_archive.setter
    def standard_archive(self, archive: pd.DataFrame) -> None:
        self.base_standard_archive = archive
        self._override_archive_key = None

    def resolve_internal_standard_channel(self) -> str:
        """Resolve the material's internal standard independently of signal picking."""
        from .concentration import resolve_internal_standard_channel

        return resolve_internal_standard_channel(
            self.channels, self.config.material, self.config.internal_standard_channel,
        )

    def _clear_notices(self, stage: PipelineStage) -> None:
        self.notices[:] = [notice for notice in self.notices if notice.stage != stage.value]

    def matches_source_experiment(self, experiment: pd.DataFrame) -> bool:
        """Return whether the source frame and its contents match the last sync.

        The canonicalized pipeline frame is a copy. Track its original source
        separately so both replacements and in-place window edits are detected.
        """
        return (
            experiment is self._source_experiment
            and _experiment_fingerprint(experiment)
            == self._source_experiment_fingerprint
        )

    def update_experiment(self, experiment: pd.DataFrame) -> None:
        """Replace selection metadata and invalidate every calculated stage."""
        if not isinstance(experiment, pd.DataFrame):
            raise TypeError("experiment must be a pandas DataFrame")
        self.experiment = canonicalize_reference_material_samples(
            experiment,
            self.standard_archive,
        )
        self._source_experiment = experiment
        self._source_experiment_fingerprint = _experiment_fingerprint(experiment)
        self.cache.invalidate(PipelineStage.SELECTION)
        self.cache.set(PipelineStage.SELECTION, self.experiment)

    def update_config(
        self,
        config: PipelineConfig,
        *,
        invalidate_from: PipelineStage | str = PipelineStage.BASELINE,
    ) -> None:
        """Replace settings and invalidate from the caller-selected stage."""
        self.config = config
        self.cache.invalidate(invalidate_from)

    def require_samples(self, names: tuple[str, ...], label: str,
                        stage: str = "downhole") -> None:
        """Raise a focused error when configured reference materials are absent."""
        requested = tuple(name for name in names if name)
        if not requested:
            raise ReductionError(f"No {label} is configured.", stage,
                                 "Choose a measured standard in Calibration materials.", ("choose_standard",))
        if "sample" not in self.total_data.columns:
            raise ReductionError("The loaded data has no sample labels.", "import",
                                 "Open a Qtegra export with sample labels.")
        available = set(
            self.total_data["sample"].dropna().astype(str).unique().tolist()
        )
        missing = [name for name in requested if name not in available]
        if missing:
            raise ReductionError(
                f"Missing {label} in the loaded session: {', '.join(missing)}.", stage,
                "Choose a standard measured in this session in Calibration materials.", ("choose_standard",),
            )

    def run_baseline(self) -> pd.DataFrame:
        """Subtract each spot/channel baseline using the reviewed selections."""
        from .io import subtract_baseline

        cached = self.cache.get(PipelineStage.BASELINE)
        if isinstance(cached, pd.DataFrame):
            return cached
        self._clear_notices(PipelineStage.BASELINE)
        if self.experiment.empty or self.total_data.empty:
            raise ReductionError(
                "No analyses are selected for reduction.", "selection",
                "Open a session and review its baseline and signal selections.",
            )
        result = subtract_baseline(
            self.experiment,
            list(self.channels),
            self.total_data,
        )
        return self.cache.set(PipelineStage.BASELINE, result)

    def run_normalization(self) -> pd.DataFrame:
        """Apply the configured material's internal elemental normalization."""
        from .concentration import internal_normalization

        cached = self.cache.get(PipelineStage.NORMALIZATION)
        if isinstance(cached, pd.DataFrame):
            return cached
        self._clear_notices(PipelineStage.NORMALIZATION)
        result = internal_normalization(
            self.experiment,
            list(self.channels),
            self.run_baseline(),
            self.standard_archive,
            material=self.config.material,
            internal_channel=self.resolve_internal_standard_channel(),
            region="plateau",
            notices=self.notices,
        )
        return self.cache.set(PipelineStage.NORMALIZATION, result)

    def run_concentration(self) -> dict[str, object]:
        """Fit concentration factors and calculate non-calibrant concentrations."""
        from .concentration import (
            calc_concentration,
            calc_concentration_calibration_factor,
        )

        cached = self.cache.get(PipelineStage.CONCENTRATION)
        if isinstance(cached, dict):
            return cached
        self._clear_notices(PipelineStage.CONCENTRATION)
        standards = self.config.concentration_standards
        normalized = self.run_normalization()
        available_samples = set(
            normalized["sample"].dropna().astype(str).unique()
        )
        active_standards = tuple(
            standard
            for standard in standards
            if standard in available_samples
        )
        if not active_standards:
            self.require_samples(standards, "concentration standards", "concentration")
        if normalized.empty:
            raise ReductionError(
                "No analyses have selected plateau sweeps for normalization.", "normalization",
                "Review plateau selections and supply missing internal reference values.",
                ('choose_standard',),
            )
        calibrants = normalized[
            normalized["sample"].isin(active_standards)
        ].reset_index(drop=True)
        calibration, plot = calc_concentration_calibration_factor(
            calibrants,
            list(self.channels),
            self.standard_archive,
            internal_channel=self.resolve_internal_standard_channel(),
            build_plot=self.build_diagnostic_plots,
            notices=self.notices,
        )
        unknowns = normalized[
            ~normalized["sample"].isin(active_standards)
        ].reset_index(drop=True)
        # This cache belongs to this normalization result. Export can reuse
        # unscaled statistics even when it fits different Pb isotope factors.
        self._concentration_statistics_cache = {}
        concentrations = calc_concentration(
            unknowns, calibration,
            statistics_cache=self._concentration_statistics_cache,
        )
        result = {
            "calibration": calibration,
            "calibration_plot": plot,
            "final_concentration_df": concentrations,
            "calibrant_count": int(len(calibrants)),
            "result_count": int(len(concentrations)),
            "active_standards": active_standards,
            "missing_standards": tuple(
                standard
                for standard in standards
                if standard not in active_standards
            ),
        }
        return self.cache.set(PipelineStage.CONCENTRATION, result)

    @property
    def counting_dwell_s(self) -> dict[str, float | None]:
        """Per-channel integration time that converts cps to counts.

        Set by the readers in ``experiment.attrs["counting_dwell"]``; empty
        for hand-built tables, whose signals are then taken as counts.
        """
        record = self._source_experiment.attrs.get("counting_dwell") or {}
        return dict(record.get("dwell_s") or {})

    def run_ratios(self) -> dict[str, pd.DataFrame]:
        """Calculate filtered plateau and complete signal isotope ratios."""
        from .ratios import calc_raw_ratios

        cached = self.cache.get(PipelineStage.RATIOS)
        if isinstance(cached, dict):
            return cached
        self._clear_notices(PipelineStage.RATIOS)
        baseline = self.run_baseline()
        result = {
            region: calc_raw_ratios(
                self.experiment,
                self.total_data,
                baseline,
                region=region,
                dwell_s=self.counting_dwell_s,
            )
            for region in ("plateau", "signal")
        }
        if result["plateau"].empty or result["signal"].empty:
            raise ReductionError(
                "No isotope-ratio sweeps remain in the selected intervals.", "ratios",
                "Review the baseline, plateau, and signal selections for the analyses.",
            )
        return self.cache.set(PipelineStage.RATIOS, result)

    def standard_rows(
        self,
        frame: pd.DataFrame,
        standard: str,
        label: str,
        stage: str = "downhole",
    ) -> pd.DataFrame:
        """Return one configured standard's rows after validating availability."""
        self.require_samples((standard,), label, stage)
        if standard not in self.standard_archive.index:
            raise ReductionError(
                f"Unknown reference standard {standard}.", stage,
                "Choose a standard from the reference material library.", ('choose_standard',),
            )
        selected = frame[frame["sample"].eq(standard)].reset_index(drop=True)
        if selected.empty:
            raise ReductionError(f"No {label} rows were produced for {standard}.", stage,
                                 "Review the standard’s plateau and signal selections.", ("choose_standard",))
        return selected

    def run_downhole(self) -> dict[str, object]:
        """Build shared downhole models and apply them to both data intervals."""
        from .fractionation import (
            apply_DH_correction,
            calc_DH_fractionations_rad,
            calc_DH_fractionations_TW,
        )

        cached = self.cache.get(PipelineStage.DOWNHOLE)
        if isinstance(cached, dict):
            return cached
        self._clear_notices(PipelineStage.DOWNHOLE)
        ratios = self.run_ratios()
        signal = ratios["signal"]
        config = self.config
        primary_68 = self.standard_rows(
            signal,
            config.primary_68_standard,
            "Pb/U primary standard",
        )
        model_options = dict(config.downhole.options)
        if config.common_pb_calibration == "tera_wasserburg":
            primary_76 = self.standard_rows(
                signal,
                config.primary_76_standard,
                "Pb/Pb primary standard",
            )
            primary_82 = self.standard_rows(
                signal,
                config.primary_82_standard,
                "Th/Pb primary standard",
            )
            correction = calc_DH_fractionations_TW(
                primary_68,
                primary_76,
                primary_82,
                self.standard_archive,
                model=config.downhole.method,
                model_kwargs=model_options,
                outlier_sigma=config.downhole.outlier_sigma,
                ratio_aggregation=config.ratio_aggregation,
                build_plot=self.build_diagnostic_plots,
            )
        else:
            correction = calc_DH_fractionations_rad(
                primary_68,
                self.standard_archive,
                ratio_names=list(config.ratio_names),
                model=config.downhole.method,
                model_kwargs=model_options,
                outlier_sigma=config.downhole.outlier_sigma,
                ratio_aggregation=config.ratio_aggregation,
                build_plot=self.build_diagnostic_plots,
            )
        result = {
            "correction_results": correction,
            "plateau_corrected": apply_DH_correction(
                ratios["plateau"],
                correction,
            ),
            "signal_corrected": apply_DH_correction(signal, correction),
            "fit_method": callable_reference(config.downhole.method),
            "fit_options": model_options,
        }
        return self.cache.set(PipelineStage.DOWNHOLE, result)

    def run_session(self) -> dict[str, object]:
        """Build shared session models and apply them to both data intervals."""
        from .fractionation import (
            apply_session_correction,
            calc_session_fractionation_rad,
            calc_session_fractionation_TW,
        )

        cached = self.cache.get(PipelineStage.SESSION)
        if isinstance(cached, dict):
            return cached
        self._clear_notices(PipelineStage.SESSION)
        downhole = self.run_downhole()
        plateau = downhole["plateau_corrected"]
        config = self.config
        primary_68 = self.standard_rows(
            plateau,
            config.primary_68_standard,
            "Pb/U primary standard",
            stage="session",
        )
        options = dict(config.session.options)
        degree = int(options.pop("degree", 2))
        if config.common_pb_calibration == "tera_wasserburg":
            primary_76 = self.standard_rows(
                plateau,
                config.primary_76_standard,
                "Pb/Pb primary standard",
                stage="session",
            )
            primary_82 = self.standard_rows(
                plateau,
                config.primary_82_standard,
                "Th/Pb primary standard",
                stage="session",
            )
            correction = calc_session_fractionation_TW(
                primary_68,
                primary_76,
                primary_82,
                self.standard_archive,
                degree=degree,
                model=config.session.method,
                model_kwargs=options,
                ratio_aggregation=config.ratio_aggregation,
                build_plot=self.build_diagnostic_plots,
            )
        else:
            correction = calc_session_fractionation_rad(
                primary_68,
                self.standard_archive,
                ratio_names=list(config.ratio_names),
                degree=degree,
                model=config.session.method,
                model_kwargs=options,
                ratio_aggregation=config.ratio_aggregation,
                build_plot=self.build_diagnostic_plots,
            )
        result = {
            "correction_results": correction,
            "plateau_corrected": apply_session_correction(
                plateau,
                correction,
            ),
            "signal_corrected": apply_session_correction(
                downhole["signal_corrected"],
                correction,
            ),
            "fit_method": callable_reference(config.session.method),
            "fit_options": {"degree": degree, **options},
        }
        return self.cache.set(PipelineStage.SESSION, result)

    def run_spot_means(self) -> pd.DataFrame:
        """Aggregate ratios and join matching calibrated concentrations."""
        from .reduction import calc_ratio_mean_and_uncertainty

        cached = self.cache.get(PipelineStage.SPOT_MEANS)
        if isinstance(cached, pd.DataFrame):
            return cached
        self._clear_notices(PipelineStage.SPOT_MEANS)
        session = self.run_session()
        ratio_means = calc_ratio_mean_and_uncertainty(
            session["plateau_corrected"],
            session["correction_results"],
            ratio_aggregation=self.config.ratio_aggregation,
        )
        if self.config.concentration_standards:
            concentration = self.run_concentration()[
                "final_concentration_df"
            ]
            concentration_columns = [
                column
                for column in concentration.columns
                if column not in {"sample"}
            ]
            result = ratio_means.merge(
                concentration[concentration_columns],
                on="spot_name",
                how="left",
                validate="one_to_one",
            )
            if {"Th", "U"}.issubset(result.columns):
                result["Th/U"] = result["Th"] / result["U"]
        else:
            result = ratio_means
        return self.cache.set(PipelineStage.SPOT_MEANS, result)

    def run_depth_profile(self) -> pd.DataFrame:
        """Calculate a complete calibrated sweep-wise depth product.

        Ratio and concentration calculations both use the inclusive reviewed
        signal interval.  This is intentionally different from spot means,
        which use the filtered plateau.  Common-Pb calculations are appended
        for configured common-Pb-rich minerals before an optional physical
        pulse-to-depth calibration is applied.
        """
        from .concentration import (
            calc_concentration_depth_profile,
            internal_normalization,
        )
        from .isoplot import construct_final_depthpro_df
        from .reduction import calc_depth_profile
        from .uncertainty import append_systematic_date_uncertainties

        cached = self.cache.get(PipelineStage.DEPTH_PROFILE)
        if isinstance(cached, pd.DataFrame):
            return cached
        self._clear_notices(PipelineStage.DEPTH_PROFILE)
        session = self.run_session()
        ratio_profile = calc_depth_profile(
            session["signal_corrected"],
            session["correction_results"],
        )
        if self.config.concentration_standards:
            concentration_result = self.run_concentration()
            signal_normalized = internal_normalization(
                self.experiment,
                list(self.channels),
                self.run_baseline(),
                self.standard_archive,
                material=self.config.material,
                internal_channel=self.resolve_internal_standard_channel(),
                region="signal",
                notices=self.notices,
                notice_stage="depth_profile",
            )
            active_standards = concentration_result["active_standards"]
            analytes = signal_normalized[
                ~signal_normalized["sample"].isin(active_standards)
            ].reset_index(drop=True)
            concentration_profile = calc_concentration_depth_profile(
                analytes,
                concentration_result["calibration"],
                internal_channel=self.resolve_internal_standard_channel(),
                dwell_s=self.counting_dwell_s,
            )
            result = construct_final_depthpro_df(
                concentration_profile,
                ratio_profile,
                material=self.config.material,
                common_pb_options=self.config.common_pb_options,
            )
        else:
            result = ratio_profile
        if self.config.depth_calibration is not None:
            result = apply_depth_calibration(
                result,
                self.config.depth_calibration,
            )
        settings = self.config.uncertainty
        long_term_relative_1s = None
        if settings.long_term_reproducibility_mode == "secondary":
            finalized = self.run_finalize()
            long_term_relative_1s = finalized.parallel_results.attrs.get(
                "long_term_reproducibility_relative_1s_used",
                0.0,
            )
        if (
            settings.propagate_decay_constants
            or settings.propagate_u238_u235
            or settings.long_term_reproducibility_mode != "none"
        ):
            result = append_systematic_date_uncertainties(
                result,
                settings,
                long_term_relative_1s=long_term_relative_1s,
            )
        return self.cache.set(PipelineStage.DEPTH_PROFILE, result)

    def run_finalize(self) -> FinalizedReduction:
        """Apply parallel common-Pb calculations, flags, and user decisions."""
        cached = self.cache.get(PipelineStage.FINALIZE)
        if isinstance(cached, FinalizedReduction):
            return cached
        self._clear_notices(PipelineStage.FINALIZE)
        session = self.run_session()
        accepted_secondary_ages = {
            name: float(self.standard_archive.loc[name, "concordia_age"])
            for name in self.config.secondary_standards
            if name in self.standard_archive.index
            and "concordia_age" in self.standard_archive.columns
            and pd.notna(self.standard_archive.loc[name, "concordia_age"])
        }
        result = finalize_reduction_results(
            self.run_spot_means(),
            sweep_data=session["plateau_corrected"],
            decision=self.config.decision,
            common_pb_options=self.config.common_pb_options,
            flag_settings=self.config.automatic_flags,
            primary_signal_column=self.config.primary_channel,
            quality_signal_data=self.run_baseline(),
            secondary_standard_names=self.config.secondary_standards,
            accepted_secondary_ages=accepted_secondary_ages,
            uncertainty_settings=self.config.uncertainty,
        )
        result.parallel_results.attrs["detector_calibration"] = (
            self.experiment.attrs.get("detector_calibration", {"mode": "qtegra"})
        )
        from .export_table import summarize_reference_materials_by_system

        result.reference_material_summary_by_system = summarize_reference_materials_by_system(
            result.parallel_results, self.standard_archive,
            (self.config.primary_68_standard, self.config.primary_76_standard,
             self.config.primary_82_standard, *self.config.secondary_standards),
            common_pb_calibration=self.config.common_pb_calibration,
        )
        return self.cache.set(PipelineStage.FINALIZE, result)

    def run_to(self, stage: PipelineStage | str):
        """Execute the minimum shared-core work needed to produce ``stage``."""
        resolved = PipelineStage(stage)
        runners = {
            PipelineStage.IMPORT: lambda: self.cache.get(PipelineStage.IMPORT),
            PipelineStage.SELECTION: lambda: self.experiment,
            PipelineStage.BASELINE: self.run_baseline,
            PipelineStage.NORMALIZATION: self.run_normalization,
            PipelineStage.CONCENTRATION: self.run_concentration,
            PipelineStage.RATIOS: self.run_ratios,
            PipelineStage.DOWNHOLE: self.run_downhole,
            PipelineStage.SESSION: self.run_session,
            PipelineStage.SPOT_MEANS: self.run_spot_means,
            PipelineStage.DEPTH_PROFILE: self.run_depth_profile,
            PipelineStage.FINALIZE: self.run_finalize,
        }
        if resolved == PipelineStage.EXPORT:
            return self.run_finalize()
        return runners[resolved]()

    def reference_library_identity(self) -> dict[str, str | None]:
        """Return the reference library's version string and content hash.

        A missing or unreadable library yields ``None`` values rather than an
        error, because provenance must never block an export.
        """
        try:
            return dict(reference_library_info(self.reference_library_path))
        except (OSError, ValueError, TypeError):
            return {"version": None, "sha256": None}

    def manifest(
        self,
        *,
        hash_inputs: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return the complete lightweight provenance record for this workflow."""
        files = []
        for path in self.input_files:
            record: dict[str, object] = {"path": str(path)}
            if path.exists():
                stat = path.stat()
                record["size_bytes"] = int(stat.st_size)
                record["modified_ns"] = int(stat.st_mtime_ns)
                if hash_inputs:
                    record["sha256"] = sha256_file(path)
            files.append(record)
        metadata_record = dict(metadata or {})
        if self.origin and "origin" not in metadata_record:
            metadata_record["origin"] = dict(self.origin)
        library = self.reference_library_identity()
        return {
            "format_version": PIPELINE_FORMAT_VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "upbrpy_version": package_version(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "source_directory": (
                None
                if self.source_directory is None
                else str(self.source_directory)
            ),
            "reference_library_path": (
                None
                if self.reference_library_path is None
                else str(self.reference_library_path)
            ),
            "reference_library_version": library.get("version"),
            "reference_library_sha256": library.get("sha256"),
            "input_files": files,
            "config": self.config.to_dict(),
            "detector_calibration": self.experiment.attrs.get(
                "detector_calibration", {"mode": "qtegra"}
            ),
            "sample_overrides": self.experiment.attrs.get("sample_overrides", {}),
            "completed_stages": [stage.value for stage in self.cache.stages()],
            "spot_count": int(len(self.experiment)),
            "sweep_count": int(len(self.total_data)),
            "channels": list(self.channels),
            "metadata": metadata_record,
            "implementation": {
                "pipeline_module": __name__,
                "executable": sys.executable,
            },
        }


__all__ = [
    "PIPELINE_FORMAT_VERSION",
    "PIPELINE_STAGE_DEFINITIONS",
    "PIPELINE_STAGE_BY_ID",
    "PipelineStage",
    "StageDefinition",
    "FitSettings",
    "PipelineConfig",
    "PipelineCache",
    "ReductionPipeline",
    "pipeline_config_from_dict",
    "config_invalidation_stage",
    "pipeline_descendants",
    "callable_reference",
    "resolve_calibration_method",
    "sha256_file",
    "package_version",
]
