"""稳定性与并发冒烟测试（Mock 解析器，不访问网络）。"""
from __future__ import annotations

import time

from PySide6.QtWidgets import QApplication

from dlrecorder.application.app import Application


def _wait_for(predicate, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def _sessions(application: Application):
    return application.db._get_conn().execute(
        "SELECT * FROM monitor_sessions ORDER BY id"
    ).fetchall()


def test_ten_rooms_monitor_and_stop_cleanly(tmp_path, qt_app):
    """10 个房间并发启动/停止后，Session 全部正常结束且无残留运行状态。"""
    application = Application(base_dir=tmp_path)
    try:
        room_ids = [
            application.service.add_room(f"stress-{i}", f"压力房间{i}")
            for i in range(10)
        ]
        assert all(room_id is not None for room_id in room_ids)

        for room_id in room_ids:
            application.service.start_monitor(room_id)
        _wait_for(
            lambda: all(
                application.service.is_monitoring(room_id) for room_id in room_ids
            )
        )

        for room_id in room_ids:
            application.service.stop_monitor(room_id)
        _wait_for(
            lambda: not any(
                application.service.is_monitoring(room_id) for room_id in room_ids
            )
        )

        rows = _sessions(application)
        assert len(rows) == len(room_ids)
        assert all(row["status"] == "stopped" for row in rows)
        assert all(row["ended_at"] is not None for row in rows)
    finally:
        application.shutdown()


def test_repeated_start_stop_cycles_keep_history_consistent(tmp_path, qt_app):
    """同一房间多轮启停后，每轮都留下独立且已结束的 Session。"""
    application = Application(base_dir=tmp_path)
    try:
        room_id = application.service.add_room("cycle-room", "循环房间")
        cycles = 5
        for _ in range(cycles):
            application.service.start_monitor(room_id)
            _wait_for(lambda: application.service.is_monitoring(room_id))
            application.service.stop_monitor(room_id)
            _wait_for(lambda: not application.service.is_monitoring(room_id))

        rows = _sessions(application)
        assert len(rows) == cycles
        assert len({row["id"] for row in rows}) == cycles
        assert all(row["status"] == "stopped" for row in rows)
    finally:
        application.shutdown()
