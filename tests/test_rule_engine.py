"""自动化规则测试：时间窗、幂等、优先级、磁盘预警、后处理与恢复。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from dlrecorder.application.rule_engine import RuleActions, RuleEngine
from dlrecorder.domain.automation_rules import RuleConfigError, ScheduleWindow
from dlrecorder.infrastructure.db import Database


class FakeActions:
    def __init__(self) -> None:
        self.monitoring: set[int] = set()
        self.recording: set[int] = set()
        self.streams: set[int] = set()
        self.free_mb: float | None = 10_000.0
        self.notifications: list[tuple[str, str]] = []
        self.calls: list[tuple[str, int]] = []

    def build(self) -> RuleActions:
        return RuleActions(
            is_monitoring=lambda room_id: room_id in self.monitoring,
            start_monitor=self._start_monitor,
            stop_monitor=self._stop_monitor,
            is_recording=lambda room_id: room_id in self.recording,
            start_recording=self._start_recording,
            stop_recording=self._stop_recording,
            has_stream=lambda room_id: room_id in self.streams,
            notify=lambda title, message: self.notifications.append((title, message)),
            free_space_mb=lambda: self.free_mb,
        )

    def _start_monitor(self, room_id: int) -> bool:
        self.calls.append(("start_monitor", room_id))
        self.monitoring.add(room_id)
        return True

    def _stop_monitor(self, room_id: int) -> bool:
        self.calls.append(("stop_monitor", room_id))
        self.monitoring.discard(room_id)
        return True

    def _start_recording(self, room_id: int) -> bool:
        self.calls.append(("start_recording", room_id))
        self.recording.add(room_id)
        return True

    def _stop_recording(self, room_id: int) -> bool:
        self.calls.append(("stop_recording", room_id))
        self.recording.discard(room_id)
        return True


@pytest.fixture
def env(tmp_path: Path):
    db = Database(tmp_path / "data" / "app.db")
    actions = FakeActions()
    engines: list[RuleEngine] = []
    events: list[tuple[int, dict]] = []

    def make_engine(**kwargs) -> RuleEngine:
        engine = RuleEngine(db, actions.build(), auto_start=False, **kwargs)
        engine.rule_run_recorded.connect(lambda rid, payload: events.append((rid, payload)))
        engines.append(engine)
        return engine

    class Env:
        pass

    instance = Env()
    instance.db = db
    instance.actions = actions
    instance.events = events
    instance.make_engine = make_engine
    try:
        yield instance
    finally:
        for engine in engines:
            engine.shutdown()
        db.close()


def test_schedule_window_parsing_and_matching() -> None:
    window = ScheduleWindow.parse({"days": [0, 1], "start": "09:30", "end": "12:00"})
    assert window.contains(datetime(2026, 1, 5, 10, 0))  # 周一
    assert not window.contains(datetime(2026, 1, 6, 13, 0))
    assert not window.contains(datetime(2026, 1, 7, 10, 0))  # 周三不在 days

    overnight = ScheduleWindow.parse({"start": "22:30", "end": "02:00"})
    assert overnight.contains(datetime(2026, 1, 5, 23, 0))
    assert overnight.contains(datetime(2026, 1, 5, 1, 0))
    assert not overnight.contains(datetime(2026, 1, 5, 12, 0))

    with pytest.raises(RuleConfigError):
        ScheduleWindow.parse({"start": "25:00", "end": "26:00"})


def test_monitor_schedule_is_idempotent_and_does_not_stop_user_tasks(env):
    room_id = env.db.add_room("rid-rule", "规则主播")
    env.db.create_automation_rule(
        name="全天监控", rule_type="monitor_schedule", room_id=room_id,
        config={"start": "00:00", "end": "23:59"}, priority=10,
    )
    engine = env.make_engine()
    noon = datetime(2026, 1, 5, 12, 0)

    assert engine.tick_once(noon) == 1
    assert env.actions.monitoring == {room_id}
    assert engine.tick_once(noon) == 0
    runs = env.db.list_rule_runs()
    assert len(runs) == 1 and runs[0]["status"] == "succeeded"

    # 窗口外，但监控是用户手动开启的 → 引擎不得停止。
    engine._managed.clear()
    assert engine.tick_once(datetime(2026, 1, 5, 23, 59, 30)) == 0
    assert room_id in env.actions.monitoring


def test_priority_resolves_conflicting_schedule_rules(env):
    room_id = env.db.add_room("rid-priority", "优先级主播")
    # priority 数字越小优先级越高。
    env.db.create_automation_rule(
        name="高优先级-关闭", rule_type="monitor_schedule", room_id=room_id,
        config={"start": "00:00", "end": "01:00"}, priority=1,
    )
    env.db.create_automation_rule(
        name="低优先级-开启", rule_type="monitor_schedule", room_id=room_id,
        config={"start": "00:00", "end": "23:59"}, priority=90,
    )
    engine = env.make_engine()
    assert engine.tick_once(datetime(2026, 1, 5, 12, 0)) == 0
    assert env.actions.monitoring == set()

    env.db.update_automation_rule(1, enabled=False)
    assert engine.tick_once(datetime(2026, 1, 5, 12, 0)) == 1
    assert env.actions.monitoring == {room_id}


def test_recording_schedule_requires_stream(env):
    room_id = env.db.add_room("rid-record-rule", "录制规则主播")
    env.db.create_automation_rule(
        name="定时录制", rule_type="recording_schedule", room_id=room_id,
        config={"start": "09:00", "end": "18:00"},
    )
    engine = env.make_engine()
    now = datetime(2026, 1, 5, 12, 0)
    assert engine.tick_once(now) == 0
    assert env.actions.recording == set()

    env.actions.streams.add(room_id)
    assert engine.tick_once(now) == 1
    assert env.actions.recording == {room_id}

    # 窗口结束：停止引擎启动的录制。
    assert engine.tick_once(datetime(2026, 1, 5, 20, 0)) == 1
    assert env.actions.recording == set()


def test_disk_guard_latches_and_recovers(env):
    env.db.create_automation_rule(
        name="磁盘预警", rule_type="disk_guard",
        config={"min_free_mb": 2048},
    )
    engine = env.make_engine()
    env.actions.free_mb = 1024.0
    assert engine.tick_once() == 1
    assert engine.tick_once() == 0
    assert len(env.actions.notifications) == 1

    env.actions.free_mb = 8192.0
    assert engine.tick_once() == 1
    titles = [title for title, _ in env.actions.notifications]
    assert titles == ["磁盘空间预警", "磁盘空间恢复"]
    assert len(env.db.list_rule_runs()) == 2


def test_invalid_rule_config_is_reported_once(env):
    room_id = env.db.add_room("rid-bad-rule", "坏规则主播")
    env.db.create_automation_rule(
        name="坏规则", rule_type="monitor_schedule", room_id=room_id,
        config={"start": "99:99", "end": "12:00"},
    )
    engine = env.make_engine()
    engine.tick_once()
    engine.tick_once()
    runs = env.db.list_rule_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "配置非法" in runs[0]["message"]
    assert len(env.actions.notifications) == 1


def test_post_process_applies_tags_and_favorite_once(env):
    room_id = env.db.add_room("rid-post", "后处理主播")
    path = Path(env.db.db_path).parent / "clip.ts"
    path.write_bytes(b"data")
    recording_id = env.db.start_recording(room_id, str(path))
    env.db.finish_recording(recording_id, "completed", 4)

    env.db.create_automation_rule(
        name="自动标签", rule_type="post_process", room_id=room_id,
        config={"tags": ["精华", "高光"], "favorite": True},
    )
    engine = env.make_engine()
    engine._handle_finished_event(recording_id, room_id, "completed")
    engine._handle_finished_event(recording_id, room_id, "completed")

    tags = {tag["name"] for tag in env.db.get_recording_tags(recording_id)}
    assert tags == {"精华", "高光"}
    assert env.db.get_recording(recording_id)["favorite"] == 1
    runs = env.db.list_rule_runs()
    assert len(runs) == 1 and runs[0]["status"] == "succeeded"

    # 非 completed 状态不触发后处理。
    engine._handle_finished_event(999, room_id, "failed")
    assert len(env.db.list_rule_runs()) == 1


def test_archive_decision_prefers_room_rule(env):
    room_id = env.db.add_room("rid-archive-rule", "归档规则主播")
    engine = env.make_engine()
    assert engine.archive_decision(room_id) is None

    env.db.create_automation_rule(
        name="全局归档", rule_type="archive", config={"enabled": True}, priority=50,
    )
    assert engine.archive_decision(room_id) is True

    env.db.create_automation_rule(
        name="本房间不归档", rule_type="archive", room_id=room_id,
        config={"enabled": False}, priority=100,
    )
    assert engine.archive_decision(room_id) is False


def test_recover_stale_rule_runs(env):
    rule_id = env.db.create_automation_rule(
        name="恢复测试", rule_type="disk_guard", config={"min_free_mb": 10},
    )
    run_id = env.db.create_rule_run(rule_id, None, status="running")
    engine = env.make_engine()
    assert engine.recover_stale_runs() == 1
    row = env.db.list_rule_runs()[0]
    assert row["id"] == run_id
    assert row["status"] == "failed"
    assert "异常退出" in row["message"]
