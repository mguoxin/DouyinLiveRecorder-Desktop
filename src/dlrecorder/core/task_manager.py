"""统一异步任务管理：单个 QThread 运行一个 asyncio 事件循环。

设计要点：
- 所有监控任务共享同一个 asyncio 事件循环，运行在专用 QThread。
- 同步 HTTP（urllib）通过专用 HttpExecutor 执行，不阻塞事件循环。
- GUI 通过 Qt 信号接收状态更新（跨线程自动排队）。
- 任务具备 create / start / stop / shutdown 生命周期。
- 异常通过 task_error 信号上报，不崩溃事件循环。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from ..domain.models import Room
from ..domain.states import ParseResult, RoomState
from ..douyin.parser import DouyinParser
from ..infrastructure.http_executor import (
    DEFAULT_CLOSE_GRACE_S,
    HttpCloseReport,
    HttpExecutor,
    RequestCancelled,
    RequestRejected,
    RequestToken,
)

logger = logging.getLogger(__name__)


@dataclass
class _TaskRegistration:
    """事件循环独占的单次监控任务注册信息。"""

    task_id: str
    task: asyncio.Task
    stop_event: asyncio.Event
    session_id: Optional[int]
    # 当前在途请求的启动令牌；仅有事件循环线程可读写。
    active_token: Optional[RequestToken] = None


@dataclass(frozen=True)
class WorkerShutdownReport:
    """工作线程关闭结果。

    ``worker_exited`` 来自 ``QThread`` 的真实运行状态；``http`` 为执行器关闭报告
    或其最终快照。只有两者同时成立才算完全关闭，数据库关闭以 ``worker_exited``
    为前提。
    """

    worker_exited: bool
    http: HttpCloseReport

    @property
    def fully_closed(self) -> bool:
        return self.worker_exited and self.http.completed


def snapshot_state(state: RoomState) -> dict:
    """将 RoomState 序列化为可跨线程传递的快照字典。"""
    return {
        "last_confirmed_room_state": state.last_confirmed_room_state,
        "current_observation": state.current_observation,
        "last_parse_result": str(state.last_parse_result) if state.last_parse_result else None,
        "last_parse_success_at": state.last_parse_success_at,
        "last_confirmed_state_at": state.last_confirmed_state_at,
        "state_expired": state.state_expired,
        "last_stream_url": state.last_stream_url,
    }


class MonitorWorker(QThread):
    """运行 asyncio 事件循环的工作线程。"""

    # 信号：room_id, state_snapshot
    room_state_updated = Signal(int, dict)
    task_error = Signal(int, str)
    task_stopped = Signal(int)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        close_grace_s: float = DEFAULT_CLOSE_GRACE_S,
        shutdown_timeout_ms: int = 7000,
    ) -> None:
        super().__init__(parent)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 仅事件循环线程可读写任务注册表；停止事件随注册记录一起清理。
        self._tasks: dict[int, _TaskRegistration] = {}
        self._state_lock = threading.Lock()
        self._loop_ready = threading.Event()
        self._shutdown_event = threading.Event()
        # 执行器工作线程不访问数据库；数据库写入只发生在事件循环线程。
        self._http = HttpExecutor()
        self._close_grace_s = close_grace_s
        self._shutdown_timeout_ms = shutdown_timeout_ms

    def run(self) -> None:
        # 使用 SelectorEventLoop：其 call_soon_threadsafe 基于 self-pipe，
        # 在 Windows 上比默认 ProactorEventLoop 的唤醒更可靠。
        self._loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            # 若 shutdown 在循环启动前已被调用，立即停止
            if self._shutdown_event.is_set():
                self._loop.stop()
            self._loop.run_forever()
        finally:
            # 清理所有任务
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()
            self._loop = None
            self._loop_ready.clear()
            # 事件循环已停止，不会再提交新请求；有界等待在途请求自然结束。
            report = self._http.close(self._close_grace_s)
            if not report.completed:
                logger.critical(
                    "HTTP 执行器关闭超时：outstanding=%s running=%s",
                    report.outstanding,
                    report.running,
                )

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._loop_ready.wait(timeout)

    # ---------- 任务生命周期 ----------
    def start_monitor(
        self,
        room: Room,
        parser: DouyinParser,
        interval: float,
        session_starter: Optional[Callable[[int], int]] = None,
        session_ender: Optional[Callable[[int, str, Optional[str]], None]] = None,
    ) -> None:
        """从 GUI 线程调用，在事件循环中启动监控并绑定 Session。"""
        if not self._loop_ready.wait(timeout=5.0):
            raise RuntimeError("事件循环未启动")
        asyncio.run_coroutine_threadsafe(
            self._start_monitor(
                room, parser, interval, session_starter, session_ender
            ),
            self._loop,
        )

    async def _start_monitor(
        self,
        room: Room,
        parser: DouyinParser,
        interval: float,
        session_starter: Optional[Callable[[int], int]],
        session_ender: Optional[Callable[[int, str, Optional[str]], None]],
    ) -> None:
        if room.id is None:
            return
        current = self._tasks.get(room.id)
        if (
            current is not None
            and not current.task.done()
            and not current.stop_event.is_set()
        ):
            return  # 同一房间已有未请求停止的任务

        session_id: Optional[int] = None
        if session_starter is not None:
            try:
                # 在事件循环线程同步创建，注册前不让出执行权，避免丢失紧随的停止请求。
                session_id = session_starter(room.id)
            except Exception as e:  # noqa: BLE001
                logger.exception("创建监控 Session 失败 room_id=%s", room.id)
                self.task_error.emit(room.id, str(e))
                return

        task_id = uuid.uuid4().hex
        stop_evt = asyncio.Event()
        task = self._loop.create_task(
            self._monitor_loop(
                room,
                parser,
                interval,
                task_id,
                stop_evt,
                session_id,
                session_ender,
            )
        )
        # 已请求停止的旧任务允许被替换；其完成回调会核对 task_id。
        self._tasks[room.id] = _TaskRegistration(
            task_id=task_id,
            task=task,
            stop_event=stop_evt,
            session_id=session_id,
        )
        task.add_done_callback(
            lambda done, rid=room.id, tid=task_id: self._on_task_done(rid, tid, done)
        )

    async def _monitor_loop(
        self,
        room: Room,
        parser: DouyinParser,
        interval: float,
        task_id: str,
        stop_evt: asyncio.Event,
        session_id: Optional[int],
        session_ender: Optional[Callable[[int, str, Optional[str]], None]],
    ) -> None:
        session_status = "stopped"
        session_error: Optional[str] = None
        # 队列已满属于瞬时过载，只向用户提示一次；后续自动重试不再重复上报。
        queue_full_notified = False
        try:
            while not stop_evt.is_set():
                # 每次请求独立令牌：停止/关闭只裁决本次请求，不影响其他任务。
                token = self._http.new_token()
                current = self._tasks.get(room.id)
                if current is not None and current.task_id == task_id:
                    current.active_token = token
                try:
                    detail = await self._http.run(
                        token,
                        lambda: parser.fetch_and_parse_detail(room.web_rid),
                        room_id=room.id,
                        task_id=task_id,
                    )
                except asyncio.CancelledError:
                    raise
                except RequestCancelled:
                    # 停止或关闭先于启动：请求未发出，无需上报错误。
                    break
                except RequestRejected as e:
                    if e.reason == "queue_full":
                        if not queue_full_notified:
                            queue_full_notified = True
                            logger.warning(
                                "监控请求排队已满，将自动重试 room_id=%s: %s",
                                room.id,
                                e,
                            )
                            self.task_error.emit(room.id, str(e))
                        else:
                            logger.debug(
                                "监控请求仍排队已满，继续重试 room_id=%s", room.id
                            )
                    else:
                        logger.warning("监控请求未被受理 room_id=%s: %s", room.id, e)
                        self.task_error.emit(room.id, str(e))
                except Exception as e:  # noqa: BLE001 - 捕获所有异常避免事件循环崩溃
                    logger.exception("监控任务异常 room_id=%s", room.id)
                    self.task_error.emit(room.id, str(e))
                else:
                    # 跨任务身份检查：被替换的旧任务不得用旧结果覆盖新任务状态。
                    current = self._tasks.get(room.id)
                    if current is None or current.task_id != task_id:
                        break
                    with self._state_lock:
                        room.state.update(detail.result, detail.status)
                        if detail.result == ParseResult.SUCCESS:
                            # 成功解析时同步流地址；失败时保留最后一次已知地址。
                            room.state.last_stream_url = detail.stream_url
                        snap = snapshot_state(room.state)
                    self.room_state_updated.emit(room.id, snap)
                finally:
                    current = self._tasks.get(room.id)
                    if current is not None and current.task_id == task_id:
                        current.active_token = None
                # 可中断的等待
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            session_status = "interrupted"
            session_error = "监控任务被取消"
            raise
        except Exception as e:  # noqa: BLE001
            session_status = "error"
            session_error = str(e)
            logger.exception("监控任务异常退出 room_id=%s", room.id)
            self.task_error.emit(room.id, str(e))
        finally:
            if session_id is not None and session_ender is not None:
                try:
                    # 每个任务只结束自己绑定的 Session；数据库层保证幂等。
                    session_ender(session_id, session_status, session_error)
                except Exception as e:  # noqa: BLE001
                    logger.exception("结束监控 Session 失败 session_id=%s", session_id)
                    self.task_error.emit(room.id, f"结束监控 Session 失败: {e}")
            current = self._tasks.get(room.id)
            if current is not None and current.task_id == task_id:
                # 被新任务替换的旧任务不再发送房间级停止状态。
                self.task_stopped.emit(room.id)

    def _on_task_done(
        self, room_id: int, task_id: str, task: asyncio.Task
    ) -> None:
        current = self._tasks.get(room_id)
        if current is not None and current.task_id == task_id:
            self._tasks.pop(room_id, None)
        if not task.cancelled():
            # 读取异常以避免未消费异常告警；异常已在监控循环中上报。
            task.exception()

    def stop_monitor(self, room_id: int) -> None:
        if not self._loop_ready.is_set():
            return
        # 不在 GUI 线程预判运行状态，确保快速启动后的停止请求进入事件循环。
        asyncio.run_coroutine_threadsafe(self._stop_monitor(room_id), self._loop)

    async def _stop_monitor(self, room_id: int) -> None:
        current = self._tasks.get(room_id)
        if current is not None and not current.task.done():
            current.stop_event.set()
            # 仅取消尚未开始的在途请求；已开始的请求自然结束后循环退出判断。
            self._http.cancel_token(current.active_token)

    def is_running(self, room_id: int) -> bool:
        if (
            not self._loop_ready.is_set()
            or self._loop is None
            or self._shutdown_event.is_set()
        ):
            # 关闭已开始：事件循环正在收尾，不再发起查询，避免 UI 线程等待超时。
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._is_running(room_id), self._loop
        )
        try:
            return future.result(timeout=5.0)
        except TimeoutError:
            # 循环无响应或正在退出：按“未运行”处理，避免异常冒泡到 Qt 槽。
            logger.warning(
                "监控线程 5s 内未响应状态查询，按未运行处理 room_id=%s", room_id
            )
            return False
        except Exception:  # noqa: BLE001 - 循环已关闭等情况
            logger.debug("查询监控状态失败 room_id=%s", room_id, exc_info=True)
            return False

    def current_session_id(self, room_id: int) -> Optional[int]:
        """返回房间当前监控任务绑定的 Session ID（只读快照，线程安全）。

        任务可能刚刚结束，但 Session 记录仍然存在，因此直接返回已绑定的 ID。
        """
        registration = self._tasks.get(room_id)
        return registration.session_id if registration is not None else None

    async def _is_running(self, room_id: int) -> bool:
        current = self._tasks.get(room_id)
        return bool(
            current is not None
            and not current.task.done()
            and not current.stop_event.is_set()
        )

    def shutdown(self, timeout_ms: Optional[int] = None) -> WorkerShutdownReport:
        """停止事件循环并等待线程结束，返回可核验的关闭报告。

        ``timeout_ms`` 只约束等待 QThread 退出的时长；执行器自身的关闭等待由
        ``close_grace_s`` 独立约束。
        """
        self._shutdown_event.set()
        # 等待循环就绪后发送停止信号；若循环尚未启动，run() 会检测到 shutdown_event
        if (
            self.isRunning()
            and self._loop_ready.wait(timeout=5.0)
            and self._loop is not None
        ):
            self._loop.call_soon_threadsafe(self._loop.stop)
        timeout = self._shutdown_timeout_ms if timeout_ms is None else timeout_ms
        self.wait(timeout)
        worker_exited = not self.isRunning()
        http_report = self._http.cached_report()
        if http_report is None:
            # 线程未退出时 close() 不会执行，用真实快照代替，不伪装成已完成。
            http_report = self._http.snapshot()
        return WorkerShutdownReport(worker_exited=worker_exited, http=http_report)
