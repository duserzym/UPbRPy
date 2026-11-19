"""Publication export choices and a preview over one prepared engine table."""
from __future__ import annotations

from dataclasses import replace
from html import escape
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.flow_layout import FlowLayout
from GUI.theme import active_theme
from UPbRPy.export_citations import group_info, reference
from UPbRPy.export_table import (
    ExportSettings, ExportTable, export_column_preset, export_table_preview,
    update_export_settings,
)
from UPbRPy.thermometry import THERMOMETERS


THERMOMETRY_FIELDS = ("a_sio2", "a_tio2", "pressure_gpa")


def _reference_html(keys):
    paragraphs = []
    for key in keys:
        ref = reference(key)
        text = escape(ref.full())
        if ref.doi:
            doi = escape(ref.doi, quote=True)
            text = text.replace(f"doi:{doi}", f'<a href="https://doi.org/{doi}">doi:{doi}</a>')
        if ref.note:
            text += "<br>" + escape(ref.note)
        if not ref.verified:
            text += "<br><b>unverified</b>"
        paragraphs.append(f"<p>{text}</p>")
    return "".join(paragraphs)


def citation_html(group: str) -> str:
    """Return linked registry references, purpose and caveat for a column group."""
    info = group_info(group)
    return (_reference_html(info.references)
            + f"<p><b>Used for:</b> {escape(info.used_for)}</p>"
            + f"<p><b>Caveat:</b> {escape(info.caveat)}</p>")


def _plain_text(html):
    document = QtGui.QTextDocument()
    document.setHtml(html)
    return document.toPlainText()


def _muted_label(text):
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {active_theme().muted}; font-size: 11px;")
    label.setMinimumWidth(0)
    return label


def _column_label(column):
    return column.label + (f" ({column.unit})" if column.unit else "")


class _FormatCheckBox(QtWidgets.QCheckBox):
    """Keep the existing format text API while displaying a compact caption."""

    def __init__(self, text, caption):
        super().__init__(text)
        self.caption = caption

    def sizeHint(self):
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.caption
        contents = self.fontMetrics().size(QtCore.Qt.TextFlag.TextShowMnemonic, self.caption)
        return self.style().sizeFromContents(QtWidgets.QStyle.ContentsType.CT_CheckBox, option, contents, self)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.caption
        painter = QtWidgets.QStylePainter(self)
        painter.drawControl(QtWidgets.QStyle.ControlElement.CE_CheckBox, option)


class _ColumnChip(QtWidgets.QToolButton):
    """Keep long column labels readable inside a narrow group card."""

    def __init__(self, *, optional=False, preview=False):
        super().__init__()
        policy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                                      QtWidgets.QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        theme = active_theme()
        self.setStyleSheet(
            f"QToolButton {{ font-size: 11px; font-weight: normal; padding: 2px 7px; "
            f"border: 1px {'dotted' if optional else 'solid'} {theme.input_border}; "
            f"border-radius: 7px; background: {theme.accent_soft if preview else theme.surface}; }}"
            f"QToolButton:checked {{ color: {theme.accent}; background: {theme.accent_soft}; "
            f"border-color: {theme.accent}; }}")
        self.preview = preview
        if preview:
            self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        if width >= self.sizeHint().width():
            return self.sizeHint().height()
        return max(22, self.fontMetrics().boundingRect(
            QtCore.QRect(0, 0, max(1, width - 16), 1000),
            QtCore.Qt.TextFlag.TextWrapAnywhere, self.text()).height() + 6)

    def sizeHint(self):
        # Leave room for fractional glyph advances so a fitting label stays on one line.
        width = self.fontMetrics().horizontalAdvance(self.text()) + 18
        return QtCore.QSize(width, max(22, self.fontMetrics().height() + 6))

    def minimumSizeHint(self):
        return QtCore.QSize(0, 22)

    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QtWidgets.QStylePainter(self)
        painter.drawComplexControl(QtWidgets.QStyle.ComplexControl.CC_ToolButton, option)
        painter.setPen(QtGui.QColor(active_theme().accent if self.preview or self.isChecked() else active_theme().text))
        flags = (QtCore.Qt.TextFlag.TextSingleLine if self.width() >= self.sizeHint().width()
                 else QtCore.Qt.TextFlag.TextWrapAnywhere)
        painter.drawText(self.rect().adjusted(8, 3, -8, -3),
                         QtCore.Qt.AlignmentFlag.AlignCenter | flags, self.text())


class _ElidedLabel(QtWidgets.QLabel):
    """Paint one line while retaining the full text for citations."""

    def __init__(self, text=""):
        super().__init__(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
        painter.drawText(self.contentsRect(), QtCore.Qt.AlignmentFlag.AlignVCenter,
                         self.fontMetrics().elidedText(self.text(), QtCore.Qt.TextElideMode.ElideRight,
                                                       self.contentsRect().width()))


class _GroupCheckBox(QtWidgets.QCheckBox):
    """Elide a card title and toggle partial groups directly to all selected."""

    def __init__(self, text):
        super().__init__(text)
        self.setTristate(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setToolTip(f"{text}\nSelect all or none in this group")

    def nextCheckState(self):
        self.setCheckState(QtCore.Qt.CheckState.Unchecked if self.checkState() == QtCore.Qt.CheckState.Checked
                           else QtCore.Qt.CheckState.Checked)

    def minimumSizeHint(self):
        return QtCore.QSize(0, super().sizeHint().height())

    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        text_rect = self.style().subElementRect(QtWidgets.QStyle.SubElement.SE_CheckBoxContents, option, self)
        option.text = self.fontMetrics().elidedText(self.text(), QtCore.Qt.TextElideMode.ElideRight,
                                                   text_rect.width())
        painter = QtWidgets.QStylePainter(self)
        painter.drawControl(QtWidgets.QStyle.ControlElement.CE_CheckBox, option)

    def hitButton(self, pos):
        return self.rect().contains(pos)


class _OutputRow(QtWidgets.QWidget):
    """Move the format chips below the destination when the row is too narrow."""

    def __init__(self, folder, browse, stem, formats):
        super().__init__()
        self.formats = formats
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.addWidget(QtWidgets.QLabel("Folder"), 0, 0)
        self.grid.addWidget(folder, 0, 1)
        self.grid.addWidget(browse, 0, 2)
        self.grid.addWidget(QtWidgets.QLabel("File stem"), 0, 3)
        self.grid.addWidget(stem, 0, 4)
        self.grid.setColumnStretch(1, 1)
        self._wrapped = None
        self._place_formats(True)

    def _place_formats(self, wrapped):
        if self._wrapped == wrapped:
            return
        self._wrapped = wrapped
        self.grid.removeWidget(self.formats)
        self.grid.addWidget(self.formats, 1 if wrapped else 0, 0 if wrapped else 5,
                            1, 6 if wrapped else 1)

    def resizeEvent(self, event):
        self._place_formats(self.width() < 690 + self.formats.sizeHint().width())
        super().resizeEvent(event)


class _GroupCard(QtWidgets.QFrame):
    """A group header and wrapping column choices owned by the export dialog."""

    def __init__(self, group, columns):
        super().__init__()
        self.setObjectName("exportGroupCard")
        theme = active_theme()
        self.setStyleSheet(f"QFrame#exportGroupCard {{ background: {theme.surface}; "
                           f"border: 1px solid {theme.input_border}; border-radius: 12px; }}")
        self.setMinimumWidth(0)
        self.columns = columns
        self.chips = []
        self.layout = QtWidgets.QVBoxLayout(self)
        self.layout.setContentsMargins(8, 7, 8, 7)
        self.layout.setSpacing(4)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(4)
        info = group_info(group)
        self.expand_button = None
        if info.section == "core" or len(columns) > 12:
            self.expand_button = QtWidgets.QToolButton()
            self.expand_button.setText("▸")
            self.expand_button.setToolTip("Show columns")
            self.expand_button.setAccessibleName(f"Show columns in {info.title}")
            self.expand_button.setCheckable(True)
            self.expand_button.setAutoRaise(True)
            self.expand_button.setFixedSize(18, 18)
            self.expand_button.setStyleSheet("QToolButton { border: none; padding: 0; }")
            self.expand_button.toggled.connect(self._expand)
            header.addWidget(self.expand_button)
        self.checkbox = _GroupCheckBox(group if info.section == "core" else info.title)
        header.addWidget(self.checkbox, 1)
        self.count_label = _muted_label("")
        self.count_label.setWordWrap(False)
        header.addWidget(self.count_label)
        self.info_button = QtWidgets.QToolButton()
        self.info_button.setText("i")
        self.info_button.setAccessibleName(f"About {info.title}")
        self.info_button.setFixedSize(18, 18)
        self.info_button.setStyleSheet(f"QToolButton {{ border: 1px solid {theme.input_border}; border-radius: 9px; padding: 0; }}")
        header.addWidget(self.info_button)
        self.layout.addLayout(header)
        self.citation_row = QtWidgets.QWidget()
        citation_layout = QtWidgets.QHBoxLayout(self.citation_row)
        citation_layout.setContentsMargins(0, 0, 0, 0)
        citation_layout.setSpacing(5)
        self.citation_label = _ElidedLabel()
        self.citation_label.setStyleSheet(f"color: {theme.accent}; font-size: 11px;")
        citation_layout.addWidget(self.citation_label, 1)
        self.unverified_tag = QtWidgets.QLabel("unverified")
        self.unverified_tag.setStyleSheet("color: #a5680b; font-size: 10px;")
        citation_layout.addWidget(self.unverified_tag)
        self.layout.addWidget(self.citation_row)
        self.body = QtWidgets.QWidget()
        flow = FlowLayout(self.body)
        for column in columns:
            chip = _ColumnChip(optional=column.optional)
            chip.setText(_column_label(column))
            chip.setCheckable(True)
            chip.setProperty("optional", column.optional)
            chip.setToolTip("Optional; off in the Publication preset" if column.optional else column.key)
            self.chips.append(chip)
            flow.addWidget(chip)
        if self.expand_button:
            self.body.hide()
        self.layout.addWidget(self.body)

    def _expand(self, expanded):
        self.body.setVisible(expanded)
        self.expand_button.setText("▾" if expanded else "▸")
        self.expand_button.setToolTip("Hide columns" if expanded else "Show columns")


class ExportResultsDialog(QtWidgets.QDialog):
    """Edit export settings without running reduction stages on the UI thread."""

    def __init__(self, table: ExportTable, parent=None, *, folder: str | Path = "",
                 file_stem: str = "", excluded_spots=()):
        super().__init__(parent)
        self.setWindowTitle("Export results")
        self.base_table = table
        self.table = table
        self.excluded_spots = tuple(excluded_spots)
        self._applying = False
        self._thermometry_values = {key: getattr(table.settings, key) for key in THERMOMETRY_FIELDS}
        self._calibration_key = table.settings.rutile_calibration
        self.thermometer = THERMOMETERS.get(table.context.get("material"))
        self.numeric_fields = {}
        self.column_items = {}
        self.group_items = {}
        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(160)
        self.preview_timer.timeout.connect(self.refresh_preview)
        self.filter_timer = QtCore.QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(120)
        self.filter_timer.timeout.connect(self._filter_columns)
        layout = QtWidgets.QVBoxLayout(self)
        self.folder_edit = QtWidgets.QLineEdit(str(folder))
        self.folder_edit.setMinimumWidth(80)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self.choose_folder)
        self.stem_edit = QtWidgets.QLineEdit(file_stem or table.context.get("session_name", "session") + "_results_table")
        self.stem_edit.setFixedWidth(220)
        formats = QtWidgets.QWidget()
        format_layout = QtWidgets.QHBoxLayout(formats)
        format_layout.setContentsMargins(0, 0, 0, 0)
        format_layout.setSpacing(5)
        self.format_checks = {}
        theme = active_theme()
        for fmt in ("xlsx", "csv", "md", "si", "audit"):
            caption = {"si": "SI workbook", "audit": "Audit files"}.get(fmt, "." + fmt)
            check = _FormatCheckBox("SI workbook (.xlsx)" if fmt == "si" else caption, caption)
            check.setChecked(fmt != "si")
            check.setStyleSheet(
                f"QCheckBox {{ padding: 4px 7px; border: 1px solid {theme.input_border}; border-radius: 9px; }}"
                f"QCheckBox:checked {{ background: {theme.accent_soft}; color: {theme.accent}; }}")
            if fmt == "audit":
                self.audit_check = check
                check.setToolTip("Audit files and replay notebook")
            else:
                self.format_checks[fmt] = check
            format_layout.addWidget(check)
        format_layout.addStretch()
        layout.addWidget(_OutputRow(self.folder_edit, browse, self.stem_edit, formats))
        tool_row = QtWidgets.QHBoxLayout()
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter columns or citations")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMinimumWidth(100)
        self.filter_edit.textChanged.connect(lambda: self.filter_timer.start())
        tool_row.addWidget(self.filter_edit, 1)
        tool_row.addWidget(QtWidgets.QLabel("Preset"))
        self.preset_group = QtWidgets.QButtonGroup(self)
        self.preset_group.setExclusive(True)
        self.preset_buttons = {}
        for name in ("publication", "everything", "minimal", "custom"):
            button = QtWidgets.QToolButton()
            button.setText(name.title())
            button.setCheckable(True)
            button.setEnabled(name != "custom")
            button.setStyleSheet(f"QToolButton {{ padding: 5px 8px; border: 1px solid {theme.input_border}; }}"
                                f"QToolButton:checked {{ background: {theme.accent_soft}; color: {theme.accent}; }}")
            self.preset_group.addButton(button)
            self.preset_buttons[name] = button
            if name != "custom":
                button.clicked.connect(lambda checked=False, name=name: self._apply_preset(name))
            tool_row.addWidget(button)
        excluded_count = int(table.rows["excluded"].sum()) if "excluded" in table.rows else len(self.excluded_spots)
        self.exclusion_label = _muted_label(f"{excluded_count} analyses excluded in Final review")
        self.exclusion_label.setToolTip(f"{excluded_count} analyses excluded in Final review (retained in the table; omitted from summaries).")
        tool_row.addWidget(self.exclusion_label)
        layout.addLayout(tool_row)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        self.left_scroll = QtWidgets.QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left_scroll.setMinimumWidth(200)
        left_page = QtWidgets.QWidget()
        self.left_layout = QtWidgets.QVBoxLayout(left_page)
        self.left_layout.setContentsMargins(4, 4, 4, 4)
        self.sections = {}
        self._core_grid_columns = 3
        applicable = [col for col in table.columns if col.materials is None or table.context.get("material") in col.materials]
        for section, title in (("core", "Core table"), ("trace", "Trace elements"),
                               ("thermometry", f"Thermometry · {table.context.get('material')} session"),
                               ("literature", "Literature indices")):
            groups = list(dict.fromkeys(col.group for col in applicable if group_info(col.group).section == section))
            if section == "thermometry":
                groups = [self.thermometer.group] if self.thermometer else []
            if not groups:
                continue
            section_widget = QtWidgets.QWidget()
            section_layout = QtWidgets.QVBoxLayout(section_widget)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.addWidget(_muted_label(title.upper()))
            grid = QtWidgets.QGridLayout()
            grid_columns = 3 if section == "core" else 2
            for column in range(grid_columns):
                grid.setColumnStretch(column, 1)
            section_layout.addLayout(grid)
            self.sections[section] = (section_widget, grid, groups)
            for index, group in enumerate(groups):
                columns = [col for col in applicable if col.group == group]
                card = _GroupCard(group, columns)
                self.group_items[group] = card
                for col, chip in zip(columns, card.chips):
                    self.column_items[col.key] = chip
                    chip.toggled.connect(lambda checked=False, group=group: self._columns_changed(group))
                card.checkbox.checkStateChanged.connect(lambda state, group=group: self._group_changed(group, state))
                card.info_button.clicked.connect(lambda checked=False, group=group: self._show_citation(group))
                if section == "thermometry":
                    self._build_thermometry(card)
                self._update_citation(group)
                grid.addWidget(card, index // grid_columns if section != "thermometry" else 0,
                               index % grid_columns if section != "thermometry" else 0,
                               1, grid_columns if section == "thermometry" else 1,
                               alignment=QtCore.Qt.AlignmentFlag.AlignTop)
            self.left_layout.addWidget(section_widget)
            self._layout_cards(section, groups)
        self.left_layout.addStretch()
        self.left_scroll.setWidget(left_page)
        self.splitter.addWidget(self.left_scroll)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setMinimumWidth(240)
        self.splitter.addWidget(self.tabs)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 4)
        self.splitter.setSizes([620, 490])
        self._build_preview_tabs()
        self._build_settings_tab()
        bottom = QtWidgets.QHBoxLayout()
        self.defaults_button = QtWidgets.QPushButton("Defaults")
        self.defaults_button.setToolTip("Restore the Publication preset; laboratory metadata is kept")
        self.defaults_button.clicked.connect(self.restore_defaults)
        bottom.addWidget(self.defaults_button)
        bottom.addStretch()
        self.error_label = QtWidgets.QLabel()
        self.error_label.setWordWrap(True)
        bottom.addWidget(self.error_label)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        self.export_button = QtWidgets.QPushButton("Export")
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self.accept)
        bottom.addWidget(self.export_button)
        layout.addLayout(bottom)
        self._set_settings(table.settings)
        self.discordance_combo.currentTextChanged.connect(self.schedule_preview)
        for edit in self.text_fields.values():
            edit.textChanged.connect(self.schedule_preview)
        self.csv_precision_check.toggled.connect(self.schedule_preview)
        self.refresh_preview()
        self.left_scroll.viewport().installEventFilter(self)
        screen = self.screen().availableGeometry()
        self.resize(min(1180, screen.width() - 40), min(760, screen.height() - 80))

    def eventFilter(self, watched, event):
        if watched is self.left_scroll.viewport() and event.type() == QtCore.QEvent.Type.Resize:
            core = self.sections.get("core")
            if core:
                _, grid, groups = core
                # Include the checkbox indicator, count, buttons, and card margins.
                card_width = max(
                    card.layout.itemAt(0).sizeHint().width()
                    + card.layout.contentsMargins().left() + card.layout.contentsMargins().right()
                    for card in (self.group_items[group] for group in groups))
                margins = self.left_layout.contentsMargins()
                available = watched.width() - margins.left() - margins.right()
                columns = 3 if available >= max(560, 3 * card_width + 2 * grid.horizontalSpacing()) else 2
                if columns != self._core_grid_columns:
                    self._core_grid_columns = columns
                    self._layout_cards("core", [group for group in groups
                                               if not self.group_items[group].isHidden()])
        return super().eventFilter(watched, event)

    def _layout_cards(self, section, shown):
        _, grid, groups = self.sections[section]
        columns = self._core_grid_columns if section == "core" else 2
        for group in groups:
            grid.removeWidget(self.group_items[group])
        for column in range(3 if section == "core" else 2):
            grid.setColumnStretch(column, int(column < columns))
        for index, group in enumerate(shown):
            span = columns if section == "thermometry" else 1
            if section == "literature" and index == len(shown) - 1 and index % columns == 0:
                span = columns
            grid.addWidget(self.group_items[group], index // columns, index % columns,
                           1, span, alignment=QtCore.Qt.AlignmentFlag.AlignTop)

    def _build_preview_tabs(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        self.exported_columns_label = QtWidgets.QLabel()
        self.exported_columns_label.setWordWrap(True)
        layout.addWidget(self.exported_columns_label)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.exported_columns_page = QtWidgets.QWidget()
        self.exported_columns_layout = QtWidgets.QVBoxLayout(self.exported_columns_page)
        scroll.setWidget(self.exported_columns_page)
        splitter.addWidget(scroll)
        notes = QtWidgets.QWidget()
        notes_layout = QtWidgets.QVBoxLayout(notes)
        notes_layout.setContentsMargins(0, 0, 0, 0)
        notes_layout.addWidget(QtWidgets.QLabel("Footnotes"))
        self.footnotes_edit = QtWidgets.QPlainTextEdit()
        self.footnotes_edit.setReadOnly(True)
        notes_layout.addWidget(self.footnotes_edit)
        splitter.addWidget(notes)
        splitter.setSizes([280, 220])
        layout.addWidget(splitter)
        self.tabs.addTab(page, "Columns && footnotes")
        self.preview_table = QtWidgets.QTableWidget()
        self.preview_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.horizontalHeader().hide()
        self.preview_table.verticalHeader().hide()
        self.tabs.addTab(self.preview_table, "Table")

    def _build_settings_tab(self):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.discordance_combo = QtWidgets.QComboBox()
        self.discordance_combo.addItems(["7/5-6/8", "7/6-6/8"])
        form.addRow("Discordance definition", self.discordance_combo)
        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(1)
        spin.setRange(0, 1000)
        spin.setSingleStep(.1)
        spin.valueChanged.connect(self.schedule_preview)
        self.numeric_fields["discordance_threshold_percent"] = spin
        form.addRow("Discordance threshold (%)", spin)
        self.csv_precision_check = QtWidgets.QCheckBox("CSV full precision")
        form.addRow(self.csv_precision_check)
        self.text_fields = {}
        for key, label in (("laboratory", "Laboratory"), ("laser_system", "Laser system"),
                           ("spot_diameter_um", "Spot diameter (µm)"), ("repetition_rate_hz", "Repetition rate (Hz)"),
                           ("fluence_j_cm2", "Fluence (J/cm²)"), ("operator", "Operator override"), ("table_title", "Table title")):
            edit = QtWidgets.QLineEdit()
            self.text_fields[key] = edit
            form.addRow(label, edit)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, "Settings")

    def _build_thermometry(self, card):
        calibration_row = QtWidgets.QHBoxLayout()
        calibration_row.addWidget(QtWidgets.QLabel("Calibration"))
        if len(self.thermometer.calibrations) > 1:
            self.calibration_combo = QtWidgets.QComboBox()
            for calibration in self.thermometer.calibrations:
                self.calibration_combo.addItem(reference(calibration.reference).short, calibration.key)
            self.calibration_combo.setCurrentIndex(self.calibration_combo.findData(self._calibration_key))
            self.calibration_combo.currentIndexChanged.connect(self._calibration_changed)
            calibration_row.addWidget(self.calibration_combo, 1)
        else:
            label = _ElidedLabel(reference(self.thermometer.calibrations[0].reference).short)
            label.setStyleSheet(f"color: {active_theme().muted}; font-size: 11px;")
            label.setToolTip(label.text())
            calibration_row.addWidget(label, 1)
        card.layout.addLayout(calibration_row)
        self.thermometry_inputs = QtWidgets.QWidget()
        self.thermometry_form = QtWidgets.QFormLayout(self.thermometry_inputs)
        self.thermometry_form.setContentsMargins(0, 0, 0, 0)
        self.thermometry_form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        card.layout.addWidget(self.thermometry_inputs)
        self._rebuild_thermometry_inputs()

    def _active_calibration(self):
        if self.thermometer.settings_field:
            return next(c for c in self.thermometer.calibrations if c.key == self._calibration_key)
        return self.thermometer.calibrations[0]

    def _rebuild_thermometry_inputs(self, *, retain_values=True):
        for key in THERMOMETRY_FIELDS:
            if key in self.numeric_fields:
                spin = self.numeric_fields.pop(key)
                if retain_values:
                    self._thermometry_values[key] = spin.value()
        while self.thermometry_form.rowCount():
            self.thermometry_form.removeRow(0)
        calibration = self._active_calibration()
        for spec in calibration.inputs:
            spin = QtWidgets.QDoubleSpinBox()
            spin.setFixedWidth(80)
            spin.setRange(spec.minimum, spec.maximum)
            spin.setSingleStep(spec.step)
            spin.setDecimals(spec.decimals)
            spin.setValue(self._thermometry_values[spec.key])
            spin.setToolTip(calibration.formula_text)
            spin.valueChanged.connect(self.schedule_preview)
            self.numeric_fields[spec.key] = spin
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(spin)
            row_layout.addWidget(_muted_label(spec.hint), 1)
            self.thermometry_form.addRow(spec.label, row)
        for fixed in calibration.fixed:
            self.thermometry_form.addRow(_muted_label(fixed))
        if hasattr(self, "calibration_combo"):
            self.calibration_combo.setToolTip(calibration.formula_text)

    def _calibration_changed(self, index):
        if self._applying:
            return
        self._calibration_key = self.calibration_combo.itemData(index)
        self._rebuild_thermometry_inputs()
        self._update_citation(self.thermometer.group)
        self._filter_columns()
        self.schedule_preview()

    def _group_citation_html(self, group):
        if self.thermometer and group == self.thermometer.group:
            calibration = self._active_calibration()
            info = group_info(group)
            return (_reference_html((calibration.reference,))
                    + f"<p><b>Formula and constants:</b> {escape(calibration.formula_text)}</p>"
                    + "".join(f"<p>{escape(note)}</p>" for note in calibration.fixed)
                    + f"<p><b>Used for:</b> {escape(info.used_for)}</p>"
                    + f"<p><b>Caveat:</b> {escape(info.caveat)}</p>")
        return citation_html(group)

    def _update_citation(self, group):
        card = self.group_items[group]
        info = group_info(group)
        keys = ((self._active_calibration().reference,) if self.thermometer and group == self.thermometer.group
                else info.references)
        card.citation_label.setText(group if info.section == "literature" else reference(keys[0]).short if keys else "")
        card.citation_row.setVisible(bool(keys))
        card.search_text = " ".join((group, group_info(group).title,
                                     *(reference(key).short + " " + reference(key).full() for key in keys))).casefold()
        card.unverified_tag.setVisible(any(not reference(key).verified for key in keys))
        # The checkbox keeps its selection instruction; the header also carries the citation text.
        card.setToolTip(_plain_text(self._group_citation_html(group)))
        card.info_button.setToolTip(card.toolTip())
        card.citation_label.setToolTip(card.citation_label.text() + "\n\n" + card.toolTip())

    def _show_citation(self, group):
        popup = QtWidgets.QFrame(self, QtCore.Qt.WindowType.Popup)
        popup.setObjectName("citationPopup")
        popup.setStyleSheet(f"QFrame#citationPopup {{ background: {active_theme().surface}; "
                            f"border: 1px solid {active_theme().input_border}; border-radius: 12px; }}")
        layout = QtWidgets.QVBoxLayout(popup)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        label = QtWidgets.QLabel(self._group_citation_html(group))
        label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setOpenExternalLinks(False)
        label.linkActivated.connect(lambda url: QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)))
        scroll.setWidget(label)
        layout.addWidget(scroll)
        screen = self.screen().availableGeometry()
        popup.resize(min(540, screen.width() - 40), min(430, screen.height() - 80))
        point = self.group_items[group].info_button.mapToGlobal(QtCore.QPoint(0, 24))
        point.setX(max(screen.left(), min(point.x(), screen.right() - popup.width())))
        point.setY(max(screen.top(), min(point.y(), screen.bottom() - popup.height())))
        popup.move(point)
        if hasattr(self, "_citation_popup"):
            self._citation_popup.close()
            self._citation_popup.deleteLater()
        self._citation_popup = popup
        popup.show()

    def _sync_group(self, group):
        card = self.group_items[group]
        count = sum(chip.isChecked() for chip in card.chips)
        state = (QtCore.Qt.CheckState.Checked if count == len(card.chips) else
                 QtCore.Qt.CheckState.PartiallyChecked if count else QtCore.Qt.CheckState.Unchecked)
        with QtCore.QSignalBlocker(card.checkbox):
            card.checkbox.setCheckState(state)
        card.count_label.setText(f"{count}/{len(card.chips)}")
        card.count_label.setToolTip(f"{count} of {len(card.chips)} columns")

    def _columns_changed(self, group):
        if self._applying:
            return
        self._sync_group(group)
        self._update_preset_buttons()
        self.schedule_preview()

    def _group_changed(self, group, state):
        if self._applying:
            return
        for chip in self.group_items[group].chips:
            with QtCore.QSignalBlocker(chip):
                chip.setChecked(state != QtCore.Qt.CheckState.Unchecked)
        self._columns_changed(group)

    def _column_choices(self):
        checked = {key for key, chip in self.column_items.items() if chip.isChecked()}
        return (tuple(c.key for c in self.base_table.columns if c.optional and c.key in checked),
                tuple(c.key for c in self.base_table.columns if not c.optional and c.key in self.column_items and c.key not in checked))

    def _update_preset_buttons(self):
        checked = {key for key, chip in self.column_items.items() if chip.isChecked()}
        selected = "custom"
        for name in ("publication", "everything", "minimal"):
            optional, excluded = export_column_preset(self.base_table, name)
            expected = {col.key for col in self.base_table.columns if col.key in self.column_items
                        and (col.key in optional if col.optional else col.key not in excluded)}
            if checked == expected:
                selected = name
                break
        self.preset_buttons[selected].setChecked(True)

    def _apply_preset(self, name):
        optional, excluded = export_column_preset(self.base_table, name)
        self._set_settings(replace(self.export_settings(), optional_columns=optional, excluded_columns=excluded))
        self.schedule_preview()

    def _filter_columns(self):
        query = self.filter_edit.text().strip().casefold()
        for section, (section_widget, grid, groups) in self.sections.items():
            shown = []
            for group in groups:
                card = self.group_items[group]
                header_match = not query or query in card.search_text
                matches = [not query or query in (col.label + " " + col.key).casefold() for col in card.columns]
                visible = header_match or any(matches)
                card.setVisible(visible)
                for chip, match in zip(card.chips, matches):
                    chip.setVisible(header_match or match)
                if card.expand_button:
                    # Filtering can reveal a matching chip without changing the user's expansion choice.
                    card.body.setVisible(bool(query) or card.expand_button.isChecked())
                    card.expand_button.setVisible(not query)
                if visible:
                    shown.append(group)
            self._layout_cards(section, shown)
            section_widget.setVisible(bool(shown))

    def choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Export folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def _set_settings(self, settings):
        self._applying = True
        try:
            for col in self.base_table.columns:
                if col.key in self.column_items:
                    chip = self.column_items[col.key]
                    with QtCore.QSignalBlocker(chip):
                        chip.setChecked(col.key in settings.optional_columns if col.optional else col.key not in settings.excluded_columns)
            with QtCore.QSignalBlocker(self.discordance_combo):
                self.discordance_combo.setCurrentText(settings.discordance_definition)
            self._thermometry_values = {key: getattr(settings, key) for key in THERMOMETRY_FIELDS}
            self._calibration_key = settings.rutile_calibration
            if self.thermometer:
                if hasattr(self, "calibration_combo"):
                    with QtCore.QSignalBlocker(self.calibration_combo):
                        self.calibration_combo.setCurrentIndex(self.calibration_combo.findData(self._calibration_key))
                self._rebuild_thermometry_inputs(retain_values=False)
                self._update_citation(self.thermometer.group)
            with QtCore.QSignalBlocker(self.numeric_fields["discordance_threshold_percent"]):
                self.numeric_fields["discordance_threshold_percent"].setValue(settings.discordance_threshold_percent)
            for key, edit in self.text_fields.items():
                with QtCore.QSignalBlocker(edit):
                    edit.setText(getattr(settings, key))
            with QtCore.QSignalBlocker(self.csv_precision_check):
                self.csv_precision_check.setChecked(settings.csv_full_precision)
            for group in self.group_items:
                self._sync_group(group)
            self._update_preset_buttons()
            self._filter_columns()
        finally:
            self._applying = False

    def export_settings(self) -> ExportSettings:
        optional, excluded = self._column_choices()
        thermometry = dict(self._thermometry_values)
        thermometry.update({key: self.numeric_fields[key].value() for key in THERMOMETRY_FIELDS if key in self.numeric_fields})
        return ExportSettings(discordance_definition=self.discordance_combo.currentText(),
            discordance_threshold_percent=self.numeric_fields["discordance_threshold_percent"].value(),
            citation=self.base_table.settings.citation, **thermometry, rutile_calibration=self._calibration_key,
            **{key: edit.text() for key, edit in self.text_fields.items()},
            csv_full_precision=self.csv_precision_check.isChecked(),
            optional_columns=optional, excluded_columns=excluded)

    def selected_formats(self) -> tuple[str, ...]:
        return tuple(fmt for fmt, check in self.format_checks.items() if check.isChecked())

    def restore_defaults(self):
        self._apply_preset("publication")
        self.refresh_preview()

    def schedule_preview(self, *_args):
        if not self._applying:
            self.preview_timer.start()

    def _refresh_exported_columns(self):
        layout = self.exported_columns_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        columns = self.table.visible_columns()
        self.exported_columns_label.setText(f"Columns that will be exported · {len(columns)}")
        for group in dict.fromkeys(col.group for col in columns):
            layout.addWidget(_muted_label(group))
            row = QtWidgets.QWidget()
            flow = FlowLayout(row)
            for col in columns:
                if col.group == group:
                    chip = _ColumnChip(preview=True)
                    chip.setText(_column_label(col))
                    chip.setToolTip(col.key)
                    flow.addWidget(chip)
            layout.addWidget(row)
        layout.addStretch()

    def refresh_preview(self):
        self.preview_timer.stop()
        self.table = update_export_settings(self.base_table, self.export_settings())
        self.preview = export_table_preview(self.table)
        rows = [self.preview["groups"], self.preview["headings"], *self.preview["rows"]]
        view = self.preview_table
        view.clearSpans()
        view.clearContents()
        view.setColumnCount(len(self.preview["headings"]))
        view.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                if r < 2 or self.preview["row_kinds"][r - 2] != "analysis":
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setBackground(QtGui.QColor(active_theme().input_bg))
                view.setItem(r, c, item)
        groups = self.preview["groups"]
        start = 0
        while start < len(groups):
            end = start + 1
            while end < len(groups) and groups[end] == groups[start]:
                end += 1
            if end - start > 1:
                view.setSpan(0, start, 1, end - start)
            start = end
        for r, kind in enumerate(self.preview["row_kinds"], 2):
            if kind != "analysis" and view.columnCount() > 1:
                view.setSpan(r, 0, 1, view.columnCount())
        view.resizeColumnsToContents()
        for c in range(view.columnCount()):
            view.setColumnWidth(c, min(240, max(95, view.columnWidth(c))))
        self.footnotes_edit.setPlainText(self.preview["footnotes"])
        self._refresh_exported_columns()
        missing = self.preview["missing_fields"]
        for key, edit in self.text_fields.items():
            edit.setPlaceholderText(f"not recorded: appears in footnote {missing[key]}" if key in missing
                                    else "Default publication title" if key == "table_title" else "")

    def accept(self):
        stem = self.stem_edit.text().strip()
        if not self.folder_edit.text().strip():
            self.error_label.setText("Choose an output folder.")
            return
        if not stem or Path(stem).name != stem or stem in {".", ".."}:
            self.error_label.setText("Enter a file stem without folders.")
            return
        if not self.selected_formats() and not self.audit_check.isChecked():
            self.error_label.setText("Select a format or audit files.")
            return
        self.refresh_preview()
        if self.selected_formats() and not self.table.visible_columns():
            self.error_label.setText("Select at least one populated column.")
            return
        super().accept()
