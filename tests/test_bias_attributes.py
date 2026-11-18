"""Measured composition attributes and display-only projections of unknowns."""
import numpy as np
import pandas as pd
import pytest

from UPbRPy.bias import (accepted_dates, bias_quantities, bias_summary, project_unknowns,
                         standard_bias_table, trace_bias_quantities)
from UPbRPy.export_table import CHONDRITE_REE, ExportSettings, _trace_quantities
from UPbRPy.radiation_damage import alpha_dose
from UPbRPy.standards import standard_archive
from UPbRPy.uncertainty import UncertaintySettings


@pytest.fixture
def frame():
    out = pd.DataFrame({'sample': ['91500'] * 3 + ['unknown'] * 3 + ['NIST610'],
                        'spot_name': [f'spot_{i}' for i in range(7)],
                        'U': [100., 200., 300., 150., 400., np.nan, 500.],
                        'Th': 50., 'Hf': 100., 'Y': 200., 'preferred_age': 1000.,
                        '206Pb/238U_age': 1100., '206Pb/238U_age_2e': 10.})
    for i, el in enumerate([*CHONDRITE_REE, 'Nb', 'Ta', 'P', 'Ti', 'Zr']):
        out[el] = float(i + 1)
    return out


def test_measured_rm_attributes_only(frame):
    quantities = bias_quantities(frame)
    assert list(quantities)[:3] == ['session_time', '238U_cps', '206Pb_cps']
    assert {'Hf', 'Y', 'U', 'Th', 'Lu/Hf', 'Hf/Y', 'alpha_dose'} <= quantities.keys()
    sparse = frame.drop(columns='Hf')
    assert not {'Hf', 'Lu/Hf', 'Hf/Y'}.intersection(bias_quantities(sparse))
    sparse.loc[sparse['sample'] == '91500', 'Y'] = np.nan
    assert 'Y' not in bias_quantities(sparse)
    sparse.loc[sparse['sample'] == '91500', 'Y'] = np.inf
    assert 'Y' not in bias_quantities(sparse)
    for key in quantities:
        assert len(standard_bias_table(frame, standard_archive, quantity=key)) == 3
    with pytest.raises(ValueError, match='Unknown bias quantity'):
        standard_bias_table(frame, standard_archive, quantity='not_a_quantity')


def test_all_shared_trace_values_equal_export(frame):
    # Nonzero, zero, NaN, negative and incomplete REE rows exercise denominators.
    frame.loc[1, ['Y', 'Yb', 'Ta', 'U', 'P', 'Hf']] = 0.
    frame.loc[2, ['Sm', 'La']] = -1.
    frame.loc[3, 'Gd'] = np.nan
    actual = trace_bias_quantities(frame)
    expected = _trace_quantities(frame, ExportSettings(), UncertaintySettings())
    shared = actual.columns.intersection(expected.columns)
    assert len(shared) == 19
    pd.testing.assert_frame_equal(actual[shared], expected[shared])
    assert np.isfinite(actual.loc[3, 'REE']) and np.isnan(actual.loc[3, 'sum_REE_Y'])
    np.testing.assert_allclose(actual.REE.iloc[:3], expected.sum_REE.iloc[:3])


def test_element_and_alpha_x_use_accepted_68_date(frame):
    for key in ('Hf', 'Y'):
        table = standard_bias_table(frame, standard_archive, quantity=key)
        np.testing.assert_allclose(table.x, frame[key].iloc[:3])
    age = accepted_dates(standard_archive, ['91500']).loc['91500', '206Pb/238U']
    table = standard_bias_table(frame, standard_archive, quantity='alpha_dose', date='preferred')
    np.testing.assert_allclose(table.x, alpha_dose(frame.U.iloc[:3], frame.Th.iloc[:3], age))
    custom = standard_archive.loc[['91500']].rename(index={'91500': 'my_control'})
    assert 'Hf' in bias_quantities(frame.replace({'sample': {'91500': 'my_control'}}), standard_archive=custom)


def test_hand_trend_unknowns_and_no_mutation(frame):
    before = frame.copy(deep=True)
    trend = {'intercept': 1., 'slope': .02, 'mswd': 2., 'sum_weights': 4.,
             'x_centre': 200., 'weighted_x_spread': 10000., 'x_range': (100., 300.)}
    out = project_unknowns(frame, standard_archive, trend, date='206Pb/238U', quantity='U')
    assert out.spot_name.tolist() == ['spot_3', 'spot_4', 'spot_5']
    np.testing.assert_allclose(out.predicted_offset_percent.iloc[:2], [4., 9.])
    np.testing.assert_allclose(out.predicted_offset_2s_percent.iloc[:2], 2 * np.sqrt(2 * (.25 + np.array([50., 200.])**2/10000)))
    corrected = 1100 / np.array([1.04, 1.09])
    np.testing.assert_allclose(out.corrected_date_ma.iloc[:2], corrected)
    np.testing.assert_allclose(out.implied_correction_ma.iloc[:2], corrected - 1100)
    np.testing.assert_allclose(out.implied_correction_percent.iloc[:2], (corrected - 1100)/1100*100)
    assert out.extrapolated.tolist() == [False, True, False]
    assert out.plottable.tolist() == [True, True, False]
    negative = project_unknowns(frame, standard_archive, dict(trend, intercept=-200., slope=0.),
                                date='206Pb/238U', quantity='U')
    assert negative.corrected_date_ma.iloc[0] == -1100.  # exact formula even for an unphysical trend
    empty = project_unknowns(frame, standard_archive, None, date='preferred', quantity='U')
    assert empty.empty and empty.columns.tolist() == out.columns.tolist()
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize('date,base', [('preferred', 'preferred_age'), ('207Pb-corrected', '207Pb_corrected_age'),
                                      ('207Pb/235U', '207Pb/235U_age'), ('207Pb/206Pb', '207Pb/206Pb_age')])
def test_unknown_dates_log_and_alpha(frame, date, base):
    frame[base] = 999.
    frame.loc[5, 'U'] = -10.
    trend = {'intercept': -1., 'slope': .5, 'mswd': .5, 'sum_weights': 2.,
             'x_centre': 2., 'weighted_x_spread': 10., 'x_range': (100., 300.)}
    out = project_unknowns(frame, standard_archive, trend, date=date, quantity='U', log_x=True)
    np.testing.assert_allclose(out.date, 999.)
    np.testing.assert_allclose(out.predicted_offset_percent.iloc[:2], -1 + .5*np.log10([150., 400.]))
    assert not out.plottable.iloc[2]
    dose = project_unknowns(frame, standard_archive, trend, date=date, quantity='alpha_dose')
    np.testing.assert_allclose(dose.x, alpha_dose(frame.U.iloc[3:6].reset_index(drop=True), 50.,
                                               frame.preferred_age.iloc[3:6].reset_index(drop=True)), equal_nan=True)
