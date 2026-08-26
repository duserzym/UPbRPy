from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from UPbRPy.pipeline import ReductionPipeline


@dataclass
class ReductionSession:
    directory: str = ""
    experiment: pd.DataFrame = field(default_factory=pd.DataFrame)
    total_data: pd.DataFrame = field(default_factory=pd.DataFrame)
    channels: list[str] = field(default_factory=list)
    primary_channel: str = "238U"
    workflow_key: str = "zircon"
    workflow_title: str = "Zircon"
    concentration_standards: tuple[str, ...] = ()
    primary_68_standard: str = ""
    primary_76_standard: str = ""
    primary_82_standard: str = ""
    secondary_standards: tuple[str, ...] = ()
    selected_spot: Optional[str] = None
    visible_channels: list[str] = field(default_factory=list)
    import_settings: dict[str, object] = field(default_factory=dict)
    plot_mode: str = "raw"
    ratio_aggregation: str = "arithmetic"
    downhole_fit_method: str = "mean"
    downhole_fit_options: dict[str, float | int | str] = field(
        default_factory=lambda: {
            "running_mean_window": 5,
            "polynomial_degree": 2,
            "exponential_initial_decay": 0.1,
        }
    )
    downhole_outlier_sigma: float | None = 5.0
    session_fit_method: str = "polynomial"
    session_fit_options: dict[str, float | int | str] = field(
        default_factory=lambda: {
            "running_mean_window": 5,
            "polynomial_degree": 2,
            "exponential_initial_decay": 0.001,
        }
    )
    common_pb_method: str = "none"
    common_pb_uncertainty_preset_name: str = "none"
    include_calibration_uncertainty: bool = True
    propagate_decay_constants: bool = False
    propagate_u238_u235: bool = False
    long_term_reproducibility_mode: str = "none"
    long_term_reproducibility_relative_1s: float = 0.0
    include_export_concentrations: bool = True
    include_export_depth_concentrations: bool = True
    age_transition_ma: float = 1000.0
    reduction_cache: dict[str, object] = field(default_factory=dict)
    pipeline: Optional["ReductionPipeline"] = None

    @property
    def loaded(self) -> bool:
        return not self.experiment.empty and not self.total_data.empty

    @property
    def spot_names(self) -> list[str]:
        if self.experiment.empty or "spot_names" not in self.experiment:
            return []
        return [str(value) for value in self.experiment["spot_names"].tolist()]
