"""Lazy public package API for UPbRPy.

Historically, importing any :mod:`UPbRPy` submodule first imported the complete
notebook-oriented compatibility API.  That pulled Bokeh, Matplotlib, and every
scientific workflow into memory before the desktop window could open.  Public
names remain backward compatible, but the compatibility facade is now loaded
only when a caller explicitly requests one of those names.

Application and library code should prefer focused imports such as
``from UPbRPy.pipeline import ReductionPipeline``.  Notebook code can continue
to use ``from UPbRPy import *`` unchanged.
"""

from __future__ import annotations

from importlib import import_module


FOCUSED_SUBMODULES = frozenset(
    {
        "age",
        "calibration",
        "concentration",
        "constants",
        "depth",
        "fractionation",
        "io",
        "isochron",
        "isoplot",
        "mpl_concordia",
        "pipeline",
        "project",
        "ratios",
        "reduction",
        "reproducibility",
        "results",
        "selection",
        "standards",
        "thermometry",
        "uncertainty",
        "visualization",
    }
)


def load_public_api() -> list[str]:
    """Load and cache the historical top-level compatibility exports."""
    existing = globals().get("__all__")
    if existing is not None:
        return existing

    core = import_module(".UPbRPy", __name__)
    isochron = import_module(".isochron", __name__)
    core_names = list(core.__all__)
    isochron_names = list(isochron.__all__)
    for name in core_names:
        globals()[name] = getattr(core, name)
    for name in isochron_names:
        globals()[name] = getattr(isochron, name)

    exports = [*core_names, *isochron_names]
    globals()["__all__"] = exports
    return exports


def __getattr__(name: str):
    """Resolve legacy top-level names on first use."""
    if name in FOCUSED_SUBMODULES:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    load_public_api()
    try:
        return globals()[name]
    except KeyError as error:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from error


def __dir__() -> list[str]:
    """Return the complete public API for interactive discovery."""
    return sorted(set(globals()) | set(load_public_api()))
