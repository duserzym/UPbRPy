"""Stand-alone supplementary-information workbooks over prepared reductions.

The writer reads cached publication quantities and provenance only. Combining
sessions never combines their systematic uncertainty budgets.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .export_table import (
    ExportColumn, ExportSettings, ExportTable, Footnote, SYSTEMS, _excel_format,
    _export_path, _numeric, _reference_records, _sample_summaries,
    build_export_table, build_footnotes, column_heading, high_204_note,
    session_display_name,
)
from .results import weighted_mean_and_mswd, publication_column_width


SECTION_ORDER = (
    "Laboratory and sample preparation", "Laser ablation system", "ICP-MS",
    "Acquisition", "Data processing", "Reference materials",
    "Decay constants and isotope ratios", "Uncertainty propagation",
    "Dates, discordance and flags", "Trace elements",
)
_GLOBAL = {"session", "method", "standards", "corrections", "uncertainty", "constants", "software"}
_SHEETS = (
    ("README", "Citation, provenance, sessions, samples and conventions"),
    ("Table S1 Methods", "Instrumental methods, reference values and uncertainty propagation"),
    ("Table S2 Sample data", "Listed samples, including excluded analyses"),
    ("Table S3 Standard data", "All reference-material and concentration-glass analyses"),
    ("Table S4 QC", "Per-session analytical weighted means against accepted dates"),
)


def _strings(obj, *names):
    for name in names:
        if not isinstance(getattr(obj, name), str):
            raise ValueError(f"{name} must be a string")


def _sequence(obj, name, kind):
    value = getattr(obj, name)
    if not isinstance(value, (tuple, list)) or any(not isinstance(x, kind) for x in value):
        raise ValueError(f"{name} must be a sequence of {kind.__name__}")
    object.__setattr__(obj, name, tuple(value))


@dataclass(frozen=True)
class MethodsEntry:
    """A supplied methods value with an explicit source."""

    section: str
    parameter: str
    value: str
    source: str = ""

    def __post_init__(self):
        _strings(self, "section", "parameter", "value", "source")
        if not self.section.strip() or not self.parameter.strip():
            raise ValueError("section and parameter must be nonempty")


@dataclass(frozen=True)
class SampleEntry:
    """A workbook display name, reduction aliases and sample-level notes."""

    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    note: str = ""

    def __post_init__(self):
        _strings(self, "name", "description", "note")
        _sequence(self, "aliases", str)
        if not self.name.strip() or any(not x.strip() for x in self.aliases):
            raise ValueError("sample names and aliases must be nonempty")


@dataclass(frozen=True)
class SupplementInfo:
    """Workbook metadata and optional sample selection."""

    title: str = ""
    citation: str = ""
    samples: tuple[SampleEntry, ...] = ()
    methods: tuple[MethodsEntry, ...] = ()
    notes: tuple[str, ...] = ()
    paper: str = "letter"
    export_date: date | None = None

    def __post_init__(self):
        _strings(self, "title", "citation", "paper")
        for name, kind in (("samples", SampleEntry), ("methods", MethodsEntry), ("notes", str)):
            _sequence(self, name, kind)
        if self.paper not in {"letter", "a4"}:
            raise ValueError("paper must be letter or a4")
        if self.export_date is not None and type(self.export_date) is not date:
            raise ValueError("export_date must be a date or None")
        aliases = {}
        for sample in self.samples:
            for name in (sample.name, *sample.aliases):
                key = name.casefold()
                if key in aliases and aliases[key] is not sample:
                    raise ValueError(f"Ambiguous sample name or alias: {name}")
                aliases[key] = sample


@dataclass(frozen=True)
class SupplementSession:
    """A prepared publication table and its original provenance notes."""

    table: ExportTable
    footnotes: tuple[Footnote, ...]
    label: str = ""

    def __post_init__(self):
        if not isinstance(self.table, ExportTable):
            raise ValueError("table must be an ExportTable")
        _sequence(self, "footnotes", Footnote)
        _strings(self, "label")
        if not self.label:
            object.__setattr__(self, "label", session_display_name(self.table.context))


def supplement_session(pipeline, settings=None, *, excluded_spots=(), label="") -> SupplementSession:
    """Prepare a session using the publication quantities and footnotes."""
    table = build_export_table(pipeline, settings, excluded_spots=excluded_spots)
    return SupplementSession(table, tuple(build_footnotes(table, pipeline)), label)


def supplement_info_from_settings(settings: ExportSettings) -> SupplementInfo:
    """Map recorded export metadata to workbook citation and method entries."""
    if not isinstance(settings, ExportSettings):
        raise ValueError("settings must be ExportSettings")
    fields = (
        (0, "laboratory", "Laboratory"), (1, "laser_system", "Laser system"),
        (1, "spot_diameter_um", "Spot diameter (µm)"),
        (1, "repetition_rate_hz", "Repetition rate (Hz)"),
        (1, "fluence_j_cm2", "Fluence (J/cm²)"), (2, "operator", "Operator"),
    )
    return SupplementInfo(title=settings.table_title, citation=settings.citation,
        methods=tuple(MethodsEntry(SECTION_ORDER[i], label, getattr(settings, key), "export settings")
                      for i, key, label in fields if getattr(settings, key).strip()))


def _text(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return "not recorded"
    if isinstance(value, (float, int, np.number)):
        return f"{value:.8g}" if np.isfinite(value) else "not recorded"
    return str(value)


def _notes(session):
    return {n.id: n.text for n in session.footnotes}


def _acquisition(session):
    qtegra = session.table.context.get("experiment_metadata", {}).get("qtegra", {})
    return qtegra, qtegra.get("acquisition_summary", {})


def _starts(session):
    _, acquisition = _acquisition(session)
    first = acquisition.get("first_start") or acquisition.get("first_start_date")
    last = acquisition.get("last_start") or acquisition.get("last_start_date")
    if not (first and last):
        return "not recorded"
    first, last = (str(x).replace("T", " ")[:16] for x in (first, last))
    if first[:10] == last[:10] and len(last) > 11:
        last = last[11:]
    return f"{first} to {last} (instrument clock)"


def _library(session):
    from .standards import reference_library_info
    try:
        record = reference_library_info(session.table.context.get("reference_library_path"))
    except (OSError, ValueError, KeyError):
        record = {}
    manifest = session.table.context.get("manifest", {})
    version = manifest.get("reference_library_version") or record.get("version")
    return "; ".join([_text(version), *(f"{key}: {record[key]}" for key in
                     ("repository", "revision", "commit_date") if record.get(key))])


def _selected(sessions, info):
    samples = list(info.samples)
    if not samples:
        seen = set()
        for session in sessions:
            rows = session.table.rows
            unknowns = rows.loc[rows.role.eq("unknown")]
            if "run_order" in unknowns:
                unknowns = unknowns.sort_values("run_order", kind="stable")
            for name in unknowns["sample"]:
                if str(name).casefold() not in seen:
                    samples.append(SampleEntry(str(name)))
                    seen.add(str(name).casefold())
    lookup = {name.casefold(): sample.name for sample in samples for name in (sample.name, *sample.aliases)}
    frames, omitted = [], 0
    for i, session in enumerate(sessions):
        rows = session.table.rows.copy()
        if "run_order" not in rows:
            rows["run_order"] = np.arange(len(rows))
        unknown = rows.role.eq("unknown")
        display = rows["sample"].astype(str).str.casefold().map(lookup)
        omitted += int((unknown & display.isna()).sum())
        rows = rows.loc[~unknown | display.notna()].copy()
        rows.loc[rows.role.eq("unknown"), "sample"] = display.loc[rows.index]
        rows["_session"] = i
        frames.append(rows)
    return samples, pd.concat(frames, ignore_index=True), omitted


def _merged_notes(sessions, columns):
    ids = dict.fromkeys(key for c in columns for key in c.footnotes if key not in _GLOBAL)
    result = []
    for key in ids:
        entries = [(s.label, _notes(s).get(key, "not recorded")) for s in sessions]
        texts = list(dict.fromkeys(text for _, text in entries))
        text = texts[0] if len(texts) == 1 else " ".join(f"{label}: {text}" for label, text in entries)
        result.append(Footnote(key, len(result) + 1, text, ()))
    return result


def _single_summary(session, rows):
    """Reuse the sample rule with aliases pooled in a single session."""
    from .uncertainty import UncertaintySettings
    context = session.table.context
    parts = pd.DataFrame(index=rows.index, columns=("session_1s",), dtype=float)
    for method in rows.preferred_date_method.dropna().unique():
        base = (str(method)[:3] + "Pb_corrected_age" if method in {"207Pb-corrected", "208Pb-corrected"}
                else ("204Pb_corrected_" if str(method).startswith("204Pb-corrected ") else "")
                + str(method).split()[-1] + "_age")
        mask = rows.preferred_date_method.eq(method)
        values = context.get("systematic_components", {}).get(base, {}).get("session_1s", {})
        parts.loc[mask, "session_1s"] = rows.loc[mask, "spot_name"].map(values)
    if not context.get("primary_reference_terms"):
        return None
    summary = _sample_summaries(rows, parts, session.table.secondary_summary,
        UncertaintySettings(**context["uncertainty_settings"]),
        context["primary_reference_terms"], context["long_term_relative_1s"])
    return summary.iloc[0]


def _sample_title(sample, rows, sessions):
    n_sessions = rows._session.nunique()
    title = (f"{sample.name} (n = {len(rows)} analyses in {n_sessions} session{'s' if n_sessions != 1 else ''}, "
             f"{int(rows.excluded.sum())} excluded)")
    included = rows.loc[~rows.excluded]
    if "preferred_date" not in rows:
        return title + "; preferred dates not recorded"
    methods = included.preferred_date_method.dropna().unique()
    stats = weighted_mean_and_mswd(included.preferred_date, included.preferred_date_2e)
    if len(methods) > 1:
        counts = included.preferred_date_method.value_counts()
        clause = "mixed methods (" + ", ".join(f"{m} {n}" for m, n in counts.items()) + "); no weighted mean"
    elif len(methods) == 1 and stats["probability"] >= .001:
        clause = f"weighted mean {methods[0]} date = {stats['weighted_mean']:.1f} ± {stats['weighted_mean_2e']:.1f} Ma (analytical 2s; MSWD = {stats['mswd']:.2f}, n = {stats['n']})"
        if n_sessions == 1:
            summary = _single_summary(sessions[int(rows._session.iloc[0])], rows)
            if summary is not None and np.isfinite(summary["2s_sys"]):
                clause += f"; ±2s-sys = {summary['2s_sys']:.1f} Ma"
    else:
        mswd = f"{stats['mswd']:.2f}" if np.isfinite(stats["mswd"]) else "not available"
        clause = f"dispersed: MSWD = {mswd}, n = {stats['n']}; no weighted mean"
    if n_sessions > 1:
        clause += "; ±2s-sys not combined across sessions"
    return title + "; " + clause


def _recorded_methods(session):
    """Return section, parameter, value and provenance for recorded methods."""
    table, notes = session.table, _notes(session)
    context = table.context
    manifest = context.get("manifest", {})
    config = manifest.get("config", {})
    qtegra, acquisition = _acquisition(session)
    rows = []
    def add(section, parameter, value, source="reduction record"):
        rows.append((SECTION_ORDER[section], parameter, _text(value), source))
    for section, key, label in ((0, "laboratory", "Laboratory"),
            (1, "laser_system", "Laser system"), (1, "spot_diameter_um", "Spot diameter (µm)"),
            (1, "repetition_rate_hz", "Repetition rate (Hz)"), (1, "fluence_j_cm2", "Fluence (J/cm²)")):
        add(section, label, getattr(table.settings, key), "export settings")
    add(0, "Sample preparation", None)
    add(1, "Laser output settings seen (%)", ", ".join(map(str, acquisition.get("laser_output_settings_percent", []))))
    add(1, "Tune names", ", ".join(acquisition.get("tune_names", [])))
    add(2, "Instrument", acquisition.get("machine") or qtegra.get("machine"))
    # Instrument logins are personal identifiers: an operator is written only
    # when supplied in the export settings, never from the Qtegra user field.
    if table.settings.operator.strip():
        add(2, "Operator", table.settings.operator, "export settings")
    add(3, "Acquisition starts", _starts(session))
    add(3, "Analyses (n)", len(table.rows))
    sweeps = acquisition.get("sweeps_per_acquisition", {})
    add(3, "Sweeps per analysis", f"median {_text(sweeps.get('median'))}; range {_text(sweeps.get('min'))} to {_text(sweeps.get('max'))}")
    dwell = acquisition.get("dwell_s", {})
    grouped = {}
    for channel, value in dwell.items():
        grouped.setdefault(value, []).append(channel)
    for value, channels in sorted(grouped.items(), key=lambda x: (x[0] is None, x[0] or 0)):
        parameter = f"Dwell {float(value) * 1000:g} ms" if value is not None else "Dwell not recorded"
        add(3, parameter, ", ".join(channels), "Qtegra acquisition summary")
    if not dwell:
        add(3, "Masses measured", ", ".join(context.get("channels", ())))
        add(3, "Dwell times (ms)", None)
    measured = context.get("channels", ())
    complete = bool(dwell) and all(dwell.get(c) is not None for c in measured)
    add(3, "Total dwell per sweep (s)", sum(dwell.values()) if complete and all(v is not None for v in dwell.values()) else None)
    selections = pd.DataFrame(context.get("selections", []))
    def unique(key):
        return ", ".join(map(str, selections.get(key, pd.Series(dtype=str)).dropna().unique()))
    add(3, "Window selection method", unique("selection_method") or config.get("selection_method"))
    for region in ("baseline", "plateau"):
        durations = _numeric(selections, region + "_end_times") - _numeric(selections, region + "_start_times")
        valid = durations.loc[np.isfinite(durations) & durations.ge(0)]
        add(3, f"Median {region} duration (s)", valid.median() if len(valid) else None)
    add(3, "Plateau filter method", unique("plateau_filter_method") or "none")
    add(3, "Plateau filter channel", unique("plateau_filter_channel") or "none")
    add(4, "Software", notes.get("software"))
    add(4, "Reference library", _library(session))
    add(4, "Ratio aggregation", config.get("ratio_aggregation"))
    # Extract the existing labelled prose without regenerating model options
    # or detector factors under a second reporting policy.
    corrections = notes.get("corrections", "")
    def extract(text, start, end=None):
        value = text.partition(start)[2]
        return value.partition(end)[0] if end and value else value or None
    add(4, "Baseline subtraction", corrections.partition("Downhole model:")[0] or None)
    add(4, "Downhole model and options", extract(corrections, "Downhole model: ", ". Session model:"))
    add(4, "Session model and options", extract(corrections, "Session model: ", ". "))
    detector = manifest.get("detector_calibration", {})
    add(4, "Detector cross-calibration mode", detector.get("mode"))
    add(4, "Detector cross-calibration factors", extract(corrections, "; factors "))
    hg = ("Mass 204 is corrected for 204Hg via 202Hg where available" if "202Hg" in context.get("channels", ())
          else "204Hg correction via 202Hg unavailable: 202Hg not measured")
    add(4, "204Hg correction", hg + "; total-Pb mass-204 correction: " + _text(context.get("total_pb", {}).get("hg_correction")))
    add(4, "Common-Pb handling", extract(corrections, "Common-Pb method dates reported: ", ". "))
    initial = extract(corrections, "Initial Pb ", "Preferred-date common-Pb method:")
    add(4, "Initial-Pb model and options", "Initial Pb " + initial if initial else None)
    add(4, "Preferred-date common-Pb method", context.get("decision", {}).get("common_pb_method"))
    if context.get("legacy_audit"):
        add(4, "Publication quantities", notes.get("legacy"))
    add(6, "Decay constants and 238U/235U", notes.get("constants"))
    uncertainty = context.get("uncertainty_settings", {})
    for mass in ("238", "235", "232"):
        add(6, f"lambda{mass} uncertainty (1s, yr^-1)", uncertainty.get(f"lambda_{mass}_1s"))
    add(6, "238U/235U uncertainty (1s)", uncertainty.get("u238_u235_1s"))
    propagation = notes.get("uncertainty", "")
    add(7, "Analytical branch", propagation.partition("The calibration-inclusive branch")[0] or None)
    calibration = extract(propagation, "The calibration-inclusive branch", "±2s-sys")
    add(7, "Calibration branch and propagation limits", "The calibration-inclusive branch" + calibration if calibration else None)
    systematic = extract(propagation, "±2s-sys", "Primary reference-value terms")
    add(7, "±2s-sys definition", "±2s-sys" + systematic if systematic else None)
    for system, term in context.get("primary_reference_terms", {}).items():
        value = term.get("relative_ratio_1s") if term.get("available") else None
        add(7, f"Primary reference-value term: {system} ({term.get('standard', 'primary')}; relative 1s, %)",
            f"{100 * value:.3g}" if value is not None and np.isfinite(value) else None)
    long_term = context.get("long_term_relative_1s")
    add(7, "Long-term term (relative ratio 1s, %)",
        f"{100 * long_term:.3g}" if long_term is not None and np.isfinite(long_term) else None)
    add(7, "Long-term mode", uncertainty.get("long_term_reproducibility_mode"))
    add(7, "Unrecorded reference uncertainties", "Reference terms with no recorded uncertainty are omitted, not estimated." if propagation else None)
    conversion = extract(propagation, "Ratio-space terms")
    add(7, "Date-space conversion and shared terms", "Ratio-space terms" + conversion if conversion else None)
    decision = context.get("decision", {})
    method, transition = decision.get("common_pb_method"), _text(decision.get("age_transition_ma"))
    rule = (f"{method}-method common-Pb-corrected 206Pb/238U; configured transition {transition} Ma"
            if method in {"207", "208"} else f"{'204Pb-corrected ' if method == '204' else ''}206Pb/238U below {transition} Ma, 207Pb/206Pb at or above")
    add(8, "Preferred-date rule", rule)
    other = "207Pb/235U" if table.settings.discordance_definition == "7/5-6/8" else "207Pb/206Pb"
    add(8, "Discordance definition and threshold", notes.get("discordance") or
        f"Discordance ({table.settings.discordance_definition}) = 100 × (1 − t206Pb/238U / t{other}); discordant when |disc| − 2s > {table.settings.discordance_threshold_percent:g}%.")
    words = sorted({word for text in table.rows.get("notes", ()) for word in str(text).split(", ") if word}, key=str.casefold)
    add(8, "Flag vocabulary used", ", ".join(words) or "none")
    add(8, "Flag definitions", notes.get("notes") or "none")
    add(8, "High-204 threshold", high_204_note(context.get("high_204", {})))
    add(8, "High-204 threshold (cps)", context.get("high_204", {}).get("threshold_cps"))
    add(9, "Concentration glasses", ", ".join(context.get("active_concentration_standards", ())))
    present = {key for column in table.visible_columns() for key in column.footnotes}
    for key, label in (("total_pb", "Total Pb"), ("chondrite", "Chondrite values and normalization"),
            ("ti_in_zircon", "Ti-in-zircon activities"), ("grimes2015", "Grimes et al. (2015)"),
            ("itano24", "Itano '24"), ("lu_pizarro", "Lu and Pizarro"),
            ("roberts2024", "Roberts et al. (2024)"), ("loucks2024", "Loucks et al. (2024)")):
        if key in present:
            add(9, label, notes.get(key))
    return rows


def _reference_rows(session):
    """Use matching library records for ratios, date uncertainties and citations."""
    from .bias import accepted_dates
    from .standards import load_standard_measurements, build_standard_archive
    context = session.table.context
    archive = pd.DataFrame.from_dict(context.get("reference_values", {}), orient="index")
    try:
        measurements = load_standard_measurements(context.get("reference_library_path"))
    except (OSError, ValueError, KeyError):
        measurements = pd.DataFrame(columns=("standard", "quantity", "role", "value", "source", "uncertainty", "uncertainty_type"))
    registry = measurements.attrs.get("sources", {})
    library = build_standard_archive(measurements) if not measurements.empty else pd.DataFrame()
    primary = context.get("primary_standards", {})
    names = tuple(dict.fromkeys((*primary.values(), *context.get("secondary_standards", ()),
                                *session.table.rows.loc[session.table.rows.role.eq("glass"), "sample"])))
    dates = accepted_dates(archive, names, systems=SYSTEMS) if not archive.empty else pd.DataFrame()
    def cite(records):
        texts = []
        for display in records.source.dropna().unique():
            matches = [entry for entry in registry.values() if entry["display"] == display]
            doi = matches[0].get("doi") if len(matches) == 1 else None
            texts.append(str(display) + (f", doi:{doi}" if doi and doi not in display else "" if doi else ", no DOI"))
        return "; ".join(texts) or "not recorded"
    result = []
    for name in names:
        row = archive.loc[name] if name in archive.index else pd.Series(dtype=float)
        glass = "glass" in str(row.get("material", "")).lower() or session.table.rows.loc[session.table.rows["sample"].eq(name), "role"].eq("glass").any()
        primary_systems = [s for s in SYSTEMS if primary.get(s) == name]
        role = "concentration glass" if glass else "primary for " + ", ".join(primary_systems) if primary_systems else "secondary"
        if glass:
            records = measurements.loc[measurements.standard.eq(name) & measurements.role.eq("concentration")]
            result.append((name, role, "concentrations", None, None, None, cite(records)))
            continue
        for system in SYSTEMS:
            ratio = row.get(system + "*", np.nan)
            records = _reference_records(measurements, archive, name, system)
            if name not in library.index or not np.isclose(ratio, library.loc[name].get(system + "*", np.nan), rtol=1e-10, atol=0):
                records = measurements.iloc[:0]
            ages = measurements.loc[measurements.standard.eq(name) & measurements.role.eq("reference_age") & measurements.quantity.eq(system + "_age")]
            ages = ages.loc[np.isclose(ages.value, row.get(system + "_age", np.nan), rtol=1e-10, atol=0)]
            citation = cite(pd.concat([records, ages]).drop_duplicates())
            if records.empty and not ages.empty:
                citation = "accepted-ratio citation not recorded; reference-date citation " + citation
            accepted = dates.loc[name, system] if name in dates.index else np.nan
            # Do not transfer a concordia-age error to a distinct system, or
            # reuse a reference-age error if accepted_dates used another value.
            candidates = measurements.loc[measurements.standard.eq(name) & measurements.quantity.eq(system + "_age")]
            candidates = candidates.loc[np.isclose(candidates.value, accepted, rtol=1e-10, atol=0)]
            if candidates.empty:
                candidates = measurements.loc[measurements.standard.eq(name) & measurements.quantity.eq("concordia_age")]
                candidates = candidates.loc[np.isclose(candidates.value, accepted, rtol=1e-10, atol=0)]
            error = None
            for record in candidates.to_dict("records"):
                factor = {"1sd": 2., "2sd": 1., "expanded_95": 2 / 1.959963984540054}.get(record.get("uncertainty_type"))
                if factor and pd.notna(record.get("uncertainty")):
                    error = record["uncertainty"] * factor
                    break
            result.append((name, role, system, ratio, accepted, error, citation))
    return result


def _logins(sessions):
    """Return instrument login strings recorded in the sessions' metadata."""
    found = set()
    def walk(value, key=""):
        if isinstance(value, Mapping):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and key.casefold() in {"users", "user", "username", "user_name"} and value.strip():
            found.add(value.strip())
    for session in sessions:
        walk(session.table.context.get("experiment_metadata", {}))
        for record in session.table.context.get("selections", ()):
            walk({k: v for k, v in record.items() if str(k).casefold() in {"user", "username", "user_name"}})
    # A domain login such as DOMAIN\account also hides the bare account.
    parts = {x.rsplit("\\", 1)[-1] for x in found if "\\" in x}
    return sorted(found | {x for x in parts if len(x) >= 3}, key=len, reverse=True)


class _Workbook:
    """Small formatting helpers shared by the five SI sheets."""

    def __init__(self, workbook, info, logins=()):
        self.workbook, self.info = workbook, info
        self.logins = tuple(logins)
        self.styles = {}
        self.body = self.style(text_wrap=True, valign="top")
        self.header = self.style(bold=True, bg_color="#EAF0F6", text_wrap=True, valign="top", bottom=1, bottom_color="#D0D5DB")
        self.title = self.style(bold=True, font_size=13, text_wrap=True, valign="top")
        self.bold = self.style(bold=True)
        self.sup = self.style(bold=True, font_script=1)

    def style(self, **options):
        key = tuple(sorted(options.items()))
        if key not in self.styles:
            self.styles[key] = self.workbook.add_format({"font_name": "Calibri", "font_size": 10, **options})
        return self.styles[key]

    def clean(self, text):
        """Replace recorded instrument logins in any text written to the workbook."""
        text = str(text)
        for login in self.logins:
            text = text.replace(login, "[instrument account]")
        return text

    def _guard(self, sheet):
        # Every string path of the worksheet goes through clean(), so a login
        # in reduction prose or supplied text can never reach a cell.
        write_string, merge_range, write_rich = sheet.write_string, sheet.merge_range, sheet.write_rich_string
        sheet.write_string = lambda row, col, text, *a: write_string(row, col, self.clean(text), *a)
        sheet.merge_range = lambda r1, c1, r2, c2, data, *a: merge_range(
            r1, c1, r2, c2, self.clean(data) if isinstance(data, str) else data, *a)
        sheet.write_rich_string = lambda row, col, *a: write_rich(
            row, col, *(self.clean(x) if isinstance(x, str) else x for x in a))
        return sheet

    def sheet(self, name, title, last, *, headers=2, portrait=False, fit=True):
        sheet = self._guard(self.workbook.add_worksheet(name))
        sheet.hide_gridlines(2)
        sheet.set_default_row(15)
        self.merge(sheet, 0, 0, last, title, self.title)
        sheet.set_row(0, 36)
        sheet.freeze_panes(headers, 0)
        sheet.set_portrait() if portrait else sheet.set_landscape()
        sheet.set_paper(1 if self.info.paper == "letter" else 9)
        if fit:
            sheet.fit_to_pages(1, 0)
        else:
            # Wide data tables stay legible: fixed scale, identifiers on every page.
            sheet.set_print_scale(55)
            sheet.repeat_columns(0, 2)
        sheet.repeat_rows(0, headers - 1)
        sheet.set_margins(.25, .25, .35, .35)
        sheet.set_footer("&A — page &P of &N", {"margin": .15})
        return sheet

    def merge(self, sheet, row, start, end, text, style=None):
        if end > start:
            sheet.merge_range(row, start, row, end, text, style or self.body)
        else:
            sheet.write_string(row, start, str(text), style or self.body)

    def line(self, sheet, row, text, last, style=None):
        self.merge(sheet, row, 0, last, text, style)
        # The rows are wrapped and tall enough for prose at normal zoom.
        sheet.set_row(row, max(30, 14 * math.ceil(len(str(text)) / max(70, min(170, (last + 1) * 22)))))

    def rich(self, sheet, row, col, segments, style):
        if len(segments) > 1:
            # Header cells are bold; body cells (e.g. dwell channel lists) are not.
            plain, raised = (self.bold, self.sup) if style is self.header else (self.style(), self.style(font_script=1))
            args = [x for value, sup in segments if value for x in (raised if sup else plain, value)]
            sheet.write_rich_string(row, col, *args, style)
        else:
            sheet.write_string(row, col, "".join(v for v, _ in segments), style)

    def cell(self, sheet, row, col, value, style=None):
        if value is None or pd.isna(value):
            return
        if isinstance(value, str):
            sheet.write_string(row, col, value, style or self.body)
        elif np.isfinite(value):
            sheet.write_number(row, col, float(value), style or self.style(valign="top"))


def _write_readme(book, sessions, info, samples, rows, omitted, title):
    sheet = book.sheet("README", title, 5, portrait=True)
    sheet.set_column(0, 0, 25, book.body)
    sheet.set_column(1, 1, 28, book.body)
    sheet.set_column(2, 5, 19, book.body)
    row = 2
    for text in ("How to cite: " + _text(info.citation),
                 "Export date: " + (info.export_date or date.today()).isoformat()):
        book.line(sheet, row, text, 5)
        row += 1
    provenance = {}
    for session in sessions:
        text = (_notes(session).get("software", "Software: not recorded")
                + " Reference library: " + _library(session))
        provenance.setdefault(text, []).append(session.label)
    for text, labels in provenance.items():
        prefix = "All sessions" if len(provenance) == 1 else ", ".join(labels)
        book.line(sheet, row, f"{prefix}: {text}", 5)
        row += 1
    row += 1
    book.line(sheet, row, "Contents", 5, book.header)
    row += 1
    sheet.write_string(row, 0, "Sheet name", book.header)
    book.merge(sheet, row, 1, 5, "What it holds", book.header)
    row += 1
    for name, description in _SHEETS:
        sheet.write_string(row, 0, name, book.body)
        book.merge(sheet, row, 1, 5, description)
        sheet.set_row(row, 30)
        row += 1
    row += 1
    book.line(sheet, row, "Sessions", 5, book.header)
    row += 1
    for col, text in enumerate(("Label", "Acquisition date range (instrument clock)", "Analyses of listed samples", "Reference-material analyses", "Glass analyses", "Excluded")):
        sheet.write_string(row, col, text, book.header)
    sheet.set_row(row, 44)
    row += 1
    for i, session in enumerate(sessions):
        group = rows.loc[rows._session.eq(i)]
        values = (session.label, _starts(session), int(group.role.eq("unknown").sum()),
                  int(group.role.isin(("primary", "secondary")).sum()), int(group.role.eq("glass").sum()), int(group.excluded.sum()))
        for col, value in enumerate(values):
            book.cell(sheet, row, col, value)
        sheet.set_row(row, 44)
        row += 1
    row += 1
    book.line(sheet, row, "Samples", 5, book.header)
    row += 1
    sheet.write_string(row, 0, "Sample", book.header)
    sheet.write_string(row, 1, "Description", book.header)
    book.merge(sheet, row, 2, 3, "Analyses (n) and sessions", book.header)
    book.merge(sheet, row, 4, 5, "Note", book.header)
    row += 1
    for sample in samples:
        group = rows.loc[rows.role.eq("unknown") & rows["sample"].eq(sample.name)]
        counts = group._session.value_counts(sort=False)
        count_text = f"{len(group)}; " + "; ".join(f"{sessions[i].label}: {n}" for i, n in counts.items()) if len(group) else "0"
        note = sample.note if len(group) else "; ".join(filter(None, (sample.note, "no analyses in these sessions")))
        sheet.write_string(row, 0, sample.name, book.body)
        sheet.write_string(row, 1, sample.description, book.body)
        book.merge(sheet, row, 2, 3, count_text)
        book.merge(sheet, row, 4, 5, note)
        sheet.set_row(row, max(32, 15 * math.ceil(max(len(count_text), len(note)) / 35)))
        row += 1
    row += 1
    book.line(sheet, row, "Conventions", 5, book.header)
    row += 1
    for text in ("Dates are in Ma; concentrations are in µg/g. All uncertainties are 2s absolute unless marked %. Single-analysis results are dates, not ages. Blank cell = not measured or not applicable.",
                 "Excluded analyses remain in the data tables and are omitted from weighted means.", *info.notes,
                 f"Unknown analyses of other samples in these sessions omitted: {omitted}."):
        book.line(sheet, row, text, 5)
        row += 1
    sheet.print_area(0, 0, row - 1, 5)


def _write_methods(book, sessions, info, references):
    last = max(len(sessions) + 2, 7)
    sheet = book.sheet("Table S1 Methods", "Table S1. Methods and reference values", last)
    sheet.set_column(0, last, 22, book.body)
    sheet.set_column(7, 7, 48, book.body)
    sheet.set_column(0, 0, 28, book.body)
    sheet.set_column(1, 1, 38, book.body)
    sheet.set_column(2, len(sessions) + 1, max(28, 65 / len(sessions)), book.body)
    sheet.set_column(len(sessions) + 2, len(sessions) + 2, 28, book.body)
    headers = ("Section", "Parameter", *(s.label for s in sessions), "Source")
    for col, value in enumerate(headers):
        sheet.write_string(1, col, value, book.header)
    sheet.set_row(1, 30)
    maps = [{(section, parameter): (value, source) for section, parameter, value, source in _recorded_methods(s)} for s in sessions]
    # A supplied entry replaces the recorded row of the same section and parameter.
    supplied = {(e.section, e.parameter.casefold()) for e in info.methods}
    keys = [key for key in dict.fromkeys(key for records in maps for key in records)
            if (key[0], key[1].casefold()) not in supplied]
    sections = tuple(dict.fromkeys((*SECTION_ORDER, *(e.section for e in info.methods))))
    row = 2
    for section in sections:
        book.line(sheet, row, section, last, book.header)
        row += 1
        user = [e for e in info.methods if e.section == section]
        entries = [(e.parameter, [_text(e.value)] * len(sessions), _text(e.source)) for e in user]
        for key in keys:
            if key[0] != section:
                continue
            entries.append((key[1], [m.get(key, ("not recorded", ""))[0] for m in maps],
                            next((m[key][1] for m in maps if key in m), "reduction record")))
        for parameter, values, source in entries:
            sheet.write_string(row, 0, section, book.body)
            sheet.write_string(row, 1, parameter, book.body)
            equal = len(set(values)) == 1
            if equal:
                book.merge(sheet, row, 2, len(sessions) + 1, values[0])
            else:
                for col, value in enumerate(values, 2):
                    sheet.write_string(row, col, value, book.body)
            if parameter.startswith("Dwell ") and parameter != "Dwell times (ms)":
                for col, value in ([(2, values[0])] if equal else list(enumerate(values, 2))):
                    book.rich(sheet, row, col, column_heading(ExportColumn("", "", value), (), "rich"), book.body)
            sheet.write_string(row, len(sessions) + 2, source, book.body)
            chars = (len(sessions) * 48 if equal else max(25, 65 / len(sessions)))
            sheet.set_row(row, max(32, 15 * math.ceil(max(map(len, values)) / chars)))
            row += 1
        if section == "Reference materials":
            fields = ("Session", "Material", "Role", "System", "Accepted ratio", "Accepted date (Ma)", "Accepted ±2s (Ma)", "Citation with DOI")
            for col, text in enumerate(fields):
                sheet.write_string(row, col, text, book.header)
            sheet.set_row(row, 42)
            row += 1
            merged = {}
            for session, records in zip(sessions, references):
                for record in records:
                    key = tuple("" if v is None or (isinstance(v, float) and not np.isfinite(v)) else v for v in record)
                    merged.setdefault(key, (record, []))[1].append(session.label)
            for record, labels in merged.values():
                label = "All sessions" if len(labels) == len(sessions) else ", ".join(labels)
                for col, value in enumerate((label, *record)):
                    book.cell(sheet, row, col, value, book.body if isinstance(value, str) else book.style(num_format="0.00000" if col == 4 else "0.00"))
                sheet.set_row(row, max(45, 15 * math.ceil(len(record[-1]) / 45)))
                row += 1
    sheet.print_area(0, 0, row - 1, last)


def _write_data(book, sessions, samples, rows, *, standards=False):
    visible = {c.key for s in sessions for c in s.table.visible_columns()}
    catalogue = list(dict((c.key, c) for s in sessions for c in s.table.columns).values())
    trace_start = next((i for i, c in enumerate(catalogue) if c.group == "Concentrations"), len(catalogue))
    trace_groups = {c.group for c in catalogue[trace_start:]}
    columns = [c for c in catalogue if c.key in visible]
    # Analysis remains visible as the third identifier, even if a caller hid it.
    columns = [ExportColumn("sample", "Analysis", "Sample"), ExportColumn("_session_label", "Analysis", "Session"),
               ExportColumn("spot_name", "Analysis", "Analysis"), *(c for c in columns if c.key != "spot_name")]
    columns = [replace(c, footnotes=tuple(k for k in c.footnotes if k not in _GLOBAL)) for c in columns]
    notes = _merged_notes(sessions, columns)
    if not standards:
        for sample in samples:
            if sample.note:
                notes.append(Footnote("sample:" + sample.name, len(notes) + 1, sample.name + ": " + sample.note, ()))
    markers = {note.id: note.number for note in notes}
    last = len(columns) - 1
    name = "Table S3 Standard data" if standards else "Table S2 Sample data"
    sheet = book.sheet(name, "Table S3. Standard data" if standards else "Table S2. Sample data", last, headers=3, fit=False)
    start = 0
    while start <= last:
        end = start
        while end < last and columns[end + 1].group == columns[start].group:
            end += 1
        book.merge(sheet, 1, start, end, columns[start].group, book.header)
        start = end + 1
    for col, column in enumerate(columns):
        book.rich(sheet, 2, col, column_heading(column, notes, "rich"), book.header)
        values = rows[column.key] if column.key in rows else pd.Series(dtype=str)
        width = publication_column_width(values, column_heading(column, notes))
        if col in (0, 1):
            labels = [s.label for s in sessions] if column.key == "_session_label" else []
            width = max(20, width, *(len(x) + 2 for x in labels))
        sheet.set_column(col, col, width, book.style(num_format=_excel_format(column), valign="top", text_wrap=column.key == "notes"), {"level": int(column.group in trace_groups)})
    sheet.set_row(2, 64)
    sheet.freeze_panes(3, 3)
    row = 3
    def analyses(group):
        nonlocal row
        for record in group.sort_values(["_session", "run_order"], kind="stable").to_dict("records"):
            record["_session_label"] = sessions[record["_session"]].label
            for col, column in enumerate(columns):
                if record["role"] == "glass" and column.group not in {"Analysis", "Composition", "Concentrations"}:
                    continue
                value = record.get(column.key)
                code = _excel_format(column)
                if column.kind == "ratio_el" and value is not None and pd.notna(value) and value != 0:
                    exponent = math.floor(math.log10(abs(value)))
                    digits = max(0, 2 - exponent)
                    code = "0.00E+00" if exponent >= 3 or exponent < -4 else "0" + ("." + "0" * digits if digits else "")
                book.cell(sheet, row, col, value, book.style(num_format=code, valign="top", text_wrap=column.key == "notes"))
            if len(str(record.get("notes") or "")) > 40:
                sheet.set_row(row, 30)
            row += 1
    if standards:
        for role, label in (("primary", "Primary reference material"), ("secondary", "Secondary reference materials"), ("glass", "Concentration reference glasses")):
            book.line(sheet, row, label, last, book.header)
            row += 1
            group = rows.loc[rows.role.eq(role)]
            for material in group["sample"].drop_duplicates():
                for i, session in enumerate(sessions):
                    subset = group.loc[group["sample"].eq(material) & group._session.eq(i)]
                    if subset.empty:
                        continue
                    summaries = session.table.sample_summaries
                    summary = summaries.loc[summaries["sample"].eq(material)] if "sample" in summaries else pd.DataFrame()
                    text = summary.iloc[0].summary_text if len(summary) else str(material)
                    book.line(sheet, row, f"{session.label}: {text}", last, book.header)
                    row += 1
                    analyses(subset)
    else:
        for sample in samples:
            group = rows.loc[rows.role.eq("unknown") & rows["sample"].eq(sample.name)]
            text = _sample_title(sample, group, sessions) if len(group) else sample.name + "; no analyses in these sessions"
            if sample.note:
                text += f". Note {markers['sample:' + sample.name]}: {sample.note}"
            book.line(sheet, row, text, last, book.header)
            row += 1
            analyses(group)
    row += 2
    book.line(sheet, row, "Methods, reference values and uncertainty propagation: see Table S1", last)
    row += 1
    for note in notes:
        sheet.write_number(row, 0, note.number, book.body)
        book.merge(sheet, row, 1, last, note.text)
        sheet.set_row(row, max(30, 15 * math.ceil(len(note.text) / 160)))
        row += 1
    sheet.print_area(0, 0, row - 1, last)


def _write_qc(book, sessions, references):
    headers = ("Session", "Material", "Role", "System", "n", "Weighted mean (Ma)", "±2s (Ma)", "MSWD", "P(χ²)", "Accepted (Ma)", "Accepted ±2s (Ma)", "Offset (Ma)", "Offset (%)", "Within 2s")
    sheet = book.sheet("Table S4 QC", "Table S4. Reference-material quality control", len(headers) - 1)
    for col, text in enumerate(headers):
        sheet.write_string(1, col, text, book.header)
        width = (max(21, *(len(s.label) + 2 for s in sessions)) if col == 0
                 else 38 if col == 2 else 21 if col < 4 else 16)
        sheet.set_column(col, col, width, book.body)
    sheet.set_row(1, 42)
    sheet.freeze_panes(2, 4)
    materials = dict.fromkeys(name for s in sessions for name in s.table.secondary_summary.get("sample", ()))
    row = 2
    for material in materials:
        book.line(sheet, row, material, len(headers) - 1, book.header)
        row += 1
        for session, records in zip(sessions, references):
            errors = {(r[0], r[2]): r[5] for r in records}
            summary = session.table.secondary_summary
            subset = summary.loc[summary["sample"].eq(material)] if "sample" in summary else pd.DataFrame()
            primary = material in session.table.context.get("primary_standards", {}).values()
            role = "primary, calibration; not independent" if primary else "secondary"
            for record in subset.to_dict("records"):
                error = errors.get((material, record["system"]))
                valid = np.isfinite(record["offset_ma"]) and np.isfinite(record["weighted_mean_2e"])
                within = ("yes" if abs(record["offset_ma"]) <= math.hypot(record["weighted_mean_2e"], error or 0.) else "no") if valid else ""
                values = (session.label, material, role, record["system"], record["n"],
                    record["weighted_mean"], record["weighted_mean_2e"], record["mswd"], record["probability"],
                    record["accepted_date"], error, record["offset_ma"], record["offset_percent"], within)
                for col, value in enumerate(values):
                    code = "0" if col == 4 else "0.0000" if col == 8 else "0.00" if col in (7, 12) else "0.0"
                    book.cell(sheet, row, col, value, book.body if isinstance(value, str) else book.style(num_format=code, valign="top"))
                row += 1
    for text in ("Weighted means use analytical 2s of included analyses. Flags alone do not exclude analyses.",
            "Within 2s: |offset| ≤ sqrt((weighted-mean 2s)² + (accepted 2s)²). When accepted uncertainty is not recorded its cell is blank and the test uses the weighted-mean 2s alone.",
            "Primary reference materials calibrate the session and are not independent quality-control evidence."):
        row += 1
        book.line(sheet, row, text, len(headers) - 1)
    sheet.print_area(0, 0, row, len(headers) - 1)


def write_supplement_xlsx(sessions: Sequence[SupplementSession], path,
                          info: SupplementInfo | None = None) -> Path:
    """Write a stand-alone SI workbook for one or more reduced sessions."""
    import xlsxwriter
    if not isinstance(sessions, Sequence) or not sessions or any(not isinstance(s, SupplementSession) for s in sessions):
        raise ValueError("sessions must be a nonempty sequence of SupplementSession")
    if info is not None and not isinstance(info, SupplementInfo):
        raise ValueError("info must be SupplementInfo or None")
    info = info or SupplementInfo()
    # ISO instrument-clock timestamps sort without imposing a timezone. Keep
    # caller order for sessions without acquisition timing and for ties.
    sessions = sorted(sessions, key=lambda s: (_starts(s) == "not recorded", _starts(s)))
    samples, rows, omitted = _selected(sessions, info)
    materials = ", ".join(dict.fromkeys(str(s.table.context.get("material", "material")) for s in sessions))
    title = info.title or f"Supplementary information: {materials} LA-ICP-MS U-Pb dates and trace-element concentrations"
    references = [_reference_rows(s) for s in sessions]
    target = _export_path(path, ".xlsx")
    with xlsxwriter.Workbook(target, {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        # Set the default as well as explicit formats, including blank cells.
        workbook.formats[0].set_font_name("Calibri")
        workbook.formats[0].set_font_size(10)
        book = _Workbook(workbook, info, _logins(sessions))
        _write_readme(book, sessions, info, samples, rows, omitted, title)
        _write_methods(book, sessions, info, references)
        _write_data(book, sessions, samples, rows)
        _write_data(book, sessions, samples, rows, standards=True)
        _write_qc(book, sessions, references)
    return target


__all__ = ["MethodsEntry", "SampleEntry", "SupplementInfo", "SupplementSession",
           "session_display_name", "supplement_session", "supplement_info_from_settings",
           "write_supplement_xlsx", "SECTION_ORDER"]
