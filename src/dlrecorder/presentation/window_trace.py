"""低开销窗口生命周期诊断（默认关闭）。

启用方式：设置环境变量 ``DLR_WINDOW_TRACE=1`` 后启动。
启用后只在窗口显示/隐藏/恢复/关闭/主题切换等关键点写入一行 INFO 日志，
不弹窗、不阻塞主线程；诊断导出会把这些行一并收集，用于定位“窗口空白”。
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("DLR_WINDOW_TRACE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_TRACE_MARKER = "window-trace"


def enabled() -> bool:
    return _ENABLED


def trace(window, event: str, **fields: object) -> None:
    """记录一次窗口状态快照；任何异常都被吞掉，绝不影响 UI。"""
    if not _ENABLED:
        return
    try:
        state = window.windowState()
        flags = []
        if state & Qt.WindowState.WindowMinimized:
            flags.append("minimized")
        if state & Qt.WindowState.WindowMaximized:
            flags.append("maximized")
        if state & Qt.WindowState.WindowFullScreen:
            flags.append("fullscreen")
        geometry = window.geometry()
        screen = window.screen()
        dpi = screen.logicalDotsPerInch() if screen is not None else 0
        screen_name = screen.name() if screen is not None else ""
        extra = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info(
            "%s event=%s visible=%s state=%s geom=%dx%d%+d%+d "
            "dpi=%.0f screen=%s %s",
            _TRACE_MARKER,
            event,
            window.isVisible(),
            ",".join(flags) or "normal",
            geometry.width(),
            geometry.height(),
            geometry.x(),
            geometry.y(),
            dpi,
            screen_name,
            extra,
        )
    except Exception:  # noqa: BLE001 - 诊断埋点不得影响窗口行为
        logger.debug("窗口追踪记录失败", exc_info=True)
