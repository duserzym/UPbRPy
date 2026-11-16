"""Per-acquisition manifest samples survive import, reduction and export."""

import json

import pandas as pd
import pytest

from UPbRPy import standard_archive
from UPbRPy.io import experiment_from_session, read_experiment_data
from UPbRPy.pipeline import FitSettings, PipelineConfig, ReductionPipeline
from UPbRPy.project import restore_project_selections
from UPbRPy.qtegra import open_qtegra
from UPbRPy.reproducibility import export_reduction_bundle, selection_export_frame
from UPbRPy.session_manifest import read_session_manifest

import synthetic_bundles
from synthetic_bundles import manifest_toml, write_raw_directory, write_session_bundle


OVERRIDES = {
    "KD27_4": {"label_sample": "KD27", "sample": "SMP2", "source": "session_manifest"}
}
SAMPLE_LABELS = {"KD27_4": "SMP2", "AFB_3": "AFB", "NIST612_1": "nist612"}
EXPECTED = {"KD27_4": "SMP2", "AFB_3": "AFB1", "NIST612_1": "NIST612", "MAD_UCSB_2": "MADUCSB"}


def sample_map(experiment):
    return experiment.set_index("spot_names")["sample"].to_dict()


@pytest.mark.parametrize("given, aliases, chosen, overridden", [
    (None, {}, "SMP1", False),
    ("", {}, "SMP1", False),
    ("smp1", {}, "SMP1", False),
    ("SMP2", {}, "SMP2", True),
    ("smp1", {"SMP1": "Original"}, "Original", False),
    ("oRIGINAL", {"smp1": "Original"}, "Original", False),
    ("other", {"SMP1": "Original", "OTHER": "Original"}, "Original", False),
    ("smp2", {"SMP1": "Original", "SMP2": "Corrected"}, "Corrected", True),
])
def test_sample_override_rule(tmp_path, given, aliases, chosen, overridden):
    labels = ("SMP1_S_91", "SMP1_S_108")
    write_raw_directory(tmp_path, labels=labels)
    session = open_qtegra(tmp_path)
    old_experiment, _, old_total = experiment_from_session(session, sample_aliases=aliases)
    mapping = None if given is None else {labels[1]: given, "absent_label": "Ignored"}
    experiment, _, total = experiment_from_session(
        session, sample_aliases=aliases, acquisition_samples=mapping,
    )
    derived = sample_map(old_experiment)[labels[0]]
    assert sample_map(experiment) == {labels[0]: derived, labels[1]: chosen}
    assert total.groupby("spot_name")["sample"].unique().map(list).to_dict() == {
        labels[0]: [derived], labels[1]: [chosen],
    }
    assert list(experiment) == list(old_experiment)
    assert list(total) == list(old_total)
    assert experiment.attrs["sample_overrides"] == ({
        labels[1]: {"label_sample": derived, "sample": chosen, "source": "session_manifest"}
    } if overridden else {})
    assert json.loads(json.dumps(experiment.attrs["sample_overrides"])) == experiment.attrs["sample_overrides"]
    if not overridden:
        pd.testing.assert_frame_equal(experiment, old_experiment, check_exact=True)
        pd.testing.assert_frame_equal(total, old_total, check_exact=True)


@pytest.mark.parametrize("source_kind", ["bundle", "raw", "file", "memory", "explicit"])
def test_manifest_samples_reach_reader_and_preserve_selection_attrs(tmp_path, source_kind):
    bundle = write_session_bundle(tmp_path, sample_labels=SAMPLE_LABELS)
    manifest = read_session_manifest(bundle / "session_manifest.toml")
    assert manifest.acquisition_samples() == SAMPLE_LABELS
    assert manifest.acquisition("MAD_UCSB_2").sample_label == ""
    source = {
        "bundle": bundle,
        "raw": bundle / "raw",
        "file": bundle / "raw" / "Synthetic_4.csv",
        "memory": open_qtegra(bundle / "raw"),
        "explicit": open_qtegra(bundle / "raw"),
    }[source_kind]
    options = {"session_manifest": manifest} if source_kind == "explicit" else {}
    experiment, _, total = read_experiment_data(source, primary_channel="44Ca", **options)
    expected = {"KD27_4": "SMP2"} if source_kind == "file" else EXPECTED
    overrides = OVERRIDES
    assert sample_map(experiment) == expected
    assert total.groupby("spot_name")["sample"].unique().map(list).to_dict() == {
        label: [sample] for label, sample in expected.items()
    }
    assert experiment.attrs["sample_overrides"] == overrides
    assert experiment["selection_error"].isna().all()
    restored = restore_project_selections(experiment, selection_export_frame(experiment).to_dict("records"))
    assert restored.attrs["sample_overrides"] == overrides


def test_old_manifest_and_disabled_manifest_keep_previous_samples(tmp_path):
    bundle = write_session_bundle(tmp_path)
    manifest_path = bundle / "session_manifest.toml"
    assert "sample_label" not in manifest_path.read_text()
    manifest = read_session_manifest(manifest_path)
    assert manifest.acquisition_samples() == {}
    assert all(row.sample_label == "" for row in manifest.acquisitions)
    kwargs = dict(session_id="id", session_key="key", raw_paths=list((bundle / "raw").glob("*.csv")))
    assert manifest_toml(**kwargs) == manifest_toml(**kwargs, sample_labels={})
    experiment, _, _ = read_experiment_data(bundle, primary_channel="44Ca")
    assert sample_map(experiment) == {**EXPECTED, "KD27_4": "KD27"}
    assert experiment.attrs["sample_overrides"] == {}

    corrected = write_session_bundle(tmp_path / "corrected", sample_labels=SAMPLE_LABELS)
    plain, _, _ = read_experiment_data(corrected / "raw", primary_channel="44Ca", session_manifest=False)
    assert sample_map(plain) == {"KD27_4": "KD27", "AFB_3": "AFB", "NIST612_1": "NIST612", "MAD_UCSB_2": "MAD"}
    assert plain.attrs["sample_overrides"] == {}


def test_reduction_bundle_publishes_corrected_samples_and_provenance(tmp_path, monkeypatch):
    # Supply the isotope channels required for a complete synthetic reduction.
    monkeypatch.setattr(synthetic_bundles, "CHANNELS", (
        "29Si", "44Ca", "202Hg", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U",
    ))
    bundle = write_session_bundle(tmp_path, sample_labels=SAMPLE_LABELS)
    experiment, channels, total = read_experiment_data(bundle, primary_channel="44Ca")
    pipeline = ReductionPipeline(
        experiment, total, channels, standard_archive,
        PipelineConfig(
            material="apatite", primary_channel="44Ca", primary_68_standard="MADUCSB",
            concentration_standards=("NIST612",), session=FitSettings(method="mean"),
        ),
        source_directory=bundle, build_diagnostic_plots=False,
    )
    assert pipeline.experiment.attrs["sample_overrides"] == OVERRIDES
    pipeline.update_experiment(experiment.copy())
    assert pipeline.manifest()["sample_overrides"] == OVERRIDES
    means = pipeline.run_spot_means().set_index("spot_name")
    assert means.loc["KD27_4", "sample"] == "SMP2"
    exported = export_reduction_bundle(pipeline, tmp_path / "export", include_depth_profile=False)
    assert not exported.warnings
    manifest = json.loads((exported.directory / "reduction_manifest.json").read_text())
    assert manifest["sample_overrides"] == OVERRIDES
    results = pd.read_csv(exported.directory / "spot_mean_results.csv").set_index("spot_name")
    assert results.loc["KD27_4", "sample"] == "SMP2"
    published = pd.read_csv(exported.directory / "UPbRPy_publication_results" / "results.csv")
    assert published.set_index("spot_name").loc["KD27_4", "sample"] == "SMP2"


def test_repeated_label_keeps_first_acquisition(tmp_path):
    """Six blanks all named ``nothing`` in one Rich export: first one wins, names stay unique."""
    labels = ("SMP1_S_91", "SMP1_S_108", "SMP1_S_120")
    write_raw_directory(tmp_path, labels=labels)
    session = open_qtegra(tmp_path)
    first = session.acquisitions[0]
    session.acquisitions[2].label = first.label
    experiment, _, total = experiment_from_session(session)
    assert sorted(experiment["spot_names"]) == sorted(labels[:2])
    assert not experiment["spot_names"].duplicated().any()
    assert len(total[total["spot_name"] == first.label]) == first.n_sweeps
    assert any("repeated label" in w for w in experiment.attrs["qtegra"]["warnings"])
    restore_project_selections(experiment, selection_export_frame(experiment).to_dict("records"))
