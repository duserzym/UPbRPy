"""Post-reduction decisions, quality flags, summaries, and table exports.

The numerical reduction deliberately retains parallel alternatives. This
module is the narrow decision layer that converts those alternatives into a
succinct, reproducible result table without deleting the underlying columns.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import chi2

from .isochron import apply_common_pb_corrections


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

    if decision.common_pb_method in {"207", "208"}:
        base = f"{decision.common_pb_method}Pb_corrected_age"
        if base not in frame.columns:
            raise KeyError(
                f"{base!r} is absent; calculate common-Pb outputs first"
            )
        frame["preferred_age"] = frame[base]
        frame["preferred_age_2e"] = frame[
            f"{base}_2e_{uncertainty_mode}"
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
        young_uncertainty = young_age + f"_2e_{uncertainty_mode}"
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
    selected = assign_preferred_age(parallel, decision=decision)
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
            "preferred_age",
            "preferred_age_2e",
            "preferred_age_method",
            "preferred_age_pass",
            "automatic_flags",
            "206Pb/238U",
            "206Pb/238U_2e_no_calibration",
            "206Pb/238U_2e_with_calibration",
            "207Pb/206Pb",
            "207Pb/206Pb_2e_no_calibration",
            "207Pb/206Pb_2e_with_calibration",
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


def build_publication_tables(finalized, *, metadata=None):
    """Build the logical tables written to a publication workbook.

    Parameters
    ----------
    finalized
        Output from :func:`finalize_reduction_results`.
    metadata
        Additional session or manuscript metadata recorded in ``Methods``.

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
        decision_rows.append({"setting": key, "value": value})
    flag_settings = finalized.parallel_results.attrs.get(
        "automatic_flag_settings", {}
    )
    for key, value in flag_settings.items():
        decision_rows.append({"setting": f"flag.{key}", "value": value})
    for section in (
        "common_pb_correction",
        "mass_204_common_pb_correction",
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
    return {
        "Results": finalized.selected_results.copy(),
        "Secondary standards": finalized.secondary_standard_summary.copy(),
        "QC metrics": finalized.quality_metrics.copy(),
        "Methods": pd.DataFrame(decision_rows),
        "Parallel audit": finalized.parallel_results.copy(),
    }


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
            "Formatted publication export requires the optional "
            "'xlsxwriter' package."
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
        numeric_format = workbook.add_format({"num_format": "0.0000"})
        age_format = workbook.add_format({"num_format": "0.0"})
        failed_format = workbook.add_format(
            {"bg_color": "#F4CCCC", "font_color": "#9C0006"}
        )
        for sheet_name, table in tables.items():
            safe_name = str(sheet_name)[:31]
            table.to_excel(writer, sheet_name=safe_name, index=False)
            worksheet = writer.sheets[safe_name]
            worksheet.hide_gridlines(2)
            worksheet.freeze_panes(1, 2 if table.shape[1] > 6 else 1)
            worksheet.set_row(0, 24, header_format)
            if table.shape[1]:
                worksheet.autofilter(0, 0, len(table), len(table.columns) - 1)
            for index, column in enumerate(table.columns):
                width = publication_column_width(table[column], column)
                column_format = (
                    age_format
                    if "age" in str(column).casefold()
                    else numeric_format
                    if pd.api.types.is_numeric_dtype(table[column])
                    else None
                )
                worksheet.set_column(index, index, width, column_format)
            if (
                "preferred_age_pass" in table.columns
                and len(table) > 0
            ):
                column_index = table.columns.get_loc("preferred_age_pass")
                worksheet.conditional_format(
                    1,
                    column_index,
                    len(table),
                    column_index,
                    {
                        "type": "cell",
                        "criteria": "==",
                        "value": False,
                        "format": failed_format,
                    },
                )

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
    "publication_column_width",
    "export_publication_tables",
]
