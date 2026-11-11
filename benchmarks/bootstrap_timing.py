"""Time UPbRPy.regression.bootstrap_discordia on synthetic mixtures.

Usage: .venv/bin/python benchmarks/bootstrap_timing.py
Reports the median of five runs after one warm-up for each size.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from UPbRPy.regression import bootstrap_discordia, concordia_xy, fit_discordia  # noqa: E402


def mixtures(n, seed=0):
    rng = np.random.default_rng(seed)
    X0, Y0 = concordia_xy(1000.0, "tera_wasserburg")
    f = rng.uniform(0.1, 0.9, n)
    X, Y = f * X0, 0.88 + (Y0 - 0.88) * f
    sx, sy = 0.03 * X, 0.02 * Y
    return X + sx * rng.standard_normal(n), Y + sy * rng.standard_normal(n), sx, sy, np.full(n, 0.3)


def main():
    for n, resamples, kwargs in ((50, 2000, {}), (200, 10000, {}),
                                 (50, 2000, {"method": "resample"}),
                                 (2400, 2000, {"method": "resample", "groups": np.repeat(np.arange(86), 28)[:2400]})):
        data = mixtures(n)
        fit = fit_discordia(*data)
        bootstrap_discordia(*data, n_resamples=200, fit=fit, **kwargs)
        times = []
        for seed in range(5):
            start = time.perf_counter()
            bootstrap_discordia(*data, n_resamples=resamples, seed=seed, fit=fit, **kwargs)
            times.append(time.perf_counter() - start)
        label = kwargs.get("method", "parametric") + (" by group" if "groups" in kwargs else "")
        print(f"{n:5d} points x {resamples:6d} resamples ({label}): median {1000 * statistics.median(times):7.1f} ms")


if __name__ == "__main__":
    main()
