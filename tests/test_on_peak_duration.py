"""Nominal on-peak duration keeps auto-picked plateaus out of the washout.

Studio's "Nominal on-peak" field sets signal_duration_limit_seconds.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from UPbRPy.io import select_signal_regions


SWEEP = 0.43


def washout_trace(seed=3, laser_on=10.0, onset=12.0, total=40.0):
    """A 44Ca-like spot: baseline, a flat 1e7 cps plateau, exponential washout."""
    rng = np.random.default_rng(seed)
    times = np.arange(0.0, total, SWEEP)
    values = np.full(times.size, 3.0e4)
    on = (times >= onset) & (times < onset + laser_on)
    values[on] = 1.2e7
    tail = times >= onset + laser_on
    values[tail] = 3.0e4 + 1.2e7 * np.exp(-(times[tail] - onset - laser_on) / 0.45)
    values *= rng.normal(1.0, 0.02, times.size)
    return times, values


APATITE = dict(
    method="threshold",
    baseline_sweep_n=15,
    peak_sweep_n=55,
    threshold=0.2,
    filter_percentile=90,
    baseline_offset=4,
    plateau_offset=2,
    plateau_width_mode="signal",
)


def plateau_values(times, values, selection):
    return values[selection.plateau_start:selection.plateau_end]


def test_signal_width_mode_runs_into_the_washout_without_a_duration():
    times, values = washout_trace()
    selection = select_signal_regions(values, time=times, **APATITE)
    inside = plateau_values(times, values, selection)
    # The documented cause: the plateau ends at the last sweep above
    # baseline + 5 % of the elevation, so falling washout sweeps are kept.
    assert inside.min() < 0.5 * np.median(inside)
    assert times[selection.plateau_end - 1] >= 22.0


@pytest.mark.parametrize("width_mode", ["signal", "sweeps", "duration"])
def test_on_peak_duration_caps_plateau_before_the_washout(width_mode):
    times, values = washout_trace()
    options = {**APATITE, "plateau_width_mode": width_mode}
    if width_mode == "duration":
        options["plateau_duration_seconds"] = 60.0
    selection = select_signal_regions(values, time=times, signal_duration_limit_seconds=9.5, **options)
    start = times[selection.signal_start_index]
    assert times[selection.plateau_end - 1] < start + 9.5
    assert abs(times[selection.plateau_end] - (start + 9.5)) <= SWEEP / 2 + 1e-9
    inside = plateau_values(times, values, selection)
    assert inside.min() > 0.8 * np.median(inside)
    assert selection.diagnostics["signal_duration_limit_applied"] == 1


def test_shorter_detected_signal_is_kept():
    times, values = washout_trace(laser_on=6.0)
    reference = select_signal_regions(values, time=times, **APATITE)
    capped = select_signal_regions(values, time=times, signal_duration_limit_seconds=20.0, **APATITE)
    assert capped.plateau_end == reference.plateau_end
    assert capped.signal_end == reference.signal_end
    assert capped.diagnostics["signal_duration_limit_applied"] == 0


def test_default_is_unchanged_and_detected_duration_is_reported():
    times, values = washout_trace()
    default = select_signal_regions(values, time=times, **APATITE)
    explicit = select_signal_regions(values, time=times, signal_duration_limit_seconds=None, **APATITE)
    assert (default.plateau_start, default.plateau_end, default.signal_end) == (
        explicit.plateau_start, explicit.plateau_end, explicit.signal_end)
    assert default.diagnostics["signal_duration_limit_applied"] == 0
    assert default.diagnostics["on_peak_detected_seconds"] == pytest.approx(10.0, abs=2 * SWEEP)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_invalid_durations_are_rejected(bad):
    times, values = washout_trace()
    with pytest.raises(ValueError, match="signal_duration_limit_seconds"):
        select_signal_regions(values, time=times, signal_duration_limit_seconds=bad, **APATITE)


def test_duration_needs_times():
    _times, values = washout_trace()
    with pytest.raises(ValueError, match="time values"):
        select_signal_regions(values, signal_duration_limit_seconds=9.5, **APATITE)


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
RICH = FIXTURE / "Zircon_04Nov26_slice_Rich.csv"


def test_engine_reader_and_services_pass_the_option_through():
    from GUI import services

    session = services.load_session(str(RICH))
    assert "selection_falling_edge_index" in session.experiment
    summary = services.detected_on_peak_summary(session)
    assert summary is not None and summary["median"] > 0
    spot = str(session.experiment["spot_names"].iloc[0])
    before = services.get_selection_regions(session, spot)["plateau"]
    duration = 0.5 * summary["median"]
    session.import_settings["signal_duration_limit_seconds"] = duration
    services.autopick_selection_regions(session, spot)
    after = services.get_selection_regions(session, spot)["plateau"]
    signal = services.get_selection_regions(session, spot)["signal"]
    assert after[1] < before[1]
    assert after[1] <= signal[0] + duration + 1.0


def test_batch_repick_skips_manual_edits_and_scopes_to_spots():
    from GUI import services

    session = services.load_session(str(RICH))
    names = session.experiment["spot_names"].astype(str).tolist()
    edited, other = names[0], names[1]
    start, end = services.get_selection_regions(session, edited)["plateau"]
    services.update_selection_region(session, edited, plateau=(start, end - 0.5), manual=True)
    assert services.manual_selection_spots(session) == {edited}
    summary = services.detected_on_peak_summary(session)
    session.import_settings["signal_duration_limit_seconds"] = 0.8 * summary["median"]
    result = services.autopick_all_selection_regions(session, overwrite_manual=False)
    assert result["skipped_manual"] == [edited]
    assert services.get_selection_regions(session, edited)["plateau"] == (start, end - 0.5)
    scoped = services.autopick_all_selection_regions(session, [other], overwrite_manual=True)
    assert scoped["selected_count"] + len(scoped["failures"]) == 1
    everything = services.autopick_all_selection_regions(session, overwrite_manual=True)
    assert everything["skipped_manual"] == []
    assert edited not in everything["failures"]
    assert services.manual_selection_spots(session) == set()


def test_legacy_projects_infer_manual_edits():
    from GUI import services

    session = services.load_session(str(RICH))
    auto = session.experiment.copy()
    restored = session.experiment.copy()
    restored.loc[restored.index[2], "plateau_end_times"] -= 0.5
    flags = services.infer_manual_selections(auto, restored)
    assert flags.tolist() == [index == restored.index[2] for index in restored.index]
    assert isinstance(flags, pd.Series)
