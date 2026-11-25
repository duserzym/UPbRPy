"""Place laser windows on the session axis and compare measured onsets.

This module accepts relative windows or a parsed session manifest. It does not
read files and never adds stage coordinates to analytical tables.
"""

from collections.abc import Mapping

import numpy as np
import pandas as pd


def add_laser_timing(
    experiment: pd.DataFrame,
    windows: Mapping[str, tuple[float | None, float | None]],
    acquisition_starts: Mapping[str, float],
    *,
    scan_positions: Mapping[str, int] | None = None,
    source: str = "manifest",
    log_sha256: tuple[str, ...] = (),
    spot_key: str = "",
) -> None:
    """Add relative firing windows to acquisition starts in session seconds.

    ``windows`` contains seconds after each acquisition's own start, already
    corrected to the acquisition clock. ``acquisition_starts`` gives those
    starts on the same session axis as total_data.Time. Do nothing when no
    finite endpoint is available, including logs containing settings only.
    """
    if not any(
        value is not None and np.isfinite(value)
        for window in windows.values() for value in window
    ):
        return
    labels = experiment["spot_names"]
    for column, endpoint in (("laser_on_time", 0), ("laser_off_time", 1)):
        experiment[column] = [
            float(acquisition_starts[label] + windows[label][endpoint])
            if label in windows and label in acquisition_starts
            and windows[label][endpoint] is not None else np.nan
            for label in labels
        ]
    experiment["laser_duration_s"] = experiment.laser_off_time - experiment.laser_on_time
    experiment["laser_scan_position"] = labels.map(scan_positions or {}).astype(float).where(
        experiment.laser_on_time.notna() | experiment.laser_off_time.notna()
    )
    experiment.attrs["laser"] = {
        "transport_delay_s": None,
        "n_pairs": 0,
        "qc_threshold_s": 2.0,
        "log_sha256": list(log_sha256),
        "source": source,
        "spot_key": spot_key,
    }


def add_manifest_laser_timing(experiment, manifest):
    """Add bundle windows without re-reading sweeps or changing selections."""
    if manifest is None or not manifest.has_laser_data:
        return
    windows = {
        row.label: window for row in manifest.acquisitions
        if (window := manifest.acquisition_laser_window(row.label)) is not None
    }
    # The manifest convenience method requires an on-time. Preserve an
    # off-only incomplete firing for display, although it cannot guide a
    # selection or contribute a transport-delay pair.
    for row in manifest.acquisitions:
        laser = row.laser
        if row.label in windows or laser is None or laser.laser_off_at is None:
            continue
        log = manifest.laser_log(laser.log_sha256)
        if log is None or log.clock_offset_s is None:
            continue
        try:
            start = pd.Timestamp(row.started_at)
            if pd.isna(start):
                continue
            start = start.tz_localize('UTC') if start.tzinfo is None else start.tz_convert('UTC')
            off = (pd.Timestamp(laser.laser_off_at) - start).total_seconds() + log.clock_offset_s
        except (ValueError, OverflowError):
            continue
        windows[row.label] = (None, off)
    # Keep labels aligned when Studio restores manifest timing after a rename.
    renames = experiment.attrs.get("spot_renames", {})
    scan_positions = {row.label: row.laser.scan_position for row in manifest.acquisitions
                      if row.laser is not None and row.laser.scan_position is not None}
    add_laser_timing(
        experiment, {label: windows[renames.get(label, label)] for label in experiment.spot_names
                     if renames.get(label, label) in windows},
        dict(zip(experiment.spot_names, experiment.spot_starting_times)),
        scan_positions={label: scan_positions.get(renames.get(label, label))
                        for label in experiment.spot_names},
        log_sha256=tuple(log.sha256 for log in manifest.laser_logs if log.sha256),
        spot_key=",".join(dict.fromkeys(log.spot_key for log in manifest.laser_logs if log.spot_key)),
    )


def _delay_pairs(experiment: pd.DataFrame) -> pd.Series:
    """Return only successful, independently detected onset differences."""
    if "laser_on_time" not in experiment:
        return pd.Series(dtype=float)
    if not {"laser_detected_onset_time", "signal_start_times"}.intersection(experiment):
        return pd.Series(dtype=float)
    detected = pd.to_numeric(experiment.get(
        "laser_detected_onset_time", experiment.get("signal_start_times"),
    ), errors="coerce")
    # A reference onset is populated only for a successful data-only pick.
    # Its validity does not depend on whether the later guided pick succeeds.
    success = (
        detected.notna() if "laser_detected_onset_time" in experiment
        else experiment["selection_error"].isna()
    )
    if "laser_detected_onset_time" not in experiment:
        success &= experiment["selection_method"].ne("laser_guided")
    pairs = detected - experiment["laser_on_time"]
    return pairs[success & np.isfinite(pairs)]


def estimate_transport_delay(experiment: pd.DataFrame) -> float | None:
    """Return median measured onset minus laser-on, requiring three pairs."""
    pairs = _delay_pairs(experiment)
    return float(pairs.median()) if len(pairs) >= 3 else None


def update_laser_qc(experiment: pd.DataFrame, *, threshold_s: float = 2.0) -> None:
    """Refresh delay and informational residuals without changing selections."""
    if "laser" not in experiment.attrs:
        return
    threshold_s = float(threshold_s)
    if not np.isfinite(threshold_s) or threshold_s < 0:
        raise ValueError("laser_qc_threshold_s must be finite and nonnegative.")
    pairs = _delay_pairs(experiment)
    delay = estimate_transport_delay(experiment)
    experiment.attrs["laser"].update(
        transport_delay_s=delay, n_pairs=len(pairs), qc_threshold_s=threshold_s,
    )
    detected = pd.to_numeric(experiment["laser_detected_onset_time"], errors="coerce")
    experiment["laser_onset_residual_s"] = (
        detected - experiment["laser_on_time"] - delay if delay is not None else np.nan
    )
    experiment["laser_onset_flag"] = experiment["laser_onset_residual_s"].abs().gt(threshold_s)


def laser_selection_options(
    spot: Mapping,
    transport_delay_s: float | None = None,
    *,
    signal_duration_limit_source: str = "fixed",
    signal_duration_limit_seconds: float | None = None,
) -> dict:
    """Return per-spot keywords for select/autopick_signal_regions.

    Studio can pass one experiment row and the session delay when re-picking.
    Missing durations use the fixed limit; missing delay uses zero and is
    reported by the laser detector. Times remain on the supplied trace axis.
    """
    if signal_duration_limit_source not in {"fixed", "laser"}:
        raise ValueError("signal_duration_limit_source must be 'fixed' or 'laser'.")
    duration = spot.get("laser_duration_s")
    if (signal_duration_limit_source == "laser" and duration is not None
            and np.isfinite(duration) and duration > 0):
        signal_duration_limit_seconds = float(duration)
    delay = 0.0 if transport_delay_s is None else float(transport_delay_s)
    result = {"signal_duration_limit_seconds": signal_duration_limit_seconds}
    for key in ("on", "off"):
        value = spot.get(f"laser_{key}_time")
        result[f"laser_predicted_{key}_time"] = (
            float(value + delay) if value is not None and np.isfinite(value) else None
        )
    result["laser_delay_assumed_zero"] = int(transport_delay_s is None)
    return result


def nondefault_laser_settings(settings: Mapping) -> dict:
    """Omit new default settings when serializing existing projects."""
    result = dict(settings)
    for name, default in (
        ("laser_qc_threshold_s", 2.0), ("signal_duration_limit_source", "fixed"),
    ):
        if result.get(name) == default:
            result.pop(name)
    if isinstance(result.get("selection_options"), Mapping):
        nested = result["selection_options"]
        cleaned = nondefault_laser_settings(nested)
        if nested and not cleaned:
            # A container introduced solely for the new defaults must not
            # change an older project's serialized import settings either.
            result.pop("selection_options")
        else:
            result["selection_options"] = cleaned
    return result


__all__ = [
    "add_laser_timing", "add_manifest_laser_timing", "estimate_transport_delay", "update_laser_qc",
    "laser_selection_options", "nondefault_laser_settings",
]
