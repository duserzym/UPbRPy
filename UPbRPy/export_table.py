"""Publication-table quantities, without rendering or file-writing policy.

The analytical columns always use the no-calibration branch. ``disc_2s_pct``
is an absolute uncertainty in percentage points; the reduction's historical
``disc_2e%`` is relative to discordance and is deliberately left unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
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
    add("notes", "Analysis")
    for key in ("U", "Th", "Pb", "204Pb", "206Pb", "207Pb", "208Pb", "Pb_208"):
        add(key, "Composition", "µg/g", "conc", key not in {"U", "Th", "Pb"}, ("total_pb",) if "Pb" in key else ())
    add("Th/U", "Composition", kind="ratio_el")
    add("206Pb_cps", "Composition", "cps", "int")
    add("206Pb/204Pb", "Composition", kind="int", footnotes=("total_pb",))
    add("206Pb/204Pb_1s", "Composition", kind="int", label="206Pb/204Pb ±1s")
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
        add(base + "_2e_sys", "Dates", "Ma", "date_2s", footnotes=("sys",), label=system + " date ±2s-sys")
    add("disc_pct", "Dates", "%", "pct", footnotes=("discordance",), label="Discordance")
    add("disc_2s_pct", "Dates", "%", "pct_2s", label="Discordance ±2s")
    for method in ("207", "208"):
        base = method + "Pb_corrected_age"
        for suffix, label in (("", "date"), ("_2e_no_calibration", "date ±2s"), ("_2e_sys", "date ±2s-sys")):
            add(base + suffix, "Common-Pb corrected", "Ma", "date" if not suffix else "date_2s", footnotes=("corrections", "sys" if suffix == "_2e_sys" else "uncertainty"), label=f"{method}-method 206Pb*/238U* {label}")
    for key, label, kind in (("preferred_date", "Preferred date", "date"), ("preferred_date_2e", "Preferred date ±2s", "date_2s"), ("preferred_date_2e_sys", "Preferred date ±2s-sys", "date_2s"), ("preferred_date_method", "Method", "text")):
        add(key, "Preferred date", "" if kind == "text" else "Ma", kind, footnotes=("preferred", "sys" if key.endswith("sys") else "uncertainty"), label=label)
    for element in elements:
        # The spreadsheet repeats U and Th in this block. Distinct keys allow
        # independent visibility while keeping one authoritative source value.
        key = element + "_conc_ppm" if element in {"U", "Th"} else element
        add(key, "Concentrations", "µg/g", "conc", label=element)
        add(element + "_95CI", "Concentrations", "µg/g", "conc", True, ("uncertainty",), label=element + " 95% CI")
    for element in CHONDRITE_REE:
        add(element + "_N", "Chondrite-normalized", kind="conc", footnotes=("chondrite",))
    zircon = ("zircon",)
    add("Ti_in_zircon_C", "Ti-in-zircon", "°C", "temp", footnotes=("ti_in_zircon",), materials=zircon, label="T")
    groups = {
        "Trace-element ratios": ("Ce/Ce*", "Eu/Eu*", "sum_REE", "(Gd/Yb)_N", "Lu/Hf", "Nb/Ta", "Nb/U", "Nb/Th", "Th/Y", "Hf/Y"),
        "Grimes et al. (2015)": ("Nb/Yb", "Ce/Yb", "Gd/Yb", "U/Yb"),
        "Itano '24": ("Nb/P", "Ce/P", "sum_REE_Y"),
        "Lu / Pizarro": ("Dy/Yb", "10000Eu/Eu*/Y", "(Ce/Nd)/Y"),
        "Roberts et al. (2024)": ("Ce/U",),
        "Loucks et al. (2024)": ("U_i", "Th_i", "10000Eu/Eu*/Yb_N", "1000(Ce/Nd)_N/Y"),
    }
    for (group, keys), citation in zip(groups.items(), ("chondrite", "grimes2015", "itano24", "lu_pizarro", "roberts2024", "loucks2024")):
        for key in keys:
            mass = key in {"sum_REE", "sum_REE_Y", "U_i", "Th_i"}
            add(key, group, "µg/g" if mass else "", "conc" if mass else "ratio_el", footnotes=(citation,), materials=zircon,
                label={"sum_REE": "ΣREE", "sum_REE_Y": "ΣREE+Y"}.get(key))
    return tuple(columns)


def _sample_summaries(rows, components, secondary, uncertainty, reference, long_term):
    records = []
    for sample, group in rows.groupby("sample", sort=False):
        included = group.loc[~group.excluded]
        methods = included.preferred_date_method.dropna().unique()
        stats = weighted_mean_and_mswd(included.preferred_date, included.preferred_date_2e)
        k = stats["n"]
        status = ("mixed methods" if len(methods) > 1 else
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
        else:
            text = f"{title}; {status}: MSWD = {stats['mswd']:.2f}, n = {k}; no weighted mean"
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
    rows["excluded"] = rows.spot_name.isin(excluded)
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
    context = dict(material=config.material, primary_standards=dict(zip(SYSTEMS, (config.primary_68_standard, config.primary_68_standard, config.primary_76_standard, config.primary_82_standard))),
        secondary_standards=config.secondary_standards, concentration_standards=config.concentration_standards,
        decision=asdict(finalized.decision), uncertainty_settings=config.uncertainty.to_dict(),
        primary_reference_terms=reference, long_term_relative_1s=long_term,
        systematic_components={base: {key: dict(zip(frame.spot_name, values.tolist())) for key, values in parts.items()} for base, parts in components.items()},
        total_pb=pb_context, discordance_covariance="analytical 68/75 rho; independent 68/76 dates",
        experiment_metadata=dict(pipeline.experiment.attrs), selections=pipeline.experiment.to_dict("records"),
        reference_values=pipeline.standard_archive.to_dict("index"),
        manifest=pipeline.manifest(metadata={"export_settings": settings.to_dict()}))
    return ExportTable(rows, columns, summaries, secondary, settings, context)


__all__ = ["CHONDRITE_REE", "ExportSettings", "ExportColumn", "ExportTable",
           "build_export_table", "summarize_reference_materials_by_system", "secondary_summary_wide"]
