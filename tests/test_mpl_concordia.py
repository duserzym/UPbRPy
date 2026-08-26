"""Tests for reusable static concordia plotting helpers."""

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from UPbRPy.mpl_concordia import plot_tera_wasserburg_concordia


def test_static_tw_concordia_can_span_requested_plot_range():
    data = pd.DataFrame(
        {
            "238U/206Pb": [9.0],
            "207Pb/206Pb": [0.20],
            "238U/206Pb_2e": [0.4],
            "207Pb/206Pb_2e": [0.02],
            "tw_rho": [0.25],
        }
    )
    fig, ax = plt.subplots()
    ax.set_xlim(4.2, 15.8)
    plot_tera_wasserburg_concordia(
        ax,
        data,
        age_min=380,
        age_max=1400,
        age_points=(400, 600, 1000, 1200),
        correlation_column="tw_rho",
    )

    curve_x = ax.lines[0].get_xdata()
    assert curve_x.min() <= 4.2
    assert curve_x.max() >= 15.8
    assert len(ax.patches) == 1
    assert ax.get_xlabel() == r"$^{238}$U/$^{206}$Pb"
    assert ax.get_ylabel() == r"$^{207}$Pb/$^{206}$Pb"
    plt.close(fig)


def test_static_tw_concordia_accepts_legacy_table_without_tw_rho():
    data = pd.DataFrame(
        {
            "238U/206Pb": [9.0],
            "207Pb/206Pb": [0.20],
            "238U/206Pb_2e": [0.4],
            "207Pb/206Pb_2e": [0.02],
            "err_corr": [0.9],
        }
    )
    fig, ax = plt.subplots()

    plot_tera_wasserburg_concordia(
        ax,
        data,
        age_min=400,
        age_max=1200,
    )

    assert len(ax.patches) == 1
    plt.close(fig)
