import numpy as np
import pandas as pd

from GUI.services import _session_fit_weights
from UPbRPy.UPbRPy import (
    CovarianceStore,
    inverse_standard_deviation_weights,
    apply_DH_correction,
    apply_session_correction,
    build_downhole_fractionation_metadata,
    calc_82_date,
    calc_session_nominal_fractionation,
    calc_depth_profile,
    calc_ratio_mean_and_uncertainty,
    evaluate_session_fractionation_fit,
    fit_weighted_polynomial_with_covariance,
    polynomial_prediction_covariance,
    propagate_session_corrected_ratio_covariance,
)


def test_session_standard_observations_use_ratio_of_known_to_plateau_mean():
    rows = []
    for spot_index in range(4):
        for sweep, ratio in enumerate((0.5, 1.0)):
            rows.append(
                {
                    "sample": "RM",
                    "spot_name": f"RM_{spot_index}",
                    "Time": float(spot_index * 10 + sweep),
                    "206Pb/238U_DH_corrected": ratio,
                }
            )
    data = pd.DataFrame(rows)

    _, _, _, metadata = calc_session_nominal_fractionation(
        1.0, data, "206Pb/238U", degree=1
    )

    np.testing.assert_allclose(
        metadata["spot_mean_fractionation"],
        np.full(4, 1.0 / 0.75),
    )


def test_session_logratio_observations_use_geometric_spot_ratio():
    rows = []
    for spot_index in range(4):
        for sweep, ratio in enumerate((0.5, 2.0)):
            rows.append(
                {
                    "sample": "RM",
                    "spot_name": f"RM_{spot_index}",
                    "Time": float(spot_index * 10 + sweep),
                    "206Pb/238U_DH_corrected": ratio,
                }
            )
    data = pd.DataFrame(rows)

    _, _, _, metadata = calc_session_nominal_fractionation(
        1.0,
        data,
        "206Pb/238U",
        degree=1,
        ratio_aggregation="logratio",
    )

    np.testing.assert_allclose(metadata["spot_mean_fractionation"], 1.0)
    assert metadata["ratio_aggregation"] == "logratio"
    assert metadata["model_settings"]["fit_space"] == "logratio"


def test_covariance_store_survives_pandas_concat_without_array_comparison():
    frame = pd.DataFrame({
        "spot_name": ["one", "one", "two", "two"],
        "Time": [0.0, 1.0, 2.0, 3.0],
    })
    store = CovarianceStore({"206Pb/238U": {"covariance": np.eye(4)}})
    frame.attrs["downhole_corrected_covariance"] = store

    combined = pd.concat([frame.iloc[:2], frame.iloc[2:]], ignore_index=True)

    assert combined.attrs["downhole_corrected_covariance"] is store


def test_large_downhole_correction_does_not_create_covariance_metadata():
    row_count = 513
    data = pd.DataFrame({
        "spot_name": ["unknown"] * row_count,
        "Time": np.arange(row_count, dtype=float),
        "206Pb/238U": np.full(row_count, 0.1),
    })
    downhole_results = {
        "206Pb/238U": {
            "interpolation": lambda relative_time: np.ones_like(relative_time, dtype=float),
        }
    }

    corrected = apply_DH_correction(data, downhole_results)

    assert corrected.attrs == {}


def test_core_session_weights_favor_more_precise_observations():
    weights = inverse_standard_deviation_weights([0.01, 0.02, 0.04])

    np.testing.assert_allclose(weights, [100.0, 50.0, 25.0])


def test_gui_session_weights_match_numpy_polyfit_convention():
    weights = _session_fit_weights(
        fractions=np.array([1.0, 1.0, 1.0]),
        stds=np.array([0.01, 0.02, 0.04]),
    )

    np.testing.assert_allclose(weights, [100.0, 50.0, 25.0])


def test_invalid_standard_deviations_use_finite_fallback():
    weights = inverse_standard_deviation_weights([0.02, 0.0, np.nan])

    np.testing.assert_allclose(weights, [50.0, 50.0, 50.0])


def test_weighted_polynomial_coefficients_match_numpy_polyfit():
    x = np.array([0.0, 1.0, 2.0, 4.0, 7.0])
    y = np.array([1.02, 1.49, 2.02, 3.05, 4.54])
    sigma = np.array([0.03, 0.02, 0.04, 0.03, 0.05])

    fit = fit_weighted_polynomial_with_covariance(x, y, sigma, degree=1)

    expected = np.polyfit(x, y, 1, w=1 / sigma)
    np.testing.assert_allclose(fit["coefficients"], expected, rtol=1e-12, atol=1e-12)
    assert fit["covariance"].shape == (2, 2)
    assert fit["covariance_unscaled"].shape == (2, 2)
    assert fit["degrees_of_freedom"] == 3
    assert fit["reduced_chi_square"] > 0


def test_prediction_uncertainty_is_smallest_near_weighted_centroid():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = 2.0 + 0.5 * x
    sigma = np.full_like(x, 0.1)

    fit = fit_weighted_polynomial_with_covariance(x, y, sigma, degree=1)
    prediction_covariance = polynomial_prediction_covariance(
        fit["covariance_unscaled"],
        np.array([1.5, 5.0]),
    )
    prediction_sigma = np.sqrt(np.diag(prediction_covariance))

    assert prediction_sigma[0] < prediction_sigma[1]


def test_prediction_covariance_scales_with_stated_standard_deviation():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = 2.0 + 0.5 * x
    sigma = np.full_like(x, 0.1)

    fit = fit_weighted_polynomial_with_covariance(x, y, sigma, degree=1)
    fit_double_sigma = fit_weighted_polynomial_with_covariance(x, y, sigma * 2, degree=1)

    np.testing.assert_allclose(
        fit_double_sigma["covariance_unscaled"],
        fit["covariance_unscaled"] * 4,
        rtol=1e-12,
        atol=1e-12,
    )


def test_session_fit_evaluation_returns_arbitrary_time_covariance_and_flags():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = 1.0 + 0.02 * x
    sigma = np.full_like(x, 0.01)
    fit = fit_weighted_polynomial_with_covariance(x, y, sigma, degree=1)
    session_result = {
        "fit": fit["coefficients"],
        "std": fit["residual_standard_deviation"],
        "fit_covariance": fit["covariance"],
        "fit_observations": {"time": x},
    }

    evaluated = evaluate_session_fractionation_fit(session_result, np.array([1.5, 4.0]))

    np.testing.assert_allclose(evaluated["prediction"], np.polyval(fit["coefficients"], [1.5, 4.0]))
    assert evaluated["prediction_covariance"].shape == (2, 2)
    assert evaluated["prediction_1sigma"][0] < evaluated["prediction_1sigma"][1]
    np.testing.assert_equal(evaluated["is_extrapolated"], [False, True])


def test_apply_session_correction_records_residual_scatter_not_coefficient_uncertainty():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = 1.0 + 0.02 * x
    sigma = np.full_like(x, 0.01)
    fit = fit_weighted_polynomial_with_covariance(x, y, sigma, degree=1)
    session_results = {
        "206Pb/238U": {
            "fit": fit["coefficients"],
            "std": fit["residual_standard_deviation"],
            "fit_covariance": fit["covariance"],
            "fit_observations": {"time": x},
        }
    }
    data = pd.DataFrame({
        "spot_name": ["unknown-1", "unknown-1"],
        "Time": [1.5, 4.0],
        "206Pb/238U_DH_corrected": [0.10, 0.12],
    })

    corrected = apply_session_correction(data, session_results)

    expected_factor = np.polyval(fit["coefficients"], corrected["Time"])
    np.testing.assert_allclose(corrected["206Pb/238U_session_correction_factor"], expected_factor)
    np.testing.assert_allclose(
        corrected["206Pb/238U_fractionation_corrected"],
        corrected["206Pb/238U_DH_corrected"] * expected_factor,
    )
    # The manuscript specifies scalar session residual scatter; a perfect
    # fit has zero scatter even with nonzero coefficient covariance.
    np.testing.assert_allclose(
        corrected["206Pb/238U_session_correction_1s"],
        fit["residual_standard_deviation"],
    )
    session_results["206Pb/238U"]["std"] = 0.025
    noisy = apply_session_correction(data, session_results)
    np.testing.assert_allclose(noisy["206Pb/238U_session_correction_1s"], 0.025)
    assert corrected["206Pb/238U_session_correction_extrapolated"].tolist() == [False, True]


def test_apply_session_correction_does_not_preserve_covariance_metadata():
    data = pd.DataFrame({
        "spot_name": ["unknown-1", "unknown-1"],
        "Time": [1.5, 4.0],
        "206Pb/238U_DH_corrected": [0.10, 0.12],
    })
    data.attrs["downhole_corrected_covariance"] = {
        "206Pb/238U": {"covariance": np.eye(2) * 1e-6}
    }
    fit = fit_weighted_polynomial_with_covariance(
        np.array([0.0, 1.0, 2.0, 3.0]),
        np.array([1.0, 1.02, 1.04, 1.06]),
        np.full(4, 0.01),
        degree=1,
    )
    session_results = {
        "206Pb/238U": {
            "fit": fit["coefficients"],
            "std": fit["residual_standard_deviation"],
            "fit_covariance": fit["covariance"],
            "fit_observations": {"time": np.array([0.0, 1.0, 2.0, 3.0])},
        }
    }

    corrected = apply_session_correction(data, session_results)

    assert corrected.attrs == {}


def test_depth_profile_uses_poisson_and_scalar_session_scatter_only():
    time_grid = np.array([0.0, 1.0])
    stacked_fractionations = np.array([
        [1.00, 1.10],
        [1.04, 1.14],
        [0.96, 1.06],
    ])
    metadata = build_downhole_fractionation_metadata(time_grid, stacked_fractionations)
    raw = pd.DataFrame({
        "sample": ["unknown", "unknown"],
        "spot_name": ["u1", "u1"],
        "Time": [10.0, 11.0],
        "206Pb/238U": [0.10, 0.10],
        "206Pb/238U_2e": [0.002, 0.002],
        "207Pb/206Pb": [0.052, 0.052],
        "207Pb/206Pb_2e": [0.0002, 0.0002],
    })
    downhole_results = {
        ratio: {
            "interpolation": lambda relative_time, metadata=metadata: np.interp(
                relative_time,
                time_grid,
                metadata["mean_fractionation"],
            ),
            "metadata": metadata,
        }
        for ratio in ["206Pb/238U", "207Pb/206Pb"]
    }
    downhole_corrected = apply_DH_correction(raw, downhole_results)
    session_results = {
        ratio: {
            "fit": np.array([1.0]),
            "std": 0.02,
            "fit_observations": {"time": np.array([9.0, 10.0, 11.0, 12.0])},
        }
        for ratio in ["206Pb/238U", "207Pb/206Pb"]
    }
    session_corrected = apply_session_correction(downhole_corrected, session_results)

    depth_profile = calc_depth_profile(session_corrected, session_results)
    expected_1sigma = depth_profile["206Pb/238U_fractionation_corrected"] * np.sqrt(
        (raw["206Pb/238U_2e"].to_numpy() / 2 / raw["206Pb/238U"].to_numpy()) ** 2
        + 0.02**2
    )
    np.testing.assert_allclose(
        depth_profile["206Pb/238U_fractionation_corrected_2e"].to_numpy() / 2,
        expected_1sigma,
    )
    assert depth_profile.attrs == {}


def test_thpb_dates_are_added_to_spot_and_sweep_outputs():
    ratios = ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th")
    data = pd.DataFrame(
        {
            "sample": ["unknown", "unknown"],
            "spot_name": ["u1", "u1"],
            "Time": [10.0, 11.0],
            "206Pb/238U": [0.10, 0.102],
            "206Pb/238U_2e": [0.002, 0.002],
            "207Pb/206Pb": [0.052, 0.0522],
            "207Pb/206Pb_2e": [0.0002, 0.0002],
            "208Pb/232Th": [0.025, 0.027],
            "208Pb/232Th_2e": [0.001, 0.001],
        }
    )
    session_results = {
        ratio: {
            "fit": np.array([1.0]),
            "std": 0.001,
            "fit_observations": {"time": np.array([9.0, 12.0])},
        }
        for ratio in ratios
    }
    for ratio in ratios:
        data[f"{ratio}_fractionation_corrected"] = data[ratio]

    depth_profile = calc_depth_profile(data, session_results)
    spot_summary = calc_ratio_mean_and_uncertainty(data, session_results)

    np.testing.assert_allclose(
        depth_profile["208Pb/232Th_age"],
        calc_82_date(depth_profile["208Pb/232Th_fractionation_corrected"]),
    )
    np.testing.assert_allclose(
        spot_summary.loc[0, "208Pb/232Th_age"],
        calc_82_date(spot_summary.loc[0, "208Pb/232Th"]),
    )
    assert "208Pb/232Th_age_2e" in depth_profile
    assert "208Pb/232Th_age_2e" in spot_summary


def test_session_corrected_covariance_includes_shared_fit_terms():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = 1.0 + 0.02 * x
    sigma = np.full_like(x, 0.01)
    fit = fit_weighted_polynomial_with_covariance(x, y, sigma, degree=1)
    session_result = {
        "fit": fit["coefficients"],
        "std": fit["residual_standard_deviation"],
        "fit_covariance": fit["covariance"],
        "fit_observations": {"time": x},
    }
    measured = np.array([0.10, 0.12])
    measured_1sigma = np.array([0.001, 0.002])
    time = np.array([1.0, 2.0])

    propagated = propagate_session_corrected_ratio_covariance(
        measured,
        time,
        session_result,
        measured_ratio_1sigma=measured_1sigma,
    )

    correction_covariance = evaluate_session_fractionation_fit(
        session_result,
        time,
    )["prediction_covariance"]
    expected_session = np.outer(measured, measured) * correction_covariance
    expected_measurement = np.diag((np.polyval(fit["coefficients"], time) * measured_1sigma) ** 2)

    np.testing.assert_allclose(propagated["covariance_session"], expected_session)
    np.testing.assert_allclose(propagated["covariance_measurement"], expected_measurement)
    np.testing.assert_allclose(propagated["covariance"], expected_session + expected_measurement)
    assert propagated["covariance"][0, 1] != 0


def test_spot_summary_uses_scalar_session_scatter_without_covariance_metadata():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = 1.0 + 0.02 * x
    sigma = np.full_like(x, 0.01)
    fit = fit_weighted_polynomial_with_covariance(x, y, sigma, degree=1)
    session_result = {
        "fit": fit["coefficients"],
        "std": 0.01,
        "fit_observations": {"time": x},
    }
    session_results = {
        "206Pb/238U": session_result,
        "207Pb/206Pb": session_result,
    }
    data = pd.DataFrame({
        "spot_name": ["a", "a", "b", "b"],
        "sample": ["unknown", "unknown", "unknown", "unknown"],
        "Time": [1.0, 1.2, 2.0, 2.2],
        "206Pb/238U": [0.100, 0.102, 0.120, 0.122],
        "207Pb/206Pb": [0.0520, 0.0522, 0.0540, 0.0542],
    })
    for ratio in ["206Pb/238U", "207Pb/206Pb"]:
        data[ratio + "_fractionation_corrected"] = (
            data[ratio] * np.polyval(fit["coefficients"], data["Time"])
        )

    summary = calc_ratio_mean_and_uncertainty(data, session_results)

    assert summary.attrs == {}
    np.testing.assert_allclose(summary["206Pb/238U_session_correction_1s"], 0.01)
