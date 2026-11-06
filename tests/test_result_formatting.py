"""Uncertainty precision for review text is independent of the GUI."""
import numpy as np
import pytest

from UPbRPy.results import format_mswd, format_value_with_uncertainty


@pytest.mark.parametrize("value,error,expected", [
    (563.9, 24, "564 ± 24"),
    (12.34, .21, "12.34 ± 0.21"),
    (12.3, .21, "12.30 ± 0.21"),
    (563.9, 240, "560 ± 240"),
    (.012345, .00021, "0.01235 ± 0.00021"),
    (1234, 99.9, "1230 ± 100"),
    (1.234, 0, "1.234"),
    (1.234, -1, "1.234"),
    (1.234, np.nan, "1.234"),
    (1.234, None, "1.234"),
    (np.nan, 1, ""),
    (np.inf, 1, ""),
    (None, 1, ""),
])
def test_uncertainty_format(value, error, expected):
    assert format_value_with_uncertainty(value, error) == expected


def test_uncertainty_significant_figures():
    assert format_value_with_uncertainty(12.34, .21, sig_figs=1) == "12.3 ± 0.2"
    with pytest.raises(ValueError, match="positive integer"):
        format_value_with_uncertainty(12, .2, sig_figs=0)


@pytest.mark.parametrize("value,expected", [
    (0, "0.00"), (.28, "0.28"), (1, "1.00"),
    (9.99, "9.99"), (10, "10.0"), (12.34, "12.3"),
    (99.9, "99.9"), (100, "100"), (1060, "1060"),
    (1060.4, "1060"), (1060.6, "1061"),
    (np.nan, ""), (np.inf, ""), (-np.inf, ""), (None, ""), ("invalid", ""),
])
def test_mswd_format(value, expected):
    assert format_mswd(value) == expected
