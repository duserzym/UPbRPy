"""Signal/on-peak coupling uses local measured sweeps and compound history."""

import numpy as np
import pandas as pd
import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI.selection_windows import EDGE_NAMES, constrain_window_edit
from GUI.widgets import TracePlotWidget
from UPbRPy import io
from test_selection_live_edits import app, windows, regions, wait_for


TIMES = np.arange(20, dtype=float) * .5
REGIONS = {"baseline": (0., .5), "signal": (1., 7.), "plateau": (2., 5.)}


def test_new_import_pick_has_exclusive_boundary_grab_gap():
    trace = np.r_[np.resize([10., 11., 9., 10.], 12), np.full(11, 1000.), np.full(17, 10.)]
    frames = [pd.DataFrame({"Time": np.arange(40.) + offset, "spot_name": name, "238U": trace})
              for name, offset in (("A_1", 0), ("B_1", 100))]
    selected = io.auto_select_baseline_peak(
        pd.DataFrame({"spot_names": ["A_1", "B_1"]}), pd.concat(frames), "238U",
        selection_method="excel_expanding_sigma", plateau_offset=0)
    padded = io.autopick_signal_regions(trace, method="excel_expanding_sigma", plateau_offset=0)
    assert padded.washout_start > padded.signal_end
    for row in selected.itertuples():
        assert row.signal_start_times == row.plateau_start_times - 1
        # q is excluded by plateau [p:q]; inclusive signal end e is q + 1.
        assert row.signal_end_times == row.plateau_end_times + 1
    assert selected.signal_end_times.iloc[0] < 40


def test_new_pick_padding_clamps_to_acquisition(monkeypatch):
    detected = io.SelectionResult(0, 4, 0, 1, 0, "test", 4, signal_start_index=0)
    monkeypatch.setattr(io, "select_signal_regions", lambda *a, **k: detected)
    assert io.autopick_signal_regions(np.ones(5)).signal_start == 0
    assert io.autopick_signal_regions(np.ones(5)).signal_end == 4


@pytest.fixture
def plot(app):
    widget = TracePlotWidget()
    widget.show_spot(pd.DataFrame({"Time": TIMES, "238U": np.arange(20.) + 10}),
                     ["238U"], {"238U": "#123456"}, REGIONS, editable_regions=True)
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def assert_order(result, times=TIMES):
    s, e = result["signal"]
    p, q = result["plateau"]
    assert times[0] <= s <= p <= q <= e <= times[-1]
    indices = [int(np.argmin(abs(times - t))) for t in (p, q)]
    assert indices[1] - indices[0] >= min(3, len(times) - 1)
    assert all(t in times for t in (s, p, q, e))


@pytest.mark.parametrize("edge", EDGE_NAMES)
@pytest.mark.parametrize("target", [0., 9.5])
def test_each_drag_direction_snaps_pushes_and_stops(plot, edge, target):
    spy = QtTest.QSignalSpy(plot.regionChanged)
    line = plot._window_lines()[edge]
    line.setValue(target + .01)
    actual = plot.current_regions()
    expected_indices = {
        ("signal_start", 0.): (0, 4, 10, 14),
        ("signal_start", 9.5): (7, 7, 10, 14),
        ("plateau_start", 0.): (0, 0, 10, 14),
        ("plateau_start", 9.5): (2, 7, 10, 14),
        ("plateau_end", 0.): (2, 4, 7, 14),
        ("plateau_end", 9.5): (2, 4, 19, 19),
        ("signal_end", 0.): (2, 4, 7, 7),
        ("signal_end", 9.5): (2, 4, 10, 19),
    }
    s, p, q, e = TIMES[list(expected_indices[edge, target])]
    assert actual == {"baseline": REGIONS["baseline"], "signal": (s, e), "plateau": (p, q)}
    assert_order(actual)
    assert spy.count() == 0
    assert plot._drag_readout.isVisible()
    assert "sweep" in plot._drag_readout.toPlainText()
    assert edge in plot._drag_pens
    line.sigPositionChangeFinished.emit(line)
    assert spy.count() == 1
    assert not plot._drag_readout.isVisible()
    assert not plot._drag_pens


@pytest.mark.parametrize("edge,target,pushed", [
    ("plateau_start", .5, "signal_start"), ("plateau_end", 8., "signal_end"),
    ("signal_start", 3., "plateau_start"), ("signal_end", 4., "plateau_end")])
def test_pushed_edges_are_highlighted(plot, edge, target, pushed):
    plot._window_lines()[edge].setValue(target)
    assert pushed in plot._drag_pens
    assert plot._window_lines()[pushed].pen.widthF() == 4


def test_whole_plateau_drag_pushes_and_stays_in_spot(plot):
    item = plot._region_overlays["plateau"]["region"]
    with QtCore.QSignalBlocker(item):
        item.setRegion((7., 10.))
    item.sigRegionChanged.emit(item)
    assert plot.current_regions()["plateau"] == (6.5, 9.5)
    assert plot.current_regions()["signal"][1] == 9.5
    assert_order(plot.current_regions())


def test_release_applies_final_body_position_even_without_move_signal(plot):
    item = plot._region_overlays["plateau"]["region"]
    spy = QtTest.QSignalSpy(plot.regionChanged)
    with QtCore.QSignalBlocker(item):
        item.setRegion((7., 10.))
    item.sigRegionChangeFinished.emit(item)
    assert plot.current_regions()["plateau"] == (6.5, 9.5)
    assert plot.current_regions()["signal"][1] == 9.5
    assert spy.count() == 1
    assert not plot._drag_readout.isVisible()


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_short_spot_minimum(n):
    times = np.arange(float(n))
    r = {"plateau": (0., float(n - 1)), "signal": (0., float(n - 1))}
    for edge in EDGE_NAMES:
        assert_order(constrain_window_edit(times, r, edge, 999), times)


def test_live_drag_defers_stats_and_coalesces_frame(plot, monkeypatch):
    calls = []
    original = plot._update_region_overlay
    monkeypatch.setattr(plot, "_update_region_overlay", lambda name: (calls.append(name), original(name)))
    for value in np.linspace(2.1, 3., 30):
        plot._signal_boundaries["signal_start"].setValue(value)
    assert not calls
    assert plot._window_frame_timer.isActive()
    plot._signal_boundaries["signal_start"].sigPositionChangeFinished.emit(None)
    assert sorted(calls) == ["baseline", "plateau"]


def test_signal_push_is_pending_and_one_undo_restores_both(windows):
    window = windows()
    before = regions(window)
    times = window._spot_sweep_times()
    p, q = before["plateau"]
    target = float(times[np.argmin(abs(times - p)) + 1])
    line = window.trace_plot._signal_boundaries["signal_start"]
    line.setValue(target)
    plotted = window.trace_plot.current_regions()
    # The pushed on-peak start keeps one sweep of grab gap after the signal start.
    pushed = float(times[np.argmin(abs(times - target)) + 1])
    assert plotted["signal"][0] == target
    assert plotted["plateau"][0] == pushed
    assert not window.pending_region_updates
    assert window.sweep_fields["plateau_start"].value() == np.argmin(abs(times - pushed)) + 1
    line.sigPositionChangeFinished.emit(line)
    assert set(window.pending_region_updates[window.current_spot_name()]) == {"signal", "plateau"}
    window.flush_selection_edits()
    assert regions(window) == plotted
    assert len(window._selection_undo) == 1
    window.undo_selection_edit()
    assert regions(window) == before


@pytest.mark.parametrize("edge", EDGE_NAMES)
def test_typed_sweeps_and_nudges_share_constraints(windows, edge):
    window = windows()
    before = regions(window)
    times = window._spot_sweep_times()
    target = len(times) if edge.endswith("start") else 1
    expected = constrain_window_edit(times, before, edge, times[target - 1])
    field = window.sweep_fields[edge]
    field.lineEdit().setText(str(target))
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
    assert regions(window) == expected
    window.undo_selection_edit()
    assert regions(window) == before
    name, side = edge.rsplit("_", 1)
    window.set_active_edit_window(name)
    window.nudge_selection_edge(side, 1 if side == "start" else -1)
    index = np.argmin(abs(times - before[name][0 if side == "start" else 1]))
    expected = constrain_window_edit(times, before, edge, times[index + (1 if side == "start" else -1)])
    assert regions(window) == expected


def test_field_escape_tab_steps_and_workflow_disable(windows, app):
    window = windows()
    window.show()
    app.processEvents()
    before = regions(window)
    field = window.sweep_fields["plateau_start"]
    old = field.value()
    field.lineEdit().setText(str(old + 1))
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Escape)
    assert field.lineEdit().text() == str(old)
    assert regions(window) == before
    field.setFocus()
    field.lineEdit().setText(str(old + 1))
    # Tab commits through the focus-out it causes. Send that event directly:
    # offscreen window activation is unreliable once other tests ran.
    QtWidgets.QApplication.sendEvent(
        field, QtGui.QFocusEvent(QtCore.QEvent.Type.FocusOut, QtCore.Qt.FocusReason.TabFocusReason))
    app.processEvents()
    assert regions(window)["plateau"][0] > before["plateau"][0]
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)
    assert field.value() == old + 2
    field.stepDown()
    assert field.value() == old + 1
    window.workflow_step_list.setCurrentRow(2)
    assert all(not f.isEnabled() for f in window.sweep_fields.values())


def test_old_saved_project_keeps_touching_windows_bit_identical(windows, tmp_path):
    window = windows()
    spot = window.current_spot_name()
    before = regions(window)
    p, q = before["plateau"]
    # Legacy selections can have no gap, non-grid times and a manual flag.
    window.pending_region_updates[spot] = {"plateau": (p + .00123, q), "signal": (p + .00123, q)}
    window.flush_selection_edits()
    saved = regions(window)
    path = tmp_path / "legacy.upbr"
    window.save_project(path)
    restored = windows(path, project=True)
    assert regions(restored, spot) == saved
    assert restored.trace_plot.current_regions() == saved
    from GUI.services import manual_selection_spots
    assert spot in manual_selection_spots(restored.session)


def test_new_pick_signal_start_never_enters_baseline(monkeypatch):
    # Plateau starts right after the baseline [0, 5): the detected signal start
    # stays at 5 rather than moving back into baseline sweep 4.
    detected = io.SelectionResult(5, 10, 0, 5, 5, "test", 12, signal_start_index=5)
    monkeypatch.setattr(io, "select_signal_regions", lambda *a, **k: detected)
    picked = io.autopick_signal_regions(np.ones(20))
    assert picked.signal_start == 5
    assert picked.signal_end == 12
    gap = io.SelectionResult(8, 12, 0, 5, 6, "test", 12, signal_start_index=8)
    monkeypatch.setattr(io, "select_signal_regions", lambda *a, **k: gap)
    picked = io.autopick_signal_regions(np.ones(20))
    assert (picked.signal_start, picked.signal_end) == (7, 13)
