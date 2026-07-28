"""Covariance models and uncertainty-propagation utilities."""

from __future__ import annotations

import numpy as np

from .constants import common_U238_U235, lambda_235, lambda_232, lambda_238
from .isoplot import calc_76_date


class CovarianceStore(dict):
    """Dictionary-like covariance metadata that is safe in ``DataFrame.attrs``.

    Pandas copies and compares ``attrs`` during slicing and concatenation. A
    regular dictionary containing NumPy arrays makes those operations both
    expensive and ambiguous because array equality does not produce one bool.
    Covariance metadata is immutable after construction, so retaining the same
    store across DataFrame copies is intentional.
    """

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        return self is other


def inverse_standard_deviation_weights(standard_deviations):
    """Return np.polyfit weights, which are 1/sigma rather than 1/sigma**2."""
    standard_deviations = np.asarray(standard_deviations, dtype=float)
    valid = np.isfinite(standard_deviations) & (standard_deviations > 0)
    fallback = np.nanmedian(standard_deviations[valid]) if np.any(valid) else 1.0
    safe_standard_deviations = np.where(valid, standard_deviations, fallback)
    return 1 / safe_standard_deviations

def polynomial_prediction_covariance(coefficient_covariance, x_values):
    """Covariance of polynomial predictions for high-to-low coefficients."""
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

__all__ = [
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
]
