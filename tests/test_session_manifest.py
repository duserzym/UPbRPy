"""Session bundles: session_manifest.toml, project origin, manifest provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from UPbRPy.io import read_experiment_data
from UPbRPy.pipeline import PipelineConfig, ReductionPipeline
from UPbRPy.project import (
    PROJECT_FORMAT_VERSION,
    build_reduction_project,
    normalize_origin,
    read_reduction_project,
    write_project_document,
    write_reduction_project,
)
from UPbRPy.session_manifest import (
    SessionManifestError,
    find_session_manifest,
    parse_session_manifest,
    read_session_manifest,
)
from UPbRPy.standards import reference_library_info

from synthetic_bundles import LABELS, write_raw_directory, write_session_bundle


SESSION_ID = "0192f0c4-0000-7000-8000-000000000001"


def samples(experiment) -> dict[str, str]:
    """Spot label -> parsed sample (the reader sorts rows by spot name)."""
    return dict(zip(experiment["spot_names"], experiment["sample"]))


def expected(*values: str) -> dict[str, str]:
    return dict(zip(LABELS, values))


@pytest.fixture()
def no_autopick(monkeypatch):
    monkeypatch.setattr(
        "UPbRPy.io.auto_select_baseline_peak",
        lambda experiment, *args, **kwargs: experiment,
    )


def test_manifest_reader_parses_contract_fields(tmp_path):
    bundle = write_session_bundle(tmp_path)
    manifest = read_session_manifest(bundle / "session_manifest.toml")

    assert manifest.session_id == SESSION_ID
    assert manifest.session_key == "Synthetic_20Mar25"
    assert manifest.sep == "_"
    assert manifest.material == "apatite"
    assert manifest.sample_aliases == {"MAD": "MADUCSB", "AFB": "AFB1"}
    assert manifest.reference_materials["primary_68"] == ("MADUCSB",)
    assert manifest.standards_config() == {
        "primary_68_standard": "MADUCSB",
        "concentration_standards": ("NIST612",),
        "secondary_standards": ("AFB1",),
    }
    assert [row.label for row in manifest.acquisitions] == list(LABELS)
    first = manifest.acquisition("NIST612_1")
    assert first.acquisition_id == "acq-0"
    assert first.kind == "primary_standard"
    assert first.started_at == "2025-03-20T21:00:00"
    assert len(first.sha256) == 64
    assert manifest.raw_directory == bundle / "raw"


def test_manifest_reader_accepts_plan_rows_and_rejects_bad_tables(tmp_path):
    manifest = parse_session_manifest(
        {
            "session": {"id": "s", "key": "k"},
            "reference_materials": {
                "plan": [
                    {"rm_name": "91500", "role": "primary_68",
                     "planned_bracket_every": 10},
                    {"rm_name": "PL", "role": "validation"},
                    {"rm_name": "FC1", "role": "secondary"},
                ]
            },
        },
        tmp_path / "session_manifest.toml",
    )
    assert manifest.sep is None
    assert manifest.standards_config() == {
        "primary_68_standard": "91500",
        "secondary_standards": ("PL", "FC1"),
    }
    with pytest.raises(SessionManifestError):
        parse_session_manifest({"session": {"sep": ""}}, tmp_path / "m.toml")
    with pytest.raises(SessionManifestError):
        parse_session_manifest({"acquisition": [{"kind": "sample"}]}, tmp_path / "m.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[session\n", encoding="utf-8")
    with pytest.raises(SessionManifestError):
        read_session_manifest(bad)


def test_find_session_manifest_inside_bundle_or_beside_raw(tmp_path):
    bundle = write_session_bundle(tmp_path)
    expected = bundle / "session_manifest.toml"
    assert find_session_manifest(bundle) == expected
    assert find_session_manifest(bundle / "raw") == expected
    other = tmp_path / "loose"
    write_raw_directory(other)
    assert find_session_manifest(other) is None


def test_reader_takes_sep_and_aliases_from_manifest(tmp_path, no_autopick):
    bundle = write_session_bundle(tmp_path)

    for directory in (bundle, bundle / "raw"):
        experiment, channels, total_data = read_experiment_data(
            directory, primary_channel="44Ca"
        )
        assert samples(experiment) == expected("NIST612", "MADUCSB", "AFB1", "KD27")
        assert Path(experiment["spot_paths"].iloc[0]).parent == bundle / "raw"
        assert "44Ca" in channels

    # Explicit arguments win over the manifest.
    experiment, _, _ = read_experiment_data(
        bundle, primary_channel="44Ca", sep="-", sample_aliases={"mad_ucsb_2": "X"}
    )
    assert samples(experiment) == expected("NIST612_1", "X", "AFB_3", "KD27_4")
    experiment, _, _ = read_experiment_data(
        bundle, primary_channel="44Ca", sample_aliases={"afb": "AFBX"}
    )
    assert samples(experiment) == expected("NIST612", "MADUCSB", "AFBX", "KD27")

    # session_manifest=False restores the plain reader.
    experiment, _, _ = read_experiment_data(
        bundle / "raw", primary_channel="44Ca", session_manifest=False
    )
    assert samples(experiment) == expected("NIST612", "MAD", "AFB", "KD27")


def test_reader_without_manifest_keeps_underscore_default(tmp_path, no_autopick):
    raw = tmp_path / "plain"
    write_raw_directory(raw)
    experiment, _, _ = read_experiment_data(raw, primary_channel="44Ca")
    assert samples(experiment) == expected("NIST612", "MAD", "AFB", "KD27")


def _pipeline(source: Path, origin=None) -> ReductionPipeline:
    experiment = pd.DataFrame(
        {
            "spot_names": ["MAD_1"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [1.0],
            "plateau_start_times": [3.0],
            "plateau_end_times": [8.0],
        }
    )
    total_data = pd.DataFrame(
        {"spot_name": ["MAD_1"], "sample": ["MAD"], "Time": [0.0], "44Ca": [1.0]}
    )
    files = tuple(sorted(source.glob("*.csv")))
    return ReductionPipeline(
        experiment,
        total_data,
        ["44Ca"],
        pd.DataFrame(),
        PipelineConfig(workflow_key="apatite", material="apatite",
                       primary_channel="44Ca"),
        source_directory=source,
        input_files=files,
        origin=origin,
    )


ORIGIN = {
    "server": "https://lab.example.org/",
    "session_id": SESSION_ID,
    "session_key": "Synthetic_20Mar25",
    "export_sha256": "ab" * 32,
    "exported_at": "2026-11-03T12:00:00Z",
}


def test_project_v2_round_trips_origin(tmp_path):
    raw = tmp_path / "raw"
    write_raw_directory(raw)
    path = write_reduction_project(_pipeline(raw, ORIGIN), tmp_path / "s.upbr")
    document = read_reduction_project(path)
    assert document["format_version"] == PROJECT_FORMAT_VERSION == 2
    assert document["origin"]["server"] == "https://lab.example.org"
    assert document["origin"]["session_key"] == "Synthetic_20Mar25"
    assert "run_id" not in document["origin"]

    document["origin"]["run_id"] = "run-1"
    write_project_document(document, path)
    assert read_reduction_project(path)["origin"]["run_id"] == "run-1"

    # A standalone pipeline writes no origin block.
    plain = build_reduction_project(_pipeline(raw), tmp_path / "p.upbr")
    assert "origin" not in plain


def test_version_1_project_loads_unchanged(tmp_path):
    raw = tmp_path / "raw"
    write_raw_directory(raw)
    document = build_reduction_project(_pipeline(raw), tmp_path / "old.upbr")
    document["format_version"] = 1
    path = tmp_path / "old.upbr"
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded = read_reduction_project(path)
    assert loaded["format_version"] == 1
    assert "origin" not in loaded
    assert loaded["selections"] == document["selections"]


def test_origin_validation():
    assert normalize_origin(None) is None
    with pytest.raises(ValueError, match="session_id"):
        normalize_origin({"server": "https://x"})
    with pytest.raises(ValueError):
        normalize_origin({"server": "https://x", "session_id": {"a": 1}})
    cleaned = normalize_origin({**ORIGIN, "run_id": None, "extra": 3})
    assert "run_id" not in cleaned and cleaned["extra"] == "3"


def test_manifest_records_origin_and_library_version(tmp_path):
    raw = tmp_path / "raw"
    write_raw_directory(raw)
    manifest = _pipeline(raw, ORIGIN).manifest(hash_inputs=True)
    assert manifest["metadata"]["origin"]["session_id"] == SESSION_ID
    assert manifest["reference_library_version"].startswith("format-2+sha256.")
    assert manifest["reference_library_sha256"] == reference_library_info()["sha256"]
    assert all(len(f["sha256"]) == 64 for f in manifest["input_files"])
    # Explicit metadata wins over the pipeline origin.
    explicit = _pipeline(raw, ORIGIN).manifest(metadata={"origin": {"x": 1}})
    assert explicit["metadata"]["origin"] == {"x": 1}
    assert "origin" not in _pipeline(raw).manifest()["metadata"]


def test_library_version_uses_declared_version_and_normalizes_newlines(tmp_path):
    for newline, folder in (("\n", "lf"), ("\r\n", "crlf")):
        root = tmp_path / folder
        (root / "glass").mkdir(parents=True)
        text = (
            'format_version = 2\nversion = "2026.11"\nsources = "sources.toml"\n'
            'material_files = ["glass/X.toml"]\n'
        )
        (root / "library.toml").write_bytes(text.replace("\n", newline).encode())
        (root / "sources.toml").write_bytes(b"format_version = 2" + newline.encode())
        (root / "glass" / "X.toml").write_bytes(b"format_version = 2" + newline.encode())
    lf = reference_library_info(tmp_path / "lf")
    crlf = reference_library_info(tmp_path / "crlf" / "library.toml")
    assert lf["version"].startswith("2026.11+sha256.")
    assert lf == crlf
