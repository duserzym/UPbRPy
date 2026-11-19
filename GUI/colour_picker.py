"""Compact palette picker shared by tree rows and concordia legends."""
from PySide6 import QtCore, QtGui, QtWidgets
from GUI.spot_colours import SAMPLE_PALETTE, SPOT_PALETTE


class ColourPicker(QtWidgets.QMenu):
    """Offer quick swatches, an opaque RGB dialog and a reset action."""

    colourChosen = QtCore.Signal(object)  # None means reset

    def __init__(self, current, parent=None, *, spots=False):
        super().__init__(parent)
        self.current = current
        panel = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(panel)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(4)
        self.swatches = {}
        for i, colour in enumerate(SPOT_PALETTE if spots else SAMPLE_PALETTE):
            button = QtWidgets.QToolButton()
            button.setFixedSize(26, 26)
            button.setToolTip(colour)
            button.setAccessibleName(f"Choose {colour}")
            button.setStyleSheet(f"QToolButton {{ background: {colour}; border: 2px solid "
                                + ("#222222" if colour == current else "transparent") + "; border-radius: 4px; }")
            button.clicked.connect(lambda checked=False, colour=colour: self._choose(colour))
            self.swatches[colour] = button
            grid.addWidget(button, i // 5, i % 5)
        action = QtWidgets.QWidgetAction(self)
        action.setDefaultWidget(panel)
        self.addAction(action)
        self.addAction("Custom colour…", self._custom)
        self.addAction("Reset to default", lambda: self._choose(None))

    def _choose(self, colour):
        self.close()
        self.colourChosen.emit(colour)

    def _custom(self):
        colour = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.current), self.parentWidget(), "Choose colour")
        if colour.isValid():
            self._choose(colour.name(QtGui.QColor.NameFormat.HexRgb))
