"""通知中心单元测试：去重、合并、恢复提示与降级。"""
from __future__ import annotations

from dlrecorder.presentation.notification_center import NotificationCenter


class FakeTray:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.fail = fail

    def show_message(self, title: str, message: str, severity: str = "info") -> None:
        if self.fail:
            raise RuntimeError("tray broken")
        self.messages.append((title, message, severity))


class Harness:
    def __init__(self, *, tray=None, clock_value: float = 1000.0) -> None:
        self.tray = tray if tray is not None else FakeTray()
        self.now = clock_value
        self.status: list[str] = []
        self.rows: list[tuple[int, str, dict]] = []
        self.center = NotificationCenter(
            tray_provider=lambda: self.tray,
            status_writer=self.status.append,
            room_state_writer=lambda rid, state, **kw: self.rows.append(
                (rid, state, kw)
            ),
            name_provider=lambda rid: f"房间{rid}",
            clock=lambda: self.now,
        )

    def flush(self) -> None:
        self.center.flush()

    def close(self) -> None:
        self.center.dispose()


def _error(room_id: int, category: str = "network", **kwargs) -> dict:
    payload = {
        "phase": "runtime_failed",
        "category": category,
        "retryable": False,
        "message": "连接中断",
    }
    payload.update(kwargs)
    return payload


def test_same_room_category_is_deduped_within_60_seconds(qt_app):
    harness = Harness()
    try:
        harness.center.on_error(1, _error(1))
        harness.flush()
        assert len(harness.tray.messages) == 1

        harness.now += 59.0
        harness.center.on_error(1, _error(1))
        harness.flush()
        assert len(harness.tray.messages) == 1

        harness.now += 2.0
        harness.center.on_error(1, _error(1))
        harness.flush()
        assert len(harness.tray.messages) == 2
    finally:
        harness.close()


def test_different_categories_are_not_suppressed(qt_app):
    harness = Harness()
    try:
        harness.center.on_error(1, _error(1, "network"))
        harness.flush()
        harness.center.on_error(1, _error(1, "disk", message="磁盘不足"))
        harness.flush()
        assert len(harness.tray.messages) == 2
    finally:
        harness.close()


def test_three_rooms_within_window_are_merged(qt_app):
    harness = Harness()
    try:
        for room_id in (1, 2, 3):
            harness.center.on_error(room_id, _error(room_id))
        harness.flush()
        assert len(harness.tray.messages) == 1
        title, message, severity = harness.tray.messages[0]
        assert "多个直播间" in title
        assert "3 个直播间" in message
        assert severity == "warning"
        assert any("3 个直播间" in text for text in harness.status)
    finally:
        harness.close()


def test_two_rooms_are_emitted_individually(qt_app):
    harness = Harness()
    try:
        harness.center.on_error(1, _error(1))
        harness.center.on_error(2, _error(2))
        harness.flush()
        assert len(harness.tray.messages) == 2
    finally:
        harness.close()


def test_reconnecting_never_emits_failure_bubble(qt_app):
    harness = Harness()
    try:
        harness.center.on_reconnecting(
            1, {"attempt": 2, "max_attempts": 5, "delay_s": 12}
        )
        harness.flush()
        assert harness.tray.messages == []
        assert harness.rows[-1][1] == "reconnecting"
        assert harness.rows[-1][2] == {"attempt": 2, "max_attempts": 5}
    finally:
        harness.close()


def test_segment_failure_with_retry_is_silent(qt_app):
    harness = Harness()
    try:
        harness.center.on_finished(
            1,
            {
                "scope": "segment",
                "status": "failed",
                "will_retry": True,
                "requested": False,
                "error": "FFmpeg 退出码 3，正在自动续录",
            },
        )
        harness.flush()
        assert harness.tray.messages == []
    finally:
        harness.close()


def test_terminal_segment_failure_notifies(qt_app):
    harness = Harness()
    try:
        harness.center.on_finished(
            1,
            {
                "scope": "segment",
                "status": "failed",
                "will_retry": False,
                "requested": False,
                "error": "FFmpeg 退出码 3",
            },
        )
        harness.flush()
        assert len(harness.tray.messages) == 1
        assert harness.rows[-1][1] == "failed"
    finally:
        harness.close()


def test_requested_stop_is_not_reported_as_failure(qt_app):
    harness = Harness()
    try:
        harness.center.on_finished(
            1,
            {
                "scope": "session",
                "status": "failed",
                "requested": True,
                "error": "已手动停止",
            },
        )
        harness.flush()
        assert harness.tray.messages == []
    finally:
        harness.close()


def test_retryable_error_updates_status_without_bubble(qt_app):
    harness = Harness()
    try:
        harness.center.on_error(
            1,
            {
                "phase": "start_failed",
                "category": "network",
                "retryable": True,
                "message": "尚未解析到直播流地址",
            },
        )
        harness.flush()
        assert harness.tray.messages == []
        assert harness.rows[-1][1] == "idle"
        assert any("暂时无法录制" in text for text in harness.status)
    finally:
        harness.close()


def test_recovery_notice_only_after_actual_recovery_and_once_per_session(qt_app):
    harness = Harness()
    try:
        harness.center.on_started(1, {"resumed": False})
        harness.center.on_reconnecting(1, {"attempt": 1, "max_attempts": 5})
        harness.center.on_recovered(1, {"attempt": 1, "file_path": "a.ts"})
        harness.flush()
        assert len(harness.tray.messages) == 1
        title, _, severity = harness.tray.messages[0]
        assert title == "已恢复录制"
        assert severity == "info"
        assert harness.rows[-1][1] == "recording"

        # 同一次会话内再次恢复不重复提示。
        harness.center.on_recovered(1, {"attempt": 2, "file_path": "b.ts"})
        harness.flush()
        assert len(harness.tray.messages) == 1

        # 新会话开始后允许再次提示。
        harness.center.on_finished(
            1, {"scope": "session", "status": "completed", "requested": False}
        )
        harness.center.on_started(1, {"resumed": False})
        harness.center.on_recovered(1, {"attempt": 1, "file_path": "c.ts"})
        harness.flush()
        assert len(harness.tray.messages) == 2
    finally:
        harness.close()


def test_tray_unavailable_degrades_without_exception(qt_app):
    harness = Harness()
    harness.tray = None
    try:
        harness.center.on_error(1, _error(1, "disk", message="磁盘不足"))
        harness.flush()
        assert harness.status, "托盘不可用时必须至少更新状态栏"
    finally:
        harness.close()


def test_tray_failure_does_not_raise(qt_app):
    harness = Harness(tray=FakeTray(fail=True))
    try:
        harness.center.on_error(1, _error(1))
        harness.flush()
        assert harness.rows[-1][1] == "failed"
    finally:
        harness.close()


def test_writer_failure_is_swallowed(qt_app):
    def broken(_text: str) -> None:
        raise RuntimeError("window gone")

    def broken_rows(_rid, _state, **_kwargs) -> None:
        raise RuntimeError("window gone")

    center = NotificationCenter(
        tray_provider=lambda: None,
        status_writer=broken,
        room_state_writer=broken_rows,
        name_provider=lambda rid: str(rid),
    )
    try:
        center.on_error(1, _error(1))
        center.on_finished(1, {"scope": "session", "status": "failed"})
        center.on_recovered(1, {"attempt": 1})
        center.flush()
    finally:
        center.dispose()
