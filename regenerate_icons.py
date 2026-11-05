"""Regenerate Studio PNG, iconset, ICO and ICNS assets from upbrpy_studio.svg.

Run ``python regenerate_icons.py`` from the repository root. Each PNG size is
rendered directly from the master with rsvg-convert to preserve SVG shadows;
if it is unavailable, the Qt exporter is used with a warning about lost shadows.
Platform containers use these rendered sizes without downscaling. Optionally
pass ``--stl-dir DIR`` to rebuild the master with make_icon.py first.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import struct
import subprocess
import sys

from PIL import Image

from GUI.icon import (
    ICON_ASSET_DIR,
    ICONSET_EXPORT_SPECS,
    LAB_ICON_SVG_PATH,
    PNG_ICON_SIZES,
    export_icon_png_series,
    export_iconset,
)


def _write_icns(path: Path, iconset_paths: list[Path]) -> None:
    """Pack direct renders into ICNS resources, including 16px and Retina."""
    chunk_types = (
        b"is32", b"ic11", b"il32", b"ic12", b"ic07",
        b"ic13", b"ic08", b"ic14", b"ic09", b"ic10",
    )
    chunks = []

    def append_chunk(kind: bytes, data: bytes) -> None:
        chunks.append(struct.pack(">4sI", kind, len(data) + 8) + data)

    for kind, png_path in zip(chunk_types, iconset_paths, strict=True):
        if kind in (b"is32", b"il32"):
            # macOS expects legacy RGB + alpha resources for native 16/32px.
            # Encode each RGB channel as ICNS RLE literal packets (1–128 bytes).
            with Image.open(png_path) as image:
                red, green, blue, alpha = image.convert("RGBA").split()
            packets = []
            for band in (red, green, blue):
                data = band.tobytes()
                for offset in range(0, len(data), 128):
                    block = data[offset:offset + 128]
                    packets.append(bytes([len(block) - 1]) + block)
            append_chunk(kind, b"".join(packets))
            append_chunk(b"s8mk" if kind == b"is32" else b"l8mk", alpha.tobytes())
        else:
            append_chunk(kind, png_path.read_bytes())
    payload = b"".join(chunks)
    path.write_bytes(struct.pack(">4sI", b"icns", len(payload) + 8) + payload)


def regenerate() -> tuple[Path, ...]:
    """Write PNG, Apple iconset, Windows ICO, and macOS ICNS assets."""
    converter = shutil.which("rsvg-convert")
    png_dir = ICON_ASSET_DIR / "png"
    iconset_dir = ICON_ASSET_DIR / "upbrpy_zircon.iconset"
    if converter is None:
        print("Warning: rsvg-convert is missing; using Qt export. "
              "SVG shadows are lost because QtSvg ignores the filters.",
              file=sys.stderr)
        png_paths = export_icon_png_series(png_dir)
        iconset_paths = export_iconset(iconset_dir)
    else:
        png_dir.mkdir(parents=True, exist_ok=True)
        iconset_dir.mkdir(parents=True, exist_ok=True)
        png_paths = []
        for size in PNG_ICON_SIZES:
            target = png_dir / f"upbrpy_zircon_{size}.png"
            subprocess.run(
                [converter, "-w", str(size), "-h", str(size),
                 "-o", str(target), str(LAB_ICON_SVG_PATH)],
                check=True,
            )
            png_paths.append(target)
        iconset_paths = []
        for size, name in ICONSET_EXPORT_SPECS:
            target = iconset_dir / name
            shutil.copyfile(png_dir / f"upbrpy_zircon_{size}.png", target)
            iconset_paths.append(target)

    # Supply every frame explicitly so Pillow never resizes the 1024px image.
    frames = []
    for png_path in png_paths:
        with Image.open(png_path) as image:
            frames.append(image.convert("RGBA"))

    ico_path = ICON_ASSET_DIR / "upbrpy_zircon.ico"
    frames[-1].save(
        ico_path,
        format="ICO",
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
        append_images=frames,
    )
    icns_path = ICON_ASSET_DIR / "upbrpy_zircon.icns"
    _write_icns(icns_path, iconset_paths)
    return (*png_paths, *iconset_paths, ico_path, icns_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stl-dir", type=Path,
                        help="Rebuild the SVG from a Mineral Habit Matrix zircon STL directory")
    args = parser.parse_args()
    if args.stl_dir is not None:
        subprocess.run(
            [sys.executable, str(ICON_ASSET_DIR / "make_icon.py"),
             str(args.stl_dir)],
            check=True,
        )
    written = regenerate()
    print(f"Regenerated {len(written)} icon assets from SVG.")
