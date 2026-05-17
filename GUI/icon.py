from __future__ import annotations

import math

from pathlib import Path

from PySide6 import QtCore, QtGui

try:
    from PySide6 import QtSvg
except ImportError:
    QtSvg = None


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
ZIRCON_SVG_PATH = ICON_ASSET_DIR / "zircon_tetragonal_crystal.svg"
_ZIRCON_RENDERER = None
_ZIRCON_SVG_VIEWBOX = QtCore.QRectF(0.0, 0.0, 612.0, 620.0)
_ZIRCON_SVG_CRYSTAL_BOUNDS = QtCore.QRectF(198.0, 148.0, 162.0, 324.0)


def render_icon_image(size: int = 256) -> QtGui.QImage:
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.transparent)

    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)

    center = QtCore.QPointF(size * 0.51, size * 0.49)

    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor(31, 20, 64, 22))
    painter.drawEllipse(QtCore.QRectF(size * 0.24, size * 0.79, size * 0.44, size * 0.06))

    painter.save()
    painter.translate(center)
    painter.rotate(-8)
    painter.translate(-center)

    crystal_rect = QtCore.QRectF(size * 0.335, size * 0.07, size * 0.33, size * 0.64)
    if not _draw_svg_crystal(painter, crystal_rect, size):
        crystal_path = _build_zircon_path(size)
        crystal_gradient = QtGui.QLinearGradient(size * 0.28, size * 0.10, size * 0.70, size * 0.88)
        crystal_gradient.setColorAt(0.0, QtGui.QColor(214, 205, 255, 232))
        crystal_gradient.setColorAt(0.15, QtGui.QColor(170, 150, 246, 224))
        crystal_gradient.setColorAt(0.45, QtGui.QColor(114, 93, 217, 208))
        crystal_gradient.setColorAt(0.73, QtGui.QColor(104, 92, 219, 152))
        crystal_gradient.setColorAt(1.0, QtGui.QColor(222, 231, 255, 92))
        painter.setBrush(crystal_gradient)
        painter.setPen(QtGui.QPen(QtGui.QColor(72, 53, 171, 235), max(1, int(size * 0.007))))
        painter.drawPath(crystal_path)

        painter.save()
        painter.setClipPath(crystal_path)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(255, 255, 255, 38))
        painter.drawPath(_build_top_highlight_path(size))
        painter.setBrush(QtGui.QColor(47, 33, 110, 68))
        painter.drawPath(_build_right_shadow_path(size))
        painter.setBrush(QtGui.QColor(248, 244, 255, 44))
        painter.drawPath(_build_base_glow_path(size))
        _draw_mountain_embedded(painter, size)
        painter.restore()
    painter.restore()

    orbit_pen = max(2, int(size * 0.0085))
    orbit_rect = QtCore.QRectF(size * 0.12, size * 0.29, size * 0.76, size * 0.30)
    _draw_orbit(painter, orbit_rect, 18, QtGui.QColor(79, 31, 21, 224), orbit_pen, 0.20)
    _draw_orbit(painter, orbit_rect.adjusted(size * 0.025, size * 0.03, -size * 0.018, -size * 0.004), -26, QtGui.QColor(92, 38, 24, 220), orbit_pen, 0.60)
    _draw_orbit(painter, orbit_rect.adjusted(-size * 0.018, size * 0.016, size * 0.018, size * 0.012), 2, QtGui.QColor(108, 47, 27, 214), max(2, int(size * 0.0075)), 0.84)

    _draw_title(painter, size)

    painter.end()
    return image


def _draw_orbit(
    painter: QtGui.QPainter,
    rect: QtCore.QRectF,
    angle: float,
    color: QtGui.QColor,
    width: int,
    dot_position: float,
) -> None:
    painter.save()
    center = rect.center()
    painter.translate(center)
    painter.rotate(angle)
    painter.translate(-center)
    pen = QtGui.QPen(color, width)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawEllipse(rect)

    theta = 2.0 * 3.141592653589793 * dot_position
    dot_point = QtCore.QPointF(
        rect.center().x() + rect.width() * 0.5 * math.cos(theta),
        rect.center().y() + rect.height() * 0.5 * math.sin(theta),
    )
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor("#26100d"))
    painter.drawEllipse(dot_point, width * 0.95, width * 0.95)
    painter.setBrush(QtGui.QColor(255, 237, 231, 130))
    painter.drawEllipse(
        QtCore.QPointF(dot_point.x() - width * 0.18, dot_point.y() - width * 0.18),
        width * 0.28,
        width * 0.28,
    )
    painter.restore()


def _draw_svg_crystal(painter: QtGui.QPainter, rect: QtCore.QRectF, size: int) -> bool:
    renderer = _load_zircon_renderer()
    if renderer is None:
        return False

    render_rect = _scaled_svg_render_rect(rect)
    painter.save()
    glow = QtGui.QRadialGradient(
        QtCore.QPointF(rect.center().x() - rect.width() * 0.06, rect.top() + rect.height() * 0.22),
        rect.width() * 0.42,
    )
    glow.setColorAt(0.0, QtGui.QColor(244, 236, 255, 42))
    glow.setColorAt(0.55, QtGui.QColor(176, 148, 246, 14))
    glow.setColorAt(1.0, QtGui.QColor(176, 148, 246, 0))
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(glow)
    painter.drawEllipse(rect.adjusted(-size * 0.015, -size * 0.02, size * 0.015, size * 0.01))

    shadow_rect = render_rect.translated(size * 0.008, size * 0.015)
    painter.setOpacity(0.14)
    renderer.render(painter, shadow_rect)
    painter.setOpacity(1.0)
    renderer.render(painter, render_rect)

    sheen = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
    sheen.setColorAt(0.0, QtGui.QColor(255, 255, 255, 26))
    sheen.setColorAt(0.28, QtGui.QColor(255, 255, 255, 0))
    sheen.setColorAt(0.72, QtGui.QColor(44, 24, 96, 0))
    sheen.setColorAt(1.0, QtGui.QColor(44, 24, 96, 18))
    painter.setBrush(sheen)
    painter.drawRoundedRect(rect.adjusted(rect.width() * 0.14, rect.height() * 0.08, -rect.width() * 0.18, -rect.height() * 0.14), size * 0.018, size * 0.018)
    painter.restore()
    return True


def _load_zircon_renderer():
    global _ZIRCON_RENDERER
    if QtSvg is None or not ZIRCON_SVG_PATH.exists():
        return None
    if _ZIRCON_RENDERER is None:
        renderer = QtSvg.QSvgRenderer(str(ZIRCON_SVG_PATH))
        if not renderer.isValid():
            return None
        _ZIRCON_RENDERER = renderer
    return _ZIRCON_RENDERER


def _scaled_svg_render_rect(target_rect: QtCore.QRectF) -> QtCore.QRectF:
    x_scale = target_rect.width() / _ZIRCON_SVG_CRYSTAL_BOUNDS.width()
    y_scale = target_rect.height() / _ZIRCON_SVG_CRYSTAL_BOUNDS.height()
    render_width = _ZIRCON_SVG_VIEWBOX.width() * x_scale
    render_height = _ZIRCON_SVG_VIEWBOX.height() * y_scale
    render_x = target_rect.left() - _ZIRCON_SVG_CRYSTAL_BOUNDS.left() * x_scale
    render_y = target_rect.top() - _ZIRCON_SVG_CRYSTAL_BOUNDS.top() * y_scale
    return QtCore.QRectF(render_x, render_y, render_width, render_height)


def _build_zircon_path(size: int) -> QtGui.QPainterPath:
    path = QtGui.QPainterPath()
    path.moveTo(size * 0.48, size * 0.08)
    path.lineTo(size * 0.57, size * 0.12)
    path.lineTo(size * 0.64, size * 0.19)
    path.lineTo(size * 0.67, size * 0.29)
    path.lineTo(size * 0.67, size * 0.66)
    path.lineTo(size * 0.64, size * 0.78)
    path.lineTo(size * 0.58, size * 0.88)
    path.lineTo(size * 0.49, size * 0.92)
    path.lineTo(size * 0.40, size * 0.88)
    path.lineTo(size * 0.33, size * 0.79)
    path.lineTo(size * 0.30, size * 0.67)
    path.lineTo(size * 0.30, size * 0.29)
    path.lineTo(size * 0.34, size * 0.19)
    path.lineTo(size * 0.41, size * 0.11)
    path.closeSubpath()
    return path


def _build_top_highlight_path(size: int) -> QtGui.QPainterPath:
    path = QtGui.QPainterPath()
    path.moveTo(size * 0.44, size * 0.11)
    path.lineTo(size * 0.56, size * 0.14)
    path.lineTo(size * 0.62, size * 0.20)
    path.lineTo(size * 0.55, size * 0.23)
    path.lineTo(size * 0.42, size * 0.18)
    path.closeSubpath()
    return path


def _build_right_shadow_path(size: int) -> QtGui.QPainterPath:
    path = QtGui.QPainterPath()
    path.moveTo(size * 0.55, size * 0.14)
    path.lineTo(size * 0.64, size * 0.19)
    path.lineTo(size * 0.67, size * 0.29)
    path.lineTo(size * 0.67, size * 0.66)
    path.lineTo(size * 0.64, size * 0.78)
    path.lineTo(size * 0.58, size * 0.88)
    path.lineTo(size * 0.57, size * 0.48)
    path.closeSubpath()
    return path


def _build_base_glow_path(size: int) -> QtGui.QPainterPath:
    path = QtGui.QPainterPath()
    path.moveTo(size * 0.31, size * 0.60)
    path.cubicTo(size * 0.39, size * 0.57, size * 0.56, size * 0.58, size * 0.67, size * 0.60)
    path.lineTo(size * 0.63, size * 0.88)
    path.lineTo(size * 0.36, size * 0.86)
    path.closeSubpath()
    return path


def _draw_mountain_embedded(painter: QtGui.QPainter, size: int) -> None:
    ridge_pen = QtGui.QPen(QtGui.QColor(203, 209, 240, 120), max(1, int(size * 0.0042)))
    ridge_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    ridge_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(ridge_pen)

    back_ridge = QtGui.QPainterPath()
    back_ridge.moveTo(size * 0.33, size * 0.62)
    back_ridge.lineTo(size * 0.39, size * 0.58)
    back_ridge.lineTo(size * 0.45, size * 0.54)
    back_ridge.lineTo(size * 0.52, size * 0.57)
    back_ridge.lineTo(size * 0.58, size * 0.53)
    back_ridge.lineTo(size * 0.66, size * 0.58)
    painter.drawPath(back_ridge)

    front_ridge = QtGui.QPainterPath()
    front_ridge.moveTo(size * 0.33, size * 0.71)
    front_ridge.lineTo(size * 0.40, size * 0.65)
    front_ridge.lineTo(size * 0.47, size * 0.73)
    front_ridge.lineTo(size * 0.54, size * 0.67)
    front_ridge.lineTo(size * 0.60, size * 0.75)
    front_ridge.lineTo(size * 0.66, size * 0.70)
    painter.drawPath(front_ridge)

    mist = QtGui.QPainterPath()
    mist.moveTo(size * 0.32, size * 0.68)
    mist.cubicTo(size * 0.40, size * 0.64, size * 0.52, size * 0.67, size * 0.68, size * 0.69)
    mist.lineTo(size * 0.68, size * 0.82)
    mist.lineTo(size * 0.32, size * 0.82)
    mist.closeSubpath()
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor(224, 231, 255, 54))
    painter.drawPath(mist)


def _draw_title(painter: QtGui.QPainter, size: int) -> None:
    if size < 160:
        return
    title_font = _pick_title_font(size)
    text = "UPbRPy"
    text_path = QtGui.QPainterPath()
    text_path.addText(QtCore.QPointF(0.0, 0.0), title_font, text)
    bounds = text_path.boundingRect()
    horizontal_offset = (size - bounds.width()) * 0.5 - bounds.left()
    vertical_offset = size * 0.991 - bounds.bottom()
    translation = QtGui.QTransform()
    translation.translate(horizontal_offset, vertical_offset)
    text_path = translation.map(text_path)

    shadow_path = QtGui.QPainterPath(text_path)
    shadow_transform = QtGui.QTransform()
    shadow_transform.translate(size * 0.005, size * 0.006)
    shadow_path = shadow_transform.map(shadow_path)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor(16, 10, 35, 110))
    painter.drawPath(shadow_path)

    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    text_gradient = QtGui.QLinearGradient(0, size * 0.90, 0, size * 0.995)
    text_gradient.setColorAt(0.0, QtGui.QColor(16, 16, 16, 255))
    text_gradient.setColorAt(0.58, QtGui.QColor(24, 24, 24, 255))
    text_gradient.setColorAt(1.0, QtGui.QColor(8, 8, 8, 255))
    painter.setBrush(QtGui.QBrush(text_gradient))
    painter.drawPath(text_path)


def _pick_title_font(size: int) -> QtGui.QFont:
    database = QtGui.QFontDatabase()
    for family in ["Didot", "Avenir Next", "Futura", "Optima", "Helvetica Neue"]:
        if family in database.families():
            font = QtGui.QFont(family)
            break
    else:
        font = QtGui.QFont()
    font.setBold(True)
    font.setWeight(QtGui.QFont.Weight.Black)
    font.setItalic(True)
    font.setPointSizeF(size * 0.078)
    font.setLetterSpacing(QtGui.QFont.SpacingType.PercentageSpacing, 104)
    return font


def render_icon_pixmap(size: int = 256) -> QtGui.QPixmap:
    return QtGui.QPixmap.fromImage(render_icon_image(size))


def export_icon_png_series(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size in PNG_ICON_SIZES:
        target = output_dir / f"upbrpy_zircon_{size}.png"
        render_icon_image(size).save(str(target), "PNG")
        written.append(target)
    return written


def export_iconset(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size, file_name in ICONSET_EXPORT_SPECS:
        target = output_dir / file_name
        render_icon_image(size).save(str(target), "PNG")
        written.append(target)
    return written


def build_app_icon() -> QtGui.QIcon:
    icon = QtGui.QIcon()
    for size in [16, 24, 32, 48, 64, 96, 128, 256, 512]:
        icon.addPixmap(render_icon_pixmap(size))
    return icon


def build_brand_pixmap(size: int = 56) -> QtGui.QPixmap:
    return render_icon_pixmap(size)