"""Reduction failures and reference repairs remain inline and reversible."""

import os
from pathlib import Path
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window, services
from GUI.main_window import MainWindow
from GUI.theme import build_application
from UPbRPy.notices import ReductionError, ReductionNotice
from UPbRPy.pipeline import PipelineStage, config_invalidation_stage
from UPbRPy.project import write_reduction_project


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"


def test_gui_uses_engine_config_invalidation_stage():
    assert services.config_invalidation_stage is config_invalidation_stage


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def idle(app, window):
    deadline = time.monotonic() + 30
    while window._active_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert window._active_worker is None
    app.processEvents()


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow()
    window.load_experiment_directory(str(RICH), background=False, offer_restore=False)

    def no_message_box(*args, **kwargs):
        pytest.fail("A reduction issue created a QMessageBox")

    monkeypatch.setattr(QtWidgets, "QMessageBox", no_message_box)
    yield window
    idle(app, window)
    for timer in (window._selection_edit_timer, window._selection_recompute_timer,
                  window._selection_autosave_timer):
        timer.stop()
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize("quiet", [False, True])
def test_background_failure_shows_inline_banner_and_unlocks_workflow(window, app, quiet):
    window.session.pipeline.cache.set(PipelineStage.CONCENTRATION, object())
    old = window.session.concentration_standards
    window.concentration_standards_edit.setText("NIST610")
    window.on_concentration_standards_changed()
    window.change_guard.apply()
    assert window.change_guard.record is not None

    def fail():
        raise ReductionError("No selected standard analyses.", "concentration",
                             "Choose a measured concentration standard.", ("choose_standard",))

    window.run_background("Calibration", fail, lambda result: pytest.fail("Unexpected success"), quiet=quiet)
    idle(app, window)
    banner = window.issue_banner
    assert not banner.isHidden()
    assert banner.stage == "concentration"
    assert "No selected standard analyses." in banner.message.text()
    assert banner.next_step.text() == "Choose a measured concentration standard."
    assert banner.details.isHidden()
    banner.details_button.click()
    assert "ReductionError" in banner.details.toPlainText()
    assert not banner.details.isHidden()
    assert window.workflow_step_list.isEnabled()
    for row in range(len(window.workflow_preset.stage_sequence)):
        assert window.workflow_step_list.item(row).flags() & QtCore.Qt.ItemFlag.ItemIsEnabled
    assert any("Needs attention" in window.workflow_step_list.item(row).text()
               for row in range(window.workflow_step_list.count()))
    assert window.step_back_action.isEnabled() or window.step_next_action.isEnabled()
    for card in (window.fit_card, window.standards_card, window.decision_card):
        assert card.isEnabled()
    banner.undo_button.click()
    assert window.session.concentration_standards == old
    assert banner.isHidden()


def test_synchronous_error_and_library_error_are_inline(window):
    window._handle_action_error("Normalization", KeyError("Missing setting"))
    assert window.issue_banner.message.text().endswith("Missing setting")
    window.issue_banner.dismiss_button.click()
    assert window.issue_banner.isHidden()
    window.request_reference_material_library("/no-such-library/library.toml")
    window.change_guard.apply()
    assert not window.reference_library_error.isHidden()
    assert "Choose a readable" in window.reference_library_error.text()


def test_banner_undo_restores_previous_session(window):
    previous = window.session
    window.session.pipeline.cache.set(PipelineStage.DOWNHOLE, object())
    combo = window.selection_method_combo
    combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
    window.change_guard.apply()
    assert window.session is not previous
    window._handle_action_error("Downhole", ReductionError(
        "No primary standard.", "downhole", "Choose a measured primary standard.",
    ))
    assert not window.issue_banner.undo_button.isHidden()
    window.issue_banner.undo_button.click()
    assert window.session is previous


def test_notice_fixes_store_private_values_and_keep_isotope_fits(window, app):
    notice = ReductionNotice("concentration", "missing_reference_value",
        "No P value for S513: P not calibrated with it.", "S513", "31P", ("choose_standard", "enter_value"))
    window.session.pipeline.notices.append(notice)
    status = window.statusBar().currentMessage()
    window._refresh_workflow_status()
    assert window.reduction_notices.notices == [notice]
    assert window.statusBar().currentMessage() == status
    row = window.workflow_preset.stage_sequence.index(PipelineStage.CONCENTRATION)
    assert "Needs attention" in window.workflow_step_list.item(row).text()
    assert window.workflow_step_list.item(row).toolTip() == notice.message
    window._fix_reduction_issue(notice, "choose_standard")
    assert window.concentration_standards_edit.hasFocus() or not window.isVisible()
    window._fix_reduction_issue(notice, "enter_value")
    assert not window.reference_value_editor.isHidden()
    window.reference_value_edit.setText("nan")
    window.reference_value_apply_button.click()
    assert window.session.reference_overrides == {}
    assert "finite" in window.reference_value_feedback.text()
    fits = object()
    window.session.pipeline.cache.set(PipelineStage.SESSION, fits)
    before = window.scientific_project_fingerprint()
    window.reference_value_edit.setText("42.5")
    window.reference_value_apply_button.click()
    idle(app, window)
    assert window.session.reference_overrides == {"S513": {"31P": 42.5}}
    assert window.scientific_project_fingerprint() != before
    assert window.session.pipeline.standard_archive.loc["S513", "31P"] == 42.5
    assert "31P" not in services.current_standard_archive() or services.current_standard_archive().loc["S513", "31P"] != 42.5
    assert window.session.pipeline.cache.get(PipelineStage.SESSION) is fits
    window.change_guard.undo()
    assert window.session.reference_overrides == {}
    assert window.scientific_project_fingerprint() == before
    assert window.session.pipeline.cache.get(PipelineStage.SESSION) is fits


def test_gui_project_restore_retains_reference_settings(window, tmp_path):
    window.session.internal_standard_channel = "29Si"
    window.session.reference_overrides = {"S513": {"31P": 23.4}}
    pipeline = services.ensure_shared_pipeline(window.session, window.workflow_preset)
    project = write_reduction_project(pipeline, tmp_path / "reference.upbr", resume_stage=PipelineStage.SELECTION)
    restored = services.restore_session_from_project(project).session
    assert restored.internal_standard_channel == "29Si"
    assert restored.reference_overrides == {"S513": {"31P": 23.4}}
    assert services.ensure_shared_pipeline(restored, window.workflow_preset).config.reference_overrides == restored.reference_overrides


def test_cannot_open_is_reported_in_active_tab(window):
    from GUI.main import StudioWindowController

    controller = SimpleNamespace(active_window=lambda: SimpleNamespace(active_reduction=window))
    StudioWindowController.open_target(controller, "/no-such-source/data.csv")
    assert not window.issue_banner.isHidden()
    assert window.issue_banner.stage == "import"
    assert "Open an existing" in window.issue_banner.next_step.text()
