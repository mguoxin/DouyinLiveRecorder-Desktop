"""进程资源采样（Windows 优先，失败时降级）。

刻意不引入 psutil：应用只展示当前进程的常驻内存与 CPU 占用，使用系统 API
即可，避免增加安装体积与依赖面。
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_to_seconds(value: _FileTime) -> float:
    ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
    return ticks / 10_000_000.0


class ProcessResourceSampler:
    """采样当前进程的 RSS、CPU 百分比与句柄数。"""

    def __init__(self) -> None:
        self._available = os.name == "nt"
        self._last_wall: Optional[float] = None
        self._last_cpu_seconds: Optional[float] = None
        self._k32 = None
        if self._available:
            try:
                self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                self._k32.K32GetProcessMemoryInfo.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(_ProcessMemoryCounters),
                    wintypes.DWORD,
                ]
                self._k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
                self._k32.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(_FileTime),
                    ctypes.POINTER(_FileTime),
                    ctypes.POINTER(_FileTime),
                    ctypes.POINTER(_FileTime),
                ]
                self._k32.GetProcessTimes.restype = wintypes.BOOL
            except Exception:  # noqa: BLE001 - 非 Windows 或受限环境降级
                logger.debug("进程资源采样不可用", exc_info=True)
                self._available = False
                self._k32 = None

    @property
    def available(self) -> bool:
        return self._available

    def sample(self) -> dict:
        if not self._available or self._k32 is None:
            return {"available": False}
        handle = wintypes.HANDLE(self._k32.GetCurrentProcess())
        memory = _ProcessMemoryCounters()
        memory.cb = ctypes.sizeof(_ProcessMemoryCounters)
        rss_mb: Optional[float] = None
        if self._k32.K32GetProcessMemoryInfo(
            handle, ctypes.byref(memory), memory.cb
        ):
            rss_mb = round(memory.WorkingSetSize / (1024 * 1024), 1)

        cpu_seconds: Optional[float] = None
        created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        if self._k32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            cpu_seconds = _filetime_to_seconds(kernel) + _filetime_to_seconds(user)

        wall = time.monotonic()
        cpu_percent: Optional[float] = None
        if cpu_seconds is not None and self._last_cpu_seconds is not None:
            elapsed = wall - (self._last_wall or wall)
            if elapsed > 0.2:
                cpu_percent = round(
                    max(0.0, cpu_seconds - self._last_cpu_seconds) / elapsed * 100.0, 1
                )
        if cpu_seconds is not None:
            self._last_cpu_seconds = cpu_seconds
            self._last_wall = wall

        return {
            "available": True,
            "rss_mb": rss_mb,
            "cpu_percent": cpu_percent,
            "cpu_seconds": cpu_seconds,
        }
