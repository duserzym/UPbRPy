"""Persistent Qt-free reduction workers, shared by Studio windows.

Only the owning GUI thread prepares requests and merges replies. Worker
threads move protocol-5 buffers and wait for children; they never serialize
the live session. Each request installs its own reference library. All other
scientific state used by services is carried by the session/configuration,
the workflow key, and ExportSettings; engine module constants are immutable.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import io
import logging
import multiprocessing
import os
import pickle
import sys
import threading
import time
import traceback

from GUI.process_support import _RestoreUnpickler, _remote_exception, start_process


_logger = logging.getLogger(__name__)
_warning_lock = threading.Lock()
_warned = False


def enabled():
    return os.environ.get("UPBRPY_STAGE_PROCESS") != "0"


def _warn(error):
    global _warned
    with _warning_lock:
        if not _warned:
            _logger.warning("Process stages unavailable; running in thread: %s",
                            str(error).replace("\n", " "))
            _warned = True


class StageCancelled(Exception):
    """The owning window or preview cancelled this request."""


@dataclass
class StageCall:
    """Name a stage while retaining its existing in-thread callable."""

    stage: str
    fallback: object
    stages: tuple = ()

    def __call__(self):
        return self.fallback()


def run_stage(session, preset_key, settings, stage, stages=()):
    """Execute a registered stage using only Qt-free service functions."""
    from GUI import services
    from GUI.workflows import get_workflow_preset

    preset = get_workflow_preset(preset_key)
    if stage == "final_review":
        means = services.ensure_spot_means(session, preset)
        final = services.ensure_finalized_results(session, preset)
        services.ensure_results_pb204(session, preset, settings)
        return means, final
    if stage == "selection":
        # Pass the actual export choices to both the population and 204Pb step.
        services.populate_recalculated_cache(session, preset, stages, export_settings=settings)
        return None
    function, takes_preset = STAGES[stage]
    function = getattr(services, function)
    return function(session, preset) if takes_preset else function(session)


STAGES = {
    "baseline": ("ensure_baseline_subtracted", False),
    "normalization": ("ensure_normalized_plateau", True),
    "concentration": ("ensure_concentration_results", True),
    "ratios": ("ensure_raw_ratios", False),
    "downhole": ("ensure_downhole_results", True),
    "session": ("ensure_session_corrected", True),
    "depth_profile": ("ensure_depth_profile", True),
    "final_review": None,
    "selection": None,
}


def calibration_preview(qtegra, options):
    """Fit both detector preview modes outside the GUI process."""
    from UPbRPy.calibration import fit_cross_calibration

    return (fit_cross_calibration(qtegra, **options, mode="constant"),
            fit_cross_calibration(qtegra, **options, mode="drift"))


def _dump(value, references=None, *, copy_buffers=False):
    buffers = []
    stream = io.BytesIO()

    class Pickler(pickle.Pickler):
        def persistent_id(self, obj):
            return references.get(id(obj)) if references else None

    pickler = Pickler if references else pickle.Pickler
    pickler(stream, protocol=5, buffer_callback=buffers.append).dump(value)
    if copy_buffers:
        buffers = [buffer.raw().tobytes() for buffer in buffers]
    return stream.getvalue(), buffers


def _load(packet, references=None):
    class Unpickler(_RestoreUnpickler):
        def persistent_load(self, token):
            return references[token]

    payload, buffers = packet
    return Unpickler(io.BytesIO(payload), buffers=buffers).load()


def _send(connection, packet):
    payload, buffers = packet
    connection.send(len(buffers))
    connection.send_bytes(payload)
    for buffer in buffers:
        connection.send_bytes(buffer.raw() if isinstance(buffer, pickle.PickleBuffer) else buffer)


def _receive(connection):
    count = connection.recv()
    payload = connection.recv_bytes()
    # Protocol 5 preserves writable numpy arrays only with writable buffers.
    # Read-only originals still carry pickle's READONLY_BUFFER opcode.
    return payload, [bytearray(connection.recv_bytes()) for _ in range(count)]


def _child(connection):
    from GUI import services

    connection.send(("ready", "PySide6" in sys.modules))
    try:
        while True:
            request = _load(_receive(connection))
            kind, arguments, references = request
            try:
                if kind == "preview":
                    value = calibration_preview(*arguments)
                else:
                    session, key, settings, stage, stages, archive, path = arguments
                    services._active_standard_archive = archive
                    services._active_standard_archive_path = path
                    pipeline = session.pipeline
                    if pipeline is not None:
                        # id-based override memo keys belong to this interpreter.
                        pipeline._override_archive_key = None
                    result = run_stage(session, key, settings, stage, stages)
                    value = StageReply(result, session.pipeline, session.reduction_cache)
            except Exception as exc:
                try:
                    serialized = pickle.dumps(exc, protocol=5)
                except Exception:
                    serialized = None
                connection.send(("error", type(exc).__module__, type(exc).__qualname__,
                                 str(exc), traceback.format_exc(), serialized))
            else:
                try:
                    packet = _dump(value, {id(obj): token for token, obj in references.items()})
                except Exception as exc:
                    connection.send(("infrastructure", str(exc)))
                else:
                    connection.send(("result",))
                    _send(connection, packet)
            # An idle child must not retain a project's data or buffers.
            request = arguments = references = value = result = session = pipeline = packet = archive = None
            services._active_standard_archive = None
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class _Worker:
    def __init__(self):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_child, args=(child,),
                                       name="UPbRPy reduction stages", daemon=True)
        try:
            start_process(self.process)
        except BaseException:
            self.connection.close()
            raise
        finally:
            child.close()

    def ready(self, cancelled):
        deadline = time.monotonic() + 30
        while not self.connection.poll(0.1):
            if cancelled():
                raise StageCancelled()
            if not self.process.is_alive():
                raise RuntimeError(f"Stage child exited with code {self.process.exitcode}")
            if time.monotonic() > deadline:
                raise RuntimeError("Stage child did not finish starting within 30 seconds")
        kind, qt_loaded = self.connection.recv()
        self.qt_loaded = qt_loaded
        if kind != "ready":
            raise RuntimeError("Stage child did not initialize")
        if qt_loaded:
            _logger.warning("Stage child imported PySide6")

    def stop(self):
        if self.process.is_alive():
            self.process.terminate()

    def close(self):
        self.stop()
        self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=1)
        self.connection.close()


class StagePool:
    """Keep one warm idle child and allow at most two simultaneous calls."""

    def __init__(self):
        self._condition = threading.Condition()
        self._idle = []
        self._workers = set()
        self._owners = {}
        self._starting = 0
        self._closed = False
        self._warming = False
        self._warm_task = None
        self._windows = set()

    def register_window(self):
        """Retain the shared pool until the last reduction window closes."""
        token = object()
        with self._condition:
            self._windows.add(token)
        return token

    def release_window(self, token):
        """Stop idle children as well when the last window closes."""
        with self._condition:
            if token not in self._windows:
                return
            self._windows.remove(token)
            if self._windows:
                return
            if self._warm_task is not None:
                self._warm_task.cancel()
            for task in self._owners.values():
                task.cancel()
            idle, self._idle = self._idle, []
            self._workers.difference_update(idle)
            for worker in idle:
                worker.stop()
            self._condition.notify_all()
        if idle:
            def reap():
                for worker in idle:
                    worker.close()
            threading.Thread(target=reap, name="Close reduction workers", daemon=True).start()

    def acquire(self, task):
        worker = None
        creating = False
        with self._condition:
            while True:
                if self._closed or task.cancelled:
                    raise StageCancelled()
                if self._idle:
                    worker = self._idle.pop()
                    self._owners[worker] = task
                    break
                # Warming and detector previews queue on the first child.
                # A second child is reserved for concurrent reduction calls
                # (each MainWindow permits only one of those at a time).
                capacity = (2 if task._kind == "stage" and not self._starting
                            and all(owner._kind == "stage" for owner in self._owners.values())
                            else 1)
                if len(self._workers) + self._starting < capacity:
                    self._starting += 1
                    creating = True
                    break
                self._condition.wait(0.1)
        try:
            if creating:
                worker = _Worker()
            with self._condition:
                self._workers.add(worker)
                self._owners[worker] = task
                task.attach(worker)
                if self._closed:
                    raise StageCancelled()
            if creating:
                worker.ready(lambda: task.cancelled or self._closed)
            return worker
        except BaseException:
            if worker is not None:
                self.release(worker, reusable=False)
            raise
        finally:
            with self._condition:
                if creating:
                    self._starting -= 1
                self._condition.notify_all()

    def release(self, worker, *, reusable):
        with self._condition:
            self._owners.pop(worker, None)
            keep = reusable and not self._closed and not self._idle and worker.process.is_alive()
            if keep:
                self._idle.append(worker)
            else:
                self._workers.discard(worker)
            self._condition.notify_all()
        if not keep:
            worker.close()

    def warm(self):
        """Start imports in a daemon thread, never on the GUI thread."""
        if not enabled():
            return
        with self._condition:
            if self._warming or self._workers or self._starting or self._closed:
                return
            self._warming = True
            task = self._warm_task = StageTask(pool=self)

        def run():
            try:
                worker = self.acquire(task)
                self.release(worker, reusable=not task.cancelled)
            except StageCancelled:
                pass
            except Exception as exc:
                _warn(exc)
            finally:
                with self._condition:
                    self._warming = False
                    self._warm_task = None

        threading.Thread(target=run, name="Warm reduction worker", daemon=True).start()

    def shutdown(self):
        """Terminate children at application exit, including outstanding calls."""
        with self._condition:
            self._closed = True
            if self._warm_task is not None:
                self._warm_task.cancel()
            for task in self._owners.values():
                task.cancel()
            for worker in self._workers:
                worker.stop()
            idle, self._idle = self._idle, []
            self._workers.difference_update(idle)
            self._condition.notify_all()
        for worker in idle:
            worker.close()


@dataclass
class StageReply:
    result: object
    pipeline: object
    reduction_cache: dict
    _experiment_token: tuple | None = None

    def merge(self, session, preset):
        """Install results, then invalidate anything edited during this request."""
        from GUI.services import ensure_shared_pipeline

        session.pipeline = self.pipeline
        session.reduction_cache = self.reduction_cache
        # Persistent references already restored the parent's library identity.
        # Translate the override memo's interpreter-local id without refitting.
        if self.pipeline._override_archive_key is not None:
            self.pipeline._override_archive_key = (
                id(self.pipeline.base_standard_archive), self.pipeline._override_archive_key[1])
        token = self._experiment_token
        unchanged = (token is not None and session.experiment is token[0]
                     and pickle.dumps(session.experiment, protocol=5) == token[1])
        ensure_shared_pipeline(session, preset, source_experiment_unchanged=unchanged)
        self._experiment_token = None
        return self.result


class StageTask:
    """Own one snapshot and cancellation handle; transport runs in a QRunnable."""

    def __init__(self, *, pool=None):
        self.pool = pool or stage_pool
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._worker = None
        self._packet = None
        self._references = {}
        self._kind = None
        self.in_thread = False
        self._experiment_token = None

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def attach(self, worker):
        with self._lock:
            self._worker = worker
            if self.cancelled:
                worker.stop()
                raise StageCancelled()

    def cancel(self):
        """Terminate the in-flight child without waiting on the GUI thread."""
        self._cancelled.set()
        with self._lock:
            if self._worker is not None:
                self._worker.stop()

    def prepare(self, session, preset_key, settings, stage, stages=()):
        """Snapshot live state synchronously on its owning GUI thread."""
        from GUI.services import current_standard_archive, current_standard_archive_path

        try:
            self._experiment_token = (session.experiment, pickle.dumps(session.experiment, protocol=5))
        except Exception:
            # The normal snapshot path below owns infrastructure fallback.
            self._experiment_token = None
        references = {name: getattr(session, name)
                      for name in ("total_data", "experiment", "qtegra", "channels")}
        references["archive"] = current_standard_archive()
        if session.pipeline is not None:
            for name in ("experiment", "total_data", "channels", "base_standard_archive"):
                references[f"pipeline.{name}"] = getattr(session.pipeline, name)
        self._prepare("stage", (session, preset_key, settings, stage, tuple(stages),
                                references["archive"], current_standard_archive_path()), references)

    def prepare_preview(self, qtegra, options):
        self._prepare("preview", (qtegra, options), {})

    def _prepare(self, kind, arguments, references):
        self._kind = kind
        self._packet = None
        self._references = {}
        try:
            self._packet = _dump((kind, arguments, references), copy_buffers=True)
            self._references = references
        except Exception as exc:
            self.in_thread = True
            _warn(exc)

    def run(self, fallback):
        """Send the prepared snapshot; use the old thread callable on failure."""
        if self.cancelled:
            raise StageCancelled()
        if not enabled() or self._packet is None:
            self.in_thread = True
            return fallback()
        worker = None
        reusable = False
        remote_error = None
        try:
            worker = self.pool.acquire(self)
            _send(worker.connection, self._packet)
            while not worker.connection.poll(0.1):
                if self.cancelled:
                    raise StageCancelled()
                if not worker.process.is_alive():
                    raise RuntimeError(f"Stage child exited with code {worker.process.exitcode}")
            message = worker.connection.recv()
            if message[0] == "error":
                remote_error = _remote_exception(*message[1:])
                reusable = True
            elif message[0] == "result":
                result = _load(_receive(worker.connection), self._references)
                reusable = True
                if isinstance(result, StageReply):
                    result._experiment_token = self._experiment_token
                return result
            else:
                raise RuntimeError(f"Stage transport failed: {message}")
        except StageCancelled:
            raise
        except Exception as exc:
            if self.cancelled:
                raise StageCancelled() from None
            _warn(exc)
        finally:
            with self._lock:
                self._worker = None
            if worker is not None:
                self.pool.release(worker, reusable=reusable and not self.cancelled)
            self._packet = None
            self._references = {}
            self._experiment_token = None
        if remote_error is not None:
            raise remote_error
        if self.cancelled:
            raise StageCancelled()
        self.in_thread = True
        return fallback()


stage_pool = StagePool()
atexit.register(stage_pool.shutdown)
