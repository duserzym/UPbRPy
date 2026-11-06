"""Publication quantities tested independently of a real analytical session."""

from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from UPbRPy.constants import lambda_232, lambda_235, lambda_238
from UPbRPy.export_table import (
    CHONDRITE_REE, ExportSettings, build_export_table,
    secondary_summary_wide, summarize_reference_materials_by_system,
)
from UPbRPy.pipeline import PipelineConfig, PipelineStage, ReductionPipeline
from UPbRPy.results import FinalizedReduction, ReductionDecision
from UPbRPy.standards import standard_archive
from UPbRPy.uncertainty import relative_ratio_date_component, systematic_date_components


def synthetic_pipeline():
    """Cache final dates over small, real concentration-normalization inputs."""
    samples = ["B", "S513", "A", "B", "91500", "NIST612", "A"]
    names = [f"{s}_{i}" for i, s in enumerate(samples)]
    experiment = pd.DataFrame(dict(spot_names=names, sample=samples,
        spot_starting_times=np.arange(7) * 10., baseline_start_times=np.arange(7) * 10.,
        baseline_end_times=np.arange(7) * 10. + 2, plateau_start_times=np.arange(7) * 10. + 2,
        plateau_end_times=np.arange(7) * 10. + 7))
    channels = ["29Si", "202Hg", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U", "49Ti"]
    atoms = np.array([1., *[standard_archive.loc["NIST612", f"{mass}Pb/204Pb_i"] for mass in (206, 207, 208)]])
    records = []
    for i, (name, sample) in enumerate(zip(names, samples)):
        # A and glass have the same isotope composition. B has excess 206Pb.
        signal = dict(zip(channels, (10000, 10, atoms[0] * 10 + 2, atoms[1] * 10,
            atoms[2] * 10, atoms[3] * 10, 200, 300, 100)))
        if sample == "B":
            signal["206Pb"] *= 5
        for j in range(8):
            row = dict(spot_name=name, sample=sample, Time=i * 10. + j)
            for channel in channels:
                baseline = 10 if channel == "202Hg" else 2 if channel == "204Hg" else 1
                row[channel] = float(baseline + (signal[channel] if j >= 2 else 0))
            records.append(row)
    frame = pd.DataFrame(dict(spot_name=names, sample=samples))
    for system, age in (("206Pb/238U", 560.), ("207Pb/235U", 562.), ("207Pb/206Pb", 565.), ("208Pb/232Th", 559.)):
        frame[system + "_age"] = age
        frame[system + "_age_2e_no_calibration"] = 2.
        frame[system + "_age_2e_with_calibration"] = 4.
        frame[system] = .1
        frame[system + "_2e_no_calibration"] = .001
    frame["206Pb/204Pb"] = 20.
    frame["206Pb/204Pb_2e"] = 4.
    frame["err_corr_no_calibration"] = .25
    frame["preferred_date"] = frame["206Pb/238U_age"]
    frame["preferred_date_method"] = "206Pb/238U"
    frame["preferred_date_2e"] = 999.  # Export must choose the analytical branch.
    frame["disc%"] = 999.
    frame["disc_2e%"] = 1000.
    frame["flag_detectable_204pb"] = True
    frame["flag_burn_through"] = False
    frame.loc[0, "flag_burn_through"] = True
    config = PipelineConfig(primary_68_standard="S513", primary_76_standard="S513", primary_82_standard="S513",
                            secondary_standards=("91500",), concentration_standards=("NIST612",))
    pipeline = ReductionPipeline(experiment, pd.DataFrame(records), channels, standard_archive,
                                 config, build_diagnostic_plots=False)
    pipeline.cache.set(PipelineStage.FINALIZE, FinalizedReduction(frame, frame.copy(), pd.DataFrame(),
        pd.DataFrame(), config.decision))
    return pipeline


def test_settings_roundtrip_and_validation():
    settings = ExportSettings(optional_columns=("204Pb",), operator="Operator", a_tio2=.7)
    assert ExportSettings.from_dict({**json.loads(json.dumps(settings.to_dict())), "future": 1}) == settings
    for values in ({"a_sio2": 0}, {"a_tio2": np.nan}, {"discordance_threshold_percent": -1},
                   {"discordance_definition": "other"}, {"csv_full_precision": "yes"},
                   {"optional_columns": "Pb"}, {"operator": 2}):
        with pytest.raises((ValueError, TypeError)):
            ExportSettings.from_dict(values)


def test_sys_decomposition_order_notes_and_no_mutation():
    pipeline = synthetic_pipeline()
    original = pipeline.run_finalize().parallel_results.copy(deep=True)
    table = build_export_table(pipeline, excluded_spots=("B_3",))
    rows = table.rows.set_index("spot_name")
    assert table.rows.spot_name.tolist() == ["B_0", "B_3", "A_2", "A_6", "S513_1", "91500_4", "NIST612_5"]
    assert rows.loc["B_0", "notes"] == "204Pb detected, burn-through"
    assert rows.loc["B_3", "notes"] == "excluded, 204Pb detected"
    assert "concentration reference glass" in rows.loc["NIST612_5", "notes"]
    assert pd.isna(rows.loc["NIST612_5", "206Pb/238U_age"])
    assert rows.loc["NIST612_5", "U"] > 0
    assert rows.loc["B_0", "206Pb_cps"] > 0
    assert rows.loc["B_0", "206Pb/204Pb_1s"] == 2
    ref = table.context["primary_reference_terms"]["206Pb/238U"]
    assert ref["available"] and ref["quantity"] == "206Pb/238U_age"
    relative = (.63 / 1.959963984540054) / relative_ratio_date_component(561.18, "206Pb/238U", 1.)
    assert ref["relative_ratio_1s"] == pytest.approx(relative)
    assert not table.context["primary_reference_terms"]["207Pb/206Pb"]["available"]
    physical = systematic_date_components(560., "206Pb/238U", pipeline.config.uncertainty)
    expected = np.sqrt(4.**2 + (2 * physical["decay_1s"])**2 + (2 * relative_ratio_date_component(560., "206Pb/238U", relative))**2)
    assert rows.loc["B_0", "206Pb/238U_age_2e_sys"] == pytest.approx(expected)
    assert rows.loc["B_0", "preferred_date_2e"] == 2.
    assert rows.loc["B_0", "preferred_date_2e_sys"] == pytest.approx(expected)
    for system in ("206Pb/238U", "207Pb/235U", "207Pb/206Pb", "208Pb/232Th"):
        assert (rows[system + "_age_2e_sys"].dropna() >= 2).all()
    pd.testing.assert_frame_equal(pipeline.run_finalize().parallel_results, original)
    keys = [c.key for c in table.columns]
    assert len(keys) == len(set(keys))
    assert "204Pb" not in {c.key for c in table.visible_columns()}
    enabled = replace(table.settings, optional_columns=("204Pb",), excluded_columns=("Th/U",))
    table.settings = enabled
    assert "204Pb" in {c.key for c in table.visible_columns()}
    assert "Th/U" not in {c.key for c in table.visible_columns()}
    assert "La_N" not in {c.key for c in table.visible_columns()}  # Entirely blank.


def test_total_pb_natural_radiogenic_and_missing_204():
    pipeline = synthetic_pipeline()
    table = build_export_table(pipeline)
    rows = table.rows.set_index("spot_name")
    assert rows.loc["A_2", "Pb"] == pytest.approx(rows.loc["A_2", "Pb_208"])
    assert rows.loc["B_0", "Pb"] > rows.loc["B_0", "Pb_208"] * 1.9
    assert table.context["total_pb"]["hg_correction"] == "per-spot baseline 204Hg/202Hg"
    abundance = table.context["total_pb"]["glass_mass_abundances"]["NIST612"]["204Pb"]
    assert rows.loc["A_2", "204Pb"] / rows.loc["A_2", "Pb"] == pytest.approx(abundance)
    # A Qtegra mass-204 channel named Pb still contains Hg interference.
    renamed = synthetic_pipeline()
    renamed.channels = tuple("204Pb" if c == "204Hg" else c for c in renamed.channels)
    renamed.total_data = renamed.total_data.rename(columns={"204Hg": "204Pb"})
    alias_rows = build_export_table(renamed).rows.set_index("spot_name")
    assert alias_rows.loc["A_2", "Pb"] == pytest.approx(rows.loc["A_2", "Pb"])
    pipeline.run_finalize().parallel_results.loc[2, "206Pb/204Pb"] = np.nan
    no204 = build_export_table(pipeline).rows.set_index("spot_name")
    assert no204.loc["A_2", "204Pb"] == 0
    assert no204.loc["A_2", "Pb"] == pytest.approx(rows.loc["A_2", "Pb"] - rows.loc["A_2", "204Pb"])
    # No 202Hg: never substitute an assumed natural Hg ratio.
    pipeline.channels = tuple(c for c in pipeline.channels if c != "202Hg")
    pipeline.cache.get(PipelineStage.NORMALIZATION).drop(columns="202Hg", inplace=True)
    missing = build_export_table(pipeline)
    assert missing.context["total_pb"]["hg_correction"] == "unavailable: no 202Hg channel"


@pytest.mark.parametrize("definition,other,rho", [("7/5-6/8", "207Pb/235U", .25), ("7/6-6/8", "207Pb/206Pb", 0.)])
def test_discordance_absolute_uncertainty(definition, other, rho):
    pipeline = synthetic_pipeline()
    table = build_export_table(pipeline, ExportSettings(discordance_definition=definition))
    row = table.rows.iloc[0]
    b = row[other + "_age"]
    assert row.disc_pct == pytest.approx(100 * (1 - 560 / b))
    assert row.disc_2s_pct == pytest.approx(100 * np.sqrt((2 / b)**2 + (560 * 2 / b**2)**2 - 2 * rho * 2 * 2 * 560 / b**3))
    assert row.disc_2s_pct < 1  # Percentage points, not relative discordance error.
    pipeline.run_finalize().parallel_results.loc[0, "206Pb/238U_age"] = 450
    assert "discordant" in build_export_table(pipeline).rows.iloc[0].notes


def test_trace_formulas():
    from UPbRPy.export_table import _trace_quantities
    from UPbRPy.uncertainty import UncertaintySettings
    values = dict(La=41.17635624508470, Ce=38.041372672818, Pr=39.082803832592425,
        Nd=38.805450105575325, Sm=40.82992084458744, Eu=39.335519136907514,
        Gd=37.769133818285184, Yb=39.1775303681023, Y=38.4042874186939,
        Ti=39.69005816038722, Lu=10., Hf=20., Nb=30., Ta=5., P=60., Th=12., U=40.,
        Dy=15., Tb=3., Ho=4., Er=5., Tm=6., preferred_date=560.)
    uncertainty = UncertaintySettings(u238_u235=140.)
    result = _trace_quantities(pd.DataFrame([values]), ExportSettings(), uncertainty).iloc[0]
    for el, value in CHONDRITE_REE.items():
        assert result[el + "_N"] == pytest.approx(values[el] / value)
    assert result["Ce/Ce*"] == pytest.approx(.2325005155682494)
    assert result["Eu/Eu*"] == pytest.approx(3.0623211911801667)
    assert result["10000Eu/Eu*/Y"] == pytest.approx(797.3904470076258)
    # The specified Ferry-Watson formula gives 960.63; the spreadsheet value
    # 960.784 agrees within the spec's 2e-4 relative tolerance.
    assert result.Ti_in_zircon_C == pytest.approx(960.784, rel=2e-4)
    assert result.Ti_in_zircon_C == pytest.approx(round(4800 / (5.711 - np.log10(values["Ti"]) + np.log10(.6)) - 273.15, 2))
    ratios = "Lu/Hf Nb/Ta Nb/U Nb/Th Th/Y Hf/Y Nb/Yb Ce/Yb Gd/Yb U/Yb Nb/P Ce/P Dy/Yb Ce/U".split()
    for ratio in ratios:
        a, b = ratio.split("/")
        assert result[ratio] == pytest.approx(values[a] / values[b])
    assert result["sum_REE"] == pytest.approx(sum(values[el] for el in CHONDRITE_REE))
    assert result["sum_REE_Y"] == pytest.approx(result.sum_REE + values["Y"])
    assert result["(Gd/Yb)_N"] == pytest.approx((values["Gd"] / .2055) / (values["Yb"] / .17))
    assert result["(Ce/Nd)/Y"] == pytest.approx(values["Ce"] / values["Nd"] / values["Y"])
    assert result["10000Eu/Eu*/Yb_N"] == pytest.approx(10000 * result["Eu/Eu*"] / (values["Yb"] / .17))
    assert result["1000(Ce/Nd)_N/Y"] == pytest.approx(1000 * (values["Ce"] / .612) / (values["Nd"] / .467) / values["Y"])
    assert result.U_i == pytest.approx(40 * (140 / 141 * np.exp(lambda_238 * 560e6) + 1 / 141 * np.exp(lambda_235 * 560e6)))
    assert result.Th_i == pytest.approx(12 * np.exp(lambda_232 * 560e6))


def test_sample_summary_rule_and_shared_systematics():
    pipeline = synthetic_pipeline()
    table = build_export_table(pipeline)
    summaries = table.sample_summaries.set_index("sample")
    a = summaries.loc["A"]
    assert a.status == "ok" and a.k == 2
    assert a["2s"] == pytest.approx(np.sqrt(2))
    row = table.rows.iloc[0]
    assert a["2s_sys"]**2 - a["2s"]**2 == pytest.approx(row.preferred_date_2e_sys**2 - 4)
    assert "weighted mean 206Pb/238U date" in a.summary_text
    assert "accepted" in summaries.loc["S513", "summary_text"]
    frame = pipeline.run_finalize().parallel_results
    frame.loc[6, "preferred_date_method"] = "207Pb/206Pb"
    mixed = build_export_table(pipeline).sample_summaries.set_index("sample").loc["A"]
    assert mixed.status == "mixed methods" and np.isnan(mixed["mean"])
    frame.loc[6, "preferred_date_method"] = "206Pb/238U"
    frame.loc[6, "preferred_date"] = 1000
    dispersed = build_export_table(pipeline).sample_summaries.set_index("sample").loc["A"]
    assert dispersed.status == "dispersed" and dispersed.probability < .001
    excluded = build_export_table(pipeline, excluded_spots=("A_6",)).sample_summaries.set_index("sample").loc["A"]
    assert excluded.n_excluded == 1 and excluded.k == 1 and excluded.status == "dispersed"


def test_secondary_summary_accepted_system_and_wide():
    pipeline = synthetic_pipeline()
    summary = summarize_reference_materials_by_system(pipeline.run_finalize().parallel_results,
        standard_archive, ("S513", "91500", "NIST612"))
    assert len(summary) == 8
    wide = secondary_summary_wide(summary).set_index("sample")
    assert wide.loc["91500", "68_accepted_date"] != pytest.approx(wide.loc["91500", "76_accepted_date"])
    row = summary.query("sample == 'S513' and system == '208Pb/232Th'").iloc[0]
    expected = np.log1p(standard_archive.loc["S513", "208Pb/232Th*"]) / lambda_232 / 1e6
    assert row.accepted_date == pytest.approx(expected)
    assert row.offset_ma == pytest.approx(559 - expected)
    assert row.offset_percent == pytest.approx(100 * (559 - expected) / expected)
    empty = summarize_reference_materials_by_system(pd.DataFrame(), pd.DataFrame(), ())
    assert empty.empty and "system" in empty
    assert secondary_summary_wide(empty).empty


def test_qtegra_pipeline_reuses_cached_stages():
    from UPbRPy.io import read_experiment_data
    from UPbRPy.pipeline import FitSettings
    fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
    experiment, channels, data = read_experiment_data(fixture)
    data["202Hg"] = 1.
    config = PipelineConfig(primary_68_standard="S513", primary_76_standard="S513",
        primary_82_standard="S513", concentration_standards=("NIST612", "NIST610"),
        session=FitSettings(method="mean"))
    pipeline = ReductionPipeline(experiment, data, [*channels, "202Hg"], standard_archive, config,
        build_diagnostic_plots=False)
    pipeline.run_finalize()
    cached = dict(pipeline.cache.as_mapping())
    table = build_export_table(pipeline)
    assert len(table.rows) == len(experiment)
    assert all(pipeline.cache.get(stage) is value for stage, value in cached.items())
    assert np.isfinite(table.rows.loc[table.rows.role.ne("glass"), "206Pb/238U_age_2e_sys"]).all()
    assert len(pipeline.run_finalize().reference_material_summary_by_system) == 4


@pytest.mark.parametrize("method", ("207", "208", "204"))
def test_common_corrected_and_preferred_systematics(method):
    pipeline = synthetic_pipeline()
    frame = pipeline.run_finalize().parallel_results
    if method in {"207", "208"}:
        base = method + "Pb_corrected_age"
        label = method + "Pb-corrected"
    else:
        base = "204Pb_corrected_206Pb/238U_age"
        label = "204Pb-corrected 206Pb/238U"
    frame[base] = 560.
    frame[base + "_2e_no_calibration"] = 3.
    frame[base + "_2e_with_calibration"] = 5.
    frame["207Pb_correction_common_207Pb/206Pb"] = .86
    frame["208Pb_correction_common_206Pb/208Pb"] = .48
    frame["preferred_date_method"] = label
    frame["preferred_date"] = 560.
    result = build_export_table(pipeline)
    row = result.rows.iloc[0]
    assert row.preferred_date_2e == 3.
    assert row.preferred_date_2e_sys >= 5.
    if method != "204":
        assert row[base + "_2e_sys"] == row.preferred_date_2e_sys
    summary = result.sample_summaries.set_index("sample").loc["A"]
    assert summary.status == "ok"
    assert summary["2s_sys"]**2 - summary["2s"]**2 == pytest.approx(row.preferred_date_2e_sys**2 - 9.)


def test_material_filter_and_zero_denominators():
    from UPbRPy.export_table import _trace_quantities
    pipeline = synthetic_pipeline()
    pipeline.config = replace(pipeline.config, material="apatite", primary_channel="29Si")
    table = build_export_table(pipeline)
    assert "Ti_in_zircon_C" not in {c.key for c in table.visible_columns()}
    result = _trace_quantities(pd.DataFrame([dict(Nb=2., U=0., Ti=-1.)]), ExportSettings(), pipeline.config.uncertainty)
    assert np.isnan(result["Nb/U"].iloc[0])
    assert np.isnan(result.Ti_in_zircon_C.iloc[0])


def test_multiple_glasses_use_same_through_origin_spot_weighting():
    pipeline = synthetic_pipeline()
    # Add a second glass acquisition at a different intensity and Pb value.
    archive = standard_archive.copy()
    archive.loc["Glass2"] = archive.loc["NIST612"]
    archive.loc["Glass2", "208Pb"] *= 2
    pipeline.standard_archive = archive
    pipeline.config = replace(pipeline.config, concentration_standards=("NIST612", "Glass2"))
    extra = pipeline.total_data.loc[pipeline.total_data.spot_name.eq("NIST612_5")].copy()
    extra["spot_name"], extra["sample"], extra["Time"] = "Glass2_8", "Glass2", extra.Time + 30
    for channel in ("204Hg", "206Pb", "207Pb", "208Pb"):
        baseline = 2. if channel == "204Hg" else 1.
        extra.loc[extra.Time.ge(82), channel] = (extra.loc[extra.Time.ge(82), channel] - baseline) * 2 + baseline
    # Hg must keep its original contribution while Pb doubles.
    extra.loc[extra.Time.ge(82), "204Hg"] -= 2.
    pipeline.total_data = pd.concat([pipeline.total_data, extra], ignore_index=True)
    select = pipeline.experiment.iloc[[5]].copy()
    select["spot_names"], select["sample"] = "Glass2_8", "Glass2"
    for col in ("spot_starting_times", "baseline_start_times", "baseline_end_times", "plateau_start_times", "plateau_end_times"):
        select[col] += 30
    pipeline.experiment = pd.concat([pipeline.experiment, select], ignore_index=True)
    table = build_export_table(pipeline)
    row = table.rows.set_index("spot_name").loc["A_2"]
    assert row.Pb == pytest.approx(row.Pb_208)


def test_export_settings_in_project_and_manifest(tmp_path):
    from UPbRPy.pipeline import pipeline_config_from_dict
    from UPbRPy.project import build_reduction_project
    pipeline = synthetic_pipeline()
    pipeline.source_directory = tmp_path
    pipeline.config = replace(pipeline.config, export_settings=ExportSettings(a_tio2=.8, operator="Person"))
    project = build_reduction_project(pipeline, tmp_path / "test.upbr")
    restored = pipeline_config_from_dict(json.loads(json.dumps(project))["pipeline"]["config"])
    assert restored.export_settings == pipeline.config.export_settings
    assert pipeline.manifest()["config"]["export_settings"]["a_tio2"] == .8
    table = build_export_table(pipeline)
    assert table.settings == restored.export_settings
    assert table.context["manifest"]["metadata"]["export_settings"]["operator"] == "Person"


def test_without_concentration_standards_preserves_existing_th_u():
    pipeline = synthetic_pipeline()
    pipeline.config = replace(pipeline.config, concentration_standards=())
    frame = pipeline.run_finalize().parallel_results
    frame["Th/U"], frame["Pb"] = .3, 5.
    table = build_export_table(pipeline)
    assert (table.rows["Th/U"] == .3).all()
    assert table.rows.Pb.isna().all()
    assert (table.rows.Pb_208 == 5.).all()


def test_complete_note_vocabulary_and_long_term_term():
    pipeline = synthetic_pipeline()
    frame = pipeline.run_finalize().parallel_results
    for flag in ("detectable_204pb", "reverse_fractionation", "high_common_pb", "burn_through",
                 "high_ratio_scatter", "u_mean_intercept_mismatch", "high_uncertainty", "session_extrapolation",
                 "discordant"):
        frame["flag_" + flag] = True
    original = build_export_table(pipeline, excluded_spots=("B_0",))
    assert original.rows.iloc[0].notes == (
        "excluded, 204Pb detected, reverse fractionation, high common Pb, burn-through, "
        "high ratio scatter, U mean/intercept mismatch, high uncertainty, session extrapolation")
    pipeline.config = replace(pipeline.config, uncertainty=replace(pipeline.config.uncertainty,
        long_term_reproducibility_mode="user", long_term_reproducibility_relative_1s=.02))
    external = build_export_table(pipeline)
    base = "206Pb/238U_age_2e_sys"
    assert external.rows.iloc[0][base]**2 - original.rows.iloc[0][base]**2 == pytest.approx(
        4 * relative_ratio_date_component(560., "206Pb/238U", .02)**2)


def test_supervisor_corrections_glass_mixed_methods_and_unmeasured_standards(tmp_path):
    """Audit fixes of 2026-11-06 found on the DemoSession session."""
    from UPbRPy.export_table import write_export_xlsx

    pipeline = synthetic_pipeline()
    pipeline.config = replace(pipeline.config, secondary_standards=("91500", "FC1"))
    frame = pipeline.run_finalize().parallel_results
    frame["flag_reverse_fractionation"] = True
    frame.loc[frame["sample"].eq("B"), "preferred_date_method"] = ["206Pb/238U", "207Pb/206Pb"]
    table = build_export_table(pipeline, excluded_spots=("NIST612_5",))
    rows = table.rows.set_index("spot_name")
    # Studio hides glasses from the concordia by default; that is not an exclusion,
    # and U-Pb screening flags do not apply to a glass.
    assert not rows.loc["NIST612_5", "excluded"]
    assert rows.loc["NIST612_5", "notes"] == "concentration reference glass"
    summaries = table.sample_summaries.set_index("sample")
    assert summaries.loc["NIST612", "status"] == "glass"
    text = summaries.loc["B", "summary_text"]
    assert "mixed methods (206Pb/238U 1, 207Pb/206Pb 1)" in text and "MSWD" not in text
    # FC1 is configured but was not analysed: it must not be cited.
    assert table.context["secondary_standards"] == ("91500",)
    workbook = write_export_xlsx(table, tmp_path / "t.xlsx")
    import zipfile
    with zipfile.ZipFile(workbook) as archive:
        strings = archive.read("xl/sharedStrings.xml").decode()
    assert "Table &lt;n&gt;" not in strings and "Weighted mean (Ma)" in strings
