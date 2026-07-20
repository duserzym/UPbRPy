"""Benchmark the table-processing core of a zircon-style reduction.

The default workload contains 198 spots, matching the current zircon example.
The second workload is five times larger. Timings intentionally exclude plot
construction so numerical table performance can be distinguished from notebook
rendering and widget overhead.

Run from the repository root with::

    python benchmarks/benchmark_table_pipeline.py

The script prints JSON so results can be archived and compared between commits.
It is a benchmark, not a unit test: no machine-dependent timing threshold is
enforced in the test suite.
"""

from __future__ import annotations

import argparse
import json
from statistics import median
from time import perf_counter

import numpy as np
import pandas as pd

from UPbRPy import (
    apply_DH_correction,
    apply_session_correction,
    calc_concentration,
    calc_depth_profile,
    calc_ratio_mean_and_uncertainty,
    calc_raw_ratios,
    internal_normalization,
    subtract_baseline,
)


CHANNELS = [
    "29Si",
    "202Hg",
    "204Hg",
    "206Pb",
    "207Pb",
    "208Pb",
    "232Th",
    "238U",
]


def make_synthetic_session(spot_count: int, sweeps_per_spot: int = 75):
    """Return deterministic experiment and signal tables for benchmarking."""
    rng = np.random.default_rng(20260719)
    experiment_records = []
    spot_frames = []
    sweep = np.arange(sweeps_per_spot, dtype=float)

    for spot_index in range(spot_count):
        spot_name = f"unknown_{spot_index:05d}"
        start = spot_index * (sweeps_per_spot + 5.0)
        time = start + sweep
        is_signal = (sweep >= 20) & (sweep <= 55)
        baseline = 50.0 + rng.normal(0, 2, sweeps_per_spot)

        frame = pd.DataFrame(
            {
                "Time": time,
                "29Si": baseline + is_signal * 1_000_000,
                "202Hg": baseline + is_signal * 20,
                "204Hg": baseline + is_signal * 60,
                "206Pb": baseline + is_signal * 10_000,
                "207Pb": baseline + is_signal * 900,
                "208Pb": baseline + is_signal * 4_000,
                "232Th": baseline + is_signal * 20_000,
                "238U": baseline + is_signal * 100_000,
                "spot_name": spot_name,
                "sample": "unknown",
            }
        )
        spot_frames.append(frame)
        experiment_records.append(
            {
                "spot_names": spot_name,
                "sample": "unknown",
                "baseline_start_times": start,
                "baseline_end_times": start + 10,
                "signal_start_times": start + 20,
                "signal_end_times": start + 55,
                "plateau_start_times": start + 25,
                "plateau_end_times": start + 45,
            }
        )

    return (
        pd.DataFrame.from_records(experiment_records),
        pd.concat(spot_frames, ignore_index=True),
    )


def make_correction_results(time_max: float):
    """Construct lightweight deterministic correction models."""
    ratios = ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th")
    downhole = {
        ratio: {
            "interpolation": lambda relative_time: (
                1.0 + 0.001 * np.asarray(relative_time, dtype=float)
            ),
            "fit_prediction_x": np.array([0.0, 35.0]),
        }
        for ratio in ratios
    }
    session = {
        ratio: {
            "fit": np.array([1.0]),
            "std": 0.01,
            "fit_observations": {"time": np.array([0.0, time_max])},
        }
        for ratio in ratios
    }
    return downhole, session


def dataframe_megabytes(*frames: pd.DataFrame) -> float:
    """Return deep DataFrame storage for the supplied outputs in MiB."""
    return sum(
        frame.memory_usage(index=True, deep=True).sum() for frame in frames
    ) / 1024**2


def run_workload(spot_count: int, sweeps_per_spot: int = 75):
    """Execute and time the numerical stages for one synthetic session."""
    experiment, total = make_synthetic_session(spot_count, sweeps_per_spot)
    standard_archive = pd.DataFrame(columns=["material", "29Si"])
    concentration_factors = pd.Series(
        {channel: 1e-3 for channel in CHANNELS if channel != "29Si"}
    )
    downhole, session = make_correction_results(float(total["Time"].max()))

    start = perf_counter()
    baseline_corrected = subtract_baseline(experiment, CHANNELS, total)
    normalized = internal_normalization(
        experiment,
        CHANNELS,
        baseline_corrected,
        standard_archive,
        material="zircon",
    )
    concentrations = calc_concentration(normalized, concentration_factors)
    signal_ratios = calc_raw_ratios(
        experiment, total, baseline_corrected, region="signal"
    )
    plateau_ratios = calc_raw_ratios(
        experiment, total, baseline_corrected, region="plateau"
    )
    signal_corrected = apply_session_correction(
        apply_DH_correction(signal_ratios, downhole), session
    )
    plateau_corrected = apply_session_correction(
        apply_DH_correction(plateau_ratios, downhole), session
    )
    depth_profile = calc_depth_profile(signal_corrected, session)
    spot_summary = calc_ratio_mean_and_uncertainty(plateau_corrected, session)
    elapsed = perf_counter() - start

    return {
        "spots": spot_count,
        "input_rows": len(total),
        "elapsed_seconds": round(elapsed, 6),
        "input_megabytes": round(dataframe_megabytes(total, experiment), 3),
        "output_megabytes": round(
            dataframe_megabytes(depth_profile, spot_summary, concentrations), 3
        ),
        "depth_rows": len(depth_profile),
        "summary_rows": len(spot_summary),
    }


def repeat_workload(spot_count: int, sweeps_per_spot: int, repeat: int):
    """Run a workload repeatedly and report its median elapsed time."""
    runs = [run_workload(spot_count, sweeps_per_spot) for _ in range(repeat)]
    representative = dict(runs[-1])
    elapsed_samples = [run["elapsed_seconds"] for run in runs]
    representative["elapsed_seconds"] = round(median(elapsed_samples), 6)
    representative["elapsed_samples"] = elapsed_samples
    representative["repeat"] = repeat
    return representative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spots", type=int, default=198)
    parser.add_argument("--scale", type=int, default=5)
    parser.add_argument("--sweeps", type=int, default=75)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.spots < 1 or args.scale < 1 or args.repeat < 1 or args.sweeps < 56:
        parser.error(
            "spots, scale, and repeat must be positive; sweeps must be at least 56"
        )

    results = [
        repeat_workload(args.spots, args.sweeps, args.repeat),
        repeat_workload(args.spots * args.scale, args.sweeps, args.repeat),
    ]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
