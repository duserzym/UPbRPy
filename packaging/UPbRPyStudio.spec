"""PyInstaller recipe for the native UPbRPy Studio desktop application."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
import sys
import tomllib

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


PACKAGING_DIRECTORY = Path(SPECPATH).resolve()
PROJECT_ROOT = PACKAGING_DIRECTORY.parent
PROJECT_METADATA = tomllib.loads(
    (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)
PROJECT_VERSION = PROJECT_METADATA["project"]["version"]
APPLICATION_NAME = "UPbRPy Studio"

data_files = [
    (
        str(PROJECT_ROOT / "UPbRPy" / "reference_materials"),
        "UPbRPy/reference_materials",
    ),
    (
        str(PROJECT_ROOT / "GUI" / "assets" / "icons"),
        "GUI/assets/icons",
    ),
    (
        str(PROJECT_ROOT / "LICENSE"),
        ".",
    ),
]
data_files += copy_metadata("UPbRPy")

hidden_imports = [
    "PySide6.QtSvg",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtNetwork",
    "xlsxwriter",
    *collect_submodules("pyqtgraph.exporters"),
]

# Database mode (the optional [database] extra) is bundled when the build
# environment has it; the client imports requests and keyring lazily, so
# PyInstaller cannot see them on its own. keyring finds its OS backends
# through entry points, hence its metadata.
if find_spec("requests") is not None and find_spec("keyring") is not None:
    hidden_imports += [
        "UPbRPy.integration.upbdb",
        "requests",
        "keyring",
        *collect_submodules("keyring.backends"),
    ]
    data_files += copy_metadata("keyring")

analysis = Analysis(
    [str(PROJECT_ROOT / "GUI" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=data_files,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "bokeh",
        "cairosvg",
        "jedi",
        "jupyterlab",
        "llvmlite",
        "matplotlib",
        "nbformat",
        "notebook",
        "numba",
        "openpyxl",
        "panel",
        "pygments",
        "pytest",
        "selenium",
        "tkinter",
        "traitlets",
        "zmq",
    ],
    noarchive=False,
    optimize=1,
)

# An editable install records the builder's checkout path in
# direct_url.json; a distributed app must not carry it.
analysis.datas = [
    entry for entry in analysis.datas
    if not (entry[0].endswith("direct_url.json") and ".dist-info" in entry[0])
]

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APPLICATION_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=(
        str(PROJECT_ROOT / "GUI" / "assets" / "icons" / "upbrpy_zircon.ico")
        if sys.platform == "win32"
        else None
    ),
)

application_directory = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APPLICATION_NAME,
)

if sys.platform == "darwin":
    application_bundle = BUNDLE(
        application_directory,
        name=f"{APPLICATION_NAME}.app",
        icon=str(
            PROJECT_ROOT
            / "GUI"
            / "assets"
            / "icons"
            / "upbrpy_zircon.icns"
        ),
        bundle_identifier="org.upbrpy.studio",
        version=PROJECT_VERSION,
        info_plist={
            "CFBundleDisplayName": APPLICATION_NAME,
            "CFBundleName": APPLICATION_NAME,
            "CFBundleShortVersionString": PROJECT_VERSION,
            "CFBundleVersion": PROJECT_VERSION,
            "LSApplicationCategoryType": (
                "public.app-category.education"
            ),
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # upbr:// links (database mode) open Studio; Qt delivers them
            # as QFileOpenEvent (GUI/launch.py).
            "CFBundleURLTypes": [
                {
                    "CFBundleURLName": "org.upbrpy.studio.link",
                    "CFBundleURLSchemes": ["upbr"],
                    "CFBundleTypeRole": "Viewer",
                }
            ],
        },
    )
