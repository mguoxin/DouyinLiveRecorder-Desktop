"""任务管理器生命周期测试。

使用短间隔验证任务启动、停止、异常传播。
"""
import asyncio
import threading
import time

import dlrecorder.core.task_manager as task_manager_module
from dlrecorder.core.task_manager import MonitorWorker
from dlrecorder.domain.models import Room
from dlrecorder.domain.states import ParseResult
from dlrecorder.douyin.parser import MockParser
from dlrecorder.infrastructure.db import Database


def _wait_for(qt_app, predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def test_snapshot_includes_parsed_stream_url(qt_app):
    """成功解析后，流地址应进入房间状态与 GUI 快照。"""
    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    room = Room(id=303, web_rid="stream-snap")
    parser = MockParser()
    parser.set_response(
        "stream-snap",
        ParseResult.SUCCESS,
        2,
        stream_url="https://cdn.example/live.flv",
    )
    updates: list[dict] = []
    worker.room_state_updated.connect(lambda rid, snap: updates.append(snap))
    try:
        worker.start_monitor(room, parser, interval=0.1)
        _wait_for(qt_app, lambda: bool(updates))
        assert updates[-1]["last_stream_url"] == "https://cdn.example/live.flv"
        assert room.state.last_stream_url == "https://cdn.example/live.flv"
    finally:
        worker.shutdown()


def _registration_state(worker: MonitorWorker, room_id: int):
    """在事件循环内读取注册状态，避免测试跨线程直接访问注册表。"""
    async def query():
        current = worker._tasks.get(room_id)
        if current is None:
            return None
        return (
            current.task_id,
            current.session_id,
            current.task.done(),
            current.stop_event.is_set(),
        )

    return asyncio.run_coroutine_threadsafe(query(), worker.loop).result(timeout=2)


def _cancel_task(worker: MonitorWorker, room_id: int) -> int:
    async def cancel():
        current = worker._tasks[room_id]
        current.task.cancel()
        return current.session_id

    return asyncio.run_coroutine_threadsafe(cancel(), worker.loop).result(timeout=2)


def test_worker_start_stop(qt_app):
    worker = MonitorWorker()
    worker.start()
    time.sleep(0.3)
    assert worker.loop is not None
    assert worker.loop.is_running()
    worker.shutdown()
    assert worker.loop is None


def test_monitor_task_runs_and_stops(qt_app):
    worker = MonitorWorker()
    worker.start()
    time.sleep(0.2)

    room = Room(id=1, web_rid="100", display_name="测试")
    parser = MockParser()
    parser.set_response("100", ParseResult.SUCCESS, 2)

    updates: list[int] = []
    errors: list[str] = []
    stopped: list[int] = []
    worker.room_state_updated.connect(lambda rid, snap: updates.append(rid))
    worker.task_error.connect(lambda rid, e: errors.append(e))
    worker.task_stopped.connect(lambda rid: stopped.append(rid))

    worker.start_monitor(room, parser, interval=0.1)
    # 等待至少一次更新
    deadline = time.time() + 3
    while not updates and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.05)
    assert updates, "应收到状态更新信号"

    worker.stop_monitor(1)
    deadline = time.time() + 3
    while not stopped and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.05)
    assert stopped, "应收到任务停止信号"

    worker.shutdown()


def test_task_exception_does_not_crash_loop(qt_app):
    """解析器抛异常时，任务应通过 task_error 上报，事件循环不崩溃。"""
    worker = MonitorWorker()
    worker.start()
    time.sleep(0.2)

    class ExplodingParser(MockParser):
        def fetch_and_parse(self, web_rid):
            raise RuntimeError("boom")

    room = Room(id=2, web_rid="200")
    parser = ExplodingParser()

    errors: list[str] = []
    worker.task_error.connect(lambda rid, e: errors.append(e))

    worker.start_monitor(room, parser, interval=0.1)
    deadline = time.time() + 3
    while not errors and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.05)
    assert errors, "应捕获到异常"
    assert "boom" in errors[0]

    # 事件循环仍在运行
    assert worker.loop is not None
    assert worker.loop.is_running()

    worker.shutdown()


def test_immediate_stop_closes_session_and_cleans_registration(qt_app):
    """启动后立即停止不丢失请求，并清理任务、停止事件和 Session。"""
    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    room = Room(id=10, web_rid="rapid-stop")
    ended: list[tuple[int, str, str | None]] = []
    stopped: list[int] = []
    worker.task_stopped.connect(lambda rid: stopped.append(rid))

    try:
        worker.start_monitor(
            room,
            MockParser(),
            interval=5.0,
            session_starter=lambda rid: 101,
            session_ender=lambda sid, status, error: ended.append((sid, status, error)),
        )
        worker.stop_monitor(room.id)

        _wait_for(qt_app, lambda: bool(ended))
        _wait_for(qt_app, lambda: not worker.is_running(room.id))
        _wait_for(qt_app, lambda: stopped == [room.id])
        assert ended == [(101, "stopped", None)]
        assert _registration_state(worker, room.id) is None
    finally:
        worker.shutdown()


def test_restarting_during_old_task_exit_preserves_new_task(qt_app):
    """旧任务完成不得删除新注册、停止新任务或发出旧停止状态。"""
    class BlockingFirstParser(MockParser):
        def __init__(self):
            super().__init__()
            self.first_call_started = threading.Event()
            self.release_first = threading.Event()
            self._calls = 0
            self._calls_lock = threading.Lock()

        def fetch_and_parse(self, web_rid):
            with self._calls_lock:
                self._calls += 1
                call_number = self._calls
            if call_number == 1:
                self.first_call_started.set()
                assert self.release_first.wait(timeout=3)
            return ParseResult.SUCCESS, 2

    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    room = Room(id=20, web_rid="restart")
    parser = BlockingFirstParser()
    created_sessions: list[int] = []
    ended: list[tuple[int, str, str | None]] = []
    stopped: list[int] = []
    worker.task_stopped.connect(lambda rid: stopped.append(rid))

    def start_session(room_id: int) -> int:
        session_id = 201 + len(created_sessions)
        created_sessions.append(session_id)
        return session_id

    try:
        worker.start_monitor(
            room,
            parser,
            interval=5.0,
            session_starter=start_session,
            session_ender=lambda sid, status, error: ended.append((sid, status, error)),
        )
        _wait_for(qt_app, parser.first_call_started.is_set)
        first = _registration_state(worker, room.id)
        assert first is not None and first[1] == 201

        worker.stop_monitor(room.id)
        _wait_for(
            qt_app,
            lambda: (state := _registration_state(worker, room.id)) is not None and state[3],
        )
        worker.start_monitor(
            room,
            parser,
            interval=5.0,
            session_starter=start_session,
            session_ender=lambda sid, status, error: ended.append((sid, status, error)),
        )
        _wait_for(
            qt_app,
            lambda: (state := _registration_state(worker, room.id)) is not None
            and state[0] != first[0],
        )
        second = _registration_state(worker, room.id)
        assert second is not None
        assert second[1] == 202
        assert second[3] is False, "旧停止事件不得影响新任务"

        parser.release_first.set()
        _wait_for(qt_app, lambda: any(sid == 201 for sid, _, _ in ended))
        current = _registration_state(worker, room.id)
        assert current is not None and current[0] == second[0]
        assert worker.is_running(room.id)
        assert stopped == [], "被替换的旧任务不得发出新任务的停止状态"

        worker.stop_monitor(room.id)
        _wait_for(qt_app, lambda: any(sid == 202 for sid, _, _ in ended))
        _wait_for(qt_app, lambda: stopped == [room.id])
        assert ended == [(201, "stopped", None), (202, "stopped", None)]
        assert created_sessions == [201, 202]
        assert _registration_state(worker, room.id) is None
    finally:
        parser.release_first.set()
        worker.shutdown()


def test_cancelled_task_closes_only_its_own_session(qt_app):
    """异常取消闭环自身 Session，其他房间继续运行并可正常停止。"""
    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    rooms = [Room(id=30, web_rid="room-30"), Room(id=31, web_rid="room-31")]
    ended: list[tuple[int, str, str | None]] = []

    try:
        for room in rooms:
            worker.start_monitor(
                room,
                MockParser(),
                interval=5.0,
                session_starter=lambda rid: rid + 100,
                session_ender=lambda sid, status, error: ended.append((sid, status, error)),
            )
        _wait_for(
            qt_app,
            lambda: all(_registration_state(worker, room.id) is not None for room in rooms),
        )

        cancelled_session = _cancel_task(worker, rooms[0].id)
        assert cancelled_session == 130
        _wait_for(qt_app, lambda: any(sid == 130 for sid, _, _ in ended))
        assert ended[0] == (130, "interrupted", "监控任务被取消")
        assert worker.is_running(rooms[1].id)
        assert all(sid != 131 for sid, _, _ in ended)

        worker.stop_monitor(rooms[1].id)
        _wait_for(qt_app, lambda: any(sid == 131 for sid, _, _ in ended))
        assert (131, "stopped", None) in ended
        assert not worker.is_running(rooms[0].id)
        assert not worker.is_running(rooms[1].id)
        assert _registration_state(worker, rooms[0].id) is None
        assert _registration_state(worker, rooms[1].id) is None
    finally:
        worker.shutdown()


FATAL_WAIT_INTERVAL = 0.05


def test_unrecoverable_task_error_marks_session_error(qt_app, monkeypatch):
    """等待阶段不可恢复异常：Session 记为 error，注册表清理，其他房间不受影响。"""
    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    fatal_room = Room(id=40, web_rid="fatal-room")
    healthy_room = Room(id=41, web_rid="healthy-room")
    ended: list[tuple[int, str, str | None]] = []
    errors: list[str] = []
    stopped: list[int] = []
    worker.task_error.connect(lambda rid, msg: errors.append(msg))
    worker.task_stopped.connect(lambda rid: stopped.append(rid))

    original_wait_for = task_manager_module.asyncio.wait_for

    def failing_wait_for(awaitable, timeout=None):
        # 仅让目标任务在「可中断等待」阶段抛出不可恢复异常，
        # 普通解析异常仍走逐次捕获路径，不能被误判为任务级错误。
        if timeout == FATAL_WAIT_INTERVAL:
            awaitable.close()
            raise RuntimeError("event loop failure")
        return original_wait_for(awaitable, timeout)

    monkeypatch.setattr(task_manager_module.asyncio, "wait_for", failing_wait_for)

    try:
        worker.start_monitor(
            fatal_room,
            MockParser(),
            FATAL_WAIT_INTERVAL,
            session_starter=lambda rid: 501,
            session_ender=lambda sid, status, error: ended.append((sid, status, error)),
        )
        worker.start_monitor(
            healthy_room,
            MockParser(),
            5.0,
            session_starter=lambda rid: 502,
            session_ender=lambda sid, status, error: ended.append((sid, status, error)),
        )

        _wait_for(qt_app, lambda: any(sid == 501 for sid, _, _ in ended))
        _wait_for(qt_app, lambda: not worker.is_running(fatal_room.id))
        assert (501, "error", "event loop failure") in ended
        assert "event loop failure" in errors
        assert _registration_state(worker, fatal_room.id) is None

        # 其他房间的任务与 Session 不受影响
        assert worker.is_running(healthy_room.id)
        assert all(sid != 502 for sid, _, _ in ended)

        worker.stop_monitor(healthy_room.id)
        _wait_for(qt_app, lambda: any(sid == 502 for sid, _, _ in ended))
        assert (502, "stopped", None) in ended
        _wait_for(qt_app, lambda: sorted(stopped) == [fatal_room.id, healthy_room.id])
        assert _registration_state(worker, healthy_room.id) is None
    finally:
        worker.shutdown()


def test_session_end_failure_keeps_registration_cleanup(qt_app, tmp_path):
    """Session 结束失败：注册表仍清理，数据库保留 running 记录且不伪造成功。"""
    db = Database(tmp_path / "session_failure.db")
    rid_a = db.add_room("end-fail", "结束失败")
    rid_b = db.add_room("end-ok", "结束正常")
    sid_a = db.start_session(rid_a)
    sid_b = db.start_session(rid_b)
    room_a = Room(id=50, web_rid="end-fail")
    room_b = Room(id=51, web_rid="end-ok")
    sessions = {room_a.id: sid_a, room_b.id: sid_b}

    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    ended: list[tuple[int, str, str | None]] = []
    errors: list[str] = []
    stopped: list[int] = []
    worker.task_error.connect(lambda rid, msg: errors.append(msg))
    worker.task_stopped.connect(lambda rid: stopped.append(rid))

    def end_session(session_id, status, error):
        if session_id == sid_a:
            raise RuntimeError("database is locked")
        ended.append((session_id, status, error))
        db.end_session(session_id, status, error)

    try:
        for room in (room_a, room_b):
            worker.start_monitor(
                room,
                MockParser(),
                5.0,
                session_starter=lambda rid: sessions[rid],
                session_ender=end_session,
            )
        _wait_for(
            qt_app,
            lambda: _registration_state(worker, room_a.id) is not None
            and _registration_state(worker, room_b.id) is not None,
        )
        worker.stop_monitor(room_a.id)
        worker.stop_monitor(room_b.id)
        _wait_for(
            qt_app,
            lambda: sorted(stopped) == sorted([room_a.id, room_b.id]),
        )
        _wait_for(
            qt_app,
            lambda: not worker.is_running(room_a.id)
            and not worker.is_running(room_b.id),
        )

        assert ended == [(sid_b, "stopped", None)]
        assert any("database is locked" in msg for msg in errors)
        assert _registration_state(worker, room_a.id) is None
        assert _registration_state(worker, room_b.id) is None

        # 失败方保留真实 running 记录，不得伪造成已结束
        row_a = db._get_conn().execute(
            "SELECT status, ended_at, error_message FROM monitor_sessions WHERE id=?",
            (sid_a,),
        ).fetchone()
        assert row_a["status"] == "running"
        assert row_a["ended_at"] is None
        assert row_a["error_message"] is None

        row_b = db._get_conn().execute(
            "SELECT status, ended_at FROM monitor_sessions WHERE id=?",
            (sid_b,),
        ).fetchone()
        assert row_b["status"] == "stopped"
        assert row_b["ended_at"] is not None
    finally:
        worker.shutdown()
        db.close()
