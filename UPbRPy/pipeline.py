"""Shared, GUI-independent orchestration for complete UPbRPy reductions.

The numerical functions in the rest of :mod:`UPbRPy` remain independently
callable.  This module records how those functions are composed into one
reproducible workflow so notebooks and graphical applications do not develop
separate reduction algorithms.
"""

from __future__ import annotations

import hashlib
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

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

PIPELINE_FORMAT_VERSION = 1


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
        (PipelineStage.SESSION,),
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
    depth_calibration: LinearDepthCalibration | None = None

    def __post_init__(self) -> None:
        """Validate calibration mode and required configuration invariants."""
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
            "selection_method": self.selection_method,
            "selection_options": dict(self.selection_options),
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
            "depth_calibration": (
                None
                if self.depth_calibration is None
                else self.depth_calibration.to_dict()
            ),
        }


def pipeline_config_from_dict(values: Mapping[str, object]) -> PipelineConfig:
    """Reconstruct immutable pipeline settings from a saved manifest."""
    data = dict(values)
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
    ) -> None:
        """Initialize a pipeline around imported data and immutable settings.

        ``build_diagnostic_plots`` is a frontend concern, not a scientific
        reduction setting. Notebook callers retain Bokeh diagnostics by
        default; native GUIs can disable them and render the returned numeric
        metadata with their own lightweight plotting widgets.
        """
        if not isinstance(experiment, pd.DataFrame):
            raise TypeError("experiment must be a pandas DataFrame")
        if not isinstance(total_data, pd.DataFrame):
            raise TypeError("total_data must be a pandas DataFrame")
        if not isinstance(standard_archive, pd.DataFrame):
            raise TypeError("standard_archive must be a pandas DataFrame")
        self.experiment = experiment
        self.total_data = total_data
        self.channels = tuple(str(channel) for channel in channels)
        self.standard_archive = standard_archive
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

    def update_experiment(self, experiment: pd.DataFrame) -> None:
        """Replace selection metadata and invalidate every calculated stage."""
        if not isinstance(experiment, pd.DataFrame):
            raise TypeError("experiment must be a pandas DataFrame")
        self.experiment = experiment
        self.cache.invalidate(PipelineStage.SELECTION)
        self.cache.set(PipelineStage.SELECTION, self.experiment)

    def update_config(
        self,
        config: PipelineConfig,
        *,
        invalidate_from: PipelineStage | str = PipelineStage.BASELINE,
    ) -> None:
        """Replace settings and invalidate calculations affected by the change."""
        self.config = config
        self.cache.invalidate(invalidate_from)

    def require_samples(self, names: tuple[str, ...], label: str) -> None:
        """Raise a focused error when configured reference materials are absent."""
        requested = tuple(name for name in names if name)
        if not requested:
            raise ValueError(f"No {label} is configured")
        if "sample" not in self.total_data.columns:
            raise KeyError("total_data must contain a sample column")
        available = set(
            self.total_data["sample"].dropna().astype(str).unique().tolist()
        )
        missing = [name for name in requested if name not in available]
        if missing:
            raise ValueError(
                f"Missing {label} in the loaded session: {', '.join(missing)}"
            )

    def run_baseline(self) -> pd.DataFrame:
        """Subtract each spot/channel baseline using the reviewed selections."""
        from .io import subtract_baseline

        cached = self.cache.get(PipelineStage.BASELINE)
        if isinstance(cached, pd.DataFrame):
            return cached
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
        result = internal_normalization(
            self.experiment,
            list(self.channels),
            self.run_baseline(),
            self.standard_archive,
            material=self.config.material,
            internal_channel=self.config.primary_channel,
            region="plateau",
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
            self.require_samples(standards, "concentration standards")
        calibrants = normalized[
            normalized["sample"].isin(active_standards)
        ].reset_index(drop=True)
        calibration, plot = calc_concentration_calibration_factor(
            calibrants,
            list(self.channels),
            self.standard_archive,
            internal_channel=self.config.primary_channel,
            build_plot=self.build_diagnostic_plots,
        )
        unknowns = normalized[
            ~normalized["sample"].isin(active_standards)
        ].reset_index(drop=True)
        concentrations = calc_concentration(unknowns, calibration)
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

    def run_ratios(self) -> dict[str, pd.DataFrame]:
        """Calculate filtered plateau and complete signal isotope ratios."""
        from .ratios import calc_raw_ratios

        cached = self.cache.get(PipelineStage.RATIOS)
        if isinstance(cached, dict):
            return cached
        baseline = self.run_baseline()
        result = {
            region: calc_raw_ratios(
                self.experiment,
                self.total_data,
                baseline,
                region=region,
            )
            for region in ("plateau", "signal")
        }
        return self.cache.set(PipelineStage.RATIOS, result)

    def standard_rows(
        self,
        frame: pd.DataFrame,
        standard: str,
        label: str,
    ) -> pd.DataFrame:
        """Return one configured standard's rows after validating availability."""
        self.require_samples((standard,), label)
        selected = frame[frame["sample"].eq(standard)].reset_index(drop=True)
        if selected.empty:
            raise ValueError(f"No {label} rows were produced for {standard!r}")
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
        downhole = self.run_downhole()
        plateau = downhole["plateau_corrected"]
        config = self.config
        primary_68 = self.standard_rows(
            plateau,
            config.primary_68_standard,
            "Pb/U primary standard",
        )
        options = dict(config.session.options)
        degree = int(options.pop("degree", 2))
        if config.common_pb_calibration == "tera_wasserburg":
            primary_76 = self.standard_rows(
                plateau,
                config.primary_76_standard,
                "Pb/Pb primary standard",
            )
            primary_82 = self.standard_rows(
                plateau,
                config.primary_82_standard,
                "Th/Pb primary standard",
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

        cached = self.cache.get(PipelineStage.DEPTH_PROFILE)
        if isinstance(cached, pd.DataFrame):
            return cached
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
                internal_channel=self.config.primary_channel,
                region="signal",
            )
            active_standards = concentration_result["active_standards"]
            analytes = signal_normalized[
                ~signal_normalized["sample"].isin(active_standards)
            ].reset_index(drop=True)
            concentration_profile = calc_concentration_depth_profile(
                analytes,
                concentration_result["calibration"],
                internal_channel=self.config.primary_channel,
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
        return self.cache.set(PipelineStage.DEPTH_PROFILE, result)

    def run_finalize(self) -> FinalizedReduction:
        """Apply parallel common-Pb calculations, flags, and user decisions."""
        cached = self.cache.get(PipelineStage.FINALIZE)
        if isinstance(cached, FinalizedReduction):
            return cached
        session = self.run_session()
        result = finalize_reduction_results(
            self.run_spot_means(),
            sweep_data=session["plateau_corrected"],
            decision=self.config.decision,
            common_pb_options=self.config.common_pb_options,
            flag_settings=self.config.automatic_flags,
            primary_signal_column=self.config.primary_channel,
            quality_signal_data=self.total_data,
            secondary_standard_names=self.config.secondary_standards,
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
            "input_files": files,
            "config": self.config.to_dict(),
            "completed_stages": [stage.value for stage in self.cache.stages()],
            "spot_count": int(len(self.experiment)),
            "sweep_count": int(len(self.total_data)),
            "channels": list(self.channels),
            "metadata": dict(metadata or {}),
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
    "pipeline_descendants",
    "callable_reference",
    "resolve_calibration_method",
    "sha256_file",
    "package_version",
]
