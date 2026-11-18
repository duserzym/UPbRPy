"""Reference-material date offsets against composition and plateau signal.

Pb concentration is calibrated from 208Pb against natural Pb in NIST glass.
For radiogenic zircon this concentration is not total Pb. REE sums exclude Y.
Accepted dates are calculated separately for each radiogenic ratio; accepted
date uncertainties are not included in the reported spot offsets.
Common-Pb-corrected controls use each material's library initial Pb, falling
back to the reported Stacey–Kramers dates where no initial Pb is documented.
The calibrated date selector follows the session's calibration method.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import t

from .constants import common_U238_U235, lambda_232, lambda_235, lambda_238
from .results import weighted_mean_and_mswd


BIAS_DATE_SYSTEMS = ("206Pb/238U", "207Pb/235U", "207Pb/206Pb", "preferred",
                     "207Pb-corrected")
BIAS_QUANTITIES = {
    "U": "U, ug/g", "Th": "Th, ug/g", "Pb": "Pb, ug/g (208Pb-based)",
    "Th/U": "Th/U", "REE": "Total REE, ug/g", "206Pb/204Pb": "206Pb/204Pb",
    "session_time": "Session time, min", "238U_cps": "238U, counts per second",
    "206Pb_cps": "206Pb, counts per second",
}
_REE = "La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu".split()


def calibrated_bias_date(common_pb_calibration: str | None) -> str:
    """Return the date quantity calibrated by the session's primary material."""
    return "207Pb-corrected" if common_pb_calibration == "tera_wasserburg" else "206Pb/238U"


def bias_date_description(date: str, common_pb_calibration: str | None) -> dict:
    """Describe the selected bias quantity and its common-Pb convention."""
    if date == "calibrated":
        date = calibrated_bias_date(common_pb_calibration)
    if date not in BIAS_DATE_SYSTEMS:
        raise ValueError(f"Unknown bias date system: {date}")
    if date == "207Pb-corrected":
        return {
            "axis": "Offset from accepted 206Pb/238U date (%), "
                    "207Pb-corrected with each material's initial Pb",
            "tooltip": "The 207Pb-corrected 206Pb/238U date uses each material's library "
                       "initial 207Pb/206Pb, with the Stacey–Kramers model where none is documented. "
                       "In a Tera–Wasserburg session the primary should sit at 0 %.",
        }
    tooltip = ("Offsets compare each spot's date with the accepted date for the same system. "
               "Accepted-date uncertainties are excluded.")
    if date == "preferred":
        tooltip = ("Offsets compare each spot's preferred date with the accepted date for its method. "
                   "Accepted-date uncertainties are excluded.")
    if common_pb_calibration == "tera_wasserburg":
        tooltip += (" The primary is calibrated on its common-Pb-corrected 206Pb/238U date "
                    "(207Pb-corrected), so uncorrected dates of common-Pb-bearing materials "
                    "sit above their accepted dates.")
    return {"axis": f"Offset from accepted {date} date (%)", "tooltip": tooltip}


def accepted_dates(standard_archive, names, *, systems=None) -> pd.DataFrame:
    """Return system-specific radiogenic dates in Ma, indexed by material.

    Missing, nonpositive, or physically unsolvable ratios give NaN. The
    207Pb/206Pb date solves the concordia ratio for ages from 0 to 10 Ga.
    ``systems`` may also request 208Pb/232Th; the default retains the original
    three-column interface.
    """
    frame = standard_archive.reindex(list(dict.fromkeys(names)))
    systems = BIAS_DATE_SYSTEMS[:3] if systems is None else tuple(systems)
    if set(systems) - {*BIAS_DATE_SYSTEMS[:3], "208Pb/232Th"}:
        raise ValueError("Unsupported accepted-date system")
    dates = pd.DataFrame(np.nan, index=frame.index, columns=systems)
    for system, decay in (("206Pb/238U", lambda_238), ("207Pb/235U", lambda_235),
                          ("208Pb/232Th", lambda_232)):
        if system not in systems:
            continue
        ratio = _numeric(frame, system + "*")
        dates[system] = np.log1p(ratio.where(ratio > 0)) / decay / 1e6
    for name, ratio in _numeric(frame, "207Pb/206Pb*").items():
        if "207Pb/206Pb" not in systems:
            break
        if not np.isfinite(ratio) or ratio <= 0:
            continue
        def residual(years):
            return (np.expm1(lambda_235 * years)
                    / np.expm1(lambda_238 * years) / common_U238_U235 - ratio)
        try:
            dates.loc[name, "207Pb/206Pb"] = brentq(residual, 1e-3, 1e10) / 1e6
        except ValueError:
            pass
    return dates


def _numeric(frame, column):
    return pd.to_numeric(frame[column], errors="coerce") if column in frame else pd.Series(
        np.nan, index=frame.index, dtype=float)


def _error_column(frame, base, uncertainty):
    suffix = "no_calibration" if uncertainty == "analytical" else "with_calibration"
    column = base + "_2e_" + suffix
    return column if column in frame else base + "_2e"


def reference_corrected_dates(group, material, uncertainty="analytical"):
    """Return reference-initial 207-corrected dates, 2s errors and provenance."""
    from .isochron import apply_common_pb_corrections

    initial = pd.to_numeric(material.get("207Pb/206Pb_i"), errors="coerce")
    if not np.isfinite(initial):
        base = "207Pb_corrected_age"
        return (_numeric(group, base), _numeric(group, _error_column(group, base, uncertainty)),
                "Stacey–Kramers model")
    inputs = pd.DataFrame(index=group.index)
    suffix = "no_calibration" if uncertainty == "analytical" else "with_calibration"
    for ratio in ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th"):
        corrected = ratio + "_fractionation_corrected"
        column = corrected if corrected in group else ratio
        errors = (column + "_2e_" + suffix, ratio + "_2e_" + suffix,
                  column + "_2e", ratio + "_2e")
        error = next((key for key in errors if key in group), ratio + "_2e")
        inputs[ratio], inputs[ratio + "_2e"] = _numeric(group, column), _numeric(group, error)
    inputs["Th/U"] = (_numeric(group, "Th/U") if "Th/U" in group else
                      _numeric(group, "Th") / _numeric(group, "U").replace(0, np.nan))
    initial_68 = pd.to_numeric(material.get("206Pb/208Pb_i"), errors="coerce")
    corrected = apply_common_pb_corrections(
        inputs, common_pb_model=None, common_pb207_pb206=float(initial),
        common_pb206_pb208=float(initial_68) if np.isfinite(initial_68) else 1.,
        include_204_method=False,
        ratio_68_column="206Pb/238U", ratio_68_2s_column="206Pb/238U_2e",
        ratio_76_column="207Pb/206Pb", ratio_76_2s_column="207Pb/206Pb_2e",
        ratio_82_column="208Pb/232Th", ratio_82_2s_column="208Pb/232Th_2e",
    )
    return (corrected["207Pb_corrected_age"], corrected["207Pb_corrected_age_2e"],
            f"initial 207Pb/206Pb {initial:.3f} (reference)")


def spot_signal_quantities(experiment, baseline_subtracted,
                           channels=("206Pb", "238U")) -> pd.DataFrame:
    """Return session minutes and mean plateau cps for every selected spot.

    Selection bounds and sweep Time are session-relative seconds. The upper
    plateau bound is exclusive. Missing channels or empty plateaus give NaN.
    Session minutes use the earliest acquisition in the entire experiment.
    """
    columns = ["spot_name", "session_time", *(channel + "_cps" for channel in channels)]
    first = _numeric(experiment, "spot_starting_times").min()
    sweeps = {name: group for name, group in baseline_subtracted.groupby("spot_name")}
    rows = []
    for selection in experiment.to_dict("records"):
        name = selection["spot_names"]
        row = {"spot_name": name,
               "session_time": (selection["spot_starting_times"] - first) / 60}
        group = sweeps.get(name, baseline_subtracted.iloc[:0])
        times = _numeric(group, "Time")
        plateau = group.loc[(times >= selection["plateau_start_times"])
                            & (times < selection["plateau_end_times"])]
        for channel in channels:
            values = _numeric(plateau, channel)
            row[channel + "_cps"] = values.where(np.isfinite(values)).mean()
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def standard_bias_table(results, standard_archive, *, date="206Pb/238U",
                        quantity="U", standards=None, signal_quantities=None,
                        uncertainty="analytical") -> pd.DataFrame:
    """Return one offset row per reference-material spot, including controls.

    Offsets and their absolute 2s uncertainties are percent of the accepted
    date. Analytical errors prefer age_2e_no_calibration; with_calibration
    prefers age_2e_with_calibration. Both fall back to age_2e when absent.
    Preferred dates always use preferred_age_2e and their per-spot method.
    207Pb-corrected dates use library initial Pb per material and the selected
    ratio-error branch; undocumented initial Pb uses reported corrected dates.
    Nonfinite x or offsets remain in the table with plottable=False. Invalid
    errors remain visible but cannot contribute to weighted statistics.
    """
    if date not in BIAS_DATE_SYSTEMS:
        raise ValueError(f"Unknown bias date system: {date}")
    if quantity not in BIAS_QUANTITIES:
        raise ValueError(f"Unknown bias quantity: {quantity}")
    if uncertainty not in {"analytical", "with_calibration"}:
        raise ValueError(f"Unknown uncertainty mode: {uncertainty}")
    names = standard_archive.index if standards is None else standards
    accepted = accepted_dates(standard_archive, names)
    accepted = accepted.loc[np.isfinite(accepted).any(axis=1)]
    # Glass ratios in the library can include derived radiogenic estimates;
    # their presence does not make the glass a zircon date control.
    materials = standard_archive.reindex(accepted.index)
    glasses = (materials["material"].astype(str).str.contains("glass", case=False, na=False)
               if "material" in materials else pd.Series(False, index=accepted.index))
    accepted = accepted.loc[~glasses & ~accepted.index.isin(["NIST610", "NIST612"])]
    frame = results.loc[results["sample"].isin(accepted.index)].copy().reset_index(drop=True)
    common_pb_source = pd.Series("", index=frame.index)
    if date == "preferred":
        methods = frame.get("preferred_age_method", pd.Series("", index=frame.index))
        systems = methods.map(_preferred_system)
        values, errors = _numeric(frame, "preferred_age"), _numeric(frame, "preferred_age_2e")
    elif date == "207Pb-corrected":
        systems = pd.Series(date, index=frame.index)
        values = pd.Series(np.nan, index=frame.index)
        errors = pd.Series(np.nan, index=frame.index)
        for sample, group in frame.groupby("sample", sort=False):
            dates, date_errors, source = reference_corrected_dates(
                group, standard_archive.loc[sample], uncertainty)
            values.loc[group.index], errors.loc[group.index] = dates, date_errors
            common_pb_source.loc[group.index] = source
    else:
        systems = pd.Series(date, index=frame.index)
        base = date + "_age"
        values = _numeric(frame, base)
        errors = _numeric(frame, _error_column(frame, base, uncertainty))
    reference_systems = systems.replace("207Pb-corrected", "206Pb/238U")
    reference = pd.Series([accepted.loc[sample, system] if system in accepted else np.nan
                           for sample, system in zip(frame["sample"], reference_systems)],
                          index=frame.index, dtype=float)
    if quantity in {"session_time", "206Pb_cps", "238U_cps"}:
        if signal_quantities is None:
            x = pd.Series(np.nan, index=frame.index)
        else:
            signals = signal_quantities.set_index("spot_name")
            if not signals.index.is_unique:
                raise ValueError("signal_quantities must have unique spot_name values")
            x = frame["spot_name"].map(_numeric(signals, quantity))
    elif quantity == "REE":
        present = [column for column in _REE if column in frame]
        x = frame[present].apply(pd.to_numeric, errors="coerce").sum(axis=1, min_count=1)
    elif quantity == "Th/U" and quantity not in frame:
        x = _numeric(frame, "Th") / _numeric(frame, "U").replace(0, np.nan)
    else:
        x = _numeric(frame, quantity)
    output = frame[["spot_name", "sample"]].copy()
    output["x"], output["date"], output["date_2s"] = x, values, errors
    output["accepted"] = reference
    output["offset_percent"] = (values - reference) / reference * 100
    output["offset_2s_percent"] = errors / reference * 100
    output["date_system"], output["quantity"] = systems, quantity
    output["plottable"] = np.isfinite(output["x"]) & np.isfinite(output["offset_percent"])
    output["common_pb_source"] = common_pb_source
    return output


def _preferred_system(method):
    if method in {"207Pb-corrected", "208Pb-corrected"}:
        return "206Pb/238U"
    return next((system for system in BIAS_DATE_SYSTEMS[:3]
                 if str(method).endswith(system)), None)


def _slope_units(table, log_x):
    if log_x:
        return "percent per decade"
    quantity = table["quantity"].iloc[0] if len(table) else "x"
    unit = ("ug/g" if quantity in {"U", "Th", "Pb", "REE"} else
            "min" if quantity == "session_time" else
            "counts per second" if quantity in {"238U_cps", "206Pb_cps"} else quantity)
    return f"percent per {unit}"


def _trend(frame, log_x, units):
    valid = np.isfinite(frame["offset_2s_percent"]) & (frame["offset_2s_percent"] > 0)
    data = frame.loc[valid]
    n = len(data)
    if n < 3:
        return None
    x = data["x"].to_numpy(dtype=float)
    if log_x:
        x = np.log10(x)
    if np.ptp(x) == 0:
        return None
    y = data["offset_percent"].to_numpy(dtype=float)
    weights = 1 / (data["offset_2s_percent"].to_numpy(dtype=float) / 2)**2
    centre = np.average(x, weights=weights)
    dx = x - centre
    spread = np.sum(weights * dx**2)
    if spread <= 0:
        return None
    mean = np.average(y, weights=weights)
    slope = np.sum(weights * dx * (y - mean)) / spread
    intercept = mean - slope * centre
    mswd = np.sum(weights * (y - (mean + slope * dx))**2) / (n - 2)
    se = np.sqrt(max(1., mswd) / spread)
    return {"slope": float(slope), "slope_2s": float(2 * se),
            "intercept": float(intercept), "n": n, "mswd": float(mswd),
            "p_value": float(2 * t.sf(abs(slope / se), n - 2)),
            "units": units, "overdispersed": bool(mswd > 1),
            "x_range": (float(data.x.min()), float(data.x.max()))}


def bias_summary(table, *, log_x=False) -> dict:
    """Return sample means and pooled and per-sample weighted linear trends.

    samples maps material names to weighted_mean_and_mswd results (percent)
    plus x_min, x_max and x_range in original x units. Trend entries are None
    unless at least three points with positive finite 2s errors and some x
    spread exist. Fits use 1/sigma**2; slope_2s is inflated by sqrt(MSWD)
    when MSWD exceeds one, and the two-sided t probability uses that error
    and n-2 degrees of freedom. Log fits exclude nonpositive x from all
    summaries and use log10(x), with slopes in percent per decade.
    """
    finite = np.isfinite(table["x"]) & np.isfinite(table["offset_percent"])
    valid = table.loc[table["plottable"] & finite & ((table["x"] > 0) if log_x else True)]
    units = _slope_units(table, log_x)
    samples, trends = {}, {}
    for sample, group in valid.groupby("sample", sort=True):
        stats = weighted_mean_and_mswd(group["offset_percent"], group["offset_2s_percent"])
        stats.update(x_min=float(group.x.min()), x_max=float(group.x.max()),
                     x_range=(float(group.x.min()), float(group.x.max())))
        samples[sample] = stats
        fit = _trend(group, log_x, units)
        if fit is not None:
            trends[sample] = fit
    return {"samples": samples, "trend": _trend(valid, log_x, units),
            "trend_by_sample": trends}


__all__ = ["BIAS_DATE_SYSTEMS", "BIAS_QUANTITIES", "accepted_dates",
           "reference_corrected_dates",
           "spot_signal_quantities", "standard_bias_table", "bias_summary",
           "calibrated_bias_date", "bias_date_description"]
