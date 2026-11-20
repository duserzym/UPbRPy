"""Studio adapter for signed lab updates and independent standards checks."""

import os
from pathlib import Path

from PySide6 import QtCore

from GUI.labupdate import qt as shared
from GUI.standards_updates import StandardsCheck, check_standards
from GUI.update_config import update_config
from GUI.updates import installed_version

DOWNLOAD_PAGE = "https://github.com/duserzym/UPbRPy/releases/latest"


class _StandardsWorker(QtCore.QThread):
    result = QtCore.Signal(object)

    def __init__(self, path, checkout, parent):
        super().__init__(parent)
        self.path = path
        self.checkout = checkout

    def run(self):
        self.result.emit(check_standards(self.path, self.checkout))


class Updater(shared.Updater):
    """Keep Studio's standards lifecycle alongside the shared application updater."""

    standardsChanged = QtCore.Signal()

    def __init__(self, parent=None, *, opener=None, runner=None, preview=None,
                 version=None, settings=None, root=None, config=None):
        super().__init__(config or update_config(), version or installed_version(), parent,
                         opener=opener, runner=runner,
                         preview=preview or os.environ.get("UPBRPY_UPDATE_PREVIEW"),
                         settings=settings, root=root)
        self._standards_job = None
        self.standards = StandardsCheck("Standards have not been checked.")
        self._standards_timer = QtCore.QTimer(self)
        self._standards_timer.setInterval(self._initial_timer.interval())
        self._standards_timer.timeout.connect(self._scheduled_standards_check)

    def _start_scheduler(self):
        if not self._started and not self._preview and not self._closing:
            self._standards_timer.start()
        super()._start_scheduler()

    def check(self, manual=False):
        if manual and not self._closing:
            self.check_standards()
        super().check(manual)

    @property
    def standards_library_changed(self):
        previous = self.prefs.value("standards/reviewed_digest", "", type=str)
        return bool(previous and self.standards.digest and previous != self.standards.digest)

    @property
    def standards_warning(self):
        return self.standards.warning or self.standards_library_changed

    def acknowledge_standards(self):
        if self.standards.digest:
            self.prefs.setValue("standards/reviewed_digest", self.standards.digest)
            self.standardsChanged.emit()

    def _scheduled_standards_check(self):
        self._standards_timer.setInterval(self._timer.interval())
        self.check_standards()

    def check_standards(self):
        if self._preview or self._standards_job is not None or self._closing:
            return
        from GUI.services import current_standard_archive_path

        default_checkout = Path.home() / "UPbRPy" / "upb-standards"
        checkout = Path(os.environ.get("UPB_STANDARDS_DIR") or default_checkout)
        job = _StandardsWorker(current_standard_archive_path(), checkout.expanduser(), self)
        self._standards_job = job
        job.result.connect(self._standards_result)
        job.finished.connect(self._standards_finished)
        shared._RUNNING.add(job)
        job.start()
        self.standardsChanged.emit()

    @property
    def standards_checking(self):
        return self._standards_job is not None

    @QtCore.Slot(object)
    def _standards_result(self, result):
        self.standards = result
        if result.digest and not self.prefs.value("standards/reviewed_digest", "", type=str):
            self.prefs.setValue("standards/reviewed_digest", result.digest)
        self.standardsChanged.emit()

    @QtCore.Slot()
    def _standards_finished(self):
        shared._RUNNING.discard(self._standards_job)
        self._standards_job.deleteLater()
        self._standards_job = None
        self.standardsChanged.emit()

    @QtCore.Slot()
    def shutdown(self):
        self._standards_timer.stop()
        super().shutdown()
        if self._standards_job is not None:
            self._standards_job.wait()
            shared._RUNNING.discard(self._standards_job)

    def _shutdown(self):
        """Compatibility with Studio's existing close hooks."""
        self.shutdown()


def _wait_for_running_threads():
    """Retain Studio's existing shutdown hook for shared and standards workers."""
    shared._wait_for_threads()
