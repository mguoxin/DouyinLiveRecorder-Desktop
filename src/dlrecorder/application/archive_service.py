"""录制文件归档服务（P0-01）。

单工作线程 + FIFO 队列，负责把已结束录制的文件复制到归档目录并校验。
不变式：
- 只处理数据库中已不是 ``recording`` 状态的记录，绝不移动正在写入的文件。
- 目标文件先写 ``.part``，大小与 SHA-256 校验通过后原子改名。
- 只有目标已验证且用户显式开启 ``delete_source`` 时才删除源文件。
- 任何失败都保留源文件，并把原因写入 ``recordings``/``archive_jobs``。
- 数据库状态不是唯一依据：每次执行前重新检查磁盘。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..domain.archive_rules import (
    ArchiveRuleError,
    ArchiveSettings,
    build_relative_path,
    ensure_within_root,
)
from ..infrastructure.db import Database
from ..infrastructure.disk import free_space_mb
from ..infrastructure.safe_fileops import (
    SafeFileOpsError,
    copy_file_verified,
    resolve_conflict_path,
    sha256_file,
)

logger = logging.getLogger(__name__)

FreeSpaceProvider = Callable[[Path], Optional[float]]
SettingsProvider = Callable[[], ArchiveSettings]

DEFAULT_MAX_ATTEMPTS = 3
_PENDING_STATES = ("pending", "copying", "verifying")
_STARTED_AT = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
)
_MAX_ERROR_LEN = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _template_values(row: dict, recording_id: int) -> dict[str, object]:
    anchor = (row.get("display_name_snapshot") or "").strip()
    web_rid = (row.get("web_rid_snapshot") or "").strip()
    if not anchor:
        anchor = web_rid or "未命名"
    started = str(row.get("started_at") or "")
    match = _STARTED_AT.match(started)
    if match:
        year, month, day, hour, minute, second = match.groups()
        date_text = f"{year}-{month}-{day}"
        time_text = f"{hour}{minute}{second}"
    else:
        date_text = datetime.now().strftime("%Y-%m-%d")
        time_text = datetime.now().strftime("%H%M%S")
    return {
        "anchor": anchor,
        "web_rid": web_rid,
        "date": date_text,
        "time": time_text,
        "session_id": row.get("session_id") or "",
        "recording_id": recording_id,
        "title": anchor,
    }


class ArchiveService(QObject):
    """归档队列执行器；UI 通过信号刷新状态。"""

    # recording_id, {status, archive_path, error, file_size}
    archive_updated = Signal(int, dict)

    def __init__(
        self,
        db: Database,
        *,
        settings_provider: SettingsProvider,
        free_space_provider: FreeSpaceProvider = free_space_mb,
        on_event: Optional[Callable[[int, dict], None]] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._settings_provider = settings_provider
        self._free_space_provider = free_space_provider
        self._on_event = on_event
        self._max_attempts = max(1, int(max_attempts))

        self._condition = threading.Condition()
        self._queue: deque[int] = deque()
        self._queued: set[int] = set()
        self._active: set[int] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="dlr-archive", daemon=True
        )
        self._thread.start()

    # ---------- 外部接口 ----------
    def enqueue(self, recording_id: int) -> bool:
        """加入归档队列；重复入队或正在处理时返回 False。"""
        if self._stop.is_set():
            return False
        with self._condition:
            if recording_id in self._queued or recording_id in self._active:
                return False
            self._queue.append(recording_id)
            self._queued.add(recording_id)
            self._condition.notify_all()
        return True

    def recover_pending(self) -> int:
        """启动恢复：重新入队未完成的归档状态与任务。"""
        candidates: set[int] = {
            int(row["id"])
            for row in self._db.list_recordings_by_archive_status(list(_PENDING_STATES))
        }
        for job in self._db.list_archive_jobs(list(_PENDING_STATES)):
            candidates.add(int(job["recording_id"]))
        count = 0
        for recording_id in sorted(candidates):
            if self.enqueue(recording_id):
                count += 1
        if count:
            logger.info("归档恢复：重新入队 %s 条未完成任务", count)
        return count

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        """等待队列清空；返回是否在超时前空闲。"""
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._queue and not self._active,
                timeout=max(0.0, timeout),
            )

    def shutdown(self, timeout: float = 10.0) -> bool:
        """停止接收新任务，等待当前任务结束；返回线程是否退出。"""
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=max(0.0, timeout))
        exited = not self._thread.is_alive()
        if not exited:
            logger.critical("归档线程未在 %.1fs 内退出", timeout)
        return exited

    # ---------- 工作线程 ----------
    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stop.is_set():
                    self._condition.wait(timeout=0.5)
                if self._stop.is_set() and not self._queue:
                    return
                recording_id = self._queue.popleft()
                self._queued.discard(recording_id)
                self._active.add(recording_id)
            try:
                self._process(recording_id)
            except Exception:  # noqa: BLE001 - 单任务失败不得终止工作线程
                logger.exception("归档任务异常 recording_id=%s", recording_id)
            finally:
                with self._condition:
                    self._active.discard(recording_id)
                    self._condition.notify_all()

    def _settings(self) -> Optional[ArchiveSettings]:
        try:
            settings = self._settings_provider()
        except Exception:  # noqa: BLE001 - 配置读取失败只影响本轮
            logger.exception("读取归档设置失败")
            return None
        return settings

    def _process(self, recording_id: int) -> None:
        row = self._db.get_recording(recording_id)
        if row is None:
            logger.info("归档跳过：录制记录不存在 recording_id=%s", recording_id)
            return

        settings = self._settings()
        if settings is None:
            return

        if not settings.enabled:
            if row["archive_status"] in _PENDING_STATES:
                self._db.set_recording_archive(
                    recording_id,
                    "none",
                    error=None,
                    expected_status=row["archive_status"],
                )
            return

        if row["status"] == "recording":
            logger.info("归档跳过：录制仍在进行 recording_id=%s", recording_id)
            return

        if row["archive_status"] == "archived":
            archived_path = row.get("archive_path")
            if archived_path and Path(archived_path).is_file():
                logger.info("归档跳过：记录已完成归档 recording_id=%s", recording_id)
                return

        source = Path(row["file_path"]) if row["file_path"] else None
        if source is None or not source.is_file():
            self._fail(recording_id, row, "源文件不存在，无法归档")
            return
        try:
            file_size = source.stat().st_size
        except OSError as exc:
            self._fail(recording_id, row, f"读取源文件失败: {exc}")
            return
        if file_size <= 0:
            self._fail(recording_id, row, "源文件为空，已跳过归档")
            return

        root_dir = settings.root_dir
        if root_dir is None:
            self._fail(recording_id, row, "未配置归档目录")
            return

        values = _template_values(row, recording_id)
        try:
            relative, unknown = build_relative_path(
                settings, values, source.suffix.lower() or ".ts"
            )
            target = ensure_within_root(root_dir, Path(root_dir) / relative)
        except ArchiveRuleError as exc:
            self._fail(recording_id, row, str(exc))
            return
        if unknown:
            logger.warning(
                "归档模板包含未知占位符 %s recording_id=%s",
                ",".join(unknown),
                recording_id,
            )

        if target == source.resolve() or target == source:
            self._mark_skipped(recording_id, target, file_size, "归档路径与源文件相同")
            return

        try:
            available_mb = self._free_space_provider(target.parent)
        except Exception:  # noqa: BLE001 - 无法读取空间时继续，由复制错误兜底
            logger.warning("读取归档目标剩余空间失败", exc_info=True)
            available_mb = None
        if available_mb is not None:
            required_mb = file_size * 1.05 / (1024 * 1024)
            if available_mb < required_mb:
                self._fail(
                    recording_id,
                    row,
                    f"目标磁盘剩余空间不足：需要约 {required_mb:.1f} MB，"
                    f"可用 {available_mb:.1f} MB",
                )
                return

        try:
            source_sha = sha256_file(source)
            target, duplicate = resolve_conflict_path(target, source_sha)
        except (OSError, SafeFileOpsError) as exc:
            self._fail(recording_id, row, f"检查目标文件失败: {exc}")
            return

        if duplicate:
            self._mark_skipped(
                recording_id, target, file_size, "目标已存在相同内容文件，未重复复制"
            )
            return

        job_id = self._begin_job(recording_id, row, source)
        if job_id is None:
            return
        self._db.update_archive_job(
            job_id,
            "copying",
            target_path=str(target),
            bytes_total=file_size,
            increment_attempts=True,
        )
        self._db.set_recording_archive(
            recording_id,
            "copying",
            expected_status=row["archive_status"],
            increment_attempts=True,
        )
        row["archive_status"] = "copying"
        try:
            copied_sha = copy_file_verified(source, target, expected_sha256=source_sha)
        except (OSError, SafeFileOpsError) as exc:
            self._db.update_archive_job(job_id, "failed", error=str(exc)[:_MAX_ERROR_LEN])
            self._fail(recording_id, row, f"复制失败: {exc}")
            return

        self._db.update_archive_job(job_id, "verifying", sha256=copied_sha)
        try:
            target_size = target.stat().st_size
        except OSError as exc:
            self._db.update_archive_job(job_id, "failed", error=str(exc)[:_MAX_ERROR_LEN])
            self._fail(recording_id, row, f"校验目标文件失败: {exc}")
            return
        if target_size != file_size:
            message = f"目标文件大小不一致: {target_size} != {file_size}"
            self._db.update_archive_job(job_id, "failed", error=message)
            self._fail(recording_id, row, message)
            return

        archived_at = _now_iso()
        self._db.update_archive_job(
            job_id, "archived", sha256=copied_sha, bytes_total=target_size
        )
        self._db.set_recording_archive(
            recording_id,
            "archived",
            archive_path=str(target),
            archived_at=archived_at,
            error=None,
        )

        if settings.delete_source:
            self._delete_source(recording_id, source, target)
        self._emit(recording_id, "archived", str(target), "", file_size)

    # ---------- 内部辅助 ----------
    def _begin_job(self, recording_id: int, row: dict, source: Path) -> Optional[int]:
        latest = self._db.get_latest_archive_job(recording_id)
        if latest is not None and latest["status"] in _PENDING_STATES:
            if int(latest["attempts"] or 0) >= self._max_attempts:
                self._fail(
                    recording_id,
                    row,
                    f"归档失败次数已达上限（{self._max_attempts} 次）",
                    job_id=int(latest["id"]),
                )
                return None
            return int(latest["id"])
        return self._db.create_archive_job(recording_id, str(source))

    def _delete_source(self, recording_id: int, source: Path, target: Path) -> None:
        """仅在目标已验证后删除源文件；失败只记录，不改变已归档状态。"""
        try:
            if source.resolve() == target.resolve():
                return
            os.remove(source)
            logger.info("归档后已删除源文件 recording_id=%s", recording_id)
        except OSError:
            logger.warning(
                "归档完成但删除源文件失败 recording_id=%s", recording_id, exc_info=True
            )

    def _mark_skipped(
        self,
        recording_id: int,
        target: Path,
        file_size: int,
        reason: str,
    ) -> None:
        self._db.set_recording_archive(
            recording_id,
            "skipped",
            archive_path=str(target),
            error=reason[:_MAX_ERROR_LEN],
        )
        latest = self._db.get_latest_archive_job(recording_id)
        if latest is not None and latest["status"] in _PENDING_STATES:
            self._db.update_archive_job(
                int(latest["id"]), "skipped", target_path=str(target),
                bytes_total=file_size, error=reason[:_MAX_ERROR_LEN],
            )
        else:
            job_id = self._db.create_archive_job(
                recording_id, str(target), status="skipped"
            )
            self._db.update_archive_job(
                job_id, "skipped", target_path=str(target),
                bytes_total=file_size, error=reason[:_MAX_ERROR_LEN],
            )
        self._emit(recording_id, "skipped", str(target), reason, file_size)

    def _fail(
        self,
        recording_id: int,
        row: dict,
        message: str,
        *,
        job_id: Optional[int] = None,
    ) -> None:
        text = message[:_MAX_ERROR_LEN]
        logger.warning("归档失败 recording_id=%s: %s", recording_id, text)
        self._db.set_recording_archive(
            recording_id,
            "failed",
            error=text,
            expected_status=row["archive_status"],
        )
        if job_id is None:
            latest = self._db.get_latest_archive_job(recording_id)
            if latest is not None and latest["status"] in _PENDING_STATES:
                job_id = int(latest["id"])
        if job_id is not None:
            self._db.update_archive_job(job_id, "failed", error=text)
        else:
            # 首次尝试即失败也要留下可追溯任务记录。
            job_id = self._db.create_archive_job(
                recording_id, str(row.get("file_path") or ""), status="failed"
            )
            self._db.update_archive_job(job_id, "failed", error=text)
        self._emit(
            recording_id,
            "failed",
            str(row.get("archive_path") or ""),
            text,
            int(row.get("file_size") or 0),
        )

    def _emit(
        self,
        recording_id: int,
        status: str,
        archive_path: str,
        error: str,
        file_size: int,
    ) -> None:
        payload = {
            "status": status,
            "archive_path": archive_path,
            "error": error,
            "file_size": file_size,
        }
        if self._on_event is not None:
            try:
                self._on_event(recording_id, payload)
            except Exception:  # noqa: BLE001 - 回调失败不影响归档结果
                logger.exception("归档事件回调失败 recording_id=%s", recording_id)
        try:
            self.archive_updated.emit(recording_id, payload)
        except RuntimeError:
            # Qt 对象已销毁（测试或退出阶段），不影响归档结果。
            logger.debug("归档信号发送失败 recording_id=%s", recording_id)
