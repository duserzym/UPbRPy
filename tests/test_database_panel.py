"""Explicit database navigation, cached checks and submission outcomes."""

from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets

import GUI.database as gui_database
from GUI.database_panel import SUBMIT_STEP
from GUI.state import ReductionSession
from GUI.workflows import WORKFLOW_PRESETS
from UPbRPy.pipeline import PipelineStage
from test_database_gui import (  # noqa: F401
    app, controller, dialogs, local_link_project, mock_client,
)
from test_database_submit_buttons import ORIGIN, window  # noqa: F401
from test_small_screens import clipped_widgets, process


def set_origin(window, submitted=False):
    window.session.origin = dict(ORIGIN)
    if not submitted:
        window.session.origin.pop("run_id")
    window.update_project_window_title()


def cache_final(window):
    result = SimpleNamespace(selected_results=pd.DataFrame({"analysis": [1, 2, 3]}))
    window.session.pipeline.cache.set(PipelineStage.FINALIZE, result)
    window.session.reduction_cache["finalized_results"] = result
    window.database_panel.refresh()


def test_strip_states_and_navigation_only(window, monkeypatch):
    panel = window.database_panel
    client = Mock(side_effect=AssertionError("Navigation must not contact the database"))
    monkeypatch.setattr(gui_database, "client", client)
    bridge = window.database()
    for name in ("link_session", "submit", "prefer"):
        monkeypatch.setattr(bridge, name, Mock())
    for origin, state, detail, button in (
        (None, "Not in the database", "Made from local files", "Link to session…"),
        ({k: v for k, v in ORIGIN.items() if k != "run_id"}, "Linked · not submitted",
         "Synthetic_20Mar25 on lab.example.org", "Submit…"),
        (ORIGIN, "Submitted · run run-prev", "Synthetic_20Mar25 on lab.example.org", "Open"),
    ):
        window.session.origin = origin
        window.update_project_window_title()
        assert panel.strip.isVisibleTo(window)
        assert panel.strip_state.text() == state
        assert panel.strip_detail.text() == detail
        assert panel.strip_detail.toolTip() == detail
        assert panel.strip_button.text() == button
        assert not panel.strip_detail.wordWrap()
        panel.strip_button.click()
        assert window.workflow_step_list.currentItem().data(QtCore.Qt.ItemDataRole.UserRole) == SUBMIT_STEP
        assert panel.isVisibleTo(window)
        assert window.plots_stack.isHidden()
        assert not window.stage_segment_bar.isVisibleTo(window)
    client.assert_not_called()
    bridge.link_session.assert_not_called()
    bridge.submit.assert_not_called()
    bridge.prefer.assert_not_called()


def test_strip_hidden_without_extra_or_loaded_session(window, monkeypatch):
    panel = window.database_panel
    monkeypatch.setattr(gui_database, "database_availability", lambda: (False, "missing"))
    window._update_database_actions()
    window._sync_workflow_step_locks()
    assert panel.strip.isHidden()
    assert panel.isHidden()
    assert not window.workflow_step_list.item(window.workflow_step_list.count() - 1).flags()
    monkeypatch.setattr(gui_database, "database_availability", lambda: (True, ""))
    window.session = ReductionSession()
    panel.refresh()
    assert panel.strip.isHidden()


@pytest.mark.parametrize("submitted", [False, True])
def test_linked_strip_visible_on_concordia_pages(window, app, submitted):
    set_origin(window, submitted=submitted)
    review = window.results_table
    window.session.plot_mode = "final_results"
    window.plots_stack.setCurrentWidget(review)
    for page in (review.spot_concordia_page, review.depth_concordia_page):
        review.tabs.setCurrentWidget(page)
        for focus in (False, True):
            window.set_plot_focus(focus)
            app.processEvents()
            assert window.database_panel.strip.isVisibleTo(window)
            assert window.database_panel.strip_detail.text() == "Synthetic_20Mar25 on lab.example.org"
    window.set_plot_focus(False)


def test_submit_step_in_all_presets_and_selection_runs_nothing(window, monkeypatch):
    refresh = Mock()
    flush = Mock()
    monkeypatch.setattr(window, "refresh_view", refresh)
    monkeypatch.setattr(window, "flush_selection_edits", flush)
    monkeypatch.setattr("GUI.main_window.ensure_shared_pipeline", Mock(side_effect=AssertionError("no pipeline")))
    for preset in WORKFLOW_PRESETS.values():
        window.workflow_preset = preset
        window._populate_workflow_ui()
        assert window.workflow_step_list.count() == len(preset.steps) + 1
        assert SUBMIT_STEP not in [stage.value for stage in preset.stage_sequence]
        refresh.reset_mock()
        flush.reset_mock()
        window.database_panel.select_step()
        assert window.current_workflow_step_title() == "Submit to database"
        # Short list label so the rail does not elide it at 1280 px.
        assert window.workflow_step_list.item(window.workflow_step_list.count() - 1).text().endswith("  Submit")
        assert window.current_workflow_stage() == PipelineStage.FINALIZE
        assert window._workflow_action_specs() == []
        window.run_current_workflow_action()
        refresh.assert_not_called()
        flush.assert_not_called()


def test_back_next_reach_submit_and_restore_plots(window, monkeypatch):
    monkeypatch.setattr(window, "refresh_view", Mock(side_effect=window._refresh_step_panels))
    for stage in window.workflow_preset.stage_sequence:
        window.session.pipeline.cache.set(stage, object())
    last_stage = len(window.workflow_preset.stage_sequence) - 1
    window.workflow_step_list.setCurrentRow(last_stage)
    assert window.step_next_button.isEnabled()
    assert "Submit to database" in window.step_next_button.toolTip()
    window.step_next_button.click()
    assert window.database_panel.isVisibleTo(window)
    assert not window.step_next_button.isEnabled()
    assert window.step_back_button.isEnabled()
    window.step_back_button.click()
    assert window.workflow_step_list.currentRow() == last_stage
    assert window.database_panel.isHidden()
    assert not window.plots_stack.isHidden()
    assert window.stage_segment_bar.isVisibleTo(window)


def test_back_from_early_submit_reaches_an_unlocked_stage(window):
    panel = window.database_panel
    panel.select_step()
    assert panel.isVisibleTo(window)
    window.step_back_button.click()
    item = window.workflow_step_list.currentItem()
    assert item.data(QtCore.Qt.ItemDataRole.UserRole) != SUBMIT_STEP
    assert item.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled
    assert panel.isHidden()
    assert window.plots_stack.isVisibleTo(window)


def test_page_rows_checks_and_existing_handlers(window, monkeypatch):
    panel = window.database_panel
    panel.select_step()
    assert panel.link.status.text() == "needed"
    assert panel.link.primary.text() == "Link to database session…"
    assert panel.check.status.text() == "waiting"
    assert "not calculated yet" in panel.check.detail.text()
    assert f"{len(window.session.experiment)} selection windows" in panel.check.detail.text()
    assert panel.submit.status.text() == "waiting"
    assert panel.submit.primary.isHidden()
    assert panel.prefer.status.text() == "after submitting"
    assert panel.prefer.primary.isHidden()
    bridge = window.database()
    link, submit, prefer, calculate = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(bridge, "link_session", link)
    monkeypatch.setattr(bridge, "submit", submit)
    monkeypatch.setattr(bridge, "prefer", prefer)
    monkeypatch.setattr(window, "run_final_review", calculate)
    panel.link.primary.click()
    link.assert_called_once_with(window)
    panel.check.primary.click()
    calculate.assert_called_once_with()
    set_origin(window)
    assert panel.link.status.text() == "done"
    assert panel.link.primary.isHidden()
    assert panel.submit.primary.isHidden()
    cache_final(window)
    assert panel.check.status.text() == "ready"
    assert "3 analyses with final results" in panel.check.detail.text()
    assert "pipeline " in panel.check.detail.text()
    assert panel.check.primary.isHidden()
    assert panel.submit.status.text() == "ready"
    assert panel.submit.primary.text() == "Submit reduction"
    panel.submit.primary.click()
    submit.assert_called_once_with(window)
    set_origin(window, submitted=True)
    assert panel.submit.status.text().startswith("done · run run-prev")
    assert panel.submit.primary.text() == "Submit again as a new run…"
    assert panel.submit.secondary.text() == "Open in UPb Lab"
    assert panel.prefer.primary.text() == "Mark as preferred"
    panel.submit.primary.click()
    assert submit.call_count == 2
    panel.prefer.primary.click()
    prefer.assert_called_once_with(window)
    opened = Mock()
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", opened)
    panel.submit.secondary.click()
    assert opened.call_args.args[0].toString() == "upbdb://run/run-previous"


def test_old_buttons_and_origin_duplicate_stay_hidden(window):
    set_origin(window, submitted=True)
    for stage in (*range(len(window.workflow_preset.steps)), window.workflow_step_list.count() - 1):
        window.workflow_step_list.setCurrentRow(stage)
        window._sync_database_button_visibility()
        assert window.database_panel.strip.isVisibleTo(window)
        assert window.step_database_block.isHidden()
        assert window.results_database_block.isHidden()
        assert window.database_origin_label.isHidden()


def test_busy_failure_success_and_preference(window, dialogs, tmp_path, monkeypatch):
    set_origin(window)
    cache_final(window)
    panel = window.database_panel
    panel.select_step()
    bridge = window.database()
    callbacks = {}
    monkeypatch.setattr(bridge, "run", lambda fn, done, **kw: callbacks.update(done=done, **kw))
    failure = Mock()
    bridge._operation_run(window.session, "submit", lambda: None, lambda result: None,
                          title="Submit", on_failure=failure)
    assert panel.submit.status.text() == "working…"
    assert not panel.submit.primary.isEnabled()
    callbacks["on_failure"]("Server refused the reduction")
    failure.assert_called_once_with("Server refused the reduction")
    assert panel.submit.status.text() == "failed"
    assert "Server refused" in panel.submit.detail.text()
    assert panel.submit.primary.isEnabled()
    scratch = tmp_path / "export"
    scratch.mkdir()
    result = SimpleNamespace(run_id="0123456789-new", counts={"windows": 3, "results": 27},
                             quarantine_items=[{"reason": "test"}])
    bridge._submitted(window, result, scratch)
    assert panel.submit.status.text().startswith("done · run 01234567 at ")
    assert "windows: 3" in panel.submit.detail.text()
    assert "results: 27" in panel.submit.detail.text()
    assert "quarantined: 1" in panel.submit.detail.text()
    assert window.session.origin["run_id"] == result.run_id
    assert not scratch.exists()
    assert "preferred" not in panel.strip_detail.text()
    bridge._preferred(window, result.run_id)
    assert panel.prefer.status.text() == "preferred"
    assert panel.prefer.primary.isHidden()
    assert panel.strip_detail.text().endswith(" · preferred")
    # Another run must not inherit the preceding run's preference or counts.
    window.session.origin["run_id"] = "another-run"
    panel.refresh()
    assert "preferred" not in panel.strip_detail.text()
    assert "windows: 3" not in panel.submit.detail.text()


def test_export_failure_is_also_on_page(window):
    set_origin(window)
    panel = window.database_panel
    panel.select_step()
    with pytest.raises(ValueError, match="Cannot export"):
        window.database()._submit_export(window.session, Mock(side_effect=ValueError("Cannot export")))
    assert panel.submit.status.text() == "failed"
    assert panel.submit.detail.text() == "Cannot export"


def test_link_busy_and_multiline_failure_are_visible(window, monkeypatch):
    panel = window.database_panel
    panel.select_step()
    bridge = window.database()
    callbacks = {}
    monkeypatch.setattr(bridge, "run", lambda fn, done, **kw: callbacks.update(done=done, **kw))
    bridge._operation_run(window.session, "link", lambda: None, lambda result: None,
                          title="Link", on_failure=lambda message: None)
    assert panel.link.status.text() == "working…"
    assert not panel.link.primary.isEnabled()
    callbacks["on_failure"]("Raw verification failed\n<details>")
    assert panel.link.status.text() == "failed"
    assert panel.link.detail.text() == "Raw verification failed <details>"
    assert panel.link.detail.toolTip() == "Raw verification failed\n<details>"
    assert panel.link.primary.isEnabled()


def test_known_preference_and_stale_final_cache_are_read_without_client(window, monkeypatch):
    set_origin(window, submitted=True)
    cache_final(window)
    panel = window.database_panel
    monkeypatch.setattr(gui_database, "client", Mock(side_effect=AssertionError("No network reads")))
    window.session.origin["preferred_run_id"] = "a-different-run"
    panel.refresh()
    assert panel.prefer.status.text() == "not preferred"
    assert panel.strip_detail.text().endswith(" · not preferred")
    window.session.origin["preferred_run_id"] = window.session.origin["run_id"]
    panel.refresh()
    assert panel.prefer.status.text() == "preferred"
    assert panel.strip_detail.text().endswith(" · preferred")
    assert panel.prefer.primary.isHidden()
    window.session.pipeline.cache.invalidate(PipelineStage.FINALIZE)
    panel.refresh()
    assert "finalized_results" in window.session.reduction_cache
    assert panel.check.status.text() == "waiting"
    gui_database.client.assert_not_called()


def test_real_link_records_verification_on_page(local_link_project, dialogs):
    window = local_link_project.window
    panel = window.database_panel
    panel.select_step()
    panel.link.primary.click()
    assert panel.link.status.text() == "done"
    assert "raw data verified" in panel.link.detail.text()
    assert "raw data verified" not in window.session.origin
    assert window.session.origin["session_id"]


def test_real_link_failure_stays_on_page(local_link_project, dialogs):
    case = local_link_project
    case.fake.fetch_session_bundle = Mock(side_effect=ValueError("Raw inputs differ"))
    panel = case.window.database_panel
    panel.select_step()
    panel.link.primary.click()
    assert panel.link.status.text() == "failed"
    assert panel.link.detail.text() == "Raw inputs differ"
    assert panel.link.primary.isEnabled()
    assert case.window.session.origin is None


def test_submit_cancel_failure_retry_and_prefer_use_unchanged_flows(window, dialogs, monkeypatch):
    set_origin(window, submitted=True)
    cache_final(window)
    panel = window.database_panel
    panel.select_step()
    bridge = window.database()
    export = Mock()
    upload = Mock(side_effect=ValueError("Upload refused"))
    prefer = Mock(return_value={})
    monkeypatch.setattr(gui_database, "client", lambda: SimpleNamespace(
        submit_reduction=upload, prefer_run=prefer, is_signed_in=lambda server: True,
    ))
    monkeypatch.setattr("GUI.services.ensure_shared_pipeline", Mock(return_value=object()))
    monkeypatch.setattr("UPbRPy.reproducibility.export_reduction_bundle", export)
    monkeypatch.setattr("UPbRPy.project.write_reduction_project", Mock())
    monkeypatch.setattr(window, "apply_pending_project_edits", Mock())
    monkeypatch.setattr(window, "project_workspace_state", lambda: {})
    monkeypatch.setattr(window, "run_background", lambda title, fn, done: done(fn()))
    monkeypatch.setattr(bridge, "_authorize", lambda server, code, then, title: then())

    def run(fn, done, **kwargs):
        try:
            result = fn()
        except Exception as exc:
            kwargs["on_failure"](str(exc))
        else:
            done(result)

    monkeypatch.setattr(bridge, "run", run)
    question = Mock(return_value=QtWidgets.QMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", question)
    panel.submit.primary.click()
    upload.assert_not_called()
    export.assert_not_called()
    assert "supersede the run this reduction submitted earlier (id run-prev" in question.call_args.args[2]
    question.return_value = QtWidgets.QMessageBox.StandardButton.Yes
    panel.submit.primary.click()
    assert panel.submit.status.text() == "failed"
    assert panel.submit.detail.text() == "Upload refused"
    assert upload.call_args.kwargs["supersedes"] == "run-previous"
    upload.side_effect = None
    upload.return_value = SimpleNamespace(run_id="new-run-123", counts={"windows": 4, "results": 12},
                                          quarantine_items=[])
    panel.submit.primary.click()
    assert panel.submit.status.text().startswith("done · run new-run- at ")
    assert "results: 12" in panel.submit.detail.text()
    assert window.session.origin["run_id"] == "new-run-123"
    panel.prefer.primary.click()
    prefer.assert_called_once_with(ORIGIN["server"], ORIGIN["session_id"], "new-run-123")
    assert panel.prefer.status.text() == "preferred"


def test_other_session_outcomes_are_ignored_and_replacement_resets(window):
    panel = window.database_panel
    bridge = window.database()
    bridge.operation_changed.emit(ReductionSession(), "submit", "error", "Other tab")
    assert "Other tab" not in panel.submit.detail.text()
    bridge.operation_changed.emit(window.session, "link", "error", "Wrong inputs")
    assert panel.link.status.text() == "failed"
    old = window.session
    window.session = ReductionSession(directory=old.directory, experiment=old.experiment,
                                      total_data=old.total_data)
    panel.refresh()
    assert panel.link.status.text() == "needed"
    assert "Wrong inputs" not in panel.link.detail.text()


@pytest.mark.parametrize("size", [(1440, 900), (1280, 800)])
def test_strip_and_page_fit_without_scrollbar(window, app, size, tmp_path):
    shell = window.window()
    shell.show()
    shell.resize(*size)
    window.apply_default_layout()
    panel = window.database_panel
    for state in ("unlinked", "linked", "submitted"):
        if state != "unlinked":
            set_origin(window, submitted=state == "submitted")
            window.session.origin["session_key"] = "A very long session name " * 8
            window.update_project_window_title()
            cache_final(window)
        panel.select_step()
        process(app)
        assert shell.size() == QtCore.QSize(*size)
        assert not clipped_widgets(shell)
        assert panel.strip.height() < 60
        assert not panel.strip_detail.wordWrap()
        assert panel.strip_detail.toolTip() == panel.strip_detail.text()
        assert not panel.findChildren(QtWidgets.QScrollArea)
        for row in (panel.link, panel.check, panel.submit, panel.prefer):
            assert panel.rect().contains(row.geometry())
            for button in (row.primary, row.secondary):
                if button.isVisibleTo(panel):
                    assert row.actions.rect().contains(button.geometry())
        # Temporary review artifacts stay outside the repository.
        shell.grab().save(str(tmp_path / f"database-{state}-{size[0]}.png"))
