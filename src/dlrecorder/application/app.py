"""应用生命周期管理。

负责组装配置、数据库、解析器、监控服务、录制服务与主窗口。
"""
from __future__ import annotations

import http.cookiejar
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

from PySide6.QtWidgets import QApplication

from .. import __version__
from ..domain.states import ParseResult
from ..douyin.engine.parser import EngineDouyinParser
from ..douyin.parser import DouyinParser, MockParser, RealDouyinParser
from ..infrastructure.backup import apply_pending_restore
from ..infrastructure.config import AppConfig
from ..infrastructure.db import Database
from ..infrastructure.http_client import UrllibHttpClient
from ..infrastructure.logging_config import setup_logging
from ..presentation.main_window import MainWindow
from .monitor_service import MonitorService
from .recording_service import RecordingService, StreamRefresh
from .settings_service import AppSettings, SettingsService

logger = logging.getLogger(__name__)

AUTO_RECORD_SETTING = "recording.auto_start"


def _default_base_dir() -> Path:
    """确定数据目录的基准路径。

    - ``DLR_DATA_DIR`` 显式指定时优先使用（自动化验收/测试用于隔离数据）。
    - 打包运行时使用可执行文件所在目录，避免从不同工作目录启动时在别处
      生成新的空数据库，导致用户看不到自己的房间与历史。
    - 源码运行保持当前工作目录。
    """
    override = os.environ.get("DLR_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _build_parser() -> DouyinParser:
    """生产默认使用真实解析器；``DLR_PARSER=mock`` 仅用于开发/自动化验证。

    Mock 模式下可用 ``DLR_MOCK_STREAM_URL`` 指定本地媒体或本地测试流，
    用于不访问抖音的端到端录制验证。
    """
    if os.environ.get("DLR_PARSER", "real").strip().lower() == "mock":
        parser = MockParser()
        stream_url = os.environ.get("DLR_MOCK_STREAM_URL", "").strip()
        parser.set_default(ParseResult.SUCCESS, 2, stream_url or None)
        return parser
    # 匿名 Cookie 会话（ttwid 等），不读取或保存用户凭据。
    # 主链路：上游引擎（web enter + a_bogus）；回退：公开直播页内嵌数据。
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    legacy = RealDouyinParser(UrllibHttpClient(opener), timeout_s=15.0)
    return EngineDouyinParser(timeout_s=10.0, fallback=legacy)


class Application:
    """应用装配与生命周期。"""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        *,
        recording_options: Optional[dict[str, Any]] = None,
    ) -> None:
        base = Path(base_dir) if base_dir else _default_base_dir()
        self.config = AppConfig.from_base_dir(base)
        setup_logging(self.config.log_dir)
        logger.info("应用初始化，版本=%s，数据目录=%s", __version__, self.config.data_dir)

        # 恢复请求必须在打开数据库之前应用。
        restore = apply_pending_restore(self.config.data_dir)
        if restore is not None:
            logger.warning(
                "启动恢复结果: %s（安全副本=%s）", restore.message, restore.safety_copy
            )

        self.qt_app = QApplication.instance() or QApplication([])
        self.qt_app.setApplicationName("抖音直播录制工具")

        self.db = Database(self.config.db_path)
        interrupted_count = self.db.interrupt_running_sessions()
        if interrupted_count:
            logger.info("已将 %s 个遗留 running Session 标记为 interrupted", interrupted_count)
        self.settings = SettingsService(self.db, self.config)
        current = self.settings.load()
        logger.info("FFmpeg 路径: %s", current.ffmpeg_path)
        self.parser = _build_parser()
        logger.info("解析器: %s", type(self.parser).__name__)
        self.service = MonitorService(
            self.db,
            self.parser,
            monitor_interval_s=current.monitor_interval_s,
        )
        self.service.load_rooms()

        options = dict(recording_options or {})
        options.setdefault("output_dir", current.recordings_dir)
        options.setdefault("ffmpeg_path", current.ffmpeg_path)
        options.setdefault("min_free_mb", float(current.min_free_mb))
        options.setdefault("session_id_provider", self.service.get_session_id)
        options.setdefault("stream_resolver", self._refresh_stream)
        self.recording = RecordingService(self.db, **options)
        self._auto_record = self.db.get_setting(AUTO_RECORD_SETTING, "0") == "1"
        self._auto_pending: set[int] = set()
        # 用户手动停止过录制的房间：在下一次确认下播或再次手动开始前不再自动启动。
        self._auto_suppressed: set[int] = set()
        self._shutdown_done = False
        self._connect_recording()

        self.main_window = MainWindow(
            self.service,
            self.recording,
            auto_record=self._auto_record,
            settings_service=self.settings,
            database=self.db,
            on_settings_applied=self._apply_settings,
        )
        self.main_window.auto_record_toggled.connect(self._set_auto_record)
        self.main_window.recording_stop_requested.connect(
            self._on_manual_stop_requested
        )

    def _refresh_stream(self, web_rid: str) -> StreamRefresh:
        """重新解析房间，供录制中断后的自动续录使用（在录制线程中同步调用）。"""
        try:
            detail = self.parser.fetch_and_parse_detail(web_rid)
        except Exception as e:  # noqa: BLE001 - 解析失败只影响本轮续录
            logger.exception("自动续录重新解析失败 web_rid=%s", web_rid)
            return StreamRefresh(live=None, error=str(e))
        if detail.result != ParseResult.SUCCESS:
            return StreamRefresh(live=None, error=f"解析失败: {detail.result}")
        if detail.status == 4:
            return StreamRefresh(live=False, error="直播已结束")
        if detail.stream_url:
            return StreamRefresh(live=True, stream_url=detail.stream_url)
        if detail.status == 2:
            return StreamRefresh(live=None, error="直播中但未取得流地址")
        return StreamRefresh(live=None, error="未能确认直播状态")

    def _connect_recording(self) -> None:
        self.service.room_state_updated.connect(self._on_room_state_updated)
        self.service.task_stopped.connect(self._on_task_stopped)
        self.service.room_removed.connect(self._on_room_removed)
        self.recording.recording_started.connect(self._on_recording_started)
        self.recording.recording_finished.connect(self._on_recording_finished)
        self.recording.recording_error.connect(self._on_recording_error)
        if self._auto_record:
            logger.info("自动录制已启用")

    def _on_room_state_updated(self, room_id: int, snapshot: dict) -> None:
        """确认直播且自动录制开启时启动录制（每个房间只请求一次）。"""
        if not self._auto_record:
            return
        confirmed = snapshot.get("last_confirmed_room_state")
        if confirmed == "OFFLINE_CONFIRMED":
            # 确认下播后解除手动停止抑制：下次开播仍可自动录制。
            self._auto_suppressed.discard(room_id)
            if self.recording.is_recording(room_id):
                logger.info("房间 %s 已确认下播，停止自动录制", room_id)
                self.recording.stop_recording(room_id)
            return
        if confirmed != "LIVE":
            return
        if room_id in self._auto_suppressed:
            return
        if room_id in self._auto_pending or self.recording.is_recording(room_id):
            return
        stream_url = snapshot.get("last_stream_url") or self.service.get_stream_url(room_id)
        if not stream_url:
            return
        self._auto_pending.add(room_id)
        self.recording.start_recording(room_id, stream_url)

    def _on_manual_stop_requested(self, room_id: int) -> None:
        """用户主动停止录制：抑制该房间的自动录制，直到下播或再次手动开始。"""
        self._auto_suppressed.add(room_id)
        logger.info("房间 %s 已手动停止录制，自动录制暂停", room_id)

    def _on_task_stopped(self, room_id: int) -> None:
        if self.recording.is_recording(room_id):
            self.recording.stop_recording(room_id)

    def _on_room_removed(self, room_id: int) -> None:
        self._auto_suppressed.discard(room_id)
        if self.recording.is_recording(room_id):
            self.recording.stop_recording(room_id)

    def _on_recording_started(self, room_id: int, snapshot: dict) -> None:
        # 能启动即说明是用户手动开始（自动启动已被抑制），解除抑制。
        self._auto_suppressed.discard(room_id)
        self._auto_pending.discard(room_id)

    def _on_recording_finished(self, room_id: int, snapshot: dict) -> None:
        self._auto_pending.discard(room_id)

    def _on_recording_error(self, room_id: int, message: str) -> None:
        self._auto_pending.discard(room_id)

    def _apply_settings(self, settings: AppSettings) -> None:
        """将已保存的设置应用到运行中的服务（不中断进行中的录制）。"""
        try:
            self.recording.set_ffmpeg_path(settings.ffmpeg_path)
            self.recording.set_output_dir(settings.recordings_dir)
            self.recording.set_min_free_mb(settings.min_free_mb)
            self.service.set_monitor_interval(settings.monitor_interval_s)
        except (OSError, ValueError) as e:
            logger.error("应用运行时设置失败: %s", e, exc_info=True)
            return
        logger.info("运行时设置已应用")

    def _set_auto_record(self, enabled: bool) -> None:
        """持久化自动录制开关；关闭只影响后续自动启动。"""
        self._auto_record = enabled
        if enabled:
            # 重新开启自动录制视为用户要求恢复，清除此前的手动停止抑制。
            self._auto_suppressed.clear()
        self.db.set_setting(AUTO_RECORD_SETTING, "1" if enabled else "0")
        logger.info("自动录制已%s", "开启" if enabled else "关闭")

    def run(self) -> int:
        self.main_window.show()
        return self.qt_app.exec()

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("应用关闭中...")
        # 录制进程先停，避免数据库关闭后仍有录制线程写入。
        report = self.recording.shutdown()
        if not report.thread_exited:
            logger.critical(
                "录制服务未完全退出，remaining_active=%s", report.remaining_active
            )
        self.service.shutdown()
        self._dispose_main_window()

    def _dispose_main_window(self) -> None:
        """解除日志桥接并销毁窗口，避免跨测试/重启残留的 Qt 对象。"""
        window = getattr(self, "main_window", None)
        if window is None:
            return
        window.detach_logging()
        window.hide()
        window.deleteLater()
        self.qt_app.processEvents()
