"""Whole-sweep constraints for explicit Studio window edits only."""

import numpy as np


EDGE_NAMES = ("signal_start", "plateau_start", "plateau_end", "signal_end")
EDGE_LABELS = ("Signal begin", "On-peak begin", "On-peak end", "Signal end")


def constrain_window_edit(times, regions, edge, value):
    """Return coupled windows after moving an edge or the whole plateau.

    Times are measured sweeps of just the current spot. Plateau end is an
    exclusive boundary, so three retained sweeps require end - begin >= 3.
    A shorter acquisition uses every representable interval (n - 1 sweeps).
    Saved selections are never normalized until an explicit edit occurs.
    """
    times = np.asarray(times, dtype=float)
    times = times[np.isfinite(times)]
    result = dict(regions)
    if not times.size or not regions.get("plateau") or not regions.get("signal"):
        return result
    def index(t):
        return int(np.argmin(np.abs(times - t)))
    s, e = map(index, regions["signal"])
    p, q = map(index, regions["plateau"])
    last = len(times) - 1
    width = min(3, last)
    # A pushed edge stops one sweep beyond the edge that pushed it, when the
    # spot has room, so the two lines never end up on the same sweep (the
    # overlap made the on-peak box hard to grab). Edges the user places on
    # the same sweep explicitly are left there.
    if edge in {"plateau", "plateau_range"}:
        left = index(value[0])
        span = min(last, max(width, q - p if edge == "plateau" else index(value[1]) - left))
        p = min(max(0, left), last - span)
        q = p + span
    else:
        target = index(value)
        if edge == "plateau_start":
            p = min(target, q - width)
        elif edge == "plateau_end":
            q = max(target, p + width)
        elif edge == "signal_start":
            s = min(target, q - width)
            if s > p:
                p = s + 1 if s + 1 <= q - width else s
        elif edge == "signal_end":
            e = max(target, p + width)
            if e < q:
                q = e - 1 if e - 1 >= p + width else e
        else:
            raise ValueError(f"Unknown window edge {edge!r}")
        # Repair a too-short legacy plateau only as part of this explicit edit.
        p = min(max(0, p), last - width)
        q = min(last, max(p + width, q))
    if s > p:
        s = max(0, p - 1)
    if e < q:
        e = min(last, q + 1)
    result["signal"] = (float(times[s]), float(times[e]))
    result["plateau"] = (float(times[p]), float(times[q]))
    return result
