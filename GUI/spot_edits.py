"""Undoable spot identity, material, folder and calculation decisions."""

from copy import deepcopy
from dataclasses import dataclass
import re

import pandas as pd
from PySide6 import QtCore, QtWidgets

from GUI.services import ensure_shared_pipeline
from GUI.theme import active_theme
from GUI.spot_colours import normalise_colour
from UPbRPy.io import sample_from_label
from UPbRPy.pipeline import PipelineStage
from UPbRPy.spot_edits import original_spot_label
from UPbRPy.standards import canonicalize_reference_material_samples


@dataclass
class SpotEditState:
    experiment: pd.DataFrame
    total_data: pd.DataFrame
    selected_spot: str | None
    folders: dict
    review_flags: dict
    excluded: set
    included: dict
    selected: set
    fits: dict
    acquisition: dict
    edited: set
    sample_colours: dict
    spot_colours: dict
    invalidate_from: PipelineStage | None = PipelineStage.BASELINE


def group_key(name):
    return re.sub(r"[\W_]+", "", name).casefold()


class SpotEditing:
    """MainWindow operations, sharing its selection undo and redo history."""

    def request_sample_colour(self, sample: str, colour: str | None) -> bool:
        """Set/reset one sample's display colour as a single undo step."""
        self._set_colour_order()
        if sample not in self.spot_colours._samples:
            return False
        return self._request_colours(self.spot_colours.sample_colours, [sample], colour)

    def request_spot_colour(self, spots, colour: str | None) -> bool:
        """Set/reset the overrides for known spots as a single undo step."""
        if isinstance(spots, str):
            spots = [spots]
        names = set(spots) & set(self.session.spot_names)
        return self._request_colours(self.spot_colours.spot_colours, names, colour)

    def _request_colours(self, target, names, colour):
        colour = normalise_colour(colour) if colour is not None else None
        names = [name for name in names if target.get(name) != colour]
        if not names:
            return False
        before = self._begin_spot_edit()
        if before is None:
            return False
        for name in names:
            if colour is None:
                target.pop(name, None)
            else:
                target[name] = colour
        self._finish_spot_edit(before, calculations=False)
        return True

    def request_reset_colours(self) -> bool:
        """Reset all sample colours and spot overrides in one undo step."""
        if not (self.spot_colours.sample_colours or self.spot_colours.spot_colours):
            return False
        before = self._begin_spot_edit()
        if before is None:
            return False
        self.spot_colours.sample_colours.clear()
        self.spot_colours.spot_colours.clear()
        self._finish_spot_edit(before, calculations=False)
        return True

    def _request_new_folder_move(self, spot_name):
        count = len(self._selection_undo)
        folder = self._on_spot_add_folder()
        if folder and self.request_move_spot(spot_name, folder) and len(self._selection_undo) == count + 2:
            first, second = self._selection_undo[-2:]
            first[0].invalidate_from = second[1].invalidate_from
            self._selection_undo[-2:] = [(first[0], second[1])]

    def _spot_edit_state(self):
        table = self.results_table
        return SpotEditState(
            self.session.experiment.copy(deep=True), self.session.total_data,
            self.session.selected_spot, dict(self._spot_folder_overrides), dict(self._review_flags),
            set(table.excluded_spots), dict(table.include_flags), set(table.selected_spots),
            {key: deepcopy(panel.records) for key, panel in table.fit_panels.items()},
            dict(table._spot_acquisition), set(self._spots_edited_since_write),
            dict(self.spot_colours.sample_colours), dict(self.spot_colours.spot_colours),
        )

    def _begin_spot_edit(self):
        if self._active_worker is not None or not self.session.loaded:
            self.statusBar().showMessage("Wait for the running reduction before editing spots.")
            return None
        self.change_guard.clear()
        self._apply_tree_checks()
        self.flush_selection_edits()
        self._selection_recompute_timer.stop()
        self._selection_recompute_stages.clear()
        return self._spot_edit_state()

    def _finish_spot_edit(self, before, *, calculations=True, material_only=False):
        stage = (PipelineStage.NORMALIZATION if material_only else PipelineStage.BASELINE) if calculations else None
        self._sync_spot_edit_pipeline(material_only=material_only, calculations=calculations)
        after = self._spot_edit_state()
        before.invalidate_from = after.invalidate_from = stage
        self._selection_undo.append((before, after))
        self._selection_redo.clear()
        self._spot_edit_refreshed(calculations, material_only)

    def _sync_spot_edit_pipeline(self, *, material_only=False, calculations=True):
        pipeline = self.session.pipeline
        if pipeline is not None:
            # Normalization uses the material's internal reference value. Raw
            # ratios carry sample labels and downstream models use them too.
            stage = (PipelineStage.NORMALIZATION if material_only else PipelineStage.BASELINE) if calculations else None
            pipeline.update_inputs(self.session.experiment, self.session.total_data, invalidate_from=stage)
            if material_only and calculations:
                pipeline.cache.invalidate(PipelineStage.RATIOS)
                baseline = pipeline.cache.get(PipelineStage.BASELINE)
                if isinstance(baseline, pd.DataFrame):
                    baseline = baseline.copy()
                    samples = self.session.experiment.set_index("spot_names")["sample"]
                    baseline["sample"] = baseline["spot_name"].map(samples)
                    pipeline.cache.set(PipelineStage.BASELINE, baseline)
                    self.session.reduction_cache["baseline_subtracted_total_data"] = baseline
        ensure_shared_pipeline(self.session, self.workflow_preset)

    def _spot_edit_refreshed(self, calculations=True, material_only=False):
        # Timer-driven selection recomputes must not resurrect old stage lists.
        self._selection_recompute_timer.stop()
        self._selection_recompute_stages.clear()
        if calculations:
            self._invalidate_spot_results_view()
        selected_rows = ({str(item.data(0, QtCore.Qt.ItemDataRole.UserRole))
                          for item in self.spot_list.selectedItems()} if not calculations else None)
        populating = self._is_populating
        self._is_populating = True
        try:
            self._populate_spot_tree()
        finally:
            self._is_populating = populating
        if not calculations:
            with QtCore.QSignalBlocker(self.spot_list):
                self.spot_list.clearSelection()
                for name in selected_rows:
                    item = self._spot_tree_item(name)
                    if item is not None:
                        item.setSelected(True)
            self._refresh_spot_colours()
        self._sync_workflow_step_locks()
        self.refresh_view()
        self._sync_selection_history_actions()
        self.update_project_window_title()
        self._selection_autosave_pending = True
        self._selection_autosave_timer.start(self.selection_autosave_debounce_ms)
        if calculations:
            self._spot_rerun_stage = PipelineStage.NORMALIZATION if material_only else PipelineStage.BASELINE
            stage = "normalization and concentrations" if material_only else "baseline"
            message = f"Spots changed — re-run {stage}."
            self.workflow_status_label.setText(message)
            self.statusBar().showMessage(message)

    def _invalidate_spot_results_view(self):
        table = self.results_table
        with QtCore.QSignalBlocker(table):
            for model in (table.results_model, table.secondary_model, table.depth_model):
                model.set_frame(pd.DataFrame())
            table._spot_frame = pd.DataFrame()
            table._depth_frame = pd.DataFrame()
            table._spot_samples = self._spot_sample_map()
            table._run_order = list(self.session.spot_names)
            for model in (table.results_group_model, table.secondary_group_model,
                          table.spot_choice_group_model, table.depth_choice_group_model):
                model.removeRows(0, model.rowCount())
                model._review_items = None
            table.spot_choice_list.clear()
            table.depth_spot_combo.clear()
            table._choice_items = {}
            for canvas in (table.spot_concordia, table.depth_concordia):
                canvas.selected_spots = set(table.selected_spots)
                canvas.set_excluded_spots(table.excluded_spots)
                canvas.show_data(pd.DataFrame(), diagram=canvas._diagram,
                                 kind="spot" if canvas is table.spot_concordia else "depth",
                                 include_calibration_uncertainty=True,
                                 theme=active_theme())
            for panel in table.fit_panels.values():
                panel._timer.stop()
        # Keep the raw trace usable while the invalidated stages await a run.
        self.session.plot_mode = "raw"
        self._sync_plot_mode_buttons()

    def _restore_spot_edit(self, state):
        current = {original_spot_label(self.session.experiment, name): name
                   for name in self.session.spot_names}
        target = {original_spot_label(state.experiment, name): name
                  for name in state.experiment["spot_names"].astype(str)}
        mapping = {name: target[label] for label, name in current.items()
                   if label in target and name != target[label]}
        self._remap_window_history(mapping)
        self.session.experiment = state.experiment.copy(deep=True)
        self.session.total_data = state.total_data
        self.session.selected_spot = state.selected_spot
        self._spot_folder_overrides = dict(state.folders)
        self._review_flags = dict(state.review_flags)
        self.spot_colours.sample_colours = dict(state.sample_colours)
        self.spot_colours.spot_colours = dict(state.spot_colours)
        self._spots_edited_since_write = set(state.edited)
        table = self.results_table
        with QtCore.QSignalBlocker(table):
            table.excluded_spots = set(state.excluded)
            table.include_flags.clear()
            table.include_flags.update(state.included)
            table.selected_spots = set(state.selected)
            table._spot_acquisition = dict(state.acquisition)
            if state.invalidate_from is not None:
                for key, records in state.fits.items():
                    table.fit_panels[key].records = deepcopy(records)
                    table.fit_panels[key].invalidate()
        self.pending_region_updates.clear()
        calculations = state.invalidate_from is not None
        material_only = state.invalidate_from == PipelineStage.NORMALIZATION
        self._sync_spot_edit_pipeline(calculations=calculations, material_only=material_only)
        self._spot_edit_refreshed(calculations, material_only)

    def _remap_window_history(self, mapping):
        # Earlier window edits follow the active label; snapshots retain the
        # names at their own point in history so undoing a rename is exact.
        for history in (self._selection_undo, self._selection_redo):
            for index, entry in enumerate(history):
                history[index] = tuple({mapping.get(key, key): value for key, value in state.items()}
                                       if isinstance(state, dict) else state for state in entry)
        self.pending_region_updates = {mapping.get(key, key): value
                                       for key, value in self.pending_region_updates.items()}

    def _label_sample(self, name):
        rule = self.session.experiment.attrs.get("sample_label_rule", {})
        return sample_from_label(name, rule.get("sep", self.session.import_settings.get("sep", "_")),
                                 rule.get("aliases", self.session.import_settings.get("sample_aliases")))

    def _canonical_group(self, name):
        key = group_key(name)
        for existing in self._get_all_folder_names():
            if group_key(existing) == key:
                return existing
        return self._canonical_material(name)

    def _canonical_material(self, name):
        # Archive matching uses the same recognition as the Spots tree.
        frame = canonicalize_reference_material_samples(pd.DataFrame({"sample": [name]}),
                                                         self._spot_edit_archive())
        return str(frame["sample"].iloc[0])

    @staticmethod
    def _spot_edit_archive():
        from GUI.services import current_standard_archive
        return current_standard_archive()

    def _confirm_material(self, spot, sample):
        old = self._spot_sample_map().get(spot, self._label_sample(spot))
        if old == sample:
            return True
        message = f"{spot} will be used as {sample} for concentrations and session corrections (was {old})."
        if self._is_reference_material(old) and not self._is_reference_material(sample):
            message += f"\nIt will no longer be used as {old}."
        return QtWidgets.QMessageBox.question(
            self, "Change spot material", message,
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok

    def _set_spot_material(self, name, sample):
        experiment = self.session.experiment
        experiment.loc[experiment["spot_names"].eq(name), "sample"] = sample
        self.session.total_data = self.session.total_data.copy()
        self.session.total_data.loc[self.session.total_data["spot_name"].eq(name), "sample"] = sample
        original = original_spot_label(experiment, name)
        derived = self._label_sample(original)
        overrides = deepcopy(experiment.attrs.get("sample_overrides", {}))
        if sample == self._canonical_material(derived):
            overrides.pop(name, None)
        else:
            overrides[name] = {"label_sample": derived, "sample": sample, "source": "studio"}
        experiment.attrs["sample_overrides"] = overrides

    def request_rename_spot(self, old_name, new_name):
        """Rename a spot, retaining its raw label and reviewed windows."""
        new_name = new_name.strip()
        if old_name not in self.session.spot_names or self._active_worker is not None:
            return False
        if not new_name or any(name != old_name and name.casefold() == new_name.casefold()
                               for name in self.session.spot_names):
            QtWidgets.QMessageBox.warning(self, "Cannot rename spot",
                "Enter a non-empty name that does not match another spot (ignoring case).")
            return False
        if old_name == new_name:
            return False
        old_rule = self._canonical_material(self._label_sample(old_name))
        new_rule = self._canonical_material(self._label_sample(new_name))
        material_changed = new_rule != old_rule
        sample = new_rule if material_changed else self._spot_sample_map().get(old_name, old_rule)
        if material_changed and not self._confirm_material(old_name, sample):
            return False
        before = self._begin_spot_edit()
        if before is None:
            return False
        experiment = self.session.experiment
        original = original_spot_label(experiment, old_name)
        experiment.loc[experiment["spot_names"].eq(old_name), "spot_names"] = new_name
        self.session.total_data = self.session.total_data.copy()
        self.session.total_data.loc[self.session.total_data["spot_name"].eq(old_name), "spot_name"] = new_name
        renames = deepcopy(experiment.attrs.get("spot_renames", {}))
        renames.pop(old_name, None)
        if new_name != original:
            renames[new_name] = original
        experiment.attrs["spot_renames"] = renames
        mapping = {old_name: new_name}
        self._remap_window_history(mapping)
        for values in (self.spot_colours.spot_colours, self._spot_folder_overrides, self._review_flags,
                       self.results_table.include_flags, self.results_table._spot_acquisition):
            if old_name in values:
                values[new_name] = values.pop(old_name)
        if material_changed and self._is_reference_material(sample):
            self._spot_folder_overrides.pop(new_name, None)
        overrides = deepcopy(experiment.attrs.get("sample_overrides", {}))
        if old_name in overrides:
            overrides[new_name] = overrides.pop(old_name)
        experiment.attrs["sample_overrides"] = overrides
        table = self.results_table
        for values in (table.excluded_spots, table.selected_spots, self._spots_edited_since_write):
            if old_name in values:
                values.remove(old_name)
                values.add(new_name)
        experiment.attrs["calculation_excluded_spots"] = sorted(
            mapping.get(name, name) for name in experiment.attrs.get("calculation_excluded_spots", ()))
        for panel in table.fit_panels.values():
            for record in panel.records:
                for key in ("items", "excluded"):
                    if isinstance(record.get(key), list):
                        record[key] = [mapping.get(name, name) for name in record[key]]
            panel.invalidate()
        if self.session.selected_spot == old_name:
            self.session.selected_spot = new_name
        if material_changed:
            self._set_spot_material(new_name, sample)
        self._finish_spot_edit(before)
        return True

    def request_move_spot(self, spot, folder):
        """Move to an existing group, confirming any material reassignment."""
        folder = self._canonical_group(folder.strip())
        if folder not in self._get_all_folder_names() or spot not in self.session.spot_names:
            QtWidgets.QMessageBox.warning(self, "Unknown folder", "Create the folder with New folder… first.")
            return False
        old = self._spot_sample_map().get(spot)
        sample = folder if self._is_reference_material(folder) or self._is_reference_material(old) else old
        if not self._confirm_material(spot, sample):
            return False
        before = self._begin_spot_edit()
        if before is None:
            return False
        if sample != old:
            self._set_spot_material(spot, sample)
        if self._is_reference_material(folder):
            self._spot_folder_overrides.pop(spot, None)
        else:
            self._spot_folder_overrides[spot] = folder
        self._finish_spot_edit(before, calculations=sample != old, material_only=True)
        return True

    def request_calculation_exclusion(self, spot_name, excluded):
        """Set whether a spot is left out of every calculation, with undo."""
        if spot_name not in self.session.spot_names:
            return False
        names = set(self.session.experiment.attrs.get("calculation_excluded_spots", ()))
        if (spot_name in names) == excluded:
            return False
        before = self._begin_spot_edit()
        if before is None:
            return False
        if excluded:
            names.add(spot_name)
        else:
            names.discard(spot_name)
        self.session.experiment.attrs["calculation_excluded_spots"] = sorted(names)
        self._finish_spot_edit(before)
        return True

    def request_remove_spot(self, spot_name):
        """Remove a spot from the working session in one undo step.

        The old code's Undo refused to run unless the current workflow step
        allowed window edits, so a spot removed at Concentrations or Final
        review could not be brought back. It also replaced the session with
        copy_session and only cleared caches, leaving stale spot-keyed review
        state and fit records, and its undo restored frames without resyncing
        the shared pipeline's total_data. Removal is now recorded by raw label
        in the project, and spot edits undo from any step.
        """
        if spot_name not in self.session.spot_names:
            return
        before = self._begin_spot_edit()
        if before is None:
            return
        original = original_spot_label(self.session.experiment, spot_name)
        experiment = self.session.experiment.loc[~self.session.experiment["spot_names"].eq(spot_name)].copy()
        removed = set(experiment.attrs.get("removed_spots", ()))
        removed.add(original)
        experiment.attrs["removed_spots"] = sorted(removed)
        for key in ("sample_overrides", "spot_renames"):
            experiment.attrs[key] = {name: value for name, value in experiment.attrs.get(key, {}).items()
                                     if name != spot_name}
        self.session.experiment = experiment
        self.session.total_data = self.session.total_data.loc[
            ~self.session.total_data["spot_name"].eq(spot_name)].copy()
        if self.session.selected_spot == spot_name:
            self.session.selected_spot = next(iter(self.session.spot_names), None)
        table = self.results_table
        for values in (self.spot_colours.spot_colours, self._spot_folder_overrides, self._review_flags, table.include_flags, table._spot_acquisition):
            values.pop(spot_name, None)
        for values in (table.excluded_spots, table.selected_spots, self._spots_edited_since_write):
            values.discard(spot_name)
        experiment.attrs["calculation_excluded_spots"] = [
            name for name in experiment.attrs.get("calculation_excluded_spots", ()) if name != spot_name]
        for panel in table.fit_panels.values():
            for record in panel.records:
                for key in ("items", "excluded"):
                    if isinstance(record.get(key), list):
                        record[key] = [name for name in record[key] if name != spot_name]
            panel.invalidate()
        self._finish_spot_edit(before)
