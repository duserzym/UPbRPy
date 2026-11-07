"""Spot identity and scales in Studio's diagnostics."""

import numpy as np
import pandas as pd
import pytest

from GUI import services
from GUI.state import ReductionSession
from GUI.workflows import get_workflow_preset
from UPbRPy.selection import select_signal_regions


def test_rutile_auto_width_follows_time_at_different_sweep_rates():
    preset = get_workflow_preset("rutile")
    options = dict(preset.read_kwargs)
    options["method"] = options.pop("selection_method")
    options.update(options.pop("selection_options"))
    for interval in (0.2, 0.42):
        time = np.arange(0, 40, interval)
        cps = np.where((time >= 12) & (time < 32), 1000, 10)
        selection = select_signal_regions(cps, time=time, **options)
        assert time[selection.plateau_end] - time[selection.plateau_start] == pytest.approx(20, abs=1.5)
        assert time[selection.plateau_end] < 33


def test_calibration_scatter_uses_active_glass_names_and_element_concentrations(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from GUI.theme import THEMES, build_application
    from GUI.widgets import CalibrationSlopesWidget

    archive = pd.DataFrame({"29Si": [310_000, 340_000], "153Eu": [400., 40.]}, index=["Glass1", "Glass2"])
    monkeypatch.setattr(services, "current_standard_archive", lambda: archive)
    session = ReductionSession()
    session.reduction_cache["normalized_plateau_data"] = pd.DataFrame({
        "spot_name": ["Glass1_3", "Glass1_3", "Glass2_8", "unknown"],
        "sample": ["Glass1", "Glass1", "Glass2", "unknown"],
        "29Si": [155, 155, 170, 200],
        "151Eu": [195, 205, 20, 8],
    })
    session.reduction_cache["concentration_results"] = {
        "calibration": pd.Series({"29Si": 2000., "151Eu": 2.}),
        "active_standards": ("Glass1", "Glass2"),
    }
    data = services.get_calibration_slope_data(session, get_workflow_preset("rutile"))
    np.testing.assert_array_equal(data["channel_data"]["151Eu"]["ppm"], [400., 40.])
    np.testing.assert_array_equal(data["channel_data"]["151Eu"]["cps"], [200., 20.])
    app = QApplication.instance() or build_application([], "light")
    plot = CalibrationSlopesWidget()
    plot.show_data(data, THEMES["light"])
    lines = {line.name(): (line, points) for line, points in plot._scatter_for_line.items()}
    line, scatter = lines["151Eu"]
    assert max(line.getData()[0]) == pytest.approx(420.)
    spot = scatter.points()[0]
    assert scatter.opts["tip"](400., 200., spot.data()).startswith("Glass1_3 · Glass1 · 151Eu")
    assert scatter.zValue() > line.zValue()
    plot.set_channel("151Eu")
    assert len(plot._scatter_for_line) == 1
    plot.close()
    app.processEvents()


def test_trace_hover_spot_intervals_leave_gaps_unlabelled():
    from GUI.widgets import _spot_at_time, _spot_time_spans

    spans = _spot_time_spans(pd.DataFrame({
        "Time": [10., 11., 13., 14.],
        "spot_name": ["r1", "r1", "r2", "r2"],
    }))
    assert [_spot_at_time(spans, time) for time in (9., 10.5, 12., 13.5)] == ["", "r1", "", "r2"]


def test_reopening_identical_reference_library_preserves_cached_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "_active_standard_archive", pd.DataFrame())
    monkeypatch.setattr(services, "_active_standard_archive_path", "")
    path = tmp_path / "standards.csv"
    path.write_text("sample,238U\nGlass1,400\n")
    archive = services.load_standard_archive_from_path(str(path))
    assert services.load_standard_archive_from_path(str(path)) is archive
    path.write_text("sample,238U\nGlass1,410\n")
    updated = services.load_standard_archive_from_path(str(path))
    assert updated is not archive
    assert updated.loc["Glass1", "238U"] == 410
