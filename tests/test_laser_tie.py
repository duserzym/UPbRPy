"""Standalone laser ties use synthetic Chromium text and Qtegra readings."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from UPbRPy.io import read_experiment_data
from UPbRPy.laser_tie import LaserTie, read_session_with_laser, tie_laser_log
from UPbRPy.laserlog import read_lase
from UPbRPy.qtegra import Acquisition, Channel
from synthetic_bundles import write_session_bundle
from test_laserlog import UNFIRED, firing, scan_row, write_lase


def acquisitions(log, offsets=(3600., 3601., 3599., 3700.)):
    result = []
    for i, (scan, offset) in enumerate(zip(log.scans, offsets)):
        on = scan.firings[0].laser_on
        result.append(Acquisition(
            label=f"Synthetic_{i + 1}", sequence_index=i + 1, channels=(Channel("29Si"),),
            y=np.ones((30, 1)), time=np.arange(30.), spot_number=i + 1,
            start=on + timedelta(seconds=offset),
        ))
    return result


def timed_log(tmp_path):
    return read_lase(write_lase(tmp_path, [scan_row(i, description=f"Spot {i + 11}",
                        data=firing(minute=i)) for i in range(4)]))


def session_log(folder, *, times=True):
    """Laser log matching the synthetic bundle's four acquisition labels."""
    rows = []
    base = datetime(2025, 3, 20, 20, 0, 4)
    for i in range(4):
        on = base + timedelta(minutes=i)
        off = on + timedelta(seconds=10)
        data = (f"Spot0.Pass1.Time.LaserOn={on.strftime('%Y/%m/%d %H:%M:%S.%f')};"
                f"Spot0.Pass1.Time.LaserOff={off.strftime('%Y/%m/%d %H:%M:%S.%f')}") if times else UNFIRED
        rows.append(scan_row(i, description=f"Spot {i + 1}", data=data))
    return write_lase(folder, rows)


def test_position_description_copy_and_metadata(tmp_path):
    log = read_lase(write_lase(tmp_path, [
        scan_row(i, description="Spot 11 (Copy)" if i == 1 else f"Spot {i + 11}",
                 data=firing(minute=i)) for i in range(4)]))
    acqs = acquisitions(log)
    tie = tie_laser_log(log, acqs)
    assert tie.rows["Synthetic_2"].status == "linked"
    assert tie.rows["Synthetic_2"].scan_position == 2
    assert tie.rows["Synthetic_2"].description_number is None
    assert tie_laser_log(log, acqs, spot_key="description").counts["linked"] == 0
    acqs[0].spot_number = 11
    assert tie_laser_log(log, acqs, spot_key="description").rows[acqs[0].label].status == "linked"
    # Rich's metadata number overrides a conflicting label-derived number.
    from UPbRPy.qtegra import read_rich_csv
    path = tmp_path / "synthetic_rich.csv"
    start = (log.scans[0].firings[0].laser_on + timedelta(hours=1)).isoformat()
    path.write_text(
        ",,,,Sample 1,\n,,,,Synthetic_99,\n"
        "MainRuns,0,29Si,Y,1,\nMainRuns,0,29Si,Time,0,\n"
        f"StartTime,,,,{start},\nPMIAnalyteLaser,Laser Scan Position Number,,,1,\n")
    session = read_rich_csv(path)
    assert session.acquisitions[0].spot_number == 1
    assert any("differs from label spot number 99" in w for w in session.warnings)
    assert tie_laser_log(log, session).rows["Synthetic_99"].scan_position == 1


def test_offset_mad_doubtful_unlinked_and_sha(tmp_path):
    log = timed_log(tmp_path)
    acqs = acquisitions(log)
    acqs += [replace(acqs[0], label="Synthetic_99", spot_number=99),
             replace(acqs[0], label="Synthetic_missing", start=None)]
    tie = tie_laser_log(log, acqs)
    assert tie.clock_offset_s == 3600.5 and tie.clock_offset_spread_s == 1.
    assert tie.pairs == 4 and tie.counts == {"linked": 3, "doubtful": 1, "unlinked": 2}
    assert acqs[3].label not in tie.windows()
    assert tie.windows()[acqs[0].label] == (.5, 10.5)
    assert tie.sha256 == hashlib.sha256(log.path.read_bytes()).hexdigest()
    row = tie.rows[acqs[0].label]
    assert row.duration == 10. and row.stage_um == (1000., 2000., 300.)
    assert row.fluence_j_cm2 == 2.2 and row.shot_count == 100
    with pytest.raises(FrozenInstanceError):
        tie.pairs = 0
    assert LaserTie.from_dict(json.loads(json.dumps(tie.to_dict()))) == tie


def test_aware_and_naive_acquisitions_match(tmp_path):
    log = timed_log(tmp_path)
    acqs = acquisitions(log)
    aware = [replace(a, start=a.start.replace(tzinfo=timezone.utc).astimezone(
        timezone(timedelta(hours=-7)))) for a in acqs]
    assert tie_laser_log(log, aware) == tie_laser_log(log, acqs)


def test_no_times_requires_explicit_confirmation(tmp_path):
    log = read_lase(write_lase(tmp_path, [scan_row(data=UNFIRED)]))
    a = Acquisition(label="Synthetic_1", sequence_index=1, channels=(),
                    y=np.empty((0, 0)), time=None, spot_number=1)
    tie = tie_laser_log(log, [a])
    assert tie.requires_confirmation and tie.warnings
    assert tie.clock_offset_s is None and tie.clock_offset_spread_s is None
    assert tie.pairs == 0 and tie.counts["linked"] == 1
    assert tie.windows() == {a.label: (None, None)}
    experiment = pd.DataFrame({"spot_names": [a.label], "spot_starting_times": [0.]})
    before = experiment.copy()
    with pytest.raises(ValueError, match="Confirm"):
        tie.apply(experiment)
    tie.apply(experiment, confirmed=True)
    pd.testing.assert_frame_equal(experiment, before)


@pytest.mark.parametrize("scan_type", ["Line", "Spot"])
def test_multiple_firings_choose_closest_unused(tmp_path, scan_type):
    log = timed_log(tmp_path)
    first = log.scans[0]
    extra = replace(first.firings[0], firing_index=1,
                    laser_on=first.firings[0].laser_on + timedelta(seconds=10),
                    laser_off=first.firings[0].laser_off + timedelta(seconds=10))
    log.scans = (replace(first, scan_type=scan_type, firings=(extra, *first.firings)), *log.scans[1:])
    acqs = acquisitions(log, offsets=(3590, 3600, 3600, 3600))
    # acquisitions() uses the first firing (the extra), so first start is original+3600.
    tie = tie_laser_log(log, acqs)
    assert tie.pairs == 3 and tie.clock_offset_s == 3600
    assert tie.rows[acqs[0].label].laser_on == 0
    repeated = replace(acqs[0], label="Repeat_1", sequence_index=5,
                       start=acqs[0].start + timedelta(seconds=10))
    tie = tie_laser_log(log, [*acqs, repeated])
    assert tie.rows[repeated.label].status == "linked"
    assert tie.rows[repeated.label].laser_on == 0


def test_ambiguous_firings_without_clock_and_missing_times(tmp_path):
    log = read_lase(write_lase(tmp_path, [scan_row(data=firing() + ";" + firing(1, minute=1)),
                                                    scan_row(1, data=UNFIRED)]))
    on = log.scans[0].firings[0].laser_on
    a = Acquisition(label="Synthetic_1", sequence_index=1, channels=(),
                    y=np.empty((0, 0)), time=None, spot_number=1, start=on)
    tie = tie_laser_log(log, [a, replace(a, label="Synthetic_2", spot_number=2)])
    assert tie.pairs == 0 and tie.warnings
    assert tie.counts == {"linked": 0, "doubtful": 1, "unlinked": 1}


def test_end_to_end_equivalent_manifest_and_script(tmp_path):
    bundle = write_session_bundle(tmp_path, upbr=False, laser=True)
    path = session_log(bundle)
    manifest = bundle / "session_manifest.toml"
    manifest.write_text(manifest.read_text().replace("clock_offset_s = 3600.0", "clock_offset_s = 3596.0"))
    baseline, _, _ = read_experiment_data(bundle, primary_channel="44Ca")
    tie, experiment = read_session_with_laser(bundle, path, primary_channel="44Ca")
    assert tie.counts["linked"] == 4
    pd.testing.assert_series_equal(experiment.laser_on_time, baseline.laser_on_time)
    pd.testing.assert_series_equal(experiment.laser_off_time, baseline.laser_off_time)
    assert experiment.attrs["laser"]["source"] == "lase"
    assert not any("stage" in c for c in experiment)
    assert "stage_um" not in json.dumps(experiment.attrs)


@pytest.mark.parametrize("key, threshold", [("auto", 30), ("position", float("nan")), ("position", 0)])
def test_invalid_options(tmp_path, key, threshold):
    with pytest.raises(ValueError):
        tie_laser_log(timed_log(tmp_path), [], spot_key=key, threshold_s=threshold)
