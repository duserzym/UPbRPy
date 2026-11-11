"""Discordia and isochron regression of correlated-error U-Pb data.

The module fits straight lines through U-Pb analyses with correlated x-y
uncertainties in Tera-Wasserburg space (``238U/206Pb``, ``207Pb/206Pb``) or in
Wetherill space (``207Pb/235U``, ``206Pb/238U``) and converts the line into
concordia-intercept ages and, for Tera-Wasserburg, the common
``207Pb/206Pb`` intercept.

Methods
-------
* Model 1 (default): maximum-likelihood line with the analytical
  uncertainties only. Unanchored fits use the York et al. (2004) iteration and
  its closed-form parameter covariance. Following IsoplotR (Vermeesch 2018),
  the covariance is multiplied by the MSWD when the chi-square probability is
  below ``alpha`` (0.05 by default); both versions are kept.
* Model 2: total least squares (orthogonal regression) that ignores the
  analytical uncertainties; the scatter of the perpendicular residuals sets
  the parameter uncertainties (IsoplotR model 2).
* Model 3: maximum likelihood with an added dispersion term estimated with
  the line (IsoplotR model 3, after Vermeesch 2018 and Ludwig 2003). For
  unanchored and age-anchored fits the dispersion is in the concordia
  (radiogenic end-member) age, in Ma; for fits anchored to a fixed common
  ``207Pb/206Pb`` it is in that common-Pb ratio.
* Anchors: through a fixed common ``207Pb/206Pb`` (optionally with an
  uncertainty, used as a Gaussian prior), through Stacey and Kramers (1975)
  common Pb evaluated at the fitted age (solved jointly with the line), or
  through a fixed concordia age (optionally with an uncertainty).

Ages are solved where the line meets concordia. Their uncertainties are
propagated to first order from the line-parameter covariance by implicit
differentiation of ``a + b X(t) - Y(t) = 0``; decay-constant (and, in
Tera-Wasserburg, ``238U/235U``) uncertainties are added on request.

:func:`bootstrap_discordia` gives distributions of the same quantities from
resampled data, vectorised over all resamples (batched York iterations with a
convergence mask and batched concordia root finding).

All uncertainties passed in and returned are one standard deviation
(absolute) unless a name says otherwise. Ages are in Ma.

References
----------
York, D., Evensen, N. M., Martinez, M. L. and De Basabe Delgado, J. (2004)
  Unified equations for the slope, intercept, and standard errors of the best
  straight line. American Journal of Physics 72, 367-375.
Ludwig, K. R. (1998) On the treatment of concordant uranium-lead ages.
  Geochimica et Cosmochimica Acta 62, 665-676.
Ludwig, K. R. (2003) Isoplot 3.00. Berkeley Geochronology Center Special
  Publication 4.
Stacey, J. S. and Kramers, J. D. (1975) Approximation of terrestrial lead
  isotope evolution by a two-stage model. Earth and Planetary Science Letters
  26, 207-221.
Vermeesch, P. (2018) IsoplotR: a free and open toolbox for geochronology.
  Geoscience Frontiers 9, 1479-1493.
Vermeesch, P. (2020) Unifying the U-Pb and Th-Pb methods: joint isochron
  regression and common Pb correction. Geochronology 2, 119-131.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import warnings
from typing import Iterable

import numpy as np
from scipy.optimize import brentq, minimize
from scipy.stats import chi2

from .constants import (
    common_U238_U235,
    lambda_235,
    lambda_238,
    sigma_235,
    sigma_238,
    sigma_common_U238_U235,
)

__all__ = [
    "Anchor",
    "BootstrapResult",
    "DecayConstants",
    "DiscordiaFit",
    "InterceptAge",
    "LineFit",
    "bootstrap_discordia",
    "concordia_xy",
    "fit_discordia",
    "intersect_concordia",
    "york_fit",
    "york_fit_batch",
    "autocorrelation_inflation",
    "fit_record",
    "stacey_kramers_76",
    "isochron_fit_table",
    "isochron_footnotes",
    "write_isochron_fits",
]

EARTH_AGE_MA = 4567.3
# Intersections are searched up to this age; older ones are flagged.
MAX_AGE_MA = 5000.0
SPACES = ("tera_wasserburg", "wetherill")
MODELS = (1, 2, 3)


# ---------------------------------------------------------------------------
# Constants and concordia
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecayConstants:
    """Decay constants per Ma and the 238U/235U ratio, with one-sigma errors.

    The defaults are the UPbRPy package constants (Jaffey et al. 1971 decay
    constants; 238U/235U = 137.818 +- 0.045 (2 s), Hiess et al. 2012).
    """

    lambda_238: float = lambda_238 * 1e6
    lambda_235: float = lambda_235 * 1e6
    u238_u235: float = common_U238_U235
    sigma_238: float = sigma_238 * 1e6
    sigma_235: float = sigma_235 * 1e6
    sigma_u238_u235: float = sigma_common_U238_U235

    def covariance(self) -> np.ndarray:
        """Return the diagonal covariance of (lambda_238, lambda_235, 238U/235U)."""
        return np.diag([self.sigma_238 ** 2, self.sigma_235 ** 2, self.sigma_u238_u235 ** 2])

    def describe(self) -> str:
        """Return a one-line description for footnotes."""
        return (f"lambda238 = {self.lambda_238 * 1e-6:.6g} /a, lambda235 = {self.lambda_235 * 1e-6:.6g} /a, "
                f"238U/235U = {self.u238_u235:g}")


DEFAULT_DECAY = DecayConstants()


def _normalise_space(space: str) -> str:
    key = str(space).lower().replace("-", "_").replace(" ", "_")
    if key in {"tw", "tera_wasserburg", "terawasserburg"}:
        return "tera_wasserburg"
    if key in {"wetherill", "wc", "w"}:
        return "wetherill"
    raise ValueError("space must be 'tera_wasserburg' or 'wetherill'")


def concordia_xy(age_ma, space: str, decay: DecayConstants = DEFAULT_DECAY,
                 *, l8=None, l5=None, u=None):
    """Return concordia coordinates at ``age_ma`` in the requested space.

    ``l8``, ``l5`` and ``u`` override the decay constants (per Ma) and the
    238U/235U ratio; they may be arrays that broadcast with ``age_ma``.
    """
    space = _normalise_space(space)
    t = np.asarray(age_ma, dtype=float)
    l8 = decay.lambda_238 if l8 is None else l8
    l5 = decay.lambda_235 if l5 is None else l5
    u = decay.u238_u235 if u is None else u
    y68 = np.expm1(l8 * t)
    x75 = np.expm1(l5 * t)
    if space == "wetherill":
        return x75, y68
    with np.errstate(divide="ignore", invalid="ignore"):
        return 1.0 / y68, x75 / (y68 * u)


def _concordia_derivatives(t, space, l8, l5, u):
    """Return d(X, Y)/dt and d(X, Y)/d(l8, l5, u) at age t (arrays broadcast)."""
    e8 = np.exp(l8 * t)
    e5 = np.exp(l5 * t)
    m8 = e8 - 1.0
    m5 = e5 - 1.0
    zero = np.zeros_like(m8)
    if space == "wetherill":
        return {
            "t": (l5 * e5, l8 * e8),
            "l8": (zero, t * e8),
            "l5": (t * e5, zero),
            "u": (zero, zero),
        }
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        dXdt = -l8 * e8 / m8 ** 2
        dYdt = (l5 * e5 * m8 - m5 * l8 * e8) / (m8 ** 2 * u)
        return {
            "t": (dXdt, dYdt),
            "l8": (-t * e8 / m8 ** 2, -m5 * t * e8 / (m8 ** 2 * u)),
            "l5": (zero, t * e5 / (m8 * u)),
            "u": (zero, -m5 / (m8 * u ** 2)),
        }


def stacey_kramers_76(age_ma, decay: DecayConstants = DEFAULT_DECAY) -> np.ndarray:
    """Return the Stacey and Kramers (1975) common 207Pb/206Pb at ``age_ma``.

    The two-stage model uses its published parameters (mu = 7.19 then 9.74,
    238U/235U = 137.88 inside the model) and the supplied decay constants.
    """
    t = np.asarray(age_ma, dtype=float)
    l8, l5 = decay.lambda_238, decay.lambda_235
    t0, t1 = 4570.0, 3700.0
    older = t > t1
    u_model = 137.88
    r64 = np.where(older, 9.307 + 7.19 * (np.exp(l8 * t0) - np.exp(l8 * t)),
                   11.152 + 9.74 * (np.exp(l8 * t1) - np.exp(l8 * t)))
    r74 = np.where(older, 10.294 + 7.19 / u_model * (np.exp(l5 * t0) - np.exp(l5 * t)),
                   12.998 + 9.74 / u_model * (np.exp(l5 * t1) - np.exp(l5 * t)))
    return r74 / r64


# ---------------------------------------------------------------------------
# Data handling
# ---------------------------------------------------------------------------


def _clean(x, y, sx, sy, rho):
    """Broadcast inputs and return arrays plus the mask of usable rows."""
    x = np.asarray(x, dtype=float).ravel()
    n = x.size
    y = np.broadcast_to(np.asarray(y, dtype=float), (n,)).astype(float).ravel()
    sx = np.broadcast_to(np.asarray(sx, dtype=float), (n,)).astype(float).ravel()
    sy = np.broadcast_to(np.asarray(sy, dtype=float), (n,)).astype(float).ravel()
    rho = np.broadcast_to(np.nan_to_num(np.asarray(rho, dtype=float), nan=0.0), (n,)).astype(float).ravel()
    mask = (np.isfinite(x) & np.isfinite(y) & np.isfinite(sx) & np.isfinite(sy)
            & (sx > 0) & (sy > 0) & np.isfinite(rho) & (np.abs(rho) < 1))
    return x, y, sx, sy, rho, mask


@dataclass(frozen=True)
class Anchor:
    """Constraint on the fitted line.

    ``kind`` is one of:

    ``"none"``
        Free line.
    ``"common_pb"``
        Through a common 207Pb/206Pb ``value`` (the Tera-Wasserburg
        y-intercept). ``sigma`` > 0 makes the value a Gaussian prior
        (models 1 and 2) or the fixed dispersion of the common Pb (model 3),
        as in IsoplotR.
    ``"stacey_kramers"``
        Through the Stacey-Kramers common 207Pb/206Pb at the fitted lower
        intercept age; the age and the line are solved together.
    ``"age"``
        Through concordia at ``value`` Ma; ``sigma`` > 0 makes the age a
        Gaussian prior (models 1 and 2) or the fixed age dispersion (model 3).
    """

    kind: str = "none"
    value: float | None = None
    sigma: float = 0.0

    def __post_init__(self):
        if self.kind not in {"none", "common_pb", "stacey_kramers", "age"}:
            raise ValueError(f"unknown anchor kind {self.kind!r}")
        if self.kind in {"common_pb", "age"} and (self.value is None or not np.isfinite(self.value)):
            raise ValueError(f"anchor {self.kind!r} needs a finite value")
        if not np.isfinite(self.sigma) or self.sigma < 0:
            raise ValueError("anchor sigma must be finite and non-negative")

    def describe(self) -> str:
        """Return a short human-readable description."""
        if self.kind == "none":
            return "none"
        if self.kind == "stacey_kramers":
            return "Stacey-Kramers (1975) common Pb at the fitted age"
        unit = " Ma" if self.kind == "age" else ""
        what = "concordia age" if self.kind == "age" else "common 207Pb/206Pb"
        error = f" ± {self.sigma:g}{unit} (1s)" if self.sigma > 0 else ""
        return f"{what} {self.value:g}{unit}{error}"


# ---------------------------------------------------------------------------
# York regression
# ---------------------------------------------------------------------------


def york_fit_batch(x, y, sx, sy, rho, *, anchor_xy=None, tol: float = 1e-12, max_iter: int = 200):
    """Fit York lines to many data sets at once.

    Parameters
    ----------
    x, y, sx, sy, rho
        Arrays of shape ``(B, n)`` (B data sets of n points each). Rows with
        fewer points can be padded with ``sx = inf`` (zero weight).
    anchor_xy
        Optional ``(x0, y0)`` arrays of shape ``(B,)``: fit lines through that
        point instead of through the weighted centroid.
    tol, max_iter
        Relative slope tolerance and iteration limit. Iteration stops for a
        row once its slope changes by less than ``tol`` relative; the others
        continue (convergence mask).

    Returns
    -------
    intercept, slope, converged
        Arrays of shape ``(B,)``.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    sx = np.asarray(sx, dtype=float)
    sy = np.asarray(sy, dtype=float)
    rho = np.asarray(rho, dtype=float)
    x, y, sx, sy, rho = np.broadcast_arrays(x, y, sx, sy, rho)
    B = x.shape[0]
    with np.errstate(over="ignore", invalid="ignore"):
        vx = sx * sx
        vy = sy * sy
        cxy = rho * sx * sy
    # Points with infinite or invalid errors (padding) get zero weight.
    m = (np.isfinite(vx) & np.isfinite(vy) & np.isfinite(cxy) & (vx > 0) & (vy > 0)
         & np.isfinite(x) & np.isfinite(y)).astype(float)
    ok_pts = m > 0
    vx = np.where(ok_pts, vx, 1.0)
    vy = np.where(ok_pts, vy, 1.0)
    cxy = np.where(ok_pts, cxy, 0.0)
    x = np.where(ok_pts, x, 0.0)
    y = np.where(ok_pts, y, 0.0)
    if anchor_xy is not None:
        x0 = np.broadcast_to(np.asarray(anchor_xy[0], dtype=float), (B,))
        y0 = np.broadcast_to(np.asarray(anchor_xy[1], dtype=float), (B,))
        u0 = x - x0[:, None]
        v0 = y - y0[:, None]
        wy = m / vy
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = np.sum(wy * u0 * v0, axis=1) / np.sum(wy * u0 * u0, axis=1)
    else:
        # Start from the y-weighted least-squares slope.
        wy = m / vy
        sw = np.sum(wy, axis=1, keepdims=True)
        xm = np.sum(wy * x, axis=1, keepdims=True) / sw
        ym = np.sum(wy * y, axis=1, keepdims=True) / sw
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = np.sum(wy * (x - xm) * (y - ym), axis=1) / np.sum(wy * (x - xm) ** 2, axis=1)
    slope = np.where(np.isfinite(slope), slope, 0.0)
    # Per-row damping: York's fixed point can oscillate between two slopes
    # when some error correlations are close to +-1. A row whose update
    # changes sign halves its step (Isoplot.jl and IsoplotR do not damp; on
    # well-behaved data the undamped and damped iterations agree).
    omega = np.ones(B)
    last_step = np.zeros(B)
    xbar = np.zeros(B)
    ybar = np.zeros(B)
    done = np.zeros(B, dtype=bool)
    rows = np.arange(B)  # rows still iterating; arrays below are compacted to them
    X, Y, VX, VY, C, M = x, y, vx, vy, cxy, m
    if anchor_xy is not None:
        U0, V0 = u0, v0
    for _ in range(max_iter):
        b = slope[rows][:, None]
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            W = M / (VY + b * b * VX - 2.0 * b * C)
            if anchor_xy is None:
                sw = np.sum(W, axis=1)
                xb = np.sum(W * X, axis=1) / sw
                yb = np.sum(W * Y, axis=1) / sw
                xbar[rows], ybar[rows] = xb, yb
                U = X - xb[:, None]
                V = Y - yb[:, None]
            else:
                U, V = U0, V0
            beta = W * (U * VY + b * V * VX - (b * U + V) * C)
            WB = W * beta
            new = np.sum(WB * V, axis=1) / np.sum(WB * U, axis=1)
        old = slope[rows]
        step = new - old
        with np.errstate(invalid="ignore"):
            change = np.abs(step) <= tol * np.maximum(np.abs(new), 1e-300)
            flip = step * last_step[rows] < 0
        om = np.where(flip, np.maximum(0.5 * omega[rows], 1.0 / 64), omega[rows])
        omega[rows] = om
        last_step[rows] = step
        slope[rows] = np.where(change, new, old + om * step)
        finished = change | ~np.isfinite(slope[rows])
        done[rows] = change & np.isfinite(slope[rows])
        if finished.all():
            break
        if finished.any():
            keep = ~finished
            rows = rows[keep]
            X, Y, VX, VY, C, M = X[keep], Y[keep], VX[keep], VY[keep], C[keep], M[keep]
            if anchor_xy is not None:
                U0, V0 = U0[keep], V0[keep]
    if anchor_xy is not None:
        intercept = y0 - slope * x0
    else:
        intercept = ybar - slope * xbar
    converged = done & np.isfinite(slope)
    return intercept, slope, converged


@dataclass
class LineFit:
    """A fitted line ``y = intercept + slope * x`` with its diagnostics.

    ``cov`` is the covariance of ``(intercept, slope)`` from the analytical
    uncertainties (model 1), the residual scatter (model 2) or the analytical
    uncertainties plus the fitted dispersion (model 3). ``cov_reported`` is
    the matrix used for the reported uncertainties: for model 1 it is ``cov``
    multiplied by the MSWD when ``overdispersed`` is true.
    """

    intercept: float
    slope: float
    cov: np.ndarray
    mswd: float
    p_value: float
    df: int
    n: int
    space: str
    model: int = 1
    anchor: Anchor = field(default_factory=Anchor)
    dispersion: float = float("nan")
    dispersion_sigma: float = float("nan")
    dispersion_units: str = ""
    overdispersed: bool = False
    converged: bool = True
    centroid: tuple[float, float] = (float("nan"), float("nan"))
    anchored_age: float = float("nan")
    anchored_age_sigma: float = float("nan")
    cov_york: np.ndarray | None = None
    autocorrelation: float = float("nan")
    variance_inflation: float = 1.0
    n_effective: float = float("nan")
    theta_names: tuple[str, ...] = ()
    theta: np.ndarray | None = None
    theta_cov: np.ndarray | None = None

    @property
    def cov_reported(self) -> np.ndarray:
        """Return the covariance used for reported uncertainties."""
        if self.model == 1 and self.overdispersed and np.isfinite(self.mswd):
            return self.cov * self.mswd
        return self.cov

    @property
    def intercept_sigma(self) -> float:
        """One-sigma reported uncertainty of the intercept."""
        return float(np.sqrt(self.cov_reported[0, 0]))

    @property
    def slope_sigma(self) -> float:
        """One-sigma reported uncertainty of the slope."""
        return float(np.sqrt(self.cov_reported[1, 1]))


def york_fit(x, y, sx, sy, rho=0.0, *, tol: float = 1e-15, max_iter: int = 500,
             space: str = "tera_wasserburg") -> LineFit:
    """Fit a York et al. (2004) line to correlated-error data.

    Returns the line, its analytical covariance (York 2004, eqs. 13-15),
    MSWD, chi-square probability and degrees of freedom. Two points give an
    exact line with ``df = 0`` and undefined MSWD.
    """
    x, y, sx, sy, rho, mask = _clean(x, y, sx, sy, rho)
    x, y, sx, sy, rho = x[mask], y[mask], sx[mask], sy[mask], rho[mask]
    if x.size < 2:
        raise ValueError("a York fit needs at least two points with positive uncertainties")
    a, b, ok = york_fit_batch(x[None], y[None], sx[None], sy[None], rho[None], tol=tol, max_iter=max_iter)
    a, b, ok = float(a[0]), float(b[0]), bool(ok[0])
    wx, wy = 1.0 / sx ** 2, 1.0 / sy ** 2
    alpha = np.sqrt(wx * wy)
    W = wx * wy / (wx + b * b * wy - 2.0 * b * rho * alpha)
    xbar = np.sum(W * x) / np.sum(W)
    ybar = np.sum(W * y) / np.sum(W)
    U, V = x - xbar, y - ybar
    beta = W * (U / wy + b * V / wx - (b * U + V) * rho / alpha)
    xa = xbar + beta
    xabar = np.sum(W * xa) / np.sum(W)
    ua = xa - xabar
    var_b = 1.0 / np.sum(W * ua ** 2)
    var_a = 1.0 / np.sum(W) + xabar ** 2 * var_b
    cov_ab = -xabar * var_b
    cov = np.array([[var_a, cov_ab], [cov_ab, var_b]])
    S = float(np.sum(W * (y - a - b * x) ** 2))
    df = int(x.size - 2)
    mswd = S / df if df > 0 else float("nan")
    p = float(chi2.sf(S, df)) if df > 0 else float("nan")
    return LineFit(a, b, cov, mswd, p, df, int(x.size), _normalise_space(space), converged=ok,
                   centroid=(float(xbar), float(ybar)))


# ---------------------------------------------------------------------------
# Concordia intersections
# ---------------------------------------------------------------------------


def _residual(t, a, b, space, l8, l5, u):
    X, Y = concordia_xy(t, space, l8=l8, l5=l5, u=u)
    return a + b * X - Y


def _age_grid(space):
    young = np.geomspace(1e-3, 50.0, 48, endpoint=False)
    old = np.linspace(50.0, MAX_AGE_MA, 160)
    grid = np.concatenate([young, old])
    if space == "wetherill":
        grid = np.concatenate([-np.geomspace(3000.0, 1e-3, 40), [0.0], grid])
    return grid


def _roots_batch(a, b, space, l8, l5, u, *, iterations=60):
    """Return the first two concordia intersections for arrays of lines.

    Sign changes of the residual on a fixed age grid bracket the roots, which
    are refined by safeguarded Newton (bisection fallback). Returns
    ``(lower, upper)`` arrays with NaN where no root exists.
    """
    a = np.atleast_1d(np.asarray(a, dtype=float))
    b = np.atleast_1d(np.asarray(b, dtype=float))
    B = a.size
    l8 = np.broadcast_to(np.asarray(l8, dtype=float), (B,))
    l5 = np.broadcast_to(np.asarray(l5, dtype=float), (B,))
    u = np.broadcast_to(np.asarray(u, dtype=float), (B,))
    grid = _age_grid(space)
    with np.errstate(all="ignore"):
        F = _residual(grid[None, :], a[:, None], b[:, None], space, l8[:, None], l5[:, None], u[:, None])
    if space == "tera_wasserburg":
        F[:, grid <= 0] = np.nan
    sign = np.sign(F)
    change = (sign[:, :-1] * sign[:, 1:] < 0) & np.isfinite(F[:, :-1]) & np.isfinite(F[:, 1:])
    out = []
    remaining = change.copy()
    for _ in range(2):
        has = remaining.any(axis=1)
        k = np.argmax(remaining, axis=1)
        lo = grid[k].astype(float)
        hi = grid[k + 1].astype(float)
        flo = F[np.arange(B), k]
        t = 0.5 * (lo + hi)
        rows = np.arange(B)
        for _ in range(iterations):
            with np.errstate(all="ignore"):
                f = _residual(t, a, b, space, l8, l5, u)
                d = _concordia_derivatives(t, space, l8, l5, u)["t"]
                fp = b * d[0] - d[1]
                newton = t - f / fp
            same = np.sign(f) == np.sign(flo)
            lo = np.where(same, t, lo)
            flo = np.where(same, f, flo)
            hi = np.where(same, hi, t)
            inside = np.isfinite(newton) & (newton > lo) & (newton < hi)
            t_new = np.where(inside, newton, 0.5 * (lo + hi))
            done = np.abs(t_new - t) <= 1e-12 * np.maximum(1.0, np.abs(t))
            t = t_new
            if done.all():
                break
        out.append(np.where(has, t, np.nan))
        remaining[rows, k] = False
    return out[0], out[1]


def intersect_concordia(intercept: float, slope: float, space: str,
                        decay: DecayConstants = DEFAULT_DECAY) -> list[float]:
    """Return the ages (Ma, ascending) where a line meets concordia.

    Tera-Wasserburg intersections are searched for ages between 0 and
    5000 Ma; Wetherill intersections also between -3000 and 0 Ma, so a
    lower intercept below the origin is reported as a negative age.
    """
    space = _normalise_space(space)
    lower, upper = _roots_batch(intercept, slope, space, decay.lambda_238, decay.lambda_235, decay.u238_u235)
    roots = []
    for value in (lower[0], upper[0]):
        if np.isfinite(value):
            # Polish with Brent's method on a narrow bracket.
            f = lambda t: float(_residual(t, intercept, slope, space, decay.lambda_238,
                                          decay.lambda_235, decay.u238_u235))
            width = max(abs(value) * 1e-6, 1e-6)
            try:
                if f(value - width) * f(value + width) < 0:
                    value = brentq(f, value - width, value + width, xtol=1e-12, rtol=1e-15)
            except ValueError:
                pass
            roots.append(float(value))
    return sorted(roots)


@dataclass
class InterceptAge:
    """A concordia-intercept age with first-order uncertainties (Ma, 1 s)."""

    age_ma: float
    sigma_ma: float
    sigma_with_decay_ma: float
    x: float
    y: float
    kind: str
    note: str = ""

    @property
    def two_sigma_ma(self) -> float:
        """Twice the reported one-sigma analytical uncertainty."""
        return 2.0 * self.sigma_ma


def _age_gradients(t, a, b, space, decay):
    """Return d t / d(a, b) and d t / d(l8, l5, u) at an intersection."""
    d = _concordia_derivatives(t, space, decay.lambda_238, decay.lambda_235, decay.u238_u235)
    X, _Y = concordia_xy(t, space, decay)
    Ft = b * d["t"][0] - d["t"][1]
    grad_line = -np.array([1.0, X]) / Ft
    grad_decay = -np.array([b * d[k][0] - d[k][1] for k in ("l8", "l5", "u")]) / Ft
    return grad_line, grad_decay


# ---------------------------------------------------------------------------
# Likelihood fits (models 1-3 and anchors)
# ---------------------------------------------------------------------------


class _Problem:
    """Map fit parameters theta to a line and evaluate the objective."""

    def __init__(self, x, y, sx, sy, rho, space, model, anchor, decay):
        self.x, self.y, self.sx, self.sy, self.rho = x, y, sx, sy, rho
        self.space, self.model, self.anchor, self.decay = space, model, anchor, decay
        self.u = decay.u238_u235
        k = anchor.kind
        fixed_sigma = anchor.sigma > 0
        # Parameter names for the line.
        if k == "none":
            names = ["a", "b"]
        elif k == "common_pb":
            names = ["a", "b"] if fixed_sigma and model != 3 else (["b"] if space == "tera_wasserburg" else ["a"])
        elif k == "stacey_kramers":
            names = ["t"]
        else:  # age
            names = ["t", "b"] if fixed_sigma and model != 3 else ["b"]
        self.dispersion_kind = None
        self.fixed_w = 0.0
        if model == 3:
            self.dispersion_kind = "common_pb" if k == "common_pb" else "age"
            if fixed_sigma:
                self.fixed_w = anchor.sigma
            else:
                names.append("logw")
        self.names = tuple(names)

    def line(self, theta):
        p = dict(zip(self.names, theta))
        k, space, u, dc = self.anchor.kind, self.space, self.u, self.decay
        if k == "none":
            return p["a"], p["b"]
        if k == "common_pb":
            if "a" in p and "b" in p:
                return p["a"], p["b"]
            if space == "tera_wasserburg":
                return self.anchor.value, p["b"]
            return p["a"], 1.0 / (self.anchor.value * u)
        if k == "stacey_kramers":
            t = p["t"]
            r76 = float(stacey_kramers_76(t, dc))
            X, Y = concordia_xy(t, space, dc)
            if space == "tera_wasserburg":
                return r76, (Y - r76) / X
            slope = 1.0 / (r76 * u)
            return Y - slope * X, slope
        t = p.get("t", self.anchor.value)
        X, Y = concordia_xy(t, space, dc)
        return Y - p["b"] * X, p["b"]

    def dispersion(self, theta):
        if self.model != 3:
            return 0.0
        p = dict(zip(self.names, theta))
        return float(np.exp(p["logw"])) if "logw" in p else self.fixed_w

    def lower_age(self, a, b):
        p_age = None
        if self.anchor.kind == "age":
            return self.anchor.value
        roots = _roots_batch(a, b, self.space, self.decay.lambda_238, self.decay.lambda_235, self.u)[0]
        p_age = float(roots[0])
        return p_age

    def dispersion_jacobian(self, a, b, theta):
        """Per-point displacement per unit dispersion, shape (n, 2)."""
        p = dict(zip(self.names, theta))
        t = p.get("t")
        if t is None:
            t = self.lower_age(a, b)
        if not np.isfinite(t):
            return None
        dc = self.decay
        X, Y = concordia_xy(t, self.space, dc)
        if self.dispersion_kind == "age":
            if self.space == "wetherill":
                dX, dY = _concordia_derivatives(t, self.space, dc.lambda_238, dc.lambda_235, dc.u238_u235)["t"]
                return np.column_stack([np.full_like(self.x, dX), np.full_like(self.x, dY)])
            # A point with a fixed common-Pb component c (206Pb/238U) has
            # X = 1/(y_t + c) and Y = (x_t + c a0 U)/(U (y_t + c)), where
            # (x_t, y_t) is the Wetherill concordia point; differentiate in t.
            dx_t, dy_t = _concordia_derivatives(t, "wetherill", dc.lambda_238, dc.lambda_235, dc.u238_u235)["t"]
            return np.column_stack([-dy_t * self.x ** 2, self.x * (dx_t / self.u - self.y * dy_t)])
        # Dispersion of the common 207Pb/206Pb ratio.
        if self.space == "tera_wasserburg":
            f = self.x / X
            return np.column_stack([np.zeros_like(self.x), 1.0 - f])
        c = self.y - Y
        return np.column_stack([c * self.u, np.zeros_like(self.x)])

    def max_dispersion(self):
        """Upper bound of the model-3 dispersion (IsoplotR also bounds it)."""
        return EARTH_AGE_MA if self.dispersion_kind == "age" else 1.0

    def residual_variance(self, b):
        return self.sy ** 2 + b * b * self.sx ** 2 - 2.0 * b * self.rho * self.sx * self.sy

    def objective(self, theta):
        try:
            a, b = self.line(theta)
        except (FloatingPointError, ValueError):
            return np.inf
        if not (np.isfinite(a) and np.isfinite(b)):
            return np.inf
        r = self.y - a - b * self.x
        if self.model == 2:
            d2 = r * r / (1.0 + b * b)
            value = 0.5 * self.x.size * np.log(np.sum(d2))
        else:
            v = self.residual_variance(b)
            value = 0.0
            if self.model == 3:
                w = self.dispersion(theta)
                if w > self.max_dispersion():
                    return np.inf
                J = self.dispersion_jacobian(a, b, theta)
                if J is None:
                    return np.inf
                g = J[:, 1] - b * J[:, 0]
                v = v + w * w * g * g
                # log det(Sigma_i + w^2 J J^T) - log det(Sigma_i)
                det = self.sx ** 2 * self.sy ** 2 * (1.0 - self.rho ** 2)
                inv_xx = self.sy ** 2 / det
                inv_yy = self.sx ** 2 / det
                inv_xy = -self.rho * self.sx * self.sy / det
                q = J[:, 0] ** 2 * inv_xx + 2 * J[:, 0] * J[:, 1] * inv_xy + J[:, 1] ** 2 * inv_yy
                value += 0.5 * np.sum(np.log1p(w * w * q))
            if np.any(v <= 0):
                return np.inf
            value += 0.5 * np.sum(r * r / v)
        value += self.prior(theta, a, b)
        return float(value) if np.isfinite(value) else np.inf

    def prior(self, theta, a, b):
        anchor = self.anchor
        if anchor.sigma <= 0 or self.model == 3:
            return 0.0
        if anchor.kind == "common_pb":
            r76 = a if self.space == "tera_wasserburg" else 1.0 / (b * self.u)
            return 0.5 * ((r76 - anchor.value) / anchor.sigma) ** 2
        if anchor.kind == "age":
            t = dict(zip(self.names, theta))["t"]
            return 0.5 * ((t - anchor.value) / anchor.sigma) ** 2
        return 0.0

    def sse(self, a, b):
        """Model-1 weighted sum of squares at a line (used for the MSWD)."""
        r = self.y - a - b * self.x
        return float(np.sum(r * r / self.residual_variance(b)))


def _hessian(fun, theta, scale):
    """Central-difference Hessian with per-parameter steps."""
    k = len(theta)
    H = np.empty((k, k))
    h = 1e-4 * scale
    f0 = fun(theta)
    for i in range(k):
        ei = np.zeros(k)
        ei[i] = h[i]
        H[i, i] = (fun(theta + ei) - 2 * f0 + fun(theta - ei)) / h[i] ** 2
        for j in range(i + 1, k):
            ej = np.zeros(k)
            ej[j] = h[j]
            H[i, j] = H[j, i] = (fun(theta + ei + ej) - fun(theta + ei - ej)
                                 - fun(theta - ei + ej) + fun(theta - ei - ej)) / (4 * h[i] * h[j])
    return H


def _initial_theta(problem, start):
    a0, b0 = start.intercept, start.slope
    k, space = problem.anchor.kind, problem.space
    values = {"a": a0, "b": b0}
    t_guess = np.nan
    roots = _roots_batch(a0, b0, space, problem.decay.lambda_238, problem.decay.lambda_235, problem.u)[0]
    if np.isfinite(roots[0]):
        t_guess = float(roots[0])
    if k == "age":
        t_guess = problem.anchor.value
        X, Y = concordia_xy(t_guess, space, problem.decay)
        # Slope through the anchor and the weighted centroid.
        cx, cy = start.centroid
        if np.isfinite(cx) and cx != X:
            values["b"] = (cy - Y) / (cx - X)
    if k == "common_pb" and "b" in problem.names and "a" not in problem.names:
        cx, cy = start.centroid
        values["b"] = (cy - problem.anchor.value) / cx if cx else b0
    if k == "common_pb" and "a" in problem.names and "b" not in problem.names:
        cx, cy = start.centroid
        values["a"] = cy - cx / (problem.anchor.value * problem.u)
    if k == "stacey_kramers" and not np.isfinite(t_guess):
        t_guess = 1000.0
    values["t"] = t_guess if np.isfinite(t_guess) else 1000.0
    if problem.model == 3:
        # Start from a dispersion comparable to the scatter.
        values["logw"] = np.log(max(1e-6, 0.05 * values["t"] if problem.dispersion_kind == "age"
                                    else 0.01 * max(abs(a0), 0.1)))
    return np.array([values[name] for name in problem.names], dtype=float)


def _fit_likelihood(problem: _Problem, start: LineFit):
    theta0 = _initial_theta(problem, start)
    fun = problem.objective
    best = None
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        best = _minimise(fun, theta0)
    theta = best.x
    scale = np.maximum(np.abs(theta), 1e-3)
    if "logw" in problem.names:
        scale[problem.names.index("logw")] = 1.0
    H = _hessian(fun, theta, scale)
    theta_cov = _inverse(H)
    if "logw" in problem.names:
        i = problem.names.index("logw")
        diagonal = np.diag(theta_cov)
        if H[i, i] <= 1e-6 or not np.all(np.isfinite(diagonal)) or np.any(diagonal < 0):
            # The dispersion went to zero (its likelihood is flat in log w):
            # the line uncertainties are those with the dispersion fixed.
            keep = [j for j in range(len(theta)) if j != i]
            theta_cov = np.full_like(H, np.nan)
            theta_cov[np.ix_(keep, keep)] = _inverse(H[np.ix_(keep, keep)])
    return theta, theta_cov, bool(best.success or np.isfinite(best.fun))


def _minimise(fun, theta0):
    best = None
    for method in ("Nelder-Mead", "Powell", "Nelder-Mead"):
        res = minimize(fun, theta0 if best is None else best.x, method=method,
                       options={"xatol": 1e-12, "fatol": 1e-14, "maxiter": 20000, "maxfev": 40000}
                       if method == "Nelder-Mead" else {"xtol": 1e-12, "ftol": 1e-14, "maxiter": 20000})
        if best is None or res.fun <= best.fun:
            best = res
    return best


def _inverse(H):
    try:
        return np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return np.full_like(H, np.nan)


def _line_jacobian(problem, theta):
    """d(a, b)/d theta by central differences."""
    k = len(theta)
    J = np.zeros((2, k))
    for i in range(k):
        h = 1e-6 * max(abs(theta[i]), 1e-3)
        e = np.zeros(k)
        e[i] = h
        a1, b1 = problem.line(theta + e)
        a0, b0 = problem.line(theta - e)
        J[:, i] = [(a1 - a0) / (2 * h), (b1 - b0) / (2 * h)]
    return J


def autocorrelation_inflation(x, y, sx, sy, rho, intercept, slope, groups, order) -> float:
    """Return the pooled lag-1 autocorrelation of residuals within groups.

    Residuals ``r = y - a - b x`` are standardised by their analytical
    standard deviation (not centred within groups), ordered by ``order`` within each group (for example
    the intervals of one acquisition in time), and
    ``phi = sum(e_t e_(t-1)) / sum(e_t^2)`` is pooled over groups. The value
    is clipped to [-0.95, 0.95]. Groups with one point contribute nothing.
    """
    v = sy ** 2 + slope * slope * sx ** 2 - 2 * slope * rho * sx * sy
    e = (y - intercept - slope * x) / np.sqrt(v)
    groups = np.asarray(groups)
    order = np.asarray(order, dtype=float)
    num = den = 0.0
    for label in np.unique(groups):
        idx = np.flatnonzero(groups == label)
        if idx.size < 2:
            continue
        # Not centred per group: a whole acquisition sitting off the line is
        # also dependence between its intervals and should count.
        g = e[idx[np.argsort(order[idx], kind="stable")]]
        num += float(np.sum(g[1:] * g[:-1]))
        den += float(np.sum(g * g))
    if den <= 0:
        return 0.0
    return float(np.clip(num / den, -0.95, 0.95))


def _model1_covariance(x, y, sx, sy, rho, a, b):
    """Inverse of the Hessian of S/2 at (a, b), S = sum r^2 / v(b).

    ``r = y - a - b x`` and ``v = sy^2 + b^2 sx^2 - 2 b rho sx sy`` (the
    variance of the vertical residual), so S/2 is the profile negative
    log-likelihood of a line through correlated-error data.
    """
    r = y - a - b * x
    v = sy ** 2 + b * b * sx ** 2 - 2 * b * rho * sx * sy
    dv = 2 * b * sx ** 2 - 2 * rho * sx * sy
    d2v = 2 * sx ** 2
    haa = np.sum(1.0 / v)
    hab = np.sum(x / v + r * dv / v ** 2)
    hbb = np.sum(x * x / v + 2 * r * x * dv / v ** 2 - 0.5 * r * r * d2v / v ** 2 + r * r * dv ** 2 / v ** 3)
    return _inverse(np.array([[haa, hab], [hab, hbb]]))


def _wetherill_model2(x, y, sx, sy, rho, anchor, decay):
    """Model 2 for Wetherill data, regressed in Tera-Wasserburg coordinates.

    Orthogonal regression depends on the axis scales; like IsoplotR, the
    regression is done with 238U/206Pb and 207Pb/206Pb and the line is mapped
    back to Wetherill coordinates (a straight line in one diagram is a
    straight line in the other).
    """
    u = decay.u238_u235
    X = 1.0 / y
    Y = x / (u * y)
    # First-order error transformation (used only for the MSWD of the line).
    dXdy = -1.0 / y ** 2
    dYdx = 1.0 / (u * y)
    dYdy = -x / (u * y ** 2)
    cxx, cyy, cxy = sx ** 2, sy ** 2, rho * sx * sy
    vX = dXdy ** 2 * cyy
    vY = dYdx ** 2 * cxx + dYdy ** 2 * cyy + 2 * dYdx * dYdy * cxy
    cXY = dXdy * (dYdx * cxy + dYdy * cyy)
    sX, sY = np.sqrt(vX), np.sqrt(vY)
    tw = fit_discordia(X, Y, sX, sY, np.clip(cXY / (sX * sY), -0.999999, 0.999999),
                       space="tera_wasserburg", model=2, anchor=anchor, decay=decay).line
    A, Bs = tw.intercept, tw.slope
    J = np.array([[Bs / A ** 2, -1.0 / A], [-1.0 / (u * A ** 2), 0.0]])
    a, b = -Bs / A, 1.0 / (u * A)
    r = y - a - b * x
    v = sy ** 2 + b * b * sx ** 2 - 2 * b * rho * sx * sy
    S = float(np.sum(r * r / v))
    df = tw.df
    line = LineFit(float(a), float(b), J @ tw.cov @ J.T, S / df if df > 0 else float("nan"),
                   float(chi2.sf(S, df)) if df > 0 else float("nan"), df, tw.n, "wetherill", model=2,
                   anchor=anchor, converged=tw.converged, theta_names=tw.theta_names, theta=tw.theta,
                   theta_cov=tw.theta_cov)
    line.anchored_age, line.anchored_age_sigma = tw.anchored_age, tw.anchored_age_sigma
    return line


def _total_least_squares(x, y):
    xm, ym = x.mean(), y.mean()
    C = np.cov(np.vstack([x - xm, y - ym]))
    vals, vecs = np.linalg.eigh(C)
    vx, vy = vecs[:, -1]
    slope = vy / vx
    return ym - slope * xm, slope, (xm, ym)


# ---------------------------------------------------------------------------
# Public fit
# ---------------------------------------------------------------------------


@dataclass
class DiscordiaFit:
    """A discordia (or common-Pb isochron) fit and its concordia intercepts."""

    line: LineFit
    lower: InterceptAge | None
    upper: InterceptAge | None
    common_pb_76: float
    common_pb_76_sigma: float
    decay: DecayConstants
    include_decay_uncertainty: bool
    alpha: float
    n_used: int
    n_total: int
    notes: list[str] = field(default_factory=list)

    @property
    def space(self) -> str:
        """The coordinate system of the fit."""
        return self.line.space

    def method_text(self) -> str:
        """Describe the method, model and anchor in one sentence."""
        model = {1: "model 1 (analytical uncertainties"
                    + (", inflated by sqrt(MSWD) because p < %g)" % self.alpha if self.line.overdispersed else ")"),
                 2: "model 2 (total least squares, analytical uncertainties ignored)",
                 3: "model 3 (analytical uncertainties plus a fitted dispersion)"}[self.line.model]
        space = "Tera-Wasserburg (238U/206Pb vs 207Pb/206Pb)" if self.space == "tera_wasserburg" \
            else "Wetherill (207Pb/235U vs 206Pb/238U)"
        return (f"Error-weighted regression in {space} space after York et al. (2004), "
                f"{model}, anchor: {self.line.anchor.describe()}")

    def summary(self) -> dict:
        """Flatten the fit for tables and exports (1 s values, ages in Ma)."""
        out = {
            "space": self.space,
            "model": self.line.model,
            "anchor": self.line.anchor.describe(),
            "n": self.n_used,
            "n_total": self.n_total,
            "slope": self.line.slope,
            "slope_1s": self.line.slope_sigma,
            "intercept": self.line.intercept,
            "intercept_1s": self.line.intercept_sigma,
            "cov_intercept_slope": float(self.line.cov_reported[0, 1]),
            "mswd": self.line.mswd,
            "p_value": self.line.p_value,
            "df": self.line.df,
            "overdispersed": self.line.overdispersed,
            "dispersion": self.line.dispersion,
            "dispersion_1s": self.line.dispersion_sigma,
            "dispersion_units": self.line.dispersion_units,
            "common_207Pb_206Pb": self.common_pb_76,
            "common_207Pb_206Pb_1s": self.common_pb_76_sigma,
            "decay_uncertainty_included": self.include_decay_uncertainty,
            "autocorrelation": self.line.autocorrelation,
            "variance_inflation": self.line.variance_inflation,
            "n_effective": self.line.n_effective,
        }
        for name, item in (("lower", self.lower), ("upper", self.upper)):
            out[f"{name}_age_ma"] = item.age_ma if item else float("nan")
            out[f"{name}_age_1s"] = item.sigma_ma if item else float("nan")
            out[f"{name}_age_1s_with_decay"] = item.sigma_with_decay_ma if item else float("nan")
        return out


def fit_discordia(x, y, sx, sy, rho=0.0, *, space: str = "tera_wasserburg", model: int = 1,
                  anchor: Anchor | None = None, decay: DecayConstants = DEFAULT_DECAY,
                  include_decay_uncertainty: bool = False, alpha: float = 0.05,
                  groups=None, order=None) -> DiscordiaFit:
    """Fit a discordia or common-Pb line and solve its concordia intercepts.

    Parameters
    ----------
    x, y, sx, sy, rho
        Ratios, their one-sigma absolute uncertainties and the error
        correlation, in the coordinates of ``space`` (Tera-Wasserburg:
        238U/206Pb and 207Pb/206Pb; Wetherill: 207Pb/235U and 206Pb/238U).
        Rows with non-finite values or non-positive uncertainties are
        dropped and counted in ``n_total``.
    space
        ``"tera_wasserburg"`` or ``"wetherill"``.
    model
        1, 2 or 3 (see the module notes).
    anchor
        An :class:`Anchor`; default unanchored.
    decay
        Decay constants and 238U/235U.
    include_decay_uncertainty
        Add decay-constant (and, in Tera-Wasserburg, 238U/235U) uncertainty to
        the reported age uncertainties. Both versions are always computed.
    alpha
        Model 1: the probability below which the line's uncertainties are
        inflated by sqrt(MSWD).
    groups, order
        For depth-profile intervals: the acquisition of each point and its
        position in time. Successive intervals of one acquisition are not
        independent, so the pooled lag-1 autocorrelation ``phi`` of the
        standardised residuals within acquisitions is estimated and, when
        positive, the line covariance is multiplied by
        ``(1 + phi) / (1 - phi)`` (the AR(1) variance inflation) and the
        chi-square probability uses the correspondingly reduced effective
        number of points. See :func:`autocorrelation_inflation`.
    """
    space = _normalise_space(space)
    if model not in MODELS:
        raise ValueError("model must be 1, 2 or 3")
    anchor = anchor or Anchor()
    x, y, sx, sy, rho, mask = _clean(x, y, sx, sy, rho)
    n_total = int(x.size)
    if groups is not None:
        groups = np.asarray(list(groups))[mask]
        order = np.arange(n_total)[mask] if order is None else np.asarray(order, dtype=float)[mask]
    x, y, sx, sy, rho = x[mask], y[mask], sx[mask], sy[mask], rho[mask]
    n = int(x.size)
    minimum = 2 if anchor.kind == "none" else 1
    if model == 3 and anchor.sigma <= 0:
        minimum += 1
    if model == 2:
        minimum = max(minimum, 3)
    if n < minimum:
        raise ValueError(f"this fit needs at least {minimum} usable points; {n} given")
    notes: list[str] = []

    if model == 2 and space == "wetherill":
        line = _wetherill_model2(x, y, sx, sy, rho, anchor, decay)
    elif anchor.kind == "none" and model == 1:
        line = york_fit(x, y, sx, sy, rho, space=space)
        # Report the inverse Hessian of the likelihood (as IsoplotR's
        # discordia does); York's closed-form covariance is kept alongside.
        line.cov_york = line.cov
        line.cov = _model1_covariance(x, y, sx, sy, rho, line.intercept, line.slope)
        line.theta_names = ("a", "b")
        line.theta = np.array([line.intercept, line.slope])
        line.theta_cov = line.cov.copy()
    else:
        start = york_fit(x, y, sx, sy, rho, space=space) if n >= 2 else LineFit(
            y[0], 0.0, np.eye(2), np.nan, np.nan, 0, n, space, centroid=(x[0], y[0]))
        if model == 2 and anchor.kind == "none":
            a, b, cen = _total_least_squares(x, y)
            start = LineFit(a, b, np.eye(2), np.nan, np.nan, n - 2, n, space, centroid=cen)
        problem = _Problem(x, y, sx, sy, rho, space, model, anchor, decay)
        theta, theta_cov, ok = _fit_likelihood(problem, start)
        a, b = problem.line(theta)
        J = _line_jacobian(problem, theta)
        k_line = [i for i, name in enumerate(problem.names) if name != "logw"]
        cov = J[:, k_line] @ theta_cov[np.ix_(k_line, k_line)] @ J[:, k_line].T
        free = len(problem.names)
        df = n - free
        S = problem.sse(a, b)
        mswd = S / df if df > 0 else float("nan")
        p = float(chi2.sf(S, df)) if df > 0 else float("nan")
        line = LineFit(float(a), float(b), cov, mswd, p, int(df), n, space, model=model, anchor=anchor,
                       converged=ok, centroid=start.centroid, theta_names=problem.names,
                       theta=theta, theta_cov=theta_cov)
        if model == 3:
            w = problem.dispersion(theta)
            line.dispersion = w
            line.dispersion_units = "Ma" if problem.dispersion_kind == "age" else "207Pb/206Pb"
            if "logw" in problem.names and w > 0.1 * problem.max_dispersion():
                notes.append(f"The fitted dispersion ({w:.3g} {line.dispersion_units}) is implausibly large: "
                             "model 3 does not describe these data; check the analytical uncertainties.")
            if "logw" in problem.names:
                i = problem.names.index("logw")
                line.dispersion_sigma = float(w * np.sqrt(theta_cov[i, i])) if theta_cov[i, i] > 0 else float("nan")
            else:
                line.dispersion_sigma = 0.0
                notes.append("Dispersion fixed at the anchor uncertainty, as in IsoplotR.")
        if "t" in problem.names:
            i = problem.names.index("t")
            line.anchored_age = float(theta[i])
            line.anchored_age_sigma = float(np.sqrt(theta_cov[i, i])) if theta_cov[i, i] > 0 else float("nan")
        elif anchor.kind == "age":
            line.anchored_age, line.anchored_age_sigma = float(anchor.value), 0.0
    line.model = model
    line.anchor = anchor
    if groups is not None and model != 2:
        phi = autocorrelation_inflation(x, y, sx, sy, rho, line.intercept, line.slope, groups, order)
        kappa = (1 + phi) / (1 - phi) if phi > 0 else 1.0
        line.autocorrelation = phi
        line.variance_inflation = kappa
        line.n_effective = n / kappa
        line.cov = line.cov * kappa
        line.anchored_age_sigma *= np.sqrt(kappa)
        free = len(line.theta_names) or 2
        df_eff = line.n_effective - free
        if df_eff > 0 and np.isfinite(line.mswd):
            line.p_value = float(chi2.sf(line.mswd * df_eff, df_eff))
        if kappa > 1:
            notes.append(f"Successive intervals are autocorrelated (lag-1 phi = {phi:.2f}); "
                         f"uncertainties inflated by sqrt({kappa:.2f}), effective n = {line.n_effective:.1f}.")
    if model == 1:
        line.overdispersed = bool(np.isfinite(line.p_value) and line.p_value < alpha)
    if model == 2:
        line.overdispersed = False

    # Intercept ages.
    cov_line = line.cov_reported
    roots = intersect_concordia(line.intercept, line.slope, space, decay)
    ages: list[InterceptAge] = []
    anchored_index = None
    if anchor.kind in {"age", "stacey_kramers"} and np.isfinite(line.anchored_age) and roots:
        # The anchored (or jointly solved) age is the intersection nearest to it.
        anchored_index = int(np.argmin([abs(t - line.anchored_age) for t in roots[:2]]))
    for i, t in enumerate(roots[:2]):
        if i == anchored_index:
            # The anchored (or jointly solved) age carries its own uncertainty.
            g_line, g_decay = _age_gradients(t, line.intercept, line.slope, space, decay)
            sigma = line.anchored_age_sigma
            if model == 1 and line.overdispersed:
                sigma *= np.sqrt(line.mswd)
            sigma_dc = float(np.sqrt(sigma ** 2 + g_decay @ decay.covariance() @ g_decay))
        else:
            g_line, g_decay = _age_gradients(t, line.intercept, line.slope, space, decay)
            var = float(g_line @ cov_line @ g_line)
            sigma = float(np.sqrt(var)) if var >= 0 else float("nan")
            sigma_dc = float(np.sqrt(var + g_decay @ decay.covariance() @ g_decay))
        X, Y = concordia_xy(t, space, decay)
        note = ""
        if t < 0:
            note = "negative age"
        elif t > EARTH_AGE_MA:
            note = "older than the Earth"
        ages.append(InterceptAge(float(t), float(sigma), sigma_dc, float(X), float(Y),
                                 "lower" if i == 0 else "upper", note))
    if any(not (item.sigma_ma > 0) for item in ages if item.kind == "lower" and anchor.kind != "age"):
        notes.append("The intercept uncertainty could not be estimated (flat or degenerate likelihood).")
    if not ages:
        notes.append(f"The line does not meet concordia between 0 and {MAX_AGE_MA:g} Ma.")
    lower = ages[0] if ages else None
    upper = ages[1] if len(ages) > 1 else None
    if include_decay_uncertainty:
        for item in ages:
            item.sigma_ma, item.sigma_with_decay_ma = item.sigma_with_decay_ma, item.sigma_with_decay_ma

    # Common 207Pb/206Pb.
    if space == "tera_wasserburg":
        r76 = line.intercept
        r76_sigma = float(np.sqrt(cov_line[0, 0]))
    else:
        u = decay.u238_u235
        with np.errstate(divide="ignore", invalid="ignore"):
            r76 = 1.0 / (line.slope * u)
            r76_sigma = float(abs(r76 / line.slope) * np.sqrt(cov_line[1, 1]))
    if anchor.kind == "common_pb" and (anchor.sigma <= 0 or model == 3):
        r76, r76_sigma = anchor.value, 0.0
    if anchor.kind == "stacey_kramers" and lower is not None:
        r76 = float(stacey_kramers_76(lower.age_ma, decay))
        dt = 1e-3 * max(abs(lower.age_ma), 1.0)
        slope76 = float((stacey_kramers_76(lower.age_ma + dt, decay) - stacey_kramers_76(lower.age_ma - dt, decay)) / (2 * dt))
        r76_sigma = abs(slope76) * lower.sigma_ma
    return DiscordiaFit(line, lower, upper, float(r76), float(r76_sigma), decay,
                        include_decay_uncertainty, alpha, n, n_total, notes)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@dataclass
class BootstrapResult:
    """Distributions of line parameters and intercept ages from resampling."""

    method: str
    n_resamples: int
    seed: int
    confidence: float
    intercept: np.ndarray
    slope: np.ndarray
    lower_ma: np.ndarray
    upper_ma: np.ndarray
    common_pb_76: np.ndarray
    converged: np.ndarray
    elapsed_s: float = float("nan")
    notes: list[str] = field(default_factory=list)

    def interval(self, name: str, confidence: float | None = None) -> dict[str, float]:
        """Return median, mean, standard deviation and a percentile interval."""
        values = np.asarray(getattr(self, name), dtype=float)
        finite = values[np.isfinite(values) & self.converged]
        level = self.confidence if confidence is None else confidence
        if finite.size == 0:
            return {"median": np.nan, "mean": np.nan, "sd": np.nan, "low": np.nan, "high": np.nan,
                    "fraction": 0.0}
        q = 100 * (1 - level) / 2
        low, med, high = np.percentile(finite, [q, 50, 100 - q])
        return {"median": float(med), "mean": float(finite.mean()), "sd": float(finite.std(ddof=1))
                if finite.size > 1 else np.nan, "low": float(low), "high": float(high),
                "fraction": float(finite.size / values.size)}

    def summary(self) -> dict:
        """Flatten the percentile summaries for export."""
        out = {"bootstrap_method": self.method, "bootstrap_resamples": self.n_resamples,
               "bootstrap_seed": self.seed, "bootstrap_confidence": self.confidence}
        for name in ("lower_ma", "upper_ma", "common_pb_76", "slope", "intercept"):
            stats = self.interval(name)
            for key, value in stats.items():
                out[f"bootstrap_{name}_{key}"] = value
        return out


def _draw_points(rng, x, y, sx, sy, rho, extra_J, w, B):
    """Draw B perturbed copies of the data from each point's covariance."""
    n = x.size
    cxx = sx ** 2
    cyy = sy ** 2
    cxy = rho * sx * sy
    if extra_J is not None and w > 0:
        cxx = cxx + w * w * extra_J[:, 0] ** 2
        cyy = cyy + w * w * extra_J[:, 1] ** 2
        cxy = cxy + w * w * extra_J[:, 0] * extra_J[:, 1]
    # Cholesky of each 2x2 covariance.
    l11 = np.sqrt(cxx)
    l21 = cxy / l11
    l22 = np.sqrt(np.maximum(cyy - l21 ** 2, 0.0))
    z1 = rng.standard_normal((B, n))
    z2 = rng.standard_normal((B, n))
    xs = x + l11 * z1
    ys = y + l21 * z1 + l22 * z2
    tsx = np.broadcast_to(np.sqrt(cxx), (B, n))
    tsy = np.broadcast_to(np.sqrt(cyy), (B, n))
    trho = np.broadcast_to(cxy / (np.sqrt(cxx) * np.sqrt(cyy)), (B, n))
    return xs, ys, tsx, tsy, trho


def bootstrap_discordia(x, y, sx, sy, rho=0.0, *, space: str = "tera_wasserburg", model: int = 1,
                        anchor: Anchor | None = None, n_resamples: int = 2000, seed: int = 0,
                        method: str = "parametric", groups: Iterable | None = None, order=None,
                        decay: DecayConstants = DEFAULT_DECAY, include_decay_uncertainty: bool = False,
                        confidence: float = 0.95, fit: DiscordiaFit | None = None,
                        chunk: int = 4096) -> BootstrapResult:
    """Resample the data and refit the line, vectorised over all resamples.

    Methods
    -------
    ``"parametric"``
        As Isoplot.jl does: every resample draws each point from its own
        bivariate normal error ellipse (plus the fitted model-3 dispersion)
        and refits. Isoplot.jl refits each draw by ordinary least squares;
        here the refit is the York fit of the chosen model (model 2 uses total
        least squares), so the bootstrap matches the analytical estimator.
    ``"resample"``
        Nonparametric: draw points with replacement, keep their own
        uncertainties, and refit. ``groups`` (one label per point, for example
        the acquisition of each depth-profile interval) resamples whole groups
        instead (a block bootstrap), which keeps autocorrelated intervals of
        one acquisition together. With fewer than three groups, blocks of
        successive intervals (ordered by ``order``) are resampled within each
        group (moving-block bootstrap, block length ceil(n^(1/3))).

    Anchor uncertainties are sampled from their normal distribution; the
    Stacey-Kramers anchor is iterated to convergence for each resample; with
    ``include_decay_uncertainty`` the decay constants are drawn too.

    The generator is ``numpy.random.default_rng(seed)``, so a result is
    reproducible from ``seed`` and ``n_resamples`` (stored on the result).
    """
    import time

    started = time.perf_counter()
    space = _normalise_space(space)
    anchor = anchor or Anchor()
    if method not in {"parametric", "resample"}:
        raise ValueError("method must be 'parametric' or 'resample'")
    early_notes = []
    if model == 2 and method == "parametric":
        # Model 2 ignores the analytical uncertainties, so perturbing points
        # by them says nothing about its scatter; resample points instead.
        method = "resample"
        early_notes.append("Model 2: points resampled with replacement (its fit ignores analytical errors).")
    x, y, sx, sy, rho, mask = _clean(x, y, sx, sy, rho)
    group_labels = None if groups is None else np.asarray(list(groups))[mask]
    x, y, sx, sy, rho = x[mask], y[mask], sx[mask], sy[mask], rho[mask]
    n = x.size
    if fit is None:
        fit = fit_discordia(x, y, sx, sy, rho, space=space, model=model, anchor=anchor, decay=decay)
    notes: list[str] = list(early_notes)
    rng = np.random.default_rng(seed)
    B = int(n_resamples)
    # Model-3 dispersion is kept at its fitted value in every resample.
    extra_J, w = None, 0.0
    if model == 3 and np.isfinite(fit.line.dispersion) and fit.line.dispersion > 0:
        problem = _Problem(x, y, sx, sy, rho, space, model, anchor, decay)
        theta = fit.line.theta if fit.line.theta is not None else np.array([])
        extra_J = problem.dispersion_jacobian(fit.line.intercept, fit.line.slope, theta)
        w = fit.line.dispersion
        notes.append("Model-3 dispersion held at its fitted value in each resample.")
    intercepts = np.empty(B)
    slopes = np.empty(B)
    converged = np.empty(B, dtype=bool)
    l8 = np.full(B, decay.lambda_238)
    l5 = np.full(B, decay.lambda_235)
    uu = np.full(B, decay.u238_u235)
    if include_decay_uncertainty:
        l8 = rng.normal(decay.lambda_238, decay.sigma_238, B)
        l5 = rng.normal(decay.lambda_235, decay.sigma_235, B)
        uu = rng.normal(decay.u238_u235, decay.sigma_u238_u235, B)
    if group_labels is not None:
        _, group_index = np.unique(group_labels, return_inverse=True)
        n_groups = int(group_index.max()) + 1
        order_values = np.arange(n, dtype=float) if order is None else np.asarray(order, dtype=float)[mask]
        if method == "parametric":
            notes.append("Groups are ignored by the parametric bootstrap.")
        elif n_groups >= 3:
            # Whole acquisitions, padded to a common length with zero weight.
            sizes = np.bincount(group_index, minlength=n_groups)
            members = np.full((n_groups, sizes.max()), -1)
            for g in range(n_groups):
                members[g, :sizes[g]] = np.flatnonzero(group_index == g)
            notes.append(f"Block bootstrap: whole acquisitions resampled ({n_groups} groups).")
        else:
            # Too few acquisitions to resample: moving blocks of successive
            # intervals within each acquisition (Kuensch 1989).
            sequences = [np.flatnonzero(group_index == g)[np.argsort(order_values[group_index == g], kind="stable")]
                         for g in range(n_groups)]
            block = max(2, int(np.ceil(max(len(q) for q in sequences) ** (1 / 3))))
            notes.append(f"Moving-block bootstrap of successive intervals (block length {block}).")
    for start in range(0, B, chunk):
        stop = min(B, start + chunk)
        b = stop - start
        if method == "parametric":
            xs, ys, tsx, tsy, trho = _draw_points(rng, x, y, sx, sy, rho, extra_J, w, b)
        else:
            if group_labels is None:
                idx = rng.integers(0, n, (b, n))
            elif n_groups >= 3:
                picks = rng.integers(0, n_groups, (b, n_groups))
                idx = members[picks].reshape(b, -1)
                pad = idx < 0
                idx = np.where(pad, 0, idx)
            else:
                parts = []
                for q in sequences:
                    m = len(q)
                    L = min(block, m)
                    k = int(np.ceil(m / L))
                    starts = rng.integers(0, m - L + 1, (b, k))
                    parts.append(q[(starts[:, :, None] + np.arange(L)).reshape(b, -1)[:, :m]])
                idx = np.concatenate(parts, axis=1)
                pad = np.zeros(idx.shape, dtype=bool)
            xs, ys = x[idx], y[idx]
            tsx, tsy, trho = sx[idx], sy[idx], rho[idx]
            if extra_J is not None:
                J = extra_J[idx]
                cxx = tsx ** 2 + w * w * J[..., 0] ** 2
                cyy = tsy ** 2 + w * w * J[..., 1] ** 2
                cxy = trho * tsx * tsy + w * w * J[..., 0] * J[..., 1]
                tsx, tsy = np.sqrt(cxx), np.sqrt(cyy)
                trho = cxy / (tsx * tsy)
            if group_labels is not None and pad.any():
                tsx = np.where(pad, np.inf, tsx)
                tsy = np.where(pad, np.inf, tsy)
                trho = np.where(pad, 0.0, trho)
        a_b, s_b, ok = _refit_batch(xs, ys, tsx, tsy, trho, space, model, anchor, decay, rng,
                                    l8[start:stop], l5[start:stop], uu[start:stop])
        intercepts[start:stop] = a_b
        slopes[start:stop] = s_b
        converged[start:stop] = ok
    lower, upper = _roots_batch(intercepts, slopes, space, l8, l5, uu)
    if anchor.kind == "age":
        if anchor.sigma > 0 and model != 3:
            pass  # the drawn anchor age is the lower intercept, found by the root search
    if space == "tera_wasserburg":
        r76 = intercepts.copy()
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            r76 = 1.0 / (slopes * uu)
    return BootstrapResult(method, B, int(seed), confidence, intercepts, slopes, lower, upper, r76,
                           converged, time.perf_counter() - started, notes)


def _refit_batch(xs, ys, sx, sy, rho, space, model, anchor, decay, rng, l8, l5, uu):
    """Refit lines to resampled data sets (rows of the arrays).

    Anchors with an uncertainty (models 1 and 3 use it differently, as in
    IsoplotR) are refitted as the same estimator: a Gaussian prior on the
    Tera-Wasserburg intercept is exactly an extra point at x = 0 with that
    y uncertainty, and a prior on a concordia age is, to first order, an
    extra point on concordia whose error ellipse lies along the curve. Their
    centres are drawn from the prior in each resample.
    """
    B = xs.shape[0]
    if model == 2:
        return _refit_model2(xs, ys, sx, space, anchor, decay, rng, l8, l5, uu)
    draw = anchor.sigma > 0 and model != 3
    if anchor.kind == "none":
        return york_fit_batch(xs, ys, sx, sy, rho)
    if anchor.kind == "common_pb":
        if draw:
            value = rng.normal(anchor.value, anchor.sigma, B)
            if space == "tera_wasserburg":
                px, py = np.zeros(B), value
                psx, psy = np.full(B, 1e-12), np.full(B, anchor.sigma)
            else:
                # Prior on the slope 1/(r76 U): a distant point along the line.
                finite = np.isfinite(sx)
                cnt = finite.sum(axis=1)
                xm = np.where(finite, xs, 0).sum(axis=1) / cnt
                ym = np.where(finite, ys, 0).sum(axis=1) / cnt
                span = np.nanmax(np.where(finite, xs, np.nan), axis=1) - np.nanmin(np.where(finite, xs, np.nan), axis=1)
                L = 1e4 * np.maximum(span, 1e-12)
                b0 = 1.0 / (value * uu)
                sigma_b = anchor.sigma / (anchor.value ** 2 * uu)
                px, py = xm + L, ym + b0 * L
                psx, psy = 1e-12 * np.abs(px), L * sigma_b
            return york_fit_batch(*_append_point(xs, ys, sx, sy, rho, px, py, psx, psy, np.zeros(B)))
        if space == "tera_wasserburg":
            return york_fit_batch(xs, ys, sx, sy, rho, anchor_xy=(np.zeros(B), np.full(B, anchor.value)))
        slope = 1.0 / (anchor.value * uu)
        return _fixed_slope_batch(xs, ys, sx, sy, rho, slope)
    if anchor.kind == "age":
        if draw:
            age = rng.normal(anchor.value, anchor.sigma, B)
            px, py = concordia_xy(age, space, l8=l8, l5=l5, u=uu)
            dX, dY = _concordia_derivatives(age, space, l8, l5, uu)["t"]
            psx = np.abs(dX) * anchor.sigma
            psy = np.abs(dY) * anchor.sigma
            prho = np.sign(dX * dY) * 0.999999
            return york_fit_batch(*_append_point(xs, ys, sx, sy, rho, px, py, psx, psy, prho))
        x0, y0 = concordia_xy(np.full(B, anchor.value), space, l8=l8, l5=l5, u=uu)
        return york_fit_batch(xs, ys, sx, sy, rho, anchor_xy=(x0, y0))
    # Stacey-Kramers: alternate between the anchored line and the model age.
    a, b, ok = york_fit_batch(xs, ys, sx, sy, rho)
    t, _ = _roots_batch(a, b, space, l8, l5, uu)
    t = np.where(np.isfinite(t), t, 1000.0)
    for _ in range(30):
        r76 = stacey_kramers_76(t, decay)
        if space == "tera_wasserburg":
            a, b, ok = york_fit_batch(xs, ys, sx, sy, rho, anchor_xy=(np.zeros(B), r76))
        else:
            a, b, ok = _fixed_slope_batch(xs, ys, sx, sy, rho, 1.0 / (r76 * uu))
        t_new, _ = _roots_batch(a, b, space, l8, l5, uu)
        done = np.abs(t_new - t) <= 1e-6 * np.maximum(1.0, np.abs(t))
        t = np.where(np.isfinite(t_new), t_new, t)
        if np.all(done | ~np.isfinite(t_new)):
            break
    return a, b, ok & np.isfinite(t)


def _append_point(xs, ys, sx, sy, rho, px, py, psx, psy, prho):
    """Append one pseudo-observation to every resample."""
    def add(a, v):
        return np.concatenate([np.asarray(a, dtype=float), np.asarray(v, dtype=float)[:, None]], axis=1)
    return add(xs, px), add(ys, py), add(sx, psx), add(sy, psy), add(rho, prho)


def _fixed_slope_batch(xs, ys, sx, sy, rho, slope):
    """Best intercept for a known slope (weighted mean offset)."""
    slope = np.asarray(slope, dtype=float)[:, None]
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        v = sy ** 2 + slope ** 2 * sx ** 2 - 2 * slope * rho * sx * sy
        wgt = np.where(np.isfinite(v) & (v > 0), 1.0 / v, 0.0)
        a = np.sum(wgt * np.where(wgt > 0, ys - slope * xs, 0.0), axis=1) / np.sum(wgt, axis=1)
    return a, slope[:, 0], np.isfinite(a)


def _tls_batch(X, Y, valid, x0=None, y0=None):
    """Major axis (orthogonal regression) per row, optionally through (x0, y0)."""
    w = valid.astype(float)
    if x0 is None:
        cnt = w.sum(axis=1)
        x0 = np.sum(w * np.where(valid, X, 0), axis=1) / cnt
        y0 = np.sum(w * np.where(valid, Y, 0), axis=1) / cnt
    dx = np.where(valid, X - x0[:, None], 0.0)
    dy = np.where(valid, Y - y0[:, None], 0.0)
    sxx, syy, sxy = np.sum(dx * dx, axis=1), np.sum(dy * dy, axis=1), np.sum(dx * dy, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = (syy - sxx + np.sqrt((syy - sxx) ** 2 + 4 * sxy ** 2)) / (2 * sxy)
    return y0 - slope * x0, slope


def _refit_model2(xs, ys, sx, space, anchor, decay, rng, l8, l5, uu):
    """Model-2 refits in Tera-Wasserburg coordinates (as the point estimate)."""
    B = xs.shape[0]
    valid = np.isfinite(sx)
    if space == "wetherill":
        with np.errstate(divide="ignore", invalid="ignore"):
            X = 1.0 / ys
            Y = xs / (uu[:, None] * ys)
    else:
        X, Y = xs, ys
    draw = anchor.sigma > 0
    if anchor.kind == "none":
        A, Bs = _tls_batch(X, Y, valid)
    elif anchor.kind == "common_pb":
        value = rng.normal(anchor.value, anchor.sigma, B) if draw else np.full(B, anchor.value)
        A, Bs = _tls_batch(X, Y, valid, np.zeros(B), value)
    elif anchor.kind == "age":
        age = rng.normal(anchor.value, anchor.sigma, B) if draw else np.full(B, anchor.value)
        x0, y0 = concordia_xy(age, "tera_wasserburg", l8=l8, l5=l5, u=uu)
        A, Bs = _tls_batch(X, Y, valid, x0, y0)
    else:
        A, Bs = _tls_batch(X, Y, valid)
        t, _ = _roots_batch(A, Bs, "tera_wasserburg", l8, l5, uu)
        t = np.where(np.isfinite(t), t, 1000.0)
        for _ in range(30):
            A, Bs = _tls_batch(X, Y, valid, np.zeros(B), stacey_kramers_76(t, decay))
            t_new, _ = _roots_batch(A, Bs, "tera_wasserburg", l8, l5, uu)
            done = np.abs(t_new - t) <= 1e-6 * np.maximum(1.0, np.abs(t))
            t = np.where(np.isfinite(t_new), t_new, t)
            if np.all(done | ~np.isfinite(t_new)):
                break
    if space == "wetherill":
        with np.errstate(divide="ignore", invalid="ignore"):
            a, b = -Bs / A, 1.0 / (uu * A)
    else:
        a, b = A, Bs
    return a, b, np.isfinite(a) & np.isfinite(b)


def fit_record(fit: DiscordiaFit, boot: BootstrapResult | None = None) -> dict:
    """Return a JSON-friendly record of a fit (and bootstrap) for projects."""
    out = fit.summary()
    out["method"] = fit.method_text()
    out["decay_constants"] = asdict(fit.decay)
    out["notes"] = list(fit.notes)
    if boot is not None:
        out.update(boot.summary())
        out["bootstrap_notes"] = list(boot.notes)
    return {k: (float(v) if isinstance(v, (np.floating,)) else v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

_TABLE_COLUMNS = (
    ("label", "Fit"),
    ("view", "Points"),
    ("space", "Diagram"),
    ("n", "n"),
    ("n_total", "of N"),
    ("lower_age_ma", "Lower intercept (Ma)"),
    ("lower_age_2s", "Lower ±2s"),
    ("lower_age_2s_with_decay", "Lower ±2s incl. decay constants"),
    ("bootstrap_lower_ma_low", "Bootstrap lower 2.5 %"),
    ("bootstrap_lower_ma_high", "Bootstrap lower 97.5 %"),
    ("upper_age_ma", "Upper intercept (Ma)"),
    ("upper_age_2s", "Upper ±2s"),
    ("bootstrap_upper_ma_low", "Bootstrap upper 2.5 %"),
    ("bootstrap_upper_ma_high", "Bootstrap upper 97.5 %"),
    ("common_207Pb_206Pb", "Common 207Pb/206Pb"),
    ("common_207Pb_206Pb_2s", "Common ±2s"),
    ("bootstrap_common_pb_76_low", "Bootstrap common 2.5 %"),
    ("bootstrap_common_pb_76_high", "Bootstrap common 97.5 %"),
    ("mswd", "MSWD"),
    ("p_value", "p"),
    ("model", "Model"),
    ("anchor", "Anchor"),
    ("dispersion", "Dispersion"),
    ("dispersion_units", "Dispersion units"),
    ("autocorrelation", "Lag-1 autocorrelation"),
    ("n_effective", "Effective n"),
    ("uncertainty_branch", "Point uncertainties"),
    ("bootstrap_method", "Bootstrap"),
    ("bootstrap_resamples", "Resamples"),
    ("bootstrap_seed", "Seed"),
)


def isochron_fit_table(records: Iterable[dict]):
    """Return a publication table (pandas DataFrame) of fit records.

    ``records`` are dictionaries from :func:`fit_record`, optionally with the
    keys ``label`` (sample name or selection), ``view`` ("spot means" or
    "depth-profile intervals"), ``uncertainty_branch`` and ``items`` added by
    the caller. Uncertainties are given at two standard deviations; bootstrap
    columns are the percentile interval of the stored confidence level.
    """
    import pandas as pd

    rows = []
    for record in records:
        row = dict(record)
        for name in ("lower_age", "upper_age"):
            row[f"{name}_2s"] = 2 * row.get(f"{name}_1s", np.nan)
            row[f"{name}_2s_with_decay"] = 2 * row.get(f"{name}_1s_with_decay", np.nan)
        row["common_207Pb_206Pb_2s"] = 2 * row.get("common_207Pb_206Pb_1s", np.nan)
        row.setdefault("autocorrelation", np.nan)
        row.setdefault("n_effective", np.nan)
        rows.append(row)
    frame = pd.DataFrame(rows)
    for key, _label in _TABLE_COLUMNS:
        if key not in frame:
            frame[key] = np.nan
    return frame[[key for key, _ in _TABLE_COLUMNS]].rename(columns=dict(_TABLE_COLUMNS))


def isochron_footnotes(records: Iterable[dict]) -> list[str]:
    """Return numbered-footnote texts describing how the fits were made."""
    records = list(records)
    if not records:
        return []
    notes = [
        "Lines were fitted to correlated-error data by maximum likelihood (York et al. 2004; "
        "Ludwig 1998); model 1 uses the analytical uncertainties only and its uncertainties are "
        "multiplied by sqrt(MSWD) when p < 0.05; model 2 is total least squares in "
        "238U/206Pb-207Pb/206Pb coordinates ignoring analytical uncertainties; model 3 adds a "
        "dispersion term estimated with the line (Vermeesch 2018). Line-parameter uncertainties "
        "are from the curvature of the likelihood; intercept-age uncertainties are propagated to "
        "first order.",
        "Intercepts are where the fitted line meets concordia computed with "
        + (DecayConstants(**records[0]["decay_constants"]).describe() if "decay_constants" in records[0]
           else DEFAULT_DECAY.describe())
        + "; 'Lower ±2s incl. decay constants' adds the decay-constant (and, for Tera-Wasserburg, "
          "238U/235U) uncertainties.",
    ]
    anchors = sorted({str(r.get("anchor", "none")) for r in records} - {"none"})
    if anchors:
        notes.append("Anchors: " + "; ".join(anchors) + ". Stacey and Kramers (1975) common Pb is "
                     "evaluated at the fitted age and solved together with the line.")
    if any(str(r.get("bootstrap_method", "")) for r in records):
        notes.append("Bootstrap intervals are 2.5 and 97.5 percentiles of refitted resamples; "
                     "'parametric' redraws every point from its error ellipse (as Isoplot.jl), "
                     "'resample' draws whole acquisitions with replacement. The seed and the number "
                     "of resamples reproduce each interval.")
    if any(np.isfinite(r.get("autocorrelation", np.nan)) for r in records):
        notes.append("Depth-profile intervals of one acquisition are autocorrelated; the line "
                     "covariance is multiplied by (1 + phi)/(1 - phi), phi being the pooled lag-1 "
                     "autocorrelation of standardised residuals, and the effective n is n(1 - phi)/(1 + phi).")
    notes.append("n of N: analyses (or intervals) used of those in the fitted set; analyses excluded "
                 "in review are listed in the fit record and omitted from the fit.")
    return notes


def write_isochron_fits(records: Iterable[dict], directory, stem: str) -> list:
    """Write ``<stem>.csv``, ``<stem>.md`` and ``<stem>_footnotes.txt``."""
    from pathlib import Path

    records = list(records)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    table = isochron_fit_table(records)
    notes = isochron_footnotes(records)
    csv_path = directory / f"{stem}.csv"
    table.to_csv(csv_path, index=False)
    md_path = directory / f"{stem}.md"
    lines = ["| " + " | ".join(map(str, table.columns)) + " |",
             "|" + "---|" * len(table.columns)]

    def cell(value):
        if isinstance(value, (float, np.floating)):
            return "" if not np.isfinite(value) else f"{value:.6g}"
        return str(value)

    for _, row in table.iterrows():
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    lines += ["", *[f"[{i}] {text}" for i, text in enumerate(notes, 1)]]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    notes_path = directory / f"{stem}_footnotes.txt"
    notes_path.write_text("\n\n".join(f"[{i}] {t}" for i, t in enumerate(notes, 1)) + "\n", encoding="utf-8")
    return [csv_path, md_path, notes_path]
