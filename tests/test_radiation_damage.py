"""Independent checks of present-day atom counts and total alpha events."""
import numpy as np
import pandas as pd
import pytest

from UPbRPy import ALPHA_DOSE_TOOLTIP, alpha_dose


def hand_dose(ratio=137.818):
    # 1000 µg U/g = .001 g/g; 500 µg Th/g = .0005 g/g.
    # N_U = .001 * 6.02214076e23 / 238.02891 atoms/g;
    # N238 = N_U * 137.818/138.818; N235 = N_U/138.818;
    # N232 = .0005 * 6.02214076e23 / 232.0377; t = 1e9 years.
    n_u = .001 * 6.02214076e23 / 238.02891
    n238 = n_u * ratio / (1 + ratio)
    n235 = n_u / (1 + ratio)
    n232 = .0005 * 6.02214076e23 / 232.0377
    return (8 * n238 * (np.exp(1.55125e-10 * 1e9) - 1)
            + 7 * n235 * (np.exp(9.8485e-10 * 1e9) - 1)
            + 6 * n232 * (np.exp(4.9475e-11 * 1e9) - 1))


def test_hand_calculation_and_custom_isotope_ratio():
    assert isinstance(alpha_dose(1000, 500, 1000), float)
    assert alpha_dose(1000, 500, 1000) == pytest.approx(hand_dose(), rel=1e-10)
    assert alpha_dose(1000, 500, 1000, u238_u235=100.) == pytest.approx(hand_dose(100.), rel=1e-10)
    assert 'no annealing' in ALPHA_DOSE_TOOLTIP


def test_zero_age_broadcast_shape_and_invalid_inputs():
    np.testing.assert_array_equal(alpha_dose([0., 1000.], 500., 0.), [0., 0.])
    assert alpha_dose(np.ones((2, 3)), 1., 100.).shape == (2, 3)
    for args in ((np.nan, 500, 1000), (1000, np.nan, 1000), (1000, 500, np.nan),
                 (-1, 500, 1000), (1000, -1, 1000), (1000, 500, -1), (1000, 500, np.inf)):
        assert np.isnan(alpha_dose(*args))


def test_series_preserves_index_and_nan():
    u = pd.Series([1000., np.nan, -1.], index=['one', 'two', 'three'], name='U')
    dose = alpha_dose(u, 500., 1000.)
    assert isinstance(dose, pd.Series)
    pd.testing.assert_index_equal(dose.index, u.index)
    assert dose.iloc[0] == pytest.approx(hand_dose())
    assert dose.iloc[1:].isna().all()
