"""Legend clicks control complete series and retain only display preferences."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pandas as pd
import pytest

pytest.importorskip('PySide6')
from PySide6 import QtCore, QtTest, QtWidgets
import pyqtgraph as pg

from GUI.theme import THEMES, build_application
from GUI.widgets import (StandardBiasWidget, StandardBiasControls, CalibrationSlopesWidget,
                         TracePlotWidget, ConcordiaReviewWidget)
from UPbRPy.bias import bias_summary, project_unknowns
from UPbRPy.isochron import wetherill_point
from test_calibration_grid import fake_data


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode='light')


@pytest.fixture
def views(app):
    widgets = []
    yield widgets
    for widget in widgets:
        widget.hide()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def click_sample(view, legend, text, app):
    sample, label = next((s, l) for s, l in legend.items if l.text.startswith(text))
    scene_pos = sample.mapToScene(sample.boundingRect().center())
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.NoModifier, view.mapFromScene(scene_pos))
    app.processEvents()
    return sample.item


def bias_table():
    return pd.DataFrame({'spot_name': [f'RM_{i}' for i in range(6)], 'sample': ['A']*3+['B']*3,
                         'x': [10., 20., 30., 40., 50., 60.], 'date': 1000., 'date_2s': 10.,
                         'accepted': 990., 'offset_percent': [1., 2., 3., 4., 5., 6.],
                         'offset_2s_percent': 1., 'date_system': '206Pb/238U',
                         'quantity': 'U', 'plottable': True})


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_bias_sample_and_unknown_groups_survive_redraw(app, views, theme):
    view = StandardBiasWidget()
    views.append(view)
    view.resize(900, 560)
    table = bias_table()
    summary = bias_summary(table)
    results = pd.DataFrame({'spot_name': ['inside', 'outside'], 'sample': ['unknown']*2,
                            'U': [25., 90.], '206Pb/238U_age': [1000., 1200.]})
    unknowns = project_unknowns(results, pd.DataFrame(index=['A', 'B']), summary['trend'],
                                date='206Pb/238U', quantity='U')
    view.set_per_sample_trends(True)
    view.show_bias(table, summary, theme=THEMES[theme], x_label='U, ug/g', y_label='Bias, %', unknowns=unknowns)
    view.show()
    app.processEvents()
    assert len(view._point_rows) == 8
    assert len(view._trend_lines) == 4  # pooled, A, B, upper extension
    assert view._scatters['Unknowns on trend'].opts['symbol'] == 'd'
    np.testing.assert_allclose(view._scatters['Unknowns extrapolated'].getData()[1], [9.])
    assert 'not applied' in view.tooltip_text(7) and 'extrapolated' in view.tooltip_text(7)
    view.show_bias(table, summary, theme=THEMES[theme], x_label='Total alpha dose, α/g',
                   y_label='Bias, %', unknowns=unknowns, x_tooltip='formula')
    assert view._plot_item.getAxis('bottom').toolTip() == 'formula'
    primary = click_sample(view, view._legend, 'A ', app)
    assert not primary.isVisible()
    companions = next(parts for key, _, parts in view._legend_series.items() if key == 'A')
    assert len(companions) == 3 and all(not part.isVisible() for part in companions)
    click_sample(view, view._legend, 'Unknowns extrapolated', app)
    extension = next(parts for key, _, parts in view._legend_series.items() if key == 'Unknowns extrapolated')
    assert all(not part.isVisible() for part in extension)
    # Actual clicks select projected unknowns like RM rows.
    point = view._plot_item.vb.mapViewToScene(QtCore.QPointF(*view._point_xy[6]))
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.NoModifier, view.mapFromScene(point))
    assert view.selected_spots == {'inside'}
    view.select_rectangle(QtCore.QRectF(-100, -100, 1000, 1000))
    assert view.selected_spots == {'RM_3', 'RM_4', 'RM_5', 'inside'}
    view.show_bias(table.assign(quantity='Th'), summary, theme=THEMES[theme], x_label='Th, ug/g',
                   y_label='Bias, %', unknowns=unknowns)
    assert not view._scatters['A'].isVisible()
    assert not view._scatters['Unknowns extrapolated'].isVisible()
    assert all(not item.isVisible() for key, _, parts in view._legend_series.items()
               if key == 'A' for item in parts)
    assert summary['trend']['n'] == 6  # unknowns never entered the fit


def test_calibration_legend_controls_line_and_scatter(app, views):
    view = CalibrationSlopesWidget()
    views.append(view)
    view.resize(900, 560)
    view.show_data(fake_data(['238U', '232Th']), THEMES['light'])
    view.show()
    app.processEvents()
    line = click_sample(view, view._plot_item.legend, '238U', app)
    assert not line.isVisible() and not view._scatter_for_line[line].isVisible()
    view.apply_theme(THEMES['dark'])
    assert all(not primary.isVisible() and not parts[0].isVisible()
               for key, primary, parts in view._legend_series.items() if key == '238U')


def test_trace_legend_controls_measurements_and_statistical_overlays(app, views):
    view = TracePlotWidget()
    views.append(view)
    view.resize(1000, 500)
    frame = pd.DataFrame({'Time': np.arange(10.), '238U': np.arange(10.)+100., '206Pb': np.arange(10.)+20.})
    view.show_spot(frame, ['238U', '206Pb'], {'238U': '#336699', '206Pb': '#994433'},
                   {'baseline': (0., 3.), 'plateau': (4., 8.), 'signal': (3., 9.)}, stats_channel='238U')
    view.show()
    app.processEvents()
    curve = click_sample(view, view.plot_item.legend, '238U', app)
    assert not curve.isVisible() and not view._measurement_layers['238U'].isVisible()
    # The selection window belongs to every channel and stays editable.
    assert view._region_overlays['plateau']['region'].isVisible()
    for key in ('fill', 'mean_line', 'upper_line', 'lower_line', 'selected_points',
                'excluded_points', 'excluded_segments', 'excluded_segment_mask'):
        assert not view._region_overlays['plateau'][key].isVisible()
    assert all(not marker.isVisible() for marker in view._signal_markers.values())
    view._update_visible_measurement_points()
    assert not view._measurement_layers['238U'].isVisible()
    view.show_spot(frame, ['238U', '206Pb'], {}, {'plateau': (3., 8.)}, stats_channel='238U')
    assert not view._series_items['238U'].isVisible()
    assert not view._measurement_layers['238U'].isVisible()
    assert view._series_items['206Pb'].isVisible()
    view.show_series(frame, ['238U', '206Pb'], {}, y_label='cps')
    assert not view._series_items['238U'].isVisible()


def test_concordia_sample_legend_filters_points_ellipses_hover_and_selection(app, views):
    view = ConcordiaReviewWidget()
    views.append(view)
    view.resize(900, 560)
    x, y = wetherill_point(np.array([500., 600., 1000., 1100.]))
    frame = pd.DataFrame({'spot_name': ['A_1', 'A_2', 'B_1', 'B_2'], 'sample': ['A', 'A', 'B', 'B'],
                          '207Pb/235U': x, '206Pb/238U': y,
                          '207Pb/235U_2e': .01, '206Pb/238U_2e': .002})
    before = frame.copy(deep=True)
    view.show_data(frame, diagram='wetherill', kind='spot', include_calibration_uncertainty=True,
                   theme=THEMES['light'])
    view.show()
    app.processEvents()
    click_sample(view, view._legend, 'A', app)
    assert {point[0] for point in view._points} == {'B_1', 'B_2'}
    assert len(view._scatter.getData()[0]) == 2
    assert all(not ellipse.isVisible() for name, ellipse, _ in view._ellipses if name.startswith('A'))
    pos = view._plot_item.vb.mapViewToScene(QtCore.QPointF(x[0], y[0]))
    assert view.nearest_point(pos) is None and not view.spots_at(pos)
    view.select_rectangle(QtCore.QRectF(-10., -10., 100., 100.))
    assert view.selected_spots == {'B_1', 'B_2'}
    view.apply_theme(THEMES['dark'])
    assert {p[0] for p in view._points} == {'B_1', 'B_2'}
    view.set_included_spots({'A_1', 'B_1'})
    assert {p[0] for p in view._points} == {'B_1'}
    view.set_included_spots(None)
    click_sample(view, view._legend, 'A', app)
    assert len(view._points) == 4 and all(e.isVisible() for _, e, _ in view._ellipses)
    pd.testing.assert_frame_equal(frame, before)


def test_controls_pending_quantity_group_headers_and_silent_fallback(app, views):
    view = StandardBiasControls()
    views.append(view)
    emissions = []
    view.changed.connect(emissions.append)
    view.set_state({'quantity': 'Hf'})
    assert view.state()['quantity'] == 'Hf'
    view.log_x_check.click()
    assert view.state()['quantity'] == 'Hf'
    emissions.clear()
    view.set_quantities({'session_time': 'Session time, min', 'U': 'U, ug/g', 'Hf': 'Hf, µg/g',
                          'Lu/Hf': 'Lu/Hf', 'alpha_dose': 'Total alpha dose, α/g'})
    assert view.state()['quantity'] == 'Hf' and not emissions
    headers = [view.quantity_combo.itemText(i) for i in range(view.quantity_combo.count())
               if view.quantity_combo.itemData(i) is None]
    assert headers == ['Signal', 'Composition µg/g', 'Ratios', 'Radiation damage']
    view.set_quantities({'U': 'U, ug/g'})
    assert view.state()['quantity'] == 'U' and not emissions
    view.set_state({'quantity': 'alpha_dose'})  # missing after list known: fall back
    assert view.state()['quantity'] == 'U'
    view.set_quantities({'U': 'U, ug/g', 'alpha_dose': 'Total alpha dose, α/g'})
    view.set_state({'quantity': 'alpha_dose'})
    assert view.state()['quantity'] == 'alpha_dose' and not emissions
    from UPbRPy.radiation_damage import ALPHA_DOSE_TOOLTIP
    assert view.quantity_combo.toolTip() == ALPHA_DOSE_TOOLTIP
    index = view.quantity_combo.findData('alpha_dose')
    assert view.quantity_combo.itemData(index, QtCore.Qt.ItemDataRole.ToolTipRole) == ALPHA_DOSE_TOOLTIP
    view.resize(240, 450)
    view.show()
    app.processEvents()
    assert view.minimumSizeHint().width() <= 240


def test_controls_without_measured_u_never_select_a_group_heading(app, views):
    view = StandardBiasControls()
    views.append(view)
    view.set_quantities({'session_time': 'Session time, min'})
    view.set_state({'quantity': 'unavailable'})
    assert view.state()['quantity'] == 'session_time'
    view.set_quantities({'session_time': 'Session time, min'})
    assert view.quantity_combo.currentData() == 'session_time'


def test_hidden_parent_does_not_record_series_as_hidden(app):
    from GUI.legend_series import LegendSeries
    parent = pg.ItemGroup()
    primary, companion = pg.ScatterPlotItem(), pg.ScatterPlotItem()
    for item in (primary, companion):
        item.setParentItem(parent)
    series = LegendSeries()
    series.bind('A', primary, [companion])
    parent.setVisible(False)
    assert series.hidden == set() and companion.isVisibleTo(parent)
    parent.setVisible(True)
    assert series.hidden == set() and companion.isVisible()
    primary.setVisible(False)
    assert series.hidden == {'A'} and not companion.isVisible()
