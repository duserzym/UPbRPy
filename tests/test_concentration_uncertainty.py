"""Regression tests for concentration uncertainty definitions."""

import numpy as np
import pandas as pd
from scipy.stats import norm, t

from UPbRPy.concentration import (
    calc_concentration,
    calc_concentration_depth_profile,
    internal_normalization,
)


def test_spot_concentration_interval_uses_standard_error():
    """The reported 95% interval is Student-t times SEM, not sample SD."""
    data = pd.DataFrame(
        {
            "spot_name": ["unknown_1"] * 4,
            "sample": ["unknown"] * 4,
            "206Pb": [10.0, 12.0, 14.0, 16.0],
        }
    )
    calibration = pd.Series({"206Pb": 2.0})

    result = calc_concentration(data, calibration)

    expected_mean = np.mean(data["206Pb"]) * calibration["206Pb"]
    expected_half_width = (
        np.std(data["206Pb"], ddof=1)
        / np.sqrt(len(data))
        * t.ppf(0.975, len(data) - 1)
        * calibration["206Pb"]
    )
    assert np.isclose(result.loc["unknown_1", "Pb"], expected_mean)
    assert np.isclose(
        result.loc["unknown_1", "Pb_95CI"], expected_half_width
    )


def test_depth_concentration_uncertainty_is_propagated_from_counts():
    """Internal-standard and analyte Poisson terms both enter each sweep."""
    # A normalization factor of 500 corresponds to 200000 ppm / 400 counts.
    # The normalized analyte signal of 50000 therefore represents 100 counts.
    normalized = pd.DataFrame(
        {
            "Time": [1.0],
            "sample": ["unknown"],
            "spot_name": ["unknown_1"],
            "internal_normalization_factor": [500.0],
            "29Si": [200000.0],
            "206Pb": [50000.0],
        }
    )
    normalized.attrs["internal_channel"] = "29Si"
    calibration = pd.Series({"206Pb": 0.01})

    result = calc_concentration_depth_profile(normalized, calibration)

    expected_concentration = 500.0
    expected_1s = expected_concentration * np.sqrt(1 / 100 + 1 / 400)
    assert np.isclose(result.loc[0, "Pb"], expected_concentration)
    assert np.isclose(result.loc[0, "Pb_1s"], expected_1s)
    assert np.isclose(result.loc[0, "Pb_2s"], 2 * expected_1s)
    assert np.isclose(
        result.loc[0, "Pb_95CI"],
        norm.ppf(0.975) * expected_1s,
    )


def test_depth_concentration_requires_normalization_metadata():
    """The function refuses to label sqrt(ppm) as counting uncertainty."""
    data = pd.DataFrame(
        {
            "Time": [1.0],
            "sample": ["unknown"],
            "spot_name": ["unknown_1"],
            "29Si": [200000.0],
            "206Pb": [50000.0],
        }
    )
    calibration = pd.Series({"206Pb": 0.01})

    with np.testing.assert_raises_regex(
        KeyError, "internal_normalization_factor"
    ):
        calc_concentration_depth_profile(data, calibration)


def test_internal_normalization_distinguishes_plateau_and_full_signal():
    experiment = pd.DataFrame(
        {
            "spot_names": ["unknown_1"],
            "plateau_start_times": [2.0],
            "plateau_end_times": [4.0],
            "signal_start_times": [1.0],
            "signal_end_times": [5.0],
        }
    )
    baseline_subtracted = pd.DataFrame(
        {
            "Time": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "sample": ["unknown"] * 7,
            "spot_name": ["unknown_1"] * 7,
            "44Ca": [10.0] * 7,
            "238U": [1.0] * 7,
        }
    )
    archive = pd.DataFrame(
        columns=["material", "analyte", "concentration"]
    )

    plateau = internal_normalization(
        experiment,
        ["44Ca", "238U"],
        baseline_subtracted,
        archive,
        material="apatite",
        internal_channel="44Ca",
        region="plateau",
    )
    signal = internal_normalization(
        experiment,
        ["44Ca", "238U"],
        baseline_subtracted,
        archive,
        material="apatite",
        internal_channel="44Ca",
        region="signal",
    )

    assert plateau["Time"].tolist() == [2.0, 3.0]
    assert signal["Time"].tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert plateau.attrs["selection_region"] == "plateau"
    assert signal.attrs["selection_region"] == "signal"
    assert signal.attrs["internal_channel"] == "44Ca"
