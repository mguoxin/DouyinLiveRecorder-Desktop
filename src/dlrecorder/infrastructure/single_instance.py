"""Windows 命名互斥量单实例控制。

一个 Windows 用户会话只允许运行一个应用实例。
互斥量由内核管理：进程异常退出时由操作系统自动释放，无需额外清理文件。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183

# Local\ 前缀将作用域限定在当前登录会话，符合“一个用户会话一个实例”的约束。
DEFAULT_MUTEX_NAME = r"Local\DouyinLiveRecorder-Desktop.SingleInstance"


class SingleInstanceError(RuntimeError):
    """单实例检测本身失败；调用方不得据此放行启动。"""


def _load_kernel32():
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - 非 Windows 或系统异常
        raise SingleInstanceError(f"无法加载 kernel32: {exc}") from exc


def _bind_create_mutex(kernel32):
    """声明 CreateMutexW 的参数与返回类型（跨 32/64 位一致）。"""
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    return create_mutex


def _bind_close_handle(kernel32):
    """声明 CloseHandle 的参数与返回类型。

    HANDLE 为指针宽度类型，显式声明可避免 64 位句柄被
    默认 C int 转换截断。
    """
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return close_handle


class SingleInstanceGuard:
    """基于命名互斥量的单实例守卫。"""

    def __init__(self, name: str = DEFAULT_MUTEX_NAME) -> None:
        if not name:
            raise SingleInstanceError("互斥量名称不能为空")
        self._name = name
        self._handle: Optional[int] = None

    @property
    def name(self) -> str:
        return self._name

    def acquire(self) -> bool:
        """尝试取得实例锁。返回 False 表示已有实例；系统错误抛出异常。"""
        if self._handle is not None:
            return True
        create_mutex = _bind_create_mutex(_load_kernel32())

        ctypes.set_last_error(0)
        handle = create_mutex(None, False, self._name)
        if not handle:
            error = ctypes.get_last_error()
            raise SingleInstanceError(f"创建实例互斥量失败，WinError={error}")

        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # 已有实例持有同名互斥量：关闭本次取得的句柄后拒绝启动。
            self._close(handle)
            logger.info("检测到已有实例持有互斥量: %s", self._name)
            return False

        self._handle = handle
        logger.info("已取得实例互斥量: %s", self._name)
        return True

    def _close(self, handle: int) -> None:
        """关闭互斥量句柄；失败时抛出异常，不静默掩盖系统错误。"""
        close_handle = _bind_close_handle(_load_kernel32())
        ctypes.set_last_error(0)
        if not close_handle(handle):
            error = ctypes.get_last_error()
            raise SingleInstanceError(f"关闭实例互斥量句柄失败，WinError={error}")

    def release(self) -> None:
        """正常退出时释放实例锁；重复调用安全。

        句柄只在 CloseHandle 成功后清除：关闭失败时保留句柄状态并抛出
        SingleInstanceError，避免在锁尚未真正释放时假装成功。
        """
        handle = self._handle
        if handle is None:
            return
        self._close(handle)
        self._handle = None
        logger.info("已释放实例互斥量: %s", self._name)

    def __enter__(self) -> "SingleInstanceGuard":
        if not self.acquire():
            raise SingleInstanceError("实例互斥量已被占用")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
