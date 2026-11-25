"""Preview a standalone Chromium log before applying its tie."""

from pathlib import Path

from PySide6 import QtCore, QtWidgets

from UPbRPy.laser_tie import tie_laser_log


def laser_log_start_path(source: str) -> str:
    """Start in the session folder, selecting its only nearby .lase if unique."""
    folder = Path(source)
    folder = folder.parent if folder.is_file() else folder
    for parent in (folder, folder.parent):
        candidates = sorted(p for p in parent.iterdir()
                            if p.is_file() and p.suffix.lower() == ".lase")
        if len(candidates) == 1:
            return str(candidates[0])
    return str(folder)


class LaserOfferDialog(QtWidgets.QMessageBox):
    """One compact offer; source-specific choices can be added here later."""

    def __init__(self, source: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import laser log?")
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.setText(f"A laser log is available for this session ({source}).\n\n"
                     "Import it to help with selections? Laser data never changes "
                     "reduction numbers unless you choose a laser method.")
        self.import_button = self.addButton("Import…", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        self.not_now_button = self.addButton("Not now", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        self.setDefaultButton(self.not_now_button)
        self.setEscapeButton(self.not_now_button)


class LaserLogDialog(QtWidgets.QDialog):
    """Show clock evidence and require explicit consent for settings-only ties."""

    def __init__(self, log, acquisitions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Load laser log")
        self.resize(540, 350)
        self.log = log
        self.acquisitions = acquisitions
        layout = QtWidgets.QVBoxLayout(self)
        name = QtWidgets.QLabel(log.path.name if log.path else "Laser log")
        name.setWordWrap(True)
        layout.addWidget(name)
        form = QtWidgets.QFormLayout()
        self.key_combo = QtWidgets.QComboBox()
        self.key_combo.addItem("Position", "position")
        self.key_combo.addItem("Description", "description")
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setRange(1., 600.)
        self.threshold_spin.setValue(30.)
        self.threshold_spin.setSuffix(" s")
        form.addRow("Match by", self.key_combo)
        form.addRow("Doubtful beyond", self.threshold_spin)
        layout.addLayout(form)
        self.summary_label = QtWidgets.QLabel()
        self.summary_label.setWordWrap(True)
        self.warnings_text = QtWidgets.QTextBrowser()
        self.warnings_text.setMaximumHeight(130)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.warnings_text)
        self.confirm_check = QtWidgets.QCheckBox("I confirm linking settings by number without firing times")
        layout.addWidget(self.confirm_check)
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Apply | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply)
        self.apply_button.clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.key_combo.currentIndexChanged.connect(self.refresh_tie)
        self.threshold_spin.valueChanged.connect(self.refresh_tie)
        self.confirm_check.toggled.connect(self._sync_apply)
        self.refresh_tie()

    def refresh_tie(self, *_args):
        self.tie = tie_laser_log(self.log, self.acquisitions, spot_key=self.key_combo.currentData(),
                                 threshold_s=self.threshold_spin.value())
        counts = self.tie.counts
        offset = self.tie.clock_offset_s
        spread = self.tie.clock_offset_spread_s
        clock = f"Clock offset: {offset:g} s; spread (MAD): {spread:g} s" if offset is not None else "Clock offset: unavailable"
        self.summary_label.setText(
            f"Linked: {counts['linked']}    Doubtful: {counts['doubtful']}    Unlinked: {counts['unlinked']}\n"
            f"{clock}\nClock pairs: {self.tie.pairs}")
        self.warnings_text.setPlainText("\n".join(self.tie.warnings) or "No tie warnings.")
        self.confirm_check.setVisible(self.tie.requires_confirmation)
        self._sync_apply()

    def _sync_apply(self, *_args):
        self.apply_button.setEnabled(not self.tie.requires_confirmation or self.confirm_check.isChecked())

    def accept(self):
        if self.apply_button.isEnabled():
            super().accept()
