"""The channel tile grid lays out at any width without errors.

On 2026-11-11 the tiles briefly used the sample-chip FlowLayout, whose layout
pass raised for every tile; under pytest the nested error messages kept
growing on every resize and one run reached 11 GB. This test lays the grid
out at many widths and fails on the first error, on a bare widget.
"""

import sys
import traceback

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtWidgets

from GUI.theme import build_application
from GUI.widgets import IsotopeGridWidget, TileFlowLayout

CHANNELS = ["29Si", "31P", "49Ti", "89Y", "91Zr", "93Nb", "139La", "140Ce", "141Pr", "146Nd",
            "147Sm", "151Eu", "157Gd", "159Tb", "163Dy", "165Ho", "166Er", "169Tm", "172Yb",
            "175Lu", "177Hf", "181Ta", "202Hg", "204Hg", "206Pb", "207Pb", "208Pb", "232Th", "238U"]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def test_tiles_wrap_inside_the_grid_at_every_width(app, monkeypatch):
    errors = []
    def record(kind, error, trace):
        # Only errors raised while laying out the grid (other widgets left by
        # earlier tests may report their own).
        text = "".join(traceback.format_tb(trace))
        if any(name in text for name in ("_arrange", "IsotopeGrid", "_make_chip", "height_for_width")):
            errors.append(error)

    monkeypatch.setattr(sys, "excepthook", record)
    grid = IsotopeGridWidget()
    grid.set_channels(CHANNELS, set(CHANNELS[:4]), {}, theme_name="light")
    grid.show()
    flows = [child.layout() for child in grid.findChildren(QtWidgets.QWidget)
             if isinstance(child.layout(), TileFlowLayout)]
    assert flows, "channel tiles are laid out by TileFlowLayout"
    for width in (120, 180, 230, 260, 320, 420, 260, 180):
        grid.resize(width, 900)
        app.processEvents()
        assert not errors, errors[0]
        for button in grid._buttons.values():
            assert button.size().width() == 36 and button.size().height() == 34
            assert button.geometry().right() <= button.parentWidget().width()
        # Narrower grids need more rows, never fewer.
        heights = [flow.heightForWidth(width) for flow in flows]
        assert all(height > 0 for height in heights)
    grid.close()
