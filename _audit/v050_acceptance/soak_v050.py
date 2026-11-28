"""v0.5.0 短时长多房间稳定性与归档 soak（本地合成源，不访问真实平台）。

- 3 个房间并行录制，本地 FFmpeg lavfi 源持续写入。
- 周期性强杀 FFmpeg 验证自动续录。
- 实时采集指标、RSS、CPU 与线程数。
- 结束后归档全部完成分段并校验 SHA-256，检查数据库完整性。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import signal
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt

from dlrecorder.application.archive_service import ArchiveService
from dlrecorder.application.recording_service import RecordingService, StreamRefresh
from dlrecorder.domain.archive_rules import ArchiveSettings
from dlrecorder.infrastructure.db import Database

DURATION_S = 360
SAMPLES = 5.0
ROOMS = 3
FFMPEG = Path(r"C:\Users\kglpl\Desktop\DouyinLiveRecorder-Desktop\tools\ffmpeg\bin\ffmpeg.exe")
BASE = Path(os.environ.get("SOAK_BASE", Path.home() / "AppData/Local/Temp/dlr_v050_soak"))


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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


def rss_mb() -> float:
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    query = kernel32.K32GetProcessMemoryInfo
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    query.restype = wintypes.BOOL
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    if not query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return -1.0
    return counters.WorkingSetSize / (1024 * 1024)


def build_command(_ffmpeg: str, _url: str, path: Path) -> list[str]:
    return [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x180:rate=15",
        "-c:v",
        "mpeg2video",
        "-q:v",
        "8",
        "-f",
        "mpegts",
        str(path),
    ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    app = QCoreApplication.instance() or QCoreApplication([])
    data_dir = BASE / "data"
    if BASE.exists():
        import shutil

        shutil.rmtree(BASE, ignore_errors=True)
    data_dir.mkdir(parents=True)

    db = Database(data_dir / "app.db")
    archive_root = BASE / "archive"
    archive_root.mkdir()
    settings = ArchiveSettings(
        enabled=True,
        root_dir=archive_root,
        folder_template="{anchor}/{date}",
        name_template="{date}_{time}_{anchor}_{web_rid}",
        delete_source=False,
    )
    service = RecordingService(
        db,
        output_dir=data_dir / "recordings",
        command_builder=build_command,
        min_free_mb=0.0,
        reconnect_max_attempts=5,
        reconnect_delays_s=(1.0, 2.0, 3.0, 5.0, 5.0),
        stream_resolver=lambda _room_id: StreamRefresh(
            live=True, stream_url="local://lavfi"
        ),
        poll_interval_s=1.0,
        stop_grace_s=5.0,
        recover_interrupted=False,
        cleanup_orphans_on_start=False,
    )
    archive = ArchiveService(db, settings_provider=lambda: settings)

    def on_finished(room_id: int, snapshot: dict) -> None:
        recording_id = snapshot.get("recording_id")
        if recording_id is not None and snapshot.get("status") == "completed":
            archive.enqueue(int(recording_id))

    # soak 无 Qt 事件循环：直接连接，确保归档入队不被排队信号延迟。
    service.recording_finished.connect(on_finished, Qt.ConnectionType.DirectConnection)

    room_ids = [
        db.add_room(f"rid-soak-{index}", f"soak-{index}", f"anchor-{index}")
        for index in range(ROOMS)
    ]
    for room_id in room_ids:
        service.start_recording(room_id, "local://lavfi")

    deadline = time.monotonic() + DURATION_S
    kill_points = {90.0, 180.0, 270.0}
    started = time.monotonic()
    baseline_threads = threading.active_count()
    peak_rss = rss_mb()
    cpu0 = time.process_time()
    failures: list[str] = []
    kills = 0
    last_kill_at = 0.0
    next_sample = started + SAMPLES
    print(f"SOAK start rooms={ROOMS} duration={DURATION_S}s threads={baseline_threads}")

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            elapsed = now - started
            if kills < len(kill_points) and elapsed >= sorted(kill_points)[kills]:
                room_id = room_ids[kills % ROOMS]
                active = service._active.get(room_id)
                pid = active.process.pid if active and active.process else None
                if pid:
                    os.kill(pid, signal.SIGTERM)
                    kills += 1
                    last_kill_at = elapsed
                    print(f"SOAK kill ffmpeg room={room_id} pid={pid} t={elapsed:.0f}s")

            if now >= next_sample:
                next_sample = now + SAMPLES
                rss = rss_mb()
                peak_rss = max(peak_rss, rss)
                warmup = elapsed < 30.0
                recovering_window = (elapsed - last_kill_at) < 60.0
                for room_id in room_ids:
                    if not service.is_recording(room_id):
                        if not warmup and not recovering_window:
                            failures.append(
                                f"room {room_id} not recording at {elapsed:.0f}s"
                            )
                        continue
                    snapshot = service.metrics_snapshot(room_id)
                    if snapshot is None:
                        if not warmup:
                            failures.append(
                                f"room {room_id} metrics missing at {elapsed:.0f}s"
                            )
                        continue
                    if snapshot["health"] == "stalled":
                        failures.append(f"room {room_id} stalled at {elapsed:.0f}s")
                    if (
                        snapshot["file_size"] <= 0
                        and not warmup
                        and not recovering_window
                    ):
                        failures.append(f"room {room_id} no bytes at {elapsed:.0f}s")
                print(
                    f"SOAK t={elapsed:.0f}s rss={rss:.1f}MB threads={threading.active_count()}"
                )
            time.sleep(1.0)
    finally:
        print("SOAK stopping recordings")
        for room_id in room_ids:
            service.stop_recording(room_id)
        stop_deadline = time.monotonic() + 30
        while time.monotonic() < stop_deadline and any(
            service.is_recording(room_id) for room_id in room_ids
        ):
            time.sleep(0.5)
        archived_ok = archive.wait_until_idle(60)
        shutdown_report = service.shutdown()
        exited = shutdown_report.thread_exited and shutdown_report.remaining_active == 0
        archive_exited = archive.shutdown()

    cpu_s = time.process_time() - cpu0
    rows = db.list_recordings_by_archive_status(
        ["none", "pending", "copying", "verifying", "archived", "failed", "skipped"]
    )
    archived = [row for row in rows if row["archive_status"] == "archived"]
    completed = [row for row in rows if row["status"] == "completed"]
    hash_ok = 0
    for row in archived:
        source = Path(row["file_path"]) if row["file_path"] else None
        target = Path(row["archive_path"]) if row["archive_path"] else None
        if source and target and source.exists() and target.exists():
            if sha256(source) == sha256(target):
                hash_ok += 1
    integrity = db._get_conn().execute("PRAGMA integrity_check").fetchone()[0]
    threads_after = threading.active_count()
    db.close()
    app.processEvents()

    print(
        f"SOAK summary segments={len(rows)} completed={len(completed)} "
        f"archived={len(archived)} hash_ok={hash_ok} kills={kills}"
    )
    print(
        f"SOAK resources peak_rss={peak_rss:.1f}MB cpu={cpu_s:.1f}s "
        f"threads={baseline_threads}->{threads_after}"
    )
    print(
        f"SOAK lifecycle archive_idle={archived_ok} recording_thread_exited={exited} "
        f"archive_thread_exited={archive_exited} integrity={integrity}"
    )

    if not completed:
        failures.append("no completed segments")
    if not archived or hash_ok != len(archived):
        failures.append("archive verification failed")
    if not exited or not archive_exited:
        failures.append("thread shutdown failed")
    if integrity != "ok":
        failures.append(f"integrity={integrity}")
    if threads_after > baseline_threads + 2:
        failures.append(f"thread leak {baseline_threads}->{threads_after}")
    if failures:
        print("SOAK RESULT=FAIL")
        for item in failures[:20]:
            print("  -", item)
        return 1
    print("SOAK RESULT=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
