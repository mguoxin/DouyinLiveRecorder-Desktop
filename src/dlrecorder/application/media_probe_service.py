"""媒体资产提取服务（P1）：后台单线程探测元数据并生成缩略图。

不变式：
- 只处理数据库中已不是 ``recording`` 状态的文件，不读取正在写入的文件。
- 源文件优先使用录制文件；源被归档清理后回退到归档副本。
- 元数据提取与缩略图失败只记录到数据库，不删除、不改写用户文件。
- 同一记录不会并发处理；``force=True`` 允许人工重试失败记录。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..infrastructure.db import Database
from ..infrastructure.media_probe import MediaProbeError, MediaProber

logger = logging.getLogger(__name__)

_MAX_ERROR_LEN = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MediaProbeService(QObject):
    """媒体元数据与缩略图后台队列。"""

    # recording_id, {metadata_status, duration_s, width, height, ...}
    media_updated = Signal(int, dict)

    def __init__(
        self,
        db: Database,
        *,
        thumbnails_dir: Path,
        prober_factory: Optional[Callable[[], MediaProber]] = None,
        on_event: Optional[Callable[[int, dict], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._thumbnails_dir = Path(thumbnails_dir)
        self._prober_factory = prober_factory
        self._on_event = on_event
        self._prober: Optional[MediaProber] = None
        self._condition = threading.Condition()
        self._queue: deque[tuple[int, bool]] = deque()
        self._queued: set[int] = set()
        self._active: set[int] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="dlr-media-probe", daemon=True
        )
        self._thread.start()

    # ---------- 外部接口 ----------
    def enqueue(self, recording_id: int, *, force: bool = False) -> bool:
        """加入探测队列；重复入队或正在处理时返回 False。"""
        if self._stop.is_set():
            return False
        recording_id = int(recording_id)
        with self._condition:
            if recording_id in self._queued or recording_id in self._active:
                return False
            self._queue.append((recording_id, force))
            self._queued.add(recording_id)
            self._condition.notify_all()
        return True

    def enqueue_pending(self, *, limit: int = 200) -> int:
        """启动恢复：为尚未提取元数据的已完成录制排队。"""
        count = 0
        for row in self._db.list_recordings_pending_metadata(limit=limit):
            if self.enqueue(int(row["id"])):
                count += 1
        if count:
            logger.info("媒体探测恢复：重新入队 %s 条未提取记录", count)
        return count

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._queue and not self._active,
                timeout=max(0.0, timeout),
            )

    def shutdown(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=max(0.0, timeout))
        exited = not self._thread.is_alive()
        if not exited:
            logger.critical("媒体探测线程未在 %.1fs 内退出", timeout)
        return exited

    def reset_tool(self) -> None:
        """FFmpeg 路径变化后按需重新解析探测工具。"""
        self._prober = None

    # ---------- 工作线程 ----------
    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stop.is_set():
                    self._condition.wait(timeout=0.5)
                if self._stop.is_set() and not self._queue:
                    return
                recording_id, force = self._queue.popleft()
                self._queued.discard(recording_id)
                self._active.add(recording_id)
            try:
                self._process(recording_id, force)
            except Exception:  # noqa: BLE001 - 单任务失败不得终止工作线程
                logger.exception("媒体探测任务异常 recording_id=%s", recording_id)
            finally:
                with self._condition:
                    self._active.discard(recording_id)
                    self._condition.notify_all()

    # ---------- 处理 ----------
    def _get_prober(self) -> Optional[MediaProber]:
        if self._prober is not None:
            return self._prober
        if self._prober_factory is None:
            return None
        try:
            self._prober = self._prober_factory()
        except Exception:  # noqa: BLE001 - 工具配置失败只影响本轮
            logger.exception("初始化媒体探测工具失败")
            return None
        return self._prober

    def _source_path(self, row: dict) -> Optional[Path]:
        for key in ("file_path", "archive_path"):
            value = row.get(key)
            if not value:
                continue
            path = Path(str(value))
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def _process(self, recording_id: int, force: bool) -> None:
        row = self._db.get_recording(recording_id)
        if row is None:
            return
        if row.get("status") == "recording":
            logger.info("媒体探测跳过：录制仍在进行 recording_id=%s", recording_id)
            return
        if not force and row.get("metadata_status") == "ready":
            return

        source = self._source_path(row)
        if source is None:
            self._fail(recording_id, "录制文件不存在，无法提取媒体信息")
            return

        prober = self._get_prober()
        if prober is None:
            self._fail(recording_id, "媒体工具不可用")
            return

        previous = row.get("metadata_status")
        self._db.set_recording_metadata(
            recording_id,
            status="pending",
            error=None,
            expected_status=previous if previous in {"none", "failed", "ready"} else None,
        )
        try:
            metadata = prober.probe(source)
        except MediaProbeError as exc:
            logger.warning("媒体探测失败 recording_id=%s: %s", recording_id, exc)
            self._fail(recording_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 未预期异常同样要留痕
            logger.exception("媒体探测异常 recording_id=%s", recording_id)
            self._fail(recording_id, f"媒体探测异常: {exc}")
            return

        thumbnail_path: Optional[str] = None
        warning = ""
        try:
            self._thumbnails_dir.mkdir(parents=True, exist_ok=True)
            target = self._thumbnails_dir / f"{recording_id}.jpg"
            thumbnail = prober.thumbnail(
                source, target, duration_s=metadata.duration_s
            )
            thumbnail_path = str(thumbnail)
        except MediaProbeError as exc:
            warning = str(exc)
            logger.warning(
                "缩略图生成失败 recording_id=%s: %s", recording_id, warning
            )
        except OSError as exc:
            warning = f"缩略图保存失败: {exc}"
            logger.warning("缩略图保存失败 recording_id=%s", recording_id, exc_info=True)

        self._db.set_recording_metadata(
            recording_id,
            status="ready",
            duration_s=metadata.duration_s,
            width=metadata.width,
            height=metadata.height,
            video_codec=metadata.video_codec,
            audio_codec=metadata.audio_codec,
            thumbnail_path=thumbnail_path,
            error=warning or None,
        )
        payload = {
            "metadata_status": "ready",
            "duration_s": metadata.duration_s,
            "width": metadata.width,
            "height": metadata.height,
            "video_codec": metadata.video_codec,
            "audio_codec": metadata.audio_codec,
            "thumbnail_path": thumbnail_path or "",
            "error": warning,
            "updated_at": _now_iso(),
        }
        self._emit(recording_id, payload)

    def _fail(self, recording_id: int, message: str) -> None:
        text = message[:_MAX_ERROR_LEN]
        self._db.set_recording_metadata(recording_id, status="failed", error=text)
        self._emit(
            recording_id,
            {
                "metadata_status": "failed",
                "error": text,
                "updated_at": _now_iso(),
            },
        )

    def _emit(self, recording_id: int, payload: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(recording_id, payload)
            except Exception:  # noqa: BLE001 - 回调失败不影响结果
                logger.exception("媒体探测事件回调失败 recording_id=%s", recording_id)
        try:
            self.media_updated.emit(recording_id, payload)
        except RuntimeError:
            logger.debug("媒体探测信号发送失败 recording_id=%s", recording_id)
