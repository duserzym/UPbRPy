"""Regression tests for conventional 207- and 208-method common-Pb ages."""

from __future__ import annotations

import numpy as np
import pandas as pd

from UPbRPy.constants import (
    lambda_235,
    lambda_238,
    old_common_U238_U235,
)
from UPbRPy.isochron import (
    LAMBDA_232,
    age_207_corrected,
    age_207_corrected_uncertainty,
    age_208_corrected,
    age_208_corrected_uncertainty,
    apply_common_pb_corrections,
    stacey_kramers_common_pb,
)
from UPbRPy.isoplot import construct_final_depthpro_df, construct_final_df


def _synthetic_common_pb_measurement(
    age_ma=500.0,
    f206=0.45,
    th_u=0.8,
    common_76=0.84,
    common_68=0.47,
):
    """Return internally concordant total ratios with a known common-Pb mix."""
    age_years = age_ma * 1e6
    radiogenic_68 = np.expm1(lambda_238 * age_years)
    radiogenic_75 = np.expm1(lambda_235 * age_years)
    radiogenic_76 = (
        radiogenic_75
        / radiogenic_68
        / old_common_U238_U235
    )
    radiogenic_82 = np.expm1(LAMBDA_232 * age_years)

    total_68 = radiogenic_68 / (1.0 - f206)
    total_76 = f206 * common_76 + (1.0 - f206) * radiogenic_76
    # Common 206Pb/U is f206*total_68. Convert it to common 208Pb/Th
    # with common 206Pb/208Pb and the sample's Th/U.
    total_82 = radiogenic_82 + f206 * total_68 / (th_u * common_68)
    return total_68, total_76, total_82, th_u, common_76, common_68


def test_stacey_kramers_stage_boundaries():
    at_3700 = stacey_kramers_common_pb(3700.0)
    at_4570 = stacey_kramers_common_pb(4570.0)

    assert np.isclose(at_3700["206Pb/204Pb"], 11.152)
    assert np.isclose(at_3700["207Pb/204Pb"], 12.998)
    assert np.isclose(at_3700["208Pb/204Pb"], 31.23)
    assert np.isclose(at_4570["206Pb/204Pb"], 9.307)
    assert np.isclose(at_4570["207Pb/204Pb"], 10.294)
    assert np.isclose(at_4570["208Pb/204Pb"], 29.487)


def test_fixed_common_pb_methods_recover_synthetic_age():
    ratio_68, ratio_76, ratio_82, th_u, common_76, common_68 = (
        _synthetic_common_pb_measurement()
    )

    age_207 = age_207_corrected(
        ratio_68,
        ratio_76,
        common_76,
        u238_u235=old_common_U238_U235,
    )
    age_208 = age_208_corrected(ratio_68, ratio_82, th_u, common_68)

    assert np.isclose(age_207, 500.0, atol=1e-7)
    assert np.isclose(age_208, 500.0, atol=1e-7)


def test_vba_regression_for_age_and_uncertainty():
    """Reproduce cached row 11 in the supplied Apatite_01Mar24 workbook."""
    ratio_68 = 0.08344318778164854
    ratio_82 = 0.02425968692293224
    th_u = 27.38824261424977
    ratio_76 = 0.13033208654987044
    common_76 = 0.8683140900459079
    common_68 = 0.47552

    age_207 = age_207_corrected(
        ratio_68,
        ratio_76,
        common_76,
        u238_u235=old_common_U238_U235,
    )
    sigma_207 = age_207_corrected_uncertainty(
        age_207,
        ratio_68,
        0.0014023896244557557,
        ratio_76,
        0.006217457242696167,
        common_76,
        0.001,
        u238_u235=old_common_U238_U235,
    )
    age_208 = age_208_corrected(ratio_68, ratio_82, th_u, common_68)
    sigma_208 = age_208_corrected_uncertainty(
        age_208,
        ratio_68,
        0.0014023896244557557,
        ratio_82,
        0.0002667298280827464,
        th_u,
        0.5477648522849954,
        common_68,
        0.002,
    )

    assert np.isclose(age_207, 471.3189748676665)
    assert np.isclose(2 * sigma_207, 17.337319533238016)
    assert np.isclose(age_208, 473.5484955187427)
    assert np.isclose(2 * sigma_208, 15.219490744413662)


def test_dataframe_correction_retains_values_and_flags_high_th_u():
    ratio_68, ratio_76, ratio_82, th_u, common_76, common_68 = (
        _synthetic_common_pb_measurement(th_u=6.0)
    )
    data = pd.DataFrame(
        {
            "206Pb/238U": [ratio_68],
            "206Pb/238U_2e": [0.002],
            "207Pb/206Pb": [ratio_76],
            "207Pb/206Pb_2e": [0.004],
            "208Pb/232Th": [ratio_82],
            "208Pb/232Th_2e": [0.003],
            "Th/U": [th_u],
        }
    )
    corrected = apply_common_pb_corrections(
        data,
        common_pb_model=None,
        common_pb207_pb206=common_76,
        common_pb206_pb208=common_68,
        u238_u235=old_common_U238_U235,
    )

    assert np.isclose(corrected.loc[0, "207Pb_corrected_age"], 500.0)
    assert np.isclose(corrected.loc[0, "208Pb_corrected_age"], 500.0)
    assert corrected.loc[0, "207Pb_correction_valid"]
    assert not corrected.loc[0, "208Pb_correction_valid"]
    assert "Th/U exceeds 5" in corrected.loc[0, "208Pb_correction_warning"]


def test_common_pb_rich_material_enables_default_corrections():
    ratio_68, ratio_76, ratio_82, th_u, common_76, common_68 = (
        _synthetic_common_pb_measurement()
    )
    concentrations = pd.DataFrame(
        {
            "spot_name": ["A 1"],
            "sample": ["A"],
            "Th": [th_u * 10.0],
            "U": [10.0],
        }
    )
    ratios = pd.DataFrame(
        {
            "spot_name": ["A 1"],
            "sample": ["A"],
            "206Pb/238U": [ratio_68],
            "206Pb/238U_2e": [0.002],
            "207Pb/206Pb": [ratio_76],
            "207Pb/206Pb_2e": [0.004],
            "208Pb/232Th": [ratio_82],
            "208Pb/232Th_2e": [0.003],
        }
    )
    options = {
        "common_pb_model": None,
        "common_pb207_pb206": common_76,
        "common_pb206_pb208": common_68,
        "u238_u235": old_common_U238_U235,
    }

    apatite = construct_final_df(
        concentrations,
        ratios,
        material="apatite",
        common_pb_options=options,
    )
    unspecified = construct_final_df(concentrations, ratios)

    assert "207Pb_corrected_age" in apatite
    assert "208Pb_corrected_age" in apatite
    assert "207Pb_corrected_age" not in unspecified


def test_depth_profile_merge_uses_spot_identity_as_well_as_time():
    concentrations = pd.DataFrame(
        {
            "Time": [1.0, 1.0],
            "sample": ["A", "B"],
            "spot_name": ["A_1", "B_1"],
            "U": [10.0, 20.0],
        }
    )
    ratios = pd.DataFrame(
        {
            "Time": [1.0, 1.0],
            "sample": ["A", "B"],
            "spot_name": ["A_1", "B_1"],
            "206Pb/238U": [0.1, 0.2],
        }
    )

    merged = construct_final_depthpro_df(
        concentrations,
        ratios,
        common_pb_correction=False,
    )

    assert len(merged) == 2
    assert merged.set_index("spot_name").loc["A_1", "U"] == 10.0
    assert merged.set_index("spot_name").loc["B_1", "206Pb/238U"] == 0.2
