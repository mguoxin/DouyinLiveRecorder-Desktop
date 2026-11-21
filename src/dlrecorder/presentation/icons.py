"""用 QPainter 绘制界面图标，避免引入外部图标资源与额外依赖。"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

_CACHE: dict[tuple[str, str, int], QPixmap] = {}


def _render(name: str, color: str, size: int, scale: int = 2) -> QPixmap:
    key = (name, color, size * 100 + scale)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(float(scale))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 20.0, size / 20.0)
    _draw(painter, name, QColor(color))
    painter.end()
    _CACHE[key] = pixmap
    return pixmap


def _pen(color: QColor, width: float = 1.6) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw(painter: QPainter, name: str, color: QColor) -> None:
    painter.setPen(_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "rooms":
        painter.drawRoundedRect(QRectF(2.5, 4.0, 15.0, 12.0), 2.4, 2.4)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygonF(
                [QPointF(8.4, 7.4), QPointF(13.2, 10.0), QPointF(8.4, 12.6)]
            )
        )
    elif name == "history":
        painter.drawEllipse(QRectF(3.0, 3.0, 14.0, 14.0))
        painter.drawLine(QPointF(10.0, 6.0), QPointF(10.0, 10.2))
        painter.drawLine(QPointF(10.0, 10.2), QPointF(13.0, 12.0))
    elif name == "logs":
        painter.drawRoundedRect(QRectF(2.5, 3.5, 15.0, 13.0), 2.2, 2.2)
        painter.drawPolyline(
            QPolygonF([QPointF(6.0, 7.5), QPointF(8.6, 10.0), QPointF(6.0, 12.5)])
        )
        painter.drawLine(QPointF(10.8, 12.6), QPointF(14.0, 12.6))
    elif name == "settings":
        for y, knob_x in ((6.0, 12.8), (10.0, 7.4), (14.0, 12.2)):
            painter.drawLine(QPointF(3.2, y), QPointF(16.8, y))
            painter.setBrush(color)
            painter.drawEllipse(QPointF(knob_x, y), 1.7, 1.7)
            painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "add":
        painter.drawLine(QPointF(10.0, 4.2), QPointF(10.0, 15.8))
        painter.drawLine(QPointF(4.2, 10.0), QPointF(15.8, 10.0))
    elif name == "edit":
        painter.drawLine(QPointF(4.5, 15.5), QPointF(14.6, 5.4))
        painter.drawLine(QPointF(12.9, 3.7), QPointF(16.3, 7.1))
        painter.drawLine(QPointF(4.5, 15.5), QPointF(4.2, 12.9))
        painter.drawLine(QPointF(4.5, 15.5), QPointF(7.1, 15.2))
    elif name == "delete":
        painter.drawLine(QPointF(3.8, 6.0), QPointF(16.2, 6.0))
        painter.drawLine(QPointF(8.0, 3.8), QPointF(12.0, 3.8))
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(5.4, 6.0),
                    QPointF(6.3, 16.4),
                    QPointF(13.7, 16.4),
                    QPointF(14.6, 6.0),
                ]
            )
        )
        painter.drawLine(QPointF(8.5, 9.0), QPointF(8.9, 13.6))
        painter.drawLine(QPointF(11.5, 9.0), QPointF(11.1, 13.6))
    elif name == "play":
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygonF(
                [QPointF(6.4, 4.2), QPointF(16.2, 10.0), QPointF(6.4, 15.8)]
            )
        )
    elif name == "stop":
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(5.2, 5.2, 9.6, 9.6), 1.6, 1.6)
    elif name == "record":
        painter.drawEllipse(QRectF(3.2, 3.2, 13.6, 13.6))
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(7.1, 7.1, 5.8, 5.8))
    elif name == "folder":
        path = QPainterPath()
        path.moveTo(3.0, 6.2)
        path.lineTo(7.8, 6.2)
        path.lineTo(9.3, 8.2)
        path.lineTo(17.0, 8.2)
        path.lineTo(17.0, 15.2)
        path.lineTo(3.0, 15.2)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "refresh":
        painter.drawArc(QRectF(4.0, 4.0, 12.0, 12.0), 50 * 16, 285 * 16)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygonF(
                [QPointF(13.2, 2.6), QPointF(16.7, 5.9), QPointF(12.6, 7.0)]
            )
        )
    elif name == "external":
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(9.0, 5.0),
                    QPointF(4.2, 5.0),
                    QPointF(4.2, 15.8),
                    QPointF(15.0, 15.8),
                    QPointF(15.0, 11.0),
                ]
            )
        )
        painter.drawLine(QPointF(11.0, 4.5), QPointF(16.0, 4.5))
        painter.drawLine(QPointF(16.0, 4.5), QPointF(16.0, 9.5))
        painter.drawLine(QPointF(10.2, 10.3), QPointF(16.0, 4.5))
    elif name == "shield":
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(10.0, 3.0),
                    QPointF(16.2, 5.4),
                    QPointF(16.2, 10.0),
                    QPointF(10.0, 17.0),
                    QPointF(3.8, 10.0),
                    QPointF(3.8, 5.4),
                    QPointF(10.0, 3.0),
                ]
            )
        )
    elif name == "empty-room":
        painter.setPen(_pen(color, 1.3))
        painter.drawRoundedRect(QRectF(1.8, 4.4, 16.4, 11.6), 2.4, 2.4)
        painter.drawEllipse(QRectF(7.6, 7.6, 4.8, 4.8))
    else:
        painter.drawEllipse(QRectF(5.0, 5.0, 10.0, 10.0))


def icon(name: str, color: str, size: int = 18) -> QIcon:
    """单色图标（悬停/选中状态由调用方分别生成）。"""
    result = QIcon()
    result.addPixmap(_render(name, color, size, 1))
    result.addPixmap(_render(name, color, size, 2))
    return result


def stateful_icon(
    name: str,
    normal_color: str,
    active_color: str,
    disabled_color: str,
    size: int = 18,
) -> QIcon:
    """为可选中按钮生成 Off/On/Disabled 三种状态图标。"""
    result = QIcon()
    for scale in (1, 2):
        result.addPixmap(
            _render(name, normal_color, size, scale),
            QIcon.Mode.Normal,
            QIcon.State.Off,
        )
        result.addPixmap(
            _render(name, active_color, size, scale),
            QIcon.Mode.Normal,
            QIcon.State.On,
        )
        result.addPixmap(
            _render(name, active_color, size, scale),
            QIcon.Mode.Active,
            QIcon.State.On,
        )
        result.addPixmap(
            _render(name, disabled_color, size, scale),
            QIcon.Mode.Disabled,
            QIcon.State.Off,
        )
    return result


def _logo_pixmap(size: int, scale: int = 2) -> QPixmap:
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(float(scale))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 20.0, size / 20.0)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#E11D48"))
    painter.drawRoundedRect(QRectF(0.6, 0.6, 18.8, 18.8), 5.2, 5.2)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawPolygon(
        QPolygonF([QPointF(7.2, 5.4), QPointF(15.0, 10.0), QPointF(7.2, 14.6)])
    )
    painter.setBrush(QColor("#FFFFFF"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(15.8, 15.8), 2.2, 2.2)
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    result = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        result.addPixmap(_logo_pixmap(size, 1))
        result.addPixmap(_logo_pixmap(size, 2))
    return result


def pixmap(name: str, color: str, size: int = 40) -> QPixmap:
    return _render(name, color, size, 2)
