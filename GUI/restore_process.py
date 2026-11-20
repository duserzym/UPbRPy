"""One-shot, Qt-free spawned workers for saved-project restoration."""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
import sys
import threading
import traceback
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from GUI.services import RestoredProject


from GUI.process_support import _unpickle, _remote_exception, start_process
_logger = logging.getLogger(__name__)


class RestoreCancelled(Exception):
    """The owning window closed before restoration finished."""


def _child_restore(connection, project_file):
    # This module and its spawn bootstrap must stay free of GUI imports.
    from GUI.services import restore_session_from_project
    from GUI import results_cache

    request = results_cache.prepare(project_file)
    recorder = None
    try:
        if request is not None:
            try:
                recorder = results_cache.DependencyRecorder(request)
                recorder.__enter__()
            except Exception:
                recorder = None
                _logger.debug("Cache dependency recording unavailable", exc_info=True)

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
        if recorder is not None:
            recorder.__exit__()
        try:
            payload = pickle.dumps(restored, protocol=5)
        except Exception as exc:
            connection.send(("infrastructure", f"Cannot pickle restored project: {exc}"))
            return
        connection.send(("imports", "PySide6" in sys.modules))
        connection.send(("result", payload))
        if recorder is not None:
            try:
                results_cache.store(request, payload, recorder.dependencies(restored))
            except Exception:
                _logger.debug("Cache dependency validation failed", exc_info=True)
    finally:
        if recorder is not None:
            recorder.__exit__()
        connection.close()


def _child_warm(connection, project_file):
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    _child_restore(connection, project_file)


def _finish_payload(payload):
    from GUI.services import RestoredProject, load_standard_archive_from_path

    restored = _unpickle(payload)
    if not isinstance(restored, RestoredProject) or restored.session.pipeline is None:
        raise ValueError("Invalid restored project")
    archive = load_standard_archive_from_path(
        None if restored.reference_library_path is None else str(restored.reference_library_path))
    # Archive identity is used to detect interactive library edits.
    restored.session.pipeline.standard_archive = archive
    return restored




def _reap_store(process):
    """Allow cache writes to finish without delaying the restored window."""
    from GUI import results_cache

    try:
        process.join(timeout=300)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        results_cache.cleanup_process(process.pid)
    finally:
        process.close()


class RestoreTask:
    """Own a restore process and allow its window to cancel it safely."""

    def __init__(self, *, warm_only: bool = False) -> None:
        self._warm_only = warm_only
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
        *, use_cache: bool = True,
    ) -> RestoredProject:
        """Restore once, forwarding progress in the calling worker thread."""
        from GUI.services import restore_session_from_project
        from GUI import results_cache

        if self.cancelled:
            raise RestoreCancelled()
        request = results_cache.prepare(project_file)
        if use_cache:
            payload = results_cache.load(request)
            if payload is not None:
                try:
                    if progress is not None:
                        progress("Loading saved results")
                    restored = _finish_payload(payload) if not self._warm_only else None
                    if self.cancelled:
                        raise RestoreCancelled()
                    return results_cache.apply_view(restored, request) if restored is not None else None
                except RestoreCancelled:
                    raise
                except Exception:
                    _logger.debug("Cached project could not be restored", exc_info=True)
                    results_cache.discard(request)
        else:
            results_cache.discard(request)
        if os.environ.get("UPBRPY_RESTORE_IN_PROCESS") == "0":
            return None if self._warm_only else restore_session_from_project(project_file, progress=progress)
        receiver = sender = process = None
        remote_error = None
        succeeded = False
        try:
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_child_warm if self._warm_only else _child_restore,
                                      args=(sender, str(project_file)),
                                      name="UPbRPy project restore", daemon=True)
            with self._lock:
                if self.cancelled:
                    raise RestoreCancelled()
                start_process(process)
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
                    restored = None if self._warm_only else _finish_payload(message[1])
                    if self.cancelled:
                        raise RestoreCancelled()
                    succeeded = True
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
                    if succeeded and not self.cancelled:
                        threading.Thread(target=_reap_store, args=(process,),
                                         name="UPbRPy cache store reaper", daemon=True).start()
                    else:
                        if process.is_alive():
                            process.terminate()
                        process.join(timeout=1)
                        results_cache.cleanup_process(process.pid)
                        process.close()
                self._process = None
            for connection in (receiver, sender):
                if connection is not None:
                    connection.close()
        if self._warm_only:
            return None
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


class ResultsCacheWarmer:
    """Debounce explicit saves; one low-priority child, with no window callbacks."""

    def __init__(self):
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._timer = None
        self._task = None
        self._generation = 0

    def cancel(self):
        with self._lock:
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._task is not None:
                self._task.cancel()

    def schedule(self, project_file, *, delay=5.0):
        from GUI.results_cache import enabled

        self.cancel()
        if not enabled():
            return
        with self._lock:
            generation = self._generation
            self._timer = threading.Timer(delay, self._run, (str(project_file), generation))
            self._timer.daemon = True
            self._timer.start()

    def _run(self, project_file, generation):
        # A cancelled child finishes cleanup before another can start.
        with self._worker_lock:
            with self._lock:
                if generation != self._generation:
                    return
                task = self._task = RestoreTask(warm_only=True)
            try:
                task.restore(project_file)
            except Exception:
                _logger.debug("Saved-results warming skipped", exc_info=True)
            finally:
                with self._lock:
                    if self._task is task:
                        self._task = None


results_cache_warmer = ResultsCacheWarmer()
