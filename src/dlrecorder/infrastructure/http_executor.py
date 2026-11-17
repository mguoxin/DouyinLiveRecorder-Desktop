"""同步 HTTP 请求的并发执行与关闭管理（批次 C）。

设计要点：
- 并发上限由专用 ``ThreadPoolExecutor`` 的线程数保证，不使用许可账本。
- 在途请求（排队 + 执行中）总数受 ``max_workers + queue_capacity`` 限制，
  超出即拒绝，不进入线程池内部无界队列。
- 请求取消与启动在 ``RequestToken`` 的同一把锁内裁决：
  ``try_start()`` 成功即逻辑启动点；此后取消只允许请求自然结束。
- 关闭流程只做有界等待，结果由 :class:`HttpCloseReport` 如实上报。

已知限制（必须如实保留，不要用抽象层掩盖）：
- ``asyncio`` 取消协程不会终止已经进入同步函数的工作线程；本模块只能等待
  其自然返回，不承诺中断。
- 无法在不访问私有属性、不做无限等待的前提下证明执行器工作线程已经退出，
  因此 ``HttpCloseReport`` 不提供该断言。
- 循环线程是唯一执行数据库写入的线程，执行器线程不访问数据库。
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 4
DEFAULT_QUEUE_CAPACITY = 4
DEFAULT_CLOSE_GRACE_S = 5.0

_THREAD_NAME_PREFIX = "dlr-http"


class _Phase:
    """单次请求的执行阶段。"""

    QUEUED = "queued"        # 已在池队列中，等待工作线程取件
    DEQUEUED = "dequeued"    # 已被取件，启动判定尚未完成（报告中的“未分类”）
    RUNNING = "running"      # 已判定启动，同步函数正在执行


class RequestRejected(RuntimeError):
    """请求未被受理（关闭中、队列已满或线程池已关闭）。"""

    def __init__(self, reason: str, room_id: Optional[int]) -> None:
        super().__init__(f"请求未受理: {reason}")
        self.reason = reason
        self.room_id = room_id


class RequestCancelled(RuntimeError):
    """请求已受理但判定为不启动（停止或关闭先于启动）。"""


class RequestToken:
    """一次请求的启动裁决令牌。

    ``try_start()`` 与 ``cancel()`` 使用同一把锁，因此二者有全序：
    要么停止/关闭先到（请求不得调用同步函数），要么启动先到（只允许自然结束）。
    """

    __slots__ = ("_lock", "_closing", "_cancelled", "_started", "_soft")

    def __init__(self, closing: threading.Event) -> None:
        self._lock = threading.Lock()
        self._closing = closing
        self._cancelled = False
        self._started = False
        # 批次 C 只置位、不消费；批次 D 的读循环会读取它。
        self._soft = False

    def try_start(self) -> bool:
        with self._lock:
            if self._cancelled or self._closing.is_set():
                return False
            self._started = True
            return True

    def cancel(self) -> None:
        with self._lock:
            if self._started:
                self._soft = True
            else:
                self._cancelled = True

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started


class _CallRecord:
    """一次已受理请求的记录。仅由 ``HttpExecutor._lock`` 保护。"""

    __slots__ = ("key", "room_id", "task_id", "token", "phase", "started_at")

    def __init__(
        self,
        key: int,
        room_id: Optional[int],
        task_id: Optional[str],
        token: RequestToken,
    ) -> None:
        self.key = key
        self.room_id = room_id
        self.task_id = task_id
        self.token = token
        self.phase = _Phase.QUEUED
        self.started_at = 0.0


@dataclass(frozen=True)
class HttpCloseReport:
    """执行器关闭结果。

    ``completed=True`` 只表示：所有已提交的可调用对象都已返回，或已在启动前
    判定作废；不表示执行器线程已经退出。
    """

    completed: bool
    outstanding: int
    running: tuple[tuple[Optional[int], float], ...]
    unclassified: tuple[Optional[int], ...]
    queued: tuple[Optional[int], ...]
    completed_ok: int
    completed_error: int
    dropped_queued: int
    aborted_before_start: int
    rejected_at_admission: int
    waited_s: float
    grace_exhausted: bool


class HttpExecutor:
    """专用线程池 + 在途准入 + 关闭快照。"""

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
    ) -> None:
        if max_workers <= 0 or queue_capacity < 0:
            raise ValueError("max_workers 必须为正，queue_capacity 不能为负")
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=_THREAD_NAME_PREFIX,
        )
        self._max_workers = max_workers
        self._capacity = max_workers + queue_capacity
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._close_guard = threading.Lock()
        self._calls: dict[int, _CallRecord] = {}
        self._seq = itertools.count(1)
        self._outstanding = 0
        self._completed_ok = 0
        self._completed_error = 0
        self._dropped_queued = 0
        self._aborted_before_start = 0
        self._rejected = 0
        self._report: Optional[HttpCloseReport] = None

    # ---------- 请求 ----------
    def new_token(self) -> RequestToken:
        """为一次请求创建启动令牌。"""
        return RequestToken(self._closing)

    async def run(
        self,
        token: RequestToken,
        fn: Callable[[], Any],
        *,
        room_id: Optional[int] = None,
        task_id: Optional[str] = None,
    ) -> Any:
        """提交并等待一次同步调用。取消本协程不会终止已开始的同步函数。"""
        key = self._admit(room_id, task_id, token)
        loop = asyncio.get_running_loop()
        try:
            future = self._pool.submit(self._execute, key, token, fn)
        except RuntimeError:  # 线程池已关闭
            self._withdraw(key)
            raise RequestRejected("pool_closed", room_id) from None
        future.add_done_callback(functools.partial(self._finish, key))
        return await asyncio.wrap_future(future, loop=loop)

    def _admit(
        self,
        room_id: Optional[int],
        task_id: Optional[str],
        token: RequestToken,
    ) -> int:
        with self._lock:
            if self._closing.is_set():
                self._rejected += 1
                raise RequestRejected("closing", room_id)
            if self._outstanding >= self._capacity:
                self._rejected += 1
                raise RequestRejected("queue_full", room_id)
            key = next(self._seq)
            self._calls[key] = _CallRecord(key, room_id, task_id, token)
            self._outstanding += 1
            return key

    def _withdraw(self, key: int) -> None:
        """提交失败时撤销登记，避免计数泄漏。"""
        with self._lock:
            if self._calls.pop(key, None) is None:
                return
            self._outstanding -= 1
            self._rejected += 1
            if self._outstanding == 0:
                self._idle.notify_all()

    def _execute(self, key: int, token: RequestToken, fn: Callable[[], Any]) -> Any:
        """在池线程中执行；启动判定与阶段登记分离，避免长期持锁。"""
        with self._lock:
            record = self._calls.get(key)
            if record is not None:
                record.phase = _Phase.DEQUEUED
        if not token.try_start():
            raise RequestCancelled("已停止或正在关闭，未发起请求")
        with self._lock:
            record = self._calls.get(key)
            if record is not None:
                record.phase = _Phase.RUNNING
                record.started_at = time.monotonic()
        return fn()

    def _finish(self, key: int, future: Any) -> None:
        """Future 完成回调；``dict.pop`` 保证同一个请求只注销一次。"""
        with self._lock:
            record = self._calls.pop(key, None)
            if record is None:
                return
            self._outstanding -= 1
            if future.cancelled():
                self._dropped_queued += 1
            elif isinstance(future.exception(), RequestCancelled):
                self._aborted_before_start += 1
            elif future.exception() is not None:
                self._completed_error += 1
            else:
                self._completed_ok += 1
            if self._outstanding == 0:
                self._idle.notify_all()

    # ---------- 停止与关闭 ----------
    def cancel_token(self, token: Optional[RequestToken]) -> None:
        """取消尚未开始的请求；已开始的请求只标记软取消。"""
        if token is not None:
            token.cancel()

    def begin_close(self) -> None:
        """拒绝新请求，并取消全部已登记但尚未开始的请求。"""
        self._closing.set()
        with self._lock:
            tokens = [record.token for record in self._calls.values()]
        for token in tokens:
            token.cancel()

    def close(self, grace_s: float = DEFAULT_CLOSE_GRACE_S) -> HttpCloseReport:
        """串行化的有界关闭；重复调用返回首次报告。"""
        with self._close_guard:
            with self._lock:
                if self._report is not None:
                    return self._report
            started = time.monotonic()
            self.begin_close()
            # 关键：调用 shutdown 时不得持有 _lock，否则内联完成回调会自死锁。
            self._pool.shutdown(wait=False, cancel_futures=True)
            deadline = started + max(0.0, grace_s)
            with self._idle:
                while self._outstanding > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._idle.wait(left)
                report = self._build_report_locked(started)
                self._report = report
            return report

    def cached_report(self) -> Optional[HttpCloseReport]:
        return self._report

    def snapshot(self) -> HttpCloseReport:
        """非阻塞快照；用于关闭未完成时的真实状态上报。"""
        with self._lock:
            return self._build_report_locked(time.monotonic())

    def _build_report_locked(self, started: float) -> HttpCloseReport:
        now = time.monotonic()
        running: list[tuple[Optional[int], float]] = []
        unclassified: list[Optional[int]] = []
        queued: list[Optional[int]] = []
        for record in self._calls.values():
            if record.phase == _Phase.RUNNING:
                running.append((record.room_id, max(0.0, now - record.started_at)))
            elif record.phase == _Phase.DEQUEUED:
                unclassified.append(record.room_id)
            else:
                queued.append(record.room_id)
        return HttpCloseReport(
            completed=self._outstanding == 0,
            outstanding=self._outstanding,
            running=tuple(running),
            unclassified=tuple(unclassified),
            queued=tuple(queued),
            completed_ok=self._completed_ok,
            completed_error=self._completed_error,
            dropped_queued=self._dropped_queued,
            aborted_before_start=self._aborted_before_start,
            rejected_at_admission=self._rejected,
            waited_s=max(0.0, now - started),
            grace_exhausted=self._outstanding > 0,
        )
