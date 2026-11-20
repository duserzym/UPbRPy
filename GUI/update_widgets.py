"""Shared version controls and Studio's updates and standards dialog."""

from html import escape
from PySide6 import QtCore, QtGui, QtWidgets
from GUI.standards_updates import STANDARDS_PAGE
from GUI.theme import active_theme
from GUI.updater import DOWNLOAD_PAGE
from GUI.labupdate.qt import (CheckUpdatesButton, VersionChip as SharedVersionChip,
                              WhatsNewCard, NotesLabel, release_notes_url)
from GUI.labupdate.state import button_view


def _last_check(updater):
    value = updater.prefs.value("updates/last_check", "", type=str)
    date = QtCore.QDateTime.fromString(value, QtCore.Qt.DateFormat.ISODate)
    return date.toLocalTime().toString("HH:mm") if date.isValid() else "Never"


class VersionChip(SharedVersionChip):
    """Shared version chip opening Studio's standards-aware settings dialog."""

    def __init__(self, updater, parent=None):
        super().__init__(updater, parent)
        self._dialog = None

    def open_dialog(self, manual=False):
        """Open the shared updater's dialog; Help also requests a manual check."""
        if self._dialog is None:
            self._dialog = UpdatesDialog(self.updater, self.window())
            self._dialog.destroyed.connect(self._forget_dialog)
        elif self._dialog.parentWidget() is not self.window():
            self._dialog.setParent(self.window(), self._dialog.windowFlags())
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        if manual:
            self.updater.check(manual=True)
        return self._dialog

    def _forget_dialog(self, *_args):
        self._dialog = None


class UpdatesDialog(QtWidgets.QDialog):
    """Update status, release notes and persistent download preferences."""

    def __init__(self, updater, parent=None):
        super().__init__(parent)
        self.updater = updater
        self.setObjectName("UpdatesDialog")
        self.setWindowTitle("Updates")
        self.resize(520, 420)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        self.heading = QtWidgets.QLabel(f"UPbRPy Studio v{updater.version}")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        layout.addWidget(self.heading)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QtWidgets.QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.details)
        row = QtWidgets.QHBoxLayout()
        self.check_button = QtWidgets.QPushButton("Check now")
        self.check_button.clicked.connect(lambda: updater.check(manual=True))
        self.download_button = QtWidgets.QPushButton("Download")
        self.download_button.clicked.connect(updater.download)
        self.restart_button = QtWidgets.QPushButton("Restart to update")
        self.restart_button.clicked.connect(updater.install)
        self.page_button = QtWidgets.QPushButton("Open download page")
        self.page_button.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(DOWNLOAD_PAGE)))
        for button in (self.check_button, self.download_button, self.restart_button, self.page_button):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.auto_checkbox = QtWidgets.QCheckBox("Download updates automatically")
        self.auto_checkbox.setChecked(updater.auto_download)
        self.auto_checkbox.toggled.connect(self._auto_changed)
        layout.addWidget(self.auto_checkbox)
        self.last_check = QtWidgets.QLabel()
        self.last_check.setWordWrap(True)
        layout.addWidget(self.last_check)
        self.notes = NotesLabel()
        layout.addWidget(self.notes, 1)
        self.notes_link = QtWidgets.QLabel()
        self.notes_link.linkActivated.connect(lambda _: QtGui.QDesktopServices.openUrl(QtCore.QUrl(release_notes_url(self.updater))))
        layout.addWidget(self.notes_link)
        self.standards_status = QtWidgets.QLabel()
        self.standards_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.standards_status.setWordWrap(True)
        layout.addWidget(self.standards_status)
        standards_row = QtWidgets.QHBoxLayout()
        self.standards_check = QtWidgets.QPushButton("Check standards")
        self.standards_check.clicked.connect(updater.check_standards)
        standards_row.addWidget(self.standards_check)
        self.standards_ack = QtWidgets.QPushButton("Acknowledge library change")
        self.standards_ack.clicked.connect(updater.acknowledge_standards)
        standards_row.addWidget(self.standards_ack)
        standards_page = QtWidgets.QPushButton("Standards on GitHub")
        standards_page.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(STANDARDS_PAGE)))
        standards_row.addWidget(standards_page)
        standards_row.addStretch()
        layout.addLayout(standards_row)
        updater.stateChanged.connect(self._changed)
        updater.standardsChanged.connect(self._standards_changed)
        self._standards_changed()
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._style()
        self._changed(updater.state)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.DynamicPropertyChange:
            if bytes(event.propertyName()) == b"upbrpy.resolved_theme":
                self._style()
        return super().eventFilter(watched, event)

    def _style(self):
        theme = active_theme()
        self.setStyleSheet(f"""
            QDialog#UpdatesDialog {{ background: {theme.window}; color: {theme.text}; }}
            QDialog#UpdatesDialog QLabel {{ color: {theme.text}; }}
            QDialog#UpdatesDialog QPlainTextEdit {{ background: {theme.input_bg};
                color: {theme.text}; border: 1px solid {theme.input_border}; border-radius: 6px; }}
        """)
        self._set_notes_link()

    def _set_notes_link(self):
        self.notes_link.setText('<a href="release">Release notes</a>')

    def _auto_changed(self, checked):
        self.updater.set_auto_download(checked)

    def _changed(self, state):
        view = button_view(state, self.updater.version, self.updater.auto_download)
        self.status.setText(view.text)
        self.check_button.setEnabled(not view.busy)
        self.download_button.setVisible(view.action == "download")
        self.restart_button.setVisible(view.action == "restart")
        self.page_button.setVisible(view.action == "page")
        raw = " ".join(state.message.split())
        self.details.setText("Details: " + (raw[:119] + "…" if len(raw) > 120 else raw))
        self.details.setVisible(state.status == "error")
        self.last_check.setText(f"Last check: {_last_check(self.updater)} · "
                                + self.updater.prefs.value("updates/last_result", "Not checked yet", type=str))
        self.notes.setText(getattr(state.info, "notes", "") if state.status in {"available", "ready"} else "")
        self.notes.setVisible(bool(self.notes.text()))
        self.notes_link.setVisible(state.status in {"available", "ready"})
        self._set_notes_link()

    def _standards_changed(self):
        status = self.updater.standards
        message = "Checking standards…" if self.updater.standards_checking else status.message
        if self.updater.standards_library_changed:
            message += " Warning: the configured library changed since you last reviewed it."
        self.standards_status.setText(message)
        self.standards_status.setToolTip(escape("\n".join((*status.changed, status.details))))
        self.standards_check.setEnabled(not self.updater.standards_checking)
        self.standards_ack.setVisible(self.updater.standards_library_changed)
