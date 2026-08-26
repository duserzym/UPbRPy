"""Covariance models and uncertainty-propagation utilities."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .constants import (
    common_U238_U235,
    lambda_235,
    lambda_232,
    lambda_238,
    sigma_235,
    sigma_232,
    sigma_238,
    sigma_common_U238_U235,
)
from .age import calc_76_date, radiogenic_76_derivative


@dataclass(frozen=True)
class UncertaintySettings:
    """User-selectable systematic uncertainty components.

    Baseline uncertainty is propagated by the raw-ratio calculation and is on
    by default. Decay constants, the natural ``238U/235U`` ratio, and external
    reproducibility remain explicit opt-in reporting components so analytical
    and systematic branches are never silently mixed.
    """

    propagate_baseline: bool = True
    propagate_decay_constants: bool = False
    propagate_u238_u235: bool = False
    long_term_reproducibility_mode: str = "none"
    long_term_reproducibility_relative_1s: float = 0.0
    u238_u235: float = common_U238_U235
    u238_u235_1s: float = sigma_common_U238_U235
    lambda_235_1s: float = sigma_235
    lambda_238_1s: float = sigma_238
    lambda_232_1s: float = sigma_232

    def __post_init__(self):
        """Validate modes and non-negative one-sigma inputs."""
        mode = str(self.long_term_reproducibility_mode).strip().lower()
        if mode not in {"none", "secondary", "user"}:
            raise ValueError(
                "long_term_reproducibility_mode must be 'none', 'secondary', or 'user'"
            )
        for name in (
            "long_term_reproducibility_relative_1s",
            "u238_u235_1s",
            "lambda_235_1s",
            "lambda_238_1s",
            "lambda_232_1s",
        ):
            value = float(getattr(self, name))
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if not np.isfinite(self.u238_u235) or self.u238_u235 <= 0:
            raise ValueError("u238_u235 must be a finite positive number")
        object.__setattr__(self, "long_term_reproducibility_mode", mode)

    def to_dict(self) -> dict[str, object]:
        """Return serialization-friendly settings for projects and notebooks."""
        return asdict(self)


@dataclass(frozen=True)
class CommonPbUncertaintySettings:
    """One-sigma uncertainty and correlation assigned to initial Pb ratios.

    The values supplied by Schmitz's working reduction document are retained
    as an explicit ``boise_working`` preset, not presented as a universal
    Stacey--Kramers model uncertainty. Published software and studies use
    materially different assumptions.
    """

    relative_1s_206_204: float = 0.0061
    relative_1s_207_204: float = 0.0052
    relative_1s_208_204: float = 0.0063
    rho_206_207: float = 0.98
    rho_206_208: float = 0.98
    rho_207_208: float = 0.98
    source: str = "boise_working"

    def covariance(self, ratios) -> np.ndarray:
        """Return the absolute covariance matrix for 206/204, 207/204, 208/204."""
        ratios = np.asarray(ratios, dtype=float)
        if ratios.shape != (3,):
            raise ValueError("ratios must contain 206/204, 207/204, and 208/204")
        sigmas = ratios * np.array(
            [
                self.relative_1s_206_204,
                self.relative_1s_207_204,
                self.relative_1s_208_204,
            ]
        )
        correlation = np.array(
            [
                [1.0, self.rho_206_207, self.rho_206_208],
                [self.rho_206_207, 1.0, self.rho_207_208],
                [self.rho_206_208, self.rho_207_208, 1.0],
            ]
        )
        return np.outer(sigmas, sigmas) * correlation


def common_pb_uncertainty_preset(name="boise_working") -> CommonPbUncertaintySettings:
    """Return a named, provenance-explicit initial-Pb uncertainty convention.

    These are reporting assumptions, not uncertainties supplied by the
    Stacey--Kramers (1975) evolution equation itself. ``horstwood2016`` uses
    the relative one-sigma assumptions printed in that paper's example table;
    ``et_redux`` mirrors its generic 1% and rho=0.5 defaults.
    """
    key = str(name).strip().lower().replace("-", "_")
    if key in {"boise", "boise_working", "schmitz_working"}:
        return CommonPbUncertaintySettings()
    if key in {"horstwood", "horstwood2016"}:
        return CommonPbUncertaintySettings(
            relative_1s_206_204=0.0080,
            relative_1s_207_204=0.0032,
            relative_1s_208_204=0.0074,
            rho_206_207=0.0,
            rho_206_208=0.0,
            rho_207_208=0.0,
            source="horstwood2016_example",
        )
    if key in {"et_redux", "etredux"}:
        return CommonPbUncertaintySettings(
            relative_1s_206_204=0.01,
            relative_1s_207_204=0.01,
            relative_1s_208_204=0.01,
            rho_206_207=0.5,
            rho_206_208=0.5,
            rho_207_208=0.5,
            source="et_redux_default",
        )
    raise ValueError(
        "Unknown common-Pb uncertainty preset; use boise_working, "
        "horstwood2016, or et_redux."
    )


class CovarianceStore(dict):
    """Dictionary-like covariance metadata that is safe in ``DataFrame.attrs``.

    Pandas copies and compares ``attrs`` during slicing and concatenation. A
    regular dictionary containing NumPy arrays makes those operations both
    expensive and ambiguous because array equality does not produce one bool.
    Covariance metadata is immutable after construction, so retaining the same
    store across DataFrame copies is intentional.
    """

    def __deepcopy__(self, memo):
        """Return this immutable store instead of copying large covariance arrays."""
        return self

    def __eq__(self, other):
        """Compare stores by identity to avoid ambiguous NumPy array equality."""
        return self is other


def inverse_standard_deviation_weights(standard_deviations):
    """Return np.polyfit weights, which are 1/sigma rather than 1/sigma**2."""
    standard_deviations = np.asarray(standard_deviations, dtype=float)
    valid = np.isfinite(standard_deviations) & (standard_deviations > 0)
    fallback = np.nanmedian(standard_deviations[valid]) if np.any(valid) else 1.0
    safe_standard_deviations = np.where(valid, standard_deviations, fallback)
    return 1 / safe_standard_deviations

def polynomial_prediction_covariance(coefficient_covariance, x_values):
    """Propagate fitted polynomial-coefficient covariance to predictions at ``x_values``."""
    coefficient_covariance = np.asarray(coefficient_covariance, dtype=float)
    x_values = np.asarray(x_values, dtype=float)
    degree = coefficient_covariance.shape[0] - 1
    design = np.vander(x_values, N=degree + 1)
    return design @ coefficient_covariance @ design.T

def linear_interpolation_weight_matrix(x_grid, x_values):
    """Return a linear-interpolation matrix mapping grid values to x_values."""
    x_grid = np.asarray(x_grid, dtype=float)
    x_values = np.asarray(x_values, dtype=float)
    if x_grid.ndim != 1 or x_grid.size < 2:
        raise ValueError("x_grid must be a one-dimensional array with at least two points.")
    weights = np.zeros((x_values.size, x_grid.size), dtype=float)
    for row, value in enumerate(x_values):
        if value <= x_grid[0]:
            left = 0
            right = 1
        elif value >= x_grid[-1]:
            left = x_grid.size - 2
            right = x_grid.size - 1
        else:
            right = int(np.searchsorted(x_grid, value, side="right"))
            left = right - 1
        dx = x_grid[right] - x_grid[left]
        fraction = 0.0 if dx == 0 else (value - x_grid[left]) / dx
        weights[row, left] = 1 - fraction
        weights[row, right] = fraction
    return weights

def build_downhole_fractionation_metadata(unified_time, interpolated_fractionations, mean_fractionation=None):
    """Return mean downhole correction and covariance of the stacked mean."""
    unified_time = np.asarray(unified_time, dtype=float)
    interpolated_fractionations = np.asarray(interpolated_fractionations, dtype=float)
    if interpolated_fractionations.ndim != 2:
        raise ValueError("interpolated_fractionations must be a 2-D array.")
    if interpolated_fractionations.shape[1] != unified_time.size:
        raise ValueError("interpolated_fractionations columns must match unified_time.")

    if mean_fractionation is None:
        mean_fractionation = np.nanmean(interpolated_fractionations, axis=0)
    else:
        mean_fractionation = np.asarray(mean_fractionation, dtype=float)
        if mean_fractionation.shape != unified_time.shape:
            raise ValueError("mean_fractionation must match unified_time.")
    if interpolated_fractionations.shape[0] > 1 and np.all(
        np.isfinite(interpolated_fractionations)
    ):
        covariance = np.cov(
            interpolated_fractionations, rowvar=False, ddof=1
        ) / interpolated_fractionations.shape[0]
    elif interpolated_fractionations.shape[0] > 1:
        covariance = np.zeros((unified_time.size, unified_time.size), dtype=float)
        for left in range(unified_time.size):
            for right in range(left, unified_time.size):
                paired = (
                    np.isfinite(interpolated_fractionations[:, left])
                    & np.isfinite(interpolated_fractionations[:, right])
                )
                pair_count = int(np.count_nonzero(paired))
                if pair_count > 1:
                    pair_covariance = np.cov(
                        interpolated_fractionations[paired, left],
                        interpolated_fractionations[paired, right],
                        ddof=1,
                    )[0, 1] / pair_count
                    covariance[left, right] = pair_covariance
                    covariance[right, left] = pair_covariance
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        covariance = (
            eigenvectors
            @ np.diag(np.clip(eigenvalues, 0, None))
            @ eigenvectors.T
        )
    else:
        covariance = np.zeros((unified_time.size, unified_time.size), dtype=float)
    residuals = interpolated_fractionations - mean_fractionation
    finite_observations = int(np.count_nonzero(np.isfinite(residuals)))
    finite_time_points = int(np.count_nonzero(np.isfinite(mean_fractionation)))
    degrees_of_freedom = max(finite_observations - finite_time_points, 1)
    residual_standard_deviation = np.sqrt(
        np.nansum(residuals**2) / degrees_of_freedom
    )
    contributor_count = np.count_nonzero(
        np.isfinite(interpolated_fractionations), axis=0
    )
    singleton = contributor_count <= 1
    if np.any(singleton) and residual_standard_deviation > 0:
        diagonal = np.diag(covariance).copy()
        diagonal[singleton] = np.maximum(
            diagonal[singleton], residual_standard_deviation**2
        )
        np.fill_diagonal(covariance, diagonal)
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        covariance = (
            eigenvectors
            @ np.diag(np.clip(eigenvalues, 0, None))
            @ eigenvectors.T
        )

    return {
        "time": unified_time,
        "mean_fractionation": mean_fractionation,
        "covariance": covariance,
        "interpolated_fractionations": interpolated_fractionations,
        "contributor_count": contributor_count,
        "residuals": residuals,
        "residual_standard_deviation": residual_standard_deviation,
        "degrees_of_freedom": degrees_of_freedom,
    }

def evaluate_downhole_fractionation(metadata, relative_time_values):
    """Evaluate downhole correction and prediction covariance at relative times."""
    scalar_input = np.ndim(relative_time_values) == 0
    relative_time_values = np.atleast_1d(np.asarray(relative_time_values, dtype=float))
    time_grid = np.asarray(metadata["time"], dtype=float)
    mean_fractionation = np.asarray(metadata["mean_fractionation"], dtype=float)
    covariance = np.asarray(metadata["covariance"], dtype=float)
    weights = linear_interpolation_weight_matrix(time_grid, relative_time_values)
    prediction = weights @ mean_fractionation
    prediction_covariance = weights @ covariance @ weights.T
    prediction_1sigma = np.sqrt(np.clip(np.diag(prediction_covariance), 0, None))
    is_extrapolated = (relative_time_values < time_grid[0]) | (relative_time_values > time_grid[-1])

    result = {
        "prediction": prediction,
        "prediction_covariance": prediction_covariance,
        "prediction_1sigma": prediction_1sigma,
        "is_extrapolated": is_extrapolated,
        "weights": weights,
    }
    if scalar_input:
        result["prediction"] = float(result["prediction"][0])
        result["prediction_1sigma"] = float(result["prediction_1sigma"][0])
        result["is_extrapolated"] = bool(result["is_extrapolated"][0])
    return result

def propagate_multiplicative_correction_covariance(
    measured_values,
    correction_values,
    measured_1sigma=None,
    measured_covariance=None,
    correction_covariance=None,
):
    """Propagate covariance through ``z_i = r_i C_i``.

    This is the common linearized covariance step for both downhole and
    session fractionation corrections. Independent measured-ratio uncertainty
    contributes ``diag(C) Sigma_r diag(C)``. Shared correction-factor
    uncertainty contributes ``outer(r, r) * Sigma_C`` and can therefore create
    covariance between rows corrected with the same fitted curve.
    """
    measured_values = np.asarray(measured_values, dtype=float)
    correction_values = np.asarray(correction_values, dtype=float)
    if measured_values.shape != correction_values.shape:
        raise ValueError("measured_values and correction_values must have the same shape.")

    expected_shape = (measured_values.size, measured_values.size)
    if measured_covariance is not None:
        measured_covariance = np.asarray(measured_covariance, dtype=float)
    elif measured_1sigma is not None:
        measured_1sigma = np.asarray(measured_1sigma, dtype=float)
        if measured_1sigma.shape != measured_values.shape:
            raise ValueError("measured_1sigma must match measured_values.")
        measured_covariance = np.diag(measured_1sigma**2)
    else:
        measured_covariance = np.zeros(expected_shape, dtype=float)
    if measured_covariance.shape != expected_shape:
        raise ValueError("measured_covariance has incompatible shape.")

    if correction_covariance is None:
        correction_covariance = np.zeros(expected_shape, dtype=float)
    else:
        correction_covariance = np.asarray(correction_covariance, dtype=float)
    if correction_covariance.shape != expected_shape:
        raise ValueError("correction_covariance has incompatible shape.")

    correction_matrix = np.diag(correction_values)
    covariance_measurement = correction_matrix @ measured_covariance @ correction_matrix
    covariance_correction = np.outer(measured_values, measured_values) * correction_covariance
    covariance_total = covariance_measurement + covariance_correction

    return {
        "corrected_values": measured_values * correction_values,
        "covariance": covariance_total,
        "covariance_measurement": covariance_measurement,
        "covariance_correction": covariance_correction,
        "correction_factors": correction_values,
        "correction_covariance": correction_covariance,
    }

def propagate_downhole_corrected_ratio_covariance(
    raw_ratios,
    relative_time_values,
    downhole_result,
    raw_ratio_1sigma=None,
    raw_ratio_covariance=None,
):
    """Propagate raw sweep-ratio covariance through the downhole correction."""
    raw_ratios = np.asarray(raw_ratios, dtype=float)
    relative_time_values = np.asarray(relative_time_values, dtype=float)
    if raw_ratios.shape != relative_time_values.shape:
        raise ValueError("raw_ratios and relative_time_values must have the same shape.")

    if "metadata" in downhole_result:
        fit_evaluation = evaluate_downhole_fractionation(
            downhole_result["metadata"],
            relative_time_values,
        )
        correction_factors = np.asarray(fit_evaluation["prediction"], dtype=float)
        correction_covariance = np.asarray(fit_evaluation["prediction_covariance"], dtype=float)
        correction_1sigma = np.asarray(fit_evaluation["prediction_1sigma"], dtype=float)
        is_extrapolated = np.asarray(fit_evaluation["is_extrapolated"], dtype=bool)
    else:
        correction_factors = np.asarray(
            downhole_result["interpolation"](relative_time_values),
            dtype=float,
        )
        correction_covariance = np.zeros((raw_ratios.size, raw_ratios.size), dtype=float)
        correction_1sigma = np.zeros(raw_ratios.size, dtype=float)
        is_extrapolated = np.zeros(raw_ratios.size, dtype=bool)

    covariance_result = propagate_multiplicative_correction_covariance(
        raw_ratios,
        correction_factors,
        measured_1sigma=raw_ratio_1sigma,
        measured_covariance=raw_ratio_covariance,
        correction_covariance=correction_covariance,
    )
    return {
        "corrected_ratios": covariance_result["corrected_values"],
        "covariance": covariance_result["covariance"],
        "covariance_measurement": covariance_result["covariance_measurement"],
        "covariance_downhole": covariance_result["covariance_correction"],
        "correction_factors": covariance_result["correction_factors"],
        "correction_covariance": covariance_result["correction_covariance"],
        "correction_1sigma": correction_1sigma,
        "is_extrapolated": is_extrapolated,
    }

def relative_time_by_spot(data):
    """Return signal-start-relative times in the current row order.

    Ratio tables created by :func:`UPbRPy.ratios.calc_raw_ratios` carry an
    explicit ``signal_relative_time`` column. Older tables fall back to the
    first available row per spot for backward compatibility.
    """
    if "signal_relative_time" in data.columns:
        relative_time = data["signal_relative_time"].to_numpy(dtype=float)
        if np.all(np.isfinite(relative_time)):
            return relative_time
    relative_time = np.zeros(len(data), dtype=float)
    time_values = data["Time"].to_numpy(dtype=float)
    for row_positions in data.groupby("spot_name", sort=False).indices.values():
        relative_time[row_positions] = time_values[row_positions] - time_values[row_positions][0]
    return relative_time

def evaluate_session_fractionation_fit(session_fit_result, time_values, covariance_key="fit_covariance"):
    """Evaluate a session correction fit and its prediction covariance.

    Parameters
    ----------
    session_fit_result : dict
        One entry from a ``session_correction_results`` dictionary.
    time_values : float or array-like
        Absolute session time(s) at which the correction factor is needed.
    covariance_key : str
        Which coefficient covariance entry to use. The default uses the stated
        spot-uncertainty covariance. ``fit_covariance_overdispersion_scaled``
        can be requested where conservative excess-scatter diagnostics are
        desired.

    Returns
    -------
    dict
        ``prediction``, ``prediction_covariance``, ``prediction_1sigma``, and
        ``is_extrapolated`` arrays. Scalar input returns scalar values for
        ``prediction``, ``prediction_1sigma``, and ``is_extrapolated``.
    """
    scalar_input = np.ndim(time_values) == 0
    time_values = np.atleast_1d(np.asarray(time_values, dtype=float))
    coefficients = np.asarray(session_fit_result["fit"], dtype=float)
    prediction = np.polyval(coefficients, time_values)

    coefficient_covariance = session_fit_result.get(covariance_key)
    if coefficient_covariance is not None:
        prediction_covariance = polynomial_prediction_covariance(
            coefficient_covariance,
            time_values,
        )
        prediction_1sigma = np.sqrt(np.clip(np.diag(prediction_covariance), 0, None))
    else:
        fallback_sigma = float(session_fit_result.get("std", 0.0))
        prediction_covariance = np.eye(len(time_values)) * fallback_sigma**2
        prediction_1sigma = np.full(len(time_values), fallback_sigma)

    observation_times = (
        session_fit_result.get("fit_observations", {}).get("time")
        if isinstance(session_fit_result.get("fit_observations"), dict)
        else None
    )
    if observation_times is not None:
        observation_times = np.asarray(observation_times, dtype=float)
        observation_times = observation_times[np.isfinite(observation_times)]
    if observation_times is None or observation_times.size == 0:
        is_extrapolated = np.zeros(len(time_values), dtype=bool)
    else:
        is_extrapolated = (
            (time_values < np.min(observation_times))
            | (time_values > np.max(observation_times))
        )

    result = {
        "prediction": prediction,
        "prediction_covariance": prediction_covariance,
        "prediction_1sigma": prediction_1sigma,
        "is_extrapolated": is_extrapolated,
    }
    if scalar_input:
        result["prediction"] = float(result["prediction"][0])
        result["prediction_1sigma"] = float(result["prediction_1sigma"][0])
        result["is_extrapolated"] = bool(result["is_extrapolated"][0])
    return result

def propagate_session_corrected_ratio_covariance(
    measured_ratios,
    time_values,
    session_fit_result,
    measured_ratio_1sigma=None,
    measured_ratio_covariance=None,
    covariance_key="fit_covariance",
):
    """Propagate measured-ratio and session-fit covariance through correction.

    For a multiplicative correction ``z_i = r_i C(t_i)``, independent measured
    ratio covariance contributes ``diag(C) Sigma_r diag(C)``. The shared
    session correction contributes ``outer(r, r) * Sigma_C`` and therefore
    creates covariance between unknowns corrected by the same session fit.
    """
    measured_ratios = np.asarray(measured_ratios, dtype=float)
    time_values = np.asarray(time_values, dtype=float)
    if measured_ratios.shape != time_values.shape:
        raise ValueError("measured_ratios and time_values must have the same shape.")

    fit_evaluation = evaluate_session_fractionation_fit(
        session_fit_result,
        time_values,
        covariance_key=covariance_key,
    )
    correction_factors = np.asarray(fit_evaluation["prediction"], dtype=float)
    correction_covariance = np.asarray(fit_evaluation["prediction_covariance"], dtype=float)
    covariance_result = propagate_multiplicative_correction_covariance(
        measured_ratios,
        correction_factors,
        measured_1sigma=measured_ratio_1sigma,
        measured_covariance=measured_ratio_covariance,
        correction_covariance=correction_covariance,
    )

    return {
        "corrected_ratios": covariance_result["corrected_values"],
        "covariance": covariance_result["covariance"],
        "covariance_measurement": covariance_result["covariance_measurement"],
        "covariance_session": covariance_result["covariance_correction"],
        "correction_factors": covariance_result["correction_factors"],
        "correction_covariance": covariance_result["correction_covariance"],
        "correction_1sigma": np.asarray(fit_evaluation["prediction_1sigma"], dtype=float),
        "is_extrapolated": np.asarray(fit_evaluation["is_extrapolated"], dtype=bool),
    }

def propagate_product_covariance(x_values, y_values, x_covariance, y_covariance, xy_covariance=None, scale=1.0):
    """Propagate covariance through ``z = scale * x * y`` for vector products."""
    x_values = np.asarray(x_values, dtype=float)
    y_values = np.asarray(y_values, dtype=float)
    x_covariance = np.asarray(x_covariance, dtype=float)
    y_covariance = np.asarray(y_covariance, dtype=float)
    if x_values.shape != y_values.shape:
        raise ValueError("x_values and y_values must have the same shape.")
    expected_shape = (x_values.size, x_values.size)
    if x_covariance.shape != expected_shape or y_covariance.shape != expected_shape:
        raise ValueError("Input covariance matrices have incompatible shape.")
    if xy_covariance is None:
        xy_covariance = np.zeros(expected_shape)
    else:
        xy_covariance = np.asarray(xy_covariance, dtype=float)
        if xy_covariance.shape != expected_shape:
            raise ValueError("xy_covariance has incompatible shape.")

    jacobian_x = np.diag(scale * y_values)
    jacobian_y = np.diag(scale * x_values)
    covariance = (
        jacobian_x @ x_covariance @ jacobian_x.T
        + jacobian_y @ y_covariance @ jacobian_y.T
        + jacobian_x @ xy_covariance @ jacobian_y.T
        + jacobian_y @ xy_covariance.T @ jacobian_x.T
    )
    return {
        "values": scale * x_values * y_values,
        "covariance": covariance,
        "jacobian_x": jacobian_x,
        "jacobian_y": jacobian_y,
        "xy_covariance": xy_covariance,
    }

def propagate_reciprocal_covariance(values, covariance):
    """Propagate covariance through ``z = 1 / x``."""
    values = np.asarray(values, dtype=float)
    covariance = np.asarray(covariance, dtype=float)
    expected_shape = (values.size, values.size)
    if covariance.shape != expected_shape:
        raise ValueError("covariance has incompatible shape.")

    jacobian = np.diag(-1 / values**2)
    return {
        "values": 1 / values,
        "covariance": jacobian @ covariance @ jacobian.T,
        "jacobian": jacobian,
    }

def date_derivatives(ratios, system):
    """Return d(age Ma)/d(ratio) for supported U-Pb and Th-Pb systems."""
    ratios = np.asarray(ratios, dtype=float)
    if system == "207Pb/235U":
        return 1 / (lambda_235 * (1 + ratios)) / 1e6
    if system == "206Pb/238U":
        return 1 / (lambda_238 * (1 + ratios)) / 1e6
    if system == "208Pb/232Th":
        return 1 / (lambda_232 * (1 + ratios)) / 1e6
    if system == "207Pb/206Pb":
        derivatives = []
        for ratio in ratios:
            age_years = float(calc_76_date(float(ratio))) * 1e6
            exp_235 = np.exp(lambda_235 * age_years)
            exp_238 = np.exp(lambda_238 * age_years)
            denominator = common_U238_U235 * (exp_238 - 1) ** 2
            d_ratio_d_age = (
                lambda_235 * exp_235 * (exp_238 - 1)
                - lambda_238 * exp_238 * (exp_235 - 1)
            ) / denominator
            derivatives.append(1 / d_ratio_d_age / 1e6)
        return np.asarray(derivatives, dtype=float)
    raise ValueError(f"Unsupported date system: {system}")

def propagate_date_covariance(ratios, ratio_covariance, system):
    """Propagate ratio covariance to U-Pb date covariance using first derivatives."""
    ratios = np.asarray(ratios, dtype=float)
    ratio_covariance = np.asarray(ratio_covariance, dtype=float)
    expected_shape = (ratios.size, ratios.size)
    if ratio_covariance.shape != expected_shape:
        raise ValueError("ratio_covariance has incompatible shape.")

    derivatives = date_derivatives(ratios, system)
    jacobian = np.diag(derivatives)
    return {
        "covariance": jacobian @ ratio_covariance @ jacobian.T,
        "derivatives": derivatives,
        "jacobian": jacobian,
    }

def propagate_discordance_covariance(age_68, age_76, covariance_68, covariance_76, covariance_68_76=None):
    """Propagate covariance through ``D = 100 * (1 - age_68 / age_76)``."""
    age_68 = np.asarray(age_68, dtype=float)
    age_76 = np.asarray(age_76, dtype=float)
    covariance_68 = np.asarray(covariance_68, dtype=float)
    covariance_76 = np.asarray(covariance_76, dtype=float)
    if age_68.shape != age_76.shape:
        raise ValueError("age_68 and age_76 must have the same shape.")
    expected_shape = (age_68.size, age_68.size)
    if covariance_68.shape != expected_shape or covariance_76.shape != expected_shape:
        raise ValueError("Input covariance matrices have incompatible shape.")
    if covariance_68_76 is None:
        covariance_68_76 = np.zeros(expected_shape)
    else:
        covariance_68_76 = np.asarray(covariance_68_76, dtype=float)
        if covariance_68_76.shape != expected_shape:
            raise ValueError("covariance_68_76 has incompatible shape.")

    jacobian_68 = np.diag(-100 / age_76)
    jacobian_76 = np.diag(100 * age_68 / age_76**2)
    covariance = (
        jacobian_68 @ covariance_68 @ jacobian_68.T
        + jacobian_76 @ covariance_76 @ jacobian_76.T
        + jacobian_68 @ covariance_68_76 @ jacobian_76.T
        + jacobian_76 @ covariance_68_76.T @ jacobian_68.T
    )
    return {
        "values": 100 * (1 - age_68 / age_76),
        "covariance": covariance,
        "jacobian_68": jacobian_68,
        "jacobian_76": jacobian_76,
        "covariance_68_76": covariance_68_76,
    }

def fit_weighted_polynomial_with_covariance(x_values, y_values, standard_deviations, degree):
    """Fit a weighted polynomial and return coefficient/prediction covariance.

    The coefficient order matches ``numpy.polyfit``: highest power first.
    The default covariance treats the supplied standard deviations as known
    one-sigma uncertainties. The overdispersion-scaled covariance multiplies
    that matrix by at least the reduced chi-square, which is a simple
    diagnostic rather than a substitute for a full excess-scatter model.
    """
    x_values = np.asarray(x_values, dtype=float)
    y_values = np.asarray(y_values, dtype=float)
    standard_deviations = np.asarray(standard_deviations, dtype=float)
    finite = np.isfinite(x_values) & np.isfinite(y_values)
    if not np.any(finite):
        raise ValueError("At least one finite observation is required.")

    x_values = x_values[finite]
    y_values = y_values[finite]
    standard_deviations = standard_deviations[finite]
    weights = inverse_standard_deviation_weights(standard_deviations)

    design = np.vander(x_values, N=degree + 1)
    weighted_design = design * weights[:, np.newaxis]
    weighted_y = y_values * weights
    coefficients = np.linalg.lstsq(weighted_design, weighted_y, rcond=None)[0]

    normal_matrix = weighted_design.T @ weighted_design
    covariance_unscaled = np.linalg.pinv(normal_matrix)

    residuals = y_values - design @ coefficients
    weighted_residuals = residuals * weights
    degrees_of_freedom = max(len(y_values) - (degree + 1), 1)
    reduced_chi_square = np.sum(weighted_residuals**2) / degrees_of_freedom
    covariance_overdispersion_scaled = covariance_unscaled * max(reduced_chi_square, 1.0)
    residual_standard_deviation = np.sqrt(np.sum(residuals**2) / degrees_of_freedom)

    return {
        "coefficients": coefficients,
        "covariance": covariance_unscaled,
        "covariance_unscaled": covariance_unscaled,
        "covariance_overdispersion_scaled": covariance_overdispersion_scaled,
        "residuals": residuals,
        "residual_standard_deviation": residual_standard_deviation,
        "reduced_chi_square": reduced_chi_square,
        "degrees_of_freedom": degrees_of_freedom,
        "weights": weights,
        "x": x_values,
        "y": y_values,
        "standard_deviations": standard_deviations,
    }


def systematic_date_components(age_ma, system, settings: UncertaintySettings):
    """Return one-sigma decay-constant and U-isotope date components.

    Direct daughter/parent dates scale inversely with their decay constant.
    The 207Pb/206Pb date depends on both U decay constants and 238U/235U;
    centered numerical derivatives keep that implicit relation readable and
    consistent with :func:`UPbRPy.age.calc_76_date`.
    """
    age = np.asarray(age_ma, dtype=float)
    system = str(system)
    decay = np.zeros_like(age)
    uranium = np.zeros_like(age)
    if system == "206Pb/238U":
        decay = np.abs(age * settings.lambda_238_1s / lambda_238)
    elif system == "207Pb/235U":
        decay = np.abs(age * settings.lambda_235_1s / lambda_235)
        ratio = np.expm1(lambda_235 * age * 1e6)
        uranium = np.abs(
            ratio
            / (lambda_235 * (1 + ratio))
            / 1e6
            * settings.u238_u235_1s
            / settings.u238_u235
        )
    elif system == "208Pb/232Th":
        decay = np.abs(age * settings.lambda_232_1s / lambda_232)
    elif system == "207Pb/206Pb":
        age_years = age * 1e6
        from .age import radiogenic_76_ratio_with_constants

        base_ratio = radiogenic_76_ratio_with_constants(
            age_years,
            lambda235=lambda_235,
            lambda238=lambda_238,
            u238_u235=settings.u238_u235,
        )
        ratio_slope = np.asarray(radiogenic_76_derivative(age_years), dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            d_ratio_l235 = (
                radiogenic_76_ratio_with_constants(
                    age_years,
                    lambda235=lambda_235 + settings.lambda_235_1s,
                    lambda238=lambda_238,
                    u238_u235=settings.u238_u235,
                )
                - base_ratio
            )
            d_ratio_l238 = (
                radiogenic_76_ratio_with_constants(
                    age_years,
                    lambda235=lambda_235,
                    lambda238=lambda_238 + settings.lambda_238_1s,
                    u238_u235=settings.u238_u235,
                )
                - base_ratio
            )
            decay = np.sqrt(d_ratio_l235**2 + d_ratio_l238**2) / np.abs(ratio_slope) / 1e6
            d_ratio_u = (
                radiogenic_76_ratio_with_constants(
                    age_years,
                    lambda235=lambda_235,
                    lambda238=lambda_238,
                    u238_u235=settings.u238_u235 + settings.u238_u235_1s,
                )
                - base_ratio
            )
            uranium = np.abs(d_ratio_u / ratio_slope) / 1e6
    return {"decay_1s": decay, "u_ratio_1s": uranium}


def relative_ratio_date_component(age_ma, system, relative_ratio_1s):
    """Map a relative one-sigma ratio uncertainty into date units.

    Long-term reproducibility is observed in calibrated isotope-ratio space.
    It must therefore be propagated through the local ratio--date derivative,
    rather than multiplied directly by the numerical date.  The latter is a
    tempting shortcut but is not equivalent for the exponential U--Pb and
    Th--Pb growth equations.
    """
    age = np.asarray(age_ma, dtype=float)
    relative = float(relative_ratio_1s)
    if relative == 0:
        return np.zeros_like(age)
    age_years = age * 1e6
    if system == "206Pb/238U":
        ratio = np.expm1(lambda_238 * age_years)
        slope = lambda_238 * np.exp(lambda_238 * age_years)
    elif system == "207Pb/235U":
        ratio = np.expm1(lambda_235 * age_years)
        slope = lambda_235 * np.exp(lambda_235 * age_years)
    elif system == "208Pb/232Th":
        ratio = np.expm1(lambda_232 * age_years)
        slope = lambda_232 * np.exp(lambda_232 * age_years)
    elif system == "207Pb/206Pb":
        from .age import radiogenic_76_ratio_with_constants

        ratio = radiogenic_76_ratio_with_constants(
            age_years,
            lambda235=lambda_235,
            lambda238=lambda_238,
            u238_u235=common_U238_U235,
        )
        slope = np.asarray(radiogenic_76_derivative(age_years), dtype=float)
    else:
        raise ValueError(f"Unsupported isotope system {system!r}")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.abs(ratio * relative / slope) / 1e6


def common_corrected_systematic_components(frame, age_column, settings):
    """Return physical-constant date components for 207/208 corrections.

    Conventional common-Pb equations couple more than one decay system.
    Centered perturbations through the same implicit age solvers preserve that
    coupling and avoid assigning a 207- or 208-corrected date to a fictitious
    single daughter/parent system.
    """
    import pandas as pd

    count = len(frame)

    def values(candidates):
        """Return the first available numeric result column."""
        for candidate in candidates:
            if candidate in frame:
                return pd.to_numeric(frame[candidate], errors="coerce").to_numpy(
                    dtype=float
                )
        return np.full(count, np.nan)

    ratio_68 = values(("206Pb/238U_fractionation_corrected", "206Pb/238U"))
    ages = pd.to_numeric(frame[age_column], errors="coerce").to_numpy(dtype=float)
    age_years = ages * 1e6
    if age_column == "207Pb_corrected_age":
        ratio_other = values(
            ("207Pb/206Pb_fractionation_corrected", "207Pb/206Pb")
        )
        common = values(("207Pb_correction_common_207Pb/206Pb",))
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            exp238 = np.exp(lambda_238 * age_years)
            exp235 = np.exp(lambda_235 * age_years)
            radiogenic_68 = exp238 - 1
            d_residual_d_age_ma = (
                lambda_235 * 1e6 * exp235
                - settings.u238_u235
                * common
                * lambda_238
                * 1e6
                * exp238
            )
            d_residual_d_lambda238 = (
                -settings.u238_u235 * common * age_years * exp238
            )
            d_residual_d_lambda235 = age_years * exp235
            d_residual_d_uranium = (
                common * (ratio_68 - radiogenic_68)
                - ratio_other * ratio_68
            )
            l238_effect = np.abs(
                d_residual_d_lambda238
                * settings.lambda_238_1s
                / d_residual_d_age_ma
            )
            l235_effect = np.abs(
                d_residual_d_lambda235
                * settings.lambda_235_1s
                / d_residual_d_age_ma
            )
            decay = np.hypot(l238_effect, l235_effect)
            uranium = np.abs(
                d_residual_d_uranium
                * settings.u238_u235_1s
                / d_residual_d_age_ma
            )
    else:
        ratio_other = values(
            ("208Pb/232Th_fractionation_corrected", "208Pb/232Th")
        )
        common = values(("208Pb_correction_common_206Pb/208Pb",))
        th_u = values(("Th/U",))
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            exp238 = np.exp(lambda_238 * age_years)
            exp232 = np.exp(lambda_232 * age_years)
            d_residual_d_age_ma = (
                th_u * common * lambda_232 * 1e6 * exp232
                - lambda_238 * 1e6 * exp238
            )
            d_residual_d_lambda238 = -age_years * exp238
            d_residual_d_lambda232 = th_u * common * age_years * exp232
            l238_effect = np.abs(
                d_residual_d_lambda238
                * settings.lambda_238_1s
                / d_residual_d_age_ma
            )
            l232_effect = np.abs(
                d_residual_d_lambda232
                * settings.lambda_232_1s
                / d_residual_d_age_ma
            )
            decay = np.hypot(l238_effect, l232_effect)
        uranium = np.zeros(count)
    return {"decay_1s": decay, "u_ratio_1s": uranium}


def append_systematic_date_uncertainties(
    frame,
    settings: UncertaintySettings,
    *,
    long_term_relative_1s: float | None = None,
):
    """Append separate and combined systematic two-sigma date branches.

    Existing ``*_2e_no_calibration`` and ``*_2e_with_calibration`` columns are
    retained. New suffixes identify exactly which systematic terms were added.
    The long-term term is applied as relative uncertainty in the calibrated
    isotope ratio and mapped to date units with the local age derivative.
    """
    import pandas as pd

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    result = frame.copy()
    long_term = (
        settings.long_term_reproducibility_relative_1s
        if long_term_relative_1s is None
        else float(long_term_relative_1s)
    )
    systems = ("206Pb/238U", "207Pb/235U", "207Pb/206Pb", "208Pb/232Th")
    for age_column in [column for column in result if str(column).endswith("_age")]:
        system = next((name for name in systems if name in str(age_column)), None)
        if system is None and str(age_column) in {
            "207Pb_corrected_age",
            "208Pb_corrected_age",
        }:
            # Both model dates ultimately solve for a radiogenic 206Pb/238U
            # date; the 207 method additionally depends on 238U/235U.
            system = "206Pb/238U"
        if system is None:
            continue
        base_columns = {
            mode: age_column + f"_2e_{mode}"
            for mode in ("no_calibration", "with_calibration")
            if age_column + f"_2e_{mode}" in result
        }
        if not base_columns and age_column + "_2e" in result:
            base_columns["with_calibration"] = age_column + "_2e"
        if not base_columns:
            continue
        age = pd.to_numeric(result[age_column], errors="coerce").to_numpy(dtype=float)
        components = systematic_date_components(age, system, settings)
        if str(age_column) in {"207Pb_corrected_age", "208Pb_corrected_age"}:
            components = common_corrected_systematic_components(
                result,
                str(age_column),
                settings,
            )
        decay_1s = components["decay_1s"] if settings.propagate_decay_constants else 0.0
        uranium_1s = components["u_ratio_1s"] if settings.propagate_u238_u235 else 0.0
        reproducibility_1s = relative_ratio_date_component(
            age,
            system,
            long_term,
        )
        result[age_column + "_2e_decay_constants"] = 2 * np.asarray(components["decay_1s"])
        result[age_column + "_2e_u238_u235"] = 2 * np.asarray(components["u_ratio_1s"])
        result[age_column + "_2e_decay_and_u238_u235"] = 2 * np.sqrt(
            np.asarray(components["decay_1s"]) ** 2
            + np.asarray(components["u_ratio_1s"]) ** 2
        )
        result[age_column + "_2e_long_term_reproducibility"] = 2 * reproducibility_1s
        for mode, base_2e in base_columns.items():
            analytical_1s = (
                pd.to_numeric(result[base_2e], errors="coerce").to_numpy(dtype=float)
                / 2
            )
            combined = 2 * np.sqrt(
                analytical_1s**2
                + np.asarray(decay_1s) ** 2
                + np.asarray(uranium_1s) ** 2
                + reproducibility_1s**2
            )
            result[age_column + f"_2e_{mode}_selected_systematic"] = combined
            if mode == "with_calibration":
                result[age_column + "_2e_selected_systematic"] = combined
    result.attrs = dict(frame.attrs)
    result.attrs["uncertainty_settings"] = settings.to_dict()
    result.attrs["long_term_reproducibility_relative_1s_used"] = long_term
    return result

__all__ = [
    'UncertaintySettings',
    'CommonPbUncertaintySettings',
    'common_pb_uncertainty_preset',
    'CovarianceStore',
    'inverse_standard_deviation_weights',
    'polynomial_prediction_covariance',
    'linear_interpolation_weight_matrix',
    'build_downhole_fractionation_metadata',
    'evaluate_downhole_fractionation',
    'propagate_multiplicative_correction_covariance',
    'propagate_downhole_corrected_ratio_covariance',
    'relative_time_by_spot',
    'evaluate_session_fractionation_fit',
    'propagate_session_corrected_ratio_covariance',
    'propagate_product_covariance',
    'propagate_reciprocal_covariance',
    'date_derivatives',
    'propagate_date_covariance',
    'propagate_discordance_covariance',
    'fit_weighted_polynomial_with_covariance',
    'systematic_date_components',
    'relative_ratio_date_component',
    'common_corrected_systematic_components',
    'append_systematic_date_uncertainties',
]
