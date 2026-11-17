"""程序入口。

单实例约束：取得命名互斥量之前不得创建 Application、打开业务数据库或恢复遗留 Session。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Callable, Optional

from .application.app import Application
from .infrastructure.single_instance import (
    DEFAULT_MUTEX_NAME,
    SingleInstanceError,
    SingleInstanceGuard,
)

logger = logging.getLogger(__name__)

EXIT_ALREADY_RUNNING = 2
EXIT_INSTANCE_LOCK_ERROR = 3
EXIT_STARTUP_FAILED = 4

ALREADY_RUNNING_MESSAGE = "抖音直播录制工具已在运行，请勿重复启动。"


def _show_message(message: str) -> None:
    """使用原生消息框提示；第二实例不初始化 Qt。"""
    try:
        ctypes.windll.user32.MessageBoxW(
            None, message, "抖音直播录制工具", 0x00000040
        )
    except Exception:  # noqa: BLE001 - 提示失败不应改变退出语义
        logger.exception("显示启动提示失败")


def main(
    notify: Optional[Callable[[str], None]] = None,
    mutex_name: Optional[str] = None,
) -> int:
    show = notify or _show_message

    try:
        guard = SingleInstanceGuard(mutex_name or DEFAULT_MUTEX_NAME)
        acquired = guard.acquire()
    except SingleInstanceError as exc:
        # 检测失败不得视为允许启动。
        logger.error("单实例检测失败: %s", exc)
        show(f"单实例检测失败，程序无法启动：{exc}")
        return EXIT_INSTANCE_LOCK_ERROR

    if not acquired:
        show(ALREADY_RUNNING_MESSAGE)
        return EXIT_ALREADY_RUNNING

    # 不显式释放实例互斥量：句柄保留至进程终止，由 Windows 内核回收。
    # 这样即使监控线程/执行器线程仍在退出过程中，第二实例也无法取得资格。
    try:
        app = Application()
    except Exception as exc:  # noqa: BLE001 - 启动失败必须给出可读提示而不是静默退出
        logger.exception("应用启动失败")
        show(
            "程序启动失败，无法打开数据目录或数据库：\n"
            f"{exc}\n\n"
            "请确认程序所在目录可写，且 data 目录与 app.db 未被其他程序占用或损坏。"
        )
        return EXIT_STARTUP_FAILED
    try:
        return app.run()
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
