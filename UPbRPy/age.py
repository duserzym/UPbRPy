"""Lightweight isotope-ratio age conversions and uncertainty propagation.

This module intentionally has no plotting dependencies.  Numerical reduction
code can therefore use the age equations without importing Bokeh or
Matplotlib; :mod:`UPbRPy.isoplot` re-exports the same functions for backward
compatibility with existing notebooks.
"""

from __future__ import annotations

import numpy as np

from .constants import common_U238_U235, lambda_235, lambda_232, lambda_238


def calc_75_date(ratio):
    """Convert radiogenic ``207Pb/235U`` to an apparent age in Ma."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log1p(np.asarray(ratio, dtype=float)) / lambda_235 / 1e6


def calc_75_date_uncertainty(ratio, ratio_uncertainty):
    """Propagate an absolute 207Pb/235U ratio uncertainty to age (Ma)."""
    ratio = np.asarray(ratio, dtype=float)
    ratio_uncertainty = np.asarray(ratio_uncertainty, dtype=float)
    return np.abs(ratio_uncertainty) / (lambda_235 * (1 + ratio)) / 1e6


def calc_68_date(ratio):
    """Convert radiogenic ``206Pb/238U`` to an apparent age in Ma."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log1p(np.asarray(ratio, dtype=float)) / lambda_238 / 1e6


def calc_68_date_uncertainty(ratio, ratio_uncertainty):
    """Propagate an absolute 206Pb/238U ratio uncertainty to age (Ma)."""
    ratio = np.asarray(ratio, dtype=float)
    ratio_uncertainty = np.asarray(ratio_uncertainty, dtype=float)
    return np.abs(ratio_uncertainty) / (lambda_238 * (1 + ratio)) / 1e6


def calc_82_date(ratio, *, lambda232=lambda_232):
    """Return an apparent total-Pb ``208Pb/232Th`` date in Ma."""
    ratio = np.asarray(ratio, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log1p(ratio) / lambda232 / 1e6


def calc_82_date_uncertainty(
    ratio,
    ratio_uncertainty,
    *,
    lambda232=lambda_232,
):
    """Propagate an absolute 208Pb/232Th ratio uncertainty to date (Ma)."""
    ratio = np.asarray(ratio, dtype=float)
    ratio_uncertainty = np.asarray(ratio_uncertainty, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.abs(ratio_uncertainty) / (lambda232 * (1 + ratio)) / 1e6


def radiogenic_76_ratio_with_constants(
    age_years,
    *,
    lambda235=lambda_235,
    lambda238=lambda_238,
    u238_u235=common_U238_U235,
):
    """Evaluate radiogenic 207Pb/206Pb using explicit physical constants."""
    age_years = np.asarray(age_years, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        numerator = np.expm1(lambda235 * age_years)
        denominator = np.expm1(lambda238 * age_years)
        ratio = numerator / denominator / u238_u235
    limiting_ratio = lambda235 / lambda238 / u238_u235
    return np.where(np.abs(age_years) < 1.0, limiting_ratio, ratio)


def radiogenic_76_ratio(age_years):
    """Evaluate radiogenic 207Pb/206Pb at one or more ages in years."""
    return radiogenic_76_ratio_with_constants(age_years)


def radiogenic_76_derivative(age_years):
    """Return d(207Pb/206Pb)/dt for ages expressed in years."""
    age_years = np.asarray(age_years, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        exp_235 = np.exp(lambda_235 * age_years)
        exp_238 = np.exp(lambda_238 * age_years)
        denominator = common_U238_U235 * (exp_238 - 1) ** 2
        numerator = (
            lambda_235 * exp_235 * (exp_238 - 1)
            - lambda_238 * exp_238 * (exp_235 - 1)
        )
        derivative = numerator / denominator

    # Use a stable centered derivative for the analytic limit at zero.
    near_zero = np.abs(age_years) < 1.0
    if np.any(near_zero):
        step = 1e3
        zero_derivative = (
            radiogenic_76_ratio(step) - radiogenic_76_ratio(-step)
        ) / (2 * step)
        derivative = np.where(near_zero, zero_derivative, derivative)
    return derivative


def calc_76_date(ratio, x0=1e6, tol=1e-8):
    """Convert radiogenic ``207Pb/206Pb`` to age in Ma.

    A monotonic lookup supplies a vectorized initial estimate that is refined
    with five Newton iterations. ``x0`` and ``tol`` are retained for API
    compatibility with the former scalar solver.
    """
    del x0, tol
    measured = np.asarray(ratio, dtype=float)
    scalar_input = measured.ndim == 0
    flat = measured.reshape(-1)

    grid_age = np.linspace(-1.0e9, 10.0e9, 8193)
    grid_ratio = radiogenic_76_ratio(grid_age)
    valid = (
        np.isfinite(flat)
        & (flat >= grid_ratio[0])
        & (flat <= grid_ratio[-1])
    )
    age = np.full(flat.shape, np.nan, dtype=float)
    age[valid] = np.interp(flat[valid], grid_ratio, grid_age)

    for _iteration in range(5):
        current = age[valid]
        derivative = radiogenic_76_derivative(current)
        with np.errstate(divide="ignore", invalid="ignore"):
            update = (
                radiogenic_76_ratio(current) - flat[valid]
            ) / derivative
        age[valid] = np.clip(
            current - update,
            grid_age[0],
            grid_age[-1],
        )

    result = age.reshape(measured.shape) / 1e6
    return float(result) if scalar_input else result


def calc_76_date_uncertainty(ratio, ratio_uncertainty):
    """Propagate an absolute 207Pb/206Pb ratio uncertainty to age (Ma)."""
    ratio = np.asarray(ratio, dtype=float)
    ratio_uncertainty = np.asarray(ratio_uncertainty, dtype=float)
    scalar_input = ratio.ndim == 0 and ratio_uncertainty.ndim == 0
    age_years = np.asarray(calc_76_date(ratio), dtype=float) * 1e6
    derivative = radiogenic_76_derivative(age_years)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.abs(ratio_uncertainty / derivative) / 1e6
    return float(result) if scalar_input else result


def propagate_reciprocal_uncertainty(ratio, ratio_uncertainty):
    """Propagate absolute uncertainty through ``x = 1 / ratio``."""
    ratio = np.asarray(ratio, dtype=float)
    ratio_uncertainty = np.asarray(ratio_uncertainty, dtype=float)
    return np.abs(ratio_uncertainty) / ratio**2


__all__ = [
    "calc_75_date",
    "calc_75_date_uncertainty",
    "calc_68_date",
    "calc_68_date_uncertainty",
    "calc_82_date",
    "calc_82_date_uncertainty",
    "radiogenic_76_ratio",
    "radiogenic_76_ratio_with_constants",
    "radiogenic_76_derivative",
    "calc_76_date",
    "calc_76_date_uncertainty",
    "propagate_reciprocal_uncertainty",
]
