"""Studio version metadata and shared update-state compatibility imports."""

from importlib import metadata
from pathlib import Path
import tomllib

from GUI.labupdate.install import is_frozen
from GUI.labupdate.state import UpdateState, transition, button_view as check_button_view
from GUI.labupdate.manifest import compare_versions


def installed_version() -> str:
    """Read the frozen build's stamp, or installed/source package metadata."""
    if is_frozen():
        # Read the spec-generated metadata directly: sys.path may contain an
        # unrelated editable install. A missing stamp must fail, not guess.
        bundled = Path(__file__).resolve().parents[1] / "UPbRPy.egg-info"
        version = metadata.PathDistribution(bundled).metadata.get("Version")
        if not version:
            raise RuntimeError(f"Missing frozen UPbRPy version metadata: {bundled}")
        return version
    try:
        return metadata.version("UPbRPy")
    except metadata.PackageNotFoundError:
        project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        return tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
