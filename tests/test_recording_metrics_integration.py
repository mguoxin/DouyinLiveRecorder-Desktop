"""P1-04 服务接线集成测试：真实子进程写入文件，指标必须与实际增长一致。"""
import sys
import time
from pathlib import Path

from dlrecorder.application.recording_service import RecordingService
from dlrecorder.infrastructure.db import Database

WRITER_SCRIPT = (
    "import sys, time\n"
    "handle = open(sys.argv[1], 'wb')\n"
    "for _ in range(120):\n"
    "    handle.write(b'x' * 4096)\n"
    "    handle.flush()\n"
    "    time.sleep(0.05)\n"
    "handle.close()\n"
)


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _build_service(tmp_path: Path) -> tuple[RecordingService, Database]:
    db = Database(tmp_path / "data" / "app.db")
    service = RecordingService(
        db,
        output_dir=tmp_path / "recordings",
        command_builder=lambda _ffmpeg, _url, path: [
            sys.executable,
            "-c",
            WRITER_SCRIPT,
            str(path),
        ],
        poll_interval_s=0.1,
        stop_grace_s=1.0,
        recover_interrupted=False,
        cleanup_orphans_on_start=False,
        reconnect_max_attempts=0,
    )
    return service, db


def test_metrics_track_real_file_growth_and_stop(tmp_path: Path, qt_app):
    service, db = _build_service(tmp_path)
    try:
        room_id = db.add_room("rid-metrics-1", "主播一", "anchor-1")
        service.start_recording(room_id, "local://stream")
        assert _wait_until(lambda: service.is_recording(room_id))

        assert _wait_until(
            lambda: (snapshot := service.metrics_snapshot(room_id)) is not None
            and snapshot["file_size"] > 0,
            timeout=5.0,
        )
        time.sleep(0.5)
        snapshot = service.metrics_snapshot(room_id)
        assert snapshot is not None
        assert snapshot["status"] == "recording"
        assert snapshot["file_size"] > 0
        assert snapshot["write_speed_bps"] > 0
        assert snapshot["segment_seconds"] > 0
        assert snapshot["session_segments"] == 0
        assert snapshot["health"] in {"recovering", "good"}
        assert 0 < len(snapshot["trend"]) <= 120
        assert snapshot["file_path"].endswith(".ts")

        service.stop_recording(room_id)
        assert _wait_until(lambda: not service.is_recording(room_id), timeout=5.0)
        assert service.metrics_snapshot(room_id) is None
    finally:
        service.shutdown()
        db.close()


def test_metrics_are_independent_per_room(tmp_path: Path, qt_app):
    service, db = _build_service(tmp_path)
    try:
        first = db.add_room("rid-a", "主播A", "anchor-a")
        second = db.add_room("rid-b", "主播B", "anchor-b")
        service.start_recording(first, "local://a")
        service.start_recording(second, "local://b")
        assert _wait_until(lambda: service.is_recording(first) and service.is_recording(second))
        assert _wait_until(
            lambda: all(
                (snapshot := service.metrics_snapshot(room_id)) is not None
                and snapshot["file_size"] > 0
                for room_id in (first, second)
            ),
            timeout=5.0,
        )

        time.sleep(0.6)  # 至少积累多个采样点，速度窗口才有分母
        snapshot_a = service.metrics_snapshot(first)
        snapshot_b = service.metrics_snapshot(second)
        assert snapshot_a["room_id"] == first
        assert snapshot_b["room_id"] == second
        assert snapshot_a["file_path"] != snapshot_b["file_path"]
        assert snapshot_a["write_speed_bps"] > 0
        assert snapshot_b["write_speed_bps"] > 0
    finally:
        service.shutdown()
        db.close()
