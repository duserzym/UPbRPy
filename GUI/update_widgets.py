"""Canvas version chip and the application Updates dialog."""

from __future__ import annotations

from html import escape

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.standards_updates import STANDARDS_PAGE
from GUI.theme import active_theme, theme_color
from GUI.updater import DOWNLOAD_PAGE
from GUI.updates import check_button_view, friendly_update_error


def _percent(state):
    return min(100, max(0, int(100 * state.progress / state.total))) if state.total else 0


def _last_check(updater):
    value = updater.prefs.value("updates/last_check", "", type=str)
    date = QtCore.QDateTime.fromString(value, QtCore.Qt.DateFormat.ISODate)
    return date.toLocalTime().toString("HH:mm") if date.isValid() else "Never"


class UpdatePopover(QtWidgets.QFrame):
    """A compact view of the shared updater, anchored above its status button."""

    def __init__(self, button, open_settings):
        super().__init__(button, QtCore.Qt.WindowType.Popup)
        self.button = button
        self.updater = button.updater
        self.setObjectName("UpdatePopover")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Update status")
        layout.addWidget(self.status)
        self.action = QtWidgets.QPushButton()
        self.action.clicked.connect(self._act)
        layout.addWidget(self.action)
        self.settings_link = QtWidgets.QLabel('<a href="settings">Update settings…</a>')
        self.settings_link.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse
                                                   | QtCore.Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.settings_link.linkActivated.connect(lambda _url: self._settings(open_settings))
        layout.addWidget(self.settings_link)
        self.updater.stateChanged.connect(self._changed)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._style()
        self._changed(self.updater.state)

    def _settings(self, open_settings):
        self.hide()
        open_settings()

    def _act(self):
        action = check_button_view(self.updater.state, self.updater.version,
                                   self.updater.auto_download).action
        if action == "download":
            self.updater.download()
        elif action == "restart":
            self.updater.install()
        elif action == "page":
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(DOWNLOAD_PAGE))

    def _style(self):
        theme = active_theme()
        # Only this frame is restyled, and only when the theme changes.
        self.setStyleSheet(f"""
            QFrame#UpdatePopover {{ background: {theme.surface}; color: {theme.text};
                border: 1px solid {theme.input_border}; border-radius: 6px; }}
            QFrame#UpdatePopover QLabel {{ color: {theme.text}; border: none; }}
        """)
        self.settings_link.setText(f'<a style="color: {theme.accent}" href="settings">Update settings…</a>')

    def _changed(self, state):
        view = check_button_view(state, self.updater.version, self.updater.auto_download)
        self.status.setText(view.text)
        self.action.setText({"download": "Download", "restart": "Restart to update",
                             "page": "Open download page"}.get(view.action, ""))
        self.action.setVisible(view.action is not None)
        if self.isVisible():
            self.reanchor()

    def paintEvent(self, event):
        # Popup windows are translucent under the app's rounded-corner filter.
        # Paint an opaque surface explicitly so plot content cannot show through.
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtGui.QColor(active_theme().surface))
        painter.setPen(QtGui.QPen(theme_color(active_theme().input_border), 1))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

    def reanchor(self):
        """Keep the popup inside both the host window and its available screen."""
        window = self.button.window()
        bounds = QtCore.QRect(window.mapToGlobal(QtCore.QPoint()), window.size())
        screen = self.button.screen()
        if screen is not None:
            bounds = bounds.intersected(screen.availableGeometry())
        self.setFixedWidth(min(340, max(1, bounds.width() - 12)))
        self.adjustSize()
        anchor = self.button.mapToGlobal(QtCore.QPoint(self.button.width(), 0))
        x = max(bounds.left(), min(anchor.x() - self.width(), bounds.right() - self.width() + 1))
        y = max(bounds.top(), min(anchor.y() - self.height() - 6, bounds.bottom() - self.height() + 1))
        self.move(x, y)

    def showEvent(self, event):
        self._changed(self.updater.state)
        self.reanchor()
        super().showEvent(event)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide()
            event.accept()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.DynamicPropertyChange:
            if bytes(event.propertyName()) == b"upbrpy.resolved_theme":
                self._style()
        if self.isVisible():
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                point = event.globalPosition().toPoint()
                button_rect = QtCore.QRect(self.button.mapToGlobal(QtCore.QPoint()), self.button.size())
                if button_rect.contains(point):
                    # Consume the press so Qt's popup click replay cannot reopen it.
                    self.hide()
                    return True
                if not self.frameGeometry().contains(point):
                    self.hide()
            if watched is self.button.window():
                if event.type() in {QtCore.QEvent.Type.Move, QtCore.QEvent.Type.Resize}:
                    self.reanchor()
                elif event.type() in {QtCore.QEvent.Type.Hide, QtCore.QEvent.Type.Close}:
                    self.hide()
        return super().eventFilter(watched, event)


class CheckUpdatesButton(QtWidgets.QToolButton):
    """Always-visible manual update check, using the application updater."""

    def __init__(self, updater, open_settings, parent=None):
        super().__init__(parent)
        self.updater = updater
        self.setObjectName("CheckUpdatesButton")
        self.setAccessibleName("Check for updates")
        self.setAutoRaise(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        # 21 px keeps the status bar at its existing height (the plots keep theirs).
        self.setIconSize(QtCore.QSize(16, 16))
        self.setFixedSize(21, 21)
        self._angle = 0
        self._rotation_timer = QtCore.QTimer(self)
        self._rotation_timer.setInterval(40)
        self._rotation_timer.timeout.connect(self._rotate)
        self.popover = UpdatePopover(self, open_settings)
        self.clicked.connect(self._toggle)
        updater.stateChanged.connect(self._changed)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._changed(updater.state)

    def _toggle(self):
        if self.popover.isVisible():
            self.popover.hide()
            return
        if self.updater.state.status not in {"checking", "downloading", "installing"}:
            self.updater.check(manual=True)
        self.popover.show()
        self.popover.setFocus()

    def _changed(self, state):
        view = check_button_view(state, self.updater.version, self.updater.auto_download)
        self.setProperty("badge", view.badge)
        self.setProperty("busy", view.busy)
        animate = bool(self.style().styleHint(QtWidgets.QStyle.StyleHint.SH_Widget_Animate, None, self))
        if view.busy and animate and self.isVisible():
            self._rotation_timer.start()
        else:
            self._rotation_timer.stop()
            self._angle = 0
        self.setToolTip(f"Check for updates · v{self.updater.version} · {view.text}")
        self.update()

    def _rotate(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._changed(self.updater.state)

    def hideEvent(self, event):
        self._rotation_timer.stop()
        self.popover.hide()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.DynamicPropertyChange:
            if bytes(event.propertyName()) == b"upbrpy.resolved_theme":
                self.update()
        if watched is self and event.type() == QtCore.QEvent.Type.StyleChange:
            self._changed(self.updater.state)
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        super().paintEvent(event)
        theme = active_theme()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.save()
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self._angle)
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.text), 1.7,
                                 QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap,
                                 QtCore.Qt.PenJoinStyle.RoundJoin))
        painter.drawArc(QtCore.QRectF(-6, -6, 12, 12), 60 * 16, 300 * 16)
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(3.5, 2.5),
                                             QtCore.QPointF(6, 0), QtCore.QPointF(8.5, 2.5)]))
        painter.restore()
        if self.property("badge"):
            painter.setPen(QtGui.QPen(QtGui.QColor(theme.surface), 1.2))
            painter.setBrush(QtGui.QColor(theme.accent))
            painter.drawEllipse(QtCore.QPointF(self.width() - 4, 4), 2.6, 2.6)


class VersionChip(QtWidgets.QToolButton):
    """A plot overlay that follows update state, theme and canvas geometry."""

    def __init__(self, updater, parent=None):
        super().__init__(parent)
        self.updater = updater
        self._canvas = None
        self._dialog = None
        self._dot = False
        self._show_none = False
        self._last_state = None
        self._none_timer = QtCore.QTimer(self)
        self._none_timer.setSingleShot(True)
        self._none_timer.setInterval(5000)
        self._none_timer.timeout.connect(self._clear_none)
        self.setObjectName("VersionChip")
        self.setAutoRaise(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Studio version and updates")
        self.clicked.connect(self.open_dialog)
        updater.stateChanged.connect(self._changed)
        updater.standardsChanged.connect(self._standards_changed)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._changed(updater.state)

    def anchor_to(self, parent_widget):
        """Keep this overlay ten pixels inside the canvas, above every plot page."""
        if self._canvas is not None:
            self._canvas.removeEventFilter(self)
            if isinstance(self._canvas, QtWidgets.QStackedWidget):
                self._canvas.currentChanged.disconnect(self._position)
        self._canvas = parent_widget
        self.setParent(parent_widget)
        parent_widget.installEventFilter(self)
        if isinstance(parent_widget, QtWidgets.QStackedWidget):
            parent_widget.currentChanged.connect(self._position)
        self._position()
        self.show()

    def eventFilter(self, watched, event):
        if watched is self._canvas and event.type() in {
            QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show, QtCore.QEvent.Type.LayoutRequest,
        }:
            self._position()
        if event.type() == QtCore.QEvent.Type.DynamicPropertyChange:
            if bytes(event.propertyName()) == b"upbrpy.resolved_theme":
                self._style()
        return super().eventFilter(watched, event)

    def _position(self, *_args):
        self.adjustSize()
        if self._canvas is not None:
            self.move(max(0, self._canvas.width() - self.width() - 10),
                      max(0, self._canvas.height() - self.height() - 10))
            self.raise_()

    def _style(self):
        theme = active_theme()
        self.setStyleSheet(f"""
            QToolButton#VersionChip {{ background: {theme.surface}; color: {theme.text};
                border: 1px solid {theme.input_border}; border-radius: 13px;
                padding: 5px 12px 5px {24 if self._dot else 12}px; font-size: 11px; }}
            QToolButton#VersionChip:hover {{ background: {theme.surface_soft};
                border-color: {theme.accent}; }}
        """)
        self._position()
        self.update()

    def _changed(self, state):
        if state is not self._last_state:
            self._show_none = state.status == "none" and state.manual
            if self._show_none:
                self._none_timer.start()
            else:
                self._none_timer.stop()
            self._last_state = state
        self._dot = state.status in {"available", "ready"}
        text = {
            "checking": "Checking", "downloading": f"Downloading {_percent(state)}%",
            "available": "Update available", "ready": "Update ready, restart",
            "error": "Check failed", "installing": "Installing",
        }.get(state.status, f"v{self.updater.version}")
        if self._show_none:
            text = "Up to date"
        if self.updater.standards_warning:
            text += " · Standards warning"
        self.setText(text)
        self._style()

    def _standards_changed(self):
        self._changed(self.updater.state)

    def _clear_none(self):
        self._show_none = False
        self._changed(self.updater.state)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._dot:
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(active_theme().accent))
            painter.drawEllipse(QtCore.QPointF(12, self.height() / 2), 3, 3)

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
    """Update status, release notes and persistent download/channel preferences."""

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
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
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
        channel_row = QtWidgets.QHBoxLayout()
        channel_row.addWidget(QtWidgets.QLabel("Channel:"))
        self.channel_combo = QtWidgets.QComboBox()
        self.channel_combo.addItem("Stable", "stable")
        self.channel_combo.addItem("Beta (pre-releases)", "beta")
        self.channel_combo.setCurrentIndex(0 if updater.channel == "stable" else 1)
        self.channel_combo.currentIndexChanged.connect(self._channel_changed)
        channel_row.addWidget(self.channel_combo)
        channel_row.addStretch()
        layout.addLayout(channel_row)
        self.last_check = QtWidgets.QLabel()
        self.last_check.setWordWrap(True)
        layout.addWidget(self.last_check)
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Release notes appear after a check.")
        self.notes.setMaximumHeight(self.notes.fontMetrics().lineSpacing() * 12 + 16)
        layout.addWidget(self.notes, 1)
        self.notes_link = QtWidgets.QLabel()
        self.notes_link.setOpenExternalLinks(True)
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
        self._set_notes_link(self.updater.state.info or {})

    def _set_notes_link(self, info):
        url = escape(info.get("html_url") or DOWNLOAD_PAGE, quote=True)
        self.notes_link.setText(f'<a style="color: {active_theme().accent}" href="{url}">'
                                'Release notes on GitHub</a>')

    def _auto_changed(self, checked):
        self.updater.prefs.setValue("updates/auto_download", checked)
        self._changed(self.updater.state)
        if checked:
            self.updater.download()

    def _channel_changed(self, _index):
        self.updater.prefs.setValue("updates/channel", self.channel_combo.currentData())

    def _changed(self, state):
        info = state.info or {}
        version = info.get("version", "")
        status = {
            "idle": "Check for a new version of Studio.", "checking": "Checking for updates…",
            "none": f"You're up to date, checked {_last_check(self.updater)}",
            "available": f"Version {version} is available",
            "downloading": f"Downloading {version}, {_percent(state)}%",
            "ready": f"Version {version} is ready to install",
            "installing": "Installing and restarting",
            "error": "Update check failed: " + friendly_update_error(state.message).removeprefix(
                "The update check failed: "), "unsupported": state.message,
        }[state.status]
        if state.status == "unsupported" and info:
            status += f" Version {version} is available."
        self.status.setText(status)
        self.status.setToolTip(state.message if state.status == "error" else "")
        self.check_button.setEnabled(state.status not in {"checking", "downloading", "installing"})
        self.download_button.setVisible(state.status == "available" and not self.updater.auto_download)
        self.restart_button.setVisible(state.status == "ready")
        self.page_button.setVisible(state.status == "unsupported")
        self.last_check.setText(f"Last check: {_last_check(self.updater)} · "
                                + self.updater.prefs.value("updates/last_result", "Not checked yet", type=str))
        self.notes.setPlainText(state.message if state.status == "error" else info.get("body") or "")
        self._set_notes_link(info)

    def _standards_changed(self):
        status = self.updater.standards
        message = "Checking standards…" if self.updater.standards_checking else status.message
        if self.updater.standards_library_changed:
            message += " Warning: the configured library changed since you last reviewed it."
        self.standards_status.setText(message)
        self.standards_status.setToolTip(escape("\n".join((*status.changed, status.details))))
        self.standards_check.setEnabled(not self.updater.standards_checking)
        self.standards_ack.setVisible(self.updater.standards_library_changed)
