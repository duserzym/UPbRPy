"""Concordia options stay compact with either internal or external choosing."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ResultsTableWidget
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture
def finalized():
    ages = np.linspace(490., 510., 40)
    x, y = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    frame = pd.DataFrame({
        "spot_name": [f"A_{i}" for i in range(40)], "sample": "A",
        "207Pb/235U": x, "206Pb/238U": y,
        "238U/206Pb": tx, "207Pb/206Pb": ty,
    })
    for ratio in ("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"):
        frame[f"{ratio}_2e_with_calibration"] = frame[ratio] * .02
        frame[f"{ratio}_2e_no_calibration"] = frame[ratio] * .01
    return SimpleNamespace(parallel_results=frame, selected_results=frame.copy())


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def rect_in(widget, parent):
    return QtCore.QRect(widget.mapTo(parent, QtCore.QPoint()), widget.size())


def assert_options_layout(review):
    page = review.spot_concordia_page
    viewport = page.viewport()
    panel = page.findChild(QtWidgets.QWidget, "ConcordiaOptions")
    captions = {label.text(): label for label in panel.findChildren(QtWidgets.QLabel)
                if label.objectName() == "ConcordiaOptionLabel"}
    rows = (("Diagram", review.spot_diagram_buttons),
            ("Colour", review.spot_colour_buttons),
            ("Mouse", review.mouse_mode_buttons))
    assert set(captions) == {name for name, _ in rows}
    caption_rects = [rect_in(captions[name], panel) for name, _ in rows]
    assert len({rect.x() for rect in caption_rects}) == 1
    assert len({rect.width() for rect in caption_rects}) == 1
    previous_bottom = None
    for name, buttons in rows:
        caption = captions[name]
        assert caption.width() >= caption.sizeHint().width()
        button_rects = [rect_in(button, panel) for button in buttons.values()]
        assert all(not rect_in(caption, panel).intersects(rect) for rect in button_rects)
        assert min(rect.left() for rect in button_rects) - rect_in(caption, panel).right() - 1 >= 6
        widths = [rect.width() for rect in button_rects]
        assert max(widths) - min(widths) <= 2
        top = min(rect.top() for rect in button_rects)
        if previous_bottom is not None:
            assert 0 <= top - previous_bottom - 1 <= 12
        previous_bottom = max(rect.bottom() for rect in button_rects)
    assert panel.width() <= 230
    plot = review.spot_concordia
    assert plot.width() >= .6 * (panel.width() + plot.width())
    assert page.verticalScrollBar().maximum() == 0
    assert page.horizontalScrollBar().maximum() == 0
    for widget in [panel, *panel.findChildren(QtWidgets.QWidget)]:
        if widget.isVisible():
            assert viewport.rect().contains(rect_in(widget, viewport)), widget.objectName()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_studio_concordia_options_at_laptop_sizes(app, finalized, theme):
    from GUI.studio_window import StudioWindow

    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    shell = StudioWindow(theme_mode=theme)
    window = shell.active_reduction
    try:
        review = window.results_table
        review.show_results(finalized)
        window.plots_stack.setCurrentWidget(review)
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        shell.show()
        for width, height in ((1280, 800), (1440, 900)):
            shell.resize(width, height)
            window.apply_default_layout()
            for _ in range(5):
                app.processEvents()
            assert review.spot_summary_button.isVisible()
            assert not review.spot_chooser_section.isVisible()
            assert_options_layout(review)
            # Selecting a spot reveals the card without moving the option rows.
            review.set_selected_spots({"A_1"})
            for _ in range(5):
                app.processEvents()
            assert review.spot_card.isVisible()
            assert review.spot_card.height() >= 80
            assert_options_layout(review)
            review.set_selected_spots(set())
    finally:
        window.has_unsaved_changes = lambda: False
        dispose(shell, app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)


def test_standalone_concordia_chooser_keeps_spare_height(app, finalized, tmp_path):
    settings = QtCore.QSettings(str(tmp_path / "review.ini"), QtCore.QSettings.Format.IniFormat)
    review = ResultsTableWidget(settings=settings)
    try:
        review.apply_theme(THEMES["light"])
        review.show_results(finalized)
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        review.resize(1000, 550)
        review.show()
        for external in (False, True, False):
            review.set_external_chooser(external)
            for _ in range(5):
                app.processEvents()
            assert_options_layout(review)
            assert review.spot_chooser_section.isVisible() == (not external)
            if not external:
                assert review.spot_choice_list.height() > 150
        review.tabs.setCurrentWidget(review.depth_concordia_page)
        app.processEvents()
        widths = [button.width() for button in review.depth_diagram_buttons.values()]
        assert max(widths) - min(widths) <= 2
    finally:
        dispose(review, app)
