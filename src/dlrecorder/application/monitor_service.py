"""监控服务：GUI 与核心业务之间的协调层。

职责：
- 持有 rooms 集合、数据库、解析器、异步工作线程。
- 提供房间增删查与监控启停接口。
- 将核心层信号转发为应用层信号，供 GUI 连接。
- 程序退出时清理所有任务。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..core.task_manager import MonitorWorker, WorkerShutdownReport, snapshot_state
from ..domain.models import Room, validate_display_name, validate_web_rid
from ..domain.states import ParseResult
from ..douyin.parser import DouyinParser
from ..infrastructure.db import Database

logger = logging.getLogger(__name__)


class MonitorService(QObject):
    """监控业务协调服务。"""

    # 应用层信号
    room_added = Signal(int, str, str)        # room_id, web_rid, display_name
    room_updated = Signal(int, str, str)      # room_id, web_rid, display_name
    room_removed = Signal(int)                # room_id
    room_state_updated = Signal(int, dict)    # room_id, state_snapshot
    task_error = Signal(int, str)             # room_id, error_msg
    task_stopped = Signal(int)                # room_id

    def __init__(
        self,
        db: Database,
        parser: DouyinParser,
        monitor_interval_s: float = 120.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._parser = parser
        self._interval = monitor_interval_s
        self._rooms: dict[int, Room] = {}
        self._worker = MonitorWorker(self)
        # 连接核心信号转发
        self._worker.room_state_updated.connect(self.room_state_updated)
        self._worker.task_error.connect(self.task_error)
        self._worker.task_stopped.connect(self.task_stopped)
        self._worker.start()

    def load_rooms(self) -> None:
        """从数据库加载房间列表。"""
        for room in self._db.list_rooms():
            self._rooms[room.id] = room

    def get_rooms(self) -> list[Room]:
        return list(self._rooms.values())

    @property
    def monitor_interval_s(self) -> float:
        return self._interval

    def set_monitor_interval(self, interval_s: float) -> None:
        """修改监控间隔；仅对之后启动的监控任务生效。"""
        value = float(interval_s)
        if value <= 0:
            raise ValueError("监控间隔必须大于 0")
        self._interval = value
        logger.info("监控间隔已更新: %s 秒（对后续启动的监控生效）", value)

    def get_room(self, room_id: int) -> Optional[Room]:
        return self._rooms.get(room_id)

    def add_room(self, web_rid: str, display_name: str = "") -> Optional[int]:
        """添加房间并持久化。返回 room_id，失败返回 None。"""
        try:
            rid = validate_web_rid(web_rid)
            name = validate_display_name(display_name)
        except ValueError as e:
            logger.warning("添加房间参数非法: %s", e)
            return None
        try:
            room_id = self._db.add_room(rid, name)
        except sqlite3.IntegrityError:
            logger.warning("添加房间失败：直播间 ID 已存在 web_rid=%s", rid)
            return None
        except Exception as e:  # noqa: BLE001
            logger.exception("添加房间失败")
            return None
        room = Room(id=room_id, web_rid=rid, display_name=name)
        self._rooms[room_id] = room
        self.room_added.emit(room_id, rid, name)
        return room_id

    def update_room(
        self,
        room_id: int,
        *,
        web_rid: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> tuple[bool, str]:
        """更新房间信息，返回 (是否成功, 错误信息)。

        监控中的房间不允许修改 web_rid；显示名称可随时修改。
        """
        room = self._rooms.get(room_id)
        if room is None:
            return False, "房间不存在"
        try:
            new_rid = validate_web_rid(web_rid) if web_rid is not None else room.web_rid
            new_name = (
                validate_display_name(display_name)
                if display_name is not None
                else room.display_name
            )
        except ValueError as e:
            return False, str(e)

        rid_changed = new_rid != room.web_rid
        if rid_changed and self.is_monitoring(room_id):
            return False, "监控中不能修改直播间 ID，请先停止监控"

        try:
            self._db.update_room(
                room_id,
                web_rid=new_rid if rid_changed else None,
                display_name=new_name if new_name != room.display_name else None,
            )
        except sqlite3.IntegrityError:
            return False, "直播间 ID 已存在"
        except KeyError:
            return False, "房间不存在"
        except Exception as e:  # noqa: BLE001
            logger.exception("更新房间失败 room_id=%s", room_id)
            return False, str(e)

        room.web_rid = new_rid
        room.display_name = new_name
        self.room_updated.emit(room_id, new_rid, new_name)
        return True, ""

    def get_stream_url(self, room_id: int) -> Optional[str]:
        """返回房间最近一次成功解析到的直播流地址（仅内存）。"""
        room = self._rooms.get(room_id)
        return room.state.last_stream_url if room is not None else None

    def remove_room(self, room_id: int) -> None:
        """停止监控并删除房间。"""
        self.stop_monitor(room_id)
        self._db.delete_room(room_id)
        self._rooms.pop(room_id, None)
        self.room_removed.emit(room_id)

    def start_monitor(self, room_id: int) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            return
        # Session 由事件循环在线程内创建并绑定到唯一任务，避免快速启停产生孤立记录。
        self._worker.start_monitor(
            room,
            self._parser,
            self._interval,
            session_starter=self._db.start_session,
            session_ender=self._db.end_session,
        )

    def stop_monitor(self, room_id: int) -> None:
        # 停止请求必须始终进入事件循环，不能因任务尚未注册而被丢弃。
        self._worker.stop_monitor(room_id)

    def is_monitoring(self, room_id: int) -> bool:
        return self._worker.is_running(room_id)

    def get_session_id(self, room_id: int) -> Optional[int]:
        """返回房间当前（或最近）监控 Session ID，供录制记录关联。"""
        return self._worker.current_session_id(room_id)

    def shutdown(self) -> WorkerShutdownReport:
        """停止所有任务并关闭工作线程；仅在确认工作线程退出后关闭数据库。

        后台线程仍存活时关闭连接会让其写入失效，因此这种情况下保留连接并输出
        critical 日志，由进程终止回收。
        """
        for room_id in list(self._rooms.keys()):
            self._worker.stop_monitor(room_id)
        report = self._worker.shutdown()
        if report.worker_exited:
            self._db.close()
        else:
            logger.critical(
                "工作线程未在超时内退出，保留数据库连接："
                "http_completed=%s outstanding=%s running=%s grace_exhausted=%s",
                report.http.completed,
                report.http.outstanding,
                report.http.running,
                report.http.grace_exhausted,
            )
        return report
