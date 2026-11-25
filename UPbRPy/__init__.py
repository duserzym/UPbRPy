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

from .naming import natural_sort_key


FOCUSED_SUBMODULES = frozenset(
    {
        "age",
        "bias",
        "calibration",
        "concentration",
        "constants",
        "depth",
        "export_table",
        "fractionation",
        "io",
        "isochron",
        "isoplot",
        "laser_guidance",
        "laser_tie",
        "laserlog",
        "mpl_concordia",
        "naming",
        "notices",
        "pipeline",
        "project",
        "qtegra",
        "ratios",
        "radiation_damage",
        "reduction",
        "reproducibility",
        "results",
        "selection",
        "session_bundle",
        "session_manifest",
        "standards",
        "spot_edits",
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

    damage = import_module(".radiation_damage", __name__)
    for name in ("alpha_dose", "ALPHA_DOSE_TOOLTIP"):
        globals()[name] = getattr(damage, name)
    exports = [*core_names, *isochron_names, "alpha_dose", "ALPHA_DOSE_TOOLTIP", "natural_sort_key"]
    globals()["__all__"] = exports
    return exports


def __getattr__(name: str):
    """Resolve legacy top-level names on first use."""
    if name.startswith("__") and name.endswith("__") and name != "__all__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in {"alpha_dose", "ALPHA_DOSE_TOOLTIP"}:
        value = getattr(import_module(".radiation_damage", __name__), name)
        globals()[name] = value
        return value
    if name in FOCUSED_SUBMODULES:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    try:
        load_public_api()
    except ImportError as error:
        # The frozen Studio app leaves out the notebook facade (UPbRPy.UPbRPy).
        # Attribute probes, such as PySide's import hook while pickling a
        # restored project in the restore child, must then see a missing
        # attribute, not a ModuleNotFoundError.
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from error
    try:
        return globals()[name]
    except KeyError as error:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from error


def __dir__() -> list[str]:
    """Return the complete public API for interactive discovery."""
    return sorted(set(globals()) | set(load_public_api()))
