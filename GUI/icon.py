"""Native application-icon rendering and asset export."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtSvg


PNG_ICON_SIZES = [16, 24, 32, 48, 64, 96, 128, 256, 512, 1024]
ICONSET_EXPORT_SPECS = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]

ICON_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "icons"
LAB_ICON_SVG_PATH = ICON_ASSET_DIR / "upbrpy_isotope_geology.svg"
LAB_ICON_RENDERER: QtSvg.QSvgRenderer | None = None


def render_icon_image(size: int = 256) -> QtGui.QImage:
    """Render the scalable isotope-geology laboratory application mark.

    The primary source is an SVG that combines the laboratory mark's isotope
    orbits and mountain relief with an idealized tetragonal zircon. The
    programmatic crystal renderer remains as a packaging fallback.
    """
    image = QtGui.QImage(
        size,
        size,
        QtGui.QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(QtCore.Qt.GlobalColor.transparent)

    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(
        QtGui.QPainter.RenderHint.SmoothPixmapTransform,
        True,
    )
    renderer = load_lab_icon_renderer()
    if renderer is not None:
        renderer.render(
            painter,
            QtCore.QRectF(0.0, 0.0, float(size), float(size)),
        )
        painter.end()
        return image

    icon_rect = QtCore.QRectF(
        size * 0.055,
        size * 0.045,
        size * 0.89,
        size * 0.89,
    )
    radius = size * 0.205
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    for offset, alpha in (
        (size * 0.025, 18),
        (size * 0.016, 28),
        (size * 0.009, 36),
    ):
        shadow = icon_rect.translated(0, offset)
        painter.setBrush(QtGui.QColor(13, 21, 42, alpha))
        painter.drawRoundedRect(shadow, radius, radius)

    background = QtGui.QLinearGradient(
        icon_rect.topLeft(),
        icon_rect.bottomRight(),
    )
    background.setColorAt(0.0, QtGui.QColor("#172a52"))
    background.setColorAt(0.52, QtGui.QColor("#304985"))
    background.setColorAt(1.0, QtGui.QColor("#6f4e91"))
    painter.setBrush(background)
    painter.drawRoundedRect(icon_rect, radius, radius)

    highlight = QtGui.QRadialGradient(
        QtCore.QPointF(size * 0.30, size * 0.20),
        size * 0.62,
    )
    highlight.setColorAt(0.0, QtGui.QColor(136, 229, 255, 92))
    highlight.setColorAt(0.55, QtGui.QColor(126, 177, 255, 14))
    highlight.setColorAt(1.0, QtGui.QColor(40, 38, 86, 0))
    painter.setBrush(highlight)
    painter.drawRoundedRect(icon_rect, radius, radius)

    draw_crystallographic_zircon(painter, size)
    painter.end()
    return image


def load_lab_icon_renderer() -> QtSvg.QSvgRenderer | None:
    """Return the cached valid SVG renderer used by every icon size."""
    global LAB_ICON_RENDERER
    if LAB_ICON_RENDERER is None and LAB_ICON_SVG_PATH.exists():
        candidate = QtSvg.QSvgRenderer(str(LAB_ICON_SVG_PATH))
        if candidate.isValid():
            LAB_ICON_RENDERER = candidate
    return LAB_ICON_RENDERER


def draw_crystallographic_zircon(
    painter: QtGui.QPainter,
    size: int,
) -> None:
    """Draw the faceted tetragonal prism and dipyramidal terminations."""
    points = {
        "apex_top": QtCore.QPointF(size * 0.50, size * 0.135),
        "top_left": QtCore.QPointF(size * 0.285, size * 0.325),
        "top_mid": QtCore.QPointF(size * 0.50, size * 0.285),
        "top_right": QtCore.QPointF(size * 0.715, size * 0.325),
        "bottom_left": QtCore.QPointF(size * 0.285, size * 0.675),
        "bottom_mid": QtCore.QPointF(size * 0.50, size * 0.715),
        "bottom_right": QtCore.QPointF(size * 0.715, size * 0.675),
        "apex_bottom": QtCore.QPointF(size * 0.50, size * 0.865),
    }

    silhouette = QtGui.QPainterPath()
    silhouette.moveTo(points["apex_top"])
    silhouette.lineTo(points["top_right"])
    silhouette.lineTo(points["bottom_right"])
    silhouette.lineTo(points["apex_bottom"])
    silhouette.lineTo(points["bottom_left"])
    silhouette.lineTo(points["top_left"])
    silhouette.closeSubpath()

    painter.save()
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor(7, 11, 31, 55))
    shadow_transform = QtGui.QTransform()
    shadow_transform.translate(size * 0.018, size * 0.025)
    painter.drawPath(shadow_transform.map(silhouette))

    facet_pen = QtGui.QPen(
        QtGui.QColor(224, 244, 255, 165),
        max(0.8, size * 0.004),
    )
    facet_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    facets = [
        (
            [
                points["apex_top"],
                points["top_left"],
                points["top_mid"],
            ],
            "#c4f1ff",
            "#668be0",
        ),
        (
            [
                points["apex_top"],
                points["top_mid"],
                points["top_right"],
            ],
            "#9ee5f4",
            "#8059b6",
        ),
        (
            [
                points["top_left"],
                points["bottom_left"],
                points["bottom_mid"],
                points["top_mid"],
            ],
            "#6ed5df",
            "#426fc3",
        ),
        (
            [
                points["top_mid"],
                points["bottom_mid"],
                points["bottom_right"],
                points["top_right"],
            ],
            "#527dc9",
            "#7652a4",
        ),
        (
            [
                points["bottom_left"],
                points["apex_bottom"],
                points["bottom_mid"],
            ],
            "#52a8ce",
            "#385caa",
        ),
        (
            [
                points["bottom_mid"],
                points["apex_bottom"],
                points["bottom_right"],
            ],
            "#5d70b5",
            "#62418c",
        ),
    ]
    for vertices, start_color, end_color in facets:
        path = QtGui.QPainterPath()
        path.moveTo(vertices[0])
        for vertex in vertices[1:]:
            path.lineTo(vertex)
        path.closeSubpath()
        gradient = QtGui.QLinearGradient(
            path.boundingRect().topLeft(),
            path.boundingRect().bottomRight(),
        )
        gradient.setColorAt(0.0, QtGui.QColor(start_color))
        gradient.setColorAt(1.0, QtGui.QColor(end_color))
        painter.setPen(facet_pen)
        painter.setBrush(gradient)
        painter.drawPath(path)

    outline = QtGui.QPen(
        QtGui.QColor(235, 250, 255, 220),
        max(1.0, size * 0.006),
    )
    outline.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(outline)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawPath(silhouette)

    if size >= 48:
        # These subtle cross-lines read as facets at normal icon sizes while
        # also suggesting the sweep-wise depth-profile focus of UPbRPy.
        depth_pen = QtGui.QPen(
            QtGui.QColor(236, 252, 255, 115),
            max(0.7, size * 0.0028),
        )
        depth_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(depth_pen)
        for fraction in (0.42, 0.50, 0.58):
            painter.drawLine(
                QtCore.QPointF(size * 0.31, size * fraction),
                QtCore.QPointF(size * 0.69, size * fraction),
            )
    painter.restore()


def render_icon_pixmap(size: int = 256) -> QtGui.QPixmap:
    """Return the rendered icon as a Qt pixmap."""
    return QtGui.QPixmap.fromImage(render_icon_image(size))


def export_icon_png_series(output_dir: Path) -> list[Path]:
    """Write standard PNG icon sizes and return their paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size in PNG_ICON_SIZES:
        target = output_dir / f"upbrpy_zircon_{size}.png"
        render_icon_image(size).save(str(target), "PNG")
        written.append(target)
    return written


def export_iconset(output_dir: Path) -> list[Path]:
    """Write the PNG filenames expected by an Apple iconset."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size, file_name in ICONSET_EXPORT_SPECS:
        target = output_dir / file_name
        render_icon_image(size).save(str(target), "PNG")
        written.append(target)
    return written


def build_app_icon() -> QtGui.QIcon:
    """Build a multi-resolution Qt application icon."""
    icon = QtGui.QIcon()
    for size in [16, 24, 32, 48, 64, 96, 128, 256, 512]:
        icon.addPixmap(render_icon_pixmap(size))
    return icon


def build_brand_pixmap(size: int = 56) -> QtGui.QPixmap:
    """Return the in-app brand image at the requested pixel size."""
    return render_icon_pixmap(size)


def build_workflow_stage_icon(
    stage: str,
    *,
    state: str = "ready",
    size: int = 28,
    accent: str = "#2468d4",
    muted: str = "#788da6",
    success: str = "#159a76",
) -> QtGui.QIcon:
    """Build a semantic vector-style icon for one reduction stage.

    The small glyphs are painted from primitives rather than loaded from
    raster artwork, so they remain sharp on Retina and high-DPI displays.
    ``state`` may be ``locked``, ``ready``, ``current``, or ``complete``.
    """
    canvas_size = max(16, int(size))
    image = QtGui.QImage(
        canvas_size * 2,
        canvas_size * 2,
        QtGui.QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.setDevicePixelRatio(2.0)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

    color = QtGui.QColor(
        success if state == "complete" else muted if state == "locked" else accent
    )
    background = QtGui.QColor(color)
    background.setAlpha(38 if state != "current" else 64)
    rect = QtCore.QRectF(1.5, 1.5, canvas_size - 3.0, canvas_size - 3.0)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(background)
    painter.drawRoundedRect(rect, canvas_size * 0.28, canvas_size * 0.28)

    pen = QtGui.QPen(color, max(1.25, canvas_size * 0.065))
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

    left = canvas_size * 0.25
    right = canvas_size * 0.75
    top = canvas_size * 0.25
    bottom = canvas_size * 0.75
    center = QtCore.QPointF(canvas_size * 0.50, canvas_size * 0.50)
    stage_name = str(stage)

    if stage_name == "import":
        painter.drawRoundedRect(
            QtCore.QRectF(left, top + 2, right - left, bottom - top - 1),
            2,
            2,
        )
        painter.drawLine(center.x(), top, center.x(), bottom - 4)
        painter.drawLine(center.x(), bottom - 4, center.x() - 3, bottom - 7)
        painter.drawLine(center.x(), bottom - 4, center.x() + 3, bottom - 7)
    elif stage_name == "selection":
        painter.drawLine(left, top, left, top + 5)
        painter.drawLine(left, top, left + 5, top)
        painter.drawLine(right, top, right - 5, top)
        painter.drawLine(right, top, right, top + 5)
        painter.drawLine(left, bottom, left, bottom - 5)
        painter.drawLine(left, bottom, left + 5, bottom)
        painter.drawLine(right, bottom, right - 5, bottom)
        painter.drawLine(right, bottom, right, bottom - 5)
        painter.drawLine(left + 3, center.y(), right - 3, center.y())
    elif stage_name == "baseline":
        painter.drawLine(left, bottom, right, bottom)
        painter.drawLine(left, bottom, left, top)
        painter.drawLine(left + 2, bottom - 5, right - 2, bottom - 5)
    elif stage_name == "normalization":
        for y, knob in ((top + 2, 0.62), (center.y(), 0.40), (bottom - 2, 0.58)):
            painter.drawLine(left, y, right, y)
            x = canvas_size * knob
            painter.setBrush(color)
            painter.drawEllipse(QtCore.QPointF(x, y), 1.8, 1.8)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    elif stage_name == "concentration":
        path = QtGui.QPainterPath()
        path.moveTo(center.x() - 3, top)
        path.lineTo(center.x() - 3, center.y() - 2)
        path.lineTo(left + 2, bottom)
        path.lineTo(right - 2, bottom)
        path.lineTo(center.x() + 3, center.y() - 2)
        path.lineTo(center.x() + 3, top)
        painter.drawPath(path)
        painter.drawLine(left + 5, bottom - 5, right - 5, bottom - 5)
    elif stage_name == "ratios":
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QPointF(left + 3, top + 3), 2, 2)
        painter.drawEllipse(QtCore.QPointF(right - 3, bottom - 3), 2, 2)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawLine(left + 3, bottom - 1, right - 3, top + 1)
    elif stage_name == "downhole":
        points = [
            QtCore.QPointF(left, top + 2),
            QtCore.QPointF(center.x() - 2, center.y() - 1),
            QtCore.QPointF(right, bottom - 2),
        ]
        painter.drawPolyline(QtGui.QPolygonF(points))
        painter.drawLine(right, bottom - 2, right - 1, bottom - 7)
    elif stage_name == "session":
        painter.drawEllipse(center, (right - left) / 2, (bottom - top) / 2)
        painter.drawLine(center, QtCore.QPointF(center.x(), top + 4))
        painter.drawLine(center, QtCore.QPointF(right - 4, center.y() + 2))
    elif stage_name == "spot_means":
        painter.drawEllipse(center, 7, 7)
        painter.drawEllipse(center, 3, 3)
        painter.setBrush(color)
        painter.drawEllipse(center, 1.4, 1.4)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    elif stage_name == "depth_profile":
        for offset in (0, 4, 8):
            painter.drawLine(
                left + 2,
                top + 2 + offset,
                right - 2,
                top + 2 + offset,
            )
        painter.drawLine(center.x(), top, center.x(), bottom)
        painter.drawLine(center.x(), bottom, center.x() - 3, bottom - 4)
        painter.drawLine(center.x(), bottom, center.x() + 3, bottom - 4)
    elif stage_name in {"finalize", "export"}:
        painter.drawEllipse(center, 8, 8)
        painter.drawLine(left + 5, center.y(), center.x() - 1, bottom - 5)
        painter.drawLine(center.x() - 1, bottom - 5, right - 4, top + 5)
    else:
        painter.drawEllipse(center, 7, 7)

    if state == "complete":
        painter.setPen(
            QtGui.QPen(QtGui.QColor(success), max(1.2, canvas_size * 0.06))
        )
        painter.drawEllipse(
            QtCore.QPointF(canvas_size * 0.78, canvas_size * 0.22),
            2.3,
            2.3,
        )
    painter.end()
    return QtGui.QIcon(QtGui.QPixmap.fromImage(image))
