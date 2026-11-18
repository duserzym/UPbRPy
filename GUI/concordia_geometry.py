"""Bounded screen-space sampling for curved concordia overlays."""
from __future__ import annotations

import numpy as np


def sample_curve(function, start, stop, viewbox, *, min_points=17, max_points=8193):
    """Sample a parametric curve at <=2 logical px, capped at max_points.

    Refine long chords and curved midpoints together. All evaluations in a
    refinement pass are vectorised; no Python call is made per segment.
    The caller clips the parameter interval to the visible plot first.
    """
    parameters = np.linspace(start, stop, 17)
    ranges = np.asarray(viewbox.viewRange())
    scale = np.array([viewbox.width(), viewbox.height()]) / np.maximum(
        ranges[:, 1] - ranges[:, 0], np.finfo(float).tiny)
    # T-W near zero age spans many orders of magnitude in x. Allow enough
    # bisections for that endpoint while keeping the vertex budget bounded.
    for _ in range(32):
        xy = np.asarray(function(parameters)).T
        middle = (parameters[:-1] + parameters[1:]) / 2
        mid_xy = np.asarray(function(middle)).T
        length = np.linalg.norm(np.diff(xy, axis=0)*scale, axis=1)
        deviation = np.linalg.norm((mid_xy-(xy[:-1]+xy[1:])/2)*scale, axis=1)
        refine = np.flatnonzero((length > 2) | (deviation > .2))
        budget = max_points - len(parameters)
        if budget <= 0:
            break
        if not len(refine):
            # Retain a requested density floor by splitting the longest
            # screen chords, without discarding any adaptive breakpoints.
            needed = min(min_points, max_points) - len(parameters)
            if needed <= 0:
                break
            refine = np.argsort(length)[-needed:]
        if len(refine) > budget:
            # Spend the last pass on the longest/most curved segments.
            score = np.maximum(length[refine]/2, deviation[refine]/.2)
            refine = refine[np.argsort(score)[-budget:]]
        parameters = np.sort(np.concatenate((parameters, middle[refine])))
    x, y = function(parameters)
    return np.asarray(x), np.asarray(y)
