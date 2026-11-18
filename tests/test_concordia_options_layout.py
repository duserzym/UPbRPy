"""Concordia options stay compact with either internal or external choosing."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets

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


def assert_options_layout(review, kind="spot"):
    page = getattr(review, kind + "_concordia_page")
    assert page.findChild(QtWidgets.QWidget, "ConcordiaOptions") is None
    toolbar = getattr(review, kind + "_toolbar")
    for widget, menu, content, _priority in toolbar.entries:
        if widget.parentWidget() is toolbar and widget.isVisible():
            assert toolbar.rect().contains(rect_in(widget, toolbar))
            for button in widget.findChildren(QtWidgets.QToolButton):
                if button.isVisibleTo(toolbar):
                    assert toolbar.rect().contains(rect_in(button, toolbar))
    assert page.verticalScrollBar().maximum() == 0
    assert page.horizontalScrollBar().maximum() == 0
    status = getattr(review, kind + "_status_line")
    assert page.viewport().rect().contains(rect_in(status, page.viewport()))
    children = [child for child in status.findChildren(QtWidgets.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
                if child.isVisible()]
    for i, child in enumerate(children):
        assert all(not child.geometry().intersects(other.geometry()) for other in children[i+1:])
    if kind == "spot" and review.spot_card.isVisible():
        assert review.spot_concordia.rect().contains(review.spot_card.geometry())


def assert_depth_layout(review, app):
    review.tabs.setCurrentWidget(review.depth_concordia_page)
    for _ in range(3): app.processEvents()
    assert_options_layout(review, "depth")
    review.tabs.setCurrentWidget(review.spot_concordia_page)
    for _ in range(3): app.processEvents()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_studio_concordia_options_at_laptop_sizes(app, finalized, theme, size, tmp_path, monkeypatch):
    from GUI.main_window import MainWindow
    from GUI import main_window
    settings = QtCore.QSettings(str(tmp_path / "viewer.ini"), QtCore.QSettings.Format.IniFormat)
    monkeypatch.setattr(QtCore, "QSettings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow(theme_mode=theme)
    try:
        review = window.results_table
        review.show_results(finalized)
        review.setEnabled(True)
        window.session.plot_mode = "final_results"
        window.plots_stack.setCurrentWidget(review)
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        window.show()
        window.resize(*size)
        for _ in range(8): app.processEvents()
        assert not review.spot_spots_button.isVisible()
        assert all(menu.title() != "Session" for _, menu, _, _ in review.spot_toolbar.entries)
        if size[0] == 1440:
            assert not review.spot_toolbar.overflow.isVisible()
        else:
            assert review.summary_label._compact
            assert QtWidgets.QToolButton.text(review.summary_label) == "ⓘ"
        bar = review.tabs.tabBar()
        assert all(bar.tabRect(i).width() >= bar.tabSizeHint(i).width() for i in range(bar.count()))
        assert_options_layout(review)
        assert_depth_layout(review, app)
        assert window.plot_focus_action.shortcut() == QtGui.QKeySequence("Ctrl+Shift+F")
        normal = tuple(window.workspace_splitter.sizes())
        dimensions = [tuple((review.spot_concordia.width(), review.spot_concordia.height()))]
        review.set_selected_spots({"A_1"})
        for _ in range(3): app.processEvents()
        assert review.spot_card.isVisible()
        assert_options_layout(review)
        review.spot_card.close_button.click()
        review._update_spot_card()
        assert not review.spot_card.isVisible()
        review.set_selected_spots({"A_2"})
        for _ in range(3): app.processEvents()
        assert review.spot_card.isVisible()
        stage_visible = window.stage_segment_bar.isVisible()
        window.set_plot_focus(True)
        for _ in range(8): app.processEvents()
        assert stage_visible and window.stage_segment_bar.isVisible()
        assert review.tabs.tabBar().isVisible()
        assert window.workspace_splitter.sizes()[::2] == [26, 26]
        assert not review.spot_toolbar.overflow.isVisible()
        assert_options_layout(review)
        assert_depth_layout(review, app)
        dimensions.append((review.spot_concordia.width(), review.spot_concordia.height()))
        print(size, theme, "unfolded/focused plot sizes", dimensions)
        expected = {1280: [(755, 553), (1158, 553)],
                    1440: [(885, 653), (1318, 653)]}
        assert dimensions == expected[size[0]]
        window.sigma_level = 1
        window._update_sigma_button_text()
        assert all(button.text() == "1σ" and not button.isChecked() for button in review._sigma_buttons)
        window.sigma_level = 2
        window._update_sigma_button_text()
        window.resize(size[0]+10, size[1])
        for _ in range(3): app.processEvents()
        assert window.workspace_splitter.sizes()[::2] == [26, 26]
        window.resize(*size)
        for _ in range(3): app.processEvents()
        window.set_plot_focus(False)
        for _ in range(8): app.processEvents()
        assert tuple(window.workspace_splitter.sizes()) == normal
        assert review.spot_toolbar.overflow.isVisible() == (size[0] == 1280)
        window.set_plot_focus(True)
        window._unfold_focus_panel("sidebar")
        assert window._focus_folded == {"inspector"}
        assert window.inspector_action.isChecked() and not window.inspector_panel.isVisible()
        assert '"inspector"' in settings.value("workspace_focus_v1")
    finally:
        window.has_unsaved_changes = lambda: False
        dispose(window, app)
    restored = MainWindow(theme_mode=theme)
    try:
        restored.show()
        for _ in range(8): app.processEvents()
        assert restored._focus_folded == {"inspector"}
        restored.set_plot_focus(False)
        assert not restored._focus_folded
    finally:
        dispose(restored, app)


def test_standalone_concordia_popovers_contain_controls(app, finalized):
    review = ResultsTableWidget()
    try:
        review.apply_theme(THEMES["light"])
        review.show_results(finalized)
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        review.resize(1000, 550)
        review.show()
        for _ in range(5): app.processEvents()
        assert_options_layout(review)
        assert review.spot_spots_button.menu().isAncestorOf(review.spot_choice_list)
        for kind in ("spot", "depth"):
            samples = getattr(review, kind + "_sample_chips")
            assert samples.menu_button.menu().isAncestorOf(samples)
            menu = getattr(review, kind + "_fit_button").menu()
            panel = review.fit_panels[kind]
            assert menu.isAncestorOf(panel.fit_button)
            assert menu.isAncestorOf(panel.model_combo)
            assert menu.isAncestorOf(panel.seed)
            panel.fit_button.setChecked(True)
            assert getattr(review, kind + "_fit_button").isChecked()
            panel.fit_button.setChecked(False)
            assert not getattr(review, kind + "_fit_button").isChecked()
            menu.popup(review.mapToGlobal(QtCore.QPoint(10, 10)))
            app.processEvents()
            assert panel.model_combo.isVisible()
            menu.hide()
        menu = review.spot_spots_button.menu()
        menu.popup(review.mapToGlobal(QtCore.QPoint(10, 10)))
        app.processEvents()
        assert review.spot_choice_list.isVisible()
        menu.hide()
        review.set_external_chooser(True)
        app.processEvents()
        assert not review.spot_spots_button.isVisible()
    finally:
        dispose(review, app)


def test_focus_retains_compact_layout_and_hidden_panel_state(app, tmp_path, monkeypatch):
    from GUI.main_window import MainWindow
    from GUI import main_window
    settings = QtCore.QSettings(str(tmp_path / "compact.ini"), QtCore.QSettings.Format.IniFormat)
    monkeypatch.setattr(QtCore, "QSettings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = MainWindow()
    try:
        window.show()
        window.resize(960, 800)
        for _ in range(8): app.processEvents()
        assert window._width_mode == "compact"
        assert window.workspace_splitter.count() == 2
        before = window.workspace_splitter.sizes()
        window.set_plot_focus(True)
        for _ in range(3): app.processEvents()
        window.set_plot_focus(False)
        for _ in range(3): app.processEvents()
        assert window._width_mode == "compact"
        assert window.workspace_splitter.count() == 2
        assert window.workspace_splitter.sizes() == before
        window.resize(1280, 800)
        for _ in range(8): app.processEvents()
        assert window.workspace_splitter.count() == 3
        window.set_workspace_panel_visible("inspector", False)
        window.set_plot_focus(True)
        assert window._focus_strips["inspector"].isHidden()
        window.set_plot_focus(False)
        assert window.inspector_panel.isHidden()
        assert not window.inspector_action.isChecked()
        assert window.sidebar_panel.isVisible()
        window.set_workspace_panel_visible("inspector", True)
        window.set_plot_focus(True)
        window.set_workspace_panel_visible("sidebar", False)
        window.set_plot_focus(False)
        assert window.sidebar_panel.isHidden()
        assert not window.sidebar_action.isChecked()
        assert window.inspector_panel.isVisible()
    finally:
        dispose(window, app)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_compact_card_size_corner_stability_and_saved_disclosure(app, finalized, tmp_path, theme):
    settings = QtCore.QSettings(str(tmp_path / "card.ini"), QtCore.QSettings.Format.IniFormat)
    review = ResultsTableWidget(settings=settings)
    for system in ("206Pb/238U", "207Pb/235U", "207Pb/206Pb"):
        finalized.selected_results[system + "_age"] = 1050.
        finalized.selected_results[system + "_age_2e"] = 110.
    finalized.selected_results["disc%"] = 5.
    try:
        review.apply_theme(THEMES[theme])
        review.show_results(finalized)
        review.set_spot_acquisition({"A_1": (7, "12:34")})
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        review.resize(900, 650)
        review.show()
        for _ in range(5): app.processEvents()
        plot, card = review.spot_concordia, review.spot_card
        point = next(p for p in plot._points if p[0] == "A_1")
        vb = plot._plot_item.vb
        for right, top in ((True, True), (False, True), (True, False), (False, False)):
            review.set_selected_spots(set())
            app.processEvents()
            vb.setRange(xRange=(point[1] - (3 if right else 1), point[1] + (1 if right else 3)),
                        yRange=(point[2] - (3 if top else 1), point[2] + (1 if top else 3)), padding=0)
            review.set_selected_spots({"A_1"})
            for _ in range(3): app.processEvents()
            assert card.width() == 240 and card.height() == card.sizeHint().height()
            assert not card.more_button.isChecked() and not card.scroll.isVisible()
            assert "1050 ± 110 Ma (2σ)" in card.compact_body.text()
            assert card.compact_body.text().count("<tr>") == 3
            assert "Discordance" in card.body.text()
            assert "\n" not in card.header.text()
            for widget in (card, card.header, card.compact_body):
                assert widget.font() == review.spot_age_label.font()
            assert card.more_button.font().pointSizeF() == review.spot_age_label.font().pointSizeF()
            assert card.compact_body.y() - card.header.geometry().bottom() <= 2
            assert card.compact_body.width() >= card.compact_body.sizeHint().width()
            centre = plot.mapFromScene(vb.mapViewToScene(QtCore.QPointF(point[1], point[2])))
            assert not card.geometry().contains(centre)
            assert (card.geometry().center().x() < plot.width()/2) == right
            assert (card.geometry().center().y() > plot.height()/2) == top
            before = card.geometry()
            # A fit/readout redraw or panning with the same selection stays put.
            vb.translateBy(x=2, y=2)
            review._update_spot_card()
            assert card.geometry() == before
        vb.setRange(xRange=(point[1] - 3, point[1] + 1),
                    yRange=(point[2] - 3, point[2] + 1), padding=0)
        review.resize(1000, 700)
        for _ in range(5): app.processEvents()
        assert plot.rect().contains(card.geometry())
        assert card.geometry().center().x() < plot.width()/2
        assert card.geometry().center().y() > plot.height()/2
        card.more_button.click()
        app.processEvents()
        assert card.scroll.isVisible() and card.size() == QtCore.QSize(300, 260)
        assert settings.value("review_spot_card_expanded_v1", type=bool)
        other = ResultsTableWidget(settings=settings)
        try:
            assert other.spot_card.more_button.isChecked()
            other.spot_card.set_expanded(False)
            assert not settings.value("review_spot_card_expanded_v1", type=bool)
        finally:
            dispose(other, app)
    finally:
        dispose(review, app)


@pytest.mark.parametrize("size", [(755, 516), (885, 616), (1158, 516), (1318, 616)])
def test_fixture_card_avoids_legend_and_selected_ellipse(app, size):
    from scripts.shot_concordia_layout import fixture_preview
    finalized, _depth = fixture_preview()
    review = ResultsTableWidget()
    try:
        review.apply_theme(THEMES["light"])
        review.show_results(finalized)
        review.tabs.setCurrentWidget(review.spot_concordia_page)
        review.show()
        for _ in range(5): app.processEvents()
        plot, card = review.spot_concordia, review.spot_card
        # Isolate the canvas at the sizes used by the screenshot script.
        plot.setParent(None)
        plot.resize(*size)
        plot.show()
        for _ in range(5): app.processEvents()
        assert plot._legend is not None
        review.set_selected_spots({"S513_628"})
        for _ in range(5): app.processEvents()
        legend = plot.mapFromScene(plot._legend.sceneBoundingRect()).boundingRect()
        assert not card.geometry().intersects(legend)
        ellipse = next(e for name, e, _ in plot._ellipses if name == "S513_628")
        x, y = ellipse.getData()
        bounds = QtCore.QRectF(QtCore.QPointF(float(min(x)), float(min(y))),
                              QtCore.QPointF(float(max(x)), float(max(y))))
        selected = plot.mapFromScene(plot._plot_item.vb.mapViewToScene(bounds)).boundingRect()
        assert not card.geometry().intersects(selected)
        before = card.geometry()
        plot._plot_item.vb.translateBy(x=.1)
        for _ in range(3): app.processEvents()
        review._update_spot_card()
        assert card.geometry() == before
        # Focus/size changes can arrive while the spot tab is hidden.
        plot.hide()
        plot.resize(size[0] + 40, size[1] + 30)
        for _ in range(3): app.processEvents()
        plot.show()
        for _ in range(5): app.processEvents()
        legend = plot.mapFromScene(plot._legend.sceneBoundingRect()).boundingRect()
        assert not card.geometry().intersects(legend)
    finally:
        dispose(plot, app)
        dispose(review, app)


def test_overflow_uses_only_required_controls_in_priority_order_and_returns(app):
    from GUI.widgets import _ConcordiaToolbar
    toolbar = _ConcordiaToolbar()
    try:
        for title, priority in (("Diagram", 0), ("Colour", 2), ("Mouse", 0), ("Zoom axes", 5),
                                ("Grid", 4), ("Uncertainty", 6), ("Reset view", 3),
                                ("Samples", 0), ("Fit", 0), ("Focus", 0)):
            button = QtWidgets.QToolButton()
            button.setText(title)
            button.setFixedWidth(70)
            toolbar.add_control(button, title, priority)
        toolbar.show()
        order = ["Uncertainty", "Zoom axes", "Grid", "Reset view", "Colour"]
        seen = set()
        for width in range(900, 400, -1):
            toolbar.resize(width, 26)
            app.processEvents()
            toolbar.reflow()
            folded = {menu.title() for _, menu, _, _ in toolbar.entries if menu.menuAction().isVisible()}
            assert folded == set(order[:len(folded)])
            seen.add(len(folded))
            visible = [w for w, *_ in toolbar.entries if w.parentWidget() is toolbar]
            if folded:
                # Restoring the last removed item, including its spacing, must not fit.
                available = toolbar.width() - toolbar.row.spacing() * len(visible)
                used = sum(w.width() for w in visible) + toolbar.overflow.sizeHint().width()
                assert used + 70 + toolbar.row.spacing() > available
        assert seen == set(range(6))
        toolbar.resize(900, 26)
        for _ in range(3): app.processEvents()
        assert not toolbar.overflow.isVisible()
        assert all(w.parentWidget() is toolbar and w.isVisible() for w, *_ in toolbar.entries)
    finally:
        dispose(toolbar, app)


def test_status_wraps_complete_dates_and_moves_fit_to_second_line(app, finalized):
    review = ResultsTableWidget()
    try:
        for system, value, error in (("206Pb/238U", 1050, 110), ("207Pb/235U", 990, 150),
                                     ("207Pb/206Pb", 880, 310)):
            finalized.selected_results[system + "_age"] = value
            finalized.selected_results[system + "_age_2e"] = error
        review.show_results(finalized)
        review.set_selected_spots({"A_1"})
        review._update_age_readout()
        label, line = review.spot_age_label, review.spot_status_line
        text = "Selected 1 · 206/238 1050 ± 110 · 207/235 990 ± 150 · 207/206 880 ± 310 Ma (2σ)"
        assert label.text() == text
        assert "206Pb/238U" in label.toolTip() and "MSWD" in label.toolTip() and "n=1" in label.toolTip()
        # Isolate the status row so narrow widths exercise wrapping directly.
        line.setParent(None)
        line.show()
        for width in (1000, 450, 1000):
            line.resize(width, line.layout().heightForWidth(width))
            for _ in range(4): app.processEvents()
            line.resize(width, line.layout().heightForWidth(width))
            app.processEvents()
            assert label.wordWrap() and label.text() == text and "…" not in label.text()
            assert label.height() >= label.heightForWidth(label.width())
            assert line.rect().contains(rect_in(label, line))
            assert line.rect().contains(rect_in(review.spot_fit_label, line))
            if width == 450:
                assert label.height() > label.fontMetrics().height()
                assert line._second_row
                assert review.spot_fit_label.y() >= label.geometry().bottom()
            else:
                assert not line._second_row
                assert review.spot_fit_label.width() >= review.spot_fit_label.sizeHint().width()
        dispose(line, app)
    finally:
        dispose(review, app)


def test_corner_summary_tooltip_and_popup_keep_complete_text(app, finalized):
    review = ResultsTableWidget()
    try:
        review.show_results(finalized)
        summary = "A complete review summary with counts, preferred ages and flags."
        review.summary_label.setText(summary)
        review.resize(730, 550)
        review.show()
        for _ in range(4): app.processEvents()
        assert review.summary_label._compact
        assert review.summary_label.toolTip() == summary
        review.summary_label.menu().popup(review.mapToGlobal(QtCore.QPoint(20, 20)))
        app.processEvents()
        assert review.summary_label._details.isVisible()
        assert review.summary_label._details.text() == summary
        review.summary_label.menu().hide()
        review.resize(1100, 550)
        for _ in range(4): app.processEvents()
        assert not review.summary_label._compact
    finally:
        dispose(review, app)
