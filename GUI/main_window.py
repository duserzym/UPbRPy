from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets

from GUI.icon import (
    build_app_icon,
    build_brand_pixmap,
    build_workflow_stage_icon,
)
from GUI.services import (
    DOWNHOLE_FIT_METHODS,
    SESSION_FIT_METHODS,
    RestoredProject,
    autopick_all_selection_regions,
    autopick_selection_regions,
    build_channel_colors,
    build_notes,
    clear_reduction_cache,
    current_standard_archive,
    current_standard_archive_path,
    default_reference_material_library_path,
    ensure_baseline_subtracted,
    ensure_concentration_results,
    ensure_depth_profile,
    ensure_downhole_results,
    ensure_finalized_results,
    ensure_normalized_plateau,
    ensure_raw_ratios,
    ensure_session_corrected,
    ensure_shared_pipeline,
    ensure_spot_means,
    get_calibration_slope_data,
    get_downhole_plot_data,
    get_selection_regions,
    get_session_plot_data,
    get_spot_data,
    fit_method_label,
    load_standard_archive_from_path,
    load_session,
    project_summary,
    reference_material_summary,
    reimport_detector_calibration,
    restore_session_from_project,
    selection_quality,
    spot_base_name,
    update_selection_region,
)
from GUI.state import ReductionSession
from GUI.theme import NeumorphicFrame, active_theme, apply_theme
from GUI.widgets import (
    CalibrationSlopesWidget,
    DownholeFractionationWidget,
    DetectorCalibrationPanel,
    ElidedLabel,
    elide_tab_titles,
    IsotopeGridWidget,
    ResultsTableWidget,
    SessionFractionationWidget,
    SessionOverviewWidget,
    SpotTree,
    StyledComboBox,
    TracePlotWidget,
    make_list_item,
)
from GUI.workers import FunctionWorker
from GUI.workflows import WORKFLOW_PRESETS, format_import_defaults, get_workflow_preset, infer_workflow_preset
from UPbRPy.io import SELECTION_METHODS, selection_method_label
from UPbRPy.pipeline import PIPELINE_STAGE_BY_ID, PipelineStage
from UPbRPy.project import write_reduction_project
from UPbRPy.reproducibility import (
    export_reduction_bundle,
    selection_export_frame,
    write_reproducibility_notebook,
)


PLOT_MODE_LABELS = {
    "raw": "Raw",
    "baseline_subtracted": "Baseline",
    "normalized_plateau": "Normalized",
    "calibration_slopes": "Calibration",
    "raw_ratios": "Ratios",
    "downhole_ratios": "Downhole",
    "session_ratios": "Session",
    "depth_profile": "Depth",
    "final_results": "Final",
}

_STAGE_CACHE_KEYS: dict[str, str | None] = {
    "raw": None,
    "baseline_subtracted": "baseline_subtracted_total_data",
    "normalized_plateau": "normalized_plateau_data",
    "calibration_slopes": "concentration_results",
    "raw_ratios": "plateau_data_with_raw_ratios",
    "downhole_ratios": "downhole_results",
    "session_ratios": "session_correction_results",
    "depth_profile": "depth_profile_results",
    "final_results": "finalized_results",
}

# Stack index for the plot QStackedWidget
_PLOT_STACK_IDX: dict[str, int] = {
    "raw": 0,
    "baseline_subtracted": 0,
    "normalized_plateau": 0,
    "calibration_slopes": 1,
    "raw_ratios": 0,
    "downhole_ratios": 2,
    "session_ratios": 3,
    "depth_profile": 4,
    "final_results": 4,
}

RATIO_COLORS = {
    "206Pb/238U": "#1f63cf",
    "207Pb/206Pb": "#ff8a5b",
    "208Pb/232Th": "#19a188",
}

RATIO_AGGREGATION_OPTIONS = (
    ("Arithmetic mean (compatibility default)", "arithmetic"),
    ("Log-ratio / geometric mean", "logratio"),
)

DEFAULT_REGION_COLORS = {
    "baseline": "#c83e3e",
    "plateau": "#2c7fb8",
}

WORKFLOW_STAGE_LABELS = {
    PipelineStage.IMPORT: "Import",
    PipelineStage.SELECTION: "Selections",
    PipelineStage.BASELINE: "Baseline",
    PipelineStage.NORMALIZATION: "Normalize",
    PipelineStage.CONCENTRATION: "Concentrations",
    PipelineStage.RATIOS: "Ratios",
    PipelineStage.DOWNHOLE: "Downhole",
    PipelineStage.SESSION: "Session",
    PipelineStage.SPOT_MEANS: "Spot means",
    PipelineStage.DEPTH_PROFILE: "Depth profiles",
    PipelineStage.FINALIZE: "Final review",
    PipelineStage.EXPORT: "Export",
}


class MainWindow(QtWidgets.QMainWindow):
    """One independent reduction session presented in a native window."""

    new_window_requested = QtCore.Signal()
    next_window_requested = QtCore.Signal()
    open_project_in_new_window_requested = QtCore.Signal(str)
    open_experiment_in_new_window_requested = QtCore.Signal(str)

    def __init__(self, theme_mode: str = "light", app_icon: QtGui.QIcon | None = None) -> None:
        super().__init__()
        # Closed sessions may contain large sweep tables. Delete the window
        # promptly so those frames and pipeline caches can be reclaimed.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.settings = QtCore.QSettings("UPbRPy", "Studio")
        self.session = ReductionSession()
        self.project_path: Path | None = None
        self._saved_project_fingerprint: str | None = None
        self.channel_colors: dict[str, str] = {}
        self._is_populating = False
        self._spot_folder_overrides: dict[str, str] = {}  # {spot_name: folder_name}
        self.pending_region_updates: dict[str, tuple[float, float]] = {}
        self.workflow_preset = get_workflow_preset("zircon")
        self.selection_method = str(self.workflow_preset.read_kwargs.get("selection_method", "threshold"))
        self.theme_mode = theme_mode
        self.app_icon = app_icon or build_app_icon()
        self.sigma_level = 2
        self.selected_stats_channel: str | None = None
        self._focused_spot_name: str | None = None
        self._last_plot_mode: str | None = None
        self.reference_library_path = default_reference_material_library_path()
        self.default_reference_library_path = default_reference_material_library_path()
        self._reference_library_notice: str | None = None
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._active_worker: FunctionWorker | None = None
        self._busy_label = ""
        # Database-mode flows; the window controller shares one instance
        # between windows, a lone window creates its own on first use.
        self.database_links = None
        self.selection_colors = self._resolve_selection_colors()
        self.session.session_fit_options["polynomial_degree"] = self.workflow_preset.session_polynomial_degree

        self.setWindowTitle("UPbRPy Studio")
        self.setWindowIcon(self.app_icon)
        self.setMinimumSize(760, 620)
        self._apply_initial_window_geometry()

        self._build_menu()
        self._build_ui()
        self._initialize_reference_library()
        self._connect_system_theme_updates()
        self.apply_theme_mode(self.theme_mode, persist=False)
        self._show_placeholder_state()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        new_window_action = file_menu.addAction("New Reduction Window")
        new_window_action.setShortcut(QtGui.QKeySequence.StandardKey.New)
        new_window_action.triggered.connect(self.new_window_requested.emit)

        file_menu.addSeparator()
        open_project_action = file_menu.addAction("Open Project…")
        open_project_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        open_project_action.triggered.connect(self.choose_project_file)
        open_project_new_window_action = file_menu.addAction(
            "Open Project in New Window…"
        )
        open_project_new_window_action.triggered.connect(
            self.choose_project_file_in_new_window
        )

        save_project_action = file_menu.addAction("Save Project")
        save_project_action.setShortcut(QtGui.QKeySequence.StandardKey.Save)
        save_project_action.triggered.connect(
            lambda _checked=False: self.save_project()
        )

        save_project_as_action = file_menu.addAction("Save Project As…")
        save_project_as_action.setShortcut(
            QtGui.QKeySequence.StandardKey.SaveAs
        )
        save_project_as_action.triggered.connect(
            self.choose_project_save_path
        )

        file_menu.addSeparator()
        open_experiment_action = file_menu.addAction(
            "Open Experiment Folder…"
        )
        open_experiment_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+O"))
        open_experiment_action.triggered.connect(
            self.choose_experiment_directory
        )
        open_experiment_new_window_action = file_menu.addAction(
            "Open Experiment Folder in New Window…"
        )
        open_experiment_new_window_action.triggered.connect(
            self.choose_experiment_directory_in_new_window
        )
        open_qtegra_action = file_menu.addAction("Open Qtegra file…")
        open_qtegra_action.triggered.connect(self.choose_qtegra_file)
        open_qtegra_new_window_action = file_menu.addAction("Open Qtegra file in New Window…")
        open_qtegra_new_window_action.triggered.connect(self.choose_qtegra_file_in_new_window)

        reload_action = file_menu.addAction("Reload Current Source")
        reload_action.setShortcut(QtGui.QKeySequence("Ctrl+R"))
        reload_action.triggered.connect(self.reload_current_directory)

        file_menu.addSeparator()
        library_action = file_menu.addAction("Reference Material Library…")
        library_action.triggered.connect(
            self.choose_reference_material_library
        )

        export_menu = file_menu.addMenu("Export")
        bundle_action = export_menu.addAction(
            "Export Publication + Reproducibility Bundle…"
        )
        bundle_action.triggered.connect(self.choose_reduction_bundle_export)
        notebook_action = export_menu.addAction(
            "Export Reproducible Jupyter Notebook…"
        )
        notebook_action.triggered.connect(self.choose_notebook_export)

        database_menu = file_menu.addMenu("Database")
        database_menu.setToolTipsVisible(True)
        self.open_from_database_action = database_menu.addAction(
            "Open from Database…"
        )
        self.open_from_database_action.triggered.connect(
            lambda _checked=False: self.database().open_from_database()
        )
        self.submit_to_database_action = database_menu.addAction(
            "Submit to Database"
        )
        self.submit_to_database_action.triggered.connect(
            lambda _checked=False: self.database().submit(self)
        )
        self.prefer_run_action = database_menu.addAction("Mark as Preferred")
        self.prefer_run_action.triggered.connect(
            lambda _checked=False: self.database().prefer(self)
        )
        database_menu.addSeparator()
        self.sign_out_action = database_menu.addAction("Sign Out")
        self.sign_out_action.triggered.connect(
            lambda _checked=False: self.database().sign_out(
                (self.session.origin or {}).get("server")
            )
        )
        if sys.platform == "win32":
            register_action = database_menu.addAction("Register upbr:// Links")
            register_action.setToolTip(
                "Let browsers and UPb Studio open database links in this "
                "Studio (writes HKCU\\Software\\Classes\\upbr)."
            )
            register_action.triggered.connect(self.register_url_scheme)
        self._update_database_actions()

        file_menu.addSeparator()
        exit_action = file_menu.addAction("Quit")
        exit_action.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        application = QtWidgets.QApplication.instance()
        if application is not None:
            exit_action.triggered.connect(application.quit)

        workflow_menu = self.menuBar().addMenu("Workflow")
        mineral_menu = workflow_menu.addMenu("Mineral Preset")
        self.workflow_menu_group = QtGui.QActionGroup(self)
        self.workflow_menu_group.setExclusive(True)
        self.workflow_menu_actions: dict[str, QtGui.QAction] = {}
        for key, preset in WORKFLOW_PRESETS.items():
            action = mineral_menu.addAction(preset.title)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, workflow_key=key:
                self.select_workflow_preset(workflow_key)
            )
            self.workflow_menu_group.addAction(action)
            self.workflow_menu_actions[key] = action

        selection_menu = workflow_menu.addMenu("Auto-pick Method")
        self.selection_menu_group = QtGui.QActionGroup(self)
        self.selection_menu_group.setExclusive(True)
        self.selection_menu_actions: dict[str, QtGui.QAction] = {}
        for method in SELECTION_METHODS:
            action = selection_menu.addAction(selection_method_label(method))
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, selection_method=method:
                self.select_selection_method(selection_method)
            )
            self.selection_menu_group.addAction(action)
            self.selection_menu_actions[method] = action

        workflow_menu.addSeparator()
        previous_step = workflow_menu.addAction("Previous Step")
        previous_step.setShortcut(QtGui.QKeySequence("Ctrl+["))
        previous_step.triggered.connect(
            lambda: self.go_to_workflow_step(-1)
        )
        next_step = workflow_menu.addAction("Next Step")
        next_step.setShortcut(QtGui.QKeySequence("Ctrl+]"))
        next_step.triggered.connect(lambda: self.go_to_workflow_step(1))
        run_step = workflow_menu.addAction("Run Primary Action for This Step")
        run_step.setShortcut(QtGui.QKeySequence("Ctrl+Return"))
        run_step.triggered.connect(self.run_current_workflow_action)
        workflow_menu.addSeparator()
        auto_pick_action = workflow_menu.addAction("Auto-pick Current Spot")
        auto_pick_action.triggered.connect(self.run_autopick_current_spot)
        auto_pick_all_action = workflow_menu.addAction("Auto-pick All Spots")
        auto_pick_all_action.triggered.connect(self.run_autopick_all_spots)
        apply_windows_action = workflow_menu.addAction(
            "Apply Current Selection Windows"
        )
        apply_windows_action.triggered.connect(self.apply_current_windows)

        calibration_menu = self.menuBar().addMenu("Calibration")
        downhole_menu = calibration_menu.addMenu("Downhole Model")
        session_menu = calibration_menu.addMenu("Session Model")
        for method in DOWNHOLE_FIT_METHODS:
            action = downhole_menu.addAction(fit_method_label(method))
            action.triggered.connect(
                lambda _checked=False, fit_method=method:
                self.select_fit_method("downhole", fit_method)
            )
        for method in SESSION_FIT_METHODS:
            action = session_menu.addAction(fit_method_label(method))
            action.triggered.connect(
                lambda _checked=False, fit_method=method:
                self.select_fit_method("session", fit_method)
            )
        calibration_menu.addSeparator()
        show_inspector = calibration_menu.addAction(
            "Show Calibration Inspector"
        )
        show_inspector.triggered.connect(
            lambda: self.set_workspace_panel_visible("inspector", True)
        )

        results_menu = self.menuBar().addMenu("Results")
        final_action = results_menu.addAction("Calculate Final Results")
        final_action.triggered.connect(self.run_final_review)
        depth_action = results_menu.addAction("Calculate Depth Profiles")
        depth_action.triggered.connect(self.run_depth_profile_review)
        results_menu.addSeparator()
        results_bundle_action = results_menu.addAction(
            "Export Publication + Reproducibility Bundle…"
        )
        results_bundle_action.triggered.connect(
            self.choose_reduction_bundle_export
        )

        view_menu = self.menuBar().addMenu("View")
        theme_menu = view_menu.addMenu("Theme")
        self.theme_action_group = QtGui.QActionGroup(self)
        self.theme_action_group.setExclusive(True)
        self.theme_actions: dict[str, QtGui.QAction] = {}
        for mode, label in [("system", "Follow system"), ("light", "Light"), ("dark", "Dark")]:
            action = theme_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, selected_mode=mode: self.apply_theme_mode(selected_mode, persist=True))
            self.theme_action_group.addAction(action)
            self.theme_actions[mode] = action

        view_menu.addSeparator()
        raw_action = view_menu.addAction("Return to Raw Traces")
        raw_action.triggered.connect(self.show_raw_traces)

        view_menu.addSeparator()
        self.sidebar_action = view_menu.addAction("Workflow Rail")
        self.sidebar_action.setCheckable(True)
        self.sidebar_action.setChecked(True)
        self.sidebar_action.triggered.connect(
            lambda visible: self.set_workspace_panel_visible(
                "sidebar",
                visible,
            )
        )
        self.inspector_action = view_menu.addAction("Spot and isotope inspector")
        self.inspector_action.setCheckable(True)
        self.inspector_action.setChecked(True)
        self.inspector_action.triggered.connect(
            lambda visible: self.set_workspace_panel_visible(
                "inspector",
                visible,
            )
        )
        self.notes_log_action = view_menu.addAction("Reduction Notes and Activity Log")
        self.notes_log_action.setCheckable(True)
        self.notes_log_action.setChecked(True)
        self.notes_log_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+L"))
        self.notes_log_action.triggered.connect(self.set_notes_log_visible)
        view_menu.addSeparator()
        reset_layout_action = view_menu.addAction("Reset Layout")
        reset_layout_action.setToolTip(
            "Forget the saved window size and panel widths and fit the "
            "default layout to this screen."
        )
        reset_layout_action.triggered.connect(self.reset_layout)

        window_menu = self.menuBar().addMenu("Window")
        window_new_action = window_menu.addAction("New Reduction Window")
        window_new_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+N"))
        window_new_action.triggered.connect(self.new_window_requested.emit)
        next_window_action = window_menu.addAction("Next Reduction Window")
        next_window_action.setShortcut(QtGui.QKeySequence("Ctrl+Tab"))
        next_window_action.triggered.connect(self.next_window_requested.emit)
        window_menu.addSeparator()
        close_window_action = window_menu.addAction("Close Window")
        close_window_action.setShortcut(
            QtGui.QKeySequence.StandardKey.Close
        )
        close_window_action.triggered.connect(self.close)

        help_menu = self.menuBar().addMenu("Help")
        guide_action = help_menu.addAction("Reduction Workflow Guide")
        guide_action.triggered.connect(self.show_pipeline_guide)
        about_action = help_menu.addAction("About UPbRPy Studio")
        about_action.triggered.connect(self.show_about_dialog)

    def select_workflow_preset(self, workflow_key: str) -> None:
        """Select a mineral workflow from the native menu."""
        if not hasattr(self, "workflow_combo"):
            return
        index = self.workflow_combo.findData(workflow_key)
        if index >= 0:
            self.workflow_combo.setCurrentIndex(index)

    def select_selection_method(self, method: str) -> None:
        """Select an auto-pick method from the native menu."""
        if not hasattr(self, "selection_method_combo"):
            return
        index = self.selection_method_combo.findData(method)
        if index >= 0:
            self.selection_method_combo.setCurrentIndex(index)

    def select_fit_method(self, correction: str, method: str) -> None:
        """Select a downhole or session model from the Calibration menu."""
        combo = (
            self.downhole_fit_combo
            if correction == "downhole"
            else self.session_fit_combo
        )
        index = combo.findData(method)
        if index >= 0:
            combo.setCurrentIndex(index)
        self.set_workspace_panel_visible("inspector", True)

    def go_to_workflow_step(self, offset: int) -> None:
        """Move to the next selectable workflow stage."""
        if not hasattr(self, "workflow_step_list"):
            return
        row = self.workflow_step_list.currentRow()
        candidate = max(
            0,
            min(self.workflow_step_list.count() - 1, row + offset),
        )
        while candidate != row:
            item = self.workflow_step_list.item(candidate)
            if item is not None and (
                item.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled
            ):
                self.workflow_step_list.setCurrentRow(candidate)
                return
            candidate += 1 if offset > 0 else -1
            if candidate < 0 or candidate >= self.workflow_step_list.count():
                return

    def run_current_workflow_action(self) -> None:
        """Run the primary action attached to the selected workflow stage."""
        actions = self._workflow_action_specs()
        if actions:
            actions[0][1]()

    def show_pipeline_guide(self) -> None:
        """Show the ordered reduction stages without leaving the application."""
        lines = [
            f"{index + 1}. {step.title}"
            for index, step in enumerate(self.workflow_preset.steps)
        ]
        QtWidgets.QMessageBox.information(
            self,
            f"{self.workflow_preset.title} reduction workflow",
            "\n".join(lines)
            + "\n\nSelect a stage in the left rail for its explanation "
            "and available actions.",
        )

    def show_about_dialog(self) -> None:
        """Show concise application and shared-core information."""
        QtWidgets.QMessageBox.about(
            self,
            "About UPbRPy Studio",
            "<b>UPbRPy Studio</b><br>"
            "Native, reproducible LA-ICP-MS U-Pb and depth-profile "
            "data reduction.<br><br>"
            "The desktop interface and exported notebooks use the same "
            "open Python calculation pipeline.",
        )

    def scientific_project_fingerprint(self) -> str:
        """Serialize user decisions that materially define a reduction."""
        if not self.session.loaded:
            return ""
        payload = {
            "workflow_key": self.workflow_preset.key,
            "primary_channel": self.session.primary_channel,
            "import_settings": self.session.import_settings,
            "concentration_standards": self.session.concentration_standards,
            "primary_standards": (
                self.session.primary_68_standard,
                self.session.primary_76_standard,
                self.session.primary_82_standard,
            ),
            "secondary_standards": self.session.secondary_standards,
            "ratio_aggregation": self.session.ratio_aggregation,
            "downhole_fit": (
                self.session.downhole_fit_method,
                self.session.downhole_fit_options,
                self.session.downhole_outlier_sigma,
            ),
            "session_fit": (
                self.session.session_fit_method,
                self.session.session_fit_options,
            ),
            "decision": (
                self.session.common_pb_method,
                self.session.common_pb_uncertainty_preset_name,
                self.session.include_calibration_uncertainty,
                self.session.age_transition_ma,
            ),
            "uncertainty": (
                self.session.propagate_decay_constants,
                self.session.propagate_u238_u235,
                self.session.long_term_reproducibility_mode,
                self.session.long_term_reproducibility_relative_1s,
            ),
            "export": (
                self.session.include_export_concentrations,
                self.session.include_export_depth_concentrations,
            ),
            "display_choices": {
                "visible_channels": self.checked_channels(),
                "selected_stats_channel": self.selected_stats_channel,
                "sigma_level": self.sigma_level,
                "channel_colors": self.channel_colors,
                "selection_colors": self.selection_colors,
                "spot_folder_overrides": self._spot_folder_overrides,
            },
            "selections": selection_export_frame(
                self.session.experiment
            ).to_dict(orient="records"),
        }
        return json.dumps(
            payload,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )

    def has_unsaved_changes(self) -> bool:
        """Return whether scientific settings differ from the last project save."""
        if not self.session.loaded:
            return False
        if self.pending_region_updates:
            return True
        if self._saved_project_fingerprint is None:
            return True
        return (
            self.scientific_project_fingerprint()
            != self._saved_project_fingerprint
        )

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        """Prevent accidental loss of an imported or edited reduction."""
        if self._active_worker is not None:
            QtWidgets.QMessageBox.information(
                self,
                "Reduction still running",
                f"Please wait for {self._busy_label.lower()} to finish before "
                "closing this reduction window.",
            )
            event.ignore()
            return
        if not self.has_unsaved_changes():
            self.persist_layout_state()
            event.accept()
            return
        dialog = QtWidgets.QMessageBox(self)
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Save reduction project?")
        dialog.setText("This reduction has changes that are not saved in a .upbr project.")
        dialog.setInformativeText(
            "Save the lightweight project before closing, discard these changes, "
            "or cancel and continue working."
        )
        dialog.setStandardButtons(
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Save)
        choice = dialog.exec()
        if choice == QtWidgets.QMessageBox.StandardButton.Discard:
            self.persist_layout_state()
            event.accept()
            return
        if choice == QtWidgets.QMessageBox.StandardButton.Save:
            self.save_project()
            if not self.has_unsaved_changes():
                self.persist_layout_state()
                event.accept()
                return
        event.ignore()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        self.workspace_splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Horizontal
        )
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(10)
        # Resize continuously under the pointer. A rubber-band resize felt
        # like snapping and allowed the old responsive hide/show logic to win
        # when the mouse was released.
        self.workspace_splitter.setOpaqueResize(True)
        self.sidebar_panel = self._build_workflow_rail()
        self.main_panel = self._build_main_column()
        self.inspector_panel = self._build_inspector()
        self.workspace_splitter.addWidget(self.sidebar_panel)
        self.workspace_splitter.addWidget(self.main_panel)
        self.workspace_splitter.addWidget(self.inspector_panel)
        for panel_index in range(3):
            self.workspace_splitter.setCollapsible(panel_index, False)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setStretchFactor(2, 0)
        # Give the inspector enough width for a useful isotope grid.  The
        # centre plot remains the stretchable pane and therefore absorbs
        # changes when the application window is resized.
        self.workspace_splitter.setSizes([260, 1050, 430])
        self.workspace_splitter.splitterMoved.connect(
            self.persist_workspace_splitter_state
        )
        outer.addWidget(self.workspace_splitter, 1)

        self.setCentralWidget(central)
        self.busy_progress = QtWidgets.QProgressBar()
        self.busy_progress.setRange(0, 0)
        self.busy_progress.setFixedWidth(150)
        self.busy_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.busy_progress)
        self.capabilities_badge = QtWidgets.QLabel()
        self.capabilities_badge.setObjectName("QtegraCapabilitiesBadge")
        self.capabilities_badge.hide()
        self.statusBar().addPermanentWidget(self.capabilities_badge)

        self.statusBar().showMessage("Ready. Load an experiment directory to begin.")
        self._populate_workflow_ui()
        self._sync_fit_controls()
        self.spot_channel_splitter.splitterMoved.connect(
            lambda _pos, _index: self.persist_layout_state()
        )
        self._layout_restored = self._restore_layout_state()
        application = QtWidgets.QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.persist_layout_state)

    # ── Layout: small screens, remembered geometry, reset ─────────────────
    LAYOUT_SETTINGS = {
        "notes_log": "notes_log_visible_v1",
        "geometry": "window_geometry_v1",
        "workspace": "workspace_splitter_state_v4",
        "content": "content_splitter_state_v1",
        "spot_channel": "spot_channel_splitter_state_v1",
    }

    def _layout_splitters(self) -> dict[str, QtWidgets.QSplitter]:
        return {
            "workspace": self.workspace_splitter,
            "content": self.content_splitter,
            "spot_channel": self.spot_channel_splitter,
        }

    def _restore_layout_state(self) -> bool:
        """Restore saved splitter positions; ``False`` if none were saved."""
        restored = False
        for name, splitter in self._layout_splitters().items():
            state = self.settings.value(self.LAYOUT_SETTINGS[name])
            if isinstance(state, QtCore.QByteArray) and not state.isEmpty():
                restored = splitter.restoreState(state) or restored
        notes = self.settings.value(self.LAYOUT_SETTINGS["notes_log"])
        if notes is not None:
            self.lower_container.setVisible(str(notes).lower() in {"true", "1"})
        self._sync_notes_log_action()
        return restored

    def persist_layout_state(self) -> None:
        """Remember the window geometry and every splitter position."""
        try:
            self.settings.setValue(self.LAYOUT_SETTINGS["geometry"], self.saveGeometry())
            for name, splitter in self._layout_splitters().items():
                self.settings.setValue(self.LAYOUT_SETTINGS[name], splitter.saveState())
            self.settings.setValue(self.LAYOUT_SETTINGS["notes_log"], self.notes_log_visible())
        except RuntimeError:  # the C++ window is already gone at quit
            return
        self._sync_notes_log_action()

    def default_window_size(self) -> QtCore.QSize:
        """A window that fits the available screen (a 14-inch laptop too)."""
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return QtCore.QSize(1600, 1000)
        available = screen.availableGeometry()
        width = min(1800, max(960, int(available.width() * 0.92)), available.width())
        height = min(1150, max(680, int(available.height() * 0.90)), available.height())
        return QtCore.QSize(width, height)

    def fit_window_to_screen(self) -> None:
        """Shrink and move the window so all of it is on its screen."""
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        extra_w = max(0, frame.width() - self.width())
        extra_h = max(0, frame.height() - self.height())
        width = min(self.width(), available.width() - extra_w)
        height = min(self.height(), available.height() - extra_h)
        if (width, height) != (self.width(), self.height()):
            self.resize(max(width, self.minimumWidth()), max(height, self.minimumHeight()))
        frame = self.frameGeometry()
        x = min(max(frame.x(), available.left()), available.right() - frame.width() + 1)
        y = min(max(frame.y(), available.top()), available.bottom() - frame.height() + 1)
        if (x, y) != (frame.x(), frame.y()):
            self.move(x, y)

    def apply_default_layout(self) -> None:
        """Panel widths and the notes/log strip chosen for this window size."""
        width = max(self.width(), 760)
        compact = width < 1500
        rail = 240 if compact else 260
        inspector = 340 if compact else 430
        self.workspace_splitter.setSizes([rail, max(400, width - rail - inspector - 40), inspector])
        self.set_notes_log_visible(self.height() >= 950)
        self.spot_channel_splitter.setSizes([3, 2])

    def reset_layout(self) -> None:
        """View > Reset Layout: forget saved sizes, fit the default to the screen."""
        for key in self.LAYOUT_SETTINGS.values():
            self.settings.remove(key)
        self.set_workspace_panel_visible("sidebar", True)
        self.set_workspace_panel_visible("inspector", True)
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        self.resize(self.default_window_size())
        self.fit_window_to_screen()
        QtWidgets.QApplication.processEvents()
        self.apply_default_layout()
        self.statusBar().showMessage("Layout reset to fit this screen.")

    def set_notes_log_visible(self, visible: bool) -> None:
        """Show or hide the reduction notes and activity log strip.

        Hidden rather than merely collapsed: a collapsed splitter pane still
        adds its minimum height to the column and would squeeze the header
        and plot on a short laptop screen.
        """
        total = max(sum(self.content_splitter.sizes()), 1)
        self.lower_container.setVisible(bool(visible))
        if visible:
            lower = min(220, max(self.lower_container.minimumSizeHint().height(), total // 5))
            self.content_splitter.setSizes([max(total - lower, 1), lower])
        self._sync_notes_log_action(bool(visible))

    def notes_log_visible(self) -> bool:
        if self.lower_container.isHidden():
            return False
        sizes = self.content_splitter.sizes()
        return len(sizes) > 1 and sizes[1] > 0

    def _on_content_splitter_moved(self) -> None:
        # Dragged fully closed: hide it so it stops costing height.
        if not self.lower_container.isHidden() and self.content_splitter.sizes()[1] == 0:
            self.lower_container.setVisible(False)
        self._sync_notes_log_action()
        self.persist_layout_state()

    def _sync_notes_log_action(self, visible: bool | None = None) -> None:
        open_now = self.notes_log_visible() if visible is None else visible
        for control in (getattr(self, "notes_log_action", None),):
            if control is None:
                continue
            control.blockSignals(True)
            control.setChecked(open_now)
            control.blockSignals(False)

    def _sync_toolbar_strips(self) -> None:
        """Size the scrolling button strips from their themed size hints.

        The strips are built before the theme's padding is applied, so the
        heights measured at construction would clip the buttons.
        """
        for scroll, bar in getattr(self, "_toolbar_strips", ()):
            # The buttons' cached size hints predate the stylesheet padding.
            for child in bar.findChildren(QtWidgets.QWidget):
                child.updateGeometry()
            bar.layout().invalidate()
            bar.layout().activate()
            hint = bar.sizeHint()
            margins = bar.layout().contentsMargins()
            height = max(
                [hint.height()]
                + [
                    child.sizeHint().height() + margins.top() + margins.bottom()
                    for child in bar.findChildren(QtWidgets.QWidget)
                    if child.parentWidget() is bar and not child.isHidden()
                ]
            )
            bar.setMinimumWidth(hint.width())
            bar.setFixedHeight(height)
            bar.resize(max(hint.width(), bar.width()), height)
            scroll.setFixedHeight(height + 18)

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._sync_toolbar_strips()
        # Children inside the strips are polished after this event; size
        # the strips again once they carry the stylesheet's padding.
        QtCore.QTimer.singleShot(0, self._sync_toolbar_strips)
        if not getattr(self, "_first_show_done", False):
            self._first_show_done = True
            if self.size() == getattr(self, "_initial_size", None):
                self.fit_window_to_screen()
            if not self._layout_restored:
                self.apply_default_layout()

    def persist_workspace_splitter_state(
        self,
        _position: int,
        _index: int,
    ) -> None:
        """Remember a valid user-adjusted three-panel layout."""
        self.persist_layout_state()

    def set_workspace_panel_visible(self, panel: str, visible: bool) -> None:
        """Expose responsive side panels through the View menu."""
        widget = (
            self.sidebar_panel
            if panel == "sidebar"
            else self.inspector_panel
        )
        widget.setVisible(visible)
        action = (
            self.sidebar_action
            if panel == "sidebar"
            else self.inspector_action
        )
        action.blockSignals(True)
        action.setChecked(visible)
        action.blockSignals(False)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        """Adapt vertical density without overriding user panel widths."""
        super().resizeEvent(event)
        if not hasattr(self, "workspace_splitter"):
            return
        # Side-panel visibility is an explicit View-menu choice. Automatically
        # hiding a panel while the window or a splitter was being adjusted
        # made the handles appear to jump and collapse whole columns.
        # The notes/log strip is a collapsible splitter pane (View menu)
        # rather than something hidden below a fixed window height.
        if hasattr(self, "header_card"):
            self.apply_header_density(event.size().height())
        if hasattr(self, "workflow_step_list"):
            self.adapt_workflow_rail_height(event.size().height())

    def apply_header_density(self, height: int) -> None:
        """A one-line header on short screens leaves the height to the plot."""
        compact = height < 880
        self.header_subtitle.setVisible(not compact)
        # The theme buttons repeat View > Theme; the engine label is static.
        for widget in (self.project_engine_label, self._header_separator, *self.theme_mode_buttons.values()):
            widget.setVisible(not compact)
        self.header_card.layout().setContentsMargins(20, 4 if compact else 10, 20, 4 if compact else 10)
        self.header_card.setMinimumHeight(68 if compact else 84)
        self.header_card.setMaximumHeight(68 if compact else 96)

    def adapt_workflow_rail_height(self, height: int) -> None:
        """Keep every workflow stage visible on short displays.

        Below 940 px (a 14-inch laptop) the auto-pick controls fold behind
        the "Auto-pick settings" toggle instead of pushing the stage list
        into a scroll area; the reference library stays in the File menu.
        """
        compact = height < 940
        very_compact = height < 700
        toggle = self.autopick_settings_toggle
        toggle.setVisible(compact)
        show_autopick = not compact or toggle.isChecked()
        toggle.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if toggle.isChecked() else QtCore.Qt.ArrowType.RightArrow
        )
        self.selection_method_combo.setVisible(show_autopick)
        self.selection_parameter_stack.setVisible(show_autopick)
        self.plateau_width_control.setVisible(show_autopick)
        self.plateau_filter_combo.setVisible(show_autopick)
        self.reference_library_button.setVisible(not compact)
        self.workflow_combo.setVisible(not very_compact)
        item_height = 37 if compact else 45
        for index in range(self.workflow_step_list.count()):
            item = self.workflow_step_list.item(index)
            if item is not None:
                item.setSizeHint(QtCore.QSize(0, item_height))

    def _apply_initial_window_geometry(self) -> None:
        """Restore the saved window geometry, else fit a default to the screen.

        Saved geometry from a larger monitor is clamped on first show
        (:meth:`fit_window_to_screen`).
        """
        saved = self.settings.value(self.LAYOUT_SETTINGS["geometry"])
        if not (
            isinstance(saved, QtCore.QByteArray)
            and not saved.isEmpty()
            and self.restoreGeometry(saved)
        ):
            self.resize(self.default_window_size())
        # Clamped to the screen on first show unless a caller resizes first.
        self._initial_size = self.size()

    def _build_workflow_rail(self) -> QtWidgets.QWidget:
        """Build the always-readable icon rail for ordered reduction stages."""
        frame = NeumorphicFrame()
        frame.setObjectName("WorkflowRail")
        frame.setMinimumWidth(170)
        frame.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(8)

        brand_row = QtWidgets.QHBoxLayout()
        brand_row.setSpacing(8)
        brand_icon = QtWidgets.QLabel()
        brand_icon.setPixmap(build_brand_pixmap(38))
        brand_icon.setFixedSize(38, 38)
        brand_text = QtWidgets.QLabel("UPbRPy")
        brand_text.setObjectName("RailBrand")
        brand_row.addWidget(brand_icon)
        brand_row.addWidget(brand_text, 1)
        layout.addLayout(brand_row)

        self.workflow_combo = StyledComboBox()
        for key, preset in WORKFLOW_PRESETS.items():
            self.workflow_combo.addItem(preset.title, key)
        self.workflow_combo.setToolTip("Choose the mineral-specific workflow")
        self.workflow_combo.currentIndexChanged.connect(
            self.on_workflow_preset_changed
        )
        layout.addWidget(self.workflow_combo)

        # On short screens the auto-pick controls fold away behind this
        # toggle so every workflow stage stays in view; one click opens them.
        self.autopick_settings_toggle = QtWidgets.QToolButton()
        self.autopick_settings_toggle.setObjectName("AutopickSettingsToggle")
        self.autopick_settings_toggle.setText("Auto-pick settings")
        self.autopick_settings_toggle.setCheckable(True)
        self.autopick_settings_toggle.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.autopick_settings_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.autopick_settings_toggle.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.autopick_settings_toggle.setVisible(False)
        self.autopick_settings_toggle.toggled.connect(
            lambda _checked: self.adapt_workflow_rail_height(self.height())
        )
        layout.addWidget(self.autopick_settings_toggle)

        self.selection_method_combo = StyledComboBox()
        for method in SELECTION_METHODS:
            self.selection_method_combo.addItem(
                selection_method_label(method),
                method,
            )
        self.selection_method_combo.setToolTip(
            "Baseline and signal auto-pick method"
        )
        self.selection_method_combo.currentIndexChanged.connect(
            self.on_selection_method_changed
        )
        for combo in (self.workflow_combo, self.selection_method_combo):
            combo.setMinimumContentsLength(10)
            combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
        layout.addWidget(self.selection_method_combo)

        self.selection_parameter_stack = QtWidgets.QStackedWidget()
        self.selection_parameter_stack.setObjectName(
            "SelectionParameterStack"
        )

        threshold_page = QtWidgets.QWidget()
        threshold_layout = QtWidgets.QFormLayout(threshold_page)
        threshold_layout.setContentsMargins(0, 0, 0, 0)
        threshold_layout.setSpacing(4)
        threshold_layout.setRowWrapPolicy(
            QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows
        )
        self.selection_threshold_percent_spin = QtWidgets.QDoubleSpinBox()
        self.selection_threshold_percent_spin.setRange(0.1, 200.0)
        self.selection_threshold_percent_spin.setDecimals(1)
        self.selection_threshold_percent_spin.setSingleStep(5.0)
        self.selection_threshold_percent_spin.setSuffix(" %")
        self.selection_threshold_percent_spin.setToolTip(
            "Required signal level as a percentage of the high-signal "
            "reference mean."
        )
        self.selection_threshold_percent_spin.valueChanged.connect(
            self.on_selection_threshold_percent_changed
        )
        threshold_layout.addRow(
            "Signal level",
            self.selection_threshold_percent_spin,
        )
        self.selection_reference_percentile_spin = QtWidgets.QDoubleSpinBox()
        self.selection_reference_percentile_spin.setRange(0.0, 100.0)
        self.selection_reference_percentile_spin.setDecimals(1)
        self.selection_reference_percentile_spin.setSingleStep(1.0)
        self.selection_reference_percentile_spin.setSuffix(" %ile")
        self.selection_reference_percentile_spin.setToolTip(
            "Percentile used to identify the high-signal reference "
            "population before thresholding."
        )
        self.selection_reference_percentile_spin.valueChanged.connect(
            self.on_selection_reference_percentile_changed
        )
        threshold_layout.addRow(
            "Reference",
            self.selection_reference_percentile_spin,
        )
        self.selection_parameter_stack.addWidget(threshold_page)

        sigma_page = QtWidgets.QWidget()
        sigma_layout = QtWidgets.QFormLayout(sigma_page)
        sigma_layout.setContentsMargins(0, 0, 0, 0)
        sigma_layout.setSpacing(4)
        sigma_layout.setRowWrapPolicy(
            QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows
        )
        self.selection_sigma_spin = QtWidgets.QDoubleSpinBox()
        self.selection_sigma_spin.setRange(0.1, 50.0)
        self.selection_sigma_spin.setDecimals(1)
        self.selection_sigma_spin.setSingleStep(0.5)
        self.selection_sigma_spin.setSuffix(" σ")
        self.selection_sigma_spin.setToolTip(
            "Number of baseline standard deviations required for signal onset."
        )
        self.selection_sigma_spin.valueChanged.connect(
            self.on_selection_sigma_changed
        )
        sigma_layout.addRow("Onset threshold", self.selection_sigma_spin)
        self.selection_parameter_stack.addWidget(sigma_page)
        layout.addWidget(self.selection_parameter_stack)

        self.plateau_width_control = QtWidgets.QWidget()
        plateau_width_layout = QtWidgets.QFormLayout(
            self.plateau_width_control
        )
        plateau_width_layout.setContentsMargins(0, 0, 0, 0)
        plateau_width_layout.setSpacing(4)
        plateau_width_layout.setRowWrapPolicy(
            QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows
        )
        self.plateau_duration_spin = QtWidgets.QDoubleSpinBox()
        self.plateau_duration_spin.setRange(0.0, 600.0)
        self.plateau_duration_spin.setDecimals(1)
        self.plateau_duration_spin.setSingleStep(5.0)
        self.plateau_duration_spin.setSuffix(" s")
        self.plateau_duration_spin.setSpecialValueText("Workflow auto")
        self.plateau_duration_spin.setToolTip(
            "Workflow auto uses the mineral preset: signal-aware width for "
            "apatite depth profiles and preset sweep counts where retained. "
            "Enter a duration to impose the same plateau width on every spot "
            "the next time Re-pick all spots is used."
        )
        self.plateau_duration_spin.valueChanged.connect(
            self.on_plateau_duration_changed
        )
        plateau_width_layout.addRow(
            "Plateau width",
            self.plateau_duration_spin,
        )
        layout.addWidget(self.plateau_width_control)

        self.plateau_filter_combo = StyledComboBox()
        self.plateau_filter_combo.addItem("Plateau: no filter", "none")
        self.plateau_filter_combo.addItem("Plateau: 2σ trend", "excel_2sigma")
        self.plateau_filter_combo.setToolTip(
            "Spot-mean plateau filter for the current spot. Excluded sweeps "
            "remain visible and remain available to full-signal depth processing."
        )
        self.plateau_filter_combo.currentIndexChanged.connect(
            self.on_plateau_filter_method_changed
        )
        layout.addWidget(self.plateau_filter_combo)

        self.repick_all_spots_button = QtWidgets.QPushButton(
            "Re-pick all spots"
        )
        self.repick_all_spots_button.setProperty("accent", True)
        self.repick_all_spots_button.setEnabled(False)
        self.repick_all_spots_button.setToolTip(
            "Apply the displayed selection method and threshold settings to "
            "every spot, replacing their baseline, plateau, signal-start, "
            "and signal-end auto-picks."
        )
        self.repick_all_spots_button.clicked.connect(
            self.run_autopick_all_spots
        )
        layout.addWidget(self.repick_all_spots_button)

        section_label = QtWidgets.QLabel("Reduction workflow")
        section_label.setObjectName("SectionTitle")
        layout.addWidget(section_label)

        self.workflow_step_list = QtWidgets.QListWidget()
        self.workflow_step_list.setObjectName("WorkflowStepRail")
        self.workflow_step_list.setIconSize(QtCore.QSize(28, 28))
        self.workflow_step_list.setWordWrap(False)
        self.workflow_step_list.setTextElideMode(
            QtCore.Qt.TextElideMode.ElideRight
        )
        self.workflow_step_list.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.workflow_step_list.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.workflow_step_list.setSpacing(1)
        self.workflow_step_list.currentRowChanged.connect(
            self.on_workflow_step_changed
        )
        layout.addWidget(self.workflow_step_list, 1)

        self.database_origin_label = ElidedLabel()
        self.database_origin_label.setObjectName("DatabaseOrigin")
        self.database_origin_label.setVisible(False)
        layout.addWidget(self.database_origin_label)

        self.save_project_button = QtWidgets.QPushButton("Save project")
        self.save_project_button.setToolTip(
            "Save a lightweight .upbr file containing source paths, reviewed "
            "selections, workflow settings, and display choices."
        )
        self.save_project_button.clicked.connect(
            lambda _checked=False: self.save_project()
        )
        layout.addWidget(self.save_project_button)

        self.export_results_button = QtWidgets.QPushButton("Export results…")
        self.export_results_button.setProperty("accent", True)
        self.export_results_button.setToolTip(
            "Export publication-ready spot and depth-profile tables, audit "
            "CSVs, provenance, and a reproducible Jupyter notebook."
        )
        self.export_results_button.clicked.connect(
            self.choose_reduction_bundle_export
        )
        layout.addWidget(self.export_results_button)

        self.import_button = QtWidgets.QPushButton("Open experiment…")
        self.import_button.clicked.connect(self.choose_experiment_directory)
        layout.addWidget(self.import_button)

        self.reference_library_button = QtWidgets.QPushButton(
            "Reference library…"
        )
        self.reference_library_button.clicked.connect(
            self.choose_reference_material_library
        )
        layout.addWidget(self.reference_library_button)

        # These labels retain programmatic access to the resolved library
        # paths; the compact rail exposes them as a tooltip rather than as two
        # permanently wrapped path labels.
        self.reference_library_label = QtWidgets.QLabel()
        self.reference_library_label.setVisible(False)
        self.reference_library_default_label = QtWidgets.QLabel()
        self.reference_library_default_label.setVisible(False)

        self.workflow_description_label = QtWidgets.QLabel()
        self.workflow_description_label.setVisible(False)
        self.workflow_defaults_label = QtWidgets.QLabel()
        self.workflow_defaults_label.setVisible(False)
        self._workflow_info_btn = QtWidgets.QToolButton()
        self._workflow_info_btn.setVisible(False)
        return frame

    def _build_workflow_context_bar(self) -> QtWidgets.QWidget:
        """Show the selected stage explanation and actions beside the plot."""
        card = QtWidgets.QFrame()
        card.setObjectName("WorkflowContextBar")
        self.workflow_context_bar = card
        card.setMinimumHeight(56)
        card.setMaximumHeight(66)
        card_layout = QtWidgets.QHBoxLayout(card)
        card_layout.setContentsMargins(12, 6, 12, 6)
        card_layout.setSpacing(10)

        self.workflow_context_icon = QtWidgets.QLabel()
        self.workflow_context_icon.setFixedSize(32, 32)
        self.workflow_context_icon.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter
        )
        card_layout.addWidget(self.workflow_context_icon)

        copy_layout = QtWidgets.QVBoxLayout()
        copy_layout.setSpacing(2)
        self.workflow_hint_title = QtWidgets.QLabel()
        self.workflow_hint_title.setObjectName("WorkflowContextTitle")
        self.workflow_hint_title.setMinimumWidth(0)
        self.workflow_hint_title.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.workflow_hint_body = QtWidgets.QLabel()
        self.workflow_hint_body.setObjectName("PanelBody")
        self.workflow_hint_body.setWordWrap(False)
        self.workflow_hint_body.setMaximumHeight(18)
        self.workflow_hint_body.setMinimumWidth(0)
        self.workflow_hint_body.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        copy_layout.addWidget(self.workflow_hint_title)
        copy_layout.addWidget(self.workflow_hint_body)
        card_layout.addLayout(copy_layout, 1)

        actions_card = QtWidgets.QFrame()
        actions_card.setObjectName("WorkflowActionsInline")
        self.workflow_actions_card = actions_card
        self.workflow_actions_layout = QtWidgets.QHBoxLayout(actions_card)
        self.workflow_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.workflow_actions_layout.setSpacing(6)
        card_layout.addWidget(actions_card)

        self.workflow_status_label = QtWidgets.QLabel()
        self.workflow_status_label.setObjectName("WorkflowStatusCompact")
        self.workflow_status_label.setWordWrap(False)
        self.workflow_status_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.workflow_status_label.setMinimumWidth(0)
        self.workflow_status_label.setMaximumWidth(240)
        self.workflow_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        card_layout.addWidget(self.workflow_status_label)
        return card

    def _build_main_column(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = NeumorphicFrame()
        header.setObjectName("HeaderCard")
        header.setMinimumHeight(84)
        header.setMaximumHeight(96)
        self.header_card = header
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(20, 10, 20, 10)
        header_layout.setSpacing(18)
        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(2)
        title = ElidedLabel("U-Pb reduction workspace")
        title.setObjectName("HeroTitle")
        subtitle = ElidedLabel(
            "Shared scientific pipeline, editable selections, and "
            "stage-by-stage diagnostic review."
        )
        subtitle.setObjectName("SecondaryText")
        self.header_subtitle = subtitle
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box, 2)

        summary_box = QtWidgets.QVBoxLayout()
        summary_box.setSpacing(3)
        workspace_row = QtWidgets.QHBoxLayout()
        workspace_row.setSpacing(8)
        workspace_row.addStretch(1)
        workspace_label = QtWidgets.QLabel("Workspace")
        workspace_label.setObjectName("SectionTitle")
        self.project_name_label = ElidedLabel("No experiment loaded")
        self.project_name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.project_name_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        workspace_row.addWidget(workspace_label)
        workspace_row.addWidget(self.project_name_label, 4)

        metadata_row = QtWidgets.QHBoxLayout()
        metadata_row.setSpacing(8)
        metadata_row.addStretch(1)
        self.project_spots_label = ElidedLabel("Import a Thermo iCAP export directory")
        self.project_spots_label.setObjectName("SecondaryText")
        self.project_spots_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.project_engine_label = ElidedLabel("Shared UPbRPy core")
        self.project_engine_label.setObjectName("SecondaryText")
        self.project_engine_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        metadata_row.addWidget(self.project_spots_label, 3)
        metadata_separator = QtWidgets.QLabel("·")
        metadata_separator.setObjectName("SecondaryText")
        self._header_separator = metadata_separator
        metadata_row.addWidget(metadata_separator)
        metadata_row.addWidget(self.project_engine_label, 2)

        self.theme_mode_buttons: dict[str, QtWidgets.QToolButton] = {}
        self.theme_mode_group = QtWidgets.QButtonGroup(self)
        self.theme_mode_group.setExclusive(True)
        for tmode, tlabel, tooltip in [
            ("system", "A", "Follow system theme"),
            ("light", "L", "Light theme"),
            ("dark", "D", "Dark theme"),
        ]:
            tbtn = QtWidgets.QToolButton()
            tbtn.setText(tlabel)
            tbtn.setToolTip(tooltip)
            tbtn.setCheckable(True)
            tbtn.setProperty("modeToggle", True)
            tbtn.setProperty("compactToggle", True)
            tbtn.setFixedSize(36, 30)
            tbtn.clicked.connect(lambda _checked=False, selected_tmode=tmode: self.apply_theme_mode(selected_tmode, persist=True))
            self.theme_mode_group.addButton(tbtn)
            self.theme_mode_buttons[tmode] = tbtn
            metadata_row.addWidget(tbtn)
        summary_box.addLayout(workspace_row)
        summary_box.addLayout(metadata_row)
        header_layout.addLayout(summary_box, 2)
        layout.addWidget(header)
        layout.addWidget(self._build_workflow_context_bar())

        plot_card = NeumorphicFrame()
        plot_card.setObjectName("PlotCard")
        plot_layout = QtWidgets.QVBoxLayout(plot_card)
        plot_layout.setContentsMargins(12, 12, 12, 12)
        plot_layout.setSpacing(12)
        mode_bar = QtWidgets.QWidget()
        mode_row = QtWidgets.QHBoxLayout(mode_bar)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(8)
        self.plot_mode_buttons: dict[str, QtWidgets.QToolButton] = {}
        self.plot_mode_group = QtWidgets.QButtonGroup(self)
        self.plot_mode_group.setExclusive(True)
        for key, label in PLOT_MODE_LABELS.items():
            button = QtWidgets.QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setProperty("modeToggle", True)
            button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setSizePolicy(
                QtWidgets.QSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Fixed,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
            )
            label_lines = label.split("\n")
            button.setMinimumWidth(max(self.fontMetrics().horizontalAdvance(line) for line in label_lines) + 34)
            button.setMinimumHeight(58 if len(label_lines) > 1 else 42)
            button.clicked.connect(lambda _checked=False, plot_mode=key: self.set_plot_mode(plot_mode, refresh=True))
            self.plot_mode_group.addButton(button)
            self.plot_mode_buttons[key] = button
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        ratio_row_lbl = QtWidgets.QLabel("Ratio")
        ratio_row_lbl.setObjectName("ControlBarLabel")
        mode_row.addWidget(ratio_row_lbl)
        self.ratio_combo = StyledComboBox()
        for ratio_name in ["206Pb/238U", "207Pb/206Pb", "208Pb/232Th"]:
            self.ratio_combo.addItem(ratio_name, ratio_name)
        self.ratio_combo.currentIndexChanged.connect(self.on_ratio_changed)
        mode_row.addWidget(self.ratio_combo)
        mode_bar.setMinimumWidth(mode_row.sizeHint().width())
        mode_scroll = QtWidgets.QScrollArea()
        mode_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        mode_scroll.setWidgetResizable(False)
        mode_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        mode_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        mode_scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        mode_scroll.setFixedHeight(mode_bar.sizeHint().height() + 18)
        mode_scroll.setWidget(mode_bar)
        self._toolbar_strips = [(mode_scroll, mode_bar)]
        plot_layout.addWidget(mode_scroll)

        # ── Plot canvas ────────────────────────────────────────────────────
        # Page 0: trace + session overview
        plots_page0 = QtWidgets.QWidget()
        plots_page0.setContentsMargins(0, 0, 0, 0)
        plots_vbox = QtWidgets.QVBoxLayout(plots_page0)
        plots_vbox.setContentsMargins(0, 0, 0, 0)
        plots_vbox.setSpacing(0)
        self.trace_plot = TracePlotWidget()
        self.trace_plot.set_sigma_level(self.sigma_level)
        self.trace_plot.regionChanged.connect(self.on_plot_region_changed)
        self.trace_plot.xWindowChanged.connect(self.on_trace_x_window_changed)
        self._overview_y_fit_timer = QtCore.QTimer(self)
        self._overview_y_fit_timer.setSingleShot(True)
        self._overview_y_fit_timer.setInterval(45)
        self._overview_y_fit_timer.timeout.connect(
            self.trace_plot.fit_y_to_visible_data
        )
        plots_vbox.addWidget(self.trace_plot)
        self.session_overview_plot = SessionOverviewWidget()
        self.session_overview_plot.windowChanged.connect(self.on_overview_window_changed)
        plots_vbox.addWidget(self.session_overview_plot)

        # Diagnostic plots and the final publication-oriented review table.
        self.calibration_slopes_plot = CalibrationSlopesWidget()
        self.downhole_frac_plot = DownholeFractionationWidget()
        self.session_frac_plot = SessionFractionationWidget()
        self.downhole_frac_plot.set_sigma_level(self.sigma_level)
        self.session_frac_plot.set_sigma_level(self.sigma_level)
        self.results_table = ResultsTableWidget()
        self.results_table.tabs.currentChanged.connect(
            self.on_results_review_tab_changed
        )

        self.plots_stack = QtWidgets.QStackedWidget()
        self.plots_stack.addWidget(plots_page0)               # index 0
        self.plots_stack.addWidget(self.calibration_slopes_plot)  # index 1
        self.plots_stack.addWidget(self.downhole_frac_plot)   # index 2
        self.plots_stack.addWidget(self.session_frac_plot)    # index 3
        self.plots_stack.addWidget(self.results_table)        # index 4
        plot_layout.addWidget(self.plots_stack)

        # ── Control bar below both plots ───────────────────────────────────
        control_bar = QtWidgets.QFrame()
        control_bar.setObjectName("PlotControlBar")
        self.plot_control_bar = control_bar
        cb_layout = QtWidgets.QHBoxLayout(control_bar)
        cb_layout.setContentsMargins(8, 6, 8, 6)
        cb_layout.setSpacing(8)

        drag_lbl = QtWidgets.QLabel("Drag")
        drag_lbl.setObjectName("ControlBarLabel")
        cb_layout.addWidget(drag_lbl)
        self.interact_mode_buttons: dict[str, QtWidgets.QToolButton] = {}
        self.interact_mode_group = QtWidgets.QButtonGroup(self)
        self.interact_mode_group.setExclusive(True)
        for imode, imode_label in [("zoom", "Box"), ("pan", "Pan")]:
            ibtn = QtWidgets.QToolButton()
            ibtn.setText(imode_label)
            ibtn.setCheckable(True)
            ibtn.setProperty("modeToggle", True)
            ibtn.setSizePolicy(
                QtWidgets.QSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Fixed,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
            )
            ibtn.clicked.connect(lambda _checked=False, selected_imode=imode: self.set_interaction_mode(selected_imode))
            self.interact_mode_group.addButton(ibtn)
            self.interact_mode_buttons[imode] = ibtn
            cb_layout.addWidget(ibtn)
        self.interact_mode_buttons["zoom"].setChecked(True)

        _vsep_drag = QtWidgets.QFrame()
        _vsep_drag.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        _vsep_drag.setObjectName("ControlBarDivider")
        cb_layout.addWidget(_vsep_drag)

        zoom_lbl = QtWidgets.QLabel("Zoom")
        zoom_lbl.setObjectName("ControlBarLabel")
        cb_layout.addWidget(zoom_lbl)
        self.zoom_mode_buttons: dict[str, QtWidgets.QToolButton] = {}
        self.zoom_mode_group = QtWidgets.QButtonGroup(self)
        self.zoom_mode_group.setExclusive(True)
        for mode, mode_label in [("xy", "XY"), ("x", "X only"), ("y", "Y only")]:
            btn = QtWidgets.QToolButton()
            btn.setText(mode_label)
            btn.setCheckable(True)
            btn.setProperty("modeToggle", True)
            btn.setSizePolicy(
                QtWidgets.QSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Fixed,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
            )
            btn.clicked.connect(lambda _checked=False, selected_mode=mode: self.set_zoom_mode(selected_mode))
            self.zoom_mode_group.addButton(btn)
            self.zoom_mode_buttons[mode] = btn
            cb_layout.addWidget(btn)
        self.zoom_mode_buttons["xy"].setChecked(True)

        _vsep1 = QtWidgets.QFrame()
        _vsep1.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        _vsep1.setObjectName("ControlBarDivider")
        cb_layout.addWidget(_vsep1)

        self.reset_scale_button = QtWidgets.QPushButton("Reset scale")
        self.reset_scale_button.setMinimumWidth(110)
        self.reset_scale_button.clicked.connect(self.restore_default_main_view)
        cb_layout.addWidget(self.reset_scale_button)

        _vsep2 = QtWidgets.QFrame()
        _vsep2.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        _vsep2.setObjectName("ControlBarDivider")
        cb_layout.addWidget(_vsep2)

        self.sigma_toggle_button = QtWidgets.QToolButton()
        self.sigma_toggle_button.setProperty("modeToggle", True)
        self.sigma_toggle_button.setCheckable(True)
        self.sigma_toggle_button.setChecked(True)
        self.sigma_toggle_button.setToolTip(
            "Toggle displayed baseline, plateau, downhole-fit, and "
            "session-fit uncertainty envelopes between 1σ and 2σ."
        )
        self.sigma_toggle_button.clicked.connect(self.on_sigma_toggle_clicked)
        self._update_sigma_button_text()
        cb_layout.addWidget(self.sigma_toggle_button)

        # Wrap control bar in a centering row so buttons stay centred under the plots
        control_bar.setMinimumWidth(control_bar.sizeHint().width())
        control_scroll = QtWidgets.QScrollArea()
        control_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        control_scroll.setWidgetResizable(False)
        control_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        control_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        control_scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        control_scroll.setFixedHeight(control_bar.sizeHint().height() + 18)
        control_scroll.setWidget(control_bar)
        self._toolbar_strips.append((control_scroll, control_bar))
        plot_layout.addWidget(control_scroll)

        content_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.addWidget(plot_card)
        self.content_splitter = content_splitter

        lower_container = QtWidgets.QWidget()
        self.lower_container = lower_container
        lower = QtWidgets.QHBoxLayout()
        lower.setSpacing(18)
        lower.setContentsMargins(0, 0, 0, 0)
        lower_container.setLayout(lower)

        notes_card = NeumorphicFrame()
        notes_card.setObjectName("NotesCard")
        notes_layout = QtWidgets.QVBoxLayout(notes_card)
        notes_layout.setContentsMargins(18, 18, 18, 18)
        notes_label = QtWidgets.QLabel("Reduction notes")
        notes_label.setObjectName("SectionTitle")
        self.notes_body = QtWidgets.QLabel()
        self.notes_body.setObjectName("PanelBody")
        self.notes_body.setWordWrap(True)
        notes_layout.addWidget(notes_label)
        notes_layout.addWidget(self.notes_body)
        notes_layout.addStretch(1)
        notes_card.setMaximumHeight(220)
        notes_card.setSizePolicy(
            QtWidgets.QSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Maximum,
            )
        )
        lower.addWidget(notes_card, 1)

        log_card = NeumorphicFrame()
        log_card.setObjectName("NotesCard")
        log_layout = QtWidgets.QVBoxLayout(log_card)
        log_layout.setContentsMargins(18, 12, 18, 12)
        log_label = QtWidgets.QLabel("Activity log")
        log_label.setObjectName("SectionTitle")
        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(60)
        log_layout.addWidget(log_label)
        log_layout.addWidget(self.log_view)
        log_card.setMaximumHeight(220)
        log_card.setSizePolicy(
            QtWidgets.QSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Maximum,
            )
        )
        lower.addWidget(log_card, 1)
        content_splitter.addWidget(lower_container)
        content_splitter.setStretchFactor(0, 5)
        content_splitter.setStretchFactor(1, 2)
        # The notes and activity log may be dragged closed (or toggled in
        # View) so a short laptop screen gives the plot the height instead.
        content_splitter.setCollapsible(1, True)
        content_splitter.setSizes([900, 200])
        content_splitter.splitterMoved.connect(
            lambda _pos, _index: self._on_content_splitter_moved()
        )
        layout.addWidget(content_splitter, 1)
        return widget

    def _build_inspector(self) -> QtWidgets.QWidget:
        """Build a tabbed inspector that prioritizes frequently used controls.

        Spot navigation and isotope visibility are used continuously during
        selection review, whereas calibration models and final-result choices
        are changed only occasionally.  Keeping those groups on separate tabs
        prevents the spot tree and periodic-table grid from competing with a
        long column of session settings for the same vertical space.
        """
        panel = NeumorphicFrame()
        panel.setObjectName("InspectorPanel")
        panel.setMinimumWidth(260)
        panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.inspector_tabs = QtWidgets.QTabWidget()
        self.inspector_tabs.setObjectName("InspectorTabs")
        self.inspector_tabs.setDocumentMode(True)
        # Fusion/macOS styles otherwise draw a separate tab-bar base above
        # the tabs, which looks like a stray horizontal rule in this card.
        self.inspector_tabs.tabBar().setDrawBase(False)
        layout.addWidget(self.inspector_tabs, 1)

        browse_page = QtWidgets.QWidget()
        browse_layout = QtWidgets.QVBoxLayout(browse_page)
        browse_layout.setContentsMargins(6, 8, 6, 6)
        browse_layout.setSpacing(8)

        primary_label = QtWidgets.QLabel("Primary channel")
        primary_label.setObjectName("SectionTitle")
        self.primary_channel_combo = StyledComboBox()
        self.primary_channel_combo.currentTextChanged.connect(self.on_primary_channel_changed)
        browse_layout.addWidget(primary_label)
        browse_layout.addWidget(self.primary_channel_combo)

        # A user-adjustable vertical splitter makes it possible to favor a
        # long spot list or a large isotope grid without hiding either one.
        self.spot_channel_splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Vertical
        )
        self.spot_channel_splitter.setObjectName("SpotChannelSplitter")
        self.spot_channel_splitter.setChildrenCollapsible(False)
        self.spot_channel_splitter.setHandleWidth(7)

        spots_card = NeumorphicFrame()
        spots_card.setObjectName("InspectorSubsection")
        spots_layout = QtWidgets.QVBoxLayout(spots_card)
        spots_layout.setContentsMargins(10, 10, 10, 10)
        spots_layout.setSpacing(6)

        spots_label = QtWidgets.QLabel("Spots")
        spots_label.setObjectName("SectionTitle")

        # Keep folder management beside the section heading so it does not
        # consume a separate row of the high-value browsing area.
        spots_toolbar = QtWidgets.QHBoxLayout()
        spots_toolbar.setContentsMargins(0, 0, 0, 0)
        spots_toolbar.setSpacing(4)
        spots_toolbar.addWidget(spots_label)
        spots_toolbar.addStretch(1)
        _add_folder_btn = QtWidgets.QPushButton("+ Folder")
        _add_folder_btn.setMinimumWidth(80)
        _add_folder_btn.setStyleSheet("QPushButton { padding: 2px 10px; border-radius: 8px; font-size: 11px; }")
        _add_folder_btn.setToolTip("Add a new custom folder under Unknowns")
        _add_folder_btn.clicked.connect(self._on_spot_add_folder)
        spots_toolbar.addWidget(_add_folder_btn)

        self.spot_list = SpotTree()
        self.spot_list.setHeaderHidden(True)
        self.spot_list.setRootIsDecorated(True)
        self.spot_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.spot_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.spot_list.customContextMenuRequested.connect(self._on_spot_tree_context_menu)
        self.spot_list.currentItemChanged.connect(self._on_spot_tree_item_changed)
        self.spot_list.deleteRequested.connect(self._on_spot_tree_delete)
        self.spot_list.itemChanged.connect(self._on_spot_tree_item_renamed)
        spots_layout.addLayout(spots_toolbar)
        spots_layout.addWidget(self.spot_list, 1)
        spots_card.setMinimumHeight(140)
        self.spot_channel_splitter.addWidget(spots_card)

        channels_card = NeumorphicFrame()
        channels_card.setObjectName("InspectorSubsection")
        channels_card_layout = QtWidgets.QVBoxLayout(channels_card)
        channels_card_layout.setContentsMargins(10, 10, 10, 10)
        channels_card_layout.setSpacing(6)

        channels_heading = QtWidgets.QHBoxLayout()
        channels_heading.setContentsMargins(0, 0, 0, 0)
        channels_label = QtWidgets.QLabel("Channels")
        channels_label.setObjectName("SectionTitle")
        channels_help = ElidedLabel("click: select  ·  double-click: show/hide")
        channels_help.setObjectName("InspectorHint")
        channels_help.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        channels_heading.addWidget(channels_label)
        channels_heading.addWidget(channels_help, 1)
        channels_card_layout.addLayout(channels_heading)

        self.channel_list = QtWidgets.QListWidget()
        self.channel_list.itemChanged.connect(self.on_channel_toggled)
        self.channel_list.currentItemChanged.connect(self.on_channel_selection_changed)
        self.channel_list.itemDoubleClicked.connect(self.choose_channel_color)

        channels_scroll = QtWidgets.QScrollArea()
        channels_scroll.setObjectName("ChannelsCard")
        channels_scroll.setWidgetResizable(True)
        channels_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        channels_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        channels_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.isotope_grid = IsotopeGridWidget()
        self.isotope_grid.channelToggled.connect(self._on_isotope_chip_toggled)
        self.isotope_grid.channelSelected.connect(self._on_isotope_channel_selected)
        self.isotope_grid.colorChangeRequested.connect(self._on_isotope_color_change)
        channels_scroll.setWidget(self.isotope_grid)
        channels_card_layout.addWidget(channels_scroll, 1)
        channels_card.setMinimumHeight(180)
        self.spot_channel_splitter.addWidget(channels_card)
        self.spot_channel_splitter.setStretchFactor(0, 2)
        self.spot_channel_splitter.setStretchFactor(1, 3)
        self.spot_channel_splitter.setSizes([300, 500])
        browse_layout.addWidget(self.spot_channel_splitter, 1)

        # The legacy list mirrors grid state for compatibility but does not
        # participate in layout or consume visual space.
        browse_layout.addWidget(self.channel_list, 0)
        self.channel_list.setVisible(False)  # hidden; isotope_grid is the primary UI

        color_controls = QtWidgets.QGridLayout()
        color_controls.setContentsMargins(0, 0, 0, 0)
        color_controls.setSpacing(6)
        self.channel_color_button = QtWidgets.QPushButton("Channel color")
        self.channel_color_button.setToolTip("Change the selected channel trace color")
        self.channel_color_button.clicked.connect(self.choose_channel_color)
        self.baseline_color_button = QtWidgets.QPushButton("Baseline")
        self.baseline_color_button.setToolTip("Change the baseline selection color")
        self.baseline_color_button.clicked.connect(lambda: self.choose_region_color("baseline"))
        self.plateau_color_button = QtWidgets.QPushButton("Plateau")
        self.plateau_color_button.setToolTip("Change the plateau selection color")
        self.plateau_color_button.clicked.connect(lambda: self.choose_region_color("plateau"))
        for button in (
            self.channel_color_button,
            self.baseline_color_button,
            self.plateau_color_button,
        ):
            button.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
        color_controls.addWidget(self.channel_color_button, 0, 0, 1, 2)
        color_controls.addWidget(self.baseline_color_button, 1, 0)
        color_controls.addWidget(self.plateau_color_button, 1, 1)
        browse_layout.addLayout(color_controls)
        self.inspector_tabs.addTab(browse_page, "Browse")

        calibration_scroll = QtWidgets.QScrollArea()
        calibration_scroll.setWidgetResizable(True)
        calibration_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        calibration_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        calibration_frame = QtWidgets.QWidget()
        calibration_layout = QtWidgets.QVBoxLayout(calibration_frame)
        calibration_layout.setContentsMargins(6, 8, 6, 8)
        calibration_layout.setSpacing(10)

        standards_card = NeumorphicFrame()
        standards_card.setObjectName("SessionCard")
        self.standards_card = standards_card
        standards_layout = QtWidgets.QFormLayout(standards_card)
        standards_layout.setContentsMargins(16, 16, 16, 16)
        standards_layout.setSpacing(9)
        standards_layout.setRowWrapPolicy(
            QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows
        )
        standards_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        standards_title = QtWidgets.QLabel("Calibration materials")
        standards_title.setObjectName("SectionTitle")
        standards_layout.addRow(standards_title)

        self.primary_68_combo = StyledComboBox()
        self.primary_76_combo = StyledComboBox()
        self.primary_82_combo = StyledComboBox()
        self.primary_68_combo.currentTextChanged.connect(
            self.on_primary_68_standard_changed
        )
        self.primary_76_combo.currentTextChanged.connect(
            self.on_primary_76_standard_changed
        )
        self.primary_82_combo.currentTextChanged.connect(
            self.on_primary_82_standard_changed
        )
        standards_layout.addRow("Pb/U primary", self.primary_68_combo)
        standards_layout.addRow("Pb/Pb primary", self.primary_76_combo)
        standards_layout.addRow("Th/Pb primary", self.primary_82_combo)

        self.concentration_standards_edit = QtWidgets.QLineEdit()
        self.concentration_standards_edit.setPlaceholderText(
            "NIST610, NIST612"
        )
        self.concentration_standards_edit.editingFinished.connect(
            self.on_concentration_standards_changed
        )
        standards_layout.addRow(
            "Concentration",
            self.concentration_standards_edit,
        )
        self.secondary_standards_edit = QtWidgets.QLineEdit()
        self.secondary_standards_edit.setPlaceholderText(
            "Comma-separated secondary materials"
        )
        self.secondary_standards_edit.editingFinished.connect(
            self.on_secondary_standards_changed
        )
        standards_layout.addRow("Secondary", self.secondary_standards_edit)
        calibration_layout.addWidget(standards_card, 0)

        fit_card = NeumorphicFrame()
        fit_card.setObjectName("SessionCard")
        self.fit_card = fit_card
        fit_layout = QtWidgets.QVBoxLayout(fit_card)
        fit_layout.setContentsMargins(16, 16, 16, 16)
        fit_layout.setSpacing(10)

        fit_label = QtWidgets.QLabel("Fractionation fit settings")
        fit_label.setObjectName("SectionTitle")
        fit_layout.addWidget(fit_label)

        ratio_aggregation_label = QtWidgets.QLabel("Ratio estimator")
        ratio_aggregation_label.setObjectName("WorkflowLabel")
        self.ratio_aggregation_combo = StyledComboBox()
        for label, method in RATIO_AGGREGATION_OPTIONS:
            self.ratio_aggregation_combo.addItem(label, method)
        self.ratio_aggregation_combo.setToolTip(
            "Choose how positive sweep ratios and multiplicative correction "
            "factors are summarized. Log-ratio mode uses geometric means and "
            "log-space calibration following McLean et al. (2016). Reported "
            "spot 206Pb/204Pb is always calculated as a ratio of means."
        )
        self.ratio_aggregation_combo.currentIndexChanged.connect(
            self.on_ratio_aggregation_changed
        )
        fit_layout.addWidget(ratio_aggregation_label)
        fit_layout.addWidget(self.ratio_aggregation_combo)

        downhole_label = QtWidgets.QLabel("Downhole fit")
        downhole_label.setObjectName("WorkflowLabel")
        self.downhole_fit_combo = StyledComboBox()
        self._populate_fit_method_combo(self.downhole_fit_combo, DOWNHOLE_FIT_METHODS)
        self.downhole_fit_combo.currentIndexChanged.connect(self.on_downhole_fit_method_changed)
        fit_layout.addWidget(downhole_label)
        fit_layout.addWidget(self.downhole_fit_combo)

        self.downhole_fit_stack = QtWidgets.QStackedWidget()
        self._downhole_fit_pages = self._build_fit_parameter_pages(
            self.downhole_fit_stack,
            prefix="downhole",
            degree_default=int(self.session.downhole_fit_options.get("polynomial_degree", 2)),
            window_default=int(self.session.downhole_fit_options.get("running_mean_window", 5)),
            decay_default=float(self.session.downhole_fit_options.get("exponential_initial_decay", 0.1)),
        )
        fit_layout.addWidget(self.downhole_fit_stack)

        outlier_row = QtWidgets.QHBoxLayout()
        self.downhole_outlier_check = QtWidgets.QCheckBox(
            "Filter primary-sweep anomalies"
        )
        self.downhole_outlier_check.setChecked(
            self.session.downhole_outlier_sigma is not None
        )
        self.downhole_outlier_check.toggled.connect(
            self.on_downhole_outlier_filter_toggled
        )
        self.downhole_outlier_sigma_spin = QtWidgets.QDoubleSpinBox()
        self.downhole_outlier_sigma_spin.setRange(1.0, 20.0)
        self.downhole_outlier_sigma_spin.setDecimals(1)
        self.downhole_outlier_sigma_spin.setSingleStep(0.5)
        self.downhole_outlier_sigma_spin.setSuffix(" σ")
        self.downhole_outlier_sigma_spin.setValue(
            self.session.downhole_outlier_sigma or 5.0
        )
        self.downhole_outlier_sigma_spin.valueChanged.connect(
            self.on_downhole_outlier_sigma_changed
        )
        outlier_row.addWidget(self.downhole_outlier_check)
        outlier_row.addStretch(1)
        outlier_row.addWidget(self.downhole_outlier_sigma_spin)
        fit_layout.addLayout(outlier_row)

        session_label = QtWidgets.QLabel("Session fit")
        session_label.setObjectName("WorkflowLabel")
        self.session_fit_combo = StyledComboBox()
        self._populate_fit_method_combo(self.session_fit_combo, SESSION_FIT_METHODS)
        self.session_fit_combo.currentIndexChanged.connect(self.on_session_fit_method_changed)
        fit_layout.addWidget(session_label)
        fit_layout.addWidget(self.session_fit_combo)

        self.session_fit_stack = QtWidgets.QStackedWidget()
        self._session_fit_pages = self._build_fit_parameter_pages(
            self.session_fit_stack,
            prefix="session",
            degree_default=int(self.session.session_fit_options.get("polynomial_degree", 2)),
            window_default=int(self.session.session_fit_options.get("running_mean_window", 5)),
            decay_default=float(self.session.session_fit_options.get("exponential_initial_decay", 0.001)),
        )
        fit_layout.addWidget(self.session_fit_stack)

        self.fit_settings_note_label = QtWidgets.QLabel()
        self.fit_settings_note_label.setObjectName("PanelBody")
        self.fit_settings_note_label.setWordWrap(True)
        fit_layout.addWidget(self.fit_settings_note_label)
        calibration_layout.addWidget(fit_card, 0)
        calibration_layout.addStretch(1)
        calibration_scroll.setWidget(calibration_frame)
        # Separate scroll viewports keep the detector plot within the current
        # inspector width even when fractionation forms need a wider page.
        self.detector_calibration_panel = DetectorCalibrationPanel()
        self.detector_calibration_panel.apply_requested.connect(self.apply_detector_calibration)
        detector_scroll = QtWidgets.QScrollArea()
        detector_scroll.setWidgetResizable(True)
        detector_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        detector_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detector_scroll.setWidget(self.detector_calibration_panel)
        self.calibration_tabs = QtWidgets.QTabWidget()
        self.calibration_tabs.setDocumentMode(True)
        self.calibration_tabs.tabBar().setDrawBase(False)
        self.calibration_tabs.addTab(calibration_scroll, "Fractionation")
        self.calibration_tabs.addTab(detector_scroll, "Detector")
        self.inspector_tabs.addTab(self.calibration_tabs, "Calibration")

        results_page = QtWidgets.QWidget()
        results_layout = QtWidgets.QVBoxLayout(results_page)
        results_layout.setContentsMargins(6, 8, 6, 8)
        results_layout.setSpacing(10)

        decision_card = NeumorphicFrame()
        decision_card.setObjectName("SessionCard")
        self.decision_card = decision_card
        decision_layout = QtWidgets.QFormLayout(decision_card)
        decision_layout.setContentsMargins(16, 16, 16, 16)
        decision_layout.setSpacing(10)
        decision_layout.setRowWrapPolicy(
            QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows
        )
        decision_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        decision_title = QtWidgets.QLabel("Final result decisions")
        decision_title.setObjectName("SectionTitle")
        decision_layout.addRow(decision_title)

        self.common_pb_method_combo = StyledComboBox()
        for label, method in (
            ("No common-Pb correction", "none"),
            ("204Pb method", "204"),
            ("207 method", "207"),
            ("208 method", "208"),
        ):
            self.common_pb_method_combo.addItem(label, method)
        self.common_pb_method_combo.currentIndexChanged.connect(
            self.on_common_pb_method_changed
        )
        decision_layout.addRow("Preferred branch", self.common_pb_method_combo)

        self.common_pb_uncertainty_combo = StyledComboBox()
        for label, preset_name in (
            ("No model uncertainty", "none"),
            ("Boise working convention", "boise_working"),
            ("Horstwood (2016) example", "horstwood2016"),
            ("ET_Redux default", "et_redux"),
        ):
            self.common_pb_uncertainty_combo.addItem(label, preset_name)
        self.common_pb_uncertainty_combo.currentIndexChanged.connect(
            self.on_common_pb_uncertainty_changed
        )
        self.common_pb_uncertainty_combo.setToolTip(
            "Stacey–Kramers defines a model composition, not a universal model "
            "uncertainty. Choose an explicit published/software convention, or "
            "leave model uncertainty off. The workbook records the choice."
        )
        decision_layout.addRow(
            "Initial-Pb uncertainty",
            self.common_pb_uncertainty_combo,
        )

        self.calibration_uncertainty_check = QtWidgets.QCheckBox(
            "Include calibration uncertainty"
        )
        self.calibration_uncertainty_check.setChecked(
            self.session.include_calibration_uncertainty
        )
        self.calibration_uncertainty_check.toggled.connect(
            self.on_calibration_uncertainty_toggled
        )
        decision_layout.addRow(self.calibration_uncertainty_check)

        self.decay_uncertainty_check = QtWidgets.QCheckBox(
            "Propagate decay-constant uncertainty"
        )
        self.decay_uncertainty_check.toggled.connect(
            self.on_systematic_uncertainty_changed
        )
        decision_layout.addRow(self.decay_uncertainty_check)

        self.u_ratio_uncertainty_check = QtWidgets.QCheckBox(
            "Propagate 238U/235U uncertainty"
        )
        self.u_ratio_uncertainty_check.toggled.connect(
            self.on_systematic_uncertainty_changed
        )
        decision_layout.addRow(self.u_ratio_uncertainty_check)

        self.long_term_mode_combo = StyledComboBox()
        for label, mode in (
            ("Do not add", "none"),
            ("From secondary-standard mean offset", "secondary"),
            ("User-supplied relative 1σ", "user"),
        ):
            self.long_term_mode_combo.addItem(label, mode)
        self.long_term_mode_combo.currentIndexChanged.connect(
            self.on_systematic_uncertainty_changed
        )
        self.long_term_mode_combo.setToolTip(
            "The secondary-standard option uses the largest weighted-mean offset "
            "from its accepted date as a conservative session proxy. A true "
            "long-term reproducibility estimate should come from many sessions."
        )
        decision_layout.addRow("Long-term reproducibility", self.long_term_mode_combo)

        self.long_term_uncertainty_spin = QtWidgets.QDoubleSpinBox()
        self.long_term_uncertainty_spin.setRange(0.0, 100.0)
        self.long_term_uncertainty_spin.setDecimals(3)
        self.long_term_uncertainty_spin.setSingleStep(0.1)
        self.long_term_uncertainty_spin.setSuffix(" % 1σ")
        self.long_term_uncertainty_spin.valueChanged.connect(
            self.on_systematic_uncertainty_changed
        )
        decision_layout.addRow("User reproducibility", self.long_term_uncertainty_spin)

        self.export_concentrations_check = QtWidgets.QCheckBox(
            "Spot concentrations in export"
        )
        self.export_concentrations_check.setToolTip(
            "Include spot concentrations in the publication export"
        )
        self.export_concentrations_check.setChecked(True)
        self.export_concentrations_check.toggled.connect(
            lambda checked: setattr(
                self.session, "include_export_concentrations", bool(checked)
            )
        )
        decision_layout.addRow(self.export_concentrations_check)

        self.export_depth_concentrations_check = QtWidgets.QCheckBox(
            "Sweep concentrations in depth export"
        )
        self.export_depth_concentrations_check.setToolTip(
            "Include sweep concentrations in the depth-profile export"
        )
        self.export_depth_concentrations_check.setChecked(True)
        self.export_depth_concentrations_check.toggled.connect(
            lambda checked: setattr(
                self.session, "include_export_depth_concentrations", bool(checked)
            )
        )
        decision_layout.addRow(self.export_depth_concentrations_check)

        self.age_transition_spin = QtWidgets.QDoubleSpinBox()
        self.age_transition_spin.setRange(0.0, 4600.0)
        self.age_transition_spin.setDecimals(1)
        self.age_transition_spin.setSingleStep(50.0)
        self.age_transition_spin.setSuffix(" Ma")
        self.age_transition_spin.setValue(self.session.age_transition_ma)
        self.age_transition_spin.valueChanged.connect(
            self.on_age_transition_changed
        )
        decision_layout.addRow("Age transition", self.age_transition_spin)
        results_layout.addWidget(decision_card, 0)
        results_layout.addStretch(1)
        results_scroll = QtWidgets.QScrollArea()
        results_scroll.setWidgetResizable(True)
        results_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        results_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        results_scroll.setWidget(results_page)
        self.inspector_tabs.addTab(results_scroll, "Results")
        elide_tab_titles(self.inspector_tabs)
        return panel

    def _populate_fit_method_combo(self, combo: StyledComboBox, methods: tuple[str, ...]) -> None:
        for method in methods:
            label = fit_method_label(method)
            if method == "lowess":
                label += " (planned)"
            combo.addItem(label, method)
            if method == "lowess":
                model = combo.model()
                item = model.item(combo.count() - 1) if hasattr(model, "item") else None
                if item is not None:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled)

    def _build_fit_parameter_pages(
        self,
        stack: QtWidgets.QStackedWidget,
        *,
        prefix: str,
        degree_default: int,
        window_default: int,
        decay_default: float,
    ) -> dict[str, int]:
        pages: dict[str, int] = {}

        summary_page = QtWidgets.QWidget()
        summary_layout = QtWidgets.QVBoxLayout(summary_page)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setSpacing(4)
        summary_label = QtWidgets.QLabel("No extra arguments for this fit method.")
        summary_label.setObjectName("PanelBody")
        summary_label.setWordWrap(True)
        summary_layout.addWidget(summary_label)
        pages["none"] = stack.addWidget(summary_page)

        poly_page = QtWidgets.QWidget()
        poly_layout = QtWidgets.QHBoxLayout(poly_page)
        poly_layout.setContentsMargins(0, 0, 0, 0)
        poly_layout.setSpacing(8)
        poly_label = QtWidgets.QLabel("Polynomial degree")
        poly_label.setObjectName("SecondaryText")
        poly_spin = QtWidgets.QSpinBox()
        poly_spin.setRange(1, 6)
        poly_spin.setValue(degree_default)
        poly_spin.valueChanged.connect(
            self.on_downhole_polynomial_degree_changed if prefix == "downhole" else self.on_session_polynomial_degree_changed
        )
        setattr(self, f"{prefix}_polynomial_degree_spin", poly_spin)
        poly_layout.addWidget(poly_label)
        poly_layout.addStretch(1)
        poly_layout.addWidget(poly_spin)
        pages["polynomial"] = stack.addWidget(poly_page)

        running_page = QtWidgets.QWidget()
        running_layout = QtWidgets.QHBoxLayout(running_page)
        running_layout.setContentsMargins(0, 0, 0, 0)
        running_layout.setSpacing(8)
        running_label = QtWidgets.QLabel("Running-mean window")
        running_label.setObjectName("SecondaryText")
        running_spin = QtWidgets.QSpinBox()
        running_spin.setRange(3, 99)
        running_spin.setSingleStep(2)
        running_spin.setValue(window_default)
        running_spin.valueChanged.connect(
            self.on_downhole_running_mean_window_changed if prefix == "downhole" else self.on_session_running_mean_window_changed
        )
        setattr(self, f"{prefix}_running_mean_window_spin", running_spin)
        running_layout.addWidget(running_label)
        running_layout.addStretch(1)
        running_layout.addWidget(running_spin)
        pages["running_mean"] = stack.addWidget(running_page)

        exp_page = QtWidgets.QWidget()
        exp_layout = QtWidgets.QVBoxLayout(exp_page)
        exp_layout.setContentsMargins(0, 0, 0, 0)
        exp_layout.setSpacing(6)
        exp_formula = QtWidgets.QLabel("Exponential form: y = a + b.exp(-c x)")
        exp_formula.setObjectName("PanelBody")
        exp_formula.setWordWrap(True)
        exp_row = QtWidgets.QHBoxLayout()
        exp_row.setContentsMargins(0, 0, 0, 0)
        exp_row.setSpacing(8)
        exp_label = QtWidgets.QLabel("Initial c guess")
        exp_label.setObjectName("SecondaryText")
        exp_spin = QtWidgets.QDoubleSpinBox()
        exp_spin.setDecimals(4)
        exp_spin.setRange(0.0001, 10.0)
        exp_spin.setSingleStep(0.01)
        exp_spin.setValue(decay_default)
        exp_spin.valueChanged.connect(
            self.on_downhole_exponential_decay_changed if prefix == "downhole" else self.on_session_exponential_decay_changed
        )
        setattr(self, f"{prefix}_exponential_decay_spin", exp_spin)
        exp_row.addWidget(exp_label)
        exp_row.addStretch(1)
        exp_row.addWidget(exp_spin)
        exp_layout.addWidget(exp_formula)
        exp_layout.addLayout(exp_row)
        pages["exponential"] = stack.addWidget(exp_page)

        lowess_page = QtWidgets.QWidget()
        lowess_layout = QtWidgets.QVBoxLayout(lowess_page)
        lowess_layout.setContentsMargins(0, 0, 0, 0)
        lowess_layout.setSpacing(4)
        lowess_label = QtWidgets.QLabel("LOWESS will need either a local implementation or a statsmodels dependency before it can be enabled.")
        lowess_label.setObjectName("PanelBody")
        lowess_label.setWordWrap(True)
        lowess_layout.addWidget(lowess_label)
        pages["lowess"] = stack.addWidget(lowess_page)

        for method in ("mean", "interpolated", "linear", "cubic_spline"):
            pages[method] = pages["none"]

        return pages

    def _set_fit_stack_page(self, stack: QtWidgets.QStackedWidget, pages: dict[str, int], method: str) -> None:
        stack.setCurrentIndex(pages.get(method, pages["none"]))

    def _invalidate_downhole_fit_cache(self) -> None:
        self.session.reduction_cache.pop("downhole_results", None)
        self.session.reduction_cache.pop("session_correction_results", None)
        self.session.reduction_cache.pop("spot_mean_results", None)
        self.session.reduction_cache.pop("depth_profile_results", None)
        self.session.reduction_cache.pop("finalized_results", None)
        if self.session.pipeline is not None:
            self.session.pipeline.cache.invalidate(PipelineStage.DOWNHOLE)

    def _invalidate_session_fit_cache(self) -> None:
        self.session.reduction_cache.pop("session_correction_results", None)
        self.session.reduction_cache.pop("spot_mean_results", None)
        self.session.reduction_cache.pop("depth_profile_results", None)
        self.session.reduction_cache.pop("finalized_results", None)
        if self.session.pipeline is not None:
            self.session.pipeline.cache.invalidate(PipelineStage.SESSION)

    def _sync_fit_controls(self) -> None:
        self._is_populating = True
        try:
            self.populate_standard_controls()
            aggregation_index = self.ratio_aggregation_combo.findData(
                self.session.ratio_aggregation
            )
            if aggregation_index >= 0:
                self.ratio_aggregation_combo.setCurrentIndex(
                    aggregation_index
                )
            for combo, method, methods in [
                (self.downhole_fit_combo, self.session.downhole_fit_method, DOWNHOLE_FIT_METHODS),
                (self.session_fit_combo, self.session.session_fit_method, SESSION_FIT_METHODS),
            ]:
                for index in range(combo.count()):
                    if combo.itemData(index) == method:
                        combo.setCurrentIndex(index)
                        break

            self.downhole_polynomial_degree_spin.setValue(int(self.session.downhole_fit_options.get("polynomial_degree", 2)))
            self.downhole_running_mean_window_spin.setValue(int(self.session.downhole_fit_options.get("running_mean_window", 5)))
            self.downhole_exponential_decay_spin.setValue(float(self.session.downhole_fit_options.get("exponential_initial_decay", 0.1)))
            self.downhole_outlier_check.setChecked(
                self.session.downhole_outlier_sigma is not None
            )
            self.downhole_outlier_sigma_spin.setEnabled(
                self.session.downhole_outlier_sigma is not None
            )
            if self.session.downhole_outlier_sigma is not None:
                self.downhole_outlier_sigma_spin.setValue(
                    float(self.session.downhole_outlier_sigma)
                )

            self.session_polynomial_degree_spin.setValue(int(self.session.session_fit_options.get("polynomial_degree", self.workflow_preset.session_polynomial_degree)))
            self.session_running_mean_window_spin.setValue(int(self.session.session_fit_options.get("running_mean_window", 5)))
            self.session_exponential_decay_spin.setValue(float(self.session.session_fit_options.get("exponential_initial_decay", 0.001)))
            decision_index = self.common_pb_method_combo.findData(
                self.session.common_pb_method
            )
            if decision_index >= 0:
                self.common_pb_method_combo.setCurrentIndex(decision_index)
            common_uncertainty_index = self.common_pb_uncertainty_combo.findData(
                self.session.common_pb_uncertainty_preset_name
            )
            if common_uncertainty_index >= 0:
                self.common_pb_uncertainty_combo.setCurrentIndex(
                    common_uncertainty_index
                )
            self.calibration_uncertainty_check.setChecked(
                self.session.include_calibration_uncertainty
            )
            self.decay_uncertainty_check.setChecked(
                self.session.propagate_decay_constants
            )
            self.u_ratio_uncertainty_check.setChecked(
                self.session.propagate_u238_u235
            )
            long_term_index = self.long_term_mode_combo.findData(
                self.session.long_term_reproducibility_mode
            )
            if long_term_index >= 0:
                self.long_term_mode_combo.setCurrentIndex(long_term_index)
            self.long_term_uncertainty_spin.setValue(
                100.0 * self.session.long_term_reproducibility_relative_1s
            )
            self.long_term_uncertainty_spin.setEnabled(
                self.session.long_term_reproducibility_mode == "user"
            )
            self.export_concentrations_check.setChecked(
                self.session.include_export_concentrations
            )
            self.export_depth_concentrations_check.setChecked(
                self.session.include_export_depth_concentrations
            )
            self.age_transition_spin.setValue(
                float(self.session.age_transition_ma)
            )
        finally:
            self._is_populating = False

        self._set_fit_stack_page(self.downhole_fit_stack, self._downhole_fit_pages, self.session.downhole_fit_method)
        self._set_fit_stack_page(self.session_fit_stack, self._session_fit_pages, self.session.session_fit_method)
        self._update_fit_settings_note()

    def populate_standard_controls(self) -> None:
        """Populate standard selectors from configured and observed samples."""
        configured = [
            self.session.primary_68_standard
            or self.workflow_preset.primary_standard,
            self.session.primary_76_standard
            or self.workflow_preset.pb_ratio_standard,
            self.session.primary_82_standard
            or self.workflow_preset.th_ratio_standard,
        ]
        observed = (
            self.session.total_data["sample"]
            .dropna()
            .astype(str)
            .drop_duplicates()
            .tolist()
            if self.session.loaded and "sample" in self.session.total_data
            else []
        )
        choices = [
            value
            for value in dict.fromkeys([*configured, *observed])
            if value
        ]
        for combo, selected in zip(
            (
                self.primary_68_combo,
                self.primary_76_combo,
                self.primary_82_combo,
            ),
            configured,
        ):
            combo.clear()
            combo.addItems(choices)
            index = combo.findText(selected)
            if index >= 0:
                combo.setCurrentIndex(index)
        concentration = (
            self.session.concentration_standards
            or self.workflow_preset.concentration_standards
        )
        secondary = (
            self.session.secondary_standards
            or self.workflow_preset.secondary_standards
        )
        self.concentration_standards_edit.setText(", ".join(concentration))
        self.secondary_standards_edit.setText(", ".join(secondary))

    def _update_fit_settings_note(self) -> None:
        downhole_label = fit_method_label(self.session.downhole_fit_method)
        session_label = fit_method_label(self.session.session_fit_method)
        parts = [
            (
                "Estimator: arithmetic mean"
                if self.session.ratio_aggregation == "arithmetic"
                else "Estimator: log-ratio / geometric mean"
            ),
            "spot 206Pb/204Pb: ratio of means",
            f"Downhole: {downhole_label}",
            f"Session: {session_label}",
            (
                f"primary-sweep filter {self.session.downhole_outlier_sigma:g} σ"
                if self.session.downhole_outlier_sigma is not None
                else "primary-sweep filter off"
            ),
        ]
        if self.session.downhole_fit_method == "polynomial":
            parts.append(f"downhole degree {int(self.session.downhole_fit_options.get('polynomial_degree', 2))}")
        if self.session.session_fit_method == "polynomial":
            parts.append(f"session degree {int(self.session.session_fit_options.get('polynomial_degree', self.workflow_preset.session_polynomial_degree))}")
        if self.session.downhole_fit_method == "running_mean":
            parts.append(f"downhole window {int(self.session.downhole_fit_options.get('running_mean_window', 5))}")
        if self.session.session_fit_method == "running_mean":
            parts.append(f"session window {int(self.session.session_fit_options.get('running_mean_window', 5))}")
        if "lowess" in {self.session.downhole_fit_method, self.session.session_fit_method}:
            parts.append("LOWESS is displayed as a planned method; it is not active in this build.")
        self.fit_settings_note_label.setText(" | ".join(parts))

    def on_ratio_aggregation_changed(self, index: int) -> None:
        """Set the ratio estimator and invalidate every dependent result."""
        if self._is_populating or index < 0:
            return
        aggregation = str(
            self.ratio_aggregation_combo.itemData(index) or "arithmetic"
        )
        if aggregation == self.session.ratio_aggregation:
            return
        self.session.ratio_aggregation = aggregation
        self._invalidate_downhole_fit_cache()
        self._update_fit_settings_note()
        label = (
            "Arithmetic mean"
            if aggregation == "arithmetic"
            else "Log-ratio / geometric mean"
        )
        self.statusBar().showMessage(
            f"Ratio estimator set to {label}; rerun downhole and later stages."
        )
        if self.session.loaded:
            self.refresh_view()

    def on_downhole_fit_method_changed(self, index: int) -> None:
        if self._is_populating or index < 0:
            return
        method = str(self.downhole_fit_combo.itemData(index) or "mean")
        self.session.downhole_fit_method = method
        self._set_fit_stack_page(self.downhole_fit_stack, self._downhole_fit_pages, method)
        self._invalidate_downhole_fit_cache()
        self._update_fit_settings_note()
        self.statusBar().showMessage(f"Downhole fit method set to {fit_method_label(method)}.")
        if self.session.loaded:
            self.refresh_view()

    def on_session_fit_method_changed(self, index: int) -> None:
        if self._is_populating or index < 0:
            return
        method = str(self.session_fit_combo.itemData(index) or "polynomial")
        self.session.session_fit_method = method
        self._set_fit_stack_page(self.session_fit_stack, self._session_fit_pages, method)
        self._invalidate_session_fit_cache()
        self._update_fit_settings_note()
        self.statusBar().showMessage(f"Session fit method set to {fit_method_label(method)}.")
        if self.session.loaded:
            self.refresh_view()

    def invalidate_final_decisions(self) -> None:
        """Discard only products affected by common-Pb or age choices."""
        self.session.reduction_cache.pop("finalized_results", None)
        if self.session.pipeline is not None:
            self.session.pipeline.cache.invalidate(PipelineStage.FINALIZE)

    def invalidate_uncertainty_products(self) -> None:
        """Discard spot and depth outputs affected by uncertainty choices."""
        self.session.reduction_cache.pop("finalized_results", None)
        self.session.reduction_cache.pop("depth_profile_results", None)
        if self.session.pipeline is not None:
            self.session.pipeline.cache.invalidate(PipelineStage.DEPTH_PROFILE)
            self.session.pipeline.cache.invalidate(PipelineStage.FINALIZE)

    def on_primary_68_standard_changed(self, value: str) -> None:
        """Set the Pb/U reference material used by both correction stages."""
        if self._is_populating or not value:
            return
        self.session.primary_68_standard = value
        self.detector_calibration_panel.set_session(self.session)
        self._invalidate_downhole_fit_cache()
        self._refresh_workflow_status()

    def on_primary_76_standard_changed(self, value: str) -> None:
        """Set the Pb/Pb reference material used by both correction stages."""
        if self._is_populating or not value:
            return
        self.session.primary_76_standard = value
        self._invalidate_downhole_fit_cache()
        self._refresh_workflow_status()

    def on_primary_82_standard_changed(self, value: str) -> None:
        """Set the Th/Pb reference material used by both correction stages."""
        if self._is_populating or not value:
            return
        self.session.primary_82_standard = value
        self._invalidate_downhole_fit_cache()
        self._refresh_workflow_status()

    def on_concentration_standards_changed(self) -> None:
        """Parse comma-separated elemental calibration materials."""
        if self._is_populating:
            return
        values = tuple(
            value.strip()
            for value in self.concentration_standards_edit.text().split(",")
            if value.strip()
        )
        if not values:
            self.statusBar().showMessage(
                "Keep at least one concentration standard."
            )
            self._sync_fit_controls()
            return
        if values == self.session.concentration_standards:
            return
        self.session.concentration_standards = values
        for key in (
            "concentration_results",
            "spot_mean_results",
            "finalized_results",
        ):
            self.session.reduction_cache.pop(key, None)
        if self.session.pipeline is not None:
            self.session.pipeline.cache.invalidate(PipelineStage.CONCENTRATION)
        self._refresh_workflow_status()

    def on_secondary_standards_changed(self) -> None:
        """Parse materials used for weighted-mean and MSWD summaries."""
        if self._is_populating:
            return
        values = tuple(
            value.strip()
            for value in self.secondary_standards_edit.text().split(",")
            if value.strip()
        )
        if values == self.session.secondary_standards:
            return
        self.session.secondary_standards = values
        self.invalidate_final_decisions()
        self._refresh_workflow_status()

    def on_common_pb_method_changed(self, index: int) -> None:
        """Choose the common-Pb branch used in the concise final table."""
        if self._is_populating or index < 0:
            return
        self.session.common_pb_method = str(
            self.common_pb_method_combo.itemData(index) or "none"
        )
        self.invalidate_final_decisions()
        self._refresh_workflow_status()

    def on_common_pb_uncertainty_changed(self, index: int) -> None:
        """Choose an explicit initial-Pb uncertainty convention."""
        if self._is_populating or index < 0:
            return
        self.session.common_pb_uncertainty_preset_name = str(
            self.common_pb_uncertainty_combo.itemData(index) or "none"
        )
        self.invalidate_uncertainty_products()
        self._refresh_workflow_status()

    def on_calibration_uncertainty_toggled(self, checked: bool) -> None:
        """Choose the calibrated or analytical-only uncertainty branch."""
        if self._is_populating:
            return
        self.session.include_calibration_uncertainty = bool(checked)
        self.invalidate_final_decisions()
        self._refresh_workflow_status()

    def on_systematic_uncertainty_changed(self, *_args) -> None:
        """Synchronize decay, U-isotope, and reproducibility choices."""
        if self._is_populating:
            return
        self.session.propagate_decay_constants = (
            self.decay_uncertainty_check.isChecked()
        )
        self.session.propagate_u238_u235 = (
            self.u_ratio_uncertainty_check.isChecked()
        )
        self.session.long_term_reproducibility_mode = str(
            self.long_term_mode_combo.currentData() or "none"
        )
        self.session.long_term_reproducibility_relative_1s = (
            self.long_term_uncertainty_spin.value() / 100.0
        )
        self.long_term_uncertainty_spin.setEnabled(
            self.session.long_term_reproducibility_mode == "user"
        )
        self.invalidate_uncertainty_products()
        self._refresh_workflow_status()

    def on_age_transition_changed(self, value: float) -> None:
        """Set the age boundary for Pb/U versus Pb/Pb preferred ages."""
        if self._is_populating:
            return
        self.session.age_transition_ma = float(value)
        self.invalidate_final_decisions()
        self._refresh_workflow_status()

    def on_downhole_polynomial_degree_changed(self, value: int) -> None:
        if self._is_populating:
            return
        self.session.downhole_fit_options["polynomial_degree"] = int(value)
        self._invalidate_downhole_fit_cache()
        self._update_fit_settings_note()
        if self.session.loaded and self.session.downhole_fit_method == "polynomial":
            self.refresh_view()

    def on_session_polynomial_degree_changed(self, value: int) -> None:
        if self._is_populating:
            return
        self.session.session_fit_options["polynomial_degree"] = int(value)
        self._invalidate_session_fit_cache()
        self._update_fit_settings_note()
        if self.session.loaded and self.session.session_fit_method == "polynomial":
            self.refresh_view()

    def on_downhole_running_mean_window_changed(self, value: int) -> None:
        if self._is_populating:
            return
        self.session.downhole_fit_options["running_mean_window"] = int(value)
        self._invalidate_downhole_fit_cache()
        self._update_fit_settings_note()
        if self.session.loaded and self.session.downhole_fit_method == "running_mean":
            self.refresh_view()

    def on_downhole_outlier_filter_toggled(self, checked: bool) -> None:
        """Enable or disable robust cross-standard sweep filtering."""
        if self._is_populating:
            return
        self.downhole_outlier_sigma_spin.setEnabled(checked)
        self.session.downhole_outlier_sigma = (
            float(self.downhole_outlier_sigma_spin.value())
            if checked
            else None
        )
        self._invalidate_downhole_fit_cache()
        self._update_fit_settings_note()

    def on_downhole_outlier_sigma_changed(self, value: float) -> None:
        """Update the active robust downhole anomaly cutoff."""
        if self._is_populating or not self.downhole_outlier_check.isChecked():
            return
        self.session.downhole_outlier_sigma = float(value)
        self._invalidate_downhole_fit_cache()
        self._update_fit_settings_note()

    def on_session_running_mean_window_changed(self, value: int) -> None:
        if self._is_populating:
            return
        self.session.session_fit_options["running_mean_window"] = int(value)
        self._invalidate_session_fit_cache()
        self._update_fit_settings_note()
        if self.session.loaded and self.session.session_fit_method == "running_mean":
            self.refresh_view()

    def on_downhole_exponential_decay_changed(self, value: float) -> None:
        if self._is_populating:
            return
        self.session.downhole_fit_options["exponential_initial_decay"] = float(value)
        self._invalidate_downhole_fit_cache()
        self._update_fit_settings_note()
        if self.session.loaded and self.session.downhole_fit_method == "exponential":
            self.refresh_view()

    def on_session_exponential_decay_changed(self, value: float) -> None:
        if self._is_populating:
            return
        self.session.session_fit_options["exponential_initial_decay"] = float(value)
        self._invalidate_session_fit_cache()
        self._update_fit_settings_note()
        if self.session.loaded and self.session.session_fit_method == "exponential":
            self.refresh_view()

    def _connect_system_theme_updates(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        try:
            app.styleHints().colorSchemeChanged.connect(self.on_system_color_scheme_changed)
        except AttributeError:
            return

    def _show_placeholder_state(self) -> None:
        self.save_project_button.setEnabled(False)
        self.export_results_button.setEnabled(False)
        self._apply_project_summary(project_summary(self.session))
        self._update_reference_library_labels()
        self.trace_plot.apply_theme(active_theme(QtWidgets.QApplication.instance()))
        self.trace_plot.show_preview(self.selection_colors)
        self.session_overview_plot.apply_theme(active_theme(QtWidgets.QApplication.instance()))
        self.session_overview_plot.show_preview()
        self.notes_body.setText(self._compose_notes(None))
        self.log_view.setPlainText("Ready. Load an experiment directory to begin.\n")
        if self._reference_library_notice:
            self.append_log(self._reference_library_notice)
            self._reference_library_notice = None
        self._sync_plot_mode_buttons()
        self._refresh_workflow_status()

    def _initialize_reference_library(self) -> None:
        saved_path = str(self.settings.value("reference_library_path", "", type=str) or "")
        target_path = saved_path if saved_path else self.default_reference_library_path
        try:
            load_standard_archive_from_path(target_path)
        except Exception as exc:
            load_standard_archive_from_path(None)
            self.settings.remove("reference_library_path")
            self._reference_library_notice = f"Reference material library fallback to default after load failure: {exc}"
        self.reference_library_path = current_standard_archive_path()
        self._update_reference_library_labels()

    def _update_reference_library_labels(self) -> None:
        current_path = self.reference_library_path or current_standard_archive_path()
        self.reference_library_label.setText(f"Current library: {current_path}")
        self.reference_library_default_label.setText(f"Default library: {self.default_reference_library_path}")
        self.reference_library_button.setToolTip(
            f"Current: {current_path}\n"
            f"Default: {self.default_reference_library_path}"
        )

    def choose_reference_material_library(self) -> None:
        start_directory = str(Path(self.reference_library_path).expanduser().parent) if self.reference_library_path else str(Path.home())
        file_path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select reference material library",
            start_directory,
            "UPbRPy libraries (library.toml *.csv);;"
            "TOML manifest (library.toml);;Legacy CSV (*.csv)",
        )
        if not file_path:
            return
        try:
            load_standard_archive_from_path(file_path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Failed to load library", str(exc))
            self.append_log(f"Reference material library load failed: {file_path}: {exc}")
            return
        self.reference_library_path = current_standard_archive_path()
        self.settings.setValue("reference_library_path", self.reference_library_path)
        self._update_reference_library_labels()
        clear_reduction_cache(self.session)
        self.append_log(f"Loaded reference material library: {self.reference_library_path}")
        self.statusBar().showMessage(f"Reference material library loaded from {Path(self.reference_library_path).name}")
        self.refresh_view()

    def choose_experiment_directory(self) -> None:
        directory = self.select_experiment_directory()
        if directory:
            self.load_experiment_directory(directory)

    def choose_experiment_directory_in_new_window(self) -> None:
        """Select raw data and request loading it in an independent window."""
        directory = self.select_experiment_directory()
        if directory:
            self.open_experiment_in_new_window_requested.emit(directory)

    def select_experiment_directory(self) -> str:
        """Return a user-selected experiment directory or an empty string."""
        return QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select experiment directory",
            self.source_dialog_directory(),
        )

    def source_dialog_directory(self) -> str:
        """Start file and directory dialogs beside a file source."""
        source = Path(self.session.directory) if self.session.directory else Path.home()
        return str(source.parent if source.is_file() else source)

    def select_qtegra_file(self) -> str:
        """Return a user-selected Qtegra CSV or an empty string."""
        return QtWidgets.QFileDialog.getOpenFileName(
            self, "Open Qtegra file", self.source_dialog_directory(),
            "Qtegra exports (*.csv);;All files (*)",
        )[0]

    def choose_qtegra_file(self) -> None:
        path = self.select_qtegra_file()
        if path:
            self.load_experiment_directory(path)

    def choose_qtegra_file_in_new_window(self) -> None:
        path = self.select_qtegra_file()
        if path:
            self.open_experiment_in_new_window_requested.emit(path)

    def project_workspace_state(self) -> dict[str, object]:
        """Return compact display context worth restoring with a reduction."""
        return {
            "selected_spot": self.current_spot_name(),
            "visible_channels": self.checked_channels(),
            "selected_stats_channel": self.selected_stats_channel,
            "plot_mode": self.session.plot_mode,
            "ratio_name": self.current_ratio_name(),
            "workflow_stage": (
                None
                if self.current_workflow_stage() is None
                else self.current_workflow_stage().value
            ),
            "sigma_level": self.sigma_level,
            "channel_colors": dict(self.channel_colors),
            "selection_colors": dict(self.selection_colors),
            "spot_folder_overrides": dict(self._spot_folder_overrides),
            "include_export_concentrations": (
                self.session.include_export_concentrations
            ),
            "include_export_depth_concentrations": (
                self.session.include_export_depth_concentrations
            ),
        }

    def apply_pending_project_edits(self) -> None:
        """Commit visible controls and dragged windows before saving."""
        concentration_values = tuple(
            value.strip()
            for value in self.concentration_standards_edit.text().split(",")
            if value.strip()
        )
        if (
            concentration_values
            and concentration_values
            != self.session.concentration_standards
        ):
            self.on_concentration_standards_changed()
        secondary_values = tuple(
            value.strip()
            for value in self.secondary_standards_edit.text().split(",")
            if value.strip()
        )
        if secondary_values != self.session.secondary_standards:
            self.on_secondary_standards_changed()
        if not self.pending_region_updates:
            return
        spot_name = self.current_spot_name()
        regions = self.trace_plot.current_regions()
        if not spot_name or not regions:
            raise ValueError(
                "The edited selection windows cannot be read from the "
                "current plot. Apply or discard them before saving."
            )
        update_selection_region(
            self.session,
            spot_name,
            baseline=regions.get("baseline"),
            plateau=regions.get("plateau"),
            signal=regions.get("signal"),
        )
        self.pending_region_updates.clear()
        self.append_log(
            f"Applied pending selection edits for {spot_name} before save."
        )

    def choose_project_file(self) -> None:
        """Choose a lightweight project and recalculate its saved stages."""
        if self._active_worker is not None:
            self.statusBar().showMessage(
                f"Please wait for {self._busy_label.lower()} to finish."
            )
            return
        project_file = self.select_project_file()
        if not project_file:
            return
        self.load_project_file(project_file)

    def choose_project_file_in_new_window(self) -> None:
        """Select a project and request recalculation in another window."""
        project_file = self.select_project_file()
        if project_file:
            self.open_project_in_new_window_requested.emit(project_file)

    def select_project_file(self) -> str:
        """Return a user-selected UPbRPy project path or an empty string."""
        start_directory = (
            str(self.project_path.parent)
            if self.project_path is not None
            else self.source_dialog_directory()
        )
        project_file, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open UPbRPy project",
            start_directory,
            "UPbRPy Project (*.upbr)",
        )
        return project_file

    def load_project_file(
        self,
        project_file: str | Path,
        *,
        background: bool = True,
    ) -> None:
        """Recalculate a saved project in this window."""
        project_file = str(project_file)
        if background:
            self.run_background(
                "Opening project and recalculating saved stages",
                lambda: restore_session_from_project(project_file),
                self.install_restored_project,
            )
            return
        try:
            restored = restore_session_from_project(project_file)
        except Exception as exc:
            self._handle_action_error("Failed to open project", exc)
            return
        self.install_restored_project(restored)

    def choose_project_save_path(self) -> None:
        """Choose a destination for the current lightweight project."""
        if not self._require_loaded():
            return
        if self.project_path is not None:
            suggested = self.project_path
        else:
            source = Path(self.session.directory)
            suggested = source.with_suffix(".upbr") if source.is_file() else source.parent / f"{source.name}.upbr"
        project_file, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save UPbRPy project",
            str(suggested),
            "UPbRPy Project (*.upbr)",
        )
        if project_file:
            self.save_project(project_file)

    def save_project(self, project_file: str | Path | None = None) -> None:
        """Save paths and parameters without embedding raw or reduced data."""
        if not self._require_loaded():
            return
        if self._active_worker is not None:
            self.statusBar().showMessage(
                f"Please wait for {self._busy_label.lower()} to finish."
            )
            return
        if project_file is None and self.project_path is None:
            self.choose_project_save_path()
            return
        target = (
            Path(project_file)
            if project_file is not None
            else self.project_path
        )
        if target is None:
            return
        try:
            self.apply_pending_project_edits()
            pipeline = ensure_shared_pipeline(
                self.session,
                self.workflow_preset,
            )
            written = write_reduction_project(
                pipeline,
                target,
                resume_stage=(
                    self.current_workflow_stage()
                    or PipelineStage.IMPORT
                ),
                workspace=self.project_workspace_state(),
            )
        except Exception as exc:
            self._handle_action_error("Project save failed", exc)
            return
        self.project_path = written
        self._saved_project_fingerprint = self.scientific_project_fingerprint()
        self.update_project_window_title()
        self.append_log(f"Saved lightweight project: {written}")
        self.statusBar().showMessage(f"Project saved to {written}")

    def install_restored_project(self, result: object) -> None:
        """Install a recalculated project and restore its compact UI context."""
        restored = result
        if not isinstance(restored, RestoredProject):
            raise TypeError("Project loader returned an unexpected result.")
        self.workflow_preset = restored.preset
        self.selection_method = str(
            restored.session.import_settings.get(
                "selection_method",
                restored.preset.read_kwargs.get(
                    "selection_method",
                    "threshold",
                ),
            )
        )
        self.reference_library_path = str(
            restored.reference_library_path
            or default_reference_material_library_path()
        )
        self.settings.setValue(
            "reference_library_path",
            self.reference_library_path,
        )
        self._populate_workflow_ui()

        workspace = restored.workspace
        folder_overrides = workspace.get("spot_folder_overrides", {})
        self._spot_folder_overrides = (
            {
                str(spot): str(folder)
                for spot, folder in folder_overrides.items()
            }
            if isinstance(folder_overrides, dict)
            else {}
        )
        saved_channel_colors = workspace.get("channel_colors", {})
        if isinstance(saved_channel_colors, dict):
            self.channel_colors = {
                str(channel): str(color)
                for channel, color in saved_channel_colors.items()
            }
        saved_selection_colors = workspace.get("selection_colors", {})
        if isinstance(saved_selection_colors, dict):
            self.selection_colors.update(
                {
                    str(region): str(color)
                    for region, color in saved_selection_colors.items()
                    if str(region) in DEFAULT_REGION_COLORS
                }
            )
        self.project_path = restored.project_path
        self.install_loaded_session(
            restored.session,
            restored.session.directory,
            project_path=restored.project_path,
        )
        self.session.include_export_concentrations = bool(
            workspace.get("include_export_concentrations", True)
        )
        self.session.include_export_depth_concentrations = bool(
            workspace.get("include_export_depth_concentrations", True)
        )

        # install_loaded_session supplies defaults; now overlay saved display
        # choices and repopulate once so isotope chips and spot folders agree.
        if isinstance(saved_channel_colors, dict):
            self.channel_colors.update(
                {
                    str(channel): str(color)
                    for channel, color in saved_channel_colors.items()
                    if str(channel) in self.session.channels
                }
            )
        stats_channel = str(
            workspace.get("selected_stats_channel", "") or ""
        )
        self.selected_stats_channel = (
            stats_channel
            if stats_channel in self.session.channels
            else self.session.primary_channel
        )
        sigma_level = int(workspace.get("sigma_level", 2) or 2)
        self.sigma_level = 1 if sigma_level == 1 else 2
        self.sigma_toggle_button.setChecked(self.sigma_level == 2)
        self._update_sigma_button_text()
        self.trace_plot.set_sigma_level(self.sigma_level)
        self.downhole_frac_plot.set_sigma_level(self.sigma_level)
        self.session_frac_plot.set_sigma_level(self.sigma_level)
        self._populate_selectors()

        ratio_name = str(workspace.get("ratio_name", "") or "")
        ratio_index = self.ratio_combo.findData(ratio_name)
        if ratio_index >= 0:
            self.ratio_combo.setCurrentIndex(ratio_index)

        stage_value = workspace.get(
            "workflow_stage",
            restored.resume_stage.value,
        )
        try:
            stage = PipelineStage(stage_value)
        except ValueError:
            stage = restored.resume_stage
        for index, candidate in enumerate(
            self.workflow_preset.stage_sequence
        ):
            if candidate == stage:
                self.workflow_step_list.setCurrentRow(index)
                break

        plot_mode = str(workspace.get("plot_mode", "raw"))
        cache_key = _STAGE_CACHE_KEYS.get(plot_mode)
        if (
            plot_mode == "raw"
            or (
                cache_key is not None
                and cache_key in self.session.reduction_cache
            )
        ):
            self.set_plot_mode(plot_mode, refresh=False)
        self.update_project_window_title()
        self._update_reference_library_labels()
        self.refresh_view()
        self._saved_project_fingerprint = self.scientific_project_fingerprint()
        stage_count = len(
            [
                stage
                for stage in restored.completed_stages
                if stage
                not in {PipelineStage.IMPORT, PipelineStage.SELECTION}
            ]
        )
        self.append_log(
            f"Opened {restored.project_path.name}; recalculated "
            f"{stage_count} saved reduction stages from raw data."
        )
        self.statusBar().showMessage(
            f"Project opened and recalculated: "
            f"{restored.project_path.name}"
        )

    def update_project_window_title(self) -> None:
        """Show the active project name without exposing a long path."""
        if self.project_path is not None:
            label = self.project_path.name
        elif self.session.directory:
            label = Path(self.session.directory).name
        else:
            label = ""
        suffix = "" if not label else f" — {label}"
        origin = self.session.origin or {}
        if origin:
            from urllib.parse import urlsplit

            host = urlsplit(origin.get("server", "")).netloc or origin.get("server", "")
            suffix += f" — database {host}"
        self.setWindowTitle(f"UPbRPy Studio{suffix}")
        if hasattr(self, "database_origin_label"):
            from GUI.database import origin_summary

            text, tooltip = origin_summary(origin)
            self.database_origin_label.setText(text)
            self.database_origin_label.setToolTip(tooltip)
            self.database_origin_label.setVisible(bool(text))
        self._update_database_actions()

    def database(self):
        """The database-mode flows serving this window."""
        if self.database_links is None:
            from GUI.database import DatabaseLinks

            self.database_links = DatabaseLinks(
                open_bundle=self._open_session_bundle_here,
                open_project=self.load_project_file,
                parent_widget=lambda: self,
            )
        return self.database_links

    def _open_session_bundle_here(self, bundle) -> None:
        if bundle.project_file is not None:
            self.load_project_file(bundle.project_file)
        else:
            self.load_experiment_directory(str(bundle.raw_directory))

    def _update_database_actions(self) -> None:
        """Enable the database actions that apply, with the reason otherwise."""
        if not hasattr(self, "submit_to_database_action"):
            return
        from GUI.database import ORIGIN_REQUIRED, RUN_REQUIRED, database_availability

        available, reason = database_availability()
        origin = self.session.origin or {}
        states = {
            self.open_from_database_action: (
                available,
                "Fetch a session from a laicpmsdb server and open it.",
            ),
            self.submit_to_database_action: (
                available and bool(origin) and self.session.loaded,
                "Post this reduction to its session on "
                f"{origin.get('server', 'the database')} as a new run."
                if origin
                else ORIGIN_REQUIRED,
            ),
            self.prefer_run_action: (
                available and bool(origin.get("run_id")),
                f"Make run {origin['run_id']} the session's preferred run."
                if origin.get("run_id")
                else RUN_REQUIRED,
            ),
            self.sign_out_action: (available, "Forget this computer's sign-in."),
        }
        for action, (enabled, tip) in states.items():
            action.setEnabled(enabled)
            action.setToolTip(tip if available else reason)
            action.setStatusTip(tip if available else reason)

    def register_url_scheme(self) -> None:
        from GUI.launch import LaunchError, register_url_scheme

        try:
            command = register_url_scheme()
        except (LaunchError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, "Register upbr:// links", str(exc))
            return
        QtWidgets.QMessageBox.information(
            self,
            "Register upbr:// links",
            f"Database links now open in this Studio:\n{command}",
        )

    def choose_notebook_export(self) -> None:
        """Save the current GUI settings and reviewed windows as a notebook."""
        if not self._require_loaded() or self.session.pipeline is None:
            return
        default_name = (
            Path(self.session.directory).name
            + "_UPbRPy_reproducible.ipynb"
        )
        target, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export reproducible Jupyter notebook",
            str(Path(self.session.directory).parent / default_name),
            "Jupyter Notebook (*.ipynb)",
        )
        if not target:
            return
        try:
            pipeline = ensure_shared_pipeline(
                self.session,
                self.workflow_preset,
            )
            written = write_reproducibility_notebook(
                pipeline,
                target,
                include_concentrations=self.session.include_export_concentrations,
                include_depth_concentrations=self.session.include_export_depth_concentrations,
            )
        except Exception as exc:
            self._handle_action_error("Notebook export failed", exc)
            return
        self.append_log(f"Exported reproducible notebook: {written}")
        self.statusBar().showMessage(f"Notebook saved to {written}")

    def choose_reduction_bundle_export(self) -> None:
        """Export publication tables, audit products, and replay notebook."""
        if not self._require_loaded() or self.session.pipeline is None:
            return
        target = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose reduction export folder",
            str(Path(self.session.directory).parent),
        )
        if not target:
            return
        pipeline = ensure_shared_pipeline(
            self.session,
            self.workflow_preset,
        )

        def show_result(result: object) -> None:
            bundle = result
            self.append_log(
                f"Exported {len(bundle.files)} files to {bundle.directory}."
            )
            for warning in bundle.warnings:
                self.append_log(f"Export warning: {warning}")
            self.statusBar().showMessage(
                f"Reduction bundle saved to {bundle.directory}"
            )
            self.refresh_view()

        self.run_background(
            "Exporting reduction bundle",
            lambda: export_reduction_bundle(
                pipeline,
                target,
                include_depth_profile=(
                    self.workflow_preset.supports_depth_profiles
                ),
                hash_inputs=True,
                include_concentrations=(
                    self.session.include_export_concentrations
                ),
                include_depth_concentrations=(
                    self.session.include_export_depth_concentrations
                ),
            ),
            show_result,
        )

    def reload_current_directory(self) -> None:
        if self.session.directory:
            self.load_experiment_directory(self.session.directory, primary_channel=self.session.primary_channel)

    def load_experiment_directory(
        self,
        directory: str,
        primary_channel: str | None = None,
        *,
        background: bool = True,
    ) -> None:
        """Load an experiment, using a worker for normal interactive use."""
        inferred_preset = infer_workflow_preset(directory)
        if inferred_preset.key != self.workflow_preset.key:
            self.workflow_preset = inferred_preset
            self._sync_workflow_combo()
            self._populate_workflow_ui()
            self.append_log(f"Inferred {self.workflow_preset.title.lower()} workflow from folder name.")

        chosen_primary = primary_channel or self.primary_channel_combo.currentText() or self.workflow_preset.primary_channel or self.session.primary_channel or "238U"
        detector_settings = (
            {"detector_calibration": self.session.import_settings["detector_calibration"]}
            if str(directory) == self.session.directory and "detector_calibration" in self.session.import_settings
            else None
        )
        calculation = lambda: load_session(
                directory,
                primary_channel=chosen_primary,
                preset=self.workflow_preset,
                selection_method=self.selection_method,
                import_settings=detector_settings,
            )

        if background:
            self.run_background(
                "Loading experiment",
                calculation,
                lambda result: self.install_loaded_session(
                    result,
                    directory,
                ),
            )
            return
        try:
            loaded_session = calculation()
        except Exception as exc:
            self._handle_action_error("Failed to load experiment", exc)
            return
        self.install_loaded_session(loaded_session, directory)

    def install_loaded_session(
        self,
        loaded_session: ReductionSession,
        directory: str,
        *,
        project_path: str | Path | None = None,
    ) -> None:
        """Install a worker-produced session and refresh lightweight widgets."""
        self.session = loaded_session
        self._sync_qtegra_controls()
        self.project_path = (
            None
            if project_path is None
            else Path(project_path).resolve(strict=False)
        )
        self.update_project_window_title()
        self.pending_region_updates.clear()
        self._focused_spot_name = None
        self.channel_colors = self._resolve_channel_colors(self.session.channels)
        self._populate_selectors()
        self._sync_fit_controls()
        self._sync_selection_parameter_controls()
        self._sync_plateau_filter_control()
        self._set_default_ratio_for_preset()
        self._rebuild_workflow_actions()
        self.save_project_button.setEnabled(True)
        self.export_results_button.setEnabled(True)
        self.repick_all_spots_button.setEnabled(True)
        self.set_plot_mode("raw", refresh=False)
        self.refresh_view()
        self.append_log(f"Loaded experiment source: {directory}")
        materials = reference_material_summary(self.session, self.workflow_preset)
        if materials:
            self.append_log(f"Detected reference materials: {materials}")
        self._saved_project_fingerprint = (
            self.scientific_project_fingerprint()
            if project_path is not None
            else None
        )
        self.statusBar().showMessage(f"Loaded {len(self.session.spot_names)} spots from {Path(directory).name}")

    def _sync_qtegra_controls(self) -> None:
        qtegra = self.session.qtegra
        self.capabilities_badge.setVisible(qtegra is not None)
        if qtegra is not None:
            self.capabilities_badge.setText({
                "rich": "Rich", "rich+spot_folder": "Rich + per-spot",
                "spot_folder": "Per-spot folder",
            }.get(qtegra.format, qtegra.format))
            lines = ["Capabilities: " + (", ".join(sorted(qtegra.capabilities)) or "none"),
                     "Source files:", *(str(source.path) for source in qtegra.sources)]
            if qtegra.clock_offset_hours is not None:
                lines.append(f"Per-spot clock offset from Rich: {qtegra.clock_offset_hours:+g} h")
            if qtegra.warnings:
                lines.extend(["Warnings:", *qtegra.warnings])
            self.capabilities_badge.setToolTip("\n".join(lines))
        self.detector_calibration_panel.set_session(self.session)

    def apply_detector_calibration(self, settings: dict) -> None:
        """Re-import in a worker and install the result without resetting the workspace."""
        if not self.session.loaded or self._active_worker is not None:
            return
        self.apply_pending_project_edits()
        current = self.session

        def install(result: ReductionSession) -> None:
            self.session = result
            self._sync_qtegra_controls()
            self.set_plot_mode("raw", refresh=False)
            self.refresh_view()
            self.append_log(f"Applied detector cross-calibration: {settings.get('mode', 'qtegra')}")
            applied = result.experiment.attrs.get("detector_calibration", {})
            mode = applied.get("mode", "qtegra")
            if mode == "qtegra":
                message = "Detector calibration: Qtegra factors"
            else:
                label = {"constant": "Session constant", "drift": "Time drift"}[mode]
                message = f"Applied detector cross-calibration ({label})"
                channel = self.detector_calibration_panel.channel_combo.currentText()
                fit = applied.get("channels", {}).get(channel)
                if fit is not None:
                    factor = fit["factor"]
                    formatted = f"{factor:,.0f}" if abs(factor) >= 1000 else f"{factor:.4g}"
                    message += f": {channel} {formatted}"
                    difference = fit.get("relative_difference_from_qtegra")
                    if difference is not None:
                        message += f", {100 * difference:+.1f} % vs Qtegra"
            self.statusBar().showMessage(message)

        self.run_background(
            "Applying detector cross-calibration",
            lambda: reimport_detector_calibration(current, settings), install,
        )

    def apply_theme_mode(self, theme_mode: str, *, persist: bool) -> None:
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        self.theme_mode = theme_mode
        apply_theme(app, theme_mode)
        if persist:
            self.settings.setValue("theme_mode", theme_mode)
        self._sync_theme_controls()
        self.trace_plot.apply_theme(active_theme(app))
        self.trace_plot.set_sigma_level(self.sigma_level)
        self.trace_plot.set_zoom_mode(self.trace_plot.zoom_mode())
        self.session_overview_plot.apply_theme(active_theme(app))
        self.calibration_slopes_plot.apply_theme(active_theme(app))
        self.downhole_frac_plot.apply_theme(active_theme(app))
        self.session_frac_plot.apply_theme(active_theme(app))
        self.results_table.apply_theme(active_theme(app))
        self.detector_calibration_panel.apply_theme(active_theme(app))
        theme = active_theme(app)
        self.capabilities_badge.setStyleSheet(
            f"color: {theme.accent_strong}; background: {theme.accent_soft}; padding: 2px 6px; border-radius: 4px;"
        )
        self._update_workflow_hint(self.workflow_step_list.currentRow())
        self._sync_toolbar_strips()
        QtCore.QTimer.singleShot(0, self._sync_toolbar_strips)
        # Rebuild channel colours for new background when a session is loaded
        if self.session.loaded:
            self.channel_colors = self._resolve_channel_colors(self.session.channels)
            self.isotope_grid.set_theme(theme_mode)
            self.isotope_grid.set_channels(
                self.session.channels,
                set(self.session.visible_channels),
                self.channel_colors,
                theme_name=theme_mode,
            )
        self.refresh_view()

    def on_system_color_scheme_changed(self, _scheme: object) -> None:
        if self.theme_mode == "system":
            self.apply_theme_mode("system", persist=False)

    def _sync_theme_controls(self) -> None:
        self._is_populating = True
        try:
            for mode, button in self.theme_mode_buttons.items():
                button.blockSignals(True)
                button.setChecked(mode == self.theme_mode)
                button.blockSignals(False)
            for mode, action in self.theme_actions.items():
                action.setChecked(mode == self.theme_mode)
        finally:
            self._is_populating = False

    def _sync_workflow_combo(self) -> None:
        self._is_populating = True
        try:
            for index in range(self.workflow_combo.count()):
                if self.workflow_combo.itemData(index) == self.workflow_preset.key:
                    self.workflow_combo.setCurrentIndex(index)
                    break
            action = self.workflow_menu_actions.get(
                self.workflow_preset.key
            )
            if action is not None:
                action.setChecked(True)
        finally:
            self._is_populating = False

    def _sync_selection_method_combo(self) -> None:
        self._is_populating = True
        try:
            for index in range(self.selection_method_combo.count()):
                if self.selection_method_combo.itemData(index) == self.selection_method:
                    self.selection_method_combo.setCurrentIndex(index)
                    break
            action = self.selection_menu_actions.get(self.selection_method)
            if action is not None:
                action.setChecked(True)
        finally:
            self._is_populating = False

    def _sync_selection_parameter_controls(self) -> None:
        """Show and populate the controls relevant to the onset method."""
        if not hasattr(self, "selection_parameter_stack"):
            return
        settings = (
            self.session.import_settings
            if self.session.loaded
            else self.workflow_preset.read_kwargs
        )
        method = str(
            settings.get("selection_method", self.selection_method)
        )
        self.selection_parameter_stack.setCurrentIndex(
            0 if method == "threshold" else 1
        )
        method_options = settings.get("selection_options", {})
        if not isinstance(method_options, dict):
            method_options = {}
        sigma_default = 8.0 if method == "excel_expanding_sigma" else 5.0
        self._is_populating = True
        try:
            self.selection_threshold_percent_spin.setValue(
                float(settings.get("threshold", 0.1)) * 100.0
            )
            self.selection_reference_percentile_spin.setValue(
                float(settings.get("filter_percentile", 70.0))
            )
            self.selection_sigma_spin.setValue(
                float(method_options.get("sigma_threshold", sigma_default))
            )
            plateau_mode = str(
                method_options.get("plateau_width_mode", "sweeps")
            )
            plateau_duration = (
                float(
                    method_options.get("plateau_duration_seconds", 0.0)
                )
                if plateau_mode == "duration"
                else 0.0
            )
            self.plateau_duration_spin.setValue(plateau_duration)
        finally:
            self._is_populating = False

    def _sync_plateau_filter_control(self) -> None:
        """Reflect the active spot's saved spot-mean filtering decision."""
        if not hasattr(self, "plateau_filter_combo"):
            return
        self.plateau_filter_combo.setEnabled(self.session.loaded)
        method = self.current_plateau_filter_method()
        index = self.plateau_filter_combo.findData(method)
        self._is_populating = True
        try:
            self.plateau_filter_combo.setCurrentIndex(max(index, 0))
        finally:
            self._is_populating = False

    def _populate_workflow_ui(self) -> None:
        desc = self.workflow_preset.description
        defaults_text = format_import_defaults(self.workflow_preset)
        self.workflow_description_label.setText(desc)
        self.workflow_defaults_label.setText(defaults_text)
        # Update tooltip on the info button next to "Mineral workflow"
        tooltip_html = f"<b>{self.workflow_preset.title}</b><br>{desc}<br><br><i>Defaults:</i> {defaults_text}"
        if hasattr(self, "_workflow_info_btn"):
            self._workflow_info_btn.setToolTip(tooltip_html)
        self._sync_workflow_combo()
        self._sync_selection_method_combo()
        self._sync_selection_parameter_controls()
        self._sync_plateau_filter_control()
        self._set_default_ratio_for_preset()
        self._is_populating = True
        try:
            self.workflow_step_list.clear()
            theme = active_theme(QtWidgets.QApplication.instance())
            for index, step in enumerate(self.workflow_preset.steps):
                stage = self.workflow_preset.stage_sequence[index]
                label = WORKFLOW_STAGE_LABELS.get(stage, step.title)
                item = QtWidgets.QListWidgetItem(
                    build_workflow_stage_icon(
                        stage.value,
                        state="ready",
                        accent=theme.accent,
                        muted=theme.muted,
                        success=theme.success,
                    ),
                    f"{index + 1}  {label}",
                )
                item.setToolTip(
                    f"<b>{index + 1}. {step.title}</b><br>{step.summary}"
                )
                item.setSizeHint(QtCore.QSize(0, 45))
                self.workflow_step_list.addItem(item)
            if self.workflow_step_list.count() > 0:
                self.workflow_step_list.setCurrentRow(0)
        finally:
            self._is_populating = False
        self._resize_workflow_step_list_to_contents()
        self.adapt_workflow_rail_height(self.height())
        self._update_workflow_hint(self.workflow_step_list.currentRow())
        self._rebuild_workflow_actions()
        self._refresh_workflow_status()

    def _resize_workflow_step_list_to_contents(self) -> None:
        if self.workflow_step_list.objectName() == "WorkflowStepRail":
            # The rail's step list scrolls; 140 px keeps four steps in view
            # while the rail still fits a 14-inch laptop's height.
            self.workflow_step_list.setMinimumHeight(140)
            self.workflow_step_list.setMaximumHeight(16777215)
            return
        count = self.workflow_step_list.count()
        total_height = self.workflow_step_list.frameWidth() * 2 + 8
        for index in range(count):
            item = self.workflow_step_list.item(index)
            if item is not None:
                total_height += item.sizeHint().height()
        if count > 1:
            total_height += (count - 1) * self.workflow_step_list.spacing()
        visible_height = min(total_height, 360)
        self.workflow_step_list.setMinimumHeight(min(visible_height, 180))
        self.workflow_step_list.setMaximumHeight(visible_height)

    def current_workflow_step_title(self) -> str | None:
        row = self.workflow_step_list.currentRow()
        if row < 0 or row >= len(self.workflow_preset.steps):
            return None
        return self.workflow_preset.steps[row].title

    def current_workflow_stage(self) -> PipelineStage | None:
        """Return the explicit core stage represented by the selected step."""
        row = self.workflow_step_list.currentRow()
        if row < 0 or row >= len(self.workflow_preset.stage_sequence):
            return None
        return self.workflow_preset.stage_sequence[row]

    def _update_workflow_hint(self, row: int) -> None:
        if row < 0 or row >= len(self.workflow_preset.steps):
            self.workflow_hint_title.setText("")
            self.workflow_hint_body.setText("")
            self.workflow_context_icon.clear()
            return
        step = self.workflow_preset.steps[row]
        stage = self.workflow_preset.stage_sequence[row]
        self.workflow_hint_title.setText(
            f"{row + 1}. {step.title}"
        )
        self.workflow_hint_body.setText(step.hint)
        self.workflow_hint_body.setToolTip(step.hint)
        theme = active_theme(QtWidgets.QApplication.instance())
        self.workflow_context_icon.setPixmap(
            build_workflow_stage_icon(
                stage.value,
                state="current",
                size=34,
                accent=theme.accent,
                muted=theme.muted,
                success=theme.success,
            ).pixmap(34, 34)
        )

    def on_workflow_preset_changed(self, index: int) -> None:
        if self._is_populating or index < 0:
            return
        workflow_key = str(self.workflow_combo.itemData(index))
        self.workflow_preset = get_workflow_preset(workflow_key)
        self.session.session_fit_options["polynomial_degree"] = self.workflow_preset.session_polynomial_degree
        self._populate_workflow_ui()
        self._sync_fit_controls()
        self.notes_body.setText(self._compose_notes(self.session.selected_spot))
        self.statusBar().showMessage(f"Selected {self.workflow_preset.title.lower()} workflow preset.")
        if self.session.loaded and self.session.directory:
            self.session.workflow_key = self.workflow_preset.key
            self.session.workflow_title = self.workflow_preset.title
            self.session.concentration_standards = (
                self.workflow_preset.concentration_standards
            )
            self.session.primary_68_standard = (
                self.workflow_preset.primary_standard
            )
            self.session.primary_76_standard = (
                self.workflow_preset.pb_ratio_standard
                or self.workflow_preset.primary_standard
            )
            self.session.primary_82_standard = (
                self.workflow_preset.th_ratio_standard
                or self.workflow_preset.pb_ratio_standard
                or self.workflow_preset.primary_standard
            )
            self.session.secondary_standards = (
                self.workflow_preset.secondary_standards
            )
            clear_reduction_cache(self.session)
            ensure_shared_pipeline(self.session, self.workflow_preset)
            self._sync_fit_controls()
            self.detector_calibration_panel.set_session(self.session)
            self.append_log(f"Workflow preset changed to {self.workflow_preset.title}; reload to apply notebook defaults.")
            self.refresh_view()

    def on_selection_method_changed(self, index: int) -> None:
        if self._is_populating or index < 0:
            return
        self.selection_method = str(self.selection_method_combo.itemData(index) or "threshold")
        if self.session.loaded:
            self.session.import_settings["selection_method"] = self.selection_method
            selection_options = self.session.import_settings.setdefault(
                "selection_options", {}
            )
            if isinstance(selection_options, dict) and self.selection_method != "threshold":
                selection_options.setdefault(
                    "sigma_threshold",
                    8.0
                    if self.selection_method == "excel_expanding_sigma"
                    else 5.0,
                )
            clear_reduction_cache(self.session)
        self._sync_selection_parameter_controls()
        label = selection_method_label(self.selection_method)
        self.statusBar().showMessage(f"Baseline / peak auto-pick set to {label}.")
        self.append_log(f"Auto-pick method set to {label}; re-pick spots to apply it.")

    def on_selection_threshold_percent_changed(self, value: float) -> None:
        """Update the fractional signal threshold used by threshold picking."""
        if self._is_populating or not self.session.loaded:
            return
        self.session.import_settings["threshold"] = float(value) / 100.0
        clear_reduction_cache(self.session)
        self.statusBar().showMessage(
            "Signal threshold updated; use Re-pick all spots to apply it."
        )

    def on_selection_reference_percentile_changed(self, value: float) -> None:
        """Update the percentile used to form the high-signal reference."""
        if self._is_populating or not self.session.loaded:
            return
        self.session.import_settings["filter_percentile"] = float(value)
        clear_reduction_cache(self.session)
        self.statusBar().showMessage(
            "Signal reference percentile updated; use Re-pick all spots to apply it."
        )

    def on_selection_sigma_changed(self, value: float) -> None:
        """Update the onset sigma for either sigma-based picker."""
        if self._is_populating or not self.session.loaded:
            return
        selection_options = self.session.import_settings.setdefault(
            "selection_options", {}
        )
        if not isinstance(selection_options, dict):
            selection_options = {}
            self.session.import_settings["selection_options"] = selection_options
        selection_options["sigma_threshold"] = float(value)
        clear_reduction_cache(self.session)
        self.statusBar().showMessage(
            "Onset sigma updated; use Re-pick all spots to apply it."
        )

    def on_plateau_duration_changed(self, value: float) -> None:
        """Set a session-wide plateau duration or restore workflow auto."""
        if self._is_populating or not self.session.loaded:
            return
        selection_options = self.session.import_settings.setdefault(
            "selection_options", {}
        )
        if not isinstance(selection_options, dict):
            selection_options = {}
            self.session.import_settings["selection_options"] = (
                selection_options
            )
        if value > 0.0:
            selection_options["plateau_width_mode"] = "duration"
            selection_options["plateau_duration_seconds"] = float(value)
            message = (
                f"Plateau width set to {value:g} s for the next session-wide "
                "re-pick."
            )
        else:
            preset_options = self.workflow_preset.read_kwargs.get(
                "selection_options", {}
            )
            if not isinstance(preset_options, dict):
                preset_options = {}
            selection_options["plateau_width_mode"] = str(
                preset_options.get("plateau_width_mode", "sweeps")
            )
            selection_options.pop("plateau_duration_seconds", None)
            message = (
                "Plateau width restored to the workflow auto rule for the "
                "next session-wide re-pick."
            )
        clear_reduction_cache(self.session)
        self.statusBar().showMessage(message)
        self.append_log(message)

    def on_plateau_filter_method_changed(self, index: int) -> None:
        """Set the current spot's plateau outlier rule and display channel."""
        if self._is_populating or index < 0 or not self.session.loaded:
            return
        spot_name = self.current_spot_name()
        if not spot_name:
            return
        method = str(self.plateau_filter_combo.itemData(index) or "none")
        row_mask = self.session.experiment["spot_names"].astype(str).eq(
            spot_name
        )
        self.session.experiment.loc[row_mask, "plateau_filter_method"] = method
        self.session.experiment.loc[row_mask, "plateau_filter_channel"] = (
            self.current_stats_channel()
        )
        clear_reduction_cache(self.session)
        if self.session.pipeline is not None:
            self.session.pipeline.update_experiment(self.session.experiment)
        self.append_log(
            f"Plateau spot-mean filter for {spot_name}: "
            f"{'2σ trend' if method == 'excel_2sigma' else 'none'} "
            f"on {self.current_stats_channel()}."
        )
        self.refresh_view()

    def on_workflow_step_changed(self, row: int) -> None:
        if self._is_populating:
            return
        self._update_workflow_hint(row)
        self._rebuild_workflow_actions()
        if self.session.loaded and self._workflow_step_allows_region_edits():
            self.set_plot_mode("raw", refresh=False)
        self.refresh_view()

    # ── Spot tree helpers ──────────────────────────────────────────────────

    def _spot_sample_map(self) -> dict[str, str]:
        """Return {spot_name: sample_name} from total_data if available."""
        if not self.session.loaded or "sample" not in self.session.total_data.columns:
            return {}
        td = self.session.total_data
        if "spot_names" not in td.columns:
            return {}
        return (
            td.drop_duplicates("spot_names")
            .set_index("spot_names")["sample"]
            .astype(str)
            .to_dict()
        )

    def _is_reference_material(self, sample_name: str) -> bool:
        """Return True if *sample_name* fuzzy-matches any known RM name."""
        rm_names: set[str] = set()
        for primary in (
            self.session.primary_68_standard,
            self.session.primary_76_standard,
            self.session.primary_82_standard,
            self.workflow_preset.primary_standard,
        ):
            if primary:
                rm_names.add(primary)
        rm_names.update(
            self.session.secondary_standards
            or self.workflow_preset.secondary_standards
        )
        rm_names.update(
            self.session.concentration_standards
            or self.workflow_preset.concentration_standards
        )
        try:
            archive = current_standard_archive()
            rm_names.update(str(s) for s in archive.index.tolist() if s)
        except Exception:
            pass

        def _norm(s: str) -> str:
            return re.sub(r'[^a-z0-9]', '', s.lower())

        norm_sample = _norm(sample_name)
        for rm in rm_names:
            if not rm:
                continue
            norm_rm = _norm(rm)
            if norm_rm and (norm_rm == norm_sample
                            or norm_rm in norm_sample
                            or norm_sample in norm_rm):
                return True
        return False

    def _populate_spot_tree(self) -> None:
        self.spot_list.blockSignals(True)
        self.spot_list.clear()
        spot_names = self.session.spot_names
        if not spot_names:
            self.spot_list.blockSignals(False)
            return

        sample_map = self._spot_sample_map()  # {spot_name: sample_name}

        # Categorise each spot into RM or unknown folders
        rm_folders: dict[str, list[str]] = {}    # {folder_label: [spot_names]}
        unk_folders: dict[str, list[str]] = {}   # {folder_label: [spot_names]}

        for spot_name in spot_names:
            manual = self._spot_folder_overrides.get(spot_name)
            if manual:
                unk_folders.setdefault(manual, []).append(spot_name)
                continue

            # Use sample column first; fall back to base-name inference
            sample = sample_map.get(spot_name)
            group_name = sample if sample else spot_base_name(spot_name)

            if self._is_reference_material(group_name):
                rm_folders.setdefault(group_name, []).append(spot_name)
            else:
                unk_folders.setdefault(group_name, []).append(spot_name)

        def _add_folder_node(
            root: QtWidgets.QTreeWidgetItem,
            folder_name: str,
            spots: list[str],
        ) -> QtWidgets.QTreeWidgetItem:
            folder_item = QtWidgets.QTreeWidgetItem(root, [folder_name])
            folder_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, f"__folder__{folder_name}")
            folder_item.setFlags(
                folder_item.flags()
                | QtCore.Qt.ItemFlag.ItemIsEditable
                & ~QtCore.Qt.ItemFlag.ItemIsSelectable
            )
            folder_item.setIcon(
                0, self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirIcon)
            )
            for sn in spots:
                spot_item = QtWidgets.QTreeWidgetItem(folder_item, [sn])
                spot_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, sn)
                spot_item.setFlags(
                    spot_item.flags()
                    | QtCore.Qt.ItemFlag.ItemIsSelectable
                    | QtCore.Qt.ItemFlag.ItemIsEditable
                )
            return folder_item

        rm_root = QtWidgets.QTreeWidgetItem(self.spot_list, ["Reference Materials"])
        rm_root.setData(0, QtCore.Qt.ItemDataRole.UserRole, "__root_rm__")
        rm_root.setFlags(rm_root.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)

        unk_root = QtWidgets.QTreeWidgetItem(self.spot_list, ["Unknowns"])
        unk_root.setData(0, QtCore.Qt.ItemDataRole.UserRole, "__root_unk__")
        unk_root.setFlags(unk_root.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)

        for folder_name, spots in sorted(rm_folders.items()):
            _add_folder_node(rm_root, folder_name, spots)

        for folder_name, spots in sorted(unk_folders.items()):
            _add_folder_node(unk_root, folder_name, spots)

        for root in (rm_root, unk_root):
            if root.childCount() == 0:
                root.setHidden(True)
            else:
                root.setExpanded(True)
                for i in range(root.childCount()):
                    root.child(i).setExpanded(True)

        self.spot_list.blockSignals(False)

        if self.session.selected_spot:
            self._select_spot_in_tree(self.session.selected_spot)

    def _select_spot_in_tree(self, spot_name: str) -> None:
        root = self.spot_list.invisibleRootItem()
        for i in range(root.childCount()):
            section = root.child(i)
            for j in range(section.childCount()):
                folder = section.child(j)
                for k in range(folder.childCount()):
                    leaf = folder.child(k)
                    if leaf.data(0, QtCore.Qt.ItemDataRole.UserRole) == spot_name:
                        self.spot_list.setCurrentItem(leaf)
                        return

    def _on_spot_tree_item_changed(
        self,
        current: QtWidgets.QTreeWidgetItem | None,
        _previous: QtWidgets.QTreeWidgetItem | None,
    ) -> None:
        if self._is_populating or current is None:
            return
        if current.childCount() > 0:
            return  # group node – ignore
        spot_name = current.data(0, QtCore.Qt.ItemDataRole.UserRole) or current.text(0)
        self.on_spot_changed(spot_name)

    def _on_spot_tree_context_menu(self, pos: QtCore.QPoint) -> None:
        item = self.spot_list.itemAt(pos)
        menu = QtWidgets.QMenu(self)

        is_spot = (
            item is not None
            and item.childCount() == 0
            and item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            and not str(item.data(0, QtCore.Qt.ItemDataRole.UserRole)).startswith("__")
        )
        is_folder = (
            item is not None
            and item.childCount() >= 0
            and str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "").startswith("__folder__")
        )
        is_root = (
            item is not None
            and str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "").startswith("__root_")
        )

        if is_spot:
            rename_act = menu.addAction("Rename spot…\tF2")
            menu.addSeparator()
            move_menu = menu.addMenu("Move to folder")
            for folder_name in self._get_all_folder_names():
                if folder_name != (item.parent().text(0) if item.parent() else ""):
                    act = move_menu.addAction(folder_name)
                    act.setData(("move_spot", item, folder_name))
            menu.addSeparator()
            del_act = menu.addAction("Remove spot from session…")
        elif is_folder:
            rename_act = menu.addAction("Rename folder…\tF2")
            add_sub_act = menu.addAction("New subfolder here")
            menu.addSeparator()
            del_folder_act = menu.addAction("Delete folder…")
        elif is_root or item is None:
            add_folder_act = menu.addAction("New folder under Unknowns")
        else:
            return

        chosen = menu.exec(self.spot_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        data = chosen.data()
        if isinstance(data, tuple) and data[0] == "move_spot":
            _, leaf_item, folder_name = data
            spot_name = leaf_item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            self._spot_folder_overrides[spot_name] = folder_name
            self._populate_spot_tree()
        elif is_spot and chosen.text().startswith("Rename"):
            self.spot_list.editItem(item, 0)
        elif is_spot and chosen.text().startswith("Remove"):
            self._on_spot_tree_delete()
        elif is_folder and chosen.text().startswith("Rename"):
            self.spot_list.editItem(item, 0)
        elif is_folder and chosen.text().startswith("New sub"):
            self._on_spot_add_folder(parent_folder=item.text(0))
        elif is_folder and chosen.text().startswith("Delete"):
            self._on_spot_folder_delete(item)
        elif (is_root or item is None) and chosen.text().startswith("New folder"):
            self._on_spot_add_folder()

    def _get_all_folder_names(self) -> list[str]:
        """Return all current folder names visible in the spot tree."""
        names: list[str] = []
        root = self.spot_list.invisibleRootItem()
        for i in range(root.childCount()):
            section = root.child(i)
            for j in range(section.childCount()):
                folder = section.child(j)
                names.append(folder.text(0))
        return names

    def _on_spot_add_folder(self, parent_folder: str | None = None) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self, "New Folder", "Folder name:"
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        # Add an empty entry so it appears; spots can be moved in later
        if name not in self._spot_folder_overrides.values():
            # Ensure the folder appears by adding a sentinel placeholder in
            # unk_folders – we do this by just refreshing; the folder will
            # appear when the first spot is moved into it.
            # For now just note its existence via a dummy entry that won't conflict.
            pass
        QtWidgets.QMessageBox.information(
            self,
            "Folder created",
            f"Folder \"{name}\" created.\n\nUse \"Move to folder\" on a spot to populate it.",
        )

    def _on_spot_folder_delete(self, folder_item: QtWidgets.QTreeWidgetItem) -> None:
        folder_name = folder_item.text(0)
        n = folder_item.childCount()
        msg = (
            f"Delete folder \"{folder_name}\"?"
            + (f"\n\n{n} spot(s) will be returned to auto-grouping." if n else "")
        )
        if QtWidgets.QMessageBox.question(self, "Delete folder", msg) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        # Remove overrides for all spots in this folder
        for k, v in list(self._spot_folder_overrides.items()):
            if v == folder_name:
                del self._spot_folder_overrides[k]
        self._populate_spot_tree()

    def _on_spot_tree_delete(self) -> None:
        item = self.spot_list.currentItem()
        if item is None:
            return
        spot_name = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not spot_name or str(spot_name).startswith("__"):
            return
        if QtWidgets.QMessageBox.question(
            self,
            "Remove spot",
            f"Remove \"{spot_name}\" from this session?\n\nThis cannot be undone.",
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        # Remove from session spot list (doesn't delete the file)
        if spot_name in self.session.spot_names:
            self.session.spot_names = [s for s in self.session.spot_names if s != spot_name]
        self._spot_folder_overrides.pop(spot_name, None)
        clear_reduction_cache(self.session)
        self._populate_spot_tree()
        self.refresh_view()

    def _on_spot_tree_item_renamed(
        self, item: QtWidgets.QTreeWidgetItem, column: int
    ) -> None:
        """Handle in-tree rename edits."""
        if self._is_populating or column != 0:
            return
        role_data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        new_text = item.text(0).strip()
        if not new_text:
            return
        if role_data and not str(role_data).startswith("__"):
            # Spot renamed – update any folder override key
            old_name = str(role_data)
            if old_name in self._spot_folder_overrides:
                self._spot_folder_overrides[new_text] = self._spot_folder_overrides.pop(old_name)
            # Update UserRole so navigation still works
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, new_text)


    def _populate_selectors(self) -> None:
        self._is_populating = True
        try:
            self.primary_channel_combo.clear()
            self.primary_channel_combo.addItems(self.session.channels)
            primary_index = self.primary_channel_combo.findText(self.session.primary_channel)
            if primary_index >= 0:
                self.primary_channel_combo.setCurrentIndex(primary_index)

            self._populate_spot_tree()

            self.channel_list.clear()
            for channel in self.session.channels:
                checked = channel in self.session.visible_channels
                item = make_list_item(channel, checked=checked)
                self._apply_channel_item_swatch(item, channel)
                self.channel_list.addItem(item)
            stats_channel = self.selected_stats_channel if self.selected_stats_channel in self.session.channels else self.session.primary_channel
            stats_index = next((index for index, channel in enumerate(self.session.channels) if channel == stats_channel), 0)
            if self.channel_list.count() > 0:
                self.channel_list.setCurrentRow(stats_index)

            # Sync isotope grid
            _theme_name = active_theme(QtWidgets.QApplication.instance()).name
            self.isotope_grid.set_channels(
                self.session.channels,
                set(self.session.visible_channels),
                self.channel_colors,
                theme_name=_theme_name,
            )
            self.isotope_grid.set_selected_channel(stats_channel)
        finally:
            self._is_populating = False

    def on_primary_channel_changed(self, channel: str) -> None:
        if self._is_populating or not channel:
            return
        if not self.session.loaded:
            self.session.primary_channel = channel
            return
        if channel == self.session.primary_channel:
            return
        self.append_log(f"Reloading with primary channel: {channel}")
        self.load_experiment_directory(self.session.directory, primary_channel=channel)

    def on_spot_changed(self, spot_name: str) -> None:
        if self._is_populating:
            return
        if self.pending_region_updates:
            self.pending_region_updates.clear()
            self.append_log("Discarded unapplied window edits while switching spots.")
        self.session.selected_spot = spot_name or None
        self._sync_plateau_filter_control()
        self.refresh_view()

    def on_channel_toggled(self, _item: QtWidgets.QListWidgetItem) -> None:
        if self._is_populating:
            return
        self.refresh_view()

    def on_channel_selection_changed(self, current: QtWidgets.QListWidgetItem | None, _previous: QtWidgets.QListWidgetItem | None) -> None:
        if self._is_populating:
            return
        self.selected_stats_channel = current.text() if current is not None else self.session.primary_channel
        self.refresh_view()

    def choose_channel_color(self, item: QtWidgets.QListWidgetItem | None = None) -> None:
        target_item = item or self.channel_list.currentItem()
        if target_item is None:
            return
        channel = target_item.text()
        current_color = QtGui.QColor(self.channel_colors.get(channel, "#6177a2"))
        chosen = QtWidgets.QColorDialog.getColor(current_color, self, f"Select color for {channel}")
        if not chosen.isValid():
            return
        self.channel_colors[channel] = chosen.name(QtGui.QColor.NameFormat.HexRgb)
        self._persist_channel_colors()
        self._apply_channel_item_swatch(target_item, channel)
        self.refresh_view()

    def choose_region_color(self, region_name: str) -> None:
        current_color = QtGui.QColor(self.selection_colors.get(region_name, DEFAULT_REGION_COLORS[region_name]))
        chosen = QtWidgets.QColorDialog.getColor(current_color, self, f"Select {region_name} region color")
        if not chosen.isValid():
            return
        self.selection_colors[region_name] = chosen.name(QtGui.QColor.NameFormat.HexRgb)
        self._persist_selection_colors()
        self.refresh_view()

    def on_ratio_changed(self, _index: int) -> None:
        if self._is_populating:
            return
        self.refresh_view()

    def on_sigma_toggle_clicked(self) -> None:
        self.sigma_level = 2 if self.sigma_toggle_button.isChecked() else 1
        self._update_sigma_button_text()
        self.trace_plot.set_sigma_level(self.sigma_level)
        self.downhole_frac_plot.set_sigma_level(self.sigma_level)
        self.session_frac_plot.set_sigma_level(self.sigma_level)
        self.refresh_view()

    def _update_sigma_button_text(self) -> None:
        if hasattr(self, "sigma_toggle_button"):
            self.sigma_toggle_button.setText(f"{self.sigma_level}σ")

    def on_plot_region_changed(self, region_name: str, bounds: tuple[float, float]) -> None:
        if not self._workflow_step_allows_region_edits():
            return
        self.pending_region_updates[region_name] = bounds
        self._refresh_workflow_status()
        self.statusBar().showMessage("Selection windows edited. Apply them to update the reduction stages.")

    def checked_channels(self) -> list[str]:
        # Primary source: isotope grid (if it has any channels loaded)
        grid_channels = self.isotope_grid.checked_channels()
        if grid_channels or self.session.channels:
            return grid_channels
        # Fallback: hidden channel_list
        channels: list[str] = []
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                channels.append(item.text())
        return channels

    def _on_isotope_chip_toggled(self, channel: str, checked: bool) -> None:
        """Called when an isotope chip is double-clicked (toggled) in the grid."""
        if self._is_populating:
            return
        # Mirror into the hidden channel_list so legacy code still works
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            if item.text() == channel:
                self._is_populating = True
                item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
                self._is_populating = False
                break
        self.refresh_view()

    def _on_isotope_channel_selected(self, channel: str) -> None:
        """Called when an isotope chip is single-clicked (stats channel selected)."""
        if self._is_populating:
            return
        self.selected_stats_channel = channel
        # Mirror to hidden channel_list current selection so current_stats_channel() works
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            if item.text() == channel:
                self._is_populating = True
                self.channel_list.setCurrentRow(index)
                self._is_populating = False
                break
        self.refresh_view()

    def _on_isotope_color_change(self, channel: str) -> None:
        """Open colour picker for a channel from the isotope grid."""
        current_color = QtGui.QColor(self.channel_colors.get(channel, "#6177a2"))
        chosen = QtWidgets.QColorDialog.getColor(current_color, self, f"Select color for {channel}")
        if not chosen.isValid():
            return
        self.channel_colors[channel] = chosen.name(QtGui.QColor.NameFormat.HexRgb)
        self._persist_channel_colors()
        # Update the chip colour
        self.isotope_grid.set_color(channel, self.channel_colors[channel])
        # Also update the hidden list item swatch
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            if item.text() == channel:
                self._apply_channel_item_swatch(item, channel)
                break
        self.refresh_view()


    def current_spot_name(self) -> str | None:
        current_item = self.spot_list.currentItem()
        if current_item is not None and current_item.childCount() == 0:
            spot_name = current_item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if spot_name:
                return spot_name
            return current_item.text(0)
        return self.session.selected_spot

    def current_ratio_name(self) -> str:
        return str(self.ratio_combo.currentData() or self.workflow_preset.default_ratio)

    def current_stats_channel(self) -> str:
        current_item = self.channel_list.currentItem()
        if current_item is not None:
            return current_item.text()
        if self.selected_stats_channel:
            return self.selected_stats_channel
        return self.session.primary_channel

    def current_plateau_filter_method(
        self,
        spot_name: str | None = None,
    ) -> str:
        """Return the saved spot-mean plateau filter for the active spot."""
        target = spot_name or self.current_spot_name()
        if not target or self.session.experiment.empty:
            return "none"
        row = self.session.experiment[
            self.session.experiment["spot_names"].astype(str).eq(target)
        ]
        if row.empty or "plateau_filter_method" not in row.columns:
            return "none"
        value = row["plateau_filter_method"].iloc[0]
        return "none" if value is None or pd.isna(value) else str(value)

    def _resolve_channel_colors(self, channels: list[str]) -> dict[str, str]:
        theme_name = active_theme(QtWidgets.QApplication.instance()).name
        colors = build_channel_colors(channels, theme_name=theme_name)
        saved_text = str(self.settings.value("channel_colors_json", "{}", type=str) or "{}")
        try:
            saved_colors = json.loads(saved_text)
        except json.JSONDecodeError:
            saved_colors = {}
        if isinstance(saved_colors, dict):
            for channel in channels:
                if channel in saved_colors and isinstance(saved_colors[channel], str):
                    colors[channel] = saved_colors[channel]
        return colors

    def _persist_channel_colors(self) -> None:
        self.settings.setValue("channel_colors_json", json.dumps(self.channel_colors))

    def _resolve_selection_colors(self) -> dict[str, str]:
        colors = dict(DEFAULT_REGION_COLORS)
        saved_text = str(self.settings.value("selection_colors_json", "{}", type=str) or "{}")
        try:
            saved_colors = json.loads(saved_text)
        except json.JSONDecodeError:
            saved_colors = {}
        if isinstance(saved_colors, dict):
            for key in colors:
                value = saved_colors.get(key)
                if isinstance(value, str) and value:
                    colors[key] = value
        return colors

    def _persist_selection_colors(self) -> None:
        self.settings.setValue("selection_colors_json", json.dumps(self.selection_colors))

    def _apply_channel_item_swatch(self, item: QtWidgets.QListWidgetItem, channel: str) -> None:
        color = self.channel_colors.get(channel, "#6177a2")
        pixmap = QtGui.QPixmap(14, 14)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 36), 1))
        painter.setBrush(QtGui.QColor(color))
        painter.drawRoundedRect(1, 1, 12, 12, 4, 4)
        painter.end()
        item.setIcon(QtGui.QIcon(pixmap))

    def set_plot_mode(self, plot_mode: str, *, refresh: bool) -> None:
        self.session.plot_mode = plot_mode
        self._sync_plot_mode_buttons()
        if refresh:
            self.refresh_view()

    def _current_plot_interaction_target(self) -> object:
        mode = self.session.plot_mode
        if mode == "calibration_slopes":
            return self.calibration_slopes_plot
        if mode == "downhole_ratios":
            return self.downhole_frac_plot
        if mode == "session_ratios":
            return self.session_frac_plot
        if mode in {"depth_profile", "final_results"}:
            return self.results_table
        return self.trace_plot

    def on_results_review_tab_changed(self, _index: int) -> None:
        """Apply the shared pan/zoom toolbar when a concordia tab opens."""
        if self.session.plot_mode not in {"depth_profile", "final_results"}:
            return
        self._apply_interaction_controls_to_current_plot()
        self.plot_control_bar.setEnabled(
            self.results_table.active_concordia() is not None
        )

    def _selected_interaction_mode(self) -> str:
        for key, button in self.interact_mode_buttons.items():
            if button.isChecked():
                return key
        return "zoom"

    def _selected_zoom_mode(self) -> str:
        for key, button in self.zoom_mode_buttons.items():
            if button.isChecked():
                return key
        return "xy"

    def _apply_interaction_controls_to_current_plot(self) -> None:
        target = self._current_plot_interaction_target()
        if hasattr(target, "set_interaction_mode"):
            target.set_interaction_mode(self._selected_interaction_mode())
        if hasattr(target, "set_zoom_mode"):
            target.set_zoom_mode(self._selected_zoom_mode())

    def set_interaction_mode(self, mode: str) -> None:
        target = self._current_plot_interaction_target()
        if hasattr(target, "set_interaction_mode"):
            target.set_interaction_mode(mode)
        current_mode = target.interaction_mode() if hasattr(target, "interaction_mode") else mode
        for key, button in self.interact_mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == current_mode)
            button.blockSignals(False)

    def set_zoom_mode(self, mode: str) -> None:
        target = self._current_plot_interaction_target()
        if hasattr(target, "set_zoom_mode"):
            target.set_zoom_mode(mode)
        current_mode = target.zoom_mode() if hasattr(target, "zoom_mode") else mode
        for key, button in self.zoom_mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == current_mode)
            button.blockSignals(False)

    def restore_default_main_view(self) -> None:
        if not self.session.loaded:
            self.trace_plot.show_preview(self.selection_colors)
            self.session_overview_plot.show_preview()
            return
        target = self._current_plot_interaction_target()
        if hasattr(target, "restore_default_view"):
            target.restore_default_view()
            return
        self._focus_selected_spot(self.current_spot_name())

    def _sync_plot_mode_buttons(self) -> None:
        cache = self.session.reduction_cache if self.session.loaded else {}
        for mode, button in self.plot_mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(mode == self.session.plot_mode)
            cache_key = _STAGE_CACHE_KEYS.get(mode)
            if mode == "raw":
                button.setEnabled(True)
            elif not self.session.loaded:
                button.setEnabled(False)
            else:
                button.setEnabled(cache_key is not None and cache_key in cache)
            button.blockSignals(False)
        self.ratio_combo.setEnabled(
            self.session.plot_mode
            in {"raw_ratios", "downhole_ratios", "session_ratios"}
        )
        if self.session.plot_mode in {"depth_profile", "final_results"}:
            controls_enabled = (
                self.results_table.active_concordia() is not None
            )
        else:
            controls_enabled = True
        self.plot_control_bar.setEnabled(controls_enabled)

    def show_raw_traces(self) -> None:
        self.set_plot_mode("raw", refresh=True)

    def refresh_view(self) -> None:
        self.trace_plot.apply_theme(active_theme(QtWidgets.QApplication.instance()))
        if not self.session.loaded:
            self.trace_plot.show_preview(self.selection_colors)
            self.session_overview_plot.show_preview()
            self.notes_body.setText(self._compose_notes(None))
            self._refresh_workflow_status()
            return

        selected_spot = self.current_spot_name()
        if not selected_spot and self.session.spot_names:
            selected_spot = self.session.spot_names[0]
        self.session.selected_spot = selected_spot
        previous_x_range = self.trace_plot.visible_x_range()
        previous_y_range = self.trace_plot.visible_y_range()
        same_spot = bool(selected_spot) and selected_spot == self._focused_spot_name
        same_mode = self.session.plot_mode == self._last_plot_mode

        # Detect transition from zero channels → some channels so we re-focus
        # instead of blindly restoring the "empty placeholder" view range (0, 1).
        had_no_channels = not bool(self.session.visible_channels)
        visible_channels = self.checked_channels()
        transitioning_from_empty = had_no_channels and bool(visible_channels)

        should_focus_spot = bool(selected_spot) and (
            not same_spot or not same_mode or transitioning_from_empty
        )
        preserve_main_view = same_spot and same_mode and not transitioning_from_empty

        self.session.visible_channels = visible_channels

        spot_data = get_spot_data(self.session, selected_spot)
        regions = get_selection_regions(self.session, selected_spot)
        editable_regions = self._workflow_step_allows_region_edits()

        try:
            self._render_current_plot(selected_spot, spot_data, visible_channels, regions, editable_regions)
        except Exception as exc:
            self.append_log(f"Plot mode {self.session.plot_mode} failed: {exc}")
            self.session.plot_mode = "raw"
            self._sync_plot_mode_buttons()
            self.trace_plot.show_spot(
                spot_data,
                visible_channels,
                self.channel_colors,
                regions,
                editable_regions=editable_regions,
                stats_channel=self.current_stats_channel(),
                region_colors=self.selection_colors,
                plateau_filter_method=self.current_plateau_filter_method(
                    selected_spot
                ),
            )
        if should_focus_spot:
            self._focus_selected_spot(selected_spot)
        elif preserve_main_view and visible_channels:
            self.trace_plot.set_x_window(previous_x_range)
            self.trace_plot.set_y_window(previous_y_range)
        self._refresh_session_overview()
        if should_focus_spot:
            self._focused_spot_name = selected_spot
        self._last_plot_mode = self.session.plot_mode

        self._apply_project_summary(project_summary(self.session))
        self.notes_body.setText(self._compose_notes(selected_spot))
        self._refresh_workflow_status()

    def _refresh_session_overview(self) -> None:
        overview_channel = self.current_stats_channel()
        if overview_channel not in self.session.total_data.columns:
            overview_channel = self.session.primary_channel
        self.session_overview_plot.show_series(
            self.session.total_data,
            overview_channel,
            self.channel_colors.get(overview_channel, "#6177a2"),
            self.trace_plot.visible_x_range(),
        )

    def on_trace_x_window_changed(self, bounds: tuple[float, float]) -> None:
        self.session_overview_plot.set_window(bounds)

    def on_overview_window_changed(self, bounds: tuple[float, float]) -> None:
        """Navigate the complete session without rebuilding trace items."""
        self.trace_plot.set_x_window(bounds)
        if self.trace_plot.zoom_mode() == "xy":
            # X navigation remains immediate. Coalescing the more expensive
            # visible-data y fit prevents dozens of full-array scans during a
            # single overview drag while still following it interactively.
            self._overview_y_fit_timer.start()

    def _focus_selected_spot(self, spot_name: str | None) -> None:
        if not spot_name:
            return
        spot_frame = get_spot_data(self.session, spot_name)
        if spot_frame.empty or "Time" not in spot_frame:
            return
        start_time = float(spot_frame["Time"].min())
        end_time = float(spot_frame["Time"].max())
        padding = max(0.25, (end_time - start_time) * 0.04)
        self.trace_plot.set_x_window((start_time - padding, end_time + padding))
        self.trace_plot.fit_y_to_visible_data()

    def _render_current_plot(
        self,
        selected_spot: str | None,
        raw_spot_data: object,
        visible_channels: list[str],
        regions: dict[str, tuple[float, float] | None],
        editable_regions: bool,
    ) -> None:
        spot_data = raw_spot_data if isinstance(raw_spot_data, QtCore.QObject) is False else raw_spot_data
        # Switch the plot stack page
        stack_idx = _PLOT_STACK_IDX.get(self.session.plot_mode, 0)
        self.plots_stack.setCurrentIndex(stack_idx)
        self._apply_interaction_controls_to_current_plot()

        app = QtWidgets.QApplication.instance()
        theme = active_theme(app) if app else None
        ratio_name = self.current_ratio_name()

        if self.session.plot_mode == "calibration_slopes":
            data = get_calibration_slope_data(self.session, self.workflow_preset)
            if theme:
                self.calibration_slopes_plot.show_data(data, theme)
            return

        if self.session.plot_mode == "downhole_ratios":
            data = get_downhole_plot_data(self.session, self.workflow_preset, ratio_name)
            if theme:
                self.downhole_frac_plot.show_data(data, theme)
            return

        if self.session.plot_mode == "session_ratios":
            data = get_session_plot_data(self.session, self.workflow_preset, ratio_name)
            if theme:
                self.session_frac_plot.show_data(data, theme)
            return

        if self.session.plot_mode == "final_results":
            finalized = self.session.reduction_cache.get("finalized_results")
            if finalized is not None:
                self.results_table.show_results(finalized)
            return

        if self.session.plot_mode == "depth_profile":
            depth_profile = self.session.reduction_cache.get(
                "depth_profile_results"
            )
            if depth_profile is not None:
                self.results_table.show_depth_profile(
                    depth_profile,
                    selected_spot,
                    include_calibration_uncertainty=(
                        self.session.include_calibration_uncertainty
                    ),
                )
            return

        if self.session.plot_mode == "raw":
            self.trace_plot.show_spot(
                self.session.total_data,
                visible_channels,
                self.channel_colors,
                regions,
                editable_regions=editable_regions,
                stats_channel=self.current_stats_channel(),
                region_colors=self.selection_colors,
                spot_name=selected_spot,
                plateau_filter_method=self.current_plateau_filter_method(
                    selected_spot
                ),
                selection_frame=spot_data,
            )
            return

        if self.session.plot_mode == "baseline_subtracted":
            baseline_subtracted = ensure_baseline_subtracted(self.session)
            channels = [channel for channel in visible_channels if channel in baseline_subtracted.columns]
            self.trace_plot.show_spot(
                baseline_subtracted,
                channels,
                self.channel_colors,
                regions,
                editable_regions=editable_regions,
                y_label="Baseline-subtracted cps",
                stats_channel=self.current_stats_channel(),
                region_colors=self.selection_colors,
                spot_name=selected_spot,
                plateau_filter_method=self.current_plateau_filter_method(
                    selected_spot
                ),
            )
            return

        if self.session.plot_mode == "normalized_plateau":
            normalized = ensure_normalized_plateau(self.session, self.workflow_preset)
            channels = [channel for channel in visible_channels if channel in normalized.columns]
            self.trace_plot.show_series(normalized, channels, self.channel_colors, y_label="Normalized plateau cps", line_style="scatter")
            return

        if self.session.plot_mode == "raw_ratios":
            ratios = ensure_raw_ratios(self.session)
            self.trace_plot.show_series(ratios, [ratio_name], {ratio_name: RATIO_COLORS.get(ratio_name, "#1f63cf")}, y_label=ratio_name, line_style="scatter")
            return

        self.trace_plot.show_spot(
            spot_data,
            visible_channels,
            self.channel_colors,
            regions,
            editable_regions=editable_regions,
            stats_channel=self.current_stats_channel(),
            region_colors=self.selection_colors,
            spot_name=selected_spot,
            plateau_filter_method=self.current_plateau_filter_method(
                selected_spot
            ),
        )

    def _apply_channel_checks(self, channels: list[str]) -> None:
        self._is_populating = True
        try:
            for index in range(self.channel_list.count()):
                item = self.channel_list.item(index)
                item.setCheckState(
                    QtCore.Qt.CheckState.Checked if item.text() in channels else QtCore.Qt.CheckState.Unchecked
                )
        finally:
            self._is_populating = False

    def _apply_project_summary(self, summary: dict[str, str]) -> None:
        self.project_name_label.setText(summary["name"])
        self.project_spots_label.setText(summary["spots"])
        self.project_engine_label.setText(summary["engine"])

    def _rebuild_workflow_actions(self) -> None:
        while self.workflow_actions_layout.count():
            item = self.workflow_actions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, (label, handler, accent) in enumerate(self._workflow_action_specs()):
            button = QtWidgets.QPushButton(label)
            button.setMinimumHeight(36)
            if accent:
                button.setProperty("accent", True)
            button.clicked.connect(handler)
            self.workflow_actions_layout.addWidget(button)
            if index >= 1:
                break

    def _workflow_action_specs(self) -> list[tuple[str, object, bool]]:
        if not self.session.loaded:
            return [("Import experiment directory", self.choose_experiment_directory, True)]

        stage = self.current_workflow_stage()
        if stage == PipelineStage.IMPORT:
            return [
                ("Auto-pick current spot", self.run_autopick_current_spot, True),
                ("Reload session defaults", self.reload_current_directory, False),
            ]
        if stage == PipelineStage.SELECTION:
            return [
                ("Apply baseline and plateau edits", self.apply_current_windows, True),
                ("Reset current spot", self.run_autopick_current_spot, False),
                ("Show raw traces", self.show_raw_traces, False),
            ]
        if stage == PipelineStage.BASELINE:
            return [
                ("Subtract baseline", self.run_baseline_preview, True),
                ("Show raw traces", self.show_raw_traces, False),
            ]
        if stage == PipelineStage.NORMALIZATION:
            return [
                ("Apply internal normalization", self.run_internal_normalization_preview, True),
                ("Show raw traces", self.show_raw_traces, False),
            ]
        if stage == PipelineStage.CONCENTRATION:
            return [
                ("Calibrate and calculate concentrations", self.run_concentration_stage, True),
                ("Show normalized plateau", self.run_internal_normalization_preview, False),
            ]
        if stage == PipelineStage.RATIOS:
            return [
                ("Build raw isotope ratios", self.run_raw_ratio_preview, True),
                ("Show raw traces", self.show_raw_traces, False),
            ]
        if stage == PipelineStage.DOWNHOLE:
            return [
                ("Apply downhole fractionation", self.run_downhole_preview, True),
                ("Show raw isotope ratios", self.run_raw_ratio_preview, False),
            ]
        if stage == PipelineStage.SESSION:
            return [
                ("Apply session fractionation", self.run_session_correction_preview, True),
                ("Show downhole fractionation", self.run_downhole_preview, False),
            ]
        if stage == PipelineStage.DEPTH_PROFILE:
            return [
                ("Calculate depth profiles", self.run_depth_profile_review, True),
                ("Show final spot means", self.run_final_review, False),
            ]
        return [
            ("Calculate final results", self.run_final_review, True),
            ("Show session correction", self.run_session_correction_preview, False),
        ]

    def _workflow_step_allows_region_edits(self) -> bool:
        return self.current_workflow_stage() in {
            PipelineStage.IMPORT,
            PipelineStage.SELECTION,
        }

    def _refresh_workflow_status(self) -> None:
        self._sync_workflow_step_locks()
        if not self.session.loaded:
            message = "Open an experiment to begin"
            self.workflow_status_label.setText(message)
            self.workflow_status_label.setToolTip(message)
            return
        quality = selection_quality(self.session, self.current_spot_name())
        parts = [
            f"Baseline {quality['baseline_points']} pts",
            f"Plateau {quality['plateau_points']} pts",
        ]
        if self.pending_region_updates:
            parts.append("edited windows pending apply")
        if quality["warnings"]:
            parts.append("Selection checker: " + "; ".join(quality["warnings"]))
        else:
            parts.append("Selection checker: ready")
        materials = reference_material_summary(self.session, self.workflow_preset)
        if materials:
            parts.append(materials)
        full_status = " | ".join(parts)
        compact_status = (
            f"B {quality['baseline_points']} · "
            f"P {quality['plateau_points']} · "
            f"{'Review warning' if quality['warnings'] else 'Selections ready'}"
        )
        self.workflow_status_label.setText(compact_status)
        self.workflow_status_label.setToolTip(full_status)

    def _sync_workflow_step_locks(self) -> None:
        """Show ordered stage state from the shared dependency-aware cache."""
        completed = (
            set(self.session.pipeline.cache.stages())
            if self.session.loaded and self.session.pipeline is not None
            else set()
        )
        app = QtWidgets.QApplication.instance()
        theme = active_theme(app) if app else None
        muted_color = QtGui.QColor(theme.muted) if theme else QtGui.QColor("#888888")
        normal_color = QtGui.QColor(theme.text) if theme else QtGui.QColor("#000000")
        done_color = QtGui.QColor(theme.success) if (theme and hasattr(theme, "success")) else QtGui.QColor("#19a188")
        accent_color = QtGui.QColor(theme.accent) if (theme and hasattr(theme, "accent")) else QtGui.QColor("#69e2ff")

        for index in range(self.workflow_step_list.count()):
            item = self.workflow_step_list.item(index)
            if index >= len(self.workflow_preset.stage_sequence):
                continue
            stage = self.workflow_preset.stage_sequence[index]
            is_done = stage in completed
            if index == 0:
                unlocked = True
            else:
                previous_stage = self.workflow_preset.stage_sequence[index - 1]
                unlocked = self.session.loaded and previous_stage in completed

            if not unlocked:
                color = muted_color
                icon_state = "locked"
            elif is_done:
                color = done_color
                icon_state = "complete"
            elif unlocked and not is_done:
                is_current = self.workflow_step_list.currentRow() == index
                color = accent_color if is_current else normal_color
                icon_state = "current" if is_current else "ready"
            else:
                color = normal_color
                icon_state = "ready"

            label = WORKFLOW_STAGE_LABELS.get(
                stage,
                self.workflow_preset.steps[index].title,
            )
            item.setText(f"{index + 1}  {label}")
            item.setIcon(
                build_workflow_stage_icon(
                    stage.value,
                    state=icon_state,
                    accent=theme.accent if theme else "#2468d4",
                    muted=theme.muted if theme else "#788da6",
                    success=theme.success if theme else "#159a76",
                )
            )
            item.setData(QtCore.Qt.ItemDataRole.UserRole, stage.value)

            if unlocked:
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsSelectable | QtCore.Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
            item.setForeground(color)



    def _compose_notes(self, selected_spot: str | None) -> str:
        notes = build_notes(self.session, selected_spot, self.workflow_preset, self.current_workflow_step_title())
        if not self.session.loaded:
            return notes

        cache = self.session.reduction_cache
        stages: list[str] = []
        if "baseline_subtracted_total_data" in cache:
            stages.append("baseline subtraction")
        if "normalized_plateau_data" in cache:
            stages.append("internal normalization")
        if "concentration_results" in cache:
            stages.append("concentration calibration")
        if "plateau_data_with_raw_ratios" in cache:
            stages.append("raw ratios")
        if "downhole_results" in cache:
            stages.append("downhole correction")
        if "session_correction_results" in cache:
            stages.append("session correction")

        extras = []
        if stages:
            extras.append("Computed stages: " + ", ".join(stages) + ".")
        materials = reference_material_summary(self.session, self.workflow_preset)
        if materials:
            extras.append("Reference materials: " + materials + ".")
        if self.session.plot_mode in {"raw_ratios", "downhole_ratios", "session_ratios"}:
            extras.append(f"Ratio view: {self.current_ratio_name()}.")
        return notes + (" " + " ".join(extras) if extras else "")

    def _set_default_ratio_for_preset(self) -> None:
        self._is_populating = True
        try:
            index = self.ratio_combo.findData(self.workflow_preset.default_ratio)
            if index >= 0:
                self.ratio_combo.setCurrentIndex(index)
        finally:
            self._is_populating = False

    def _handle_action_error(self, title: str, exc: Exception) -> None:
        QtWidgets.QMessageBox.critical(self, title, str(exc))
        self.append_log(f"{title} failed: {exc}")

    def run_background(
        self,
        label: str,
        calculation: Callable[[], object],
        on_success: Callable[[object], None],
    ) -> None:
        """Run one scientific task without blocking Qt event processing."""
        if self._active_worker is not None:
            self.statusBar().showMessage(
                f"Please wait for {self._busy_label.lower()} to finish."
            )
            return

        worker = FunctionWorker(calculation)
        self._active_worker = worker
        self._busy_label = label
        self.busy_progress.setVisible(True)
        self.workflow_actions_card.setEnabled(False)
        self.workflow_step_list.setEnabled(False)
        self.fit_card.setEnabled(False)
        self.standards_card.setEnabled(False)
        self.detector_calibration_panel.setEnabled(False)
        self.decision_card.setEnabled(False)
        self.primary_channel_combo.setEnabled(False)
        self.workflow_combo.setEnabled(False)
        self.selection_method_combo.setEnabled(False)
        self.repick_all_spots_button.setEnabled(False)
        self.save_project_button.setEnabled(False)
        self.export_results_button.setEnabled(False)
        self.statusBar().showMessage(f"{label}…")

        def report_success(result: object) -> None:
            try:
                on_success(result)
            except Exception as exc:
                self._handle_action_error(f"{label} display failed", exc)

        def report_failure(message: str, details: str) -> None:
            QtWidgets.QMessageBox.critical(self, f"{label} failed", message)
            self.append_log(f"{label} failed: {message}")
            self.append_log(details.rstrip())

        def finish() -> None:
            self._active_worker = None
            self._busy_label = ""
            self.busy_progress.setVisible(False)
            self.workflow_actions_card.setEnabled(True)
            self.workflow_step_list.setEnabled(True)
            self.fit_card.setEnabled(True)
            self.standards_card.setEnabled(True)
            self.detector_calibration_panel.setEnabled(bool(
                self.session.qtegra and self.session.qtegra.has("analog_counter")
            ))
            self.decision_card.setEnabled(True)
            self.primary_channel_combo.setEnabled(True)
            self.workflow_combo.setEnabled(True)
            self.selection_method_combo.setEnabled(True)
            self.repick_all_spots_button.setEnabled(self.session.loaded)
            self.save_project_button.setEnabled(self.session.loaded)
            self.export_results_button.setEnabled(self.session.loaded)

        worker.signals.succeeded.connect(report_success)
        worker.signals.failed.connect(report_failure)
        worker.signals.finished.connect(finish)
        self.thread_pool.start(worker)

    def _require_loaded(self) -> bool:
        if self.session.loaded:
            return True
        self.statusBar().showMessage("Load an experiment directory first.")
        return False

    def run_autopick_current_spot(self) -> None:
        if not self._require_loaded():
            return
        spot_name = self.current_spot_name()
        if not spot_name:
            return
        try:
            autopick_selection_regions(self.session, spot_name)
        except Exception as exc:
            self._handle_action_error("Auto-pick failed", exc)
            return
        self.pending_region_updates.clear()
        self.set_plot_mode("raw", refresh=False)
        method = str(self.session.import_settings.get("selection_method", "threshold"))
        self.append_log(f"Re-applied {selection_method_label(method)} auto-pick for {spot_name}.")
        self.refresh_view()

    def run_autopick_all_spots(self) -> None:
        """Apply the current selection settings to every imported spot."""
        if not self._require_loaded():
            return
        method = str(
            self.session.import_settings.get(
                "selection_method", "threshold"
            )
        )

        def finish_repick(result: object) -> None:
            summary = result if isinstance(result, dict) else {}
            selected_count = int(summary.get("selected_count", 0))
            failures = summary.get("failures", {})
            failure_count = len(failures) if isinstance(failures, dict) else 0
            self.pending_region_updates.clear()
            self.set_plot_mode("raw", refresh=False)
            message = (
                f"Re-picked {selected_count} spots with "
                f"{selection_method_label(method)}"
            )
            if failure_count:
                message += f"; {failure_count} spots need review"
            message += "."
            self.append_log(message)
            self.statusBar().showMessage(message)
            self.refresh_view()

        self.run_background(
            "Re-picking all spots",
            lambda: autopick_all_selection_regions(self.session),
            finish_repick,
        )

    def apply_current_windows(self) -> None:
        if not self._require_loaded():
            return
        spot_name = self.current_spot_name()
        if not spot_name:
            return
        regions = self.trace_plot.current_regions()
        if not regions:
            self.statusBar().showMessage("Switch to the raw trace view to edit and apply windows.")
            return
        try:
            update_selection_region(
                self.session,
                spot_name,
                baseline=regions.get("baseline"),
                plateau=regions.get("plateau"),
                signal=regions.get("signal"),
            )
        except Exception as exc:
            self._handle_action_error("Selection update failed", exc)
            return
        self.pending_region_updates.clear()
        self.append_log(
            f"Applied baseline, plateau, and signal limits for {spot_name}."
        )
        self.refresh_view()

    def run_baseline_preview(self) -> None:
        if not self._require_loaded():
            return
        def show_result(result: object) -> None:
            baseline_subtracted = result
            self.set_plot_mode("baseline_subtracted", refresh=False)
            self.append_log(
                f"Baseline subtraction ready for "
                f"{len(baseline_subtracted)} sweep rows."
            )
            self.refresh_view()

        self.run_background(
            "Subtracting baselines",
            lambda: ensure_baseline_subtracted(self.session),
            show_result,
        )

    def run_internal_normalization_preview(self) -> None:
        if not self._require_loaded():
            return
        def show_result(result: object) -> None:
            normalized = result
            self.set_plot_mode("normalized_plateau", refresh=False)
            self.append_log(
                f"Internal normalization ready for {len(normalized)} "
                "plateau rows."
            )
            self.refresh_view()

        self.run_background(
            "Applying internal normalization",
            lambda: ensure_normalized_plateau(
                self.session,
                self.workflow_preset,
            ),
            show_result,
        )

    def run_concentration_stage(self) -> None:
        if not self._require_loaded():
            return
        def show_result(result: object) -> None:
            results = result
            self.set_plot_mode("calibration_slopes", refresh=False)
            self.append_log(
                f"Concentration calibration ready with "
                f"{results['calibrant_count']} calibrant rows and "
                f"{results['result_count']} output rows."
            )
            self.refresh_view()

        self.run_background(
            "Calibrating concentrations",
            lambda: ensure_concentration_results(
                self.session,
                self.workflow_preset,
            ),
            show_result,
        )

    def run_raw_ratio_preview(self) -> None:
        if not self._require_loaded():
            return
        def show_result(result: object) -> None:
            ratios = result
            self.set_plot_mode("raw_ratios", refresh=False)
            self.append_log(
                f"Raw isotope ratios ready for {len(ratios)} plateau rows."
            )
            self.refresh_view()

        self.run_background(
            "Building isotope ratios",
            lambda: ensure_raw_ratios(self.session),
            show_result,
        )

    def run_downhole_preview(self) -> None:
        if not self._require_loaded():
            return
        def show_result(result: object) -> None:
            results = result
            self.set_plot_mode("downhole_ratios", refresh=False)
            self.append_log(
                f"Downhole correction ready using "
                f"{results.get('fit_label', 'Pointwise mean')} with "
                f"{self.session.primary_68_standard} and "
                f"{len(results['corrected_ratios'])} plateau rows."
            )
            self.refresh_view()

        self.run_background(
            "Fitting downhole fractionation",
            lambda: ensure_downhole_results(
                self.session,
                self.workflow_preset,
            ),
            show_result,
        )

    def run_session_correction_preview(self) -> None:
        if not self._require_loaded():
            return
        def show_result(result: object) -> None:
            results = result
            self.set_plot_mode("session_ratios", refresh=False)
            self.append_log(
                f"Session correction ready using "
                f"{results.get('fit_label', 'Polynomial')} for "
                f"{len(results['corrected_ratios'])} corrected plateau rows."
            )
            self.refresh_view()

        self.run_background(
            "Fitting session fractionation",
            lambda: ensure_session_corrected(
                self.session,
                self.workflow_preset,
            ),
            show_result,
        )

    def run_final_review(self) -> None:
        if not self._require_loaded():
            return
        def calculate_final():
            spot_means = ensure_spot_means(
                self.session,
                self.workflow_preset,
            )
            final = ensure_finalized_results(
                self.session,
                self.workflow_preset,
            )
            return spot_means, final

        def show_result(result: object) -> None:
            spot_means, final = result
            self.set_plot_mode("final_results", refresh=False)
            final_table = getattr(final, "selected_results", spot_means)
            self.append_log(
                f"Final reduction ready for {len(final_table)} spots, "
                "including parallel common-Pb outputs, automatic flags, "
                "and preferred ages."
            )
            self.refresh_view()

        self.run_background(
            "Finalizing reduction",
            calculate_final,
            show_result,
        )

    def run_depth_profile_review(self) -> None:
        """Calculate and retain the corrected full-signal sweep product."""
        if not self._require_loaded():
            return
        def show_result(result: object) -> None:
            depth_profile = result
            self.set_plot_mode("depth_profile", refresh=False)
            self.append_log(
                f"Depth-profile reduction ready for {len(depth_profile)} "
                "corrected signal sweeps."
            )
            self.refresh_view()

        self.run_background(
            "Calculating depth profiles",
            lambda: ensure_depth_profile(
                self.session,
                self.workflow_preset,
            ),
            show_result,
        )

    def append_log(self, message: str) -> None:
        current = self.log_view.toPlainText().rstrip()
        updated = f"{current}\n{message}" if current else message
        self.log_view.setPlainText(updated + "\n")
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)
