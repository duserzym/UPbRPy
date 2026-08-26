import numpy as np
import pandas as pd

from UPbRPy import (
    aggregate_ratio,
    align_downhole_traces,
    apply_DH_correction,
    build_downhole_fractionation_metadata,
    calc_raw_ratios,
    calc_depth_profile,
    calc_ratio_mean_and_uncertainty,
    calc_tw_err_corr_from_counts,
    calc_tw_err_corr_from_pairs,
    calc_wetherill_err_corr_from_counts,
    calc_68_date,
    calc_68_date_uncertainty,
    calc_75_date,
    calc_75_date_uncertainty,
    calc_76_date,
    calc_76_date_uncertainty,
    calc_82_date,
    calc_82_date_uncertainty,
    get_wetherill_point,
    propagate_date_covariance,
    propagate_discordance_covariance,
    propagate_product_covariance,
    project_tw_206pb_238u,
    project_tw_238u_206pb,
    propagate_downhole_corrected_ratio_covariance,
    propagate_multiplicative_correction_covariance,
    propagate_reciprocal_covariance,
    propagate_reciprocal_uncertainty,
    evaluate_downhole_fractionation,
    ratio_standard_error,
    ratio_of_means_206pb_204pb,
)


def test_logratio_aggregation_is_reciprocal_and_reports_log_sem():
    ratios = np.array([0.5, 1.0, 2.0, 4.0])

    forward = aggregate_ratio(ratios, "logratio")
    reverse = aggregate_ratio(1 / ratios, "logratio")

    assert np.isclose(forward, np.exp(np.mean(np.log(ratios))))
    assert np.isclose(reverse, 1 / forward)
    assert ratio_standard_error(ratios, "logratio") > 0


def test_tw_sweep_correlation_uses_shared_206pb_denominator():
    n238 = np.array([400.0, 900.0])
    n206 = np.array([100.0, 100.0])
    n207 = np.array([25.0, 100.0])

    result = calc_tw_err_corr_from_counts(n238, n206, n207)
    expected = (1 / n206) / np.sqrt(
        (1 / n238 + 1 / n206) * (1 / n207 + 1 / n206)
    )

    np.testing.assert_allclose(result, expected)
    assert np.all((result > 0) & (result < 1))


def test_wetherill_sweep_correlation_uses_shared_238u_denominator():
    n207 = np.array([25.0, 100.0])
    n206 = np.array([100.0, 225.0])
    n238 = np.array([400.0, 900.0])

    result = calc_wetherill_err_corr_from_counts(n207, n206, n238)
    expected = (1 / n238) / np.sqrt(
        (1 / n207 + 1 / n238) * (1 / n206 + 1 / n238)
    )

    np.testing.assert_allclose(result, expected)


def test_depth_profile_207pb_235u_counting_error_cancels_206pb():
    n206, n207, n238 = 100.0, 25.0, 400.0
    ratio_68 = n206 / n238
    ratio_76 = n207 / n206
    data = pd.DataFrame(
        {
            "sample": ["unknown"],
            "spot_name": ["u1"],
            "Time": [10.0],
            "signal_relative_time": [0.0],
            "206Pb": [n206],
            "207Pb": [n207],
            "238U": [n238],
            "206Pb/238U": [ratio_68],
            "206Pb/238U_2e": [
                2 * ratio_68 * np.sqrt(1 / n206 + 1 / n238)
            ],
            "207Pb/206Pb": [ratio_76],
            "207Pb/206Pb_2e": [
                2 * ratio_76 * np.sqrt(1 / n207 + 1 / n206)
            ],
            "206Pb/238U_fractionation_corrected": [ratio_68],
            "207Pb/206Pb_fractionation_corrected": [ratio_76],
        }
    )
    session = {
        ratio: {
            "fit": np.array([1.0]),
            "std": 0.0,
            "fit_observations": {"time": np.array([9.0, 11.0])},
        }
        for ratio in ("206Pb/238U", "207Pb/206Pb")
    }

    result = calc_depth_profile(data, session).iloc[0]
    expected_relative_2e = 2 * np.sqrt(1 / n207 + 1 / n238)

    assert np.isclose(
        result["207Pb/235U_fractionation_corrected_2e_no_calibration%"],
        100 * expected_relative_2e,
    )
    assert np.isclose(
        result["tw_rho_no_calibration"],
        calc_tw_err_corr_from_counts(n238, n206, n207),
    )


def test_tw_spot_correlation_respects_selected_ratio_estimator():
    ratio_86 = np.array([8.0, 9.0, 11.0, 13.0])
    ratio_76 = np.array([0.051, 0.052, 0.055, 0.058])

    arithmetic = calc_tw_err_corr_from_pairs(
        ratio_86, ratio_76, "arithmetic"
    )
    logratio = calc_tw_err_corr_from_pairs(
        ratio_86, ratio_76, "logratio"
    )

    assert np.isclose(arithmetic, np.corrcoef(ratio_86, ratio_76)[0, 1])
    assert np.isclose(
        logratio,
        np.corrcoef(np.log(ratio_86), np.log(ratio_76))[0, 1],
    )


def test_logratio_spot_summary_uses_geometric_corrected_ratios():
    ratios = ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th")
    data = pd.DataFrame(
        {
            "sample": ["unknown"] * 3,
            "spot_name": ["u1"] * 3,
            "Time": [1.0, 2.0, 3.0],
            "206Pb/238U": [0.1, 0.2, 0.4],
            "207Pb/206Pb": [0.05, 0.06, 0.09],
            "208Pb/232Th": [0.02, 0.03, 0.08],
        }
    )
    session_results = {
        ratio: {
            "fit": np.array([1.0]),
            "std": 0.0,
            "fit_observations": {"time": np.array([0.0, 4.0])},
        }
        for ratio in ratios
    }
    for ratio in ratios:
        data[ratio + "_fractionation_corrected"] = data[ratio]

    summary = calc_ratio_mean_and_uncertainty(
        data,
        session_results,
        ratio_aggregation="logratio",
    )

    assert summary.loc[0, "ratio_aggregation"] == "logratio"
    assert np.isfinite(summary.loc[0, "tw_rho_no_calibration"])
    for ratio in ratios:
        assert np.isclose(
            summary.loc[0, ratio],
            np.exp(np.mean(np.log(data[ratio]))),
        )


def test_206pb_204pb_spot_summary_uses_ratio_of_integrated_means():
    ratios = ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th")
    data = pd.DataFrame(
        {
            "sample": ["unknown", "unknown"],
            "spot_name": ["u1", "u1"],
            "Time": [1.0, 2.0],
            "206Pb": [100.0, 100.0],
            "204Pb": [1.0, 9.0],
            "204Pb_2e": [2.0, 6.0],
            "206Pb/204Pb": [100.0, 100.0 / 9.0],
            "206Pb/204Pb_2e": [20.0, 4.0],
            "206Pb/238U": [0.1, 0.102],
            "207Pb/206Pb": [0.05, 0.051],
            "208Pb/232Th": [0.02, 0.021],
        }
    )
    session_results = {
        ratio: {
            "fit": np.array([1.0]),
            "std": 0.001,
            "fit_observations": {"time": np.array([0.0, 3.0])},
        }
        for ratio in ratios
    }
    for ratio in ratios:
        data[ratio + "_fractionation_corrected"] = data[ratio]

    expected_ratio, expected_1e = ratio_of_means_206pb_204pb(
        data["206Pb"], data["204Pb"], data["204Pb_2e"]
    )
    arithmetic = calc_ratio_mean_and_uncertainty(data, session_results)
    logratio = calc_ratio_mean_and_uncertainty(
        data, session_results, ratio_aggregation="logratio"
    )

    assert np.isclose(expected_ratio, 20.0)
    assert not np.isclose(expected_ratio, data["206Pb/204Pb"].mean())
    for summary in (arithmetic, logratio):
        assert np.isclose(summary.loc[0, "206Pb/204Pb"], expected_ratio)
        assert np.isclose(summary.loc[0, "206Pb/204Pb_2e"], 2 * expected_1e)
        assert summary.loc[0, "206Pb/204Pb_aggregation"] == "ratio_of_means"


def test_raw_ratio_regions_share_signal_start_relative_time():
    experiment = pd.DataFrame(
        {
            "spot_names": ["RM_1"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [2.0],
            "signal_start_times": [3.0],
            "signal_end_times": [8.0],
            "plateau_start_times": [5.0],
            "plateau_end_times": [7.0],
        }
    )
    total = pd.DataFrame(
        {
            "sample": ["RM"] * 10,
            "spot_name": ["RM_1"] * 10,
            "Time": np.arange(10.0),
            "202Hg": np.full(10, 10.0),
            "204Hg": np.full(10, 20.0),
            "206Pb": np.full(10, 100.0),
            "207Pb": np.full(10, 50.0),
            "208Pb": np.full(10, 80.0),
            "232Th": np.full(10, 40.0),
            "238U": np.full(10, 200.0),
        }
    )

    signal = calc_raw_ratios(experiment, total, total, region="signal")
    plateau = calc_raw_ratios(experiment, total, total, region="plateau")

    assert signal["Time"].tolist() == [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    assert signal["signal_relative_time"].tolist() == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert plateau["Time"].tolist() == [5.0, 6.0]
    assert plateau["signal_relative_time"].tolist() == [2.0, 3.0]


def test_raw_ratio_uncertainty_includes_shared_baseline_mean_component():
    """A noisy baseline must increase each sweep's reported ratio error."""
    experiment = pd.DataFrame(
        {
            "spot_names": ["RM_1"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [4.0],
            "signal_start_times": [4.0],
            "signal_end_times": [7.0],
            "plateau_start_times": [4.0],
            "plateau_end_times": [7.0],
        }
    )
    times = np.arange(8.0)
    raw = pd.DataFrame(
        {
            "sample": ["RM"] * 8,
            "spot_name": ["RM_1"] * 8,
            "Time": times,
            "202Hg": [9, 11, 8, 12, 110, 110, 110, 110],
            "204Hg": [18, 22, 16, 24, 220, 220, 220, 220],
            "206Pb": [8, 12, 9, 11, 1010, 1010, 1010, 1010],
            "207Pb": [4, 6, 4, 6, 105, 105, 105, 105],
            "208Pb": [7, 13, 8, 12, 810, 810, 810, 810],
            "232Th": [18, 22, 19, 21, 410, 410, 410, 410],
            "238U": [17, 23, 18, 22, 2010, 2010, 2010, 2010],
        }
    )
    baseline_means = raw.loc[raw["Time"] < 4].drop(
        columns=["sample", "spot_name", "Time"]
    ).mean()
    corrected = raw.copy()
    for channel, mean in baseline_means.items():
        corrected[channel] = corrected[channel] - mean

    ratios = calc_raw_ratios(experiment, raw, corrected, region="signal")

    assert (ratios["206Pb/238U_baseline_2e"] > 0).all()
    assert (
        ratios["206Pb/238U_2e"]
        > ratios["206Pb/238U_baseline_2e"]
    ).all()
    assert "204Pb_counting_2e" in ratios
    assert "204Pb_baseline_2e" in ratios


def test_saved_plateau_filter_excludes_spot_mean_sweep_but_not_signal_profile():
    times = np.arange(25.0)
    primary_signal = 100.0 - 2.0 * times + 0.1 * (times % 2)
    primary_signal[10] += 30.0
    pb206 = np.full(times.size, 100.0)
    pb206[10] = 1000.0
    experiment = pd.DataFrame(
        {
            "spot_names": ["RM_1"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [2.0],
            "signal_start_times": [2.0],
            "signal_end_times": [22.0],
            "plateau_start_times": [2.0],
            "plateau_end_times": [22.0],
            "plateau_filter_method": ["excel_2sigma"],
            "plateau_filter_channel": ["29Si"],
        }
    )
    total = pd.DataFrame(
        {
            "sample": ["RM"] * times.size,
            "spot_name": ["RM_1"] * times.size,
            "Time": times,
            "29Si": primary_signal,
            "202Hg": np.full(times.size, 10.0),
            "204Hg": np.full(times.size, 20.0),
            "206Pb": pb206,
            "207Pb": np.full(times.size, 5.2),
            "208Pb": np.full(times.size, 80.0),
            "232Th": np.full(times.size, 40.0),
            "238U": np.full(times.size, 200.0),
        }
    )

    signal = calc_raw_ratios(experiment, total, total, region="signal")
    plateau = calc_raw_ratios(experiment, total, total, region="plateau")
    unfiltered_experiment = experiment.copy()
    unfiltered_experiment["plateau_filter_method"] = "none"
    unfiltered_plateau = calc_raw_ratios(
        unfiltered_experiment,
        total,
        total,
        region="plateau",
    )

    assert 10.0 in signal["Time"].tolist()
    assert 10.0 not in plateau["Time"].tolist()
    assert len(signal) == 21
    assert len(plateau) == 19
    assert len(unfiltered_plateau) == 20
    assert 10.0 in unfiltered_plateau["Time"].tolist()
    assert unfiltered_plateau.attrs["plateau_filter_excluded_count"] == 0
    assert plateau.attrs["plateau_filter_excluded_count"] == 1
    excluded = plateau.attrs["plateau_filter_excluded"].iloc[0]
    assert excluded["spot_name"] == "RM_1"
    assert excluded["Time"] == 10.0
    assert signal.loc[signal["Time"] == 10.0, "206Pb/238U"].iloc[0] == 5.0

    session_results = {}
    for ratio in ("206Pb/238U", "207Pb/206Pb"):
        plateau[ratio + "_fractionation_corrected"] = plateau[ratio]
        session_results[ratio] = {"fit": np.array([1.0]), "std": 0.001}
    spot_means = calc_ratio_mean_and_uncertainty(plateau, session_results)

    assert np.isclose(spot_means.loc[0, "206Pb/238U"], 0.5)
    assert np.isclose(spot_means.loc[0, "207Pb/206Pb"], 0.052)


def test_downhole_alignment_covers_union_without_endpoint_extrapolation():
    time, stacked, _, _ = align_downhole_traces(
        [np.array([0.0, 1.0]), np.array([0.0, 1.0, 2.0])],
        [np.array([1.0, 2.0]), np.array([1.0, 2.0, 3.0])],
    )

    assert time.tolist() == [0.0, 1.0, 2.0]
    np.testing.assert_allclose(stacked[0, :2], [1.0, 2.0])
    assert np.isnan(stacked[0, 2])
    np.testing.assert_allclose(stacked[1], [1.0, 2.0, 3.0])


def test_downhole_factor_uses_target_over_measured_convention():
    raw = pd.DataFrame(
        {
            "sample": ["RM", "RM"],
            "spot_name": ["RM_1", "RM_1"],
            "Time": [10.0, 11.0],
            "206Pb/238U": [0.25, 0.20],
        }
    )
    correction_results = {
        "206Pb/238U": {
            "interpolation": lambda relative_time: np.array([2.0, 2.5]),
        }
    }

    corrected = apply_DH_correction(raw, correction_results)

    np.testing.assert_allclose(
        corrected["206Pb/238U_DH_corrected"],
        [0.5, 0.5],
    )


def test_downhole_application_uses_saved_signal_relative_time_for_plateau_rows():
    raw = pd.DataFrame(
        {
            "sample": ["RM", "RM"],
            "spot_name": ["RM_1", "RM_1"],
            "Time": [15.0, 16.0],
            "signal_relative_time": [5.0, 6.0],
            "206Pb/238U": [0.10, 0.10],
        }
    )
    correction_results = {
        "206Pb/238U": {
            "interpolation": lambda relative_time: np.asarray(relative_time),
        }
    }

    corrected = apply_DH_correction(raw, correction_results)

    np.testing.assert_allclose(
        corrected["206Pb/238U_DH_correction_factor"], [5.0, 6.0]
    )


def test_downhole_metadata_evaluates_interpolated_covariance():
    time_grid = np.array([0.0, 1.0, 2.0])
    stacked_fractionations = np.array([
        [1.00, 1.10, 1.20],
        [1.04, 1.14, 1.24],
        [0.96, 1.06, 1.16],
    ])

    metadata = build_downhole_fractionation_metadata(time_grid, stacked_fractionations)
    evaluated = evaluate_downhole_fractionation(metadata, np.array([0.5, 2.5]))

    weights = np.array([
        [0.5, 0.5, 0.0],
        [0.0, -0.5, 1.5],
    ])
    expected_covariance = weights @ metadata["covariance"] @ weights.T

    np.testing.assert_allclose(evaluated["prediction"], [1.05, 1.25])
    np.testing.assert_allclose(evaluated["prediction_covariance"], expected_covariance)
    np.testing.assert_equal(evaluated["is_extrapolated"], [False, True])


def test_apply_downhole_correction_does_not_propagate_model_uncertainty():
    time_grid = np.array([0.0, 1.0, 2.0])
    stacked_fractionations = np.array([
        [1.00, 1.10, 1.20],
        [1.04, 1.14, 1.24],
        [0.96, 1.06, 1.16],
    ])
    metadata = build_downhole_fractionation_metadata(time_grid, stacked_fractionations)
    raw = pd.DataFrame({
        "sample": ["unknown", "unknown"],
        "spot_name": ["u1", "u1"],
        "Time": [10.0, 11.0],
        "206Pb/238U": [0.10, 0.10],
    })
    correction_results = {
        "206Pb/238U": {
            "interpolation": lambda relative_time: np.interp(relative_time, time_grid, metadata["mean_fractionation"]),
            "metadata": metadata,
        }
    }

    corrected = apply_DH_correction(raw, correction_results)

    np.testing.assert_allclose(corrected["206Pb/238U_DH_correction_factor"], [1.0, 1.1])
    assert "206Pb/238U_DH_correction_1s" not in corrected
    assert "downhole_corrected_covariance" not in corrected.attrs


def test_multiplicative_correction_covariance_matches_jacobian_terms():
    measured = np.array([0.10, 0.12])
    correction = np.array([1.1, 1.2])
    measured_covariance = np.array([[1e-6, 2e-7], [2e-7, 4e-6]])
    correction_covariance = np.array([[1e-4, 3e-5], [3e-5, 2e-4]])

    propagated = propagate_multiplicative_correction_covariance(
        measured,
        correction,
        measured_covariance=measured_covariance,
        correction_covariance=correction_covariance,
    )

    expected_measurement = np.diag(correction) @ measured_covariance @ np.diag(correction)
    expected_correction = np.outer(measured, measured) * correction_covariance

    np.testing.assert_allclose(propagated["corrected_values"], measured * correction)
    np.testing.assert_allclose(propagated["covariance_measurement"], expected_measurement)
    np.testing.assert_allclose(propagated["covariance_correction"], expected_correction)
    np.testing.assert_allclose(propagated["covariance"], expected_measurement + expected_correction)


def test_downhole_corrected_covariance_includes_shared_curve_terms():
    time_grid = np.array([0.0, 1.0, 2.0])
    stacked_fractionations = np.array([
        [1.00, 1.10, 1.20],
        [1.04, 1.14, 1.24],
        [0.96, 1.06, 1.16],
    ])
    metadata = build_downhole_fractionation_metadata(time_grid, stacked_fractionations)
    raw = np.array([0.10, 0.10])
    relative_time = np.array([0.0, 1.0])

    propagated = propagate_downhole_corrected_ratio_covariance(
        raw,
        relative_time,
        {
            "interpolation": lambda time: np.interp(time, time_grid, metadata["mean_fractionation"]),
            "metadata": metadata,
        },
        raw_ratio_1sigma=np.array([0.001, 0.001]),
    )

    correction_covariance = evaluate_downhole_fractionation(
        metadata,
        relative_time,
    )["prediction_covariance"]
    expected_downhole = np.outer(raw, raw) * correction_covariance

    np.testing.assert_allclose(propagated["covariance_downhole"], expected_downhole)
    assert propagated["covariance"][0, 1] != 0


def test_apply_downhole_correction_does_not_attach_covariance_metadata():
    time_grid = np.array([0.0, 1.0, 2.0])
    stacked_fractionations = np.array([
        [1.00, 1.10, 1.20],
        [1.04, 1.14, 1.24],
        [0.96, 1.06, 1.16],
    ])
    metadata = build_downhole_fractionation_metadata(time_grid, stacked_fractionations)
    raw = pd.DataFrame({
        "sample": ["unknown", "unknown", "unknown", "unknown"],
        "spot_name": ["u1", "u1", "u2", "u2"],
        "Time": [10.0, 11.0, 20.0, 21.0],
        "206Pb/238U": [0.10, 0.10, 0.12, 0.12],
        "206Pb/238U_2e": [0.002, 0.002, 0.003, 0.003],
    })
    correction_results = {
        "206Pb/238U": {
            "interpolation": lambda relative_time: np.interp(relative_time, time_grid, metadata["mean_fractionation"]),
            "metadata": metadata,
        }
    }

    corrected = apply_DH_correction(raw, correction_results)

    assert corrected.attrs == {}
    np.testing.assert_allclose(corrected["206Pb/238U_DH_corrected"], [0.10, 0.11, 0.12, 0.132])


def test_reciprocal_preserves_relative_uncertainty_to_first_order():
    ratio = np.array([0.1, 0.5, 2.0])
    uncertainty = np.array([0.002, 0.015, 0.08])

    reciprocal = 1 / ratio
    reciprocal_uncertainty = propagate_reciprocal_uncertainty(ratio, uncertainty)

    np.testing.assert_allclose(
        reciprocal_uncertainty / reciprocal,
        uncertainty / ratio,
    )


def test_product_covariance_retains_cross_terms():
    x = np.array([2.0, 3.0])
    y = np.array([5.0, 7.0])
    cov_x = np.array([[0.04, 0.01], [0.01, 0.09]])
    cov_y = np.array([[0.25, 0.03], [0.03, 0.49]])
    cov_xy = np.array([[0.02, 0.01], [0.00, 0.03]])

    propagated = propagate_product_covariance(x, y, cov_x, cov_y, cov_xy, scale=10.0)

    jac_x = np.diag(10.0 * y)
    jac_y = np.diag(10.0 * x)
    expected = jac_x @ cov_x @ jac_x.T + jac_y @ cov_y @ jac_y.T
    expected += jac_x @ cov_xy @ jac_y.T + jac_y @ cov_xy.T @ jac_x.T

    np.testing.assert_allclose(propagated["values"], 10.0 * x * y)
    np.testing.assert_allclose(propagated["covariance"], expected)


def test_reciprocal_covariance_matches_jacobian():
    ratio = np.array([0.1, 0.2])
    covariance = np.array([[1e-6, 2e-7], [2e-7, 4e-6]])

    propagated = propagate_reciprocal_covariance(ratio, covariance)

    jacobian = np.diag(-1 / ratio**2)
    np.testing.assert_allclose(propagated["values"], 1 / ratio)
    np.testing.assert_allclose(propagated["covariance"], jacobian @ covariance @ jacobian.T)


def test_206pb_238u_date_covariance_matches_jacobian():
    ratio = np.array([0.1, 0.2])
    covariance = np.array([[1e-6, 2e-7], [2e-7, 4e-6]])

    propagated = propagate_date_covariance(ratio, covariance, "206Pb/238U")

    derivatives = np.array([
        calc_68_date_uncertainty(ratio[0], 1.0),
        calc_68_date_uncertainty(ratio[1], 1.0),
    ])
    jacobian = np.diag(derivatives)
    np.testing.assert_allclose(propagated["covariance"], jacobian @ covariance @ jacobian.T)


def test_discordance_covariance_matches_jacobian():
    age_68 = np.array([100.0, 150.0])
    age_76 = np.array([110.0, 180.0])
    cov_68 = np.array([[1.0, 0.2], [0.2, 2.0]])
    cov_76 = np.array([[1.5, 0.3], [0.3, 2.5]])
    cov_68_76 = np.array([[0.4, 0.1], [0.2, 0.5]])

    propagated = propagate_discordance_covariance(
        age_68,
        age_76,
        cov_68,
        cov_76,
        cov_68_76,
    )

    jac_68 = np.diag(-100 / age_76)
    jac_76 = np.diag(100 * age_68 / age_76**2)
    expected = jac_68 @ cov_68 @ jac_68.T + jac_76 @ cov_76 @ jac_76.T
    expected += jac_68 @ cov_68_76 @ jac_76.T + jac_76 @ cov_68_76.T @ jac_68.T

    np.testing.assert_allclose(propagated["values"], 100 * (1 - age_68 / age_76))
    np.testing.assert_allclose(propagated["covariance"], expected)


def test_206pb_238u_age_uncertainty_matches_finite_difference():
    ratio = 0.18
    uncertainty = 0.001
    analytic = calc_68_date_uncertainty(ratio, uncertainty)
    finite_difference = (
        calc_68_date(ratio + uncertainty) - calc_68_date(ratio - uncertainty)
    ) / 2

    np.testing.assert_allclose(analytic, finite_difference, rtol=1e-5)


def test_207pb_235u_age_uncertainty_matches_finite_difference():
    ratio = 1.6
    uncertainty = 0.01
    analytic = calc_75_date_uncertainty(ratio, uncertainty)
    finite_difference = (
        calc_75_date(ratio + uncertainty) - calc_75_date(ratio - uncertainty)
    ) / 2

    np.testing.assert_allclose(analytic, finite_difference, rtol=1e-5)


def test_208pb_232th_age_uncertainty_matches_finite_difference():
    ratio = 0.12
    uncertainty = 0.002
    analytic = calc_82_date_uncertainty(ratio, uncertainty)
    finite_difference = (
        calc_82_date(ratio + uncertainty)
        - calc_82_date(ratio - uncertainty)
    ) / 2

    np.testing.assert_allclose(analytic, finite_difference, rtol=1e-5)


def test_208pb_232th_age_reproduces_zircon_spreadsheet_formula():
    ratio = 0.4746695621005803
    ratio_1s = 0.010767270632896358
    spreadsheet_lambda_232 = 4.94752e-11

    age = calc_82_date(ratio, lambda232=spreadsheet_lambda_232)
    age_2s = calc_82_date_uncertainty(
        ratio,
        2 * ratio_1s,
        lambda232=spreadsheet_lambda_232,
    )

    np.testing.assert_allclose(age, 7851.083755088551)
    np.testing.assert_allclose(age_2s, 295.1571783865583)


def test_207pb_206pb_age_uncertainty_matches_finite_difference():
    age_ma = 1000.0
    ratio = get_wetherill_point(age_ma)[0] / get_wetherill_point(age_ma)[1] / 137.818
    uncertainty = 0.0001
    analytic = calc_76_date_uncertainty(ratio, uncertainty)
    finite_difference = (
        calc_76_date(ratio + uncertainty) - calc_76_date(ratio - uncertainty)
    ) / 2

    np.testing.assert_allclose(analytic, finite_difference, rtol=2e-4)


def test_tw_horizontal_projection_recovers_endpoints_and_mixtures():
    radiogenic_68 = 0.077
    radiogenic_86 = 1 / radiogenic_68
    radiogenic_76 = 0.056
    common_76 = 0.86

    mixture_fractions = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
    expected_x = mixture_fractions * radiogenic_86
    y_on_line = common_76 + (radiogenic_76 - common_76) * mixture_fractions

    projected_x = project_tw_238u_206pb(
        y_on_line,
        radiogenic_68,
        radiogenic_76,
        common_76,
    )

    np.testing.assert_allclose(projected_x, expected_x)
    assert projected_x[0] == 0.0
    np.testing.assert_allclose(projected_x[-1], radiogenic_86)

    projected_68 = project_tw_206pb_238u(
        y_on_line[1:],
        radiogenic_68,
        radiogenic_76,
        common_76,
    )
    np.testing.assert_allclose(projected_68, 1 / expected_x[1:])
    np.testing.assert_allclose(projected_68[-1], radiogenic_68)


def test_tw_horizontal_projection_preserves_corrected_207_206_on_line():
    radiogenic_68 = 0.081
    radiogenic_86 = 1 / radiogenic_68
    radiogenic_76 = 0.058
    common_76 = 0.84
    corrected_76 = np.array([0.12, 0.2, 0.45])

    projected_x = project_tw_238u_206pb(
        corrected_76,
        radiogenic_68,
        radiogenic_76,
        common_76,
    )
    slope = (common_76 - radiogenic_76) / (0 - radiogenic_86)
    y_reconstructed = common_76 + slope * projected_x

    np.testing.assert_allclose(y_reconstructed, corrected_76)


def test_tw_projection_rejects_degenerate_endpoints():
    with np.testing.assert_raises(ValueError):
        project_tw_238u_206pb(
            0.1,
            radiogenic_206pb_238u=0.0,
            radiogenic_207pb_206pb=0.056,
            common_207pb_206pb=0.86,
        )

    with np.testing.assert_raises(ValueError):
        project_tw_238u_206pb(
            0.1,
            radiogenic_206pb_238u=0.077,
            radiogenic_207pb_206pb=0.86,
            common_207pb_206pb=0.86,
        )
