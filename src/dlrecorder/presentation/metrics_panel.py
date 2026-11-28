"""实时录制指标面板与 120 秒趋势线（P1-04 一期）。

仅展示当前选中房间；数据由 RoomsPage 以 1 Hz 主动拉取，不加逐房间信号。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import LIGHT, ThemeTokens

_HEALTH_TEXT = {
    "good": "良好",
    "recovering": "恢复中",
    "stalled": "停滞",
    "degraded": "降速",
    "reconnecting": "重连中",
    "failed": "失败",
}


def _format_size(size: object) -> str:
    try:
        value = float(int(size or 0))
    except (TypeError, ValueError):
        return "--"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _format_speed(bps: object) -> str:
    try:
        value = float(bps or 0.0)
    except (TypeError, ValueError):
        return "--"
    if value <= 0:
        return "0 B/s"
    if value < 1024:
        return f"{value:.0f} B/s"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB/s"
    return f"{value / (1024 * 1024):.1f} MB/s"


def _format_duration(seconds: object) -> str:
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class SparklineWidget(QWidget):
    """自绘写入速度趋势（最多 120 点），不引入图表依赖。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(120, 32)
        self.setMaximumHeight(40)
        self._points: tuple[int, ...] = ()
        self._line = QColor(LIGHT.accent)
        self._warning = QColor(LIGHT.warning)

    def set_points(self, points: tuple[int, ...], *, stalled: bool = False) -> None:
        self._points = points
        self._stalled = stalled
        self.update()

    def set_colors(self, line: str, warning: str) -> None:
        self._line = QColor(line)
        self._warning = QColor(warning)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = max(1, self.width())
        height = max(1, self.height())
        painter.fillRect(0, 0, width, height, Qt.BrushStyle.NoBrush)
        points = self._points
        if len(points) < 2:
            pen = QPen(QColor(self._warning), 1)
            painter.setPen(pen)
            painter.drawLine(0, height - 2, width, height - 2)
            return
        peak = max(1, max(points))
        color = self._warning if getattr(self, "_stalled", False) else self._line
        pen = QPen(color, 1.4)
        painter.setPen(pen)
        step = width / max(1, len(points) - 1)
        previous: Optional[QPointF] = None
        for index, value in enumerate(points):
            ratio = max(0.0, min(1.0, value / peak))
            point = QPointF(index * step, height - 2 - ratio * (height - 4))
            if previous is not None:
                painter.drawLine(previous, point)
            previous = point


class MetricsPanel(QFrame):
    """房间页底部录制详情面板。"""

    collapsed_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._tokens = LIGHT
        self._collapsed = False
        self._health = "good"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.dot = QLabel("●")
        self.status_label = QLabel("未录制")
        self.status_label.setObjectName("Secondary")
        self.duration_label = QLabel("已录制 00:00:00")
        self.size_label = QLabel("文件 --")
        self.speed_label = QLabel("写入 --")
        self.health_label = QLabel("健康 --")
        self.health_label.setObjectName("Secondary")
        header.addWidget(self.dot)
        header.addWidget(self.status_label)
        header.addWidget(self.duration_label)
        header.addWidget(self.size_label)
        header.addWidget(self.speed_label)
        header.addWidget(self.health_label)
        header.addStretch(1)
        self.toggle_button = QPushButton("收起")
        self.toggle_button.setProperty("variant", "ghost")
        self.toggle_button.clicked.connect(self.toggle)
        header.addWidget(self.toggle_button)
        outer.addLayout(header)

        self.body = QWidget()
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(16)
        self.session_label = QLabel("本场 --")
        self.session_label.setObjectName("Muted")
        self.sparkline = SparklineWidget()
        body_layout.addWidget(self.session_label)
        body_layout.addStretch(1)
        body_layout.addWidget(self.sparkline)
        outer.addWidget(self.body)

        self.apply_theme(LIGHT)
        self.hide()

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self.body.setVisible(not self._collapsed)
        self.toggle_button.setText("展开" if self._collapsed else "收起")
        self.collapsed_changed.emit(self._collapsed)

    def apply_theme(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.sparkline.set_colors(tokens.accent, tokens.warning)
        self._apply_health_color()

    def _apply_health_color(self) -> None:
        colors = {
            "good": self._tokens.success,
            "recovering": self._tokens.text_muted,
            "stalled": self._tokens.danger,
            "degraded": self._tokens.warning,
            "reconnecting": self._tokens.warning,
            "failed": self._tokens.danger,
        }
        color = colors.get(self._health, self._tokens.text_muted)
        self.dot.setStyleSheet(f"color: {color};")
        self.health_label.setStyleSheet(f"color: {color};")

    def update_metrics(self, metrics: Optional[dict]) -> None:
        """传入录制服务快照；``None`` 表示该房间未录制。"""
        if not metrics:
            self._health = "good"
            self.status_label.setText("该直播间当前未录制")
            self.duration_label.setText("已录制 00:00:00")
            self.size_label.setText("文件 --")
            self.speed_label.setText("写入 --")
            self.health_label.setText("健康 --")
            self.session_label.setText("本场 --")
            self.sparkline.set_points(())
            self._apply_health_color()
            return

        status = metrics.get("status") or "recording"
        if status == "reconnecting":
            attempts = int(metrics.get("attempts") or 0)
            total = int(metrics.get("max_attempts") or 0)
            self.status_label.setText(
                f"重连中 {attempts}/{total}" if total else "重连中"
            )
            self.speed_label.setText("写入 —")
        else:
            self.status_label.setText("录制中")
            self.speed_label.setText(
                f"写入 {_format_speed(metrics.get('write_speed_bps'))}"
            )

        self.duration_label.setText(
            f"已录制 {_format_duration(metrics.get('segment_seconds'))}"
        )
        self.size_label.setText(f"文件 {_format_size(metrics.get('file_size'))}")
        self.session_label.setText(
            f"本场 {int(metrics.get('session_segments') or 0)} 个分段 · "
            f"{_format_size(metrics.get('session_bytes'))}"
        )

        self._health = str(metrics.get("health") or "good")
        label = _HEALTH_TEXT.get(self._health, self._health)
        if self._health == "stalled":
            label = f"停滞 {int(metrics.get('stall_seconds') or 0)} 秒无数据"
        self.health_label.setText(f"健康 {label}")
        self._apply_health_color()

        trend = tuple(int(value) for value in metrics.get("trend") or ())
        self.sparkline.set_points(trend, stalled=self._health in {"stalled", "degraded"})
