"""Standalone bias views and compact controls, exercised offscreen."""
import os

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6 import QtCore, QtTest, QtWidgets
import pyqtgraph as pg

from GUI.theme import THEMES, build_application
from GUI.widgets import StandardBiasControls, StandardBiasWidget, _REVIEW_SAMPLE_COLORS
from UPbRPy.bias import bias_summary


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode='light')


@pytest.fixture
def table():
    return pd.DataFrame({'spot_name': [f'spot_{i}' for i in range(6)],
                         'sample': ['B'] * 3 + ['A'] * 3, 'x': [10., 20., 30., 40., 50., 60.],
                         'date': [501., 502., 503., 504., 505., 506.], 'date_2s': 5.,
                         'accepted': 500., 'offset_percent': [.2, .4, .6, .8, 1., 1.2],
                         'offset_2s_percent': 1., 'date_system': '206Pb/238U',
                         'quantity': 'U', 'plottable': True})


@pytest.fixture
def widget(app, table):
    view = StandardBiasWidget()
    view.resize(900, 560)
    view.show_bias(table, bias_summary(table), theme=THEMES['light'],
                   x_label='U, ug/g', y_label='Offset from accepted 206Pb/238U date (%)')
    view.show()
    app.processEvents()
    yield view
    view.close()
    view.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def click_point(widget, app, index, modifier=QtCore.Qt.KeyboardModifier.NoModifier):
    xy = widget._point_xy[index]
    scene = widget._plot_item.vb.mapViewToScene(QtCore.QPointF(*xy))
    pos = widget.mapFromScene(scene)
    QtTest.QTest.mouseClick(widget.viewport(), QtCore.Qt.MouseButton.LeftButton, modifier, pos)
    app.processEvents()


def test_scatter_legend_means_error_bars_and_theme(widget, table):
    assert set(widget._scatters) == {'A', 'B'}
    assert len([item for item in widget._plot_item.items if isinstance(item, pg.ScatterPlotItem)]) == 2
    assert len(widget._legend.items) == 2
    assert len(widget._error_bars) == 2 and len(widget._mean_lines) == 2
    assert len(widget._trend_lines) == 1
    assert widget._scatters['A']._bias_color == _REVIEW_SAMPLE_COLORS[0]
    assert widget._scatters['B']._bias_color == _REVIEW_SAMPLE_COLORS[1]
    for sample, label in widget._legend.items:
        assert '+/-' in label.text and 'n 3' in label.text
    assert 'slope' in widget._trend_text.toPlainText()
    widget.set_per_sample_trends(True)
    assert len(widget._trend_lines) == 3
    widget.apply_theme(THEMES['dark'])
    assert len(widget._legend.items) == 2
    assert widget._trend_text.color == pg.mkColor(THEMES['dark'].text)


def test_selection_is_silent_until_mouse_click(widget, app):
    emissions = []
    widget.selectionChanged.connect(emissions.append)
    widget.set_selected_spots({'spot_0'})
    assert emissions == []
    assert widget.selected_spots == {'spot_0'}
    click_point(widget, app, 0)  # sorted sample order: A / spot_3
    assert emissions[-1] == {'spot_3'}
    click_point(widget, app, 1, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert emissions[-1] == {'spot_3', 'spot_4'}
    click_point(widget, app, 0, QtCore.Qt.KeyboardModifier.MetaModifier)
    assert emissions[-1] == {'spot_4'}
    vb = widget._plot_item.vb
    empty = widget.mapFromScene(vb.mapViewToScene(QtCore.QPointF(15., -.2)))
    QtTest.QTest.mouseClick(widget.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.NoModifier, empty)
    app.processEvents()
    assert emissions[-1] == set()


def test_hover_log_axes_and_interaction_modes(widget, table, app):
    names = []
    widget.spotHovered.connect(names.append)
    widget.show_bias(table, bias_summary(table, log_x=True), theme=THEMES['light'], log_x=True,
                     x_label='U, ug/g', y_label='Offset from accepted 206Pb/238U date (%)')
    app.processEvents()
    assert widget._plot_item.getAxis('bottom').logMode
    np.testing.assert_allclose(widget._scatters['A'].getData()[0], np.log10([40., 50., 60.]))
    np.testing.assert_allclose(widget._error_bars[0].opts['x'], np.log10([40., 50., 60.]))
    assert 'per decade' in widget._trend_text.toPlainText()
    pos = widget._plot_item.vb.mapViewToScene(QtCore.QPointF(*widget._point_xy[0]))
    widget._on_mouse_moved(pos)
    assert names[-1] == 'spot_3'
    assert widget._hover_text.isVisible()
    for text in ['spot_3', 'Accepted', '504', 'Ma', '+/-']:
        assert text in widget.tooltip_text(0)
    widget._set_hover(None)
    assert names[-1] == '' and not widget._hover_text.isVisible()
    widget.set_zoom_mode('x')
    assert widget.zoom_mode() == 'x'
    assert widget._plot_item.vb.state['mouseEnabled'] == [True, False]
    widget.set_interaction_mode('pan')
    assert widget.interaction_mode() == 'pan'
    assert widget._plot_item.vb.state['mouseMode'] == pg.ViewBox.PanMode
    widget.set_interaction_mode('select')
    assert widget.interaction_mode() == 'select'
    widget.show_bias(table, bias_summary(table), theme=THEMES['light'], log_x=False,
                     x_label='U, ug/g', y_label='Offset (%)')
    assert not widget._plot_item.getAxis('bottom').logMode
    np.testing.assert_allclose(widget._scatters['A'].getData()[0], [40., 50., 60.])


def test_controls_round_trip_emission_summary_and_narrow_width(app, table):
    controls = StandardBiasControls()
    emissions = []
    controls.changed.connect(emissions.append)
    assert controls.state() == {'date': '206Pb/238U', 'quantity': 'U',
                                 'log_x': False, 'per_sample_trends': False}
    state = {'date': 'preferred', 'quantity': 'Pb', 'log_x': True, 'per_sample_trends': True}
    controls.set_state(state)
    assert controls.state() == state and not emissions
    controls.log_x_check.click()
    assert emissions[-1] == dict(state, log_x=False)
    controls.quantity_combo.setCurrentIndex(0)
    assert emissions[-1]['quantity'] == 'U'
    controls.date_combo.setCurrentIndex(1)
    assert emissions[-1]['date'] == '207Pb/235U'
    controls.set_summary(bias_summary(table))
    assert 'slope' in controls.summary_label.text() and 'A:' in controls.summary_label.text()
    controls.resize(240, 450)
    controls.show()
    app.processEvents()
    assert controls.minimumSizeHint().width() <= 240
    assert controls.width() <= 240
    controls.close()
    controls.deleteLater()


def test_empty_and_nonpositive_log_data(app, table):
    widget = StandardBiasWidget()
    for frame in (table.iloc[:0], table.assign(x=-1.)):
        widget.show_bias(frame, bias_summary(frame, log_x=True), theme=THEMES['light'], log_x=True,
                         x_label='U, ug/g', y_label='Offset (%)')
        assert not widget._scatters
    widget.close()
    widget.deleteLater()
