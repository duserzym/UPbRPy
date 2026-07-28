"""Raw isotope-ratio calculations and Tera-Wasserburg projections."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import t as student_t
import warnings


def normalize_plateau_filter_method(method) -> str:
    """Return the canonical name for a saved plateau-filter method.

    Parameters
    ----------
    method
        User-facing method name or alias. Matching is case-insensitive and
        ignores spaces, hyphens, and underscores.

    Returns
    -------
    str
        ``"none"`` or the canonical Excel-style two-sigma method name.
    """
    if method is None or (not isinstance(method, str) and pd.isna(method)):
        return "none"
    normalized = str(method).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "none",
        "none": "none",
        "off": "none",
        "no": "none",
        "unfiltered": "none",
        "2sigma": "excel_2sigma",
        "2_sigma": "excel_2sigma",
        "trend_2sigma": "excel_2sigma",
        "excel_2sigma": "excel_2sigma",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise ValueError(
            "plateau filter method must be 'none' or 'excel_2sigma'."
        ) from error


def excel_plateau_inlier_mask(
    time,
    signal,
    *,
    confidence: float = 0.95,
    iterations: int = 3,
) -> np.ndarray:
    """Return an iterative inlier mask around a linear plateau trend.

    The reference VBA describes this as a 2-sigma filter. Its worksheet uses a
    two-sided Student-t confidence multiplier times ``STEYX`` and repeats the
    removal three times for count data. This implementation retains original
    row positions and never treats non-finite measurements as inliers.
    """
    x = np.asarray(time, dtype=float)
    y = np.asarray(signal, dtype=float)
    if x.ndim != 1 or y.ndim != 1 or x.size != y.size:
        raise ValueError(
            "time and signal must be one-dimensional arrays of equal length."
        )
    confidence = float(confidence)
    iterations = int(iterations)
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1.")
    if iterations < 1:
        raise ValueError("iterations must be positive.")

    finite = np.isfinite(x) & np.isfinite(y)
    inliers = finite.copy()
    for _ in range(iterations):
        selected = np.flatnonzero(inliers)
        if selected.size < 3:
            break
        selected_x = x[selected]
        selected_y = y[selected]
        if np.ptp(selected_x) == 0:
            predicted = np.full_like(selected_y, np.mean(selected_y))
        else:
            slope, intercept = np.polyfit(selected_x, selected_y, 1)
            predicted = intercept + slope * selected_x
        residuals = selected_y - predicted
        residual_se = float(
            np.sqrt(np.sum(residuals**2) / max(1, selected.size - 2))
        )
        if not np.isfinite(residual_se) or residual_se == 0:
            break
        # The workbook passes COUNT as TINV's degrees of freedom.
        multiplier = float(student_t.ppf((1 + confidence) / 2, selected.size))
        keep_selected = np.abs(residuals) <= multiplier * residual_se
        updated = np.zeros_like(inliers)
        updated[selected[keep_selected]] = True
        if updated.sum() < 3 or np.array_equal(updated, inliers):
            break
        inliers = updated
    return inliers


def plateau_inlier_mask(time, signal, method="none") -> np.ndarray:
    """Return the Boolean plateau-inlier mask selected by ``method``.

    ``method="none"`` retains all row positions for depth-profile work. The
    Excel-compatible method evaluates residuals about a fitted time trend, so
    a gently sloping plateau is not incorrectly treated as a set of outliers.
    """
    method = normalize_plateau_filter_method(method)
    time_array = np.asarray(time, dtype=float)
    signal_array = np.asarray(signal, dtype=float)
    dimensions_match = (
        time_array.ndim == 1
        and signal_array.ndim == 1
        and time_array.size == signal_array.size
    )
    if not dimensions_match:
        raise ValueError(
            "time and signal must be one-dimensional arrays of equal length."
        )
    if method == "none":
        return np.ones(time_array.size, dtype=bool)
    return excel_plateau_inlier_mask(time_array, signal_array)

def project_tw_238u_206pb(
    corrected_207pb_206pb,
    radiogenic_206pb_238u,
    radiogenic_207pb_206pb,
    common_207pb_206pb,
):
    """Horizontally project a Tera-Wasserburg point onto a known mixing line.

    Parameters
    ----------
    corrected_207pb_206pb : float or array-like
        Pb/Pb-fractionation-corrected total ``207Pb/206Pb`` value(s). This is
        the horizontal coordinate-preserving ordinate in Tera-Wasserburg space.
    radiogenic_206pb_238u : float
        Radiogenic endpoint ``206Pb/238U`` of the reference material.
    radiogenic_207pb_206pb : float
        Radiogenic endpoint ``207Pb/206Pb`` of the reference material.
    common_207pb_206pb : float
        Common-Pb endpoint ``207Pb/206Pb`` at ``238U/206Pb = 0``.

    Returns
    -------
    float or numpy.ndarray
        Projected Tera-Wasserburg x coordinate, ``238U/206Pb``.
    """
    if radiogenic_206pb_238u <= 0:
        raise ValueError("radiogenic_206pb_238u must be positive.")

    radiogenic_238u_206pb = 1 / radiogenic_206pb_238u
    slope = (common_207pb_206pb - radiogenic_207pb_206pb) / (
        0 - radiogenic_238u_206pb
    )
    if slope == 0:
        raise ValueError(
            "Tera-Wasserburg line has zero slope; radiogenic and common "
            "207Pb/206Pb endpoints must differ."
        )

    corrected_207pb_206pb = np.asarray(corrected_207pb_206pb, dtype=float)
    projected = (corrected_207pb_206pb - common_207pb_206pb) / slope
    if projected.ndim == 0:
        return float(projected)
    return projected

def project_tw_206pb_238u(
    corrected_207pb_206pb,
    radiogenic_206pb_238u,
    radiogenic_207pb_206pb,
    common_207pb_206pb,
):
    """Project a total-Pb analysis to radiogenic ``206Pb/238U`` in TW space.

    This reciprocal wrapper returns the Pb/U calibration target after
    horizontally projecting the measured ``207Pb/206Pb`` value onto the line
    between the reference material's radiogenic and common-Pb endmembers.
    """
    projected_238u_206pb = project_tw_238u_206pb(
        corrected_207pb_206pb,
        radiogenic_206pb_238u,
        radiogenic_207pb_206pb,
        common_207pb_206pb,
    )
    projected_array = np.asarray(projected_238u_206pb, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        projected = 1 / projected_array
    if projected.ndim == 0:
        return float(projected)
    return projected

def propagate_raw_ratio_uncertaintiess(nom, denom):
    """Return the two-sigma relative Poisson uncertainty of a raw ratio.

    Parameters
    ----------
    nom, denom
        Integrated numerator and denominator counts. Although the instrument
        columns are labelled ``cps``, the stored values are integrated counts.

    Returns
    -------
    array-like
        Fractional two-sigma uncertainty. Multiplying this value by
        ``nom / denom`` gives the absolute ratio uncertainty.

    Notes
    -----
    For independent Poisson counts and :math:`R=N/D`,
    :math:`\\sigma_R/R = \\sqrt{1/N + 1/D}`.
    """
    # Independent numerator and denominator variances add in relative space.
    eR = np.sqrt(1/nom + 1/denom)

    return 2 * eR

def calc_raw_ratios(
    experiment,
    total_data,
    baseline_subtracted_total_data,
    *,
    region="plateau",
):
    """Calculate sweep-wise raw isotope ratios in a selected spot region.

    Parameters
    ----------
    experiment
        Selection table containing baseline, plateau, and signal boundaries.
    total_data
        Unsubtracted count data. Baseline mass-204 and mass-202 signals are
        used to estimate the session's ``204Hg/202Hg`` ratio.
    baseline_subtracted_total_data
        Background-subtracted sweep data used for isotope-ratio calculation.
    region
        ``"plateau"`` for spot/session calculations or ``"signal"`` for the
        complete depth-profile interval.

    Returns
    -------
    pandas.DataFrame
        Sweep-wise ratios, two-sigma counting uncertainties, Hg-corrected
        ``204Pb``, and time relative to signal start.

    ``region='signal'`` returns the inclusive signal-start through signal-end
    interval used to construct a full downhole correction and never applies a
    plateau filter. ``region='plateau'`` uses the historical half-open plateau
    interval, then applies each spot's saved ``plateau_filter_method`` to the
    saved filter channel. Consequently rejected plateau sweeps are omitted
    from isotope-ratio session calibration and final spot means without
    changing downhole or depth-profile data. Both outputs carry
    ``signal_relative_time``, whose zero is the saved signal start rather than
    the first row of the selected region.
    """
    region = str(region).strip().lower()
    if region not in {"plateau", "signal"}:
        raise ValueError("region must be 'plateau' or 'signal'.")
    required_selection_columns = {
        "spot_names",
        "baseline_start_times",
        "baseline_end_times",
        "signal_start_times",
        "signal_end_times",
    }
    if region == "plateau":
        required_selection_columns.update(
            {"plateau_start_times", "plateau_end_times"}
        )
    missing = required_selection_columns - set(experiment)
    if missing:
        raise KeyError(
            "experiment is missing ratio-selection columns: "
            + ", ".join(sorted(missing))
        )

    output_columns = [
        "sample",
        "spot_name",
        "Time",
        "signal_relative_time",
        "204Pb",
        "204Pb_2e",
        "206Pb/204Pb",
        "206Pb/204Pb_2e",
        "206Pb/208Pb",
        "206Pb/208Pb_2e",
        "238U",
        "206Pb/238U",
        "207Pb/206Pb",
        "208Pb/232Th",
        "206Pb/238U_2e",
        "207Pb/206Pb_2e",
        "208Pb/232Th_2e",
    ]
    ratio_frames = []
    skipped_spots = []
    excluded_plateau_records = []
    applied_plateau_filters = []
    experiment_by_spot = (
        experiment.drop_duplicates("spot_names", keep="first")
        .set_index("spot_names")
    )
    total_by_spot = total_data.groupby("spot_name", sort=False)
    corrected_by_spot = baseline_subtracted_total_data.groupby(
        "spot_name", sort=False
    )

    # Group both source tables once. Calling a boolean full-table filter for
    # every spot makes otherwise simple ratio construction scale quadratically
    # with the number of analyses.
    for spot_name, spot_corrected in corrected_by_spot:
        if spot_name not in experiment_by_spot.index:
            skipped_spots.append(str(spot_name))
            continue
        row = experiment_by_spot.loc[spot_name]
        boundary_names = [
            "baseline_start_times",
            "baseline_end_times",
            "signal_start_times",
            "signal_end_times",
        ]
        if region == "plateau":
            boundary_names.extend(["plateau_start_times", "plateau_end_times"])
        boundaries = pd.to_numeric(row[boundary_names], errors="coerce")
        if boundaries.isna().any():
            skipped_spots.append(str(spot_name))
            continue

        baseline_start = float(boundaries["baseline_start_times"])
        baseline_end = float(boundaries["baseline_end_times"])
        signal_start = float(boundaries["signal_start_times"])
        signal_end = float(boundaries["signal_end_times"])
        if region == "plateau":
            region_start = float(boundaries["plateau_start_times"])
            region_end = float(boundaries["plateau_end_times"])
        else:
            region_start = signal_start
            region_end = signal_end

        if region == "plateau":
            selected = spot_corrected.loc[
                (spot_corrected["Time"] >= region_start)
                & (spot_corrected["Time"] < region_end)
            ].copy()
        else:
            selected = spot_corrected.loc[
                (spot_corrected["Time"] >= region_start)
                & (spot_corrected["Time"] <= region_end)
            ].copy()
        if selected.empty:
            skipped_spots.append(str(spot_name))
            continue

        if region == "plateau":
            filter_method = normalize_plateau_filter_method(
                row.get("plateau_filter_method", "none")
            )
            filter_channel = row.get(
                "plateau_filter_channel",
                row.get("selection_channel"),
            )
            if pd.isna(filter_channel) or not str(filter_channel).strip():
                filter_channel = row.get("selection_channel")
            filter_channel = (
                None if pd.isna(filter_channel) else str(filter_channel)
            )
            if filter_method != "none":
                if filter_channel is None or filter_channel not in selected.columns:
                    raise KeyError(
                        f"Saved plateau filter channel {filter_channel!r} is not "
                        f"available for spot {spot_name!r}."
                    )
                inliers = plateau_inlier_mask(
                    pd.to_numeric(selected["Time"], errors="coerce"),
                    pd.to_numeric(selected[filter_channel], errors="coerce"),
                    filter_method,
                )
                excluded = selected.loc[
                    ~inliers,
                    ["sample", "spot_name", "Time"],
                ].copy()
                if not excluded.empty:
                    excluded["plateau_filter_method"] = filter_method
                    excluded["plateau_filter_channel"] = filter_channel
                    excluded["plateau_filter_signal"] = pd.to_numeric(
                        selected.loc[~inliers, filter_channel], errors="coerce"
                    ).to_numpy()
                    excluded_plateau_records.extend(excluded.to_dict("records"))
                selected = selected.loc[inliers].copy()
            applied_plateau_filters.append(
                {
                    "spot_name": str(spot_name),
                    "plateau_filter_method": filter_method,
                    "plateau_filter_channel": filter_channel,
                }
            )
            if selected.empty:
                skipped_spots.append(str(spot_name))
                continue
        selected["signal_relative_time"] = (
            pd.to_numeric(selected["Time"], errors="coerce") - signal_start
        )

        try:
            spot_total = total_by_spot.get_group(spot_name)
        except KeyError:
            skipped_spots.append(str(spot_name))
            continue
        baseline_total = spot_total.loc[
            (spot_total["Time"] >= baseline_start)
            & (spot_total["Time"] < baseline_end)
        ]
        baseline_202hg = baseline_total["202Hg"].mean()
        baseline_204 = baseline_total["204Hg"].mean()

        with np.errstate(divide="ignore", invalid="ignore"):
            hg_204_scale = baseline_204 / baseline_202hg
            pb_204 = selected["204Hg"] - selected["202Hg"] * hg_204_scale
            # CPS values are integrated counts in the input format. Treat the
            # two independent mass measurements as Poisson variables. The
            # uncertainty of the baseline-derived 204Hg/202Hg scale is not
            # included because the legacy Excel routine likewise treats that
            # scale as fixed.
            pb_204_1s = np.sqrt(
                np.clip(selected["204Hg"], 0, None)
                + hg_204_scale**2 * np.clip(selected["202Hg"], 0, None)
            )
            ratio_64 = selected["206Pb"] / pb_204
            ratio_64_2e = 2 * np.abs(ratio_64) * np.sqrt(
                1 / np.clip(selected["206Pb"], np.finfo(float).tiny, None)
                + (pb_204_1s / pb_204) ** 2
            )
            ratio_68pb = selected["206Pb"] / selected["208Pb"]
            ratio_68pb_2e = (
                propagate_raw_ratio_uncertaintiess(
                    selected["206Pb"], selected["208Pb"]
                )
                * ratio_68pb
            )
            ratio_82 = selected["208Pb"] / selected["232Th"]
            ratio_68 = selected["206Pb"] / selected["238U"]
            ratio_76 = selected["207Pb"] / selected["206Pb"]
            ratio_82_2e = (
                propagate_raw_ratio_uncertaintiess(
                    selected["208Pb"], selected["232Th"]
                )
                * ratio_82
            )
            ratio_68_2e = (
                propagate_raw_ratio_uncertaintiess(
                    selected["206Pb"], selected["238U"]
                )
                * ratio_68
            )
            ratio_76_2e = (
                propagate_raw_ratio_uncertaintiess(
                    selected["207Pb"], selected["206Pb"]
                )
                * ratio_76
            )
        selected["204Pb"] = pb_204.where(np.isfinite(pb_204), np.nan)
        selected["204Pb_2e"] = (2 * pb_204_1s).where(
            np.isfinite(pb_204_1s), np.nan
        )
        selected["206Pb/204Pb"] = ratio_64.where(
            np.isfinite(ratio_64) & (ratio_64 >= 0), 0
        )
        selected["206Pb/204Pb_2e"] = ratio_64_2e.where(
            np.isfinite(ratio_64_2e) & (ratio_64 > 0), np.nan
        )
        selected["206Pb/208Pb"] = ratio_68pb
        selected["206Pb/208Pb_2e"] = ratio_68pb_2e
        selected["206Pb/238U"] = ratio_68
        selected["206Pb/238U_2e"] = ratio_68_2e
        selected["207Pb/206Pb"] = ratio_76
        selected["207Pb/206Pb_2e"] = ratio_76_2e
        selected["208Pb/232Th"] = ratio_82
        selected["208Pb/232Th_2e"] = ratio_82_2e
        ratio_frames.append(selected.loc[:, output_columns])

    if skipped_spots:
        warnings.warn(
            "Raw ratios skipped spots with incomplete or empty selections: "
            + ", ".join(skipped_spots),
            RuntimeWarning,
            stacklevel=2,
        )
    if not ratio_frames:
        return pd.DataFrame(columns=output_columns)
    result = pd.concat(ratio_frames, ignore_index=True)
    result.attrs["selection_region"] = region
    result.attrs["relative_time_origin"] = "signal_start_times"
    result.attrs["skipped_spots"] = tuple(skipped_spots)
    result.attrs["plateau_filter_excluded"] = pd.DataFrame(
        excluded_plateau_records
    )
    result.attrs["plateau_filter_excluded_count"] = len(excluded_plateau_records)
    result.attrs["plateau_filters"] = pd.DataFrame(applied_plateau_filters)
    return result

__all__ = [
    'normalize_plateau_filter_method',
    'excel_plateau_inlier_mask',
    'plateau_inlier_mask',
    'project_tw_238u_206pb',
    'project_tw_206pb_238u',
    'propagate_raw_ratio_uncertaintiess',
    'calc_raw_ratios',
]
