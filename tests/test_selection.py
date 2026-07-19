import numpy as np
import pandas as pd
import pytest

from GUI.services import autopick_selection_regions
from GUI.state import ReductionSession
from UPbRPy.io import auto_select_baseline_peak
from UPbRPy.selection import (
    SelectionError,
    available_selection_methods,
    register_selection_method,
    select_baseline_peak,
    select_signal_regions,
)


def stepped_trace(onset=12, size=45):
    baseline = np.resize(np.array([10.0, 11.0, 9.0, 10.0]), onset)
    return np.r_[baseline, np.full(size - onset, 100.0)]


def pulsed_trace(onset=12, signal_end=31, size=45):
    values = np.resize(np.array([10.0, 11.0, 9.0, 10.0]), size)
    values[onset : signal_end + 1] = 100.0
    return values


def test_builtin_selection_methods_are_discoverable():
    assert available_selection_methods() == (
        "threshold",
        "rolling_sigma",
        "excel_expanding_sigma",
    )


def test_threshold_method_preserves_legacy_tuple_api():
    result = select_signal_regions(
        stepped_trace(),
        method="threshold",
        baseline_sweep_n=10,
        peak_sweep_n=20,
        threshold=0.2,
        filter_percentile=70,
        baseline_offset=2,
        plateau_offset=2,
    )

    assert result.detection_index == 12
    assert result.as_tuple() == (14, 34, 0, 10)
    assert select_baseline_peak(
        stepped_trace(), 10, 20, 0.2, 70, 2, 2
    ) == result.as_tuple()


def test_signal_start_recovers_a_subtle_leading_ramp_before_threshold_onset():
    values = stepped_trace()
    values[12] = 25.0
    values[13:] = 100.0

    result = select_signal_regions(
        values,
        method="threshold",
        threshold=0.5,
        filter_percentile=70,
    )

    assert result.detection_index == 13
    assert result.signal_start == 12
    assert result.diagnostics["signal_start_state"] == "leading_ramp_recovered"
    # Conventional selection windows remain anchored to the primary detector.
    assert result.plateau_start == 15


def test_rolling_sigma_detects_signal_from_preceding_running_baseline():
    result = select_signal_regions(
        stepped_trace(),
        method="rolling_sigma",
        baseline_sweep_n=10,
        peak_sweep_n=20,
        baseline_offset=2,
        plateau_offset=2,
        rolling_window=5,
        sigma_threshold=5,
        consecutive=2,
    )

    assert result.detection_index == 12
    assert result.as_tuple() == (14, 34, 0, 10)
    assert result.diagnostics["rolling_window"] == 5
    assert result.diagnostics["sigma_threshold"] == 5


def test_rolling_sigma_consecutive_hits_reject_single_sweep_spike():
    values = np.resize(np.array([10.0, 11.0, 9.0, 10.0]), 45)
    values[12] = 100.0
    values[20:] = 100.0

    result = select_signal_regions(
        values,
        method="running_5sigma",
        baseline_sweep_n=10,
        peak_sweep_n=15,
        rolling_window=5,
        sigma_threshold=5,
        consecutive=2,
    )

    assert result.method == "rolling_sigma"
    assert result.detection_index == 20


def test_selection_interpolates_missing_sweeps_before_detection():
    values = stepped_trace()
    values[[3, 8]] = np.nan

    result = select_signal_regions(
        values,
        method="rolling_sigma",
        rolling_window=5,
        sigma_threshold=5,
    )

    assert result.detection_index == 12


def test_rolling_sigma_reports_trace_without_a_detectable_signal():
    with pytest.raises(SelectionError, match="No signal onset"):
        select_signal_regions(
            np.ones(40),
            method="rolling_sigma",
            rolling_window=5,
            sigma_threshold=5,
        )


def test_excel_vba_method_uses_expanding_six_sweep_reference_and_eight_sigma():
    values = stepped_trace(onset=12)

    result = select_signal_regions(values, method="excel_vba")

    assert result.method == "excel_expanding_sigma"
    assert result.detection_index == 12
    assert result.diagnostics["initial_window"] == 6
    assert result.diagnostics["sigma_threshold"] == 8
    np.testing.assert_allclose(
        result.diagnostics["expanding_mean"],
        np.mean(values[:12]),
    )
    np.testing.assert_allclose(
        result.diagnostics["expanding_std"],
        np.std(values[:12], ddof=1),
    )


def test_sigma_threshold_is_positive_configurable_and_defaults_to_eight():
    values = np.r_[np.resize([10.0, 11.0, 9.0, 10.0], 12), np.full(33, 15.0)]

    with pytest.raises(SelectionError, match="No signal onset"):
        select_signal_regions(values, method="excel_expanding_sigma")
    selected = select_signal_regions(
        values,
        method="excel_expanding_sigma",
        sigma_threshold=4,
    )

    assert selected.signal_start == 12
    assert selected.diagnostics["sigma_threshold"] == 4
    with pytest.raises(ValueError, match="positive"):
        select_signal_regions(
            values,
            method="excel_expanding_sigma",
            sigma_threshold=0,
        )


def test_signal_end_is_last_elevated_sweep_and_clamps_plateau_tail():
    selected = select_signal_regions(
        pulsed_trace(signal_end=22),
        method="excel_expanding_sigma",
        peak_sweep_n=20,
    )

    assert selected.signal_start == 12
    assert selected.signal_end == 22
    assert selected.washout_start == 23
    assert selected.post_baseline_start == 23
    assert selected.plateau_start == 14
    assert selected.plateau_end == 22
    assert selected.diagnostics["signal_end_state"] == "washout_baseline_detected"


def test_signal_end_retains_falling_tail_until_stable_washout_baseline():
    values = pulsed_trace(signal_end=22)
    values[23:26] = [70.0, 40.0, 20.0]

    selected = select_signal_regions(
        values,
        method="excel_expanding_sigma",
        peak_sweep_n=20,
    )

    assert selected.diagnostics["signal_end_falling_edge_start"] == 23
    assert selected.signal_end == 25
    assert selected.washout_start == 26
    assert selected.diagnostics["signal_end_state"] == "washout_baseline_detected"


def test_signal_end_excludes_baseline_like_tail_before_stable_recovery():
    values = pulsed_trace(signal_end=22)
    values[23:32] = [70.0, 40.0, 20.0, 10.5, 12.0, 5.0, 10.0, 11.0, 9.0]

    selected = select_signal_regions(
        values,
        method="excel_expanding_sigma",
        peak_sweep_n=20,
    )

    assert selected.diagnostics["signal_end_falling_edge_start"] == 23
    assert selected.signal_end == 25
    assert selected.washout_start == 26
    assert selected.diagnostics["signal_end_stable_baseline_start"] == 29
    assert values[selected.signal_end] > selected.diagnostics[
        "signal_end_washout_retention_threshold"
    ]
    assert values[selected.washout_start] <= selected.diagnostics[
        "signal_end_washout_retention_threshold"
    ]


def test_optional_signal_duration_limit_caps_free_pick_in_elapsed_seconds():
    values = pulsed_trace(signal_end=31)
    times = np.arange(values.size, dtype=float) * 0.4

    unrestricted = select_signal_regions(
        values,
        time=times,
        method="excel_expanding_sigma",
        peak_sweep_n=20,
    )
    limited = select_signal_regions(
        values,
        time=times,
        method="excel_expanding_sigma",
        peak_sweep_n=20,
        signal_duration_limit_seconds=3.1,
    )

    expected_end = int(np.argmin(np.abs(times - (times[limited.signal_start] + 3.1))))
    assert unrestricted.signal_end == 31
    assert limited.signal_end == expected_end == 20
    assert limited.diagnostics["signal_end_unrestricted_index"] == 31
    assert limited.diagnostics["signal_duration_limit_applied"] == 1
    assert limited.plateau_end == limited.signal_end


def test_signal_duration_limit_defaults_to_none_and_requires_timestamps():
    values = pulsed_trace(signal_end=31)
    unrestricted = select_signal_regions(
        values,
        method="excel_expanding_sigma",
    )

    assert unrestricted.signal_end == 31
    assert unrestricted.diagnostics["signal_duration_limit_seconds"] == "none"
    with pytest.raises(ValueError, match="time values are required"):
        select_signal_regions(
            values,
            method="excel_expanding_sigma",
            signal_duration_limit_seconds=9,
        )


def test_custom_detector_can_be_registered_without_changing_io_code():
    def fixed_detector(values, **options):
        del values, options
        return 15, {"source": "test"}

    register_selection_method("fixed_test", fixed_detector)
    result = select_signal_regions(stepped_trace(), method="fixed_test")

    assert result.detection_index == 15
    assert result.diagnostics["source"] == "test"


def test_experiment_autopick_uses_each_spots_positional_index():
    trace = stepped_trace(size=45)
    total_data = pd.concat(
        [
            pd.DataFrame({"Time": np.arange(45.0), "spot_name": "A_1", "238U": trace}),
            pd.DataFrame({"Time": np.arange(100.0, 145.0), "spot_name": "B_1", "238U": trace}),
        ],
        ignore_index=True,
    )
    experiment = pd.DataFrame({"spot_names": ["A_1", "B_1"]})

    selected = auto_select_baseline_peak(
        experiment,
        total_data,
        "238U",
        selection_method="rolling_sigma",
        selection_options={"rolling_window": 5, "sigma_threshold": 5, "consecutive": 2},
    )

    assert selected["selection_method"].tolist() == ["rolling_sigma", "rolling_sigma"]
    assert selected["plateau_start_times"].tolist() == [14.0, 114.0]
    assert selected["baseline_end_times"].tolist() == [10.0, 110.0]
    assert selected["signal_start_times"].tolist() == [12.0, 112.0]
    assert selected["signal_end_times"].tolist() == [44.0, 144.0]


def test_gui_autopick_uses_shared_selection_method():
    trace = stepped_trace(size=45)
    total_data = pd.DataFrame(
        {"Time": np.arange(45.0), "spot_name": "A_1", "238U": trace}
    )
    experiment = pd.DataFrame(
        {
            "spot_names": ["A_1"],
            "baseline_start_times": [0.0],
            "baseline_end_times": [10.0],
            "plateau_start_times": [14.0],
            "plateau_end_times": [34.0],
        }
    )
    session = ReductionSession(
        experiment=experiment,
        total_data=total_data,
        channels=["238U"],
        primary_channel="238U",
        import_settings={
            "primary_channel": "238U",
            "selection_method": "rolling_sigma",
            "selection_options": {
                "rolling_window": 5,
                "sigma_threshold": 5,
                "consecutive": 2,
            },
            "baseline_sweep_n": 10,
            "peak_sweep_n": 20,
            "baseline_offset": 2,
            "plateau_offset": 2,
        },
    )

    regions = autopick_selection_regions(session, "A_1")

    assert regions == {"baseline": (0.0, 10.0), "plateau": (14.0, 34.0)}
    assert session.experiment.loc[0, "selection_method"] == "rolling_sigma"
