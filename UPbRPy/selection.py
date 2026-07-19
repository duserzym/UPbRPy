"""Compatibility exports for selection APIs now implemented in :mod:`UPbRPy.io`."""

from .io import (
    SELECTION_METHODS,
    SelectionError,
    SelectionResult,
    available_selection_methods,
    register_selection_method,
    selection_method_label,
    find_signal_start,
    find_signal_end,
    select_baseline_peak,
    select_signal_regions,
)

__all__ = [
    "SELECTION_METHODS",
    "SelectionError",
    "SelectionResult",
    "available_selection_methods",
    "register_selection_method",
    "selection_method_label",
    "find_signal_start",
    "find_signal_end",
    "select_baseline_peak",
    "select_signal_regions",
]
