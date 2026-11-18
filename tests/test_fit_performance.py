"""Numerical compatibility with the optimizer captured before acceleration."""
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize._numdiff import approx_derivative

from benchmarks.fit_timing import ANCHORS, mixtures
from UPbRPy.regression import (
    Anchor, DEFAULT_DECAY, _Problem, _initial_theta, fit_discordia, york_fit,
)

BASELINE = json.loads(Path(__file__).with_name("fit_baseline.json").read_text())


@pytest.mark.parametrize("case", BASELINE, ids=lambda c: f"{c['space']}-{c['n']}-{c['model']}-{c['anchor']}")
def test_estimates_match_original_optimizer(case):
    # These numbers came from the original NM/Powell/NM implementation,
    # captured before changing regression.py, including covariance and roots.
    fit = fit_discordia(*mixtures(case["n"], case["space"]), space=case["space"],
                        model=case["model"], anchor=ANCHORS[case["anchor"]])
    for key, expected in case["expected"].items():
        tolerance = 1e-4 if "1s" in key or key == "dispersion" else 1e-6
        np.testing.assert_allclose(fit.summary()[key], expected, rtol=tolerance, atol=1e-12,
                                   equal_nan=True, err_msg=key)


@pytest.mark.parametrize("space", ["tera_wasserburg", "wetherill"])
@pytest.mark.parametrize("model", [1, 2, 3])
@pytest.mark.parametrize("anchor", list(ANCHORS))
def test_likelihood_gradient_including_anchor_and_dispersion(space, model, anchor):
    data = mixtures(30, space)
    problem = _Problem(*data, space, model, ANCHORS[anchor] or Anchor(), DEFAULT_DECAY)
    theta = _initial_theta(problem, york_fit(*data))
    theta += .01*np.maximum(np.abs(theta), 1e-3)
    # Relative steps matter for the small Wetherill slope.
    numerical = approx_derivative(problem.objective, theta, method="3-point", rel_step=1e-5).ravel()
    value, gradient = problem.value_gradient(theta)
    assert np.isfinite(value)
    np.testing.assert_allclose(gradient, numerical, rtol=2e-5, atol=1e-5)


def test_tls_does_not_run_numerical_minimization(monkeypatch):
    import UPbRPy.regression as module
    monkeypatch.setattr(module, "_minimise", lambda *a: pytest.fail("TLS has a closed form"))
    for anchor in (None, Anchor("common_pb", .88), Anchor("age", 1000)):
        assert fit_discordia(*mixtures(200), model=2, anchor=anchor).line.converged
