"""HttpExecutor 单元测试（批次 C · T1–T10）。

全部使用标准库与 Mock，不发起真实网络请求。
"""
import asyncio
import threading
import time

import pytest

from dlrecorder.infrastructure.http_executor import (
    HttpExecutor,
    RequestCancelled,
    RequestRejected,
)


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _async_in_thread(coro):
    """在独立线程中运行协程，返回 (thread, box)。"""
    box: dict = {}

    def runner() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - 测试需保留原始异常类型
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, box


def test_t1_normal_completion():
    executor = HttpExecutor()
    try:
        result = asyncio.run(
            executor.run(executor.new_token(), lambda: "ok", room_id=1, task_id="t1")
        )
        assert result == "ok"
        report = executor.close(grace_s=1.0)
        assert report.completed is True
        assert report.outstanding == 0
        assert report.completed_ok == 1
        assert report.completed_error == 0
        assert report.aborted_before_start == 0
        assert report.rejected_at_admission == 0
        assert report.grace_exhausted is False
    finally:
        executor.close(grace_s=0)


def test_t2_exception_propagates_to_caller():
    executor = HttpExecutor()

    def boom():
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(executor.run(executor.new_token(), boom, room_id=2))
        report = executor.close(grace_s=1.0)
        assert report.completed_error == 1
        assert report.completed_ok == 0
    finally:
        executor.close(grace_s=0)


def test_t3_completion_callback_is_idempotent():
    """每个请求只被回调注销一次；重复注销不得改变计数。"""
    executor = HttpExecutor()
    total = 12
    try:
        for _ in range(total):
            assert asyncio.run(executor.run(executor.new_token(), lambda: None)) is None
        report = executor.close(grace_s=1.0)
        assert report.completed_ok == total
        assert report.outstanding == 0

        # 对已注销的 key 再次回调：必须是 no-op，不得二次递减计数。
        executor._finish(10**9, object())
        assert executor.snapshot().completed_ok == total
        assert executor.snapshot().outstanding == 0
        assert executor.close(grace_s=1.0) is report
    finally:
        executor.close(grace_s=0)


def test_t4_queue_full_is_rejected():
    executor = HttpExecutor(max_workers=1, queue_capacity=1)
    gate = threading.Event()
    executed: list[str] = []

    def blocking(tag: str) -> str:
        executed.append(tag)
        assert gate.wait(timeout=10)
        return tag

    first_thread, first_box = _async_in_thread(
        executor.run(executor.new_token(), lambda: blocking("first"), room_id=11)
    )
    try:
        assert _wait(lambda: executor.snapshot().outstanding == 1)
        second_thread, second_box = _async_in_thread(
            executor.run(executor.new_token(), lambda: blocking("second"), room_id=12)
        )
        assert _wait(lambda: executor.snapshot().outstanding == 2)

        with pytest.raises(RequestRejected) as excinfo:
            asyncio.run(executor.run(executor.new_token(), lambda: "third", room_id=13))
        assert excinfo.value.reason == "queue_full"
        assert "third" not in executed

        gate.set()
        first_thread.join(timeout=10)
        second_thread.join(timeout=10)
        assert first_box.get("result") == "first"
        assert second_box.get("result") == "second"

        report = executor.close(grace_s=1.0)
        assert report.completed is True
        assert report.completed_ok == 2
        assert report.rejected_at_admission == 1
    finally:
        gate.set()
        executor.close(grace_s=0)


def test_t5_cancel_while_queued_never_invokes_function():
    executor = HttpExecutor(max_workers=1, queue_capacity=1)
    gate = threading.Event()
    calls: list[str] = []

    def first_fn() -> str:
        calls.append("first")
        assert gate.wait(timeout=10)
        return "first"

    def second_fn() -> str:
        calls.append("second")
        return "second"

    first_thread, first_box = _async_in_thread(
        executor.run(executor.new_token(), first_fn, room_id=21)
    )
    token = executor.new_token()
    second_thread = None
    second_box: dict = {}
    try:
        assert _wait(lambda: executor.snapshot().outstanding == 1)
        second_thread, second_box = _async_in_thread(
            executor.run(token, second_fn, room_id=22)
        )
        assert _wait(lambda: executor.snapshot().outstanding == 2)
        assert _wait(lambda: len(executor.snapshot().queued) == 1)
        token.cancel()
        gate.set()

        first_thread.join(timeout=10)
        second_thread.join(timeout=10)
        assert first_box.get("result") == "first"
        assert isinstance(second_box.get("error"), RequestCancelled)
        assert calls == ["first"], "已取消的排队请求不得调用同步函数"

        report = executor.close(grace_s=1.0)
        assert report.aborted_before_start == 1
        assert report.completed_ok == 1
    finally:
        gate.set()
        executor.close(grace_s=0)


def test_t6_close_before_start_aborts_queued_request():
    executor = HttpExecutor(max_workers=1, queue_capacity=1)
    gate = threading.Event()
    calls: list[str] = []

    def first_fn() -> str:
        calls.append("first")
        assert gate.wait(timeout=10)
        return "first"

    def second_fn() -> str:
        calls.append("second")
        return "second"

    first_thread, _ = _async_in_thread(
        executor.run(executor.new_token(), first_fn, room_id=31)
    )
    second_thread = None
    second_box: dict = {}
    try:
        assert _wait(lambda: executor.snapshot().outstanding == 1)
        second_thread, second_box = _async_in_thread(
            executor.run(executor.new_token(), second_fn, room_id=32)
        )
        assert _wait(lambda: executor.snapshot().outstanding == 2)
        executor.begin_close()
        gate.set()
        first_thread.join(timeout=10)
        second_thread.join(timeout=10)

        assert isinstance(second_box.get("error"), RequestCancelled)
        assert calls == ["first"], "关闭先于启动时不得调用同步函数"

        with pytest.raises(RequestRejected) as excinfo:
            asyncio.run(executor.run(executor.new_token(), lambda: "late"))
        assert excinfo.value.reason == "closing"

        report = executor.close(grace_s=1.0)
        assert report.aborted_before_start == 1
        assert report.rejected_at_admission >= 1
    finally:
        gate.set()
        executor.close(grace_s=0)


def test_t7_start_before_close_allows_natural_completion():
    executor = HttpExecutor(max_workers=1)
    gate = threading.Event()

    def slow() -> str:
        assert gate.wait(timeout=10)
        return "done"

    thread, box = _async_in_thread(
        executor.run(executor.new_token(), slow, room_id=41)
    )
    try:
        assert _wait(lambda: bool(executor.snapshot().running))
        executor.begin_close()
        gate.set()
        thread.join(timeout=10)

        assert box.get("result") == "done", "已启动的请求应自然结束，不被关闭中断"
        report = executor.close(grace_s=1.0)
        assert report.completed_ok == 1
        assert report.aborted_before_start == 0
    finally:
        gate.set()
        executor.close(grace_s=0)


def test_t8_dequeued_phase_is_reported_as_unclassified():
    """工作线程已取件但启动判定未完成时，如实归入 unclassified。"""
    executor = HttpExecutor(max_workers=1)
    token = executor.new_token()
    token._lock.acquire()
    thread, box = None, None
    try:
        thread, box = _async_in_thread(
            executor.run(token, lambda: "ok", room_id=51)
        )
        assert _wait(lambda: executor.snapshot().unclassified == (51,))
        snapshot = executor.snapshot()
        assert snapshot.running == ()
        assert snapshot.queued == ()
        assert snapshot.completed is False
    finally:
        token._lock.release()

    thread.join(timeout=10)
    assert box.get("result") == "ok"
    report = executor.close(grace_s=1.0)
    assert report.completed_ok == 1
    assert report.unclassified == ()
    executor.close(grace_s=0)


def test_t9_close_is_serialized_and_returns_same_report():
    executor = HttpExecutor(max_workers=1)
    gate = threading.Event()

    def slow() -> str:
        assert gate.wait(timeout=10)
        return "done"

    thread, _ = _async_in_thread(
        executor.run(executor.new_token(), slow, room_id=61)
    )
    assert _wait(lambda: bool(executor.snapshot().running))

    reports: list = []
    reports_lock = threading.Lock()

    def closer() -> None:
        report = executor.close(grace_s=2.0)
        with reports_lock:
            reports.append(report)

    closers = [threading.Thread(target=closer) for _ in range(2)]
    for closer_thread in closers:
        closer_thread.start()
    assert _wait(lambda: executor._closing.is_set(), timeout=2.0)
    assert executor.cached_report() is None, "关闭必须等待运行中的请求"
    gate.set()
    for closer_thread in closers:
        closer_thread.join(timeout=10)
    thread.join(timeout=10)

    assert len(reports) == 2
    assert reports[0] is reports[1], "并发 close 必须返回同一份缓存报告"
    assert reports[0].completed is True
    executor.close(grace_s=0)


def test_t10_close_timeout_reports_running_request():
    executor = HttpExecutor(max_workers=1)
    gate = threading.Event()
    thread, box = _async_in_thread(
        executor.run(executor.new_token(), lambda: gate.wait(10) and "late", room_id=71)
    )
    assert _wait(lambda: bool(executor.snapshot().running))

    started = time.monotonic()
    report = executor.close(grace_s=0.2)
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 2.0, "close 必须有界，不得无限等待"
        assert report.completed is False
        assert report.grace_exhausted is True
        assert report.outstanding == 1
        assert report.running and report.running[0][0] == 71
        assert report.waited_s >= 0.2
    finally:
        gate.set()
        thread.join(timeout=10)

    assert box.get("result") == "late"
    # 关闭已缓存，不重复构建；快照反映真实状态，最终自然完成。
    assert executor.cached_report() is report
    assert _wait(lambda: executor.snapshot().completed is True)
