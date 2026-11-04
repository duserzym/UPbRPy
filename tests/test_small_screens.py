"""UPbRPy Studio on laptop screens (14-inch MacBook: about 1440 x 900 usable).

At 1440 x 900 and 1280 x 800 the window keeps the requested size, no
visible widget is smaller than the minimum its layout asks for (on every
plot page and inspector tab), and the trace editor and result tables keep
a usable area. Window geometry and splitter positions are remembered, and
View > Reset Layout restores a layout that fits the screen.
"""

from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from synthetic_bundles import write_raw_directory  # noqa: E402


SIZES = [(1440, 900), (1280, 800)]


@pytest.fixture(scope="module")
def app():
    from GUI.theme import build_application

    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


@pytest.fixture()
def settings_reset(app):
    settings = QtCore.QSettings("UPbRPy", "Studio")
    settings.clear()
    yield settings
    settings.clear()


def process(app, rounds=8):
    for _ in range(rounds):
        app.processEvents()


def effective_minimum(widget) -> tuple[int, int]:
    """The size Qt layouts guarantee a widget (explicit minimum, else hint)."""
    hint = widget.minimumSizeHint()
    explicit = widget.minimumSize()
    policy = widget.sizePolicy()
    ignored = QtWidgets.QSizePolicy.Policy.Ignored
    width = explicit.width() if explicit.width() > 0 else (
        0 if policy.horizontalPolicy() == ignored else hint.width()
    )
    height = explicit.height() if explicit.height() > 0 else (
        0 if policy.verticalPolicy() == ignored else hint.height()
    )
    return width, height


def clipped_widgets(window) -> list[tuple]:
    """Visible widgets squeezed below their minimum (fixed-size ones aside)."""
    collapsed = [
        splitter.widget(index)
        for splitter in window.findChildren(QtWidgets.QSplitter)
        for index, size in enumerate(splitter.sizes())
        if size == 0
    ]
    found = []
    for widget in window.findChildren(QtWidgets.QWidget):
        if widget.isWindow() or not widget.isVisibleTo(window):
            continue
        if widget.minimumSize() == widget.maximumSize():
            continue
        if isinstance(widget, QtWidgets.QHeaderView) and widget.count() == 0:
            continue  # an empty table's header has no height by design
        if any(widget is pane or pane.isAncestorOf(widget) or widget.isAncestorOf(pane) for pane in collapsed):
            continue
        need_w, need_h = effective_minimum(widget)
        if widget.width() + 1 < need_w or widget.height() + 1 < need_h:
            found.append((type(widget).__name__, widget.objectName(), widget.width(), widget.height(), need_w, need_h))
    return found


@pytest.fixture()
def loaded_window(app, settings_reset, tmp_path, monkeypatch):
    from GUI.main_window import MainWindow

    monkeypatch.setattr("UPbRPy.io.auto_select_baseline_peak", lambda experiment, *a, **k: experiment.assign(
        baseline_start_times=0.0, baseline_end_times=4.0,
        plateau_start_times=8.0, plateau_end_times=16.0,
        signal_start_times=6.0, signal_end_times=18.0,
    ))
    raw = tmp_path / "Apatite_synthetic"
    write_raw_directory(raw)
    window = MainWindow()
    window.show()
    window.load_experiment_directory(str(raw), background=False)
    process(app)
    yield window
    window.has_unsaved_changes = lambda: False
    window.close()
    process(app)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_every_page_fits_laptop_sizes(app, loaded_window, size):
    window = loaded_window
    assert window.session.loaded
    window.resize(*size)
    window.apply_default_layout()
    process(app)
    assert (window.width(), window.height()) == size
    hint = window.minimumSizeHint()
    assert hint.width() <= size[0] and hint.height() <= size[1], hint
    problems = {}
    for page in range(window.plots_stack.count()):
        window.plots_stack.setCurrentIndex(page)
        for tab in range(window.inspector_tabs.count()):
            window.inspector_tabs.setCurrentIndex(tab)
            process(app, 3)
            for item in clipped_widgets(window):
                problems.setdefault(item, (page, tab))
    assert not problems, problems


@pytest.mark.parametrize("size, trace_height", [((1440, 900), 300), ((1280, 800), 240)])
def test_trace_editor_and_results_keep_working_room(app, loaded_window, size, trace_height):
    window = loaded_window
    window.resize(*size)
    window.apply_default_layout()
    window.plots_stack.setCurrentIndex(0)
    process(app)
    assert window.trace_plot.height() >= trace_height
    assert window.trace_plot.width() >= 600
    assert window.sidebar_panel.isVisible() and window.inspector_panel.isVisible()
    window.plots_stack.setCurrentIndex(window.plots_stack.indexOf(window.results_table))
    process(app)
    table = window.results_table.results_table
    assert table.height() >= 250 and table.width() >= 600


def test_notes_log_collapses_on_short_screens_and_toggles(app, loaded_window):
    window = loaded_window
    window.resize(1280, 800)
    window.apply_default_layout()
    process(app)
    assert not window.notes_log_visible()
    assert not window.notes_log_action.isChecked()
    window.notes_log_action.trigger()
    process(app)
    assert window.notes_log_visible() and window.lower_container.isVisible()
    window.notes_log_action.trigger()
    process(app)
    assert not window.lower_container.isVisible()
    window.resize(1600, 1000)
    window.apply_default_layout()
    process(app)
    assert window.notes_log_visible()


def test_geometry_and_splitters_are_remembered_and_reset(app, settings_reset):
    from GUI.main_window import MainWindow

    first = MainWindow()
    first.show()
    first.resize(700 + 60, 640)
    first.resize(780, 660)
    process(app)
    first.workspace_splitter.setSizes([250, 300, 210])
    first.set_notes_log_visible(True)
    process(app)
    saved_sizes = first.workspace_splitter.sizes()
    first.persist_layout_state()
    first.has_unsaved_changes = lambda: False
    first.close()
    process(app)

    second = MainWindow()
    second.show()
    process(app)
    assert (second.width(), second.height()) == (780, 660)
    assert second.workspace_splitter.sizes() == saved_sizes
    assert second.notes_log_visible()

    second.set_workspace_panel_visible("inspector", False)
    second.reset_layout()
    process(app)
    assert second.inspector_panel.isVisible() and second.sidebar_panel.isVisible()
    assert not settings_reset.contains("window_geometry_v1")
    # The offscreen screen is 800 x 800: the reset window fits on it.
    screen = second.screen().availableGeometry()
    assert second.width() <= screen.width() and second.height() <= screen.height()
    second.has_unsaved_changes = lambda: False
    second.close()
    process(app)


def test_saved_geometry_larger_than_the_screen_is_clamped(app, settings_reset):
    from GUI.main_window import MainWindow

    big = MainWindow()  # e.g. last used on a large external monitor
    big.resize(2400, 1600)
    big.persist_layout_state()
    big.deleteLater()
    process(app)

    window = MainWindow()  # Qt fits restored geometry to this screen
    window.show()
    process(app)
    screen = window.screen().availableGeometry()
    assert window.width() <= screen.width() and window.height() <= screen.height()
    window.has_unsaved_changes = lambda: False
    window.close()
    process(app)
