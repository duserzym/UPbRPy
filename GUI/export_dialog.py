"""Publication export choices and a preview over one prepared engine table."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.theme import active_theme
from UPbRPy.export_table import (
    ExportSettings, ExportTable, export_table_preview,
    update_export_settings,
)


class ExportResultsDialog(QtWidgets.QDialog):
    """Edit export settings without running reduction stages on the UI thread."""

    def __init__(self, table: ExportTable, parent=None, *, folder: str | Path = "",
                 file_stem: str = "", excluded_spots=()):
        super().__init__(parent)
        self.setWindowTitle("Export results")
        self.base_table = table
        self.table = table
        self.excluded_spots = tuple(excluded_spots)
        self.resize(1120, 700)
        screen = self.screen().availableGeometry()
        self.resize(min(1180, self.width(), screen.width() - 40),
                    min(760, self.height(), screen.height() - 80))
        layout = QtWidgets.QVBoxLayout(self)
        output = QtWidgets.QFormLayout()
        folder_row = QtWidgets.QWidget()
        folder_layout = QtWidgets.QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_edit = QtWidgets.QLineEdit(str(folder))
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self.choose_folder)
        folder_layout.addWidget(self.folder_edit, 1)
        folder_layout.addWidget(browse)
        output.addRow("Folder", folder_row)
        self.stem_edit = QtWidgets.QLineEdit(file_stem or table.context.get("session_name", "session") + "_results_table")
        output.addRow("File stem", self.stem_edit)
        formats_row = QtWidgets.QWidget()
        formats_layout = QtWidgets.QHBoxLayout(formats_row)
        formats_layout.setContentsMargins(0, 0, 0, 0)
        self.format_checks = {}
        for fmt in ("xlsx", "csv", "md", "si"):
            check = QtWidgets.QCheckBox("SI workbook (.xlsx)" if fmt == "si" else "." + fmt)
            check.setChecked(fmt != "si")
            self.format_checks[fmt] = check
            formats_layout.addWidget(check)
        self.audit_check = QtWidgets.QCheckBox("Audit files and replay notebook")
        self.audit_check.setChecked(True)
        formats_layout.addWidget(self.audit_check)
        formats_layout.addStretch()
        output.addRow("Formats", formats_row)
        layout.addLayout(output)
        excluded_count = int(table.rows["excluded"].sum()) if "excluded" in table.rows else len(self.excluded_spots)
        self.exclusion_label = QtWidgets.QLabel(f"{excluded_count} analyses excluded in Final review (retained in the table; omitted from summaries).")
        self.exclusion_label.setWordWrap(True)
        layout.addWidget(self.exclusion_label)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)
        columns_page = QtWidgets.QWidget()
        columns_layout = QtWidgets.QVBoxLayout(columns_page)
        self.column_tree = QtWidgets.QTreeWidget()
        self.column_tree.setHeaderLabels(["Column groups and columns"])
        self.column_items = {}
        self.group_items = {}
        for col in table.columns:
            if col.materials is not None and table.context.get("material") not in col.materials:
                continue
            if col.group not in self.group_items:
                group = QtWidgets.QTreeWidgetItem(self.column_tree, [col.group])
                group.setFlags(group.flags() | QtCore.Qt.ItemFlag.ItemIsAutoTristate | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                self.group_items[col.group] = group
            item = QtWidgets.QTreeWidgetItem(self.group_items[col.group],
                [col.label + (f" ({col.unit})" if col.unit else "") + (" — optional" if col.optional else "")])
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, col.key)
            self.column_items[col.key] = item
        columns_layout.addWidget(self.column_tree)
        self.defaults_button = QtWidgets.QPushButton("Defaults")
        self.defaults_button.clicked.connect(self.restore_defaults)
        columns_layout.addWidget(self.defaults_button, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.tabs.addTab(columns_page, "Columns")
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        settings_page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(settings_page)
        self.discordance_combo = QtWidgets.QComboBox()
        self.discordance_combo.addItems(["7/5-6/8", "7/6-6/8"])
        form.addRow("Discordance definition", self.discordance_combo)
        self.numeric_fields = {}
        for key, label, low, high in (("discordance_threshold_percent", "Discordance threshold (%)", 0, 1000),
                                      ("a_sio2", "aSiO2", .000001, 100), ("a_tio2", "aTiO2", .000001, 100)):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setDecimals(1 if key == "discordance_threshold_percent" else 2)
            spin.setRange(low, high)
            spin.setSingleStep(.1)
            self.numeric_fields[key] = spin
            form.addRow(label, spin)
        self.csv_precision_check = QtWidgets.QCheckBox("CSV full precision")
        form.addRow(self.csv_precision_check)
        self.text_fields = {}
        for key, label in (("laboratory", "Laboratory"), ("laser_system", "Laser system"),
                           ("spot_diameter_um", "Spot diameter (µm)"), ("repetition_rate_hz", "Repetition rate (Hz)"),
                           ("fluence_j_cm2", "Fluence (J/cm²)"), ("operator", "Operator override"), ("table_title", "Table title")):
            edit = QtWidgets.QLineEdit()
            self.text_fields[key] = edit
            form.addRow(label, edit)
        scroll.setWidget(settings_page)
        self.tabs.addTab(scroll, "Settings")
        preview_page = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_page)
        self.preview_table = QtWidgets.QTableWidget()
        self.preview_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.horizontalHeader().hide()
        self.preview_table.verticalHeader().hide()
        preview_layout.addWidget(self.preview_table, 3)
        self.footnotes_edit = QtWidgets.QPlainTextEdit()
        self.footnotes_edit.setReadOnly(True)
        preview_layout.addWidget(self.footnotes_edit, 2)
        self.tabs.addTab(preview_page, "Preview")
        self.error_label = QtWidgets.QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.export_button = buttons.addButton("Export", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.export_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(160)
        self.preview_timer.timeout.connect(self.refresh_preview)
        self._set_settings(table.settings)
        self.column_tree.itemChanged.connect(self.schedule_preview)
        self.discordance_combo.currentTextChanged.connect(self.schedule_preview)
        for spin in self.numeric_fields.values():
            spin.valueChanged.connect(self.schedule_preview)
        for edit in self.text_fields.values():
            edit.textChanged.connect(self.schedule_preview)
        self.csv_precision_check.toggled.connect(self.schedule_preview)
        self.refresh_preview()

    def choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Export folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def _set_settings(self, settings):
        self.column_tree.blockSignals(True)
        for col in self.base_table.columns:
            if col.key in self.column_items:
                checked = col.key in settings.optional_columns if col.optional else col.key not in settings.excluded_columns
                self.column_items[col.key].setCheckState(0, QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        self.column_tree.blockSignals(False)
        self.discordance_combo.setCurrentText(settings.discordance_definition)
        for key, spin in self.numeric_fields.items():
            spin.setValue(getattr(settings, key))
        for key, edit in self.text_fields.items():
            edit.setText(getattr(settings, key))
        self.csv_precision_check.setChecked(settings.csv_full_precision)

    def export_settings(self) -> ExportSettings:
        checked = {key for key, item in self.column_items.items() if item.checkState(0) == QtCore.Qt.CheckState.Checked}
        return ExportSettings(discordance_definition=self.discordance_combo.currentText(),
            citation=self.base_table.settings.citation,
            **{key: spin.value() for key, spin in self.numeric_fields.items()},
            **{key: edit.text() for key, edit in self.text_fields.items()},
            csv_full_precision=self.csv_precision_check.isChecked(),
            optional_columns=tuple(c.key for c in self.base_table.columns if c.optional and c.key in checked),
            excluded_columns=tuple(c.key for c in self.base_table.columns if not c.optional and c.key in self.column_items and c.key not in checked))

    def selected_formats(self) -> tuple[str, ...]:
        return tuple(fmt for fmt, check in self.format_checks.items() if check.isChecked())

    def restore_defaults(self):
        # Defaults resets column choices; laboratory metadata remains available.
        self._set_settings(replace(self.export_settings(), optional_columns=(), excluded_columns=()))
        self.refresh_preview()

    def schedule_preview(self, *_args):
        self.preview_timer.start()

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
