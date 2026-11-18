"""Windows 进程身份查询与 Job Object 辅助。

用途：
- 在启动 FFmpeg 后记录 ``(pid, 创建时间, 可执行文件路径)`` 三元组，用于后续
  判断某个进程是否仍是当初启动的那一个（PID 会被系统复用，单凭 PID 不可信）。
- 用 Job Object 的 ``KILL_ON_JOB_CLOSE`` 让本应用的 FFmpeg 子进程在应用被强制
  结束时由系统一并终止，避免产生孤儿进程。

非 Windows 平台全部降级为"不可用"：查询返回 ``None``、分配 Job 返回 ``False``，
调用方按保守策略处理，不影响程序运行。
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - 仅在 Windows 上导入
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _SYNCHRONIZE = 0x00100000
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _SYNCHRONIZE_WAIT_MS = 0

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL


@dataclass(frozen=True)
class ProcessIdentity:
    """一个进程在某一时刻的身份信息。``create_time`` 是 Windows FILETIME（100ns）。"""

    pid: int
    create_time: int
    exe_path: str


def query_process(pid: int) -> Optional[ProcessIdentity]:
    """查询进程身份；进程不存在或无权访问时返回 None。"""
    if not _IS_WINDOWS or pid <= 0:
        return None
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = _FILETIME()
        exited = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not _kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        create_time = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return ProcessIdentity(pid=pid, create_time=create_time, exe_path=buffer.value)
    finally:
        _kernel32.CloseHandle(handle)


def is_process_alive(pid: int) -> bool:
    """进程是否仍在运行（不等待）。"""
    if not _IS_WINDOWS or pid <= 0:
        return False
    handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return _kernel32.WaitForSingleObject(handle, _SYNCHRONIZE_WAIT_MS) == _WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(handle)


def terminate_process(pid: int, exit_code: int = 1) -> bool:
    """强制结束指定 PID；仅应在身份校验通过后调用。"""
    if not _IS_WINDOWS or pid <= 0:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, exit_code))
    finally:
        _kernel32.CloseHandle(handle)


class KillOnCloseJob:
    """Windows Job Object：句柄关闭时终止其中所有进程。

    应用被强制结束时，操作系统会关闭本进程持有的句柄，从而连带终止 FFmpeg，
    这是防止孤儿进程的主要机制；进程登记表（ProcessRegistry）作为第二道防线，
    用于处理 Job 分配失败或历史遗留进程。
    """

    def __init__(self) -> None:
        self._handle: Optional[int] = None
        self._closed = False
        if not _IS_WINDOWS:
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("创建 Job Object 失败，孤儿进程将由登记表清理兜底")
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.warning("配置 Job Object 失败，孤儿进程将由登记表清理兜底")
            _kernel32.CloseHandle(handle)
            return
        self._handle = handle

    @property
    def active(self) -> bool:
        return self._handle is not None and not self._closed

    def assign(self, pid: int) -> bool:
        """把进程加入 Job；失败只记录日志，不阻断录制。"""
        if not self.active or pid <= 0:
            return False
        process_handle = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not process_handle:
            logger.warning("无法打开 FFmpeg 进程以加入 Job pid=%s", pid)
            return False
        try:
            ok = bool(
                _kernel32.AssignProcessToJobObject(self._handle, process_handle)
            )
        finally:
            _kernel32.CloseHandle(process_handle)
        if not ok:
            logger.warning("FFmpeg 加入 Job 失败 pid=%s（将由登记表清理兜底）", pid)
        return ok

    def close(self) -> None:
        if self._handle is not None and not self._closed:
            _kernel32.CloseHandle(self._handle)
        self._handle = None
        self._closed = True


__all__ = [
    "KillOnCloseJob",
    "ProcessIdentity",
    "is_process_alive",
    "query_process",
    "terminate_process",
]
