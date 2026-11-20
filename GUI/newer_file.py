"""Plain refusal and update action for incompatible saved reductions."""

from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from UPbRPy.pipeline import NewerFileError
from UPbRPy.project import read_reduction_project


def show_newer_file_dialog(
    parent: QtWidgets.QWidget | None, error: NewerFileError, *,
    open_updates: Callable[[], None] | None = None,
) -> None:
    """Show one refusal with an update action, without technical details."""
    dialog = QtWidgets.QMessageBox(parent)
    dialog.setWindowTitle("Newer Studio file")
    dialog.setIcon(QtWidgets.QMessageBox.Icon.Information)
    dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    dialog.setText(str(error))
    update = dialog.addButton("Update Studio…", QtWidgets.QMessageBox.ButtonRole.ActionRole)
    close = dialog.addButton("Close", QtWidgets.QMessageBox.ButtonRole.RejectRole)
    dialog.setDefaultButton(close)
    dialog.setEscapeButton(close)
    dialog.exec()
    if dialog.clickedButton() is update:
        if open_updates is not None:
            open_updates()
        else:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl("https://github.com/duserzym/UPbRPy/releases/latest")
            )


def refuse_newer_project(
    parent: QtWidgets.QWidget | None, project_file: str | Path, *,
    open_updates: Callable[[], None] | None = None,
) -> bool:
    """Refuse incompatible settings before allocating a tab or loading data.

    Ordinary read/validation errors are left to the normal project loader.
    """
    try:
        read_reduction_project(project_file)
    except NewerFileError as error:
        show_newer_file_dialog(parent, error, open_updates=open_updates)
        return True
    except (OSError, ValueError):
        pass
    return False
