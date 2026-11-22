"""主窗口：侧边栏导航、页面容器、系统托盘与退出收尾。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..application.monitor_service import MonitorService
from ..application.recording_service import RecordingService
from ..application.settings_service import AppSettings, SettingsService
from ..domain.models import validate_web_rid
from ..infrastructure.backup import prepare_restore, verify_sqlite
from ..infrastructure.db import Database
from . import icons
from .pages import HistoryPage, LogsPage, RoomsPage, SettingsPage, Sidebar
from .theme import ThemeManager
from .tray import TrayController
from .widgets import (
    AddRoomDialog,
    QtLogBridge,
    RoomEditDialog,
    ask_confirm,
    choose_existing_file,
    choose_save_file,
    show_info,
    show_warning,
)

logger = logging.getLogger(__name__)

PAGE_ROOMS = 0
PAGE_HISTORY = 1
PAGE_LOGS = 2
PAGE_SETTINGS = 3


class MainWindow(QMainWindow):
    auto_record_toggled = Signal(bool)
    # 用户主动点击“停止录制”（区别于自动停止），用于抑制自动录制重新拉起
    recording_stop_requested = Signal(int)
    # 后台关闭完成（True=全部服务正常停止），跨线程排队回主线程
    shutdown_finished = Signal(bool)

    def __init__(
        self,
        service: MonitorService,
        recording: RecordingService,
        *,
        auto_record: bool = False,
        settings_service: Optional[SettingsService] = None,
        database: Optional[Database] = None,
        on_settings_applied: Optional[Callable[[AppSettings], None]] = None,
        theme_manager: Optional[ThemeManager] = None,
        log_dir: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._recording = recording
        self._auto_record = auto_record
        self._settings_service = settings_service
        self._database = database
        self._on_settings_applied = on_settings_applied
        self._log_dir = log_dir
        self._closing = False
        self._tray_hint_shown = False
        self._shutdown_started = False
        self._shutdown_complete = False
        self._shutdown_thread: Optional[threading.Thread] = None
        self._shutdown_ok = True

        self.setWindowTitle(f"抖音直播录制工具 v{__version__}")
        self.resize(1120, 740)
        self.setMinimumSize(900, 600)
        self.setWindowIcon(icons.app_icon())

        # 日志桥接（处理器由 QtLogBridge 自行挂载，关闭时必须解除）
        self._log_bridge = QtLogBridge()

        self._theme = theme_manager or ThemeManager(
            QApplication.instance(), database, self
        )

        self._build_ui()
        self._connect_signals()
        self._load_initial_rooms()
        self._setup_tray()
        self._apply_theme()
        self._refresh_summary()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("Root")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        root.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.pages.setObjectName("ContentRoot")
        self.rooms_page = RoomsPage(self._auto_record)
        self.history_page = HistoryPage()
        self.logs_page = LogsPage()
        if self._settings_service is not None:
            current = self._settings_service.load()
            self.settings_page: Optional[SettingsPage] = SettingsPage(
                self._settings_service,
                current,
                self._theme,
                backup_enabled=self._database is not None,
                restore_enabled=self._database is not None,
            )
        else:
            self.settings_page = None
            placeholder = QWidget()
            placeholder_layout = QVBoxLayout(placeholder)
            label = QLabel("当前运行方式未启用设置服务。")
            label.setObjectName("Muted")
            placeholder_layout.addWidget(label)
            placeholder_layout.addStretch(1)
        self.pages.addWidget(self.rooms_page)
        self.pages.addWidget(self.history_page)
        self.pages.addWidget(self.logs_page)
        self.pages.addWidget(
            self.settings_page if self.settings_page is not None else placeholder
        )
        root.addWidget(self.pages, 1)

        # 兼容既有调用点
        self.room_list = self.rooms_page.room_list
        self.log_widget = self.logs_page.log_widget
        self.chk_auto_record = self.rooms_page.chk_auto_record
        if self.settings_page is not None:
            self.btn_backup = self.settings_page.btn_backup
            self.btn_restore = self.settings_page.btn_restore

        self.setStatusBar(QStatusBar())
        self.statusBar().setSizeGripEnabled(False)
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, 1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Muted")
        self.statusBar().addPermanentWidget(self.summary_label)

    def _connect_signals(self) -> None:
        self.sidebar.page_selected.connect(self._navigate)
        self.rooms_page.add_requested.connect(self._on_add_room)
        self.rooms_page.edit_requested.connect(self._on_edit_room)
        self.rooms_page.remove_requested.connect(self._on_remove_room)
        self.rooms_page.monitor_toggle_requested.connect(self._on_toggle_monitor)
        self.rooms_page.record_toggle_requested.connect(self._on_toggle_record)
        self.rooms_page.open_dir_requested.connect(self._on_open_recordings_dir)
        self.rooms_page.auto_record_changed.connect(self._on_auto_record_toggled)
        self.rooms_page.selection_changed.connect(self._update_room_actions)

        self.history_page.refresh_requested.connect(self._refresh_history)
        self.history_page.open_dir_requested.connect(self._on_open_recordings_dir)
        self.history_page.open_file_requested.connect(self._open_file)
        self.history_page.open_folder_requested.connect(self._open_file_folder)

        self.logs_page.open_dir_requested.connect(self._on_open_log_dir)

        if self.settings_page is not None:
            self.settings_page.settings_saved.connect(self._on_settings_saved)
            self.settings_page.backup_requested.connect(self._on_backup_database)
            self.settings_page.restore_requested.connect(self._on_restore_database)
            if self._database is None:
                self.settings_page.btn_backup.setEnabled(False)
                self.settings_page.btn_restore.setEnabled(False)

        svc = self._service
        svc.room_added.connect(self._on_room_added)
        svc.room_updated.connect(self._on_room_updated)
        svc.room_removed.connect(self._on_room_removed)
        svc.room_state_updated.connect(self.room_list.update_state)
        svc.room_state_updated.connect(self._on_room_state_updated)
        svc.task_error.connect(self._on_task_error)
        svc.task_stopped.connect(self._on_task_stopped)
        self._log_bridge.log_emitted.connect(self.log_widget.append_log)

        rec = self._recording
        rec.recording_started.connect(self._on_recording_started)
        rec.recording_finished.connect(self._on_recording_finished)
        rec.recording_error.connect(self._on_recording_error)
        rec.recording_reconnecting.connect(self._on_recording_reconnecting)

        self._theme.theme_changed.connect(self._on_theme_changed)
        self.shutdown_finished.connect(
            self._on_shutdown_finished, Qt.ConnectionType.QueuedConnection
        )

    def _load_initial_rooms(self) -> None:
        for room in self._service.get_rooms():
            if room.id is not None:
                self.room_list.add_room(room.id, room.web_rid, room.display_name)
                self.room_list.set_monitoring(
                    room.id, self._service.is_monitoring(room.id)
                )
                self.room_list.set_recording(
                    room.id, self._recording.is_recording(room.id)
                )
        self.rooms_page.refresh_empty_state()

    # ------------------------------------------------------------------
    # 托盘
    # ------------------------------------------------------------------
    def _setup_tray(self) -> None:
        self._tray: Optional[TrayController] = None
        app = QApplication.instance()
        try:
            tray = TrayController(self)
        except Exception:  # noqa: BLE001 - 托盘可选，初始化失败不得阻止启动
            logger.exception("系统托盘初始化失败，降级为普通窗口运行")
            if app is not None:
                app.setQuitOnLastWindowClosed(True)
            self.sidebar.set_tray_note("系统托盘不可用")
            return
        if tray.available:
            self._tray = tray
            if app is not None:
                app.setQuitOnLastWindowClosed(False)
            self._tray.show_requested.connect(self._restore_window)
            self._tray.quit_requested.connect(self.close)
            self._tray.auto_record_changed.connect(self.chk_auto_record.setChecked)
            self._tray.open_dir_requested.connect(self._on_open_recordings_dir)
            self._tray.start_all_requested.connect(self._start_all_monitoring)
            self._tray.stop_all_requested.connect(self._stop_all_monitoring)
            self._tray.set_auto_record(self.chk_auto_record.isChecked())
            self.sidebar.set_tray_note("最小化后在托盘运行")
        else:
            self._tray = None
            if app is not None:
                app.setQuitOnLastWindowClosed(True)
            self.sidebar.set_tray_note("系统托盘不可用")
            logger.warning("系统托盘不可用，窗口关闭将直接退出")

    def _hide_to_tray(self) -> None:
        if self._tray is None or self._closing or not self.isMinimized():
            return
        self.hide()
        self.setWindowState(Qt.WindowState.WindowNoState)
        if not self._tray_hint_shown:
            self._tray.show_message(
                "已最小化到托盘",
                "直播监控与录制继续运行，双击托盘图标可恢复主窗口。",
            )
            self._tray_hint_shown = True

    def _restore_window(self) -> None:
        self.showNormal()
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.activateWindow()
        self.raise_()

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    def _navigate(self, index: int) -> None:
        if index < 0 or index >= self.pages.count():
            return
        self.sidebar.set_current(index)
        self.pages.setCurrentIndex(index)
        if index == PAGE_HISTORY:
            self._refresh_history()
        elif index == PAGE_SETTINGS and self.settings_page is not None:
            self.settings_page.sync_theme()

    # ------------------------------------------------------------------
    # 房间操作
    # ------------------------------------------------------------------
    def _on_add_room(self) -> None:
        dialog = AddRoomDialog(self)
        if dialog.exec() != AddRoomDialog.DialogCode.Accepted:
            return
        raw_web_rid, name = dialog.values()
        try:
            web_rid = validate_web_rid(raw_web_rid)
        except ValueError as e:
            show_warning(self, "无法添加直播间", str(e))
            return
        room_id = self._service.add_room(web_rid, name.strip())
        if room_id is None:
            show_warning(
                self, "添加失败", f"无法添加直播间 {web_rid}，可能已存在。"
            )
            return
        self.status_label.setText(f"已添加直播间 {room_id}")
        self._navigate(PAGE_ROOMS)
        self._refresh_summary()

    def _on_edit_room(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            show_info(self, "提示", "请先选择一个直播间。")
            return
        room = self._service.get_room(room_id)
        if room is None:
            show_warning(self, "错误", "直播间不存在。")
            return
        dialog = RoomEditDialog(room.web_rid, room.display_name, self)
        if dialog.exec() != RoomEditDialog.DialogCode.Accepted:
            return
        web_rid, display_name = dialog.values()
        ok, error = self._service.update_room(
            room_id, web_rid=web_rid, display_name=display_name
        )
        if not ok:
            show_warning(self, "更新失败", error)
        else:
            self.status_label.setText(f"直播间 {room_id} 已更新")

    def _on_remove_room(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            show_info(self, "提示", "请先选择一个直播间。")
            return
        name = self.room_list.selected_display_name()
        if not ask_confirm(
            self,
            "删除直播间",
            f"确定删除“{name}”？",
            informative="只会删除监控列表中的房间，已录制的文件不会被删除。",
            ok_text="删除",
            cancel_text="取消",
            danger=True,
        ):
            return
        if self._recording.is_recording(room_id):
            self._recording.stop_recording(room_id)
        self._service.remove_room(room_id)
        self._refresh_summary()

    def _on_toggle_monitor(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            return
        if self._service.is_monitoring(room_id):
            self._on_stop_monitor()
        else:
            self._on_start_monitor()

    def _on_toggle_record(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            return
        if self._recording.is_recording(room_id):
            self._on_stop_recording()
        else:
            self._on_start_recording()

    def _on_start_recording(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            show_info(self, "提示", "请先选择一个直播间。")
            return
        stream_url = self._service.get_stream_url(room_id)
        if not stream_url:
            show_info(
                self,
                "无法录制",
                "尚未解析到直播流地址。请先开始监控并等待解析成功。",
            )
            return
        try:
            self._recording.start_recording(room_id, stream_url)
        except (RuntimeError, ValueError, OSError) as e:
            show_warning(self, "无法开始录制", str(e))
            return
        self._update_room_actions()
        self._refresh_summary()

    def _on_stop_recording(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            show_info(self, "提示", "请先选择一个直播间。")
            return
        if not self._recording.is_recording(room_id):
            show_info(self, "提示", "该直播间当前未在录制。")
            return
        self.recording_stop_requested.emit(room_id)
        self._recording.stop_recording(room_id)
        self._update_room_actions()
        self._refresh_summary()

    def _on_start_monitor(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            show_info(self, "提示", "请先选择一个直播间。")
            return
        try:
            self._service.start_monitor(room_id)
            monitoring = self._service.is_monitoring(room_id)
        except RuntimeError as e:
            show_warning(self, "无法开始监控", str(e))
            monitoring = False
        # 以后台实际状态为准，避免 Session 创建失败时界面仍显示“监控中”。
        self.room_list.set_monitoring(room_id, monitoring)
        if monitoring:
            self.status_label.setText(f"监控中: 直播间 {room_id}")
        else:
            self.status_label.setText(
                f"直播间 {room_id} 监控未启动，请查看日志"
            )
        self._update_room_actions()
        self._refresh_summary()

    def _on_stop_monitor(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            show_info(self, "提示", "请先选择一个直播间。")
            return
        self._service.stop_monitor(room_id)
        self.room_list.set_monitoring(room_id, False)
        self.status_label.setText(f"直播间 {room_id} 已停止监控")
        self._update_room_actions()
        self._refresh_summary()

    def _start_all_monitoring(self) -> None:
        started = 0
        for room in self._service.get_rooms():
            if room.id is None or self._service.is_monitoring(room.id):
                continue
            try:
                self._service.start_monitor(room.id)
            except RuntimeError as e:
                logger.warning("开始监控失败 room_id=%s: %s", room.id, e)
                continue
            self.room_list.set_monitoring(
                room.id, self._service.is_monitoring(room.id)
            )
            started += 1
        self.status_label.setText(f"已开始监控 {started} 个直播间")
        self._refresh_summary()

    def _stop_all_monitoring(self) -> None:
        stopped = 0
        for room in self._service.get_rooms():
            if room.id is None or not self._service.is_monitoring(room.id):
                continue
            self._service.stop_monitor(room.id)
            self.room_list.set_monitoring(room.id, False)
            stopped += 1
        self.status_label.setText(f"已停止监控 {stopped} 个直播间")
        self._refresh_summary()

    def _on_room_added(self, room_id: int, web_rid: str, display_name: str) -> None:
        self.room_list.add_room(room_id, web_rid, display_name)
        self.rooms_page.refresh_empty_state()
        self.status_label.setText(f"已添加直播间 {room_id}")
        self._refresh_summary()

    def _on_room_updated(self, room_id: int, web_rid: str, display_name: str) -> None:
        self.room_list.update_room(room_id, web_rid, display_name)

    def _on_room_removed(self, room_id: int) -> None:
        self.room_list.remove_room(room_id)
        self.rooms_page.refresh_empty_state()
        self._refresh_summary()

    def _on_room_state_updated(self, _room_id: int, _snapshot: dict) -> None:
        self._refresh_summary()

    def _update_room_actions(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            self.rooms_page.set_action_state(False, False, False)
            return
        self.rooms_page.set_action_state(
            True,
            self._service.is_monitoring(room_id),
            self._recording.is_recording(room_id),
        )

    # ------------------------------------------------------------------
    # 后台事件
    # ------------------------------------------------------------------
    def _on_task_error(self, room_id: int, error: str) -> None:
        logger.error("房间 %s 监控异常: %s", room_id, error)
        self.status_label.setText(f"直播间 {room_id} 异常: {error}")

    def _on_task_stopped(self, room_id: int) -> None:
        self.room_list.set_monitoring(room_id, False)
        self.status_label.setText(f"直播间 {room_id} 已停止监控")
        self._update_room_actions()
        self._refresh_summary()

    def _on_recording_started(self, room_id: int, snapshot: dict) -> None:
        self.room_list.set_recording(room_id, True)
        self.status_label.setText(f"直播间 {room_id} 开始录制")
        logger.info("房间 %s 开始录制: %s", room_id, snapshot.get("file_path"))
        self._update_room_actions()
        self._refresh_summary()

    def _on_recording_finished(self, room_id: int, snapshot: dict) -> None:
        self.room_list.set_recording(room_id, False)
        scope = snapshot.get("scope", "segment")
        size = snapshot.get("file_size")
        status = snapshot.get("status", "")
        error = snapshot.get("error", "")
        if scope == "session":
            # 会话结束没有产出文件，不展示文件大小。
            reason = error or "直播已结束"
            self.status_label.setText(f"直播间 {room_id} 录制会话结束：{reason}")
        elif status == "failed":
            self.status_label.setText(f"直播间 {room_id} 录制失败: {error}")
        else:
            self.status_label.setText(
                f"直播间 {room_id} 录制结束，文件 {size or 0} 字节"
            )
        logger.info(
            "房间 %s 录制结束 scope=%s status=%s size=%s file=%s",
            room_id,
            scope,
            status,
            size,
            snapshot.get("file_path"),
        )
        if self.pages.currentIndex() == PAGE_HISTORY:
            self._refresh_history()
        self._update_room_actions()
        self._refresh_summary()

    def _on_recording_error(self, room_id: int, message: str) -> None:
        self.room_list.set_recording(room_id, False)
        self.status_label.setText(f"直播间 {room_id} 录制错误: {message}")
        self._update_room_actions()
        self._refresh_summary()

    def _on_recording_reconnecting(self, room_id: int, snapshot: dict) -> None:
        self.room_list.set_recording(room_id, True)
        self.status_label.setText(
            f"直播间 {room_id} 录制中断，"
            f"{snapshot.get('delay_s', 0):.0f} 秒后自动续录"
            f"（第 {snapshot.get('attempt', 1)}/{snapshot.get('max_attempts', 1)} 次）"
        )
        logger.warning(
            "房间 %s 录制中断，准备第 %s 次自动续录: %s",
            room_id,
            snapshot.get("attempt"),
            snapshot.get("error"),
        )

    def _on_auto_record_toggled(self, enabled: bool) -> None:
        self._auto_record = enabled
        if self._tray is not None:
            self._tray.set_auto_record(enabled)
        self.auto_record_toggled.emit(enabled)
        self.status_label.setText("自动录制已开启" if enabled else "自动录制已关闭")

    # ------------------------------------------------------------------
    # 历史 / 目录
    # ------------------------------------------------------------------
    def _refresh_history(self) -> None:
        try:
            rows = self._recording.list_history(limit=500)
        except Exception:  # noqa: BLE001 - 历史读取失败不应影响录制
            logger.exception("读取录制历史失败")
            rows = []
        self.history_page.set_records(rows)

    def _on_show_history(self) -> None:
        self._navigate(PAGE_HISTORY)

    def _on_open_recordings_dir(self) -> None:
        target = self._recording.output_dir
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            show_warning(self, "无法打开", f"录制目录不可用：{e}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _on_open_log_dir(self) -> None:
        if self._log_dir is None:
            show_info(self, "提示", "当前运行方式未提供日志目录。")
            return
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            show_warning(self, "无法打开", f"日志目录不可用：{e}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._log_dir)))

    @staticmethod
    def _open_file(path: str) -> None:
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @staticmethod
    def _open_file_folder(path: str) -> None:
        if not path:
            return
        folder = Path(path).parent
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _on_settings_saved(self, saved: AppSettings) -> None:
        if self._on_settings_applied is not None:
            self._on_settings_applied(saved)
        self.status_label.setText("设置已保存")

    def _on_open_settings(self) -> None:
        self._navigate(PAGE_SETTINGS)

    def _on_backup_database(self) -> None:
        if self._database is None:
            return
        backup_dir = self._database.db_path.parent / "backups"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            show_warning(self, "无法备份", f"备份目录不可用：{e}")
            return
        default_name = backup_dir / f"app-backup-{datetime.now():%Y%m%d-%H%M%S}.db"
        path = choose_save_file(
            self,
            "备份数据库",
            str(default_name),
            ["SQLite 数据库 (*.db)"],
        )
        if not path:
            return
        target = Path(path)
        if target.exists() and not ask_confirm(
            self,
            "覆盖文件",
            "目标文件已存在，是否覆盖？",
            informative=str(target),
            ok_text="覆盖",
            cancel_text="取消",
            danger=True,
        ):
            return
        try:
            dest = self._database.backup_to(target)
        except Exception as e:  # noqa: BLE001 - 备份失败必须明确反馈
            logger.exception("数据库备份失败")
            show_warning(self, "备份失败", f"数据库备份失败：{e}")
            return
        show_info(self, "备份完成", f"数据库已备份到：\n{dest}")

    def _on_restore_database(self) -> None:
        if self._database is None:
            return
        start_dir = self._database.db_path.parent / "backups"
        path = choose_existing_file(
            self,
            "选择数据库备份",
            str(start_dir),
            ["SQLite 数据库 (*.db)", "所有文件 (*)"],
        )
        if not path:
            return
        backup_path = Path(path)
        ok, message = verify_sqlite(backup_path)
        if not ok:
            show_warning(
                self, "无法恢复", f"所选文件不是可用的数据库备份：\n{message}"
            )
            return
        if not ask_confirm(
            self,
            "确认恢复",
            "恢复将在退出并重新启动程序后生效。",
            informative=(
                "恢复前会自动为当前数据库创建安全副本；录制文件不会被删除。"
                "是否继续？"
            ),
            ok_text="安排恢复",
            cancel_text="取消",
        ):
            return
        try:
            prepare_restore(backup_path, self._database.db_path.parent)
        except (ValueError, OSError) as e:
            show_warning(self, "无法安排恢复", str(e))
            return
        show_info(
            self,
            "已安排恢复",
            "恢复请求已保存。请关闭并重新启动程序以完成恢复。",
        )

    # ------------------------------------------------------------------
    # 状态 / 主题
    # ------------------------------------------------------------------
    def _refresh_summary(self) -> None:
        rooms = self._service.get_rooms()
        monitoring = 0
        for room in rooms:
            if room.id is not None and self._service.is_monitoring(room.id):
                monitoring += 1
        try:
            recording = len(self._recording.active_rooms())
        except Exception:  # noqa: BLE001
            recording = 0
        self.summary_label.setText(
            f"{len(rooms)} 个直播间 · {monitoring} 监控中 · {recording} 录制中"
        )
        if self._tray is not None:
            self._tray.set_recording_count(recording)

    def _on_theme_changed(self, _effective: str) -> None:
        self._apply_theme()

    def _apply_theme(self) -> None:
        tokens = self._theme.tokens
        self.sidebar.set_theme_tokens(tokens)
        self.rooms_page.set_theme_tokens(tokens)
        self.history_page.set_theme_tokens(tokens)
        self.logs_page.set_theme_tokens(tokens)
        if self.settings_page is not None:
            self.settings_page.set_theme_tokens(tokens)
            self.settings_page.sync_theme()
        self.sidebar.set_theme_note(self._theme.effective_label())
        QTimer.singleShot(0, lambda: self._theme.refresh_title_bar(self))

    def detach_logging(self) -> None:
        """解除日志桥接，必须在窗口销毁前调用。"""
        self._log_bridge.close()

    def dispose(self) -> None:
        """应用退出时释放托盘与日志桥接（可重复调用）。"""
        if self._tray is not None:
            self._tray.dispose()
            self._tray = None
        self.detach_logging()

    # ------------------------------------------------------------------
    # 窗口事件
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._theme.refresh_title_bar(self)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self._tray is not None
            and not self._closing
        ):
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._shutdown_complete:
            event.accept()
            return
        if self._shutdown_started:
            # 关闭已在后台进行，忽略重复关闭请求，避免并发停止。
            event.ignore()
            return
        active = []
        try:
            active = self._recording.active_rooms()
        except Exception:  # noqa: BLE001
            logger.debug("读取活动录制失败", exc_info=True)
        if active and not ask_confirm(
            self,
            "退出前停止录制",
            f"当前有 {len(active)} 个直播间正在录制。",
            informative="退出会安全停止录制并完成文件收尾，已录制的文件不会删除。",
            ok_text="停止录制并退出",
            cancel_text="继续录制",
            danger=True,
        ):
            event.ignore()
            return

        self._closing = True
        self._shutdown_started = True
        logger.info("主窗口关闭，开始后台退出（录制中房间=%s）", active)
        # 后台停止期间保持窗口可见并给出明确状态，避免用户误以为程序卡死。
        if not self.isVisible():
            self._restore_window()
        self.status_label.setText("正在停止录制与监控，请稍候（最长约 30 秒）…")
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(False)
        event.ignore()
        self._shutdown_thread = threading.Thread(
            target=self._shutdown_worker,
            name="dlr-gui-shutdown",
            daemon=True,
        )
        self._shutdown_thread.start()

    def _shutdown_worker(self) -> None:
        """在后台线程停止录制与监控；结果经 QueuedConnection 回到主线程。"""
        ok = True
        try:
            self._recording.shutdown()
        except Exception:  # noqa: BLE001 - 退出路径必须继续清理
            logger.exception("停止录制服务失败")
            ok = False
        try:
            self._service.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("停止监控服务失败")
            ok = False
        self.shutdown_finished.emit(ok)

    def _on_shutdown_finished(self, ok: bool) -> None:
        self._shutdown_ok = ok
        self._shutdown_complete = True
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(True)
        self.status_label.setText(
            "已停止录制与监控，正在退出…" if ok else "停止过程中出现异常，详见日志"
        )
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()
