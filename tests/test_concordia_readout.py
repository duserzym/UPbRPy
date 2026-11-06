"""Qt-free common-age readouts over parallel-result frames."""

from dataclasses import FrozenInstanceError

import numpy as np
import pandas as pd
import pytest

import UPbRPy.isochron as isochron
from UPbRPy.isochron import concordia_age_readout, tera_wasserburg_point, wetherill_point


@pytest.fixture
def concordant_frame():
    ages = np.full(5, 500.)
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({"spot_name": [f"A_{i}" for i in range(5)], "sample": "A",
                          "207Pb/235U": x, "206Pb/238U": y,
                          "238U/206Pb": tx, "207Pb/206Pb": ty})
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[f"{ratio}_2e"] = frame[ratio] * .01
    frame["err_corr"] = -.2
    frame["tw_rho"] = .3
    return frame


def test_readout_recovers_age_in_both_spaces_and_single_spot(concordant_frame):
    wetherill = concordia_age_readout(concordant_frame)
    tw = concordia_age_readout(concordant_frame, space="tw")
    assert wetherill.age_ma == pytest.approx(500, abs=.001)
    assert tw.age_ma == pytest.approx(wetherill.age_ma, abs=.001)
    assert wetherill.n == tw.n == 5
    assert tw.space == "tera_wasserburg"
    assert wetherill.age_2se > 0
    assert "Ma (2σ) · MSWD" in wetherill.text
    single = concordia_age_readout(concordant_frame.iloc[:1])
    assert single.age_ma == pytest.approx(500, abs=.001)
    assert "n = 1" in single.text
    with pytest.raises(FrozenInstanceError):
        single.n = 2


@pytest.mark.parametrize("space,stem", [("wetherill", "err_corr"), ("tw", "tw_rho")])
def test_readout_uses_matching_uncertainty_and_correlation_branches(concordant_frame, monkeypatch, space, stem):
    frame = concordant_frame.copy()
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[f"{ratio}_2e_no_calibration"] = frame[f"{ratio}_2e"]
        frame[f"{ratio}_2e_with_calibration"] = frame[f"{ratio}_2e"] * 3
    frame[f"{stem}_no_calibration"] = -.1
    frame[f"{stem}_with_calibration"] = .4
    calls = []
    original = isochron.concordia_age

    def fit(x, y, sx, sy, rho, **kwargs):
        calls.append((sx, sy, rho))
        return original(x, y, sx, sy, rho, **kwargs)

    monkeypatch.setattr(isochron, "concordia_age", fit)
    analytical = concordia_age_readout(frame, space=space, include_calibration_uncertainty=False)
    total = concordia_age_readout(frame, space=space)
    assert total.age_2se > analytical.age_2se
    np.testing.assert_allclose(calls[1][0], calls[0][0] * 3)
    np.testing.assert_allclose(calls[1][1], calls[0][1] * 3)
    np.testing.assert_allclose(calls[0][2], -.1)
    np.testing.assert_allclose(calls[1][2], .4)


def test_readout_drops_unusable_rows_and_never_changes_frame(concordant_frame):
    frame = pd.concat([concordant_frame] * 2, ignore_index=True)
    for row, column, value in ((0, "207Pb/235U", np.nan), (1, "206Pb/238U", -1),
                               (2, "207Pb/235U_2e", 0), (3, "err_corr", np.inf),
                               (4, "206Pb/238U_2e", "invalid")):
        frame[column] = frame[column].astype(object)
        frame.loc[row, column] = value
    before = frame.copy(deep=True)
    result = concordia_age_readout(frame)
    assert result.n == 5
    assert result.age_ma == pytest.approx(500, abs=.001)
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize("space,stem", [("wetherill", "err_corr"), ("tw", "tw_rho")])
@pytest.mark.parametrize("branch", ["", "_with_calibration", "_no_calibration"])
def test_readout_keeps_missing_correlations_as_zero_and_clips_endpoints(
    concordant_frame, monkeypatch, space, stem, branch,
):
    frame = concordant_frame.copy()
    frame[f"{stem}{branch}"] = [np.nan, -1., 1., -.2, .3]
    before = frame.copy(deep=True)
    calls = []
    original = isochron.concordia_age

    def fit(x, y, sx, sy, rho, **kwargs):
        calls.append(rho)
        return original(x, y, sx, sy, rho, **kwargs)

    monkeypatch.setattr(isochron, "concordia_age", fit)
    result = concordia_age_readout(
        frame, space=space, include_calibration_uncertainty=branch != "_no_calibration",
    )
    assert result.n == len(frame)
    assert result.age_ma == pytest.approx(500, abs=.001)
    np.testing.assert_allclose(calls[0], [0., -.999, .999, -.2, .3])
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize("frame", [None, pd.DataFrame(), pd.DataFrame({"bad": [1]}),
                                   pd.DataFrame({"207Pb/235U": [np.nan]})])
def test_empty_or_bad_readout_is_none(frame):
    assert concordia_age_readout(frame) is None


def test_readout_bad_space_and_failed_fit_are_none(concordant_frame, monkeypatch):
    assert concordia_age_readout(concordant_frame, space="invalid") is None

    def fail(*args, **kwargs):
        raise ValueError("fit failed")

    monkeypatch.setattr(isochron, "concordia_age", fail)
    assert concordia_age_readout(concordant_frame) is None
