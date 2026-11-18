"""Qt-free spawn bootstrap and trusted local worker serialization."""

from __future__ import annotations

import importlib
import io
from importlib.machinery import ModuleSpec
import pickle
import sys
import threading
from types import ModuleType


_start_lock = threading.Lock()


def start_process(process):
    """Start a child without replaying the application's Qt entry point."""
    with _start_lock:
        original_main = sys.modules["__main__"]
        bootstrap = ModuleType("__main__")
        bootstrap.__spec__ = ModuleSpec(__name__, loader=None)
        try:
            sys.modules["__main__"] = bootstrap
            process.start()
        finally:
            sys.modules["__main__"] = original_main


class _RestoreUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        # PySide's __import__ hook inspects imported packages for Qt feature
        # use. On the lazy UPbRPy package that probes __wrapped__ and loads
        # the notebook plotting facade. Resolve these trusted child globals
        # directly, avoiding that hook (and unavailable frozen plotting libs).
        sys.audit("pickle.find_class", module, name)
        value = importlib.import_module(module)
        for part in name.split("."):
            value = getattr(value, part)
        return value


def _unpickle(payload):
    return _RestoreUnpickler(io.BytesIO(payload)).load()


def _remote_exception(module, name, message, details, serialized=None):
    try:
        cls = importlib.import_module(module)
        for part in name.split("."):
            cls = getattr(cls, part)
        if not isinstance(cls, type) or not issubclass(cls, Exception):
            raise TypeError(name)
        try:
            error = _unpickle(serialized) if serialized is not None else cls(message)
        except Exception:
            error = RuntimeError(message)
        if not isinstance(error, Exception) or str(error) != message:
            raise ValueError("Exception constructor changed message")
    except Exception:
        error = RuntimeError(message)
    error.add_note(details)
    return error

