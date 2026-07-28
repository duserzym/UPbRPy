"""Reusable calibration-curve models for fractionation corrections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.interpolate import UnivariateSpline, interp1d
from scipy.optimize import curve_fit


ArrayFunction = Callable[[np.ndarray], np.ndarray]


@dataclass
class CalibrationModel:
    """A fitted one-dimensional correction model.

    Instances are callable and retain lightweight fit diagnostics. They do not
    calculate or store coefficient or prediction covariance.
    """

    method: str
    predictor: ArrayFunction
    parameters: np.ndarray
    x: np.ndarray
    y: np.ndarray
    residuals: np.ndarray
    residual_standard_deviation: float
    degrees_of_freedom: int
    settings: dict

    def __call__(self, values):
        """Evaluate the fitted correction model at one or more coordinates."""
        values = np.asarray(values, dtype=float)
        return np.asarray(self.predictor(values), dtype=float)

    @property
    def x_range(self):
        """Return the minimum and maximum fitted calibration coordinates."""
        return float(np.nanmin(self.x)), float(np.nanmax(self.x))


def _prepare_xy(x, y, weights=None):
    """Validate, filter, and sort paired one-dimensional calibration observations."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.shape != y.shape:
        raise ValueError("x and y must have the same shape.")
    finite = np.isfinite(x) & np.isfinite(y)
    if weights is not None:
        weights = np.asarray(weights, dtype=float)
        if weights.shape != x.shape:
            raise ValueError("weights must match x and y.")
        finite &= np.isfinite(weights) & (weights > 0)
        weights = weights[finite]
    x = x[finite]
    y = y[finite]
    if x.size < 1:
        raise ValueError("At least one finite calibration observation is required.")
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    if weights is not None:
        weights = weights[order]
    return x, y, weights


def _exponential(time, asymptote, amplitude, rate):
    """Evaluate an asymptotic exponential-decay curve at calibration coordinates."""
    return asymptote + amplitude * np.exp(-rate * time)


def fit_calibration_model(
    x,
    y,
    method="polynomial",
    *,
    weights=None,
    degree=2,
    smoothing=None,
    p0=None,
    bounds=(-np.inf, np.inf),
):
    """Fit a built-in or user-supplied one-dimensional calibration function.

    Parameters
    ----------
    x, y : array-like
        Calibration coordinates and correction factors.
    method : str or callable
        Built-ins are ``'interpolate'``, ``'mean'``, ``'linear'``,
        ``'polynomial'``, ``'orthogonal_polynomial'``, ``'exponential'``, and
        ``'spline'``. A callable is treated as ``f(x, *parameters)`` and fitted
        with :func:`scipy.optimize.curve_fit`; a one-argument callable is used
        directly as an already-calibrated predictor.
    weights : array-like, optional
        Relative least-squares weights. These affect fitting only; UPbRPy does
        not propagate coefficient covariance.
    degree : int, optional
        Polynomial degree for regular or orthogonal polynomial models.
    smoothing : float, optional
        Smoothing factor passed to ``UnivariateSpline``. ``None`` uses SciPy's
        default.
    p0, bounds : optional
        Initial parameters and bounds for exponential or custom parametric
        functions.
    """
    x, y, weights = _prepare_xy(x, y, weights)
    settings = {"degree": int(degree), "smoothing": smoothing}

    if callable(method):
        method_name = getattr(method, "__name__", "custom")
        if p0 is None:
            try:
                direct = np.asarray(method(x), dtype=float)
            except TypeError:
                direct = None
            if direct is not None and direct.shape == y.shape:
                predictor = method
                parameters = np.array([], dtype=float)
                parameter_count = 0
            else:
                sigma = None if weights is None else 1 / weights
                parameters, _ = curve_fit(
                    method, x, y, p0=p0, bounds=bounds, sigma=sigma,
                    absolute_sigma=False, maxfev=100000,
                )
                predictor = lambda values, f=method, p=parameters: f(values, *p)
                parameter_count = len(parameters)
        else:
            sigma = None if weights is None else 1 / weights
            parameters, _ = curve_fit(
                method, x, y, p0=p0, bounds=bounds, sigma=sigma,
                absolute_sigma=False, maxfev=100000,
            )
            predictor = lambda values, f=method, p=parameters: f(values, *p)
            parameter_count = len(parameters)
    else:
        method_name = str(method).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "average": "mean",
            "constant": "mean",
            "linear_interpolation": "interpolate",
            "pointwise_mean": "interpolate",
            "poly": "polynomial",
            "orthogonal": "orthogonal_polynomial",
            "cubic_spline": "spline",
            "exp": "exponential",
        }
        method_name = aliases.get(method_name, method_name)

        if method_name == "mean":
            parameters = np.array([np.average(y, weights=weights)], dtype=float)
            predictor = lambda values, value=parameters[0]: np.full_like(
                np.asarray(values, dtype=float), value, dtype=float
            )
            parameter_count = 1
        elif method_name == "interpolate":
            if x.size < 2:
                raise ValueError("Interpolation requires at least two observations.")
            interpolator = interp1d(x, y, kind="linear", fill_value="extrapolate")
            predictor = interpolator
            parameters = y.copy()
            parameter_count = len(y)
        elif method_name in {"linear", "polynomial"}:
            fit_degree = 1 if method_name == "linear" else int(degree)
            if fit_degree < 0 or x.size <= fit_degree:
                raise ValueError("Polynomial degree must be nonnegative and smaller than the observation count.")
            parameters = np.polyfit(x, y, fit_degree, w=weights)
            predictor = lambda values, p=parameters: np.polyval(p, values)
            parameter_count = len(parameters)
            settings["degree"] = fit_degree
        elif method_name == "orthogonal_polynomial":
            fit_degree = int(degree)
            if fit_degree < 0 or x.size <= fit_degree:
                raise ValueError("Polynomial degree must be nonnegative and smaller than the observation count.")
            orthogonal_fit = np.polynomial.Legendre.fit(x, y, fit_degree, w=weights)
            parameters = orthogonal_fit.coef.copy()
            predictor = orthogonal_fit
            parameter_count = len(parameters)
            settings["degree"] = fit_degree
            settings["domain"] = orthogonal_fit.domain.tolist()
        elif method_name == "exponential":
            x_origin = float(x[0])
            shifted_x = x - x_origin
            if p0 is None:
                span = max(float(np.ptp(shifted_x)), np.finfo(float).eps)
                p0 = (float(y[-1]), float(y[0] - y[-1]), 1 / span)
            if bounds == (-np.inf, np.inf):
                bounds = ((-np.inf, -np.inf, 0.0), (np.inf, np.inf, np.inf))
            sigma = None if weights is None else 1 / weights
            parameters, _ = curve_fit(
                _exponential, shifted_x, y, p0=p0, bounds=bounds, sigma=sigma,
                absolute_sigma=False, maxfev=100000,
            )
            predictor = lambda values, p=parameters, origin=x_origin: _exponential(
                np.asarray(values, dtype=float) - origin, *p
            )
            parameter_count = len(parameters)
            settings["x_origin"] = x_origin
        elif method_name == "spline":
            if x.size < 2:
                raise ValueError("Spline fitting requires at least two observations.")
            spline = UnivariateSpline(
                x, y, w=weights, k=min(3, x.size - 1), s=smoothing
            )
            predictor = spline
            parameters = spline.get_coeffs().copy()
            parameter_count = len(parameters)
        else:
            raise ValueError(
                "Unknown calibration method. Choose mean, interpolate, linear, "
                "polynomial, orthogonal_polynomial, exponential, spline, or pass a callable."
            )

    residuals = y - np.asarray(predictor(x), dtype=float)
    degrees_of_freedom = max(x.size - parameter_count, 1)
    residual_standard_deviation = float(
        np.sqrt(np.sum(residuals**2) / degrees_of_freedom)
    )
    return CalibrationModel(
        method=method_name,
        predictor=predictor,
        parameters=np.asarray(parameters, dtype=float),
        x=x,
        y=y,
        residuals=residuals,
        residual_standard_deviation=residual_standard_deviation,
        degrees_of_freedom=degrees_of_freedom,
        settings=settings,
    )


def evaluate_correction_result(result, values):
    """Evaluate either a modern or legacy correction-result mapping.

    Modern mappings expose ``predictor``; older downhole results used
    ``interpolation`` or polynomial coefficients stored in ``fit``.
    """
    if "predictor" in result:
        return np.asarray(result["predictor"](values), dtype=float)
    if "interpolation" in result:
        return np.asarray(result["interpolation"](values), dtype=float)
    return np.asarray(np.polyval(result["fit"], values), dtype=float)


__all__ = [
    "CalibrationModel",
    "fit_calibration_model",
    "evaluate_correction_result",
]
