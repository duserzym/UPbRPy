"""Publication-table quantities, display policy, and portable file writers.

The analytical columns always use the no-calibration branch. ``disc_2s_pct``
is an absolute uncertainty in percentage points; the reduction's historical
``disc_2e%`` is relative to discordance and is deliberately left unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import date
from html import escape
from pathlib import Path
import re
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from .bias import accepted_dates, spot_signal_quantities
from .concentration import calc_concentration, element_from_channel
from .constants import lambda_232, lambda_235, lambda_238
from .results import weighted_mean_and_mswd
from .thermometry import Ti_in_zircon_thermometry
from .uncertainty import (
    common_corrected_systematic_components,
    relative_ratio_date_component,
    systematic_date_components,
)

if TYPE_CHECKING:
    from .pipeline import ReductionPipeline


# Sun and McDonough (1989), doi:10.1144/GSL.SP.1989.042.01.19, µg/g.
CHONDRITE_REE = dict(zip(
    "La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu".split(),
    (.237, .612, .095, .467, .153, .058, .2055, .0374, .254,
     .0566, .1655, .0255, .17, .0254),
))
SYSTEMS = ("206Pb/238U", "207Pb/235U", "207Pb/206Pb", "208Pb/232Th")
_PB_MASSES = dict(zip(("204Pb", "206Pb", "207Pb", "208Pb"),
                      (203.973, 205.974, 206.976, 207.977)))
_ELEMENT_MASSES = dict(zip(
    "P Ti Y Zr Nb La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta Th U".split(),
    (31, 49, 89, 91, 93, 139, 140, 141, 146, 147, 153, 157, 159,
     163, 165, 166, 169, 172, 175, 177, 181, 232, 238),
))


@dataclass(frozen=True)
class ExportSettings:
    """Serializable table choices; activity values must be positive."""

    discordance_definition: str = "7/5-6/8"
    discordance_threshold_percent: float = 5.0
    a_sio2: float = 1.0
    a_tio2: float = .6
    optional_columns: tuple[str, ...] = ()
    excluded_columns: tuple[str, ...] = ()
    csv_full_precision: bool = False
    laboratory: str = ""
    laser_system: str = ""
    spot_diameter_um: str = ""
    repetition_rate_hz: str = ""
    fluence_j_cm2: str = ""
    operator: str = ""
    table_title: str = ""

    def __post_init__(self):
        if self.discordance_definition not in {"7/5-6/8", "7/6-6/8"}:
            raise ValueError("Unsupported discordance_definition")
        for name in ("discordance_threshold_percent", "a_sio2", "a_tio2"):
            value = float(getattr(self, name))
            if not np.isfinite(value) or value < 0 or (name != "discordance_threshold_percent" and value == 0):
                raise ValueError(f"{name} must be finite and {'non-negative' if name == 'discordance_threshold_percent' else 'positive'}")
            object.__setattr__(self, name, value)
        for name in ("optional_columns", "excluded_columns"):
            value = getattr(self, name)
            if not isinstance(value, (tuple, list)) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError(f"{name} must be a sequence of nonempty column keys")
            object.__setattr__(self, name, tuple(dict.fromkeys(value)))
        if not isinstance(self.csv_full_precision, bool):
            raise ValueError("csv_full_precision must be a bool")
        for name in ("laboratory", "laser_system", "spot_diameter_um", "repetition_rate_hz",
                     "fluence_j_cm2", "operator", "table_title"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")

    def to_dict(self) -> dict:
        result = asdict(self)
        for name in ("optional_columns", "excluded_columns"):
            result[name] = list(result[name])
        return result

    @classmethod
    def from_dict(cls, values: Mapping) -> ExportSettings:
        """Validate known fields and ignore keys written by newer versions."""
        return cls(**{f.name: values[f.name] for f in fields(cls) if f.name in values})


@dataclass(frozen=True)
class ExportColumn:
    key: str
    group: str
    label: str
    unit: str = ""
    kind: str = "text"
    optional: bool = False
    footnotes: tuple[str, ...] = ()
    materials: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Footnote:
    """Numbered publication text and the metadata fields still unavailable."""

    id: str
    number: int
    text: str
    missing_fields: tuple[str, ...]


@dataclass
class ExportTable:
    rows: pd.DataFrame
    columns: tuple[ExportColumn, ...]
    sample_summaries: pd.DataFrame
    secondary_summary: pd.DataFrame
    settings: ExportSettings
    context: dict

    def visible_columns(self) -> tuple[ExportColumn, ...]:
        """Select enabled, applicable columns containing at least one value."""
        return tuple(c for c in self.columns
                     if (c.key in self.settings.optional_columns if c.optional
                         else c.key not in self.settings.excluded_columns)
                     and (c.materials is None or self.context.get("material") in c.materials)
                     and c.key in self.rows
                     and self.rows[c.key].replace("", np.nan).notna().any())


def _numeric(frame, key):
    return (pd.to_numeric(frame[key], errors="coerce") if key in frame
            else pd.Series(np.nan, index=frame.index, dtype=float))


def summarize_reference_materials_by_system(
    results: pd.DataFrame, standard_archive: pd.DataFrame, names: Iterable[str], *,
    excluded_spots: Iterable[str] = (),
) -> pd.DataFrame:
    """Summarize analytical dates against the same system's accepted date.

    Flags do not exclude analyses. Glasses are omitted even if explicitly named.
    ``n`` counts observations with finite dates and positive analytical errors.
    """
    columns = ("sample", "system", "n", "weighted_mean", "weighted_mean_2e",
               "mswd", "probability", "accepted_date", "offset_ma", "offset_percent")
    names = tuple(dict.fromkeys(names))
    if results.empty or not names:
        return pd.DataFrame(columns=columns)
    accepted = accepted_dates(standard_archive, names, systems=SYSTEMS)
    frame = results.loc[~results.spot_name.isin(tuple(excluded_spots))]
    records = []
    for name in names:
        if name not in standard_archive.index or "glass" in str(standard_archive.loc[name].get("material", "")).lower():
            continue
        group = frame.loc[frame["sample"].eq(name)]
        if not results["sample"].eq(name).any():
            continue
        for system in SYSTEMS:
            stats = weighted_mean_and_mswd(_numeric(group, system + "_age"),
                                          _numeric(group, system + "_age_2e_no_calibration"))
            reference = accepted.loc[name, system]
            offset = stats["weighted_mean"] - reference
            records.append(dict(sample=name, system=system, **stats, accepted_date=reference,
                                offset_ma=offset, offset_percent=100 * offset / reference
                                if np.isfinite(reference) and reference != 0 else np.nan))
    return pd.DataFrame(records, columns=columns)


def secondary_summary_wide(summary: pd.DataFrame) -> pd.DataFrame:
    """Put the 206/238 and 207/206 summary systems side by side."""
    columns = ["sample", "n"]
    metrics = ("weighted_mean", "weighted_mean_2e", "accepted_date", "offset_percent")
    columns += [f"{suffix}_{metric}" for suffix in ("68", "76") for metric in metrics]
    if summary.empty:
        return pd.DataFrame(columns=columns)
    records = []
    for name, group in summary.groupby("sample", sort=False):
        record = {"sample": name, "n": int(group["n"].max())}
        for system, suffix in ((SYSTEMS[0], "68"), (SYSTEMS[2], "76")):
            rows = group.loc[group.system.eq(system)]
            for metric in metrics:
                record[f"{suffix}_{metric}"] = rows.iloc[0][metric] if len(rows) else np.nan
        records.append(record)
    return pd.DataFrame(records, columns=columns)


def _primary_terms(pipeline):
    """Read only documented uncertainties matching the active archive values.

    Expanded 95% errors use the normal 1.959964 coverage factor. An age error
    is mapped through the local date/ratio derivative before being used for
    other analyses. Missing uncertainties are zero with explicit provenance.
    """
    from .standards import load_standard_measurements

    path = pipeline.reference_library_path
    try:
        measurements = load_standard_measurements(path)
    except (OSError, ValueError, KeyError):
        measurements = pd.DataFrame(columns=("standard", "quantity", "value", "uncertainty", "uncertainty_type"))
    names = (pipeline.config.primary_68_standard, pipeline.config.primary_68_standard,
             pipeline.config.primary_76_standard, pipeline.config.primary_82_standard)
    terms = {}
    for system, name in zip(SYSTEMS, names):
        record = dict(standard=name, relative_ratio_1s=0., available=False,
                      quantity=None, uncertainty_type=None)
        for quantity in (system + "*", system + "_age"):
            candidates = measurements.loc[measurements.standard.eq(name) & measurements.quantity.eq(quantity)]
            if candidates.empty or name not in pipeline.standard_archive.index:
                continue
            row = candidates.iloc[0]
            value = pipeline.standard_archive.loc[name].get(quantity, np.nan)
            if not np.isfinite(value) or not np.isclose(value, row.value, rtol=1e-10, atol=0):
                continue
            factor = {"1sd": 1., "2sd": 2., "expanded_95": 1.959963984540054}.get(row.uncertainty_type)
            if factor is None or not np.isfinite(row.uncertainty):
                continue
            error = row.uncertainty / factor
            relative = (error / relative_ratio_date_component(value, system, 1.)
                        if quantity.endswith("_age") else error / abs(value))
            record.update(relative_ratio_1s=float(relative), available=True, quantity=quantity,
                          value=float(value), uncertainty=float(row.uncertainty),
                          uncertainty_type=row.uncertainty_type, source=row.get("source", ""),
                          source_locator=row.get("source_locator", ""))
            break
        terms[system] = record
    return terms


def _systematic_components(frame, base, system, uncertainty, reference, long_term):
    age = _numeric(frame, base).to_numpy()
    physical = (common_corrected_systematic_components(frame, base, uncertainty)
                if base in {"207Pb_corrected_age", "208Pb_corrected_age"}
                else systematic_date_components(age, system, uncertainty))
    analytical = _numeric(frame, base + "_2e_no_calibration").to_numpy()
    calibration = _numeric(frame, base + "_2e_with_calibration").to_numpy()
    return dict(
        session_1s=np.sqrt(np.maximum(0, calibration**2 - analytical**2)) / 2,
        decay_1s=np.asarray(physical["decay_1s"]),
        u_ratio_1s=np.asarray(physical["u_ratio_1s"]),
        reference_1s=relative_ratio_date_component(age, system, reference),
        long_term_1s=relative_ratio_date_component(age, system, long_term),
    )


def _total_pb(pipeline, normalized, active_standards, results):
    """Calibrate Pb isotope mass fractions with the element pipeline's policy.

    Each glass spot supplies one plateau-mean internally normalized cps value;
    a through-origin least-squares slope maps it to elemental Pb times its
    isotope mass abundance. Sample sweeps use ``calc_concentration``'s same
    Student-t filtering. Thus natural Pb matching the glass composition sums
    to the old 208-based Pb when all four channels are measured and 204 is
    detected. Missing/undetected 204 contributes zero. Radiogenic Pb retains
    the 206 and 207 mass fractions absent from the old estimate.

    Mass 204 uses the existing per-spot baseline 204Hg/202Hg correction, before
    internal normalization. No natural Hg ratio is substituted if that baseline
    ratio cannot be estimated. The input frames and cached stages are unchanged.
    """
    data = normalized.copy()
    measured = [x for x in _PB_MASSES if x in pipeline.channels and x != "204Pb"]
    has204 = "204Pb" in pipeline.channels or "204Hg" in pipeline.channels
    correction = "not measured"
    if has204:
        mass204 = "204Hg" if "204Hg" in data else "204Pb"
        if "202Hg" in data:
            raw_groups = dict(tuple(pipeline.total_data.groupby("spot_name", sort=False)))
            scales = {}
            for row in pipeline.experiment.to_dict("records"):
                raw = raw_groups[row["spot_names"]]
                baseline = raw.loc[raw.Time.ge(row["baseline_start_times"]) & raw.Time.lt(row["baseline_end_times"])]
                denom = baseline["202Hg"].mean()
                scales[row["spot_names"]] = baseline[mass204].mean() / denom if denom > 0 else np.nan
            data["204Pb"] = data[mass204] - data["202Hg"] * data.spot_name.map(scales)
            correction = "per-spot baseline 204Hg/202Hg"
        else:
            data["204Pb"] = np.nan
            correction = "unavailable: no 202Hg channel"
        measured = ["204Pb", *measured]
    compositions = {}
    for name in active_standards:
        row = pipeline.standard_archive.loc[name]
        atoms = np.array([1., row.get("206Pb/204Pb_i", np.nan),
                          row.get("207Pb/204Pb_i", np.nan), row.get("208Pb/204Pb_i", np.nan)])
        masses = atoms * np.array(list(_PB_MASSES.values()))
        if np.isfinite(masses).all() and (masses > 0).all():
            compositions[name] = dict(zip(_PB_MASSES, masses / masses.sum()))
    factors = {}
    glass = data.loc[data["sample"].isin(active_standards)]
    means = glass.groupby("spot_name", sort=False)[measured].mean()
    samples = glass.groupby("spot_name", sort=False)["sample"].first()
    for isotope in measured:
        x = means[isotope].to_numpy()
        y = np.array([pipeline.standard_archive.loc[sample].get("208Pb", np.nan)
                      * compositions.get(sample, {}).get(isotope, np.nan) for sample in samples])
        valid = np.isfinite(x) & np.isfinite(y) & (x > 0)
        factors[isotope] = float(np.dot(x[valid], y[valid]) / np.dot(x[valid], x[valid])) if valid.any() else np.nan
    # calc_concentration normally strips mass numbers. Rename temporarily so
    # each Pb mass is kept as a separate pseudo-element throughout the reducer.
    aliases = {isotope: f"PbMass{chr(65 + i)}" for i, isotope in enumerate(measured)}
    concentrations = calc_concentration(data.rename(columns=aliases),
        pd.Series({aliases[k]: v for k, v in factors.items()}, dtype=float))
    output = concentrations.rename(columns={v: k for k, v in aliases.items()}).set_index("spot_name", drop=False)
    for isotope in _PB_MASSES:
        if isotope not in output:
            output[isotope] = 0.
    detected = _numeric(results.set_index("spot_name"), "206Pb/204Pb").gt(0)
    use204 = (pd.Series(output.index.map(detected).fillna(False).astype(bool), index=output.index)
              & np.isfinite(output["204Pb"]) & output["204Pb"].gt(0))
    omitted = output.index[~use204].tolist()
    output.loc[omitted, "204Pb"] = 0.
    output["Pb"] = output[list(_PB_MASSES)].sum(axis=1, min_count=len(_PB_MASSES))
    return output, dict(measured_channels=measured, glass_mass_abundances=compositions,
                        calibration_factors=factors, hg_correction=correction,
                        atomic_masses=dict(_PB_MASSES),
                        missing_glass_compositions=[name for name in active_standards if name not in compositions],
                        detection_rule="positive integrated 206Pb/204Pb and finite positive calibrated 204Pb",
                        omitted_204_spots=omitted)


def _trace_quantities(frame, settings, uncertainty):
    out = {}
    n = {el: _numeric(frame, el) / value for el, value in CHONDRITE_REE.items()}
    out.update({el + "_N": value for el, value in n.items()})
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        out["Ti_in_zircon_C"] = Ti_in_zircon_thermometry(_numeric(frame, "Ti").where(_numeric(frame, "Ti") > 0), settings.a_sio2, settings.a_tio2)
        out["Ce/Ce*"] = n["Ce"] / np.sqrt(n["La"] * n["Pr"])
        out["Eu/Eu*"] = n["Eu"] / np.sqrt(n["Sm"] * n["Gd"])
        out["sum_REE"] = pd.DataFrame({el: _numeric(frame, el) for el in CHONDRITE_REE}).sum(axis=1, min_count=len(CHONDRITE_REE))
        out["(Gd/Yb)_N"] = n["Gd"] / n["Yb"]
        for ratio in ("Lu/Hf", "Nb/Ta", "Nb/U", "Nb/Th", "Th/Y", "Hf/Y", "Nb/Yb", "Ce/Yb", "Gd/Yb", "U/Yb", "Nb/P", "Ce/P", "Dy/Yb", "Ce/U"):
            a, b = ratio.split("/")
            out[ratio] = _numeric(frame, a) / _numeric(frame, b).replace(0, np.nan)
        out["sum_REE_Y"] = out["sum_REE"] + _numeric(frame, "Y")
        out["10000Eu/Eu*/Y"] = 10000 * out["Eu/Eu*"] / _numeric(frame, "Y").replace(0, np.nan)
        out["(Ce/Nd)/Y"] = _numeric(frame, "Ce") / _numeric(frame, "Nd").replace(0, np.nan) / _numeric(frame, "Y").replace(0, np.nan)
        years = _numeric(frame, "preferred_date") * 1e6
        f238 = uncertainty.u238_u235 / (1 + uncertainty.u238_u235)
        out["U_i"] = _numeric(frame, "U") * (f238 * np.exp(lambda_238 * years) + (1 - f238) * np.exp(lambda_235 * years))
        out["Th_i"] = _numeric(frame, "Th") * np.exp(lambda_232 * years)
        out["10000Eu/Eu*/Yb_N"] = 10000 * out["Eu/Eu*"] / n["Yb"].replace(0, np.nan)
        out["1000(Ce/Nd)_N/Y"] = 1000 * n["Ce"] / n["Nd"].replace(0, np.nan) / _numeric(frame, "Y").replace(0, np.nan)
    return pd.DataFrame(out, index=frame.index).replace([np.inf, -np.inf], np.nan)


def _catalogue(elements):
    columns = []
    def add(key, group, unit="", kind="text", optional=False, footnotes=(), materials=None, label=None):
        columns.append(ExportColumn(key, group, label or key, unit, kind, optional, footnotes, materials))
    add("spot_name", "Analysis", footnotes=("session", "method"), label="Analysis")
    add("notes", "Analysis", footnotes=("notes", "glasses"), label="Notes")
    for key in ("U", "Th", "Pb", "204Pb", "206Pb", "207Pb", "208Pb", "Pb_208"):
        add(key, "Composition", "µg/g", "conc", key not in {"U", "Th", "Pb"}, ("total_pb",) if "Pb" in key else (), label="Pb (208)" if key == "Pb_208" else key)
    add("Th/U", "Composition", kind="ratio_el")
    add("206Pb_cps", "Composition", "cps", "int", label="206Pb")
    add("206Pb/204Pb", "Composition", kind="int", footnotes=("pb204",))
    add("206Pb/204Pb_1s", "Composition", kind="int", footnotes=("pb204",), label="206Pb/204Pb ±1s")
    for system in (SYSTEMS[3], SYSTEMS[1], SYSTEMS[0], "238U/206Pb", SYSTEMS[2], "208Pb/206Pb"):
        optional = system == "208Pb/206Pb"
        add(system, "Isotope ratios", kind={SYSTEMS[0]: "ratio_68", SYSTEMS[1]: "ratio_75", SYSTEMS[3]: "ratio_82", "238U/206Pb": "ratio_86"}.get(system, "ratio_76"), optional=optional, footnotes=("standards", "corrections"))
        add(system + "_2e_no_calibration%", "Isotope ratios", "%", "pct_2s", optional, ("uncertainty",), label=system + " ±2s")
        if system == SYSTEMS[0]:
            add("err_corr_no_calibration", "Isotope ratios", kind="rho", label="ρ 206/238–207/235")
    add("tw_rho_no_calibration", "Isotope ratios", kind="rho", optional=True, label="ρ Tera-Wasserburg")
    for system in (SYSTEMS[3], SYSTEMS[2], SYSTEMS[1], SYSTEMS[0]):
        base = system + "_age"
        add(base, "Dates", "Ma", "date", label=system + " date")
        add(base + "_2e_no_calibration", "Dates", "Ma", "date_2s", footnotes=("uncertainty",), label=system + " date ±2s")
        add(base + "_2e_sys", "Dates", "Ma", "date_2s", footnotes=("uncertainty", "constants"), label=system + " date ±2s-sys")
    add("disc_pct", "Dates", "%", "pct", footnotes=("discordance",), label="Discordance")
    add("disc_2s_pct", "Dates", "%", "pct_2s", label="Discordance ±2s")
    for method in ("207", "208"):
        base = method + "Pb_corrected_age"
        for suffix, label in (("", "date"), ("_2e_no_calibration", "date ±2s"), ("_2e_sys", "date ±2s-sys")):
            add(base + suffix, "Common-Pb corrected", "Ma", "date" if not suffix else "date_2s", footnotes=("corrections", "uncertainty"), label=f"{method}-method 206Pb*/238U* {label}")
    for key, label, kind in (("preferred_date", "Preferred date", "date"), ("preferred_date_2e", "Preferred date ±2s", "date_2s"), ("preferred_date_2e_sys", "Preferred date ±2s-sys", "date_2s"), ("preferred_date_method", "Method", "text")):
        add(key, "Preferred date", "" if kind == "text" else "Ma", kind, footnotes=("preferred", "uncertainty"), label=label)
    for element in elements:
        # The spreadsheet repeats U and Th in this block. Distinct keys allow
        # independent visibility while keeping one authoritative source value.
        key = element + "_conc_ppm" if element in {"U", "Th"} else element
        add(key, "Concentrations", "µg/g", "conc", label=element)
        add(element + "_95CI", "Concentrations", "µg/g", "conc", True, ("uncertainty",), label=element + " 95% CI")
    for element in CHONDRITE_REE:
        add(element + "_N", "Chondrite-normalized", kind="conc", footnotes=("chondrite",), label=element)
    zircon = ("zircon",)
    add("Ti_in_zircon_C", "Ti-in-zircon", "°C", "temp", footnotes=("ti_in_zircon",), materials=zircon, label="T")
    groups = {
        "Trace-element ratios": ("Ce/Ce*", "Eu/Eu*", "sum_REE", "(Gd/Yb)_N", "Lu/Hf", "Nb/Ta", "Nb/U", "Nb/Th", "Th/Y", "Hf/Y"),
        "Grimes et al. (2015)": ("Nb/Yb", "Ce/Yb", "Gd/Yb", "U/Yb"),
        "Itano '24": ("Nb/P", "Ce/P", "sum_REE_Y"),
        "Lu et al. (2016); Pizarro et al. (2020)": ("Dy/Yb", "10000Eu/Eu*/Y", "(Ce/Nd)/Y"),
        "Roberts et al. (2024)": ("Ce/U",),
        "Loucks et al. (2024)": ("U_i", "Th_i", "10000Eu/Eu*/Yb_N", "1000(Ce/Nd)_N/Y"),
    }
    for (group, keys), citation in zip(groups.items(), ("chondrite", "grimes2015", "itano24", "lu_pizarro", "roberts2024", "loucks2024")):
        for key in keys:
            mass = key in {"sum_REE", "sum_REE_Y", "U_i", "Th_i"}
            add(key, group, "µg/g" if mass else "", "conc" if mass else "ratio_el", footnotes=(citation,), materials=zircon,
                label={"sum_REE": "ΣREE", "sum_REE_Y": "ΣREE+Y", "10000Eu/Eu*/Y": "10,000(Eu/Eu*)/Y",
                       "10000Eu/Eu*/Yb_N": "10,000(Eu/Eu*)/Yb_N"}.get(key))
    return tuple(columns)


def _sample_summaries(rows, components, secondary, uncertainty, reference, long_term):
    records = []
    for sample, group in rows.groupby("sample", sort=False):
        included = group.loc[~group.excluded]
        methods = included.preferred_date_method.dropna().unique()
        stats = weighted_mean_and_mswd(included.preferred_date, included.preferred_date_2e)
        k = stats["n"]
        status = ("glass" if group.role.iloc[0] == "glass" else "mixed methods" if len(methods) > 1 else
                  "ok" if len(methods) == 1 and stats["probability"] >= .001 else "dispersed")
        sys = np.nan
        if status == "ok":
            valid = np.isfinite(included.preferred_date) & np.isfinite(included.preferred_date_2e) & included.preferred_date_2e.gt(0)
            chosen = included.loc[valid]
            weights = 1 / chosen.preferred_date_2e.to_numpy()**2
            method = str(methods[0])
            system = next((s for s in SYSTEMS if s in method), SYSTEMS[0])
            mean = stats["weighted_mean"]
            # Infer the shared session term in ratio space, then apply it at
            # the mean. It must not shrink with the number of analyses.
            date_per_relative = relative_ratio_date_component(chosen.preferred_date, system, 1.)
            session_relative = np.average(
                components.session_1s.loc[chosen.index] / date_per_relative, weights=weights)
            if method in {"207Pb-corrected", "208Pb-corrected"}:
                base = method[:3] + "Pb_corrected_age"
                mean_frame = chosen.select_dtypes(include="number").apply(
                    lambda col: np.average(col, weights=weights)).to_frame().T
                mean_frame[base] = mean
                physical = common_corrected_systematic_components(mean_frame, base, uncertainty)
            else:
                physical = systematic_date_components(mean, system, uncertainty)
            shared = [relative_ratio_date_component(mean, system, session_relative),
                      np.asarray(physical["decay_1s"]).item(),
                      np.asarray(physical["u_ratio_1s"]).item(),
                      relative_ratio_date_component(mean, system, reference[system]["relative_ratio_1s"]),
                      relative_ratio_date_component(mean, system, long_term)]
            sys = float(np.sqrt(stats["weighted_mean_2e"]**2 + 4 * np.sum(np.square(shared))))
        title = f"{sample} (n = {len(group)}, {int(group.excluded.sum())} excluded)"
        if group.role.iloc[0] in {"primary", "secondary"}:
            parts = []
            for system in (SYSTEMS[0], SYSTEMS[2]):
                candidates = secondary.loc[secondary["sample"].eq(sample) & secondary.system.eq(system)]
                if candidates.empty:
                    continue
                record = candidates.iloc[0]
                parts.append(f"{system} {record.weighted_mean:.1f} ± {record.weighted_mean_2e:.1f} Ma (2s; accepted {record.accepted_date:.1f} Ma)")
            text = title + "; " + "; ".join(parts)
        elif group.role.iloc[0] == "glass":
            text = f"{title}; concentration reference glass"
        elif status == "ok":
            text = f"{title}; weighted mean {methods[0]} date = {stats['weighted_mean']:.1f} ± {stats['weighted_mean_2e']:.1f} / ± {sys:.1f} Ma (2s; MSWD = {stats['mswd']:.2f}, n = {k})"
        elif status == "mixed methods":
            # A pooled MSWD across 206/238 and 207/206 dates means nothing.
            counts = included.preferred_date_method.value_counts()
            text = (f"{title}; preferred dates use mixed methods ("
                    + ", ".join(f"{method} {count}" for method, count in counts.items())
                    + "); no weighted mean")
        else:
            text = f"{title}; {status}: MSWD = {stats['mswd']:.2f}, n = {k}; no weighted mean"
        text = re.sub(r"\bnan\b", "not available", text, flags=re.I)
        records.append(dict(sample=sample, role=group.role.iloc[0], n=len(group), n_excluded=int(group.excluded.sum()),
                            status=status, method=methods[0] if len(methods) == 1 else "", k=k,
                            mean=stats["weighted_mean"] if status == "ok" else np.nan,
                            **{"2s": stats["weighted_mean_2e"] if status == "ok" else np.nan, "2s_sys": sys},
                            mswd=stats["mswd"], probability=stats["probability"], summary_text=text))
    return pd.DataFrame(records)


def build_export_table(pipeline: ReductionPipeline, settings: ExportSettings | None = None, *,
                       excluded_spots: Iterable[str] = ()) -> ExportTable:
    """Build the full catalogue and publication quantities from cached stages.

    Input reduction columns, including legacy discordance and preferred-date
    error policies, are never modified. Exclusions affect summaries only.
    """
    settings = settings or pipeline.config.export_settings or ExportSettings()
    excluded = set(excluded_spots)
    finalized = pipeline.run_finalize()
    frame = finalized.parallel_results.copy().reset_index(drop=True)
    config = pipeline.config
    primary = tuple(dict.fromkeys((config.primary_68_standard, config.primary_76_standard, config.primary_82_standard)))
    primary = tuple(x for x in primary if x)
    rows = frame.copy()
    rows["role"] = rows["sample"].map(lambda name: "primary" if name in primary else "secondary" if name in config.secondary_standards else "glass" if name in config.concentration_standards else "unknown")
    # Studio hides glasses from the concordia review by default; that is not
    # a rejection, and glasses are never part of a U-Pb summary.
    rows["excluded"] = rows.spot_name.isin(excluded) & rows.role.ne("glass")
    order = pipeline.experiment.sort_values("spot_starting_times", kind="stable").spot_names.tolist()
    rows["run_order"] = rows.spot_name.map({name: i + 1 for i, name in enumerate(order)})
    signals = spot_signal_quantities(pipeline.experiment, pipeline.run_baseline()).set_index("spot_name")
    rows["206Pb_cps"] = rows.spot_name.map(signals["206Pb_cps"])
    rows["Pb_208"] = _numeric(rows, "Pb")
    # A legacy concentration is never labelled total Pb without isotope
    # calibration, including when a caller supplies a cached final frame.
    rows["Pb"] = np.nan
    element_masses = dict(_ELEMENT_MASSES)
    pb_context = dict(measured_channels=[], omitted_204_spots=rows.spot_name.tolist(), hg_correction="not calibrated")
    if config.concentration_standards:
        concentration = pipeline.run_concentration()
        normalized = pipeline.run_normalization()
        # Calibrant concentrations are intentionally absent from finalization;
        # calculate their table values using the already fitted factors.
        glass_conc = calc_concentration(normalized.loc[normalized["sample"].isin(
            concentration["active_standards"])], concentration["calibration"])
        all_conc = pd.concat([concentration["final_concentration_df"], glass_conc],
                             ignore_index=True).set_index("spot_name", drop=False)
        for key in all_conc:
            if key not in {"spot_name", "sample"}:
                rows[key] = rows.spot_name.map(all_conc[key])
        rows["Pb_208"] = _numeric(rows, "Pb")
        pb, pb_context = _total_pb(pipeline, normalized, concentration["active_standards"], rows)
        for key in ("Pb", *_PB_MASSES):
            rows[key] = rows.spot_name.map(pb[key])
        for channel in concentration["calibration"].index:
            element = element_from_channel(channel)
            if element and element != "Pb":
                element_masses[element] = int(''.join(filter(str.isdigit, channel)) or element_masses.get(element, 0))
    elements = sorted(element_masses, key=element_masses.get)
    for element in ("U", "Th"):
        rows[element + "_conc_ppm"] = _numeric(rows, element)
    th_u = _numeric(rows, "Th") / _numeric(rows, "U").replace(0, np.nan)
    rows["Th/U"] = th_u.combine_first(_numeric(rows, "Th/U"))
    rows["206Pb/204Pb_1s"] = _numeric(rows, "206Pb/204Pb_2e") / 2
    for system in (*SYSTEMS, "238U/206Pb"):
        key = system + "_2e_no_calibration%"
        if key not in rows:
            rows[key] = 100 * _numeric(rows, system + "_2e_no_calibration") / _numeric(rows, system).abs().replace(0, np.nan)
    rows["208Pb/206Pb"] = 1 / _numeric(rows, "206Pb/208Pb").replace(0, np.nan)
    rows["208Pb/206Pb_2e_no_calibration%"] = 100 * _numeric(rows, "206Pb/208Pb_2e") / _numeric(rows, "206Pb/208Pb").abs().replace(0, np.nan)
    reference = _primary_terms(pipeline)
    long_term = (config.uncertainty.long_term_reproducibility_relative_1s if config.uncertainty.long_term_reproducibility_mode == "user" else
                 float(frame.attrs.get("long_term_reproducibility_relative_1s_used", 0.)) if config.uncertainty.long_term_reproducibility_mode == "secondary" else 0.)
    components = {}
    for base in [s + "_age" for s in SYSTEMS] + ["207Pb_corrected_age", "208Pb_corrected_age", "204Pb_corrected_206Pb/238U_age", "204Pb_corrected_207Pb/206Pb_age"]:
        if base not in rows:
            continue
        system = next((s for s in SYSTEMS if s in base), SYSTEMS[0])
        parts = _systematic_components(rows, base, system, config.uncertainty, reference[system]["relative_ratio_1s"], long_term)
        components[base] = parts
        rows[base + "_2e_sys"] = np.sqrt(_numeric(rows, base + "_2e_no_calibration")**2 + 4 * sum(v**2 for v in parts.values()))
    preferred_parts = pd.DataFrame(np.nan, index=rows.index, columns=("session_1s", "decay_1s", "u_ratio_1s", "reference_1s", "long_term_1s"))
    for method in rows.preferred_date_method.dropna().unique():
        base = (str(method)[:3] + "Pb_corrected_age" if method in {"207Pb-corrected", "208Pb-corrected"}
                else ("204Pb_corrected_" if str(method).startswith("204Pb-corrected ") else "") + str(method).split()[-1] + "_age")
        mask = rows.preferred_date_method.eq(method)
        rows.loc[mask, "preferred_date_2e"] = _numeric(rows, base + "_2e_no_calibration")[mask]
        rows.loc[mask, "preferred_date_2e_sys"] = _numeric(rows, base + "_2e_sys")[mask]
        if base in components:
            preferred_parts.loc[mask] = pd.DataFrame(components[base], index=rows.index).loc[mask]
    other = SYSTEMS[1] if settings.discordance_definition == "7/5-6/8" else SYSTEMS[2]
    a, b = _numeric(rows, SYSTEMS[0] + "_age"), _numeric(rows, other + "_age").replace(0, np.nan)
    ea, eb = _numeric(rows, SYSTEMS[0] + "_age_2e_no_calibration"), _numeric(rows, other + "_age_2e_no_calibration")
    rows["disc_pct"] = 100 * (1 - a / b)
    # First-order date propagation, with analytical 68/75 correlation where
    # supplied; the 68/76 date cross-covariance is not a public spot output.
    rho = _numeric(rows, "err_corr_no_calibration").fillna(0).clip(-1, 1) if other == SYSTEMS[1] else 0.
    rows["disc_2s_pct"] = 100 * np.sqrt(np.maximum(0, (ea / b)**2 + (a * eb / b**2)**2 - 2 * rho * ea * eb * a / b**3))
    rows = pd.concat([rows, _trace_quantities(rows, settings, config.uncertainty)], axis=1)
    flags = (("flag_detectable_204pb", "204Pb detected"), ("flag_reverse_fractionation", "reverse fractionation"), ("flag_high_common_pb", "high common Pb"), ("flag_burn_through", "burn-through"), ("flag_high_ratio_scatter", "high ratio scatter"), ("flag_u_mean_intercept_mismatch", "U mean/intercept mismatch"), ("flag_high_uncertainty", "high uncertainty"), ("flag_session_extrapolation", "session extrapolation"))
    notes = []
    for row in rows.to_dict("records"):
        words = ["excluded"] if row["excluded"] else []
        if abs(row["disc_pct"]) - row["disc_2s_pct"] > settings.discordance_threshold_percent and row["role"] != "glass":
            words.append("discordant")
        if row["role"] == "glass":
            # U-Pb screening flags have no meaning for a concentration glass.
            words.append("concentration reference glass")
        else:
            words += [label for key, label in flags if pd.notna(row.get(key)) and bool(row.get(key))]
        notes.append(", ".join(words))
    rows["notes"] = notes
    columns = _catalogue(elements)
    for column in columns:
        if column.key not in rows:
            rows[column.key] = np.nan
    blank = [c.key for c in columns if c.group in {"Isotope ratios", "Dates", "Common-Pb corrected", "Preferred date"} or c.key in {"206Pb/204Pb", "206Pb/204Pb_1s"}]
    for key in blank:
        rows.loc[rows.role.eq("glass"), key] = "" if key == "preferred_date_method" else np.nan
    secondary = summarize_reference_materials_by_system(rows, pipeline.standard_archive,
        (*primary, *config.secondary_standards), excluded_spots=excluded)
    first = rows.groupby("sample").run_order.min()
    rows["_role_order"] = rows.role.map({"unknown": 0, "primary": 1, "secondary": 2, "glass": 3})
    rows["_sample_order"] = rows["sample"].map(first)
    rows = rows.sort_values(["_role_order", "_sample_order", "run_order"], kind="stable")
    summaries = _sample_summaries(rows, preferred_parts, secondary, config.uncertainty, reference, long_term)
    rows = rows[[c.key for c in columns] + ["sample", "role", "excluded", "run_order"]].reset_index(drop=True)
    context = dict(material=config.material, channels=tuple(pipeline.channels),
        reference_library_path=pipeline.reference_library_path,
        session_name=Path(pipeline.source_directory).stem if pipeline.source_directory else "session",
        primary_standards=dict(zip(SYSTEMS, (config.primary_68_standard, config.primary_68_standard, config.primary_76_standard, config.primary_82_standard))),
        # Only materials analysed in this session are cited in footnotes.
        secondary_standards=tuple(x for x in config.secondary_standards if rows["sample"].eq(x).any()),
        concentration_standards=config.concentration_standards,
        active_concentration_standards=tuple(concentration["active_standards"]) if config.concentration_standards else (),
        decision=asdict(finalized.decision), uncertainty_settings=config.uncertainty.to_dict(),
        primary_reference_terms=reference, long_term_relative_1s=long_term,
        systematic_components={base: {key: dict(zip(frame.spot_name, values.tolist())) for key, values in parts.items()} for base, parts in components.items()},
        total_pb=pb_context, discordance_covariance="analytical 68/75 rho; independent 68/76 dates",
        experiment_metadata=dict(pipeline.experiment.attrs), selections=pipeline.experiment.to_dict("records"),
        reference_values=pipeline.standard_archive.to_dict("index"),
        manifest=pipeline.manifest(metadata={"export_settings": settings.to_dict()}))
    return ExportTable(rows, columns, summaries, secondary, settings, context)


def _git_commit(package_root=None):
    """Read only the adjacent checkout's Git metadata, including worktrees."""
    root = Path(package_root) if package_root is not None else Path(__file__).resolve().parent.parent
    gitdir = root / ".git"
    try:
        if gitdir.is_file():
            directive = gitdir.read_text().strip()
            if not directive.startswith("gitdir: "):
                return None
            gitdir = (root / directive[8:]).resolve()
        head = (gitdir / "HEAD").read_text().strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head):
            return head.lower()
        if not head.startswith("ref: refs/"):
            return None
        ref = head[5:]
        if ".." in Path(ref).parts:
            return None
        dirs = [gitdir]
        if (gitdir / "commondir").is_file():
            dirs.append((gitdir / (gitdir / "commondir").read_text().strip()).resolve())
        for directory in dirs:
            loose = directory / ref
            if loose.is_file():
                value = loose.read_text().strip()
                if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
                    return value.lower()
            packed = directory / "packed-refs"
            if packed.is_file():
                for line in packed.read_text().splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", parts[0]):
                        return parts[0].lower()
    except (OSError, UnicodeError):
        pass
    return None


def _reference_records(measurements, archive, name, system, seen=()):
    """Trace a ratio through the library's direct, age and algebraic inputs.

    Only records matching active archive values are cited. The public loader
    expands source IDs to display text; its attrs retain the registry used to
    resolve those displays back to DOI metadata.
    """
    if name not in archive.index or system in seen:
        return measurements.iloc[:0]
    def direct(quantity):
        candidates = measurements.loc[measurements.standard.eq(name) & measurements.quantity.eq(quantity)]
        value = archive.loc[name].get(quantity, np.nan)
        return candidates.loc[np.isclose(candidates.value, value, rtol=1e-10, atol=0)]
    for quantity in (system + "*", system + "_age"):
        rows = direct(quantity)
        if not rows.empty:
            return rows
    # The archive derives 68/75/82 from a concordia age before algebraic links.
    if system != SYSTEMS[2]:
        rows = direct("concordia_age")
        if not rows.empty:
            return rows
    links = {SYSTEMS[0]: (SYSTEMS[1], SYSTEMS[2]),
             SYSTEMS[1]: (SYSTEMS[0], SYSTEMS[2]),
             SYSTEMS[2]: (SYSTEMS[0], SYSTEMS[1])}
    if system in links:
        parts = [_reference_records(measurements, archive, name, s, (*seen, system)) for s in links[system]]
        if all(not part.empty for part in parts):
            return pd.concat(parts).drop_duplicates()
    return direct("concordia_age") if system == SYSTEMS[2] else measurements.iloc[:0]


def build_footnotes(table: ExportTable, pipeline: ReductionPipeline) -> list[Footnote]:
    """Generate global notes, then notes in first visible-column use order.

    No reduction stages or table values are changed. Missing field paths are
    stable identifiers for an export preview; missing values are never guessed.
    Rebuild the notes after changing visible columns or export settings.
    """
    from .standards import build_standard_archive, load_standard_measurements

    context, settings = table.context, table.settings
    manifest = context["manifest"]
    config = manifest["config"]
    uncertainty = context["uncertainty_settings"]
    metadata = context.get("experiment_metadata", {})
    qtegra = metadata.get("qtegra", {})
    acquisition = qtegra.get("acquisition_summary", {})
    notes = {}
    missing = {}
    def fmt(note, field, value, unit="", precision=None):
        if value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (int, float)) and not np.isfinite(value)):
            missing.setdefault(note, []).append(field)
            return "not recorded"
        return (format(value, precision or ".8g") if isinstance(value, (int, float)) else str(value)) + (" " + unit if unit else "")
    def sentence(note, value):
        notes[note] = value

    sources = qtegra.get("sources", [])
    rich = [Path(s["path"]).stem for s in sources if s.get("format") == "rich"]
    paths = [Path(s["path"]) for s in sources] or [Path(s["path"]) for s in manifest.get("input_files", [])]
    session_name = (metadata.get("session_name") or manifest.get("metadata", {}).get("origin", {}).get("session_key")
                    or (", ".join(re.sub(r"_Rich$", "", x, flags=re.I) for x in rich) if rich else None)
                    or (paths[0].parent.name if paths else None))
    first, last = acquisition.get("first_start_date"), acquisition.get("last_start_date")
    dates = first if first == last else f"{first} to {last}" if first and last else None
    starts = None
    if acquisition.get("first_start") and acquisition.get("last_start"):
        start, end = (str(acquisition[key]).replace("T", " ")[:16]
                      for key in ("first_start", "last_start"))
        starts = f"{start} to {end[11:] if start[:10] == end[:10] else end} (instrument clock)"
    session_fields = [("session_name", "Session", session_name, ""),
        ("acquisition_starts", "acquisition starts", starts or dates, ""),
        ("operator", "operator", settings.operator or ", ".join(acquisition.get("users", [])), ""),
        ("instrument", "instrument", acquisition.get("machine") or qtegra.get("machine"), ""),
        ("laboratory", "laboratory", settings.laboratory, ""),
        ("laser_system", "laser system", settings.laser_system, ""),
        ("spot_diameter_um", "spot diameter", settings.spot_diameter_um, "µm"),
        ("repetition_rate_hz", "repetition rate", settings.repetition_rate_hz, "Hz"),
        ("fluence_j_cm2", "fluence", settings.fluence_j_cm2, "J/cm²")]
    sentence("session", "; ".join(f"{label}: {fmt('session', field, value, unit)}" for field, label, value, unit in session_fields) + ".")

    grouped = {}
    for channel, value in acquisition.get("dwell_s", {}).items():
        grouped.setdefault(value, []).append(channel)
    dwell_parts = []
    for value, channels in sorted(grouped.items(), key=lambda item: (item[0] is None, item[0] or 0)):
        for channel in channels:
            display = fmt("method", "dwell_s." + channel, value, "s")
        dwell_parts.append(f"{', '.join(channels)} {display}")
    dwell = "; ".join(dwell_parts)
    if not dwell:
        dwell = fmt("method", "dwell_s", None)
    sweeps = acquisition.get("sweeps_per_acquisition", {})
    windows = []
    selections = pd.DataFrame(context.get("selections", []))
    methods = selections.get("selection_method", pd.Series(dtype=str)).dropna().unique()
    selection = ", ".join(map(str, methods)) or config.get("selection_method")
    for region in ("baseline", "plateau"):
        duration = _numeric(selections, region + "_end_times") - _numeric(selections, region + "_start_times")
        duration = duration.loc[np.isfinite(duration) & duration.ge(0)]
        windows.append(f"{region} median duration {fmt('method', region + '_duration_s', float(duration.median()) if len(duration) else None, 's', '.1f')}")
    tune = fmt("method", "tune_names", ", ".join(acquisition.get("tune_names", [])))
    laser_output = fmt("method", "laser_output_settings_percent", ", ".join(acquisition.get("laser_output_settings_percent", [])), "%")
    sentence("method", f"Dwell times per channel (median over acquisitions): {dwell}. "
        f"Sweeps per analysis: median {fmt('method', 'sweeps.median', sweeps.get('median'))}, "
        f"range {fmt('method', 'sweeps.min', sweeps.get('min'))} to {fmt('method', 'sweeps.max', sweeps.get('max'))}. "
        f"Window selection method: {fmt('method', 'selection_method', selection)}; {'; '.join(windows)}. "
        f"Ratio aggregation: {fmt('method', 'ratio_aggregation', config.get('ratio_aggregation'))}. "
        f"Tune names: {tune}. Laser output settings seen: {laser_output}.")

    try:
        measurements = load_standard_measurements(pipeline.reference_library_path)
    except (OSError, ValueError, KeyError):
        measurements = pd.DataFrame(columns=("standard", "quantity", "role", "value", "source"))
    registry = measurements.attrs.get("sources", {})
    library_archive = build_standard_archive(measurements) if not measurements.empty else pd.DataFrame()
    def citations(note, field, records, compact=False):
        values = []
        for display in records.source.unique():
            entries = [entry for entry in registry.values() if entry["display"] == display]
            entry = entries[0] if len(entries) == 1 else {}
            doi = entry.get("doi")
            text = display
            if compact:
                author_year = re.match(r"(.+?)\s*\((\d{4})\)", display)
                if author_year:
                    text = f"{author_year[1]}, {author_year[2]}"
                elif "UPbR26plus" in display:
                    text = "UPbR26plus archive value"
            if doi and doi not in text:
                text += f", doi:{doi}"
            elif not doi:
                missing_doi = fmt(note, field + ".doi", None)
                text += ", no DOI" if compact else "; DOI " + missing_doi
            values.append(text)
        return "; ".join(values) if values else fmt(note, field + ".citation", None)
    standard_text = []
    primaries = context["primary_standards"]
    names = tuple(dict.fromkeys((*primaries.values(), *context["secondary_standards"])))
    dates = accepted_dates(pipeline.standard_archive, names, systems=SYSTEMS)
    for role in ("Primary", "Secondary"):
        for name in (tuple(dict.fromkeys(primaries.values())) if role == "Primary" else context["secondary_standards"]):
            systems = [s for s in SYSTEMS if primaries[s] == name] if role == "Primary" else SYSTEMS
            row = pipeline.standard_archive.loc[name] if name in pipeline.standard_archive.index else pd.Series(dtype=float)
            parts, first_citation = [], None
            for system in systems:
                field = f"{role.lower()}.{name or 'unassigned'}.{system}"
                reference_records = _reference_records(measurements, pipeline.standard_archive, name, system)
                if (name not in library_archive.index or not np.isclose(
                        row.get(system + "*", np.nan), library_archive.loc[name].get(system + "*", np.nan), rtol=1e-10, atol=0)):
                    reference_records = measurements.iloc[:0]
                ratio_source_missing = reference_records.empty
                age_records = measurements.loc[measurements.standard.eq(name)
                    & measurements.role.eq("reference_age") & measurements.quantity.eq(system + "_age")]
                age_records = age_records.loc[np.isclose(age_records.value, row.get(system + "_age", np.nan), rtol=1e-10, atol=0)]
                # Reference-age inputs can supply uncertainty independently of
                # the ratio defining accepted_dates; cite both provenances.
                reference_records = pd.concat([reference_records, age_records]).drop_duplicates()
                citation = citations("standards", field, reference_records, compact=True)
                if ratio_source_missing and not age_records.empty:
                    citation = ("accepted-ratio citation " + fmt("standards", field + ".citation", None)
                                + "; reference-age citation " + citation)
                if first_citation is None:
                    first_citation = citation
                extra = f"; {citation}" if citation != first_citation else ""
                parts.append(f"{system} {fmt('standards', field + '.ratio', row.get(system + '*'), precision='.5g')} "
                    f"({fmt('standards', field + '.date', dates.loc[name, system], 'Ma', '.2f')}{extra})")
            standard_text.append(f"{role} standard {fmt('standards', role.lower() + '.name', name)} ({first_citation}): " + ", ".join(parts))
    glasses = {}
    for name in context["active_concentration_standards"]:
        records = measurements.loc[measurements.standard.eq(name) & measurements.role.eq("concentration")]
        glasses.setdefault(citations('standards', 'glass.' + name, records, compact=True), []).append(name)
    for citation, glass_names in glasses.items():
        standard_text.append(f"Concentration reference glasses {' and '.join(glass_names)} ({citation})")
    sentence("standards", ". ".join(standard_text) + ".")

    propagated = "is propagated" if uncertainty["propagate_baseline"] else "is not propagated (disabled in settings)"
    detector = manifest.get("detector_calibration", {})
    model_text = []
    for model in ("downhole", "session"):
        fit = config.get(model, {})
        # Option dictionaries carry defaults for every model; name only the
        # options the chosen method reads.
        used_by = {"degree": {"polynomial", "orthogonal_polynomial"},
                   "exponential_initial_decay": {"exponential"},
                   "running_mean_window": {"running_mean"}}
        options = ", ".join(f"{key} = {value}" for key, value in sorted(fit.get("options", {}).items())
                            if fit.get("method") in used_by.get(key, {fit.get("method")})) or "no options"
        rejection = (f"outlier sigma = {fit['outlier_sigma']}" if fit.get("outlier_sigma") is not None
                     else "outlier rejection disabled")
        model_text.append(f"{model.capitalize()} model: {fmt('corrections', model + '.method', fit.get('method'))} "
                          f"({options}; {rejection})")
    # Reports may contain acquisition arrays for drift: summarize factors only.
    channels = detector.get("channels", {})
    factors = []
    for channel, record in channels.items():
        if not isinstance(record, dict):
            continue
        parts = [f"{key} {fmt('corrections', 'detector_calibration.' + channel + '.' + key, record.get(key))}"
                 for key in ("factor", "qtegra_factor")]
        drift = np.asarray(record.get("factors", []), dtype=float)
        drift = drift[np.isfinite(drift)]
        if drift.size:
            parts.append(f"drift-factor range {drift.min():.8g} to {drift.max():.8g} (median {np.median(drift):.8g})")
        factors.append(f"{channel}: {', '.join(parts)}")
    factor_text = "; ".join(factors) or fmt("corrections", "detector_calibration.factors", None)
    common_methods = [method for method in ("207", "208") if any(
        c.key.startswith(method + "Pb_corrected_age") for c in table.visible_columns())]
    common_options = config.get("common_pb_options", {})
    initial_pb = ("Initial Pb follows Stacey and Kramers (1975), doi:10.1016/0012-821X(75)90088-6. "
                  if common_options.get("common_pb_model", "stacey_kramers") == "stacey_kramers"
                  else "Initial Pb uses the configured fixed composition. ")
    if common_options:
        initial_pb += "Common-Pb options: " + "; ".join(f"{key} = {value}" for key, value in sorted(common_options.items())) + ". "
    sentence("corrections", f"Per-spot baselines are subtracted; baseline-mean uncertainty {propagated}. "
        + ". ".join(model_text) + ". "
        + ("Mass 204 is corrected for 204Hg via 202Hg where available; " if "202Hg" in pipeline.channels else "204Hg correction via 202Hg is unavailable because 202Hg was not measured; ")
        + f"total-Pb mass-204 correction: {context['total_pb']['hg_correction']}. "
        + f"Common-Pb method dates reported: {', '.join(common_methods) + ' method' if common_methods else 'none'}. "
        + initial_pb
        + f"Preferred-date common-Pb method: {context['decision']['common_pb_method']}. "
        + f"Detector cross-calibration mode: {fmt('corrections', 'detector_calibration.mode', detector.get('mode'))}; factors {factor_text}.")

    reference_terms = []
    for system, term in context["primary_reference_terms"].items():
        value = term["relative_ratio_1s"] * 100 if term["available"] else None
        reference_terms.append(f"{system} ({term['standard']}): {fmt('uncertainty', 'primary_reference_terms.' + system, value, '% (relative 1s)', '.2g')}")
    physical = "; ".join(f"lambda{mass} 1s = {uncertainty['lambda_' + mass + '_1s']:.8g} yr^-1" for mass in ("238", "235", "232"))
    sentence("uncertainty", "All ±2s values are absolute 2s unless the unit is %. Spot analytical 2s are two standard errors of the mean. "
        + "Analytical 2s includes counting statistics, "
        + ("propagated baseline-mean uncertainty, " if uncertainty["propagate_baseline"] else "")
        + "within-analysis dispersion, and applicable common-Pb composition uncertainty. "
        + "The calibration-inclusive branch additionally includes session-calibration residual scatter. "
        + "Downhole-fit uncertainty, fitted-coefficient covariance and cross-ratio session covariance are not propagated. "
        + "±2s-sys adds in quadrature to the calibration-inclusive 2s twice each 1s date-space term for the primary reference value, decay constants, "
        + "238U/235U (207Pb/235U and 207Pb/206Pb), and long-term reproducibility. "
        + "Primary reference-value terms in ratio space are " + "; ".join(reference_terms) + ". "
        + "Reference terms with no recorded uncertainty are omitted, not estimated. "
        + f"Physical uncertainty inputs: {physical}; 238U/235U 1s = {uncertainty['u238_u235_1s']:.8g}. "
        + f"Long-term term: {context['long_term_relative_1s'] * 100:.2g}% (relative ratio 1s; mode {uncertainty['long_term_reproducibility_mode']}). "
        + "Ratio-space terms are mapped to dates with the local ratio–date derivative. Systematic terms are included in ±2s-sys regardless of the selected-systematic switches. "
        + "Weighted means report analytical 2s and add shared systematic terms once to the mean.")
    sentence("constants", f"Decay constants are lambda238 = {lambda_238:.8g} yr^-1 and lambda235 = {lambda_235:.8g} yr^-1 "
        + "(Jaffey et al. 1971, doi:10.1103/PhysRevC.4.1889), "
        + f"and lambda232 = {lambda_232:.8g} yr^-1 (Le Roux and Glendenin 1963). "
        + f"238U/235U = {uncertainty['u238_u235']:.8g} ± {2 * uncertainty['u238_u235_1s']:.8g} (2s; "
        + "Hiess et al. 2012, doi:10.1126/science.1215507).")
    other = "207Pb/235U" if settings.discordance_definition == "7/5-6/8" else "207Pb/206Pb"
    sentence("discordance", f"Discordance ({settings.discordance_definition}) = 100 × (1 − t206Pb/238U / t{other}); "
        + f"discordance and its analytical 2s are in percentage points. The discordant note means |disc| − 2s > {settings.discordance_threshold_percent:g}%. "
        + f"Uncertainty propagation uses {context['discordance_covariance']}.")
    decision = context["decision"]
    preferred = (f"The preferred date uses the {decision['common_pb_method']}-method common-Pb-corrected 206Pb/238U date. "
                 if decision["common_pb_method"] in {"207", "208"} else
                 f"The preferred date uses {'204Pb-corrected ' if decision['common_pb_method'] == '204' else ''}206Pb/238U below {decision['age_transition_ma']:g} Ma and 207Pb/206Pb at or above that transition. ")
    sentence("preferred", preferred + f"The configured age transition is {decision['age_transition_ma']:g} Ma. "
        + "Sample weighted means use inverse analytical variance and included analyses only; they are reported only when all included analyses share one preferred-date method and the MSWD probability is at least 0.001. Otherwise the sample is labelled dispersed or mixed methods and no weighted mean is reported.")

    pb = context["total_pb"]
    compositions = []
    for name, abundances in pb.get("glass_mass_abundances", {}).items():
        row = pipeline.standard_archive.loc[name]
        values = ", ".join(f"{mass}Pb/204Pb_i = {row[str(mass) + 'Pb/204Pb_i']:.4g}" for mass in (206, 207, 208))
        records = measurements.loc[measurements.standard.eq(name) & measurements.role.eq("initial_ratio")]
        compositions.append(f"{name}: {values}; isotope mass abundances "
                            + ", ".join(f"{isotope} {value:.4g}" for isotope, value in abundances.items())
                            + f" ({citations('total_pb', 'glass.' + name, records)})")
    composition_text = "; ".join(compositions) or fmt("total_pb", "glass_mass_abundances", None)
    contributing = len(table.rows) - len(pb["omitted_204_spots"])
    sentence("total_pb", "Total Pb is the sum of calibrated 204Pb, 206Pb, 207Pb and 208Pb mass fractions. "
        + "Each isotope is calibrated from internally normalized sample/glass cps against glass Pb concentration times that isotope's mass abundance. "
        + f"Glass compositions used: {composition_text}. Mass 204 is Hg-corrected where possible; unmeasured or undetected 204 contributes zero. "
        + f"204Pb contributed to {contributing} of {len(table.rows)} analyses. Pb (208), when shown, is the separate 208Pb-based elemental estimate.")
    sentence("pb204", "206Pb/204Pb uses Hg-corrected mass 204 and is blank when 204Pb is not detected; its uncertainty is absolute ±1s.")
    sentence("chondrite", "Chondrite-normalized values use Sun and McDonough (1989), doi:10.1144/GSL.SP.1989.042.01.19. "
        + "CI chondrite concentrations (µg/g) are " + ", ".join(f"{el} {value:g}" for el, value in CHONDRITE_REE.items())
        + ". Subscript _N denotes chondrite normalization. Ratios are mass ratios of µg/g values unless subscript N; ΣREE sums La to Lu, excluding Pm and Y. U_i and Th_i restore radioactive decay using the preferred date.")
    sentence("ti_in_zircon", f"Ti-in-zircon temperatures use Ferry and Watson (2007), doi:10.1007/s00410-007-0201-0, "
        + f"with assumed aSiO2 = {settings.a_sio2:g} and aTiO2 = {settings.a_tio2:g}.")
    for key, citation in {
        "grimes2015": "Grimes et al. (2015), doi:10.1007/s00410-015-1199-3",
        "itano24": '"Itano \'24" as labelled in UPbR26plus; citation not verified',
        "lu_pizarro": "Lu et al. (2016, SEG Spec. Publ. 19, 329-347) and Pizarro et al. (2020), doi:10.1016/j.oregeorev.2020.103771",
        "roberts2024": "Roberts et al. (2024), doi:10.1016/j.epsl.2024.118638",
        "loucks2024": "Loucks et al. (2024), doi:10.5382/econgeo.5071",
    }.items():
        sentence(key, citation + ".")
    sentence("software", f"Reduced with UPbRPy {fmt('software', 'upbrpy_version', manifest.get('upbrpy_version'))}, "
        + f"git commit {fmt('software', 'git_commit', (_git_commit() or '')[:12])}; reference library version "
        + f"{fmt('software', 'reference_library_version', manifest.get('reference_library_version') or (manifest.get('reference_library_sha256') or '')[:12])}. "
        + f"Data-reduction date: {date.today().isoformat()}.")
    vocabulary = {
        "excluded": "Excluded analyses remain in the table and are omitted from weighted means.",
        "discordant": "Discordant means |disc| − 2s exceeds the configured discordance threshold.",
        "204Pb detected": "204Pb detected denotes flag_detectable_204pb, a detectable mass-204 signal after Hg correction where available.",
        "reverse fractionation": "Reverse fractionation denotes a flagged reverse downhole fractionation trend.",
        "high common Pb": "High common Pb denotes a common-Pb fraction above the configured screening limit.",
        "burn-through": "Burn-through denotes primary-signal relative standard deviation above the configured screening limit.",
        "high ratio scatter": "High ratio scatter denotes within-analysis ratio scatter above the configured screening limit.",
        "U mean/intercept mismatch": "U mean/intercept mismatch denotes disagreement between mean and intercept U estimates above the configured limit.",
        "high uncertainty": "High uncertainty denotes date uncertainty above the configured screening limit.",
        "session extrapolation": "Session extrapolation denotes an analysis outside the reference-material time range.",
        "concentration reference glass": "Concentration reference glass identifies a glass used for concentration calibration.",
    }
    terms = {word for value in table.rows.notes for word in str(value).split(", ") if word}
    sentence("notes", " ".join(text for term, text in vocabulary.items() if term in terms))
    sentence("glasses", "U-Pb ratios and dates are not reported for reference glasses.")
    global_ids = ("session", "method", "standards", "corrections", "uncertainty", "constants", "software")
    ordered = dict.fromkeys((*global_ids, *(key for column in table.visible_columns() for key in column.footnotes)))
    return [Footnote(key, i, notes[key], tuple(dict.fromkeys(missing.get(key, ()))))
            for i, key in enumerate(ordered, 1)]


def column_heading(column: ExportColumn, footnotes: Iterable[Footnote], style: str = "plain") -> str | list[tuple[str, bool]]:
    """Append resolved markers; rich segments superscript isotope masses too.

    Subscript tokens ``_N`` and ``_i`` are preserved for the future writer.
    A missing referenced ID raises KeyError; unsupported styles raise ValueError.
    """
    if style not in {"plain", "markdown", "rich"}:
        raise ValueError("style must be plain, markdown, or rich")
    numbers = {note.id: note.number for note in footnotes}
    markers = sorted({numbers[key] for key in column.footnotes})
    heading = column.label + (f" ({column.unit})" if column.unit else "")
    marker = ",".join(map(str, markers))
    if style == "plain":
        return heading + (f" [{marker}]" if marker else "")
    if style == "markdown":
        return escape(heading) + (f"<sup>{marker}</sup>" if marker else "")
    segments = []
    end = 0
    for match in re.finditer(r"(?<![\w.])\d{2,3}(?=[A-Z][a-z]?)", heading):
        if match.start() > end:
            segments.append((heading[end:match.start()], False))
        segments.append((match.group(), True))
        end = match.end()
    if end < len(heading):
        segments.append((heading[end:], False))
    if marker:
        segments.append((marker, True))
    return segments


def footnotes_text(footnotes: Iterable[Footnote]) -> str:
    """Render one numbered paragraph per footnote."""
    return "\n\n".join(f"[{note.number}] {note.text}" for note in footnotes)


def footnotes_markdown(footnotes: Iterable[Footnote]) -> str:
    """Render footnotes as a numbered Markdown list."""
    return "\n\n".join(f"{note.number}. {escape(note.text)}" for note in footnotes)


__all__ = ["CHONDRITE_REE", "ExportSettings", "ExportColumn", "ExportTable", "Footnote",
           "build_export_table", "build_footnotes", "column_heading", "footnotes_text", "footnotes_markdown",
           "summarize_reference_materials_by_system", "secondary_summary_wide"]


def format_value(column: ExportColumn, value, *, full_precision: bool = False) -> str:
    """Render a cell using section 4.6; non-finite and missing cells are blank."""
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, str):
        return value
    if not np.isfinite(value):
        return ""
    if column.kind == "text":
        return str(value)
    if full_precision:
        return str(value)
    kind = column.kind
    if kind == "ratio_el":
        return format(value, ".3g")
    if kind == "conc":
        decimals = 2 if abs(value) < 10 else 1 if abs(value) < 100 else 0
    elif kind == "pct_2s" and column.key != "disc_2s_pct":
        decimals = 2 if abs(value) < 1 else 1
    else:
        decimals = {"date": 1, "date_2s": 1, "ratio_68": 5, "ratio_76": 5,
                    "ratio_82": 5, "ratio_75": 4, "ratio_86": 3,
                    "rho": 2, "pct": 1, "pct_2s": 1, "int": 0, "temp": 0}.get(kind, 2)
    return f"{value:.{decimals}f}"


def update_export_settings(table: ExportTable, settings: ExportSettings) -> ExportTable:
    """Apply presentation choices and derived display quantities without engine calls.

    The returned table owns its changed rows and context. Preferred dates and
    sample statistics do not depend on these settings and are retained.
    """
    updated = replace(table, rows=table.rows.copy(), settings=settings, context=dict(table.context))
    rows = updated.rows
    if settings.discordance_definition != table.settings.discordance_definition:
        other = SYSTEMS[1] if settings.discordance_definition == "7/5-6/8" else SYSTEMS[2]
        a, b = _numeric(rows, SYSTEMS[0] + "_age"), _numeric(rows, other + "_age").replace(0, np.nan)
        ea = _numeric(rows, SYSTEMS[0] + "_age_2e_no_calibration")
        eb = _numeric(rows, other + "_age_2e_no_calibration")
        rho = _numeric(rows, "err_corr_no_calibration").fillna(0).clip(-1, 1) if other == SYSTEMS[1] else 0.
        rows["disc_pct"] = 100 * (1 - a / b)
        rows["disc_2s_pct"] = 100 * np.sqrt(np.maximum(0, (ea / b)**2 + (a * eb / b**2)**2 - 2 * rho * ea * eb * a / b**3))
    if (settings.a_sio2, settings.a_tio2) != (table.settings.a_sio2, table.settings.a_tio2):
        rows["Ti_in_zircon_C"] = Ti_in_zircon_thermometry(
            _numeric(rows, "Ti").where(_numeric(rows, "Ti") > 0), settings.a_sio2, settings.a_tio2)
    discordant = (rows.disc_pct.abs() - rows.disc_2s_pct > settings.discordance_threshold_percent) & ~rows.role.eq("glass")
    def notes(text, flagged):
        words = [w for w in str(text).split(", ") if w and w != "discordant"]
        if flagged:
            words.insert(int("excluded" in words), "discordant")
        return ", ".join(words)
    rows["notes"] = [notes(text, flag) for text, flag in zip(rows.notes, discordant)]
    manifest = dict(updated.context["manifest"])
    manifest["metadata"] = {**manifest.get("metadata", {}), "export_settings": settings.to_dict()}
    updated.context["manifest"] = manifest
    return updated


def _table_footnotes(table):
    # Only library provenance and channel names are required, never reduction stages.
    from types import SimpleNamespace
    if table.context.get("legacy_audit"):
        return [Footnote("legacy", 1, "Legacy audit results: publication quantities and acquisition metadata are unavailable in this cached frame.", ())]
    source = SimpleNamespace(reference_library_path=table.context.get("reference_library_path"),
        standard_archive=pd.DataFrame.from_dict(table.context["reference_values"], orient="index"),
        channels=table.context.get("channels", ()))
    return build_footnotes(table, source)


def _display_rows(table):
    """Yield (kind, summary text, data) in publication order."""
    summaries = table.sample_summaries.set_index("sample") if not table.sample_summaries.empty else pd.DataFrame()
    previous_role = previous_sample = None
    for record in table.rows.to_dict("records"):
        role, sample = record["role"], record["sample"]
        if role != previous_role and role != "unknown":
            if previous_role in {None, "unknown"}:
                yield "section", "Reference materials", None
            yield "section", {"primary": "Primary standard", "secondary": "Secondary standards", "glass": "Reference glasses"}[role], None
        if sample != previous_sample:
            text = summaries.loc[sample, "summary_text"] if sample in summaries.index else str(sample)
            text = re.sub(r"\bnan\b", "not available", text, flags=re.I)
            yield "sample", text, None
        yield "analysis", "", record
        previous_role, previous_sample = role, sample


def export_table_preview(table: ExportTable, n_rows: int = 8) -> dict:
    """Return group/heading rows, up to n display rows, and numbered footnotes."""
    from itertools import islice
    columns, notes = table.visible_columns(), _table_footnotes(table)
    rows, kinds = [], []
    for kind, text, record in islice(_display_rows(table), max(0, n_rows)):
        rows.append([format_value(c, record.get(c.key)) for c in columns] if record is not None
                    else [text] + [""] * max(0, len(columns) - 1))
        kinds.append(kind)
    return dict(groups=[c.group for c in columns], headings=[column_heading(c, notes) for c in columns],
                rows=rows, row_kinds=kinds, footnotes=footnotes_text(notes),
                missing_fields={field: note.number for note in notes for field in note.missing_fields})


def _export_path(path, suffix):
    target = Path(path).with_suffix(suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def write_export_csv(table: ExportTable, path: str | Path) -> list[Path]:
    """Write a flat BOM-encoded CSV, sample summaries, and numbered footnotes."""
    import csv
    target = _export_path(path, ".csv")
    columns, notes = table.visible_columns(), _table_footnotes(table)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Sample", *(f"{c.group}: {column_heading(c, notes)}" for c in columns)])
        for row in table.rows.to_dict("records"):
            writer.writerow([row["sample"], *(format_value(c, row.get(c.key),
                full_precision=table.settings.csv_full_precision) for c in columns)])
    samples = target.with_name(target.stem + "_samples.csv")
    table.sample_summaries.to_csv(samples, index=False, encoding="utf-8-sig", na_rep="")
    footnote_path = target.with_name(target.stem + "_footnotes.txt")
    footnote_path.write_text(footnotes_text(notes) + "\n", encoding="utf-8-sig")
    return [target, samples, footnote_path]


def write_export_markdown(table: ExportTable, path: str | Path) -> Path:
    """Write a GitHub-flavoured table with sample rows and numbered notes."""
    target = _export_path(path, ".md")
    columns, notes = table.visible_columns(), _table_footnotes(table)
    def safe(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>").replace("\r", "")
    def line(values):
        return "| " + " | ".join(safe(x) for x in values) + " |"
    lines = [line(column_heading(c, notes, "markdown") for c in columns), line("---" for c in columns)]
    for kind, text, record in _display_rows(table):
        lines.append(line([escape(format_value(c, record.get(c.key))) for c in columns]) if record is not None
                     else line(["**" + escape(text) + "**"] + [""] * max(0, len(columns) - 1)))
    target.write_text("\n".join(lines) + "\n\n" + footnotes_markdown(notes) + "\n", encoding="utf-8")
    return target


def _excel_format(column):
    if column.kind in {"conc", "ratio_el"}:
        return "[<10]0.00;[<100]0.0;0"
    if column.kind == "pct_2s" and column.key != "disc_2s_pct":
        return "[<1]0.00;0.0"
    return {"text": "General", "date": "0.0", "date_2s": "0.0", "ratio_68": "0.00000",
            "ratio_76": "0.00000", "ratio_82": "0.00000", "ratio_75": "0.0000",
            "ratio_86": "0.000", "rho": "0.00", "pct": "0.0", "pct_2s": "0.0",
            "int": "0", "temp": "0"}.get(column.kind, "General")


def write_export_xlsx(table: ExportTable, path: str | Path) -> Path:
    """Write full-precision publication cells, rich headings and reference summaries."""
    import math
    import xlsxwriter
    from .results import publication_column_width
    target = _export_path(path, ".xlsx")
    columns, notes = table.visible_columns(), _table_footnotes(table)
    if not columns:
        raise ValueError("Select at least one populated export column")
    with xlsxwriter.Workbook(target, {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        sheet = workbook.add_worksheet("Table")
        header = workbook.add_format(dict(bold=True, bg_color="#e9eff7", text_wrap=True, valign="top"))
        title_format = workbook.add_format(dict(bold=True, font_size=13, text_wrap=True, valign="top"))
        wrapped = workbook.add_format(dict(text_wrap=True, valign="top"))
        normal = workbook.add_format(dict(bold=True))
        superscript = workbook.add_format(dict(bold=True, font_script=1))
        formats = [workbook.add_format(dict(num_format=_excel_format(c), valign="top", text_wrap=c.key == "notes")) for c in columns]
        last = len(columns) - 1
        def merged(row, start, end, text, style):
            if end > start:
                sheet.merge_range(row, start, row, end, text, style)
            else:
                sheet.write_string(row, start, str(text), style)
        session = table.context.get("session_name", "session")
        title = table.settings.table_title or f"U-Pb isotope ratios, dates and trace-element concentrations by LA-ICP-MS: {session}"
        merged(0, 0, last, title, title_format)
        sheet.set_row(0, 38)
        start = 0
        while start < len(columns):
            end = start
            while end < last and columns[end + 1].group == columns[start].group:
                end += 1
            merged(1, start, end, columns[start].group, header)
            start = end + 1
        for i, column in enumerate(columns):
            segments = column_heading(column, notes, "rich")
            if len(segments) > 1:
                args = [x for value, sup in segments for x in (superscript if sup else normal, value)]
                sheet.write_rich_string(2, i, *args, header)
            else:
                sheet.write_string(2, i, column_heading(column, notes), header)
            sheet.set_column(i, i, publication_column_width(table.rows[column.key], column_heading(column, notes)), formats[i])
        sheet.set_row(2, 64)
        analysis_end = next((i + 1 for i, c in enumerate(columns) if c.key == "spot_name"), 0)
        sheet.freeze_panes(3, analysis_end)
        row = 3
        ratio_formats = {}
        for kind, text, record in _display_rows(table):
            if record is None:
                merged(row, 0, last, text, header)
                sheet.set_row(row, 32 if kind == "sample" else 22)
            else:
                for i, column in enumerate(columns):
                    value = record.get(column.key)
                    if value is None or pd.isna(value) or (not isinstance(value, str) and not np.isfinite(value)):
                        sheet.write_blank(row, i, None, formats[i])
                    elif isinstance(value, str):
                        sheet.write_string(row, i, value, formats[i])
                    else:
                        style = formats[i]
                        if column.kind == "ratio_el" and value != 0:
                            # Excel's two conditional sections cannot cover all
                            # orders of magnitude; choose a display-only style.
                            exponent = math.floor(math.log10(abs(value)))
                            digits = max(0, 2 - exponent)
                            code = "0.00E+00" if exponent >= 3 or exponent < -4 else "0" + ("." + "0" * digits if digits else "")
                            if code not in ratio_formats:
                                ratio_formats[code] = workbook.add_format(dict(num_format=code, valign="top"))
                            style = ratio_formats[code]
                        sheet.write_number(row, i, value, style)
                if record.get("notes"):
                    sheet.set_row(row, 32)
            row += 1
        table_end = row - 1
        row += 2
        for note in notes:
            if last >= 1:
                sheet.write_number(row, 0, note.number, wrapped)
                merged(row, 1, last, note.text, wrapped)
            else:
                sheet.write_string(row, 0, f"[{note.number}] {note.text}", wrapped)
            sheet.set_row(row, max(30, 15 * math.ceil(len(note.text) / max(60, min(180, len(columns) * 12)))))
            row += 1
        sheet.set_landscape()
        sheet.set_paper(9)
        sheet.fit_to_pages(1, 0)
        sheet.repeat_rows(0, 2)
        sheet.print_area(0, 0, max(table_end, row - 1), last)
        reference = workbook.add_worksheet("Reference materials")
        names = dict(sample="Material", system="System", n="n", weighted_mean="Weighted mean (Ma)",
                     weighted_mean_2e="±2s (Ma)", mswd="MSWD", probability="P(χ²)", accepted_date="Accepted (Ma)",
                     offset_ma="Offset (Ma)", offset_percent="Offset (%)",
                     **{f"{p}_{m}": f"{s} {t}" for p, s in (("68", "206Pb/238U"), ("76", "207Pb/206Pb"))
                        for m, t in (("weighted_mean", "weighted mean (Ma)"), ("weighted_mean_2e", "±2s (Ma)"),
                                     ("accepted_date", "accepted (Ma)"), ("offset_percent", "offset (%)"))})
        number_formats = {}
        def reference_format(key):
            code = ("0" if key == "n" else "0.00" if key in {"mswd", "probability"} or key.endswith("percent")
                    else "0.0" if key != "sample" and key != "system" else None)
            if code is None:
                return None
            if code not in number_formats:
                number_formats[code] = workbook.add_format(dict(num_format=code))
            return number_formats[code]
        offset = 0
        for label, frame in (("Per-system secondary summary (Ma)", table.secondary_summary),
                             ("206/238 and 207/206 (Ma)", secondary_summary_wide(table.secondary_summary))):
            reference.write_string(offset, 0, label, header)
            for i, key in enumerate(frame.columns):
                reference.write_string(offset + 1, i, names.get(key, str(key)), header)
                reference.set_column(i, i, publication_column_width(frame[key], names.get(key, str(key))))
            for r, values in enumerate(frame.itertuples(index=False, name=None), offset + 2):
                for i, value in enumerate(values):
                    if pd.notna(value) and (isinstance(value, str) or np.isfinite(value)):
                        style = None if isinstance(value, str) else reference_format(frame.columns[i])
                        reference.write(r, i, value, style)
            offset += len(frame) + 4
        reference.freeze_panes(2, 1)
    return target


__all__ += ["format_value", "export_table_preview", "update_export_settings",
            "write_export_xlsx", "write_export_csv", "write_export_markdown"]
