"""Counting statistics use integrated counts, not Qtegra count rates."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from UPbRPy import calc_depth_profile, calc_raw_ratios, read_experiment_data
from UPbRPy.concentration import calc_concentration_depth_profile
from UPbRPy.qtegra import _count_quantum, counting_dwell, read_rich_csv, read_spot_folder


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/qtegra/Zircon_04Nov26_slice"
RICH = FIXTURE / "Zircon_04Nov26_slice_Rich.csv"
FOLDER = FIXTURE / "zircon_04Nov26"
CHANNELS = ("202Hg", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U")


def test_count_quantum_recovers_step_from_rates():
    rng = np.random.default_rng(1)
    dwell = 0.075
    counts = rng.poisson(3.0, 2000)
    rates = counts / dwell * (1 + 2e-6 * counts)  # small dead-time correction
    assert _count_quantum(rates) == pytest.approx(1 / dwell, rel=1e-4)
    # Counts that never fall to one count still give the step from the gaps.
    assert _count_quantum((counts + 11) / dwell) == pytest.approx(1 / dwell, rel=1e-4)
    # Continuous analog-like values are not quantized.
    assert _count_quantum(rng.uniform(1e5, 2e5, 2000)) is None


def test_rich_dwell_from_count_quantization_matches_recorded_dwell():
    recorded = counting_dwell(read_spot_folder(FOLDER))
    inferred = counting_dwell(read_rich_csv(RICH))
    assert {source for _value, source in recorded.values()} == {"recorded"}
    for channel in ("91Zr", "177Hf", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U"):
        value, source = inferred[channel]
        assert source == "count_quantization", channel
        assert value == pytest.approx(recorded[channel][0], rel=1e-3), channel


def test_reader_records_counting_dwell_on_experiment():
    experiment, _channels, _total = read_experiment_data(RICH, primary_channel="29Si")
    record = experiment.attrs["counting_dwell"]
    assert record["dwell_s"]["206Pb"] == pytest.approx(0.06, rel=1e-3)
    assert record["source"]["206Pb"] == "count_quantization"
    assert set(record["dwell_s"]) == set(record["source"])


def _synthetic_spot(dwell, n_sweeps=4000, seed=7):
    """One spot with zero baseline and Poisson counts stored as cps."""
    rng = np.random.default_rng(seed)
    mean_counts = {
        "202Hg": 50.0, "204Hg": 10.0, "206Pb": 2000.0, "207Pb": 400.0,
        "208Pb": 800.0, "232Th": 3000.0, "238U": 6000.0,
    }
    n_baseline = 5
    times = np.arange(n_baseline + n_sweeps, dtype=float)
    data = {"sample": "RM", "spot_name": "RM_1", "Time": times}
    for channel, mean in mean_counts.items():
        counts = np.concatenate([np.zeros(n_baseline), rng.poisson(mean, n_sweeps)])
        data[channel] = counts / dwell[channel]
    total = pd.DataFrame(data)
    experiment = pd.DataFrame({
        "spot_names": ["RM_1"],
        "baseline_start_times": [0.0],
        "baseline_end_times": [float(n_baseline)],
        "signal_start_times": [float(n_baseline)],
        "signal_end_times": [times[-1]],
    })
    return experiment, total, mean_counts


def test_raw_ratio_errors_use_dwell_and_match_poisson_scatter():
    dwell = dict(zip(CHANNELS, (0.02, 0.02, 0.075, 0.075, 0.075, 0.005, 0.025)))
    experiment, total, mean_counts = _synthetic_spot(dwell)

    ratios = calc_raw_ratios(experiment, total, total, region="signal", dwell_s=dwell)

    n206 = total["206Pb"].iloc[5:].to_numpy() * dwell["206Pb"]
    n207 = total["207Pb"].iloc[5:].to_numpy() * dwell["207Pb"]
    n238 = total["238U"].iloc[5:].to_numpy() * dwell["238U"]
    np.testing.assert_allclose(
        ratios["207Pb/206Pb_2e"] / ratios["207Pb/206Pb"],
        2 * np.sqrt(1 / n207 + 1 / n206),
    )
    np.testing.assert_allclose(
        ratios["206Pb/238U_2e"] / ratios["206Pb/238U"],
        2 * np.sqrt(1 / n206 + 1 / n238),
    )
    np.testing.assert_allclose(ratios["206Pb_counts"], n206)
    np.testing.assert_allclose(ratios["207Pb_counts"], n207)
    np.testing.assert_allclose(ratios["238U_counts"], n238)
    assert ratios.attrs["counting_dwell_s"]["206Pb"] == 0.075

    # The stated errors describe the simulated scatter.
    for ratio in ("207Pb/206Pb", "206Pb/238U"):
        z = (ratios[ratio] - ratios[ratio].mean()) / (ratios[ratio + "_2e"] / 2)
        assert np.std(z) == pytest.approx(1.0, abs=0.05), ratio


def test_raw_ratios_read_dwell_from_experiment_attrs():
    dwell = dict.fromkeys(CHANNELS, 0.05)
    experiment, total, _ = _synthetic_spot(dwell, n_sweeps=20)
    explicit = calc_raw_ratios(experiment, total, total, region="signal", dwell_s=dwell)
    experiment.attrs["counting_dwell"] = {"dwell_s": dwell}
    from_attrs = calc_raw_ratios(experiment, total, total, region="signal")
    np.testing.assert_allclose(from_attrs["207Pb/206Pb_2e"], explicit["207Pb/206Pb_2e"])

    # Without any dwell the signals are taken as counts (dwell 1 s).
    experiment.attrs.clear()
    legacy = calc_raw_ratios(experiment, total, total, region="signal")
    np.testing.assert_allclose(
        legacy["207Pb/206Pb_2e"], explicit["207Pb/206Pb_2e"] * np.sqrt(0.05)
    )


def test_depth_profile_correlations_use_integrated_counts():
    n206, n207, n238 = 100.0, 25.0, 400.0
    dwell = 0.05
    ratio_68 = n206 / n238
    ratio_76 = n207 / n206
    data = pd.DataFrame({
        "sample": ["unknown"],
        "spot_name": ["u1"],
        "Time": [10.0],
        "signal_relative_time": [0.0],
        "206Pb": [n206 / dwell],
        "207Pb": [n207 / dwell],
        "238U": [n238 / dwell],
        "206Pb_counts": [n206],
        "207Pb_counts": [n207],
        "238U_counts": [n238],
        "206Pb/238U": [ratio_68],
        "206Pb/238U_2e": [2 * ratio_68 * np.sqrt(1 / n206 + 1 / n238)],
        "207Pb/206Pb": [ratio_76],
        "207Pb/206Pb_2e": [2 * ratio_76 * np.sqrt(1 / n207 + 1 / n206)],
        "206Pb/238U_fractionation_corrected": [ratio_68],
        "207Pb/206Pb_fractionation_corrected": [ratio_76],
    })
    session = {
        ratio: {"fit": np.array([1.0]), "std": 0.0,
                "fit_observations": {"time": np.array([9.0, 11.0])}}
        for ratio in ("206Pb/238U", "207Pb/206Pb")
    }

    result = calc_depth_profile(data, session).iloc[0]

    expected_rho = (1 / n206) / np.sqrt((1 / n238 + 1 / n206) * (1 / n207 + 1 / n206))
    assert result["tw_rho_no_calibration"] == pytest.approx(expected_rho)
    assert result["207Pb/235U_fractionation_corrected_2e_no_calibration%"] == pytest.approx(
        200 * np.sqrt(1 / n207 + 1 / n238)
    )


def test_depth_concentration_counts_use_dwell():
    normalized = pd.DataFrame({
        "Time": [1.0],
        "sample": ["unknown"],
        "spot_name": ["unknown_1"],
        "internal_normalization_factor": [500.0],
        "29Si": [200000.0],
        "206Pb": [50000.0],
    })
    normalized.attrs["internal_channel"] = "29Si"
    calibration = pd.Series({"206Pb": 0.01})

    result = calc_concentration_depth_profile(
        normalized, calibration, dwell_s={"29Si": 0.01, "206Pb": 0.05}
    )

    # 400 cps x 0.01 s = 4 counts; 100 cps x 0.05 s = 5 counts.
    assert result.loc[0, "Pb_1s"] == pytest.approx(500.0 * np.sqrt(1 / 5 + 1 / 4))
