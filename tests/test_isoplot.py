import numpy as np
import pandas as pd
from bokeh.embed import json_item

from UPbRPy.isoplot import calc_concordia_ellipse, concordia


def test_concordia_ellipse_coordinates_are_real():
    ellipse_x, ellipse_y = calc_concordia_ellipse(
        0.24,
        0.038,
        0.006,
        0.0012,
        0.65,
    )

    assert np.issubdtype(ellipse_x.dtype, np.floating)
    assert np.issubdtype(ellipse_y.dtype, np.floating)
    assert np.all(np.isfinite(ellipse_x))
    assert np.all(np.isfinite(ellipse_y))


def test_concordia_figure_serializes_for_bokeh_widget_rendering():
    data = pd.DataFrame(
        {
            "207Pb/235U": [0.24],
            "206Pb/238U": [0.038],
            "207Pb/235U_2e": [0.012],
            "206Pb/238U_2e": [0.0024],
            "err_corr": [0.65],
            "sample": ["synthetic"],
            "207Pb/235U_age": [220.0],
            "206Pb/238U_age": [240.0],
        }
    )

    item = json_item(concordia(data))

    assert "doc" in item