import numpy as np
import pandas as pd

from UPbRPy.isochron import (
    apply_204pb_common_pb_correction,
    apply_common_pb_corrections,
)
from UPbRPy.results import (
    AutomaticFlagSettings,
    FinalizedReduction,
    ReductionDecision,
    apply_automatic_flags,
    assign_preferred_age,
    calculate_spot_quality_metrics,
    build_publication_tables,
    export_publication_tables,
    summarize_secondary_standards,
    weighted_mean_and_mswd,
)


def build_mass_204_example():
    """Return totals generated from a known radiogenic/common-Pb mixture."""
    common_64 = 18.0
    common_74 = 15.6
    common_84 = 38.0
    total_64 = 30.0
    total_84 = 50.0
    radiogenic_68 = 0.1
    radiogenic_76 = 0.06
    radiogenic_82 = 0.05
    radiogenic_fraction_206 = (total_64 - common_64) / total_64
    radiogenic_fraction_208 = (total_84 - common_84) / total_84
    total_68 = radiogenic_68 / radiogenic_fraction_206
    total_76 = (
        radiogenic_76 * (total_64 - common_64) + common_74
    ) / total_64
    total_82 = radiogenic_82 / radiogenic_fraction_208
    frame = pd.DataFrame(
        {
            "sample": ["apatite"],
            "spot_name": ["A1"],
            "206Pb/204Pb": [total_64],
            "206Pb/204Pb_2e": [0.2],
            "206Pb/208Pb": [total_64 / total_84],
            "206Pb/208Pb_2e": [0.01],
            "206Pb/238U": [total_68],
            "206Pb/238U_2e_no_calibration": [0.002],
            "206Pb/238U_2e_with_calibration": [0.003],
            "207Pb/206Pb": [total_76],
            "207Pb/206Pb_2e_no_calibration": [0.001],
            "207Pb/206Pb_2e_with_calibration": [0.002],
            "208Pb/232Th": [total_82],
            "208Pb/232Th_2e_no_calibration": [0.002],
            "208Pb/232Th_2e_with_calibration": [0.003],
        }
    )
    for ratio in ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th"):
        frame[ratio + "_2e"] = frame[ratio + "_2e_with_calibration"]
    return frame


def test_mass_204_correction_recovers_known_radiogenic_ratios():
    corrected = apply_204pb_common_pb_correction(
        build_mass_204_example(),
        common_pb_model=None,
        common_pb206_pb204=18.0,
        common_pb207_pb204=15.6,
        common_pb208_pb204=38.0,
    )

    assert np.isclose(corrected.loc[0, "204Pb_corrected_206Pb/238U"], 0.1)
    assert np.isclose(corrected.loc[0, "204Pb_corrected_207Pb/206Pb"], 0.06)
    assert np.isclose(corrected.loc[0, "204Pb_corrected_208Pb/232Th"], 0.05)
    assert bool(corrected.loc[0, "204Pb_correction_valid"])
    assert (
        corrected.loc[
            0, "204Pb_corrected_206Pb/238U_2e_with_calibration"
        ]
        > corrected.loc[
            0, "204Pb_corrected_206Pb/238U_2e_no_calibration"
        ]
    )

    all_methods_input = build_mass_204_example()
    all_methods_input["Th/U"] = 1.0
    all_methods = apply_common_pb_corrections(
        all_methods_input,
        common_pb_model=None,
        common_pb206_pb204=18.0,
        common_pb207_pb204=15.6,
        common_pb208_pb204=38.0,
        common_pb207_pb206=15.6 / 18.0,
        common_pb206_pb208=18.0 / 38.0,
    )
    assert "mass_204_common_pb_correction" in all_methods.attrs
    assert all_methods.attrs["common_pb_correction"]["methods"] == (
        "204",
        "207",
        "208",
    )


def test_quality_flags_and_preferred_age_are_explicit():
    sweeps = pd.DataFrame(
        {
            "spot_name": ["A1", "A1", "A1"],
            "signal_relative_time": [0.0, 1.0, 2.0],
            "206Pb/238U": [0.12, 0.10, 0.08],
        }
    )
    metrics = calculate_spot_quality_metrics(sweeps)
    results = pd.DataFrame(
        {
            "spot_name": ["A1"],
            "sample": ["unknown"],
            "206Pb/238U_age": [800.0],
            "206Pb/238U_age_2e_no_calibration": [8.0],
            "206Pb/238U_age_2e_with_calibration": [12.0],
            "206Pb/238U_age_2e_with_calibration%": [1.5],
            "207Pb/206Pb_age": [850.0],
            "207Pb/206Pb_age_2e_no_calibration": [20.0],
            "207Pb/206Pb_age_2e_with_calibration": [30.0],
            "disc%": [5.0],
        }
    )
    flagged = apply_automatic_flags(
        results,
        quality_metrics=metrics,
        settings=AutomaticFlagSettings(),
    )
    selected = assign_preferred_age(
        flagged,
        decision=ReductionDecision(
            common_pb_method="none",
            include_calibration_uncertainty=False,
            age_transition_ma=1000,
        ),
    )

    assert bool(selected.loc[0, "flag_reverse_fractionation"])
    assert selected.loc[0, "preferred_age"] == 800.0
    assert selected.loc[0, "preferred_age_2e"] == 8.0
    assert selected.loc[0, "preferred_age_method"] == "206Pb/238U"
    assert not bool(selected.loc[0, "preferred_age_pass"])


def test_secondary_standard_weighted_mean_and_mswd():
    summary = weighted_mean_and_mswd([100.0, 102.0], [2.0, 2.0])
    assert summary["n"] == 2
    assert np.isclose(summary["weighted_mean"], 101.0)
    assert np.isclose(summary["weighted_mean_2e"], np.sqrt(2))
    assert np.isclose(summary["mswd"], 2.0)

    results = pd.DataFrame(
        {
            "sample": ["secondary", "secondary"],
            "preferred_age": [100.0, 102.0],
            "preferred_age_2e": [2.0, 2.0],
            "preferred_age_pass": [True, True],
        }
    )
    standards = summarize_secondary_standards(
        results,
        accepted_values={"secondary": (100.5, 1.0)},
    )
    assert standards.loc[0, "n"] == 2
    assert np.isclose(standards.loc[0, "weighted_mean"], 101.0)
    assert np.isclose(standards.loc[0, "offset_from_accepted_age"], 0.5)


def test_publication_export_writes_styled_workbook_and_csv(tmp_path):
    tables = {
        "Results": pd.DataFrame(
            {
                "sample": ["unknown"],
                "preferred_age": [100.0],
                "preferred_age_2e": [2.0],
                "preferred_age_pass": [True],
            }
        ),
        "Methods": pd.DataFrame(
            {"setting": ["common_pb_method"], "value": ["none"]}
        ),
    }
    workbook = export_publication_tables(
        tables,
        tmp_path / "publication_tables.xlsx",
    )

    assert workbook.exists()
    assert workbook.stat().st_size > 0
    assert (
        tmp_path / "publication_tables" / "results.csv"
    ).exists()
    assert (
        tmp_path / "publication_tables" / "methods.csv"
    ).exists()


def test_publication_tables_include_depth_results_and_explanatory_notes():
    """The final workbook contract documents and includes sweep-wise output."""
    spot_results = pd.DataFrame(
        {
            "sample": ["unknown"],
            "spot_name": ["spot_1"],
            "preferred_age": [100.0],
            "preferred_age_2e": [2.0],
        }
    )
    finalized = FinalizedReduction(
        parallel_results=spot_results.copy(),
        selected_results=spot_results.copy(),
        quality_metrics=pd.DataFrame(),
        secondary_standard_summary=pd.DataFrame(),
        decision=ReductionDecision(),
    )
    depth_profile = pd.DataFrame(
        {
            "sample": ["unknown"],
            "spot_name": ["spot_1"],
            "signal_relative_time": [0.0],
            "206Pb/238U_fractionation_corrected": [0.02],
            "206Pb/238U_fractionation_corrected_2e_no_calibration": [0.001],
            "206Pb/238U_fractionation_corrected_2e_with_calibration": [0.002],
            "206Pb/238U_age": [127.7],
            "raw_detector_column": [12345.0],
        }
    )

    tables = build_publication_tables(
        finalized,
        depth_profile=depth_profile,
    )

    assert "Depth profiles" in tables
    assert "Table notes" in tables
    assert "raw_detector_column" not in tables["Depth profiles"]
    depth_notes = tables["Table notes"].query(
        "worksheet == 'Depth profiles'"
    )["note"].str.cat(sep=" ")
    assert "signal_relative_time" in depth_notes
    assert "Plateau outlier rejection" in depth_notes
