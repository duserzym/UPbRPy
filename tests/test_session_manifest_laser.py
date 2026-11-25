"""Synthetic version-2 laser metadata and version-1 compatibility."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from pathlib import Path
import tomllib

import pytest

from UPbRPy import session_manifest as module
from UPbRPy.session_manifest import (
    AcquisitionLaser,
    SessionAcquisition,
    SessionLaserLog,
    SessionLaserSetting,
    SessionManifest,
    parse_session_manifest,
    read_session_manifest,
)


SHA = "ab" * 32
LOG = f'''
[[laser_log]]
id = "00000000-0000-7000-8000-000000000001"
file_name = "synthetic.lase"
sha256 = "{SHA}"
system_type = "Chromium"
serial_number = "DEMO-0001"
software_version = "0.0.0.0"
time_zone = "America/Boise"
laser_instrument = "Synthetic laser"
spot_key = "position"
has_laser_times = true
clock_offset_s = 3600.0
clock_offset_spread_s = 0.5
offset_pairs = 12
linked = 12
'''
SETTING = f'''
[[laser_setting]]
log_sha256 = "{SHA}"
fluence_j_cm2 = 2.5
rep_rate_hz = 10.0
spot_um = 20.0
spot_shape = "Circle"
shot_count = 100
output_pct = 25.0
scan_speed_um_s = 5.0
acquisitions = 12
'''
ACQUISITION = f'''
[[acquisition]]
label = "SYNTHETIC_1"
started_at = 2000-01-02T01:00:00Z
laser_log_sha256 = "{SHA}"
laser_scan_position = 42
laser_on_at = 2000-01-01T17:00:02-07:00
laser_off_at = "2000-01-02T00:00:12+00:00"
laser_fluence_j_cm2 = 2.5
laser_rep_rate_hz = 10.0
laser_spot_um = 20.0
laser_spot_shape = "Circle"
laser_shot_count = 100
laser_output_pct = 25.0
laser_stage_um = [1000.0, 2000.0, 300.0]
laser_link_method = "spot_number"
'''
V2 = 'format = "upbdb-session-bundle"\nformat_version = 2\n' + LOG + SETTING + ACQUISITION


def parse(text):
    return parse_session_manifest(tomllib.loads(text), Path("synthetic.toml"))


def test_full_v2_parse(tmp_path):
    path = tmp_path / "session_manifest.toml"
    path.write_text(V2, encoding="utf-8")
    manifest = read_session_manifest(path)
    assert manifest.format_version == 2
    assert manifest.warnings == ()
    assert manifest.has_laser_data
    assert manifest.laser_logs == (SessionLaserLog(
        id="00000000-0000-7000-8000-000000000001",
        file_name="synthetic.lase", sha256=SHA, system_type="Chromium",
        serial_number="DEMO-0001", software_version="0.0.0.0",
        time_zone="America/Boise", laser_instrument="Synthetic laser",
        spot_key="position", has_laser_times=True, clock_offset_s=3600.0,
        clock_offset_spread_s=0.5, offset_pairs=12, linked=12,
    ),)
    assert manifest.laser_settings == (SessionLaserSetting(
        log_sha256=SHA, fluence_j_cm2=2.5, rep_rate_hz=10.0, spot_um=20.0,
        output_pct=25.0, scan_speed_um_s=5.0, spot_shape="Circle",
        shot_count=100, acquisitions=12,
    ),)
    row = manifest.acquisition("SYNTHETIC_1")
    assert row.started_at == "2000-01-02T01:00:00+00:00"
    assert row.laser == AcquisitionLaser(
        log_sha256=SHA, scan_position=42,
        laser_on_at=datetime(2000, 1, 2, 0, 0, 2, tzinfo=timezone.utc),
        laser_off_at=datetime(2000, 1, 2, 0, 0, 12, tzinfo=timezone.utc),
        fluence_j_cm2=2.5, rep_rate_hz=10.0, spot_um=20.0, output_pct=25.0,
        spot_shape="Circle", shot_count=100, stage_um=(1000.0, 2000.0, 300.0),
        link_method="spot_number",
    )
    assert row.laser.duration_s == 10.0
    assert manifest.laser_log(SHA) is manifest.laser_logs[0]
    assert manifest.laser_log(SHA.upper()) is manifest.laser_logs[0]
    assert manifest.laser_log("cd" * 32) is None
    assert manifest.laser_log("") is None
    assert manifest.acquisition_laser_window("SYNTHETIC_1") == (2.0, 12.0)


@pytest.mark.parametrize("version", ["", "format_version = 1\n"])
def test_v1_defaults_and_non_laser_equality(version):
    text = version + '''
[session]
id = "synthetic-session"
key = "Synthetic_session"
sep = "_"
material = "apatite"
instrument = "Synthetic ICP-MS"
method = "Synthetic method"
operator = "Synthetic operator"
[samples]
SYN = "SYNTHETIC"
[reference_materials]
primary_68 = "SyntheticRM"
[[acquisition]]
label = "SYN_1"
acquisition_id = 123
spot_id = "synthetic-spot"
grain_id = "synthetic-grain"
sample_id = "synthetic-sample"
started_at = 2000-01-02T01:00:00
sha256 = "original hash text"
kind = "sample"
filename = "synthetic.csv"
sample_label = "SYNTHETIC"
atlas_spot_id = "synthetic-atlas-spot"
'''
    old = parse(text)
    with_log = parse(text + LOG)
    assert old == replace(with_log, laser_logs=())
    assert old.format_version == 1
    assert old.laser_logs == old.laser_settings == old.warnings == ()
    assert not old.has_laser_data
    assert old.acquisitions == (SessionAcquisition(
        label="SYN_1", acquisition_id="123", spot_id="synthetic-spot",
        grain_id="synthetic-grain", sample_id="synthetic-sample",
        started_at="2000-01-02T01:00:00", sha256="original hash text",
        kind="sample", filename="synthetic.csv", sample_label="SYNTHETIC",
        atlas_spot_id="synthetic-atlas-spot",
    ),)
    assert old.acquisition_laser_window("SYN_1") is None


def test_log_without_times_and_missing_metadata_defaults():
    manifest = parse(f'''
format_version = 2
[[laser_log]]
sha256 = "{SHA}"
has_laser_times = false
offset_pairs = 0
[[laser_setting]]
log_sha256 = "{SHA}"
[[acquisition]]
label = "SYNTHETIC_1"
laser_log_sha256 = "{SHA}"
laser_spot_um = 20
laser_stage_um = [1, 2, 3]
''')
    assert manifest.laser_logs == (SessionLaserLog(sha256=SHA),)
    assert manifest.laser_settings == (SessionLaserSetting(log_sha256=SHA),)
    laser = manifest.acquisitions[0].laser
    assert laser == AcquisitionLaser(log_sha256=SHA, spot_um=20.0, stage_um=(1., 2., 3.))
    assert type(laser.spot_um) is float
    assert all(type(coordinate) is float for coordinate in laser.stage_um)
    assert laser.duration_s is None
    assert manifest.acquisition_laser_window("SYNTHETIC_1") is None
    assert manifest.warnings == ()


@pytest.mark.parametrize("on", [
    "2000-01-02T00:00:02", '"2000-01-02T00:00:02"',
    '"2000-01-02T02:00:02+02:00"',
])
def test_partial_firing_and_naive_or_string_times(on):
    manifest = parse(LOG + f'''
[[acquisition]]
label = "SYNTHETIC_1"
started_at = "2000-01-02T01:00:00"
laser_log_sha256 = "{SHA}"
laser_on_at = {on}
''')
    laser = manifest.acquisitions[0].laser
    assert laser.laser_on_at == datetime(2000, 1, 2, 0, 0, 2, tzinfo=timezone.utc)
    assert laser.laser_on_at.tzinfo is timezone.utc
    assert laser.laser_off_at is None
    assert laser.duration_s is None
    assert manifest.acquisition_laser_window("SYNTHETIC_1") == (2., None)


@pytest.mark.parametrize("start", [
    '"2000-01-02T01:00:00"', '"2000-01-01T23:00:00-02:00"',
])
def test_window_uses_acquisition_clock_and_start_offset(start):
    text = V2.replace("started_at = 2000-01-02T01:00:00Z", f"started_at = {start}")
    manifest = parse(text)
    assert manifest.acquisition_laser_window("SYNTHETIC_1") == (2., 12.)
    # Zero is a recorded offset, not missing; negative relative times are allowed.
    zero_offset = parse(text.replace("clock_offset_s = 3600.0", "clock_offset_s = 0"))
    assert zero_offset.acquisition_laser_window("SYNTHETIC_1") == (-3598., -3588.)


@pytest.mark.parametrize("old, new", [
    ("started_at = 2000-01-02T01:00:00Z", ""),
    ("started_at = 2000-01-02T01:00:00Z", 'started_at = "invalid"'),
    ("clock_offset_s = 3600.0", ""),
    (f'laser_log_sha256 = "{SHA}"', f'laser_log_sha256 = "{"cd" * 32}"'),
    ("laser_on_at = 2000-01-01T17:00:02-07:00", ""),
])
def test_window_missing_required_metadata(old, new):
    manifest = parse(V2.replace(old, new))
    assert manifest.acquisition_laser_window("SYNTHETIC_1") is None
    assert manifest.acquisition_laser_window("missing") is None


@pytest.mark.parametrize("key, bad, field, default", [
    ("laser_log_sha256", '"not hex"', "log_sha256", ""),
    ("laser_scan_position", "true", "scan_position", None),
    ("laser_on_at", '"not a datetime"', "laser_on_at", None),
    ("laser_off_at", "2000-01-02", "laser_off_at", None),
    ("laser_fluence_j_cm2", '"2.5"', "fluence_j_cm2", None),
    ("laser_rep_rate_hz", "true", "rep_rate_hz", None),
    ("laser_spot_um", "nan", "spot_um", None),
    ("laser_output_pct", "inf", "output_pct", None),
    ("laser_spot_shape", "42", "spot_shape", ""),
    ("laser_shot_count", "1.5", "shot_count", None),
    ("laser_stage_um", "[1.0, 2.0]", "stage_um", None),
    ("laser_stage_um", "[1.0, true, 3.0]", "stage_um", None),
    ("laser_stage_um", '[1.0, "2.0", 3.0]', "stage_um", None),
    ("laser_link_method", "false", "link_method", ""),
])
def test_malformed_acquisition_value_is_dropped_with_context(key, bad, field, default):
    manifest = parse(f'[[acquisition]]\nlabel = "SYNTHETIC_1"\n{key} = {bad}\n')
    assert getattr(manifest.acquisitions[0].laser, field) == default
    assert len(manifest.warnings) == 1
    assert "SYNTHETIC_1" in manifest.warnings[0]
    assert key in manifest.warnings[0]


def test_malformed_log_and_setting_values_are_local():
    manifest = parse('''
laser_log = ["not a table", {sha256 = "zz", linked = true, file_name = 4, has_laser_times = "false", clock_offset_s = "3600", clock_offset_spread_s = false, offset_pairs = 1.5}]
laser_setting = [false, {log_sha256 = "ab", fluence_j_cm2 = "2.5", rep_rate_hz = true, spot_um = [], output_pct = inf, scan_speed_um_s = {}, shot_count = true, spot_shape = 3, acquisitions = 2.5}]
''')
    assert manifest.laser_logs == (SessionLaserLog(),)
    assert manifest.laser_settings == (SessionLaserSetting(),)
    for table, keys in (
        ("laser_log", ["sha256", "linked", "file_name", "has_laser_times",
                       "clock_offset_s", "clock_offset_spread_s", "offset_pairs"]),
        ("laser_setting", ["log_sha256", "fluence_j_cm2", "rep_rate_hz",
                           "spot_um", "output_pct", "scan_speed_um_s",
                           "shot_count", "spot_shape", "acquisitions"]),
    ):
        assert any(f"{table} row 1" in warning for warning in manifest.warnings)
        for key in keys:
            assert any(f"{table} row 2" in warning and f"invalid {key};" in warning
                       for warning in manifest.warnings)
    # Invalid hashes cannot accidentally join by their empty-string defaults.
    assert manifest.laser_log("") is None


def test_malformed_table_containers_do_not_stop_loading():
    manifest = parse('laser_log = 3\nlaser_setting = "wrong"\n')
    assert manifest.laser_logs == manifest.laser_settings == ()
    assert len(manifest.warnings) == 2
    assert "laser_log" in manifest.warnings[0]
    assert "laser_setting" in manifest.warnings[1]


def test_unknown_metadata_ignored_and_future_versions_accepted():
    text = '''
format_version = 999
unknown_top_level = "ignored"
[laser]
unknown_table = true
[[acquisition]]
label = "SYNTHETIC_1"
unknown_key = 1
laser_unknown_key = "ignored"
'''
    manifest = parse(text)
    assert manifest == SessionManifest(
        path=Path("synthetic.toml"), format_version=999,
        acquisitions=(SessionAcquisition("SYNTHETIC_1"),),
    )
    known = parse(V2)
    extra = parse(V2 + 'unknown_key = []\n' + LOG + 'unknown_key = {}\n' + SETTING + 'unknown_key = false\n')
    assert replace(extra, laser_logs=extra.laser_logs[:1],
                   laser_settings=extra.laser_settings[:1]) == known


@pytest.mark.parametrize("model", [SessionLaserLog, SessionLaserSetting, AcquisitionLaser])
def test_new_models_are_frozen_and_exported(model):
    assert model.__name__ in module.__all__
    value = model()
    with pytest.raises(FrozenInstanceError):
        setattr(value, fields(model)[0].name, "changed")


def test_acquisition_metadata_alone_counts_as_laser_data():
    manifest = parse('[[acquisition]]\nlabel = "SYNTHETIC_1"\nlaser_spot_um = 20\n')
    assert manifest.has_laser_data
    assert not parse(SETTING).has_laser_data


def test_format_version_is_provenance_not_content():
    """UPbDB's no-log v2 bundle must compare equal to its v1 counterpart."""
    v1 = parse_session_manifest(
        tomllib.loads('format = "upbdb-session-bundle"\nformat_version = 1\n[session]\nkey = "K"\n'),
        "session_manifest.toml",
    )
    v2 = parse_session_manifest(
        tomllib.loads('format = "upbdb-session-bundle"\nformat_version = 2\n[session]\nkey = "K"\n'),
        "session_manifest.toml",
    )
    assert (v1.format_version, v2.format_version) == (1, 2)
    assert v1 == v2
