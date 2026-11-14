"""Discordia/isochron regression: references, synthetic truth and edge cases.

Reference values were computed with IsoplotR 6.6 (R) on the synthetic data
below, with IsoplotR's decay constants set to UPbRPy's (lambda238 =
1.55125e-4 /Ma, lambda235 = 9.8485e-4 /Ma, 238U/235U = 137.818), and from the
Isoplot.jl test suite (two-analysis intercepts).
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from UPbRPy.regression import (
    Anchor,
    DecayConstants,
    bootstrap_discordia,
    concordia_xy,
    fit_discordia,
    fit_record,
    intersect_concordia,
    stacey_kramers_76,
    york_fit,
    york_fit_batch,
)

# Synthetic Wetherill data: 14 mixtures of 600 Ma radiogenic Pb and common Pb
# (207Pb/206Pb = 0.86) with 2.5 % and 1.5 % (1 s) errors.
SYNTH_W = np.array([
    [50.2029, 1.36549, 0.533397, 0.008274, 0.609],
    [34.3714, 0.827194, 0.374229, 0.005549, 0.530],
    [30.2244, 0.751511, 0.348465, 0.005166, 0.750],
    [23.8368, 0.62468, 0.307681, 0.004524, 0.309],
    [21.0322, 0.542044, 0.269246, 0.004105, 0.708],
    [20.5161, 0.519495, 0.264709, 0.003991, 0.707],
    [17.7817, 0.429845, 0.239887, 0.003537, 0.366],
    [9.70507, 0.238439, 0.171385, 0.002568, 0.520],
    [7.42159, 0.183241, 0.152024, 0.002289, 0.336],
    [6.94101, 0.175183, 0.146416, 0.002248, 0.711],
    [6.73708, 0.16891, 0.147584, 0.002216, 0.710],
    [5.66123, 0.146192, 0.137175, 0.002101, 0.527],
    [5.25255, 0.134621, 0.135832, 0.002043, 0.712],
    [2.81148, 0.068873, 0.114205, 0.00171, 0.535],
])


def synth():
    x, sx, y, sy, rho = SYNTH_W.T
    return x, y, sx, sy, rho


def tw_mixtures(n, *, age=1000.0, common=0.88, f_range=(0.1, 0.9), rel=(0.03, 0.02), rho=0.3,
                seed=0, dispersion_ma=0.0):
    """Tera-Wasserburg mixtures of radiogenic and common Pb with known truth."""
    rng = np.random.default_rng(seed)
    f = rng.uniform(*f_range, n)
    ages = age + dispersion_ma * rng.standard_normal(n)
    X0, Y0 = concordia_xy(ages, "tera_wasserburg")
    X = f * X0
    Y = common + (Y0 - common) * f
    sx, sy = rel[0] * X, rel[1] * Y
    r = np.full(n, rho)
    z1, z2 = rng.standard_normal((2, n))
    return X + sx * z1, Y + sy * (r * z1 + np.sqrt(1 - r * r) * z2), sx, sy, r


# --- IsoplotR 6.6 references (ludwig(), format 1) -----------------------------

ISOPLOTR = {
    # key: (t, sigma_t, a0, sigma_a0, mswd)
    (1, "none"): (590.117478433099, np.sqrt(32.9445098514324), 0.844390094312226, np.sqrt(7.5520056221502e-05), 1.22950140068041),
    (2, "none"): (587.560226573965, np.sqrt(58.5598793405429), 0.838684822167613, np.sqrt(0.000100522373151815), None),
    (3, "none"): (590.117680382699, np.sqrt(32.9445820754372), 0.844391998911007, np.sqrt(7.55204312646153e-05), None),
    (1, "common"): (597.634886103156, np.sqrt(14.8967686726025), 0.86, 0.0, 1.37976059281493),
    (1, "sk"): (605.417064069227, np.sqrt(15.8507029180502), 0.877603871280609, None, 2.2270521972185),
    (1, "age"): (600.0, 0.0, 0.855487906125273, np.sqrt(3.52841181647088e-05), 1.36446780650921),
}
ANCHORS = {"none": Anchor(), "common": Anchor("common_pb", 0.86), "sk": Anchor("stacey_kramers"),
           "age": Anchor("age", 600.0)}


@pytest.mark.parametrize("model,anchor", list(ISOPLOTR))
def test_wetherill_fits_match_isoplotr(model, anchor):
    t, st, a0, sa0, mswd = ISOPLOTR[(model, anchor)]
    fit = fit_discordia(*synth(), space="wetherill", model=model, anchor=ANCHORS[anchor])
    ages = [item for item in (fit.lower, fit.upper) if item is not None]
    age = min(ages, key=lambda item: abs(item.age_ma - t))
    # IsoplotR reports unscaled (model-1) uncertainties from ludwig().
    scale = np.sqrt(fit.line.mswd) if fit.line.overdispersed else 1.0
    assert age.age_ma == pytest.approx(t, rel=2e-4)
    if st > 0:
        assert age.sigma_ma / scale == pytest.approx(st, rel=0.01)
    else:
        assert age.sigma_ma == 0
    assert fit.common_pb_76 == pytest.approx(a0, rel=2e-4)
    if sa0:
        assert fit.common_pb_76_sigma / scale == pytest.approx(sa0, rel=0.02)
    if mswd is not None:
        # Stacey-Kramers: IsoplotR's MSWD differs by 0.2 % (its model curve
        # is evaluated slightly differently); the ages agree to 2e-5.
        assert fit.line.mswd == pytest.approx(mswd, rel=5e-3 if anchor == "sk" else 1e-3)


def test_york_matches_isoplotr_on_pearson_york_data():
    x = np.array([0.0, 0.9, 1.8, 2.6, 3.3, 4.4, 5.2, 6.1, 6.5, 7.4])
    y = np.array([5.9, 5.4, 4.4, 4.6, 3.5, 3.7, 2.8, 2.8, 2.4, 1.5])
    wx = np.array([1000, 1000, 500, 800, 200, 80, 60, 20, 1.8, 1])
    wy = np.array([1, 1.8, 4, 8, 20, 20, 70, 70, 100, 500])
    line = york_fit(x, y, 1 / np.sqrt(wx), 1 / np.sqrt(wy), 0.0)
    assert line.intercept == pytest.approx(5.47991022414368, rel=1e-9)
    assert line.slope == pytest.approx(-0.480533407465674, rel=1e-9)
    assert np.sqrt(line.cov[0, 0]) == pytest.approx(0.294970735337994, rel=1e-6)
    assert np.sqrt(line.cov[1, 1]) == pytest.approx(0.0579850089558614, rel=1e-6)
    assert line.cov[0, 1] == pytest.approx(-0.0164725446364712, rel=1e-6)
    assert line.mswd == pytest.approx(1.48329415010509, rel=1e-9)
    assert line.p_value == pytest.approx(0.157267228378567, rel=1e-6)


def test_isoplot_jl_two_analysis_intercepts():
    """Isoplot.jl test suite: two analyses, Monte Carlo intercepts."""
    decay = DecayConstants(lambda_238=np.log(2) / 4.4683e3, lambda_235=9.8569e-4)
    x = np.array([22.6602, 33.6602])
    y = np.array([0.40864, 0.50864])
    sx, sy, rho = np.full(2, 0.0175), np.full(2, 0.00017), np.full(2, 0.83183)
    fit = fit_discordia(x, y, sx, sy, rho, space="wetherill", decay=decay)
    assert fit.line.df == 0 and np.isnan(fit.line.mswd)
    assert fit.lower.age_ma == pytest.approx(1318.12, abs=0.05)
    assert fit.upper.age_ma == pytest.approx(4601.82, abs=0.05)
    assert fit.lower.sigma_ma == pytest.approx(2.04, abs=0.05)
    assert fit.upper.sigma_ma == pytest.approx(1.53, abs=0.05)
    boot = bootstrap_discordia(x, y, sx, sy, rho, space="wetherill", decay=decay, n_resamples=20000,
                               seed=3, fit=fit)
    assert boot.interval("lower_ma")["mean"] == pytest.approx(1318.12, abs=0.1)
    assert boot.interval("lower_ma")["sd"] == pytest.approx(2.04, abs=0.1)
    assert boot.interval("upper_ma")["mean"] == pytest.approx(4601.82, abs=0.1)
    assert boot.interval("upper_ma")["sd"] == pytest.approx(1.53, abs=0.1)


# --- Synthetic truth -------------------------------------------------------------


def test_analytical_uncertainty_matches_monte_carlo_truth():
    """Spread of fitted ages over replicate data sets equals the analytical sigma."""
    ages, sigmas, commons = [], [], []
    for seed in range(400):
        fit = fit_discordia(*tw_mixtures(40, seed=seed), space="tera_wasserburg")
        ages.append(fit.lower.age_ma)
        sigmas.append(fit.lower.sigma_ma)
        commons.append(fit.common_pb_76)
    ages = np.array(ages)
    assert np.mean(ages) == pytest.approx(1000.0, abs=3 * np.std(ages) / np.sqrt(ages.size) + 2)
    assert np.std(ages, ddof=1) == pytest.approx(np.median(sigmas), rel=0.12)
    assert np.mean(commons) == pytest.approx(0.88, abs=0.002)


def test_bootstrap_agrees_with_analytical_uncertainty():
    data = tw_mixtures(50, seed=7)
    fit = fit_discordia(*data, space="tera_wasserburg")
    boot = bootstrap_discordia(*data, space="tera_wasserburg", n_resamples=4000, seed=11, fit=fit)
    lower = boot.interval("lower_ma")
    assert lower["median"] == pytest.approx(fit.lower.age_ma, abs=0.2 * fit.lower.sigma_ma)
    half_width = (lower["high"] - lower["low"]) / 2
    assert half_width == pytest.approx(1.96 * fit.lower.sigma_ma, rel=0.15)
    common = boot.interval("common_pb_76")
    assert (common["high"] - common["low"]) / 2 == pytest.approx(1.96 * fit.common_pb_76_sigma, rel=0.15)
    resampled = bootstrap_discordia(*data, space="tera_wasserburg", n_resamples=4000, seed=11,
                                    method="resample", fit=fit)
    assert resampled.interval("lower_ma")["median"] == pytest.approx(fit.lower.age_ma,
                                                                      abs=0.3 * fit.lower.sigma_ma)


@pytest.mark.slow
def test_overdispersion_models():
    data = tw_mixtures(60, seed=5, dispersion_ma=60.0, rel=(0.01, 0.006))
    m1 = fit_discordia(*data, space="tera_wasserburg", model=1)
    assert m1.line.mswd > 2 and m1.line.overdispersed
    np.testing.assert_allclose(m1.line.cov_reported, m1.line.cov * m1.line.mswd)
    m3 = fit_discordia(*data, space="tera_wasserburg", model=3)
    assert m3.line.dispersion_units == "Ma"
    assert 20 < m3.line.dispersion < 150
    assert m3.lower.age_ma == pytest.approx(1000, abs=4 * m3.lower.sigma_ma)
    m2 = fit_discordia(*data, space="tera_wasserburg", model=2)
    assert m2.lower.age_ma == pytest.approx(1000, abs=4 * m2.lower.sigma_ma)
    assert "model 3" in m3.method_text() and "model 2" in m2.method_text()


def test_tera_wasserburg_and_wetherill_give_the_same_line_for_exact_points():
    ages = np.array([1000.0])
    X0, Y0 = concordia_xy(ages, "tera_wasserburg")
    f = np.linspace(0.1, 0.9, 9)
    X, Y = f * X0, 0.88 + (Y0 - 0.88) * f
    u = DecayConstants().u238_u235
    y = 1 / X
    x = Y * u * y
    tw = fit_discordia(X, Y, 0.01 * X, 0.01 * Y, 0.0, space="tera_wasserburg")
    w = fit_discordia(x, y, 0.01 * x, 0.01 * y, 0.0, space="wetherill")
    assert tw.lower.age_ma == pytest.approx(1000.0, rel=1e-9)
    assert w.lower.age_ma == pytest.approx(1000.0, rel=1e-9)
    assert tw.common_pb_76 == pytest.approx(0.88, rel=1e-9)
    assert w.common_pb_76 == pytest.approx(0.88, rel=1e-9)


def test_anchors():
    data = tw_mixtures(30, seed=2)
    common = fit_discordia(*data, anchor=Anchor("common_pb", 0.88))
    assert common.line.intercept == 0.88 and common.common_pb_76_sigma == 0
    assert common.line.df == 29
    prior = fit_discordia(*data, anchor=Anchor("common_pb", 0.88, 0.005))
    free = fit_discordia(*data)
    assert min(0.88, free.common_pb_76) - 1e-6 <= prior.common_pb_76 <= max(0.88, free.common_pb_76) + 1e-6
    age = fit_discordia(*data, anchor=Anchor("age", 1000.0))
    X, Y = concordia_xy(1000.0, "tera_wasserburg")
    assert age.line.intercept + age.line.slope * X == pytest.approx(Y, rel=1e-12)
    assert age.lower.age_ma == pytest.approx(1000.0) and age.lower.sigma_ma == 0
    sk = fit_discordia(*data, anchor=Anchor("stacey_kramers"))
    assert sk.common_pb_76 == pytest.approx(float(stacey_kramers_76(sk.lower.age_ma)), rel=1e-6)
    assert sk.line.intercept == pytest.approx(sk.common_pb_76, rel=1e-6)
    with pytest.raises(ValueError):
        Anchor("age")


def test_decay_constant_uncertainty_is_added_on_request():
    data = tw_mixtures(30, seed=4, rel=(0.002, 0.001))
    plain = fit_discordia(*data)
    with_decay = fit_discordia(*data, include_decay_uncertainty=True)
    assert with_decay.lower.sigma_ma > plain.lower.sigma_ma
    assert plain.lower.sigma_with_decay_ma == pytest.approx(with_decay.lower.sigma_ma)
    boot = bootstrap_discordia(*data, n_resamples=2000, include_decay_uncertainty=True, seed=1)
    assert boot.interval("lower_ma")["sd"] > 0


# --- Degenerate cases -----------------------------------------------------------


def test_two_points_vertical_cluster_and_no_intersection():
    two = fit_discordia([2.0, 4.0], [0.5, 0.3], [0.02, 0.04], [0.005, 0.004], 0.1)
    assert two.line.df == 0 and np.isnan(two.line.mswd) and two.lower is not None
    rng = np.random.default_rng(0)
    # All points at one 238U/206Pb (a vertical cluster): the slope is ill
    # defined, the fit must not raise and its uncertainties are large.
    x = 5.0 + 0.001 * rng.standard_normal(10)
    y = rng.uniform(0.2, 0.8, 10)
    vertical = fit_discordia(x, y, np.full(10, 0.05), np.full(10, 0.01), 0.0)
    assert not np.isfinite(vertical.line.slope_sigma) or vertical.line.slope_sigma > 1
    # A line above the whole of concordia (207Pb/206Pb > 1.5) never meets it.
    never = fit_discordia([1, 2, 3, 4.0], [1.6, 1.7, 1.8, 1.9], 0.01, 0.01, 0.0)
    assert never.lower is None and never.upper is None and never.notes
    with pytest.raises(ValueError):
        fit_discordia([1.0], [0.5], 0.1, 0.1)


def test_negative_wetherill_lower_intercept():
    # The chord between concordia at -50 Ma (below the origin) and 2000 Ma.
    (xa, ya), (xb, yb) = (concordia_xy(t, "wetherill") for t in (-50.0, 2000.0))
    slope = (yb - ya) / (xb - xa)
    x = np.linspace(0.2, 0.9, 6) * xb
    y = ya + slope * (x - xa)
    fit = fit_discordia(x, y, 0.005 * x, 0.005 * y, 0.5, space="wetherill")
    assert fit.lower.age_ma < 0 and fit.lower.note == "negative age"
    assert fit.upper.age_ma == pytest.approx(2000.0, rel=1e-6)
    assert intersect_concordia(fit.line.intercept, fit.line.slope, "wetherill")[0] < 0


def test_york_damping_handles_near_unit_correlation():
    rng = np.random.default_rng(9)
    x = rng.uniform(1, 10, 30)
    y = 0.2 + 0.05 * x + 0.01 * rng.standard_normal(30)
    sx = 0.05 * x
    sy = 0.01 + 0.002 * x
    rho = rng.uniform(0.0, 0.99999, 30)
    a, b, ok = york_fit_batch(x[None], y[None], sx[None], sy[None], rho[None])
    assert ok[0]
    # The York solution is the minimum of the weighted sum of squares.
    def S(a, b):
        v = sy ** 2 + b * b * sx ** 2 - 2 * b * rho * sx * sy
        return np.sum((y - a - b * x) ** 2 / v)
    from scipy.optimize import minimize
    best = minimize(lambda p: S(*p), [a[0], b[0]], method="Nelder-Mead",
                    options={"xatol": 1e-14, "fatol": 1e-14, "maxiter": 20000})
    assert S(a[0], b[0]) <= best.fun + 1e-9


# --- Bootstrap mechanics -----------------------------------------------------------


def test_bootstrap_is_reproducible_and_fast():
    data = tw_mixtures(50, seed=1)
    fit = fit_discordia(*data)
    bootstrap_discordia(*data, n_resamples=200, seed=5, fit=fit)  # warm-up
    start = time.perf_counter()
    first = bootstrap_discordia(*data, n_resamples=2000, seed=5, fit=fit)
    elapsed = time.perf_counter() - start
    second = bootstrap_discordia(*data, n_resamples=2000, seed=5, fit=fit)
    np.testing.assert_array_equal(first.lower_ma, second.lower_ma)
    assert first.seed == 5 and first.n_resamples == 2000
    assert first.converged.mean() > 0.99
    assert elapsed < 1.0  # about 0.02 s on an M4 Pro
    record = fit_record(fit, first)
    assert record["bootstrap_seed"] == 5 and record["bootstrap_resamples"] == 2000


def test_block_bootstrap_by_acquisition_is_wider_for_autocorrelated_intervals():
    rng = np.random.default_rng(12)
    acquisitions = 12
    per = 25
    base = tw_mixtures(acquisitions, seed=12, rel=(0.0, 0.0))
    x, y, groups = [], [], []
    for k in range(acquisitions):
        # Successive intervals share a strongly autocorrelated offset.
        offset = np.cumsum(rng.normal(0, 0.004, per))
        x.append(base[0][k] * (1 + 0.01 * rng.standard_normal(per)))
        y.append(base[1][k] + offset)
        groups += [k] * per
    x, y = np.concatenate(x), np.concatenate(y)
    sx, sy = 0.01 * x, np.full_like(y, 0.004)
    fit = fit_discordia(x, y, sx, sy, 0.0)
    block = bootstrap_discordia(x, y, sx, sy, 0.0, method="resample", groups=groups,
                                n_resamples=1500, seed=2, fit=fit)
    naive = bootstrap_discordia(x, y, sx, sy, 0.0, method="resample", n_resamples=1500, seed=2, fit=fit)
    width = lambda b: np.subtract(*reversed([b.interval("lower_ma")[k] for k in ("low", "high")]))
    assert width(block) > 1.3 * width(naive)


@pytest.mark.parametrize("anchor", [Anchor("common_pb", 0.88), Anchor("common_pb", 0.88, 0.01),
                                    Anchor("age", 1000.0), Anchor("age", 1000.0, 20.0),
                                    Anchor("stacey_kramers")])
@pytest.mark.parametrize("space", ["tera_wasserburg", "wetherill"])
def test_anchored_bootstrap_runs(anchor, space):
    X, Y, sX, sY, r = tw_mixtures(30, seed=3)
    if space == "wetherill":
        u = DecayConstants().u238_u235
        y = 1 / X
        x = Y * u * y
        X, Y, sX, sY = x, y, 0.03 * x, 0.02 * y
    fit = fit_discordia(X, Y, sX, sY, r, space=space, anchor=anchor)
    boot = bootstrap_discordia(X, Y, sX, sY, r, space=space, anchor=anchor, n_resamples=500, seed=0, fit=fit)
    stats = boot.interval("lower_ma")
    assert stats["fraction"] > 0.9
    if anchor.kind == "age" and anchor.sigma == 0:
        # Every resampled line passes through the anchor.
        assert np.nanmax(np.abs(np.where(np.abs(boot.lower_ma - 1000) < 1, boot.lower_ma, boot.upper_ma) - 1000)) < 1e-6


def test_autocorrelated_intervals_inflate_uncertainty():
    rng = np.random.default_rng(21)
    acquisitions, per = 10, 30
    base = tw_mixtures(acquisitions, seed=21, rel=(0.0, 0.0))
    x, y, groups, order = [], [], [], []
    for k in range(acquisitions):
        e = np.zeros(per)
        for t in range(1, per):
            e[t] = 0.8 * e[t - 1] + 0.6 * rng.standard_normal()
        x.append(np.full(per, base[0][k]) * (1 + 0.001 * rng.standard_normal(per)))
        y.append(base[1][k] + 0.01 * e)
        groups += [f"acq{k}"] * per
        order += list(range(per))
    x, y = np.concatenate(x), np.concatenate(y)
    sx, sy = 0.001 * x, np.full_like(y, 0.01)
    plain = fit_discordia(x, y, sx, sy, 0.0)
    grouped = fit_discordia(x, y, sx, sy, 0.0, groups=groups, order=order)
    assert 0.6 < grouped.line.autocorrelation < 0.95
    assert grouped.line.variance_inflation > 3
    np.testing.assert_allclose(grouped.line.cov, plain.line.cov * grouped.line.variance_inflation, rtol=1e-9)
    # Fewer effective points: the chi-square probability is higher.
    assert grouped.line.p_value >= plain.line.p_value
    assert grouped.line.n_effective < x.size / 3 and grouped.notes
    # Shuffling the intervals in time removes the autocorrelation.
    shuffled = fit_discordia(x, y, sx, sy, 0.0, groups=groups, order=rng.permutation(len(order)))
    assert shuffled.line.variance_inflation < grouped.line.variance_inflation


def test_single_acquisition_uses_moving_blocks():
    data = tw_mixtures(40, seed=8)
    boot = bootstrap_discordia(*data, method="resample", groups=["one"] * 40, order=np.arange(40),
                               n_resamples=300, seed=1)
    assert any("Moving-block" in note for note in boot.notes)
    assert boot.interval("lower_ma")["fraction"] > 0.9


def test_fit_records_export_with_footnotes(tmp_path):
    data = tw_mixtures(20, seed=31)
    fit = fit_discordia(*data, anchor=Anchor("stacey_kramers"))
    boot = bootstrap_discordia(*data, anchor=Anchor("stacey_kramers"), n_resamples=300, seed=4, fit=fit)
    record = fit_record(fit, boot)
    record.update(label="Sample A", view="spot means", uncertainty_branch="analytical", items=["a", "b"])
    from UPbRPy.regression import isochron_fit_table, write_isochron_fits

    table = isochron_fit_table([record])
    assert table.loc[0, "Lower ±2s"] == pytest.approx(2 * fit.lower.sigma_ma)
    assert table.loc[0, "Seed"] == 4 and table.loc[0, "Resamples"] == 300
    paths = write_isochron_fits([record], tmp_path, "fits")
    text = (tmp_path / "fits.md").read_text()
    assert "Stacey and Kramers" in text and "Bootstrap intervals" in text
    assert {p.name for p in paths} == {"fits.csv", "fits.md", "fits_footnotes.txt"}


@pytest.mark.parametrize("anchor", [Anchor("common_pb", 0.88, 0.01), Anchor("age", 1000.0, 15.0)])
@pytest.mark.parametrize("model", [1, 2])
def test_bootstrap_of_prior_anchors_tracks_the_estimator(anchor, model):
    data = tw_mixtures(40, seed=13)
    fit = fit_discordia(*data, model=model, anchor=anchor)
    boot = bootstrap_discordia(*data, model=model, anchor=anchor, n_resamples=3000, seed=2, fit=fit)
    stats = boot.interval("lower_ma")
    if model == 1:
        # Centred on the analytical estimate, and with the same spread.
        assert stats["median"] == pytest.approx(fit.lower.age_ma, abs=0.35 * fit.lower.sigma_ma)
        assert (stats["high"] - stats["low"]) / 2 == pytest.approx(1.96 * fit.lower.sigma_ma, rel=0.25)
    else:
        # Model 2 draws the anchor and holds it fixed (an approximation).
        assert stats["low"] < fit.lower.age_ma < stats["high"]
