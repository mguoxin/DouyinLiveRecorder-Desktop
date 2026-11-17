"""磁盘空间保护与 FFmpeg 可用性探测测试（本地桩进程，不访问网络）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from dlrecorder.application.recording_service import RecordingService
from dlrecorder.infrastructure.db import Database
from dlrecorder.infrastructure.ffmpeg import probe_ffmpeg

STUB = r"""
import sys, time
path = sys.argv[1]
with open(path, "wb") as f:
    f.write(b"x" * 4096)
    f.flush()
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


def _make_service(tmp_path, db, provider, **kwargs) -> RecordingService:
    stub = tmp_path / "stub.py"
    stub.write_text(STUB, encoding="utf-8")
    options = {
        "output_dir": tmp_path / "recordings",
        "command_builder": lambda f, u, o: [sys.executable, str(stub), str(o)],
        "poll_interval_s": 0.03,
        "stall_timeout_s": 0,
        "stop_grace_s": 1.0,
        "shutdown_timeout_s": 10.0,
        "free_space_provider": provider,
    }
    options.update(kwargs)
    return RecordingService(db, **options)


def test_start_is_blocked_when_disk_space_is_low(tmp_path, qt_app):
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("low-disk", "空间不足")
    service = _make_service(
        tmp_path, db, lambda path: 100.0, min_free_mb=512
    )
    errors: list[str] = []
    service.recording_error.connect(lambda rid, msg: errors.append(msg))
    try:
        service.start_recording(room_id, "https://example.invalid/live.flv")
        _wait_for(qt_app, lambda: bool(errors))

        assert "磁盘剩余空间不足" in errors[0]
        assert "512" in errors[0]
        assert service.active_rooms() == []
        assert db.list_recordings() == []
    finally:
        service.shutdown()
        db.close()


def test_recording_stops_when_space_runs_out(tmp_path, qt_app):
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("space-runout", "空间耗尽")
    calls = {"count": 0}

    def provider(path: Path):
        calls["count"] += 1
        # 前 5 次视为充足，之后模拟空间不足，避免受空闲轮询影响。
        return 10_000.0 if calls["count"] <= 5 else 10.0

    service = _make_service(tmp_path, db, provider, min_free_mb=512)
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    try:
        service.start_recording(room_id, "https://example.invalid/live.flv")
        _wait_for(qt_app, lambda: bool(started))
        _wait_for(qt_app, lambda: bool(finished))

        assert finished[0]["status"] == "failed"
        assert "磁盘剩余空间不足" in finished[0]["error"]
        row = db.get_recording(finished[0]["recording_id"])
        assert row["status"] == "failed"
        assert service.active_rooms() == []
    finally:
        service.shutdown()
        db.close()


def test_disk_check_disabled_when_threshold_is_zero(tmp_path, qt_app):
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("no-check", "不检查")
    service = _make_service(tmp_path, db, lambda path: 0.0, min_free_mb=0)
    started: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    try:
        service.start_recording(room_id, "https://example.invalid/live.flv")
        _wait_for(qt_app, lambda: bool(started))
        assert service.is_recording(room_id) is True
    finally:
        service.shutdown()
        db.close()


def test_runtime_setters_apply_to_new_recordings(tmp_path, qt_app):
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("setters", "设置变更")
    service = _make_service(tmp_path, db, lambda path: None, min_free_mb=0)
    started: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    new_dir = tmp_path / "new-recordings"
    try:
        service.set_ffmpeg_path("ffmpeg-new")
        service.set_output_dir(new_dir)
        service.set_min_free_mb(128)
        assert service.output_dir == new_dir
        assert service.min_free_mb == 128.0

        service.start_recording(room_id, "https://example.invalid/live.flv")
        _wait_for(qt_app, lambda: bool(started))
        assert Path(started[0]["file_path"]).parent == new_dir
    finally:
        service.shutdown()
        db.close()


def test_probe_ffmpeg_missing_executable(tmp_path):
    ok, message = probe_ffmpeg(str(tmp_path / "not-exists" / "ffmpeg.exe"))
    assert ok is False
    assert message


def test_probe_ffmpeg_detects_stub_version(tmp_path):
    if sys.platform != "win32":
        pytest.skip("仅 Windows 使用 .cmd 桩程序")
    stub = tmp_path / "ffmpeg-stub.cmd"
    stub.write_text("@echo ffmpeg version 6.0-stub\r\n", encoding="ascii")

    ok, message = probe_ffmpeg(str(stub), timeout_s=10.0)

    assert ok is True
    assert "ffmpeg version 6.0-stub" in message


def test_free_space_falls_back_to_existing_ancestor(tmp_path, caplog):
    """录制目录被外部删除时，按上级目录所在卷读取，不报错、不刷告警。"""
    from dlrecorder.infrastructure import disk

    missing = tmp_path / "recordings" / "已删除"

    with caplog.at_level("WARNING"):
        free = disk.free_space_mb(missing)

    assert free is not None and free > 0
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_free_space_warns_only_once_when_unreadable(tmp_path, caplog, monkeypatch):
    """底层读取持续失败时，同一路径只告警一次，避免每秒刷屏。"""
    from dlrecorder.infrastructure import disk

    def boom(_path):
        raise OSError(3, "系统找不到指定的路径。")

    monkeypatch.setattr(disk.shutil, "disk_usage", boom)
    missing = tmp_path / "不可读" / "recordings"

    with caplog.at_level("DEBUG"):
        for _ in range(5):
            assert disk.free_space_mb(missing) is None

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, f"同一路径只应告警一次，实际={len(warnings)}"
