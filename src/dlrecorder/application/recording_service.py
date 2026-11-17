"""录制服务：FFmpeg 进程、录制状态机与录制历史持久化。

线程模型：
- 所有 FFmpeg 启动/停止/轮询都在专用管理线程中执行，避免阻塞 GUI。
- 外部（GUI）只投递命令，并通过 Qt 信号接收结果；信号跨线程自动排队。
- 数据库写入只发生在管理线程；``Database`` 内部有锁且允许跨线程连接。

生命周期：
- ``start_recording`` 立即返回，实际启动结果通过信号通知。
- ``stop_recording`` 有界停止：q → terminate → kill，均如实记录。
- ``shutdown`` 停止全部录制并等待管理线程退出；未退出时如实报告。
- 程序启动时把遗留 ``recording`` 记录标记为 ``interrupted``，保留文件与路径。
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..domain.recording import RecordingStatus
from ..infrastructure.db import Database
from ..infrastructure.disk import free_space_mb
from ..infrastructure.ffmpeg import (
    FFmpegProcess,
    FFmpegStopReport,
    build_record_command,
    file_size,
)

logger = logging.getLogger(__name__)

CommandBuilder = Callable[[str, str, Path], list[str]]
FreeSpaceProvider = Callable[[Path], Optional[float]]

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_STALL_TIMEOUT_S = 120.0
DEFAULT_STOP_GRACE_S = 5.0
DEFAULT_SHUTDOWN_TIMEOUT_S = 30.0

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class RecordingShutdownReport:
    """录制服务关闭结果。"""

    thread_exited: bool
    remaining_active: int


@dataclass
class _ActiveRecording:
    room_id: int
    recording_id: int
    file_path: Path
    process: FFmpegProcess
    last_size: int = 0
    last_growth_at: float = 0.0


def _safe_component(text: str, limit: int = 40) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("_", text or "").strip(" .")
    return (cleaned or "room")[:limit]


class RecordingService(QObject):
    """录制任务管理服务。"""

    # room_id, snapshot
    recording_started = Signal(int, dict)
    recording_finished = Signal(int, dict)
    recording_error = Signal(int, str)

    def __init__(
        self,
        db: Database,
        *,
        output_dir: Path,
        ffmpeg_path: str = "ffmpeg",
        min_free_mb: float = 0.0,
        free_space_provider: FreeSpaceProvider = free_space_mb,
        session_id_provider: Optional[Callable[[int], Optional[int]]] = None,
        command_builder: CommandBuilder = build_record_command,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
        stop_grace_s: float = DEFAULT_STOP_GRACE_S,
        shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
        recover_interrupted: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._output_dir = Path(output_dir)
        self._ffmpeg_path = ffmpeg_path
        self._min_free_mb = max(0.0, float(min_free_mb))
        self._free_space_provider = free_space_provider
        self._session_id_provider = session_id_provider
        self._command_builder = command_builder
        self._poll_interval_s = max(0.01, poll_interval_s)
        self._stall_timeout_s = max(0.0, stall_timeout_s)
        self._stop_grace_s = max(0.0, stop_grace_s)
        self._shutdown_timeout_s = max(0.1, shutdown_timeout_s)

        self._output_error = ""
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.critical("录制输出目录不可用: %s", e)
            self._output_error = str(e)

        self._queue: queue.Queue = queue.Queue()
        self._active: dict[int, _ActiveRecording] = {}
        self._active_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_report: Optional[RecordingShutdownReport] = None
        if recover_interrupted:
            recovered = self.recover_interrupted_recordings()
            if recovered:
                logger.info("已将 %s 条遗留录制记录标记为 interrupted", recovered)

        self._thread = threading.Thread(
            target=self._run,
            name="dlr-recording",
            daemon=True,
        )
        self._thread.start()

    # ---------- 外部接口 ----------
    def start_recording(self, room_id: int, stream_url: Optional[str]) -> None:
        """请求开始录制；结果通过信号返回，不阻塞调用线程。"""
        if self._shutdown_report is not None:
            self._emit_error(room_id, "录制服务已关闭，无法开始录制")
            return
        self._queue.put(("start", room_id, stream_url))

    def stop_recording(self, room_id: int) -> None:
        """请求停止录制；结果通过信号返回。"""
        self._queue.put(("stop", room_id))

    def is_recording(self, room_id: int) -> bool:
        with self._active_lock:
            return room_id in self._active

    def active_rooms(self) -> list[int]:
        with self._active_lock:
            return sorted(self._active)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def min_free_mb(self) -> float:
        return self._min_free_mb

    def set_ffmpeg_path(self, path: str) -> None:
        """修改 FFmpeg 路径；仅影响之后启动的录制。"""
        text = (path or "").strip()
        if not text:
            raise ValueError("FFmpeg 路径不能为空")
        self._ffmpeg_path = text
        logger.info("FFmpeg 路径已更新: %s", text)

    def set_output_dir(self, path: Path) -> None:
        """修改录制目录；仅影响之后启动的录制，不移动已有文件。"""
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        self._output_dir = target
        self._output_error = ""
        logger.info("录制目录已更新: %s", target)

    def set_min_free_mb(self, value: float) -> None:
        self._min_free_mb = max(0.0, float(value))
        logger.info("磁盘最低剩余空间已更新: %.0f MB", self._min_free_mb)

    def _disk_below_threshold(self) -> Optional[float]:
        """返回低于阈值的剩余空间；阈值关闭或无法读取时返回 None。"""
        if self._min_free_mb <= 0:
            return None
        free = self._free_space_provider(self._output_dir)
        if free is None or free >= self._min_free_mb:
            return None
        return free

    def list_history(
        self,
        room_id: Optional[int] = None,
        limit: int = 200,
    ) -> list[dict]:
        """返回录制历史（最近优先），供界面只读展示。"""
        return self._db.list_recordings(room_id=room_id, limit=limit)

    def recover_interrupted_recordings(self) -> int:
        """把遗留 recording 记录标记为 interrupted，并按磁盘实际大小回填。"""
        rows = self._db.list_recordings(
            status=RecordingStatus.RECORDING.value, limit=10000
        )
        recovered = 0
        for row in rows:
            path = row.get("file_path")
            size = file_size(Path(path)) if path else 0
            try:
                if self._db.interrupt_recording(int(row["id"]), size):
                    recovered += 1
            except Exception:  # noqa: BLE001 - 单条恢复失败不应阻断启动
                logger.exception("恢复遗留录制记录失败 id=%s", row.get("id"))
        return recovered

    def shutdown(self) -> RecordingShutdownReport:
        """停止全部录制并等待管理线程退出（幂等）。"""
        with self._shutdown_lock:
            if self._shutdown_report is not None:
                return self._shutdown_report
            self._queue.put(("shutdown",))
            self._thread.join(self._shutdown_timeout_s)
            exited = not self._thread.is_alive()
            with self._active_lock:
                remaining = len(self._active)
            if not exited:
                logger.critical(
                    "录制管理线程未在 %.1fs 内退出，remaining_active=%s",
                    self._shutdown_timeout_s,
                    remaining,
                )
            self._shutdown_report = RecordingShutdownReport(
                thread_exited=exited, remaining_active=remaining
            )
            return self._shutdown_report

    # ---------- 管理线程 ----------
    def _run(self) -> None:
        try:
            while True:
                try:
                    command = self._queue.get(timeout=self._poll_interval_s)
                except queue.Empty:
                    self._poll_active()
                    continue
                kind = command[0]
                try:
                    if kind == "start":
                        self._handle_start(command[1], command[2])
                    elif kind == "stop":
                        self._handle_stop(command[1])
                    elif kind == "poll":
                        self._poll_active()
                    elif kind == "shutdown":
                        self._stop_all()
                        break
                except Exception as e:  # noqa: BLE001 - 单条命令失败不终止管理线程
                    logger.exception("录制命令执行失败: %s", kind)
                    room_id = command[1] if len(command) > 1 else 0
                    self.recording_error.emit(int(room_id), str(e))
        finally:
            # 兜底：线程退出前不得留下仍在写的 FFmpeg 进程。
            self._stop_all()

    def _handle_start(self, room_id: int, stream_url: Optional[str]) -> None:
        if self._output_error:
            self._emit_error(room_id, f"录制输出目录不可用: {self._output_error}")
            return
        free = self._disk_below_threshold()
        if free is not None:
            self._emit_error(
                room_id,
                f"磁盘剩余空间不足：可用 {free:.0f} MB，"
                f"最低要求 {self._min_free_mb:.0f} MB，已阻止开始录制",
            )
            return
        with self._active_lock:
            if room_id in self._active:
                duplicate = True
            else:
                duplicate = False
        if duplicate:
            self._emit_error(room_id, "该房间已在录制中")
            return
        if not stream_url:
            self._emit_error(room_id, "尚未解析到直播流地址，无法开始录制")
            return

        room = self._db.get_room(room_id)
        if room is None:
            self._emit_error(room_id, f"房间不存在: {room_id}")
            return

        output_path = self._allocate_output_path(room.web_rid, room.display_name)
        session_id: Optional[int] = None
        if self._session_id_provider is not None:
            try:
                session_id = self._session_id_provider(room_id)
            except Exception:  # noqa: BLE001 - 关联失败不应阻止录制
                logger.warning(
                    "获取监控 Session 失败 room_id=%s", room_id, exc_info=True
                )
        try:
            recording_id = self._db.start_recording(
                room_id, str(output_path), session_id=session_id
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("创建录制记录失败 room_id=%s", room_id)
            self._emit_error(room_id, f"创建录制记录失败: {e}")
            return

        command = self._command_builder(
            self._ffmpeg_path, stream_url, output_path
        )
        process = FFmpegProcess(command, stop_grace_s=self._stop_grace_s)
        try:
            process.start()
        except OSError as e:
            logger.error("FFmpeg 启动失败 room_id=%s: %s", room_id, e)
            self._db.finish_recording(
                recording_id, RecordingStatus.FAILED.value, 0
            )
            self._emit_error(room_id, f"FFmpeg 启动失败: {e}")
            return

        active = _ActiveRecording(
            room_id=room_id,
            recording_id=recording_id,
            file_path=output_path,
            process=process,
            last_size=0,
            last_growth_at=time.monotonic(),
        )
        with self._active_lock:
            self._active[room_id] = active
        logger.info(
            "开始录制 room_id=%s recording_id=%s file=%s",
            room_id,
            recording_id,
            output_path,
        )
        self.recording_started.emit(
            room_id,
            {
                "recording_id": recording_id,
                "room_id": room_id,
                "file_path": str(output_path),
                "status": RecordingStatus.RECORDING.value,
                "file_size": 0,
                "error": "",
            },
        )

    @staticmethod
    def _unconfirmed_exit_error(report: FFmpegStopReport) -> str:
        """停止后仍无法确认进程退出时，不得把录制标记为成功。"""
        return "" if report.exited else "FFmpeg 未能确认退出，进程可能仍在写入"

    def _handle_stop(self, room_id: int) -> None:
        with self._active_lock:
            active = self._active.get(room_id)
        if active is None:
            logger.debug("停止录制请求被忽略：房间未在录制 room_id=%s", room_id)
            return
        report = active.process.stop()
        self._finalize(
            active,
            exit_code=report.exit_code,
            requested=True,
            error=self._unconfirmed_exit_error(report),
        )

    def _poll_active(self) -> None:
        with self._active_lock:
            actives = list(self._active.values())
        now = time.monotonic()
        low_space = self._disk_below_threshold()
        for active in actives:
            exit_code = active.process.poll()
            if exit_code is not None:
                self._finalize(active, exit_code=exit_code, requested=False)
                continue
            if low_space is not None:
                logger.error(
                    "磁盘剩余空间不足（可用 %.0f MB），停止录制 room_id=%s",
                    low_space,
                    active.room_id,
                )
                report = active.process.stop()
                self._finalize(
                    active,
                    exit_code=report.exit_code,
                    requested=True,
                    error=(
                        f"磁盘剩余空间不足（可用 {low_space:.0f} MB）"
                        + (
                            ""
                            if report.exited
                            else "；FFmpeg 未能确认退出，进程可能仍在写入"
                        )
                    ),
                )
                continue
            size = file_size(active.file_path)
            if size > active.last_size:
                active.last_size = size
                active.last_growth_at = now
            elif (
                self._stall_timeout_s > 0
                and now - active.last_growth_at >= self._stall_timeout_s
            ):
                logger.error(
                    "录制输出长时间未增长，停止录制 room_id=%s file=%s",
                    active.room_id,
                    active.file_path,
                )
                report = active.process.stop()
                self._finalize(
                    active,
                    exit_code=report.exit_code,
                    requested=True,
                    error=(
                        "输出文件长时间未增长"
                        + (
                            ""
                            if report.exited
                            else "；FFmpeg 未能确认退出，进程可能仍在写入"
                        )
                    ),
                )

    def _finalize(
        self,
        active: _ActiveRecording,
        *,
        exit_code: Optional[int],
        requested: bool,
        error: str = "",
    ) -> None:
        with self._active_lock:
            # 只有仍登记为同一对象的录制才允许结束，避免重复落库。
            if self._active.get(active.room_id) is not active:
                return
            self._active.pop(active.room_id, None)

        size = file_size(active.file_path)
        status = RecordingStatus.COMPLETED
        if error:
            status = RecordingStatus.FAILED
        elif exit_code not in (0, None):
            status = RecordingStatus.FAILED
            error = f"FFmpeg 退出码 {exit_code}"
        elif size <= 0:
            status = RecordingStatus.FAILED
            error = "输出文件为空"

        if status == RecordingStatus.FAILED:
            logger.error(
                "录制失败 room_id=%s recording_id=%s file=%s exit=%s reason=%s",
                active.room_id,
                active.recording_id,
                active.file_path,
                exit_code,
                error or "未知原因",
            )
        else:
            logger.info(
                "录制完成 room_id=%s recording_id=%s file=%s size=%s requested=%s",
                active.room_id,
                active.recording_id,
                active.file_path,
                size,
                requested,
            )

        try:
            updated = self._db.finish_recording(
                active.recording_id, status.value, size
            )
            if not updated:
                logger.warning(
                    "录制记录已不是 recording，跳过更新 recording_id=%s",
                    active.recording_id,
                )
        except Exception as e:  # noqa: BLE001 - 数据库失败不得吞掉信号
            logger.exception("更新录制记录失败 recording_id=%s", active.recording_id)
            self._emit_error(active.room_id, f"更新录制记录失败: {e}")

        active.process.close_streams()
        self.recording_finished.emit(
            active.room_id,
            {
                "recording_id": active.recording_id,
                "room_id": active.room_id,
                "file_path": str(active.file_path),
                "status": status.value,
                "file_size": size,
                "error": error,
            },
        )

    def _stop_all(self) -> None:
        with self._active_lock:
            actives = list(self._active.values())
        for active in actives:
            report = active.process.stop()
            self._finalize(
                active,
                exit_code=report.exit_code,
                requested=True,
                error=self._unconfirmed_exit_error(report),
            )

    def _allocate_output_path(self, web_rid: str, display_name: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:6]
        name = (
            f"{_safe_component(display_name or web_rid)}_"
            f"{_safe_component(web_rid)}_{stamp}_{unique}.ts"
        )
        return self._output_dir / name

    def _emit_error(self, room_id: int, message: str) -> None:
        logger.warning("录制错误 room_id=%s: %s", room_id, message)
        self.recording_error.emit(room_id, message)
