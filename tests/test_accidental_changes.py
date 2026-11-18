"""Wheel gestures and destructive settings preserve reviewed reductions."""

import os
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from GUI import main_window, services
from GUI.main_window import MainWindow
from GUI.theme import build_application
from GUI.wheel_guard import install_wheel_guard
from UPbRPy.pipeline import PipelineStage


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"


@pytest.fixture(scope="module")
def app():
    application = QtWidgets.QApplication.instance() or build_application([], "light")
    install_wheel_guard(application)
    return application


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args))
    window = MainWindow()
    window.load_experiment_directory(str(RICH), background=False, offer_restore=False)
    yield window
    wait_idle(app, window)
    for timer in (window._selection_edit_timer, window._selection_recompute_timer,
                  window._selection_autosave_timer):
        timer.stop()
    window.has_unsaved_changes = lambda: False
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not errors


def wait_idle(app, window):
    deadline = time.monotonic() + 15
    while window._active_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert window._active_worker is None


def wheel(app, widget, delta=-120):
    local = QtCore.QPointF(widget.rect().center())
    event = QtGui.QWheelEvent(local, QtCore.QPointF(widget.mapToGlobal(local.toPoint())),
                             QtCore.QPoint(), QtCore.QPoint(0, delta),
                             QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
                             QtCore.Qt.ScrollPhase.NoScrollPhase, False)
    app.sendEvent(widget, event)


def cache_downhole(window):
    products = {key: object() for key in (
        "downhole_results", "session_correction_results", "spot_mean_results",
        "depth_profile_results", "finalized_results", "results_pb204", "results_pb204_source",
    )}
    window.session.reduction_cache.update(products)
    stages = {stage: object() for stage in (PipelineStage.DOWNHOLE, PipelineStage.SESSION,
                                          PipelineStage.SPOT_MEANS, PipelineStage.DEPTH_PROFILE,
                                          PipelineStage.FINALIZE, PipelineStage.EXPORT)}
    for stage, result in stages.items():
        window.session.pipeline.cache.set(stage, result)
    return products, stages


def review_window(window):
    spot = window.session.spot_names[0]
    start, end = services.get_selection_regions(window.session, spot)["plateau"]
    services.update_selection_region(window.session, spot, plateau=(start + 0.2, end - 0.2), manual=True)
    return spot


def test_wheel_leaves_every_studio_combo_spin_and_child_unchanged(window, app):
    assert install_wheel_guard(app) is install_wheel_guard(app)
    controls = [widget for widget in window.findChildren(QtWidgets.QWidget)
                if isinstance(widget, (QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QSlider))]
    assert window.primary_channel_combo in controls
    assert window.primary_68_combo in controls
    assert window.downhole_outlier_sigma_spin in controls
    for focused in (False, True):
        for widget in controls:
            if focused:
                widget.setFocus()
            if isinstance(widget, QtWidgets.QComboBox):
                before = widget.currentIndex()
                wheel(app, widget)
                assert widget.currentIndex() == before
            else:
                before = widget.value()
                wheel(app, widget)
                assert widget.value() == before
                if isinstance(widget, QtWidgets.QAbstractSpinBox):
                    wheel(app, widget.lineEdit())
                    assert widget.value() == before
    assert window.change_guard.pending is None


@pytest.mark.parametrize("kind", ["combo", "spin", "date", "slider"])
def test_wheel_scrolls_enclosing_area(app, kind):
    area = QtWidgets.QScrollArea()
    body = QtWidgets.QWidget()
    body.setMinimumSize(250, 1600)
    layout = QtWidgets.QVBoxLayout(body)
    if kind == "combo":
        control = QtWidgets.QComboBox()
        control.addItems(["first", "second"])
    elif kind == "spin":
        control = QtWidgets.QDoubleSpinBox()
    elif kind == "date":
        control = QtWidgets.QDateEdit()
    else:
        control = QtWidgets.QSlider()
    layout.addWidget(control)
    layout.addStretch()
    area.setWidget(body)
    area.resize(300, 200)
    area.show()
    app.processEvents()
    before = area.verticalScrollBar().value()
    wheel(app, control.lineEdit() if kind in {"spin", "date"} else control)
    assert area.verticalScrollBar().value() > before
    area.close()


def test_open_combo_popup_scrolls_and_keyboard_still_selects(app):
    combo = QtWidgets.QComboBox()
    combo.addItems([f"Channel {i}" for i in range(100)])
    combo.setMaxVisibleItems(5)
    combo.show()
    app.processEvents()
    combo.showPopup()
    app.processEvents()
    view = combo.view()
    before = view.verticalScrollBar().value()
    wheel(app, view.viewport(), -720)
    assert view.verticalScrollBar().value() > before
    combo.hidePopup()
    QtTest.QTest.keyClick(combo, QtCore.Qt.Key.Key_Down)
    assert combo.currentIndex() == 1
    combo.close()


def test_primary_standard_waits_cancel_and_undo_restore_identity(window, app):
    session = window.session
    old = session.primary_68_standard
    products, stages = cache_downhole(window)
    retained = object()
    session.reduction_cache["plateau_data_with_raw_ratios"] = retained
    session.pipeline.cache.set(PipelineStage.RATIOS, retained)
    proposed = "S513" if old != "S513" else "91500"
    window.primary_68_combo.setCurrentText(proposed)
    bar = window.pending_change_bar
    assert not bar.isHidden()
    assert "Pb/U primary" in bar.message.text()
    assert "recomputes downhole" in bar.message.text()
    assert session.primary_68_standard == old
    assert session.reduction_cache["downhole_results"] is products["downhole_results"]
    bar.cancel_button.click()
    assert window.primary_68_combo.currentText() == old
    assert session.primary_68_standard == old
    assert session.reduction_cache["downhole_results"] is products["downhole_results"]
    window.primary_68_combo.setCurrentText(proposed)
    bar.apply_button.click()
    assert session.primary_68_standard == proposed
    assert "downhole_results" not in session.reduction_cache
    assert not session.pipeline.cache.has(PipelineStage.DOWNHOLE)
    assert session.reduction_cache["plateau_data_with_raw_ratios"] is retained
    assert session.pipeline.cache.get(PipelineStage.RATIOS) is retained
    assert not bar.undo_button.isHidden()
    bar.undo_button.click()
    assert window.primary_68_combo.currentText() == old
    assert session.primary_68_standard == old
    assert all(session.reduction_cache[key] is result for key, result in products.items())
    assert all(session.pipeline.cache.get(stage) is result for stage, result in stages.items())
    assert window.change_guard.record is None


def test_escape_cancels_and_restores_value(window, app):
    cache_downhole(window)
    old = window.session.primary_76_standard
    window.show()
    window.inspector_tabs.setCurrentWidget(window.calibration_tabs)
    app.processEvents()
    window.primary_76_combo.setCurrentText("S513" if old != "S513" else "91500")
    assert window.change_guard.pending
    window.activateWindow()
    app.processEvents()
    QtTest.QTest.keyClick(window.primary_76_combo, QtCore.Qt.Key.Key_Escape)
    assert window.change_guard.pending is None
    assert window.primary_76_combo.currentText() == old


def test_primary_channel_reviews_then_undo_reinstalls_old_session(window, app):
    spot = review_window(window)
    old = window.session
    old_channel = old.primary_channel
    previous_regions = services.get_selection_regions(old, spot)
    history = [({"before": 1}, {"after": 2})]
    window._selection_undo = list(history)
    proposed = next(channel for channel in old.channels if channel != old_channel)
    window.primary_channel_combo.setCurrentText(proposed)
    assert window.session is old
    assert window._active_worker is None
    assert "all 12 spots" in window.pending_change_bar.message.text()
    assert "1 reviewed windows" in window.pending_change_bar.message.text()
    window.pending_change_bar.apply_button.click()
    wait_idle(app, window)
    assert window.session is not old
    assert window.session.primary_channel == proposed
    window.pending_change_bar.undo_button.click()
    assert window.session is old
    assert window.primary_channel_combo.currentText() == old_channel
    assert window._selection_undo == history
    assert services.get_selection_regions(window.session, spot) == previous_regions


def test_primary_channel_failed_worker_keeps_session(window, app, monkeypatch):
    review_window(window)
    old = window.session
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args))
    def fail(*args, **kwargs):
        raise ValueError("synthetic reload failure")
    monkeypatch.setattr(main_window, "load_session", fail)
    proposed = next(channel for channel in old.channels if channel != old.primary_channel)
    window.primary_channel_combo.setCurrentText(proposed)
    window.pending_change_bar.apply_button.click()
    wait_idle(app, window)
    assert not errors
    assert not window.issue_banner.isHidden()
    assert "synthetic reload failure" in window.issue_banner.message.text()
    assert window.session is old
    assert window.primary_channel_combo.currentText() == old.primary_channel
    assert window.change_guard.record is None


def test_repick_button_applies_directly_with_selection_undo(window, app):
    spot = review_window(window)
    regions = services.get_selection_regions(window.session, spot)
    window.repick_overwrite_manual_check.setChecked(True)
    window.repick_all_spots_button.click()
    assert window.change_guard.pending is None
    wait_idle(app, window)
    assert services.get_selection_regions(window.session, spot) != regions
    window.undo_selection_edit()
    assert services.get_selection_regions(window.session, spot) == regions
    window.redo_selection_edit()
    assert services.get_selection_regions(window.session, spot) != regions


def test_repick_still_skips_manual_windows_without_overwrite(window, app):
    spot = review_window(window)
    regions = services.get_selection_regions(window.session, spot)
    window.repick_overwrite_manual_check.setChecked(False)
    window.repick_all_spots_button.click()
    assert window.change_guard.pending is None
    wait_idle(app, window)
    assert services.get_selection_regions(window.session, spot) == regions


@pytest.mark.parametrize("name,stage", [
    ("session_fit_combo", PipelineStage.SESSION),
    ("downhole_fit_combo", PipelineStage.DOWNHOLE),
    ("ratio_aggregation_combo", PipelineStage.DOWNHOLE),
])
def test_other_setting_combos_confirm_without_recomputing(window, name, stage, monkeypatch):
    products, stages = cache_downhole(window)
    monkeypatch.setattr(main_window, "ensure_downhole_results", lambda *a, **k: pytest.fail("Recomputed on setting change"))
    combo = getattr(window, name)
    before = combo.currentIndex()
    combo.setCurrentIndex((before + 1) % combo.count())
    assert window.change_guard.pending
    assert window.session.pipeline.cache.get(stage) is stages[stage]
    window.pending_change_bar.apply_button.click()
    assert not window.session.pipeline.cache.has(stage)
    window.pending_change_bar.undo_button.click()
    assert combo.currentIndex() == before
    assert window.session.pipeline.cache.get(stage) is stages[stage]


@pytest.mark.parametrize("name", ["workflow_combo", "selection_method_combo"])
def test_workflow_and_selection_method_undo_keep_old_session(window, name):
    old = window.session
    products, stages = cache_downhole(window)
    combo = getattr(window, name)
    before = combo.currentIndex()
    combo.setCurrentIndex((before + 1) % combo.count())
    assert window.change_guard.pending
    assert window.session is old
    window.pending_change_bar.apply_button.click()
    assert window.session is not old
    assert old.reduction_cache["downhole_results"] is products["downhole_results"]
    window.pending_change_bar.undo_button.click()
    assert window.session is old
    assert combo.currentIndex() == before
    assert all(old.pipeline.cache.get(stage) is result for stage, result in stages.items())


@pytest.mark.parametrize("name,key", [
    ("concentration_standards_edit", "concentration_results"),
])
def test_standard_fields_confirm_cancel_and_restore_cached_identity(window, name, key):
    edit = getattr(window, name)
    old_text = edit.text()
    product = object()
    window.session.reduction_cache[key] = product
    edit.setText("NIST610" if name.startswith("concentration") else "S513")
    edit.editingFinished.emit()
    assert window.change_guard.pending
    assert window.session.reduction_cache[key] is product
    window.pending_change_bar.cancel_button.click()
    assert edit.text() == old_text
    edit.setText("NIST610" if name.startswith("concentration") else "S513")
    edit.editingFinished.emit()
    window.pending_change_bar.apply_button.click()
    assert key not in window.session.reduction_cache
    window.pending_change_bar.undo_button.click()
    assert edit.text() == old_text
    assert window.session.reduction_cache[key] is product


def test_unfinished_text_is_not_accepted_by_another_control(window):
    edit = window.concentration_standards_edit
    old = edit.text()
    edit.setText("NIST610")
    window.primary_76_combo.setCurrentText("S513" if window.session.primary_76_standard != "S513" else "91500")
    assert window.change_guard.values["concentration_standards_edit"] == old
    product = object()
    window.session.reduction_cache["concentration_results"] = product
    edit.editingFinished.emit()
    assert window.change_guard.pending
    window.pending_change_bar.cancel_button.click()
    assert edit.text() == old
    assert window.session.reduction_cache["concentration_results"] is product


def test_session_fit_without_session_results_applies_directly(window):
    downhole = object()
    window.session.reduction_cache["downhole_results"] = downhole
    window.session.pipeline.cache.set(PipelineStage.DOWNHOLE, downhole)
    combo = window.session_fit_combo
    combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
    assert window.change_guard.pending is None
    assert window.session.reduction_cache["downhole_results"] is downhole
    assert window.session.pipeline.cache.get(PipelineStage.DOWNHOLE) is downhole


def test_reference_library_undo_restores_archive_and_old_session(window, monkeypatch):
    old_session = window.session
    cache_downhole(window)
    archive = services.current_standard_archive()
    archive_path = services.current_standard_archive_path()
    updated = archive.copy()
    def load(path):
        services._active_standard_archive = updated
        services._active_standard_archive_path = path
        return updated
    monkeypatch.setattr(main_window, "load_standard_archive_from_path", load)
    monkeypatch.setattr(window, "_update_reference_library_labels", lambda: None)
    window.request_reference_material_library("/tmp/example-library.toml")
    assert window.change_guard.pending
    assert services.current_standard_archive() is archive
    window.pending_change_bar.apply_button.click()
    assert services.current_standard_archive() is updated
    window.pending_change_bar.undo_button.click()
    assert services.current_standard_archive() is archive
    assert services.current_standard_archive_path() == archive_path
    assert window.session is old_session


def test_repick_worker_failure_does_not_install_modified_selections(window, app, monkeypatch):
    spot = review_window(window)
    old = window.session
    before = services.get_selection_regions(old, spot)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args))
    def fail(session, *args, **kwargs):
        session.experiment.loc[:, "plateau_start_times"] = 0.0
        raise ValueError("synthetic re-pick failure")
    monkeypatch.setattr(main_window, "autopick_all_selection_regions", fail)
    window.repick_overwrite_manual_check.setChecked(True)
    window.repick_all_spots_button.click()
    assert window.change_guard.pending is None
    wait_idle(app, window)
    assert not errors
    assert not window.issue_banner.isHidden()
    assert "synthetic re-pick failure" in window.issue_banner.message.text()
    assert window.session is old
    assert services.get_selection_regions(old, spot) == before


def test_installing_another_session_releases_previous_undo(window):
    combo = window.session_fit_combo
    combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
    assert window.change_guard.record
    replacement = main_window.copy_session(window.session)
    window.install_loaded_session(replacement, replacement.directory)
    assert window.change_guard.record is None
    assert window.session is replacement


def test_remove_reviewed_spot_is_direct_with_selection_undo(window):
    spot = review_window(window)
    regions = services.get_selection_regions(window.session, spot)
    window._spot_folder_overrides[spot] = "Reviewed"
    window._review_flags[spot] = "Check this spot"
    window.request_remove_spot(spot)
    assert window.change_guard.pending is None
    assert spot not in window.session.spot_names
    # Selection Undo must preserve settings accepted after the removal.
    window.on_selection_threshold_percent_changed(110)
    window.undo_selection_edit()
    assert window.session.import_settings["threshold"] == 1.1
    assert spot in window.session.spot_names
    assert services.get_selection_regions(window.session, spot) == regions
    assert window._spot_folder_overrides[spot] == "Reviewed"
    assert window._review_flags[spot] == "Check this spot"
    window.redo_selection_edit()
    assert spot not in window.session.spot_names
    assert spot not in window._spot_folder_overrides


@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_menu_change_reveals_inline_confirmation_on_laptop(window, app, size):
    window.resize(*size)
    window.show()
    window.apply_default_layout()
    cache_downhole(window)
    window.autopick_settings_toggle.setChecked(False)
    combo = window.selection_method_combo
    combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
    app.processEvents()
    bar = window.pending_change_bar
    assert bar.isVisible()
    assert window.compact_settings_panel.isVisible()
    rect = QtCore.QRect(bar.mapTo(window, QtCore.QPoint()), bar.size())
    assert window.rect().contains(rect)


@pytest.mark.parametrize("name", [
    "secondary_standards_edit", "common_pb_method_combo", "common_pb_uncertainty_combo",
    "calibration_uncertainty_check", "age_transition_spin",
    "selection_threshold_percent_spin", "selection_reference_percentile_spin",
    "selection_sigma_spin", "plateau_duration_spin", "plateau_filter_combo",
])
def test_reversible_settings_apply_immediately_and_undo_cached_identity(window, name, monkeypatch):
    products, stages = cache_downhole(window)
    monkeypatch.setattr(main_window, "ensure_downhole_results",
                        lambda *a, **k: pytest.fail("Recomputed on setting change"))
    widget = getattr(window, name)
    if isinstance(widget, QtWidgets.QComboBox):
        before = widget.currentIndex()
        widget.setCurrentIndex((before + 1) % widget.count())
        current_value = widget.currentIndex
    elif isinstance(widget, QtWidgets.QAbstractSpinBox):
        before = widget.value()
        widget.setValue(before + widget.singleStep())
        current_value = widget.value
    elif isinstance(widget, QtWidgets.QCheckBox):
        before = widget.isChecked()
        widget.setChecked(not before)
        current_value = widget.isChecked
    else:
        before = widget.text()
        widget.setText("S513" if before != "S513" else "91500")
        widget.editingFinished.emit()
        current_value = widget.text
    assert window.change_guard.pending is None
    assert window.change_guard.record is not None
    assert window.pending_change_bar.undo_button.isVisibleTo(window.pending_change_bar)
    assert "Changed" in window.pending_change_bar.message.text()
    assert "finalized_results" not in window.session.reduction_cache
    assert not window.session.pipeline.cache.has(PipelineStage.FINALIZE)
    if name.startswith("selection_") or name in ("plateau_duration_spin", "plateau_filter_combo"):
        assert "downhole_results" not in window.session.reduction_cache
    else:
        assert window.session.reduction_cache["downhole_results"] is products["downhole_results"]
    window.pending_change_bar.undo_button.click()
    assert current_value() == before
    assert all(window.session.reduction_cache[key] is result for key, result in products.items())
    assert all(window.session.pipeline.cache.get(stage) is result for stage, result in stages.items())


def test_early_setting_with_only_final_results_applies_with_undo(window):
    product = object()
    window.session.reduction_cache["finalized_results"] = product
    combo = window.downhole_fit_combo
    before = combo.currentIndex()
    combo.setCurrentIndex((before + 1) % combo.count())
    assert window.change_guard.pending is None
    assert "finalized_results" not in window.session.reduction_cache
    window.pending_change_bar.undo_button.click()
    assert combo.currentIndex() == before
    assert window.session.reduction_cache["finalized_results"] is product
