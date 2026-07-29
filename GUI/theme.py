from __future__ import annotations

import sys
import os
from dataclasses import dataclass

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from PySide6 import QtCore, QtGui, QtWidgets

import pyqtgraph as pg


@dataclass(frozen=True)
class ThemeSpec:
    name: str
    window: str
    window_alt: str
    surface: str
    surface_soft: str
    input_bg: str
    input_border: str
    text: str
    muted: str
    accent: str
    accent_soft: str
    accent_hover: str
    accent_strong: str
    success: str
    success_soft: str
    plot_background: str
    axis_text: str
    axis_line: str
    grid_alpha: float


THEMES: dict[str, ThemeSpec] = {
    "light": ThemeSpec(
        name="light",
        window="#edf3fb",
        window_alt="#f5f9ff",
        surface="#ffffff",
        surface_soft="#f6faff",
        input_bg="#ffffff",
        input_border="#bfd0e3",
        text="#102238",
        muted="#4f6783",
        accent="#1f63cf",
        accent_soft="rgba(31, 99, 207, 0.10)",
        accent_hover="rgba(31, 99, 207, 0.18)",
        accent_strong="#0f55bf",
        success="#19836d",
        success_soft="rgba(25, 131, 109, 0.12)",
        plot_background="#ffffff",
        axis_text="#243d5a",
        axis_line="#a0b8cc",
        grid_alpha=0.35,
    ),
    "dark": ThemeSpec(
        name="dark",
        window="#07111c",
        window_alt="#0a1726",
        surface="#0d1c2d",
        surface_soft="#11263a",
        input_bg="#091523",
        input_border="#2a4159",
        text="#eaf2ff",
        muted="#90a6c1",
        accent="#69b4ff",
        accent_soft="rgba(105, 180, 255, 0.12)",
        accent_hover="rgba(105, 180, 255, 0.20)",
        accent_strong="#8bd7ff",
        success="#7ef7c9",
        success_soft="rgba(126, 247, 201, 0.12)",
        plot_background="#06111d",
        axis_text="#edf4ff",
        axis_line="#6d8298",
        grid_alpha=0.13,
    ),
}


def build_application(argv: list[str] | None = None, theme_mode: str = "light") -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or sys.argv)
    app.setApplicationName("UPbRPy Studio")
    app.setOrganizationName("UPbRPy")
    app.setStyle("Fusion")
    app.setFont(_pick_font(app))
    pg.setConfigOptions(antialias=True)
    apply_theme(app, theme_mode)
    return app


def resolve_theme_name(app: QtWidgets.QApplication, theme_mode: str) -> str:
    if theme_mode == "system":
        return system_theme_name(app)
    return theme_mode if theme_mode in THEMES else "light"


def system_theme_name(app: QtWidgets.QApplication) -> str:
    try:
        color_scheme = app.styleHints().colorScheme()
    except AttributeError:
        return "light"
    return "dark" if color_scheme == QtCore.Qt.ColorScheme.Dark else "light"


def active_theme(app: QtWidgets.QApplication | None = None) -> ThemeSpec:
    resolved_name = "light"
    application = app or QtWidgets.QApplication.instance()
    if application is not None:
        resolved_name = str(application.property("upbrpy.resolved_theme") or resolved_name)
    return THEMES.get(resolved_name, THEMES["light"])


def apply_theme(app: QtWidgets.QApplication, theme_mode: str) -> ThemeSpec:
    resolved_name = resolve_theme_name(app, theme_mode)
    theme = THEMES[resolved_name]
    app.setPalette(_build_palette(theme))
    app.setStyleSheet(_stylesheet(theme))
    app.setProperty("upbrpy.theme_mode", theme_mode)
    app.setProperty("upbrpy.resolved_theme", resolved_name)
    return theme


def _pick_font(app: QtWidgets.QApplication) -> QtGui.QFont:
    database = QtGui.QFontDatabase()
    for family in ["Avenir Next", "Helvetica Neue", "Segoe UI", "Arial"]:
        if family in database.families():
            font = QtGui.QFont(family)
            font.setPointSizeF(10.5)
            return font
    font = app.font()
    font.setPointSizeF(10.5)
    return font


def _build_palette(theme: ThemeSpec) -> QtGui.QPalette:
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(theme.window))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(theme.input_bg))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(theme.surface_soft))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(theme.text))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(theme.text))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(theme.text))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(theme.surface))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(theme.accent))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(theme.surface))
    return palette


def _stylesheet(theme: ThemeSpec) -> str:
    scroll_track = "rgba(106, 128, 152, 0.12)" if theme.name == "light" else "rgba(173, 196, 219, 0.10)"
    scroll_handle = "rgba(79, 103, 131, 0.42)" if theme.name == "light" else "rgba(173, 196, 219, 0.34)"
    scroll_handle_hover = "rgba(52, 82, 117, 0.62)" if theme.name == "light" else "rgba(210, 228, 247, 0.52)"
    combo_popup_hover = "rgba(31, 99, 207, 0.14)" if theme.name == "light" else theme.accent_soft
    combo_popup_selected = "rgba(31, 99, 207, 0.20)" if theme.name == "light" else theme.accent_hover
    combo_popup_selected_text = "#123457" if theme.name == "light" else theme.text
    return f"""
    QWidget {{
        color: {theme.text};
    }}
    QLabel {{
        background: transparent;
    }}
    QMainWindow {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                    stop:0 {theme.window}, stop:1 {theme.window_alt});
    }}
    QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{
        background: transparent;
        border: none;
    }}
    QMenuBar, QStatusBar {{
        background: {theme.window_alt};
        border-bottom: 1px solid {theme.input_border};
    }}
    QMenuBar::item {{
        padding: 6px 10px;
        background: transparent;
    }}
    QMenuBar::item:selected, QMenu {{
        background: {theme.surface};
    }}
    QMenu::item:selected {{
        background: {theme.accent_soft};
    }}
    #SidebarPanel, #WorkflowRail, #InspectorPanel, #HeaderCard, #PlotCard, #MetricCard, #NotesCard, #SessionCard, #WorkflowCard, #SelectorCard, #WorkflowHintCard, #WorkflowActionsCard, #WorkflowContextBar {{
        background: {theme.surface};
        border: 1px solid {theme.input_border};
        border-radius: 20px;
    }}
    #WorkflowRail {{
        border-radius: 16px;
    }}
    #WorkflowContextBar {{
        background: {theme.surface_soft};
        border-radius: 14px;
    }}
    #WorkflowActionsInline {{
        background: transparent;
        border: none;
    }}
    #WorkflowCard {{
        background: {theme.surface};
        border: 2px solid {theme.accent};
        border-radius: 20px;
    }}
    #WorkflowHintCard, #WorkflowActionsCard {{
        background: {theme.surface_soft};
        border-radius: 16px;
    }}
    #IsotopeElemLabel {{
        color: {theme.muted};
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
    }}
    #IsotopeSectionLabel {{
        color: {theme.muted};
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }}
    #IsotopeSectionLine {{
        background: {theme.input_border};
    }}
    #ChannelsCard {{
        background: {theme.input_bg};
        border: 1px solid {theme.input_border};
        border-radius: 14px;
    }}
    #PlotControlBar {{
        background: {theme.surface_soft};
        border: 1px solid {theme.input_border};
        border-radius: 12px;
    }}
    #ControlBarDivider {{
        color: {theme.axis_line};
        max-width: 1px;
        margin: 0px 2px;
    }}
    #ControlBarLabel {{
        color: {theme.muted};
        font-size: 12px;
        font-weight: 600;
    }}
    #Brand {{
        font-size: 24px;
        font-weight: 700;
        letter-spacing: 0.03em;
    }}
    #RailBrand {{
        font-size: 18px;
        font-weight: 750;
        letter-spacing: 0.02em;
    }}
    #HeroTitle {{
        font-size: 28px;
        font-weight: 700;
    }}
    #SectionTitle, #MetricLabel, #DetailLabel {{
        color: {theme.muted};
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.08em;
    }}
    #WorkflowLabel {{
        color: {theme.text};
        font-size: 12px;
        font-weight: 700;
    }}
    #WorkflowContextTitle {{
        color: {theme.text};
        font-size: 13px;
        font-weight: 700;
    }}
    #WorkflowStatusCompact {{
        color: {theme.muted};
        font-size: 10px;
    }}
    #MetricValue {{
        font-size: 22px;
        font-weight: 700;
    }}
    #SecondaryText {{
        color: {theme.muted};
    }}
    #PanelBody {{
        color: {theme.muted};
        font-size: 12px;
        line-height: 1.5;
    }}
    #Pill, #StatusPill {{
        padding: 6px 12px;
        border-radius: 999px;
        background: {theme.accent_soft};
        border: 1px solid {theme.input_border};
    }}
    #StatusPill {{
        background: {theme.success_soft};
        color: {theme.success};
    }}
    QPushButton, QToolButton[modeToggle="true"] {{
        background: {theme.accent_soft};
        border: 1px solid {theme.input_border};
        border-radius: 12px;
        padding: 10px 14px;
        font-weight: 600;
    }}
    QPushButton:hover, QToolButton[modeToggle="true"]:hover {{
        background: {theme.accent_hover};
    }}
    QPushButton:pressed, QToolButton[modeToggle="true"]:pressed {{
        background: {theme.accent_hover};
    }}
    QPushButton[accent="true"], QToolButton[modeToggle="true"]:checked {{
        background: {theme.accent};
        color: #ffffff;
        border-color: {theme.accent_strong};
    }}
    QPushButton:disabled, QToolButton[modeToggle="true"]:disabled {{
        background: transparent;
        color: {theme.muted};
        border-color: {theme.input_border};
    }}
    QListWidget, QTextEdit, QTextBrowser {{
        background: {theme.input_bg};
        border: 1px solid {theme.input_border};
        border-radius: 14px;
        padding: 8px;
    }}
    QComboBox {{
        combobox-popup: 0;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                    stop:0 {theme.surface}, stop:1 {theme.input_bg});
        border: 1px solid {theme.input_border};
        border-radius: 10px;
        padding: 6px 30px 6px 10px;
        min-height: 24px;
        font-weight: 500;
    }}
    QComboBox:hover {{
        border-color: {theme.accent};
        background: {theme.surface};
    }}
    QComboBox:focus {{
        border-color: {theme.accent_strong};
        background: {theme.surface};
    }}
    QTreeWidget {{
        background: {theme.input_bg};
        border: 1px solid {theme.input_border};
        border-radius: 14px;
        padding: 4px;
    }}
    QTreeWidget::item {{
        padding: 5px 4px;
        border-radius: 6px;
    }}
    QTreeWidget::item:selected {{
        background: {theme.accent_soft};
        color: {theme.text};
    }}
    QTextEdit, QTextBrowser {{
        selection-background-color: {theme.accent_soft};
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        border: none;
        border-left: 1px solid {theme.input_border};
        width: 24px;
        margin: 2px 3px 2px 0;
        border-top-right-radius: 8px;
        border-bottom-right-radius: 8px;
        background: {theme.surface_soft};
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0;
        height: 0;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 6px solid {theme.muted};
    }}
    QComboBoxPrivateContainer {{
        background: transparent;
        border: none;
        padding: 0px;
    }}
    QComboBox QAbstractItemView {{
        background: {theme.surface};
        border: 1px solid {theme.input_border};
        border-radius: 12px;
        padding: 6px;
        selection-background-color: {combo_popup_selected};
        selection-color: {combo_popup_selected_text};
        color: {theme.text};
        outline: 0;
    }}
    QComboBox QAbstractItemView::item {{
        padding: 8px 10px;
        border-radius: 8px;
        min-height: 20px;
        color: {theme.text};
        background-color: {theme.surface};
    }}
    QComboBox QAbstractItemView::item:hover {{
        background-color: {combo_popup_hover};
        color: {combo_popup_selected_text};
    }}
    QComboBox QAbstractItemView::item:selected {{
        background-color: {combo_popup_selected};
        color: {combo_popup_selected_text};
    }}
    QComboBox QAbstractItemView::item:selected:active {{
        background-color: {combo_popup_selected};
        color: {combo_popup_selected_text};
    }}
    QComboBox QAbstractItemView::item:selected:!active {{
        background-color: {combo_popup_selected};
        color: {combo_popup_selected_text};
    }}
    QListWidget::item {{
        padding: 6px 4px;
        border-radius: 8px;
    }}
    #WorkflowStepList {{
        border-radius: 16px;
        padding: 10px;
        font-size: 13px;
        font-weight: 600;
    }}
    #WorkflowStepList::item {{
        padding: 10px 10px;
        margin: 2px 0px;
        border-radius: 12px;
    }}
    #WorkflowStepRail {{
        background: transparent;
        border: none;
        border-radius: 10px;
        padding: 2px;
        font-size: 12px;
        font-weight: 650;
        outline: none;
    }}
    #WorkflowStepRail::item {{
        padding: 4px 5px;
        margin: 1px 0px;
        border-radius: 9px;
    }}
    #WorkflowStepRail::item:selected {{
        background: {theme.accent_soft};
        color: {theme.text};
    }}
    QListWidget::item:selected {{
        background: {theme.accent_soft};
        color: {theme.text};
    }}
    QAbstractItemView::item:selected {{
        color: {theme.text};
    }}
    QToolTip {{
        color: {theme.text};
        background: {theme.surface};
        border: 1px solid {theme.input_border};
        padding: 6px 8px;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 14px;
        margin: 6px 4px 6px 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 14px;
        margin: 0 6px 4px 6px;
    }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: {scroll_handle};
        border-radius: 7px;
        min-height: 36px;
        min-width: 36px;
    }}
    QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
        background: {scroll_handle_hover};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        background: transparent;
        border: none;
        width: 0px;
        height: 0px;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: {scroll_track};
        border-radius: 7px;
    }}
    """
