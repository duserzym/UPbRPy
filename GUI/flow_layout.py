"""A height-for-width layout for wrapping compact column widgets."""
from PySide6 import QtCore, QtWidgets


class FlowLayout(QtWidgets.QLayout):
    """Arrange visible widgets in rows without imposing their natural width."""

    def __init__(self, parent=None, *, spacing=6):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QtCore.QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        # Width is controlled by the pane, including when a chip has a long label.
        margins = self.contentsMargins()
        height = max((item.minimumSize().height() for item in self._items
                      if not item.isEmpty()), default=0)
        return QtCore.QSize(margins.left() + margins.right(),
                            height + margins.top() + margins.bottom())

    def _arrange(self, rect, test=False):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            size = item.sizeHint()
            width = min(size.width(), max(0, area.width()))
            if x > area.x() and x + width > area.right() + 1:
                x, y = area.x(), y + row_height + self.spacing()
                row_height = 0
            widget = item.widget()
            height = widget.heightForWidth(width) if widget and widget.hasHeightForWidth() else size.height()
            if not test:
                item.setGeometry(QtCore.QRect(x, y, width, height))
            x += width + self.spacing()
            row_height = max(row_height, height)
        return y + row_height - rect.y() + margins.bottom()
