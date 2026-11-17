"""录制服务测试（本地桩进程，不读取真实直播流）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from dlrecorder.application.recording_service import RecordingService
from dlrecorder.infrastructure.db import Database

STUB = r"""
import sys, time
path, mode = sys.argv[1], sys.argv[2]
with open(path, "wb") as f:
    if mode != "empty":
        f.write(b"x" * 8192)
        f.flush()
if mode == "exit3":
    sys.exit(3)
if mode in ("sleep", "stall"):
    time.sleep(30)
if mode == "stdin":
    sys.stdin.readline()
"""


def _wait_for(qt_app, predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


class _Env:
    def __init__(self, db: Database, room_id: int, tmp_path: Path) -> None:
        self.db = db
        self.room_id = room_id
        self.tmp_path = tmp_path
        self.stub = tmp_path / "stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.services: list[RecordingService] = []

    def service(self, mode: str = "stdin", **kwargs) -> RecordingService:
        def builder(ffmpeg_path: str, stream_url: str, output_path: Path) -> list[str]:
            return [sys.executable, str(self.stub), str(output_path), mode]

        options = {
            "output_dir": self.tmp_path / "recordings",
            "command_builder": builder,
            "poll_interval_s": 0.03,
            "stall_timeout_s": 0,
            "stop_grace_s": 2.0,
            "shutdown_timeout_s": 10.0,
        }
        options.update(kwargs)
        service = RecordingService(self.db, **options)
        self.services.append(service)
        return service

    def shutdown(self) -> None:
        for service in reversed(self.services):
            service.shutdown()
        self.services.clear()
        self.db.close()


@pytest.fixture
def env(tmp_path, qt_app):
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("rec-room", "录制主播")
    environment = _Env(db, room_id, tmp_path)
    yield environment
    environment.shutdown()


def test_start_and_stop_persists_history(env, qt_app):
    service = env.service()
    started: list[tuple[int, dict]] = []
    finished: list[tuple[int, dict]] = []
    service.recording_started.connect(lambda rid, snap: started.append((rid, snap)))
    service.recording_finished.connect(lambda rid, snap: finished.append((rid, snap)))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(started))

    snapshot = started[0][1]
    path = Path(snapshot["file_path"])
    assert path.parent == env.tmp_path / "recordings"
    assert "rec-room" in path.name
    assert service.is_recording(env.room_id)
    row = env.db.get_recording(snapshot["recording_id"])
    assert row["status"] == "recording"
    assert row["ended_at"] is None
    assert row["web_rid_snapshot"] == "rec-room"
    assert row["session_id"] is None

    _wait_for(qt_app, lambda: path.exists() and path.stat().st_size > 0)
    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: bool(finished))

    result = finished[0][1]
    assert result["status"] == "completed"
    assert result["file_size"] > 0
    assert result["error"] == ""
    assert service.is_recording(env.room_id) is False
    row = env.db.get_recording(result["recording_id"])
    assert row["status"] == "completed"
    assert row["ended_at"] is not None
    assert row["file_size"] > 0


def test_recording_links_to_monitor_session(env, qt_app):
    """录制记录关联当前监控 Session，便于历史追溯。"""
    session_id = env.db.start_session(env.room_id)
    service = env.service(session_id_provider=lambda rid: session_id)
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(started))
    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: bool(finished))

    row = env.db.get_recording(started[0]["recording_id"])
    assert row["session_id"] == session_id
    assert row["status"] == "completed"


def test_unexpected_nonzero_exit_marks_failed(env, qt_app):
    service = env.service("exit3")
    finished: list[dict] = []
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(finished))

    assert finished[0]["status"] == "failed"
    assert "退出码 3" in finished[0]["error"]
    assert env.db.get_recording(finished[0]["recording_id"])["status"] == "failed"


def test_empty_output_marks_failed(env, qt_app):
    service = env.service("empty")
    finished: list[dict] = []
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(finished))

    assert finished[0]["status"] == "failed"
    assert "输出文件为空" in finished[0]["error"]


def test_duplicate_start_is_rejected(env, qt_app):
    service = env.service()
    started: list[dict] = []
    errors: list[str] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_error.connect(lambda rid, msg: errors.append(msg))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(started))
    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(errors))

    assert "已在录制中" in errors[0]
    assert len(service.active_rooms()) == 1


def test_missing_stream_url_is_rejected(env, qt_app):
    service = env.service()
    errors: list[str] = []
    service.recording_error.connect(lambda rid, msg: errors.append(msg))

    service.start_recording(env.room_id, None)
    _wait_for(qt_app, lambda: bool(errors))

    assert "尚未解析到直播流地址" in errors[0]
    assert env.db.list_recordings(status="recording") == []


def test_missing_room_is_rejected(env, qt_app):
    service = env.service()
    errors: list[str] = []
    service.recording_error.connect(lambda rid, msg: errors.append(msg))

    service.start_recording(999999, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(errors))

    assert "房间不存在" in errors[0]


def test_two_rooms_record_independently(env, qt_app):
    second_room = env.db.add_room("rec-room-2", "第二主播")
    service = env.service()
    started: list[tuple[int, dict]] = []
    finished: list[tuple[int, dict]] = []
    service.recording_started.connect(lambda rid, snap: started.append((rid, snap)))
    service.recording_finished.connect(lambda rid, snap: finished.append((rid, snap)))

    service.start_recording(env.room_id, "https://example.invalid/a.flv")
    service.start_recording(second_room, "https://example.invalid/b.flv")
    _wait_for(qt_app, lambda: len(started) == 2)
    assert service.active_rooms() == sorted([env.room_id, second_room])

    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: len(finished) == 1)

    assert service.is_recording(env.room_id) is False
    assert service.is_recording(second_room) is True

    service.stop_recording(second_room)
    _wait_for(qt_app, lambda: len(finished) == 2)
    assert service.active_rooms() == []


def test_stalled_output_is_stopped_and_failed(tmp_path, qt_app):
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("stall-room", "停滞主播")
    env = _Env(db, room_id, tmp_path)
    service = env.service(
        "stall", stall_timeout_s=0.2, poll_interval_s=0.03, stop_grace_s=0.3
    )
    finished: list[dict] = []
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    try:
        service.start_recording(room_id, "https://example.invalid/live.flv")
        _wait_for(qt_app, lambda: bool(finished), timeout=10)
        assert finished[0]["status"] == "failed"
        assert "未增长" in finished[0]["error"]
    finally:
        env.shutdown()


def test_shutdown_stops_active_recordings(env, qt_app):
    service = env.service()
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(started))
    path = Path(started[0]["file_path"])
    _wait_for(qt_app, lambda: path.exists() and path.stat().st_size > 0)

    report = service.shutdown()

    assert report.thread_exited is True
    assert report.remaining_active == 0
    row = env.db.get_recording(started[0]["recording_id"])
    assert row["status"] == "completed"
    assert row["file_size"] > 0
    # 幂等：重复关闭不抛错
    assert service.shutdown().thread_exited is True


def test_start_after_shutdown_is_rejected(env, qt_app):
    service = env.service()
    service.shutdown()
    errors: list[str] = []
    service.recording_error.connect(lambda rid, msg: errors.append(msg))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")

    assert errors and "已关闭" in errors[0]


def test_recovery_marks_legacy_recording_interrupted(tmp_path, qt_app):
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("recover-room", "遗留主播")
    file_path = tmp_path / "legacy.ts"
    file_path.write_bytes(b"x" * 100)
    legacy_id = db.start_recording(room_id, str(file_path))
    stub = tmp_path / "stub.py"
    stub.write_text(STUB, encoding="utf-8")

    service = RecordingService(
        db,
        output_dir=tmp_path / "recordings",
        command_builder=lambda f, u, o: [sys.executable, str(stub), str(o), "stdin"],
        poll_interval_s=0.05,
        stop_grace_s=0.3,
    )
    try:
        row = db.get_recording(legacy_id)
        assert row["status"] == "interrupted"
        assert row["ended_at"] is not None
        assert row["file_size"] == 100
        assert row["file_path"] == str(file_path)
    finally:
        service.shutdown()
        db.close()


def test_unconfirmed_process_exit_is_not_marked_completed(env, qt_app, monkeypatch):
    """停止后无法确认 FFmpeg 退出时，必须标记失败而不是 completed。"""
    from dlrecorder.application import recording_service as rs

    service = env.service()
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(started))
    _wait_for(
        qt_app, lambda: Path(started[0]["file_path"]).exists()
    )

    original_stop = rs.FFmpegProcess.stop

    def fake_stop(self):
        report = original_stop(self)
        # 进程实际已退出，但停止报告无法确认；服务不得据此判定完成。
        return rs.FFmpegStopReport(
            report.exit_code,
            False,
            True,
            True,
            False,
            report.waited_s,
        )

    monkeypatch.setattr(rs.FFmpegProcess, "stop", fake_stop)
    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: bool(finished))

    assert finished[0]["status"] == "failed"
    assert "未能确认退出" in finished[0]["error"]
    assert env.db.get_recording(finished[0]["recording_id"])["status"] == "failed"
