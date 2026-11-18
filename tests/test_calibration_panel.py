"""Calibration controls retain results and fit laptop inspector widths."""

import threading
from types import SimpleNamespace

import pandas as pd
import pytest
from PySide6 import QtCore, QtWidgets
from GUI import services
from UPbRPy.notices import ReductionNotice
from UPbRPy.pipeline import PipelineStage
from test_accidental_changes import app, window, cache_downhole, wheel, wait_idle
from test_small_screens import clipped_widgets, process


def primaries(window):
    return tuple(getattr(window.session, f"primary_{ratio}_standard") for ratio in (68, 76, 82))


def test_shared_primary_one_apply_and_one_undo(window, monkeypatch):
    original = primaries(window)
    products, stages = cache_downhole(window)
    calls = []
    apply = window.change_guard.apply
    monkeypatch.setattr(window.change_guard, "apply", lambda: (calls.append("apply"), apply())[1])
    selected = "91500" if original[0] != "91500" else "S513"
    window.primary_standard_combo.setCurrentText(selected)
    assert primaries(window) == original
    assert window.change_guard.pending is not None
    assert window.pending_change_bar.parentWidget() is window.calibration_tabs
    assert window.calibration_pending_layout.indexOf(window.pending_change_bar) == 1
    window.change_guard.apply()
    assert calls == ["apply"]
    assert primaries(window) == (selected,) * 3
    assert window.change_guard.pending is None
    window.change_guard.undo()
    assert primaries(window) == original
    assert window.same_primary_check.isChecked()
    assert window.primary_standard_combo.currentText() == original[0]
    assert all(window.session.reduction_cache[key] is result for key, result in products.items())
    assert all(window.session.pipeline.cache.get(stage) is result for stage, result in stages.items())


def test_unchecking_reveals_three_existing_pickers(window):
    window.same_primary_check.setChecked(False)
    assert not window.primary_separate_controls.isHidden()
    assert window.primary_standard_combo.isHidden()
    assert all(not combo.isHidden() for combo in (window.primary_68_combo, window.primary_76_combo,
                                                 window.primary_82_combo))
    assert window.change_guard.pending is None


def test_joining_different_primaries_cancels_and_undo_restores_layout(window):
    window.session.primary_76_standard = "91500"
    window._sync_fit_controls()
    original = primaries(window)
    cache_downhole(window)
    assert not window.same_primary_check.isChecked()
    window.same_primary_check.setChecked(True)
    assert window.change_guard.pending
    window.change_guard.cancel()
    assert primaries(window) == original
    assert not window.same_primary_check.isChecked()
    assert not window.primary_separate_controls.isHidden()
    window.same_primary_check.setChecked(True)
    window.change_guard.apply()
    assert len(set(primaries(window))) == 1
    window.change_guard.undo()
    assert primaries(window) == original
    assert not window.same_primary_check.isChecked()
    assert not window.primary_separate_controls.isHidden()


def test_secondary_menu_add_remove_and_immediate_undo(window):
    window.session.secondary_standards = ("91500",)
    window._sync_fit_controls()
    window._populate_material_menu("secondary")
    menu = window.secondary_add_menu
    names = [action.text() for action in menu.actions()]
    assert "PL" in names
    assert not {"S513", "NIST610", "NIST612", "91500"}.intersection(names)
    next(action for action in menu.actions() if action.text() == "PL").trigger()
    assert window.session.secondary_standards == ("91500", "PL")
    assert window.change_guard.pending is None
    assert window.change_guard.record is not None
    window._remove_calibration_material("secondary", "PL")
    assert window.session.secondary_standards == ("91500",)
    window.change_guard.undo()
    assert window.session.secondary_standards == ("91500", "PL")


def test_last_secondary_can_be_removed_and_restored(window):
    window.session.secondary_standards = ("91500",)
    window._sync_fit_controls()
    window._remove_calibration_material("secondary", "91500")
    assert window.session.secondary_standards == ()
    assert window.secondary_standards_edit.text() == ""
    assert window.secondary_materials.rows == ()
    window._sync_fit_controls()
    assert window.secondary_standards_edit.text() == ""
    window.change_guard.undo()
    assert window.session.secondary_standards == ("91500",)
    assert window.secondary_standards_edit.text() == "91500"


def test_choose_standard_can_replace_with_an_existing_glass(window):
    window.session.concentration_standards = ("NIST610", "NIST612", "S513")
    window._sync_fit_controls()
    window._populate_material_menu("concentration", replacing="S513")
    next(action for action in window.glass_add_menu.actions() if action.text() == "NIST610").trigger()
    assert window.session.concentration_standards == ("NIST610", "NIST612")
    window.change_guard.undo()
    assert window.session.concentration_standards == ("NIST610", "NIST612", "S513")


def test_internal_standard_menu_invalidates_only_normalization_branch(window):
    products, stages = cache_downhole(window)
    normalized, concentrations = object(), object()
    window.session.reduction_cache.update(normalized_plateau_data=normalized, concentration_results=concentrations)
    window.session.pipeline.cache.set(PipelineStage.NORMALIZATION, normalized)
    window.session.pipeline.cache.set(PipelineStage.CONCENTRATION, concentrations)
    baseline = services.ensure_baseline_subtracted(window.session)
    window._populate_internal_standard_menu()
    action = next(action for action in window.internal_standard_menu.actions() if "91Zr" in action.text())
    action.trigger()
    assert window.session.internal_standard_channel is None
    assert window.change_guard.pending
    window.change_guard.apply()
    assert window.session.internal_standard_channel == "91Zr"
    assert "normalized_plateau_data" not in window.session.reduction_cache
    assert "concentration_results" not in window.session.reduction_cache
    assert window.session.reduction_cache["baseline_subtracted_total_data"] is baseline
    for stage in (PipelineStage.DOWNHOLE, PipelineStage.SESSION):
        assert window.session.pipeline.cache.get(stage) is stages[stage]
    window.change_guard.undo()
    assert window.session.internal_standard_channel is None
    assert window.session.reduction_cache["normalized_plateau_data"] is normalized
    assert window.session.reduction_cache["concentration_results"] is concentrations


def test_state_line_reads_notices_banner_and_cache_without_computation(window, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("State line started reduction")

    monkeypatch.setattr(window, "run_background", forbidden)
    window._refresh_calibration_state()
    assert window.calibration_state_button.text() == "Up to date"
    notice = ReductionNotice("concentration", "missing_reference_value", "No P value for S513.",
                            "S513", "31P", ("choose_standard", "enter_value"))
    window.session.pipeline.notices.append(notice)
    window._refresh_workflow_status()
    assert window.calibration_state_button.text() == "Needs attention (1)"
    window.calibration_tabs.setCurrentIndex(1)
    window.calibration_state_button.click()
    assert window.calibration_tabs.currentIndex() == 0
    window._show_reduction_issue("Downhole", ValueError("Missing standard"))
    assert window.calibration_state_button.text() == "Needs attention (2)"
    window.issue_banner.dismiss_button.click()
    assert window.calibration_state_button.text() == "Needs attention (1)"
    window.session.pipeline.notices.clear()
    window._refresh_workflow_status()
    # Avoid workflow navigation: this also checks an uncached current stage.
    monkeypatch.setattr(window, "current_workflow_stage", lambda: PipelineStage.DOWNHOLE)
    window._refresh_calibration_state()
    assert window.calibration_state_button.text() == "Not computed yet"


def test_state_line_tracks_background_stage_progress(window, app):
    started, release = threading.Event(), threading.Event()
    final_order = window._calibration_required_stages(PipelineStage.FINALIZE)
    # Final review calculates isotope corrections before the concentration join.
    assert final_order.index(PipelineStage.SESSION) < final_order.index(PipelineStage.NORMALIZATION)

    def calculation():
        started.set()
        release.wait(10)

    try:
        window.run_background("Fitting session fractionation", calculation, lambda result: None)
        assert started.wait(3)
        stages = window._calibration_work_stages
        assert stages[-1] == PipelineStage.SESSION
        assert "Recomputing" in window.calibration_state_button.text()
        assert f"1 of {len(stages)}" in window.calibration_state_button.text()
        window.session.pipeline.cache.set(stages[0], object())
        window._refresh_calibration_state()
        assert f"2 of {len(stages)}" in window.calibration_state_button.text()
    finally:
        release.set()
        wait_idle(app, window)
    assert not window._calibration_state_timer.isActive()
    assert "Recomputing" not in window.calibration_state_button.text()


def test_new_picker_wheels_and_tab_switches_do_not_compute(window, app, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Switching Calibration tabs computed results")

    monkeypatch.setattr(window, "run_background", forbidden)
    for control in (window.primary_standard_combo, window.internal_standard_combo,
                    window.primary_68_combo, window.primary_76_combo, window.primary_82_combo):
        before = control.currentIndex()
        wheel(app, control)
        assert control.currentIndex() == before
    for index in range(3):
        window.calibration_tabs.setCurrentIndex(index)
        app.processEvents()
    assert [window.calibration_tabs.tabText(i) for i in range(3)] == ["Standards", "Fits", "Detector"]


def test_summaries_use_cached_products_and_retain_rows(window, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Calibration summary computed results")

    for name in ("run_downhole", "run_session", "run_concentration", "run_finalize"):
        monkeypatch.setattr(window.session.pipeline, name, forbidden)
    summary = pd.DataFrame([dict(sample="91500", offset_percent=1.8, mswd=1.3,
                               offset_from_accepted_age=18, weighted_mean_2e=2, accepted_age_2e=1)])
    window.session.reduction_cache["finalized_results"] = SimpleNamespace(secondary_standard_summary=summary)
    window._refresh_calibration_summary()
    row = next(row for row in window.secondary_materials.rows if row[0] == "91500")
    assert row[2] == "+1.8 % · MSWD 1.3"
    assert row[3] == "warn"
    assert row[4] == "Outside 2σ of the reference value"
    rows = window.secondary_materials.rows
    child = window.secondary_materials.grid.itemAt(0).widget()
    window._refresh_calibration_summary()
    assert window.secondary_materials.rows is rows
    assert window.secondary_materials.grid.itemAt(0).widget() is child


def assert_inside_viewport(scroll):
    viewport, page = scroll.viewport(), scroll.widget()
    outside = []
    for widget in [page, *page.findChildren(QtWidgets.QWidget)]:
        if widget.isWindow() or not widget.isVisibleTo(page):
            continue
        x = widget.mapTo(viewport, QtCore.QPoint()).x()
        if x < 0 or x + widget.width() > viewport.width():
            outside.append((type(widget).__name__, widget.objectName(), x, widget.width(), viewport.width()))
    assert not outside, outside


@pytest.fixture
def calibration_shell(window):
    from GUI.studio_window import StudioWindow

    shell = StudioWindow()
    shell.add_tab(window)
    yield shell
    shell.take_tab(window)
    shell.close()


@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_standards_fit_laptop_inspector(window, calibration_shell, app, size):
    shell = calibration_shell
    window.session.secondary_standards = ("91500", "Seiland", "LTSE", "PL")
    window.session.pipeline.notices.append(ReductionNotice(
        "concentration", "missing_reference_value", "No P value for S513: P not calibrated with it.",
        "S513", "31P", ("choose_standard", "enter_value")))
    summary = pd.DataFrame([dict(sample=name, offset_percent=-2.5, mswd=.72,
                               offset_from_accepted_age=-2.5, weighted_mean_2e=1, accepted_age_2e=.5)
                            for name in window.session.secondary_standards])
    window.session.reduction_cache["finalized_results"] = SimpleNamespace(secondary_standard_summary=summary)
    ratio = window.workflow_preset.default_ratio
    window.session.reduction_cache["downhole_results"] = {"correction_results": {ratio: {
        "metadata": {"fitted_fractionation": [1., 1.]}, "fit_residual_standard_deviation": .055}}}
    window.session.reduction_cache["session_correction_results"] = {"correction_results": {ratio: {
        "fit_observations": {"fractionation": [1., 1.059], "time": [0., 10440.]}, "fit_residuals": [0., 0.]}}}
    window._sync_fit_controls()
    window._refresh_workflow_status()
    shell.resize(*size)
    window.apply_default_layout()
    window.inspector_tabs.setCurrentWidget(window.calibration_tabs)
    shell.show()
    process(app)
    assert (shell.width(), shell.height()) == size
    # Cover both the default panel and the narrow inspector in the review shots.
    for inspector_width in (340, 260):
        sizes = window.workspace_splitter.sizes()
        window.workspace_splitter.setSizes([sizes[0], sum(sizes) - sizes[0] - inspector_width, inspector_width])
        for index in (0, 1, 2):
            window.calibration_tabs.setCurrentIndex(index)
            process(app)
            scroll = window.calibration_tabs.currentWidget()
            assert scroll.horizontalScrollBar().maximum() == 0
            assert not clipped_widgets(shell)
            if index in (0, 1):
                assert_inside_viewport(scroll)
            if index == 0:
                for label in window.secondary_materials.findChildren(QtWidgets.QLabel):
                    if label.objectName() == "SecondaryText":
                        assert QtWidgets.QLabel.text(label) == label.text(), (label.text(), label.size())
                for pill in window.glass_materials.findChildren(QtWidgets.QFrame, "CalibrationMaterialChip"):
                    for label in pill.findChildren(QtWidgets.QLabel):
                        assert QtWidgets.QLabel.text(label) == label.text(), (label.text(), label.size())
            if index == 0 and size == (1440, 900):
                assert scroll.verticalScrollBar().maximum() == 0, (scroll.widget().size(), scroll.viewport().size())


def test_informational_omissions_are_muted_without_attention(window):
    pipeline = window.session.pipeline
    pipeline.notices.clear()
    window._refresh_workflow_status()
    row = window.workflow_preset.stage_sequence.index(PipelineStage.CONCENTRATION)
    item = window.workflow_step_list.item(row)
    original_icon = item.icon().pixmap(16, 16).toImage()
    notice = ReductionNotice("concentration", "element_not_calibrated", "No Hg value in NIST610, NIST612: Hg not calibrated.",
                             "NIST610", "202Hg", ("choose_standard", "enter_value"), level="info")
    pipeline.notices.append(notice)
    window._refresh_workflow_status()
    assert window.calibration_state_button.text() == "Up to date"
    assert "Needs attention" not in item.text()
    assert item.icon().pixmap(16, 16).toImage() == original_icon
    labels = window.reduction_notices.widget().findChildren(QtWidgets.QLabel)
    assert any(label.text() == notice.message and label.objectName() == "SecondaryText" for label in labels)


@pytest.mark.parametrize("code", ["missing_reference_value", "missing_internal_value", "export_concentrations_unavailable"])
def test_reference_issues_still_need_attention(window, code):
    notice = ReductionNotice("concentration", code, "A reference value needs attention.")
    assert notice.level == "attention"
    window.session.pipeline.notices[:] = [notice]
    window._refresh_workflow_status()
    assert window.calibration_state_button.text() == "Needs attention (1)"
    row = window.workflow_preset.stage_sequence.index(PipelineStage.CONCENTRATION)
    assert "Needs attention" in window.workflow_step_list.item(row).text()


def test_element_count_deduplicates_hg_channels(window):
    channels = "7Li 9Be 11B 23Na 24Mg 27Al 29Si 31P 39K 44Ca 45Sc 49Ti 51V 52Cr 55Mn 56Fe 59Co 60Ni 63Cu 66Zn 85Rb 88Sr 89Y 91Zr 93Nb".split()
    window.session.channels = [*channels, "202Hg", "204Hg"]
    window.session.reduction_cache["concentration_results"] = {
        "calibration": pd.Series(1., index=[channel for channel in channels if channel != "29Si"])}
    window._refresh_calibration_summary()
    assert window.calibrated_elements_label.text() == "Elements: 24 calibrated · 1 not (Hg)"
    assert window.internal_standard_label.text() == "Si · 151 443 µg/g"
    assert not window.internal_standard_label.wordWrap()


def test_secondary_two_lines_and_glass_pills(window, app):
    summary = pd.DataFrame([dict(sample="91500", offset_percent=.1, mswd=.62),
                            dict(sample="PL", offset_percent=-2.5, mswd=.72,
                                 offset_from_accepted_age=-2.5, weighted_mean_2e=.5)])
    window.session.secondary_standards = ("91500", "PL")
    window.session.reduction_cache["finalized_results"] = SimpleNamespace(secondary_standard_summary=summary)
    window._refresh_calibration_summary()
    assert window.secondary_materials.rows[0][3] == "ok"
    assert window.secondary_materials.rows[1][2] == "−2.5 % · MSWD 0.72"
    assert window.secondary_materials.grid.itemAt(1).widget().toolTip() == "Outside 2σ of the reference value"
    first = window.secondary_materials.grid.itemAt(0).widget()
    labels = first.findChildren(QtWidgets.QLabel)
    assert any(label.accessibleName() == "Within 2σ" and not label.pixmap().isNull() for label in labels)
    assert any(label.text() == "91500" for label in labels)
    assert any(label.text() == "2 an." for label in labels)
    assert any(label.text() == "+0.1 % · MSWD 0.62" and label.objectName() == "SecondaryText" for label in labels)
    pills = window.glass_materials.findChildren(QtWidgets.QFrame, "CalibrationMaterialChip")
    assert len(pills) == 2
    for pill in pills:
        assert len(pill.findChildren(QtWidgets.QToolButton)) == 1
        assert pill.findChildren(QtWidgets.QToolButton)[0].text() == "×"
    assert window.glass_add_button.parentWidget() is window.glass_materials
    layout = window.glass_materials.grid
    assert layout.heightForWidth(100) > layout.heightForWidth(300)


def test_91500_offset_uses_cached_same_system_summary(window):
    from UPbRPy.standards import standard_archive

    assert pd.isna(standard_archive.loc["91500", "concordia_age"])
    assert standard_archive.loc["91500", "206Pb/238U*"] > 0
    window.session.secondary_standards = ("91500",)
    legacy = pd.DataFrame([dict(sample="91500", weighted_mean=1063.5, weighted_mean_2e=2.,
                                accepted_age=float("nan"), offset_percent=float("nan"), mswd=.62)])
    by_system = pd.DataFrame([
        dict(sample="91500", system="206Pb/238U", accepted_date=1062.438686,
             weighted_mean=1063.5, weighted_mean_2e=2., offset_ma=1.061314,
             offset_percent=.099894, mswd=.62),
        dict(sample="91500", system="207Pb/206Pb", offset_percent=5., mswd=10.),
    ])
    window.session.reduction_cache["finalized_results"] = SimpleNamespace(
        secondary_standard_summary=legacy, reference_material_summary_by_system=by_system)
    window._refresh_calibration_summary()
    row = window.secondary_materials.rows[0]
    assert row[2] == "+0.1 % · MSWD 0.62"
    assert row[3] == "ok"
    assert row[4] == "206Pb/238U dates. Within 2σ of the reference value"
    assert window.secondary_materials.grid.itemAt(0).widget().toolTip() == row[4]


@pytest.mark.parametrize("accepted,mean,reason", [
    (float("nan"), 1063.5, "no accepted reference age"),
    (1062.4, float("nan"), "no finite weighted mean"),
])
def test_unavailable_secondary_offset_explains_why(window, accepted, mean, reason):
    window.session.secondary_standards = ("91500",)
    window.session.reduction_cache["finalized_results"] = SimpleNamespace(
        secondary_standard_summary=pd.DataFrame([dict(sample="91500", accepted_age=accepted,
            weighted_mean=mean, offset_percent=float("nan"), mswd=.62)]))
    window._refresh_calibration_summary()
    row = window.secondary_materials.grid.itemAt(0).widget()
    assert "Offset unavailable" in row.toolTip() and reason in row.toolTip()


def test_notice_actions_share_one_compact_row(window, app):
    notice = ReductionNotice("concentration", "missing_reference_value", "No P value for S513.",
                             "S513", "31P", ("choose_standard", "enter_value"))
    window.session.pipeline.notices[:] = [notice]
    window._refresh_workflow_status()
    window.inspector_tabs.setCurrentWidget(window.calibration_tabs)
    window.show()
    process(app)
    buttons = window.reduction_notices.findChildren(QtWidgets.QToolButton)
    assert [button.text() for button in buttons] == ["Choose standard", "Enter value"]
    assert buttons[0].y() == buttons[1].y()
    assert buttons[0].width() + buttons[1].width() < window.reduction_notices.width()
