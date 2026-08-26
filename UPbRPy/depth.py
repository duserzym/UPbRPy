"""Calibration of signal-relative ablation time to physical depth.

The reduction pipeline deliberately preserves ``signal_relative_time`` as the
primary measured coordinate.  This module adds a physical ``depth_um``
coordinate only when the user supplies a session- and matrix-appropriate
pulse--depth calibration.  It does not infer drilling rate from isotope data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LinearDepthCalibration:
    """Describe a linear crater-depth calibration.

    Parameters
    ----------
    depth_per_pulse_um
        Fitted depth removed per laser pulse, in micrometres per pulse.
    repetition_rate_hz
        Laser repetition rate used for the calibration and unknowns.
    intercept_um
        Fitted depth-axis intercept.  A nonzero value can diagnose a surface
        reconstruction offset; it should be forced to zero only when justified.
    coefficient_covariance
        Two-by-two covariance matrix for ``(intercept, depth_per_pulse)``.
    residual_1s_um
        One-standard-deviation pit-to-pit residual scatter about the fit.
    repetition_rate_1s_hz
        Optional uncertainty of the laser repetition rate.
    geometry_1s_um
        Optional term representing crater relief and calibration transfer.
    pulse_range
        Minimum and maximum calibrated pulse counts.  Rows outside this range
        are retained but explicitly marked as extrapolations.
    force_zero
        Whether the fit was constrained to pass through the origin.
    """

    depth_per_pulse_um: float
    repetition_rate_hz: float
    intercept_um: float = 0.0
    coefficient_covariance: tuple[tuple[float, float], tuple[float, float]] = (
        (0.0, 0.0),
        (0.0, 0.0),
    )
    residual_1s_um: float = 0.0
    repetition_rate_1s_hz: float = 0.0
    geometry_1s_um: float = 0.0
    pulse_range: tuple[float, float] = (0.0, np.inf)
    force_zero: bool = False

    def __post_init__(self) -> None:
        """Validate physical values and normalize tuple-backed metadata."""
        if not np.isfinite(self.depth_per_pulse_um) or self.depth_per_pulse_um <= 0:
            raise ValueError("depth_per_pulse_um must be finite and positive.")
        if not np.isfinite(self.repetition_rate_hz) or self.repetition_rate_hz <= 0:
            raise ValueError("repetition_rate_hz must be finite and positive.")
        covariance = np.asarray(self.coefficient_covariance, dtype=float)
        if covariance.shape != (2, 2) or not np.all(np.isfinite(covariance)):
            raise ValueError("coefficient_covariance must be a finite 2 x 2 matrix.")
        if any(
            not np.isfinite(value) or value < 0
            for value in (
                self.residual_1s_um,
                self.repetition_rate_1s_hz,
                self.geometry_1s_um,
            )
        ):
            raise ValueError("Depth-calibration uncertainty terms must be nonnegative.")
        pulse_range = tuple(float(value) for value in self.pulse_range)
        if len(pulse_range) != 2 or pulse_range[0] > pulse_range[1]:
            raise ValueError(
                "pulse_range must contain ordered minimum and maximum values."
            )
        object.__setattr__(
            self,
            "coefficient_covariance",
            tuple(tuple(float(value) for value in row) for row in covariance),
        )
        object.__setattr__(self, "pulse_range", pulse_range)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON/TOML-friendly calibration record."""
        values = asdict(self)
        values["model"] = "linear"
        values["coefficient_covariance"] = [
            list(row) for row in self.coefficient_covariance
        ]
        values["pulse_range"] = list(self.pulse_range)
        return values


def linear_depth_calibration_from_dict(
    values: Mapping[str, object],
) -> LinearDepthCalibration:
    """Reconstruct :class:`LinearDepthCalibration` from saved settings."""
    data = dict(values)
    model = str(data.pop("model", "linear")).strip().lower()
    if model != "linear":
        raise ValueError(f"Unsupported saved depth-calibration model {model!r}.")
    return LinearDepthCalibration(**data)


def fit_linear_depth_calibration(
    pulse_counts,
    depths_um,
    *,
    repetition_rate_hz,
    depth_1s_um=None,
    force_zero=False,
    repetition_rate_1s_hz=0.0,
    geometry_1s_um=0.0,
) -> LinearDepthCalibration:
    """Fit measured crater depths as a linear function of laser pulse count.

    When ``depth_1s_um`` is supplied, inverse-variance weighted least squares
    is used and its stated absolute uncertainties define coefficient
    covariance.  Otherwise the ordinary least-squares covariance is scaled by
    the residual variance.  Replicate pits and several pulse counts are
    strongly preferred over a single endpoint depth.
    """
    pulses, depths = np.broadcast_arrays(
        np.asarray(pulse_counts, dtype=float),
        np.asarray(depths_um, dtype=float),
    )
    valid = np.isfinite(pulses) & np.isfinite(depths) & (pulses >= 0)
    if depth_1s_um is not None:
        depth_sigma = np.broadcast_to(
            np.asarray(depth_1s_um, dtype=float), pulses.shape
        )
        valid &= np.isfinite(depth_sigma) & (depth_sigma > 0)
    else:
        depth_sigma = np.ones(pulses.shape, dtype=float)
    pulses = pulses[valid]
    depths = depths[valid]
    depth_sigma = depth_sigma[valid]
    parameter_count = 1 if force_zero else 2
    if pulses.size <= parameter_count:
        raise ValueError(
            "Depth calibration requires more valid pits than fitted parameters."
        )

    design = pulses[:, None] if force_zero else np.column_stack(
        (np.ones(pulses.size), pulses)
    )
    weights = 1 / depth_sigma**2
    normal = design.T @ (weights[:, None] * design)
    normal_inverse = np.linalg.pinv(normal)
    coefficients = normal_inverse @ design.T @ (weights * depths)
    fitted = design @ coefficients
    residuals = depths - fitted
    degrees_of_freedom = pulses.size - parameter_count
    residual_variance = float(np.sum(residuals**2) / degrees_of_freedom)
    if depth_1s_um is None:
        parameter_covariance = normal_inverse * residual_variance
    else:
        parameter_covariance = normal_inverse

    if force_zero:
        intercept = 0.0
        slope = float(coefficients[0])
        covariance = np.zeros((2, 2), dtype=float)
        covariance[1, 1] = parameter_covariance[0, 0]
    else:
        intercept = float(coefficients[0])
        slope = float(coefficients[1])
        covariance = parameter_covariance
    if slope <= 0:
        raise ValueError("The fitted drilling rate is not positive.")

    return LinearDepthCalibration(
        depth_per_pulse_um=slope,
        repetition_rate_hz=float(repetition_rate_hz),
        intercept_um=intercept,
        coefficient_covariance=tuple(
            tuple(float(value) for value in row) for row in covariance
        ),
        residual_1s_um=float(np.sqrt(max(residual_variance, 0.0))),
        repetition_rate_1s_hz=float(repetition_rate_1s_hz),
        geometry_1s_um=float(geometry_1s_um),
        pulse_range=(float(np.min(pulses)), float(np.max(pulses))),
        force_zero=bool(force_zero),
    )


def sweep_time_bounds(
    frame: pd.DataFrame,
    *,
    time_column="signal_relative_time",
    spot_column="spot_name",
) -> tuple[np.ndarray, np.ndarray]:
    """Return finite time bounds represented by every sequential sweep.

    Interior boundaries are halfway between adjacent sweep coordinates.  The
    first interval begins at zero when the first retained coordinate is the
    reviewed signal start; the final interval uses half of the preceding sweep
    spacing.  Bounds are calculated independently for each spot.
    """
    if time_column not in frame.columns:
        raise KeyError(f"Missing depth-coordinate column {time_column!r}.")
    lower = np.full(len(frame), np.nan, dtype=float)
    upper = np.full(len(frame), np.nan, dtype=float)
    groups = (
        frame.groupby(spot_column, sort=False, dropna=False).indices.values()
        if spot_column in frame.columns
        else (np.arange(len(frame)),)
    )
    times_all = pd.to_numeric(frame[time_column], errors="coerce").to_numpy()
    for positions in groups:
        positions = np.asarray(positions, dtype=int)
        times = times_all[positions]
        order = np.argsort(times, kind="stable")
        ordered_positions = positions[order]
        ordered_times = times[order]
        finite = np.isfinite(ordered_times)
        ordered_positions = ordered_positions[finite]
        ordered_times = ordered_times[finite]
        if not ordered_times.size:
            continue
        if ordered_times.size == 1:
            lower[ordered_positions] = np.maximum(0.0, ordered_times)
            upper[ordered_positions] = np.maximum(0.0, ordered_times)
            continue
        midpoints = (ordered_times[:-1] + ordered_times[1:]) / 2
        first_lower = max(0.0, ordered_times[0] - (midpoints[0] - ordered_times[0]))
        last_upper = ordered_times[-1] + (ordered_times[-1] - midpoints[-1])
        lower[ordered_positions] = np.r_[first_lower, midpoints]
        upper[ordered_positions] = np.r_[midpoints, last_upper]
    return lower, upper


def apply_depth_calibration(
    frame: pd.DataFrame,
    calibration: LinearDepthCalibration | Mapping[str, object],
    *,
    time_column="signal_relative_time",
    spot_column="spot_name",
    signal_start_1s_seconds=0.0,
) -> pd.DataFrame:
    """Add calibrated depth coordinates and uncertainty to sweep data.

    The returned copy retains signal-relative time and adds the sweep midpoint
    ``depth_um``, finite interval bounds, a one-standard-deviation depth
    uncertainty, pulse count, and an extrapolation flag.  The uncertainty
    includes fitted coefficient covariance, repetition-rate uncertainty,
    signal-start precision, and the configured crater-geometry term.  Fitted
    residual scatter is reported in the calibration metadata but is not added
    again when ``geometry_1s_um`` already represents transfer scatter.
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame.")
    if not isinstance(calibration, LinearDepthCalibration):
        calibration = linear_depth_calibration_from_dict(calibration)
    result = frame.copy()
    relative_time = pd.to_numeric(
        result[time_column], errors="coerce"
    ).to_numpy(dtype=float)
    pulse_count = calibration.repetition_rate_hz * relative_time
    result["laser_pulse_count"] = pulse_count
    result["depth_um"] = (
        calibration.intercept_um
        + calibration.depth_per_pulse_um * pulse_count
    )

    time_lower, time_upper = sweep_time_bounds(
        result,
        time_column=time_column,
        spot_column=spot_column,
    )
    result["depth_interval_start_um"] = (
        calibration.intercept_um
        + calibration.depth_per_pulse_um
        * calibration.repetition_rate_hz
        * time_lower
    )
    result["depth_interval_end_um"] = (
        calibration.intercept_um
        + calibration.depth_per_pulse_um
        * calibration.repetition_rate_hz
        * time_upper
    )

    coefficient_covariance = np.asarray(
        calibration.coefficient_covariance, dtype=float
    )
    design = np.column_stack((np.ones(len(result)), pulse_count))
    coefficient_variance = np.einsum(
        "ij,jk,ik->i", design, coefficient_covariance, design
    )
    signal_start_sigma = np.broadcast_to(
        np.asarray(signal_start_1s_seconds, dtype=float), relative_time.shape
    )
    if np.any(~np.isfinite(signal_start_sigma)) or np.any(signal_start_sigma < 0):
        raise ValueError("signal_start_1s_seconds must be finite and nonnegative.")
    depth_variance = (
        coefficient_variance
        + (
            calibration.depth_per_pulse_um
            * relative_time
            * calibration.repetition_rate_1s_hz
        ) ** 2
        + (
            calibration.depth_per_pulse_um
            * calibration.repetition_rate_hz
            * signal_start_sigma
        ) ** 2
        + calibration.geometry_1s_um**2
    )
    result["depth_1s_um"] = np.sqrt(np.clip(depth_variance, 0.0, None))
    result["depth_2s_um"] = 2 * result["depth_1s_um"]
    result["depth_calibration_extrapolated"] = (
        (pulse_count < calibration.pulse_range[0])
        | (pulse_count > calibration.pulse_range[1])
    )
    result.attrs = dict(frame.attrs)
    result.attrs["depth_calibration"] = calibration.to_dict()
    return result


__all__ = [
    "LinearDepthCalibration",
    "apply_depth_calibration",
    "fit_linear_depth_calibration",
    "linear_depth_calibration_from_dict",
    "sweep_time_bounds",
]
