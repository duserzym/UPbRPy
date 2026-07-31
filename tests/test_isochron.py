from dataclasses import replace

import numpy as np

from UPbRPy.isochron import (
    line_concordia_derivative,
    concordia_intercepts,
    concordia_age,
    concordia_age_from_frame,
    concordia_age_summary,
    fit_discordia,
    fit_summary,
    monte_carlo_discordia_intercepts,
    monte_carlo_summary_frame,
    tera_wasserburg_point,
    tw_depth_profile_sweep_ages,
    tw_single_sweep_intercept,
    wetherill_point,
)


def test_wetherill_discordia_intercepts_recover_synthetic_ages():
    lower_age = 100.0
    upper_age = 1100.0
    x1, y1 = wetherill_point(lower_age)
    x2, y2 = wetherill_point(upper_age)
    slope = (y2 - y1) / (x2 - x1)
    intercept = y1 - slope * x1
    x = np.linspace(x1, x2, 8)
    y = intercept + slope * x
    sx = np.full_like(x, 0.002)
    sy = np.full_like(y, 0.0004)

    line, ages = fit_discordia(x, y, sx, sy, 0.0, space="wetherill")

    assert np.isclose(line.p_value, 1.0)
    assert len(ages) == 2
    assert ages[0].kind == "lower"
    assert ages[1].kind == "upper"
    np.testing.assert_allclose(ages[0].age_ma, lower_age, atol=1e-6)
    np.testing.assert_allclose(ages[1].age_ma, upper_age, atol=1e-6)


def test_tera_wasserburg_discordia_intercept_recovers_synthetic_age():
    lower_age = 220.0
    x_conc, y_conc = tera_wasserburg_point(lower_age)
    common_pb_76 = 0.84
    x = np.linspace(0.0, x_conc, 8)[1:]
    y = common_pb_76 + (y_conc - common_pb_76) / x_conc * x
    sx = np.full_like(x, 0.2)
    sy = np.full_like(y, 0.002)

    line, ages = fit_discordia(x, y, sx, sy, 0.0, space="tw")

    assert np.isclose(line.intercept, common_pb_76)
    assert len(ages) == 1
    assert ages[0].kind == "lower"
    np.testing.assert_allclose(ages[0].age_ma, lower_age, atol=1e-6)


def test_anchored_tw_fit_accepts_anchor_covariance():
    age = 260.0
    x_conc, y_conc = tera_wasserburg_point(age)
    initial_76 = 0.84
    fraction = np.linspace(0.45, 0.90, 7)
    x = x_conc * fraction
    y = initial_76 + (y_conc - initial_76) * fraction
    sx = np.full_like(x, 0.12)
    sy = np.full_like(y, 0.0015)
    anchor = (0.0, initial_76)
    anchor_cov = np.array([[0.0, 0.0], [0.0, 0.006**2]])

    fixed_line, fixed_ages = fit_discordia(
        x,
        y,
        sx,
        sy,
        0.0,
        space="tw",
        anchor_point=anchor,
    )
    uncertain_line, uncertain_ages = fit_discordia(
        x,
        y,
        sx,
        sy,
        0.0,
        space="tw",
        anchor_point=anchor,
        anchor_cov=anchor_cov,
    )

    np.testing.assert_allclose(uncertain_ages[0].age_ma, age, atol=0.05)
    assert uncertain_line.intercept_sigma > fixed_line.intercept_sigma
    assert uncertain_ages[0].sigma_ma > fixed_ages[0].sigma_ma


def test_single_sweep_tw_intercept_recovers_age_and_propagates_initial_pb():
    age = 260.0
    x, y = tera_wasserburg_point(age)
    initial_76 = 0.84
    mixed_y = initial_76 + (y - initial_76) * 0.72
    mixed_x = x * 0.72

    no_initial_uncertainty = tw_single_sweep_intercept(
        mixed_x,
        mixed_y,
        0.12,
        0.0015,
        initial_pb207_pb206=initial_76,
    )
    with_initial_uncertainty = tw_single_sweep_intercept(
        mixed_x,
        mixed_y,
        0.12,
        0.0015,
        initial_pb207_pb206=initial_76,
        sigma_initial_pb207_pb206=0.003,
    )

    assert no_initial_uncertainty.lower_intercept is not None
    assert with_initial_uncertainty.lower_intercept is not None
    np.testing.assert_allclose(no_initial_uncertainty.lower_intercept.age_ma, age, atol=1e-6)
    assert (
        with_initial_uncertainty.lower_intercept.sigma_ma
        > no_initial_uncertainty.lower_intercept.sigma_ma
    )


def test_monte_carlo_anchored_fit_samples_anchor_covariance():
    age = 240.0
    x_conc, y_conc = tera_wasserburg_point(age)
    initial_76 = 0.82
    fraction = np.linspace(0.50, 0.95, 8)
    x = x_conc * fraction
    y = initial_76 + (y_conc - initial_76) * fraction
    sx = np.full_like(x, 0.16)
    sy = np.full_like(y, 0.0018)
    anchor = (0.0, initial_76)
    anchor_cov = np.array([[0.0, 0.0], [0.0, 0.012**2]])

    fixed = monte_carlo_discordia_intercepts(
        x,
        y,
        sx,
        sy,
        0.0,
        space="tw",
        n=500,
        anchor_point=anchor,
        random_state=123,
    )
    uncertain = monte_carlo_discordia_intercepts(
        x,
        y,
        sx,
        sy,
        0.0,
        space="tw",
        n=500,
        anchor_point=anchor,
        anchor_cov=anchor_cov,
        random_state=123,
    )

    assert np.isfinite(uncertain.lower_ma).sum() > 450
    assert np.nanstd(uncertain.lower_ma) > np.nanstd(fixed.lower_ma)


def test_depth_profile_sweep_ages_add_expected_columns_and_mc_summary():
    age = 180.0
    x, y = tera_wasserburg_point(age)
    initial_76 = 0.82
    fraction = np.array([0.55, 0.75, 0.95])
    frame = {
        "Time": [1.0, 2.0, 3.0],
        "spot_name": ["A", "A", "A"],
        "238U/206Pb_fractionation_corrected": x * fraction,
        "207Pb/206Pb_fractionation_corrected": initial_76 + (y - initial_76) * fraction,
        "238U/206Pb_fractionation_corrected_2e": np.full(3, 0.20),
        "207Pb/206Pb_fractionation_corrected_2e": np.full(3, 0.002),
    }

    out = tw_depth_profile_sweep_ages(
        frame,
        initial_pb207_pb206=initial_76,
        monte_carlo_n=100,
        random_state=123,
    )

    assert "tw_lower_intercept_age" in out
    assert "tw_lower_intercept_mc_median" in out
    np.testing.assert_allclose(out["tw_lower_intercept_age"], age, atol=1e-6)
    assert np.all(np.isfinite(out["tw_lower_intercept_age_2s"]))
    assert np.all(out["tw_lower_intercept_mc_n"] > 0)


def test_depth_profile_sweep_ages_accept_per_row_initial_pb_columns():
    ages = np.array([160.0, 220.0, 300.0])
    initial = np.array([0.80, 0.84, 0.88])
    fractions = np.array([0.60, 0.70, 0.80])
    x_conc, y_conc = tera_wasserburg_point(ages)
    frame = {
        "238U/206Pb_fractionation_corrected": x_conc * fractions,
        "207Pb/206Pb_fractionation_corrected": initial + (y_conc - initial) * fractions,
        "238U/206Pb_fractionation_corrected_2e": np.full(3, 0.20),
        "207Pb/206Pb_fractionation_corrected_2e": np.full(3, 0.002),
        "initial_76": initial,
        "initial_76_2e": np.full(3, 0.004),
    }

    out = tw_depth_profile_sweep_ages(
        frame,
        initial_pb207_pb206_col="initial_76",
        sigma_initial_pb207_pb206_col="initial_76_2e",
    )

    np.testing.assert_allclose(out["tw_initial_207Pb_206Pb"], initial)
    np.testing.assert_allclose(out["tw_lower_intercept_age"], ages, atol=1e-6)
    assert np.all(out["tw_lower_intercept_age_2s"] > 0)


def test_depth_profile_initial_pb_uncertainty_level_is_explicit():
    age = 230.0
    initial = 0.905
    x_conc, y_conc = tera_wasserburg_point(age)
    fraction = 0.7
    frame = {
        "238U/206Pb_fractionation_corrected": [x_conc * fraction],
        "207Pb/206Pb_fractionation_corrected": [
            initial + (y_conc - initial) * fraction
        ],
        "238U/206Pb_fractionation_corrected_2e": [0.20],
        "207Pb/206Pb_fractionation_corrected_2e": [0.002],
    }

    from_two_sigma = tw_depth_profile_sweep_ages(
        frame,
        initial_pb207_pb206=initial,
        sigma_initial_pb207_pb206=0.00006,
        initial_uncertainties_are_2sigma=True,
    )
    from_one_sigma = tw_depth_profile_sweep_ages(
        frame,
        initial_pb207_pb206=initial,
        sigma_initial_pb207_pb206=0.00003,
    )

    np.testing.assert_allclose(
        from_two_sigma["tw_initial_207Pb_206Pb_1s"],
        0.00003,
    )
    np.testing.assert_allclose(
        from_two_sigma["tw_lower_intercept_age_2s"],
        from_one_sigma["tw_lower_intercept_age_2s"],
    )


def test_vectorized_depth_profile_intercepts_match_scalar_solver():
    ages = np.array([85.0, 180.0, 420.0, 950.0, 1700.0])
    initial = np.array([0.79, 0.82, 0.85, 0.88, 0.91])
    fractions = np.array([0.45, 0.58, 0.70, 0.82, 0.93])
    x_concordia, y_concordia = tera_wasserburg_point(ages)
    frame = {
        "238U/206Pb_fractionation_corrected": x_concordia * fractions,
        "207Pb/206Pb_fractionation_corrected": (
            initial + (y_concordia - initial) * fractions
        ),
        "238U/206Pb_fractionation_corrected_2e": np.linspace(0.16, 0.30, 5),
        "207Pb/206Pb_fractionation_corrected_2e": np.linspace(0.0015, 0.003, 5),
        "rho": np.linspace(-0.4, 0.4, 5),
        "initial": initial,
        "initial_1s": np.linspace(0.0004, 0.0012, 5),
    }

    vectorized = tw_depth_profile_sweep_ages(
        frame,
        initial_pb207_pb206_col="initial",
        sigma_initial_pb207_pb206_col="initial_1s",
        rho_col="rho",
    )
    scalar = [
        tw_single_sweep_intercept(
            frame["238U/206Pb_fractionation_corrected"][index],
            frame["207Pb/206Pb_fractionation_corrected"][index],
            frame["238U/206Pb_fractionation_corrected_2e"][index] / 2.0,
            frame["207Pb/206Pb_fractionation_corrected_2e"][index] / 2.0,
            frame["rho"][index],
            initial_pb207_pb206=initial[index],
            sigma_initial_pb207_pb206=frame["initial_1s"][index],
        )
        for index in range(len(ages))
    ]

    np.testing.assert_allclose(
        vectorized["tw_lower_intercept_age"],
        [result.lower_intercept.age_ma for result in scalar],
        rtol=0,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        vectorized["tw_lower_intercept_age_1s"],
        [result.lower_intercept.sigma_ma for result in scalar],
        rtol=1e-12,
        atol=1e-12,
    )
    np.testing.assert_allclose(
        vectorized["tw_line_slope"],
        [result.line.slope for result in scalar],
        rtol=0,
        atol=0,
    )


def test_fit_summary_and_mc_summary_frame_are_flat_and_exportable():
    lower_age = 100.0
    upper_age = 1100.0
    x1, y1 = wetherill_point(lower_age)
    x2, y2 = wetherill_point(upper_age)
    slope = (y2 - y1) / (x2 - x1)
    intercept = y1 - slope * x1
    x = np.linspace(x1, x2, 6)
    y = intercept + slope * x
    sx = np.full_like(x, 0.002)
    sy = np.full_like(y, 0.0004)

    line, ages = fit_discordia(x, y, sx, sy, 0.0, space="wetherill")
    summary = fit_summary(line, ages)
    mc = monte_carlo_discordia_intercepts(
        x,
        y,
        sx,
        sy,
        0.0,
        space="wetherill",
        n=50,
        random_state=123,
    )
    mc_frame = monte_carlo_summary_frame(mc)

    assert "line_slope" in summary
    assert "lower_intercept_age" in summary
    assert "upper_intercept_age_2s" in summary
    assert set(mc_frame["intercept"]) == {"lower", "upper"}
    assert np.all(mc_frame["n_finite"] > 0)


def test_taylor_intercept_uncertainty_matches_symmetric_line_perturbation():
    lower_age = 100.0
    upper_age = 1100.0
    x1, y1 = wetherill_point(lower_age)
    x2, y2 = wetherill_point(upper_age)
    slope = (y2 - y1) / (x2 - x1)
    intercept = y1 - slope * x1
    x = np.linspace(x1, x2, 10)
    y = intercept + slope * x
    sx = np.full_like(x, 0.002)
    sy = np.full_like(y, 0.0004)

    line, ages = fit_discordia(x, y, sx, sy, 0.0, space="wetherill")
    upper = ages[1]
    slope_step = line.slope_sigma * 1e-3

    plus_line = replace(line, slope=line.slope + slope_step)
    minus_line = replace(line, slope=line.slope - slope_step)

    finite_difference = (
        concordia_intercepts(plus_line, space="wetherill")[1].age_ma
        - concordia_intercepts(minus_line, space="wetherill")[1].age_ma
    ) / (2 * slope_step)
    taylor_sensitivity = upper.x / line_concordia_derivative(
        upper.age_ma,
        line,
        "wetherill",
    )

    assert np.isfinite(finite_difference)
    np.testing.assert_allclose(finite_difference, taylor_sensitivity, rtol=1e-4)


def test_wetherill_common_concordia_age_recovers_concordant_synthetic_data():
    age = 420.0
    x0, y0 = wetherill_point(age)
    offsets = np.array([-0.25, -0.10, 0.0, 0.10, 0.25])
    sx = np.full(offsets.shape, 0.002)
    sy = np.full(offsets.shape, 0.0004)
    x = x0 + offsets * sx
    y = y0 - offsets * sy

    result = concordia_age(x, y, sx, sy, 0.0, space="wetherill")
    summary = concordia_age_summary(result)

    np.testing.assert_allclose(result.age_ma, age, atol=0.25)
    assert result.sigma_ma > 0
    assert result.df == 2 * len(x) - 1
    assert summary["concordia_age_2s"] == result.two_sigma_ma


def test_tera_wasserburg_common_concordia_age_from_frame_recovers_synthetic_data():
    age = 260.0
    x0, y0 = tera_wasserburg_point(age)
    offsets = np.array([-0.25, 0.0, 0.25, 0.10])
    sx = np.full(offsets.shape, 0.15)
    sy = np.full(offsets.shape, 0.0015)
    frame = {
        "238U/206Pb": x0 + offsets * sx,
        "207Pb/206Pb": y0 - offsets * sy,
        "238U/206Pb_2e": 2 * sx,
        "207Pb/206Pb_2e": 2 * sy,
        "err_corr": np.zeros_like(offsets),
    }

    result = concordia_age_from_frame(frame, space="tw")

    np.testing.assert_allclose(result.age_ma, age, atol=0.4)
    assert result.sigma_ma > 0
    assert result.space == "tera_wasserburg"


def test_single_analysis_common_concordia_age_is_supported_in_both_spaces():
    age = 510.0
    wx, wy = wetherill_point(age)
    wetherill_result = concordia_age(
        [wx],
        [wy],
        [0.002],
        [0.0004],
        [0.0],
        space="wetherill",
    )
    tx, ty = tera_wasserburg_point(age)
    tw_result = concordia_age(
        [tx],
        [ty],
        [0.15],
        [0.0015],
        [0.0],
        space="tw",
    )

    np.testing.assert_allclose(wetherill_result.age_ma, age, atol=1e-5)
    np.testing.assert_allclose(tw_result.age_ma, age, atol=1e-5)
    assert wetherill_result.df == 1
    assert tw_result.df == 1
    assert wetherill_result.sigma_ma > 0
    assert tw_result.sigma_ma > 0
