"""Time analytical fits and 2000 bootstrap resamples on scattered mixtures.

Usage: .venv/bin/python benchmarks/fit_timing.py [--repeats 3] [--profile]
Medians use the same data for every model and anchor; all times are ms.
"""
from __future__ import annotations

import argparse
import cProfile
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from UPbRPy.regression import Anchor, bootstrap_discordia, concordia_xy, fit_discordia

ANCHORS = {"none": None, "common": Anchor("common_pb", .88),
           "common±": Anchor("common_pb", .88, .01), "age": Anchor("age", 1000.),
           "age±": Anchor("age", 1000., 10.), "SK": Anchor("stacey_kramers")}


def mixtures(n, space="tera_wasserburg"):
    rng = np.random.default_rng(0)
    if n == 200:
        # Match the supplied scratch benchmark: its 30-point draw precedes
        # its 200-point draw in the same generator (bootstraps use own seeds).
        rng.uniform(.1, .9, 30)
        rng.standard_normal(30)
        rng.standard_normal(30)
    X0, Y0 = concordia_xy(1000., "tera_wasserburg")
    f = rng.uniform(.1, .9, n)
    x, y = f * X0, .88 + (Y0 - .88) * f
    sx, sy = .03 * x, .02 * y
    x, y = x + 3*sx*rng.standard_normal(n), y + 3*sy*rng.standard_normal(n)
    rho = np.full(n, .3)
    if space == "wetherill":
        u = 137.818
        X, Y = y*u/x, 1/x
        vx, vy, c = sx*sx, sy*sy, rho*sx*sy
        ax, ay, bx = -u*y/x**2, u/x, -1/x**2
        sX = np.sqrt(ax*ax*vx + ay*ay*vy + 2*ax*ay*c)
        sY = np.abs(bx)*sx
        rho = bx*(ax*vx + ay*c)/(sX*sY)
        x, y, sx, sy = X, Y, sX, sY
    return x, y, sx, sy, rho


def median_ms(call, repeats):
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        times.append(1000*(time.perf_counter()-start))
    return statistics.median(times)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    fit_discordia(*mixtures(30))  # warm imports
    print("space n model | none common common± age age± SK", flush=True)
    for space in ("tera_wasserburg", "wetherill"):
        for n in (30, 200):
            for model in (1, 2, 3):
                data = mixtures(n, space)
                timings = [median_ms(lambda a=a: fit_discordia(*data, space=space, model=model, anchor=a), args.repeats)
                           for a in ANCHORS.values()]
                print(f"{space:16} {n:3} {model} | " + " ".join(f"{t:.2f}" for t in timings), flush=True)
    print("bootstrap TW: n model anchor | single 16x125 4x500", flush=True)
    for n in (30, 200):
        data = mixtures(n)
        for model in (1, 2, 3):
            for name in ("none", "common±"):
                kwargs = dict(model=model, anchor=ANCHORS[name])
                fit = fit_discordia(*data, **kwargs)
                def run(chunk):
                    for k, offset in enumerate(range(0, 2000, chunk)):
                        bootstrap_discordia(*data, **kwargs, fit=fit, n_resamples=min(chunk, 2000-offset),
                                            seed=k, chunk=chunk)
                timings = [median_ms(lambda c=c: run(c), args.repeats) for c in (2000, 125, 500)]
                print(f"{n:3} {model} {name:7} | " + " ".join(f"{t:.2f}" for t in timings), flush=True)
    if args.profile:
        profiler = cProfile.Profile()
        for model in (2, 3):
            for space in ("tera_wasserburg", "wetherill"):
                for anchor in ANCHORS.values():
                    profiler.runcall(fit_discordia, *mixtures(200, space), space=space, model=model, anchor=anchor)
        profiler.print_stats(sort="cumtime")


if __name__ == "__main__":
    main()
