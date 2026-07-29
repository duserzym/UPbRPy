"""Regenerate all platform icons from the lab-inspired SVG source."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from GUI.icon import ICON_ASSET_DIR, export_icon_png_series, export_iconset


def regenerate() -> tuple[Path, ...]:
    """Write PNG, Apple iconset, Windows ICO, and macOS ICNS assets."""
    png_paths = export_icon_png_series(ICON_ASSET_DIR / "png")
    iconset_paths = export_iconset(
        ICON_ASSET_DIR / "upbrpy_zircon.iconset"
    )
    source = Image.open(
        ICON_ASSET_DIR / "png" / "upbrpy_zircon_1024.png"
    ).convert("RGBA")

    ico_path = ICON_ASSET_DIR / "upbrpy_zircon.ico"
    source.save(
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
    )
    icns_path = ICON_ASSET_DIR / "upbrpy_zircon.icns"
    source.save(
        icns_path,
        format="ICNS",
        sizes=[
            (16, 16),
            (32, 32),
            (64, 64),
            (128, 128),
            (256, 256),
            (512, 512),
            (1024, 1024),
        ],
    )
    return (*png_paths, *iconset_paths, ico_path, icns_path)


if __name__ == "__main__":
    written = regenerate()
    print(f"Regenerated {len(written)} icon assets from SVG.")
