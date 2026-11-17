"""批次 C 集成测试（T11–T15、T18、T19）。

覆盖：执行器线程边界、任务身份隔离、停止取消、关闭报告、数据库关闭守门。
全部使用 Mock 解析器与临时数据库，不发起真实网络请求。
"""
import asyncio
import logging
import sqlite3
import threading
import time

from dlrecorder.application.monitor_service import MonitorService
from dlrecorder.core.task_manager import MonitorWorker
from dlrecorder.domain.models import Room
from dlrecorder.domain.states import ParseResult
from dlrecorder.douyin.parser import MockParser
from dlrecorder.infrastructure.db import Database
from dlrecorder.infrastructure.http_executor import HttpExecutor


def _wait_for(qt_app, predicate, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def _registration_state(worker: MonitorWorker, room_id: int):
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


def _task_object(worker: MonitorWorker, room_id: int):
    async def query():
        return worker._tasks[room_id].task

    return asyncio.run_coroutine_threadsafe(query(), worker.loop).result(timeout=2)


class _ThreadNameParser(MockParser):
    """记录实际执行解析的线程名。"""

    def __init__(self) -> None:
        super().__init__()
        self.thread_names: list[str] = []
        self._lock = threading.Lock()

    def fetch_and_parse(self, web_rid):
        with self._lock:
            self.thread_names.append(threading.current_thread().name)
        return ParseResult.SUCCESS, 2


def test_t11_requests_run_on_dedicated_executor_threads(qt_app):
    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    room = Room(id=101, web_rid="thread-name")
    parser = _ThreadNameParser()
    updates: list[dict] = []
    worker.room_state_updated.connect(lambda rid, snap: updates.append(snap))
    try:
        worker.start_monitor(room, parser, interval=0.1)
        _wait_for(qt_app, lambda: bool(updates))
        assert parser.thread_names
        assert all(name.startswith("dlr-http") for name in parser.thread_names), (
            f"解析请求必须在专用执行器线程执行，实际={parser.thread_names}"
        )
        assert worker.loop is not None and worker.loop.is_running()
        assert updates[-1]["last_confirmed_room_state"] == "LIVE"
    finally:
        worker.shutdown()


class _StaleFirstParser(MockParser):
    """第一次调用阻塞并返回旧结果，用于验证旧任务不得覆盖新任务状态。"""

    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0

    def fetch_and_parse(self, web_rid):
        with self._lock:
            self.calls += 1
            call_number = self.calls
        if call_number == 1:
            self.first_started.set()
            assert self.release_first.wait(timeout=10)
            return ParseResult.SUCCESS, 4  # 旧结果：下播状态
        return ParseResult.SUCCESS, 2      # 新结果：LIVE


def test_t12_replaced_task_result_does_not_overwrite_new_state(qt_app):
    worker = MonitorWorker()
    worker.start()
    assert worker.wait_until_ready()
    room = Room(id=102, web_rid="stale-result")
    parser = _StaleFirstParser()
    updates: list[dict] = []
    stopped: list[int] = []
    worker.room_state_updated.connect(lambda rid, snap: updates.append(snap))
    worker.task_stopped.connect(lambda rid: stopped.append(rid))
    try:
        worker.start_monitor(room, parser, interval=5.0)
        _wait_for(qt_app, parser.first_started.is_set)
        old_task = _task_object(worker, room.id)
        old_state = _registration_state(worker, room.id)
        assert old_state is not None

        worker.stop_monitor(room.id)
        # 停止请求先于重启进入事件循环（同一线程按提交顺序执行）。
        worker.start_monitor(room, parser, interval=5.0)
        _wait_for(
            qt_app,
            lambda: (state := _registration_state(worker, room.id)) is not None
            and state[0] != old_state[0],
        )
        _wait_for(
            qt_app,
            lambda: bool(updates) and updates[-1]["last_confirmed_room_state"] == "LIVE",
        )

        parser.release_first.set()
        _wait_for(qt_app, old_task.done)
        _wait_for(qt_app, lambda: True)

        assert updates[-1]["current_observation"] == 2, "旧任务结果不得覆盖新任务状态"
        assert updates[-1]["last_confirmed_room_state"] == "LIVE"
        assert parser.calls >= 2
        assert stopped == [], "被替换的旧任务不得发出房间级停止信号"
    finally:
        parser.release_first.set()
        worker.shutdown()


def test_t13_stop_cancels_queued_request_before_it_runs(qt_app):
    worker = MonitorWorker()
    # 单工作线程 + 排队容量，使第二个房间的请求稳定停留在队列中。
    worker._http = HttpExecutor(max_workers=1, queue_capacity=8)
    worker.start()
    assert worker.wait_until_ready()
    gate = threading.Event()
    calls = {"blocking": 0, "queued": 0}
    errors: list[tuple[int, str]] = []
    stopped: list[int] = []

    class Parser(MockParser):
        def fetch_and_parse(self, web_rid):
            if web_rid == "blocking":
                calls["blocking"] += 1
                assert gate.wait(timeout=10)
                return ParseResult.SUCCESS, 2
            calls["queued"] += 1
            return ParseResult.SUCCESS, 2

    parser = Parser()
    blocking_room = Room(id=111, web_rid="blocking")
    queued_room = Room(id=112, web_rid="queued")
    worker.task_error.connect(lambda rid, msg: errors.append((rid, msg)))
    worker.task_stopped.connect(lambda rid: stopped.append(rid))
    try:
        worker.start_monitor(blocking_room, parser, interval=5.0)
        _wait_for(qt_app, lambda: calls["blocking"] == 1)
        worker.start_monitor(queued_room, parser, interval=5.0)
        _wait_for(qt_app, lambda: worker._http.snapshot().queued == (queued_room.id,))

        worker.stop_monitor(queued_room.id)
        # 必须等待停止请求真正进入事件循环（stop_event 置位）后再释放阻塞请求，
        # 否则队列中的请求可能先被工作线程取走。
        _wait_for(
            qt_app,
            lambda: (state := _registration_state(worker, queued_room.id)) is not None
            and state[3],
        )
        gate.set()

        _wait_for(qt_app, lambda: queued_room.id in stopped)
        assert calls["queued"] == 0, "停止时尚未开始的请求不得调用解析器"
        assert not [e for e in errors if e[0] == queued_room.id], "取消不应上报为错误"
    finally:
        gate.set()
        worker.stop_monitor(blocking_room.id)
        worker.shutdown()


def test_t14_shutdown_returns_verified_report(qt_app):
    worker = MonitorWorker(close_grace_s=1.0)
    worker.start()
    assert worker.wait_until_ready()
    room = Room(id=121, web_rid="report")
    updates: list[dict] = []
    worker.room_state_updated.connect(lambda rid, snap: updates.append(snap))
    worker.start_monitor(room, MockParser(), interval=0.1)
    _wait_for(qt_app, lambda: bool(updates))

    report = worker.shutdown()

    assert report.worker_exited is True, "正常关闭时工作线程必须已退出"
    assert report.http.completed is True, "执行器关闭报告必须已完成"
    assert report.fully_closed is True
    assert report.http.completed_ok >= 1
    assert worker.loop is None
    assert worker.isRunning() is False
    # 重复关闭返回同样的判定，不阻塞、不改变结论。
    again = worker.shutdown()
    assert again.worker_exited is True


def test_t15_queue_full_rejection_is_reported_without_crashing_loop(qt_app):
    worker = MonitorWorker()
    worker._http = HttpExecutor(max_workers=1, queue_capacity=1)
    worker.start()
    assert worker.wait_until_ready()
    gate = threading.Event()
    errors: list[tuple[int, str]] = []

    class BlockingParser(MockParser):
        def fetch_and_parse(self, web_rid):
            assert gate.wait(timeout=10)
            return ParseResult.SUCCESS, 2

    rooms = [Room(id=131, web_rid="cap-1"), Room(id=132, web_rid="cap-2"), Room(id=133, web_rid="cap-3")]
    worker.task_error.connect(lambda rid, msg: errors.append((rid, msg)))
    try:
        for room in rooms:
            worker.start_monitor(room, BlockingParser(), interval=5.0)
        _wait_for(qt_app, lambda: bool(errors))
        rejected_rooms = {rid for rid, msg in errors if "queue_full" in msg}
        assert rejected_rooms == {133}, f"第三个请求应因队列已满被拒绝，实际={errors}"
        assert worker.loop is not None and worker.loop.is_running(), "拒绝不得导致事件循环崩溃"
        assert worker._http.snapshot().rejected_at_admission >= 1
    finally:
        gate.set()
        worker.shutdown()


def test_t15b_repeated_queue_full_is_reported_once(qt_app):
    worker = MonitorWorker()
    worker._http = HttpExecutor(max_workers=1, queue_capacity=1)
    worker.start()
    assert worker.wait_until_ready()
    gate = threading.Event()
    errors: list[tuple[int, str]] = []

    class BlockingParser(MockParser):
        def fetch_and_parse(self, web_rid):
            assert gate.wait(timeout=10)
            return ParseResult.SUCCESS, 2

    rooms = [
        Room(id=141, web_rid="cap-a"),
        Room(id=142, web_rid="cap-b"),
        Room(id=143, web_rid="cap-c"),
    ]
    worker.task_error.connect(lambda rid, msg: errors.append((rid, msg)))
    try:
        for room in rooms:
            worker.start_monitor(room, BlockingParser(), interval=0.05)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            qt_app.processEvents()
            time.sleep(0.02)
        rejected = [rid for rid, msg in errors if "queue_full" in msg]
        assert rejected.count(143) == 1, f"重复排队满只提示一次，实际={errors}"
        assert worker._http.snapshot().rejected_at_admission >= 2
    finally:
        gate.set()
        worker.shutdown()


def test_t18_timeout_without_worker_exit_keeps_database_open(qt_app, tmp_path, caplog):
    db_path = tmp_path / "data" / "app.db"
    db = Database(db_path)
    service = MonitorService(db, MockParser(), monitor_interval_s=0.1)
    service.add_room("blocked-room", "阻塞退出")
    stubborn_started = threading.Event()

    async def stubborn():
        # 抑制取消：模拟无法在超时内退出的后台工作项。
        stubborn_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(1.5)

    try:
        assert service._worker.wait_until_ready()
        asyncio.run_coroutine_threadsafe(stubborn(), service._worker.loop)
        assert stubborn_started.wait(timeout=5)
        service._worker._shutdown_timeout_ms = 200

        with caplog.at_level(logging.CRITICAL, logger="dlrecorder.application.monitor_service"):
            report = service.shutdown()

        assert report.worker_exited is False, "超时未退出时必须如实报告"
        assert db._conn is not None, "工作线程未退出时不得关闭数据库连接"
        assert "保留数据库连接" in caplog.text
    finally:
        assert service._worker.wait(5000), "测试清理：阻塞任务结束后线程应退出"
        db.close()


def test_t19_normal_shutdown_closes_database(qt_app, tmp_path):
    db_path = tmp_path / "data" / "app.db"
    db = Database(db_path)
    service = MonitorService(db, MockParser(), monitor_interval_s=0.1)
    room_id = service.add_room("normal-room", "正常退出")
    try:
        service.start_monitor(room_id)
        _wait_for(qt_app, lambda: service.is_monitoring(room_id))

        report = service.shutdown()

        assert report.worker_exited is True
        assert report.http.completed is True
        assert report.fully_closed is True
        assert db._conn is None, "工作线程退出后数据库必须关闭"
        assert service._worker.isRunning() is False

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT status, ended_at FROM monitor_sessions ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "正常退出应留下可核验的 Session 记录"
        # 关闭路径以取消结束监控任务，Session 必须闭环且不得残留 running。
        assert rows[-1][0] != "running"
        assert rows[-1][1] is not None
    finally:
        if db._conn is not None:
            db.close()
