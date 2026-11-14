"""Database status and the GUI-only submission step, without network reads."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.theme import active_theme
from GUI.widgets import ElidedLabel
from UPbRPy.pipeline import PipelineStage, package_version


SUBMIT_STEP = "studio_submit_database"


def is_submit_step(window) -> bool:
    item = window.workflow_step_list.currentItem()
    return item is not None and item.data(QtCore.Qt.ItemDataRole.UserRole) == SUBMIT_STEP


class DatabaseRow(QtWidgets.QFrame):
    """One status row with a single-line explanation and two optional actions."""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 10, 7)
        layout.setSpacing(3)
        heading = QtWidgets.QHBoxLayout()
        heading.setSpacing(10)
        self.title = QtWidgets.QLabel(title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.status = ElidedLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        heading.addWidget(self.title)
        heading.addWidget(self.status, 1)
        layout.addLayout(heading)
        self.detail = ElidedLabel()
        self.detail.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.detail)
        self.actions = QtWidgets.QWidget()
        actions = QtWidgets.QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        self.primary = QtWidgets.QPushButton()
        self.primary.setProperty("accent", True)
        self.secondary = QtWidgets.QPushButton()
        actions.addWidget(self.primary)
        actions.addWidget(self.secondary)
        actions.addStretch()
        layout.addWidget(self.actions)

    def show_state(self, status, detail, color, primary="", secondary="", *, enabled=True):
        theme = active_theme()
        self.setStyleSheet(
            f"DatabaseRow {{ border: 1px solid {theme.input_border};"
            f" border-left: 4px solid {color}; border-radius: 8px;"
            f" background: {theme.surface}; }}"
        )
        self.status.setText(status)
        self.status.setStyleSheet(f"color: {color};")
        self.detail.setText(" ".join(str(detail).splitlines()))
        self.detail.setToolTip(str(detail))
        self.detail.setStyleSheet(f"color: {theme.muted};")
        for button, text in ((self.primary, primary), (self.secondary, secondary)):
            button.setText(text)
            button.setVisible(bool(text))
            button.setEnabled(enabled)
        self.actions.setVisible(bool(primary or secondary))


class DatabasePanel(QtWidgets.QWidget):
    """Present cached session state and outcomes from existing DatabaseLinks flows."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._session = None
        self._run_id = None
        self._bridge = None
        self._operations = {}
        self._verified = ""
        self._submitted = None
        self._submitted_at = ""
        self._preferred = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(9)
        heading = QtWidgets.QLabel("Submit to database")
        font = heading.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 3)
        heading.setFont(font)
        layout.addWidget(heading)
        self.link = DatabaseRow("1. Link to a database session")
        self.check = DatabaseRow("2. Check before submitting")
        self.submit = DatabaseRow("3. Submit the reduction")
        self.prefer = DatabaseRow("4. Preferred run")
        for row in (self.link, self.check, self.submit, self.prefer):
            layout.addWidget(row)
        layout.addStretch()
        self.link.primary.clicked.connect(lambda: window.database().link_session(window))
        self.check.primary.clicked.connect(lambda: window.run_final_review())
        self.submit.primary.clicked.connect(lambda: window.database().submit(window))
        self.submit.secondary.clicked.connect(self.open_run)
        self.prefer.primary.clicked.connect(lambda: window.database().prefer(window))

        self.strip = QtWidgets.QWidget()
        strip = QtWidgets.QHBoxLayout(self.strip)
        strip.setContentsMargins(4, 1, 4, 3)
        strip.setSpacing(8)
        strip.addWidget(QtWidgets.QLabel("Database"))
        self.strip_state = QtWidgets.QLabel()
        self.strip_state.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        strip.addWidget(self.strip_state)
        self.strip_detail = ElidedLabel()
        self.strip_detail.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        strip.addWidget(self.strip_detail, 1)
        self.strip_button = QtWidgets.QPushButton()
        self.strip_button.clicked.connect(self.select_step)
        strip.addWidget(self.strip_button)
        self.hide()
        self.strip.hide()

    def select_step(self) -> None:
        """Navigate only; linking and submitting require a separate page action."""
        rail = self.window.workflow_step_list
        for index in range(rail.count()):
            if rail.item(index).data(QtCore.Qt.ItemDataRole.UserRole) == SUBMIT_STEP:
                rail.setCurrentRow(index)
                return

    def open_run(self) -> None:
        run_id = (self.window.session.origin or {}).get("run_id")
        if run_id:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(f"laicpmsdb://run/{run_id}"))

    @QtCore.Slot(object, str, str, object)
    def operation_changed(self, session, operation, state, result) -> None:
        """Keep transient verification, counts, errors and preference per session."""
        if session is not self.window.session:
            return
        self.refresh()
        self._operations[operation] = (state, result)
        if state == "done":
            if operation == "link":
                self._verified = str(result)
            elif operation == "submit":
                self._submitted = result
                self._submitted_at = datetime.now().astimezone().strftime("%H:%M %Z")
            elif operation == "prefer" and result == self._run_id:
                self._preferred = True
        self.refresh()

    def refresh(self) -> None:
        from GUI.database import database_availability

        window = self.window
        session = window.session
        if session is not self._session:
            self._session = session
            self._operations.clear()
            self._verified = ""
            self._submitted = None
            self._submitted_at = ""
            self._run_id = None
            self._preferred = None
        available, _reason = database_availability()
        visible = available and session.loaded
        self.strip.setVisible(visible)
        selected = visible and is_submit_step(window)
        self.setVisible(selected)
        for widget in window._database_plot_widgets:
            widget.setVisible(not selected)
        if not visible:
            return
        bridge = window.database()
        if bridge is not self._bridge and hasattr(bridge, "operation_changed"):
            if self._bridge is not None:
                self._bridge.operation_changed.disconnect(self.operation_changed)
            bridge.operation_changed.connect(self.operation_changed)
            self._bridge = bridge
        theme = active_theme()
        origin = session.origin or {}
        run_id = origin.get("run_id")
        if run_id != self._run_id:
            self._run_id = run_id
            self._submitted = None
            self._submitted_at = ""
            self._preferred = None
            self._operations.pop("prefer", None)
        host = urlsplit(origin.get("server", "")).netloc or origin.get("server", "")
        key = origin.get("session_key") or origin.get("session_id", "")
        identity = f"{key} on {host}" if origin else "Made from local files"
        preferred = self._preferred
        if preferred is None and isinstance(origin.get("is_preferred"), bool):
            preferred = origin["is_preferred"]
        if preferred is None and "preferred_run_id" in origin and run_id:
            preferred = origin["preferred_run_id"] == run_id
        if run_id:
            state, color, button = f"Submitted · run {run_id[:8]}", theme.success, "Open"
        elif origin:
            state, color, button = "Linked · not submitted", theme.accent, "Submit…"
        else:
            state, color, button = "Not in the database", theme.muted, "Link to session…"
        self.strip_state.setText(state)
        self.strip_state.setStyleSheet(f"color: {color}; font-weight: bold;")
        suffix = (" · preferred" if preferred else " · not preferred") if run_id and preferred is not None else ""
        self.strip_detail.setText(" ".join((identity + suffix).splitlines()))
        self.strip_detail.setToolTip(identity + suffix)
        self.strip_button.setText(button)
        self.link.show_state(
            "done" if origin else "needed",
            identity + (f" · raw data verified: {self._verified}" if origin and self._verified else ""),
            theme.success if origin else theme.accent,
            "" if origin else "Link to database session…",
        )
        final = session.reduction_cache.get("finalized_results")
        if session.pipeline is not None:
            # Read only: never run a stage or synchronize a pipeline here.
            final = session.pipeline.cache.get(PipelineStage.FINALIZE)
        ready = final is not None and not window.pending_region_updates
        results = getattr(final, "selected_results", None)
        count = len(results) if results is not None else 0
        columns = [f"{part}_{edge}_times" for part in ("baseline", "signal", "plateau")
                   for edge in ("start", "end")]
        windows = (int(session.experiment[columns].notna().all(axis=1).sum())
                   if all(column in session.experiment for column in columns) else 0)
        detail = f"{count} analyses with final results · {windows} selection windows · pipeline {package_version()}"
        if not ready:
            detail = f"Final results are not calculated yet · {windows} selection windows"
        self.check.show_state("ready" if ready else "waiting", detail,
                              theme.success if ready else theme.muted,
                              "" if ready else "Calculate final results")
        if run_id:
            status = f"done · run {run_id[:8]}" + (f" at {self._submitted_at}" if self._submitted_at else "")
            detail = "This reduction has been submitted."
            if self._submitted is not None:
                counts = ", ".join(f"{key}: {value}" for key, value in self._submitted.counts.items())
                detail = (counts + " · " if counts else "") + f"quarantined: {len(self._submitted.quarantine_items)}"
            self.submit.show_state(status, detail, theme.success,
                                   "Submit again as a new run…", "Open in UPb Lab")
        else:
            self.submit.show_state("ready" if origin and ready else "waiting",
                                   "Submit this reduction as a new database run." if origin and ready
                                   else "Link a session and calculate final results before submitting.",
                                   theme.accent if origin and ready else theme.muted,
                                   "Submit reduction" if origin and ready else "")
        self.prefer.show_state(
            "after submitting" if not run_id else "preferred" if preferred else "not preferred" if preferred is False else "not checked",
            "The preferred run is shown by default in UPb Lab." if run_id
            else "Submit a reduction before choosing the preferred run.",
            theme.success if preferred else theme.accent if run_id else theme.muted,
            "Mark as preferred" if run_id and not preferred else "",
        )
        busy = window._active_worker is not None or any(state == "working" for state, _ in self._operations.values())
        for name, row in (("link", self.link), ("submit", self.submit), ("prefer", self.prefer)):
            state, result = self._operations.get(name, ("", None))
            if state in {"working", "error"}:
                row.show_state("working…" if state == "working" else "failed",
                               "Please wait…" if state == "working" else str(result),
                               theme.accent if state == "working" else theme.danger,
                               row.primary.text() if not row.primary.isHidden() else "",
                               row.secondary.text() if not row.secondary.isHidden() else "")
        for row in (self.link, self.check, self.submit, self.prefer):
            row.primary.setEnabled(not busy)
            row.secondary.setEnabled(not busy)
