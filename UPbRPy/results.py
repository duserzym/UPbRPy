"""Post-reduction decisions, quality flags, summaries, and table exports.

The numerical reduction deliberately retains parallel alternatives. This
module is the narrow decision layer that converts those alternatives into a
succinct, reproducible result table without deleting the underlying columns.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re

import numpy as np
import pandas as pd

from .constants import (
    common_U238_U235,
    lambda_232,
    lambda_235,
    lambda_238,
    sigma_232,
    sigma_235,
    sigma_238,
    sigma_common_U238_U235,
)


def format_value_with_uncertainty(value, two_se, sig_figs=2) -> str:
    """Format a value and absolute 2SE at the uncertainty's decimal precision."""
    if not isinstance(sig_figs, int) or sig_figs < 1:
        raise ValueError("sig_figs must be a positive integer")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(value):
        return ""
    try:
        error = float(two_se)
    except (TypeError, ValueError):
        error = np.nan
    if not np.isfinite(error) or error <= 0:
        return f"{value:.6g}"
    places = sig_figs - 1 - int(np.floor(np.log10(error)))
    error = round(error, places)
    # Rounding can carry the uncertainty into the next power of ten.
    places = sig_figs - 1 - int(np.floor(np.log10(error)))
    decimals = max(0, places)
    return f"{round(value, places):.{decimals}f} ± {error:.{decimals}f}"


def format_mswd(value) -> str:
    """Format finite MSWD values with fewer decimals at larger magnitudes."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(value):
        return ""
    decimals = 2 if value < 10 else 1 if value < 100 else 0
    return f"{value:.{decimals}f}"


@dataclass(frozen=True)
class AutomaticFlagSettings:
    """Thresholds for workbook-style automatic data-quality flags."""

    maximum_discordance_percent: float = 20.0
    maximum_u_mean_intercept_difference_percent: float = 25.0
    maximum_ratio_rsd_percent: float = 20.0
    maximum_primary_signal_rsd_percent: float = 50.0
    maximum_relative_2e_percent: float = 20.0
    minimum_radiogenic_206_fraction: float = 0.05
    minimum_204pb_detection_sigma: float = 3.0
    flag_session_extrapolation: bool = True


@dataclass(frozen=True)
class ReductionDecision:
    """User-visible choices used to select one result from parallel outputs.

    Parameters
    ----------
    common_pb_method
        ``"none"``, ``"204"``, ``"207"``, or ``"208"``.
    include_calibration_uncertainty
        Select the total uncertainty branch when true, or the analytical
        branch without session-calibration scatter when false.
    age_transition_ma
        For uncorrected and 204-corrected results, use 206Pb/238U below this
        age and 207Pb/206Pb at or above it.
    """

    common_pb_method: str = "none"
    include_calibration_uncertainty: bool = True
    age_transition_ma: float = 1000.0

    def __post_init__(self):
        """Normalize method aliases and validate immutable decision settings."""
        method = str(self.common_pb_method).strip().lower()
        if method not in {"none", "204", "207", "208"}:
            raise ValueError(
                "common_pb_method must be 'none', '204', '207', or '208'"
            )
        if not np.isfinite(self.age_transition_ma) or self.age_transition_ma < 0:
            raise ValueError("age_transition_ma must be a non-negative number")
        object.__setattr__(self, "common_pb_method", method)


@dataclass
class FinalizedReduction:
    """Parallel results and the concise table selected from them."""

    parallel_results: pd.DataFrame
    selected_results: pd.DataFrame
    quality_metrics: pd.DataFrame
    secondary_standard_summary: pd.DataFrame
    decision: ReductionDecision


def calculate_spot_quality_metrics(
    sweep_data,
    *,
    ratio_column: str | None = None,
    primary_signal_column: str | None = None,
    signal_data=None,
):
    """Summarize sweep behaviour used by the automatic flag rules.

    Parameters
    ----------
    sweep_data
        Raw or calibrated isotope-ratio sweeps containing ``spot_name``,
        time, and ``206Pb/238U``.
    ratio_column
        Explicit Pb/U ratio column. When omitted, the corrected column is
        preferred over the raw column.
    primary_signal_column
        Matrix-element channel used to diagnose burn-through.
    signal_data
        Optional count table supplying the primary signal and ``238U``. It is
        restricted to the time interval represented by each spot's ratio
        sweeps before metrics are calculated.

    Returns
    -------
    pandas.DataFrame
        One row per spot with slope, relative scatter, U intercept mismatch,
        mass-204 detection significance, and sweep count.

    The function works with either raw or fractionation-corrected sweep tables.
    A negative 206Pb/238U slope reproduces the workbook's reverse-fractionation
    diagnostic. Relative standard deviations preserve anomalous measurements
    for inspection rather than silently filtering them.
    """
    if not isinstance(sweep_data, pd.DataFrame):
        raise TypeError("sweep_data must be a pandas DataFrame")
    if "spot_name" not in sweep_data.columns:
        raise KeyError("sweep_data must contain 'spot_name'")

    if ratio_column is None:
        for candidate in (
            "206Pb/238U_fractionation_corrected",
            "206Pb/238U",
        ):
            if candidate in sweep_data.columns:
                ratio_column = candidate
                break
    if ratio_column is None or ratio_column not in sweep_data.columns:
        raise KeyError("Could not find a 206Pb/238U sweep column")

    signal_groups = {}
    if signal_data is not None:
        if not isinstance(signal_data, pd.DataFrame):
            raise TypeError("signal_data must be a pandas DataFrame")
        if "spot_name" not in signal_data.columns:
            raise KeyError("signal_data must contain 'spot_name'")
        signal_groups = {
            name: group
            for name, group in signal_data.groupby("spot_name", sort=False)
        }

    records = []
    for spot_name, spot in sweep_data.groupby("spot_name", sort=False):
        ratio = pd.to_numeric(spot[ratio_column], errors="coerce")
        time_column = (
            "signal_relative_time"
            if "signal_relative_time" in spot.columns
            else "Time"
        )
        time = pd.to_numeric(spot[time_column], errors="coerce")
        valid = np.isfinite(ratio) & np.isfinite(time)
        slope = np.nan
        if valid.sum() >= 2 and np.ptp(time[valid]) > 0:
            slope = float(np.polyfit(time[valid], ratio[valid], 1)[0])
        ratio_mean = float(ratio[valid].mean()) if valid.any() else np.nan
        # RSD diagnoses unstable ablation while preserving the actual sweeps;
        # it is a flagging statistic, not a hidden rejection criterion.
        ratio_rsd = (
            float(ratio[valid].std() / abs(ratio_mean) * 100)
            if valid.sum() >= 2 and ratio_mean != 0
            else np.nan
        )

        signal_spot = signal_groups.get(spot_name, spot)
        if (
            signal_spot is not spot
            and "Time" in signal_spot.columns
            and "Time" in spot.columns
        ):
            selected_times = pd.to_numeric(spot["Time"], errors="coerce")
            finite_times = selected_times[np.isfinite(selected_times)]
            if not finite_times.empty:
                signal_times = pd.to_numeric(
                    signal_spot["Time"], errors="coerce"
                )
                signal_spot = signal_spot[
                    signal_times.between(
                        finite_times.min(),
                        finite_times.max(),
                        inclusive="both",
                    )
                ]
        primary_rsd = np.nan
        if (
            primary_signal_column is not None
            and primary_signal_column in signal_spot.columns
        ):
            primary = pd.to_numeric(
                signal_spot[primary_signal_column], errors="coerce"
            )
            primary = primary[np.isfinite(primary)]
            primary_mean = primary.mean()
            if primary.size >= 2 and primary_mean != 0:
                primary_rsd = float(primary.std() / abs(primary_mean) * 100)

        u_mean_intercept_difference = np.nan
        if "238U" in signal_spot.columns:
            uranium = pd.to_numeric(signal_spot["238U"], errors="coerce")
            uranium_time_column = (
                "signal_relative_time"
                if "signal_relative_time" in signal_spot.columns
                else "Time"
            )
            uranium_time = pd.to_numeric(
                signal_spot[uranium_time_column], errors="coerce"
            )
            uranium_valid = np.isfinite(uranium) & np.isfinite(uranium_time)
            if (
                uranium_valid.sum() >= 2
                and np.ptp(uranium_time[uranium_valid]) > 0
            ):
                # Compare the observed mean to the t=0 intercept of the fitted
                # U trend, matching the legacy burn-through diagnostic.
                relative_uranium_time = (
                    uranium_time[uranium_valid]
                    - uranium_time[uranium_valid].min()
                )
                uranium_intercept = np.polyfit(
                    relative_uranium_time,
                    uranium[uranium_valid],
                    1,
                )[1]
                uranium_mean = uranium[uranium_valid].mean()
                if uranium_mean != 0:
                    u_mean_intercept_difference = float(
                        abs(uranium_mean - uranium_intercept)
                        / abs(uranium_mean)
                        * 100
                    )

        pb204_mean = (
            float(pd.to_numeric(spot["204Pb"], errors="coerce").mean())
            if "204Pb" in spot.columns
            else np.nan
        )
        pb204_detection_sigma = np.nan
        if "204Pb" in spot.columns and "204Pb_2e" in spot.columns:
            pb204_values = pd.to_numeric(spot["204Pb"], errors="coerce")
            pb204_one_sigma = (
                pd.to_numeric(spot["204Pb_2e"], errors="coerce") / 2
            )
            valid_204 = (
                np.isfinite(pb204_values)
                & np.isfinite(pb204_one_sigma)
                & (pb204_one_sigma > 0)
            )
            if valid_204.any():
                # Independent sweep variances add; division by n converts the
                # uncertainty of the sum to the uncertainty of the mean.
                combined_sigma = np.sqrt(
                    np.sum(pb204_one_sigma[valid_204] ** 2)
                ) / valid_204.sum()
                if combined_sigma > 0:
                    pb204_detection_sigma = float(
                        pb204_values[valid_204].mean() / combined_sigma
                    )
        records.append(
            {
                "spot_name": spot_name,
                "qc_206Pb/238U_slope": slope,
                "qc_206Pb/238U_rsd_percent": ratio_rsd,
                "qc_primary_signal_rsd_percent": primary_rsd,
                "qc_u_mean_intercept_difference_percent": (
                    u_mean_intercept_difference
                ),
                "qc_204Pb_mean": pb204_mean,
                "qc_204Pb_detection_sigma": pb204_detection_sigma,
                "qc_sweep_count": int(valid.sum()),
            }
        )
    return pd.DataFrame.from_records(records)


def apply_automatic_flags(
    results,
    *,
    quality_metrics=None,
    settings: AutomaticFlagSettings | None = None,
):
    """Append transparent Boolean flags and a compact flag summary.

    Parameters
    ----------
    results
        Spot or sweep result table.
    quality_metrics
        Optional output from :func:`calculate_spot_quality_metrics`.
    settings
        User-editable thresholds. Defaults reproduce the workbook-style
        warning levels.

    Returns
    -------
    pandas.DataFrame
        Copy of ``results`` with individual ``flag_*`` columns,
        ``automatic_flags``, and ``automatic_pass``.

    Every flag has its own column. ``automatic_flags`` is only a readable
    summary, and ``automatic_pass`` is true when no rule fires. This mirrors
    the Excel workflow while avoiding opaque cell colour as the sole record of
    a decision.
    """
    if not isinstance(results, pd.DataFrame):
        raise TypeError("results must be a pandas DataFrame")
    settings = settings or AutomaticFlagSettings()
    frame = results.copy()
    if quality_metrics is not None and not quality_metrics.empty:
        if "spot_name" not in frame.columns:
            raise KeyError("results must contain 'spot_name' to merge metrics")
        metric_columns = [
            column
            for column in quality_metrics.columns
            if column == "spot_name" or column not in frame.columns
        ]
        frame = frame.merge(
            quality_metrics[metric_columns],
            on="spot_name",
            how="left",
        )

    def numeric_column(name, default=np.nan):
        """Return one result column as numeric values or a default series."""
        if name not in frame.columns:
            return pd.Series(default, index=frame.index, dtype=float)
        return pd.to_numeric(frame[name], errors="coerce")

    frame["flag_reverse_fractionation"] = (
        numeric_column("qc_206Pb/238U_slope") < 0
    )
    frame["flag_u_mean_intercept_mismatch"] = (
        numeric_column("qc_u_mean_intercept_difference_percent")
        > settings.maximum_u_mean_intercept_difference_percent
    )
    frame["flag_high_ratio_scatter"] = (
        numeric_column("qc_206Pb/238U_rsd_percent")
        > settings.maximum_ratio_rsd_percent
    )
    frame["flag_burn_through"] = (
        numeric_column("qc_primary_signal_rsd_percent")
        > settings.maximum_primary_signal_rsd_percent
    )
    frame["flag_high_common_pb"] = (
        numeric_column("204Pb_correction_f206_radiogenic")
        < settings.minimum_radiogenic_206_fraction
    )
    frame["flag_detectable_204pb"] = (
        numeric_column("qc_204Pb_detection_sigma")
        >= settings.minimum_204pb_detection_sigma
    )
    frame["flag_discordant"] = (
        np.abs(numeric_column("disc%"))
        > settings.maximum_discordance_percent
    )
    relative_uncertainty = numeric_column(
        "206Pb/238U_age_2e_with_calibration%"
    )
    if relative_uncertainty.isna().all():
        relative_uncertainty = numeric_column("206Pb/238U_age_2e%")
    frame["flag_high_uncertainty"] = (
        relative_uncertainty > settings.maximum_relative_2e_percent
    )
    extrapolation_columns = [
        column
        for column in frame.columns
        if column.endswith("_session_correction_extrapolated")
    ]
    if extrapolation_columns and settings.flag_session_extrapolation:
        frame["flag_session_extrapolation"] = (
            frame[extrapolation_columns].fillna(False).astype(bool).any(axis=1)
        )
    else:
        frame["flag_session_extrapolation"] = False

    flag_labels = {
        "flag_reverse_fractionation": "reverse fractionation",
        "flag_u_mean_intercept_mismatch": "U mean/intercept mismatch",
        "flag_high_ratio_scatter": "high ratio scatter",
        "flag_burn_through": "possible burn-through",
        "flag_high_common_pb": "high common Pb",
        "flag_detectable_204pb": "detectable 204Pb",
        "flag_discordant": "discordant",
        "flag_high_uncertainty": "high uncertainty",
        "flag_session_extrapolation": "session extrapolation",
    }
    frame["automatic_flags"] = [
        "; ".join(
            label
            for column, label in flag_labels.items()
            if bool(row[column])
        )
        for _, row in frame.iterrows()
    ]
    frame["automatic_pass"] = frame["automatic_flags"].eq("")
    frame.attrs = dict(results.attrs)
    frame.attrs["automatic_flag_settings"] = asdict(settings)
    return frame


def assign_preferred_age(
    results,
    *,
    decision: ReductionDecision | None = None,
    use_selected_systematics: bool = False,
):
    """Select preferred ages without discarding any parallel calculation.

    Parameters
    ----------
    results
        Table containing uncorrected and common-Pb-corrected age branches.
    decision
        Common-Pb method, calibration-uncertainty policy, and transition age.

    Returns
    -------
    pandas.DataFrame
        Copy with preferred value, uncertainty, method, validity, and pass
        columns appended.

    The chosen value, uncertainty, method, and pass flag are appended. The
    original uncorrected, 204-, 207-, and 208-method columns remain available
    for audit and alternative exports.
    """
    if not isinstance(results, pd.DataFrame):
        raise TypeError("results must be a pandas DataFrame")
    decision = decision or ReductionDecision()
    frame = results.copy()
    uncertainty_mode = (
        "with_calibration"
        if decision.include_calibration_uncertainty
        else "no_calibration"
    )
    uncertainty_suffix = f"_2e_{uncertainty_mode}"
    if use_selected_systematics:
        uncertainty_suffix += "_selected_systematic"

    if decision.common_pb_method in {"207", "208"}:
        base = f"{decision.common_pb_method}Pb_corrected_age"
        if base not in frame.columns:
            raise KeyError(
                f"{base!r} is absent; calculate common-Pb outputs first"
            )
        frame["preferred_age"] = frame[base]
        requested = base + uncertainty_suffix
        fallback = f"{base}_2e_{uncertainty_mode}"
        frame["preferred_age_2e"] = frame[
            requested if requested in frame else fallback
        ]
        frame["preferred_age_method"] = (
            decision.common_pb_method + "Pb-corrected"
        )
        validity_column = (
            f"{decision.common_pb_method}Pb_correction_valid"
        )
        correction_valid = (
            frame[validity_column].fillna(False).astype(bool)
            if validity_column in frame.columns
            else True
        )
    else:
        prefix = (
            "204Pb_corrected_"
            if decision.common_pb_method == "204"
            else ""
        )
        young_age = prefix + "206Pb/238U_age"
        old_age = prefix + "207Pb/206Pb_age"
        young_uncertainty = young_age + uncertainty_suffix
        old_uncertainty = old_age + uncertainty_suffix
        if young_uncertainty not in frame:
            young_uncertainty = young_age + f"_2e_{uncertainty_mode}"
        if old_uncertainty not in frame:
            old_uncertainty = old_age + f"_2e_{uncertainty_mode}"
        missing = [
            column
            for column in (
                young_age,
                old_age,
                young_uncertainty,
                old_uncertainty,
            )
            if column not in frame.columns
        ]
        if missing:
            raise KeyError(
                "Preferred-age inputs are absent: " + ", ".join(missing)
            )
        use_young = (
            pd.to_numeric(frame[young_age], errors="coerce")
            < decision.age_transition_ma
        )
        frame["preferred_age"] = np.where(
            use_young, frame[young_age], frame[old_age]
        )
        frame["preferred_age_2e"] = np.where(
            use_young, frame[young_uncertainty], frame[old_uncertainty]
        )
        label_prefix = "204Pb-corrected " if prefix else ""
        frame["preferred_age_method"] = np.where(
            use_young,
            label_prefix + "206Pb/238U",
            label_prefix + "207Pb/206Pb",
        )
        correction_valid = (
            frame["204Pb_correction_valid"].fillna(False).astype(bool)
            if prefix and "204Pb_correction_valid" in frame.columns
            else True
        )

    automatic_pass = (
        frame["automatic_pass"].fillna(False).astype(bool)
        if "automatic_pass" in frame.columns
        else True
    )
    frame["preferred_age_valid"] = (
        np.isfinite(pd.to_numeric(frame["preferred_age"], errors="coerce"))
        & np.isfinite(
            pd.to_numeric(frame["preferred_age_2e"], errors="coerce")
        )
        & correction_valid
    )
    frame["preferred_age_pass"] = (
        frame["preferred_age_valid"] & automatic_pass
    )
    # Public single-analysis terminology follows Horstwood et al. (2016).
    # Legacy age names remain aliases so existing notebooks do not break.
    frame["preferred_date"] = frame["preferred_age"]
    frame["preferred_date_2e"] = frame["preferred_age_2e"]
    frame["preferred_date_method"] = frame["preferred_age_method"]
    frame["preferred_date_valid"] = frame["preferred_age_valid"]
    frame["preferred_date_pass"] = frame["preferred_age_pass"]
    frame.attrs = dict(results.attrs)
    frame.attrs["reduction_decision"] = asdict(decision)
    return frame


def weighted_mean_and_mswd(values, two_sigma_uncertainties):
    """Calculate an inverse-variance weighted mean and goodness of fit.

    Parameters
    ----------
    values
        Age or ratio estimates.
    two_sigma_uncertainties
        Absolute two-sigma uncertainties corresponding to ``values``.

    Returns
    -------
    dict
        Finite observation count, weighted mean, internal two-sigma
        uncertainty, MSWD, and upper-tail chi-square probability.

    Notes
    -----
    Weights are ``1 / sigma_i**2``. MSWD is the weighted residual sum of
    squares divided by ``n - 1``; no external-error inflation is applied.
    """
    values = np.asarray(values, dtype=float)
    two_sigma_uncertainties = np.asarray(
        two_sigma_uncertainties, dtype=float
    )
    one_sigma = two_sigma_uncertainties / 2
    valid = (
        np.isfinite(values)
        & np.isfinite(one_sigma)
        & (one_sigma > 0)
    )
    values = values[valid]
    one_sigma = one_sigma[valid]
    if values.size == 0:
        return {
            "n": 0,
            "weighted_mean": np.nan,
            "weighted_mean_2e": np.nan,
            "mswd": np.nan,
            "probability": np.nan,
        }
    weights = 1 / one_sigma**2
    mean = float(np.sum(weights * values) / np.sum(weights))
    mean_2e = float(2 / np.sqrt(np.sum(weights)))
    if values.size == 1:
        mswd = np.nan
        probability = np.nan
    else:
        # Load SciPy only when an MSWD probability is actually requested.
        # This keeps the desktop shell independent of compiled statistics
        # extensions until a reduction reaches its final summary.
        from scipy.stats import chi2

        degrees_of_freedom = values.size - 1
        chi_square = float(np.sum(weights * (values - mean) ** 2))
        mswd = chi_square / degrees_of_freedom
        probability = float(chi2.sf(chi_square, degrees_of_freedom))
    return {
        "n": int(values.size),
        "weighted_mean": mean,
        "weighted_mean_2e": mean_2e,
        "mswd": mswd,
        "probability": probability,
    }


def summarize_secondary_standards(
    results,
    *,
    accepted_values=None,
    sample_column: str = "sample",
    age_column: str = "preferred_age",
    uncertainty_column: str = "preferred_age_2e",
    use_passed_only: bool = False,
):
    """Calculate per-standard weighted means, MSWD, and accepted-age offsets.

    Parameters
    ----------
    results
        Post-decision spot results.
    accepted_values
        Mapping from sample name to accepted age or ``(age, 2σ)``.
    sample_column, age_column, uncertainty_column
        Columns defining groups and weighted observations.
    use_passed_only
        Exclude flagged rows only when explicitly enabled.

    Returns
    -------
    pandas.DataFrame
        One row per secondary material with counts, weighted mean, MSWD,
        probability, and accepted-value offsets.

    Automatic flags are reported as ``n_flagged`` but do not silently remove
    analyses by default. Set ``use_passed_only=True`` when the user has decided
    that every automatic warning is an exclusion criterion.
    """
    if not isinstance(results, pd.DataFrame):
        raise TypeError("results must be a pandas DataFrame")
    for column in (sample_column, age_column, uncertainty_column):
        if column not in results.columns:
            raise KeyError(f"results is missing {column!r}")
    accepted_values = accepted_values or {}
    records = []
    for sample, group in results.groupby(sample_column, sort=False):
        selected = group
        if use_passed_only and "preferred_age_pass" in group.columns:
            selected = group[group["preferred_age_pass"].fillna(False)]
        summary = weighted_mean_and_mswd(
            selected[age_column],
            selected[uncertainty_column],
        )
        accepted = accepted_values.get(sample)
        if isinstance(accepted, (tuple, list)):
            accepted_age = float(accepted[0])
            accepted_2e = float(accepted[1]) if len(accepted) > 1 else np.nan
        elif accepted is None:
            accepted_age = np.nan
            accepted_2e = np.nan
        else:
            accepted_age = float(accepted)
            accepted_2e = np.nan
        offset = summary["weighted_mean"] - accepted_age
        records.append(
            {
                sample_column: sample,
                "n_total": int(len(group)),
                "n_flagged": int(
                    (~group["preferred_age_pass"].fillna(False)).sum()
                )
                if "preferred_age_pass" in group.columns
                else 0,
                **summary,
                "accepted_age": accepted_age,
                "accepted_age_2e": accepted_2e,
                "offset_from_accepted_age": offset,
                "offset_percent": (
                    offset / accepted_age * 100
                    if np.isfinite(accepted_age) and accepted_age != 0
                    else np.nan
                ),
            }
        )
    return pd.DataFrame.from_records(records)


def finalize_reduction_results(
    results,
    *,
    sweep_data=None,
    decision: ReductionDecision | None = None,
    common_pb_options=None,
    flag_settings: AutomaticFlagSettings | None = None,
    primary_signal_column: str | None = None,
    quality_signal_data=None,
    secondary_standard_names=None,
    accepted_secondary_ages=None,
    uncertainty_settings=None,
):
    """Run common-Pb alternatives, QC, selection, and summaries.

    Parameters
    ----------
    results
        Spot or sweep table after fractionation and concentration calibration.
    sweep_data, quality_signal_data
        Optional data used to calculate spot-level automatic metrics.
    decision
        Preferred common-Pb and uncertainty branch.
    common_pb_options
        Keyword arguments forwarded to
        :func:`UPbRPy.isochron.apply_common_pb_corrections`.
    flag_settings
        Thresholds for automatic warnings.
    primary_signal_column
        Matrix-element signal used for burn-through diagnostics.
    secondary_standard_names, accepted_secondary_ages
        Optional secondary-material selection and accepted values.

    Returns
    -------
    FinalizedReduction
        Complete audit table, concise selected table, QC metrics, and
        secondary-standard summary.
    """
    from .isochron import apply_common_pb_corrections
    from .uncertainty import UncertaintySettings, append_systematic_date_uncertainties

    decision = decision or ReductionDecision()
    expected_columns = {
        "207Pb_corrected_age",
        "208Pb_corrected_age",
    }
    if {"206Pb/204Pb", "206Pb/208Pb"}.issubset(results.columns):
        expected_columns.add("204Pb_corrected_206Pb/238U")
    parallel = (
        results.copy()
        if expected_columns.issubset(results.columns)
        else apply_common_pb_corrections(
            results,
            **(common_pb_options or {}),
        )
    )
    settings = uncertainty_settings or UncertaintySettings()
    if not isinstance(settings, UncertaintySettings):
        settings = UncertaintySettings(**dict(settings))
    metrics = (
        calculate_spot_quality_metrics(
            sweep_data,
            primary_signal_column=primary_signal_column,
            signal_data=quality_signal_data,
        )
        if sweep_data is not None
        else pd.DataFrame()
    )
    parallel = apply_automatic_flags(
        parallel,
        quality_metrics=metrics,
        settings=flag_settings,
    )
    systematic_requested = (
        settings.propagate_decay_constants
        or settings.propagate_u238_u235
        or settings.long_term_reproducibility_mode != "none"
    )
    if settings.long_term_reproducibility_mode == "user":
        parallel = append_systematic_date_uncertainties(parallel, settings)
    elif settings.propagate_decay_constants or settings.propagate_u238_u235:
        parallel = append_systematic_date_uncertainties(
            parallel,
            settings,
            long_term_relative_1s=0.0,
        )
    selected = assign_preferred_age(
        parallel,
        decision=decision,
        use_selected_systematics=systematic_requested,
    )
    secondary_source = selected
    if secondary_standard_names is not None:
        secondary_source = selected[
            selected["sample"].isin(secondary_standard_names)
        ]
    secondary = (
        summarize_secondary_standards(
            secondary_source,
            accepted_values=accepted_secondary_ages,
        )
        if not secondary_source.empty
        else pd.DataFrame()
    )
    if settings.long_term_reproducibility_mode == "secondary":
        offsets = pd.to_numeric(
            secondary.get("offset_percent", pd.Series(dtype=float)),
            errors="coerce",
        ).abs()
        # A single session cannot establish true long-term reproducibility.
        # This explicit option uses the largest observed secondary-standard
        # weighted-mean offset as a conservative relative 1-sigma term.
        relative_1s = float(offsets.max() / 100) if offsets.notna().any() else 0.0
        parallel = append_systematic_date_uncertainties(
            parallel,
            settings,
            long_term_relative_1s=relative_1s,
        )
        selected = assign_preferred_age(
            parallel,
            decision=decision,
            use_selected_systematics=True,
        )
        selected.attrs["secondary_reproducibility_estimator"] = (
            "maximum absolute weighted-mean offset from accepted date"
        )
        selected.attrs["secondary_reproducibility_relative_1s"] = relative_1s
        secondary_source = selected
        if secondary_standard_names is not None:
            secondary_source = selected[
                selected["sample"].isin(secondary_standard_names)
            ]
        secondary = (
            summarize_secondary_standards(
                secondary_source,
                accepted_values=accepted_secondary_ages,
            )
            if not secondary_source.empty
            else pd.DataFrame()
        )
    concise_columns = [
        column
        for column in (
            "sample",
            "spot_name",
            "preferred_date",
            "preferred_date_2e",
            "preferred_date_method",
            "preferred_date_pass",
            "automatic_flags",
            "206Pb/238U",
            "206Pb/238U_2e_no_calibration",
            "206Pb/238U_2e_with_calibration",
            "207Pb/206Pb",
            "207Pb/206Pb_2e_no_calibration",
            "207Pb/206Pb_2e_with_calibration",
            "err_corr_no_calibration",
            "err_corr_with_calibration",
            "tw_rho_no_calibration",
            "tw_rho_with_calibration",
            "208Pb/232Th",
            "208Pb/232Th_2e_no_calibration",
            "208Pb/232Th_2e_with_calibration",
        )
        if column in selected.columns
    ]
    concise = selected[concise_columns].copy()
    concise.attrs = dict(selected.attrs)
    return FinalizedReduction(
        parallel_results=selected,
        selected_results=concise,
        quality_metrics=metrics,
        secondary_standard_summary=secondary,
        decision=decision,
    )


def publication_depth_profile_table(depth_profile, *, include_concentrations=False):
    """Return publication-relevant sweep columns in a readable order.

    Raw detector channels and transient intermediate columns remain available
    in the audit CSV.  The workbook keeps identifiers, signal/depth position,
    corrected ratios, both uncertainty branches, ages, correlations, and
    common-Pb/intercept decisions.
    """
    if depth_profile is None:
        return pd.DataFrame()
    if not isinstance(depth_profile, pd.DataFrame):
        raise TypeError("depth_profile must be a pandas DataFrame")
    leading = [
        column
        for column in (
            "sample",
            "spot_name",
            "Time",
            "signal_relative_time",
            "depth_um",
            "sweep_index",
        )
        if column in depth_profile.columns
    ]
    scientific = [
        column
        for column in depth_profile.columns
        if column not in leading
        and (
            "fractionation_corrected" in str(column)
            or "age" in str(column).casefold()
            or "common_pb" in str(column).casefold()
            or "intercept" in str(column).casefold()
            or "preferred" in str(column).casefold()
            or "automatic_flag" in str(column).casefold()
            or str(column).startswith("err_corr")
            or str(column).startswith("tw_rho")
        )
    ]
    if include_concentrations:
        concentration_pattern = re.compile(
            r"^[A-Z][a-z]?(?:_(?:1s|1s%|2s|2s%|2e|2e%|95ci|95ci%))?$",
            flags=re.IGNORECASE,
        )
        scientific.extend(
            column
            for column in depth_profile.columns
            if column not in leading
            and column not in scientific
            and concentration_pattern.match(str(column))
        )
    result = depth_profile.loc[:, leading + scientific].copy()
    result = result.rename(
        columns={
            column: str(column).replace("_age", "_date")
            for column in result.columns
            if "_age" in str(column)
        }
    )
    return result


def publication_spot_concentration_table(finalized):
    """Return spot dates beside all available elemental concentrations.

    Element names are isotope agnostic (for example ``U`` rather than
    ``238U``). Their ``1s``, ``2s``, and ``95CI`` companion columns are kept
    when present. Raw detector-isotope channels are excluded.
    """
    if not isinstance(finalized, FinalizedReduction):
        raise TypeError("finalized must be a FinalizedReduction")
    source = finalized.parallel_results
    concentration_pattern = re.compile(
        r"^[A-Z][a-z]?(?:_(?:1s|1s%|2s|2s%|2e|2e%|95ci|95ci%))?$",
        flags=re.IGNORECASE,
    )
    leading = [
        column
        for column in (
            "sample",
            "spot_name",
            "preferred_date",
            "preferred_date_2e",
            "preferred_date_method",
            "preferred_date_pass",
        )
        if column in source
    ]
    date_columns = [
        column
        for column in source
        if column not in leading
        and (
            "_date" in str(column).casefold()
            or (
                "_age" in str(column).casefold()
                and "preferred_age" not in str(column).casefold()
            )
        )
    ]
    concentration_columns = [
        column
        for column in source
        if column not in leading
        and column not in date_columns
        and concentration_pattern.match(str(column))
    ]
    result = source.loc[
        :,
        leading + date_columns + concentration_columns,
    ].copy()
    return result.rename(
        columns={
            column: str(column).replace("_age", "_date")
            for column in result.columns
            if "_age" in str(column)
        }
    )


def build_horstwood_table4(finalized, *, include_concentrations=True):
    """Return a Horstwood et al. (2016) Table 4-style single-analysis table.

    The export uses ``date`` for single-analysis daughter/parent results and
    reports analytical and systematic-inclusive 2-sigma columns separately.
    Internal legacy ``*_age`` columns remain untouched for notebook
    compatibility. Missing optional measurements are simply omitted.
    """
    if not isinstance(finalized, FinalizedReduction):
        raise TypeError("finalized must be a FinalizedReduction")
    source = finalized.parallel_results
    output = pd.DataFrame(index=source.index)

    def copy_first(label, candidates):
        for candidate in candidates:
            if candidate in source.columns:
                output[label] = source[candidate]
                return

    copy_first("Analysis", ("spot_name",))
    copy_first("Sample", ("sample",))
    copy_first("Comments / flags", ("automatic_flags",))
    copy_first(
        "Beam diameter (µm)",
        ("beam_diameter_um", "spot_diameter_um", "laser_spot_size_um"),
    )
    method = finalized.decision.common_pb_method
    fraction_candidates = (
        (f"{method}Pb_common_fraction_206",)
        if method in {"204", "207", "208"}
        else ()
    ) + (
        "204Pb_common_fraction_206",
        "207Pb_common_fraction_206",
        "208Pb_common_fraction_206",
    )
    copy_first("f206c", fraction_candidates)
    if include_concentrations:
        copy_first("U (µg g-1)", ("U",))
        copy_first("U 2SD (µg g-1)", ("U_2s", "U_2e"))
        copy_first("U 95% CI (µg g-1)", ("U_95CI",))
        copy_first("Th/U", ("Th/U",))

    def copy_ratio(label, base, *, inverse_base=None):
        """Copy a reported ratio and its two uncertainty branches."""
        direct_candidates = (f"{base}_fractionation_corrected", base)
        direct = next(
            (candidate for candidate in direct_candidates if candidate in source),
            None,
        )
        if direct is not None:
            output[label] = source[direct]
        elif inverse_base is not None:
            inverse = next(
                (
                    candidate
                    for candidate in (
                        f"{inverse_base}_fractionation_corrected",
                        inverse_base,
                    )
                    if candidate in source
                ),
                None,
            )
            if inverse is not None:
                with np.errstate(divide="ignore", invalid="ignore"):
                    output[label] = 1.0 / pd.to_numeric(
                        source[inverse], errors="coerce"
                    )
                for branch, suffix in (
                    ("no_calibration", " 2σ analytical"),
                    ("with_calibration", " 2σ incl. calibration"),
                ):
                    uncertainty = next(
                        (
                            candidate
                            for candidate in (
                                inverse + f"_2e_{branch}",
                                inverse_base + f"_2e_{branch}",
                                inverse + "_2e",
                                inverse_base + "_2e",
                            )
                            if candidate in source
                        ),
                        None,
                    )
                    if uncertainty is not None:
                        with np.errstate(divide="ignore", invalid="ignore"):
                            output[label + suffix] = pd.to_numeric(
                                source[uncertainty], errors="coerce"
                            ) / pd.to_numeric(source[inverse], errors="coerce") ** 2
                return
        if direct is None:
            return
        copy_first(
            label + " 2σ analytical",
            (
                direct + "_2e_no_calibration",
                f"{base}_2e_no_calibration",
                direct + "_2e",
                f"{base}_2e",
            ),
        )
        copy_first(
            label + " 2σ incl. calibration",
            (
                direct + "_2e_with_calibration",
                f"{base}_2e_with_calibration",
                direct + "_2e",
                f"{base}_2e",
            ),
        )

    for label, base, inverse in (
        ("206Pb/204Pb", "206Pb/204Pb", None),
        ("238U/206Pb", "238U/206Pb", "206Pb/238U"),
        ("207Pb/206Pb", "207Pb/206Pb", None),
        ("208Pb/206Pb", "208Pb/206Pb", "206Pb/208Pb"),
        ("207Pb/235U", "207Pb/235U", None),
        ("206Pb/238U", "206Pb/238U", None),
        ("208Pb/232Th", "208Pb/232Th", None),
    ):
        copy_ratio(label, base, inverse_base=inverse)
    copy_first("rho Wetherill", ("err_corr_with_calibration", "err_corr"))
    copy_first("rho Tera-Wasserburg", ("tw_rho_with_calibration", "tw_rho"))

    for system in ("206Pb/238U", "207Pb/235U", "207Pb/206Pb", "208Pb/232Th"):
        age = f"{system}_age"
        copy_first(f"{system} date (Ma)", (age,))
        copy_first(
            f"{system} date 2σ analytical (Ma)",
            (age + "_2e_no_calibration", age + "_2e"),
        )
        copy_first(
            f"{system} date 2σ incl. calibration (Ma)",
            (age + "_2e_with_calibration", age + "_2e"),
        )
        copy_first(
            f"{system} date 2σ selected total (Ma)",
            (
                age + "_2e_selected_systematic",
                age + "_2e_with_calibration",
                age + "_2e",
            ),
        )
    copy_first("Preferred date (Ma)", ("preferred_date", "preferred_age"))
    copy_first(
        "Preferred date 2σ (Ma)",
        ("preferred_date_2e", "preferred_age_2e"),
    )
    copy_first(
        "Preferred date method",
        ("preferred_date_method", "preferred_age_method"),
    )
    return output.reset_index(drop=True)


def publication_table_notes(tables):
    """Build geology-facing footnotes for each exported result table."""
    shared = [
        (
            "Ratios are dimensionless. Single-analysis daughter/parent results "
            "are dates in Ma; the term age is reserved for an interpreted "
            "population or geological event."
        ),
        (
            "Uncertainty columns are absolute 2σ values: spot aggregates use "
            "two standard errors of the mean, whereas individual sweeps use "
            "twice their propagated one-sigma measurement uncertainty. A two-sigma "
            "concordia ellipse uses semiaxes of 2 standard errors "
            "and contains about 86.5% joint probability for a bivariate normal "
            "distribution; it is not a 95% joint-confidence ellipse."
        ),
        (
            "Analytical 2σ includes counting statistics, propagated baseline-mean "
            "uncertainty, within-analysis dispersion, and applicable common-Pb "
            "composition uncertainty. The calibration-inclusive branch additionally "
            "includes session-calibration residual scatter."
        ),
        (
            "Selected-total date uncertainty begins with the calibration-inclusive "
            "branch and adds only the user-selected systematic terms: decay constants, "
            "238U/235U, and/or external long-term reproducibility. Each choice and "
            "numerical value is recorded in the Methods worksheet."
        ),
        (
            "Long-term reproducibility is entered as relative 1SD in calibrated-ratio "
            "space and converted to date uncertainty with the local ratio--date "
            "derivative. A secondary-standard estimate is a session validation "
            "diagnostic; a defensible long-term value normally requires repeated "
            "measurements across many sessions."
        ),
        (
            "Downhole-fit uncertainty is treated as a systematic alignment/correction "
            "model and is not propagated sweep by sweep. Session residual scatter is "
            "propagated; fitted-coefficient covariance and cross-ratio session "
            "covariance are not propagated."
        ),
        (
            "err_corr is the propagated correlation coefficient for the "
            "207Pb/235U--206Pb/238U Wetherill coordinates; tw_rho is the "
            "correlation for the 238U/206Pb--207Pb/206Pb Tera--Wasserburg "
            "coordinates."
        ),
        (
            "Reported spot 206Pb/204Pb is the ratio of the accepted plateau "
            "mean 206Pb signal to the mean Hg-corrected mass-204 signal; "
            "depth-profile 206Pb/204Pb values remain sweep resolved."
        ),
        (
            "Common-Pb fraction names have the form METHODPb_common_fraction_206 "
            "and report the fraction of measured 206Pb inferred to be common Pb. "
            "The f206c column uses the method selected in the Methods worksheet."
        ),
    ]
    notes = {
        "Results": shared
        + [
            (
                "preferred_date and preferred_date_method record the selected "
                "common-Pb and age-transition decision; parallel alternatives "
                "are retained in Parallel audit."
            ),
            (
                "automatic_flags are screening diagnostics, not silently applied "
                "deletions; preferred_date_pass records the resulting decision."
            ),
        ],
        "Secondary standards": [
            (
                "Weighted means use inverse-variance weights from accepted spot "
                "uncertainties; MSWD describes dispersion relative to those errors."
            )
        ],
        "Horstwood Table 4": shared
        + [
            (
                "Reporting structure and uncertainty terminology follow Horstwood "
                "et al. (2016), https://doi.org/10.1111/j.1751-908X.2016.00379.x."
            ),
            (
                "Single-analysis daughter/parent results are termed dates; "
                "interpretive weighted means or geological populations may be ages."
            ),
            (
                "The analytical, calibration-inclusive, and selected-total 2σ date "
                "columns are kept separate following Horstwood et al. (2016). Blank "
                "cells mean that the isotope system or optional field was not measured "
                "or could not be calculated; they are not zeros."
            ),
            (
                "The default 238U/235U ratio is 137.818 ± 0.045 (2SD; Hiess "
                "et al., 2012, https://doi.org/10.1126/science.1215507). Decay "
                "constants are lambda-238 = 1.55125e-10 yr-1 ± 0.11% (2SD) "
                "and lambda-235 = 9.8485e-10 yr-1 ± 0.14% (2SD; Jaffey et "
                "al., 1971, https://doi.org/10.1103/PhysRevC.4.1889), and "
                "lambda-232 = 4.9475e-11 yr-1 ± 0.042e-11 yr-1 (2SD; Le Roux "
                "and Glendenin, 1963). These systematic "
                "uncertainties enter a reported date only when their corresponding "
                "Methods-sheet option is enabled."
            ),
            (
                "Initial common-Pb compositions may be fixed from a cogenetic phase or "
                "calculated with the Stacey--Kramers model. Model uncertainty is not "
                "defined by Stacey and Kramers (1975) itself: the selected uncertainty "
                "preset, relative 1SD values, and correlations are recorded explicitly "
                "in Methods. Stacey and Kramers (1975): "
                "https://doi.org/10.1016/0012-821X(75)90088-6."
            ),
            (
                "U and Th/U are elemental concentrations or concentration ratios. "
                "They are included only when concentration export is enabled; units "
                "are micrograms per gram for U and dimensionless for Th/U."
            ),
        ],
        "Depth profiles": shared
        + [
            (
                "signal_relative_time is zero at the reviewed beginning of the "
                "sample signal; depth_um is present only when a drilling-rate "
                "calibration has been supplied."
            ),
            (
                "Depth rows retain the reviewed full signal interval. Plateau "
                "outlier rejection affects the spot mean but does not silently "
                "remove full-signal sweeps from depth-profile reduction."
            ),
            (
                "Downhole correction is indexed by signal-relative time and is "
                "derived from pooled primary-standard full-signal profiles; "
                "session correction is derived from primary-standard plateau means."
            ),
        ],
        "Spot concentrations": shared
        + [
            (
                "Element columns are isotope-agnostic calibrated concentrations "
                "in micrograms per gram. Companion 1SD, 2SD, and 95% confidence "
                "interval columns are retained when calculated; Th/U is dimensionless."
            ),
            (
                "This optional sheet places the full concentration inventory beside "
                "the corresponding single-analysis dates. Detector isotope-count "
                "columns are retained only in the Parallel audit sheet."
            ),
        ],
        "QC metrics": [
            (
                "Quality metrics preserve the measured data and support automatic "
                "flags; they are not additional hidden filtering rules."
            )
        ],
        "Methods": [
            (
                "This sheet records the user-visible reduction decision, automatic "
                "flag thresholds, common-Pb settings, and reference-library provenance."
            )
        ],
        "Parallel audit": shared
        + [
            (
                "This audit table retains uncorrected and 204Pb-, 207-, and "
                "208-method alternatives where available, before the preferred "
                "result decision is applied."
            )
        ],
    }
    rows = []
    for sheet_name in tables:
        for number, note in enumerate(notes.get(sheet_name, shared), start=1):
            rows.append(
                {"worksheet": sheet_name, "footnote": number, "note": note}
            )
    return pd.DataFrame(rows)


def build_publication_tables(
    finalized,
    *,
    metadata=None,
    depth_profile=None,
    include_concentrations=True,
    include_depth_concentrations=True,
):
    """Build the logical tables written to a publication workbook.

    Parameters
    ----------
    finalized
        Output from :func:`finalize_reduction_results`.
    metadata
        Additional session or manuscript metadata recorded in ``Methods``.
    depth_profile
        Optional full-signal result table. Publication-relevant corrected
        ratios, uncertainties, ages, and common-Pb outputs are added as a
        ``Depth profiles`` worksheet.

    Returns
    -------
    dict[str, pandas.DataFrame]
        Concise results, secondary standards, QC metrics, reproducibility
        settings, and the complete parallel audit table.
    """
    if not isinstance(finalized, FinalizedReduction):
        raise TypeError("finalized must be a FinalizedReduction")
    decision_rows = [
        {"setting": key, "value": value}
        for key, value in asdict(finalized.decision).items()
    ]
    for key, value in (metadata or {}).items():
        if key == "detector_calibration":
            continue
        decision_rows.append({"setting": key, "value": value})
    detector_calibration = (metadata or {}).get(
        "detector_calibration",
        finalized.parallel_results.attrs.get("detector_calibration", {"mode": "qtegra"}),
    )
    decision_rows.append({
        "setting": "detector_calibration.mode",
        "value": detector_calibration.get("mode", "qtegra"),
    })
    for channel, calibration in detector_calibration.get("channels", {}).items():
        for key in ("factor", "factor_se", "qtegra_factor"):
            decision_rows.append({
                "setting": f"detector_calibration.{channel}.{key}",
                "value": calibration.get(key),
            })
    flag_settings = finalized.parallel_results.attrs.get(
        "automatic_flag_settings", {}
    )
    for key, value in flag_settings.items():
        decision_rows.append({"setting": f"flag.{key}", "value": value})
    for section in (
        "common_pb_correction",
        "mass_204_common_pb_correction",
        "uncertainty_settings",
    ):
        settings = finalized.parallel_results.attrs.get(section, {})
        for key, value in settings.items():
            decision_rows.append(
                {
                    "setting": f"{section}.{key}",
                    "value": str(value)
                    if isinstance(value, (tuple, list, dict))
                    else value,
                }
            )
    decision_rows.extend(
        [
            {
                "setting": "constant.238U/235U",
                "value": common_U238_U235,
            },
            {
                "setting": "constant.238U/235U_1SD",
                "value": sigma_common_U238_U235,
            },
            {
                "setting": "constant.238U/235U_reference",
                "value": "Hiess et al. (2012); https://doi.org/10.1126/science.1215507",
            },
            {"setting": "constant.lambda_238_yr-1", "value": lambda_238},
            {"setting": "constant.lambda_238_1SD_yr-1", "value": sigma_238},
            {"setting": "constant.lambda_235_yr-1", "value": lambda_235},
            {"setting": "constant.lambda_235_1SD_yr-1", "value": sigma_235},
            {
                "setting": "constant.uranium_decay_reference",
                "value": "Jaffey et al. (1971); https://doi.org/10.1103/PhysRevC.4.1889",
            },
            {"setting": "constant.lambda_232_yr-1", "value": lambda_232},
            {"setting": "constant.lambda_232_1SD_yr-1", "value": sigma_232},
            {
                "setting": "constant.thorium_decay_reference",
                "value": "Le Roux and Glendenin (1963)",
            },
            {
                "setting": "uncertainty.no_calibration",
                "value": (
                    "counting/within-spot precision; no session residual "
                    "scatter"
                ),
            },
            {
                "setting": "uncertainty.with_calibration",
                "value": (
                    "analytical uncertainty plus session-calibration "
                    "residual scatter"
                ),
            },
            {
                "setting": "covariance_policy",
                "value": (
                    "no downhole/session coefficient or cross-ratio "
                    "covariance propagated"
                ),
            },
        ]
    )
    tables = {
        "Results": finalized.selected_results.drop(
            columns=[
                column
                for column in finalized.selected_results.columns
                if str(column).startswith("preferred_age")
            ],
            errors="ignore",
        ).copy(),
        "Horstwood Table 4": build_horstwood_table4(
            finalized,
            include_concentrations=include_concentrations,
        ),
        "Secondary standards": finalized.secondary_standard_summary.copy(),
        "QC metrics": finalized.quality_metrics.copy(),
        "Methods": pd.DataFrame(decision_rows),
        "Parallel audit": finalized.parallel_results.copy(),
    }
    if include_concentrations:
        spot_concentrations = publication_spot_concentration_table(finalized)
        if not spot_concentrations.empty:
            tables["Spot concentrations"] = spot_concentrations
    depth_table = publication_depth_profile_table(
        depth_profile,
        include_concentrations=include_depth_concentrations,
    )
    if not depth_table.empty:
        tables["Depth profiles"] = depth_table
    tables["Table notes"] = publication_table_notes(tables)
    return tables


def publication_column_width(values, heading, minimum=10, maximum=34):
    """Return a bounded display width for one worksheet column.

    Width is based on the longest heading or non-null cell, with a small
    padding and user-configurable lower and upper limits.
    """
    text_lengths = [len(str(heading))]
    text_lengths.extend(
        len(str(value))
        for value in values
        if value is not None and not pd.isna(value)
    )
    return min(max(max(text_lengths, default=minimum) + 2, minimum), maximum)


def publication_number_format(column):
    """Return an isotope-aware Excel number format for a publication column."""
    label = str(column)
    folded = label.casefold()
    if "date" in folded or "age" in folded:
        return "0.0"
    if folded == "footnote":
        return "0"
    if label in {"f206c", "Th/U"} or "rho" in folded:
        return "0.000"
    if "diameter" in folded:
        return "0"
    if "µg g-1" in label or "concentration" in folded:
        return "0.0"
    if any(token in label for token in ("Pb/", "U/", "/Pb", "/U", "/Th")):
        return "0.000000"
    return "0.0000"


def export_publication_tables(tables, output_path):
    """Write styled Excel and companion CSV publication tables.

    Parameters
    ----------
    tables
        Mapping of worksheet names to DataFrames, normally returned by
        :func:`build_publication_tables`.
    output_path
        Target XLSX path. A missing ``.xlsx`` suffix is added automatically.

    Returns
    -------
    pathlib.Path
        Path to the written workbook.

    The workbook uses frozen/filterable headers, semantic number formats, a
    compact methods sheet, and conditional highlighting for failed automatic
    decisions. Each table is also written as a plain CSV beside the workbook,
    ensuring a transparent non-proprietary export.
    """
    output_path = Path(output_path)
    if output_path.suffix.lower() != ".xlsx":
        output_path = output_path.with_suffix(".xlsx")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import xlsxwriter  # noqa: F401
    except ImportError as error:
        raise ImportError(
            "Formatted publication export requires 'xlsxwriter', which is a "
            "core UPbRPy dependency. Reinstall or update UPbRPy in the active "
            "notebook environment."
        ) from error

    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        workbook = writer.book
        header_format = workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#1F4E78",
                "bottom": 1,
                "bottom_color": "#A6A6A6",
                "align": "center",
                "valign": "vcenter",
            }
        )
        group_formats = {
            "Identification & screening": workbook.add_format(
                {
                    "bold": True,
                    "font_color": "#FFFFFF",
                    "bg_color": "#44546A",
                    "align": "center",
                    "valign": "vcenter",
                    "border": 1,
                    "border_color": "#FFFFFF",
                }
            ),
            "Composition": workbook.add_format(
                {
                    "bold": True,
                    "font_color": "#FFFFFF",
                    "bg_color": "#548235",
                    "align": "center",
                    "valign": "vcenter",
                    "border": 1,
                    "border_color": "#FFFFFF",
                }
            ),
            "Isotope ratios (2σ)": workbook.add_format(
                {
                    "bold": True,
                    "font_color": "#FFFFFF",
                    "bg_color": "#2F75B5",
                    "align": "center",
                    "valign": "vcenter",
                    "border": 1,
                    "border_color": "#FFFFFF",
                }
            ),
            "Single-analysis dates (Ma; 2σ)": workbook.add_format(
                {
                    "bold": True,
                    "font_color": "#FFFFFF",
                    "bg_color": "#8064A2",
                    "align": "center",
                    "valign": "vcenter",
                    "border": 1,
                    "border_color": "#FFFFFF",
                }
            ),
            "Preferred result": workbook.add_format(
                {
                    "bold": True,
                    "font_color": "#FFFFFF",
                    "bg_color": "#C65911",
                    "align": "center",
                    "valign": "vcenter",
                    "border": 1,
                    "border_color": "#FFFFFF",
                }
            ),
        }
        numeric_formats = {}
        failed_format = workbook.add_format(
            {"bg_color": "#F4CCCC", "font_color": "#9C0006"}
        )
        footnote_heading_format = workbook.add_format(
            {
                "bold": True,
                "font_color": "#1F4E78",
                "top": 1,
                "top_color": "#A6A6A6",
            }
        )
        footnote_format = workbook.add_format(
            {
                "font_color": "#4F4F4F",
                "italic": True,
                "text_wrap": True,
                "valign": "top",
            }
        )
        wrapped_text_format = workbook.add_format(
            {"text_wrap": True, "valign": "top"}
        )
        notes_frame = tables.get("Table notes", pd.DataFrame())
        for sheet_name, table in tables.items():
            safe_name = str(sheet_name)[:31]
            is_table4 = sheet_name == "Horstwood Table 4"
            header_row = 1 if is_table4 else 0
            table.to_excel(
                writer,
                sheet_name=safe_name,
                index=False,
                startrow=header_row,
            )
            worksheet = writer.sheets[safe_name]
            worksheet.hide_gridlines(2)
            worksheet.set_landscape()
            worksheet.fit_to_pages(1, 0)
            worksheet.set_margins(left=0.25, right=0.25, top=0.55, bottom=0.55)
            worksheet.repeat_rows(0, header_row)
            worksheet.set_footer(
                "&LUPbRPy publication export&C&P of &N&R&D"
            )
            worksheet.freeze_panes(
                header_row + 1,
                2 if table.shape[1] > 6 else 1,
            )
            worksheet.set_row(header_row, 34 if is_table4 else 24, header_format)
            if table.shape[1]:
                worksheet.autofilter(
                    header_row,
                    0,
                    header_row + len(table),
                    len(table.columns) - 1,
                )
            if is_table4 and table.shape[1]:
                groups = []
                for index, column in enumerate(table.columns):
                    text = str(column)
                    if text in {
                        "Analysis",
                        "Sample",
                        "Comments / flags",
                        "Beam diameter (µm)",
                    }:
                        group = "Identification & screening"
                    elif text in {
                        "f206c",
                        "U (µg g-1)",
                        "U 2SD (µg g-1)",
                        "U 95% CI (µg g-1)",
                        "Th/U",
                    }:
                        group = "Composition"
                    elif "date" in text.casefold():
                        group = (
                            "Preferred result"
                            if text.startswith("Preferred")
                            else "Single-analysis dates (Ma; 2σ)"
                        )
                    else:
                        group = "Isotope ratios (2σ)"
                    groups.append(group)
                start = 0
                while start < len(groups):
                    end = start
                    while end + 1 < len(groups) and groups[end + 1] == groups[start]:
                        end += 1
                    group = groups[start]
                    if end > start:
                        worksheet.merge_range(
                            0,
                            start,
                            0,
                            end,
                            group,
                            group_formats[group],
                        )
                    else:
                        worksheet.write(0, start, group, group_formats[group])
                    start = end + 1
                worksheet.set_row(0, 23)
            for index, column in enumerate(table.columns):
                width = publication_column_width(table[column], column)
                column_format = None
                if pd.api.types.is_numeric_dtype(table[column]):
                    format_code = publication_number_format(column)
                    if format_code not in numeric_formats:
                        numeric_formats[format_code] = workbook.add_format(
                            {"num_format": format_code}
                        )
                    column_format = numeric_formats[format_code]
                worksheet.set_column(index, index, width, column_format)
                column_text = str(column)
                comment = None
                if (
                    column_text.endswith("_2e_no_calibration")
                    or "2σ analytical" in column_text
                ):
                    comment = (
                        "Absolute 2SE analytical uncertainty. Includes counting, "
                        "baseline, within-analysis, and applicable common-Pb "
                        "composition uncertainty; excludes session residual scatter."
                    )
                elif (
                    column_text.endswith("_2e_with_calibration")
                    or "2σ incl. calibration" in column_text
                ):
                    comment = (
                        "Absolute 2SE uncertainty including session calibration "
                        "residual scatter."
                    )
                elif "2σ selected total" in column_text:
                    comment = (
                        "Absolute 2SE selected total uncertainty: calibration-inclusive "
                        "uncertainty plus the systematic components enabled in Methods."
                    )
                elif column_text.endswith("_2e"):
                    comment = "Absolute two-standard-error (2SE) uncertainty."
                elif column_text.startswith("err_corr"):
                    comment = (
                        "Error correlation for 207Pb/235U and 206Pb/238U in "
                        "Wetherill space."
                    )
                elif column_text.startswith("tw_rho"):
                    comment = (
                        "Error correlation for 238U/206Pb and 207Pb/206Pb in "
                        "Tera-Wasserburg space."
                    )
                elif column_text == "signal_relative_time":
                    comment = (
                        "Elapsed seconds from the reviewed beginning of sample "
                        "signal (time zero)."
                    )
                elif column_text == "f206c":
                    comment = (
                        "Fraction of measured 206Pb inferred to be common Pb using "
                        "the selected correction method."
                    )
                if comment:
                    worksheet.write_comment(header_row, index, comment)
            if sheet_name == "Table notes" and "note" in table.columns:
                note_index = table.columns.get_loc("note")
                worksheet.set_column(note_index, note_index, 105, wrapped_text_format)
                for row_index, note in enumerate(table["note"], start=header_row + 1):
                    line_count = max(2, int(np.ceil(len(str(note)) / 115)))
                    worksheet.set_row(row_index, min(18 * line_count, 90))
            elif sheet_name == "Methods" and "value" in table.columns:
                setting_index = table.columns.get_loc("setting")
                value_index = table.columns.get_loc("value")
                worksheet.set_column(setting_index, setting_index, 44)
                worksheet.set_column(value_index, value_index, 90, wrapped_text_format)
            if (
                "preferred_age_pass" in table.columns
                and len(table) > 0
            ):
                column_index = table.columns.get_loc("preferred_age_pass")
                worksheet.conditional_format(
                    header_row + 1,
                    column_index,
                    header_row + len(table),
                    column_index,
                    {
                        "type": "cell",
                        "criteria": "==",
                        "value": False,
                        "format": failed_format,
                    },
                )

            if sheet_name != "Table notes" and not notes_frame.empty:
                sheet_notes = notes_frame[
                    notes_frame["worksheet"].eq(sheet_name)
                ]
                if not sheet_notes.empty:
                    first_note_row = header_row + len(table) + 3
                    last_column = min(max(len(table.columns) - 1, 0), 9)
                    if last_column:
                        worksheet.merge_range(
                            first_note_row,
                            0,
                            first_note_row,
                            last_column,
                            "Table notes",
                            footnote_heading_format,
                        )
                    else:
                        worksheet.write(
                            first_note_row,
                            0,
                            "Table notes",
                            footnote_heading_format,
                        )
                    for offset, note in enumerate(
                        sheet_notes["note"].astype(str), start=1
                    ):
                        text = f"{offset}. {note}"
                        row = first_note_row + offset
                        if last_column:
                            worksheet.merge_range(
                                row,
                                0,
                                row,
                                last_column,
                                text,
                                footnote_format,
                            )
                        else:
                            worksheet.write(row, 0, text, footnote_format)
                        worksheet.set_row(row, 44)

    csv_directory = output_path.with_suffix("")
    csv_directory.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        filename = (
            str(name).strip().casefold().replace(" ", "_").replace("/", "_")
        )
        table.to_csv(csv_directory / f"{filename}.csv", index=False)
    return output_path


__all__ = [
    "AutomaticFlagSettings",
    "ReductionDecision",
    "FinalizedReduction",
    "calculate_spot_quality_metrics",
    "apply_automatic_flags",
    "assign_preferred_age",
    "weighted_mean_and_mswd",
    "summarize_secondary_standards",
    "finalize_reduction_results",
    "build_publication_tables",
    "publication_depth_profile_table",
    "publication_spot_concentration_table",
    "build_horstwood_table4",
    "publication_table_notes",
    "publication_column_width",
    "publication_number_format",
    "export_publication_tables",
]
