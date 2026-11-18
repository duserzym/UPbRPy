"""Keep every graphical part of a legend series visible or hidden together."""
from __future__ import annotations

import weakref


def _shown(item):
    """Return the item's own visibility flag, ignoring hidden parents.

    QGraphicsItem.isVisible() is also False while an ancestor is hidden, and
    Qt notifies children when a parent hides. Only the legend toggle on the
    item itself may record a series as hidden.
    """
    parent = item.parentItem()
    return item.isVisibleTo(parent) if parent is not None else item.isVisible()


class LegendSeries:
    """Remember hidden label keys while retaining only weak graphics references.

    Call reset before clearing a plot, then bind each freshly drawn series.
    A callback supports batched graphics whose rows must be filtered instead.
    Visibility is local to the widget and never changes analytical inclusion.
    """

    def __init__(self):
        self.hidden = set()
        self._bindings = {}

    def reset(self):
        """Disconnect old items before plot.clear; keep label preferences."""
        for primary, slot, _parts in self._bindings.values():
            item = primary()
            if item is not None:
                try:
                    item.visibleChanged.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._bindings.clear()

    def items(self):
        """Yield live (key, primary, companions) for inspecting linked series."""
        for key, (source, _slot, parts) in self._bindings.items():
            if source() is not None:
                yield key, source(), [ref() for ref in parts if ref() is not None]

    def bind(self, key, primary, companions=(), *, legend=None, changed=None):
        """Link a primary legend item to companions and an optional callback."""
        previous = self._bindings.pop(key, None)
        if previous is not None and previous[0]() is not None:
            try:
                previous[0]().visibleChanged.disconnect(previous[1])
            except (RuntimeError, TypeError):
                pass
        owner = weakref.ref(self)
        source = weakref.ref(primary)
        parts = [weakref.ref(item) for item in companions]
        labels = []
        if legend is not None:
            labels = [weakref.ref(item) for sample, label in legend.items
                      if sample.item is primary for item in (sample, label)]
        callback = weakref.WeakMethod(changed) if getattr(changed, '__self__', None) is not None else None

        def sync(*_):
            controller, item = owner(), source()
            if controller is None or item is None:
                return
            visible = _shown(item)
            if visible:
                controller.hidden.discard(key)
            else:
                controller.hidden.add(key)
            for ref in parts:
                part = ref()
                if part is not None:
                    part.setVisible(visible)
            for ref in labels:
                label = ref()
                if label is not None:
                    label.setOpacity(1. if visible else .4)
            if callback is not None and callback() is not None:
                callback()(key, visible)

        primary.setVisible(key not in self.hidden)
        primary.visibleChanged.connect(sync)
        self._bindings[key] = (source, sync, parts)
        # Initial state without invoking a row-redraw callback during assembly.
        shown = _shown(primary)
        for item in companions:
            item.setVisible(shown)
        for ref in labels:
            if ref() is not None:
                ref().setOpacity(1. if shown else .4)
