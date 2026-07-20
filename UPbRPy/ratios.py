"""Raw isotope-ratio calculations and Tera-Wasserburg projections."""

from __future__ import annotations

import numpy as np
import pandas as pd
import warnings

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
    """Return the projected ``206Pb/238U`` target for TW fractionation."""
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
    '''
    function for propagating the uncertainty of the raw ratios
        using Poisson distribution relative uncertainty where
        uncertainty of each individual analyte is sqrt(cps)/cps
        and the uncertainty of the ratio is given by the following formula:
        relative uncertainty of the ratio = sqrt(1/nom + 1/denom)

        return 2 standard errors of the ratio
    '''
    # calculate the relative uncertainty of the numerator and denominator
    eR = np.sqrt(1/nom + 1/denom)

    return 2 * eR # return 2 standard relative errors of the ratio, not in percentage format but in fraction format

def calc_raw_ratios(
    experiment,
    total_data,
    baseline_subtracted_total_data,
    *,
    region="plateau",
):
    """Calculate sweep-wise raw isotope ratios in a selected spot region.

    ``region='signal'`` returns the inclusive signal-start through signal-end
    interval used to construct a full downhole correction. ``region='plateau'``
    retains the historical half-open plateau interval used for spot means.
    Both outputs carry ``signal_relative_time``, whose zero is the saved signal
    start rather than the first row of the selected region.
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
        "206Pb/204Pb",
        "206Pb/238U",
        "207Pb/206Pb",
        "208Pb/232Th",
        "206Pb/238U_2e",
        "207Pb/206Pb_2e",
        "208Pb/232Th_2e",
    ]
    ratio_frames = []
    skipped_spots = []
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
            ratio_64 = selected["206Pb"] / (
                selected["204Hg"]
                - selected["202Hg"] * (baseline_204 / baseline_202hg)
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
        selected["206Pb/204Pb"] = ratio_64.where(
            np.isfinite(ratio_64) & (ratio_64 >= 0), 0
        )
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
    return result

__all__ = [
    'project_tw_238u_206pb',
    'project_tw_206pb_238u',
    'propagate_raw_ratio_uncertaintiess',
    'calc_raw_ratios',
]
