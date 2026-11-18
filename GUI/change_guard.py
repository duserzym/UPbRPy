"""Confirm destructive UI settings and retain one reversible change."""

from copy import copy, deepcopy
from dataclasses import fields
from functools import wraps
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from GUI import services
from GUI.services import _REDUCTION_CACHE_STAGES, manual_selection_spots, pipeline_config_from_session
from GUI.state import ReductionSession
from GUI.widgets import PendingChangeBar
from UPbRPy.pipeline import PipelineCache, PipelineStage, pipeline_descendants


def confirm_change(label, control, *stages, replace_session=False, review=False, immediate=False):
    """Guard a MainWindow handler that discards the specified stage products."""
    def decorate(function):
        @wraps(function)
        def guarded(window, *args, **kwargs):
            guard = getattr(window, "change_guard", None)
            if guard is None or window._is_populating or guard.running:
                return function(window, *args, **kwargs)
            return guard.request(function, label, control, stages, replace_session, review, immediate, args, kwargs)
        guarded.change_control = control
        return guarded
    return decorate


def sync_change_controls(function):
    """Remember accepted widget values after a programmatic UI population."""
    @wraps(function)
    def synchronized(window, *args, **kwargs):
        result = function(window, *args, **kwargs)
        guard = getattr(window, "change_guard", None)
        if guard is not None:
            groups = {
                "_sync_selection_parameter_controls": "selection_threshold_percent_spin selection_reference_percentile_spin selection_sigma_spin plateau_duration_spin",
                "_sync_plateau_filter_control": "plateau_filter_combo",
                "_populate_selectors": "primary_channel_combo",
                "_populate_workflow_ui": "workflow_combo selection_method_combo",
                "populate_standard_controls": "primary_68_combo primary_76_combo primary_82_combo primary_standard_combo same_primary_check internal_standard_combo concentration_standards_edit secondary_standards_edit",
            }
            names = groups.get(function.__name__)
            if function.__name__ == "_sync_fit_controls":
                names = " ".join(name for name in guard.controls if name not in {
                    "workflow_combo", "selection_method_combo", "primary_channel_combo", "plateau_filter_combo",
                    "selection_threshold_percent_spin", "selection_reference_percentile_spin", "selection_sigma_spin", "plateau_duration_spin",
                    "repick_all_spots_button", "detector_calibration_panel",
                })
            guard.sync(names.split() if names else None)
        return result
    return synchronized


def copy_session(session):
    """Copy mutable settings and selections, sharing sweep tables and results."""
    updated = copy(session)
    for name in ("import_settings", "downhole_fit_options", "session_fit_options"):
        setattr(updated, name, deepcopy(getattr(session, name)))
    updated.experiment = session.experiment.copy(deep=True)
    updated.reduction_cache = dict(session.reduction_cache)
    if session.pipeline is not None:
        updated.pipeline = copy(session.pipeline)
        updated.pipeline.notices = list(session.pipeline.notices)
        updated.pipeline.cache = PipelineCache()
        for stage, result in session.pipeline.cache.as_mapping().items():
            updated.pipeline.cache.set(stage, result)
    return updated


def _value(widget):
    if isinstance(widget, QtWidgets.QComboBox):
        return widget.currentData(), widget.currentText()
    if isinstance(widget, QtWidgets.QAbstractSpinBox):
        return widget.value()
    if isinstance(widget, QtWidgets.QAbstractButton):
        return widget.isChecked()
    if isinstance(widget, QtWidgets.QLineEdit):
        return widget.text()
    return None


def _set_value(widget, value):
    with QtCore.QSignalBlocker(widget):
        if isinstance(widget, QtWidgets.QComboBox):
            data, text = value
            index = widget.findData(data) if data is not None else widget.findText(text)
            if index < 0 and text:
                widget.addItem(text, data)
                index = widget.count() - 1
            widget.setCurrentIndex(index)
        elif isinstance(widget, QtWidgets.QAbstractSpinBox):
            widget.setValue(value)
        elif isinstance(widget, QtWidgets.QAbstractButton):
            widget.setChecked(value)
        elif isinstance(widget, QtWidgets.QLineEdit):
            widget.setText(value)


class ChangeGuard:
    """Own one pending change or one undo record for a reduction window."""

    def __init__(self, window):
        self.window = window
        self.bar = PendingChangeBar(window)
        window.pending_change_bar = self.bar
        self.bar.apply_requested.connect(self.apply)
        self.bar.cancel_requested.connect(self.cancel)
        self.bar.undo_requested.connect(self.undo)
        self.escape = QtGui.QShortcut(QtGui.QKeySequence("Escape"), window)
        self.escape.activated.connect(self.cancel)
        self.escape.setEnabled(False)
        self.pending = None
        self.record = None
        self.transaction = None
        self.running = False
        self.controls = set()
        for name in dir(type(window)):
            control = getattr(getattr(type(window), name), "change_control", None)
            if control:
                self.controls.update(control.split())
        self.values = {}
        self.sync()

    def sync(self, names=None):
        if self.running or self.pending is not None:
            return
        for name in self.controls if names is None else names:
            value = _value(getattr(self.window, name))
            if name in {"concentration_standards_edit", "secondary_standards_edit"}:
                attribute = name.removesuffix("_edit")
                accepted = getattr(self.window.session, attribute)
                if not self.window.session.loaded:
                    accepted = accepted or getattr(self.window.workflow_preset, attribute)
                if self._standard_names(value) != accepted:
                    # Typing in one field must not be accepted by an unrelated change.
                    value = self.values.get(name, ", ".join(accepted))
            self.values[name] = value

    @staticmethod
    def _standard_names(text):
        return tuple(part.strip() for part in text.split(",") if part.strip())

    def clear(self):
        """Release the retained results when unrelated scientific work changes."""
        if self.running:
            return
        self.cancel()
        self.record = None
        self.bar.hide()

    def _place(self, control):
        widget = getattr(self.window, control.split()[0])
        calibration = self.window.calibration_tabs
        is_calibration = calibration.isAncestorOf(widget)
        parent = calibration if is_calibration else widget.parentWidget()
        while parent is not None and parent.layout() is None:
            parent = parent.parentWidget()
        if parent is None:
            return
        old_parent = self.bar.parentWidget()
        if old_parent and old_parent.layout():
            old_layout = (self.window.calibration_pending_layout if old_parent is calibration
                          else old_parent.layout())
            if isinstance(old_layout, QtWidgets.QFormLayout):
                old_layout.takeRow(self.bar)
            else:
                old_layout.removeWidget(self.bar)
        self.bar.setParent(parent)
        layout = self.window.calibration_pending_layout if is_calibration else parent.layout()
        if is_calibration:
            layout.insertWidget(1, self.bar)
            return
        if isinstance(layout, QtWidgets.QFormLayout):
            layout.addRow(self.bar)
        elif isinstance(layout, QtWidgets.QGridLayout):
            layout.addWidget(self.bar, layout.rowCount(), 0, 1, layout.columnCount())
        else:
            layout.addWidget(self.bar)

    def _reveal(self):
        """Make confirmations reached through menus and shortcuts visible too."""
        w = self.window
        if w.inspector_panel.isAncestorOf(self.bar):
            w.set_workspace_panel_visible("inspector", True)
        elif w.sidebar_panel.isAncestorOf(self.bar):
            w.set_workspace_panel_visible("sidebar", True)
        for tabs in w.findChildren(QtWidgets.QTabWidget):
            for index in range(tabs.count()):
                page = tabs.widget(index)
                if page is self.bar or page.isAncestorOf(self.bar):
                    tabs.setCurrentIndex(index)
                    break
        if w.compact_settings_panel.isAncestorOf(self.bar):
            w.autopick_settings_toggle.setChecked(True)
            w._position_compact_panels()
        parent = self.bar.parentWidget()
        while parent is not None:
            if parent.layout():
                parent.layout().activate()
            if isinstance(parent, QtWidgets.QScrollArea):
                parent.ensureWidgetVisible(self.bar, 8, 8)
            parent = parent.parentWidget()

    def request(self, function, label, control, stages, replace_session, review, immediate, args, kwargs):
        w = self.window
        if w._active_worker is not None:
            for name in control.split():
                _set_value(getattr(w, name), self.values[name])
            w.statusBar().showMessage(f"Please wait for {w._busy_label.lower()} to finish.")
            return
        # Direct handler calls and menu actions use the same path as widget signals.
        names = control.split()
        widget = getattr(w, names[0])
        if len(names) == 1 and args and not isinstance(widget, QtWidgets.QPushButton):
            if isinstance(widget, QtWidgets.QComboBox):
                with QtCore.QSignalBlocker(widget):
                    if isinstance(args[0], str):
                        _set_value(widget, (None, args[0]))
                    elif isinstance(args[0], int):
                        widget.setCurrentIndex(args[0])
            elif isinstance(widget, (QtWidgets.QAbstractSpinBox, QtWidgets.QCheckBox)):
                _set_value(widget, args[0])
        proposed = {name: _value(getattr(w, name)) for name in names}
        if function.__name__ == "on_concentration_standards_changed" and not self._standard_names(proposed[names[0]]):
            return function(w, *args, **kwargs)
        if self.pending is not None:
            if self.pending["function"] is function and self.pending["proposed"] == proposed and self.pending["args"] == args:
                return
            self.cancel()
            for name, value in proposed.items():
                _set_value(getattr(w, name), value)
        if all(value == self.values.get(name) for name, value in proposed.items()) and not isinstance(widget, QtWidgets.QPushButton) and proposed[names[0]] is not None and function.__name__ != "reload_current_directory":
            return
        if isinstance(widget, QtWidgets.QLineEdit) and self._standard_names(proposed[names[0]]) == self._standard_names(self.values[names[0]]):
            self.values[names[0]] = proposed[names[0]]
            return
        w.flush_selection_edits()
        targets = set()
        for stage in stages:
            resolved = PipelineStage(stage)
            targets.update((resolved, *pipeline_descendants(resolved)))
        cache_keys = {key for key, stage in _REDUCTION_CACHE_STAGES.items() if stage in targets}
        if PipelineStage.BASELINE in targets:
            cache_keys.update(w.session.reduction_cache)
        # Final decisions and depth profiles are cheap, reversible changes.
        # Only discarded products at session correction or earlier warrant a pause.
        expensive_targets = targets - {PipelineStage.SPOT_MEANS, PipelineStage.DEPTH_PROFILE,
                                       PipelineStage.FINALIZE, PipelineStage.EXPORT}
        expensive_keys = {key for key in cache_keys if _REDUCTION_CACHE_STAGES.get(key) in expensive_targets}
        has_results = w.session.loaded and (bool(expensive_keys.intersection(w.session.reduction_cache)) or bool(
            w.session.pipeline and expensive_targets.intersection(w.session.pipeline.cache.stages())
        ))
        reviewed_spots = manual_selection_spots(w.session) | set(w.pending_region_updates)
        reviewed = len(reviewed_spots)
        old = {name: self.values.get(name) for name in names}
        self.pending = dict(function=function, label=label, control=control, args=args, kwargs=kwargs,
                            proposed=proposed, old=old, targets=targets, cache_keys=cache_keys,
                            replace_session=replace_session)
        self._place(control)
        if not immediate and (has_results or (review and reviewed) or (replace_session and w.session.loaded)):
            before = self._display(old[names[0]])
            after = self._display(proposed[names[0]])
            change = f"{label} {before} → {after}" if before != after else label
            if function.__name__ == "request_reference_material_library":
                change += f" → {Path(args[0]).name}"
            if review:
                count = len(w.session.spot_names)
                effect = f"re-picks all {count} spots; replaces {reviewed} reviewed windows"
            else:
                if PipelineStage.BASELINE in targets:
                    effect = "discards computed reduction results"
                elif PipelineStage.DOWNHOLE in targets:
                    effect = "recomputes downhole, session and final results"
                elif PipelineStage.SESSION in targets:
                    effect = "recomputes session and later results"
                elif PipelineStage.CONCENTRATION in targets:
                    effect = "recomputes concentration, spot means and final results"
                elif PipelineStage.DEPTH_PROFILE in targets:
                    effect = "recomputes depth profiles and final results"
                else:
                    effect = "recomputes final results"
                if function.__name__ == "request_detector_calibration":
                    effect = "re-imports signals and discards computed results; preserves reviewed windows"
            self.bar.show_pending(f"{change}: {effect}.")
            self.escape.setEnabled(True)
            self._reveal()
        else:
            self.apply()

    @staticmethod
    def _display(value):
        return str(value[1] if isinstance(value, tuple) else value)

    def cancel(self):
        if self.pending is None:
            return
        for name, value in self.pending["old"].items():
            _set_value(getattr(self.window, name), value)
        self.window._sync_calibration_primary_layout()
        if self.pending["function"].__name__ == "request_detector_calibration":
            self.window._sync_qtegra_controls()
        self.pending = None
        self.escape.setEnabled(False)
        if self.record is None:
            self.bar.hide()
        else:
            self._place(self.record["change"]["control"])
            self.bar.show_undo(self.record["change"]["label"])
            self._reveal()

    def apply(self):
        if self.pending is None or self.window._active_worker is not None:
            return
        w = self.window
        change = self.pending
        self.pending = None
        self.record = None
        self.escape.setEnabled(False)
        self.bar.hide()
        session = w.session
        excluded = {"experiment", "total_data", "qtegra", "pipeline", "reduction_cache"}
        settings = {field.name: deepcopy(getattr(session, field.name)) for field in fields(ReductionSession)
                    if field.name not in excluded}
        pipeline = session.pipeline
        self.transaction = dict(
            change=change, session=session, settings=settings, preset=w.workflow_preset,
            selection_method=w.selection_method, pipeline=pipeline,
            config=pipeline.config if pipeline else None,
            products={key: value for key, value in session.reduction_cache.items() if key in change["cache_keys"]},
            stages={stage: value for stage, value in pipeline.cache.as_mapping().items() if stage in change["targets"]} if pipeline else {},
            notices=list(pipeline.notices) if pipeline else [],
            workspace=w.project_workspace_state(), path=w.project_path, fingerprint=w._saved_project_fingerprint,
            history=(list(w._selection_undo), list(w._selection_redo), set(w._spots_edited_since_write)),
            library=(services.current_standard_archive(), services.current_standard_archive_path(), w.reference_library_path),
        )
        self.running = True
        for name, value in change["proposed"].items():
            _set_value(getattr(w, name), value)
        # Workflow and selection-method handlers mutate settings in place.
        # Give them private metadata so the old session remains an undo target.
        if change["replace_session"] and change["function"].__name__ in {"on_workflow_preset_changed", "on_selection_method_changed", "on_plateau_filter_method_changed", "request_reference_material_library"}:
            w.session = copy_session(session)
        try:
            result = change["function"](w, *change["args"], **change["kwargs"])
            if result is False:
                self.fail()
        except Exception:
            self.fail()
            raise
        if self.running and w._active_worker is None:
            self.complete()

    def complete(self):
        """Commit only after a synchronous handler or worker succeeded."""
        if not self.running:
            return
        w = self.window
        record = self.transaction
        change = record["change"]
        if not change["replace_session"]:
            changed_settings = {name: value for name, value in record["settings"].items()
                                if getattr(w.session, name) != value}
            if not changed_settings:
                self._restore(record)
                self.transaction = None
                self.running = False
                self.sync()
                return
            record["settings"] = changed_settings
            # Keep both cache interfaces consistent, including derived Pb204 products.
            for key in change["cache_keys"]:
                w.session.reduction_cache.pop(key, None)
            if w.session.pipeline is not None:
                for stage in change["targets"]:
                    w.session.pipeline.cache.invalidate(stage)
                w.session.pipeline.config = pipeline_config_from_session(w.session, w.workflow_preset)
            # A settings change never schedules numerical work through refresh_view.
            if w.session.plot_mode != "raw":
                w.set_plot_mode("raw", refresh=False)
        self.record = record
        self.transaction = None
        self.running = False
        self.sync()
        self.bar.show_undo(change["label"])
        if w.session.loaded and w.session.plot_mode == "raw":
            w.refresh_view()
        w._refresh_workflow_status()
        w.update_project_window_title()
        self._reveal()

    def fail(self):
        """Roll back settings after a failed synchronous or worker operation."""
        if not self.running:
            return
        record = self.transaction
        self.transaction = None
        self._restore(record)
        self.running = False
        self.record = None
        self.sync()
        self.bar.hide()
        self._refresh_restored_view()

    def undo(self):
        if self.record is None or self.window._active_worker is not None:
            return
        record = self.record
        self.record = None
        self.running = True
        self._restore(record)
        self.running = False
        self.sync()
        self.bar.hide()
        self._refresh_restored_view()

    def _refresh_restored_view(self):
        w = self.window
        # A view without a saved product would otherwise run a stage on Undo.
        products = {
            "baseline_subtracted": "baseline_subtracted_total_data",
            "normalized_plateau": "normalized_plateau_data",
            "calibration_slopes": "concentration_results",
            "raw_ratios": "plateau_data_with_raw_ratios",
            "downhole_ratios": "downhole_results",
            "session_ratios": "session_correction_results",
            "standard_bias": "finalized_results",
            "depth_profile": "depth_profile_results",
            "final_results": "finalized_results",
        }
        key = products.get(w.session.plot_mode)
        if key is not None and key not in w.session.reduction_cache:
            w.set_plot_mode("raw", refresh=False)
        w.refresh_view()

    def _restore(self, record):
        w = self.window
        session = record["session"]
        w.workflow_preset = record["preset"]
        w.selection_method = record["selection_method"]
        if record["change"]["function"].__name__ == "request_reference_material_library":
            archive, path, window_path = record["library"]
            services._active_standard_archive = archive
            services._active_standard_archive_path = path
            w.reference_library_path = window_path
            w.settings.setValue("reference_library_path", window_path)
            w._update_reference_library_labels()
        if record["change"]["replace_session"]:
            w.install_loaded_session(session, session.directory, project_path=record["path"])
            for name, value in record["settings"].items():
                setattr(session, name, value)
            w.results_table.restore_review_state(record["workspace"], session.spot_names)
            w._review_flags = dict(record["workspace"]["review_flags"])
            w._spot_folder_overrides = dict(record["workspace"]["spot_folder_overrides"])
            w._selection_undo, w._selection_redo, w._spots_edited_since_write = record["history"]
            w._saved_project_fingerprint = record["fingerprint"]
        else:
            for name, value in record["settings"].items():
                setattr(session, name, value)
            for key in record["change"]["cache_keys"]:
                session.reduction_cache.pop(key, None)
            session.reduction_cache.update(record["products"])
            session.pipeline = record["pipeline"]
            if session.pipeline:
                for stage in record["change"]["targets"]:
                    session.pipeline.cache.invalidate(stage)
                for stage, value in record["stages"].items():
                    session.pipeline.cache.set(stage, value)
                session.pipeline.config = record["config"]
                session.pipeline.notices = list(record["notices"])
        w._sync_workflow_combo()
        w._sync_selection_method_combo()
        if record["change"]["replace_session"]:
            w._populate_workflow_ui()
        w._sync_fit_controls()
        w._sync_selection_parameter_controls()
        for name, value in record["change"]["old"].items():
            _set_value(getattr(w, name), value)
        w._sync_calibration_primary_layout()
        w._sync_selection_history_actions()
        w._refresh_workflow_status()
        w.update_project_window_title()
