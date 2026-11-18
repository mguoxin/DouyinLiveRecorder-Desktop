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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

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
from ..infrastructure.orphan import cleanup_orphans
from ..infrastructure.process_registry import ProcessRecord, ProcessRegistry
from ..infrastructure.winproc import KillOnCloseJob, query_process

logger = logging.getLogger(__name__)

CommandBuilder = Callable[[str, str, Path], list[str]]
FreeSpaceProvider = Callable[[Path], Optional[float]]

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_STALL_TIMEOUT_S = 120.0
DEFAULT_STOP_GRACE_S = 5.0
DEFAULT_SHUTDOWN_TIMEOUT_S = 30.0
DEFAULT_RECONNECT_MAX_ATTEMPTS = 5
DEFAULT_RECONNECT_DELAYS_S: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 30.0)

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class RecordingShutdownReport:
    """录制服务关闭结果。"""

    thread_exited: bool
    remaining_active: int


@dataclass(frozen=True)
class StreamRefresh:
    """一次重新解析的结果，供录制中断后的自动续录使用。

    ``live``：True=确认直播中；False=确认已结束；None=无法确认（网络失败等）。
    """

    live: Optional[bool] = None
    stream_url: Optional[str] = None
    error: str = ""


@dataclass
class _ActiveRecording:
    room_id: int
    recording_id: int
    file_path: Path
    process: Optional[FFmpegProcess]
    web_rid: str = ""
    display_name: str = ""
    last_size: int = 0
    last_growth_at: float = 0.0
    # 已发起的自动续录次数；等待重试期间 process 为 None。
    attempts: int = 0
    next_retry_at: Optional[float] = None
    # 已确认可用的新流地址（退出码 0 但直播仍在时提前解析得到），下一轮直接使用。
    pending_stream_url: Optional[str] = None


def _safe_component(text: str, limit: int = 40) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("_", text or "").strip(" .")
    return (cleaned or "room")[:limit]


class RecordingService(QObject):
    """录制任务管理服务。"""

    # room_id, snapshot
    recording_started = Signal(int, dict)
    recording_finished = Signal(int, dict)
    recording_error = Signal(int, str)
    # room_id, snapshot（attempt/max_attempts/delay_s/exit_code/error）
    recording_reconnecting = Signal(int, dict)

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
        stream_resolver: Optional[Callable[[str], StreamRefresh]] = None,
        reconnect_max_attempts: int = DEFAULT_RECONNECT_MAX_ATTEMPTS,
        reconnect_delays_s: Sequence[float] = DEFAULT_RECONNECT_DELAYS_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
        stop_grace_s: float = DEFAULT_STOP_GRACE_S,
        shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
        recover_interrupted: bool = True,
        process_registry: Optional[ProcessRegistry] = None,
        cleanup_orphans_on_start: bool = True,
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
        self._stream_resolver = stream_resolver
        self._reconnect_max_attempts = max(0, int(reconnect_max_attempts))
        self._reconnect_delays_s = tuple(
            max(0.0, float(delay)) for delay in reconnect_delays_s
        ) or (0.0,)
        self._poll_interval_s = max(0.01, poll_interval_s)
        self._stall_timeout_s = max(0.0, stall_timeout_s)
        self._stop_grace_s = max(0.0, stop_grace_s)
        self._shutdown_timeout_s = max(0.1, shutdown_timeout_s)
        self._registry = process_registry
        # Job Object 让 FFmpeg 在应用被强制结束时随进程句柄关闭而终止；
        # 进程登记表是第二道防线，两者都不改变正常录制流程。
        self._job = KillOnCloseJob() if process_registry is not None else None

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
        if self._registry is not None and cleanup_orphans_on_start:
            self._cleanup_orphans()
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

    def _cleanup_orphans(self) -> None:
        """启动时清理上次异常退出遗留的 FFmpeg 进程。"""
        try:
            report = cleanup_orphans(self._registry)
        except Exception:  # noqa: BLE001 - 清理失败不应阻断启动
            logger.exception("检查遗留 FFmpeg 进程失败，已跳过清理")
            return
        if report.checked == 0:
            return
        logger.warning(
            "遗留 FFmpeg 检查: 登记=%s 已终止=%s 跳过=%s 未终止=%s",
            report.checked,
            len(report.terminated),
            report.skipped,
            len(report.failed),
        )
        for record in report.terminated:
            size = file_size(Path(record.file_path)) if record.file_path else 0
            try:
                self._db.interrupt_recording(record.recording_id, size)
            except Exception:  # noqa: BLE001 - 单条记录失败不影响其他记录
                logger.exception(
                    "标记遗留录制记录失败 recording_id=%s", record.recording_id
                )

    def _register_process(self, active: "_ActiveRecording") -> None:
        """把刚启动的 FFmpeg 写入进程登记表。"""
        if self._registry is None or active.process is None:
            return
        pid = active.process.pid
        if not pid:
            return
        identity = query_process(pid)
        record = ProcessRecord(
            pid=pid,
            create_time=identity.create_time if identity is not None else 0,
            exe_path=identity.exe_path if identity is not None else self._ffmpeg_path,
            recording_id=active.recording_id,
            room_id=active.room_id,
            file_path=str(active.file_path),
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        try:
            self._registry.add(record)
        except Exception:  # noqa: BLE001 - 登记失败不影响录制
            logger.exception("写入 FFmpeg 进程登记表失败 pid=%s", pid)

    def _unregister_process(self, recording_id: int) -> None:
        if self._registry is None:
            return
        try:
            self._registry.remove(recording_id=recording_id)
        except Exception:  # noqa: BLE001
            logger.exception("移除 FFmpeg 进程登记失败 recording_id=%s", recording_id)

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
            # 关闭 Job 句柄：极端情况下仍有子进程存活时，系统会一并终止。
            if self._job is not None:
                self._job.close()
            return self._shutdown_report

    # ---------- 管理线程 ----------
    def _run(self) -> None:
        try:
            while True:
                try:
                    command = self._queue.get(timeout=self._poll_interval_s)
                except queue.Empty:
                    try:
                        self._poll_active()
                    except Exception:  # noqa: BLE001 - 轮询异常不得终止管理线程
                        logger.exception("录制轮询失败")
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
        process = FFmpegProcess(
            command, stop_grace_s=self._stop_grace_s, job=self._job
        )
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
            web_rid=room.web_rid,
            display_name=room.display_name,
            last_size=0,
            last_growth_at=time.monotonic(),
        )
        with self._active_lock:
            self._active[room_id] = active
        self._register_process(active)
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
        if active.process is None:
            # 正在等待自动续录：没有打开的录制分段，直接结束会话。
            self._end_without_segment(
                active, "已手动停止（等待自动续录期间）", failed=True
            )
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
            if active.process is None:
                self._poll_reconnect(active, now)
                continue
            exit_code = active.process.poll()
            if exit_code is not None:
                self._handle_exit(active, exit_code)
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

    # ---------- 自动续录 ----------
    def _can_retry(self, active: _ActiveRecording, exit_code: Optional[int]) -> bool:
        """仅对非零异常退出且仍有重试预算的分段尝试续录。

        退出码 0 视为直播自然结束，不续录，避免把已结束的直播反复拉起。
        """
        return (
            self._stream_resolver is not None
            and self._reconnect_max_attempts > 0
            and exit_code not in (0, None)
            and active.attempts < self._reconnect_max_attempts
        )

    def _retry_delay(self, attempt: int) -> float:
        index = min(max(1, attempt), len(self._reconnect_delays_s)) - 1
        return self._reconnect_delays_s[index]

    def _handle_exit(self, active: _ActiveRecording, exit_code: int) -> None:
        if self._can_retry(active, exit_code):
            self._begin_reconnect(active, exit_code)
            return
        if exit_code == 0 and self._confirmed_live_after_clean_exit(active):
            self._begin_reconnect(active, exit_code)
            return
        error = ""
        if (
            self._stream_resolver is not None
            and self._reconnect_max_attempts > 0
            and exit_code != 0
            and active.attempts > 0
        ):
            error = (
                f"FFmpeg 退出码 {exit_code}，"
                f"自动续录失败（已尝试 {active.attempts} 次）"
            )
        self._finalize(active, exit_code=exit_code, requested=False, error=error)

    def _confirmed_live_after_clean_exit(self, active: _ActiveRecording) -> bool:
        """退出码 0 也可能是网络 EOF 造成的提前结束，需确认直播是否仍在进行。

        仅在重新解析明确返回"直播中且有地址"时按中断续录；真实下播、解析失败或
        无法确认时保持原行为（按完成收尾），避免直播结束后反复重试。
        """
        if (
            self._stream_resolver is None
            or self._reconnect_max_attempts <= 0
            or active.attempts >= self._reconnect_max_attempts
        ):
            return False
        refresh = self._resolve_stream(active)
        if refresh.live is True and refresh.stream_url:
            logger.warning(
                "FFmpeg 正常退出但直播仍在进行，按网络中断续录 room_id=%s",
                active.room_id,
            )
            active.pending_stream_url = refresh.stream_url
            return True
        return False

    def _begin_reconnect(self, active: _ActiveRecording, exit_code: int) -> None:
        """结束当前分段并安排在退避后重新解析、续录。"""
        logger.warning(
            "录制异常退出，准备自动续录 room_id=%s exit=%s attempts=%s/%s",
            active.room_id,
            exit_code,
            active.attempts + 1,
            self._reconnect_max_attempts,
        )
        self._close_segment(
            active,
            exit_code=exit_code,
            requested=False,
            error=f"FFmpeg 退出码 {exit_code}，正在自动续录",
        )
        active.process = None
        active.attempts += 1
        delay = self._retry_delay(active.attempts)
        active.next_retry_at = time.monotonic() + delay
        self.recording_reconnecting.emit(
            active.room_id,
            {
                "room_id": active.room_id,
                "attempt": active.attempts,
                "max_attempts": self._reconnect_max_attempts,
                "delay_s": delay,
                "exit_code": exit_code,
                "error": f"FFmpeg 退出码 {exit_code}",
            },
        )

    def _poll_reconnect(self, active: _ActiveRecording, now: float) -> None:
        if active.next_retry_at is None or now < active.next_retry_at:
            return
        active.next_retry_at = None

        room = self._db.get_room(active.room_id)
        if room is None:
            self._end_without_segment(active, "房间已删除，停止自动续录", failed=True)
            return
        low_space = self._disk_below_threshold()
        if low_space is not None:
            self._end_without_segment(
                active,
                f"磁盘剩余空间不足（可用 {low_space:.0f} MB），停止自动续录",
                failed=True,
            )
            return

        if active.pending_stream_url:
            # 退出码 0 的提前结束已确认直播仍在，直接使用刚解析到的地址。
            stream_url = active.pending_stream_url
            active.pending_stream_url = None
            self._start_segment(active, room, stream_url)
            return

        refresh = self._resolve_stream(active)
        if refresh.live is False:
            logger.info(
                "重新解析确认直播已结束，停止自动续录 room_id=%s", active.room_id
            )
            self._end_without_segment(
                active, "直播已结束，未继续录制", failed=False
            )
            return
        if refresh.live is not True or not refresh.stream_url:
            self._after_failed_attempt(
                active, refresh.error or "重新解析未获得可用直播流地址"
            )
            return
        self._start_segment(active, room, refresh.stream_url)

    def _resolve_stream(self, active: _ActiveRecording) -> StreamRefresh:
        assert self._stream_resolver is not None
        try:
            return self._stream_resolver(active.web_rid)
        except Exception as e:  # noqa: BLE001 - 解析异常按一次失败尝试处理
            logger.exception("自动续录重新解析失败 room_id=%s", active.room_id)
            return StreamRefresh(live=None, error=str(e))

    def _start_segment(self, active: _ActiveRecording, room, stream_url: str) -> None:
        """为续录创建新的输出文件与录制记录，并启动 FFmpeg。"""
        output_path = self._allocate_output_path(room.web_rid, room.display_name)
        session_id: Optional[int] = None
        if self._session_id_provider is not None:
            try:
                session_id = self._session_id_provider(active.room_id)
            except Exception:  # noqa: BLE001 - 关联失败不应阻止续录
                logger.warning(
                    "获取监控 Session 失败 room_id=%s", active.room_id, exc_info=True
                )
        try:
            recording_id = self._db.start_recording(
                active.room_id, str(output_path), session_id=session_id
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("创建续录记录失败 room_id=%s", active.room_id)
            self._after_failed_attempt(active, f"创建录制记录失败: {e}")
            return

        try:
            command = self._command_builder(self._ffmpeg_path, stream_url, output_path)
            process = FFmpegProcess(
                command, stop_grace_s=self._stop_grace_s, job=self._job
            )
            process.start()
        except (OSError, ValueError) as e:
            logger.error("续录 FFmpeg 启动失败 room_id=%s: %s", active.room_id, e)
            try:
                self._db.finish_recording(
                    recording_id, RecordingStatus.FAILED.value, 0
                )
            except Exception:  # noqa: BLE001
                logger.exception("更新续录失败记录失败 recording_id=%s", recording_id)
            self._after_failed_attempt(active, f"FFmpeg 启动失败: {e}")
            return

        active.recording_id = recording_id
        active.file_path = output_path
        active.process = process
        active.web_rid = room.web_rid
        active.display_name = room.display_name
        active.last_size = 0
        active.last_growth_at = time.monotonic()
        self._register_process(active)
        logger.info(
            "自动续录已启动 room_id=%s attempt=%s recording_id=%s file=%s",
            active.room_id,
            active.attempts,
            recording_id,
            output_path,
        )
        self.recording_started.emit(
            active.room_id,
            {
                "recording_id": recording_id,
                "room_id": active.room_id,
                "file_path": str(output_path),
                "status": RecordingStatus.RECORDING.value,
                "file_size": 0,
                "error": "",
                "resumed": True,
                "attempt": active.attempts,
            },
        )

    def _after_failed_attempt(self, active: _ActiveRecording, reason: str) -> None:
        if active.attempts >= self._reconnect_max_attempts:
            logger.error(
                "自动续录失败，停止录制 room_id=%s attempts=%s reason=%s",
                active.room_id,
                active.attempts,
                reason,
            )
            self._end_without_segment(
                active,
                f"自动续录失败：{reason}（已尝试 {active.attempts} 次）",
                failed=True,
            )
            return
        active.attempts += 1
        delay = self._retry_delay(active.attempts)
        active.next_retry_at = time.monotonic() + delay
        logger.warning(
            "自动续录尝试失败，将重试 room_id=%s attempts=%s/%s reason=%s",
            active.room_id,
            active.attempts,
            self._reconnect_max_attempts,
            reason,
        )
        self.recording_reconnecting.emit(
            active.room_id,
            {
                "room_id": active.room_id,
                "attempt": active.attempts,
                "max_attempts": self._reconnect_max_attempts,
                "delay_s": delay,
                "exit_code": None,
                "error": reason,
            },
        )

    def _end_without_segment(
        self, active: _ActiveRecording, message: str, *, failed: bool
    ) -> None:
        """结束没有打开分段的录制会话（重试等待中放弃或手动停止）。"""
        if not self._drop_active(active):
            return
        status = RecordingStatus.FAILED if failed else RecordingStatus.COMPLETED
        if failed:
            logger.error("录制会话结束 room_id=%s: %s", active.room_id, message)
            self.recording_error.emit(active.room_id, message)
        else:
            logger.info("录制会话结束 room_id=%s: %s", active.room_id, message)
        self.recording_finished.emit(
            active.room_id,
            {
                "recording_id": None,
                "room_id": active.room_id,
                "file_path": str(active.file_path),
                "status": status.value,
                "file_size": 0,
                "error": message if failed else "",
            },
        )

    def _drop_active(self, active: _ActiveRecording) -> bool:
        with self._active_lock:
            if self._active.get(active.room_id) is not active:
                return False
            self._active.pop(active.room_id, None)
            return True

    def _close_segment(
        self,
        active: _ActiveRecording,
        *,
        exit_code: Optional[int],
        requested: bool,
        error: str = "",
    ) -> None:
        """结束一个录制分段：更新数据库并发出 recording_finished。"""
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

        self._unregister_process(active.recording_id)
        if active.process is not None:
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

    def _finalize(
        self,
        active: _ActiveRecording,
        *,
        exit_code: Optional[int],
        requested: bool,
        error: str = "",
    ) -> None:
        if not self._drop_active(active):
            return
        self._close_segment(
            active,
            exit_code=exit_code,
            requested=requested,
            error=error,
        )

    def _stop_all(self) -> None:
        with self._active_lock:
            actives = list(self._active.values())
        for active in actives:
            if active.process is None:
                # 关闭时取消等待中的续录，不伪造新的录制分段。
                self._drop_active(active)
                continue
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
