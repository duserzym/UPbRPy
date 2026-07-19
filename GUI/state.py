from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class ReductionSession:
    directory: str = ""
    experiment: pd.DataFrame = field(default_factory=pd.DataFrame)
    total_data: pd.DataFrame = field(default_factory=pd.DataFrame)
    channels: list[str] = field(default_factory=list)
    primary_channel: str = "238U"
    workflow_key: str = "zircon"
    workflow_title: str = "Zircon"
    selected_spot: Optional[str] = None
    visible_channels: list[str] = field(default_factory=list)
    import_settings: dict[str, object] = field(default_factory=dict)
    plot_mode: str = "raw"
    downhole_fit_method: str = "interpolated"
    downhole_fit_options: dict[str, float | int | str] = field(
        default_factory=lambda: {
            "running_mean_window": 5,
            "polynomial_degree": 2,
            "exponential_initial_decay": 0.1,
        }
    )
    session_fit_method: str = "polynomial"
    session_fit_options: dict[str, float | int | str] = field(
        default_factory=lambda: {
            "running_mean_window": 5,
            "polynomial_degree": 2,
            "exponential_initial_decay": 0.001,
        }
    )
    reduction_cache: dict[str, object] = field(default_factory=dict)

    @property
    def loaded(self) -> bool:
        return not self.experiment.empty and not self.total_data.empty

    @property
    def spot_names(self) -> list[str]:
        if self.experiment.empty or "spot_names" not in self.experiment:
            return []
        return [str(value) for value in self.experiment["spot_names"].tolist()]
