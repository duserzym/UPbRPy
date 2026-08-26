import numpy as np
import pandas as pd

from UPbRPy import (
    apply_session_correction,
    calc_DH_dynamic_fractionation,
    calc_DH_nominal_fractionation,
    calc_session_nominal_fractionation,
    fit_calibration_model,
)


def test_builtin_calibration_models_are_callable_without_covariance():
    x = np.linspace(0.0, 5.0, 12)
    y = 1.1 - 0.12 * np.exp(-0.7 * x)

    for method, kwargs in [
        ("interpolate", {}),
        ("linear", {}),
        ("polynomial", {"degree": 2}),
        ("orthogonal_polynomial", {"degree": 2}),
        ("exponential", {}),
        ("spline", {"smoothing": 0}),
    ]:
        fitted = fit_calibration_model(x, y, method=method, **kwargs)
        assert fitted(x).shape == x.shape
        assert np.all(np.isfinite(fitted(x)))
        assert not hasattr(fitted, "covariance")


def test_custom_parametric_calibration_function_is_fitted():
    def custom(time, intercept, slope):
        return intercept + slope * time

    x = np.linspace(0.0, 4.0, 9)
    y = custom(x, 1.2, -0.03)
    fitted = fit_calibration_model(x, y, method=custom, p0=(1.0, 0.0))

    np.testing.assert_allclose(fitted.parameters, [1.2, -0.03], atol=1e-10)
    np.testing.assert_allclose(fitted(x), y, atol=1e-10)


def test_downhole_default_is_pointwise_mean_and_model_is_replaceable():
    rows = []
    for spot_name, offset in [("RM_1", 0.0), ("RM_2", 0.02)]:
        for time, ratio in zip([0.0, 1.0, 2.0], [0.50, 0.45, 0.40]):
            rows.append(
                {
                    "sample": "RM",
                    "spot_name": spot_name,
                    "Time": time,
                    "signal_relative_time": time,
                    "206Pb/238U": ratio + offset,
                }
            )
    data = pd.DataFrame(rows)

    _, mean_model, mean_metadata = calc_DH_nominal_fractionation(
        1.0, data, "206Pb/238U", return_metadata=True
    )
    _, linear_model, linear_metadata = calc_DH_nominal_fractionation(
        1.0,
        data,
        "206Pb/238U",
        return_metadata=True,
        correction_model="linear",
    )

    np.testing.assert_allclose(mean_model(mean_metadata["time"]), mean_metadata["mean_fractionation"])
    assert mean_metadata["model"] == "mean"
    assert linear_metadata["model"] == "linear"
    assert np.all(np.isfinite(linear_model(linear_metadata["time"])))


def test_downhole_logratio_mode_uses_geometric_standard_center():
    rows = []
    for spot_name, ratio in (("RM_1", 0.5), ("RM_2", 2.0)):
        for time in (0.0, 1.0, 2.0):
            rows.append(
                {
                    "sample": "RM",
                    "spot_name": spot_name,
                    "Time": time,
                    "signal_relative_time": time,
                    "206Pb/238U": ratio,
                }
            )
    data = pd.DataFrame(rows)

    _, fitted, metadata = calc_DH_nominal_fractionation(
        1.0,
        data,
        "206Pb/238U",
        return_metadata=True,
        ratio_aggregation="logratio",
    )

    np.testing.assert_allclose(metadata["mean_fractionation"], 1.0)
    np.testing.assert_allclose(fitted(metadata["time"]), 1.0)
    assert metadata["ratio_aggregation"] == "logratio"
    assert metadata["model_settings"]["fit_space"] == "logratio"


def test_dynamic_downhole_filter_rejects_one_extreme_primary_sweep():
    rows = []
    for spot_index in range(6):
        for relative_time in range(12):
            factor = 1.0 + 0.01 * relative_time + 0.002 * spot_index
            if spot_index == 2 and relative_time == 8:
                factor = 80.0
            rows.append(
                {
                    "sample": "Pb-rich RM",
                    "spot_name": f"RM_{spot_index}",
                    "Time": float(relative_time),
                    "signal_relative_time": float(relative_time),
                    "measured": 1.0,
                    "projected": factor,
                }
            )
    data = pd.DataFrame(rows)

    filtered_plot, filtered_model, filtered = calc_DH_dynamic_fractionation(
        data,
        "measured",
        "projected",
        return_metadata=True,
        outlier_sigma=5,
    )
    _, unfiltered_model, unfiltered = calc_DH_dynamic_fractionation(
        data,
        "measured",
        "projected",
        return_metadata=True,
        outlier_sigma=None,
    )

    spike_time = filtered["time"][8]
    assert filtered["excluded_count"] == 1
    excluded = filtered["excluded_aligned_points"].iloc[0]
    assert excluded["spot_name"] == "RM_2"
    assert excluded["signal_relative_time"] == spike_time
    assert np.isnan(filtered["residuals"][2, 8])
    assert filtered_model(spike_time) < 1.2
    assert unfiltered_model(spike_time) > 10
    assert unfiltered["excluded_count"] == 0
    assert filtered_plot.y_range.end < 2.0
    outlier_sources = [
        renderer.data_source.data
        for renderer in filtered_plot.renderers
        if hasattr(renderer, "data_source")
        and "fractionation_factor" in renderer.data_source.data
    ]
    assert any(
        80.0 in np.asarray(source["fractionation_factor"], dtype=float)
        for source in outlier_sources
    )
    assert any(
        "display_factor" in source
        and np.max(np.asarray(source["display_factor"], dtype=float))
        < filtered_plot.y_range.end
        for source in outlier_sources
    )


def test_session_custom_model_is_used_when_applying_correction():
    def custom(time, intercept, slope):
        return intercept + slope * time

    rows = []
    for spot, time, factor in [("RM_1", 0.0, 1.0), ("RM_2", 10.0, 1.1), ("RM_3", 20.0, 1.2)]:
        for delta in (0.0, 0.2):
            rows.append(
                {
                    "sample": "RM",
                    "spot_name": spot,
                    "Time": time + delta,
                    "206Pb/238U_DH_corrected": 1.0 / factor,
                }
            )
    standards = pd.DataFrame(rows)
    _, _, scatter, metadata = calc_session_nominal_fractionation(
        1.0,
        standards,
        "206Pb/238U",
        degree=1,
        model=custom,
        model_kwargs={"p0": (1.0, 0.01)},
    )
    results = {
        "206Pb/238U": {
            "fit": metadata["parameters"],
            "predictor": metadata["predictor"],
            "model": metadata["model"],
            "std": scatter,
            "fit_observations": {"time": metadata["spot_mean_times"]},
        }
    }
    unknown = pd.DataFrame(
        {
            "spot_name": ["unknown"],
            "Time": [15.0],
            "206Pb/238U_DH_corrected": [0.1],
        }
    )

    corrected = apply_session_correction(unknown, results)

    expected = 0.1 * metadata["predictor"](np.array([15.0]))[0]
    np.testing.assert_allclose(corrected["206Pb/238U_fractionation_corrected"], [expected])
    assert corrected.attrs == {}
