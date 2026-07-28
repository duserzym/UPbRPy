"""U-Pb isochron and discordia line fits.

This module keeps the calculation path independent from the plotting and GUI
code.  Ages are reported in Ma, ratio uncertainties are expected as one-sigma
absolute uncertainties, and line equations use ``y = intercept + slope * x``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq, minimize_scalar
from scipy.stats import chi2

from .constants import (
    common_U238_U235,
    lambda_235,
    lambda_232,
    lambda_238,
    old_common_U238_U235,
)

U238_U235 = common_U238_U235
LAMBDA_235 = lambda_235
LAMBDA_238 = lambda_238
LAMBDA_232 = lambda_232
EARTH_AGE_MA = 4567.0

# The conventional 208Pb correction becomes poorly conditioned as a sample's
# radiogenic 208Pb/206Pb approaches the common-Pb composition. Chew et al.
# (2011) advise against this correction for high-Th/U targets and use Th/U > 5
# as a practical warning range.
DEFAULT_208_MAX_TH_U = 5.0


@dataclass
class CommonPbAgeResult:
    """Result of a single-analysis 207Pb- or 208Pb-corrected age.

    ``f206`` is the inferred fraction of measured 206Pb that is common Pb.
    ``valid`` is a conservative data-quality flag; a finite age is retained
    even when the flag is false so anomalous measurements remain visible.
    """

    age_ma: float
    sigma_ma: float
    f206: float
    common_ratio: float
    iterations: int
    converged: bool
    valid: bool
    warning: str = ""

    @property
    def two_sigma_ma(self) -> float:
        """Return the absolute two-sigma age uncertainty in Ma."""
        return 2.0 * self.sigma_ma


def stacey_kramers_common_pb(
    age_ma,
    *,
    u238_u235: float = old_common_U238_U235,
):
    """Return the Stacey-Kramers (1975) two-stage common-Pb composition.

    Parameters
    ----------
    age_ma
        Model age in Ma. Scalars and array-like inputs are accepted.
    u238_u235
        Uranium isotope ratio used by the original model. The published and
        legacy Excel value, 137.88, is the default. This parameter is distinct
        from the modern package ratio used in U-Pb age equations.

    Returns
    -------
    dict
        Arrays named ``206Pb/204Pb``, ``207Pb/204Pb``, ``208Pb/204Pb``,
        ``207Pb/206Pb``, ``208Pb/206Pb``, and ``206Pb/208Pb``.

    Notes
    -----
    The first stage runs from 4.57 to 3.70 Ga and the second stage from
    3.70 Ga to the requested age. Ages outside 0--4570 Ma are extrapolated so
    slightly negative analytical ages can still be inspected.
    """
    age_years = np.asarray(age_ma, dtype=float) * 1e6
    stage_boundary = 3.7e9
    earth_age = 4.57e9
    older = age_years > stage_boundary

    ratio_64_first = 9.307 + 7.19 * (
        np.exp(LAMBDA_238 * earth_age) - np.exp(LAMBDA_238 * age_years)
    )
    ratio_74_first = 10.294 + 7.19 / u238_u235 * (
        np.exp(LAMBDA_235 * earth_age) - np.exp(LAMBDA_235 * age_years)
    )
    ratio_84_first = 29.487 + 33.21 * (
        np.exp(LAMBDA_232 * earth_age) - np.exp(LAMBDA_232 * age_years)
    )

    ratio_64_second = 11.152 + 9.74 * (
        np.exp(LAMBDA_238 * stage_boundary) - np.exp(LAMBDA_238 * age_years)
    )
    ratio_74_second = 12.998 + 9.74 / u238_u235 * (
        np.exp(LAMBDA_235 * stage_boundary) - np.exp(LAMBDA_235 * age_years)
    )
    ratio_84_second = 31.23 + 36.84 * (
        np.exp(LAMBDA_232 * stage_boundary) - np.exp(LAMBDA_232 * age_years)
    )

    ratio_64 = np.where(older, ratio_64_first, ratio_64_second)
    ratio_74 = np.where(older, ratio_74_first, ratio_74_second)
    ratio_84 = np.where(older, ratio_84_first, ratio_84_second)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio_76 = ratio_74 / ratio_64
        ratio_86 = ratio_84 / ratio_64
        ratio_68 = ratio_64 / ratio_84
    return {
        "206Pb/204Pb": ratio_64,
        "207Pb/204Pb": ratio_74,
        "208Pb/204Pb": ratio_84,
        "207Pb/206Pb": ratio_76,
        "208Pb/206Pb": ratio_86,
        "206Pb/208Pb": ratio_68,
    }


def solve_common_pb_age_equation(
    residual,
    derivative,
    *,
    initial_age_ma: float,
    age_bounds_ma: tuple[float, float],
    tolerance_ma: float,
    max_iterations: int,
) -> tuple[float, int, bool]:
    """Solve a scalar age equation, retaining the legacy Newton behaviour.

    Newton iteration reproduces the Excel VBA path. A bounded Brent fallback
    makes the public functions robust when the derivative is nearly singular.
    """
    age = float(initial_age_ma)
    lower, upper = map(float, age_bounds_ma)
    for iteration in range(1, max_iterations + 1):
        value = float(residual(age))
        slope = float(derivative(age))
        if not np.isfinite(value) or not np.isfinite(slope) or slope == 0:
            break
        next_age = age - value / slope
        if not np.isfinite(next_age):
            break
        if abs(next_age - age) <= tolerance_ma:
            return float(next_age), iteration, True
        age = next_age

    # Scan the allowed interval because a residual need not have opposite signs
    # exactly at its endpoints. Choose the root nearest the initial estimate.
    grid = np.linspace(lower, upper, 513)
    values = np.asarray([residual(point) for point in grid], dtype=float)
    candidates: list[tuple[float, float]] = []
    for left, right, f_left, f_right in zip(
        grid[:-1], grid[1:], values[:-1], values[1:]
    ):
        if not np.isfinite(f_left) or not np.isfinite(f_right):
            continue
        if f_left == 0:
            return float(left), max_iterations, True
        if f_left * f_right < 0:
            candidates.append((float(left), float(right)))
    if not candidates:
        return np.nan, max_iterations, False
    left, right = min(
        candidates,
        key=lambda bracket: abs(np.mean(bracket) - initial_age_ma),
    )
    try:
        root = brentq(residual, left, right, xtol=tolerance_ma)
    except ValueError:
        return np.nan, max_iterations, False
    return float(root), max_iterations, True


def age_207_corrected(
    total_pb206_u238: float,
    total_pb207_pb206: float,
    common_pb207_pb206: float,
    *,
    u238_u235: float = U238_U235,
    initial_age_ma: float = 1000.0,
    age_bounds_ma: tuple[float, float] = (-1000.0, EARTH_AGE_MA),
    tolerance_ma: float = 1e-7,
    max_iterations: int = 100,
    return_solver_info: bool = False,
):
    """Calculate a concordance-assuming 207Pb-corrected age in Ma.

    This is the semi-total-Pb/U, or conventional ``207 method``, implemented
    by the legacy Excel ``Age7corr`` VBA function. The inputs are calibrated
    *total* ratios. Common Pb is removed by solving the concordance equation,
    so this result cannot be used to test concordance or Pb loss.
    """
    ratio_68 = float(total_pb206_u238)
    ratio_76 = float(total_pb207_pb206)
    common_76 = float(common_pb207_pb206)
    l238_ma = LAMBDA_238 * 1e6
    l235_ma = LAMBDA_235 * 1e6

    if not all(np.isfinite([ratio_68, ratio_76, common_76])):
        result = (np.nan, 0, False)
        return result if return_solver_info else result[0]

    def residual(age_ma):
        """Return the 207-method equation misfit for a trial age."""
        radiogenic_68 = np.expm1(l238_ma * age_ma)
        radiogenic_75 = np.expm1(l235_ma * age_ma)
        return (
            u238_u235 * common_76 * (ratio_68 - radiogenic_68)
            - u238_u235 * ratio_76 * ratio_68
            + radiogenic_75
        )

    def derivative(age_ma):
        """Return the age derivative of the 207-method residual."""
        return (
            l235_ma * np.exp(l235_ma * age_ma)
            - u238_u235
            * common_76
            * l238_ma
            * np.exp(l238_ma * age_ma)
        )

    result = solve_common_pb_age_equation(
        residual,
        derivative,
        initial_age_ma=initial_age_ma,
        age_bounds_ma=age_bounds_ma,
        tolerance_ma=tolerance_ma,
        max_iterations=max_iterations,
    )
    return result if return_solver_info else result[0]


def age_207_corrected_uncertainty(
    age_ma: float,
    total_pb206_u238: float,
    total_pb206_u238_1s: float,
    total_pb207_pb206: float,
    total_pb207_pb206_1s: float,
    common_pb207_pb206: float,
    common_pb207_pb206_1s: float,
    *,
    u238_u235: float = U238_U235,
) -> float:
    """Propagate independent one-sigma inputs to a 207-corrected age.

    The implicit-derivative form is algebraically equivalent to the
    ``AgeEr7corr`` VBA routine. Ratio covariance is deliberately not included,
    matching the legacy data-reduction pipeline.
    """
    values = np.asarray(
        [
            age_ma,
            total_pb206_u238,
            total_pb206_u238_1s,
            total_pb207_pb206,
            total_pb207_pb206_1s,
            common_pb207_pb206,
            common_pb207_pb206_1s,
        ],
        dtype=float,
    )
    if not np.all(np.isfinite(values)):
        return np.nan

    l238_ma = LAMBDA_238 * 1e6
    l235_ma = LAMBDA_235 * 1e6
    radiogenic_68 = np.expm1(l238_ma * age_ma)
    d_age = (
        l235_ma * np.exp(l235_ma * age_ma)
        - u238_u235
        * common_pb207_pb206
        * l238_ma
        * np.exp(l238_ma * age_ma)
    )
    variance_residual = (
        (
            u238_u235
            * (common_pb207_pb206 - total_pb207_pb206)
            * total_pb206_u238_1s
        )
        ** 2
        + (
            u238_u235
            * total_pb206_u238
            * total_pb207_pb206_1s
        )
        ** 2
        + (
            u238_u235
            * (total_pb206_u238 - radiogenic_68)
            * common_pb207_pb206_1s
        )
        ** 2
    )
    if d_age == 0 or variance_residual < 0:
        return np.nan
    return float(np.sqrt(variance_residual) / abs(d_age))


def age_208_corrected(
    total_pb206_u238: float,
    total_pb208_th232: float,
    th232_u238: float,
    common_pb206_pb208: float,
    *,
    initial_age_ma: float = 1000.0,
    age_bounds_ma: tuple[float, float] = (-1000.0, EARTH_AGE_MA),
    tolerance_ma: float = 1e-7,
    max_iterations: int = 100,
    return_solver_info: bool = False,
):
    """Calculate a concordance-assuming 208Pb-corrected age in Ma.

    ``common_pb206_pb208`` is intentionally expressed as 206Pb/208Pb.
    The legacy workbook's cell label reverses this ratio, but its VBA algebra
    and formula call pass 206Pb/208Pb. The method also assumes that Th/U has
    remained closed since crystallisation.
    """
    ratio_68 = float(total_pb206_u238)
    ratio_82 = float(total_pb208_th232)
    th_u = float(th232_u238)
    common_68 = float(common_pb206_pb208)
    l238_ma = LAMBDA_238 * 1e6
    l232_ma = LAMBDA_232 * 1e6

    if not all(np.isfinite([ratio_68, ratio_82, th_u, common_68])):
        result = (np.nan, 0, False)
        return result if return_solver_info else result[0]

    def residual(age_ma):
        """Return the 208-method equation misfit for a trial age."""
        radiogenic_68 = np.expm1(l238_ma * age_ma)
        radiogenic_82 = np.expm1(l232_ma * age_ma)
        return (
            ratio_68
            - radiogenic_68
            - th_u * common_68 * (ratio_82 - radiogenic_82)
        )

    def derivative(age_ma):
        """Return the age derivative of the 208-method residual."""
        return (
            th_u * common_68 * l232_ma * np.exp(l232_ma * age_ma)
            - l238_ma * np.exp(l238_ma * age_ma)
        )

    result = solve_common_pb_age_equation(
        residual,
        derivative,
        initial_age_ma=initial_age_ma,
        age_bounds_ma=age_bounds_ma,
        tolerance_ma=tolerance_ma,
        max_iterations=max_iterations,
    )
    return result if return_solver_info else result[0]


def age_208_corrected_uncertainty(
    age_ma: float,
    total_pb206_u238: float,
    total_pb206_u238_1s: float,
    total_pb208_th232: float,
    total_pb208_th232_1s: float,
    th232_u238: float,
    th232_u238_1s: float,
    common_pb206_pb208: float,
    common_pb206_pb208_1s: float,
) -> float:
    """Propagate independent one-sigma inputs to a 208-corrected age.

    This implicit-derivative form is equivalent to the workbook's
    ``AgeEr8corr`` function and intentionally omits covariance terms.
    """
    values = np.asarray(
        [
            age_ma,
            total_pb206_u238,
            total_pb206_u238_1s,
            total_pb208_th232,
            total_pb208_th232_1s,
            th232_u238,
            th232_u238_1s,
            common_pb206_pb208,
            common_pb206_pb208_1s,
        ],
        dtype=float,
    )
    if not np.all(np.isfinite(values)):
        return np.nan

    l238_ma = LAMBDA_238 * 1e6
    l232_ma = LAMBDA_232 * 1e6
    radiogenic_82 = np.expm1(l232_ma * age_ma)
    excess_82 = total_pb208_th232 - radiogenic_82
    d_age = (
        th232_u238
        * common_pb206_pb208
        * l232_ma
        * np.exp(l232_ma * age_ma)
        - l238_ma * np.exp(l238_ma * age_ma)
    )
    variance_residual = (
        total_pb206_u238_1s**2
        + (th232_u238 * common_pb206_pb208 * total_pb208_th232_1s) ** 2
        + (common_pb206_pb208 * excess_82 * th232_u238_1s) ** 2
        + (th232_u238 * excess_82 * common_pb206_pb208_1s) ** 2
    )
    if d_age == 0 or variance_residual < 0:
        return np.nan
    return float(np.sqrt(variance_residual) / abs(d_age))


def calculate_common_pb_result(
    *,
    method: str,
    ratio_68: float,
    ratio_68_1s: float,
    ratio_other: float,
    ratio_other_1s: float,
    common_ratio: float | None,
    common_ratio_1s: float,
    th_u: float,
    th_u_1s: float,
    initial_age_ma: float,
    use_stacey_kramers: bool,
    stacey_kramers_u238_u235: float,
    u238_u235: float,
    tolerance_ma: float,
    max_iterations: int,
    age_bounds_ma: tuple[float, float],
    max_208_th_u: float,
) -> CommonPbAgeResult:
    """Calculate one conventional 207- or 208-corrected age result.

    Parameters
    ----------
    method
        ``"207"`` or ``"208"``.
    ratio_68, ratio_68_1s
        Total ``206Pb/238U`` and absolute one-sigma uncertainty.
    ratio_other, ratio_other_1s
        Total ``207Pb/206Pb`` for the 207 method or ``208Pb/232Th`` for the
        208 method, with its one-sigma uncertainty.
    common_ratio, common_ratio_1s
        Fixed common ``207Pb/206Pb`` or ``206Pb/208Pb`` composition and its
        one-sigma uncertainty. ``common_ratio`` is ignored when
        ``use_stacey_kramers`` is true.
    th_u, th_u_1s
        Elemental Th/U and uncertainty used by the 208 method.
    initial_age_ma, tolerance_ma, max_iterations, age_bounds_ma
        Numerical solver controls.
    use_stacey_kramers, stacey_kramers_u238_u235
        Enable iterative model composition and select its U isotope ratio.
    u238_u235
        U isotope ratio used in modern age equations.
    max_208_th_u
        Th/U threshold above which the 208 result is retained but flagged.

    Returns
    -------
    CommonPbAgeResult
        Age, uncertainty, common fraction, convergence diagnostics, and
        conservative validity state.
    """
    age_guess = float(initial_age_ma)
    common = float(common_ratio) if common_ratio is not None else np.nan
    converged = False
    solver_iterations = 0
    model_iterations = max_iterations if use_stacey_kramers else 1

    for model_iteration in range(1, model_iterations + 1):
        if use_stacey_kramers:
            composition = stacey_kramers_common_pb(
                age_guess,
                u238_u235=stacey_kramers_u238_u235,
            )
            ratio_name = "207Pb/206Pb" if method == "207" else "206Pb/208Pb"
            common = float(composition[ratio_name])

        if method == "207":
            age, solver_iterations, solved = age_207_corrected(
                ratio_68,
                ratio_other,
                common,
                u238_u235=u238_u235,
                initial_age_ma=age_guess,
                age_bounds_ma=age_bounds_ma,
                tolerance_ma=tolerance_ma,
                max_iterations=max_iterations,
                return_solver_info=True,
            )
        else:
            age, solver_iterations, solved = age_208_corrected(
                ratio_68,
                ratio_other,
                th_u,
                common,
                initial_age_ma=age_guess,
                age_bounds_ma=age_bounds_ma,
                tolerance_ma=tolerance_ma,
                max_iterations=max_iterations,
                return_solver_info=True,
            )
        if not solved or not np.isfinite(age):
            break
        if not use_stacey_kramers or abs(age - age_guess) <= tolerance_ma:
            converged = True
            age_guess = float(age)
            break
        age_guess = float(age)

    age = age_guess if converged else (float(age) if "age" in locals() else np.nan)
    if use_stacey_kramers and np.isfinite(age):
        composition = stacey_kramers_common_pb(
            age,
            u238_u235=stacey_kramers_u238_u235,
        )
        ratio_name = "207Pb/206Pb" if method == "207" else "206Pb/208Pb"
        common = float(composition[ratio_name])

    l238_ma = LAMBDA_238 * 1e6
    radiogenic_68 = np.expm1(l238_ma * age) if np.isfinite(age) else np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        f206 = 1.0 - radiogenic_68 / ratio_68

    if method == "207":
        sigma = age_207_corrected_uncertainty(
            age,
            ratio_68,
            ratio_68_1s,
            ratio_other,
            ratio_other_1s,
            common,
            common_ratio_1s,
            u238_u235=u238_u235,
        )
    else:
        sigma = age_208_corrected_uncertainty(
            age,
            ratio_68,
            ratio_68_1s,
            ratio_other,
            ratio_other_1s,
            th_u,
            th_u_1s,
            common,
            common_ratio_1s,
        )

    warnings = []
    lower, upper = age_bounds_ma
    if not converged:
        warnings.append("age solver did not converge")
    if np.isfinite(f206) and not 0 <= f206 <= 1:
        warnings.append("inferred f206 is outside 0-1")
    if np.isfinite(age) and not lower <= age <= upper:
        warnings.append("age is outside configured bounds")
    if method == "208" and np.isfinite(th_u) and th_u > max_208_th_u:
        warnings.append(f"Th/U exceeds {max_208_th_u:g}")
    valid = bool(
        converged
        and np.isfinite(age)
        and np.isfinite(sigma)
        and np.isfinite(f206)
        and 0 <= f206 <= 1
        and lower <= age <= upper
        and (method != "208" or th_u <= max_208_th_u)
    )
    return CommonPbAgeResult(
        age_ma=float(age),
        sigma_ma=float(sigma),
        f206=float(f206),
        common_ratio=float(common),
        iterations=model_iteration if use_stacey_kramers else solver_iterations,
        converged=bool(converged),
        valid=valid,
        warning="; ".join(warnings),
    )


def apply_204pb_common_pb_correction(
    data,
    *,
    common_pb_model: str | None = "stacey_kramers",
    common_pb206_pb204: float | str | None = None,
    common_pb207_pb204: float | str | None = None,
    common_pb208_pb204: float | str | None = None,
    common_pb206_pb204_1s: float = 0.0,
    common_pb207_pb204_1s: float = 0.0,
    common_pb208_pb204_1s: float = 0.0,
    initial_age_ma: float = 1000.0,
    max_iterations: int = 20,
    tolerance_ma: float = 1e-5,
    stacey_kramers_u238_u235: float = old_common_U238_U235,
):
    """Append conventional mass-204 common-Pb-corrected ratios and ages.

    The measured mass-204 signal must already have its mercury contribution
    removed. :func:`UPbRPy.ratios.calc_raw_ratios` performs that first step and
    supplies ``206Pb/204Pb`` plus its counting-statistics uncertainty. This
    function performs the distinct second step: it uses an initial common-Pb
    composition to remove common 206Pb, 207Pb, and 208Pb from the calibrated
    U-Pb-Th ratios.

    A fixed composition can be supplied as scalars or column names. Otherwise
    Stacey-Kramers compositions are iterated against the corrected 206Pb/238U
    age. Uncertainty propagation follows the independent-input derivatives in
    the legacy workbook and intentionally contains no covariance terms.
    Common-Pb-composition uncertainties default to zero because they must be
    supplied by the chosen geological model or independent measurement rather
    than invented by the reduction software.
    """
    import pandas as pd

    if not isinstance(data, pd.DataFrame):
        raise TypeError("data must be a pandas DataFrame")
    if common_pb_model not in (None, "stacey_kramers"):
        raise ValueError("common_pb_model must be None or 'stacey_kramers'")
    fixed_specs = (
        common_pb206_pb204,
        common_pb207_pb204,
        common_pb208_pb204,
    )
    if common_pb_model is None and any(value is None for value in fixed_specs):
        raise ValueError(
            "A fixed 204-method correction requires 206Pb/204Pb, "
            "207Pb/204Pb, and 208Pb/204Pb common-Pb compositions."
        )

    frame = data.copy()

    def choose_column(explicit, candidates, label, required=True):
        """Resolve an explicit or first-available mass-204 input column."""
        if explicit is not None:
            if explicit not in frame.columns:
                raise KeyError(f"{label} column {explicit!r} is not present")
            return explicit
        for candidate in candidates:
            if candidate in frame.columns:
                return candidate
        if required:
            raise KeyError(
                f"Could not find {label}; tried "
                + ", ".join(repr(candidate) for candidate in candidates)
            )
        return None

    def resolve_values(specification):
        """Expand a scalar/column common-Pb specification to row values."""
        if isinstance(specification, str):
            if specification not in frame.columns:
                raise KeyError(
                    f"Common-Pb ratio column {specification!r} is not present"
                )
            return frame[specification].to_numpy(dtype=float)
        if specification is None:
            return np.full(len(frame), np.nan)
        return np.full(len(frame), float(specification))

    ratio_columns = {
        "64": choose_column(
            None,
            ("206Pb/204Pb", "206Pb/204Pb_fractionation_corrected"),
            "measured 206Pb/204Pb",
        ),
        "68pb": choose_column(
            None,
            ("206Pb/208Pb", "206Pb/208Pb_fractionation_corrected"),
            "measured 206Pb/208Pb",
        ),
        "68": choose_column(
            None,
            ("206Pb/238U_fractionation_corrected", "206Pb/238U"),
            "total 206Pb/238U",
        ),
        "76": choose_column(
            None,
            ("207Pb/206Pb_fractionation_corrected", "207Pb/206Pb"),
            "total 207Pb/206Pb",
        ),
        "82": choose_column(
            None,
            ("208Pb/232Th_fractionation_corrected", "208Pb/232Th"),
            "total 208Pb/232Th",
        ),
    }
    uncertainty_columns = {}
    for key, ratio_column in ratio_columns.items():
        uncertainty_columns[(key, "with_calibration")] = choose_column(
            None,
            (
                ratio_column + "_2e_with_calibration",
                ratio_column + "_2e",
            ),
            f"2-sigma {ratio_column} uncertainty",
        )
        uncertainty_columns[(key, "no_calibration")] = choose_column(
            None,
            (ratio_column + "_2e_no_calibration", ratio_column + "_2e"),
            f"2-sigma {ratio_column} uncertainty without calibration",
        )

    ratio_values = {
        key: frame[column].to_numpy(dtype=float)
        for key, column in ratio_columns.items()
    }
    common_fixed = {
        "64": resolve_values(common_pb206_pb204),
        "74": resolve_values(common_pb207_pb204),
        "84": resolve_values(common_pb208_pb204),
    }
    common_sigmas = {
        "64": float(common_pb206_pb204_1s),
        "74": float(common_pb207_pb204_1s),
        "84": float(common_pb208_pb204_1s),
    }

    ages = np.full(len(frame), np.nan)
    iterations = np.zeros(len(frame), dtype=int)
    converged = np.zeros(len(frame), dtype=bool)
    common_values = {
        "64": np.full(len(frame), np.nan),
        "74": np.full(len(frame), np.nan),
        "84": np.full(len(frame), np.nan),
    }
    corrected = {
        "68": np.full(len(frame), np.nan),
        "76": np.full(len(frame), np.nan),
        "82": np.full(len(frame), np.nan),
    }

    for index in range(len(frame)):
        total_64 = ratio_values["64"][index]
        total_68 = ratio_values["68"][index]
        total_76 = ratio_values["76"][index]
        total_82 = ratio_values["82"][index]
        total_68pb = ratio_values["68pb"][index]
        if not np.all(
            np.isfinite([total_64, total_68, total_76, total_82, total_68pb])
        ) or total_64 <= 0:
            continue

        age = float(initial_age_ma)
        row_converged = common_pb_model is None
        used_iterations = 1
        for iteration in range(1, max_iterations + 1):
            if common_pb_model == "stacey_kramers":
                model = stacey_kramers_common_pb(
                    age,
                    u238_u235=stacey_kramers_u238_u235,
                )
                common_64 = float(model["206Pb/204Pb"])
                common_74 = float(model["207Pb/204Pb"])
                common_84 = float(model["208Pb/204Pb"])
            else:
                common_64 = common_fixed["64"][index]
                common_74 = common_fixed["74"][index]
                common_84 = common_fixed["84"][index]

            denominator = total_64 - common_64
            if not np.isfinite(denominator) or denominator <= 0:
                break
            corrected_68 = total_68 * denominator / total_64
            if corrected_68 <= -1 or not np.isfinite(corrected_68):
                break
            next_age = float(np.log1p(corrected_68) / LAMBDA_238 / 1e6)
            used_iterations = iteration
            if common_pb_model is None or abs(next_age - age) <= tolerance_ma:
                age = next_age
                row_converged = True
                break
            age = next_age

        if common_pb_model == "stacey_kramers" and np.isfinite(age):
            model = stacey_kramers_common_pb(
                age,
                u238_u235=stacey_kramers_u238_u235,
            )
            common_64 = float(model["206Pb/204Pb"])
            common_74 = float(model["207Pb/204Pb"])
            common_84 = float(model["208Pb/204Pb"])

        denominator = total_64 - common_64
        total_84 = total_64 / total_68pb
        if denominator <= 0 or total_84 == 0:
            continue
        common_values["64"][index] = common_64
        common_values["74"][index] = common_74
        common_values["84"][index] = common_84
        corrected["68"][index] = total_68 * denominator / total_64
        corrected["76"][index] = (
            total_64 * total_76 - common_74
        ) / denominator
        corrected["82"][index] = total_82 * (
            total_84 - common_84
        ) / total_84
        ages[index] = age
        iterations[index] = used_iterations
        converged[index] = row_converged

    frame["204Pb_corrected_206Pb/238U"] = corrected["68"]
    frame["204Pb_corrected_207Pb/206Pb"] = corrected["76"]
    frame["204Pb_corrected_208Pb/232Th"] = corrected["82"]
    frame["204Pb_correction_common_206Pb/204Pb"] = common_values["64"]
    frame["204Pb_correction_common_207Pb/204Pb"] = common_values["74"]
    frame["204Pb_correction_common_208Pb/204Pb"] = common_values["84"]
    frame["204Pb_correction_f206_radiogenic"] = (
        1 - common_values["64"] / ratio_values["64"]
    )
    frame["204Pb_correction_iterations"] = iterations
    frame["204Pb_correction_converged"] = converged
    frame["204Pb_correction_valid"] = (
        converged
        & np.isfinite(corrected["68"])
        & np.isfinite(corrected["76"])
        & np.isfinite(corrected["82"])
        & (corrected["68"] > 0)
        & (corrected["76"] > 0)
        & (corrected["82"] > 0)
    )
    frame["204Pb_correction_warning"] = np.where(
        frame["204Pb_correction_valid"],
        "",
        "invalid or non-convergent mass-204 correction",
    )

    for uncertainty_mode in ("no_calibration", "with_calibration"):
        sigmas = {
            key: frame[column].to_numpy(dtype=float) / 2
            for (key, mode), column in uncertainty_columns.items()
            if mode == uncertainty_mode
        }
        total_64 = ratio_values["64"]
        total_68pb = ratio_values["68pb"]
        denominator = total_64 - common_values["64"]
        numerator_76 = total_64 * ratio_values["76"] - common_values["74"]
        total_84 = total_64 / total_68pb
        with np.errstate(divide="ignore", invalid="ignore"):
            # The radiogenic 206Pb fraction is
            # (measured 206/204 - common 206/204) / measured 206/204.
            f206 = denominator / total_64
            f206_variance = (
                (common_values["64"] / total_64**2 * sigmas["64"]) ** 2
                + (common_sigmas["64"] / total_64) ** 2
            )
            sigma_68 = np.abs(corrected["68"]) * np.sqrt(
                (sigmas["68"] / ratio_values["68"]) ** 2
                + f206_variance / f206**2
            )

            # Propagate the 207/206 correction analytically from
            # (measured 206/204 * measured 207/206 - common 207/204)
            # / (measured 206/204 - common 206/204).
            derivative_64 = (
                common_values["74"]
                - ratio_values["76"] * common_values["64"]
            ) / denominator**2
            derivative_76 = total_64 / denominator
            derivative_74 = -1 / denominator
            derivative_common64 = numerator_76 / denominator**2
            sigma_76 = np.sqrt(
                (derivative_64 * sigmas["64"]) ** 2
                + (derivative_76 * sigmas["76"]) ** 2
                + (derivative_74 * common_sigmas["74"]) ** 2
                + (derivative_common64 * common_sigmas["64"]) ** 2
            )

            # Convert measured 206/208 and 206/204 to total 208/204 before
            # removing the common-208 fraction from measured 208Pb/232Th.
            f208 = 1 - common_values["84"] * total_68pb / total_64
            f208_variance = (
                (
                    common_values["84"]
                    * total_68pb
                    / total_64**2
                    * sigmas["64"]
                )
                ** 2
                + (common_values["84"] / total_64 * sigmas["68pb"]) ** 2
                + (total_68pb / total_64 * common_sigmas["84"]) ** 2
            )
            sigma_82 = np.abs(corrected["82"]) * np.sqrt(
                (sigmas["82"] / ratio_values["82"]) ** 2
                + f208_variance / f208**2
            )

        for key, values in (
            ("206Pb/238U", sigma_68),
            ("207Pb/206Pb", sigma_76),
            ("208Pb/232Th", sigma_82),
        ):
            frame[
                f"204Pb_corrected_{key}_2e_{uncertainty_mode}"
            ] = 2 * values

    # Backward-compatible generic uncertainties use the inclusive branch.
    for ratio_name in ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th"):
        frame[f"204Pb_corrected_{ratio_name}_2e"] = frame[
            f"204Pb_corrected_{ratio_name}_2e_with_calibration"
        ]

    # A local import avoids an isochron/isoplot import cycle at module load.
    from .isoplot import (
        calc_68_date,
        calc_68_date_uncertainty,
        calc_75_date,
        calc_75_date_uncertainty,
        calc_76_date,
        calc_76_date_uncertainty,
        calc_82_date,
        calc_82_date_uncertainty,
    )

    frame["204Pb_corrected_207Pb/235U"] = (
        frame["204Pb_corrected_207Pb/206Pb"]
        * frame["204Pb_corrected_206Pb/238U"]
        * U238_U235
    )
    frame["204Pb_corrected_206Pb/238U_age"] = calc_68_date(
        frame["204Pb_corrected_206Pb/238U"]
    )
    frame["204Pb_corrected_208Pb/232Th_age"] = calc_82_date(
        frame["204Pb_corrected_208Pb/232Th"]
    )
    frame["204Pb_corrected_207Pb/206Pb_age"] = [
        calc_76_date(value) if np.isfinite(value) and value > 0 else np.nan
        for value in frame["204Pb_corrected_207Pb/206Pb"]
    ]
    frame["204Pb_corrected_207Pb/235U_age"] = calc_75_date(
        frame["204Pb_corrected_207Pb/235U"]
    )
    for uncertainty_mode in ("no_calibration", "with_calibration"):
        ratio_68_uncertainty = frame[
            f"204Pb_corrected_206Pb/238U_2e_{uncertainty_mode}"
        ]
        ratio_76_uncertainty = frame[
            f"204Pb_corrected_207Pb/206Pb_2e_{uncertainty_mode}"
        ]
        ratio_82_uncertainty = frame[
            f"204Pb_corrected_208Pb/232Th_2e_{uncertainty_mode}"
        ]
        ratio_75_relative = np.sqrt(
            (
                ratio_68_uncertainty
                / frame["204Pb_corrected_206Pb/238U"]
            )
            ** 2
            + (
                ratio_76_uncertainty
                / frame["204Pb_corrected_207Pb/206Pb"]
            )
            ** 2
        )
        ratio_75_uncertainty = (
            frame["204Pb_corrected_207Pb/235U"] * ratio_75_relative
        )
        frame[
            f"204Pb_corrected_207Pb/235U_2e_{uncertainty_mode}"
        ] = ratio_75_uncertainty
        frame[
            f"204Pb_corrected_206Pb/238U_age_2e_{uncertainty_mode}"
        ] = calc_68_date_uncertainty(
            frame["204Pb_corrected_206Pb/238U"],
            ratio_68_uncertainty,
        )
        frame[
            f"204Pb_corrected_208Pb/232Th_age_2e_{uncertainty_mode}"
        ] = calc_82_date_uncertainty(
            frame["204Pb_corrected_208Pb/232Th"],
            ratio_82_uncertainty,
        )
        frame[
            f"204Pb_corrected_207Pb/235U_age_2e_{uncertainty_mode}"
        ] = calc_75_date_uncertainty(
            frame["204Pb_corrected_207Pb/235U"],
            ratio_75_uncertainty,
        )
        frame[
            f"204Pb_corrected_207Pb/206Pb_age_2e_{uncertainty_mode}"
        ] = [
            calc_76_date_uncertainty(ratio, uncertainty)
            if np.isfinite(ratio) and ratio > 0
            else np.nan
            for ratio, uncertainty in zip(
                frame["204Pb_corrected_207Pb/206Pb"],
                ratio_76_uncertainty,
            )
        ]
    for age_system in (
        "206Pb/238U",
        "207Pb/235U",
        "207Pb/206Pb",
        "208Pb/232Th",
    ):
        frame[f"204Pb_corrected_{age_system}_age_2e"] = frame[
            f"204Pb_corrected_{age_system}_age_2e_with_calibration"
        ]

    frame.attrs = dict(data.attrs)
    frame.attrs["mass_204_common_pb_correction"] = {
        "model": common_pb_model or "fixed",
        "common_pb206_pb204_1s": common_pb206_pb204_1s,
        "common_pb207_pb204_1s": common_pb207_pb204_1s,
        "common_pb208_pb204_1s": common_pb208_pb204_1s,
        "stacey_kramers_u238_u235": stacey_kramers_u238_u235,
    }
    return frame


def apply_common_pb_corrections(
    data,
    *,
    common_pb_model: str | None = "stacey_kramers",
    common_pb207_pb206: float | str | None = None,
    common_pb206_pb208: float | str | None = None,
    common_pb207_pb206_1s: float = 0.001,
    common_pb206_pb208_1s: float = 0.002,
    th_u_relative_1s: float = 0.02,
    initial_age_ma: float = 1000.0,
    max_iterations: int = 20,
    tolerance_ma: float = 1e-5,
    age_bounds_ma: tuple[float, float] = (-1000.0, EARTH_AGE_MA),
    max_208_th_u: float = DEFAULT_208_MAX_TH_U,
    u238_u235: float = U238_U235,
    stacey_kramers_u238_u235: float = old_common_U238_U235,
    ratio_68_column: str | None = None,
    ratio_68_2s_column: str | None = None,
    ratio_76_column: str | None = None,
    ratio_76_2s_column: str | None = None,
    ratio_82_column: str | None = None,
    ratio_82_2s_column: str | None = None,
    th_u_column: str = "Th/U",
    include_204_method: bool = True,
    common_pb206_pb204: float | str | None = None,
    common_pb207_pb204: float | str | None = None,
    common_pb208_pb204: float | str | None = None,
    common_pb206_pb204_1s: float = 0.0,
    common_pb207_pb204_1s: float = 0.0,
    common_pb208_pb204_1s: float = 0.0,
):
    """Append conventional 204-, 207-, and 208-method common-Pb outputs.

    The input is copied. Spot-mean and sweep-wise frames are both supported;
    their calibrated ratio column names are detected automatically. A fixed
    common-Pb composition can be supplied as a scalar or column name. When it
    is omitted, ``common_pb_model='stacey_kramers'`` iterates each analysis
    from a 1000 Ma initial estimate following Chew et al. (2011).

    The uncertainty equations follow the independent-input propagation in the
    Excel VBA routines. The defaults reproduce its absolute one-sigma common
    ratio uncertainties (0.001 for 207Pb/206Pb and 0.002 for 206Pb/208Pb) and
    its 2% relative one-sigma Th/U uncertainty. No ratio covariance is added.
    """
    import pandas as pd

    if not isinstance(data, pd.DataFrame):
        raise TypeError("data must be a pandas DataFrame")
    if common_pb_model not in (None, "stacey_kramers"):
        raise ValueError("common_pb_model must be None or 'stacey_kramers'")
    if common_pb_model is None and (
        common_pb207_pb206 is None or common_pb206_pb208 is None
    ):
        raise ValueError(
            "Fixed correction requires both common_pb207_pb206 and "
            "common_pb206_pb208."
        )

    frame = data.copy()
    if include_204_method and {
        "206Pb/204Pb",
        "206Pb/208Pb",
    }.issubset(frame.columns):
        frame = apply_204pb_common_pb_correction(
            frame,
            common_pb_model=common_pb_model,
            common_pb206_pb204=common_pb206_pb204,
            common_pb207_pb204=common_pb207_pb204,
            common_pb208_pb204=common_pb208_pb204,
            common_pb206_pb204_1s=common_pb206_pb204_1s,
            common_pb207_pb204_1s=common_pb207_pb204_1s,
            common_pb208_pb204_1s=common_pb208_pb204_1s,
            initial_age_ma=initial_age_ma,
            max_iterations=max_iterations,
            tolerance_ma=tolerance_ma,
            stacey_kramers_u238_u235=stacey_kramers_u238_u235,
        )

    def choose_column(explicit, candidates, label):
        """Resolve an explicit or first-available required input column."""
        if explicit is not None:
            if explicit not in frame.columns:
                raise KeyError(f"{label} column {explicit!r} is not present")
            return explicit
        for candidate in candidates:
            if candidate in frame.columns:
                return candidate
        raise KeyError(
            f"Could not find {label}; tried " + ", ".join(repr(c) for c in candidates)
        )

    ratio_68_column = choose_column(
        ratio_68_column,
        ("206Pb/238U_fractionation_corrected", "206Pb/238U"),
        "total 206Pb/238U",
    )
    ratio_76_column = choose_column(
        ratio_76_column,
        ("207Pb/206Pb_fractionation_corrected", "207Pb/206Pb"),
        "total 207Pb/206Pb",
    )
    ratio_82_column = choose_column(
        ratio_82_column,
        ("208Pb/232Th_fractionation_corrected", "208Pb/232Th"),
        "total 208Pb/232Th",
    )
    ratio_68_2s_column = choose_column(
        ratio_68_2s_column,
        (
            f"{ratio_68_column}_2e",
            "206Pb/238U_2e",
            "206Pb/238U_fractionation_corrected_2e",
        ),
        "2-sigma total 206Pb/238U uncertainty",
    )
    ratio_76_2s_column = choose_column(
        ratio_76_2s_column,
        (
            f"{ratio_76_column}_2e",
            "207Pb/206Pb_2e",
            "207Pb/206Pb_fractionation_corrected_2e",
        ),
        "2-sigma total 207Pb/206Pb uncertainty",
    )
    ratio_82_2s_column = choose_column(
        ratio_82_2s_column,
        (
            f"{ratio_82_column}_2e",
            "208Pb/232Th_2e",
            "208Pb/232Th_fractionation_corrected_2e",
        ),
        "2-sigma total 208Pb/232Th uncertainty",
    )

    if th_u_column not in frame.columns:
        if "Th" not in frame.columns or "U" not in frame.columns:
            raise KeyError(
                f"{th_u_column!r} is absent and calibrated Th and U columns "
                "are not available to derive it."
            )
        with np.errstate(divide="ignore", invalid="ignore"):
            frame[th_u_column] = frame["Th"] / frame["U"]

    def resolve_common(specification):
        """Expand a fixed common-Pb scalar or column to a NumPy array."""
        if isinstance(specification, str):
            if specification not in frame.columns:
                raise KeyError(
                    f"Common-Pb ratio column {specification!r} is not present"
                )
            return frame[specification].to_numpy(dtype=float)
        if specification is None:
            return np.full(len(frame), np.nan)
        return np.full(len(frame), float(specification))

    common_76_values = resolve_common(common_pb207_pb206)
    common_68_values = resolve_common(common_pb206_pb208)
    ratio_68_values = frame[ratio_68_column].to_numpy(dtype=float)
    ratio_68_1s = frame[ratio_68_2s_column].to_numpy(dtype=float) / 2.0
    ratio_76_values = frame[ratio_76_column].to_numpy(dtype=float)
    ratio_76_1s = frame[ratio_76_2s_column].to_numpy(dtype=float) / 2.0
    ratio_82_values = frame[ratio_82_column].to_numpy(dtype=float)
    ratio_82_1s = frame[ratio_82_2s_column].to_numpy(dtype=float) / 2.0
    th_u_values = frame[th_u_column].to_numpy(dtype=float)
    th_u_1s = np.abs(th_u_values) * float(th_u_relative_1s)
    use_model_207 = common_pb_model == "stacey_kramers" and common_pb207_pb206 is None
    use_model_208 = common_pb_model == "stacey_kramers" and common_pb206_pb208 is None

    results_207 = []
    results_208 = []
    for index in range(len(frame)):
        shared = dict(
            ratio_68=ratio_68_values[index],
            ratio_68_1s=ratio_68_1s[index],
            th_u=th_u_values[index],
            th_u_1s=th_u_1s[index],
            initial_age_ma=initial_age_ma,
            stacey_kramers_u238_u235=stacey_kramers_u238_u235,
            u238_u235=u238_u235,
            tolerance_ma=tolerance_ma,
            max_iterations=max_iterations,
            age_bounds_ma=age_bounds_ma,
            max_208_th_u=max_208_th_u,
        )
        results_207.append(
            calculate_common_pb_result(
                method="207",
                ratio_other=ratio_76_values[index],
                ratio_other_1s=ratio_76_1s[index],
                common_ratio=(
                    None if use_model_207 else common_76_values[index]
                ),
                common_ratio_1s=common_pb207_pb206_1s,
                use_stacey_kramers=use_model_207,
                **shared,
            )
        )
        results_208.append(
            calculate_common_pb_result(
                method="208",
                ratio_other=ratio_82_values[index],
                ratio_other_1s=ratio_82_1s[index],
                common_ratio=(
                    None if use_model_208 else common_68_values[index]
                ),
                common_ratio_1s=common_pb206_pb208_1s,
                use_stacey_kramers=use_model_208,
                **shared,
            )
        )

    def choose_optional_uncertainty_column(ratio_column):
        """Find the analytical uncertainty branch for one calibrated ratio."""
        for candidate in (
            ratio_column + "_2e_no_calibration",
            ratio_column.replace(
                "_fractionation_corrected", ""
            ) + "_2e_no_calibration",
        ):
            if candidate in frame.columns:
                return candidate
        return None

    no_calibration_columns = {
        "68": choose_optional_uncertainty_column(ratio_68_column),
        "76": choose_optional_uncertainty_column(ratio_76_column),
        "82": choose_optional_uncertainty_column(ratio_82_column),
    }
    no_calibration_sigmas = {
        key: (
            frame[column].to_numpy(dtype=float) / 2
            if column is not None
            else fallback
        )
        for key, column, fallback in (
            ("68", no_calibration_columns["68"], ratio_68_1s),
            ("76", no_calibration_columns["76"], ratio_76_1s),
            ("82", no_calibration_columns["82"], ratio_82_1s),
        )
    }
    no_calibration_age_sigmas = {
        "207": np.asarray(
            [
                age_207_corrected_uncertainty(
                    result.age_ma,
                    ratio_68_values[index],
                    no_calibration_sigmas["68"][index],
                    ratio_76_values[index],
                    no_calibration_sigmas["76"][index],
                    result.common_ratio,
                    common_pb207_pb206_1s,
                    u238_u235=u238_u235,
                )
                for index, result in enumerate(results_207)
            ],
            dtype=float,
        ),
        "208": np.asarray(
            [
                age_208_corrected_uncertainty(
                    result.age_ma,
                    ratio_68_values[index],
                    no_calibration_sigmas["68"][index],
                    ratio_82_values[index],
                    no_calibration_sigmas["82"][index],
                    th_u_values[index],
                    th_u_1s[index],
                    result.common_ratio,
                    common_pb206_pb208_1s,
                )
                for index, result in enumerate(results_208)
            ],
            dtype=float,
        ),
    }

    for method, results, common_label in (
        ("207", results_207, "207Pb/206Pb"),
        ("208", results_208, "206Pb/208Pb"),
    ):
        ages = np.asarray([result.age_ma for result in results])
        sigmas = np.asarray([result.sigma_ma for result in results])
        frame[f"{method}Pb_corrected_age"] = ages
        frame[f"{method}Pb_corrected_age_2e"] = 2.0 * sigmas
        frame[f"{method}Pb_corrected_age_2e_with_calibration"] = (
            2.0 * sigmas
        )
        frame[f"{method}Pb_corrected_age_2e_no_calibration"] = (
            2.0 * no_calibration_age_sigmas[method]
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            frame[f"{method}Pb_corrected_age_2e%"] = 200.0 * sigmas / ages
            frame[
                f"{method}Pb_corrected_age_2e_with_calibration%"
            ] = 200.0 * sigmas / ages
            frame[
                f"{method}Pb_corrected_age_2e_no_calibration%"
            ] = 200.0 * no_calibration_age_sigmas[method] / ages
        frame[f"{method}Pb_correction_f206"] = [
            result.f206 for result in results
        ]
        frame[f"{method}Pb_correction_common_{common_label}"] = [
            result.common_ratio for result in results
        ]
        frame[f"{method}Pb_correction_iterations"] = [
            result.iterations for result in results
        ]
        frame[f"{method}Pb_correction_converged"] = [
            result.converged for result in results
        ]
        frame[f"{method}Pb_correction_valid"] = [
            result.valid for result in results
        ]
        frame[f"{method}Pb_correction_warning"] = [
            result.warning for result in results
        ]
        radiogenic_68 = np.expm1(LAMBDA_238 * ages * 1e6)
        frame[f"206Pb/238U_{method}Pb_corrected"] = radiogenic_68
        frame[f"206Pb/238U_{method}Pb_corrected_2e"] = (
            2.0
            * LAMBDA_238
            * 1e6
            * np.exp(LAMBDA_238 * ages * 1e6)
            * sigmas
        )
        frame[
            f"206Pb/238U_{method}Pb_corrected_2e_with_calibration"
        ] = frame[f"206Pb/238U_{method}Pb_corrected_2e"]
        frame[
            f"206Pb/238U_{method}Pb_corrected_2e_no_calibration"
        ] = (
            2.0
            * LAMBDA_238
            * 1e6
            * np.exp(LAMBDA_238 * ages * 1e6)
            * no_calibration_age_sigmas[method]
        )

    # Retain mass-204 provenance when that optional branch was calculated.
    frame.attrs = dict(frame.attrs)
    frame.attrs["common_pb_correction"] = {
        "methods": (
            ("204", "207", "208")
            if "204Pb_corrected_206Pb/238U" in frame.columns
            else ("207", "208")
        ),
        "model": common_pb_model or "fixed",
        "common_pb207_pb206_1s": common_pb207_pb206_1s,
        "common_pb206_pb208_1s": common_pb206_pb208_1s,
        "th_u_relative_1s": th_u_relative_1s,
        "max_208_th_u": max_208_th_u,
        "u238_u235": u238_u235,
        "stacey_kramers_u238_u235": stacey_kramers_u238_u235,
    }
    return frame


@dataclass
class LineFit:
    """Straight-line fit with parameter covariance.

    The covariance matrix is ordered as ``[intercept, slope]``.
    """

    intercept: float
    slope: float
    cov: np.ndarray
    mswd: float
    p_value: float
    df: int
    anchored: bool = False
    anchor: tuple[float, float] | None = None

    @property
    def intercept_sigma(self) -> float:
        """Return the one-sigma uncertainty of the fitted intercept."""
        return float(np.sqrt(self.cov[0, 0]))

    @property
    def slope_sigma(self) -> float:
        """Return the one-sigma uncertainty of the fitted slope."""
        return float(np.sqrt(self.cov[1, 1]))


@dataclass
class AgeIntercept:
    """Concordia intersection age and first-order line-fit uncertainty."""

    age_ma: float
    sigma_ma: float
    x: float
    y: float
    kind: str

    @property
    def two_sigma_ma(self) -> float:
        """Return the concordia-intercept age uncertainty at two standard deviations."""
        return 2.0 * self.sigma_ma


@dataclass
class MonteCarloIntercepts:
    """Monte Carlo concordia-intercept age distributions."""

    lower_ma: np.ndarray
    upper_ma: np.ndarray
    line_intercepts: np.ndarray
    line_slopes: np.ndarray

    def summary(self, confidence: float = 0.95) -> dict[str, dict[str, float]]:
        """Summarize finite Monte Carlo intercept ages.

        Parameters
        ----------
        confidence
            Central probability interval, between zero and one.

        Returns
        -------
        dict
            Median plus lower and upper asymmetric distances for the lower and
            upper intercept distributions.
        """
        alpha = (1.0 - confidence) / 2.0
        probs = [100 * alpha, 50.0, 100 * (1.0 - alpha)]
        out: dict[str, dict[str, float]] = {}
        for name, values in {"lower": self.lower_ma, "upper": self.upper_ma}.items():
            finite = values[np.isfinite(values)]
            if finite.size == 0:
                out[name] = {"median": np.nan, "minus": np.nan, "plus": np.nan}
                continue
            lo, med, hi = np.percentile(finite, probs)
            out[name] = {"median": float(med), "minus": float(med - lo), "plus": float(hi - med)}
        return out


@dataclass
class SingleSweepResult:
    """Single sweep line fit through an assumed initial Pb composition."""

    line: LineFit
    lower_intercept: AgeIntercept | None
    monte_carlo_summary: dict[str, float] | None = None


@dataclass
class ConcordiaAgeResult:
    """Maximum-likelihood common concordia age for a set of analyses."""

    age_ma: float
    sigma_ma: float
    x: float
    y: float
    mswd: float
    p_value: float
    df: int
    n: int
    space: str

    @property
    def two_sigma_ma(self) -> float:
        """Return twice the fitted one-sigma common concordia-age uncertainty."""
        return 2.0 * self.sigma_ma


def line_fit_summary(line: LineFit, *, prefix: str = "line") -> dict[str, float | bool]:
    """Flatten line-fit parameters and diagnostics for tabular export.

    The covariance is represented by intercept/slope one-sigma uncertainties
    plus their covariance term; the original matrix is not embedded in a cell.
    """
    return {
        f"{prefix}_intercept": line.intercept,
        f"{prefix}_intercept_1s": line.intercept_sigma,
        f"{prefix}_slope": line.slope,
        f"{prefix}_slope_1s": line.slope_sigma,
        f"{prefix}_cov_intercept_slope": float(line.cov[0, 1]),
        f"{prefix}_mswd": line.mswd,
        f"{prefix}_p_value": line.p_value,
        f"{prefix}_df": line.df,
        f"{prefix}_anchored": line.anchored,
    }


def intercept_summary(intercepts: list[AgeIntercept], *, prefix: str = "") -> dict[str, float]:
    """Flatten concordia-intercept ages, uncertainties, flags, and plot coordinates."""
    out: dict[str, float] = {}
    stem = f"{prefix}_" if prefix else ""
    for intercept in intercepts:
        key = f"{stem}{intercept.kind}"
        out[f"{key}_intercept_age"] = intercept.age_ma
        out[f"{key}_intercept_age_1s"] = intercept.sigma_ma
        out[f"{key}_intercept_age_2s"] = intercept.two_sigma_ma
        out[f"{key}_intercept_x"] = intercept.x
        out[f"{key}_intercept_y"] = intercept.y
    return out


def fit_summary(line: LineFit, intercepts: list[AgeIntercept]) -> dict[str, float | bool]:
    """Combine line-fit and concordia-intercept summaries for tabular export."""
    out = line_fit_summary(line)
    out.update(intercept_summary(intercepts))
    return out


def concordia_age_summary(result: ConcordiaAgeResult, *, prefix: str = "concordia") -> dict[str, float | int | str]:
    """Flatten a common concordia-age result and fit diagnostics."""
    return {
        f"{prefix}_age": result.age_ma,
        f"{prefix}_age_1s": result.sigma_ma,
        f"{prefix}_age_2s": result.two_sigma_ma,
        f"{prefix}_x": result.x,
        f"{prefix}_y": result.y,
        f"{prefix}_mswd": result.mswd,
        f"{prefix}_p_value": result.p_value,
        f"{prefix}_df": result.df,
        f"{prefix}_n": result.n,
        f"{prefix}_space": result.space,
    }


def monte_carlo_summary_frame(mc: MonteCarloIntercepts, confidence: float = 0.95):
    """Return finite-count and confidence summaries for MC intercept ages."""
    import pandas as pd

    rows = []
    summary = mc.summary(confidence=confidence)
    for name, values in summary.items():
        samples = getattr(mc, f"{name}_ma")
        finite = samples[np.isfinite(samples)]
        rows.append({
            "intercept": name,
            "median": values["median"],
            "minus": values["minus"],
            "plus": values["plus"],
            "confidence": confidence,
            "n_finite": int(finite.size),
        })
    return pd.DataFrame(rows)


def wetherill_point(age_ma, lambda_235=LAMBDA_235, lambda_238=LAMBDA_238):
    """Return Wetherill concordia coordinates for one or more ages.

    Ages are supplied in Ma; the returned x and y coordinates are
    ``207Pb/235U`` and ``206Pb/238U``.
    """
    age_years = np.asarray(age_ma, dtype=float) * 1e6
    return np.exp(lambda_235 * age_years) - 1.0, np.exp(lambda_238 * age_years) - 1.0


def tera_wasserburg_point(
    age_ma,
    lambda_235=LAMBDA_235,
    lambda_238=LAMBDA_238,
    u238_u235=U238_U235,
):
    """Return Tera-Wasserburg concordia coordinates for one or more ages.

    Wetherill coordinates are inverted and combined with ``238U/235U`` to
    return ``238U/206Pb`` and radiogenic ``207Pb/206Pb``.
    """
    pb207_u235, pb206_u238 = wetherill_point(age_ma, lambda_235, lambda_238)
    return 1.0 / pb206_u238, pb207_u235 / pb206_u238 / u238_u235


def as_arrays(x, y, sx, sy, rho, *, min_points=2):
    """Validate, broadcast, and filter bivariate analytical observations.

    Non-finite rows, non-positive uncertainties, and singular correlations are
    removed. A scalar ``rho`` is broadcast to the length of ``x``.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    sx = np.asarray(sx, dtype=float)
    sy = np.asarray(sy, dtype=float)
    rho = np.zeros_like(x, dtype=float) + np.asarray(rho, dtype=float)
    mask = (
        np.isfinite(x)
        & np.isfinite(y)
        & np.isfinite(sx)
        & np.isfinite(sy)
        & np.isfinite(rho)
        & (sx > 0)
        & (sy > 0)
        & (np.abs(rho) < 1)
    )
    if mask.sum() < min_points:
        raise ValueError(
            f"At least {min_points} finite point(s) with positive uncertainties are required."
        )
    return x[mask], y[mask], sx[mask], sy[mask], rho[mask]


def covariance_inverses(sx, sy, rho):
    """Return inverse 2×2 covariance matrices for correlated x-y errors.

    Each covariance is ``[[sx², rho*sx*sy], [rho*sx*sy, sy²]]``. A
    non-positive determinant is rejected because York and Mahalanobis fits
    require positive-definite analytical covariance.
    """
    cov_xx = sx**2
    cov_yy = sy**2
    cov_xy = rho * sx * sy
    det = cov_xx * cov_yy - cov_xy**2
    if np.any(det <= 0):
        raise ValueError("All analytical covariance matrices must be positive definite.")
    inv = np.empty((sx.size, 2, 2), dtype=float)
    inv[:, 0, 0] = cov_yy / det
    inv[:, 1, 1] = cov_xx / det
    inv[:, 0, 1] = inv[:, 1, 0] = -cov_xy / det
    return inv


def york_fit(x, y, sx, sy, rho=0.0, *, inflate_mswd=False, max_iter=50, tol=1e-15) -> LineFit:
    """Fit ``y = intercept + slope*x`` with correlated x-y errors.

    The York et al. (2004) iteration updates the slope-dependent weights until
    convergence. Returned parameter covariance is internal unless
    ``inflate_mswd`` requests multiplication by MSWD when MSWD exceeds one.
    """
    x, y, sx, sy, rho = as_arrays(x, y, sx, sy, rho)
    if x.size < 3:
        raise ValueError("Unanchored York fits require at least three points.")

    slope, intercept = np.polyfit(x, y, 1)
    wx = 1.0 / sx**2
    wy = 1.0 / sy**2
    alpha = np.sqrt(wx * wy)
    for _ in range(max_iter):
        old_slope = slope
        weights = wx * wy / (wx + slope**2 * wy - 2.0 * slope * rho * alpha)
        x_bar = np.sum(weights * x) / np.sum(weights)
        y_bar = np.sum(weights * y) / np.sum(weights)
        u = x - x_bar
        v = y - y_bar
        beta = weights * (u / wy + slope * v / wx - (slope * u + v) * rho / alpha)
        slope = np.sum(weights * beta * v) / np.sum(weights * beta * u)
        if (old_slope / slope - 1.0) ** 2 < tol:
            break

    intercept = y_bar - slope * x_bar
    x_adjusted = x_bar + beta
    x_adjusted_bar = np.sum(weights * x_adjusted) / np.sum(weights)
    u_adjusted = x_adjusted - x_adjusted_bar
    slope_sigma = np.sqrt(1.0 / np.sum(weights * u_adjusted**2))
    intercept_sigma = np.sqrt(1.0 / np.sum(weights) + x_adjusted_bar**2 * slope_sigma**2)
    cov_ab = -x_adjusted_bar * slope_sigma**2

    sse = np.sum(weights * (y - slope * x - intercept) ** 2)
    df = int(x.size - 2)
    mswd = float(sse / df)
    p_value = float(1.0 - chi2.cdf(sse, df))
    cov = np.array(
        [[intercept_sigma**2, cov_ab], [cov_ab, slope_sigma**2]],
        dtype=float,
    )
    if inflate_mswd and mswd > 1.0:
        cov *= mswd
    return LineFit(float(intercept), float(slope), cov, mswd, p_value, df)


def line_negloglike_slope(slope, x, y, sx, sy, rho, anchor):
    """Evaluate the anchored-line negative log likelihood for one slope."""
    x0, y0 = anchor
    residual = y - y0 - slope * (x - x0)
    variance = sy**2 + slope**2 * sx**2 - 2.0 * slope * rho * sx * sy
    if np.any(variance <= 0):
        return np.inf
    return 0.5 * np.sum(np.log(variance) + residual**2 / variance)


def second_derivative(fun, value):
    """Estimate a scalar function's second derivative by central difference."""
    step = max(abs(value), 1.0) * 1e-5
    return (fun(value + step) - 2.0 * fun(value) + fun(value - step)) / step**2


def anchored_york_fit(
    x,
    y,
    sx,
    sy,
    rho=0.0,
    *,
    anchor: tuple[float, float],
    anchor_cov: np.ndarray | None = None,
    inflate_mswd=False,
) -> LineFit:
    """Fit a York-style line constrained through an ``(x, y)`` anchor.

    Analytical x-y covariance controls the slope likelihood. Optional anchor
    covariance is propagated into the intercept/slope covariance matrix.
    """
    x, y, sx, sy, rho = as_arrays(x, y, sx, sy, rho)
    x0, y0 = map(float, anchor)
    initial_slope = np.linalg.lstsq((x - x0)[:, None], y - y0, rcond=None)[0][0]
    scale = max(abs(initial_slope), 1.0)
    objective = lambda b: line_negloglike_slope(b, x, y, sx, sy, rho, (x0, y0))
    fit = minimize_scalar(objective, bracket=(initial_slope - scale, initial_slope + scale))
    slope = float(fit.x)
    intercept = y0 - slope * x0

    curvature = second_derivative(objective, slope)
    slope_var = np.inf if curvature <= 0 else 1.0 / curvature
    cov = np.array(
        [[x0**2 * slope_var, -x0 * slope_var], [-x0 * slope_var, slope_var]],
        dtype=float,
    )
    if anchor_cov is not None:
        anchor_cov = np.asarray(anchor_cov, dtype=float)
        if anchor_cov.shape != (2, 2):
            raise ValueError("anchor_cov must be a 2 by 2 covariance matrix ordered as x,y.")
        jac = np.array([[-slope, 1.0], [0.0, 0.0]])
        cov += jac @ anchor_cov @ jac.T

    residual = y - intercept - slope * x
    variance = sy**2 + slope**2 * sx**2 - 2.0 * slope * rho * sx * sy
    sse = float(np.sum(residual**2 / variance))
    df = max(int(x.size - 1), 1)
    mswd = sse / df
    p_value = float(1.0 - chi2.cdf(sse, df))
    if inflate_mswd and mswd > 1.0:
        cov *= mswd
    return LineFit(intercept, slope, cov, mswd, p_value, df, anchored=True, anchor=(x0, y0))


def anchored_concordia_fit(x, y, sx, sy, rho=0.0, *, space="wetherill", age_ma: float) -> LineFit:
    """Fit a York-style line anchored to concordia at ``age_ma``."""
    anchor = concordia_point(age_ma, space=space)
    return anchored_york_fit(x, y, sx, sy, rho, anchor=anchor)


def line_from_anchor_and_point(
    point: tuple[float, float],
    point_cov: np.ndarray,
    anchor: tuple[float, float],
    anchor_cov: np.ndarray | None = None,
) -> LineFit:
    """Return the line through one measured point and one anchor.

    The fitted covariance is a first-order propagation from ``point`` and
    ``anchor`` into the line parameters ``[intercept, slope]``.
    """
    x1, y1 = map(float, point)
    x0, y0 = map(float, anchor)
    dx = x1 - x0
    if not np.isfinite(dx) or dx == 0:
        raise ValueError("The measured point and anchor must have different x values.")

    point_cov = np.asarray(point_cov, dtype=float)
    if point_cov.shape != (2, 2):
        raise ValueError("point_cov must be a 2 by 2 covariance matrix ordered as x,y.")
    if anchor_cov is None:
        anchor_cov = np.zeros((2, 2), dtype=float)
    else:
        anchor_cov = np.asarray(anchor_cov, dtype=float)
        if anchor_cov.shape != (2, 2):
            raise ValueError("anchor_cov must be a 2 by 2 covariance matrix ordered as x,y.")

    slope = (y1 - y0) / dx
    intercept = y0 - slope * x0

    dslope = np.array([
        -slope / dx,  # x1
        1.0 / dx,  # y1
        slope / dx,  # x0
        -1.0 / dx,  # y0
    ])
    dintercept = np.array([
        -x0 * dslope[0],
        -x0 * dslope[1],
        -slope - x0 * dslope[2],
        1.0 - x0 * dslope[3],
    ])
    jac = np.vstack([dintercept, dslope])
    input_cov = np.zeros((4, 4), dtype=float)
    input_cov[:2, :2] = point_cov
    input_cov[2:, 2:] = anchor_cov
    cov = jac @ input_cov @ jac.T
    return LineFit(
        intercept=float(intercept),
        slope=float(slope),
        cov=cov,
        mswd=np.nan,
        p_value=np.nan,
        df=0,
        anchored=True,
        anchor=(x0, y0),
    )


def concordia_point(age_ma, *, space="wetherill") -> tuple[float, float]:
    """Return scalar concordia coordinates in the requested diagram space."""
    key = normalise_space(space)
    if key == "wetherill":
        x, y = wetherill_point(age_ma)
    else:
        x, y = tera_wasserburg_point(age_ma)
    return float(np.asarray(x)), float(np.asarray(y))


def normalise_space(space: str) -> str:
    """Normalize common concordia-diagram aliases to one canonical name."""
    key = space.lower().replace("-", "_").replace(" ", "_")
    if key in {"wetherill", "wc"}:
        return "wetherill"
    if key in {"tw", "tera_wasserburg", "terawasserburg"}:
        return "tera_wasserburg"
    raise ValueError("space must be 'wetherill' or 'tera_wasserburg'.")


def line_concordia_residual(age_ma, line: LineFit, space: str):
    """Return vertical line-minus-concordia misfit at an age in Ma."""
    x, y = concordia_point(age_ma, space=space)
    return y - line.intercept - line.slope * x


def line_concordia_derivative(age_ma, line: LineFit, space: str):
    """Return the age derivative of the line-concordia residual.

    The derivative is analytical in both Wetherill and Tera-Wasserburg space
    and is used to propagate line-parameter uncertainty into intercept age.
    """
    t = age_ma * 1e6
    if space == "wetherill":
        dx_dt = LAMBDA_235 * 1e6 * np.exp(LAMBDA_235 * t)
        dy_dt = LAMBDA_238 * 1e6 * np.exp(LAMBDA_238 * t)
    else:
        r75 = np.exp(LAMBDA_235 * t) - 1.0
        r68 = np.exp(LAMBDA_238 * t) - 1.0
        dr75_dt = LAMBDA_235 * 1e6 * np.exp(LAMBDA_235 * t)
        dr68_dt = LAMBDA_238 * 1e6 * np.exp(LAMBDA_238 * t)
        dx_dt = -dr68_dt / r68**2
        dy_dt = (dr75_dt * r68 - r75 * dr68_dt) / (r68**2 * U238_U235)
    return dy_dt - line.slope * dx_dt


def concordia_curve_derivative(age_ma, space: str):
    """Return ``[dx/dt, dy/dt]`` for concordia at an age in Ma."""
    t = age_ma * 1e6
    if space == "wetherill":
        dx_dt = LAMBDA_235 * 1e6 * np.exp(LAMBDA_235 * t)
        dy_dt = LAMBDA_238 * 1e6 * np.exp(LAMBDA_238 * t)
    else:
        r75 = np.exp(LAMBDA_235 * t) - 1.0
        r68 = np.exp(LAMBDA_238 * t) - 1.0
        dr75_dt = LAMBDA_235 * 1e6 * np.exp(LAMBDA_235 * t)
        dr68_dt = LAMBDA_238 * 1e6 * np.exp(LAMBDA_238 * t)
        dx_dt = -dr68_dt / r68**2
        dy_dt = (dr75_dt * r68 - r75 * dr68_dt) / (r68**2 * U238_U235)
    return np.array([dx_dt, dy_dt], dtype=float)


def concordia_curve_second_derivative(age_ma, space: str):
    """Estimate concordia's second age derivative by central difference."""
    step = max(abs(age_ma), 1.0) * 1e-5
    return (
        concordia_curve_derivative(age_ma + step, space)
        - concordia_curve_derivative(age_ma - step, space)
    ) / (2.0 * step)


def concordia_age_objective(age_ma, xy, inv_cov, space):
    """Return half the summed Mahalanobis distance to concordia."""
    c = np.array(concordia_point(age_ma, space=space), dtype=float)
    residual = xy - c
    return float(0.5 * np.einsum("ni,nij,nj->", residual, inv_cov, residual))


def concordia_age_curvature(age_ma, xy, inv_cov, space):
    """Return likelihood curvature used to estimate concordia-age uncertainty."""
    c = np.array(concordia_point(age_ma, space=space), dtype=float)
    dc = concordia_curve_derivative(age_ma, space)
    d2c = concordia_curve_second_derivative(age_ma, space)
    residual = xy - c
    curvature_terms = []
    for r, omega in zip(residual, inv_cov):
        curvature_terms.append(dc @ omega @ dc - r @ omega @ d2c)
    return float(np.sum(curvature_terms))


def concordia_age(
    x,
    y,
    sx,
    sy,
    rho=0.0,
    *,
    space="wetherill",
    age_bounds=(0.001, EARTH_AGE_MA),
    inflate_mswd=False,
) -> ConcordiaAgeResult:
    """Fit a common concordia age to multiple analyses.

    Parameters
    ----------
    x, y
        Measured concordia coordinates.
    sx, sy, rho
        Absolute one-sigma uncertainties and their error correlation.
    space
        Wetherill or Tera-Wasserburg diagram.
    age_bounds
        Positive bounded search interval in Ma.
    inflate_mswd
        Multiply the internal age uncertainty by ``sqrt(MSWD)`` when MSWD is
        greater than one.

    Returns
    -------
    ConcordiaAgeResult
        Maximum-likelihood age, uncertainty, concordia coordinates, and
        goodness-of-fit statistics.

    Notes
    -----
    The fit minimizes the summed Mahalanobis distance between measurements and
    concordia. Age uncertainty is the inverse square root of the one-parameter
    likelihood curvature.
    """
    key = normalise_space(space)
    x, y, sx, sy, rho = as_arrays(x, y, sx, sy, rho, min_points=1)
    xy = np.column_stack([x, y])
    inv_cov = covariance_inverses(sx, sy, rho)
    lower, upper = map(float, age_bounds)
    if lower <= 0 or upper <= lower:
        raise ValueError("age_bounds must be positive and increasing.")

    objective = lambda age: concordia_age_objective(age, xy, inv_cov, key)
    fit = minimize_scalar(objective, bounds=(lower, upper), method="bounded")
    age = float(fit.x)
    sse = float(2.0 * fit.fun)
    df = max(2 * x.size - 1, 1)
    mswd = sse / df
    p_value = float(1.0 - chi2.cdf(sse, df))
    curvature = concordia_age_curvature(age, xy, inv_cov, key)
    sigma = np.nan if curvature <= 0 else np.sqrt(1.0 / curvature)
    if inflate_mswd and np.isfinite(sigma) and mswd > 1.0:
        sigma *= np.sqrt(mswd)
    cx, cy = concordia_point(age, space=key)
    return ConcordiaAgeResult(
        age_ma=age,
        sigma_ma=float(sigma),
        x=cx,
        y=cy,
        mswd=float(mswd),
        p_value=p_value,
        df=int(df),
        n=int(x.size),
        space=key,
    )


def concordia_age_from_frame(
    data,
    *,
    space="wetherill",
    x_col: str | None = None,
    y_col: str | None = None,
    sx_col: str | None = None,
    sy_col: str | None = None,
    rho_col: str | None = None,
    uncertainties_are_2sigma=True,
    **kwargs,
) -> ConcordiaAgeResult:
    """Fit a concordia age from canonical UPbRPy DataFrame columns.

    Default columns are chosen from ``space``. Input uncertainties are treated
    as two-sigma by default and divided by two before fitting.
    """
    import pandas as pd

    data = pd.DataFrame(data)
    key = normalise_space(space)
    if key == "wetherill":
        x_col = x_col or "207Pb/235U"
        y_col = y_col or "206Pb/238U"
        sx_col = sx_col or "207Pb/235U_2e"
        sy_col = sy_col or "206Pb/238U_2e"
        rho_col = rho_col or "err_corr"
    else:
        x_col = x_col or "238U/206Pb"
        y_col = y_col or "207Pb/206Pb"
        sx_col = sx_col or "238U/206Pb_2e"
        sy_col = sy_col or "207Pb/206Pb_2e"
        rho_col = rho_col or "err_corr"
    divisor = 2.0 if uncertainties_are_2sigma else 1.0
    rho = data[rho_col].to_numpy(float) if rho_col in data else 0.0
    return concordia_age(
        data[x_col].to_numpy(float),
        data[y_col].to_numpy(float),
        data[sx_col].to_numpy(float) / divisor,
        data[sy_col].to_numpy(float) / divisor,
        rho,
        space=key,
        **kwargs,
    )


def find_intersection_ages(line: LineFit, space: str, max_age_ma=EARTH_AGE_MA):
    """Find unique line-concordia intersections over the Earth-age interval.

    A dense age grid brackets sign changes, after which Brent's method refines
    each root. Near-duplicate roots are collapsed at ``1e-4`` Ma.
    """
    grid = np.linspace(0.001, max_age_ma, 1200)
    residual = np.array([line_concordia_residual(t, line, space) for t in grid])
    roots: list[float] = []
    for left, right, f_left, f_right in zip(grid[:-1], grid[1:], residual[:-1], residual[1:]):
        if not np.isfinite(f_left + f_right):
            continue
        if f_left == 0:
            roots.append(float(left))
        elif f_left * f_right < 0:
            try:
                roots.append(float(brentq(line_concordia_residual, left, right, args=(line, space))))
            except ValueError:
                pass
    unique: list[float] = []
    for root in roots:
        if not unique or abs(root - unique[-1]) > 1e-4:
            unique.append(root)
    return unique


def concordia_intercepts(line: LineFit, *, space="wetherill") -> list[AgeIntercept]:
    """Find line-concordia intersections and propagate line covariance.

    The implicit-function gradient maps intercept/slope covariance into an age
    variance using the derivative of the line-concordia residual.
    """
    key = normalise_space(space)
    roots = find_intersection_ages(line, key)
    out: list[AgeIntercept] = []
    for i, age in enumerate(roots):
        x, y = concordia_point(age, space=key)
        dres_dt = line_concordia_derivative(age, line, key)
        grad = np.array([1.0 / dres_dt, x / dres_dt])
        variance = float(grad @ line.cov @ grad.T)
        sigma = np.sqrt(max(variance, 0.0))
        if key == "tera_wasserburg":
            kind = "lower" if i == 0 else "upper"
        elif len(roots) == 1:
            kind = "intercept"
        else:
            kind = "lower" if i == 0 else "upper"
        out.append(AgeIntercept(float(age), float(sigma), x, y, kind))
    return out


def fit_discordia(
    x,
    y,
    sx,
    sy,
    rho=0.0,
    *,
    space="wetherill",
    anchor_point: tuple[float, float] | None = None,
    anchor_age_ma: float | None = None,
    anchor_cov: np.ndarray | None = None,
    inflate_mswd=False,
) -> tuple[LineFit, list[AgeIntercept]]:
    """Fit an anchored or unanchored discordia and return its intersections.

    ``anchor_point`` accepts a fixed diagram coordinate, while
    ``anchor_age_ma`` anchors directly to concordia. Supplying neither uses an
    unconstrained York fit.
    """
    key = normalise_space(space)
    if anchor_point is not None and anchor_age_ma is not None:
        raise ValueError("Use either anchor_point or anchor_age_ma, not both.")
    if anchor_cov is not None and anchor_point is None and anchor_age_ma is None:
        raise ValueError("anchor_cov requires anchor_point or anchor_age_ma.")
    if anchor_age_ma is not None:
        anchor_point = concordia_point(anchor_age_ma, space=key)
    if anchor_point is None:
        line = york_fit(x, y, sx, sy, rho, inflate_mswd=inflate_mswd)
    else:
        line = anchored_york_fit(
            x,
            y,
            sx,
            sy,
            rho,
            anchor=anchor_point,
            anchor_cov=anchor_cov,
            inflate_mswd=inflate_mswd,
        )
    return line, concordia_intercepts(line, space=key)


def fit_discordia_from_frame(
    data,
    *,
    space="wetherill",
    x_col: str | None = None,
    y_col: str | None = None,
    sx_col: str | None = None,
    sy_col: str | None = None,
    rho_col: str | None = None,
    uncertainties_are_2sigma=True,
    **kwargs,
):
    """Fit discordia from canonical UPbRPy ratio and uncertainty columns."""
    import pandas as pd

    data = pd.DataFrame(data)
    key = normalise_space(space)
    if key == "wetherill":
        x_col = x_col or "207Pb/235U"
        y_col = y_col or "206Pb/238U"
        sx_col = sx_col or "207Pb/235U_2e"
        sy_col = sy_col or "206Pb/238U_2e"
        rho_col = rho_col or "err_corr"
    else:
        x_col = x_col or "238U/206Pb"
        y_col = y_col or "207Pb/206Pb"
        sx_col = sx_col or "238U/206Pb_2e"
        sy_col = sy_col or "207Pb/206Pb_2e"
        rho_col = rho_col or "err_corr"
    divisor = 2.0 if uncertainties_are_2sigma else 1.0
    rho = data[rho_col].to_numpy(float) if rho_col in data else 0.0
    return fit_discordia(
        data[x_col].to_numpy(float),
        data[y_col].to_numpy(float),
        data[sx_col].to_numpy(float) / divisor,
        data[sy_col].to_numpy(float) / divisor,
        rho,
        space=key,
        **kwargs,
    )


def tw_single_sweep_intercept(
    u238_pb206,
    pb207_pb206,
    sigma_u238_pb206,
    sigma_pb207_pb206,
    rho=0.0,
    *,
    initial_pb207_pb206: float,
    sigma_initial_pb207_pb206: float = 0.0,
) -> SingleSweepResult:
    """Fit one TW sweep through assumed initial Pb and get lower age.

    Parameters are one measured TW point, its one-sigma uncertainties and
    correlation, plus an initial-Pb ``207Pb/206Pb`` anchor and uncertainty.

    The assumed initial composition is the Tera-Wasserburg point
    ``(238U/206Pb = 0, 207Pb/206Pb = initial_pb207_pb206)``.
    """
    point_cov = np.array(
        [
            [sigma_u238_pb206**2, rho * sigma_u238_pb206 * sigma_pb207_pb206],
            [rho * sigma_u238_pb206 * sigma_pb207_pb206, sigma_pb207_pb206**2],
        ],
        dtype=float,
    )
    anchor_cov = np.array(
        [[0.0, 0.0], [0.0, sigma_initial_pb207_pb206**2]],
        dtype=float,
    )
    line = line_from_anchor_and_point(
        point=(u238_pb206, pb207_pb206),
        point_cov=point_cov,
        anchor=(0.0, initial_pb207_pb206),
        anchor_cov=anchor_cov,
    )
    intercepts = concordia_intercepts(line, space="tw")
    lower = intercepts[0] if intercepts else None
    return SingleSweepResult(line=line, lower_intercept=lower)


def single_sweep_mc_summary(
    u238_pb206,
    pb207_pb206,
    sigma_u238_pb206,
    sigma_pb207_pb206,
    rho,
    initial_pb207_pb206,
    sigma_initial_pb207_pb206,
    *,
    n,
    confidence,
    rng,
):
    """Monte Carlo-propagate one anchored TW sweep to a lower-intercept age.

    Measurement coordinates are drawn from their correlated bivariate normal
    distribution and the initial-Pb anchor is drawn independently. The result
    reports a median and central asymmetric confidence interval.
    """
    point_cov = np.array(
        [
            [sigma_u238_pb206**2, rho * sigma_u238_pb206 * sigma_pb207_pb206],
            [rho * sigma_u238_pb206 * sigma_pb207_pb206, sigma_pb207_pb206**2],
        ],
        dtype=float,
    )
    point_draws = rng.multivariate_normal([u238_pb206, pb207_pb206], point_cov, size=n)
    if sigma_initial_pb207_pb206 > 0:
        anchor_draws = rng.normal(initial_pb207_pb206, sigma_initial_pb207_pb206, size=n)
    else:
        anchor_draws = np.full(n, initial_pb207_pb206, dtype=float)

    ages = np.full(n, np.nan)
    for i, (point_draw, anchor_y) in enumerate(zip(point_draws, anchor_draws)):
        try:
            line = line_from_anchor_and_point(
                point=(point_draw[0], point_draw[1]),
                point_cov=np.zeros((2, 2)),
                anchor=(0.0, anchor_y),
            )
        except ValueError:
            continue
        roots = find_intersection_ages(line, "tera_wasserburg")
        if roots:
            ages[i] = roots[0]

    finite = ages[np.isfinite(ages)]
    if finite.size == 0:
        return {
            "median": np.nan,
            "minus": np.nan,
            "plus": np.nan,
            "n_finite": 0,
        }
    alpha = (1.0 - confidence) / 2.0
    lo, med, hi = np.percentile(finite, [100 * alpha, 50.0, 100 * (1.0 - alpha)])
    return {
        "median": float(med),
        "minus": float(med - lo),
        "plus": float(hi - med),
        "n_finite": int(finite.size),
    }


def tw_depth_profile_sweep_ages(
    data,
    *,
    initial_pb207_pb206: float | None = None,
    sigma_initial_pb207_pb206: float = 0.0,
    initial_pb207_pb206_col: str | None = None,
    sigma_initial_pb207_pb206_col: str | None = None,
    x_col="238U/206Pb_fractionation_corrected",
    y_col="207Pb/206Pb_fractionation_corrected",
    sx_col="238U/206Pb_fractionation_corrected_2e",
    sy_col="207Pb/206Pb_fractionation_corrected_2e",
    rho_col: str | None = None,
    uncertainties_are_2sigma=True,
    initial_uncertainties_are_2sigma=False,
    monte_carlo_n: int = 0,
    monte_carlo_confidence: float = 0.95,
    random_state=None,
):
    """Calculate sweep-wise TW lower-intercept ages for a depth profile.

    Each row is treated as a single Tera-Wasserburg isochron line from an
    assumed initial ``207Pb/206Pb`` composition at ``x=0`` to the measured
    sweep point. The initial composition can be a scalar or a per-row column.
    ``uncertainties_are_2sigma`` describes the sweep-ratio uncertainty
    columns. ``initial_uncertainties_are_2sigma`` independently describes the
    TIMS/common-Pb uncertainty, regardless of whether it is supplied as a
    scalar or column. Both are converted to one-sigma before propagation.

    This is the single-analysis special case of an anchored total-Pb/U
    isochron. IsoplotR's Ludwig regression treats an uncertain common-Pb
    composition as a Gaussian constraint; here the same constraint is
    propagated through the two-point line analytically, with optional Monte
    Carlo verification.

    The returned object is a copy of ``data`` with line and age columns
    appended. Ages and analytical uncertainties are in Ma.
    """
    import pandas as pd

    frame = pd.DataFrame(data).copy()
    if initial_pb207_pb206_col is None and initial_pb207_pb206 is None:
        raise ValueError("Provide initial_pb207_pb206 or initial_pb207_pb206_col.")
    if initial_pb207_pb206_col is not None and initial_pb207_pb206_col not in frame.columns:
        raise ValueError(f"Missing initial Pb column: {initial_pb207_pb206_col}")
    if sigma_initial_pb207_pb206_col is not None and sigma_initial_pb207_pb206_col not in frame.columns:
        raise ValueError(f"Missing initial Pb uncertainty column: {sigma_initial_pb207_pb206_col}")

    divisor = 2.0 if uncertainties_are_2sigma else 1.0
    initial_divisor = 2.0 if initial_uncertainties_are_2sigma else 1.0
    rng = np.random.default_rng(random_state)

    result_columns = {
        "tw_initial_207Pb_206Pb": [],
        "tw_initial_207Pb_206Pb_1s": [],
        "tw_line_slope": [],
        "tw_line_intercept": [],
        "tw_lower_intercept_age": [],
        "tw_lower_intercept_age_1s": [],
        "tw_lower_intercept_age_2s": [],
        "tw_lower_intercept_238U_206Pb": [],
        "tw_lower_intercept_207Pb_206Pb": [],
        "tw_lower_intercept_mc_median": [],
        "tw_lower_intercept_mc_minus": [],
        "tw_lower_intercept_mc_plus": [],
        "tw_lower_intercept_mc_n": [],
    }

    for _, row in frame.iterrows():
        x = float(row[x_col])
        y = float(row[y_col])
        sx = float(row[sx_col]) / divisor
        sy = float(row[sy_col]) / divisor
        rho = float(row[rho_col]) if rho_col and rho_col in frame.columns else 0.0
        initial = (
            float(row[initial_pb207_pb206_col])
            if initial_pb207_pb206_col is not None
            else float(initial_pb207_pb206)
        )
        sigma_initial = (
            float(row[sigma_initial_pb207_pb206_col]) / initial_divisor
            if sigma_initial_pb207_pb206_col is not None
            else float(sigma_initial_pb207_pb206) / initial_divisor
        )

        for key in result_columns:
            result_columns[key].append(np.nan)
        result_columns["tw_initial_207Pb_206Pb"][-1] = initial
        result_columns["tw_initial_207Pb_206Pb_1s"][-1] = sigma_initial

        if not np.all(np.isfinite([x, y, sx, sy, rho, initial, sigma_initial])):
            continue
        if x <= 0 or sx <= 0 or sy <= 0 or sigma_initial < 0 or abs(rho) >= 1:
            continue
        try:
            sweep = tw_single_sweep_intercept(
                x,
                y,
                sx,
                sy,
                rho,
                initial_pb207_pb206=initial,
                sigma_initial_pb207_pb206=sigma_initial,
            )
        except ValueError:
            continue

        result_columns["tw_line_slope"][-1] = sweep.line.slope
        result_columns["tw_line_intercept"][-1] = sweep.line.intercept
        if sweep.lower_intercept is not None:
            age = sweep.lower_intercept
            result_columns["tw_lower_intercept_age"][-1] = age.age_ma
            result_columns["tw_lower_intercept_age_1s"][-1] = age.sigma_ma
            result_columns["tw_lower_intercept_age_2s"][-1] = age.two_sigma_ma
            result_columns["tw_lower_intercept_238U_206Pb"][-1] = age.x
            result_columns["tw_lower_intercept_207Pb_206Pb"][-1] = age.y

        if monte_carlo_n and monte_carlo_n > 0:
            mc = single_sweep_mc_summary(
                x,
                y,
                sx,
                sy,
                rho,
                initial,
                sigma_initial,
                n=monte_carlo_n,
                confidence=monte_carlo_confidence,
                rng=rng,
            )
            result_columns["tw_lower_intercept_mc_median"][-1] = mc["median"]
            result_columns["tw_lower_intercept_mc_minus"][-1] = mc["minus"]
            result_columns["tw_lower_intercept_mc_plus"][-1] = mc["plus"]
            result_columns["tw_lower_intercept_mc_n"][-1] = mc["n_finite"]

    for key, values in result_columns.items():
        frame[key] = values
    return frame


def monte_carlo_discordia_intercepts(
    x,
    y,
    sx,
    sy,
    rho=0.0,
    *,
    space="wetherill",
    n=10000,
    anchor_point: tuple[float, float] | None = None,
    anchor_age_ma: float | None = None,
    anchor_cov: np.ndarray | None = None,
    random_state=None,
) -> MonteCarloIntercepts:
    """Resample data ellipses, refit lines, and collect intercept ages.

    This follows the simple Isoplot.jl-style idea: each analysis is sampled
    from its 2D covariance ellipse, an ordinary least-squares line is fit to
    each resampled dataset, and each line is intersected with concordia.
    """
    key = normalise_space(space)
    x, y, sx, sy, rho = as_arrays(x, y, sx, sy, rho)
    if anchor_point is not None and anchor_age_ma is not None:
        raise ValueError("Use either anchor_point or anchor_age_ma, not both.")
    if anchor_cov is not None and anchor_point is None and anchor_age_ma is None:
        raise ValueError("anchor_cov requires anchor_point or anchor_age_ma.")
    if anchor_age_ma is not None:
        anchor_point = concordia_point(anchor_age_ma, space=key)
    if anchor_cov is not None:
        anchor_cov = np.asarray(anchor_cov, dtype=float)
        if anchor_cov.shape != (2, 2):
            raise ValueError("anchor_cov must be a 2 by 2 covariance matrix ordered as x,y.")

    rng = np.random.default_rng(random_state)
    draws = np.empty((x.size, n, 2), dtype=float)
    for i in range(x.size):
        cov = np.array([[sx[i] ** 2, rho[i] * sx[i] * sy[i]], [rho[i] * sx[i] * sy[i], sy[i] ** 2]])
        draws[i] = rng.multivariate_normal([x[i], y[i]], cov, size=n)

    lower = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    line_intercepts = np.full(n, np.nan)
    line_slopes = np.full(n, np.nan)
    anchor_draws = None
    if anchor_point is not None:
        if anchor_cov is None:
            anchor_draws = np.tile(np.asarray(anchor_point, dtype=float), (n, 1))
        else:
            anchor_draws = rng.multivariate_normal(anchor_point, anchor_cov, size=n)
    for j in range(n):
        xj = draws[:, j, 0]
        yj = draws[:, j, 1]
        if anchor_point is None:
            slope, intercept = np.polyfit(xj, yj, 1)
        else:
            x0, y0 = anchor_draws[j]
            slope = np.linalg.lstsq((xj - x0)[:, None], yj - y0, rcond=None)[0][0]
            intercept = y0 - slope * x0
        line = LineFit(float(intercept), float(slope), np.zeros((2, 2)), np.nan, np.nan, 0)
        roots = find_intersection_ages(line, key)
        line_intercepts[j] = intercept
        line_slopes[j] = slope
        if len(roots) >= 1:
            lower[j] = roots[0]
        if len(roots) >= 2:
            upper[j] = roots[-1]
    return MonteCarloIntercepts(lower, upper, line_intercepts, line_slopes)


__all__ = [
    "LineFit",
    "AgeIntercept",
    "MonteCarloIntercepts",
    "SingleSweepResult",
    "ConcordiaAgeResult",
    "CommonPbAgeResult",
    "LAMBDA_232",
    "DEFAULT_208_MAX_TH_U",
    "stacey_kramers_common_pb",
    "solve_common_pb_age_equation",
    "age_207_corrected",
    "age_207_corrected_uncertainty",
    "age_208_corrected",
    "age_208_corrected_uncertainty",
    "calculate_common_pb_result",
    "apply_204pb_common_pb_correction",
    "apply_common_pb_corrections",
    "line_fit_summary",
    "intercept_summary",
    "fit_summary",
    "concordia_age_summary",
    "monte_carlo_summary_frame",
    "wetherill_point",
    "tera_wasserburg_point",
    "york_fit",
    "anchored_york_fit",
    "anchored_concordia_fit",
    "line_from_anchor_and_point",
    "concordia_point",
    "concordia_age",
    "concordia_age_from_frame",
    "concordia_intercepts",
    "fit_discordia",
    "fit_discordia_from_frame",
    "tw_single_sweep_intercept",
    "tw_depth_profile_sweep_ages",
    "monte_carlo_discordia_intercepts",
    "as_arrays",
    "covariance_inverses",
    "line_negloglike_slope",
    "second_derivative",
    "normalise_space",
    "line_concordia_residual",
    "line_concordia_derivative",
    "concordia_curve_derivative",
    "concordia_curve_second_derivative",
    "concordia_age_objective",
    "concordia_age_curvature",
    "find_intersection_ages",
    "single_sweep_mc_summary",
]
