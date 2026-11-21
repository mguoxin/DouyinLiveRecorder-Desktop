"""系统托盘：最小化驻留、恢复窗口与安全退出入口。"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import icons

logger = logging.getLogger(__name__)


class TrayController(QObject):
    """封装 QSystemTrayIcon；不可用时整体降级，不影响主窗口。"""

    show_requested = Signal()
    quit_requested = Signal()
    auto_record_changed = Signal(bool)
    open_dir_requested = Signal()
    start_all_requested = Signal()
    stop_all_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._icon: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._action_auto = None
        if not self.available:
            logger.warning("系统托盘不可用，最小化将保持普通窗口行为")
            return

        self._icon = QSystemTrayIcon(icons.app_icon(), self)
        self._icon.setToolTip("抖音直播录制工具")
        menu = QMenu(parent)
        self._menu = menu
        menu.addAction("显示主窗口", self.show_requested.emit)
        menu.addSeparator()
        self._action_auto = menu.addAction("自动录制")
        self._action_auto.setCheckable(True)
        self._action_auto.toggled.connect(self.auto_record_changed.emit)
        menu.addAction("开始全部监控", self.start_all_requested.emit)
        menu.addAction("停止全部监控", self.stop_all_requested.emit)
        menu.addSeparator()
        menu.addAction("打开录制目录", self.open_dir_requested.emit)
        menu.addSeparator()
        menu.addAction("退出", self.quit_requested.emit)
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._on_activated)
        self._icon.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_requested.emit()

    def set_auto_record(self, checked: bool) -> None:
        if self._action_auto is None:
            return
        self._action_auto.blockSignals(True)
        self._action_auto.setChecked(checked)
        self._action_auto.blockSignals(False)

    def set_recording_count(self, count: int) -> None:
        if self._icon is None:
            return
        if count > 0:
            self._icon.setToolTip(
                f"抖音直播录制工具 · {count} 个直播间正在录制"
            )
        else:
            self._icon.setToolTip("抖音直播录制工具 · 后台运行中")

    def show_message(self, title: str, message: str) -> None:
        if self._icon is None or not QSystemTrayIcon.supportsMessages():
            return
        self._icon.showMessage(
            title,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    def dispose(self) -> None:
        if self._icon is not None:
            self._icon.hide()
            self._icon.setContextMenu(None)
            self._icon.deleteLater()
            self._icon = None
        if self._menu is not None:
            self._menu.deleteLater()
            self._menu = None
