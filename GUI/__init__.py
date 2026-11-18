"""UPbRPy desktop GUI package built on PySide6 and pyqtgraph."""


def _load_engine_standards():
    """Keep Studio importable when a private overlay is broken.

    Services retry the overlay and attach the error to the active archive for
    the reference-library notice. Engine-only imports retain strict failures.
    """
    import importlib
    import os

    try:
        importlib.import_module("UPbRPy.standards")
    except (OSError, ValueError):
        configured = os.environ.get("UPBRPY_LAB_STANDARDS")
        os.environ["UPBRPY_LAB_STANDARDS"] = "none"
        try:
            importlib.import_module("UPbRPy.standards")
        finally:
            if configured is None:
                os.environ.pop("UPBRPY_LAB_STANDARDS", None)
            else:
                os.environ["UPBRPY_LAB_STANDARDS"] = configured


_load_engine_standards()
