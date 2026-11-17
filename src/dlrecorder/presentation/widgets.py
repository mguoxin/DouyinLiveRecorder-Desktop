"""GUI 组件：房间列表、日志展示与设置对话框。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import shiboken6
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..application.settings_service import (
    MAX_MIN_FREE_MB,
    MIN_MONITOR_INTERVAL_S,
    MAX_MONITOR_INTERVAL_S,
    AppSettings,
    SettingsService,
)
from ..domain.models import validate_display_name, validate_web_rid
from ..infrastructure.ffmpeg import probe_ffmpeg

logger = logging.getLogger(__name__)


COLUMNS = ("ID", "直播间ID", "名称", "确认状态", "最近解析", "监控中", "录制中")


class RoomListWidget(QTableWidget):
    """直播间列表表格。"""

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(COLUMNS)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._room_rows: dict[int, int] = {}  # room_id -> row index

    def add_room(self, room_id: int, web_rid: str, display_name: str) -> None:
        if room_id in self._room_rows:
            return
        row = self.rowCount()
        self.insertRow(row)
        self._room_rows[room_id] = row
        self.setItem(row, 0, QTableWidgetItem(str(room_id)))
        self.setItem(row, 1, QTableWidgetItem(web_rid))
        self.setItem(row, 2, QTableWidgetItem(display_name or "(未命名)"))
        self.setItem(row, 3, QTableWidgetItem("未知"))
        self.setItem(row, 4, QTableWidgetItem("—"))
        self.setItem(row, 5, QTableWidgetItem("否"))
        self.setItem(row, 6, QTableWidgetItem("否"))

    def update_room(self, room_id: int, web_rid: str, display_name: str) -> None:
        row = self._room_rows.get(room_id)
        if row is None:
            return
        self.item(row, 1).setText(web_rid)
        self.item(row, 2).setText(display_name or "(未命名)")

    def remove_room(self, room_id: int) -> None:
        row = self._room_rows.pop(room_id, None)
        if row is not None:
            self.removeRow(row)
            # 重新映射后续行号
            for rid, r in list(self._room_rows.items()):
                if r > row:
                    self._room_rows[rid] = r - 1

    def update_state(self, room_id: int, snapshot: dict) -> None:
        row = self._room_rows.get(room_id)
        if row is None:
            return
        confirmed = snapshot.get("last_confirmed_room_state") or "未知"
        parse_result = snapshot.get("last_parse_result") or "—"
        self.item(row, 3).setText(str(confirmed))
        self.item(row, 4).setText(str(parse_result))

    def set_monitoring(self, room_id: int, monitoring: bool) -> None:
        row = self._room_rows.get(room_id)
        if row is not None:
            self.item(row, 5).setText("是" if monitoring else "否")

    def set_recording(self, room_id: int, recording: bool) -> None:
        row = self._room_rows.get(room_id)
        if row is not None:
            self.item(row, 6).setText("是" if recording else "否")

    def selected_room_id(self) -> Optional[int]:
        rows = self.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        item = self.item(row, 0)
        return int(item.text()) if item else None


class LogWidget(QPlainTextEdit):
    """日志展示区，限制最大行数避免无限增长。"""

    def __init__(self, max_blocks: int = 500, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_blocks)

    def append_log(self, message: str) -> None:
        self.appendPlainText(message)


class QtLogBridge(QObject):  # type: ignore[name-defined]
    """将 logging 记录桥接到 Qt 信号，供 GUI 展示。

    处理器会挂到全局 root logger；窗口销毁前必须调用 ``close`` 解除挂载，
    否则后续日志会向已销毁的 QObject 发信号，导致进程崩溃。
    """

    log_emitted = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._closed = False
        self._handler = _QtLogHandler(self)
        logging.getLogger().addHandler(self._handler)

    @property
    def handler(self) -> logging.Handler:
        return self._handler

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logging.getLogger().removeHandler(self._handler)


class _QtLogHandler(logging.Handler):
    def __init__(self, bridge: "QtLogBridge") -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        bridge = self._bridge
        if bridge._closed or not shiboken6.isValid(bridge):
            return
        try:
            msg = self.format(record)
            bridge.log_emitted.emit(msg)
        except Exception:  # noqa: BLE001
            pass


_STATUS_LABELS = {
    "recording": "录制中",
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
}


def _format_size(size: object) -> str:
    try:
        value = int(size or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


class RecordingHistoryDialog(QDialog):
    """只读录制历史列表。不提供删除操作，避免误删用户文件。"""

    def __init__(self, recordings: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("录制历史")
        self.resize(920, 420)

        headers = ("ID", "房间", "开始时间", "结束时间", "状态", "大小", "文件")
        self.table = QTableWidget(len(recordings), len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)

        for row, record in enumerate(recordings):
            display_name = record.get("display_name_snapshot") or ""
            web_rid = record.get("web_rid_snapshot") or ""
            room_text = f"{display_name} ({web_rid})" if display_name else web_rid
            values = (
                str(record.get("id", "")),
                room_text,
                str(record.get("started_at") or "—"),
                str(record.get("ended_at") or "—"),
                _STATUS_LABELS.get(record.get("status", ""), str(record.get("status", ""))),
                _format_size(record.get("file_size")),
                str(record.get("file_path") or ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(buttons)


class RoomEditDialog(QDialog):
    """房间信息编辑对话框，提交前做与后端一致的校验。"""

    def __init__(
        self,
        web_rid: str = "",
        display_name: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑直播间")
        self.setMinimumWidth(360)

        self.web_rid_edit = QLineEdit(web_rid)
        self.name_edit = QLineEdit(display_name)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #c62828;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        form = QFormLayout(self)
        form.addRow("直播间 ID", self.web_rid_edit)
        form.addRow("显示名称", self.name_edit)
        form.addRow(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, str]:
        return self.web_rid_edit.text(), self.name_edit.text()

    def _on_accept(self) -> None:
        web_rid, display_name = self.values()
        try:
            validate_web_rid(web_rid)
            validate_display_name(display_name)
        except ValueError as e:
            self.error_label.setText(str(e))
            self.error_label.show()
            return
        self.accept()


class SettingsDialog(QDialog):
    """软件设置：FFmpeg 路径、录制目录、监控间隔与磁盘保护阈值。"""

    def __init__(
        self,
        service: SettingsService,
        current: AppSettings,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("软件设置")
        self.setMinimumWidth(560)
        self._service = service
        self.result_settings: Optional[AppSettings] = None

        # FFmpeg 路径
        self.ffmpeg_edit = QLineEdit(current.ffmpeg_path)
        self.btn_browse_ffmpeg = QPushButton("浏览...")
        self.btn_detect_ffmpeg = QPushButton("检测")
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_row.addWidget(self.btn_browse_ffmpeg)
        ffmpeg_row.addWidget(self.btn_detect_ffmpeg)
        self.ffmpeg_status = QLabel("未检测")
        self.ffmpeg_status.setWordWrap(True)

        # 录制目录
        self.output_edit = QLineEdit(str(current.recordings_dir))
        self.btn_browse_output = QPushButton("浏览...")
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.btn_browse_output)

        # 监控间隔
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(
            int(MIN_MONITOR_INTERVAL_S), int(MAX_MONITOR_INTERVAL_S)
        )
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setValue(int(round(current.monitor_interval_s)))

        # 磁盘保护阈值
        self.min_free_spin = QSpinBox()
        self.min_free_spin.setRange(0, MAX_MIN_FREE_MB)
        self.min_free_spin.setSingleStep(128)
        self.min_free_spin.setSuffix(" MB")
        self.min_free_spin.setValue(int(current.min_free_mb))

        hint = QLabel(
            "监控间隔与录制目录的修改对之后启动的监控/录制生效；"
            "磁盘最低剩余空间为 0 表示不检查。程序不会自动删除任何录制文件。"
        )
        hint.setWordWrap(True)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #c62828;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        form = QFormLayout(self)
        form.addRow("FFmpeg 可执行文件", ffmpeg_row)
        form.addRow("", self.ffmpeg_status)
        form.addRow("录制目录", output_row)
        form.addRow("监控间隔", self.interval_spin)
        form.addRow("磁盘最低剩余空间", self.min_free_spin)
        form.addRow(hint)
        form.addRow(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.btn_browse_ffmpeg.clicked.connect(self._on_browse_ffmpeg)
        self.btn_detect_ffmpeg.clicked.connect(self._on_detect_ffmpeg)
        self.btn_browse_output.clicked.connect(self._on_browse_output)

    def _on_browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 FFmpeg 可执行文件",
            self.ffmpeg_edit.text(),
            "可执行文件 (*.exe);;所有文件 (*)",
        )
        if path:
            self.ffmpeg_edit.setText(path)
            self.ffmpeg_status.setText("未检测")
            self.ffmpeg_status.setStyleSheet("")

    def _on_detect_ffmpeg(self) -> None:
        ok, message = probe_ffmpeg(self.ffmpeg_edit.text())
        self.ffmpeg_status.setText(("可用：" if ok else "不可用：") + message)
        self.ffmpeg_status.setStyleSheet(
            "color: #2e7d32;" if ok else "color: #c62828;"
        )

    def _on_browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择录制目录", self.output_edit.text()
        )
        if path:
            self.output_edit.setText(path)

    def _on_accept(self) -> None:
        candidate = AppSettings(
            ffmpeg_path=self.ffmpeg_edit.text(),
            recordings_dir=Path(self.output_edit.text()),
            monitor_interval_s=float(self.interval_spin.value()),
            min_free_mb=self.min_free_spin.value(),
        )
        try:
            saved = self._service.save(candidate)
        except ValueError as e:
            self.error_label.setText(str(e))
            self.error_label.show()
            return
        self.result_settings = saved
        self.accept()
