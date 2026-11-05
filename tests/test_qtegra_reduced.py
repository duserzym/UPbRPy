"""Reduced Rich exports retain signals and advertise only exported capabilities."""

from pathlib import Path
import shutil
import warnings

import numpy as np
import pandas as pd
import pytest

from UPbRPy import io, qtegra as q
from UPbRPy.pipeline import PipelineConfig, ReductionPipeline


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
RICH = FIXTURE / "Zircon_04Nov26_slice_Rich.csv"
SPOTS = FIXTURE / "zircon_04Nov26"
FULL = q.CAPABILITIES - {"dwell_xcal"}


def reduced(tmp_path, *, drop=(), keep=None, name="reduced.csv"):
    """Filter raw fixture lines without rewriting the BOM, CRLF or cells."""
    lines = RICH.read_bytes().splitlines(keepends=True)
    selected = lines[:2]
    for line in lines[2:]:
        keys = line.split(b",", 4)
        group = keys[0].decode()
        key = keys[3].decode() if group == "MainRuns" else group
        if key not in drop and (keep is None or key in keep):
            selected.append(line)
    target = tmp_path / name
    target.write_bytes(b"".join(selected))
    return target


@pytest.fixture(scope="module")
def full():
    return q.read_rich_csv(RICH)


VARIANTS = [
    (("Analog", "Counter"), None, FULL - {"analog_counter"}),
    (("X [u]",), None, FULL - {"mass"}),
    (("TuneSettings", "StatusReport"), None, FULL - {"instrument_state"}),
    (("Info",), None, FULL - {"user"}),
    (("StopTime",), None, FULL - {"stop_times"}),
    (("BrigidMS", "PMIAnalyteLaser"), None, FULL - {"sequence_fields"}),
    (("Time",), None, FULL - {"per_channel_time"}),
    (("StartTime",), None, FULL - {"timestamps"}),
    ((), ("Y", "Time"), {"per_channel_time"}),
    ((), ("Y", "StartTime"), {"timestamps"}),
    (("Analog",), None, FULL - {"analog_counter"}),
    (("Counter",), None, FULL - {"analog_counter"}),
    (("TuneSettings",), None, FULL),
    (("StatusReport",), None, FULL),
    (("BrigidMS",), None, FULL),
    (("PMIAnalyteLaser",), None, FULL),
]


@pytest.mark.parametrize("drop,keep,expected", VARIANTS, ids=[
    "no-detectors", "no-mass", "no-state", "no-user", "no-stop", "no-sequence",
    "no-time", "no-start", "Y-Time", "Y-StartTime", "no-analog", "no-counter",
    "no-tune", "no-readbacks", "laser-only", "brigid-only",
])
def test_reduced_capabilities_and_fields(tmp_path, full, drop, keep, expected):
    source = reduced(tmp_path, drop=drop, keep=keep)
    raw = source.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert q.detect_format(source) == "rich"
    session = q.read_rich_csv(source)
    assert session.capabilities == expected
    assert q.open_qtegra(source).capabilities == expected
    assert session.labels == full.labels
    assert session.channel_names == full.channel_names
    assert session.software is session.machine is session.pulse_counting_threshold is None
    reconstruct = "per_channel_time" not in expected
    assert session.times_reconstructed == reconstruct
    assert session.nominal_cycle_time_s == (q.DEFAULT_CYCLE_TIME_S if reconstruct else None)
    if reconstruct:
        assert session.warnings == [
            "No MainRuns Time rows: sweep times reconstructed with cycle_time_s=0.4255 s from StartTime."
        ]
    elif ("Analog" in drop) != ("Counter" in drop):
        assert len(session.warnings) == 1
        missing = "Analog" if "Analog" in drop else "Counter"
        assert f"Missing MainRuns {missing} rows" in session.warnings[0]
    elif "timestamps" not in expected:
        assert session.warnings == [
            "No StartTime row: acquisitions are placed back to back on the session "
            "time axis; time-dependent drift corrections are approximate."
        ]
    else:
        assert session.warnings == []
    for actual, original in zip(session.acquisitions, full.acquisitions):
        np.testing.assert_array_equal(actual.y.view("uint64"), original.y.view("uint64"))
        assert all(c.dwell_s is c.xcal is None for c in actual.channels)
        if "analog_counter" not in expected:
            assert actual.analog is actual.counter is None
        else:
            np.testing.assert_array_equal(actual.analog, original.analog)
            np.testing.assert_array_equal(actual.counter, original.counter)
        if "mass" not in expected:
            assert all(c.mass is None for c in actual.channels)
        if "instrument_state" not in expected:
            assert actual.tune_name is None
            assert actual.tune_settings == actual.readbacks == {}
        if "TuneSettings" in drop:
            assert actual.tune_name is None and actual.tune_settings == {}
        if "StatusReport" in drop:
            assert actual.readbacks == {}
        if "user" not in expected:
            assert actual.user is None
        if "timestamps" not in expected:
            assert actual.start is actual.start_text is None
        if "stop_times" not in expected:
            assert actual.stop is actual.stop_text is None
        if "sequence_fields" not in expected:
            assert actual.sequence_fields == {}
        if reconstruct:
            assert actual.time.ndim == 1 and not actual.time_per_channel
            np.testing.assert_array_equal(actual.time, np.arange(actual.n_sweeps) * q.DEFAULT_CYCLE_TIME_S)
        else:
            assert actual.time_per_channel
            np.testing.assert_array_equal(actual.time, original.time)


@pytest.mark.parametrize("reader", [q.detect_format, q.read_rich_csv, q.open_qtegra])
def test_y_only_is_unsupported(tmp_path, reader):
    source = reduced(tmp_path, keep=("Y",))
    with pytest.raises(q.UnsupportedQtegraFormat, match="^Unsupported Qtegra export: no time information") as error:
        reader(source)
    assert "no MainRuns Time rows and no StartTime row" in str(error.value)


def test_partial_laser_group(tmp_path, full):
    source = reduced(tmp_path)
    lines = source.read_bytes().splitlines(keepends=True)
    source.write_bytes(b"".join(line for line in lines if not line.startswith(b"PMIAnalyteLaser,Identifier,")))
    assert sum(line.startswith(b"PMIAnalyteLaser,") for line in source.read_bytes().splitlines()) == 2
    session = q.read_rich_csv(source)
    assert session.capabilities == FULL
    for actual, original in zip(session.acquisitions, full.acquisitions):
        assert actual.sequence_fields == {k: v for k, v in original.sequence_fields.items() if k != "Identifier"}
        np.testing.assert_array_equal(actual.y.view("uint64"), original.y.view("uint64"))


@pytest.mark.parametrize("footer_rows", [0, 2, 100])
def test_detection_scans_variable_footer_without_table_loading(tmp_path, monkeypatch, footer_rows):
    source = reduced(tmp_path, keep=("Y", "StartTime"))
    # Put arbitrary metadata after StartTime, crossing reverse-scan chunks.
    row = b"BrigidMS,Comment,,," + b"x" * 5000 + b",\r\n"
    with source.open("ab") as handle:
        handle.write(row * footer_rows)
    monkeypatch.setattr(pd, "read_csv", lambda *a, **k: pytest.fail("Detection loaded the table"))
    assert q.detect_format(source) == "rich"
    no_footer = reduced(tmp_path, keep=("Y", "Time"), name="no-footer.csv")
    assert q.detect_format(no_footer) == "rich"


@pytest.mark.parametrize("cycle", [None, 0.25])
def test_reconstructed_experiment_calendar_axis_and_warning(tmp_path, cycle):
    source = reduced(tmp_path, keep=("Y", "StartTime"))
    session = q.read_rich_csv(source, cycle_time_s=cycle)
    spacing = q.DEFAULT_CYCLE_TIME_S if cycle is None else cycle
    with pytest.warns(UserWarning, match="reconstructed") as caught:
        experiment, channels, data = io.read_experiment_data(source, cycle_time_s=cycle)
    assert len(caught) == 1
    assert experiment.attrs["qtegra"]["times_reconstructed"]
    assert experiment.attrs["qtegra"]["nominal_cycle_time_s"] == spacing
    assert np.isfinite(data[["Time", *channels]].to_numpy()).all()
    assert np.isfinite(experiment["plateau_start_times"].astype(float)).all()
    origin = min(a.start for a in session.acquisitions)
    starts = experiment.set_index("spot_names")["spot_starting_times"]
    for acquisition in session.acquisitions:
        offset = (acquisition.start - origin).total_seconds()
        assert starts[acquisition.label] == offset
        actual = data.loc[data.spot_name == acquisition.label, "Time"].to_numpy()
        np.testing.assert_allclose(actual, offset + np.arange(acquisition.n_sweeps) * spacing,
                                   rtol=0, atol=2e-9)


@pytest.mark.parametrize("cycle", [0, -1, float("inf"), float("-inf"), float("nan"), "bad"])
@pytest.mark.parametrize("reader", [q.read_rich_csv, q.open_qtegra, io.read_experiment_data])
def test_invalid_nominal_cycle(tmp_path, cycle, reader):
    source = reduced(tmp_path, drop=("Time",))
    with pytest.raises(ValueError, match="cycle_time_s must be finite and > 0"):
        reader(source, cycle_time_s=cycle)


def test_cycle_ignored_for_recorded_time_and_spots(full):
    for source in (RICH, SPOTS):
        with warnings.catch_warnings(record=True) as caught:
            experiment, _, data = io.read_experiment_data(source, cycle_time_s=float("nan"))
        assert not caught
        assert not experiment.attrs["qtegra"]["times_reconstructed"]
        assert np.isfinite(data.Time).all()
    session = q.read_rich_csv(RICH, cycle_time_s=-1)
    np.testing.assert_array_equal(session.acquisitions[0].time, full.acquisitions[0].time)


def test_folder_discovery_forwards_cycle(tmp_path):
    reduced(tmp_path, keep=("Y", "StartTime"))
    session = q.open_qtegra(tmp_path, cycle_time_s=0.3)
    assert session.nominal_cycle_time_s == 0.3
    np.testing.assert_array_equal(session.acquisitions[0].time, np.arange(72) * 0.3)


def test_every_channel_requires_y(tmp_path):
    source = reduced(tmp_path)
    lines = source.read_bytes().splitlines(keepends=True)
    source.write_bytes(b"".join(
        line for line in lines
        if not (line.startswith(b"MainRuns,") and line.split(b",", 4)[2:4] == [b"238U", b"Y"])
    ))
    with pytest.raises(ValueError, match="Missing MainRuns Y rows for channels: 238U"):
        q.read_rich_csv(source)


def test_merge_minimal_start_time_keeps_nominal_timing(tmp_path):
    source = reduced(tmp_path, keep=("Y", "StartTime"))
    rich = q.read_rich_csv(source)
    merged = q.merge(rich, q.read_spot_folder(SPOTS))
    assert merged.capabilities == {"timestamps", "instrument_state", "dwell_xcal"}
    assert merged.times_reconstructed and merged.nominal_cycle_time_s == q.DEFAULT_CYCLE_TIME_S
    for acquisition in merged.acquisitions:
        assert acquisition.analog is acquisition.counter is None
        assert all(channel.mass is None for channel in acquisition.channels)
        assert acquisition.tune_settings and acquisition.readbacks
        np.testing.assert_array_equal(acquisition.time, np.arange(acquisition.n_sweeps) * q.DEFAULT_CYCLE_TIME_S)
    with pytest.warns(UserWarning, match="reconstructed") as caught:
        experiment, _, data = io.read_experiment_data(merged)
    assert len(caught) == 1
    assert np.isfinite(data.Time).all()
    assert experiment.attrs["qtegra"]["times_reconstructed"]


@pytest.mark.parametrize("drop", [("Time",), ("StartTime",)])
def test_merge_reduced_time_variants(tmp_path, drop):
    source = reduced(tmp_path, drop=drop)
    rich = q.read_rich_csv(source)
    folder = q.read_spot_folder(SPOTS)
    if "StartTime" in drop:
        with pytest.raises(ValueError, match="StartTime is needed to merge"):
            q.merge(rich, folder)
        return
    merged = q.merge(rich, folder)
    assert merged.capabilities == rich.capabilities | folder.capabilities
    assert merged.clock_offset_hours == 7.0
    assert merged.times_reconstructed
    for actual, original in zip(merged.acquisitions, rich.acquisitions):
        np.testing.assert_array_equal(actual.time, original.time)
        np.testing.assert_array_equal(actual.y, original.y)
        assert all(c.dwell_s is not None and c.xcal is not None for c in actual.channels)
    shutil.copytree(SPOTS, tmp_path / "spots")
    assert q.open_qtegra(tmp_path).format == "rich+spot_folder"


def test_y_time_reduction_matches_full(tmp_path):
    source = reduced(tmp_path, keep=("Y", "Time"))
    full_exp, full_channels, full_data = io.read_experiment_data(RICH)
    experiment, channels, data = io.read_experiment_data(source)
    assert channels == full_channels
    # Without StartTime, calendar gaps are unknowable. Compare exact Y and
    # selection indices; exclude absolute/session time and source provenance.
    pd.testing.assert_frame_equal(data.drop(columns="Time"), full_data.drop(columns="Time"), check_exact=True)
    time_columns = [c for c in experiment if c.endswith("_times")]
    excluded = ["spot_paths", *time_columns]
    left, right = experiment.drop(columns=excluded), full_exp.drop(columns=excluded)
    left.attrs = right.attrs = {}
    pd.testing.assert_frame_equal(left, right, check_exact=True)
    for actual, original in zip(experiment.itertuples(), full_exp.itertuples()):
        t = data.loc[data.spot_name == actual.spot_names, "Time"].to_numpy()
        ft = full_data.loc[full_data.spot_name == original.spot_names, "Time"].to_numpy()
        for column in time_columns:
            if column != "spot_starting_times":
                assert np.flatnonzero(t == getattr(actual, column)).tolist() == np.flatnonzero(ft == getattr(original, column)).tolist()
        np.testing.assert_allclose(t - actual.spot_starting_times,
                                   ft - original.spot_starting_times, rtol=0, atol=1e-9)
    assert (np.diff(data.Time) > 0).all()
    # Exercise the pipeline through baseline subtraction. The fixture omits
    # 202Hg, which the raw-ratio stage requires, so final ratios cannot run.
    results = []
    for exp, signals in ((experiment, data), (full_exp, full_data)):
        pipeline = ReductionPipeline(exp, signals, channels, pd.DataFrame(), PipelineConfig())
        results.append(pipeline.run_baseline())
    actual, original = results
    assert not actual.empty
    pd.testing.assert_frame_equal(actual.drop(columns="Time"),
                                  original.drop(columns="Time"), check_exact=True)


def test_gui_open_y_time(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    from GUI.main_window import MainWindow
    from GUI.theme import build_application

    source = reduced(tmp_path, keep=("Y", "Time"))
    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    QtCore.QSettings("UPbRPy", "Studio").clear()
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[1:]))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *args: (str(source), ""))
    window = MainWindow(theme_mode="light")
    try:
        loader = window.load_experiment_directory
        monkeypatch.setattr(window, "load_experiment_directory", lambda path: loader(path, background=False))
        window.show()
        window.choose_qtegra_file()
        app.processEvents()
        assert errors == []
        assert window.session.loaded
        assert window.session.qtegra.capabilities == {"per_channel_time"}
        assert window.capabilities_badge.text() == "Rich"
        assert not window.detector_calibration_panel.isEnabled()
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        app.processEvents()
        QtCore.QSettings("UPbRPy", "Studio").clear()
