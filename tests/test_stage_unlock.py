"""Stage retention and Studio service cache synchronization without a display."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from GUI import services
from GUI.workflows import get_workflow_preset
from UPbRPy.pipeline import PipelineStage


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"
PRESET = get_workflow_preset("zircon")


def step_unlocked(session, stage):
    """Apply Studio's previous-stage rule using the preset's actual order."""
    index = PRESET.stage_sequence.index(stage)
    return services.unlocked_workflow_rows(
        PRESET.stage_sequence, session.pipeline.cache.stages(), session.loaded,
    )[index]


def assert_retained(pipeline, before):
    for stage, result in before.items():
        assert pipeline.cache.get(stage) is result, stage


def run_concentration_steps(session):
    for stage, action, next_stage in (
        (PipelineStage.BASELINE, lambda: services.ensure_baseline_subtracted(session),
         PipelineStage.NORMALIZATION),
        (PipelineStage.NORMALIZATION, lambda: services.ensure_normalized_plateau(session, PRESET),
         PipelineStage.CONCENTRATION),
        (PipelineStage.CONCENTRATION, lambda: services.ensure_concentration_results(session, PRESET),
         PipelineStage.RATIOS),
    ):
        before = session.pipeline.cache.as_mapping()
        assert step_unlocked(session, stage)
        action()
        assert stage in session.pipeline.cache.stages()
        assert_retained(session.pipeline, before)
        assert step_unlocked(session, next_stage)


@pytest.fixture(params=["qtegra", "drift", "constant"])
def session(request):
    loaded = services.load_session(str(FIXTURE), preset=PRESET)
    if request.param != "qtegra":
        loaded = services.reimport_detector_calibration(
            loaded, {"mode": request.param, "on": "all"},
        )
    return loaded


def test_concentration_unlocks_ratios_and_unchanged_sync_retains_objects(session):
    run_concentration_steps(session)
    assert PRESET.stage_sequence.index(PipelineStage.RATIOS) == 5
    before = session.pipeline.cache.as_mapping()
    for _ in range(2):
        assert services.ensure_shared_pipeline(session, PRESET) is session.pipeline
        assert session.pipeline.cache.as_mapping().keys() == before.keys()
        assert_retained(session.pipeline, before)
    # Compatibility products also come back from the same cached core objects.
    assert services.ensure_concentration_results(session, PRESET) is before[PipelineStage.CONCENTRATION]
    assert services.ensure_normalized_plateau(session, PRESET) is before[PipelineStage.NORMALIZATION]
    assert services.ensure_baseline_subtracted(session) is before[PipelineStage.BASELINE]


def test_later_steps_unlock_successively_when_fixture_supports_ratios(session):
    run_concentration_steps(session)
    if "202Hg" not in session.channels:
        pytest.skip(
            "Qtegra slice lacks 202Hg required by calc_raw_ratios for Hg correction; "
            "ratios, downhole, session, spot means, final and depth cannot run. "
            "S513 primary standards are present."
        )
    actions = (
        (PipelineStage.RATIOS, lambda: services.ensure_raw_ratios(session)),
        (PipelineStage.DOWNHOLE, lambda: services.ensure_downhole_results(session, PRESET)),
        (PipelineStage.SESSION, lambda: services.ensure_session_corrected(session, PRESET)),
        (PipelineStage.SPOT_MEANS, lambda: services.ensure_spot_means(session, PRESET)),
        (PipelineStage.FINALIZE, lambda: services.ensure_finalized_results(session, PRESET)),
        (PipelineStage.DEPTH_PROFILE, lambda: services.ensure_depth_profile(session, PRESET)),
    )
    for stage, action in actions:
        before = session.pipeline.cache.as_mapping()
        if stage in PRESET.stage_sequence:
            assert step_unlocked(session, stage)
        action()
        assert stage in session.pipeline.cache.stages()
        assert_retained(session.pipeline, before)
        if stage in PRESET.stage_sequence[:-1]:
            index = PRESET.stage_sequence.index(stage)
            assert step_unlocked(session, PRESET.stage_sequence[index + 1])


@pytest.mark.parametrize("refresh_pipeline", [True, False])
def test_in_place_window_edit_recomputes_concentration(session, refresh_pipeline):
    run_concentration_steps(session)
    frame = session.experiment
    old = session.pipeline.cache.as_mapping()
    row = frame.iloc[0]
    services.update_selection_region(
        session, row.spot_names,
        plateau=(float(row.plateau_start_times) + 0.5, float(row.plateau_end_times)),
        refresh_pipeline=refresh_pipeline,
    )
    assert session.experiment is frame
    services.ensure_shared_pipeline(session, PRESET)
    assert session.pipeline.cache.stages() == (PipelineStage.IMPORT, PipelineStage.SELECTION)
    assert session.pipeline.cache.get(PipelineStage.SELECTION) is not old[PipelineStage.SELECTION]
    assert not session.reduction_cache
    result = services.ensure_concentration_results(session, PRESET)
    assert result is not old[PipelineStage.CONCENTRATION]
    assert session.pipeline.cache.get(PipelineStage.BASELINE) is not old[PipelineStage.BASELINE]
    assert session.reduction_cache["concentration_results"] is result
    assert session.pipeline.cache.get(PipelineStage.CONCENTRATION) is result
    assert step_unlocked(session, PipelineStage.RATIOS)


@pytest.mark.parametrize("edit", ["replace", "filter", "list", "array", "dict"])
def test_source_replacement_and_object_cell_edits_are_detected(edit):
    session = services.load_session(str(FIXTURE), preset=PRESET)
    if edit in {"list", "array", "dict"}:
        value = {"list": [1, 2], "array": np.array([1, 2]), "dict": {"excluded": [1, 2]}}[edit]
        session.experiment["review_indices"] = pd.Series(
            [value] + [None] * (len(session.experiment) - 1), dtype=object,
        )
        services.ensure_shared_pipeline(session, PRESET)
    run_concentration_steps(session)
    old_selection = session.pipeline.experiment
    assert session.pipeline.matches_source_experiment(session.experiment)
    if edit == "replace":
        session.experiment = session.experiment.copy()
    elif edit == "filter":
        session.experiment.loc[0, "plateau_filter_method"] = "none"
    elif edit == "dict":
        session.experiment.at[0, "review_indices"]["excluded"].append(3)
    else:
        session.experiment.at[0, "review_indices"][0] = 3
    assert not session.pipeline.matches_source_experiment(session.experiment)
    services.ensure_shared_pipeline(session, PRESET)
    assert session.pipeline.experiment is not old_selection
    assert session.pipeline.matches_source_experiment(session.experiment)
    assert session.pipeline.cache.stages() == (PipelineStage.IMPORT, PipelineStage.SELECTION)
    assert not session.reduction_cache


def test_clear_cache_syncs_main_window_filter_edits():
    session = services.load_session(str(FIXTURE), preset=PRESET)
    run_concentration_steps(session)
    session.experiment.loc[0, "plateau_filter_method"] = "none"
    services.clear_reduction_cache(session)
    assert session.pipeline.experiment.loc[0, "plateau_filter_method"] == "none"
    assert session.pipeline.matches_source_experiment(session.experiment)
    assert session.pipeline.cache.stages() == (PipelineStage.IMPORT, PipelineStage.SELECTION)
    assert not session.reduction_cache


def test_missing_pipeline_stage_overrides_stale_gui_keys():
    session = services.load_session(str(FIXTURE), preset=PRESET)
    run_concentration_steps(session)
    stale = dict(session.reduction_cache)
    session.pipeline.cache.invalidate(PipelineStage.NORMALIZATION)
    result = services.ensure_concentration_results(session, PRESET)
    assert result is not stale["concentration_results"]
    assert session.reduction_cache["normalized_plateau_data"] is not stale["normalized_plateau_data"]
    assert session.pipeline.cache.get(PipelineStage.CONCENTRATION) is result
    assert session.pipeline.cache.get(PipelineStage.BASELINE) is stale["baseline_subtracted_total_data"]


def test_populate_recalculated_cache_retains_completed_stages():
    session = services.load_session(str(FIXTURE), preset=PRESET)
    run_concentration_steps(session)
    completed = session.pipeline.cache.as_mapping()
    session.reduction_cache.clear()
    services.populate_recalculated_cache(session, PRESET, completed)
    assert_retained(session.pipeline, completed)
    assert session.reduction_cache["concentration_results"] is completed[PipelineStage.CONCENTRATION]
    assert step_unlocked(session, PipelineStage.RATIOS)
