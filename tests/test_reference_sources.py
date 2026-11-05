"""Audit source metadata, canonical inputs and Boise standard resolution."""

from pathlib import Path

import numpy as np
import pytest

import UPbRPy
from UPbRPy.constants import common_U238_U235
from UPbRPy.io import read_experiment_data
from UPbRPy.pipeline import PipelineConfig, ReductionPipeline
from UPbRPy.standards import check_reference_sources


def synthetic_library(tmp_path, sources, measurements):
    """Write a small independent library for source-audit failure cases."""
    (tmp_path / "library.toml").write_text(
        'format_version = 2\nsources = "sources.toml"\n'
        'material_files = ["material.toml"]\n', encoding="utf-8",
    )
    (tmp_path / "sources.toml").write_text(
        "format_version = 2\n" + sources, encoding="utf-8",
    )
    (tmp_path / "material.toml").write_text(
        'format_version = 2\nmaterial = "zircon"\n'
        '[standards.Example]\naliases = []\n' + measurements, encoding="utf-8",
    )
    return tmp_path


MEASUREMENT = '''
[[standards.Example.measurements]]
role = "concentration"
unit = "ug/g"
status = "provisional"
source = "example"
[standards.Example.measurements.values]
"29Si" = 1.0
'''


def test_every_registered_measurement_has_a_source_and_every_source_has_metadata():
    # This traverses all registered files and checks value-level source overrides.
    # Missing DOI documentation warns without failing this test.
    check_reference_sources()
    assert UPbRPy.check_reference_sources is check_reference_sources


def test_audit_warns_and_returns_all_sources_without_doi_metadata(tmp_path):
    root = synthetic_library(tmp_path, '''
[sources.example]
display = "Example"
kind = "journal_article"
[sources.other]
display = "Other"
kind = "book"
''', MEASUREMENT)
    with pytest.warns(UserWarning, match="example, other"):
        assert check_reference_sources(root / "library.toml") == ["example", "other"]
    # An incomplete DOI audit must not prevent loading an otherwise valid library.
    assert len(UPbRPy.load_standard_measurements(root)) == 1


def test_audit_lists_all_unknown_sources_including_value_overrides(tmp_path):
    root = synthetic_library(tmp_path, '''
[sources.example]
display = "Example"
kind = "book"
no_doi_reason = "Unpublished example."
''', MEASUREMENT.replace('source = "example"', 'source = "missing_group"').replace(
        '"29Si" = 1.0',
        '"29Si" = 1.0\n"91Zr" = { value = 2.0, source = "missing_value" }',
    ))
    with pytest.raises(ValueError) as error:
        check_reference_sources(root)
    for offender in ("missing_group", "missing_value", "Example/29Si", "Example/91Zr"):
        assert offender in str(error.value)


@pytest.mark.parametrize("metadata, message", [
    ('kind = "book"', "requires a display"),
    ('display = "Example"', "requires a kind"),
    ('display = "Example"\nkind = " "', "requires a kind"),
    ('display = "Example"\nkind = 12', "requires a kind"),
    ('display = "Example"\nkind = "book"\nno_doi_reason = 12', "no_doi_reason"),
    ('display = "Example"\nkind = "book"\nstatus = ["provisional"]', "status"),
])
def test_source_metadata_is_validated_by_audit_and_loader(tmp_path, metadata, message):
    root = synthetic_library(tmp_path, "[sources.example]\n" + metadata, MEASUREMENT)
    for reader in (check_reference_sources, UPbRPy.load_standard_measurements):
        with pytest.raises(ValueError, match=message):
            reader(root)


EXPECTED = {
    "S513": {
        "29Si": 151443.22960004435,
        "91Zr": 487760.24454904505,
        "177Hf": 8259.0,
        "206Pb/204Pb_i": 17.81427985313352,
        "207Pb/204Pb_i": 15.576740129737942,
        "208Pb/204Pb_i": 37.59322277150352,
        "206Pb/238U*": 0.090955,
        "207Pb/235U*": 0.73873,
        "208Pb/232Th*": 0.028153518284538936,
        "206Pb/238U_age": 561.18,
    },
    "LTSE": {
        "29Si": 151443.22960004435,
        "91Zr": 487760.24454904505,
        "206Pb/204Pb_i": 18.076074416,
        "207Pb/204Pb_i": 18.076074416 / 1.1606485235997472,
        "208Pb/204Pb_i": 18.076074416 / 0.47745654851370506,
        "206Pb/238U_age": 560.83,
        "concordia_age": 560.83,
    },
}


@pytest.mark.parametrize("standard", EXPECTED)
def test_new_materials_load_exact_inputs_and_derive_redundant_ratios(standard):
    measurements = UPbRPy.load_standard_measurements()
    records = measurements.loc[measurements["standard"].eq(standard)]
    assert records.set_index("quantity")["value"].to_dict() == EXPECTED[standard]
    assert set(records["aliases"]) == ({"SLZC"} if standard == "LTSE" else {""})
    published_age = standard == "LTSE"
    by_quantity = records.set_index("quantity")
    assert set(by_quantity.drop(index="206Pb/238U_age", errors="ignore")["status"]) == {"provisional"}
    assert by_quantity.loc["206Pb/238U_age", "status"] == ("recommended" if published_age else "provisional")
    assert set(records["material"]) == {"zircon"}
    archive = UPbRPy.build_standard_archive(measurements)
    documented = UPbRPy.build_standard_archive(measurements, include_derived=False)
    row = archive.loc[standard]
    expected_derived = {
        "207Pb/206Pb*": row["207Pb/235U*"] / row["206Pb/238U*"] / common_U238_U235,
        "206Pb/207Pb*": row["206Pb/238U*"] * common_U238_U235 / row["207Pb/235U*"],
        "238U/206Pb*": 1 / row["206Pb/238U*"],
        "204Pb/206Pb_i": 1 / row["206Pb/204Pb_i"],
        "204Pb/207Pb_i": 1 / row["207Pb/204Pb_i"],
        "204Pb/208Pb_i": 1 / row["208Pb/204Pb_i"],
        "206Pb/207Pb_i": row["206Pb/204Pb_i"] / row["207Pb/204Pb_i"],
        "207Pb/206Pb_i": row["207Pb/204Pb_i"] / row["206Pb/204Pb_i"],
        "206Pb/208Pb_i": row["206Pb/204Pb_i"] / row["208Pb/204Pb_i"],
    }
    for quantity, value in expected_derived.items():
        assert quantity not in set(records["quantity"])
        assert np.isnan(documented.loc[standard].get(quantity, np.nan))
        assert row[quantity] == pytest.approx(value)


def test_new_material_provenance_separates_published_and_archive_values():
    measurements = UPbRPy.load_standard_measurements()
    sources = measurements.attrs["sources"]
    s513 = measurements.loc[measurements["standard"].eq("S513")].set_index("quantity")
    for quantity in ("177Hf", "206Pb/238U*", "207Pb/235U*", "206Pb/238U_age"):
        assert s513.loc[quantity, "source"] == sources["luo2025"]["display"]
    assert s513.loc["208Pb/232Th*", "source"] == sources["boise_upbr26plus_2026"]["display"]
    assert s513.loc["206Pb/238U_age", "uncertainty"] == 0.63
    assert s513.loc["206Pb/238U_age", "uncertainty_type"] == "expanded_95"
    for quantity in ("206Pb/204Pb_i", "207Pb/204Pb_i", "208Pb/204Pb_i"):
        assert s513.loc[quantity, "source"] == sources["stacey1975"]["display"]
        assert "rows labelled 206Pb/207Pb and 206Pb/208Pb" in s513.loc[quantity, "notes"]
    for standard in EXPECTED:
        rows = measurements.loc[measurements["standard"].eq(standard)]
        for quantity in ("29Si", "91Zr"):
            assert rows.set_index("quantity").loc[quantity, "source"] == sources["deer2013"]["display"]
    ltse = measurements.loc[measurements["standard"].eq("LTSE")]
    assert ltse["notes"].str.contains("SLZC", regex=False).all()
    age = ltse.set_index("quantity").loc["206Pb/238U_age"]
    assert age["source"] == sources["hu2023"]["display"]
    assert (age["value"], age["uncertainty"], age["uncertainty_type"]) == (560.83, 1.29, "expanded_95")
    assert set(ltse.loc[ltse["role"].eq("reference_age"), "source"]) == {sources["hu2023"]["display"]}
    # Radiogenic ratios are derived as concordant values from the published age.
    assert not ltse["role"].eq("radiogenic_ratio").any()
    row = UPbRPy.build_standard_archive(measurements).loc["LTSE"]
    assert row["206Pb/238U*"] == pytest.approx(np.expm1(1.55125e-10 * 560.83e6), rel=1e-6)
    assert row["208Pb/232Th*"] == pytest.approx(np.expm1(4.9475e-11 * 560.83e6), rel=1e-3)
    archive_values = ltse.loc[ltse["role"].eq("initial_ratio")]
    assert set(archive_values["source"]) == {sources["boise_upbr26plus_2026"]["display"]}
    assert set(archive_values["status"]) == {"provisional"}
    assert sources["boise_upbr26plus_2026"]["kind"] == "lab_compilation"
    assert sources["boise_upbr26plus_2026"]["status"] == "provisional"


def test_qtegra_fixture_selects_s513_and_resolves_its_library_ratios():
    fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
    experiment, channels, data = read_experiment_data(fixture)
    config = PipelineConfig(
        primary_68_standard="S513", primary_76_standard="S513",
        primary_82_standard="S513", concentration_standards=("NIST612", "NIST610"),
    )
    pipeline = ReductionPipeline(
        experiment, data, channels, UPbRPy.build_standard_archive(), config,
        build_diagnostic_plots=False,
    )
    primary = pipeline.standard_rows(data, config.primary_standard, "primary standard")
    assert set(primary["sample"]) == {"S513"}
    assert "S513_628" in set(primary["spot_name"])
    assert pipeline.standard_archive.loc[config.primary_standard, "206Pb/238U*"] == 0.090955
    for ratio in config.ratio_names:
        assert np.isfinite(pipeline.standard_archive.loc[config.primary_standard, ratio + "*"])
