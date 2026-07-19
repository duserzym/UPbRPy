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

from .constants import common_U238_U235, lambda_235, lambda_238

U238_U235 = common_U238_U235
LAMBDA_235 = lambda_235
LAMBDA_238 = lambda_238
EARTH_AGE_MA = 4567.0


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
        return float(np.sqrt(self.cov[0, 0]))

    @property
    def slope_sigma(self) -> float:
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
        return 2.0 * self.sigma_ma


@dataclass
class MonteCarloIntercepts:
    """Monte Carlo concordia-intercept age distributions."""

    lower_ma: np.ndarray
    upper_ma: np.ndarray
    line_intercepts: np.ndarray
    line_slopes: np.ndarray

    def summary(self, confidence: float = 0.95) -> dict[str, dict[str, float]]:
        """Return median and asymmetric confidence limits for each intercept."""
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
        return 2.0 * self.sigma_ma


def line_fit_summary(line: LineFit, *, prefix: str = "line") -> dict[str, float | bool]:
    """Return a flat dictionary of line-fit parameters and diagnostics."""
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
    """Return a flat dictionary of concordia-intercept age results."""
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
    """Return a single flat summary for a line fit and its intercept ages."""
    out = line_fit_summary(line)
    out.update(intercept_summary(intercepts))
    return out


def concordia_age_summary(result: ConcordiaAgeResult, *, prefix: str = "concordia") -> dict[str, float | int | str]:
    """Return a flat dictionary for common concordia-age results."""
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
    """Return a DataFrame summary of Monte Carlo intercept distributions."""
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
    """Return Wetherill coordinates: 207Pb/235U, 206Pb/238U."""
    age_years = np.asarray(age_ma, dtype=float) * 1e6
    return np.exp(lambda_235 * age_years) - 1.0, np.exp(lambda_238 * age_years) - 1.0


def tera_wasserburg_point(
    age_ma,
    lambda_235=LAMBDA_235,
    lambda_238=LAMBDA_238,
    u238_u235=U238_U235,
):
    """Return Tera-Wasserburg coordinates: 238U/206Pb, 207Pb/206Pb."""
    pb207_u235, pb206_u238 = wetherill_point(age_ma, lambda_235, lambda_238)
    return 1.0 / pb206_u238, pb207_u235 / pb206_u238 / u238_u235


def as_arrays(x, y, sx, sy, rho, *, min_points=2):
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
    """Fit ``y = intercept + slope*x`` with the York et al. (2004) algorithm."""
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
    x0, y0 = anchor
    residual = y - y0 - slope * (x - x0)
    variance = sy**2 + slope**2 * sx**2 - 2.0 * slope * rho * sx * sy
    if np.any(variance <= 0):
        return np.inf
    return 0.5 * np.sum(np.log(variance) + residual**2 / variance)


def second_derivative(fun, value):
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
    """Fit a York-style line constrained through a fixed ``(x, y)`` anchor."""
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
    """Fit a line anchored to concordia at ``age_ma``."""
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
    """Return concordia coordinates for ``space``."""
    key = normalise_space(space)
    if key == "wetherill":
        x, y = wetherill_point(age_ma)
    else:
        x, y = tera_wasserburg_point(age_ma)
    return float(np.asarray(x)), float(np.asarray(y))


def normalise_space(space: str) -> str:
    key = space.lower().replace("-", "_").replace(" ", "_")
    if key in {"wetherill", "wc"}:
        return "wetherill"
    if key in {"tw", "tera_wasserburg", "terawasserburg"}:
        return "tera_wasserburg"
    raise ValueError("space must be 'wetherill' or 'tera_wasserburg'.")


def line_concordia_residual(age_ma, line: LineFit, space: str):
    x, y = concordia_point(age_ma, space=space)
    return y - line.intercept - line.slope * x


def line_concordia_derivative(age_ma, line: LineFit, space: str):
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
    step = max(abs(age_ma), 1.0) * 1e-5
    return (
        concordia_curve_derivative(age_ma + step, space)
        - concordia_curve_derivative(age_ma - step, space)
    ) / (2.0 * step)


def concordia_age_objective(age_ma, xy, inv_cov, space):
    c = np.array(concordia_point(age_ma, space=space), dtype=float)
    residual = xy - c
    return float(0.5 * np.einsum("ni,nij,nj->", residual, inv_cov, residual))


def concordia_age_curvature(age_ma, xy, inv_cov, space):
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

    The fit minimizes the summed Mahalanobis distance between the measured
    points and the concordia curve. The age uncertainty is the first-order
    curvature uncertainty of the one-parameter likelihood.
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
    """DataFrame wrapper for :func:`concordia_age` using UPbRPy columns."""
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
    """Find line-concordia intersections and propagate line covariance."""
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
    """Fit a discordia line and return its concordia intersections."""
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
    """DataFrame convenience wrapper for UPbRPy ratio tables."""
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
    monte_carlo_n: int = 0,
    monte_carlo_confidence: float = 0.95,
    random_state=None,
):
    """Calculate sweep-wise TW lower-intercept ages for a depth profile.

    Each row is treated as a single Tera-Wasserburg isochron line from an
    assumed initial ``207Pb/206Pb`` composition at ``x=0`` to the measured
    sweep point. The initial composition can be a scalar or a per-row column.
    The returned object is a copy of ``data`` with line and age columns
    appended.
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
    rng = np.random.default_rng(random_state)

    result_columns = {
        "tw_initial_207Pb_206Pb": [],
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
            float(row[sigma_initial_pb207_pb206_col]) / divisor
            if sigma_initial_pb207_pb206_col is not None
            else float(sigma_initial_pb207_pb206)
        )

        for key in result_columns:
            result_columns[key].append(np.nan)
        result_columns["tw_initial_207Pb_206Pb"][-1] = initial

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
