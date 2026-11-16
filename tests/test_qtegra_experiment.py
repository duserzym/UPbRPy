"""Qtegra sessions preserve the historical experiment tables and selection."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from UPbRPy import io, qtegra
from qtegra_helpers import write_y_only_csv
from synthetic_bundles import write_raw_directory, write_session_bundle


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_demo"
RICH = FIXTURE / "Zircon_demo_Rich.csv"
FOLDER = FIXTURE / "zircon_demo"


def frozen_legacy_experiment(directory, sep="_", sample_aliases=None):
    """Discovery and reading from io.read_experiment_data before milestone 3.

    Keep this independent of the session adapter, including the original
    pandas parsing, timestamp arithmetic, column dtypes and sorting.
    """
    spot_names = []
    spot_paths = []
    spot_starting_times = []
    csv_files = [name for name in os.listdir(directory) if name.endswith('.csv')]
    if not csv_files:
        raise FileNotFoundError(f"No CSV files were found in {directory!r}.")

    # Use one instrument export to obtain the common channel names.
    with open(os.path.join(directory, csv_files[0])) as file_handle:
        first_file = file_handle.readlines()

    # now update the global variable channels
    element_channels = first_file[13].split(',')[1:-1]

    time_format = "%m/%d/%Y %I:%M:%S %p"

    # Read only the short header during discovery; numerical tables are loaded
    # once below after the experiment has been placed in chronological order.
    for filename in csv_files:
        this_path = os.path.join(directory, filename)
        spot_paths.append(this_path)
        with open(this_path) as file_handle:
            first_line = file_handle.readline()
        spot_names.append(first_line.split(':')[0])
        spot_starting_times.append(
            datetime.strptime(first_line[-24:-2], time_format)
        )

    # Create a pandas dataframe to store the experiment data.
    experiment = pd.DataFrame()
    experiment['spot_names'] = spot_names
    parsed_samples = [spot_name.split(sep)[0] for spot_name in spot_names]
    if sample_aliases:
        aliases = {
            str(source).strip().casefold(): str(target).strip()
            for source, target in sample_aliases.items()
        }
        parsed_samples = [
            aliases.get(sample.casefold(), sample) for sample in parsed_samples
        ]
    experiment['sample'] = parsed_samples
    experiment['spot_paths'] = spot_paths
    experiment['spot_starting_times'] = spot_starting_times

    experiment = experiment.sort_values(by='spot_starting_times').reset_index(drop=True)
    spot_frames = []
    for i in range(experiment.shape[0]):
        # Accumulate spot frames and concatenate once. Repeated concatenation
        # would copy all previously imported rows for every additional spot.
        this_data = pd.read_csv(experiment['spot_paths'][i], skiprows=13)
        this_data = this_data.iloc[1:].reset_index(drop=True)
        this_data = this_data.dropna(axis=1, how='all')
        this_data = this_data.apply(pd.to_numeric, errors='coerce')
        # Convert relative seconds to absolute timestamps without a Python row
        # loop; pandas performs this operation as a vectorized timedelta add.
        this_data['Time'] = (
            experiment['spot_starting_times'][i]
            + pd.to_timedelta(this_data['Time'], unit='s')
        )
        this_data['spot_name'] = experiment['spot_names'][i]
        this_data['sample'] = experiment['sample'][i]
        spot_frames.append(this_data)

    total_data = pd.concat(spot_frames, ignore_index=True)

    experiment = experiment.sort_values(by='spot_names').reset_index(drop=True)
    #initialize the columns for the experiment dataframe with null values

    # we should also align the time column in both the experiment and total data to be in total seconds with respect to the start of the experiment
    # this will help with the later session-wide fractionation corrections
    experiment_start_time = experiment['spot_starting_times'].min()
    experiment['spot_starting_times'] = (experiment['spot_starting_times'] - experiment_start_time).dt.total_seconds()
    total_data['Time'] = (total_data['Time'] - experiment_start_time).dt.total_seconds()

    experiment['plateau_start_times'] = None
    experiment['plateau_end_times'] = None
    experiment['baseline_start_times'] = None
    experiment['baseline_end_times'] = None
    experiment['signal_start_times'] = None
    experiment['signal_end_times'] = None

    return experiment, element_channels, total_data


SELECTION = {
    "baseline_sweep_n": 10,
    "peak_sweep_n": 20,
    "threshold": 0.1,
    "filter_percentile": 70,
    "baseline_offset": 2,
    "plateau_offset": 2,
    "selection_method": "threshold",
    "selection_fallback_methods": (),
    "selection_options": {},
    "signal_duration_limit_seconds": None,
    "selection_channels": {},
}
WINDOWS = (
    "plateau_start_times", "plateau_end_times", "baseline_start_times",
    "baseline_end_times", "signal_start_times", "signal_end_times",
)


@pytest.mark.parametrize("case", ["fixture", "synthetic", "integer_and_empty"])
def test_spot_folder_is_bit_identical_to_legacy(tmp_path, case):
    folder = FOLDER
    if case != "fixture":
        folder = tmp_path / "spots"
        paths = write_raw_directory(folder)
        if case == "integer_and_empty":
            for path in paths:
                lines = path.read_text().splitlines()
                for index in range(15, len(lines)):
                    cells = lines[index].split(",")
                    cells[2] = str(int(float(cells[2])))
                    cells[3] = ""
                    lines[index] = ",".join(cells)
                path.write_text("\n".join(lines) + "\n")
    aliases = {"nist612": "Glass", "mad": "MADUCSB"}
    expected, expected_channels, expected_data = frozen_legacy_experiment(
        folder, sample_aliases=aliases,
    )
    expected = io.auto_select_baseline_peak(
        expected, expected_data, "29Si", **SELECTION,
    )
    actual, channels, data = io.read_experiment_data(
        directory=folder, sample_aliases=aliases, **SELECTION,
    )
    assert channels == expected_channels
    assert_frame_equal(actual, expected, check_exact=True)
    assert_frame_equal(data, expected_data, check_exact=True)
    assert actual.attrs["selection_parameters"] == SELECTION
    if case == "integer_and_empty":
        assert data["44Ca"].dtype == np.dtype("int64")
        assert "206Pb" not in data


def assert_provenance(experiment, session):
    expected = {
        "format": session.format,
        "capabilities": sorted(session.capabilities),
        "sources": [
            {"path": str(source.path), "sha256": source.sha256, "format": source.format}
            for source in session.sources
        ],
        "times_reconstructed": session.times_reconstructed,
        "nominal_cycle_time_s": session.nominal_cycle_time_s,
        "clock_offset_hours": session.clock_offset_hours,
        "software": session.software,
        "machine": session.machine,
        "warnings": session.warnings,
        # Added 2026-11-06 for the publication-table footnotes.
        "acquisition_summary": io.acquisition_summary(session),
    }
    assert experiment.attrs["qtegra"] == expected
    assert json.loads(json.dumps(expected, allow_nan=False)) == expected


def selection_indices(experiment, data):
    indices = []
    for row in experiment.itertuples(index=False):
        times = data.loc[data["spot_name"] == row.spot_names, "Time"].to_numpy()
        indices.append(tuple(
            None if pd.isna(value := getattr(row, column))
            else int(np.flatnonzero(times == value)[0])
            for column in WINDOWS
        ))
    return indices


def relative_times_ns(experiment, data):
    starts = experiment.set_index("spot_names")["spot_starting_times"]
    return np.rint(data["Time"].to_numpy() * 1e9).astype("int64") - np.rint(
        data["spot_name"].map(starts).to_numpy() * 1e9,
    ).astype("int64")


def test_rich_and_folder_have_identical_signals_times_and_selection_indices():
    rich, channels, rich_data = io.read_experiment_data(RICH)
    folder, folder_channels, folder_data = io.read_experiment_data(FOLDER)
    assert channels == folder_channels
    assert_frame_equal(rich[["spot_names", "sample"]], folder[["spot_names", "sample"]],
                       check_exact=True)
    # Rich prints Y with 17 significant digits (round-trip double), the
    # per-spot files with 15, e.g. 449124.38355127671 vs 449124.383551277.
    # The same measurement therefore agrees to the 15th digit, not bit for bit.
    assert_frame_equal(rich_data[channels], folder_data[channels], check_exact=False,
                       check_dtype=False, rtol=1e-14, atol=0)
    np.testing.assert_array_equal(rich_data["spot_name"], folder_data["spot_name"])
    np.testing.assert_array_equal(rich_data["sample"], folder_data["sample"])
    # Compare exact nanoseconds, before floating-point session subtraction
    # can introduce sub-nanosecond cancellation error.
    np.testing.assert_array_equal(relative_times_ns(rich, rich_data),
                                  relative_times_ns(folder, folder_data))
    np.testing.assert_allclose(rich["spot_starting_times"], folder["spot_starting_times"],
                               rtol=0, atol=1)
    assert selection_indices(rich, rich_data) == selection_indices(folder, folder_data)
    assert rich.attrs["selection_parameters"] == folder.attrs["selection_parameters"]
    assert rich["spot_paths"].tolist() == [str(RICH)] * len(rich)
    assert_provenance(rich, qtegra.read_rich_csv(RICH))
    assert_provenance(folder, qtegra.read_spot_folder(FOLDER))


def test_fixture_root_builds_from_merged_session():
    experiment, channels, data = io.read_experiment_data(FIXTURE)
    merged = qtegra.open_qtegra(FIXTURE)
    assert experiment.attrs["qtegra"]["format"] == "rich+spot_folder"
    assert_provenance(experiment, merged)
    direct, direct_channels, direct_data = io.read_experiment_data(merged)
    assert_frame_equal(experiment, direct, check_exact=True)
    assert_frame_equal(data, direct_data, check_exact=True)
    assert channels == direct_channels
    assert experiment.attrs == direct.attrs


@pytest.mark.parametrize("filename", [RICH.name])
def test_folder_with_single_export(tmp_path, filename):
    source = tmp_path / filename
    source.write_bytes((FIXTURE / filename).read_bytes())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        experiment, _, _ = io.read_experiment_data(tmp_path)
    assert len(caught) == 0
    assert experiment["spot_paths"].tolist() == [str(source)] * len(experiment)


@pytest.mark.parametrize("layout", ["beside", "bundle_raw"])
def test_file_source_uses_manifest_in_parent_with_existing_rules(tmp_path, layout):
    if layout == "bundle_raw":
        bundle = write_session_bundle(tmp_path)
        parent = bundle / "raw"
        manifest = bundle / "session_manifest.toml"
    else:
        parent = tmp_path
        manifest = parent / "session_manifest.toml"
    source = parent / RICH.name
    source.write_bytes(RICH.read_bytes())
    manifest.write_text('[session]\nsep = "-"\n[samples]\nNIST612_896 = "Glass"\n')
    experiment, _, _ = io.read_experiment_data(directory=source)
    samples = experiment.set_index("spot_names")["sample"]
    assert samples["NIST612_896"] == "Glass"
    assert samples["91500_738"] == "91500_738"
    assert set(experiment["spot_paths"]) == {str(source)}
    overridden, _, _ = io.read_experiment_data(
        source, sep="_", sample_aliases={"nist612": "Override"},
    )
    assert overridden.set_index("spot_names").loc["NIST612_896", "sample"] == "Override"
    ignored, _, _ = io.read_experiment_data(source, session_manifest=False)
    assert ignored.set_index("spot_names").loc["NIST612_896", "sample"] == "NIST612"


def test_session_adapter_uses_in_memory_values_before_selection(tmp_path, monkeypatch):
    paths = write_raw_directory(tmp_path)
    session = qtegra.open_qtegra(tmp_path)
    session.acquisitions[0].y[0, 0] = 123.5
    for path in paths:
        path.unlink()
    monkeypatch.setattr(io, "open_qtegra", lambda *args, **kwargs: pytest.fail("reread"))
    experiment, channels, data = io.experiment_from_session(
        session, sample_aliases={"nist612": "Glass"},
    )
    assert "experiment_from_session" in io.__all__
    assert all(experiment[column].isna().all() for column in WINDOWS)
    assert "selection_parameters" not in experiment.attrs
    assert data.loc[0, channels[0]] == 123.5
    assert data.loc[0, "sample"] == "Glass"
    selected, _, selected_data = io.read_experiment_data(session, session_manifest=False)
    assert selected_data.loc[0, channels[0]] == 123.5
    assert "selection_parameters" in selected.attrs


def test_raw_input_files_detects_sources_without_loading_sweeps(tmp_path, monkeypatch):
    from GUI.services import raw_input_files

    def no_loading(*args, **kwargs):
        pytest.fail("raw_input_files must only detect headers")

    monkeypatch.setattr(qtegra, "open_qtegra", no_loading)
    monkeypatch.setattr(qtegra, "read_rich_csv", no_loading)
    monkeypatch.setattr(qtegra, "read_spot_folder", no_loading)
    assert raw_input_files(RICH) == (RICH,)
    spots = tuple(sorted(FOLDER.glob("*.csv")))
    assert raw_input_files(FOLDER) == spots
    assert raw_input_files(FIXTURE) == tuple(sorted((RICH, *spots)))
    write_y_only_csv(RICH, tmp_path / "Y_only.csv")
    assert raw_input_files(tmp_path) == ()
    unrelated = tmp_path / "notes.csv"
    unrelated.write_text("note,value\nhello,1\n")
    rich = tmp_path / RICH.name
    rich.write_bytes(RICH.read_bytes())
    assert raw_input_files(tmp_path) == (rich,)
    spot = tmp_path / "spot.csv"
    spot.write_bytes(spots[0].read_bytes())
    assert raw_input_files(tmp_path) == tuple(sorted((rich, spot)))


@pytest.mark.parametrize("as_folder", [False, True])
def test_experiment_rejects_y_only_export(tmp_path, as_folder):
    source = write_y_only_csv(RICH, tmp_path / "Y_only.csv")
    with pytest.raises(qtegra.UnsupportedQtegraFormat, match="^Unsupported Qtegra export: no time information"):
        io.read_experiment_data(tmp_path if as_folder else source)
