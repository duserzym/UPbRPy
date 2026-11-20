"""AST and isolated import checks for the copyable package dependency boundary."""

import ast
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1] / "GUI/labupdate"


def test_only_allowed_imports_and_docstrings():
    allowed = set(sys.stdlib_module_names) | {"PySide6", "truststore", "certifi"}
    for path in ROOT.glob("*.py"):
        source = path.read_text()
        tree = ast.parse(source)
        assert source.startswith('"""') and ast.get_docstring(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.level == 1
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed, (path.name, name)
                if name.startswith("PySide6"):
                    assert path.name == "qt.py"
                if name in {"certifi", "truststore"}:
                    assert path.name == "tls.py"
                    assert any(isinstance(parent, ast.Try) and node in list(ast.walk(parent))
                               and any(isinstance(handler.type, ast.Name) and handler.type.id == "ImportError" for handler in parent.handlers)
                               for parent in ast.walk(tree))


def test_copied_package_imports_without_repo_or_qt(tmp_path):
    shutil.copytree(ROOT, tmp_path / "labupdate")
    script = '''import sys
sys.path.insert(0, sys.argv[1])
import labupdate
from labupdate import config, tls, minisign, manifest, install, state, selftest
assert 'GUI' not in sys.modules and 'UPbRPy' not in sys.modules
assert 'PySide6' not in sys.modules
assert config.UpdateConfig('Other', 'lab/other', '', settings_org='lab', settings_app='other')
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_source_instructions():
    source = (ROOT / "SOURCE.md").read_text()
    assert "duserzym/UPbRPy" in source and "VENDORED" in source and "source commit" in source
