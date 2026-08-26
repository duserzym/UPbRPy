"""Regression tests for uncertainty branches and Table 4 reporting."""

import numpy as np
import pandas as pd

from UPbRPy.age import calc_68_date
from UPbRPy.isoplot import calc_concordia_ellipse
from UPbRPy.results import (
    FinalizedReduction,
    ReductionDecision,
    build_horstwood_table4,
    build_publication_tables,
)
from UPbRPy.uncertainty import (
    UncertaintySettings,
    append_systematic_date_uncertainties,
    common_pb_uncertainty_preset,
)


def test_decay_and_u_ratio_branches_are_separate_and_combinable():
    ratio = 0.18
    age = float(calc_68_date(ratio))
    frame = pd.DataFrame(
        {
            "206Pb/238U_age": [age],
            "206Pb/238U_age_2e_no_calibration": [2.0],
            "206Pb/238U_age_2e_with_calibration": [3.0],
        }
    )
    settings = UncertaintySettings(
        propagate_decay_constants=True,
        propagate_u238_u235=True,
        long_term_reproducibility_mode="user",
        long_term_reproducibility_relative_1s=0.01,
    )
    result = append_systematic_date_uncertainties(frame, settings)

    assert result.loc[0, "206Pb/238U_age_2e_decay_constants"] > 0
    assert result.loc[0, "206Pb/238U_age_2e_u238_u235"] == 0
    assert np.isclose(
        result.loc[0, "206Pb/238U_age_2e_decay_and_u238_u235"],
        result.loc[0, "206Pb/238U_age_2e_decay_constants"],
    )
    expected = 2 * ratio * 0.01 / (
        1.55125e-10 * (1 + ratio)
    ) / 1e6
    assert np.isclose(
        result.loc[0, "206Pb/238U_age_2e_long_term_reproducibility"],
        expected,
    )
    assert (
        result.loc[0, "206Pb/238U_age_2e_with_calibration_selected_systematic"]
        > 3.0
    )


def test_common_pb_presets_are_provenance_explicit():
    boise = common_pb_uncertainty_preset("boise_working")
    redux = common_pb_uncertainty_preset("et_redux")
    assert boise.relative_1s_206_204 == 0.0061
    assert boise.rho_206_207 == 0.98
    assert redux.relative_1s_206_204 == 0.01
    assert redux.rho_206_207 == 0.5


def test_concordia_ellipse_default_is_true_two_sigma():
    x, y = calc_concordia_ellipse(0.0, 0.0, 1.0, 1.0, 0.0, n=721)
    assert np.isclose(np.nanmax(np.abs(x)), 2.0, rtol=1e-5)
    assert np.isclose(np.nanmax(np.abs(y)), 2.0, rtol=1e-5)


def test_horstwood_table_uses_date_terminology_and_optional_concentrations():
    parallel = pd.DataFrame(
        {
            "sample": ["secondary"],
            "spot_name": ["secondary_1"],
            "preferred_age": [500.0],
            "preferred_age_2e": [5.0],
            "preferred_age_method": ["206Pb/238U"],
            "206Pb/238U": [0.08],
            "206Pb/238U_2e_no_calibration": [0.001],
            "206Pb/238U_2e_with_calibration": [0.002],
            "206Pb/238U_age": [500.0],
            "206Pb/238U_age_2e_no_calibration": [4.0],
            "206Pb/238U_age_2e_with_calibration": [5.0],
            "U": [100.0],
            "Th/U": [0.5],
        }
    )
    finalized = FinalizedReduction(
        parallel_results=parallel,
        selected_results=parallel,
        quality_metrics=pd.DataFrame(),
        secondary_standard_summary=pd.DataFrame(),
        decision=ReductionDecision(),
    )
    with_concentration = build_horstwood_table4(finalized)
    dates_only = build_horstwood_table4(
        finalized,
        include_concentrations=False,
    )
    assert "Preferred date (Ma)" in with_concentration
    assert "206Pb/238U date (Ma)" in with_concentration
    assert "U (µg g-1)" in with_concentration
    assert "U (µg g-1)" not in dates_only


def test_publication_methods_and_notes_record_constants_and_references():
    parallel = pd.DataFrame(
        {
            "sample": ["secondary"],
            "spot_name": ["secondary_1"],
            "preferred_date": [500.0],
            "preferred_date_2e": [5.0],
            "preferred_date_method": ["206Pb/238U"],
        }
    )
    finalized = FinalizedReduction(
        parallel_results=parallel,
        selected_results=parallel,
        quality_metrics=pd.DataFrame(),
        secondary_standard_summary=pd.DataFrame(),
        decision=ReductionDecision(),
    )
    tables = build_publication_tables(finalized, include_concentrations=False)
    methods = tables["Methods"].set_index("setting")["value"]
    assert methods["constant.238U/235U"] == 137.818
    assert methods["constant.lambda_238_yr-1"] == 1.55125e-10
    assert methods["constant.lambda_235_yr-1"] == 9.8485e-10
    assert methods["constant.lambda_232_yr-1"] == 4.9475e-11
    notes = " ".join(tables["Table notes"]["note"].astype(str))
    assert "1.55125e-10" in notes
    assert "9.8485e-10" in notes
    assert "4.9475e-11" in notes
    assert "10.1126/science.1215507" in notes
