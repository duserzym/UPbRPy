"""Reverse-fractionation flags use raw trends while scatter stays corrected."""

import numpy as np
import pandas as pd
import pytest

from UPbRPy.results import (
    apply_automatic_flags,
    calculate_spot_quality_metrics,
    finalize_reduction_results,
)


@pytest.fixture
def sweeps():
    time = np.tile(np.arange(5, dtype=float), 2)
    return pd.DataFrame(
        {
            "spot_name": np.repeat(["A_1", "A_2"], 5),
            "signal_relative_time": time,
            "206Pb/238U": 0.1 + 0.01 * time,
            "206Pb/238U_fractionation_corrected": 0.1 - 1e-6 * time,
        }
    )


def finalize(sweeps):
    """Supply already-calculated age branches to exercise finalization QC."""
    results = pd.DataFrame({"spot_name": ["A_1", "A_2"]})
    results["sample"] = "A"
    results["207Pb_corrected_age"] = 800.0
    results["208Pb_corrected_age"] = 800.0
    for ratio in ("206Pb/238U", "207Pb/206Pb"):
        results[f"{ratio}_age"] = 800.0
        results[f"{ratio}_age_2e_with_calibration"] = 8.0
    return finalize_reduction_results(results, sweep_data=sweeps)


@pytest.mark.parametrize("raw_slope", [0.01, -0.01], ids=["rising", "falling"])
def test_finalization_flags_raw_trend_and_keeps_corrected_scatter(sweeps, raw_slope):
    sweeps["206Pb/238U"] = 0.1 + raw_slope * sweeps["signal_relative_time"]

    finalized = finalize(sweeps)

    np.testing.assert_allclose(
        finalized.quality_metrics["qc_206Pb/238U_slope"], raw_slope,
    )
    assert finalized.parallel_results["flag_reverse_fractionation"].tolist() == (
        [raw_slope < 0] * 2
    )
    for results in (finalized.parallel_results, finalized.selected_results):
        assert results["automatic_flags"].str.contains("reverse fractionation").tolist() == (
            [raw_slope < 0] * 2
        )
    corrected = sweeps.groupby("spot_name")["206Pb/238U_fractionation_corrected"]
    expected_rsd = corrected.std() / corrected.mean().abs() * 100
    np.testing.assert_allclose(
        finalized.quality_metrics.set_index("spot_name")["qc_206Pb/238U_rsd_percent"],
        expected_rsd,
    )


@pytest.mark.parametrize(
    "kwargs,expected_slope",
    [({}, -1e-6), ({"slope_ratio_column": None}, -1e-6),
     ({"slope_ratio_column": "absent"}, -1e-6),
     ({"ratio_column": "206Pb/238U"}, 0.01)],
)
def test_default_and_missing_slope_column_use_ratio_column(sweeps, kwargs, expected_slope):
    metrics = calculate_spot_quality_metrics(sweeps, **kwargs)
    flagged = apply_automatic_flags(
        pd.DataFrame({"spot_name": ["A_1", "A_2"]}), quality_metrics=metrics,
    )

    np.testing.assert_allclose(metrics["qc_206Pb/238U_slope"], expected_slope)
    assert flagged["flag_reverse_fractionation"].tolist() == [expected_slope < 0] * 2


def test_slope_and_scatter_use_independent_finite_values(sweeps):
    sweeps.loc[0, "206Pb/238U"] = np.nan
    sweeps.loc[1, "206Pb/238U_fractionation_corrected"] = np.nan

    metrics = calculate_spot_quality_metrics(sweeps, slope_ratio_column="206Pb/238U")
    default_metrics = calculate_spot_quality_metrics(sweeps)

    np.testing.assert_allclose(metrics["qc_206Pb/238U_slope"], 0.01)
    pd.testing.assert_series_equal(
        metrics["qc_206Pb/238U_rsd_percent"],
        default_metrics["qc_206Pb/238U_rsd_percent"],
    )
    assert metrics["qc_sweep_count"].tolist() == [4, 5]


def test_finalization_without_raw_ratio_keeps_corrected_slope(sweeps):
    finalized = finalize(sweeps.drop(columns="206Pb/238U"))

    np.testing.assert_allclose(finalized.quality_metrics["qc_206Pb/238U_slope"], -1e-6)
    assert finalized.parallel_results["flag_reverse_fractionation"].all()
