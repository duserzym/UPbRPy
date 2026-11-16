"""Tests for the normalized, source-traceable reference-material archive."""

from __future__ import annotations

from pathlib import Path
import shutil

import numpy as np
import pandas as pd
import pytest

import UPbRPy
from UPbRPy.constants import common_U238_U235, lambda_235, lambda_238
from UPbRPy.standards import (
    REQUIRED_COLUMNS,
    build_standard_archive,
    canonicalize_reference_material_samples,
    load_standard_measurements,
    standard_archive,
    standard_archive_v2,
    standard_measurements,
    standard_sources,
)


def test_reference_material_matching_ignores_case_and_punctuation():
    archive = pd.DataFrame(index=["TR1", "NIST610"])
    data = pd.DataFrame(
        {"sample": ["TR-1", "nist_610", "Unknown-1"], "value": [1, 2, 3]}
    )

    canonical = canonicalize_reference_material_samples(data, archive)

    assert canonical["sample"].tolist() == ["TR1", "NIST610", "Unknown-1"]
    assert canonical.attrs["reference_material_sample_aliases"] == {
        "TR-1": "TR1",
        "nist_610": "NIST610",
    }


def test_reference_alias_matches_krag_and_ignores_ambiguous_names():
    data = pd.DataFrame({"sample": ["Krag", "KNW", "Unknown"]})
    assert canonicalize_reference_material_samples(data, standard_archive)["sample"].tolist() == [
        "KNW", "KNW", "Unknown",
    ]
    archive = pd.DataFrame(index=["A", "B"])
    archive.attrs["standard_aliases"] = {"A": ("shared",), "B": ("shared", "A")}
    assert canonicalize_reference_material_samples(
        pd.DataFrame({"sample": ["shared", "A"]}), archive,
    )["sample"].tolist() == ["shared", "A"]


def test_normalized_archive_has_unique_documented_values_and_provenance():
    assert tuple(standard_measurements.columns) == REQUIRED_COLUMNS
    assert not standard_measurements.duplicated(["standard", "quantity"]).any()
    assert standard_measurements["source"].str.strip().ne("").all()
    assert standard_measurements["unit"].str.strip().ne("").all()
    assert set(standard_measurements["material"]) == {
        "apatite",
        "carbonate",
        "glass",
        "hematite",
        "rutile",
        "titanite",
        "zircon",
    }


def test_packaged_archive_can_be_reloaded_and_is_in_public_api():
    pd.testing.assert_frame_equal(load_standard_measurements(), standard_measurements)
    assert UPbRPy.standard_measurements is standard_measurements
    assert UPbRPy.standard_archive is standard_archive
    assert UPbRPy.standard_archive_v2 is standard_archive_v2
    assert UPbRPy.standard_archive_v2 is UPbRPy.standard_archive
    assert not hasattr(UPbRPy, "standard_archive_legacy")
    assert not hasattr(UPbRPy, "standard_archive_csv")
    assert UPbRPy.standard_sources is standard_sources


def test_primary_archive_attrs_are_scalar_and_safe_for_dataframe_operations():
    assert standard_archive.attrs["source_library"] == "TOML"
    assert standard_archive.attrs["measurement_count"] == len(
        standard_measurements
    )
    subset = standard_archive.loc[["NIST610", "NIST612"]]
    assert "NIST610" in repr(subset)
    combined = pd.concat([subset.iloc[:1], subset.iloc[1:]])
    assert combined.index.tolist() == ["NIST610", "NIST612"]


def test_toml_source_registry_is_resolved_and_preserved_as_metadata():
    assert standard_sources["meng2024"]["doi"] == "10.1039/D4JA00190G"
    assert standard_sources["axelsson2018"]["doi"] == "10.1111/ggr.12213"
    assert standard_measurements.attrs["sources"] == standard_sources
    assert not standard_measurements["notes"].str.contains(
        "{quantity}", regex=False
    ).any()
    assert not any(
        source.get("kind") == "reduction_workbook"
        for source in standard_sources.values()
    )
    assert standard_measurements["source_locator"].ne("").any()


def test_packaged_toml_contains_publication_provenance_not_excel_locations():
    source_root = Path(__file__).parents[1] / "UPbRPy" / "reference_materials"
    library_text = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.rglob("*.toml")
        if path.name != "sources.toml"
    )
    for legacy_marker in ("Instructions!", ".xlsm", "reduction_workbook"):
        assert legacy_marker not in library_text
    assert 'locator = "Abstract, p. 403"' in library_text
    # Lab compilations may name their workbook in the shared source registry.
    for source in standard_sources.values():
        if ".xlsm" in str(source):
            assert source["kind"] == "lab_compilation"
            assert source["status"] == "provisional"


def test_former_long_csv_layout_remains_an_explicit_migration_input(tmp_path):
    csv_path = tmp_path / "measurements.csv"
    standard_measurements.to_csv(csv_path, index=False)
    reloaded = load_standard_measurements(csv_path)
    pd.testing.assert_frame_equal(
        reloaded,
        standard_measurements,
        check_like=True,
        check_dtype=False,
        check_exact=False,
        rtol=1e-14,
    )


def test_toml_loader_rejects_an_unknown_source_id(tmp_path):
    source_root = Path(__file__).parents[1] / "UPbRPy" / "reference_materials"
    destination = tmp_path / "reference_materials"
    shutil.copytree(source_root, destination)

    rutile_path = destination / "rutile" / "R632.toml"
    rutile_text = rutile_path.read_text(encoding="utf-8")
    rutile_path.write_text(
        rutile_text.replace(
            'source = "axelsson2018"',
            'source = "missing_source"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown source"):
        load_standard_measurements(destination)


def test_canonical_initial_ratios_generate_reciprocals_and_cross_ratios():
    row = standard_archive.loc["91500"]
    assert np.isclose(row["204Pb/206Pb_i"], 1 / row["206Pb/204Pb_i"])
    assert np.isclose(row["207Pb/206Pb_i"], 1 / row["206Pb/207Pb_i"])
    assert np.isclose(
        row["206Pb/207Pb_i"],
        row["206Pb/204Pb_i"] / row["207Pb/204Pb_i"],
    )
    assert np.isclose(
        row["206Pb/208Pb_i"],
        row["206Pb/204Pb_i"] / row["208Pb/204Pb_i"],
    )


def test_documented_ratios_take_precedence_over_age_derived_values():
    row = standard_archive.loc["91500"]
    assert row["206Pb/238U*"] == 0.17917
    assert row["207Pb/235U*"] == 1.8502
    assert row["208Pb/232Th*"] == 0.05374
    # The upstream 207Pb/206Pb age takes precedence over the linked U-Pb ratios.
    assert row["207Pb/206Pb*"] == 0.07491807081490459

    # PL reports a direct 207Pb/206Pb value whose published precision is not
    # exactly reproduced from the rounded companion ratios.
    assert standard_archive.loc["PL", "207Pb/206Pb*"] == 0.053236797


def test_independent_knw_system_ages_derive_their_own_ratios():
    table3_ratios = (standard_measurements["standard"] == "KNW") & (
        standard_measurements["role"] == "radiogenic_ratio"
    )
    row = build_standard_archive(standard_measurements.loc[~table3_ratios]).loc["KNW"]
    expected_68 = np.expm1(lambda_238 * 1088.2e6)
    expected_75 = np.expm1(lambda_235 * 1086.6e6)
    expected_76 = (
        np.expm1(lambda_235 * 1083.1e6)
        / np.expm1(lambda_238 * 1083.1e6)
        / common_U238_U235
    )
    assert np.isclose(row["206Pb/238U*"], expected_68)
    assert np.isclose(row["207Pb/235U*"], expected_75)
    assert np.isclose(row["207Pb/206Pb*"], expected_76)


def test_knw_table3_ratios_have_provenance_and_take_precedence():
    knw = standard_measurements.query("standard == 'KNW' and role == 'radiogenic_ratio'").set_index("quantity")
    assert knw["notes"].str.startswith("Table 3, KNW1–KNW7.").all()
    assert knw["status"].eq("provisional").all()
    assert knw["uncertainty_type"].eq("2sd").all()
    assert knw["value"].to_dict() == {"206Pb/238U*": 0.183870, "207Pb/235U*": 1.91596, "207Pb/206Pb*": 0.075539}
    for ratio, value in knw["value"].items():
        assert standard_archive.loc["KNW", ratio] == value
    assert not standard_measurements.query("standard == 'KNW' and role == 'initial_ratio'").size


def test_rutile_literature_values_include_uncertainty_semantics():
    knw_zr = standard_measurements.query(
        "standard == 'KNW' and quantity == '91Zr'"
    ).iloc[0]
    assert knw_zr["value"] == 1183
    assert knw_zr["uncertainty"] == 198
    assert knw_zr["uncertainty_type"] == "expanded_95"

    r632_zr = standard_measurements.query(
        "standard == 'R632' and quantity == '91Zr'"
    ).iloc[0]
    assert r632_zr["value"] == 4294
    assert r632_zr["uncertainty"] == 196
    assert r632_zr["uncertainty_type"] == "1sd"


def test_apatite_id_tims_values_come_from_the_cited_publications():
    expected = {
        "AFB1": (473.25, 0.09, "expanded_95"),
        "BR5": (2040.34, 0.09, "expanded_95"),
        "OL2": (868.87, 0.25, "expanded_95"),
        "MADUCSB": (467.4, 8.4, "2sd"),
    }
    for standard, (age, uncertainty, convention) in expected.items():
        row = standard_measurements.query(
            "standard == @standard and quantity == 'concordia_age'"
        ).iloc[0]
        assert row["value"] == age
        assert row["uncertainty"] == uncertainty
        assert row["uncertainty_type"] == convention


def test_wide_archive_covers_concentration_and_ratio_calibration_interfaces():
    for standard in ("NIST610", "NIST612"):
        assert np.isfinite(standard_archive.loc[standard, "29Si"])
        assert np.isfinite(standard_archive.loc[standard, "238U"])

    for standard in ("91500", "AFB1", "R632", "KNW"):
        assert np.isfinite(standard_archive.loc[standard, "206Pb/238U*"])
        assert np.isfinite(standard_archive.loc[standard, "207Pb/206Pb*"])
        assert np.isfinite(standard_archive.loc[standard, "208Pb/232Th*"])


def test_builder_can_return_only_documented_values():
    documented = build_standard_archive(include_derived=False)
    assert documented.loc["91500", "206Pb/238U*"] == 0.17917
    assert np.isnan(documented.loc["91500", "238U/206Pb*"])
    assert np.isnan(documented.loc["R632", "206Pb/238U*"])


def test_iolite_imports_remain_distinct_from_curated_records():
    iolite = standard_measurements[
        standard_measurements["standard"].str.startswith("IOLITE_")
    ]
    assert iolite["standard"].nunique() == 24
    assert set(iolite["status"]) == {"provisional"}
    assert {"91500", "IOLITE_Z_91500"}.issubset(
        set(standard_measurements["standard"])
    )


def test_every_manifest_record_has_its_own_editable_toml_file():
    source_root = Path(__file__).parents[1] / "UPbRPy" / "reference_materials"
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - old local test environments
        import tomli as tomllib

    manifest = tomllib.loads(
        source_root.joinpath("library.toml").read_text(encoding="utf-8")
    )
    assert len(manifest["material_files"]) == 51
    actual_files = {
        path.relative_to(source_root).as_posix()
        for path in source_root.glob("*/*.toml")
    }
    assert set(manifest["material_files"]) == actual_files
    for relative_path in manifest["material_files"]:
        document = tomllib.loads(
            source_root.joinpath(relative_path).read_text(encoding="utf-8")
        )
        assert len(document["standards"]) == 1


def test_manifest_resource_paths_are_case_insensitive(tmp_path):
    source_root = Path(__file__).parents[1] / "UPbRPy" / "reference_materials"
    destination = tmp_path / "reference_materials"
    shutil.copytree(source_root, destination)

    manifest_path = destination / "library.toml"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest_text.replace(
            'sources = "sources.toml"',
            'sources = "SOURCES.TOML"',
        ).replace(
            '"apatite/AFB1.toml"',
            '"APATITE/afb1.TOML"',
        ),
        encoding="utf-8",
    )

    reloaded = load_standard_measurements(destination)
    pd.testing.assert_frame_equal(reloaded, standard_measurements)


def test_iolite_weight_percent_values_are_normalized_to_micrograms_per_gram():
    nist_611_si = standard_measurements.query(
        "standard == 'IOLITE_G_NIST611' and quantity == '29Si'"
    ).iloc[0]
    assert nist_611_si["unit"] == "ug/g"
    assert np.isclose(nist_611_si["value"], 325803.3886457068)
