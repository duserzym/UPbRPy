from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from UPbRPy.pipeline import PipelineStage


@dataclass(frozen=True)
class WorkflowStep:
    title: str
    summary: str
    hint: str


@dataclass(frozen=True)
class WorkflowPreset:
    key: str
    title: str
    description: str
    primary_channel: str
    read_kwargs: dict[str, object]
    internal_material: str
    supports_depth_profiles: bool
    special_note: str
    steps: tuple[WorkflowStep, ...]
    stage_sequence: tuple[PipelineStage, ...]
    concentration_standards: tuple[str, ...] = ()
    primary_standard: str = ""
    pb_ratio_standard: str = ""
    th_ratio_standard: str = ""
    # Primary standards to prefer, in order, when the session measured them;
    # ``primary_standard`` applies when none of them was measured.
    preferred_primary_standards: tuple[str, ...] = ()
    common_pb_calibration: str = "radiogenic"
    secondary_standards: tuple[str, ...] = ()
    session_polynomial_degree: int = 2
    default_ratio: str = "206Pb/238U"
    plateau_filter_method: str = "none"


COMMON_RATIO_STEP = WorkflowStep(
    title="Build raw isotope ratios",
    summary="Create a baseline-subtracted plateau table and calculate raw sweep-wise ratios before correction.",
    hint="The notebooks explicitly avoid using internally normalized data for isotope ratios. Use baseline-subtracted plateau data for ratio construction, then inspect standards and unknowns through time.",
)


WORKFLOW_PRESETS: dict[str, WorkflowPreset] = {
    "zircon": WorkflowPreset(
        key="zircon",
        title="Zircon",
        description="Radiogenic zircon workflow with Si-based internal normalization, NIST glass concentration calibration, and full downhole plus session fractionation correction.",
        primary_channel="29Si",
        read_kwargs={
            "selection_method": "threshold",
            "baseline_sweep_n": 10,
            "peak_sweep_n": 20,
            "threshold": 1,
            "filter_percentile": 0.3,
            "signal_duration_limit_seconds": 9.0,
        },
        internal_material="zircon",
        supports_depth_profiles=True,
        special_note="Zircon notebooks treat 206Pb/238U and related ratios as highly radiogenic, so initial Pb can usually be ignored for the primary SRM correction path.",
        concentration_standards=("NIST610", "NIST612"),
        primary_standard="PL",
        pb_ratio_standard="PL",
        th_ratio_standard="PL",
        # S513 (Luo et al. 2025) is the lab's zircon primary whenever measured.
        preferred_primary_standards=("S513", "PL"),
        secondary_standards=("FC1", "91500", "Seiland", "LTSE"),
        default_ratio="206Pb/238U",
        plateau_filter_method="excel_2sigma",
        steps=(
            WorkflowStep(
                title="Import and auto-pick windows",
                summary="Read all spot files, unify the session time axis, and auto-detect baseline and plateau windows.",
                hint="The zircon notebook uses 10 baseline sweeps, 20 peak sweeps, threshold 1, and filter percentile 0.3 for the initial auto-pick. First review the auto windows before doing any subtraction.",
            ),
            WorkflowStep(
                title="Review baseline and plateau picks",
                summary="Inspect Si-driven baseline, plateau, and signal-edge picks and correct any bad intervals.",
                hint="The notebook plots selected data right after import. A bad selection here propagates into every later result, so this is the first manual QC gate.",
            ),
            WorkflowStep(
                title="Subtract baseline",
                summary="Subtract the mean baseline for each spot and each channel.",
                hint="After subtraction, the notebook checks a representative trace again. Use this stage to verify that baseline subtraction did not invert or clip low-count channels.",
            ),
            WorkflowStep(
                title="Internal normalization",
                summary="Apply sweep-wise Si normalization using the zircon material mode.",
                hint="The zircon notebook uses 29Si as the internal analyte and normalizes sweep-wise, not spot-wise. This keeps depth-profile variance information while reducing common drift and tilt.",
            ),
            WorkflowStep(
                title="Calibrate and calculate concentrations",
                summary="Use NIST glass standards for origin-anchored cps-to-ppm calibration, then convert plateau cps to concentrations with the notebook-style uncertainty estimate.",
                hint="The notebook separates glass, standard, and unknown traces before fitting concentration factors, then carries the Student-t based per-spot uncertainty into the concentration outputs. The GUI currently runs that as one concentration stage.",
            ),
            COMMON_RATIO_STEP,
            WorkflowStep(
                title="Downhole correction",
                summary="Stack primary standard ratios sweep-by-sweep and derive downhole correction curves for the key ratios.",
                hint="The zircon workflow applies downhole correction before any session-wide fractionation fit. This must be shown as a separate stage so users do not skip it.",
            ),
            WorkflowStep(
                title="Session fractionation correction",
                summary="Fit the corrected primary-standard ratios through session time and apply the session-wide fractionation model.",
                hint="The notebook treats longer-period fractionation drift independently from downhole trends. In the GUI this should read as a second correction pass, not part of the same button.",
            ),
            WorkflowStep(
                title="Dates and concordia review",
                summary="Inspect corrected ratios, concordia-style outputs, and final spot-mean tables.",
                hint="For zircon projects, final review usually centers on Wetherill or Tera-Wasserburg concordia and spot-date plots after both correction stages are complete.",
            ),
            WorkflowStep(
                title="Depth-profile export",
                summary="When needed, keep sweep-level corrected results for depth-profile plots and downstream notebook export.",
                hint="Depth-profile work should preserve the corrected sweep-level data, not only spot means. The GUI should make that export obvious and separate from the spot table.",
            ),
        ),
        stage_sequence=(
            PipelineStage.IMPORT,
            PipelineStage.SELECTION,
            PipelineStage.BASELINE,
            PipelineStage.NORMALIZATION,
            PipelineStage.CONCENTRATION,
            PipelineStage.RATIOS,
            PipelineStage.DOWNHOLE,
            PipelineStage.SESSION,
            PipelineStage.FINALIZE,
            PipelineStage.DEPTH_PROFILE,
        ),
    ),
    "apatite": WorkflowPreset(
        key="apatite",
        title="Apatite",
        description="Apatite workflow with Ca-based internal normalization, longer plateau picks, and explicit attention to common or initial Pb behavior.",
        primary_channel="44Ca",
        read_kwargs={
            "selection_method": "threshold",
            "baseline_sweep_n": 15,
            "peak_sweep_n": 55,
            "threshold": 0.2,
            "filter_percentile": 90,
            "baseline_offset": 4,
            "plateau_offset": 2,
            "sep": " ",
            "selection_options": {
                "plateau_width_mode": "signal",
            },
        },
        internal_material="apatite",
        supports_depth_profiles=True,
        special_note="Apatite notebooks use longer measurement windows and emphasize that 206Pb/238U correction can require handling initial Pb rather than assuming a fully radiogenic SRM.",
        concentration_standards=("NIST610", "NIST612"),
        primary_standard="MADUCSB",
        pb_ratio_standard="NIST610",
        th_ratio_standard="NIST610",
        secondary_standards=("MADJCU",),
        common_pb_calibration="tera_wasserburg",
        default_ratio="206Pb/238U",
        steps=(
            WorkflowStep(
                title="Import with apatite defaults",
                summary="Load the session using the apatite notebook defaults for longer baseline and peak windows.",
                hint="The apatite notebook uses 44Ca as the primary channel with 15 baseline sweeps, 55 peak sweeps, threshold 0.2, filter percentile 90, baseline offset 4, and plateau offset 2.",
            ),
            WorkflowStep(
                title="Review baseline and plateau picks",
                summary="Confirm that the longer apatite plateau windows isolate the useful ablation interval without dragging in washout.",
                hint="Apatite auto-picks are more permissive than zircon. The GUI should remind users that these notebook defaults are optimized for broader plateaus, not short sharp peaks.",
            ),
            WorkflowStep(
                title="Subtract baseline",
                summary="Perform spot-wise baseline subtraction and verify the corrected primary channel.",
                hint="The notebook checks the corrected primary channel immediately after subtraction. That plot should be one click away in the GUI.",
            ),
            WorkflowStep(
                title="Internal normalization",
                summary="Normalize sweep-wise using 44Ca and the apatite material mode.",
                hint="The notebook explicitly uses sweep-wise normalization so depth-profile behavior remains visible. Keep that explanation visible because users often expect a single spot mean normalization.",
            ),
            WorkflowStep(
                title="Calibrate and calculate concentrations",
                summary="Fit concentration calibration factors from NIST glasses, then compute spot concentrations with the notebook-style uncertainty estimate.",
                hint="As in the notebook, concentration calibration stays separate from isotopic ratio work. The GUI currently runs calibration and concentration output together so users can inspect the fit and final ppm products in one step.",
            ),
            COMMON_RATIO_STEP,
            WorkflowStep(
                title="Downhole correction",
                summary="Stack primary-standard ratios and calculate downhole correction factors for the target ratios.",
                hint="Apatite still uses the same two-stage fractionation structure, but users should be reminded that low-204 channels can be unstable and often need extra QC.",
            ),
            WorkflowStep(
                title="Session fractionation and common Pb context",
                summary="Fit the session-wide fractionation model while keeping the apatite initial-Pb caution explicit.",
                hint="The apatite notebook explains that the exact 206Pb/238U correction can depend on initial Pb assumptions. The GUI should flag that this workflow is less plug-and-play than zircon.",
            ),
            WorkflowStep(
                title="Final dates and review",
                summary="Review corrected ratios, spot summaries, and final date outputs with the appropriate common-Pb caveats.",
                hint="For apatite, the final review should emphasize standards behavior and consistency of the Pb-ratio treatment rather than only producing a final age table.",
            ),
        ),
        stage_sequence=(
            PipelineStage.IMPORT,
            PipelineStage.SELECTION,
            PipelineStage.BASELINE,
            PipelineStage.NORMALIZATION,
            PipelineStage.CONCENTRATION,
            PipelineStage.RATIOS,
            PipelineStage.DOWNHOLE,
            PipelineStage.SESSION,
            PipelineStage.FINALIZE,
        ),
    ),
    "rutile": WorkflowPreset(
        key="rutile",
        title="Rutile",
        description="Rutile workflow with Ti-based internal normalization, glass outlier filtering before concentration calibration, and optional Zr-in-rutile thermometry plus depth-profile review.",
        primary_channel="49Ti",
        read_kwargs={
            "selection_method": "threshold",
            "baseline_sweep_n": 10,
            "peak_sweep_n": 20,
            "threshold": 0.1,
            "filter_percentile": 80,
            "baseline_offset": 2,
            "plateau_offset": 2,
        },
        internal_material="rutile",
        supports_depth_profiles=True,
        special_note="Rutile notebooks add a dedicated NIST outlier-filter step before concentration calibration and often branch into Zr-in-rutile thermometry and profile-level review after final concentrations.",
        concentration_standards=("NIST610", "NIST612"),
        primary_standard="TR1",
        pb_ratio_standard="NIST610",
        th_ratio_standard="NIST610",
        secondary_standards=("R632",),
        common_pb_calibration="tera_wasserburg",
        default_ratio="206Pb/238U",
        steps=(
            WorkflowStep(
                title="Import with rutile defaults",
                summary="Load rutile sessions using the Ti-driven auto-pick defaults from the notebooks.",
                hint="The rutile notebook uses 49Ti with a lower threshold than apatite. This is tuned for rutile traces and should be the default unless the user overrides it intentionally.",
            ),
            WorkflowStep(
                title="Review baseline and plateau picks",
                summary="Inspect Ti-driven windows and confirm the selected interval follows the useful signal plateau.",
                hint="This should be presented as mandatory QC because bad plateau edges will affect both concentration thermometry and any later profile interpretation.",
            ),
            WorkflowStep(
                title="Subtract baseline",
                summary="Subtract the per-spot baseline and verify the corrected Ti and Pb channels.",
                hint="Use this step to catch spots whose low-count channels were pushed close to zero by subtraction before starting ratio calculations.",
            ),
            WorkflowStep(
                title="Internal normalization",
                summary="Apply sweep-wise Ti normalization using the rutile material mode.",
                hint="The rutile notebook mirrors the same sweep-wise logic as zircon and apatite, but with 49Ti as the stable internal analyte.",
            ),
            WorkflowStep(
                title="Filter glass outliers",
                summary="Trim extreme NIST glass points before fitting concentration calibration factors.",
                hint="This is a notebook-specific difference for rutile. The GUI should make this a visible stage so users know the calibration fit was cleaned before use.",
            ),
            WorkflowStep(
                title="Calibrate concentrations and thermometry",
                summary="Fit the origin-anchored glass calibration, convert spot means to concentrations, and keep the rutile thermometry outputs tied to the same concentration stage.",
                hint="The notebook plots a representative analyte and then fits concentration factors before exporting concentrations and optional Zr-in-rutile temperatures. The GUI currently exposes that as one concentration-focused action.",
            ),
            COMMON_RATIO_STEP,
            WorkflowStep(
                title="Downhole correction",
                summary="Correct the main isotope ratios for downhole variability using the primary standard.",
                hint="Rutile follows the same two-stage isotope correction structure as zircon. Keep the correction plots easy to inspect because the profile use case makes bad fits obvious.",
            ),
            WorkflowStep(
                title="Session fractionation correction",
                summary="Fit the time-dependent session fractionation model after downhole correction.",
                hint="Present this as a session-scale drift model, separate from per-sweep downhole structure.",
            ),
            WorkflowStep(
                title="Depth-profile and concordia review",
                summary="Review final spot means and corrected sweep-level depth-profile tables side by side.",
                hint="The MA1 rutile depth-profile notebook reads both final spot and final depth-profile tables. The GUI should make users aware that these are related but separate products.",
            ),
        ),
        stage_sequence=(
            PipelineStage.IMPORT,
            PipelineStage.SELECTION,
            PipelineStage.BASELINE,
            PipelineStage.NORMALIZATION,
            PipelineStage.CONCENTRATION,
            PipelineStage.CONCENTRATION,
            PipelineStage.RATIOS,
            PipelineStage.DOWNHOLE,
            PipelineStage.SESSION,
            PipelineStage.DEPTH_PROFILE,
        ),
    ),
    "titanite": WorkflowPreset(
        key="titanite",
        title="Titanite",
        description="Titanite workflow following the current notebook defaults, with Si-based selection and titanite-specific internal normalization.",
        primary_channel="29Si",
        read_kwargs={
            "selection_method": "threshold",
            "baseline_sweep_n": 10,
            "peak_sweep_n": 20,
            "threshold": 0.3,
            "filter_percentile": 80,
            "baseline_offset": 2,
            "plateau_offset": 2,
        },
        internal_material="titanite",
        supports_depth_profiles=True,
        special_note="Titanite concentrations use the titanite stoichiometric Si abundance; isotope ratios use baseline-subtracted signals.",
        concentration_standards=("NIST610", "NIST612"),
        primary_standard="MKED",
        pb_ratio_standard="NIST610",
        th_ratio_standard="NIST610",
        secondary_standards=("BLR",),
        common_pb_calibration="tera_wasserburg",
        default_ratio="206Pb/238U",
        steps=(
            WorkflowStep(
                title="Import with titanite defaults",
                summary="Load titanite sessions using the notebook's Si-driven auto-pick parameters.",
                hint="Titanite uses 29Si for the import and selection stage, with a stronger threshold than rutile. This keeps import behavior aligned to the current notebook sequence.",
            ),
            WorkflowStep(
                title="Review baseline and plateau picks",
                summary="Confirm the auto-picked windows before moving on to any correction steps.",
                hint="This stage should look identical to the notebook sequence so users can compare GUI and notebook behavior directly.",
            ),
            WorkflowStep(
                title="Subtract baseline",
                summary="Subtract the spot-wise baseline and inspect corrected trace behavior.",
                hint="The GUI should emphasize that subtraction is still per spot and per channel, matching the core function behavior used in the notebooks.",
            ),
            WorkflowStep(
                title="Internal normalization",
                summary="Normalize concentration signals using titanite and the selected internal channel.",
                hint="The titanite material and Si channel match the current manuscript and notebook workflow.",
            ),
            WorkflowStep(
                title="Calibrate concentrations and titanite thermometry",
                summary="Use the notebook sequence of NIST inspection, optional outlier filtering, concentration calibration, and titanite thermometry-ready outputs.",
                hint="The titanite notebook mirrors the rutile concentration path closely here. The core library already contains titanite thermometry functions, and the GUI presents that concentration stage as one combined action.",
            ),
            COMMON_RATIO_STEP,
            WorkflowStep(
                title="Downhole correction",
                summary="Apply the notebook-style downhole correction to key ratios using the primary standard.",
                hint="Treat this as a separate reviewable fit stage, exactly like zircon and rutile.",
            ),
            WorkflowStep(
                title="Session fractionation correction",
                summary="Model the session-scale drift after downhole correction and apply it to standards and unknowns.",
                hint="The two-stage correction logic remains the same even though titanite currently shares some backend assumptions with rutile.",
            ),
            WorkflowStep(
                title="Final titanite review",
                summary="Review corrected ratios, concentration outputs, and any thermometry products together.",
                hint="Review the selected common-Pb method and uncertainty branch before exporting.",
            ),
        ),
        stage_sequence=(
            PipelineStage.IMPORT,
            PipelineStage.SELECTION,
            PipelineStage.BASELINE,
            PipelineStage.NORMALIZATION,
            PipelineStage.CONCENTRATION,
            PipelineStage.RATIOS,
            PipelineStage.DOWNHOLE,
            PipelineStage.SESSION,
            PipelineStage.FINALIZE,
        ),
    ),
}


def get_workflow_preset(key: str) -> WorkflowPreset:
    return WORKFLOW_PRESETS.get(key, WORKFLOW_PRESETS["zircon"])


def infer_workflow_preset(directory: str) -> WorkflowPreset:
    """Guess the mineral preset from the source name or its session manifest.

    A session bundle's ``raw/`` folder is named after nothing, so its
    manifest's ``material`` and then the bundle folder's name are used.
    """
    folder = Path(directory)
    names = [folder.name, folder.parent.name] if folder.is_file() else [folder.name]
    if folder.is_file():
        folder = folder.parent
    try:
        from UPbRPy.session_manifest import (
            find_session_manifest,
            read_session_manifest,
        )

        manifest_path = find_session_manifest(folder)
        if manifest_path is not None:
            manifest = read_session_manifest(manifest_path)
            names = [manifest.material, manifest.session_key, folder.parent.name, *names]
    except (OSError, ValueError):
        pass
    for name in names:
        lowered = (name or "").lower()
        for key in ["apatite", "zircon", "rutile", "titanite"]:
            if key in lowered:
                return WORKFLOW_PRESETS[key]
    return WORKFLOW_PRESETS["zircon"]


def resolve_reference_standards(preset: WorkflowPreset, measured: Iterable[str]) -> dict[str, object]:
    """Primary and secondary standards for a session that measured ``measured``.

    The first of ``preset.preferred_primary_standards`` found among the
    measured sample names (case-insensitive) replaces the preset primary,
    including the Pb and Th ratio standards that default to it; the displaced
    preset primary becomes a secondary when it was measured.
    """
    names = {str(name).casefold(): str(name) for name in measured}
    primary = preset.primary_standard
    for candidate in preset.preferred_primary_standards:
        if candidate.casefold() in names:
            primary = candidate
            break

    def swap(name: str) -> str:
        return primary if not name or name == preset.primary_standard else name

    secondaries = [s for s in preset.secondary_standards if s != primary]
    if primary != preset.primary_standard and preset.primary_standard.casefold() in names:
        secondaries.append(preset.primary_standard)
    return {
        "primary_68_standard": primary,
        "primary_76_standard": swap(preset.pb_ratio_standard),
        "primary_82_standard": swap(preset.th_ratio_standard or preset.pb_ratio_standard),
        "secondary_standards": tuple(dict.fromkeys(secondaries)),
    }


def format_import_defaults(preset: WorkflowPreset) -> str:
    if not preset.read_kwargs:
        return f"Primary {preset.primary_channel}; default read_experiment_data settings."
    ordered_pairs = [f"primary {preset.primary_channel}"]
    label_map = {
        "selection_method": "auto-pick",
        "baseline_sweep_n": "baseline sweeps",
        "peak_sweep_n": "peak sweeps",
        "threshold": "threshold",
        "filter_percentile": "filter pct",
        "baseline_offset": "baseline offset",
        "plateau_offset": "plateau offset",
    }
    for key, value in preset.read_kwargs.items():
        ordered_pairs.append(f"{label_map.get(key, key)} {value}")
    return "; ".join(ordered_pairs) + "."
