"""本机性能基准与长时稳定性测试（开发工具，不访问网络）。

单次基准：测量 Application 初始化、20 个 Mock 房间并发监控的运行内存与关闭耗时。
长时模式：``--soak-hours`` 持续运行并每分钟记录内存、房间状态与告警计数。
结果仅代表运行所在机器，不等同于低配（i3 / 4GB）验收。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DLR_PARSER"] = "mock"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from dlrecorder.application.app import Application  # noqa: E402


class _MemoryCounters(ctypes.Structure):
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


def working_set_mb() -> float:
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(_MemoryCounters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_MemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return round(counters.WorkingSetSize / 1048576, 2)


def machine_info() -> dict:
    """记录运行机器信息，便于区分本机基准与低配验收结果。"""

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "platform": platform.platform(),
        "processor": os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        "cpu_count": os.cpu_count(),
        "total_ram_gb": round(status.ullTotalPhys / 1073741824, 2),
    }


class _WarningCounter(logging.Handler):
    """统计运行期间告警次数，用于发现队列满等异常。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self.count += 1


def _active_counts(app: Application, room_ids: list[int]) -> tuple[int, int]:
    live = sum(
        1
        for room in app.service.get_rooms()
        if room.state.last_confirmed_room_state == "LIVE"
    )
    monitoring = sum(1 for room_id in room_ids if app.service.is_monitoring(room_id))
    return live, monitoring


def run_soak(
    hours: float,
    rooms: int,
    interval: float,
    log_path: Path,
    sample_s: float = 60.0,
) -> None:
    """持续运行指定时长，每分钟写入一行 JSON 采样。"""
    base = Path(tempfile.mkdtemp(prefix="dlr-soak-"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    app = Application(base_dir=base)
    qt_app = QApplication.instance()
    qt_app.processEvents()
    room_ids = [
        app.service.add_room(f"soak-{i}", f"稳定性房间{i}") for i in range(rooms)
    ]
    app.service.set_monitor_interval(interval)
    for room_id in room_ids:
        app.service.start_monitor(room_id)
        qt_app.processEvents()
        time.sleep(0.1)

    counter = _WarningCounter()
    logging.getLogger("dlrecorder").addHandler(counter)
    started = time.monotonic()
    deadline = started + hours * 3600
    next_sample = started
    print(
        f"soak started: hours={hours} rooms={rooms} interval={interval}s "
        f"log={log_path}",
        flush=True,
    )
    try:
        with log_path.open("a", encoding="utf-8") as fp:
            while time.monotonic() < deadline:
                qt_app.processEvents()
                now = time.monotonic()
                if now >= next_sample:
                    live, monitoring = _active_counts(app, room_ids)
                    sample = {
                        "elapsed_h": round((now - started) / 3600, 4),
                        "working_set_mb": working_set_mb(),
                        "live_rooms": live,
                        "monitoring_rooms": monitoring,
                        "warnings": counter.count,
                        "threads": __import__("threading").active_count(),
                    }
                    fp.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    fp.flush()
                    next_sample = now + sample_s
                time.sleep(0.05)
    except KeyboardInterrupt:
        print("soak interrupted by user", flush=True)
    finally:
        logging.getLogger("dlrecorder").removeHandler(counter)
        app.shutdown()
        app.db.close()
        print(
            json.dumps(
                {
                    "soak_finished": True,
                    "samples_log": str(log_path),
                    "warnings_total": counter.count,
                    "machine": machine_info(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        shutil.rmtree(base, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="性能基准与长时稳定性测试")
    parser.add_argument("--soak-hours", type=float, default=0.0, help="长时运行小时数")
    parser.add_argument("--rooms", type=int, default=20, help="房间数量")
    parser.add_argument("--interval", type=float, default=1.0, help="监控间隔（秒）")
    parser.add_argument("--soak-log", type=Path, default=None, help="采样日志路径")
    parser.add_argument("--soak-sample", type=float, default=60.0, help="采样间隔（秒）")
    args = parser.parse_args()

    if args.soak_hours > 0:
        log_path = args.soak_log or (
            Path(tempfile.gettempdir())
            / "dlr-soak"
            / f"soak-{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
        run_soak(
            args.soak_hours,
            args.rooms,
            args.interval,
            log_path,
            sample_s=args.soak_sample,
        )
        return

    base = Path(tempfile.mkdtemp(prefix="dlr-benchmark-"))
    started = time.perf_counter()
    app = Application(base_dir=base)
    init_s = time.perf_counter() - started
    qt_app = QApplication.instance()
    qt_app.processEvents()
    idle_mb = working_set_mb()

    room_ids = [app.service.add_room(f"benchmark-{i}", f"基准房间{i}") for i in range(20)]
    app.service.set_monitor_interval(1.0)
    start_monitor_at = time.perf_counter()
    for room_id in room_ids:
        app.service.start_monitor(room_id)
        # 模拟用户逐个启动，避免瞬时超过监控请求在途上限。
        qt_app.processEvents()
        time.sleep(0.1)
    monitor_start_s = time.perf_counter() - start_monitor_at

    peak_mb = idle_mb
    deadline = time.perf_counter() + 10.0
    while time.perf_counter() < deadline:
        qt_app.processEvents()
        peak_mb = max(peak_mb, working_set_mb())
        time.sleep(0.05)

    live_rooms, monitoring_rooms = _active_counts(app, room_ids)
    shutdown_at = time.perf_counter()
    app.shutdown()
    shutdown_s = time.perf_counter() - shutdown_at
    app.db.close()

    print(
        json.dumps(
            {
                "machine": machine_info(),
                "rooms": len(room_ids),
                "init_s": round(init_s, 3),
                "idle_working_set_mb": idle_mb,
                "peak_working_set_mb": peak_mb,
                "monitor_start_s": round(monitor_start_s, 3),
                "live_rooms_after_10s": live_rooms,
                "monitoring_rooms": monitoring_rooms,
                "shutdown_s": round(shutdown_s, 3),
                "note": "Mock 解析器，无网络；请以 machine 字段确认是否为目标低配环境",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
