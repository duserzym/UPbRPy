"""Experiment I/O and reusable baseline/signal-window selection strategies.

Selection indices use the same boundary convention as the original UPbRPy
workflow: starts are inclusive and ends identify the time boundary used by
downstream ``start <= Time < end`` slicing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

from pathlib import Path
import warnings

import numpy as np
import pandas as pd

from .qtegra import QtegraSession, counting_dwell, open_qtegra


class SelectionError(ValueError):
    """Raised when a signal trace cannot produce valid selection windows."""


@dataclass(frozen=True)
class SelectionResult:
    """Indices and diagnostics produced by a selection strategy.

    Baseline and plateau ends are exclusive slicing boundaries. ``signal_end``
    is instead the inclusive index of the last sample-derived sweep so the two
    highlighted signal-edge measurements are unambiguous.
    """

    plateau_start: int
    plateau_end: int
    baseline_start: int
    baseline_end: int
    detection_index: int
    method: str
    signal_end: int
    washout_start: int | None = None
    signal_start_index: int | None = None
    diagnostics: Mapping[str, float | int | str] = field(default_factory=dict)

    @property
    def signal_start(self) -> int:
        """Return the inclusive first sweep attributed to sample signal."""
        if self.signal_start_index is None:
            return self.detection_index
        return self.signal_start_index

    @property
    def post_baseline_start(self) -> int | None:
        """Compatibility alias for the first non-retained washout sweep."""
        return self.washout_start

    def as_tuple(self) -> tuple[int, int, int, int]:
        """Return plateau and baseline bounds in historical index order."""
        return (
            self.plateau_start,
            self.plateau_end,
            self.baseline_start,
            self.baseline_end,
        )


Selector = Callable[..., tuple[int, Mapping[str, float | int | str]]]
_SELECTORS: dict[str, Selector] = {}
_ALIASES: dict[str, str] = {}


def register_selection_method(
    name: str,
    selector: Selector,
    *,
    aliases: tuple[str, ...] = (),
) -> None:
    """Register a signal-onset detector for use by ``select_signal_regions``."""
    canonical = normalize_method_name(name)
    if not canonical:
        raise ValueError("Selection method name cannot be empty.")
    _SELECTORS[canonical] = selector
    _ALIASES[canonical] = canonical
    for alias in aliases:
        _ALIASES[normalize_method_name(alias)] = canonical


def available_selection_methods(*, include_laser: bool = False) -> tuple[str, ...]:
    """Return detector names, optionally including timed laser guidance.

    Session controls should request include_laser only when laser times are
    available. Ordinary inputs retain their existing detector choices.
    """
    return tuple(name for name in _SELECTORS if include_laser or name != 'laser_guided')


def selection_method_label(method: str) -> str:
    """Return a concise label suitable for notebook output and GUI controls."""
    canonical = resolve_method(method)
    return {
        "threshold": "Signal threshold",
        "rolling_sigma": "Running mean + σ threshold",
        "excel_expanding_sigma": "Excel expanding mean + σ threshold",
        "laser_guided": "Laser-guided",
    }.get(canonical, canonical.replace("_", " ").title())


def normalize_method_name(name: str) -> str:
    """Normalize a user-facing selection-method label for stable alias lookup."""
    return str(name).strip().lower().replace("-", "_").replace(" ", "_")


def resolve_method(method: str) -> str:
    """Resolve a selection-method name or alias to its canonical registry key."""
    normalized = normalize_method_name(method)
    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        choices = ", ".join(available_selection_methods(include_laser=True))
        raise ValueError(f"Unknown selection method {method!r}. Choose from: {choices}.") from exc


def coerce_trace(data) -> np.ndarray:
    """Return a finite one-dimensional signal trace for window selection.

    Internal non-finite values are linearly interpolated only for the purpose
    of detecting boundaries; the original analytical data remain unchanged.
    """
    values = np.asarray(data, dtype=float)
    if values.ndim != 1:
        raise SelectionError("Selection data must be one-dimensional.")
    if values.size < 3:
        raise SelectionError("Selection data must contain at least three sweeps.")
    finite = np.isfinite(values)
    if not np.any(finite):
        raise SelectionError("Selection data contain no finite values.")
    if not np.all(finite):
        indices = np.arange(values.size)
        values = values.copy()
        values[~finite] = np.interp(indices[~finite], indices[finite], values[finite])
    return values


def validate_common_options(
    size: int,
    baseline_sweep_n: int,
    peak_sweep_n: int,
    baseline_offset: int,
    plateau_offset: int,
) -> tuple[int, int, int, int]:
    """Validate and normalize integer options shared by selection-window methods."""
    baseline_sweep_n = int(baseline_sweep_n)
    peak_sweep_n = int(peak_sweep_n)
    baseline_offset = int(baseline_offset)
    plateau_offset = int(plateau_offset)
    if baseline_sweep_n < 1 or peak_sweep_n < 1:
        raise ValueError("baseline_sweep_n and peak_sweep_n must be positive.")
    if baseline_offset < 0 or plateau_offset < 0:
        raise ValueError("baseline_offset and plateau_offset cannot be negative.")
    if baseline_sweep_n >= size:
        raise SelectionError(
            f"baseline_sweep_n={baseline_sweep_n} leaves no signal data in a {size}-sweep trace."
        )
    return baseline_sweep_n, peak_sweep_n, baseline_offset, plateau_offset


def selection_windows(
    detection_index: int,
    *,
    size: int,
    baseline_sweep_n: int,
    peak_sweep_n: int,
    baseline_offset: int,
    plateau_offset: int,
    signal_end: int | None = None,
) -> tuple[int, int, int, int]:
    """Construct baseline and plateau bounds around a detected signal onset.

    Baseline and plateau ends are exclusive boundaries. ``signal_end`` is an
    inclusive measured sweep and therefore caps the plateau's exclusive end at
    or before that sweep.
    """
    baseline_end = detection_index - baseline_offset
    baseline_start = baseline_end - baseline_sweep_n
    plateau_start = detection_index + plateau_offset

    # Retain the original fallback for signals detected too early to provide a
    # complete baseline window.
    if baseline_start < 0:
        baseline_start = 0
        baseline_end = baseline_sweep_n
        plateau_start = baseline_sweep_n + plateau_offset

    last_boundary = size - 1
    baseline_start = min(max(0, baseline_start), last_boundary)
    baseline_end = min(max(baseline_start + 1, baseline_end), last_boundary)
    plateau_start = min(max(baseline_end, plateau_start), last_boundary)
    plateau_stop_boundary = last_boundary
    if signal_end is not None:
        # Plateau end is an exclusive time boundary, whereas signal end is an
        # inclusive measured sweep. Keeping the plateau boundary at or before
        # the signal-end sweep guarantees the visible plateau range is wholly
        # contained by the signal range.
        plateau_stop_boundary = min(plateau_stop_boundary, int(signal_end))
    plateau_end = min(plateau_start + peak_sweep_n, plateau_stop_boundary)
    if plateau_end <= plateau_start:
        raise SelectionError(
            "The detected signal returns to baseline before a usable plateau window begins."
        )
    return plateau_start, plateau_end, baseline_start, baseline_end


def robust_center_scale(values: np.ndarray) -> tuple[float, float]:
    """Return a median center and a nonzero robust spread estimate."""
    center = float(np.median(values))
    median_absolute_deviation = float(np.median(np.abs(values - center)))
    scale = 1.4826 * median_absolute_deviation
    if not np.isfinite(scale) or scale == 0:
        scale = float(np.std(values, ddof=1)) if values.size > 1 else 0.0
    if not np.isfinite(scale) or scale == 0:
        scale = max(abs(center) * 0.01, float(np.finfo(float).eps))
    return center, scale


def find_signal_start(
    values: np.ndarray,
    *,
    detected_onset: int,
    baseline_start: int,
    baseline_end: int,
    sigma_threshold: float = 3.0,
    relative_increase: float = 0.05,
) -> tuple[int, Mapping[str, float | int | str]]:
    """Detect signal onset as a fraction of a high-percentile plateau mean."""
    """Backtrack a confirmed onset to the first contiguous elevated sweep.

    The primary selector still anchors the baseline and plateau windows. This
    refinement only expands the depth-profile signal boundary across a subtle
    leading ramp that remains continuously above the robust baseline band.
    """
    sigma_threshold = float(sigma_threshold)
    relative_increase = float(relative_increase)
    if sigma_threshold <= 0:
        raise ValueError("signal_start_sigma_threshold must be positive.")
    if relative_increase < 0:
        raise ValueError("signal_start_relative_increase cannot be negative.")

    baseline = values[int(baseline_start) : int(baseline_end)]
    if baseline.size < 2:
        raise SelectionError("At least two baseline sweeps are needed to refine signal start.")
    baseline_center, baseline_scale = robust_center_scale(baseline)
    start_threshold = baseline_center + max(
        sigma_threshold * baseline_scale,
        abs(baseline_center) * relative_increase,
    )

    signal_start = int(detected_onset)
    earliest_candidate = int(baseline_end)
    while (
        signal_start > earliest_candidate
        and values[signal_start - 1] > start_threshold
    ):
        signal_start -= 1

    return signal_start, {
        "raw_detection_index": int(detected_onset),
        "signal_start_state": (
            "leading_ramp_recovered"
            if signal_start < int(detected_onset)
            else "detected_onset_retained"
        ),
        "signal_start_threshold": start_threshold,
        "signal_start_baseline_center": baseline_center,
        "signal_start_baseline_scale": baseline_scale,
        "signal_start_sigma_threshold": sigma_threshold,
        "signal_start_relative_increase": relative_increase,
    }


def find_signal_end(
    values: np.ndarray,
    *,
    signal_start: int,
    baseline_start: int,
    baseline_end: int,
    plateau_start: int,
    plateau_end: int,
    sigma_threshold: float = 3.0,
    consecutive: int = 2,
    fraction_threshold: float = 0.8,
    baseline_sigma_threshold: float = 3.0,
    baseline_consecutive: int = 3,
    minimum_signal_fraction: float = 0.05,
) -> tuple[int, int | None, Mapping[str, float | int | str]]:
    """Find the final statistically elevated sweep before washout.

    The preceding plateau sweeps supply a rolling robust linear signal trend.
    The first sustained run below that trend by
    ``sigma_threshold`` robust residual standard deviations identifies the
    falling edge. The pre-ablation baseline supplies an upper washout threshold
    based on both ``baseline_sigma_threshold`` and ``minimum_signal_fraction``
    of the baseline-to-plateau elevation. Within the falling tail, the last
    sweep above the larger threshold is the inclusive signal end. A later run
    of ``baseline_consecutive`` sweeps inside the two-sided baseline band
    confirms stable washout recovery, but noisy baseline-like sweeps before
    that run are not retained as sample signal.
    """
    sigma_threshold = float(sigma_threshold)
    consecutive = int(consecutive)
    fraction_threshold = float(fraction_threshold)
    baseline_sigma_threshold = float(baseline_sigma_threshold)
    baseline_consecutive = int(baseline_consecutive)
    minimum_signal_fraction = float(minimum_signal_fraction)
    if sigma_threshold <= 0:
        raise ValueError("signal_end_sigma_threshold must be positive.")
    if consecutive < 1:
        raise ValueError("signal_end_consecutive must be positive.")
    if not 0 < fraction_threshold < 1:
        raise ValueError("signal_end_fraction_threshold must be between 0 and 1.")
    if baseline_sigma_threshold <= 0:
        raise ValueError("signal_end_baseline_sigma_threshold must be positive.")
    if baseline_consecutive < 1:
        raise ValueError("signal_end_baseline_consecutive must be positive.")
    if not 0 <= minimum_signal_fraction < 1:
        raise ValueError(
            "signal_end_minimum_signal_fraction must be between 0 and 1."
        )

    baseline = values[int(baseline_start) : int(baseline_end)]
    if baseline.size < 2:
        raise SelectionError("At least two baseline sweeps are needed to detect signal end.")
    baseline_center, baseline_std = robust_center_scale(baseline)
    washout_half_width = baseline_sigma_threshold * baseline_std
    washout_lower = baseline_center - washout_half_width
    washout_upper = baseline_center + washout_half_width
    plateau_start = int(plateau_start)
    plateau_end = int(plateau_end)
    plateau_center = float(np.median(values[plateau_start:plateau_end]))
    signal_elevation = max(0.0, plateau_center - baseline_center)
    retention_threshold = max(
        washout_upper,
        baseline_center + minimum_signal_fraction * signal_elevation,
    )
    rolling_window = min(8, plateau_end - plateau_start)
    if rolling_window < 3:
        raise SelectionError("At least three plateau sweeps are needed to detect signal end.")
    search_start = max(plateau_start + rolling_window, int(signal_start) + 3)
    search_stop = values.size - consecutive + 1
    signal_slope = 0.0
    signal_intercept = float(np.median(values[plateau_start:plateau_end]))
    residual_std = 0.0
    detection_scale = max(baseline_std, abs(signal_intercept) * 0.01)
    falling_edge_start: int | None = None
    falling_edge_threshold = float("nan")
    for index in range(search_start, search_stop):
        reference_indices = np.arange(index - rolling_window, index, dtype=float)
        reference_values = values[index - rolling_window : index]
        pair_slopes = []
        for left in range(reference_values.size - 1):
            for right in range(left + 1, reference_values.size):
                pair_slopes.append(
                    (reference_values[right] - reference_values[left])
                    / (reference_indices[right] - reference_indices[left])
                )
        signal_slope = float(np.median(pair_slopes))
        signal_intercept = float(
            np.median(reference_values - signal_slope * reference_indices)
        )
        reference_prediction = signal_intercept + signal_slope * reference_indices
        residuals = reference_values - reference_prediction
        residual_center = float(np.median(residuals))
        residual_std = 1.4826 * float(
            np.median(np.abs(residuals - residual_center))
        )
        if residual_std == 0:
            residual_std = float(np.std(residuals, ddof=1))
        signal_level = float(np.median(np.abs(reference_values)))
        detection_scale = max(residual_std, baseline_std, signal_level * 0.01)
        candidate_indices = np.arange(index, index + consecutive, dtype=float)
        expected = signal_intercept + signal_slope * candidate_indices
        lower_threshold = expected - sigma_threshold * detection_scale
        candidate_values = values[index : index + consecutive]
        material_drop = np.min(candidate_values) < np.median(reference_values) * fraction_threshold
        if np.all(candidate_values < lower_threshold) and material_drop:
            falling_edge_start = index
            falling_edge_threshold = float(lower_threshold[0])
            break

    baseline_search_start = (
        falling_edge_start if falling_edge_start is not None else search_start
    )
    baseline_search_stop = values.size - baseline_consecutive + 1
    stable_baseline_start: int | None = None
    for index in range(baseline_search_start, baseline_search_stop):
        candidate_values = values[index : index + baseline_consecutive]
        if np.all(
            (candidate_values >= washout_lower)
            & (candidate_values <= washout_upper)
        ):
            stable_baseline_start = index
            break

    if falling_edge_start is not None:
        tail_stop = (
            stable_baseline_start
            if stable_baseline_start is not None
            else values.size
        )
        tail = values[falling_edge_start:tail_stop]
        elevated_tail = np.flatnonzero(tail > retention_threshold)
        if elevated_tail.size:
            signal_end = falling_edge_start + int(elevated_tail[-1])
        else:
            signal_end = falling_edge_start - 1
        # First sweep not retained as statistically elevated sample signal.
        # Stable baseline recovery may occur a few noisy sweeps later.
        washout_start = signal_end + 1
    elif stable_baseline_start is not None:
        # A gradual washout can return to the baseline band without satisfying
        # the sharper rolling-trend falling-edge test. Retain the last sweep
        # that is still elevated above the washout threshold, while never
        # moving the signal end inside the selected plateau.
        tail = values[search_start:stable_baseline_start]
        elevated_tail = np.flatnonzero(tail > retention_threshold)
        if elevated_tail.size:
            signal_end = search_start + int(elevated_tail[-1])
        else:
            signal_end = stable_baseline_start - 1
        signal_end = max(signal_end, plateau_end - 1)
        washout_start = signal_end + 1
    else:
        washout_start = None

    if stable_baseline_start is not None:
        signal_end_state = "washout_baseline_detected"
    elif falling_edge_start is not None:
        signal_end_state = "falling_edge_detected_without_baseline_recovery"
    else:
        signal_end = values.size - 1
        signal_end_state = "incomplete"

    return signal_end, washout_start, {
        "signal_end_state": signal_end_state,
        "signal_end_threshold": falling_edge_threshold,
        "signal_end_baseline_center": baseline_center,
        "signal_end_baseline_std": baseline_std,
        "signal_end_washout_baseline_lower": washout_lower,
        "signal_end_washout_baseline_upper": washout_upper,
        "signal_end_washout_baseline_sigma_threshold": baseline_sigma_threshold,
        "signal_end_washout_baseline_consecutive": baseline_consecutive,
        "signal_end_washout_retention_threshold": retention_threshold,
        "signal_end_plateau_center": plateau_center,
        "signal_end_minimum_signal_fraction": minimum_signal_fraction,
        "signal_end_stable_baseline_start": (
            -1 if stable_baseline_start is None else stable_baseline_start
        ),
        "signal_end_falling_edge_start": (
            -1 if falling_edge_start is None else falling_edge_start
        ),
        "signal_end_signal_slope": signal_slope,
        "signal_end_signal_intercept": signal_intercept,
        "signal_end_residual_std": residual_std,
        "signal_end_detection_scale": detection_scale,
        "signal_end_rolling_window": rolling_window,
        "signal_end_sigma_threshold": sigma_threshold,
        "signal_end_consecutive": consecutive,
        "signal_end_fraction_threshold": fraction_threshold,
    }


def threshold_onset(
    values: np.ndarray,
    *,
    threshold: float = 0.1,
    filter_percentile: float = 70,
    baseline_sweep_n: int = 0,
    **_: object,
) -> tuple[int, Mapping[str, float | int | str]]:
    """Detect onset above both plateau-fraction and baseline-noise thresholds.

    The historical selector used a fraction of a rough plateau mean. That
    works when the pre-ablation background is near zero, but can place the
    threshold below a high Si background in glass analyses. Requiring the
    larger of that value and the robust baseline center plus three scales
    prevents baseline sweeps from being mistaken for signal.
    """
    percentile = float(filter_percentile)
    if not 0 <= percentile <= 100:
        raise ValueError("filter_percentile must be between 0 and 100.")
    percentile_value = float(np.percentile(values, percentile))
    rough_plateau = values[values > percentile_value]
    if rough_plateau.size == 0:
        rough_plateau = values[np.argmax(values) : np.argmax(values) + 1]
    search_start = max(0, int(baseline_sweep_n))
    baseline_reference = values[:search_start]
    if baseline_reference.size >= 2:
        baseline_center, baseline_scale = robust_center_scale(baseline_reference)
        baseline_threshold = baseline_center + 3.0 * baseline_scale
    else:
        baseline_center = float(values[0])
        baseline_scale = 0.0
        baseline_threshold = baseline_center
    plateau_fraction_threshold = float(
        np.mean(rough_plateau) * float(threshold)
    )
    threshold_value = max(plateau_fraction_threshold, baseline_threshold)
    hits = np.flatnonzero(values[search_start:] > threshold_value) + search_start
    if hits.size == 0:
        raise SelectionError(
            f"No signal exceeded the calculated threshold ({threshold_value:g})."
        )
    return int(hits[0]), {
        "threshold_value": threshold_value,
        "plateau_fraction_threshold": plateau_fraction_threshold,
        "baseline_threshold": baseline_threshold,
        "baseline_center": baseline_center,
        "baseline_scale": baseline_scale,
        "filter_percentile": percentile,
        "rough_plateau_mean": float(np.mean(rough_plateau)),
    }


def rolling_sigma_onset(
    values: np.ndarray,
    *,
    baseline_sweep_n: int,
    rolling_window: int = 5,
    sigma_threshold: float = 5.0,
    consecutive: int = 1,
    **_: object,
) -> tuple[int, Mapping[str, float | int | str]]:
    """Detect sustained signal onset relative to immediately preceding sweeps.

    At each candidate sweep the threshold is the mean of the preceding
    ``rolling_window`` measurements plus ``sigma_threshold`` times their sample
    standard deviation. Requiring consecutive exceedances suppresses isolated
    baseline spikes.
    """
    rolling_window = int(rolling_window)
    consecutive = int(consecutive)
    sigma_threshold = float(sigma_threshold)
    if rolling_window < 2:
        raise ValueError("rolling_window must be at least 2.")
    if consecutive < 1:
        raise ValueError("consecutive must be positive.")
    if sigma_threshold <= 0:
        raise ValueError("sigma_threshold must be positive.")

    search_start = max(int(baseline_sweep_n), rolling_window)
    search_stop = values.size - consecutive + 1
    for index in range(search_start, search_stop):
        reference = values[index - rolling_window : index]
        running_mean = float(np.mean(reference))
        running_std = float(np.std(reference, ddof=1))
        threshold_value = running_mean + sigma_threshold * running_std
        if np.all(values[index : index + consecutive] > threshold_value):
            return index, {
                "threshold_value": threshold_value,
                "running_mean": running_mean,
                "running_std": running_std,
                "rolling_window": rolling_window,
                "sigma_threshold": sigma_threshold,
                "consecutive": consecutive,
            }
    raise SelectionError(
        "No signal onset exceeded the running baseline mean by "
        f"{sigma_threshold:g} standard deviations."
    )


def excel_expanding_sigma_onset(
    values: np.ndarray,
    *,
    initial_window: int = 6,
    sigma_threshold: float = 8.0,
    consecutive: int = 1,
    **_: object,
) -> tuple[int, Mapping[str, float | int | str]]:
    """Reproduce the onset detector in the reference workbook's VBA.

    The VBA starts with six sweeps, expands the reference range by one sweep on
    each iteration, and tests the following value against mean + 8 sample
    standard deviations. ``consecutive`` is an optional Python-side safeguard;
    its default of one exactly matches the macro.
    """
    initial_window = int(initial_window)
    consecutive = int(consecutive)
    sigma_threshold = float(sigma_threshold)
    if initial_window < 2:
        raise ValueError("initial_window must be at least 2.")
    if consecutive < 1:
        raise ValueError("consecutive must be positive.")
    if sigma_threshold <= 0:
        raise ValueError("sigma_threshold must be positive.")

    search_stop = values.size - consecutive + 1
    for index in range(initial_window, search_stop):
        reference = values[:index]
        expanding_mean = float(np.mean(reference))
        expanding_std = float(np.std(reference, ddof=1))
        threshold_value = expanding_mean + sigma_threshold * expanding_std
        if np.all(values[index : index + consecutive] > threshold_value):
            return index, {
                "threshold_value": threshold_value,
                "expanding_mean": expanding_mean,
                "expanding_std": expanding_std,
                "reference_sweeps": index,
                "initial_window": initial_window,
                "sigma_threshold": sigma_threshold,
                "consecutive": consecutive,
                "source": "Zircon_28Mar25_Zhang1-i193-LA-UPbR22.xlsm Module1.ImportRawData",
            }
    raise SelectionError(
        "No signal onset exceeded the expanding baseline mean by "
        f"{sigma_threshold:g} standard deviations."
    )


def laser_guided_onset(values, *, time=None, laser_predicted_on_time=None, **options):
    """Locate the measured sweep nearest a predicted laser arrival time."""
    if laser_predicted_on_time is None or not np.isfinite(laser_predicted_on_time):
        raise SelectionError("no laser time for this spot")
    if time is None:
        raise SelectionError("time values are required for laser-guided selection")
    if not time[0] <= laser_predicted_on_time <= time[-1]:
        raise SelectionError("predicted laser-on is outside this spot")
    return int(np.argmin(np.abs(time - laser_predicted_on_time))), {
        "laser_predicted_on_time": float(laser_predicted_on_time),
        "laser_delay_assumed_zero": int(options.get("laser_delay_assumed_zero", 0)),
    }


register_selection_method("threshold", threshold_onset, aliases=("percentile_threshold",))
register_selection_method(
    "rolling_sigma",
    rolling_sigma_onset,
    aliases=("running_sigma", "running_mean_sigma", "running_5sigma"),
)
register_selection_method(
    "excel_expanding_sigma",
    excel_expanding_sigma_onset,
    aliases=("excel_vba", "expanding_sigma"),
)

register_selection_method("laser_guided", laser_guided_onset)

SELECTION_METHODS = available_selection_methods()


def select_signal_regions(
    data,
    *,
    time=None,
    method: str = "threshold",
    baseline_sweep_n: int = 10,
    peak_sweep_n: int = 20,
    threshold: float = 0.1,
    filter_percentile: float = 70,
    baseline_offset: int = 2,
    plateau_offset: int = 2,
    signal_end_sigma_threshold: float = 3.0,
    signal_end_consecutive: int = 2,
    signal_end_fraction_threshold: float = 0.8,
    signal_end_baseline_sigma_threshold: float = 3.0,
    signal_end_baseline_consecutive: int = 3,
    signal_end_minimum_signal_fraction: float = 0.05,
    signal_duration_limit_seconds: float | None = None,
    signal_start_sigma_threshold: float = 3.0,
    signal_start_relative_increase: float = 0.05,
    signal_minimum_relative_increase: float = 0.1,
    plateau_width_mode: str = "sweeps",
    plateau_duration_seconds: float | None = None,
    **method_options,
) -> SelectionResult:
    """Select baseline and plateau windows using a registered onset detector.

    ``threshold`` reproduces the original percentile-based algorithm.
    ``rolling_sigma`` compares each candidate onset against the mean and sample
    standard deviation of the preceding ``rolling_window`` sweeps.
    ``excel_expanding_sigma`` reproduces the reference workbook's expanding
    mean plus eight-standard-deviation onset detector. ``laser_guided`` uses
    ``laser_predicted_on_time`` and optional ``laser_predicted_off_time`` on
    the supplied ``time`` axis. The laser_guidance.laser_selection_options
    helper prepares these per-spot options. With no off-time, the ordinary
    washout detector supplies the end. Guided edges stay at predicted sweeps,
    without the extra grab sweep used by ``autopick_signal_regions``. When
    ``signal_duration_limit_seconds`` is supplied, the freely selected signal
    end is capped at the measured sweep nearest that many seconds after the
    detected signal start. The default ``None`` leaves the end unconstrained.
    ``plateau_width_mode='signal'`` expands the plateau to the last complete
    pre-washout sweep, which is useful for long depth-profile ablations.
    ``'duration'`` instead uses ``plateau_duration_seconds`` while still
    capping the plateau before washout. The default ``'sweeps'`` preserves the
    traditional fixed ``peak_sweep_n`` behavior.

    ``signal_duration_limit_seconds`` acts as a nominal on-peak (laser-on)
    duration: because the plateau end is an exclusive boundary at or before
    the signal end, every plateau sweep is then measured before detected
    signal start plus that duration, whatever the width mode, and a shorter
    detected signal is kept. With ``plateau_width_mode='signal'`` and no
    limit, the plateau runs to the last sweep above the washout retention
    threshold, which on short ablations includes the first falling sweeps.
    The diagnostics report ``on_peak_detected_seconds``: detected signal start
    to the start of the falling edge (or to the signal end when no falling
    edge is found).
    """
    values = coerce_trace(data)
    times: np.ndarray | None = None
    if time is not None:
        times = np.asarray(time, dtype=float)
        if times.ndim != 1 or times.size != values.size:
            raise ValueError(
                "time must be one-dimensional and match the signal data."
            )
        if not np.all(np.isfinite(times)):
            raise ValueError("time must contain only finite values.")
        if np.any(np.diff(times) <= 0):
            raise ValueError("time must be strictly increasing within a spot.")
    common = validate_common_options(
        values.size,
        baseline_sweep_n,
        peak_sweep_n,
        baseline_offset,
        plateau_offset,
    )
    baseline_sweep_n, peak_sweep_n, baseline_offset, plateau_offset = common
    canonical = resolve_method(method)
    detector_options = dict(method_options)
    laser_off_index = None
    if canonical == "laser_guided":
        detector_options["time"] = times
        off = method_options.get("laser_predicted_off_time")
        if off is not None and np.isfinite(off) and times is not None:
            laser_off_index = int(np.argmin(np.abs(times - off)))
    detection_index, diagnostics = _SELECTORS[canonical](
        values,
        baseline_sweep_n=baseline_sweep_n,
        threshold=threshold,
        filter_percentile=filter_percentile,
        **detector_options,
    )
    initial_windows = selection_windows(
        detection_index,
        size=values.size,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
        signal_end=laser_off_index,
    )
    plateau_start, plateau_end, baseline_start, baseline_end = initial_windows
    baseline_reference = values[baseline_start:baseline_end]
    plateau_reference = values[plateau_start : min(plateau_end, plateau_start + 8)]
    baseline_center = float(np.median(baseline_reference))
    baseline_mad = float(np.median(np.abs(baseline_reference - baseline_center)))
    baseline_scale = 1.4826 * baseline_mad
    if baseline_scale == 0:
        baseline_scale = float(np.std(baseline_reference, ddof=1))
    relative_increase = float(signal_minimum_relative_increase)
    if relative_increase <= 0:
        raise ValueError("signal_minimum_relative_increase must be positive.")
    minimum_signal_level = max(
        baseline_center + 3 * baseline_scale,
        baseline_center * (1 + relative_increase),
    )
    if plateau_reference.size < 3 or float(np.median(plateau_reference)) <= minimum_signal_level:
        raise SelectionError(
            "The detected onset does not produce a plateau distinguishable from baseline."
        )
    signal_start, start_diagnostics = find_signal_start(
        values,
        detected_onset=detection_index,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        sigma_threshold=signal_start_sigma_threshold,
        relative_increase=signal_start_relative_increase,
    )
    if canonical == "laser_guided":
        signal_start = detection_index
        start_diagnostics = {"signal_start_source": "laser_guided"}
    signal_end, washout_start, end_diagnostics = find_signal_end(
        values,
        signal_start=signal_start,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        plateau_start=plateau_start,
        plateau_end=plateau_end,
        sigma_threshold=signal_end_sigma_threshold,
        consecutive=signal_end_consecutive,
        fraction_threshold=signal_end_fraction_threshold,
        baseline_sigma_threshold=signal_end_baseline_sigma_threshold,
        baseline_consecutive=signal_end_baseline_consecutive,
        minimum_signal_fraction=signal_end_minimum_signal_fraction,
    )
    if laser_off_index is not None:
        signal_end = laser_off_index
        washout_start = signal_end + 1 if signal_end + 1 < values.size else None
        end_diagnostics = {"signal_end_source": "laser_guided"}
    unrestricted_signal_end = int(signal_end)
    duration_limit_applied = False
    duration_limit_index = -1
    if signal_duration_limit_seconds is not None:
        duration_limit = float(signal_duration_limit_seconds)
        if not np.isfinite(duration_limit) or duration_limit <= 0:
            raise ValueError("signal_duration_limit_seconds must be positive or None.")
        if times is None:
            raise ValueError(
                "time values are required when signal_duration_limit_seconds is set."
            )
        target_time = float(times[signal_start] + duration_limit)
        duration_limit_index = int(np.argmin(np.abs(times - target_time)))
        duration_limit_index = max(int(signal_start), duration_limit_index)
        if signal_end > duration_limit_index:
            signal_end = duration_limit_index
            washout_start = min(signal_end + 1, values.size - 1)
            duration_limit_applied = True
        end_diagnostics = {
            **end_diagnostics,
            "signal_end_unrestricted_index": unrestricted_signal_end,
            "signal_end_unrestricted_time": float(times[unrestricted_signal_end]),
            "signal_duration_limit_seconds": duration_limit,
            "signal_duration_limit_index": duration_limit_index,
            "signal_duration_limit_time": float(times[duration_limit_index]),
            "signal_duration_limit_applied": int(duration_limit_applied),
        }
    else:
        end_diagnostics = {
            **end_diagnostics,
            "signal_end_unrestricted_index": unrestricted_signal_end,
            "signal_duration_limit_seconds": "none",
            "signal_duration_limit_index": -1,
            "signal_duration_limit_applied": 0,
        }
    falling_edge_index = int(end_diagnostics.get("signal_end_falling_edge_start", -1))
    on_peak_diagnostics: dict[str, float] = {}
    if times is not None:
        detected_end = falling_edge_index if falling_edge_index >= 0 else unrestricted_signal_end
        on_peak_diagnostics["on_peak_detected_seconds"] = float(
            times[detected_end] - times[signal_start]
        )
    windows = selection_windows(
        detection_index,
        size=values.size,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
        signal_end=signal_end,
    )
    plateau_start, plateau_end, baseline_start, baseline_end = windows
    width_mode = str(plateau_width_mode).strip().lower().replace("-", "_")
    width_mode = {
        "auto": "signal",
        "full_signal": "signal",
        "fixed_duration": "duration",
        "fixed_sweeps": "sweeps",
    }.get(width_mode, width_mode)
    if width_mode not in {"sweeps", "signal", "duration"}:
        raise ValueError(
            "plateau_width_mode must be 'sweeps', 'signal', or 'duration'."
        )
    if width_mode == "signal":
        # ``signal_end`` identifies an inclusive measured sweep, whereas the
        # plateau end is an exclusive boundary. Ending at that index retains
        # the complete stable ablation profile without including the final
        # edge point or any washout-baseline sweeps.
        plateau_end = max(plateau_start + 1, int(signal_end))
    elif width_mode == "duration":
        if plateau_duration_seconds is None:
            raise ValueError(
                "plateau_duration_seconds is required for duration mode."
            )
        duration = float(plateau_duration_seconds)
        if not np.isfinite(duration) or duration <= 0:
            raise ValueError("plateau_duration_seconds must be positive.")
        if times is None:
            raise ValueError(
                "time values are required for a duration-based plateau."
            )
        target_time = float(times[plateau_start] + duration)
        duration_end = int(np.argmin(np.abs(times - target_time)))
        plateau_end = min(
            int(signal_end),
            max(plateau_start + 1, duration_end),
        )
    plateau_end = min(int(signal_end), int(plateau_end))
    if plateau_end <= plateau_start:
        raise SelectionError(
            "The requested plateau width leaves no usable pre-washout sweeps."
        )
    plateau_diagnostics: dict[str, float | int | str] = {
        "plateau_width_mode": width_mode,
        "plateau_width_sweeps": int(plateau_end - plateau_start),
    }
    if times is not None:
        plateau_diagnostics["plateau_width_seconds"] = float(
            times[plateau_end] - times[plateau_start]
        )
    if plateau_duration_seconds is not None:
        plateau_diagnostics["plateau_duration_seconds_requested"] = float(
            plateau_duration_seconds
        )
    return SelectionResult(
        plateau_start,
        plateau_end,
        baseline_start,
        baseline_end,
        detection_index=detection_index,
        method=canonical,
        signal_end=signal_end,
        washout_start=washout_start,
        signal_start_index=signal_start,
        diagnostics={
            **diagnostics,
            **start_diagnostics,
            **end_diagnostics,
            **on_peak_diagnostics,
            **plateau_diagnostics,
        },
    )


def autopick_signal_regions(data, **options) -> SelectionResult:
    """Pick new windows with one measured sweep outside each plateau edge.

    The detector's statistical boundaries and diagnostics remain available via
    ``select_signal_regions``. Use this entry point for import and re-picking;
    never apply it to saved or manually reviewed selections.
    Laser-guided windows retain their predicted edges without grab gaps.
    """
    selection = select_signal_regions(data, **options)
    if selection.method == "laser_guided":
        return selection
    # Plateau [p, q) excludes sweep q; signal [s, e] includes sweep e.
    # A grab gap of one sweep AFTER the displayed exclusive boundary q means
    # e >= q + 1 (not q or q - 1). Before p means s <= p - 1. Both are local
    # acquisition indices, clamped to [0, n - 1], never the following spot.
    signal_end = min(len(data) - 1, max(selection.signal_end, selection.plateau_end + 1))
    washout_start = selection.washout_start
    if washout_start is not None and washout_start <= signal_end:
        washout_start = signal_end + 1 if signal_end + 1 < len(data) else None
    # Never pull the signal start back into the baseline window [b0, b1):
    # when the plateau starts right after the baseline, keep the detected start.
    signal_start = max(min(selection.signal_start, selection.baseline_end),
                       min(selection.signal_start, selection.plateau_start - 1), 0)
    return replace(
        selection,
        signal_start_index=signal_start,
        signal_end=signal_end,
        washout_start=washout_start,
    )


def select_baseline_peak(
    data,
    baseline_sweep_n: int = 10,
    peak_sweep_n: int = 20,
    threshold: float = 0.1,
    filter_percentile: float = 70,
    baseline_offset: int = 2,
    plateau_offset: int = 2,
    *,
    method: str = "threshold",
    **method_options,
) -> tuple[int, int, int, int]:
    """Return legacy four-index bounds from the modern selection result."""
    return select_signal_regions(
        data,
        method=method,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        threshold=threshold,
        filter_percentile=filter_percentile,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
        **method_options,
    ).as_tuple()


def resolve_session_manifest(directory, session_manifest):
    """Return the SessionManifest selected by ``read_experiment_data``."""
    if session_manifest is False:
        return None
    from .session_manifest import (
        SessionManifest,
        find_session_manifest,
        read_session_manifest,
    )

    if isinstance(session_manifest, SessionManifest):
        return session_manifest
    if session_manifest is None or session_manifest is True:
        if isinstance(directory, QtegraSession):
            if not directory.sources:
                return None
            directory = directory.sources[0].path
        directory = Path(directory).expanduser()
        if directory.is_file():
            directory = directory.parent
        found = find_session_manifest(directory)
        return None if found is None else read_session_manifest(found)
    return read_session_manifest(session_manifest)


def read_experiment_data(
    directory,
    primary_channel='29Si',
    baseline_sweep_n=10,
    peak_sweep_n=20,
    threshold=0.1,
    filter_percentile=70,
    baseline_offset=2,
    plateau_offset=2,
    sep=None,
    sample_aliases: Mapping[str, str] | None = None,
    selection_channels: Mapping[str, str] | None = None,
    selection_method='threshold',
    selection_fallback_methods: tuple[str, ...] = (),
    selection_options=None,
    signal_duration_limit_seconds=None,
    session_manifest=None,
    *,
    detector_calibration=None,
    cycle_time_s=None,
    restored_selections=None,
    laser_qc_threshold_s=2.0,
    signal_duration_limit_source="fixed",
):
    """Read Qtegra exports or an in-memory session and initialize selections.

    Acquisitions retain reader order (sequence order for single CSVs, time
    order for per-spot folders). Their relative sweep times are aligned onto
    one session axis, and baseline, plateau, signal-start, and signal-end
    windows are initialized for every analysis.

    Parameters
    ----------
    directory : str, path-like or QtegraSession
        Rich CSV, per-spot folder or file, folder containing these exports,
        session bundle root, or a QtegraSession.
    primary_channel : str
        Default matrix-sensitive channel used to detect ablation signals.
    baseline_sweep_n, peak_sweep_n : int
        Requested baseline and plateau window lengths in measured sweeps.
    threshold, filter_percentile : float
        Parameters used by the threshold fallback detector.
    baseline_offset, plateau_offset : int
        Numbers of transition sweeps left between the detected onset and the
        baseline or plateau window, respectively.
    sep : str, optional
        Delimiter used to obtain the sample label from each spot name. When
        omitted, the session manifest's ``sep`` is used, else ``'_'``.
    sample_aliases : mapping of str to str, optional
        Exact, case-insensitive replacements applied after splitting each spot
        name at ``sep``. This preserves legacy parsing while allowing session
        labels such as ``MAD_UCSB`` or ``MAD`` to resolve to the reference
        library name ``MADUCSB`` without editing the instrument exports.
        Merged over the session manifest's ``[samples]`` map; an explicit
        entry wins over the manifest's entry for the same alias.
    selection_channels : mapping of str to str, optional
        Per-spot or per-sample channels for automatic signal-window detection.
        Exact spot names take precedence; otherwise sample keys are matched
        case-insensitively after ``sample_aliases`` are applied. This is useful
        when the preferred matrix channel is absent from a glass acquisition,
        or when one anomalous analysis needs a better-resolved onset channel.
    selection_fallback_methods : tuple of str, optional
        Ordered detector methods tried only when ``selection_method`` raises a
        :class:`SelectionError`. The successful method is recorded for each
        analysis, making mixed-method recovery explicit and auditable.
    selection_options : mapping, optional
        Extra parameters forwarded to the registered signal-onset detectors.
    signal_duration_limit_seconds : float or None
        Optional maximum elapsed time from detected signal start to inclusive
        signal end. ``None`` preserves the unrestricted automatic endpoint.
    session_manifest : path-like, SessionManifest, bool or None
        The ``session_manifest.toml`` of a database session bundle
        (:mod:`UPbRPy.session_manifest`). ``None`` (default) looks for one
        inside ``directory`` (a bundle root, whose ``raw/`` is then read) or
        beside it (``directory`` is the bundle's ``raw/``). A CSV source looks
        in its parent folder with the same rules; an in-memory session uses
        its first source path. ``False`` ignores any manifest. A manifest
        supplies ``sep``, ``sample_aliases`` and per-acquisition samples;
        explicit separator and alias arguments still win.
    detector_calibration : str, mapping or None, keyword-only
        None or 'qtegra' retains exported Y. 'constant', 'drift', or a mapping
        with mode, on, window, primary_standard, channels and smoothing_s fits
        and applies detector factors before building tables and selections.
    cycle_time_s : float or None, keyword-only
        Nominal sweep spacing for Rich CSVs with StartTime but no Time rows.
        None uses DEFAULT_CYCLE_TIME_S (0.4255 s); otherwise finite and > 0.
        Recorded Time rows win; per-spot input and in-memory sessions ignore
        this keyword. Reconstructed timing emits one UserWarning per import.
    restored_selections : sequence of mappings or None, keyword-only
        Complete saved threshold selections that the caller will overlay after
        import. When every generated field is saved, initialize the selection
        schema from one spot only. Incomplete records and other detectors keep
        full automatic selection.
    laser_qc_threshold_s : float, keyword-only
        Flag absolute onset residuals strictly above this threshold (default
        2.0 s). Informational only; requires usable session laser times.
    signal_duration_limit_source : str, keyword-only
        'fixed' (default) uses signal_duration_limit_seconds. 'laser' uses
        each spot's firing duration where available, otherwise the fixed limit.

    Returns
    -------
    experiment : pandas.DataFrame
        One row per analysis with file provenance and initialized selection
        times and diagnostics.
    element_channels : list of str
        Signal channel names reported in the instrument exports.
    total_data : pandas.DataFrame
        All measured sweeps on one session time axis, with spot and sample
        identifiers attached.
    """
    manifest = resolve_session_manifest(directory, session_manifest)
    if manifest is not None:
        from .session_manifest import raw_directory_for

        if not isinstance(directory, QtegraSession):
            directory = raw_directory_for(directory)
        if sep is None and manifest.sep:
            sep = manifest.sep
        if manifest.sample_aliases:
            merged = {
                str(alias).strip().casefold(): target
                for alias, target in manifest.sample_aliases.items()
            }
            for alias, target in (sample_aliases or {}).items():
                merged[str(alias).strip().casefold()] = target
            sample_aliases = merged
    if sep is None:
        sep = '_'

    session = (
        directory if isinstance(directory, QtegraSession)
        else open_qtegra(Path(directory).expanduser(), cycle_time_s=cycle_time_s)
    )
    if session.times_reconstructed:
        warnings.warn(
            f"Sweep times reconstructed with cycle_time_s={session.nominal_cycle_time_s} s from StartTime.",
            UserWarning, stacklevel=2,
        )
    calibration_options = (
        dict(detector_calibration) if isinstance(detector_calibration, Mapping)
        else {"mode": "qtegra" if detector_calibration is None else detector_calibration}
    )
    mode = calibration_options.pop("mode", "qtegra")
    calibration_record = {"mode": "qtegra"}
    if mode != "qtegra":
        from .calibration import apply_cross_calibration, fit_cross_calibration

        calibration = fit_cross_calibration(
            session, channel=calibration_options.pop("channels", None),
            mode=mode, sep=sep, **calibration_options,
        )
        session = apply_cross_calibration(session, calibration)
        calibration_record = calibration.to_dict()
    experiment, element_channels, total_data = experiment_from_session(
        session, sep=sep, sample_aliases=sample_aliases,
        acquisition_samples=manifest.acquisition_samples() if manifest is not None else None,
    )
    experiment.attrs["detector_calibration"] = calibration_record

    # The axis itself remains the reader's recorded-start axis. Window offsets
    # use manifest.started_at (not the potentially rounded CSV start), because
    # UPbDB computed the log clock correction against that instant.
    if manifest is not None and manifest.has_laser_data:
        from .laser_guidance import add_laser_timing

        windows = {
            row.label: window for row in manifest.acquisitions
            if (window := manifest.acquisition_laser_window(row.label)) is not None
        }
        # The manifest convenience method requires an on-time. Preserve an
        # off-only incomplete firing for display, although it cannot guide a
        # selection or contribute a transport-delay pair.
        for row in manifest.acquisitions:
            laser = row.laser
            if row.label in windows or laser is None or laser.laser_off_at is None:
                continue
            log = manifest.laser_log(laser.log_sha256)
            if log is None or log.clock_offset_s is None:
                continue
            try:
                start = pd.Timestamp(row.started_at)
                if pd.isna(start):
                    continue
                start = start.tz_localize('UTC') if start.tzinfo is None else start.tz_convert('UTC')
                off = (pd.Timestamp(laser.laser_off_at) - start).total_seconds() + log.clock_offset_s
            except (ValueError, OverflowError):
                continue
            windows[row.label] = (None, off)
        add_laser_timing(
            experiment, windows,
            dict(zip(experiment.spot_names, experiment.spot_starting_times)),
            scan_positions={row.label: row.laser.scan_position for row in manifest.acquisitions
                            if row.laser is not None and row.laser.scan_position is not None},
            log_sha256=tuple(log.sha256 for log in manifest.laser_logs if log.sha256),
            spot_key=",".join(dict.fromkeys(log.spot_key for log in manifest.laser_logs if log.spot_key)),
        )

    selection_data = total_data
    if restored_selections:
        from .project import _complete_threshold_selections, _restore_selection_inputs_valid

        if (
            'laser' not in experiment.attrs
            and _SELECTORS.get('threshold') is threshold_onset
            and _complete_threshold_selections(
                experiment, restored_selections, selection_method, selection_fallback_methods,
            )
            and _restore_selection_inputs_valid(total_data, primary_channel, selection_channels)
        ):
            # One ordinary pick preserves pandas' inferred dtypes and column
            # order. Every generated value is overwritten by the caller; no
            # other threshold pick can introduce another output column.
            selection_data = total_data.loc[
                total_data['spot_name'] == total_data['spot_name'].iloc[0]
            ]
    pick_args = (
        primary_channel, baseline_sweep_n, peak_sweep_n, threshold,
        filter_percentile, baseline_offset, plateau_offset,
    )
    pick_options = dict(
        selection_channels=selection_channels,
        selection_method=selection_method,
        selection_fallback_methods=selection_fallback_methods,
        selection_options=selection_options,
        signal_duration_limit_seconds=signal_duration_limit_seconds,
        laser_qc_threshold_s=laser_qc_threshold_s,
        signal_duration_limit_source=signal_duration_limit_source,
    )
    if selection_data is total_data:
        experiment = auto_select_baseline_peak(
            experiment, total_data, *pick_args, **pick_options,
        )
    else:
        picked = auto_select_baseline_peak(
            experiment.copy(), selection_data, *pick_args, **pick_options,
        )
        if picked['selection_error'].notna().any():
            # A failed first pick creates columns in a different order. Keep
            # the ordinary path if the raw trace no longer yields a template.
            experiment = auto_select_baseline_peak(
                experiment, total_data, *pick_args, **pick_options,
            )
        else:
            experiment = picked

    # Keep the parameters that produced the initial selections with the
    # experiment table. Interactive editors can then reproduce the same
    # auto-pick instead of silently falling back to the selector defaults.
    # These attrs contain only scalar values and a shallow options mapping, so
    # they remain safe for ordinary pandas slicing and concatenation.
    from .laser_guidance import nondefault_laser_settings

    experiment.attrs['selection_parameters'] = {
        'baseline_sweep_n': int(baseline_sweep_n),
        'peak_sweep_n': int(peak_sweep_n),
        'threshold': float(threshold),
        'filter_percentile': float(filter_percentile),
        'baseline_offset': int(baseline_offset),
        'plateau_offset': int(plateau_offset),
        'selection_method': str(selection_method),
        'selection_fallback_methods': tuple(selection_fallback_methods),
        'selection_options': nondefault_laser_settings(selection_options or {}),
        'signal_duration_limit_seconds': signal_duration_limit_seconds,
        'selection_channels': dict(selection_channels or {}),
    }

    experiment.attrs['selection_parameters'].update(nondefault_laser_settings({
        'laser_qc_threshold_s': float(laser_qc_threshold_s),
        'signal_duration_limit_source': signal_duration_limit_source,
    }))
    return experiment, element_channels, total_data


def acquisition_summary(session: QtegraSession) -> dict:
    """Keep small, JSON-safe acquisition metadata on the recorded clock.

    ISO export text retains every fractional digit. Per-spot text is kept
    alongside its parsed ISO representation. Missing dwell values stay None.
    """
    acquisitions = session.acquisitions
    timed = sorted((a for a in acquisitions if a.start is not None), key=lambda a: a.start)
    stopped = sorted((a for a in acquisitions if a.stop is not None), key=lambda a: a.stop)
    summary = {}
    for prefix, acquisition in (("first_start", timed[0] if timed else None),
                                ("last_start", timed[-1] if timed else None),
                                ("last_stop", stopped[-1] if stopped else None)):
        field = "stop" if prefix == "last_stop" else "start"
        stamp = getattr(acquisition, field) if acquisition else None
        original = getattr(acquisition, field + "_text") if acquisition else None
        summary[prefix] = (original if original and "T" in original else stamp.isoformat()) if stamp else None
        summary[prefix + "_text"] = original
        summary[prefix + "_date"] = stamp.date().isoformat() if stamp else None
    dwell = {}
    for acquisition in acquisitions:
        for channel in acquisition.channels:
            dwell.setdefault(channel.name, [])
            if channel.dwell_s is not None and np.isfinite(channel.dwell_s):
                dwell[channel.name].append(channel.dwell_s)
    sweeps = [a.n_sweeps for a in acquisitions]
    summary.update(
        users=sorted({a.user for a in acquisitions if a.user}),
        machine=session.machine,
        tune_names=sorted({a.tune_name for a in acquisitions if a.tune_name}),
        dwell_s={name: float(np.median(values)) if values else None for name, values in dwell.items()},
        sweeps_per_acquisition={"median": float(np.median(sweeps)) if sweeps else None,
                                "min": int(min(sweeps)) if sweeps else None,
                                "max": int(max(sweeps)) if sweeps else None},
        laser_output_settings_percent=sorted({str(value) for a in acquisitions
            for key, value in a.sequence_fields.items()
            if key == "Laser Output Setting [%]" and value not in (None, "")}),
    )
    return summary


def sample_from_label(label, sep="_", aliases=None):
    """Derive a sample from an acquisition label using the import alias rule."""
    prefix = str(label).split(sep)[0]
    normalized = {str(key).strip().casefold(): str(value).strip()
                  for key, value in (aliases or {}).items()}
    return normalized.get(prefix.casefold(), prefix)


def experiment_from_session(session, sep="_", sample_aliases=None, acquisition_samples=None):
    """Build experiment and sweep tables from a QtegraSession before selection.

    Return ``(experiment, element_channels, total_data)`` in the historical
    column order and dtypes. Session seconds use each acquisition's first
    channel time and recorded start.
    Without recorded starts, acquisitions follow sequence order on a continuous
    axis: the next start follows the previous last sweep by its median spacing.
    Calendar gaps cannot be recovered from per-acquisition Time rows alone.
    Relative seconds pass through pandas nanosecond timedeltas and absolute
    timestamps before subtraction, matching the original per-spot reader.
    Source paths and JSON-safe session provenance are retained.
    ``acquisition_samples`` maps spot labels to manifest sample names. Both
    label prefixes and supplied samples use ``sample_aliases``. A supplied
    sample equivalent to the prefix or its alias preserves the derived name;
    other non-empty samples override it in both tables and are recorded in
    ``experiment.attrs["sample_overrides"]``.
    ``experiment.attrs["counting_dwell"]["dwell_s"]`` maps each channel to
    the integration time that converts its cps to counts, and ``"source"``
    says how it was obtained; see :func:`UPbRPy.qtegra.counting_dwell`.
    """
    if not session.acquisitions:
        raise ValueError("Cannot build an experiment from an empty QtegraSession.")
    aliases = {
        str(source).strip().casefold(): str(target).strip()
        for source, target in (sample_aliases or {}).items()
    }
    starts = []
    samples = []
    sample_overrides = {}
    spot_frames = []
    # A label the instrument repeated (six blanks all named ``nothing`` in one
    # Rich export) keeps its first acquisition only, as UPbDB does; spot
    # names must be unique for selections, projects and exports.
    acquisitions = []
    seen_labels = set()
    repeated_labels = []
    for acquisition in session.acquisitions:
        if acquisition.label in seen_labels:
            repeated_labels.append(acquisition.label)
            continue
        seen_labels.add(acquisition.label)
        acquisitions.append(acquisition)
    next_start = min(
        (a.start for a in acquisitions if a.start is not None),
        default=pd.Timestamp("1970-01-01"),
    )
    for acquisition in acquisitions:
        if acquisition.sweep_time is None:
            raise ValueError(f"{acquisition.label}: no sweep times.")
        if acquisition.start is not None:
            start = acquisition.start
        else:
            start = next_start
        finite_times = acquisition.sweep_time[np.isfinite(acquisition.sweep_time)]
        spacings = np.diff(finite_times)
        spacings = spacings[spacings > 0]
        spacing = float(np.median(spacings)) if spacings.size else 0.0
        duration = float(finite_times[-1]) + spacing if finite_times.size else 0.0
        next_start = start + pd.to_timedelta(duration, unit="s")
        starts.append(start)
        prefix = acquisition.label.split(sep)[0]
        derived = sample_from_label(acquisition.label, sep, aliases)
        sample = derived
        given = (acquisition_samples or {}).get(acquisition.label)
        if given:
            resolved = aliases.get(given.casefold(), given)
            if resolved.casefold() not in {prefix.casefold(), derived.casefold()}:
                sample = resolved
                sample_overrides[acquisition.label] = {
                    "label_sample": derived,
                    "sample": sample,
                    "source": "session_manifest",
                }
        samples.append(sample)
        values = {"Time": acquisition.sweep_time}
        values.update({
            name: acquisition.y[:, index]
            for index, name in enumerate(acquisition.channel_names)
        })
        if acquisition._data_dtypes:
            values = {
                name: data for name, data in values.items()
                if name in acquisition._data_dtypes
            }
        this_data = pd.DataFrame(values)
        if not acquisition._data_dtypes:
            this_data = this_data.dropna(axis=1, how="all")
        this_data = this_data.apply(pd.to_numeric, errors="coerce")
        for name, dtype in acquisition._data_dtypes.items():
            if name in this_data and np.issubdtype(np.dtype(dtype), np.integer):
                column = this_data[name]
                # A caller may have changed Y after reading the session.
                if column.notna().all() and (column % 1 == 0).all():
                    this_data[name] = column.astype(dtype)
        this_data["Time"] = start + pd.to_timedelta(this_data["Time"], unit="s")
        this_data["spot_name"] = acquisition.label
        this_data["sample"] = sample
        spot_frames.append(this_data)

    experiment = pd.DataFrame({
        "spot_names": [acquisition.label for acquisition in acquisitions],
        "sample": samples,
        "spot_paths": [
            str(acquisition.source_path) if acquisition.source_path is not None else None
            for acquisition in acquisitions
        ],
        "spot_starting_times": starts,
    })
    total_data = pd.concat(spot_frames, ignore_index=True)
    experiment = experiment.sort_values(by="spot_names").reset_index(drop=True)
    experiment_start_time = experiment["spot_starting_times"].min()
    experiment["spot_starting_times"] = (
        experiment["spot_starting_times"] - experiment_start_time
    ).dt.total_seconds()
    total_data["Time"] = (total_data["Time"] - experiment_start_time).dt.total_seconds()
    for column in (
        "plateau_start_times", "plateau_end_times", "baseline_start_times",
        "baseline_end_times", "signal_start_times", "signal_end_times",
    ):
        experiment[column] = None
    experiment.attrs["sample_overrides"] = sample_overrides
    experiment.attrs["sample_label_rule"] = {"sep": sep, "aliases": dict(sample_aliases or {})}
    # Y is in cps; Poisson variances need the integration time per reading.
    dwell = counting_dwell(session)
    experiment.attrs["counting_dwell"] = {
        "dwell_s": {name: value for name, (value, _source) in dwell.items()},
        "source": {name: source for name, (_value, source) in dwell.items()},
    }
    experiment.attrs["qtegra"] = {
        "format": session.format,
        "capabilities": sorted(session.capabilities),
        "sources": [
            {"path": str(source.path), "sha256": source.sha256, "format": source.format}
            for source in session.sources
        ],
        "times_reconstructed": session.times_reconstructed,
        "nominal_cycle_time_s": session.nominal_cycle_time_s,
        "clock_offset_hours": session.clock_offset_hours,
        "software": session.software,
        "machine": session.machine,
        "acquisition_summary": acquisition_summary(session),
        "warnings": list(session.warnings) + [
            f"{label}: repeated label; only its first acquisition is used."
            for label in dict.fromkeys(repeated_labels)
        ],
    }
    return experiment, list(session.channel_names), total_data


def read_spot_data(spot_path):
    '''
    function for reading spot data from a .csv file exported from Thermo iCap

    Parameters
    ----------
    spot_path : str
        path to the file containing the spot data

    Returns
    -------
    spot_data : pandas.DataFrame
        a pandas dataframe containing the spot data
    '''
    spot_data = pd.read_csv(spot_path, skiprows=13)
    spot_data = spot_data.iloc[1:].reset_index(drop=True)
    spot_data = spot_data.dropna(axis=1, how='all')
    spot_data = spot_data.apply(pd.to_numeric, errors='coerce')
    return spot_data

def auto_select_baseline_peak(experiment,
                              total_data,
                              primary_channel,
                              baseline_sweep_n =10,
                              peak_sweep_n = 20,
                              threshold=0.1,
                              filter_percentile=70,
                              baseline_offset=2,
                              plateau_offset=2,
                              selection_channels: Mapping[str, str] | None = None,
                              selection_method='threshold',
                              selection_fallback_methods: tuple[str, ...] = (),
                              selection_options: Mapping[str, object] | None = None,
                              signal_duration_limit_seconds: float | None = None,
                              laser_qc_threshold_s: float = 2.0,
                              signal_duration_limit_source: str = "fixed"):
    """Initialize signal windows for every analysis in an experiment.

    Parameters
    ----------
    experiment : pandas.DataFrame
        Per-analysis metadata containing ``spot_names`` and, normally,
        canonical ``sample`` labels.
    total_data : pandas.DataFrame
        Session sweep table with ``spot_name``, ``Time``, and signal channels.
    primary_channel : str
        Default signal channel for non-overridden analyses.
    selection_channels : mapping, optional
        Case-insensitive spot- or sample-specific detection channels.
    selection_method : str
        Primary registered onset detector.
    selection_fallback_methods : tuple of str, optional
        Detectors attempted in order only after the primary method fails.
    selection_options : mapping, optional
        Extra detector parameters, such as ``sigma_threshold``.
    signal_duration_limit_seconds : float, optional
        Nominal on-peak duration after detected signal start. Signal handles
        may extend one sweep beyond the resulting plateau boundary.
    laser_qc_threshold_s : float, optional
        Informational onset residual threshold in seconds, default 2.0.
    signal_duration_limit_source : str, optional
        'fixed' (default), or 'laser' to use per-spot firing durations with
        the fixed limit as fallback. Laser timing columns and attrs can be
        installed with UPbRPy.laser_guidance.add_laser_timing.

    Returns
    -------
    experiment : pandas.DataFrame
        The same table with selection times, method/channel provenance,
        diagnostics, and any unresolved error message added.
    """
    method_options = dict(selection_options or {})
    option_duration_limit = method_options.pop(
        'signal_duration_limit_seconds', None
    )
    if signal_duration_limit_seconds is None:
        signal_duration_limit_seconds = option_duration_limit
    from .laser_guidance import laser_selection_options, update_laser_qc

    laser_qc_threshold_s = method_options.pop('laser_qc_threshold_s', laser_qc_threshold_s)
    signal_duration_limit_source = method_options.pop(
        'signal_duration_limit_source', signal_duration_limit_source,
    )
    # Validate even if a session has no timed firings.
    laser_selection_options({}, signal_duration_limit_source=signal_duration_limit_source)
    has_laser = 'laser' in experiment.attrs
    guided = resolve_method(selection_method) == 'laser_guided'
    uses_guidance = has_laser and (guided or any(
        _ALIASES.get(normalize_method_name(method)) == 'laser_guided'
        for method in selection_fallback_methods
    ))
    if has_laser and uses_guidance:
        reference_method = next(
            (resolve_method(method) for method in selection_fallback_methods
             if resolve_method(method) != 'laser_guided'), 'threshold',
        )
        # A separate data-only pass prevents guided predictions from entering
        # the delay estimate. Use the same channel and selection parameters.
        reference = experiment.drop(columns=[
            c for c in experiment if c.startswith('laser_') and c != 'laser_duration_s'
        ]).copy()
        reference.attrs.pop('laser', None)
        reference = auto_select_baseline_peak(
            reference, total_data, primary_channel, baseline_sweep_n, peak_sweep_n,
            threshold, filter_percentile, baseline_offset, plateau_offset,
            selection_channels=selection_channels,
            selection_method=reference_method if guided else selection_method,
            selection_fallback_methods=() if guided else (reference_method,),
            selection_options=method_options,
            signal_duration_limit_seconds=signal_duration_limit_seconds,
            signal_duration_limit_source=signal_duration_limit_source,
        )
        experiment['laser_detected_onset_time'] = pd.to_numeric(
            reference['signal_start_times'], errors='coerce',
        ).where(reference['selection_error'].isna())
        experiment.attrs['laser']['reference_method'] = reference_method
        update_laser_qc(experiment, threshold_s=laser_qc_threshold_s)
    elif has_laser:
        experiment['laser_detected_onset_time'] = np.nan
        experiment.attrs['laser'].pop('reference_method', None)
    channels_by_sample = {
        str(sample).strip().casefold(): str(channel).strip()
        for sample, channel in (selection_channels or {}).items()
    }

    # Group once so automatic selection does not rescan the complete session
    # table for every analysis.
    for spot_name, spot_data in total_data.groupby('spot_name', sort=False):
        row_mask = experiment['spot_names'] == spot_name
        sample_name = (
            str(experiment.loc[row_mask, 'sample'].iloc[0])
            if 'sample' in experiment.columns
            else str(spot_name).split('_')[0]
        )
        # Silicon is normally the cleanest primary signal for glass. Some
        # acquisition methods omit Si, however, so use the mineral workflow's
        # configured primary channel rather than failing the whole session.
        use_glass_silicon = 'NIST' in spot_name and '29Si' in spot_data.columns
        selection_channel = channels_by_sample.get(str(spot_name).casefold())
        if selection_channel is None:
            selection_channel = channels_by_sample.get(sample_name.casefold())
        if selection_channel is None:
            selection_channel = '29Si' if use_glass_silicon else primary_channel
        if selection_channel not in spot_data.columns:
            raise KeyError(
                f"Selection channel {selection_channel!r} is not present for "
                f"spot {spot_name!r}."
            )
        values = spot_data[selection_channel].tolist()
        spot_options = {}
        spot_duration_limit = signal_duration_limit_seconds
        if has_laser or (signal_duration_limit_source == 'laser' and 'laser_duration_s' in experiment):
            spot_options = laser_selection_options(
                experiment.loc[row_mask].iloc[0],
                experiment.attrs.get('laser', {}).get('transport_delay_s'),
                signal_duration_limit_source=signal_duration_limit_source,
                signal_duration_limit_seconds=signal_duration_limit_seconds,
            )
            spot_duration_limit = spot_options.pop('signal_duration_limit_seconds')
        attempted_methods = [
            selection_method,
            *selection_fallback_methods,
        ]
        selection = None
        selection_errors = []
        for candidate_method in attempted_methods:
            candidate_options = dict(method_options)
            if resolve_method(candidate_method) == 'laser_guided':
                candidate_options.update(spot_options)
            try:
                selection = autopick_signal_regions(
                    values,
                    time=spot_data['Time'].to_numpy(dtype=float),
                    method=candidate_method,
                    baseline_sweep_n=baseline_sweep_n,
                    peak_sweep_n=peak_sweep_n,
                    threshold=threshold,
                    filter_percentile=filter_percentile,
                    baseline_offset=baseline_offset,
                    plateau_offset=plateau_offset,
                    signal_duration_limit_seconds=spot_duration_limit,
                    **candidate_options,
                )
                break
            except SelectionError as error:
                selection_errors.append(
                    f"{resolve_method(candidate_method)}: {error}"
                )
        if selection is None:
            experiment.loc[row_mask, 'selection_method'] = resolve_method(
                selection_method
            )
            experiment.loc[row_mask, 'selection_channel'] = selection_channel
            experiment.loc[row_mask, 'selection_error'] = " | ".join(
                selection_errors
            )
            continue
        if has_laser and not guided and selection.method != 'laser_guided':
            experiment.loc[row_mask, 'laser_detected_onset_time'] = spot_data.iloc[selection.signal_start]['Time']
        experiment.loc[row_mask, 'selection_error'] = None
        experiment.loc[row_mask, 'plateau_start_times'] = spot_data.iloc[selection.plateau_start]['Time']
        experiment.loc[row_mask, 'plateau_end_times'] = spot_data.iloc[selection.plateau_end]['Time']
        experiment.loc[row_mask, 'baseline_start_times'] = spot_data.iloc[selection.baseline_start]['Time']
        experiment.loc[row_mask, 'baseline_end_times'] = spot_data.iloc[selection.baseline_end]['Time']
        experiment.loc[row_mask, 'signal_start_times'] = spot_data.iloc[selection.signal_start]['Time']
        experiment.loc[row_mask, 'signal_end_times'] = spot_data.iloc[selection.signal_end]['Time']
        experiment.loc[row_mask, 'selection_method'] = selection.method
        experiment.loc[row_mask, 'selection_channel'] = selection_channel
        experiment.loc[row_mask, 'selection_detection_index'] = selection.detection_index
        experiment.loc[row_mask, 'selection_signal_end_index'] = selection.signal_end
        experiment.loc[row_mask, 'selection_unrestricted_signal_end_index'] = (
            selection.diagnostics['signal_end_unrestricted_index']
        )
        experiment.loc[row_mask, 'selection_signal_duration_limit_seconds'] = (
            spot_duration_limit
        )
        experiment.loc[row_mask, 'selection_signal_duration_limit_applied'] = (
            selection.diagnostics['signal_duration_limit_applied']
        )
        experiment.loc[row_mask, 'selection_washout_start_index'] = selection.washout_start
        experiment.loc[row_mask, 'selection_post_baseline_start_index'] = selection.washout_start
        experiment.loc[row_mask, 'selection_falling_edge_index'] = (
            selection.diagnostics.get('signal_end_falling_edge_start', -1)
        )
        if 'sigma_threshold' in selection.diagnostics:
            experiment.loc[row_mask, 'selection_sigma_threshold'] = selection.diagnostics['sigma_threshold']
        experiment.loc[row_mask, 'selection_error'] = None

    if has_laser:
        update_laser_qc(experiment, threshold_s=laser_qc_threshold_s)
    return experiment

def subtract_baseline(experiment, channels, total_data):
    """Subtract each spot's selected baseline from all requested channels.

    Spots are grouped once and accumulated in a list before concatenation. This
    keeps runtime and temporary memory approximately linear in the number of
    rows instead of repeatedly scanning and copying the complete session table.

    Parameters
    ----------
    experiment : pandas.DataFrame
        a pandas dataframe containing the experiment metadata
    channels : list
        a list of the channels in the data
    total_data : pandas.DataFrame
        a pandas dataframe containing the total data

    Returns
    -------
    baseline_corrected_total_data : pandas.DataFrame
        a pandas dataframe containing the baseline corrected data
    """
    if total_data.empty:
        return total_data.copy()

    selection_by_spot = (
        experiment.drop_duplicates('spot_names', keep='first')
        .set_index('spot_names')[['baseline_start_times', 'baseline_end_times']]
    )
    corrected_frames = []
    channel_names = list(channels)
    for spot_name, spot_frame in total_data.groupby('spot_name', sort=False):
        spot_frame = spot_frame.copy()
        baseline_start, baseline_end = selection_by_spot.loc[spot_name]
        baseline_mask = (
            (spot_frame['Time'] >= baseline_start)
            & (spot_frame['Time'] < baseline_end)
        )
        baseline_means = spot_frame.loc[baseline_mask, channel_names].mean()
        spot_frame.loc[:, channel_names] = spot_frame[channel_names].subtract(
            baseline_means, axis='columns'
        )
        corrected_frames.append(spot_frame)

    result = pd.concat(corrected_frames, ignore_index=True)
    return result.sort_values(by='Time', ignore_index=True)


__all__ = [
    'SELECTION_METHODS',
    'SelectionError',
    'SelectionResult',
    'available_selection_methods',
    'register_selection_method',
    'laser_guided_onset',
    'selection_method_label',
    'select_baseline_peak',
    'select_signal_regions',
    'autopick_signal_regions',
    'read_experiment_data',
    'experiment_from_session',
    'sample_from_label',
    'acquisition_summary',
    'resolve_session_manifest',
    'read_spot_data',
    'auto_select_baseline_peak',
    'subtract_baseline',
    'normalize_method_name',
    'resolve_method',
    'coerce_trace',
    'validate_common_options',
    'selection_windows',
    'robust_center_scale',
    'find_signal_start',
    'find_signal_end',
    'threshold_onset',
    'rolling_sigma_onset',
    'excel_expanding_sigma_onset',
]
