"""The middle column gives its height to the plot canvas on laptops."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.main_window import MainWindow
from GUI.services import project_summary
from GUI.studio_window import StudioWindow
from GUI.theme import build_application


FIXTURE = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def pump(app):
    for _ in range(15):
        app.processEvents()


@pytest.fixture(params=[False, True], ids=["standalone", "studio-tab"])
def workspace(app, request):
    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    shell = StudioWindow(theme_mode="light") if request.param else MainWindow(theme_mode="light")
    window = shell.active_reduction if request.param else shell
    shell.show()
    try:
        assert window.project_summary_label.text() == "No experiment loaded"
        window.load_experiment_directory(str(FIXTURE), background=False)
        yield shell, window
    finally:
        window.has_unsaved_changes = lambda: False
        shell.close()
        pump(app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)


@pytest.mark.parametrize("size", [(1440, 900), (1280, 800)])
def test_plot_canvas_uses_the_middle_column(app, workspace, size):
    shell, window = workspace
    shell.resize(*size)
    window.apply_default_layout()
    pump(app)

    column = window.main_panel
    visible = [column.layout().itemAt(i).widget() for i in range(column.layout().count())
               if not column.layout().itemAt(i).isEmpty()]
    assert visible == [window.content_splitter]
    assert window.content_splitter.widget(0) is window.plot_card
    assert window.plot_card.isVisible()
    assert 0 <= window.plot_card.mapTo(column, QtCore.QPoint()).y() <= 12
    assert window.plot_card.height() == column.height()

    # Offscreen measured 517/433 px standalone and 521/421 px in Studio
    # before the change, rather than the display's 553/453 px. Reconstruct
    # that reference from the column height minus the legacy header, stage
    # card, two gaps, and the current plot card's unchanged fixed parts.
    legacy_header = 68 if shell is not window or size[1] < 880 else 84
    legacy_card_height = column.height() - legacy_header - 62 - 2 * column.layout().spacing()
    fixed_plot_parts = window.plot_card.height() - window.plots_stack.height()
    reference_canvas_height = legacy_card_height - fixed_plot_parts
    assert window.plots_stack.height() >= reference_canvas_height + 136

    caption = window.step_actions_caption
    assert caption.height() <= 28
    assert not caption.findChildren(QtWidgets.QPushButton)
    for label in (window.step_actions_title, window.workflow_status_label):
        assert label.fontMetrics().height() <= label.height()
        if label.isVisible():
            assert caption.rect().contains(label.geometry())
            shown = QtWidgets.QLabel.text(label)
            assert label.fontMetrics().horizontalAdvance(shown) <= label.width()
            assert shown == label.text() or ("…" in shown and label.toolTip())
        assert label.toolTip() == window.step_actions_block.toolTip()
    row = window.workflow_step_list.currentRow()
    step = window.workflow_preset.steps[row]
    assert window.step_actions_title.text() == f"{row + 1} · {step.title}"
    assert window.step_actions_block.toolTip() == f"{window._workflow_full_status}\n\n{step.hint}"

    summary = project_summary(window.session)
    window._apply_project_summary(summary)
    pump(app)
    label = window.project_summary_label
    full_text = f"{summary['name']} · {summary['spots']}"
    assert label.text() == full_text
    assert summary["name"] in QtWidgets.QLabel.text(label)
    assert "12 spots" in QtWidgets.QLabel.text(label)
    assert 0 < label.width() <= 360
    assert label.fontMetrics().horizontalAdvance(QtWidgets.QLabel.text(label)) <= label.width()
    assert label.toolTip() == f"{full_text}\n{summary['engine']}"
    assert label.parentWidget() is window.statusBar()
    assert label.geometry().right() < window.capabilities_badge.x()

    long_name = "zircon_session_" * 20
    window._apply_project_summary({**summary, "name": long_name})
    pump(app)
    assert label.width() <= 360
    assert QtWidgets.QLabel.text(label).endswith("…")
    assert label.toolTip() == f"{long_name} · {summary['spots']}\n{summary['engine']}"
    window._apply_project_summary({**summary, "name": "No experiment loaded"})
    assert label.text() == "No experiment loaded"


def test_compact_inspector_remains_available_on_table_pages(app, workspace):
    shell, window = workspace
    shell.resize(900, 700)
    pump(app)
    window.set_plot_mode("final_results", refresh=False)
    window.on_results_review_tab_changed(0)
    pump(app)
    assert not window.reset_scale_button.isEnabled()
    assert window.inspector_toggle.isVisible()
    assert window.inspector_toggle.isEnabled()
    window.inspector_toggle.click()
    pump(app)
    assert window.inspector_panel.isVisible()
    window.inspector_toggle.click()
    window.set_plot_mode("raw", refresh=False)
    assert window.reset_scale_button.isEnabled()
    assert not window.ratio_combo.isEnabled()
