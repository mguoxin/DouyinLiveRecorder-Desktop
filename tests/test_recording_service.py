"""录制服务测试（本地桩进程，不读取真实直播流）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from dlrecorder.application.recording_service import RecordingService, StreamRefresh
from dlrecorder.infrastructure.db import Database

STUB = r"""
import os, sys, time
path, mode = sys.argv[1], sys.argv[2]
counter = sys.argv[3] if len(sys.argv) > 3 else ""
with open(path, "wb") as f:
    if mode != "empty":
        f.write(b"x" * 8192)
        f.flush()
runs = 0
if counter:
    if os.path.exists(counter):
        with open(counter, encoding="utf-8") as c:
            runs = len(c.read())
    with open(counter, "a", encoding="utf-8") as c:
        c.write("x")
if mode.startswith("clean:"):
    # 前 N 次以退出码 0 提前结束（模拟网络 EOF），之后保持运行等待停止。
    if runs < int(mode.split(":", 1)[1]):
        sys.exit(0)
    time.sleep(30)
elif mode.startswith("flaky:"):
    if runs < int(mode.split(":", 1)[1]):
        sys.exit(3)
    sys.stdin.readline()
elif mode == "exit3":
    sys.exit(3)
elif mode in ("sleep", "stall"):
    time.sleep(30)
elif mode == "stdin":
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
        counter = self.tmp_path / "runs.txt"

        def builder(ffmpeg_path: str, stream_url: str, output_path: Path) -> list[str]:
            return [
                sys.executable,
                str(self.stub),
                str(output_path),
                mode,
                str(counter),
            ]

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
    assert result["scope"] == "segment"
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


def test_process_registry_tracks_active_segment(env, qt_app):
    from dlrecorder.infrastructure.process_registry import ProcessRegistry

    registry = ProcessRegistry(env.tmp_path / "runtime" / "ffmpeg_processes.json")
    service = env.service(process_registry=registry, cleanup_orphans_on_start=False)
    started: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(started))

    entries = registry.entries()
    assert [e.recording_id for e in entries] == [started[0]["recording_id"]]
    assert entries[0].pid > 0

    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: not service.is_recording(env.room_id))
    _wait_for(qt_app, lambda: registry.entries() == [])


def test_startup_clears_stale_registry_entries(tmp_path, qt_app):
    from dlrecorder.infrastructure.process_registry import ProcessRecord, ProcessRegistry

    db = Database(tmp_path / "app.db")
    db.add_room("rec-room", "录制主播")
    registry = ProcessRegistry(tmp_path / "reg.json")
    registry.add(
        ProcessRecord(
            pid=999999,
            create_time=1,
            exe_path=str(tmp_path / "ffmpeg.exe"),
            recording_id=999,
            room_id=1,
            file_path=str(tmp_path / "missing.ts"),
            started_at="2026-11-18T10:00:00Z",
        )
    )
    service = RecordingService(
        db,
        output_dir=tmp_path / "recordings",
        process_registry=registry,
        poll_interval_s=0.05,
    )
    try:
        assert registry.entries() == []
    finally:
        service.shutdown()
        db.close()


def _live_resolver(url: str = "https://example.invalid/resumed.flv"):
    return lambda web_rid: StreamRefresh(live=True, stream_url=url)

def test_unexpected_exit_is_resumed_with_new_file(env, qt_app):
    service = env.service(
        "flaky:1",
        stream_resolver=_live_resolver(),
        reconnect_delays_s=(0.05,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    reconnecting: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.recording_reconnecting.connect(lambda rid, snap: reconnecting.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: len(started) == 2, timeout=15)

    assert started[1]["resumed"] is True
    assert started[1]["file_path"] != started[0]["file_path"]
    assert reconnecting and reconnecting[0]["attempt"] == 1
    assert service.is_recording(env.room_id) is True

    second_path = Path(started[1]["file_path"])
    _wait_for(qt_app, lambda: second_path.exists() and second_path.stat().st_size > 0)
    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: len(finished) == 2)

    assert finished[0]["status"] == "failed"
    assert "自动续录" in finished[0]["error"]
    assert finished[1]["status"] == "completed"
    assert env.db.get_recording(finished[0]["recording_id"])["status"] == "failed"
    assert env.db.get_recording(finished[1]["recording_id"])["status"] == "completed"


def test_clean_exit_while_live_resumes_new_segment(env, qt_app):
    """退出码 0 但直播仍在（网络 EOF）时按中断处理，新文件续录。"""
    service = env.service(
        "clean:1",
        stream_resolver=_live_resolver(),
        reconnect_delays_s=(0.05,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: len(started) == 2, timeout=15)

    assert started[1]["resumed"] is True
    assert started[1]["file_path"] != started[0]["file_path"]
    assert service.is_recording(env.room_id) is True
    assert finished[0]["status"] == "failed"
    assert finished[0]["recording_id"] != started[1]["recording_id"]


def test_clean_exit_while_offline_stays_completed(env, qt_app):
    """退出码 0 且重新解析确认已下播时，保持完成，不反复重试。"""
    service = env.service(
        "once",
        stream_resolver=lambda web_rid: StreamRefresh(
            live=False, error="直播已结束"
        ),
        reconnect_delays_s=(0.05,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(finished), timeout=15)
    time.sleep(0.3)
    qt_app.processEvents()

    assert len(started) == 1
    assert finished[0]["status"] == "completed"
    assert service.is_recording(env.room_id) is False
    assert env.db.list_recordings(status="recording") == []


def test_clean_exit_with_unknown_status_is_retried(env, qt_app):
    """退出码 0 但重新解析无法确认直播状态（网络异常）时，按异常重试并续录。"""
    calls = {"n": 0}

    def flaky_resolver(web_rid):
        calls["n"] += 1
        if calls["n"] == 1:
            return StreamRefresh(live=None, error="网络请求失败")
        return StreamRefresh(live=True, stream_url="https://example.invalid/resumed.flv")

    service = env.service(
        "clean:1",
        stream_resolver=flaky_resolver,
        reconnect_delays_s=(0.05,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    reconnecting: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.recording_reconnecting.connect(lambda rid, snap: reconnecting.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: len(started) == 2, timeout=15)

    assert started[1]["resumed"] is True
    assert reconnecting[0]["attempt"] == 1
    assert finished[0]["status"] == "failed"
    assert "无法确认直播状态" in finished[0]["error"]
    assert service.is_recording(env.room_id) is True


def test_unknown_status_retry_is_bounded(env, qt_app):
    """无法确认直播状态时的重试同样受预算约束，不会无限续录。"""
    service = env.service(
        "clean:99",
        stream_resolver=lambda web_rid: StreamRefresh(live=None, error="网络请求失败"),
        reconnect_max_attempts=1,
        reconnect_delays_s=(0.02,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: not service.is_recording(env.room_id), timeout=20)
    time.sleep(0.3)
    qt_app.processEvents()

    assert len(started) == 1
    assert finished[0]["status"] == "failed"
    assert "无法确认直播状态" in finished[0]["error"]
    assert finished[-1]["scope"] == "session"
    assert finished[-1]["status"] == "failed"
    assert finished[-1]["file_size"] is None
    assert "已尝试 1 次" in finished[-1]["error"]
    assert env.db.list_recordings(status="recording") == []


def test_stalled_output_resumes_after_stop(env, qt_app):
    """输出长时间不增长时，在该进程确认退出后续录，而不是直接结束整场录制。"""
    service = env.service(
        "stall",
        stream_resolver=_live_resolver(),
        reconnect_max_attempts=1,
        reconnect_delays_s=(0.02,),
        poll_interval_s=0.03,
        stall_timeout_s=0.2,
    )
    started: list[dict] = []
    finished: list[dict] = []
    reconnecting: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.recording_reconnecting.connect(lambda rid, snap: reconnecting.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: len(started) == 2, timeout=20)
    _wait_for(qt_app, lambda: not service.is_recording(env.room_id), timeout=25)

    assert started[1]["resumed"] is True
    assert reconnecting[0]["attempt"] == 1
    assert "长时间未增长" in reconnecting[0]["error"]
    # 第二次分段同样无增长且预算已用完：如实标记失败，不留 recording 记录。
    assert finished[-1]["status"] == "failed"
    assert env.db.list_recordings(status="recording") == []


def test_resume_attempts_are_bounded(env, qt_app):
    service = env.service(
        "flaky:99",
        stream_resolver=_live_resolver(),
        reconnect_max_attempts=2,
        reconnect_delays_s=(0.02,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(
        qt_app,
        lambda: len(finished) >= 3 and service.is_recording(env.room_id) is False,
        timeout=20,
    )

    # 初始分段 + 2 次续录分段
    assert len(started) == 3
    assert finished[-1]["scope"] == "segment"
    assert finished[-1]["status"] == "failed"
    assert "已尝试 2 次" in finished[-1]["error"]
    assert env.db.list_recordings(status="recording") == []


def test_confirmed_offline_during_resume_ends_session(env, qt_app):
    service = env.service(
        "flaky:99",
        stream_resolver=lambda web_rid: StreamRefresh(
            live=False, error="直播已结束"
        ),
        reconnect_max_attempts=3,
        reconnect_delays_s=(0.02,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: len(finished) == 2, timeout=15)

    assert len(started) == 1
    assert finished[-1]["scope"] == "session"
    assert finished[-1]["recording_id"] is None
    assert finished[-1]["file_path"] == ""
    assert finished[-1]["file_size"] is None
    assert finished[-1]["status"] == "completed"
    assert service.is_recording(env.room_id) is False


def test_stop_while_waiting_for_resume_cancels_retry(env, qt_app):
    service = env.service(
        "flaky:99",
        stream_resolver=_live_resolver(),
        reconnect_delays_s=(5.0,),
        poll_interval_s=0.03,
    )
    started: list[dict] = []
    finished: list[dict] = []
    reconnecting: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.recording_reconnecting.connect(lambda rid, snap: reconnecting.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(reconnecting), timeout=10)
    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: len(finished) == 2)

    assert len(started) == 1
    assert finished[-1]["scope"] == "session"
    assert finished[-1]["recording_id"] is None
    assert finished[-1]["file_path"] == ""
    assert finished[-1]["file_size"] is None
    assert finished[-1]["status"] == "failed"
    assert "手动停止" in finished[-1]["error"]
    assert service.is_recording(env.room_id) is False


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


def test_shutdown_skips_further_resume_resolution(env, qt_app):
    """关闭请求后管理线程必须放弃后续续录解析，尽快处理 shutdown。"""
    calls: list[str] = []

    def resolver(web_rid: str) -> StreamRefresh:
        calls.append(web_rid)
        time.sleep(0.2)
        return StreamRefresh(live=None, error="网络请求失败")

    service = env.service(
        "flaky:99",
        stream_resolver=resolver,
        reconnect_delays_s=(0.5,),
        poll_interval_s=0.02,
        shutdown_timeout_s=10.0,
    )
    started: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(started))
    _wait_for(qt_app, lambda: bool(calls), timeout=10)

    began = time.monotonic()
    report = service.shutdown()
    elapsed = time.monotonic() - began

    assert report.thread_exited is True
    assert elapsed < 3.0
    # 关闭置位后不再发起第二次解析；只保留关闭前已在执行的那一次。
    assert len(calls) == 1


def test_start_error_exposes_structured_payload(env, qt_app):
    """结构化错误载荷携带阶段/类别/可重试语义，兼容信号保持不变。"""
    service = env.service()
    infos: list[dict] = []
    legacy: list[str] = []
    service.recording_error_info.connect(lambda rid, payload: infos.append(payload))
    service.recording_error.connect(lambda rid, msg: legacy.append(msg))

    service.start_recording(env.room_id, None)
    _wait_for(qt_app, lambda: bool(infos))

    assert infos[0]["phase"] == "start_failed"
    assert infos[0]["category"] == "network"
    assert infos[0]["retryable"] is True
    assert "尚未解析到直播流地址" in infos[0]["message"]
    assert legacy == ["尚未解析到直播流地址，无法开始录制"]


def test_duplicate_start_is_categorized_without_failure_state(env, qt_app):
    service = env.service()
    infos: list[dict] = []
    service.recording_error_info.connect(lambda rid, payload: infos.append(payload))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: service.is_recording(env.room_id))
    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(infos))

    assert infos[0]["phase"] == "start_failed"
    assert infos[0]["category"] == "duplicate"


def test_reconnect_marks_segment_will_retry_and_emits_recovered(env, qt_app):
    """续录路径：分段事件标记 will_retry，新分段启动后才发 recovered。"""
    service = env.service(
        "flaky:1",
        stream_resolver=_live_resolver(),
        reconnect_delays_s=(0.05,),
        poll_interval_s=0.03,
    )
    finished: list[dict] = []
    recovered: list[dict] = []
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.recording_recovered.connect(lambda rid, snap: recovered.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(recovered), timeout=15)

    assert finished[0]["scope"] == "segment"
    assert finished[0]["status"] == "failed"
    assert finished[0]["will_retry"] is True
    assert finished[0]["requested"] is False
    assert recovered[0]["attempt"] == 1


def test_terminal_segment_failure_is_not_marked_will_retry(env, qt_app):
    """没有续录预算的异常退出只发分段失败，必须可被通知层识别为最终失败。"""
    service = env.service("exit3")
    finished: list[dict] = []
    recovered: list[dict] = []
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.recording_recovered.connect(lambda rid, snap: recovered.append(snap))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(finished), timeout=15)

    assert finished[0]["scope"] == "segment"
    assert finished[0]["status"] == "failed"
    assert finished[0]["will_retry"] is False
    assert recovered == []


def test_manual_stop_while_waiting_does_not_emit_error(env, qt_app):
    """用户主动停止等待中的续录不应被报告为录制错误。"""
    service = env.service(
        "flaky:99",
        stream_resolver=_live_resolver(),
        reconnect_delays_s=(5.0,),
        poll_interval_s=0.03,
    )
    reconnecting: list[dict] = []
    finished: list[dict] = []
    errors: list[str] = []
    service.recording_reconnecting.connect(lambda rid, snap: reconnecting.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    service.recording_error.connect(lambda rid, msg: errors.append(msg))

    service.start_recording(env.room_id, "https://example.invalid/live.flv")
    _wait_for(qt_app, lambda: bool(reconnecting), timeout=10)
    service.stop_recording(env.room_id)
    _wait_for(qt_app, lambda: len(finished) == 2)

    assert errors == []
    assert finished[-1]["scope"] == "session"
    assert finished[-1]["requested"] is True
