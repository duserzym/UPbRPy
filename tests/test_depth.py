"""Tests for pulse-count to physical-depth calibration."""

import numpy as np
import pandas as pd

from UPbRPy.depth import (
    LinearDepthCalibration,
    apply_depth_calibration,
    fit_linear_depth_calibration,
)
from UPbRPy.pipeline import PipelineConfig, pipeline_config_from_dict


def test_linear_depth_fit_recovers_drilling_rate_and_intercept():
    pulses = np.array([0.0, 100.0, 200.0, 300.0])
    depths = 0.25 + 0.0832 * pulses

    calibration = fit_linear_depth_calibration(
        pulses,
        depths,
        repetition_rate_hz=10.0,
    )

    assert np.isclose(calibration.intercept_um, 0.25)
    assert np.isclose(calibration.depth_per_pulse_um, 0.0832)
    assert calibration.pulse_range == (0.0, 300.0)


def test_depth_calibration_adds_midpoints_intervals_and_uncertainty():
    calibration = LinearDepthCalibration(
        depth_per_pulse_um=0.0832,
        repetition_rate_hz=10.0,
        intercept_um=0.0,
        coefficient_covariance=((0.01, 0.0), (0.0, 1e-8)),
        geometry_1s_um=0.2,
        pulse_range=(0.0, 30.0),
    )
    sweeps = pd.DataFrame(
        {
            "spot_name": ["a", "a", "a"],
            "signal_relative_time": [0.0, 1.0, 2.0],
        }
    )

    result = apply_depth_calibration(
        sweeps,
        calibration,
        signal_start_1s_seconds=0.05,
    )

    np.testing.assert_allclose(result["laser_pulse_count"], [0.0, 10.0, 20.0])
    np.testing.assert_allclose(result["depth_um"], [0.0, 0.832, 1.664])
    np.testing.assert_allclose(
        result["depth_interval_start_um"], [0.0, 0.416, 1.248]
    )
    np.testing.assert_allclose(
        result["depth_interval_end_um"], [0.416, 1.248, 2.08]
    )
    assert np.all(result["depth_1s_um"] > 0)
    assert not result["depth_calibration_extrapolated"].any()


def test_pipeline_config_round_trips_depth_calibration():
    calibration = LinearDepthCalibration(
        depth_per_pulse_um=0.0832,
        repetition_rate_hz=10.0,
        pulse_range=(0.0, 900.0),
    )
    config = PipelineConfig(depth_calibration=calibration)

    restored = pipeline_config_from_dict(config.to_dict())

    assert restored.depth_calibration == calibration
