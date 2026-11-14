"""One-shot, Qt-free spawned workers for saved-project restoration."""

from __future__ import annotations

import importlib
import io
from importlib.machinery import ModuleSpec
import logging
import multiprocessing
import os
import pickle
import sys
import threading
import traceback
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from GUI.services import RestoredProject


_start_lock = threading.Lock()
_logger = logging.getLogger(__name__)


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


class RestoreCancelled(Exception):
    """The owning window closed before restoration finished."""


def _child_restore(connection, project_file):
    # This module and its spawn bootstrap must stay free of GUI imports.
    from GUI.services import restore_session_from_project

    try:
        connection.send(("imports", "PySide6" in sys.modules))
        try:
            restored = restore_session_from_project(
                project_file, progress=lambda stage: connection.send(("progress", stage)),
            )
        except Exception as exc:
            try:
                serialized = pickle.dumps(exc, protocol=5)
            except Exception:
                serialized = None
            connection.send(("error", type(exc).__module__, type(exc).__qualname__,
                             str(exc), traceback.format_exc(), serialized))
            return
        try:
            payload = pickle.dumps(restored, protocol=5)
        except Exception as exc:
            connection.send(("infrastructure", f"Cannot pickle restored project: {exc}"))
            return
        connection.send(("imports", "PySide6" in sys.modules))
        connection.send(("result", payload))
    finally:
        connection.close()


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
            error = cls.__new__(cls)
            Exception.__init__(error, message)
        if str(error) != message:
            raise ValueError("Exception constructor changed message")
    except Exception:
        error = RuntimeError(message)
    error.add_note(details)
    return error


class RestoreTask:
    """Own a restore process and allow its window to cancel it safely."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Terminate this task's child if it has started."""
        self._cancelled.set()
        with self._lock:
            if self._process is not None and self._process.is_alive():
                self._process.terminate()

    def restore(
        self, project_file: str | Path, progress: Callable[[str], None] | None = None,
    ) -> RestoredProject:
        """Restore once, forwarding progress in the calling worker thread."""
        from GUI.services import load_standard_archive_from_path, restore_session_from_project

        if self.cancelled:
            raise RestoreCancelled()
        if os.environ.get("UPBRPY_RESTORE_IN_PROCESS") == "0":
            return restore_session_from_project(project_file, progress=progress)
        receiver = sender = process = None
        remote_error = None
        try:
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_child_restore, args=(sender, str(project_file)),
                                      name="UPbRPy project restore", daemon=True)
            with self._lock:
                if self.cancelled:
                    raise RestoreCancelled()
                # Spawn normally replays __main__, which may import Qt or run
                # an unguarded embedding script. Our target has no __main__
                # objects, so give spawn this importable, Qt-free bootstrap.
                # Restore the parent module immediately, even if start fails.
                with _start_lock:
                    original_main = sys.modules["__main__"]
                    bootstrap = ModuleType("__main__")
                    bootstrap.__spec__ = ModuleSpec(__name__, loader=None)
                    try:
                        sys.modules["__main__"] = bootstrap
                        process.start()
                    finally:
                        sys.modules["__main__"] = original_main
                self._process = process
            sender.close()
            while True:
                if self.cancelled:
                    raise RestoreCancelled()
                if not receiver.poll(0.1):
                    if not process.is_alive():
                        raise RuntimeError(f"Restore child exited with code {process.exitcode}")
                    continue
                message = receiver.recv()
                kind = message[0]
                if kind == "progress":
                    if progress is not None:
                        progress(message[1])
                elif kind == "imports":
                    # Diagnostic only: a frozen app's runtime hooks may load
                    # Qt in the child. That costs memory, not correctness, so
                    # never discard a finished restore for it.
                    if message[1]:
                        _logger.warning("Restore child imported PySide6")
                elif kind == "error":
                    remote_error = _remote_exception(*message[1:])
                    break
                elif kind == "infrastructure":
                    raise RuntimeError(message[1])
                elif kind == "result":
                    restored = _unpickle(message[1])
                    # The archive is the only module-level state changed by
                    # services.restore_session_from_project and its helpers.
                    archive = load_standard_archive_from_path(
                        None if restored.reference_library_path is None
                        else str(restored.reference_library_path))
                    # ensure_shared_pipeline uses identity to detect library
                    # edits. Pickling copied this otherwise identical frame;
                    # reconnect it without invalidating recalculated stages.
                    restored.session.pipeline.standard_archive = archive
                    return restored
                else:
                    raise RuntimeError(f"Unknown restore message: {kind}")
        except RestoreCancelled:
            raise
        except Exception as exc:
            if self.cancelled:
                raise RestoreCancelled() from None
            _logger.warning("Process restore unavailable; restoring in thread: %s", str(exc).replace("\n", " "))
        finally:
            with self._lock:
                if process is not None and process.pid is not None:
                    process.join(timeout=0.2)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=1)
                    process.close()
                self._process = None
            for connection in (receiver, sender):
                if connection is not None:
                    connection.close()
        if remote_error is not None:
            raise remote_error
        if self.cancelled:
            raise RestoreCancelled()
        return restore_session_from_project(project_file, progress=progress)


def restore_project_in_process(
    project_file: str | Path, progress: Callable[[str], None] | None = None,
) -> RestoredProject:
    """Restore in a spawn child, falling back to this thread on transport failure."""
    return RestoreTask().restore(project_file, progress)
