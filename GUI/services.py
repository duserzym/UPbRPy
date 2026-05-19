from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy.interpolate import CubicSpline, interp1d
from scipy.optimize import curve_fit

from GUI.state import ReductionSession
from GUI.workflows import WorkflowPreset, format_import_defaults, infer_workflow_preset
from UPbRPy import (
    apply_DH_correction,
    calc_concentration,
    calc_concentration_calibration_factor,
    calc_raw_ratios,
    internal_normalization,
    read_experiment_data,
    select_baseline_peak,
    standard_archive,
    subtract_baseline,
)


DEFAULT_REFERENCE_LIBRARY_PATH = str(files("UPbRPy") / "standards.csv")
_active_standard_archive = standard_archive.copy()
_active_standard_archive_path = DEFAULT_REFERENCE_LIBRARY_PATH


FIT_METHOD_LABELS: dict[str, str] = {
    "interpolated": "Interpolated",
    "linear": "Linear",
    "exponential": "Exponential",
    "cubic_spline": "Cubic spline",
    "running_mean": "Running mean",
    "lowess": "LOWESS",
    "polynomial": "Polynomial",
}

DOWNHOLE_FIT_METHODS: tuple[str, ...] = (
    "interpolated",
    "linear",
    "exponential",
    "cubic_spline",
    "running_mean",
    "lowess",
    "polynomial",
)

SESSION_FIT_METHODS: tuple[str, ...] = (
    "interpolated",
    "linear",
    "exponential",
    "cubic_spline",
    "running_mean",
    "lowess",
    "polynomial",
)


def fit_method_label(method: str) -> str:
    return FIT_METHOD_LABELS.get(method, method.replace("_", " ").title())


def _exp_decay(x: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    return a + b * np.exp(-c * x)


def _coerce_running_mean_window(value: object, size: int) -> int:
    window = int(value) if value is not None else 5
    window = max(3, window)
    if window % 2 == 0:
        window += 1
    if size <= 2:
        return size
    return min(window, size if size % 2 == 1 else size - 1)


def _running_mean(values: np.ndarray, window: int) -> np.ndarray:
    if values.size == 0 or window <= 1:
        return values
    pad = window // 2
    padded = np.pad(values, (pad, pad), mode="edge")
    kernel = np.ones(window, dtype=float) / window
    return np.convolve(padded, kernel, mode="valid")


def _session_fit_weights(fractions: np.ndarray, stds: np.ndarray) -> np.ndarray:
    safe_stds = np.where(np.isfinite(stds) & (stds > 0), stds, np.nanmedian(stds[np.isfinite(stds) & (stds > 0)]) if np.any(np.isfinite(stds) & (stds > 0)) else 1.0)
    relative = np.where(safe_stds > 0, fractions / safe_stds, 1.0)
    weights = np.where(np.isfinite(relative) & (relative != 0), 1 / relative**2, 1.0)
    return weights.astype(float)


def _evaluate_fit_model(model: dict[str, Any], x_values: np.ndarray | pd.Series | list[float]) -> np.ndarray:
    evaluator = model.get("evaluator")
    x_arr = np.asarray(x_values, dtype=float)
    if callable(evaluator):
        return np.asarray(evaluator(x_arr), dtype=float)
    raise ValueError("Invalid fit model: missing evaluator.")


def _fit_fractionation_model(
    x_values: np.ndarray,
    y_values: np.ndarray,
    method: str,
    options: dict[str, float | int | str],
    *,
    weights: np.ndarray | None = None,
) -> tuple[dict[str, Any], np.ndarray]:
    x_arr = np.asarray(x_values, dtype=float)
    y_arr = np.asarray(y_values, dtype=float)
    weights_arr = None if weights is None else np.asarray(weights, dtype=float)

    if method == "lowess":
        raise NotImplementedError("LOWESS is planned but not yet implemented in this GUI build.")

    if method == "interpolated":
        interpolator = interp1d(x_arr, y_arr, kind="linear", fill_value="extrapolate")
        model = {"kind": method, "evaluator": interpolator, "parameter_count": 0}
    elif method == "linear":
        coeffs = np.polyfit(x_arr, y_arr, 1, w=weights_arr)
        model = {
            "kind": method,
            "coeffs": coeffs,
            "evaluator": lambda x, c=coeffs: np.polyval(c, x),
            "parameter_count": 2,
        }
    elif method == "polynomial":
        degree = max(1, int(options.get("polynomial_degree", 2)))
        coeffs = np.polyfit(x_arr, y_arr, degree, w=weights_arr)
        model = {
            "kind": method,
            "coeffs": coeffs,
            "degree": degree,
            "evaluator": lambda x, c=coeffs: np.polyval(c, x),
            "parameter_count": degree + 1,
        }
    elif method == "exponential":
        initial_decay = max(float(options.get("exponential_initial_decay", 0.1)), 1e-6)
        p0 = (float(y_arr[-1]), float(y_arr[0] - y_arr[-1]), initial_decay)
        params, _cov = curve_fit(
            _exp_decay,
            x_arr,
            y_arr,
            p0=p0,
            bounds=([-np.inf, -np.inf, 0.0], [np.inf, np.inf, np.inf]),
            maxfev=20000,
        )
        model = {
            "kind": method,
            "params": params,
            "evaluator": lambda x, p=params: _exp_decay(np.asarray(x, dtype=float), *p),
            "parameter_count": 3,
        }
    elif method == "cubic_spline":
        spline = CubicSpline(x_arr, y_arr, bc_type="natural", extrapolate=True)
        model = {"kind": method, "spline": spline, "evaluator": spline, "parameter_count": 4}
    elif method == "running_mean":
        window = _coerce_running_mean_window(options.get("running_mean_window", 5), len(y_arr))
        smoothed = _running_mean(y_arr, window)
        interpolator = interp1d(x_arr, smoothed, kind="linear", fill_value="extrapolate")
        model = {
            "kind": method,
            "window": window,
            "smoothed": smoothed,
            "evaluator": interpolator,
            "parameter_count": 1,
        }
    else:
        raise ValueError(f"Unsupported fit method: {method}")

    return model, _evaluate_fit_model(model, x_arr)


def _build_session_primary_standard_summary(
    dh_corrected: pd.DataFrame,
    primary_standard: str,
    ratio_name: str,
    known_ratio: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    corrected_col = f"{ratio_name}_DH_corrected"
    ps_rows = dh_corrected[dh_corrected["sample"] == primary_standard]
    if ps_rows.empty or corrected_col not in ps_rows.columns:
        return np.array([]), np.array([]), np.array([]), []

    spot_mean_times: list[float] = []
    spot_mean_fracs: list[float] = []
    spot_frac_errs: list[float] = []
    spot_names_out: list[str] = []
    for spot_name in ps_rows["spot_name"].unique():
        this_spot = ps_rows[ps_rows["spot_name"] == spot_name]
        r_vals = this_spot[corrected_col].to_numpy(dtype=float)
        valid = np.isfinite(r_vals) & (r_vals > 0)
        if valid.sum() < 2:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            spot_fractionations = np.where(r_vals[valid] > 0, known_ratio / r_vals[valid], np.nan)
        spot_fractionations = spot_fractionations[np.isfinite(spot_fractionations)]
        if spot_fractionations.size < 2:
            continue
        spot_mean_times.append(float(this_spot["Time"].mean()))
        spot_mean_fracs.append(float(np.mean(spot_fractionations)))
        spot_frac_errs.append(float(np.std(spot_fractionations)))
        spot_names_out.append(str(spot_name))

    if len(spot_mean_times) < 2:
        return np.array([]), np.array([]), np.array([]), []

    times = np.array(spot_mean_times, dtype=float)
    fracs = np.array(spot_mean_fracs, dtype=float)
    errs = np.array(spot_frac_errs, dtype=float)
    order = np.argsort(times)
    return times[order], fracs[order], errs[order], [spot_names_out[i] for i in order]


def _build_downhole_correction_results(
    primary_standard_ratios: pd.DataFrame,
    standard_archive_frame: pd.DataFrame,
    *,
    method: str,
    options: dict[str, float | int | str],
    ratio_names: tuple[str, ...] = ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th"),
) -> dict[str, Any]:
    primary_standard = str(primary_standard_ratios["sample"].iloc[0])
    results: dict[str, Any] = {}

    for ratio_name in ratio_names:
        known_ratio = float(standard_archive_frame.loc[primary_standard, ratio_name + "*"])
        spot_times: list[np.ndarray] = []
        spot_ratios: list[np.ndarray] = []
        for spot_name in primary_standard_ratios["spot_name"].unique():
            this_spot = primary_standard_ratios[primary_standard_ratios["spot_name"] == spot_name].copy()
            rel_t = (this_spot["Time"] - this_spot["Time"].iloc[0]).to_numpy(dtype=float)
            ratios = this_spot[ratio_name].to_numpy(dtype=float)
            valid = np.isfinite(ratios) & (ratios > 0)
            if valid.sum() < 2:
                continue
            spot_times.append(rel_t[valid])
            spot_ratios.append(ratios[valid])

        if not spot_times:
            continue

        max_time = float(max(times[-1] for times in spot_times))
        point_count = max(len(times) for times in spot_times)
        unified_time = np.linspace(0.0, max_time, point_count)
        stacked = np.array([np.interp(unified_time, times, ratios) for times, ratios in zip(spot_times, spot_ratios)])
        mean_ratio = np.nanmean(stacked, axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            mean_fractionation = np.where(mean_ratio > 0, known_ratio / mean_ratio, np.nan)

        fit_model, fit_values = _fit_fractionation_model(unified_time, mean_fractionation, method, options)
        all_times = np.concatenate(spot_times)
        all_fractionations = np.concatenate([np.where(ratios > 0, known_ratio / ratios, np.nan) for ratios in spot_ratios])
        valid = np.isfinite(all_times) & np.isfinite(all_fractionations)
        residuals = all_fractionations[valid] - _evaluate_fit_model(fit_model, all_times[valid])
        std_value = float(np.std(residuals)) if residuals.size else 0.0

        results[ratio_name] = {
            "plot": None,
            "method": method,
            "fit_label": fit_method_label(method),
            "model": fit_model,
            "interpolation": lambda x, model=fit_model: _evaluate_fit_model(model, x),
            "fit_time": unified_time,
            "fit_values": fit_values,
            "mean_fractionation": mean_fractionation,
            "std": std_value,
        }

    return results


def _build_session_correction_results(
    primary_standard_dh_corrected: pd.DataFrame,
    standard_archive_frame: pd.DataFrame,
    *,
    primary_standard: str,
    method: str,
    options: dict[str, float | int | str],
    ratio_names: tuple[str, ...] = ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th"),
) -> dict[str, Any]:
    results: dict[str, Any] = {}

    for ratio_name in ratio_names:
        known_ratio = float(standard_archive_frame.loc[primary_standard, ratio_name + "*"])
        times, fracs, errs, spot_names = _build_session_primary_standard_summary(
            primary_standard_dh_corrected,
            primary_standard,
            ratio_name,
            known_ratio,
        )
        if times.size < 2:
            continue
        weights = _session_fit_weights(fracs, errs)
        fit_model, fit_at_points = _fit_fractionation_model(times, fracs, method, options, weights=weights)
        residuals = fracs - fit_at_points
        parameter_count = max(int(fit_model.get("parameter_count", 1)), 1)
        dof = max(len(residuals) - parameter_count, 1)
        fit_std = float(np.sqrt(np.sum(weights * residuals**2) / dof)) if residuals.size else 0.0
        fit_times = np.linspace(float(times[0]), float(times[-1]), 200)
        fit_values = _evaluate_fit_model(fit_model, fit_times)
        results[ratio_name] = {
            "plot": None,
            "method": method,
            "fit_label": fit_method_label(method),
            "fit_model": fit_model,
            "fit": fit_model,
            "std": fit_std,
            "fit_times": fit_times,
            "fit_values": fit_values,
            "spot_mean_times": times,
            "spot_mean_fracs": fracs,
            "spot_frac_errs": errs,
            "spot_names": spot_names,
        }

    return results


def _apply_session_correction_models(
    dh_corrected_ratios: pd.DataFrame,
    session_correction_results: dict[str, Any],
) -> pd.DataFrame:
    corrected = dh_corrected_ratios.copy()
    for ratio_name, result in session_correction_results.items():
        model = result.get("fit_model") or result.get("fit")
        corrected[f"{ratio_name}_fractionation_corrected"] = (
            corrected[f"{ratio_name}_DH_corrected"] * _evaluate_fit_model(model, corrected["Time"].to_numpy(dtype=float))
        )
    return corrected


# ── Isotope channel parsing ────────────────────────────────────────────────

_MASS_RE_SVC = re.compile(r'^(\d+)\s*([A-Za-z]+)$')


def _parse_channel_name(name: str) -> tuple[str, str] | None:
    """Return (Element_Capitalized, mass_str) for '238U' or 'U238', else None."""
    s = name.strip()
    m = _MASS_RE_SVC.match(s)
    if m:
        return m.group(2).capitalize(), m.group(1)
    m2 = re.match(r'^([A-Za-z]+)\s*(\d+)$', s)
    if m2:
        return m2.group(1).capitalize(), m2.group(2)
    return None


# ── Discrete element colour palette (tab10-inspired) ─────────────────────────
#
# Two variants optimised for dark and light backgrounds.  All isotopes of
# the same element share the same base colour; visual distinction between
# isotopes is provided by the chip border-style in the UI widget.

_PALETTE_DARK: list[str] = [
    "#5590f5",  # 0  vivid blue       U
    "#f5c040",  # 1  gold             Pb
    "#f07840",  # 2  orange           Th
    "#b864f0",  # 3  violet           Hf
    "#40c870",  # 4  green            Si
    "#d04848",  # 5  red              Fe
    "#40c8d8",  # 6  cyan             Ti / Zr
    "#f060a8",  # 7  hot-pink         Sr / Na
    "#b8d840",  # 8  lime             Ca / Al
    "#f0a040",  # 9  amber            Mn / P
    "#7090d8",  # 10 steel-blue       Nb / Ta
    "#d090f0",  # 11 lavender         REE / misc
]

_PALETTE_LIGHT: list[str] = [
    "#1040c0",  # 0 deep blue
    "#806010",  # 1 dark gold
    "#a04010",  # 2 burnt orange
    "#5810a0",  # 3 deep purple
    "#0a6030",  # 4 forest green
    "#900000",  # 5 crimson
    "#0a6878",  # 6 dark teal
    "#880860",  # 7 deep magenta
    "#506818",  # 8 olive
    "#804808",  # 9 dark amber
    "#183878",  # 10 dark steel-blue
    "#600878",  # 11 dark lavender
]

# Fixed palette-index per known element.  Unrecognised elements are assigned
# indices dynamically by cycling through the palette.
_ELEM_PALETTE_IDX: dict[str, int] = {
    # Geochronology – must be maximally distinct
    "U": 0, "Pb": 1, "Th": 2, "Hf": 3,
    # Internal standards
    "Si": 4, "Ca": 8, "Al": 8, "Mg": 4,
    "Ti": 6, "Fe": 5, "Mn": 9, "P": 9, "Na": 7, "K": 7,
    # REE + Y: cycle 4-11 to stay clear of geochronology colours (0-3)
    "Y": 4, "La": 5, "Ce": 6, "Pr": 7, "Nd": 8, "Sm": 9, "Eu": 10,
    "Gd": 11, "Tb": 4, "Dy": 5, "Ho": 6, "Er": 7, "Tm": 8, "Yb": 9, "Lu": 10,
    # HFSE
    "Zr": 6, "Nb": 10, "Ta": 10,
    # Alkali / alkaline earth
    "Sr": 7, "Ba": 9, "Rb": 2, "Li": 11, "Cs": 5,
    # Others
    "Hg": 6, "Bi": 3,
}

# Channel section definitions for the isotope grid UI.
# Each entry is (label, ordered_element_list | None for catch-all).
CHANNEL_SECTIONS: list[tuple[str, list[str] | None]] = [
    ("Geochronology",
     ["U", "Pb", "Th", "Hf"]),
    ("Internal Standards",
     ["Si", "Ca", "Al", "Ti", "Mg", "Fe", "Mn", "P", "Na", "K"]),
    ("REE + Y",
     ["Y", "La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd",
      "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu"]),
    ("HFSE",
     ["Zr", "Nb", "Ta"]),
    ("Alkali / Alkaline Earth",
     ["Sr", "Ba", "Rb", "Na", "K", "Li", "Cs"]),
    ("Other",
     None),
]

# Regex for stripping trailing spot-number suffixes.
_SPOT_SUFFIX_SEP = re.compile(r'[\s\-_\.]+\d+[a-zA-Z]{0,2}$')
_SPOT_SUFFIX_NOSEP = re.compile(r'(?<=[a-zA-Z])\d+[a-zA-Z]{0,1}$')


def spot_base_name(name: str) -> str:
    """Strip trailing spot-number suffixes: 'GJ1_001' -> 'GJ1'."""
    s = name.strip()
    stripped = _SPOT_SUFFIX_SEP.sub('', s)
    if stripped and stripped != s:
        return stripped.strip()
    stripped = _SPOT_SUFFIX_NOSEP.sub('', s)
    if stripped and stripped != s:
        return stripped.strip()
    return s


def default_visible_channels(channels: Iterable[str], primary_channel: str) -> list[str]:
    ordered = [str(channel) for channel in channels]
    if primary_channel in ordered:
        ordered = [primary_channel] + [channel for channel in ordered if channel != primary_channel]
    return ordered[: min(4, len(ordered))]


def build_channel_colors(
    channels: Iterable[str],
    theme_name: str = "dark",
) -> dict[str, str]:
    """Assign discrete palette colours to channels.

    All isotopes of the same element receive the same base colour.  Visual
    distinction between isotopes is provided by the chip border-style in the
    IsotopeGridWidget.
    """
    palette = _PALETTE_DARK if (theme_name == "dark") else _PALETTE_LIGHT
    channel_list = [str(ch) for ch in channels]

    # Dynamic assignment for elements not in the fixed map.
    _dyn_color: dict[str, str] = {}
    _dyn_idx: list[int] = [0]

    def _elem_color(elem: str) -> str:
        if elem in _ELEM_PALETTE_IDX:
            return palette[_ELEM_PALETTE_IDX[elem] % len(palette)]
        if elem not in _dyn_color:
            _dyn_color[elem] = palette[_dyn_idx[0] % len(palette)]
            _dyn_idx[0] += 1
        return _dyn_color[elem]

    colors: dict[str, str] = {}
    unparsed_idx = 0
    for ch in channel_list:
        parsed = _parse_channel_name(ch)
        if parsed:
            elem, _ = parsed
            colors[ch] = _elem_color(elem)
        else:
            # Unnamed channels: assign from the end of the palette backwards.
            colors[ch] = palette[(len(palette) - 1 - unparsed_idx) % len(palette)]
            unparsed_idx += 1

    return colors


def default_reference_material_library_path() -> str:
    return DEFAULT_REFERENCE_LIBRARY_PATH


def current_standard_archive() -> pd.DataFrame:
    return _active_standard_archive


def current_standard_archive_path() -> str:
    return _active_standard_archive_path


def load_standard_archive_from_path(path: str | None) -> pd.DataFrame:
    global _active_standard_archive
    global _active_standard_archive_path

    if not path:
        _active_standard_archive = standard_archive.copy()
        _active_standard_archive_path = DEFAULT_REFERENCE_LIBRARY_PATH
        return _active_standard_archive

    target_path = Path(path).expanduser().resolve()
    loaded_archive = pd.read_csv(target_path, index_col=0)
    _active_standard_archive = loaded_archive
    _active_standard_archive_path = str(target_path)
    return _active_standard_archive


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
        current_standard_archive(),
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
    calibration, calibration_plot = calc_concentration_calibration_factor(nist_data, session.channels, current_standard_archive())
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
    correction_results = _build_downhole_correction_results(
        primary_standard_ratios.copy(),
        current_standard_archive(),
        method=session.downhole_fit_method,
        options=session.downhole_fit_options,
    )
    corrected = apply_DH_correction(raw_ratios.copy(), correction_results)
    results = {
        "correction_results": correction_results,
        "corrected_ratios": corrected,
        "fit_method": session.downhole_fit_method,
        "fit_label": fit_method_label(session.downhole_fit_method),
        "fit_options": dict(session.downhole_fit_options),
    }
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
    session_results = _build_session_correction_results(
        primary_standard.copy(),
        current_standard_archive(),
        primary_standard=preset.primary_standard,
        method=session.session_fit_method,
        options=session.session_fit_options,
    )
    session_corrected = _apply_session_correction_models(corrected.copy(), session_results)
    results = {
        "correction_results": session_results,
        "corrected_ratios": session_corrected,
        "fit_method": session.session_fit_method,
        "fit_label": fit_method_label(session.session_fit_method),
        "fit_options": dict(session.session_fit_options),
    }
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


# ── Diagnostic plot data helpers ──────────────────────────────────────────

def get_downhole_plot_data(
    session: ReductionSession,
    preset: WorkflowPreset,
    ratio_name: str = "206Pb/238U",
) -> dict | None:
    """Return a dict of arrays needed to draw the downhole fractionation diagnostic.

    Returns None if the prerequisite cache entries are absent.
    """
    raw_ratios = session.reduction_cache.get("plateau_data_with_raw_ratios")
    downhole_results = session.reduction_cache.get("downhole_results")
    if not isinstance(raw_ratios, pd.DataFrame) or raw_ratios.empty:
        return None
    primary_standard = preset.primary_standard
    if not primary_standard:
        return None
    if ratio_name not in raw_ratios.columns:
        return None
    try:
        archive = current_standard_archive()
        known_ratio = float(archive.loc[primary_standard, ratio_name + "*"])
    except Exception:
        return None

    ps_rows = raw_ratios[raw_ratios["sample"] == primary_standard]
    if ps_rows.empty:
        return None

    spot_times: list[np.ndarray] = []
    spot_ratios: list[np.ndarray] = []
    spot_names: list[str] = []
    for spot_name in ps_rows["spot_name"].unique():
        this_spot = ps_rows[ps_rows["spot_name"] == spot_name]
        t = this_spot["Time"].values.astype(float)
        r = this_spot[ratio_name].values.astype(float)
        valid = np.isfinite(r) & (r > 0)
        if valid.sum() < 2:
            continue
        rel_t = t - t[0]
        spot_times.append(rel_t[valid])
        spot_ratios.append(r[valid])
        spot_names.append(str(spot_name))

    if not spot_times:
        return None

    max_t = float(max(t[-1] for t in spot_times))
    n_pts = max(len(t) for t in spot_times)
    unified_time = np.linspace(0.0, max_t, n_pts)

    stacked = np.array([np.interp(unified_time, t, r) for t, r in zip(spot_times, spot_ratios)])
    mean_ratio = np.nanmean(stacked, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_frac = np.where(mean_ratio > 0, known_ratio / mean_ratio, np.nan)

    # Std of individual fractionation residuals vs the mean curve (scalar, like notebook)
    all_frac_flat = np.concatenate([
        np.where(r > 0, known_ratio / r, np.nan) for r in stacked
    ])
    valid_mask = np.isfinite(all_frac_flat)
    mean_frac_repeated = np.tile(mean_frac, len(spot_times))[valid_mask]
    std_frac = float(np.std(all_frac_flat[valid_mask] - mean_frac_repeated))

    fit_curve = mean_frac
    fit_label = fit_method_label(session.downhole_fit_method)
    if isinstance(downhole_results, dict):
        correction_results = downhole_results.get("correction_results")
        if isinstance(correction_results, dict) and ratio_name in correction_results:
            ratio_result = correction_results[ratio_name]
            fit_curve = np.asarray(ratio_result.get("fit_values", mean_frac), dtype=float)
            fit_label = str(ratio_result.get("fit_label", fit_label))

    return {
        "ratio_name": ratio_name,
        "known_ratio": known_ratio,
        "primary_standard": primary_standard,
        "spot_names": spot_names,
        "spot_times": spot_times,
        "spot_ratios": spot_ratios,
        "unified_time": unified_time,
        "mean_frac": fit_curve,
        "fit_label": fit_label,
        "std_frac": std_frac,
    }


def get_session_plot_data(
    session: ReductionSession,
    preset: WorkflowPreset,
    ratio_name: str = "206Pb/238U",
) -> dict | None:
    """Return a dict of arrays for drawing the session fractionation diagnostic."""
    session_results = session.reduction_cache.get("session_correction_results")
    downhole_results = session.reduction_cache.get("downhole_results")
    if not isinstance(session_results, dict) or not isinstance(downhole_results, dict):
        return None

    dh_corrected = downhole_results.get("corrected_ratios")
    if not isinstance(dh_corrected, pd.DataFrame) or dh_corrected.empty:
        return None

    primary_standard = preset.primary_standard
    if not primary_standard:
        return None
    try:
        archive = current_standard_archive()
        known_ratio = float(archive.loc[primary_standard, ratio_name + "*"])
    except Exception:
        return None

    t_arr, f_arr, e_arr, spot_names_out = _build_session_primary_standard_summary(
        dh_corrected,
        primary_standard,
        ratio_name,
        known_ratio,
    )
    if t_arr.size < 2:
        return None

    ratio_results = session_results.get("correction_results", {}).get(ratio_name)
    if not ratio_results:
        return None
    p_std = float(ratio_results.get("std", 0.0))
    fit_times = np.asarray(ratio_results.get("fit_times", np.linspace(float(t_arr[0]), float(t_arr[-1]), 200)), dtype=float)
    fit_vals = np.asarray(ratio_results.get("fit_values", []), dtype=float)
    if fit_vals.size == 0:
        fit_model = ratio_results.get("fit_model") or ratio_results.get("fit")
        fit_vals = _evaluate_fit_model(fit_model, fit_times)

    return {
        "ratio_name": ratio_name,
        "known_ratio": known_ratio,
        "primary_standard": primary_standard,
        "spot_names": spot_names_out,
        "spot_mean_times": t_arr,
        "spot_mean_fracs": f_arr,
        "spot_frac_errs": e_arr,
        "fit_times": fit_times,
        "fit_vals": fit_vals,
        "fit_std": p_std,
        "fit_label": str(ratio_results.get("fit_label", fit_method_label(session.session_fit_method))),
        "show_uncertainty": False,
    }


def get_calibration_slope_data(
    session: ReductionSession,
    preset: WorkflowPreset,
) -> dict | None:
    """Return a dict of arrays for drawing the concentration calibration slopes."""
    conc_results = session.reduction_cache.get("concentration_results")
    if not isinstance(conc_results, dict):
        return None
    calibration = conc_results.get("calibration")
    if not isinstance(calibration, pd.Series) or calibration.empty:
        return None

    normalized = session.reduction_cache.get("normalized_plateau_data")
    if not isinstance(normalized, pd.DataFrame) or normalized.empty:
        return None
    if not preset.concentration_standards:
        return None

    nist_data = normalized[normalized["sample"].isin(preset.concentration_standards)].reset_index(drop=True)
    if nist_data.empty:
        return None

    try:
        archive = current_standard_archive()
    except Exception:
        return None

    channels = list(calibration.index)
    channel_data: dict[str, dict] = {}
    for channel in channels:
        if channel not in nist_data.columns:
            continue
        cps_vals: list[float] = []
        ppm_vals: list[float] = []
        sample_labels: list[str] = []
        for sn in nist_data["spot_name"].unique():
            this_spot = nist_data[nist_data["spot_name"] == sn]
            sample = str(this_spot["sample"].values[0])
            if sample not in archive.index or channel not in archive.columns:
                continue
            known_ppm = float(archive.loc[sample, channel])
            if not np.isfinite(known_ppm) or known_ppm <= 0:
                continue
            mean_cps = float(this_spot[channel].mean())
            if not np.isfinite(mean_cps):
                continue
            cps_vals.append(mean_cps)
            ppm_vals.append(known_ppm)
            sample_labels.append(sample)
        if len(cps_vals) >= 2:
            channel_data[channel] = {
                "cps": np.array(cps_vals),
                "ppm": np.array(ppm_vals),
                "slope": float(calibration[channel]),
                "labels": sample_labels,
            }

    if not channel_data:
        return None
    return {
        "channels": [c for c in channels if c in channel_data],
        "channel_data": channel_data,
    }


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
