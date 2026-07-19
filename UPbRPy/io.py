"""Experiment I/O and reusable baseline/signal-window selection strategies.

Selection indices use the same boundary convention as the original UPbRPy
workflow: starts are inclusive and ends identify the time boundary used by
downstream ``start <= Time < end`` slicing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


class SelectionError(ValueError):
    """Raised when a signal trace cannot produce valid selection windows."""


@dataclass(frozen=True)
class SelectionResult:
    """Indices and diagnostics produced by a selection strategy.

    Baseline and plateau ends are exclusive slicing boundaries. ``signal_end``
    is instead the inclusive index of the last sample-derived sweep so the two
    highlighted signal-edge measurements are unambiguous.
    """

    plateau_start: int
    plateau_end: int
    baseline_start: int
    baseline_end: int
    detection_index: int
    method: str
    signal_end: int
    washout_start: int | None = None
    diagnostics: Mapping[str, float | int | str] = field(default_factory=dict)

    @property
    def signal_start(self) -> int:
        """Return the inclusive first detected sample-signal index."""
        return self.detection_index

    @property
    def post_baseline_start(self) -> int | None:
        """Compatibility alias for the first detected washout sweep."""
        return self.washout_start

    def as_tuple(self) -> tuple[int, int, int, int]:
        """Return the historical UPbRPy index ordering."""
        return (
            self.plateau_start,
            self.plateau_end,
            self.baseline_start,
            self.baseline_end,
        )


Selector = Callable[..., tuple[int, Mapping[str, float | int | str]]]
_SELECTORS: dict[str, Selector] = {}
_ALIASES: dict[str, str] = {}


def register_selection_method(
    name: str,
    selector: Selector,
    *,
    aliases: tuple[str, ...] = (),
) -> None:
    """Register a signal-onset detector for use by ``select_signal_regions``."""
    canonical = normalize_method_name(name)
    if not canonical:
        raise ValueError("Selection method name cannot be empty.")
    _SELECTORS[canonical] = selector
    _ALIASES[canonical] = canonical
    for alias in aliases:
        _ALIASES[normalize_method_name(alias)] = canonical


def available_selection_methods() -> tuple[str, ...]:
    """Return the canonical registered selection method names."""
    return tuple(_SELECTORS)


def selection_method_label(method: str) -> str:
    """Return a concise label suitable for notebook output and GUI controls."""
    canonical = resolve_method(method)
    return {
        "threshold": "Signal threshold",
        "rolling_sigma": "Running mean + σ threshold",
        "excel_expanding_sigma": "Excel expanding mean + σ threshold",
    }.get(canonical, canonical.replace("_", " ").title())


def normalize_method_name(name: str) -> str:
    return str(name).strip().lower().replace("-", "_").replace(" ", "_")


def resolve_method(method: str) -> str:
    normalized = normalize_method_name(method)
    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        choices = ", ".join(available_selection_methods())
        raise ValueError(f"Unknown selection method {method!r}. Choose from: {choices}.") from exc


def coerce_trace(data) -> np.ndarray:
    values = np.asarray(data, dtype=float)
    if values.ndim != 1:
        raise SelectionError("Selection data must be one-dimensional.")
    if values.size < 3:
        raise SelectionError("Selection data must contain at least three sweeps.")
    finite = np.isfinite(values)
    if not np.any(finite):
        raise SelectionError("Selection data contain no finite values.")
    if not np.all(finite):
        indices = np.arange(values.size)
        values = values.copy()
        values[~finite] = np.interp(indices[~finite], indices[finite], values[finite])
    return values


def validate_common_options(
    size: int,
    baseline_sweep_n: int,
    peak_sweep_n: int,
    baseline_offset: int,
    plateau_offset: int,
) -> tuple[int, int, int, int]:
    baseline_sweep_n = int(baseline_sweep_n)
    peak_sweep_n = int(peak_sweep_n)
    baseline_offset = int(baseline_offset)
    plateau_offset = int(plateau_offset)
    if baseline_sweep_n < 1 or peak_sweep_n < 1:
        raise ValueError("baseline_sweep_n and peak_sweep_n must be positive.")
    if baseline_offset < 0 or plateau_offset < 0:
        raise ValueError("baseline_offset and plateau_offset cannot be negative.")
    if baseline_sweep_n >= size:
        raise SelectionError(
            f"baseline_sweep_n={baseline_sweep_n} leaves no signal data in a {size}-sweep trace."
        )
    return baseline_sweep_n, peak_sweep_n, baseline_offset, plateau_offset


def selection_windows(
    detection_index: int,
    *,
    size: int,
    baseline_sweep_n: int,
    peak_sweep_n: int,
    baseline_offset: int,
    plateau_offset: int,
    signal_end: int | None = None,
) -> tuple[int, int, int, int]:
    baseline_end = detection_index - baseline_offset
    baseline_start = baseline_end - baseline_sweep_n
    plateau_start = detection_index + plateau_offset

    # Retain the original fallback for signals detected too early to provide a
    # complete baseline window.
    if baseline_start < 0:
        baseline_start = 0
        baseline_end = baseline_sweep_n
        plateau_start = baseline_sweep_n + plateau_offset

    last_boundary = size - 1
    baseline_start = min(max(0, baseline_start), last_boundary)
    baseline_end = min(max(baseline_start + 1, baseline_end), last_boundary)
    plateau_start = min(max(baseline_end, plateau_start), last_boundary)
    plateau_stop_boundary = last_boundary
    if signal_end is not None:
        plateau_stop_boundary = min(plateau_stop_boundary, int(signal_end) + 1)
    plateau_end = min(plateau_start + peak_sweep_n, plateau_stop_boundary)
    if plateau_end <= plateau_start:
        raise SelectionError(
            "The detected signal returns to baseline before a usable plateau window begins."
        )
    return plateau_start, plateau_end, baseline_start, baseline_end


def find_signal_end(
    values: np.ndarray,
    *,
    signal_start: int,
    baseline_start: int,
    baseline_end: int,
    plateau_start: int,
    plateau_end: int,
    sigma_threshold: float = 3.0,
    consecutive: int = 2,
    fraction_threshold: float = 0.8,
) -> tuple[int, int | None, Mapping[str, float | int | str]]:
    """Find the final stable signal sweep before the falling washout edge.

    The preceding plateau sweeps supply a rolling robust linear signal trend.
    The first sustained run below that trend by
    ``sigma_threshold`` robust residual standard deviations starts washout;
    the preceding sweep is the inclusive signal end. This deliberately marks
    the falling edge rather than waiting for the washout tail to reach the
    instrumental baseline.
    """
    sigma_threshold = float(sigma_threshold)
    consecutive = int(consecutive)
    fraction_threshold = float(fraction_threshold)
    if sigma_threshold <= 0:
        raise ValueError("signal_end_sigma_threshold must be positive.")
    if consecutive < 1:
        raise ValueError("signal_end_consecutive must be positive.")
    if not 0 < fraction_threshold < 1:
        raise ValueError("signal_end_fraction_threshold must be between 0 and 1.")

    baseline = values[int(baseline_start) : int(baseline_end)]
    if baseline.size < 2:
        raise SelectionError("At least two baseline sweeps are needed to detect signal end.")
    baseline_center = float(np.median(baseline))
    median_absolute_deviation = float(np.median(np.abs(baseline - baseline_center)))
    baseline_std = 1.4826 * median_absolute_deviation
    if baseline_std == 0:
        q25, q75 = np.percentile(baseline, [25, 75])
        baseline_std = float((q75 - q25) / 1.349)
    plateau_start = int(plateau_start)
    plateau_end = int(plateau_end)
    rolling_window = min(8, plateau_end - plateau_start)
    if rolling_window < 3:
        raise SelectionError("At least three plateau sweeps are needed to detect signal end.")
    search_start = max(plateau_start + rolling_window, int(signal_start) + 3)
    search_stop = values.size - consecutive + 1
    signal_slope = 0.0
    signal_intercept = float(np.median(values[plateau_start:plateau_end]))
    residual_std = 0.0
    detection_scale = max(baseline_std, abs(signal_intercept) * 0.01)
    for index in range(search_start, search_stop):
        reference_indices = np.arange(index - rolling_window, index, dtype=float)
        reference_values = values[index - rolling_window : index]
        pair_slopes = []
        for left in range(reference_values.size - 1):
            for right in range(left + 1, reference_values.size):
                pair_slopes.append(
                    (reference_values[right] - reference_values[left])
                    / (reference_indices[right] - reference_indices[left])
                )
        signal_slope = float(np.median(pair_slopes))
        signal_intercept = float(
            np.median(reference_values - signal_slope * reference_indices)
        )
        reference_prediction = signal_intercept + signal_slope * reference_indices
        residuals = reference_values - reference_prediction
        residual_center = float(np.median(residuals))
        residual_std = 1.4826 * float(
            np.median(np.abs(residuals - residual_center))
        )
        if residual_std == 0:
            residual_std = float(np.std(residuals, ddof=1))
        signal_level = float(np.median(np.abs(reference_values)))
        detection_scale = max(residual_std, baseline_std, signal_level * 0.01)
        candidate_indices = np.arange(index, index + consecutive, dtype=float)
        expected = signal_intercept + signal_slope * candidate_indices
        lower_threshold = expected - sigma_threshold * detection_scale
        candidate_values = values[index : index + consecutive]
        material_drop = np.min(candidate_values) < np.median(reference_values) * fraction_threshold
        if np.all(candidate_values < lower_threshold) and material_drop:
            return index - 1, index, {
                "signal_end_state": "falling_edge_detected",
                "signal_end_threshold": float(lower_threshold[0]),
                "signal_end_baseline_center": baseline_center,
                "signal_end_baseline_std": baseline_std,
                "signal_end_signal_slope": signal_slope,
                "signal_end_signal_intercept": signal_intercept,
                "signal_end_residual_std": residual_std,
                "signal_end_detection_scale": detection_scale,
                "signal_end_rolling_window": rolling_window,
                "signal_end_sigma_threshold": sigma_threshold,
                "signal_end_consecutive": consecutive,
                "signal_end_fraction_threshold": fraction_threshold,
            }
    return values.size - 1, None, {
        "signal_end_state": "incomplete",
        "signal_end_threshold": float("nan"),
        "signal_end_baseline_center": baseline_center,
        "signal_end_baseline_std": baseline_std,
        "signal_end_signal_slope": signal_slope,
        "signal_end_signal_intercept": signal_intercept,
        "signal_end_residual_std": residual_std,
        "signal_end_detection_scale": detection_scale,
        "signal_end_rolling_window": rolling_window,
        "signal_end_sigma_threshold": sigma_threshold,
        "signal_end_consecutive": consecutive,
        "signal_end_fraction_threshold": fraction_threshold,
    }


def threshold_onset(
    values: np.ndarray,
    *,
    threshold: float = 0.1,
    filter_percentile: float = 70,
    baseline_sweep_n: int = 0,
    **_: object,
) -> tuple[int, Mapping[str, float | int | str]]:
    percentile = float(filter_percentile)
    if not 0 <= percentile <= 100:
        raise ValueError("filter_percentile must be between 0 and 100.")
    percentile_value = float(np.percentile(values, percentile))
    rough_plateau = values[values > percentile_value]
    if rough_plateau.size == 0:
        rough_plateau = values[np.argmax(values) : np.argmax(values) + 1]
    threshold_value = float(np.mean(rough_plateau) * float(threshold))
    search_start = max(0, int(baseline_sweep_n))
    hits = np.flatnonzero(values[search_start:] > threshold_value) + search_start
    if hits.size == 0:
        raise SelectionError(
            f"No signal exceeded the calculated threshold ({threshold_value:g})."
        )
    return int(hits[0]), {
        "threshold_value": threshold_value,
        "filter_percentile": percentile,
        "rough_plateau_mean": float(np.mean(rough_plateau)),
    }


def rolling_sigma_onset(
    values: np.ndarray,
    *,
    baseline_sweep_n: int,
    rolling_window: int = 5,
    sigma_threshold: float = 5.0,
    consecutive: int = 1,
    **_: object,
) -> tuple[int, Mapping[str, float | int | str]]:
    rolling_window = int(rolling_window)
    consecutive = int(consecutive)
    sigma_threshold = float(sigma_threshold)
    if rolling_window < 2:
        raise ValueError("rolling_window must be at least 2.")
    if consecutive < 1:
        raise ValueError("consecutive must be positive.")
    if sigma_threshold <= 0:
        raise ValueError("sigma_threshold must be positive.")

    search_start = max(int(baseline_sweep_n), rolling_window)
    search_stop = values.size - consecutive + 1
    for index in range(search_start, search_stop):
        reference = values[index - rolling_window : index]
        running_mean = float(np.mean(reference))
        running_std = float(np.std(reference, ddof=1))
        threshold_value = running_mean + sigma_threshold * running_std
        if np.all(values[index : index + consecutive] > threshold_value):
            return index, {
                "threshold_value": threshold_value,
                "running_mean": running_mean,
                "running_std": running_std,
                "rolling_window": rolling_window,
                "sigma_threshold": sigma_threshold,
                "consecutive": consecutive,
            }
    raise SelectionError(
        "No signal onset exceeded the running baseline mean by "
        f"{sigma_threshold:g} standard deviations."
    )


def excel_expanding_sigma_onset(
    values: np.ndarray,
    *,
    initial_window: int = 6,
    sigma_threshold: float = 8.0,
    consecutive: int = 1,
    **_: object,
) -> tuple[int, Mapping[str, float | int | str]]:
    """Reproduce the onset detector in the reference workbook's VBA.

    The VBA starts with six sweeps, expands the reference range by one sweep on
    each iteration, and tests the following value against mean + 8 sample
    standard deviations. ``consecutive`` is an optional Python-side safeguard;
    its default of one exactly matches the macro.
    """
    initial_window = int(initial_window)
    consecutive = int(consecutive)
    sigma_threshold = float(sigma_threshold)
    if initial_window < 2:
        raise ValueError("initial_window must be at least 2.")
    if consecutive < 1:
        raise ValueError("consecutive must be positive.")
    if sigma_threshold <= 0:
        raise ValueError("sigma_threshold must be positive.")

    search_stop = values.size - consecutive + 1
    for index in range(initial_window, search_stop):
        reference = values[:index]
        expanding_mean = float(np.mean(reference))
        expanding_std = float(np.std(reference, ddof=1))
        threshold_value = expanding_mean + sigma_threshold * expanding_std
        if np.all(values[index : index + consecutive] > threshold_value):
            return index, {
                "threshold_value": threshold_value,
                "expanding_mean": expanding_mean,
                "expanding_std": expanding_std,
                "reference_sweeps": index,
                "initial_window": initial_window,
                "sigma_threshold": sigma_threshold,
                "consecutive": consecutive,
                "source": "Zircon_28Mar25_Zhang1-i193-LA-UPbR22.xlsm Module1.ImportRawData",
            }
    raise SelectionError(
        "No signal onset exceeded the expanding baseline mean by "
        f"{sigma_threshold:g} standard deviations."
    )


register_selection_method("threshold", threshold_onset, aliases=("percentile_threshold",))
register_selection_method(
    "rolling_sigma",
    rolling_sigma_onset,
    aliases=("running_sigma", "running_mean_sigma", "running_5sigma"),
)
register_selection_method(
    "excel_expanding_sigma",
    excel_expanding_sigma_onset,
    aliases=("excel_vba", "expanding_sigma"),
)

SELECTION_METHODS = available_selection_methods()


def select_signal_regions(
    data,
    *,
    method: str = "threshold",
    baseline_sweep_n: int = 10,
    peak_sweep_n: int = 20,
    threshold: float = 0.1,
    filter_percentile: float = 70,
    baseline_offset: int = 2,
    plateau_offset: int = 2,
    signal_end_sigma_threshold: float = 3.0,
    signal_end_consecutive: int = 2,
    signal_end_fraction_threshold: float = 0.8,
    signal_minimum_relative_increase: float = 0.1,
    **method_options,
) -> SelectionResult:
    """Select baseline and plateau windows using a registered onset detector.

    ``threshold`` reproduces the original percentile-based algorithm.
    ``rolling_sigma`` compares each candidate onset against the mean and sample
    standard deviation of the preceding ``rolling_window`` sweeps.
    ``excel_expanding_sigma`` reproduces the reference workbook's expanding
    mean plus eight-standard-deviation onset detector.
    """
    values = coerce_trace(data)
    common = validate_common_options(
        values.size,
        baseline_sweep_n,
        peak_sweep_n,
        baseline_offset,
        plateau_offset,
    )
    baseline_sweep_n, peak_sweep_n, baseline_offset, plateau_offset = common
    canonical = resolve_method(method)
    detection_index, diagnostics = _SELECTORS[canonical](
        values,
        baseline_sweep_n=baseline_sweep_n,
        threshold=threshold,
        filter_percentile=filter_percentile,
        **method_options,
    )
    initial_windows = selection_windows(
        detection_index,
        size=values.size,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
    )
    plateau_start, plateau_end, baseline_start, baseline_end = initial_windows
    baseline_reference = values[baseline_start:baseline_end]
    plateau_reference = values[plateau_start : min(plateau_end, plateau_start + 8)]
    baseline_center = float(np.median(baseline_reference))
    baseline_mad = float(np.median(np.abs(baseline_reference - baseline_center)))
    baseline_scale = 1.4826 * baseline_mad
    if baseline_scale == 0:
        baseline_scale = float(np.std(baseline_reference, ddof=1))
    relative_increase = float(signal_minimum_relative_increase)
    if relative_increase <= 0:
        raise ValueError("signal_minimum_relative_increase must be positive.")
    minimum_signal_level = max(
        baseline_center + 3 * baseline_scale,
        baseline_center * (1 + relative_increase),
    )
    if plateau_reference.size < 3 or float(np.median(plateau_reference)) <= minimum_signal_level:
        raise SelectionError(
            "The detected onset does not produce a plateau distinguishable from baseline."
        )
    signal_end, washout_start, end_diagnostics = find_signal_end(
        values,
        signal_start=detection_index,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        plateau_start=plateau_start,
        plateau_end=plateau_end,
        sigma_threshold=signal_end_sigma_threshold,
        consecutive=signal_end_consecutive,
        fraction_threshold=signal_end_fraction_threshold,
    )
    windows = selection_windows(
        detection_index,
        size=values.size,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
        signal_end=signal_end,
    )
    return SelectionResult(
        *windows,
        detection_index=detection_index,
        method=canonical,
        signal_end=signal_end,
        washout_start=washout_start,
        diagnostics={**diagnostics, **end_diagnostics},
    )


def select_baseline_peak(
    data,
    baseline_sweep_n: int = 10,
    peak_sweep_n: int = 20,
    threshold: float = 0.1,
    filter_percentile: float = 70,
    baseline_offset: int = 2,
    plateau_offset: int = 2,
    *,
    method: str = "threshold",
    **method_options,
) -> tuple[int, int, int, int]:
    """Compatibility wrapper returning the historical four-index tuple."""
    return select_signal_regions(
        data,
        method=method,
        baseline_sweep_n=baseline_sweep_n,
        peak_sweep_n=peak_sweep_n,
        threshold=threshold,
        filter_percentile=filter_percentile,
        baseline_offset=baseline_offset,
        plateau_offset=plateau_offset,
        **method_options,
    ).as_tuple()


def read_experiment_data(directory, primary_channel='29Si', baseline_sweep_n =10, peak_sweep_n = 20, threshold=0.1, filter_percentile=70, baseline_offset=2, plateau_offset=2, sep='_', selection_method='threshold', selection_options=None):
    '''
    function for extracting spot names from files exported from Thermo iCap

    You may follow the instruction on Iolite website below for guidance on generating the proper files from iCap
    https://iolite.xyz/docs/import_prep/exporting_data_from_instruments.html

    Parameters
    ----------
    directory : str
        path to the folder containing the files
        we assume that the files in the folders are in .csv format

    Returns
    -------
    spot_names : list
        list of spot names extracted from the files
    spot_paths : list
        list of paths to the files
    '''
    spot_names = []
    spot_paths = []
    spot_starting_times = []
    # use the first .csv file to get the channel names
    first_file = open(os.path.join(directory,[f if f.endswith('.csv') else None for f in os.listdir(directory)][0])).readlines()

    # now update the global variable channels
    element_channels = first_file[13].split(',')[1:-1]

    time_format = "%m/%d/%Y %I:%M:%S %p"

    # make a pandas dataframe that stitches all data of all spots together
    total_data = pd.DataFrame()

    # loop through all the .csv files in the directory using io and get their first lines
    for file in os.listdir(directory):
        if file.endswith('.csv'):
            # use os tools to get the time of start, spot name and path of the file
            this_path = os.path.join(directory, file)
            spot_paths.append(this_path)
            file = open(this_path)
            lines = file.readlines()
            this_spot_name = lines[0].split(':')[0]
            spot_names.append(this_spot_name)
            this_starting_time = datetime.strptime(lines[0][-24:-2], time_format)
            spot_starting_times.append(this_starting_time)
            file.close()
        # create a pandas dataframe to store the experiment data
    experiment = pd.DataFrame()
    experiment['spot_names'] = spot_names
    experiment['sample'] = [spot_name.split(sep)[0] for spot_name in spot_names]
    experiment['spot_paths'] = spot_paths
    experiment['spot_starting_times'] = spot_starting_times

    experiment = experiment.sort_values(by='spot_starting_times').reset_index(drop=True)
    for i in range(experiment.shape[0]):
        # then use pandas to read csv files and append them to the total data
        this_data = pd.read_csv(experiment['spot_paths'][i], skiprows=13)
        this_data = this_data.iloc[1:].reset_index(drop=True)
        this_data = this_data.dropna(axis=1, how='all')
        this_data = this_data.apply(pd.to_numeric, errors='coerce')
        # make sure to change the time column to datetime and add start time to all the times
        this_data['Time'] = [experiment['spot_starting_times'][i] + timedelta(seconds=this_data['Time'][j]) for j in range(this_data.shape[0])]
        this_data['spot_name'] = experiment['spot_names'][i]
        this_data['sample'] = experiment['sample'][i]
        total_data = pd.concat([total_data, this_data], axis=0)

    experiment = experiment.sort_values(by='spot_names').reset_index(drop=True)
    #initialize the columns for the experiment dataframe with null values

    # we should also align the time column in both the experiment and total data to be in total seconds with respect to the start of the experiment
    # this will help with the later session-wide fractionation corrections
    experiment_start_time = experiment['spot_starting_times'].min()
    experiment['spot_starting_times'] = (experiment['spot_starting_times'] - experiment_start_time).dt.total_seconds()
    total_data['Time'] = (total_data['Time'] - experiment_start_time).dt.total_seconds()

    experiment['plateau_start_times'] = None
    experiment['plateau_end_times'] = None
    experiment['baseline_start_times'] = None
    experiment['baseline_end_times'] = None
    experiment['signal_start_times'] = None
    experiment['signal_end_times'] = None

    experiment = auto_select_baseline_peak(
        experiment,
        total_data,
        primary_channel,
        baseline_sweep_n,
        peak_sweep_n,
        threshold,
        filter_percentile,
        baseline_offset,
        plateau_offset,
        selection_method=selection_method,
        selection_options=selection_options,
    )

    return experiment, element_channels, total_data

def read_spot_data(spot_path):
    '''
    function for reading spot data from a .csv file exported from Thermo iCap

    Parameters
    ----------
    spot_path : str
        path to the file containing the spot data

    Returns
    -------
    spot_data : pandas.DataFrame
        a pandas dataframe containing the spot data
    '''
    spot_data = pd.read_csv(spot_path, skiprows=13)
    spot_data = spot_data.iloc[1:].reset_index(drop=True)
    spot_data = spot_data.dropna(axis=1, how='all')
    spot_data = spot_data.apply(pd.to_numeric, errors='coerce')
    return spot_data

def auto_select_baseline_peak(experiment,
                              total_data,
                              primary_channel,
                              baseline_sweep_n =10,
                              peak_sweep_n = 20,
                              threshold=0.1,
                              filter_percentile=70,
                              baseline_offset=2,
                              plateau_offset=2,
                              selection_method='threshold',
                              selection_options: Mapping[str, object] | None = None):
    '''
    specific function for selecting the baseline and peak regions in the data given a parsed experiment dataframe

    Parameters
    ----------
    experiment: pandas.DataFrame
        a pandas dataframe containing the experiment metadata
    total_data : pandas.DataFrame
        a pandas dataframe containing the total data
    baseline_z_threshold : float
        the threshold for selecting the baseline region
    plateau_z_threshold : float
        the threshold for selecting the peak region

    Returns
    -------
    experiment : pandas.DataFrame
        a pandas dataframe containing the spot data with the baseline and peak regions selected
    '''
    spot_names = total_data['spot_name'].unique()
    method_options = dict(selection_options or {})

    for spot_name in spot_names:
        spot_data = total_data[total_data['spot_name'] == spot_name]
        # always use 29Si for glass
        if 'NIST' in spot_name:
            values = spot_data['29Si'].tolist()
        else:
            values = spot_data[primary_channel].tolist()
        row_mask = experiment['spot_names'] == spot_name
        try:
            selection = select_signal_regions(
                values,
                method=selection_method,
                baseline_sweep_n=baseline_sweep_n,
                peak_sweep_n=peak_sweep_n,
                threshold=threshold,
                filter_percentile=filter_percentile,
                baseline_offset=baseline_offset,
                plateau_offset=plateau_offset,
                **method_options,
            )
        except SelectionError as error:
            experiment.loc[row_mask, 'selection_method'] = resolve_method(selection_method)
            experiment.loc[row_mask, 'selection_channel'] = '29Si' if 'NIST' in spot_name else primary_channel
            experiment.loc[row_mask, 'selection_error'] = str(error)
            continue
        experiment.loc[row_mask, 'plateau_start_times'] = spot_data.iloc[selection.plateau_start]['Time']
        experiment.loc[row_mask, 'plateau_end_times'] = spot_data.iloc[selection.plateau_end]['Time']
        experiment.loc[row_mask, 'baseline_start_times'] = spot_data.iloc[selection.baseline_start]['Time']
        experiment.loc[row_mask, 'baseline_end_times'] = spot_data.iloc[selection.baseline_end]['Time']
        experiment.loc[row_mask, 'signal_start_times'] = spot_data.iloc[selection.signal_start]['Time']
        experiment.loc[row_mask, 'signal_end_times'] = spot_data.iloc[selection.signal_end]['Time']
        experiment.loc[row_mask, 'selection_method'] = selection.method
        experiment.loc[row_mask, 'selection_channel'] = '29Si' if 'NIST' in spot_name else primary_channel
        experiment.loc[row_mask, 'selection_detection_index'] = selection.detection_index
        experiment.loc[row_mask, 'selection_signal_end_index'] = selection.signal_end
        experiment.loc[row_mask, 'selection_washout_start_index'] = selection.washout_start
        experiment.loc[row_mask, 'selection_post_baseline_start_index'] = selection.washout_start
        if 'sigma_threshold' in selection.diagnostics:
            experiment.loc[row_mask, 'selection_sigma_threshold'] = selection.diagnostics['sigma_threshold']
        experiment.loc[row_mask, 'selection_error'] = None

    return experiment

def subtract_baseline(experiment, channels, total_data):
    '''
    function for subtracting the baseline from the total data

    Parameters
    ----------
    experiment : pandas.DataFrame
        a pandas dataframe containing the experiment metadata
    channels : list
        a list of the channels in the data
    total_data : pandas.DataFrame
        a pandas dataframe containing the total data

    Returns
    -------
    baseline_corrected_total_data : pandas.DataFrame
        a pandas dataframe containing the baseline corrected data
    '''
    baseline_corrected_total_data = pd.DataFrame()
    for spot_name in total_data['spot_name'].unique():
        this_spot_data = total_data[total_data['spot_name'] == spot_name]
        # get the baseline start and end times for this spot
        baseline_start_time = experiment[experiment['spot_names'] == spot_name]['baseline_start_times'].values[0]
        baseline_end_time = experiment[experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]
        this_spot_baseline = this_spot_data[(this_spot_data['Time'] >= baseline_start_time) & (this_spot_data['Time'] < baseline_end_time)]
        for channel in channels:
            this_spot_data.loc[:, channel] = this_spot_data[channel] - this_spot_baseline[channel].mean()
        baseline_corrected_total_data = pd.concat([baseline_corrected_total_data, this_spot_data])

    baseline_corrected_total_data.sort_values(by='Time', inplace=True, ignore_index=True)
    return baseline_corrected_total_data


__all__ = [
    'SELECTION_METHODS',
    'SelectionError',
    'SelectionResult',
    'available_selection_methods',
    'register_selection_method',
    'selection_method_label',
    'select_baseline_peak',
    'select_signal_regions',
    'read_experiment_data',
    'read_spot_data',
    'auto_select_baseline_peak',
    'subtract_baseline',
    'normalize_method_name',
    'resolve_method',
    'coerce_trace',
    'validate_common_options',
    'selection_windows',
    'find_signal_end',
    'threshold_onset',
    'rolling_sigma_onset',
    'excel_expanding_sigma_onset',
]
