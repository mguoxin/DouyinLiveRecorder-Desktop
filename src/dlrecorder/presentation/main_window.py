"""主窗口。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
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
from .widgets import (
    LogWidget,
    QtLogBridge,
    RecordingHistoryDialog,
    RoomEditDialog,
    RoomListWidget,
    SettingsDialog,
)

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    auto_record_toggled = Signal(bool)
    # 用户主动点击“停止录制”（区别于自动停止），用于抑制自动录制重新拉起
    recording_stop_requested = Signal(int)

    def __init__(
        self,
        service: MonitorService,
        recording: RecordingService,
        *,
        auto_record: bool = False,
        settings_service: Optional[SettingsService] = None,
        database: Optional[Database] = None,
        on_settings_applied: Optional[Callable[[AppSettings], None]] = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._recording = recording
        self._auto_record = auto_record
        self._settings_service = settings_service
        self._database = database
        self._on_settings_applied = on_settings_applied
        self.setWindowTitle(f"抖音直播录制工具 v{__version__}")
        self.resize(900, 600)

        # 日志桥接（处理器由 QtLogBridge 自行挂载，关闭时必须解除）
        self._log_bridge = QtLogBridge()

        self._build_ui()
        self._connect_signals()
        self._load_initial_rooms()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 工具栏按钮
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.btn_add = QPushButton("添加房间")
        self.btn_edit = QPushButton("编辑房间")
        self.btn_remove = QPushButton("删除房间")
        self.btn_start = QPushButton("开始监控")
        self.btn_stop = QPushButton("停止监控")
        self.btn_record_start = QPushButton("开始录制")
        self.btn_record_stop = QPushButton("停止录制")
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_edit)
        toolbar.addWidget(self.btn_remove)
        toolbar.addSeparator()
        toolbar.addWidget(self.btn_start)
        toolbar.addWidget(self.btn_stop)
        toolbar.addSeparator()
        toolbar.addWidget(self.btn_record_start)
        toolbar.addWidget(self.btn_record_stop)
        self.chk_auto_record = QCheckBox("自动录制")
        self.chk_auto_record.setChecked(self._auto_record)
        toolbar.addWidget(self.chk_auto_record)
        toolbar.addSeparator()
        self.btn_history = QPushButton("录制历史")
        self.btn_open_dir = QPushButton("打开录制目录")
        toolbar.addWidget(self.btn_history)
        toolbar.addWidget(self.btn_open_dir)
        toolbar.addSeparator()
        self.btn_settings = QPushButton("设置")
        self.btn_backup = QPushButton("备份数据")
        self.btn_restore = QPushButton("恢复备份")
        toolbar.addWidget(self.btn_settings)
        toolbar.addWidget(self.btn_backup)
        toolbar.addWidget(self.btn_restore)
        if self._settings_service is None:
            self.btn_settings.setEnabled(False)
        if self._database is None:
            self.btn_backup.setEnabled(False)
            self.btn_restore.setEnabled(False)

        # 分割器：上方房间列表，下方日志
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.room_list = RoomListWidget()
        self.log_widget = LogWidget()
        splitter.addWidget(self.room_list)
        splitter.addWidget(self.log_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        # 状态栏
        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label)

    def _connect_signals(self) -> None:
        self.btn_add.clicked.connect(self._on_add_room)
        self.btn_edit.clicked.connect(self._on_edit_room)
        self.btn_remove.clicked.connect(self._on_remove_room)
        self.btn_start.clicked.connect(self._on_start_monitor)
        self.btn_stop.clicked.connect(self._on_stop_monitor)
        self.btn_record_start.clicked.connect(self._on_start_recording)
        self.btn_record_stop.clicked.connect(self._on_stop_recording)
        self.chk_auto_record.toggled.connect(self._on_auto_record_toggled)
        self.btn_history.clicked.connect(self._on_show_history)
        self.btn_open_dir.clicked.connect(self._on_open_recordings_dir)
        self.btn_settings.clicked.connect(self._on_open_settings)
        self.btn_backup.clicked.connect(self._on_backup_database)
        self.btn_restore.clicked.connect(self._on_restore_database)

        svc = self._service
        svc.room_added.connect(self._on_room_added)
        svc.room_updated.connect(self.room_list.update_room)
        svc.room_removed.connect(self.room_list.remove_room)
        svc.room_state_updated.connect(self.room_list.update_state)
        svc.task_error.connect(self._on_task_error)
        svc.task_stopped.connect(self._on_task_stopped)
        self._log_bridge.log_emitted.connect(self.log_widget.append_log)

        rec = self._recording
        rec.recording_started.connect(self._on_recording_started)
        rec.recording_finished.connect(self._on_recording_finished)
        rec.recording_error.connect(self._on_recording_error)
        rec.recording_reconnecting.connect(self._on_recording_reconnecting)

    def _load_initial_rooms(self) -> None:
        for room in self._service.get_rooms():
            if room.id is not None:
                self.room_list.add_room(room.id, room.web_rid, room.display_name)

    # ---------- 槽函数 ----------
    def _on_add_room(self) -> None:
        text, ok = QInputDialog.getText(
            self,
            "添加直播间",
            "直播间链接或 ID（例如 https://live.douyin.com/123456 "
            "或 https://www.douyin.com/follow/live/123456）:",
        )
        if not ok or not text.strip():
            return
        try:
            web_rid = validate_web_rid(text)
        except ValueError as e:
            QMessageBox.warning(self, "无法添加直播间", str(e))
            return
        name, ok2 = QInputDialog.getText(self, "添加直播间", "显示名称（可选）:")
        room_id = self._service.add_room(web_rid, name.strip() if ok2 else "")
        if room_id is None:
            QMessageBox.warning(
                self, "添加失败", f"无法添加直播间 {web_rid}，可能已存在。"
            )

    def _on_edit_room(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            QMessageBox.information(self, "提示", "请先选择一个房间。")
            return
        room = self._service.get_room(room_id)
        if room is None:
            QMessageBox.warning(self, "错误", "房间不存在。")
            return
        dialog = RoomEditDialog(room.web_rid, room.display_name, self)
        if dialog.exec() != RoomEditDialog.DialogCode.Accepted:
            return
        web_rid, display_name = dialog.values()
        ok, error = self._service.update_room(
            room_id, web_rid=web_rid, display_name=display_name
        )
        if not ok:
            QMessageBox.warning(self, "更新失败", error)
        else:
            self.status_label.setText(f"房间 {room_id} 已更新")

    def _on_remove_room(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            QMessageBox.information(self, "提示", "请先选择一个房间。")
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除房间 {room_id}？录制文件不会删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._recording.is_recording(room_id):
            self._recording.stop_recording(room_id)
        self._service.remove_room(room_id)

    def _on_start_recording(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            QMessageBox.information(self, "提示", "请先选择一个房间。")
            return
        stream_url = self._service.get_stream_url(room_id)
        if not stream_url:
            QMessageBox.information(
                self,
                "无法录制",
                "尚未解析到直播流地址。请先开始监控并等待解析成功。",
            )
            return
        self._recording.start_recording(room_id, stream_url)

    def _on_stop_recording(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            QMessageBox.information(self, "提示", "请先选择一个房间。")
            return
        if not self._recording.is_recording(room_id):
            QMessageBox.information(self, "提示", "该房间当前未在录制。")
            return
        self.recording_stop_requested.emit(room_id)
        self._recording.stop_recording(room_id)

    def _on_start_monitor(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            QMessageBox.information(self, "提示", "请先选择一个房间。")
            return
        try:
            self._service.start_monitor(room_id)
            monitoring = self._service.is_monitoring(room_id)
        except RuntimeError as e:
            QMessageBox.warning(self, "无法开始监控", str(e))
            monitoring = False
        # 以后台实际状态为准，避免 Session 创建失败时界面仍显示“监控中”。
        self.room_list.set_monitoring(room_id, monitoring)
        if monitoring:
            self.status_label.setText(f"监控中: 房间 {room_id}")
        else:
            self.status_label.setText(f"房间 {room_id} 监控未启动，请查看日志")

    def _on_stop_monitor(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            QMessageBox.information(self, "提示", "请先选择一个房间。")
            return
        self._service.stop_monitor(room_id)
        self.room_list.set_monitoring(room_id, False)

    def _on_room_added(self, room_id: int, web_rid: str, display_name: str) -> None:
        self.room_list.add_room(room_id, web_rid, display_name)
        self.status_label.setText(f"已添加房间 {room_id}")

    def _on_task_error(self, room_id: int, error: str) -> None:
        logger.error("房间 %s 监控异常: %s", room_id, error)
        self.status_label.setText(f"房间 {room_id} 异常: {error}")

    def _on_task_stopped(self, room_id: int) -> None:
        self.room_list.set_monitoring(room_id, False)
        self.status_label.setText(f"房间 {room_id} 已停止监控")

    def _on_recording_started(self, room_id: int, snapshot: dict) -> None:
        self.room_list.set_recording(room_id, True)
        self.status_label.setText(f"房间 {room_id} 开始录制")
        logger.info("房间 %s 开始录制: %s", room_id, snapshot.get("file_path"))

    def _on_recording_finished(self, room_id: int, snapshot: dict) -> None:
        self.room_list.set_recording(room_id, False)
        size = snapshot.get("file_size", 0)
        status = snapshot.get("status", "")
        error = snapshot.get("error", "")
        if status == "failed":
            self.status_label.setText(f"房间 {room_id} 录制失败: {error}")
        else:
            self.status_label.setText(
                f"房间 {room_id} 录制结束，文件 {size} 字节"
            )
        logger.info(
            "房间 %s 录制结束 status=%s size=%s file=%s",
            room_id,
            status,
            size,
            snapshot.get("file_path"),
        )

    def _on_recording_error(self, room_id: int, message: str) -> None:
        self.status_label.setText(f"房间 {room_id} 录制错误: {message}")

    def _on_recording_reconnecting(self, room_id: int, snapshot: dict) -> None:
        self.room_list.set_recording(room_id, True)
        self.status_label.setText(
            f"房间 {room_id} 录制中断，"
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
        self.auto_record_toggled.emit(enabled)
        self.status_label.setText("自动录制已开启" if enabled else "自动录制已关闭")

    def _on_show_history(self) -> None:
        rows = self._recording.list_history(limit=200)
        if not rows:
            QMessageBox.information(self, "录制历史", "暂无录制记录。")
            return
        RecordingHistoryDialog(rows, self).exec()

    def _on_open_recordings_dir(self) -> None:
        try:
            self._recording.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "无法打开", f"录制目录不可用：{e}")
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._recording.output_dir))
        )

    def _on_open_settings(self) -> None:
        if self._settings_service is None:
            return
        dialog = SettingsDialog(
            self._settings_service, self._settings_service.load(), self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        saved = dialog.result_settings
        if saved is None:
            return
        if self._on_settings_applied is not None:
            self._on_settings_applied(saved)
        self.status_label.setText("设置已保存")

    def _on_backup_database(self) -> None:
        if self._database is None:
            return
        backup_dir = self._database.db_path.parent / "backups"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "无法备份", f"备份目录不可用：{e}")
            return
        default_name = backup_dir / f"app-backup-{datetime.now():%Y%m%d-%H%M%S}.db"
        path, _ = QFileDialog.getSaveFileName(
            self, "备份数据库", str(default_name), "SQLite 数据库 (*.db)"
        )
        if not path:
            return
        target = Path(path)
        if target.exists():
            answer = QMessageBox.question(
                self,
                "覆盖文件",
                f"目标文件已存在，是否覆盖？\n{target}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            dest = self._database.backup_to(target)
        except Exception as e:  # noqa: BLE001 - 备份失败必须明确反馈
            logger.exception("数据库备份失败")
            QMessageBox.warning(self, "备份失败", f"数据库备份失败：{e}")
            return
        QMessageBox.information(self, "备份完成", f"数据库已备份到：\n{dest}")

    def _on_restore_database(self) -> None:
        if self._database is None:
            return
        start_dir = self._database.db_path.parent / "backups"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择数据库备份",
            str(start_dir),
            "SQLite 数据库 (*.db);;所有文件 (*)",
        )
        if not path:
            return
        backup_path = Path(path)
        ok, message = verify_sqlite(backup_path)
        if not ok:
            QMessageBox.warning(
                self, "无法恢复", f"所选文件不是可用的数据库备份：\n{message}"
            )
            return
        answer = QMessageBox.question(
            self,
            "确认恢复",
            "恢复将在退出并重新启动程序后生效；恢复前会自动为当前数据库创建安全副本。"
            "录制文件不会被删除。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            prepare_restore(backup_path, self._database.db_path.parent)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "无法安排恢复", str(e))
            return
        QMessageBox.information(
            self,
            "已安排恢复",
            "恢复请求已保存。请关闭并重新启动程序以完成恢复。",
        )

    def detach_logging(self) -> None:
        """解除日志桥接，必须在窗口销毁前调用。"""
        self._log_bridge.close()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        logger.info("主窗口关闭")
        self._recording.shutdown()
        self._service.shutdown()
        self.detach_logging()
        super().closeEvent(event)
