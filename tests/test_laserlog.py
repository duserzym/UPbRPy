"""Chromium logs generated from synthetic coordinates and local 2030 times."""

from dataclasses import FrozenInstanceError, fields
from datetime import datetime
import importlib

import pytest

from UPbRPy import laserlog


HEADER = (
    "Scan Type,Description,Selected,Lock Edit,Vertex Count,Vertex List,"
    "Preablation Settings,Ablation Settings,Data"
)
SETTINGS = (
    "Dosage=0;DwellTime=1.00;LineSpacing=0.00;Laser.Output=46.00;"
    "Laser.Fluence=2.20;LineScanMode=1;PassCount=1;Laser.RepRate=10.00;"
    "ScanSpeed=0.00;PassEnabled=1;ShotCount=100;SpotSpacing=0.00;"
    "Laser.SpotSize=25µm Circle;Laser.SpotRotation=0.00;ZDepth=0.00"
)
UNFIRED = "ExpectedLaseTime=10.0;ScanLength=0.0"


def firing(index=0, pass_index=1, *, minute=0, incomplete=False):
    """Make one synthetic firing's semicolon data, with optional missing keys."""
    prefix = f"Spot{index}.Pass{pass_index}."
    base = f"2030/01/02 10:{minute:02}:"
    values = {
        "Position.Start": "1000,2000,300",
        "Time.Start": base + "00.123",
        "Time.LaserOn": base + "01.456",
    }
    if not incomplete:
        values.update({
            "Time.LaserOff": base + "11.456",
            "Laser.LastOutputReading": "4.90",
            "Time.End": base + "12.000",
        })
    return ";".join(prefix + key + "=" + value for key, value in values.items())


def scan_row(
    index=0, *, description="Spot 1", scan_type="Spot",
    vertices="1000.00,2000.00,300.00", vertex_count=1,
    pre=SETTINGS, ablation=SETTINGS, data=None, selected="1", locked="0",
):
    """Quote the five text fields exactly as in a Chromium scan row."""
    if data is None:
        data = "SpotCount=1;" + firing() + ";" + UNFIRED

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    return f"Scan{index}=" + ",".join([
        scan_type, quote(description), selected, locked, str(vertex_count),
        quote(vertices), quote(pre), quote(ablation), quote(data),
    ])


def write_lase(tmp_path, rows=None, *, newline="\r\n", footer=True, extra=()):
    """Write generated ISO-8859-1 bytes, including a literal 0xB5 µ byte."""
    lines = ["[Scans]", "Header=" + HEADER, *(rows if rows is not None else [scan_row()])]
    lines.extend([
        "[GasControl:Helium]", "Flow=0.80", "[GasControl:Argon]", "Flow=1.00",
        "[Events:All]", "Enabled=1", "[Events:Pass0.Event0]", "Action=Wait",
        "[Events:Pass1.Event0]", "Action=Lase", "[Laser.ExperimentSettings]",
        "StabilityMode=1", "HighVoltage=1000", "EnergySetpoint=2.20", *extra,
    ])
    if footer:
        lines.extend([
            "[SystemInfo]", "SystemType=Chromium", "SerialNumber=TEST-0000",
            "Version=0.0.0.0", "WindowsVersion=TEST",
        ])
    path = tmp_path / "synthetic.lase"
    path.write_bytes((newline.join(lines) + newline).encode("iso-8859-1"))
    return path


def test_fired_spot(tmp_path):
    path = write_lase(tmp_path)
    log = laserlog.read_lase(path)
    assert log.path == path
    assert log.scan_header == tuple(HEADER.split(","))
    assert not log.warnings and not log.truncated
    scan, = log.scans
    assert (scan.index, scan.scan_type, scan.description, scan.spot_number) == (0, "Spot", "Spot 1", 1)
    assert scan.line_number == 3
    assert scan.selected is True and scan.locked is False
    assert scan.vertices_um == ((1000.0, 2000.0, 300.0),)
    assert scan.declared_vertex_count == 1
    assert scan.declared_spot_count == 1
    assert scan.expected_lase_time_s == 10.0 and scan.scan_length_um == 0.0
    settings = scan.ablation
    assert scan.preablation == settings
    assert settings.output_pct == 46.0
    assert settings.fluence_j_cm2 == 2.2
    assert settings.rep_rate_hz == 10.0
    assert settings.shot_count == 100
    assert settings.spot_size_um == 25.0 and settings.spot_shape == "Circle"
    assert settings.scan_speed_um_s == 0.0
    assert settings.pass_enabled is True and settings.pass_count == 1
    assert settings.dwell_time_s == 1.0
    assert list(settings.raw) == [part.split("=")[0] for part in SETTINGS.split(";")]
    assert settings.raw["Laser.Output"] == "46.00"
    assert settings.key() == (2.2, 10.0, 100, 46.0, 25.0, "Circle", 0.0)
    assert hash(settings.key()) == hash(scan.preablation.key())
    shot, = scan.firings
    assert (shot.scan_index, shot.firing_index, shot.pass_index) == (0, 0, 1)
    assert shot.start == datetime(2030, 1, 2, 10, 0, 0, 123000)
    assert shot.laser_on == datetime(2030, 1, 2, 10, 0, 1, 456000)
    assert shot.laser_off == datetime(2030, 1, 2, 10, 0, 11, 456000)
    assert shot.end == datetime(2030, 1, 2, 10, 0, 12)
    assert shot.laser_on.tzinfo is None
    assert shot.last_output_reading == 4.9
    assert shot.start_position_um == (1000.0, 2000.0, 300.0)
    assert shot.raw["Time.Start"] == "2030/01/02 10:00:00.123"
    assert "Spot0.Pass1.Time.Start" in scan.data
    assert shot.complete and shot.laser_duration_s == 10.0
    assert scan.fired and scan.fired_count == 1
    assert log.fired_scans == (scan,) and log.unfired_scans == ()
    assert log.firings == (shot,)
    assert log.first_laser_on == log.last_laser_on == shot.laser_on
    assert log.scans_by_spot_number() == {1: [scan]}
    assert log.system_info["SerialNumber"] == "TEST-0000"
    assert log.experiment_settings["EnergySetpoint"] == "2.20"
    assert log.gas_control == {"Helium": {"Flow": "0.80"}, "Argon": {"Flow": "1.00"}}
    assert log.events == {"All": {"Enabled": "1"}, "Pass0.Event0": {"Action": "Wait"},
                          "Pass1.Event0": {"Action": "Lase"}}


def test_line_and_unfired_scan(tmp_path):
    path = write_lase(tmp_path, [scan_row(
        scan_type="Line", description="Line 12", vertex_count=2,
        vertices="1000.00,2000.00,300.00;1100.00,2100.00,300.00", data=UNFIRED,
    )])
    log = laserlog.read_lase(path)
    scan, = log.scans
    assert scan.scan_type == "Line" and scan.spot_number == 12
    assert scan.vertices_um == ((1000.0, 2000.0, 300.0), (1100.0, 2100.0, 300.0))
    assert scan.declared_vertex_count == 2
    assert scan.declared_spot_count is None
    assert not scan.fired and scan.fired_count == 0 and scan.firings == ()
    assert log.fired_scans == () and log.unfired_scans == (scan,)
    assert log.firings == () and log.first_laser_on is None and log.last_laser_on is None
    assert not log.warnings and not log.truncated


def test_multiple_incomplete_and_missing_laser_on(tmp_path):
    rows = [
        scan_row(3, data="SpotCount=2;" + firing(incomplete=True)),
        scan_row(1, data="SpotCount=2;" + firing(1, minute=1) + ";" + firing(0, minute=2)),
        scan_row(2, data="SpotCount=1;Spot0.Pass1.Time.Start=2030/01/02 10:00:00.123"),
    ]
    log = laserlog.read_lase(write_lase(tmp_path, rows))
    assert [scan.index for scan in log.scans] == [1, 2, 3]
    multi, missing, incomplete = log.scans
    assert [shot.firing_index for shot in multi.firings] == [0, 1]
    assert multi.declared_spot_count == multi.fired_count == 2
    assert incomplete.declared_spot_count == 2 and incomplete.fired_count == 1
    assert len(incomplete.firings) == 1  # Do not invent Spot1.
    shot, = incomplete.firings
    assert shot.start is not None and shot.laser_on is not None
    assert shot.end is shot.laser_off is shot.last_output_reading is None
    assert not shot.complete and shot.laser_duration_s is None
    assert not missing.fired and missing.fired_count == 0
    assert [(f.scan_index, f.firing_index) for f in log.firings] == [(3, 0), (1, 1), (1, 0), (2, 0)]
    assert log.first_laser_on == shot.laser_on
    assert log.last_laser_on == multi.firings[0].laser_on
    assert not log.warnings


def test_pass_indices_and_ties_are_preserved(tmp_path):
    data = "SpotCount=1;" + firing(pass_index=1) + ";" + firing(pass_index=0)
    log = laserlog.read_lase(write_lase(tmp_path, [scan_row(data=data)]))
    assert [(f.firing_index, f.pass_index) for f in log.firings] == [(0, 0), (0, 1)]
    assert log.scans[0].fired_count == 2


@pytest.mark.parametrize("bad", [
    'Scan2=Spot,"bad",1,0,1',
    scan_row(2, vertices="invalid,2000.00,300.00"),
    scan_row(2, vertices="1000.00,2000.00"),
    scan_row(2).replace('"Spot 1"', '"Spot 1"trailing'),
    scan_row(2).replace('"Spot 1"', 'Spot" 1'),
    scan_row(2)[:-1],
])
def test_malformed_scan_is_skipped_with_source_line(tmp_path, bad):
    log = laserlog.read_lase(write_lase(tmp_path, [scan_row(0), bad, scan_row(4)]))
    assert [scan.index for scan in log.scans] == [0, 4]
    warning, = log.warnings
    assert warning.line_number == 4
    assert "Scan2" in warning.message
    assert not log.truncated


@pytest.mark.parametrize("partial", [scan_row(2)[:-1], 'Scan2=Spot,"Spot 3",1', "Scan2"])
def test_partial_final_scan_sets_truncated_even_with_system_info(tmp_path, partial):
    path = write_lase(tmp_path)
    path.write_bytes(path.read_bytes() + b"[Scans]\r\n" + partial.encode("iso-8859-1"))
    log = laserlog.read_lase(path)
    assert [scan.index for scan in log.scans] == [0]
    assert log.truncated
    warning, = log.warnings
    assert warning.line_number == len(path.read_bytes().splitlines())


def test_missing_system_info_and_optional_final_newline(tmp_path):
    path = write_lase(tmp_path, footer=False)
    log = laserlog.read_lase(path)
    assert len(log.scans) == 1 and log.truncated
    assert any(w.line_number is None and "SystemInfo" in w.message for w in log.warnings)
    path = write_lase(tmp_path)
    path.write_bytes(path.read_bytes().rstrip(b"\r\n"))
    log = laserlog.read_lase(path)
    assert len(log.scans) == 1 and not log.truncated and not log.warnings


def test_duplicate_indices_keep_first(tmp_path):
    rows = [scan_row(5, description="Spot 7"), scan_row(5, description="Spot 99"), scan_row(1)]
    log = laserlog.read_lase(write_lase(tmp_path, rows))
    assert [scan.index for scan in log.scans] == [1, 5]
    assert log.scans[1].description == "Spot 7"
    warning, = log.warnings
    assert warning.line_number == 4 and "Duplicate Scan5" in warning.message


def test_duplicate_partial_final_row_still_marks_truncation(tmp_path):
    path = write_lase(tmp_path)
    path.write_bytes(path.read_bytes() + b"[Scans]\n" + scan_row(0)[:-1].encode("iso-8859-1"))
    log = laserlog.read_lase(path)
    assert len(log.scans) == 1 and log.truncated
    assert any("Duplicate Scan0" in warning.message for warning in log.warnings)


def test_copy_descriptions_and_spot_groups(tmp_path):
    descriptions = ["Spot 7", "Spot 7 (Copy)", "Spot 7 (Copy) (Copy)", "Spot 7", "Line 12"]
    log = laserlog.read_lase(write_lase(tmp_path, [
        scan_row(i, description=description) for i, description in enumerate(descriptions)
    ]))
    assert [s.spot_number for s in log.scans] == [7, None, None, 7, 12]
    assert log.scans_by_spot_number() == {7: [log.scans[0], log.scans[3]], 12: [log.scans[4]]}


def test_unknown_keys_and_sections_and_quoted_punctuation(tmp_path):
    path = write_lase(tmp_path, [scan_row(
        description='Synthetic, "quoted"; copy',
        ablation=SETTINGS + ";FutureSetting=  preserved=value  ",
        data=firing() + ";FutureData=raw=value;Spot0.Pass1.FutureFiringKey= unchanged ",
    ), "FutureScanKey=kept"], extra=["[Unknown:Section]", "FutureKey=value=kept"])
    log = laserlog.read_lase(path)
    scan, = log.scans
    assert scan.description == 'Synthetic, "quoted"; copy'
    assert scan.ablation.raw["FutureSetting"] == "  preserved=value  "
    assert scan.data["FutureData"] == "raw=value"
    assert scan.firings[0].raw["FutureFiringKey"] == " unchanged "
    assert log.other_sections == {"Unknown:Section": {"FutureKey": "value=kept"},
                                  "Scans": {"FutureScanKey": "kept"}}
    assert not log.warnings


@pytest.mark.parametrize("newline", ["\r\n", "\r", "\n"])
@pytest.mark.parametrize("encoding", ["iso-8859-1", "utf-8", "utf-8-sig"])
def test_encodings_line_endings_and_memory_parsing(tmp_path, newline, encoding):
    path = write_lase(tmp_path, newline=newline)
    original = path.read_bytes()
    assert b"25\xb5m Circle" in original
    path.write_bytes(original.decode("iso-8859-1").encode(encoding))
    log = laserlog.read_lase(path)
    assert log.scans[0].ablation.raw["Laser.SpotSize"] == "25µm Circle"
    assert log.scans[0].ablation.spot_size_um == 25.0
    assert laserlog.parse_lase(path.read_bytes(), path=path) == log
    assert laserlog.parse_lase(path.read_text(encoding=encoding), path=str(path)) == log
    assert laserlog.parse_lase(path.read_bytes()).path is None


@pytest.mark.parametrize("character", ["\x85", "\x0c"])
@pytest.mark.parametrize("newline", ["\r\n", "\r", "\n"])
def test_description_controls_preserve_scan_and_source_lines(tmp_path, character, newline):
    description = f"Synthetic{character}description"
    path = write_lase(tmp_path, [
        scan_row(description=description), 'Scan1=Spot,"bad",1,0,1',
    ], newline=newline)
    assert character.encode("iso-8859-1") in path.read_bytes()
    assert laserlog.detect_format(path) == "lase"
    log = laserlog.read_lase(path)
    scan, = log.scans
    assert scan.description == description and scan.line_number == 3
    warning, = log.warnings
    assert warning.line_number == 4 and "Scan1" in warning.message
    assert not log.truncated
    assert laserlog.parse_lase(path.read_bytes(), path=path) == log
    assert laserlog.parse_lase(path.read_bytes().decode("iso-8859-1"), path=path) == log


@pytest.mark.parametrize("separator", ["\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\u2028", "\u2029"])
def test_signature_requires_cr_or_lf(tmp_path, separator):
    content = f"[Scans]{separator}Header={HEADER}\n".encode("utf-8")
    path = tmp_path / "synthetic.lase"
    path.write_bytes(content)
    for reader in (laserlog.detect_format, laserlog.read_lase):
        with pytest.raises(laserlog.UnsupportedLaserLogFormat):
            reader(path)
    with pytest.raises(laserlog.UnsupportedLaserLogFormat):
        laserlog.parse_lase(content)


def test_um_spelling_and_absent_settings(tmp_path):
    scan, = laserlog.read_lase(write_lase(tmp_path, [scan_row(
        ablation="Laser.SpotSize=20um Circle;PassEnabled=0", pre="",
    )])).scans
    assert scan.ablation.spot_size_um == 20.0 and scan.ablation.spot_shape == "Circle"
    assert scan.ablation.pass_enabled is False
    assert scan.preablation.raw == {}
    assert scan.preablation.key() == (None,) * 7
    assert all(getattr(scan.preablation, f.name) is None
               for f in fields(scan.preablation) if f.name != "raw")


@pytest.mark.parametrize(("key", "bad", "attribute"), [
    ("Laser.Output", "bad", "output_pct"),
    ("Laser.Fluence", "NaN", "fluence_j_cm2"),
    ("Laser.RepRate", "inf", "rep_rate_hz"),
    ("ShotCount", "1.5", "shot_count"),
    ("Laser.SpotSize", "bad", "spot_size_um"),
    ("ScanSpeed", "bad", "scan_speed_um_s"),
    ("PassEnabled", "yes", "pass_enabled"),
    ("PassCount", "bad", "pass_count"),
    ("DwellTime", "", "dwell_time_s"),
])
def test_unparseable_settings_warn_and_keep_raw(tmp_path, key, bad, attribute):
    log = laserlog.read_lase(write_lase(tmp_path, [scan_row(ablation=f"{key}={bad}")]))
    assert getattr(log.scans[0].ablation, attribute) is None
    assert log.scans[0].ablation.raw[key] == bad
    warning, = log.warnings
    assert warning.line_number == 3 and key in warning.message


@pytest.mark.parametrize(("key", "attribute"), [
    ("Time.Start", "start"), ("Time.LaserOn", "laser_on"),
    ("Time.LaserOff", "laser_off"), ("Time.End", "end"),
    ("Position.Start", "start_position_um"), ("Laser.LastOutputReading", "last_output_reading"),
])
def test_unparseable_firing_fields_warn_and_keep_raw(tmp_path, key, attribute):
    log = laserlog.read_lase(write_lase(tmp_path, [scan_row(data=f"Spot0.Pass1.{key}=bad")]))
    shot, = log.scans[0].firings
    assert getattr(shot, attribute) is None and shot.raw[key] == "bad"
    warning, = log.warnings
    assert warning.line_number == 3 and f"Spot0.Pass1.{key}" in warning.message


def test_unparseable_scan_values_and_data_numbers(tmp_path):
    log = laserlog.read_lase(write_lase(tmp_path, [scan_row(
        vertex_count="bad", selected="bad", locked="bad",
        data="SpotCount=bad;ExpectedLaseTime=bad;ScanLength=bad",
    )]))
    scan, = log.scans
    assert scan.declared_vertex_count is scan.selected is scan.locked is None
    assert scan.declared_spot_count is scan.expected_lase_time_s is scan.scan_length_um is None
    assert scan.data["SpotCount"] == "bad"
    assert len(log.warnings) == 6
    for key in ("Vertex Count", "Selected", "Lock Edit", "SpotCount", "ExpectedLaseTime", "ScanLength"):
        assert any(w.line_number == 3 and key in w.message for w in log.warnings)


def test_distinct_settings_count_all_scans_in_index_order(tmp_path):
    rows = [scan_row(2, ablation="Laser.Fluence=3.0"), scan_row(0), scan_row(1, data=UNFIRED),
            scan_row(3, ablation=SETTINGS + ";Unrelated=kept")]
    log = laserlog.read_lase(write_lase(tmp_path, rows))
    assert log.distinct_ablation_settings() == [(log.scans[0].ablation, 3), (log.scans[2].ablation, 1)]


@pytest.mark.parametrize("prefix", [b"", b"\r\n \t", b"\xef\xbb\xbf  \r\n"])
def test_detection_and_line_numbers_with_leading_whitespace(tmp_path, prefix):
    path = write_lase(tmp_path)
    path.write_bytes(prefix + path.read_bytes())
    assert laserlog.detect_format(path) == "lase"
    assert laserlog.read_lase(path).scans[0].line_number == 3 + prefix.count(b"\n")


@pytest.mark.parametrize("content", [
    b",,,,Sample 1,\r\n,,,,Synthetic_1,\r\nMainRuns,0,238U,Y,100,\r\n",
    b"random text\n", b"[Scans]\nHeader=Wrong,Header\n", b"", b"[Other]\nHeader=Scan Type,\n",
])
def test_detection_and_read_reject_non_chromium(tmp_path, content):
    path = tmp_path / "synthetic.lase"
    path.write_bytes(content)
    for reader in (laserlog.detect_format, laserlog.read_lase):
        with pytest.raises(laserlog.UnsupportedLaserLogFormat):
            reader(path)
    with pytest.raises(laserlog.UnsupportedLaserLogFormat):
        laserlog.parse_lase(content)


def test_no_placed_scans(tmp_path):
    log = laserlog.read_lase(write_lase(tmp_path, []))
    assert log.scans == log.firings == log.fired_scans == log.unfired_scans == ()
    assert log.first_laser_on is log.last_laser_on is None
    assert log.distinct_ablation_settings() == [] and log.scans_by_spot_number() == {}
    assert not log.warnings and not log.truncated


def test_imports_and_frozen_records(tmp_path):
    import UPbRPy.laserlog

    assert UPbRPy.laserlog is laserlog is importlib.import_module("UPbRPy.laserlog")
    assert "laserlog" in UPbRPy.FOCUSED_SUBMODULES
    assert set(laserlog.__all__) == {
        "LaserSettings", "LaserFiring", "LaserScan", "LaserLogWarning", "LaserLog",
        "UnsupportedLaserLogFormat", "read_lase", "parse_lase", "detect_format",
    }
    log = laserlog.read_lase(write_lase(tmp_path))
    scan, = log.scans
    for obj in (scan, scan.ablation, scan.firings[0], laserlog.LaserLogWarning(3, "test")):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, fields(obj)[0].name, None)
