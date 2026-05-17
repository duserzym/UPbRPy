from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from GUI.state import ReductionSession
from GUI.workflows import WorkflowPreset, format_import_defaults, infer_workflow_preset
from UPbRPy import (
    apply_DH_correction,
    apply_session_correction,
    calc_DH_fractionations_rad,
    calc_concentration,
    calc_concentration_calibration_factor,
    calc_raw_ratios,
    calc_session_fractionation_rad,
    internal_normalization,
    read_experiment_data,
    select_baseline_peak,
    standard_archive,
    subtract_baseline,
)


CHANNEL_PALETTE = [
    "#467489",
    "#a35d4e",
    "#9c8445",
    "#4f8474",
    "#7465a0",
    "#a25c79",
    "#5d8291",
    "#6177a2",
]


def default_visible_channels(channels: Iterable[str], primary_channel: str) -> list[str]:
    ordered = [str(channel) for channel in channels]
    if primary_channel in ordered:
        ordered = [primary_channel] + [channel for channel in ordered if channel != primary_channel]
    return ordered[: min(4, len(ordered))]


def build_channel_colors(channels: Iterable[str]) -> dict[str, str]:
    channel_list = [str(channel) for channel in channels]
    return {
        channel: CHANNEL_PALETTE[index % len(CHANNEL_PALETTE)]
        for index, channel in enumerate(channel_list)
    }


def load_session(
    directory: str,
    primary_channel: str | None = None,
    preset: WorkflowPreset | None = None,
) -> ReductionSession:
    workflow_preset = preset or infer_workflow_preset(directory)
    effective_primary = primary_channel or workflow_preset.primary_channel
    experiment, channels, total_data = read_experiment_data(
        directory,
        primary_channel=effective_primary,
        **workflow_preset.read_kwargs,
    )
    clean_channels = [str(channel) for channel in channels if str(channel)]
    resolved_primary = effective_primary if effective_primary in clean_channels else clean_channels[0]
    selected_spot = str(experiment["spot_names"].iloc[0]) if not experiment.empty else None
    return ReductionSession(
        directory=directory,
        experiment=experiment,
        total_data=total_data,
        channels=clean_channels,
        primary_channel=resolved_primary,
        workflow_key=workflow_preset.key,
        workflow_title=workflow_preset.title,
        selected_spot=selected_spot,
        visible_channels=default_visible_channels(clean_channels, resolved_primary),
        import_settings={"primary_channel": resolved_primary, **workflow_preset.read_kwargs},
        plot_mode="raw",
        reduction_cache={},
    )


def clear_reduction_cache(session: ReductionSession) -> None:
    session.reduction_cache.clear()


def update_selection_region(
    session: ReductionSession,
    spot_name: str,
    *,
    baseline: tuple[float, float] | None = None,
    plateau: tuple[float, float] | None = None,
) -> None:
    if not session.loaded:
        return
    if baseline is not None:
        start_time, end_time = sorted(float(value) for value in baseline)
        session.experiment.loc[session.experiment["spot_names"] == spot_name, "baseline_start_times"] = start_time
        session.experiment.loc[session.experiment["spot_names"] == spot_name, "baseline_end_times"] = end_time
    if plateau is not None:
        start_time, end_time = sorted(float(value) for value in plateau)
        session.experiment.loc[session.experiment["spot_names"] == spot_name, "plateau_start_times"] = start_time
        session.experiment.loc[session.experiment["spot_names"] == spot_name, "plateau_end_times"] = end_time
    clear_reduction_cache(session)


def autopick_selection_regions(session: ReductionSession, spot_name: str) -> dict[str, tuple[float, float] | None]:
    if not session.loaded:
        return {"baseline": None, "plateau": None}
    spot_data = get_spot_data(session, spot_name)
    if spot_data.empty:
        return {"baseline": None, "plateau": None}

    selection_channel = "29Si" if "NIST" in spot_name and "29Si" in spot_data else str(
        session.import_settings.get("primary_channel", session.primary_channel)
    )
    if selection_channel not in spot_data:
        selection_channel = session.primary_channel if session.primary_channel in spot_data else str(spot_data.columns[1])

    plateau_start, plateau_end, baseline_start, baseline_end = select_baseline_peak(
        spot_data[selection_channel].to_numpy(dtype=float),
        baseline_sweep_n=int(session.import_settings.get("baseline_sweep_n", 10)),
        peak_sweep_n=int(session.import_settings.get("peak_sweep_n", 20)),
        threshold=float(session.import_settings.get("threshold", 0.1)),
        filter_percentile=float(session.import_settings.get("filter_percentile", 70)),
        baseline_offset=int(session.import_settings.get("baseline_offset", 2)),
        plateau_offset=int(session.import_settings.get("plateau_offset", 2)),
    )

    max_index = max(0, len(spot_data) - 1)
    baseline = (
        float(spot_data.iloc[min(baseline_start, max_index)]["Time"]),
        float(spot_data.iloc[min(baseline_end, max_index)]["Time"]),
    )
    plateau = (
        float(spot_data.iloc[min(plateau_start, max_index)]["Time"]),
        float(spot_data.iloc[min(plateau_end, max_index)]["Time"]),
    )
    update_selection_region(session, spot_name, baseline=baseline, plateau=plateau)
    return {"baseline": baseline, "plateau": plateau}


def get_spot_data(session: ReductionSession, spot_name: str | None) -> pd.DataFrame:
    if not session.loaded or not spot_name:
        return pd.DataFrame()
    return session.total_data[session.total_data["spot_name"] == spot_name].reset_index(drop=True)


def extract_spot_frame(frame: pd.DataFrame, spot_name: str | None) -> pd.DataFrame:
    if frame.empty or not spot_name or "spot_name" not in frame:
        return pd.DataFrame()
    return frame[frame["spot_name"] == spot_name].reset_index(drop=True)


def get_selection_regions(session: ReductionSession, spot_name: str | None) -> dict[str, tuple[float, float] | None]:
    if not session.loaded or not spot_name:
        return {"baseline": None, "plateau": None}
    row = session.experiment[session.experiment["spot_names"] == spot_name]
    if row.empty:
        return {"baseline": None, "plateau": None}

    def _pair(start_key: str, end_key: str) -> tuple[float, float] | None:
        start_value = row[start_key].iloc[0]
        end_value = row[end_key].iloc[0]
        if pd.isna(start_value) or pd.isna(end_value):
            return None
        return float(start_value), float(end_value)

    return {
        "baseline": _pair("baseline_start_times", "baseline_end_times"),
        "plateau": _pair("plateau_start_times", "plateau_end_times"),
    }


def selection_quality(session: ReductionSession, spot_name: str | None) -> dict[str, Any]:
    if not session.loaded or not spot_name:
        return {"baseline_points": 0, "plateau_points": 0, "warnings": ["Load a session to inspect selections."], "status": "idle"}

    spot_data = get_spot_data(session, spot_name)
    regions = get_selection_regions(session, spot_name)
    baseline_frame = slice_region(spot_data, regions["baseline"])
    plateau_frame = slice_region(spot_data, regions["plateau"])
    warnings: list[str] = []

    if len(baseline_frame) < 5:
        warnings.append("baseline window has fewer than 5 points")
    if len(plateau_frame) < 5:
        warnings.append("plateau window has fewer than 5 points")

    baseline_region = regions["baseline"]
    plateau_region = regions["plateau"]
    if baseline_region and plateau_region:
        overlap_start = max(baseline_region[0], plateau_region[0])
        overlap_end = min(baseline_region[1], plateau_region[1])
        if overlap_start < overlap_end:
            warnings.append("baseline and plateau windows overlap")

    status = "ready" if not warnings else "review"
    return {
        "baseline_points": len(baseline_frame),
        "plateau_points": len(plateau_frame),
        "warnings": warnings,
        "status": status,
    }


def ensure_baseline_subtracted(session: ReductionSession) -> pd.DataFrame:
    cache_key = "baseline_subtracted_total_data"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    if not session.loaded:
        return pd.DataFrame()
    baseline_subtracted = subtract_baseline(session.experiment.copy(), session.channels, session.total_data.copy())
    session.reduction_cache[cache_key] = baseline_subtracted
    return baseline_subtracted


def ensure_normalized_plateau(session: ReductionSession, preset: WorkflowPreset) -> pd.DataFrame:
    cache_key = "normalized_plateau_data"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    baseline_subtracted = ensure_baseline_subtracted(session)
    if baseline_subtracted.empty:
        return pd.DataFrame()
    normalized = internal_normalization(
        session.experiment.copy(),
        session.channels,
        baseline_subtracted.copy(),
        standard_archive,
        material=preset.internal_material,
    )
    session.reduction_cache[cache_key] = normalized
    return normalized


def ensure_concentration_results(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    cache_key = "concentration_results"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
    if not preset.concentration_standards:
        raise ValueError(f"{preset.title} does not yet define concentration standards for the GUI workflow.")

    normalized = ensure_normalized_plateau(session, preset)
    if normalized.empty:
        raise ValueError("Internal normalization did not produce any plateau data.")

    _require_samples(session, preset.concentration_standards, "concentration standards")
    nist_data = normalized[normalized["sample"].isin(preset.concentration_standards)].reset_index(drop=True)
    calibration, calibration_plot = calc_concentration_calibration_factor(nist_data, session.channels, standard_archive)
    unknown_standard_data = normalized[~normalized["sample"].isin(preset.concentration_standards)].reset_index(drop=True)
    final_concentration_df = calc_concentration(unknown_standard_data, calibration)
    results = {
        "calibration": calibration,
        "calibration_plot": calibration_plot,
        "final_concentration_df": final_concentration_df,
        "calibrant_count": int(len(nist_data)),
        "result_count": int(len(final_concentration_df)),
    }
    session.reduction_cache[cache_key] = results
    return results


def ensure_raw_ratios(session: ReductionSession) -> pd.DataFrame:
    cache_key = "plateau_data_with_raw_ratios"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached
    baseline_subtracted = ensure_baseline_subtracted(session)
    if baseline_subtracted.empty:
        return pd.DataFrame()
    ratios = calc_raw_ratios(session.experiment.copy(), session.total_data.copy(), baseline_subtracted.copy())
    session.reduction_cache[cache_key] = ratios
    return ratios


def ensure_downhole_results(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    cache_key = "downhole_results"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
    if not preset.primary_standard:
        raise ValueError(f"{preset.title} does not yet define a primary standard for downhole correction.")

    raw_ratios = ensure_raw_ratios(session)
    if raw_ratios.empty:
        raise ValueError("Raw isotope ratios are not available for downhole correction.")

    _require_samples(session, (preset.primary_standard,), "primary standard")
    primary_standard_ratios = raw_ratios[raw_ratios["sample"] == preset.primary_standard].reset_index(drop=True)
    correction_results = calc_DH_fractionations_rad(primary_standard_ratios.copy(), standard_archive)
    corrected = apply_DH_correction(raw_ratios.copy(), correction_results)
    results = {"correction_results": correction_results, "corrected_ratios": corrected}
    session.reduction_cache[cache_key] = results
    return results


def ensure_session_corrected(session: ReductionSession, preset: WorkflowPreset) -> dict[str, Any]:
    cache_key = "session_correction_results"
    cached = session.reduction_cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached

    downhole_results = ensure_downhole_results(session, preset)
    corrected = downhole_results["corrected_ratios"]
    primary_standard = corrected[corrected["sample"] == preset.primary_standard].reset_index(drop=True)
    session_results = calc_session_fractionation_rad(
        primary_standard.copy(),
        standard_archive,
        degree=preset.session_polynomial_degree,
    )
    session_corrected = apply_session_correction(corrected.copy(), session_results)
    results = {"correction_results": session_results, "corrected_ratios": session_corrected}
    session.reduction_cache[cache_key] = results
    return results


def reference_material_summary(session: ReductionSession, preset: WorkflowPreset) -> str:
    if not session.loaded or "sample" not in session.total_data:
        return ""
    counts = session.total_data["sample"].value_counts()
    parts: list[str] = []
    if preset.primary_standard:
        parts.append(f"{preset.primary_standard}: {int(counts.get(preset.primary_standard, 0))} primary rows")
    if preset.concentration_standards:
        standard_counts = [f"{name} {int(counts.get(name, 0))}" for name in preset.concentration_standards]
        parts.append("Glasses " + ", ".join(standard_counts))
    return " | ".join(parts)


def _require_samples(session: ReductionSession, sample_names: tuple[str, ...], label: str) -> None:
    available = set(session.total_data["sample"].dropna().astype(str).unique().tolist()) if session.loaded else set()
    missing = [sample_name for sample_name in sample_names if sample_name not in available]
    if missing:
        raise ValueError(f"Missing {label} in the loaded session: {', '.join(missing)}")


def project_summary(session: ReductionSession) -> dict[str, str]:
    if not session.loaded:
        return {
            "name": "No experiment loaded",
            "spots": "Import a Thermo iCAP export directory",
            "mode": "Choose a notebook-derived mineral workflow",
            "engine": "Shared UPbRPy core",
        }
    project_name = Path(session.directory).name or session.directory
    return {
        "name": project_name,
        "spots": f"{len(session.spot_names)} spots · {len(session.channels)} channels",
        "mode": f"{session.workflow_title} workflow · depth profile + spot",
        "engine": f"Primary channel: {session.primary_channel}",
    }


def spot_metrics(session: ReductionSession, spot_name: str | None) -> dict[str, str]:
    if not session.loaded or not spot_name:
        return {
            "Active spot": "Preview",
            "Selection span": "34.8 s",
            "206Pb/238U": "0.09142 ± preview",
            "Primary signal": "1.95 kcps",
        }

    spot_data = get_spot_data(session, spot_name)
    regions = get_selection_regions(session, spot_name)
    plateau = regions["plateau"]
    baseline = regions["baseline"]

    plateau_data = slice_region(spot_data, plateau)
    baseline_data = slice_region(spot_data, baseline)

    selection_span = f"{plateau[1] - plateau[0]:.1f} s" if plateau else "Not set"
    ratio = "Unavailable"
    if not plateau_data.empty and {"206Pb", "238U"}.issubset(plateau_data.columns):
        denominator = float(plateau_data["238U"].mean())
        if denominator != 0:
            ratio = f"{float(plateau_data['206Pb'].mean()) / denominator:.5f}"

    primary_signal = "Unavailable"
    if session.primary_channel in spot_data:
        primary_source = plateau_data if not plateau_data.empty else spot_data
        primary_signal = f"{float(primary_source[session.primary_channel].mean()) / 1000:.2f} kcps"

    if not baseline_data.empty and session.primary_channel in baseline_data:
        baseline_value = float(baseline_data[session.primary_channel].mean())
        primary_signal = f"{primary_signal} · BL {baseline_value / 1000:.2f} kcps"

    return {
        "Active spot": spot_name,
        "Selection span": selection_span,
        "206Pb/238U": ratio,
        "Primary signal": primary_signal,
    }


def slice_region(frame: pd.DataFrame, region: tuple[float, float] | None) -> pd.DataFrame:
    if frame.empty or region is None:
        return pd.DataFrame()
    start_time, end_time = region
    return frame[(frame["Time"] >= start_time) & (frame["Time"] <= end_time)].reset_index(drop=True)


def build_notes(session: ReductionSession, spot_name: str | None, preset: WorkflowPreset, current_step: str | None = None) -> str:
    if not session.loaded:
        return (
            f"{preset.description} Import defaults: {format_import_defaults(preset)} "
            "The GUI shell stays separate from the UPbRPy reduction engine so notebook behavior remains backward-compatible."
        )

    spot_data = get_spot_data(session, spot_name)
    sample_names = sorted(spot_data["sample"].dropna().unique().tolist()) if "sample" in spot_data else []
    sample_label = ", ".join(sample_names[:3]) if sample_names else "Unknown sample"
    import_defaults = format_import_defaults(preset)
    next_step = f" Focus step: {current_step}." if current_step else ""
    return (
        f"Loaded {len(session.spot_names)} spots from {Path(session.directory).name}. "
        f"Viewing {spot_name} from {sample_label}. Using the {preset.title.lower()} notebook sequence with {import_defaults} "
        f"{preset.special_note}{next_step} The spot trace view is backed by pyqtgraph while reductions continue to come from the shared UPbRPy source functions."
    )
