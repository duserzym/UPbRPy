"""The tabbed application shell; reductions retain their own widgets and state."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from GUI.main_window import MainWindow
from GUI.theme import WallpaperWidget, active_theme, theme_color, paint_wallpaper


class _MirrorMenu(QtWidgets.QMenu):
    """Own the platform menu while sharing the reduction's leaf actions."""

    def __init__(self, source: QtWidgets.QMenu, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self._source: QtWidgets.QMenu | None = source
        self._submenus: list[_MirrorMenu] = []
        source.menuAction().changed.connect(self._sync_state)
        source.destroyed.connect(self._source_destroyed)
        self.aboutToShow.connect(self._refresh)
        self._sync_state()
        self._sync_contents()

    def _sync_state(self) -> None:
        source = self._source
        if source is not None and isValid(source):
            action = source.menuAction()
            self.setTitle(source.title())
            self.setObjectName(source.objectName())
            self.setToolTipsVisible(source.toolTipsVisible())
            self.setIcon(source.icon())
            self.menuAction().setEnabled(action.isEnabled())
            self.menuAction().setVisible(action.isVisible())

    def _clear_contents(self) -> None:
        # Remove shared actions before disposing menus; their owner is the
        # reduction, which may be deleted before DeferredDelete is processed.
        for action in self.actions():
            self.removeAction(action)
        for submenu in self._submenus:
            submenu.dispose()
        self._submenus.clear()

    def _sync_contents(self) -> None:
        self._clear_contents()
        if self._source is not None and isValid(self._source):
            actions = self._source.actions()
            submenus = {action: menu for action in actions
                        if (menu := action.menu()) is not None}
            # QAction.menu() gives PySide's wrapper a reference to its menu
            # action. Retain the original submenu tree on its source parent
            # too, so Python GC cannot collect inactive submenu/action cycles.
            self._source._studio_submenus = list(submenus.values())
            for action in actions:
                if action in submenus:
                    submenu = _MirrorMenu(submenus[action], self)
                    self._submenus.append(submenu)
                    self.addMenu(submenu)
                else:
                    self.addAction(action)

    def _refresh(self) -> None:
        # A source aboutToShow handler may clear and delete its old submenus.
        self._clear_contents()
        if self._source is not None and isValid(self._source):
            self._source.aboutToShow.emit()
            self._sync_state()
            self._sync_contents()

    def _source_destroyed(self) -> None:
        self._source = None
        self._clear_contents()
        self.menuAction().setEnabled(False)

    def dispose(self) -> None:
        """Disconnect and empty the mirror before scheduling its deletion."""
        if self._source is not None and isValid(self._source):
            self._source.menuAction().changed.disconnect(self._sync_state)
            self._source.destroyed.disconnect(self._source_destroyed)
        self._source = None
        self._clear_contents()
        self.deleteLater()


class _DocumentCloseButton(QtWidgets.QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setToolTip("Close tab")
        self.setAccessibleName("Close tab")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        theme = active_theme()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if self.underMouse() or self.hasFocus():
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(theme_color(theme.accent_soft))
            painter.drawEllipse(QtCore.QRectF(1, 1, 18, 18))
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.muted), 1.5,
                                 QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap))
        painter.drawLine(QtCore.QPointF(7, 7), QtCore.QPointF(13, 13))
        painter.drawLine(QtCore.QPointF(7, 13), QtCore.QPointF(13, 7))


class StudioWindow(QtWidgets.QMainWindow):
    """Present independent reductions as movable, closable document tabs."""

    new_window_requested = QtCore.Signal()
    move_tab_requested = QtCore.Signal(object)
    activated = QtCore.Signal()
    GEOMETRY_KEY = "window_geometry_v1"

    def __init__(
        self,
        theme_mode: str = "light",
        app_icon: QtGui.QIcon | None = None,
        *,
        reduction_factory: Callable[[], MainWindow] | None = None,
        create_empty: bool = True,
    ) -> None:
        super().__init__()
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.settings = QtCore.QSettings("UPbRPy", "Studio")
        self._reduction_factory = reduction_factory or (
            lambda: MainWindow(theme_mode=theme_mode, app_icon=app_icon)
        )
        self._connections: dict[MainWindow, list[tuple[object, Callable]]] = {}
        self._shortcuts: dict[MainWindow, dict[QtGui.QAction, list[QtGui.QKeySequence]]] = {}
        self._shown_menus: list[_MirrorMenu] = []
        self._checking_close = False
        self._close_confirmed = False
        self.setMinimumSize(760, 620)
        self.setProperty("wallpaperRoot", True)
        if app_icon is not None:
            self.setWindowIcon(app_icon)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setObjectName("DocumentTabs")
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(QtCore.Qt.TextElideMode.ElideRight)
        wallpaper = WallpaperWidget()
        layout = QtWidgets.QVBoxLayout(wallpaper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.tabs)
        self.setCentralWidget(wallpaper)
        self.tabs.currentChanged.connect(self._activate_tab)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        saved = self.settings.value(self.GEOMETRY_KEY)
        if not (isinstance(saved, QtCore.QByteArray) and self.restoreGeometry(saved)):
            self.resize(self.default_window_size())
        self._initial_size = self.size()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.persist_layout_state)
            # QTableView and plot widgets may consume Ctrl+Tab for focus traversal.
            # Handle it before their key handlers, scoped to this shell's children.
            app.installEventFilter(self)
        if create_empty:
            self.add_tab()

    def paintEvent(self, event):
        paint_wallpaper(self)

    @property
    def active_reduction(self) -> MainWindow | None:
        """The reduction whose menus and status are currently visible."""
        return self.tabs.currentWidget()

    def add_tab(self, reduction: MainWindow | None = None) -> MainWindow:
        """Insert an existing reduction, or create an empty one, and select it."""
        reduction = reduction or self._reduction_factory()
        # Drop the reduction's own Cocoa menu bar while it is still a separate
        # top-level window. Reparenting first attaches that menu bar to the
        # shell's window; destroying it afterwards detaches the shell's menu
        # bar from macOS, which then shows only the application menu.
        reduction.menuBar().setNativeMenuBar(False)
        reduction.setParent(self.tabs)
        reduction.setWindowFlags(QtCore.Qt.WindowType.Widget)
        # Leave room for the shell's menus and tabs at its 760 x 620 minimum,
        # so the reduction's status bar (and its Check for updates button)
        # is not cut off there; larger windows are unaffected.
        reduction.setMinimumSize(760, 540)
        # Recover the tab bar's height from surrounding space, keeping the
        # existing plot and detector canvas sizes on 1280 x 800 screens.
        reduction.centralWidget().layout().setContentsMargins(10, 0, 10, 0)
        reduction.main_panel.layout().setSpacing(6)
        reduction.inspector_panel.layout().setContentsMargins(10, 0, 10, 0)
        reduction.menuBar().hide()
        if not hasattr(reduction, "_studio_menus"):
            reduction._studio_menus = reduction.menuBar().actions()
            # Retain the original menus on the reduction. The shell builds
            # fresh menus because Cocoa cannot move an existing platform menu.
            for action in reduction._studio_menus:
                reduction.menuBar().removeAction(action)
        shortcuts = getattr(reduction, "_studio_shortcuts", None)
        if shortcuts is None:
            shortcuts = {
                action: action.shortcuts()
                for action in reduction.findChildren(QtGui.QAction)
                if action.shortcuts()
            }
            reduction._studio_shortcuts = shortcuts
        self._shortcuts[reduction] = shortcuts
        for action in shortcuts:
            action.setShortcuts([])
        connections = [
            (reduction.new_tab_requested, lambda: self.add_tab()),
            (reduction.new_window_requested, self.new_window_requested.emit),
            (reduction.next_tab_requested, self.next_tab),
            (reduction.previous_tab_requested, self.previous_tab),
            (reduction.close_tab_requested, lambda: self.close_tab(self.tabs.indexOf(reduction))),
            (reduction.move_to_new_window_requested, lambda: self.move_tab_requested.emit(reduction)),
            (reduction.open_project_requested, self.open_project),
            (reduction.open_source_requested, self.open_source),
            (reduction.title_changed, lambda title: self._update_title(reduction, title)),
        ]
        for signal, slot in connections:
            signal.connect(slot)
        self._connections[reduction] = connections
        index = self.tabs.addTab(reduction, "Untitled")
        close = _DocumentCloseButton(self.tabs.tabBar())
        close.clicked.connect(lambda: self.close_tab(self.tabs.indexOf(reduction)))
        self.tabs.tabBar().setTabButton(index, QtWidgets.QTabBar.ButtonPosition.RightSide, close)
        self.tabs.setCurrentIndex(index)
        reduction.update_project_window_title()
        self._activate_tab(index)
        self._update_move_actions()
        return reduction

    def reduction_for_new_content(self) -> MainWindow:
        """Reuse the active tab only when it has no content or running work."""
        reduction = self.active_reduction
        if reduction is not None and (
            not reduction.session.directory
            and reduction.project_path is None
            and reduction._active_worker is None
        ):
            return reduction
        return self.add_tab()

    def open_project(self, path: str) -> MainWindow:
        reduction = self.reduction_for_new_content()
        reduction.load_project_file(path)
        return reduction

    def open_source(self, path: str) -> MainWindow:
        reduction = self.reduction_for_new_content()
        reduction.load_experiment_directory(path)
        return reduction

    def _activate_tab(self, index: int) -> None:
        self.menuBar().clear()
        for menu in self._shown_menus:
            menu.dispose()
        self._shown_menus.clear()
        active = self.active_reduction
        for reduction, shortcuts in self._shortcuts.items():
            for action, sequences in shortcuts.items():
                action.setShortcuts(sequences if reduction is active else [])
        if active is not None:
            for action in active._studio_menus:
                menu = _MirrorMenu(action.menu(), self)
                self._shown_menus.append(menu)
                self.menuBar().addMenu(menu)
            active.update_project_window_title()

    def _update_title(self, reduction: MainWindow, title: str) -> None:
        index = self.tabs.indexOf(reduction)
        if index < 0:
            return
        self.tabs.setTabText(index, title)
        source = reduction.project_path or reduction.session.directory
        if title.startswith("• "):
            self.tabs.tabBar().setAccessibleTabName(index, f"{title[2:]}, unsaved changes")
        else:
            self.tabs.tabBar().setAccessibleTabName(index, title)
        self.tabs.setTabToolTip(index, str(Path(source).resolve()) if source else "")
        if reduction is self.active_reduction:
            self.setWindowTitle(reduction.windowTitle())

    def _update_move_actions(self) -> None:
        for index in range(self.tabs.count()):
            self.tabs.widget(index).move_tab_action.setEnabled(self.tabs.count() > 1)

    def next_tab(self) -> None:
        self._cycle_tab(1)

    def previous_tab(self) -> None:
        self._cycle_tab(-1)

    def _cycle_tab(self, step: int) -> None:
        if self.tabs.count():
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if (
            event.type() in (QtCore.QEvent.Type.ShortcutOverride, QtCore.QEvent.Type.KeyPress)
            and isinstance(watched, QtWidgets.QWidget)
            and (watched is self or self.isAncestorOf(watched))
            and event.key() in (QtCore.Qt.Key.Key_Tab, QtCore.Qt.Key.Key_Backtab)
            # On macOS Qt's MetaModifier is the physical Control key.
            and (
                event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier
                or (sys.platform == "darwin" and event.modifiers() & QtCore.Qt.KeyboardModifier.MetaModifier)
            )
            and not event.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier
            and (sys.platform == "darwin" or not event.modifiers() & QtCore.Qt.KeyboardModifier.MetaModifier)
        ):
            event.accept()
            if event.type() == QtCore.QEvent.Type.KeyPress:
                self._cycle_tab(-1 if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier else 1)
            return True
        return super().eventFilter(watched, event)

    def take_tab(self, reduction: MainWindow) -> MainWindow:
        """Detach a reduction without closing it or altering its session."""
        index = self.tabs.indexOf(reduction)
        if index < 0:
            raise ValueError("The reduction does not belong to this window.")
        for signal, slot in self._connections.pop(reduction):
            signal.disconnect(slot)
        for action in self._shortcuts.pop(reduction):
            action.setShortcuts([])
        # currentChanged must see only the remaining reductions.
        self.tabs.removeTab(index)
        reduction.hide()
        reduction.setParent(None)
        self._activate_tab(self.tabs.currentIndex())
        self._update_move_actions()
        return reduction

    def close_tab(self, index: int) -> bool:
        """Close after Save/Discard approval; keep an empty last tab."""
        reduction = self.tabs.widget(index)
        if reduction is None or not reduction.request_close():
            return False
        self.take_tab(reduction).deleteLater()
        if not self.tabs.count():
            self.add_tab()
        return True

    def confirm_close(self) -> bool:
        """Ask about unsaved tabs without removing any of them."""
        if self._checking_close:
            return False
        self._checking_close = True
        try:
            for index in range(self.tabs.count()):
                reduction = self.tabs.widget(index)
                if reduction._active_worker is not None or reduction.has_unsaved_changes():
                    self.tabs.setCurrentIndex(index)
                if not reduction.request_close():
                    return False
            return True
        finally:
            self._checking_close = False

    def close_confirmed(self) -> None:
        """Close after every Studio window has approved application quit."""
        self._close_confirmed = True
        try:
            self.close()
        finally:
            self._close_confirmed = False

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 - Qt API
        if not self._close_confirmed and not self.confirm_close():
            event.ignore()
            return
        self.persist_layout_state()
        while self.tabs.count():
            self.take_tab(self.tabs.widget(0)).deleteLater()
        event.accept()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.ActivationChange and self.isActiveWindow():
            self.activated.emit()

    def persist_layout_state(self) -> None:
        """Remember one shell geometry; splitter state belongs to each reduction."""
        try:
            self.settings.setValue(self.GEOMETRY_KEY, self.saveGeometry())
        except RuntimeError:
            pass  # Qt may have deleted the shell during application shutdown.

    def default_window_size(self) -> QtCore.QSize:
        """Choose a size that fits the current screen, including laptop screens."""
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return QtCore.QSize(1600, 1000)
        available = screen.availableGeometry()
        return QtCore.QSize(
            min(1800, max(960, int(available.width() * 0.92)), available.width()),
            min(1150, max(680, int(available.height() * 0.90)), available.height()),
        )

    def fit_window_to_screen(self) -> None:
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        width = min(self.width(), available.width() - max(0, frame.width() - self.width()))
        height = min(self.height(), available.height() - max(0, frame.height() - self.height()))
        self.resize(max(width, self.minimumWidth()), max(height, self.minimumHeight()))
        frame = self.frameGeometry()
        self.move(
            min(max(frame.x(), available.left()), available.right() - frame.width() + 1),
            min(max(frame.y(), available.top()), available.bottom() - frame.height() + 1),
        )

    def reset_layout(self) -> None:
        self.settings.remove(self.GEOMETRY_KEY)
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        self.resize(self.default_window_size())
        self.fit_window_to_screen()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not getattr(self, "_first_show_done", False):
            self._first_show_done = True
            if self.size() == self._initial_size:
                self.fit_window_to_screen()
