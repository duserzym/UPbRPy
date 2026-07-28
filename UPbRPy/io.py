"""Experiment I/O and reusable baseline/signal-window selection strategies.

Selection indices use the same boundary convention as the original UPbRPy
workflow: starts are inclusive and ends identify the time boundary used by
downstream ``start <= Time < end`` slicing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

import os
from datetime import datetime

import numpy as np
import pandas as pd


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


def available_selection_methods() -> tuple[str, ...]:
    """Return canonical names for all registered onset detectors."""
    return tuple(_SELECTORS)


def selection_method_label(method: str) -> str:
    """Return a concise label suitable for notebook output and GUI controls."""
    canonical = resolve_method(method)
    return {
        "threshold": "Signal threshold",
        "rolling_sigma": "Running mean + σ threshold",
        "excel_expanding_sigma": "Excel expanding mean + σ threshold",
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
        choices = ", ".join(available_selection_methods())
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
    """Detect onset above a rolling mean plus a configurable sigma threshold."""
    percentile = float(filter_percentile)
    if not 0 <= percentile <= 100:
        raise ValueError("filter_percentile must be between 0 and 100.")
    percentile_value = float(np.percentile(values, percentile))
    rough_plateau = values[values > percentile_value]
    if rough_plateau.size == 0:
        rough_plateau = values[np.argmax(values) : np.argmax(values) + 1]
    threshold_value = float(np.mean(rough_plateau) * float(threshold))
    search_start = max(0, int(baseline_sweep_n))
    hits = np.flatnonzero(values[search_start:] > threshold_value) + search_start
    if hits.size == 0:
        raise SelectionError(
            f"No signal exceeded the calculated threshold ({threshold_value:g})."
        )
    return int(hits[0]), {
        "threshold_value": threshold_value,
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
    **method_options,
) -> SelectionResult:
    """Select baseline and plateau windows using a registered onset detector.

    ``threshold`` reproduces the original percentile-based algorithm.
    ``rolling_sigma`` compares each candidate onset against the mean and sample
    standard deviation of the preceding ``rolling_window`` sweeps.
    ``excel_expanding_sigma`` reproduces the reference workbook's expanding
    mean plus eight-standard-deviation onset detector. When
    ``signal_duration_limit_seconds`` is supplied, the freely selected signal
    end is capped at the measured sweep nearest that many seconds after the
    detected signal start. The default ``None`` leaves the end unconstrained.
    """
    values = coerce_trace(data)
    common = validate_common_options(
        values.size,
        baseline_sweep_n,
        peak_sweep_n,
        baseline_offset,
        plateau_offset,
    )
    baseline_sweep_n, peak_sweep_n, baseline_offset, plateau_offset = common
    canonical = resolve_method(method)
    detection_index, diagnostics = _SELECTORS[canonical](
        values,
        baseline_sweep_n=baseline_sweep_n,
        threshold=threshold,
        filter_percentile=filter_percentile,
        **method_options,
    )
    initial_windows = selection_windows(
        detection_index,
        size=values.size,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
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
    unrestricted_signal_end = int(signal_end)
    duration_limit_applied = False
    duration_limit_index = -1
    if signal_duration_limit_seconds is not None:
        duration_limit = float(signal_duration_limit_seconds)
        if not np.isfinite(duration_limit) or duration_limit <= 0:
            raise ValueError("signal_duration_limit_seconds must be positive or None.")
        if time is None:
            raise ValueError(
                "time values are required when signal_duration_limit_seconds is set."
            )
        times = np.asarray(time, dtype=float)
        if times.ndim != 1 or times.size != values.size:
            raise ValueError("time must be one-dimensional and match the signal data.")
        if not np.all(np.isfinite(times)):
            raise ValueError("time must contain only finite values.")
        if np.any(np.diff(times) <= 0):
            raise ValueError("time must be strictly increasing within a spot.")
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
    windows = selection_windows(
        detection_index,
        size=values.size,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
        signal_end=signal_end,
    )
    return SelectionResult(
        *windows,
        detection_index=detection_index,
        method=canonical,
        signal_end=signal_end,
        washout_start=washout_start,
        signal_start_index=signal_start,
        diagnostics={**diagnostics, **start_diagnostics, **end_diagnostics},
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


def read_experiment_data(
    directory,
    primary_channel='29Si',
    baseline_sweep_n=10,
    peak_sweep_n=20,
    threshold=0.1,
    filter_percentile=70,
    baseline_offset=2,
    plateau_offset=2,
    sep='_',
    selection_method='threshold',
    selection_options=None,
    signal_duration_limit_seconds=None,
):
    '''
    function for extracting spot names from files exported from Thermo iCap

    You may follow the instruction on Iolite website below for guidance on generating the proper files from iCap
    https://iolite.xyz/docs/import_prep/exporting_data_from_instruments.html

    Parameters
    ----------
    directory : str
        path to the folder containing the files
        we assume that the files in the folders are in .csv format
    signal_duration_limit_seconds : float or None
        Optional maximum elapsed time from detected signal start to inclusive
        signal end. ``None`` preserves the unrestricted automatic endpoint.

    Returns
    -------
    spot_names : list
        list of spot names extracted from the files
    spot_paths : list
        list of paths to the files
    '''
    spot_names = []
    spot_paths = []
    spot_starting_times = []
    csv_files = [name for name in os.listdir(directory) if name.endswith('.csv')]
    if not csv_files:
        raise FileNotFoundError(f"No CSV files were found in {directory!r}.")

    # Use one instrument export to obtain the common channel names.
    with open(os.path.join(directory, csv_files[0])) as file_handle:
        first_file = file_handle.readlines()

    # now update the global variable channels
    element_channels = first_file[13].split(',')[1:-1]

    time_format = "%m/%d/%Y %I:%M:%S %p"

    # Read only the short header during discovery; numerical tables are loaded
    # once below after the experiment has been placed in chronological order.
    for filename in csv_files:
        this_path = os.path.join(directory, filename)
        spot_paths.append(this_path)
        with open(this_path) as file_handle:
            first_line = file_handle.readline()
        spot_names.append(first_line.split(':')[0])
        spot_starting_times.append(
            datetime.strptime(first_line[-24:-2], time_format)
        )

    # Create a pandas dataframe to store the experiment data.
    experiment = pd.DataFrame()
    experiment['spot_names'] = spot_names
    experiment['sample'] = [spot_name.split(sep)[0] for spot_name in spot_names]
    experiment['spot_paths'] = spot_paths
    experiment['spot_starting_times'] = spot_starting_times

    experiment = experiment.sort_values(by='spot_starting_times').reset_index(drop=True)
    spot_frames = []
    for i in range(experiment.shape[0]):
        # Accumulate spot frames and concatenate once. Repeated concatenation
        # would copy all previously imported rows for every additional spot.
        this_data = pd.read_csv(experiment['spot_paths'][i], skiprows=13)
        this_data = this_data.iloc[1:].reset_index(drop=True)
        this_data = this_data.dropna(axis=1, how='all')
        this_data = this_data.apply(pd.to_numeric, errors='coerce')
        # Convert relative seconds to absolute timestamps without a Python row
        # loop; pandas performs this operation as a vectorized timedelta add.
        this_data['Time'] = (
            experiment['spot_starting_times'][i]
            + pd.to_timedelta(this_data['Time'], unit='s')
        )
        this_data['spot_name'] = experiment['spot_names'][i]
        this_data['sample'] = experiment['sample'][i]
        spot_frames.append(this_data)

    total_data = pd.concat(spot_frames, ignore_index=True)

    experiment = experiment.sort_values(by='spot_names').reset_index(drop=True)
    #initialize the columns for the experiment dataframe with null values

    # we should also align the time column in both the experiment and total data to be in total seconds with respect to the start of the experiment
    # this will help with the later session-wide fractionation corrections
    experiment_start_time = experiment['spot_starting_times'].min()
    experiment['spot_starting_times'] = (experiment['spot_starting_times'] - experiment_start_time).dt.total_seconds()
    total_data['Time'] = (total_data['Time'] - experiment_start_time).dt.total_seconds()

    experiment['plateau_start_times'] = None
    experiment['plateau_end_times'] = None
    experiment['baseline_start_times'] = None
    experiment['baseline_end_times'] = None
    experiment['signal_start_times'] = None
    experiment['signal_end_times'] = None

    experiment = auto_select_baseline_peak(
        experiment,
        total_data,
        primary_channel,
        baseline_sweep_n,
        peak_sweep_n,
        threshold,
        filter_percentile,
        baseline_offset,
        plateau_offset,
        selection_method=selection_method,
        selection_options=selection_options,
        signal_duration_limit_seconds=signal_duration_limit_seconds,
    )

    # Keep the parameters that produced the initial selections with the
    # experiment table. Interactive editors can then reproduce the same
    # auto-pick instead of silently falling back to the selector defaults.
    # These attrs contain only scalar values and a shallow options mapping, so
    # they remain safe for ordinary pandas slicing and concatenation.
    experiment.attrs['selection_parameters'] = {
        'baseline_sweep_n': int(baseline_sweep_n),
        'peak_sweep_n': int(peak_sweep_n),
        'threshold': float(threshold),
        'filter_percentile': float(filter_percentile),
        'baseline_offset': int(baseline_offset),
        'plateau_offset': int(plateau_offset),
        'selection_method': str(selection_method),
        'selection_options': dict(selection_options or {}),
        'signal_duration_limit_seconds': signal_duration_limit_seconds,
    }

    return experiment, element_channels, total_data

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
                              selection_method='threshold',
                              selection_options: Mapping[str, object] | None = None,
                              signal_duration_limit_seconds: float | None = None):
    '''
    specific function for selecting the baseline and peak regions in the data given a parsed experiment dataframe

    Parameters
    ----------
    experiment: pandas.DataFrame
        a pandas dataframe containing the experiment metadata
    total_data : pandas.DataFrame
        a pandas dataframe containing the total data
    baseline_z_threshold : float
        the threshold for selecting the baseline region
    plateau_z_threshold : float
        the threshold for selecting the peak region

    Returns
    -------
    experiment : pandas.DataFrame
        a pandas dataframe containing the spot data with the baseline and peak regions selected
    '''
    method_options = dict(selection_options or {})
    option_duration_limit = method_options.pop(
        'signal_duration_limit_seconds', None
    )
    if signal_duration_limit_seconds is None:
        signal_duration_limit_seconds = option_duration_limit

    # Group once so automatic selection does not rescan the complete session
    # table for every analysis.
    for spot_name, spot_data in total_data.groupby('spot_name', sort=False):
        # always use 29Si for glass
        if 'NIST' in spot_name:
            values = spot_data['29Si'].tolist()
        else:
            values = spot_data[primary_channel].tolist()
        row_mask = experiment['spot_names'] == spot_name
        try:
            selection = select_signal_regions(
                values,
                time=spot_data['Time'].to_numpy(dtype=float),
                method=selection_method,
                baseline_sweep_n=baseline_sweep_n,
                peak_sweep_n=peak_sweep_n,
                threshold=threshold,
                filter_percentile=filter_percentile,
                baseline_offset=baseline_offset,
                plateau_offset=plateau_offset,
                signal_duration_limit_seconds=signal_duration_limit_seconds,
                **method_options,
            )
        except SelectionError as error:
            experiment.loc[row_mask, 'selection_method'] = resolve_method(selection_method)
            experiment.loc[row_mask, 'selection_channel'] = '29Si' if 'NIST' in spot_name else primary_channel
            experiment.loc[row_mask, 'selection_error'] = str(error)
            continue
        experiment.loc[row_mask, 'plateau_start_times'] = spot_data.iloc[selection.plateau_start]['Time']
        experiment.loc[row_mask, 'plateau_end_times'] = spot_data.iloc[selection.plateau_end]['Time']
        experiment.loc[row_mask, 'baseline_start_times'] = spot_data.iloc[selection.baseline_start]['Time']
        experiment.loc[row_mask, 'baseline_end_times'] = spot_data.iloc[selection.baseline_end]['Time']
        experiment.loc[row_mask, 'signal_start_times'] = spot_data.iloc[selection.signal_start]['Time']
        experiment.loc[row_mask, 'signal_end_times'] = spot_data.iloc[selection.signal_end]['Time']
        experiment.loc[row_mask, 'selection_method'] = selection.method
        experiment.loc[row_mask, 'selection_channel'] = '29Si' if 'NIST' in spot_name else primary_channel
        experiment.loc[row_mask, 'selection_detection_index'] = selection.detection_index
        experiment.loc[row_mask, 'selection_signal_end_index'] = selection.signal_end
        experiment.loc[row_mask, 'selection_unrestricted_signal_end_index'] = (
            selection.diagnostics['signal_end_unrestricted_index']
        )
        experiment.loc[row_mask, 'selection_signal_duration_limit_seconds'] = (
            signal_duration_limit_seconds
        )
        experiment.loc[row_mask, 'selection_signal_duration_limit_applied'] = (
            selection.diagnostics['signal_duration_limit_applied']
        )
        experiment.loc[row_mask, 'selection_washout_start_index'] = selection.washout_start
        experiment.loc[row_mask, 'selection_post_baseline_start_index'] = selection.washout_start
        if 'sigma_threshold' in selection.diagnostics:
            experiment.loc[row_mask, 'selection_sigma_threshold'] = selection.diagnostics['sigma_threshold']
        experiment.loc[row_mask, 'selection_error'] = None

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
    'selection_method_label',
    'select_baseline_peak',
    'select_signal_regions',
    'read_experiment_data',
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
