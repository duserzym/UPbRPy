"""Studio must not depend on modules the packaged app leaves out.

``packaging/UPbRPyStudio.spec`` excludes matplotlib and bokeh, and PyInstaller
cannot follow ``UPbRPy/__init__.py``'s lazy legacy API (``import_module(
".UPbRPy")``), so ``UPbRPy.UPbRPy`` and ``UPbRPy.visualization`` are not in the
bundle. ``from UPbRPy import <name>`` for a name that is not a submodule goes
through that API; on 2026-11-11 one such import made every project fail to
open in the installed app.
"""

import ast
from pathlib import Path
import subprocess
import sys

import UPbRPy


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"


def test_gui_does_not_import_through_the_lazy_package_api():
    offenders = []
    for path in sorted((ROOT / "GUI").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "UPbRPy" and node.level == 0:
                for alias in node.names:
                    if alias.name not in UPbRPy.FOCUSED_SUBMODULES:
                        offenders.append(f"{path.name}:{node.lineno} imports {alias.name}")
    assert not offenders, "Import from the defining UPbRPy submodule instead: " + "; ".join(offenders)


BLOCKED_RUN = r'''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in ("UPbRPy.UPbRPy", "UPbRPy.visualization") or name.split(".")[0] in ("matplotlib", "bokeh"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv[1])
from GUI.services import ensure_shared_pipeline, load_session, restore_session_from_project
from GUI.workflows import infer_workflow_preset
from UPbRPy.pipeline import PipelineStage
from UPbRPy.project import write_reduction_project
preset = infer_workflow_preset(sys.argv[2])
session = load_session(sys.argv[2], preset=preset)
pipeline = ensure_shared_pipeline(session, preset)
pipeline.run_to(PipelineStage.CONCENTRATION)
project = write_reduction_project(pipeline, sys.argv[3], resume_stage=PipelineStage.SELECTION)
restored = restore_session_from_project(project)
print("ok", restored.session.experiment["spot_names"].nunique())
'''


def test_studio_loads_and_restores_without_unbundled_modules(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", BLOCKED_RUN, str(ROOT), str(FIXTURE), str(tmp_path / "p.upbr")],
        capture_output=True,
        text=True,
        timeout=300,
        env={"QT_QPA_PLATFORM": "offscreen", "UPBRPY_AUTOSAVE_DIR": str(tmp_path / "autosave"),
             "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip().splitlines()[-1].startswith("ok ")
