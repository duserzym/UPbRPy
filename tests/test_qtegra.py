"""Qtegra input contract checked against the small Boise export fixture."""

from copy import deepcopy
import csv
from dataclasses import FrozenInstanceError
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
import pytest

import UPbRPy
from UPbRPy import qtegra as q


FIXTURE = Path(__file__).parent / "fixtures" / "qtegra" / "Zircon_04Nov26_slice"
RICH = FIXTURE / "Zircon_04Nov26_slice_Rich.csv"
UPBR = FIXTURE / "Zircon_04Nov26_slice_UPbR.csv"
SPOTS = FIXTURE / "zircon_04Nov26"
SPOT = SPOTS / "Zircon_demo_1.csv"
NAMES = ("29Si", "91Zr", "177Hf", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U")
LABELS = [
    "NIST612_896", "NIST612_897", "NIST612_898", "NIST610_863", "NIST610_864",
    "NIST610_865", "S513_628", "S513_629", "SAMPLE-A_S_323",
    "SAMPLE-A_S_324", "SAMPLE-A_S_325", "SAMPLE-A_S_326",
]


@pytest.fixture(scope="module")
def rich():
    return q.read_rich_csv(RICH)


@pytest.fixture(scope="module")
def spots():
    return q.read_spot_folder(SPOTS)


@pytest.fixture(scope="module")
def upbr():
    return q.read_upbr_csv(UPBR)


@pytest.fixture(scope="module")
def merged(rich, spots):
    return q.merge(rich, spots)


def copy_rows(source, target, edit):
    """Edit a fixture copy while retaining its BOM and line endings."""
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    edit(rows)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle, lineterminator="\r\n").writerows(rows)
    return target


def test_public_api():
    assert UPbRPy.qtegra is q
    assert "qtegra" in UPbRPy.FOCUSED_SUBMODULES
    assert set(q.__all__) == {
        "CAPABILITIES", "COUNTER_SATURATED", "DEFAULT_CYCLE_TIME_S",
        "Channel", "Acquisition", "SourceFile", "QtegraSession",
        "read_rich_csv", "read_spot_folder", "read_spot_file", "read_upbr_csv",
        "merge", "recover_xcal", "detect_format", "open_qtegra",
    }
    assert q.COUNTER_SATURATED == -33.0
    assert q.DEFAULT_CYCLE_TIME_S == 0.4255
    assert q.CAPABILITIES == frozenset({
        "timestamps", "stop_times", "per_channel_time", "analog_counter", "mass",
        "dwell_xcal", "instrument_state", "sequence_fields", "user",
    })
    with pytest.raises(FrozenInstanceError):
        q.Channel("29Si").mass = 29
    with pytest.raises(FrozenInstanceError):
        q.SourceFile(RICH, "digest", "rich").format = "upbr"


def test_reader_counts_and_order(rich, spots, upbr):
    for session in (rich, spots, upbr):
        assert len(session.acquisitions) == 12
        assert session.labels == LABELS
        assert session.channel_names == NAMES
        assert [a.sequence_index for a in session.acquisitions] == list(range(1, 13))
        assert [a.n_sweeps for a in session.acquisitions] == [72] * 12
        for acquisition in session.acquisitions:
            assert acquisition.channel_names == NAMES
            assert acquisition.y.shape == (72, 9)
            assert acquisition.y.dtype == np.dtype(float)
            if session is not upbr:
                assert acquisition.start_offset_s is None
            assert session.acquisition(acquisition.label) is acquisition
            assert acquisition.channel_index("238U") == 8
            with pytest.raises(ValueError):
                acquisition.channel_index("missing")
        with pytest.raises(KeyError):
            session.acquisition("missing")
    assert rich.capabilities == q.CAPABILITIES - {"dwell_xcal"}
    assert spots.capabilities == {"timestamps", "instrument_state", "dwell_xcal"}
    assert not upbr.capabilities
    assert rich.has("analog_counter")
    assert not spots.has("analog_counter")
    assert not rich.has("unknown")


def test_rich_metadata_and_per_channel_time(rich):
    first = rich.acquisitions[0]
    assert first.user == r"BOISESTATE\ERB5110"
    assert first.start_text == "2026-11-04T15:55:37.4924868"
    assert first.stop_text == "2026-11-04T15:56:09.7022924"
    assert first.start == datetime(2026, 11, 4, 15, 55, 37, 492486)
    assert first.stop == datetime(2026, 11, 4, 15, 56, 9, 702292)
    assert first.start.tzinfo is None and first.stop.tzinfo is None
    assert first.source_path == RICH
    assert first.tune_name == "Laser_STDS"
    assert first.tune_settings == {}
    assert len(rich.acquisitions[1].tune_settings) == 36
    assert rich.acquisitions[1].tune_settings["Plasma Power"] == 1550.0
    assert len(first.readbacks) == 43
    assert first.readbacks["Plasma Power Readback"] == pytest.approx(1548.6077186126038)
    assert first.sequence_fields == {
        "Label": "NIST612_896", "Duration [s]": "30", "Comment": "<Comment>",
        "Evaluate": "True", "Sample Type": "UNKNOWN", "Standard": "",
        "Dilution Factor": "1", "Amount": "", "Final Quantity": "",
        "Identifier": "<Identifier>", "Laser Scan Position Number": "896",
        "Laser Output Setting [%]": "46",
    }
    assert [a.spot_number for a in rich.acquisitions] == [896, 897, 898, 863, 864, 865, 628, 629, 323, 324, 325, 326]
    assert first.channels[0].mass == 28.97599983215332
    assert first.channels[-1].mass == 238.05099487304687
    assert all(c.dwell_s is None and c.xcal is None for c in first.channels)
    assert first.time_per_channel
    assert first.time.shape == first.analog.shape == first.counter.shape == (72, 9)
    np.testing.assert_array_equal(first.sweep_time, first.time[:, 0])
    assert not np.array_equal(first.time[:, 0], first.time[:, -1])
    assert not rich.warnings
    # Check all original timestamp strings, including the six-digit stop time.
    with RICH.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    for key, attribute in (("StartTime", "start_text"), ("StopTime", "stop_text")):
        row = next(row for row in rows if row[0] == key)
        assert [getattr(a, attribute) for a in rich.acquisitions] == row[4:-1]


def test_spot_metadata_and_single_file(spots):
    first = q.read_spot_file(SPOT)
    assert first.sequence_index == 0
    assert first.label == LABELS[0]
    assert first.channel_names == NAMES
    assert first.source_path == SPOT
    assert first.start_text == "11/04/2026 10:55:37 PM"
    assert first.start == datetime(2026, 11, 4, 22, 55, 37)
    assert first.stop is None and first.stop_text is None
    assert first.analog is None and first.counter is None
    assert first.time.shape == first.sweep_time.shape == (72,)
    assert not first.time_per_channel
    assert first.tune_name == "Laser_STDS" and len(first.tune_settings) == 36
    assert len(first.readbacks) == 43
    assert first.sequence_fields["spot_file_start_text"] == "NIST612_896:11/04/2026 10:55:37 PM;"
    assert all(channel.mass is None for channel in first.channels)
    assert [channel.dwell_s for channel in first.channels] == [.005, .001, .001, .01, .06, .06, .02, .01, .01]
    assert [channel.xcal for channel in first.channels] == [
        65695.80659, 84939.1898, 103455.99083, 110131.25119, 110930.51962,
        111330.15384, 111729.78805, 109350.46619, 109642.0085,
    ]
    assert spots.software == "Qtegra 2.10.4345.64"
    assert spots.machine == "iCAP RQ"
    assert spots.pulse_counting_threshold == 2500000.0
    assert spots.clock_offset_hours is None and spots.clock_offset_seconds_median is None
    # Use the existing per-spot numerical parser as an independent reference.
    expected = pd.read_csv(SPOT, skiprows=13).iloc[1:]
    expected = expected.dropna(axis=1, how="all").apply(pd.to_numeric, errors="coerce")
    np.testing.assert_array_equal(first.y, expected.iloc[:, 1:].to_numpy(dtype=float))
    np.testing.assert_array_equal(first.time, expected["Time"].to_numpy(dtype=float))


def test_hashes(rich, spots, upbr, merged):
    for session, count, formats in (
        (rich, 1, {"rich"}), (spots, 12, {"spot"}), (upbr, 1, {"upbr"}),
        (merged, 13, {"rich", "spot"}),
    ):
        assert len(session.sources) == count
        assert {source.format for source in session.sources} == formats
        for source in session.sources:
            assert isinstance(source.path, Path)
            assert source.sha256 == sha256(source.path.read_bytes()).hexdigest()


def test_upbr_y_and_reconstructed_times(rich, upbr):
    assert upbr.times_reconstructed
    assert upbr.nominal_cycle_time_s == q.DEFAULT_CYCLE_TIME_S
    assert any("reconstructed" in warning for warning in upbr.warnings)
    for i, (original, derived) in enumerate(zip(rich.acquisitions, upbr.acquisitions)):
        np.testing.assert_array_equal(original.y, derived.y)
        np.testing.assert_array_equal(derived.time, np.arange(72) * q.DEFAULT_CYCLE_TIME_S)
        assert derived.time[0] == 0
        assert derived.start_offset_s == pytest.approx(i * 72 * q.DEFAULT_CYCLE_TIME_S)
        assert derived.start is None and derived.start_text is None
        assert derived.stop is None and derived.stop_text is None
        assert derived.analog is None and derived.counter is None
        assert not derived.time_per_channel
        assert all(c.mass is None and c.dwell_s is None and c.xcal is None for c in derived.channels)
    custom = q.read_upbr_csv(UPBR, cycle_time_s=.5, acquisition_gap_s=2)
    assert custom.nominal_cycle_time_s == .5
    for i, acquisition in enumerate(custom.acquisitions):
        np.testing.assert_array_equal(acquisition.time, np.arange(72) * .5)
        assert acquisition.start_offset_s == i * 38


@pytest.mark.parametrize("kwargs", [
    {"cycle_time_s": 0}, {"cycle_time_s": -1}, {"cycle_time_s": np.nan},
    {"cycle_time_s": np.inf}, {"acquisition_gap_s": -1}, {"acquisition_gap_s": np.nan},
])
def test_invalid_reconstruction_parameters(kwargs):
    with pytest.raises(ValueError):
        q.read_upbr_csv(UPBR, **kwargs)


def test_merge_metadata_and_preserved_rich_values(rich, spots, merged):
    assert merged.format == "rich+spot_folder"
    assert merged.capabilities == rich.capabilities | spots.capabilities == q.CAPABILITIES
    assert merged.clock_offset_hours == 7.0
    seconds = [(s.start - r.start).total_seconds() for r, s in zip(rich.acquisitions, spots.acquisitions)]
    assert merged.clock_offset_seconds_median == float(np.median(seconds))
    assert merged.software == spots.software
    assert merged.machine == spots.machine
    assert merged.pulse_counting_threshold == spots.pulse_counting_threshold
    assert merged.sources == rich.sources + spots.sources
    assert merged.acquisitions[0].tune_settings == spots.acquisitions[0].tune_settings
    assert any(LABELS[0] in warning and "tune_settings" in warning for warning in merged.warnings)
    assert len(merged.warnings) == 1  # Clock rounding does not produce a warning.
    for original, spot, result in zip(rich.acquisitions, spots.acquisitions, merged.acquisitions):
        for field in ("y", "time", "counter", "analog"):
            np.testing.assert_array_equal(getattr(original, field), getattr(result, field))
            assert not np.shares_memory(getattr(original, field), getattr(result, field))
        assert result.start == original.start
        assert result.start_text == original.start_text
        assert result.stop == original.stop
        assert result.stop_text == original.stop_text
        assert result.readbacks == original.readbacks
        assert result.sequence_fields["spot_file_start_text"] == spot.sequence_fields["spot_file_start_text"]
        assert [c.mass for c in result.channels] == [c.mass for c in original.channels]
        assert [(c.dwell_s, c.xcal) for c in result.channels] == [(c.dwell_s, c.xcal) for c in spot.channels]
    assert rich.acquisitions[0].tune_settings == {}
    assert rich.acquisitions[0].channels[0].dwell_s is None
    assert "spot_file_start_text" not in rich.acquisitions[0].sequence_fields
    assert spots.acquisitions[0].start.hour == 22


@pytest.mark.parametrize("problem", ["y", "nan", "time", "time_nan", "channels", "sweeps"])
def test_merge_mismatch_names_label_and_channel(rich, spots, problem):
    changed = deepcopy(spots)
    acquisition = changed.acquisitions[0]
    if problem == "y":
        acquisition.y[0, 0] *= 2
    elif problem == "nan":
        acquisition.y[0, 0] = np.nan
    elif problem == "time":
        acquisition.time[0] += .01
    elif problem == "time_nan":
        acquisition.time[0] = np.nan
    elif problem == "channels":
        acquisition.channels = (q.Channel("wrong"), *acquisition.channels[1:])
    else:
        acquisition.y = acquisition.y[:-1]
    with pytest.raises(ValueError) as error:
        q.merge(rich, changed)
    assert LABELS[0] in str(error.value)
    assert "29Si" in str(error.value)


def test_merge_labels_and_empty_instrument_state(rich, spots):
    changed = deepcopy(spots)
    changed.acquisitions = changed.acquisitions[2:]
    with pytest.raises(ValueError) as error:
        q.merge(rich, changed)
    assert LABELS[0] in str(error.value) and LABELS[1] in str(error.value)
    changed = deepcopy(spots)
    extra = deepcopy(changed.acquisitions[0])
    extra.label = "extra_999"
    changed.acquisitions.append(extra)
    assert any("extra_999" in warning for warning in q.merge(rich, changed).warnings)
    changed.acquisitions.append(extra)
    with pytest.raises(ValueError, match="duplicate"):
        q.merge(rich, changed)
    changed_rich = deepcopy(rich)
    changed_rich.acquisitions[1].readbacks.clear()
    result = q.merge(changed_rich, spots)
    assert result.acquisitions[1].readbacks == spots.acquisitions[1].readbacks
    assert any(LABELS[1] in warning and "readbacks" in warning for warning in result.warnings)


def test_merge_matching_missing_cells_and_rtol(rich, spots):
    left, right = deepcopy(rich), deepcopy(spots)
    left.acquisitions[0].y[1, 0] = right.acquisitions[0].y[1, 0] = np.nan
    right.acquisitions[0].y[0, 0] *= 1.00001
    q.merge(left, right, rtol=1e-4)
    with pytest.raises(ValueError, match="29Si"):
        q.merge(left, right)


def test_rich_y_detector_rule(merged):
    saturated_channels = set()
    for acquisition in merged.acquisitions:
        valid = np.isfinite(acquisition.counter) & (acquisition.counter != q.COUNTER_SATURATED)
        np.testing.assert_array_equal(acquisition.y[valid], acquisition.counter[valid])
        for i, channel in enumerate(acquisition.channels):
            saturated = acquisition.counter[:, i] == q.COUNTER_SATURATED
            if saturated.any():
                saturated_channels.add(channel.name)
                np.testing.assert_allclose(
                    acquisition.y[saturated, i], acquisition.analog[saturated, i] * channel.xcal,
                    rtol=1e-9, atol=0,
                )
    assert saturated_channels == {"29Si", "91Zr", "177Hf"}


def test_recover_xcal(rich, spots, upbr):
    factors = q.recover_xcal(rich)
    assert factors.keys() == {"29Si", "91Zr", "177Hf"}
    for name, factor in factors.items():
        assert factor == pytest.approx(spots.acquisitions[0].channels[NAMES.index(name)].xcal, rel=1e-9)
    for acquisition in rich.acquisitions:
        for name, factor in q.recover_xcal(acquisition).items():
            assert factor == pytest.approx(factors[name], rel=1e-9)
    assert q.recover_xcal(upbr) == {}
    changed = deepcopy(rich.acquisitions[0])
    changed.counter[:] = q.COUNTER_SATURATED
    changed.analog[:] = 0
    changed.analog[0, 0] = np.nan
    assert q.recover_xcal(changed) == {}
    # Check that session recovery pools sweeps rather than acquisition medians.
    first, second = deepcopy(rich.acquisitions[0]), deepcopy(rich.acquisitions[1])
    for acquisition in (first, second):
        acquisition.counter[:] = 0
        acquisition.analog[:, 0] = 1
    first.counter[0, 0] = q.COUNTER_SATURATED
    first.y[0, 0] = 1
    second.counter[:3, 0] = q.COUNTER_SATURATED
    second.y[:3, 0] = 3
    session = deepcopy(rich)
    session.acquisitions = [first, second]
    assert q.recover_xcal(session) == {"29Si": 3}


@pytest.mark.parametrize("source, kind", [
    (RICH, "rich"), (UPBR, "upbr"), (SPOT, "spot"),
    (SPOTS, "spot_folder"), (FIXTURE, "folder"),
])
def test_detect_format(source, kind):
    assert q.detect_format(source) == kind


def test_detection_uses_content_and_start_time(tmp_path):
    # Detect a Rich footer even when the first MainRuns block has only Y.
    def edit(rows):
        rows[:] = [row for row in rows if row[0] != "MainRuns" or row[3] == "Y"]
    source = copy_rows(RICH, tmp_path / "not_named_rich.csv", edit)
    assert q.detect_format(source) == "rich"
    # A non-Y quantity late in the first channel block also identifies Rich.
    def reorder(rows):
        main = [row for row in rows if row[0] == "MainRuns"]
        metadata = [row for row in rows[2:] if row[0] != "MainRuns"]
        main.sort(key=lambda row: (NAMES.index(row[2]), row[3] != "Y"))
        rows[2:] = main + metadata
    source = copy_rows(RICH, tmp_path / "Y_first.csv", reorder)
    assert q.detect_format(source) == "rich"


@pytest.mark.parametrize("source, kind, count", [
    (RICH, "rich", 12), (UPBR, "upbr", 12), (SPOT, "spot_folder", 1),
    (SPOTS, "spot_folder", 12), (FIXTURE, "rich+spot_folder", 12),
])
def test_open_qtegra(source, kind, count):
    session = q.open_qtegra(source)
    assert session.format == kind
    assert len(session.acquisitions) == count
    assert session.channel_names == NAMES
    if source == FIXTURE:
        assert session.capabilities == q.CAPABILITIES
        assert any("UPbR" in warning and "ignored" in warning for warning in session.warnings)
        assert len(session.sources) == 13
        assert UPBR not in {source.path for source in session.sources}
    elif source == SPOT:
        assert session.acquisitions[0].sequence_index == 1


def test_folder_layouts_and_reconstruction_kwargs(tmp_path):
    rich_path = shutil.copy2(RICH, tmp_path / "record.csv")
    assert q.open_qtegra(tmp_path).format == "rich"
    for spot in SPOTS.glob("*.csv"):
        shutil.copy2(spot, tmp_path / spot.name)
    assert q.open_qtegra(tmp_path).format == "rich+spot_folder"
    shutil.copy2(RICH, tmp_path / "duplicate.csv")
    with pytest.raises(ValueError, match="More than one Rich"):
        q.open_qtegra(tmp_path)
    Path(rich_path).unlink()
    (tmp_path / "duplicate.csv").unlink()
    assert q.open_qtegra(tmp_path).format == "spot_folder"
    single_folder = tmp_path / "derived"
    single_folder.mkdir()
    shutil.copy2(UPBR, single_folder / "derived.csv")
    session = q.open_qtegra(single_folder, cycle_time_s=.5, acquisition_gap_s=2)
    assert session.acquisitions[1].time[0] == 0
    assert session.acquisitions[1].start_offset_s == 38
    assert q.open_qtegra(UPBR, cycle_time_s=.25).nominal_cycle_time_s == .25
    shutil.copy2(UPBR, tmp_path / UPBR.name)
    assert q.open_qtegra(tmp_path).format == "spot_folder"
    shutil.copy2(UPBR, single_folder / "duplicate.csv")
    with pytest.raises(ValueError, match="More than one UPbR"):
        q.open_qtegra(single_folder)
    with pytest.raises(TypeError, match="Unexpected"):
        q.open_qtegra(RICH, typo=True)


def test_ambiguous_companion_folders(tmp_path):
    shutil.copy2(RICH, tmp_path / RICH.name)
    shutil.copytree(SPOTS, tmp_path / "first")
    shutil.copytree(SPOTS, tmp_path / "second")
    with pytest.raises(ValueError, match="Ambiguous"):
        q.open_qtegra(tmp_path)


def test_spot_folder_time_order_ties_and_ignored_files(tmp_path):
    # Rename the latest spot to sort before the first one alphabetically.
    shutil.copy2(SPOTS / "Zircon_demo_12.csv", tmp_path / "a.csv")
    shutil.copy2(SPOT, tmp_path / "z.csv")
    shutil.copy2(SPOT, tmp_path / "b.csv")
    shutil.copy2(RICH, tmp_path / RICH.name)
    (tmp_path / "notes.txt").write_text("notes")
    (tmp_path / "unrelated.csv").write_text("a,b\n1,2\n")
    session = q.read_spot_folder(tmp_path)
    assert session.labels == [LABELS[0], LABELS[0], LABELS[-1]]
    assert [a.source_path.name for a in session.acquisitions] == ["b.csv", "z.csv", "a.csv"]
    assert [a.sequence_index for a in session.acquisitions] == [1, 2, 3]
    assert len(session.sources) == 3


def test_invalid_files_and_empty_folders(tmp_path):
    with pytest.raises(ValueError, match="No Qtegra"):
        q.open_qtegra(tmp_path)
    with pytest.raises(ValueError, match="No Qtegra per-spot"):
        q.read_spot_folder(tmp_path)
    invalid = tmp_path / "other.csv"
    invalid.write_text("a,b\n1,2\n")
    for reader in (q.detect_format, q.open_qtegra, q.read_spot_file, q.read_rich_csv, q.read_upbr_csv):
        with pytest.raises(ValueError):
            reader(invalid)


def test_spot_number_mismatch_warns_and_keeps_exported_value(tmp_path):
    def edit(rows):
        next(row for row in rows if row[1] == "Laser Scan Position Number")[4] = "999"
    source = copy_rows(RICH, tmp_path / "mismatch.csv", edit)
    session = q.read_rich_csv(source)
    assert session.acquisitions[0].spot_number == 999
    assert len(session.warnings) == 1 and LABELS[0] in session.warnings[0]
    assert "896" in session.warnings[0] and "999" in session.warnings[0]


def test_empty_sweep_72_interior_missing_and_present_sweep_72(tmp_path, rich):
    assert all(a.n_sweeps == 72 and np.isfinite(a.y).all() for a in rich.acquisitions)

    def edit(rows):
        for row in rows:
            if row[:4] == ["MainRuns", "2", "29Si", "Y"]:
                row[4] = ""  # An interior missing cell must stay NaN.
            if row[:2] == ["MainRuns", "3"] and row[3] == "Y":
                row[4] = ""  # A whole interior sweep must stay present.
            if row[:4] == ["MainRuns", "72", "29Si", "Y"]:
                row[5] = "0"  # Zero is a present sweep, not a missing value.
    for original, reader in ((RICH, q.read_rich_csv), (UPBR, q.read_upbr_csv)):
        source = copy_rows(original, tmp_path / original.name, edit)
        session = reader(source)
        first, second = session.acquisitions[:2]
        assert first.n_sweeps == 72 and second.n_sweeps == 73
        assert np.isnan(first.y[2, 0])
        assert np.isnan(first.y[3]).all()
        assert np.isfinite(first.y[2, 1:]).all()
        assert second.y[72, 0] == 0
        assert np.isnan(second.y[72, 1:]).all()
        if reader is q.read_upbr_csv:
            assert second.start_offset_s == 72 * q.DEFAULT_CYCLE_TIME_S
            assert session.acquisitions[2].start_offset_s == pytest.approx(
                (72 + 73) * q.DEFAULT_CYCLE_TIME_S,
            )


def test_spot_empty_cells_stay_nan(tmp_path):
    def edit(rows):
        rows[16][1] = ""
        rows[17][1:-1] = [""] * 9
        rows[-1:-1] = [[""] * 11]  # Empty trailing sweep.
    source = copy_rows(SPOT, tmp_path / "missing.csv", edit)
    acquisition = q.read_spot_file(source)
    assert acquisition.n_sweeps == 72
    assert np.isnan(acquisition.y[1, 0])
    assert np.isnan(acquisition.y[2]).all()
    assert acquisition.y[0, 1] == 0
