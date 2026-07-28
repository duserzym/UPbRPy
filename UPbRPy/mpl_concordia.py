"""Static Matplotlib plots for final U--Pb concordia figures.

The main :mod:`UPbRPy.UPbRPy` module provides interactive HoloViews plots.
This module reuses the package's decay constants, Wetherill coordinate
calculation, and weighted-mean implementation while providing composable
Matplotlib axes for multi-panel publication figures.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse
import numpy as np
import pandas as pd

from .isoplot import get_wetherill_point, weighted_mean


def ellipse_from_covariance(ax, x, y, sx_2sigma, sy_2sigma, rho,
                             facecolor, edgecolor=None, alpha=0.3,
                             linewidth=0.9, zorder=2):
    """Draw the two-standard-deviation ellipse implied by ratio uncertainties."""
    covariance = np.array([
        [sx_2sigma**2, rho * sx_2sigma * sy_2sigma],
        [rho * sx_2sigma * sy_2sigma, sy_2sigma**2],
    ])
    values, vectors = np.linalg.eigh(covariance)
    order = values.argsort()[::-1]
    values, vectors = values[order], vectors[:, order]
    angle = np.degrees(np.arctan2(vectors[1, 0], vectors[0, 0]))
    ellipse = Ellipse(
        (x, y), 2 * np.sqrt(values[0]), 2 * np.sqrt(values[1]),
        angle=angle, facecolor=facecolor,
        edgecolor=edgecolor or facecolor, alpha=alpha,
        linewidth=linewidth, zorder=zorder,
    )
    ax.add_patch(ellipse)
    return ellipse


def plot_wetherill_concordia(
    ax,
    data,
    age_min,
    age_max,
    age_points=None,
    accepted=None,
    accepted_color="#77c9b1",
    rejected_color="0.65",
    concordia_color="0.25",
    ellipse_alpha=0.35,
    fontsize=11,
):
    """Plot a static Wetherill concordia with optional reference-age points.

    Parameters
    ----------
    ax : matplotlib.axes.Axes
        Axis on which to draw.
    data : pandas.DataFrame
        Must contain the standard UPbRPy Wetherill ratio, two-sigma percent
        uncertainty, and correlation columns.
    age_min, age_max : float
        Concordia curve range in Ma.
    age_points : sequence of float, optional
        Ages in Ma to mark and label along concordia.
    accepted : array-like of bool, optional
        Mask distinguishing accepted and excluded analyses.
    """
    frame = pd.DataFrame(data).reset_index(drop=True)
    ages = np.linspace(age_min, age_max, 600)
    concordia_x, concordia_y = get_wetherill_point(ages)
    ax.plot(concordia_x, concordia_y, color=concordia_color, linewidth=1.4,
            zorder=1)

    if accepted is None:
        accepted = np.ones(len(frame), dtype=bool)
    accepted = np.asarray(accepted, dtype=bool)
    for idx, row in frame.iterrows():
        x = float(row["207Pb/235U"])
        y = float(row["206Pb/238U"])
        sx = x * float(row["207Pb/235U_2sigma%"]) / 100
        sy = y * float(row["206Pb/238U_2sigma%"]) / 100
        color = accepted_color if accepted[idx] else rejected_color
        ellipse_from_covariance(
            ax, x, y, sx, sy, float(row["error_corr"]), color,
            alpha=ellipse_alpha if accepted[idx] else ellipse_alpha * 0.5,
        )

    if age_points is None:
        age_points = np.linspace(age_min, age_max, 6)
    age_points = np.asarray(age_points, dtype=float)
    point_x, point_y = get_wetherill_point(age_points)
    ax.scatter(point_x, point_y, s=18, color=concordia_color, zorder=4)
    for age, x, y in zip(age_points, point_x, point_y):
        # Offset labels normal to the concordia curve in display coordinates.
        # A small opaque backing prevents the curve from visually running
        # through a label even where the local slope changes rapidly.
        delta = max((age_max - age_min) / 500, 0.02)
        neighbor_ages = np.array([max(age_min, age - delta),
                                  min(age_max, age + delta)])
        nx, ny = get_wetherill_point(neighbor_ages)
        display = ax.transData.transform(np.column_stack([nx, ny]))
        tangent = display[1] - display[0]
        normal = np.array([-tangent[1], tangent[0]], dtype=float)
        if np.linalg.norm(normal) == 0:
            normal = np.array([1.0, 1.0])
        normal /= np.linalg.norm(normal)
        if normal[1] < 0:
            normal *= -1
        offset = 9 * normal
        ax.annotate(
            f"{age:g}", (x, y), xytext=offset,
            textcoords="offset points", fontsize=fontsize - 2,
            color=concordia_color,
            ha="left" if offset[0] >= 0 else "right", va="bottom",
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.82,
                      boxstyle="round,pad=0.08"),
            zorder=5,
        )

    ax.set_xlabel(r"$^{207}$Pb/$^{235}$U", fontsize=fontsize)
    ax.set_ylabel(r"$^{206}$Pb/$^{238}$U", fontsize=fontsize)
    ax.tick_params(labelsize=fontsize - 1)
    ax.ticklabel_format(useOffset=False)
    ax.grid(color="0.9", linewidth=0.6)
    return ax


def plot_ranked_weighted_mean(
    ax,
    dates,
    uncertainties_2sigma,
    labels=None,
    color="#77c9b1",
    student_t=True,
    fontsize=11,
):
    """Plot age-ranked analyses with age on the vertical axis.

    The weighted mean and its uncertainty are calculated with UPbRPy's
    :func:`weighted_mean`. Individual two-sigma uncertainties are shown as
    vertical translucent bars sorted from young (left) to old (right). The
    weighted mean and its confidence band are horizontal.
    """
    dates = np.asarray(dates, dtype=float)
    uncertainties = np.asarray(uncertainties_2sigma, dtype=float)
    if labels is None:
        labels = np.arange(1, len(dates) + 1).astype(str)
    labels = np.asarray(labels).astype(str)
    order = np.argsort(dates)
    dates, uncertainties, labels = dates[order], uncertainties[order], labels[order]
    ranks = np.arange(len(dates))

    mean, confidence = weighted_mean(dates, uncertainties, student_T=student_t)
    ax.axhspan(mean - confidence, mean + confidence, color=color, alpha=0.18,
               zorder=0)
    ax.axhline(mean, color=color, linewidth=1.8, zorder=1)
    ax.bar(
        ranks, 2 * uncertainties, bottom=dates - uncertainties, width=0.52,
        color=color, edgecolor=color, alpha=0.38, linewidth=0.8, zorder=2,
    )
    ax.scatter(ranks, dates, s=28, color=color, edgecolor="white",
               linewidth=0.5, zorder=3)
    ax.set_xticks(ranks, labels, rotation=45, ha="right")
    ax.set_xlabel("Age-ranked analysis", fontsize=fontsize)
    ax.set_ylabel(r"$^{206}$Pb/$^{238}$U age (Ma)", fontsize=fontsize)
    ax.tick_params(labelsize=fontsize - 1)
    ax.grid(axis="y", color="0.9", linewidth=0.6)
    return mean, confidence


__all__ = [
    "ellipse_from_covariance",
    "plot_wetherill_concordia",
    "plot_ranked_weighted_mean",
]
