"""录制状态通知中心：去重、合并与降级。

设计要点：
- 只做内存操作，不阻塞录制线程或 Qt 主线程。
- 同一房间同一类错误 60 秒内只提醒一次（状态栏每次仍更新）。
- 3 秒窗口内多个房间同时异常时合并为一条汇总，避免通知风暴。
- 重连中的暂时故障不产生“失败”气泡，只有真正恢复后才提示恢复。
- 托盘不可用时自动降级为状态栏/日志，不抛异常。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

# 同房间同类错误的去重窗口（秒）。
DEDUPE_WINDOW_S = 60.0
# 多房间异常合并窗口（毫秒）。
COALESCE_WINDOW_MS = 3000
# 达到该数量即合并为一条汇总通知。
COALESCE_THRESHOLD = 3
# 托盘正文截断长度，避免过长气泡。
_MESSAGE_LIMIT = 120

TrayProvider = Callable[[], object]
StatusWriter = Callable[[str], None]
RoomStateWriter = Callable[..., None]
NameProvider = Callable[[int], str]


class NotificationCenter(QObject):
    """把录制事件转换为低打扰的用户提示。"""

    def __init__(
        self,
        *,
        tray_provider: TrayProvider,
        status_writer: StatusWriter,
        room_state_writer: RoomStateWriter,
        name_provider: Optional[NameProvider] = None,
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._tray_provider = tray_provider
        self._status_writer = status_writer
        self._room_state_writer = room_state_writer
        self._name_provider = name_provider or (lambda room_id: str(room_id))
        self._clock = clock
        self._last_emit: dict[tuple[int, str], float] = {}
        self._recovered_announced: set[int] = set()
        self._pending: list[dict] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.flush)

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------
    def on_started(self, room_id: int, snapshot: dict) -> None:
        self._guard(self._on_started, room_id, snapshot)

    def on_reconnecting(self, room_id: int, snapshot: dict) -> None:
        self._guard(self._on_reconnecting, room_id, snapshot)

    def on_recovered(self, room_id: int, snapshot: dict) -> None:
        self._guard(self._on_recovered, room_id, snapshot)

    def on_finished(self, room_id: int, snapshot: dict) -> None:
        self._guard(self._on_finished, room_id, snapshot)

    def on_error(self, room_id: int, payload: dict) -> None:
        self._guard(self._on_error, room_id, payload)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _on_started(self, room_id: int, snapshot: dict) -> None:
        if not snapshot.get("resumed"):
            # 新会话开始：允许下一次恢复提示。
            self._recovered_announced.discard(room_id)
        self._set_room_state(room_id, "recording")

    def _on_reconnecting(self, room_id: int, snapshot: dict) -> None:
        # 等待续录期间没有打开的录制分段，房间行应显示“重连中”。
        self._set_room_state(
            room_id,
            "reconnecting",
            attempt=int(snapshot.get("attempt") or 1),
            max_attempts=int(snapshot.get("max_attempts") or 1),
        )

    def _on_recovered(self, room_id: int, snapshot: dict) -> None:
        self._set_room_state(room_id, "recording")
        if room_id in self._recovered_announced:
            return
        self._recovered_announced.add(room_id)
        attempt = int(snapshot.get("attempt") or 1)
        name = self._name(room_id)
        self._write_status(f"直播间 {name} 已恢复录制（第 {attempt} 次重连成功）")
        # 恢复提示不受 60 秒去重限制，立即发出。
        self._emit(
            "已恢复录制",
            f"「{name}」第 {attempt} 次重连成功，已写入新分段",
            severity="info",
        )

    def _on_finished(self, room_id: int, snapshot: dict) -> None:
        scope = snapshot.get("scope", "segment")
        status = snapshot.get("status", "")
        requested = bool(snapshot.get("requested"))
        will_retry = bool(snapshot.get("will_retry"))
        error = str(snapshot.get("error") or "")
        name = self._name(room_id)

        if scope == "session":
            self._recovered_announced.discard(room_id)
            if status == "failed":
                self._set_room_state(room_id, "failed")
                if not requested:
                    self._enqueue(
                        room_id,
                        "session",
                        "录制已停止",
                        f"「{name}」{error or '录制会话已结束'}",
                        "warning",
                    )
            else:
                self._set_room_state(room_id, "idle")
            return

        if will_retry:
            # 分段中断后紧接着会进入自动续录，交由 reconnecting 事件表达。
            return
        if status == "failed":
            self._set_room_state(room_id, "failed")
            if not requested:
                # 没有后续续录事件的分段失败：必须提示，避免漏报。
                self._enqueue(
                    room_id,
                    "segment",
                    "本段录制中断",
                    f"「{name}」{error or '本段录制失败'}，监控继续",
                    "warning",
                )
            return
        self._set_room_state(room_id, "idle")

    def _on_error(self, room_id: int, payload: dict) -> None:
        phase = str(payload.get("phase") or "runtime_failed")
        category = str(payload.get("category") or "other")
        retryable = bool(payload.get("retryable"))
        message = str(payload.get("message") or "")
        name = self._name(room_id)

        if category == "duplicate":
            # 重复开始不是故障：只更新状态栏，不改变房间行，也不发气泡。
            self._write_status(f"直播间 {name}：{message}")
            return
        if retryable:
            # 可自动重试的暂时性失败只更新状态栏与房间行，不发气泡。
            self._set_room_state(room_id, "idle")
            self._write_status(f"直播间 {name} 暂时无法录制：{message}")
            return
        self._set_room_state(room_id, "failed")
        if phase == "db_failed":
            title = "数据库更新失败"
        elif phase == "start_failed":
            title = "无法开始录制"
        else:
            title = "录制中断"
        self._enqueue(room_id, category, title, f"「{name}」{message}", "warning")

    # ------------------------------------------------------------------
    # 去重与合并
    # ------------------------------------------------------------------
    def _enqueue(
        self,
        room_id: int,
        category: str,
        title: str,
        message: str,
        severity: str,
    ) -> None:
        key = (room_id, category)
        now = self._clock()
        last = self._last_emit.get(key)
        if last is not None and now - last < DEDUPE_WINDOW_S:
            return
        self._last_emit[key] = now
        self._pending.append(
            {
                "room_id": room_id,
                "title": title,
                "message": message,
                "severity": severity,
            }
        )
        if not self._timer.isActive():
            self._timer.start(COALESCE_WINDOW_MS)

    def flush(self) -> None:
        """发送待处理通知；供 3 秒合并窗口到期或测试直接调用。"""
        self._timer.stop()
        pending, self._pending = self._pending, []
        if not pending:
            return
        if len(pending) >= COALESCE_THRESHOLD:
            rooms = {item["room_id"] for item in pending}
            self._write_status(
                f"{len(rooms)} 个直播间录制异常，详情见日志页"
            )
            self._emit(
                "多个直播间录制异常",
                f"{len(rooms)} 个直播间受影响，详情见日志页",
                severity="warning",
            )
            return
        for item in pending:
            self._emit(item["title"], item["message"], item["severity"])

    # ------------------------------------------------------------------
    # 输出适配
    # ------------------------------------------------------------------
    def _emit(self, title: str, message: str, severity: str) -> None:
        text = message[:_MESSAGE_LIMIT]
        tray = None
        try:
            tray = self._tray_provider()
        except Exception:  # noqa: BLE001 - 托盘异常不得影响录制
            logger.exception("读取托盘状态失败，通知降级为日志")
        if tray is not None:
            try:
                tray.show_message(title, text, severity)
                return
            except Exception:  # noqa: BLE001 - 气泡失败不得影响录制
                logger.exception("托盘通知发送失败，降级为日志")
        logger.info("通知（托盘不可用）：%s - %s", title, text)
        # 托盘不可用时降级到状态栏，保证用户仍能看到异常。
        self._write_status(f"{title}：{text}")

    def _write_status(self, text: str) -> None:
        try:
            self._status_writer(text)
        except Exception:  # noqa: BLE001 - 窗口已销毁时忽略
            logger.debug("状态栏更新失败", exc_info=True)

    def _set_room_state(self, room_id: int, state: str, **kwargs) -> None:
        try:
            self._room_state_writer(room_id, state, **kwargs)
        except Exception:  # noqa: BLE001 - 房间行更新失败不得影响录制
            logger.debug("房间状态更新失败 room_id=%s", room_id, exc_info=True)

    def _name(self, room_id: int) -> str:
        try:
            return self._name_provider(room_id) or str(room_id)
        except Exception:  # noqa: BLE001
            return str(room_id)

    def _guard(self, func, *args) -> None:
        try:
            func(*args)
        except Exception:  # noqa: BLE001 - 通知中心绝不向上抛异常
            logger.exception("通知中心处理事件失败")

    def dispose(self) -> None:
        """停止合并定时器并丢弃未发送通知（窗口销毁前调用）。"""
        self._timer.stop()
        self._pending.clear()
