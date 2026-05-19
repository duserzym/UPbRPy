from __future__ import annotations

import json
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.icon import build_app_icon, build_brand_pixmap
from GUI.services import (
    autopick_selection_regions,
    build_channel_colors,
    build_notes,
    clear_reduction_cache,
    current_standard_archive_path,
    default_reference_material_library_path,
    ensure_baseline_subtracted,
    ensure_concentration_results,
    ensure_downhole_results,
    ensure_normalized_plateau,
    ensure_raw_ratios,
    ensure_session_corrected,
    get_selection_regions,
    get_spot_data,
    load_standard_archive_from_path,
    load_session,
    project_summary,
    reference_material_summary,
    selection_quality,
    spot_metrics,
    update_selection_region,
)
from GUI.state import ReductionSession
from GUI.theme import active_theme, apply_theme
from GUI.widgets import MetricCard, SessionOverviewWidget, TracePlotWidget, make_list_item
from GUI.workflows import WORKFLOW_PRESETS, format_import_defaults, get_workflow_preset, infer_workflow_preset


PLOT_MODE_LABELS = {
    "raw": "Raw traces",
    "baseline_subtracted": "Baseline",
    "normalized_plateau": "Normalized",
    "raw_ratios": "Raw ratios",
    "downhole_ratios": "Downhole",
    "session_ratios": "Session",
}

_STAGE_CACHE_KEYS: dict[str, str | None] = {
    "raw": None,
    "baseline_subtracted": "baseline_subtracted_total_data",
    "normalized_plateau": "normalized_plateau_data",
    "raw_ratios": "plateau_data_with_raw_ratios",
    "downhole_ratios": "downhole_results",
    "session_ratios": "session_correction_results",
}

RATIO_COLORS = {
    "206Pb/238U": "#1f63cf",
    "207Pb/206Pb": "#ff8a5b",
    "208Pb/232Th": "#19a188",
}

DEFAULT_REGION_COLORS = {
    "baseline": "#8a4c34",
    "plateau": "#2c5e87",
}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, theme_mode: str = "light", app_icon: QtGui.QIcon | None = None) -> None:
        super().__init__()
        self.settings = QtCore.QSettings("UPbRPy", "Studio")
        self.session = ReductionSession()
        self.channel_colors: dict[str, str] = {}
        self._is_populating = False
        self.pending_region_updates: dict[str, tuple[float, float]] = {}
        self.workflow_preset = get_workflow_preset("zircon")
        self.theme_mode = theme_mode
        self.app_icon = app_icon or build_app_icon()
        self.sigma_level = 2
        self.selected_stats_channel: str | None = None
        self._focused_spot_name: str | None = None
        self._last_plot_mode: str | None = None
        self.reference_library_path = default_reference_material_library_path()
        self.default_reference_library_path = default_reference_material_library_path()
        self._reference_library_notice: str | None = None
        self.selection_colors = self._resolve_selection_colors()

        self.setWindowTitle("UPbRPy Studio")
        self.setWindowIcon(self.app_icon)
        self.resize(1560, 1150)
        self.setMinimumSize(1280, 1000)

        self._build_menu()
        self._build_ui()
        self._initialize_reference_library()
        self._connect_system_theme_updates()
        self.apply_theme_mode(self.theme_mode, persist=False)
        self._show_placeholder_state()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        open_action = file_menu.addAction("Open Experiment Folder…")
        open_action.triggered.connect(self.choose_experiment_directory)

        reload_action = file_menu.addAction("Reload Current Folder")
        reload_action.triggered.connect(self.reload_current_directory)

        file_menu.addSeparator()
        exit_action = file_menu.addAction("Quit")
        exit_action.triggered.connect(self.close)

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

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(central)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(18)

        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main_column(), 1)
        outer.addWidget(self._build_inspector(), 0)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready. Load an experiment directory to begin.")
        self._populate_workflow_ui()

    def _build_sidebar(self) -> QtWidgets.QFrame:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(356)

        frame = QtWidgets.QFrame()
        frame.setObjectName("SidebarPanel")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        brand_row = QtWidgets.QHBoxLayout()
        brand_row.setSpacing(12)
        icon_label = QtWidgets.QLabel()
        icon_label.setPixmap(build_brand_pixmap(56))
        icon_label.setFixedSize(56, 56)
        brand_copy = QtWidgets.QVBoxLayout()
        brand = QtWidgets.QLabel("UPbRPy Studio")
        brand.setObjectName("Brand")
        caption = QtWidgets.QLabel("Iolite-style native workspace for U-Pb, trace elements, and depth-profile review.")
        caption.setWordWrap(True)
        caption.setObjectName("SecondaryText")
        brand_copy.addWidget(brand)
        brand_copy.addWidget(caption)
        brand_row.addWidget(icon_label)
        brand_row.addLayout(brand_copy, 1)
        layout.addLayout(brand_row)

        self.import_button = QtWidgets.QPushButton("Import experiment directory")
        self.import_button.setProperty("accent", True)
        self.import_button.clicked.connect(self.choose_experiment_directory)
        layout.addWidget(self.import_button)

        library_card = QtWidgets.QFrame()
        library_card.setObjectName("SessionCard")
        library_layout = QtWidgets.QVBoxLayout(library_card)
        library_layout.setContentsMargins(16, 16, 16, 16)
        library_layout.setSpacing(8)
        library_label = QtWidgets.QLabel("Reference material library")
        library_label.setObjectName("SectionTitle")
        self.reference_library_label = QtWidgets.QLabel()
        self.reference_library_label.setWordWrap(True)
        self.reference_library_label.setObjectName("SecondaryText")
        self.reference_library_default_label = QtWidgets.QLabel()
        self.reference_library_default_label.setWordWrap(True)
        self.reference_library_default_label.setObjectName("SecondaryText")
        self.reference_library_button = QtWidgets.QPushButton("Import reference material library")
        self.reference_library_button.clicked.connect(self.choose_reference_material_library)
        library_layout.addWidget(library_label)
        library_layout.addWidget(self.reference_library_label)
        library_layout.addWidget(self.reference_library_default_label)
        library_layout.addWidget(self.reference_library_button)
        layout.addWidget(library_card)

        preset_card = QtWidgets.QFrame()
        preset_card.setObjectName("SessionCard")
        preset_layout = QtWidgets.QVBoxLayout(preset_card)
        preset_layout.setContentsMargins(16, 16, 16, 16)
        preset_layout.setSpacing(8)
        preset_label = QtWidgets.QLabel("Mineral workflow")
        preset_label.setObjectName("SectionTitle")
        self.workflow_combo = QtWidgets.QComboBox()
        for key, preset in WORKFLOW_PRESETS.items():
            self.workflow_combo.addItem(preset.title, key)
        self.workflow_combo.currentIndexChanged.connect(self.on_workflow_preset_changed)
        self.workflow_description_label = QtWidgets.QLabel()
        self.workflow_description_label.setWordWrap(True)
        self.workflow_description_label.setObjectName("SecondaryText")
        self.workflow_defaults_label = QtWidgets.QLabel()
        self.workflow_defaults_label.setWordWrap(True)
        self.workflow_defaults_label.setObjectName("SecondaryText")

        preset_layout.addWidget(preset_label)
        preset_layout.addWidget(self.workflow_combo)
        preset_layout.addWidget(self.workflow_description_label)
        preset_layout.addWidget(self.workflow_defaults_label)
        layout.addWidget(preset_card)

        workflow_card = QtWidgets.QFrame()
        workflow_card.setObjectName("WorkflowCard")
        workflow_layout = QtWidgets.QVBoxLayout(workflow_card)
        workflow_layout.setContentsMargins(16, 16, 16, 16)
        workflow_layout.setSpacing(10)
        workflow_label = QtWidgets.QLabel("Processing sequence")
        workflow_label.setObjectName("SectionTitle")
        self.workflow_step_list = QtWidgets.QListWidget()
        self.workflow_step_list.setMinimumHeight(180)
        self.workflow_step_list.setMaximumHeight(220)
        self.workflow_step_list.currentRowChanged.connect(self.on_workflow_step_changed)
        hint_card = QtWidgets.QFrame()
        hint_card.setObjectName("WorkflowHintCard")
        hint_layout = QtWidgets.QVBoxLayout(hint_card)
        hint_layout.setContentsMargins(12, 12, 12, 12)
        hint_layout.setSpacing(6)
        self.workflow_hint_title = QtWidgets.QLabel()
        self.workflow_hint_title.setObjectName("WorkflowLabel")
        self.workflow_hint_title.setWordWrap(True)
        self.workflow_hint_body = QtWidgets.QLabel()
        self.workflow_hint_body.setObjectName("PanelBody")
        self.workflow_hint_body.setWordWrap(True)
        hint_layout.addWidget(self.workflow_hint_title)
        hint_layout.addWidget(self.workflow_hint_body)
        actions_label = QtWidgets.QLabel("Interactive actions")
        actions_label.setObjectName("SectionTitle")
        actions_card = QtWidgets.QFrame()
        actions_card.setObjectName("WorkflowActionsCard")
        self.workflow_actions_layout = QtWidgets.QVBoxLayout(actions_card)
        self.workflow_actions_layout.setContentsMargins(12, 12, 12, 12)
        self.workflow_actions_layout.setSpacing(8)
        self.workflow_status_label = QtWidgets.QLabel()
        self.workflow_status_label.setObjectName("SecondaryText")
        self.workflow_status_label.setWordWrap(True)
        workflow_layout.addWidget(workflow_label)
        workflow_layout.addWidget(self.workflow_step_list)
        workflow_layout.addWidget(hint_card)
        workflow_layout.addWidget(actions_label)
        workflow_layout.addWidget(actions_card)
        workflow_layout.addWidget(self.workflow_status_label)
        layout.addWidget(workflow_card, 1)

        layout.addStretch(1)
        scroll.setWidget(frame)
        return scroll

    def _build_main_column(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        header = QtWidgets.QFrame()
        header.setObjectName("HeaderCard")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(24, 20, 24, 20)
        title_box = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("U-Pb reduction workspace")
        title.setObjectName("HeroTitle")
        subtitle = QtWidgets.QLabel("Notebook-derived zircon workflow, editable selections, and reduction-stage previews guided by iolite-style review steps.")
        subtitle.setObjectName("SecondaryText")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)

        summary_box = QtWidgets.QVBoxLayout()
        summary_box.setSpacing(4)
        workspace_label = QtWidgets.QLabel("Workspace")
        workspace_label.setObjectName("SectionTitle")
        self.project_name_label = QtWidgets.QLabel("No experiment loaded")
        self.project_name_label.setWordWrap(True)
        self.project_name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.project_name_label.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.project_spots_label = QtWidgets.QLabel("Import a Thermo iCAP export directory")
        self.project_spots_label.setObjectName("SecondaryText")
        self.project_spots_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.project_mode_label = QtWidgets.QLabel("Choose a notebook-derived mineral workflow")
        self.project_mode_label.setObjectName("SecondaryText")
        self.project_mode_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.project_engine_label = QtWidgets.QLabel("Shared UPbRPy core")
        self.project_engine_label.setObjectName("SecondaryText")
        self.project_engine_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        summary_box.addWidget(workspace_label, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        summary_box.addWidget(self.project_name_label)
        summary_box.addWidget(self.project_spots_label)
        summary_box.addWidget(self.project_mode_label)
        summary_box.addWidget(self.project_engine_label)

        header_right = QtWidgets.QVBoxLayout()
        header_right.setSpacing(10)
        header_right.addLayout(summary_box)
        theme_row = QtWidgets.QHBoxLayout()
        theme_row.setSpacing(4)
        theme_row.addStretch(1)
        self.theme_mode_buttons: dict[str, QtWidgets.QToolButton] = {}
        self.theme_mode_group = QtWidgets.QButtonGroup(self)
        self.theme_mode_group.setExclusive(True)
        for tmode, tlabel in [("system", "Auto"), ("light", "Light"), ("dark", "Dark")]:
            tbtn = QtWidgets.QToolButton()
            tbtn.setText(tlabel)
            tbtn.setCheckable(True)
            tbtn.setProperty("modeToggle", True)
            tbtn.clicked.connect(lambda _checked=False, selected_tmode=tmode: self.apply_theme_mode(selected_tmode, persist=True))
            self.theme_mode_group.addButton(tbtn)
            self.theme_mode_buttons[tmode] = tbtn
            theme_row.addWidget(tbtn)
        header_right.addLayout(theme_row)
        header_layout.addLayout(header_right)
        layout.addWidget(header)

        metrics = QtWidgets.QHBoxLayout()
        metrics.setSpacing(14)
        self.metric_cards = {
            "Active spot": MetricCard("Active spot", "Preview", "#69e2ff"),
            "Selection span": MetricCard("Selection span", "34.8 s", "#ffba6b"),
            "206Pb/238U": MetricCard("206Pb/238U", "0.09142 ± preview", "#19a188"),
            "Primary signal": MetricCard("Primary signal", "1.95 kcps", "#8ea7ff"),
        }
        for card in self.metric_cards.values():
            metrics.addWidget(card)
        layout.addLayout(metrics)

        plot_card = QtWidgets.QFrame()
        plot_card.setObjectName("PlotCard")
        plot_layout = QtWidgets.QVBoxLayout(plot_card)
        plot_layout.setContentsMargins(12, 12, 12, 12)
        plot_layout.setSpacing(12)
        mode_row = QtWidgets.QHBoxLayout()
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
            button.setMinimumWidth(self.fontMetrics().horizontalAdvance(label) + 32)
            button.clicked.connect(lambda _checked=False, plot_mode=key: self.set_plot_mode(plot_mode, refresh=True))
            self.plot_mode_group.addButton(button)
            self.plot_mode_buttons[key] = button
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        ratio_row_lbl = QtWidgets.QLabel("Ratio")
        ratio_row_lbl.setObjectName("ControlBarLabel")
        mode_row.addWidget(ratio_row_lbl)
        self.ratio_combo = QtWidgets.QComboBox()
        for ratio_name in ["206Pb/238U", "207Pb/206Pb", "208Pb/232Th"]:
            self.ratio_combo.addItem(ratio_name, ratio_name)
        self.ratio_combo.currentIndexChanged.connect(self.on_ratio_changed)
        mode_row.addWidget(self.ratio_combo)
        plot_layout.addLayout(mode_row)

        # ── Plot canvas ────────────────────────────────────────────────────
        self.trace_plot = TracePlotWidget()
        self.trace_plot.set_sigma_level(self.sigma_level)
        self.trace_plot.regionChanged.connect(self.on_plot_region_changed)
        self.trace_plot.xWindowChanged.connect(self.on_trace_x_window_changed)
        plot_layout.addWidget(self.trace_plot)

        plot_layout.addSpacing(0)
        self.session_overview_plot = SessionOverviewWidget()
        self.session_overview_plot.windowChanged.connect(self.on_overview_window_changed)
        plot_layout.addWidget(self.session_overview_plot)

        # ── Control bar below both plots ───────────────────────────────────
        control_bar = QtWidgets.QFrame()
        control_bar.setObjectName("PlotControlBar")
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
        self.reset_scale_button.setStyleSheet("padding: 5px 9px; font-size: 10px;")
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
        self.sigma_toggle_button.clicked.connect(self.on_sigma_toggle_clicked)
        self._update_sigma_button_text()
        cb_layout.addWidget(self.sigma_toggle_button)
        cb_layout.addStretch(1)
        plot_layout.addWidget(control_bar)

        content_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.addWidget(plot_card)

        lower_container = QtWidgets.QWidget()
        lower = QtWidgets.QHBoxLayout()
        lower.setSpacing(18)
        lower.setContentsMargins(0, 0, 0, 0)
        lower_container.setLayout(lower)

        notes_card = QtWidgets.QFrame()
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

        log_card = QtWidgets.QFrame()
        log_card.setObjectName("NotesCard")
        log_layout = QtWidgets.QVBoxLayout(log_card)
        log_layout.setContentsMargins(18, 18, 18, 18)
        log_label = QtWidgets.QLabel("Activity log")
        log_label.setObjectName("SectionTitle")
        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
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
        content_splitter.setSizes([900, 200])
        layout.addWidget(content_splitter, 1)
        return widget

    def _build_inspector(self) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("InspectorPanel")
        frame.setFixedWidth(320)
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        selector_card = QtWidgets.QFrame()
        selector_card.setObjectName("SelectorCard")
        selector_layout = QtWidgets.QVBoxLayout(selector_card)
        selector_layout.setContentsMargins(16, 16, 16, 16)
        selector_layout.setSpacing(10)

        primary_label = QtWidgets.QLabel("Primary channel")
        primary_label.setObjectName("SectionTitle")
        self.primary_channel_combo = QtWidgets.QComboBox()
        self.primary_channel_combo.currentTextChanged.connect(self.on_primary_channel_changed)
        selector_layout.addWidget(primary_label)
        selector_layout.addWidget(self.primary_channel_combo)

        spots_label = QtWidgets.QLabel("Spots")
        spots_label.setObjectName("SectionTitle")
        self.spot_list = QtWidgets.QListWidget()
        self.spot_list.currentTextChanged.connect(self.on_spot_changed)
        selector_layout.addWidget(spots_label)
        selector_layout.addWidget(self.spot_list, 1)

        channels_label = QtWidgets.QLabel("Channels")
        channels_label.setObjectName("SectionTitle")
        self.channel_list = QtWidgets.QListWidget()
        self.channel_list.itemChanged.connect(self.on_channel_toggled)
        self.channel_list.currentItemChanged.connect(self.on_channel_selection_changed)
        self.channel_list.itemDoubleClicked.connect(self.choose_channel_color)
        selector_layout.addWidget(channels_label)
        selector_layout.addWidget(self.channel_list, 1)
        self.channel_color_button = QtWidgets.QPushButton("Set selected channel color")
        self.channel_color_button.clicked.connect(self.choose_channel_color)
        selector_layout.addWidget(self.channel_color_button)
        selection_colors_row = QtWidgets.QHBoxLayout()
        selection_colors_row.setSpacing(8)
        self.baseline_color_button = QtWidgets.QPushButton("Baseline color")
        self.baseline_color_button.clicked.connect(lambda: self.choose_region_color("baseline"))
        self.plateau_color_button = QtWidgets.QPushButton("Plateau color")
        self.plateau_color_button.clicked.connect(lambda: self.choose_region_color("plateau"))
        selection_colors_row.addWidget(self.baseline_color_button)
        selection_colors_row.addWidget(self.plateau_color_button)
        selector_layout.addLayout(selection_colors_row)
        layout.addWidget(selector_card, 1)
        return frame

    def _connect_system_theme_updates(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        try:
            app.styleHints().colorSchemeChanged.connect(self.on_system_color_scheme_changed)
        except AttributeError:
            return

    def _show_placeholder_state(self) -> None:
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

    def choose_reference_material_library(self) -> None:
        start_directory = str(Path(self.reference_library_path).expanduser().parent) if self.reference_library_path else str(Path.home())
        file_path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select reference material library",
            start_directory,
            "CSV Files (*.csv)",
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
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select experiment directory",
            self.session.directory or str(Path.home()),
        )
        if directory:
            self.load_experiment_directory(directory)

    def reload_current_directory(self) -> None:
        if self.session.directory:
            self.load_experiment_directory(self.session.directory, primary_channel=self.session.primary_channel)

    def load_experiment_directory(self, directory: str, primary_channel: str | None = None) -> None:
        inferred_preset = infer_workflow_preset(directory)
        if inferred_preset.key != self.workflow_preset.key:
            self.workflow_preset = inferred_preset
            self._sync_workflow_combo()
            self._populate_workflow_ui()
            self.append_log(f"Inferred {self.workflow_preset.title.lower()} workflow from folder name.")

        chosen_primary = primary_channel or self.primary_channel_combo.currentText() or self.workflow_preset.primary_channel or self.session.primary_channel or "238U"
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.session = load_session(directory, primary_channel=chosen_primary, preset=self.workflow_preset)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Failed to load experiment", str(exc))
            self.append_log(f"Load failed for {directory}: {exc}")
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        self.pending_region_updates.clear()
        self._focused_spot_name = None
        self.channel_colors = self._resolve_channel_colors(self.session.channels)
        self._populate_selectors()
        self._set_default_ratio_for_preset()
        self.set_plot_mode("raw", refresh=False)
        self.refresh_view()
        self.append_log(f"Loaded experiment directory: {directory}")
        materials = reference_material_summary(self.session, self.workflow_preset)
        if materials:
            self.append_log(f"Detected reference materials: {materials}")
        self.statusBar().showMessage(f"Loaded {len(self.session.spot_names)} spots from {Path(directory).name}")

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
        finally:
            self._is_populating = False

    def _populate_workflow_ui(self) -> None:
        self.workflow_description_label.setText(self.workflow_preset.description)
        self.workflow_defaults_label.setText(format_import_defaults(self.workflow_preset))
        self._sync_workflow_combo()
        self._set_default_ratio_for_preset()
        self._is_populating = True
        try:
            self.workflow_step_list.clear()
            for index, step in enumerate(self.workflow_preset.steps, start=1):
                item = QtWidgets.QListWidgetItem(f"{index}. {step.title}")
                item.setToolTip(step.summary)
                self.workflow_step_list.addItem(item)
            if self.workflow_step_list.count() > 0:
                self.workflow_step_list.setCurrentRow(0)
        finally:
            self._is_populating = False
        self._update_workflow_hint(self.workflow_step_list.currentRow())
        self._rebuild_workflow_actions()
        self._refresh_workflow_status()

    def current_workflow_step_title(self) -> str | None:
        row = self.workflow_step_list.currentRow()
        if row < 0 or row >= len(self.workflow_preset.steps):
            return None
        return self.workflow_preset.steps[row].title

    def _update_workflow_hint(self, row: int) -> None:
        if row < 0 or row >= len(self.workflow_preset.steps):
            self.workflow_hint_title.setText("")
            self.workflow_hint_body.setText("")
            return
        step = self.workflow_preset.steps[row]
        self.workflow_hint_title.setText(step.summary)
        self.workflow_hint_body.setText(step.hint)

    def on_workflow_preset_changed(self, index: int) -> None:
        if self._is_populating or index < 0:
            return
        workflow_key = str(self.workflow_combo.itemData(index))
        self.workflow_preset = get_workflow_preset(workflow_key)
        self._populate_workflow_ui()
        self.notes_body.setText(self._compose_notes(self.session.selected_spot))
        self.statusBar().showMessage(f"Selected {self.workflow_preset.title.lower()} workflow preset.")
        if self.session.loaded and self.session.directory:
            self.session.workflow_key = self.workflow_preset.key
            self.session.workflow_title = self.workflow_preset.title
            self.session.reduction_cache.clear()
            self.append_log(f"Workflow preset changed to {self.workflow_preset.title}; reload to apply notebook defaults.")
            self.refresh_view()

    def on_workflow_step_changed(self, row: int) -> None:
        if self._is_populating:
            return
        self._update_workflow_hint(row)
        self._rebuild_workflow_actions()
        if self.session.loaded and self._workflow_step_allows_region_edits():
            self.set_plot_mode("raw", refresh=False)
        self.refresh_view()

    def _populate_selectors(self) -> None:
        self._is_populating = True
        try:
            self.primary_channel_combo.clear()
            self.primary_channel_combo.addItems(self.session.channels)
            primary_index = self.primary_channel_combo.findText(self.session.primary_channel)
            if primary_index >= 0:
                self.primary_channel_combo.setCurrentIndex(primary_index)

            self.spot_list.clear()
            self.spot_list.addItems(self.session.spot_names)
            if self.session.selected_spot:
                matches = self.spot_list.findItems(self.session.selected_spot, QtCore.Qt.MatchFlag.MatchExactly)
                if matches:
                    self.spot_list.setCurrentItem(matches[0])

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
        channels: list[str] = []
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                channels.append(item.text())
        return channels

    def current_spot_name(self) -> str | None:
        current_item = self.spot_list.currentItem()
        if current_item is not None:
            return current_item.text()
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

    def _resolve_channel_colors(self, channels: list[str]) -> dict[str, str]:
        colors = build_channel_colors(channels)
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

    def set_interaction_mode(self, mode: str) -> None:
        self.trace_plot.set_interaction_mode(mode)
        for key, button in self.interact_mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == self.trace_plot.interaction_mode())
            button.blockSignals(False)

    def set_zoom_mode(self, mode: str) -> None:
        self.trace_plot.set_zoom_mode(mode)
        for key, button in self.zoom_mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == self.trace_plot.zoom_mode())
            button.blockSignals(False)

    def restore_default_main_view(self) -> None:
        if not self.session.loaded:
            self.trace_plot.show_preview(self.selection_colors)
            self.session_overview_plot.show_preview()
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
        self.ratio_combo.setEnabled(self.session.plot_mode in {"raw_ratios", "downhole_ratios", "session_ratios"})

    def show_raw_traces(self) -> None:
        self.set_plot_mode("raw", refresh=True)

    def refresh_view(self) -> None:
        self.trace_plot.apply_theme(active_theme(QtWidgets.QApplication.instance()))
        if not self.session.loaded:
            self.trace_plot.show_preview(self.selection_colors)
            self.session_overview_plot.show_preview()
            self._apply_metrics(spot_metrics(self.session, None))
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
        should_focus_spot = bool(selected_spot) and (not same_spot or not same_mode)
        preserve_main_view = same_spot and same_mode

        visible_channels = self.checked_channels()
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
        self._apply_metrics(spot_metrics(self.session, selected_spot))
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
        self.trace_plot.set_x_window(bounds)
        if self.trace_plot.zoom_mode() == "xy":
            self.trace_plot.fit_y_to_visible_data()

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
        if self.session.plot_mode == "raw":
            self.trace_plot.show_spot(
                self.session.total_data,
                visible_channels,
                self.channel_colors,
                regions,
                editable_regions=editable_regions,
                stats_channel=self.current_stats_channel(),
                region_colors=self.selection_colors,
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
            )
            return

        if self.session.plot_mode == "normalized_plateau":
            normalized = ensure_normalized_plateau(self.session, self.workflow_preset)
            channels = [channel for channel in visible_channels if channel in normalized.columns]
            self.trace_plot.show_series(normalized, channels, self.channel_colors, y_label="Normalized plateau cps", line_style="scatter")
            return

        ratio_name = self.current_ratio_name()
        if self.session.plot_mode == "raw_ratios":
            ratios = ensure_raw_ratios(self.session)
            self.trace_plot.show_series(ratios, [ratio_name], {ratio_name: RATIO_COLORS.get(ratio_name, "#1f63cf")}, y_label=ratio_name, line_style="scatter")
            return

        if self.session.plot_mode == "downhole_ratios":
            downhole = ensure_downhole_results(self.session, self.workflow_preset)["corrected_ratios"]
            corrected_name = f"{ratio_name}_DH_corrected"
            colors = {
                ratio_name: "#8a97a8",
                corrected_name: RATIO_COLORS.get(ratio_name, "#1f63cf"),
            }
            self.trace_plot.show_series(downhole, [ratio_name, corrected_name], colors, y_label=ratio_name, line_style="scatter")
            return

        if self.session.plot_mode == "session_ratios":
            session_corrected = ensure_session_corrected(self.session, self.workflow_preset)["corrected_ratios"]
            downhole_name = f"{ratio_name}_DH_corrected"
            session_name = f"{ratio_name}_fractionation_corrected"
            colors = {
                downhole_name: "#8a97a8",
                session_name: RATIO_COLORS.get(ratio_name, "#1f63cf"),
            }
            self.trace_plot.show_series(session_corrected, [downhole_name, session_name], colors, y_label=ratio_name, line_style="scatter")
            return

        self.trace_plot.show_spot(
            spot_data,
            visible_channels,
            self.channel_colors,
            regions,
            editable_regions=editable_regions,
            stats_channel=self.current_stats_channel(),
            region_colors=self.selection_colors,
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
        self.project_mode_label.setText(summary["mode"])
        self.project_engine_label.setText(summary["engine"])

    def _apply_metrics(self, metrics: dict[str, str]) -> None:
        for label, value in metrics.items():
            if label in self.metric_cards:
                self.metric_cards[label].set_value(value)

    def _rebuild_workflow_actions(self) -> None:
        while self.workflow_actions_layout.count():
            item = self.workflow_actions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, (label, handler, accent) in enumerate(self._workflow_action_specs()):
            button = QtWidgets.QPushButton(label)
            if accent:
                button.setProperty("accent", True)
            button.clicked.connect(handler)
            self.workflow_actions_layout.addWidget(button)
            if index >= 2:
                break

    def _workflow_action_specs(self) -> list[tuple[str, object, bool]]:
        if not self.session.loaded:
            return [("Import experiment directory", self.choose_experiment_directory, True)]

        title = (self.current_workflow_step_title() or "").lower()
        if "import" in title:
            return [
                ("Auto-pick current spot", self.run_autopick_current_spot, True),
                ("Reload session defaults", self.reload_current_directory, False),
            ]
        if "review baseline" in title:
            return [
                ("Apply edited windows", self.apply_current_windows, True),
                ("Reset current spot", self.run_autopick_current_spot, False),
                ("Return to raw traces", self.show_raw_traces, False),
            ]
        if "subtract baseline" in title:
            return [
                ("Preview baseline-subtracted traces", self.run_baseline_preview, True),
                ("Return to raw traces", self.show_raw_traces, False),
            ]
        if "internal normalization" in title:
            return [
                ("Run internal normalization preview", self.run_internal_normalization_preview, True),
                ("Return to raw traces", self.show_raw_traces, False),
            ]
        if "calibrate" in title or "concentration" in title or "thermometry" in title:
            return [
                ("Run concentration stage", self.run_concentration_stage, True),
                ("Show normalized plateau", self.run_internal_normalization_preview, False),
            ]
        if "raw isotope" in title or "raw ratio" in title:
            return [
                ("Run raw ratio preview", self.run_raw_ratio_preview, True),
                ("Return to raw traces", self.show_raw_traces, False),
            ]
        if "downhole" in title:
            return [
                ("Run downhole correction preview", self.run_downhole_preview, True),
                ("Show raw ratios", self.run_raw_ratio_preview, False),
            ]
        if "session fractionation" in title or "session-wide" in title:
            return [
                ("Run session correction preview", self.run_session_correction_preview, True),
                ("Show downhole ratios", self.run_downhole_preview, False),
            ]
        return [
            ("Show final corrected ratios", self.run_final_review, True),
            ("Return to raw traces", self.show_raw_traces, False),
        ]

    def _workflow_step_allows_region_edits(self) -> bool:
        title = (self.current_workflow_step_title() or "").lower()
        return "import" in title or "review baseline" in title

    def _refresh_workflow_status(self) -> None:
        if not self.session.loaded:
            self.workflow_status_label.setText("Load a session to activate workflow actions and selection checks.")
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
        self.workflow_status_label.setText(" | ".join(parts))

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
        self.append_log(f"Re-applied notebook auto-pick for {spot_name}.")
        self.refresh_view()

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
        update_selection_region(
            self.session,
            spot_name,
            baseline=regions.get("baseline"),
            plateau=regions.get("plateau"),
        )
        self.pending_region_updates.clear()
        self.append_log(f"Applied edited baseline/plateau windows for {spot_name}.")
        self.refresh_view()

    def run_baseline_preview(self) -> None:
        if not self._require_loaded():
            return
        try:
            baseline_subtracted = ensure_baseline_subtracted(self.session)
        except Exception as exc:
            self._handle_action_error("Baseline preview failed", exc)
            return
        self.set_plot_mode("baseline_subtracted", refresh=False)
        self.append_log(f"Baseline subtraction ready for {len(baseline_subtracted)} sweep rows.")
        self.refresh_view()

    def run_internal_normalization_preview(self) -> None:
        if not self._require_loaded():
            return
        try:
            normalized = ensure_normalized_plateau(self.session, self.workflow_preset)
        except Exception as exc:
            self._handle_action_error("Internal normalization failed", exc)
            return
        self.set_plot_mode("normalized_plateau", refresh=False)
        self.append_log(f"Internal normalization ready for {len(normalized)} plateau rows.")
        self.refresh_view()

    def run_concentration_stage(self) -> None:
        if not self._require_loaded():
            return
        try:
            results = ensure_concentration_results(self.session, self.workflow_preset)
        except Exception as exc:
            self._handle_action_error("Concentration stage failed", exc)
            return
        self.set_plot_mode("normalized_plateau", refresh=False)
        self.append_log(
            f"Concentration calibration ready with {results['calibrant_count']} calibrant rows and {results['result_count']} output rows."
        )
        self.refresh_view()

    def run_raw_ratio_preview(self) -> None:
        if not self._require_loaded():
            return
        try:
            ratios = ensure_raw_ratios(self.session)
        except Exception as exc:
            self._handle_action_error("Raw ratio preview failed", exc)
            return
        self.set_plot_mode("raw_ratios", refresh=False)
        self.append_log(f"Raw isotope ratios ready for {len(ratios)} plateau rows.")
        self.refresh_view()

    def run_downhole_preview(self) -> None:
        if not self._require_loaded():
            return
        try:
            results = ensure_downhole_results(self.session, self.workflow_preset)
        except Exception as exc:
            self._handle_action_error("Downhole correction failed", exc)
            return
        self.set_plot_mode("downhole_ratios", refresh=False)
        self.append_log(
            f"Downhole correction ready using {self.workflow_preset.primary_standard} and {len(results['corrected_ratios'])} plateau rows."
        )
        self.refresh_view()

    def run_session_correction_preview(self) -> None:
        if not self._require_loaded():
            return
        try:
            results = ensure_session_corrected(self.session, self.workflow_preset)
        except Exception as exc:
            self._handle_action_error("Session correction failed", exc)
            return
        self.set_plot_mode("session_ratios", refresh=False)
        self.append_log(f"Session correction ready for {len(results['corrected_ratios'])} corrected plateau rows.")
        self.refresh_view()

    def run_final_review(self) -> None:
        if not self._require_loaded():
            return
        self.run_session_correction_preview()
        self.append_log("Final corrected ratio preview ready for concordia and export review.")

    def append_log(self, message: str) -> None:
        current = self.log_view.toPlainText().rstrip()
        updated = f"{current}\n{message}" if current else message
        self.log_view.setPlainText(updated + "\n")
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)
