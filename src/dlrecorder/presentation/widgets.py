"""GUI 组件：房间列表、日志、对话框与设置面板。

所有视觉颜色来自 :mod:`theme` 的语义令牌，不在控件内写死主题色。
"""
from __future__ import annotations

import html
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import shiboken6
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..application.settings_service import (
    MAX_MIN_FREE_MB,
    MIN_MONITOR_INTERVAL_S,
    MAX_MONITOR_INTERVAL_S,
    QUALITY_OPTIONS,
    AppSettings,
    SettingsService,
)
from ..domain.models import validate_display_name, validate_web_rid
from ..infrastructure.ffmpeg import probe_ffmpeg
from . import icons
from .theme import (
    LIGHT,
    MODE_LABELS,
    MODE_SYSTEM,
    ThemeManager,
    ThemeTokens,
    app_is_dark,
    apply_title_bar,
)

logger = logging.getLogger(__name__)


COLUMNS = ("ID", "直播间 ID", "名称", "直播状态", "最近解析", "监控", "录制")

_ROOM_STATE_LABELS = {
    "LIVE": "直播中",
    "OFFLINE_CONFIRMED": "未开播",
    "OFFLINE": "未开播",
    "UNKNOWN": "未知",
}
_PARSE_LABELS = {
    "SUCCESS": "正常",
    "FAILED": "失败",
    "RISK_CONTROL": "风控验证",
    "TIMEOUT": "超时",
    "OFFLINE": "未开播",
}


# ---------------------------------------------------------------------------
# 通用对话框辅助：中文按钮、非原生文件选择、统一消息框
# ---------------------------------------------------------------------------
def localize_dialog_buttons(box: QDialogButtonBox) -> None:
    mapping = {
        QDialogButtonBox.StandardButton.Ok: "确定",
        QDialogButtonBox.StandardButton.Cancel: "取消",
        QDialogButtonBox.StandardButton.Close: "关闭",
        QDialogButtonBox.StandardButton.Save: "保存",
        QDialogButtonBox.StandardButton.Yes: "是",
        QDialogButtonBox.StandardButton.No: "否",
        QDialogButtonBox.StandardButton.Apply: "应用",
    }
    for standard, text in mapping.items():
        button = box.button(standard)
        if button is not None:
            button.setText(text)


def show_info(parent: Optional[QWidget], title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    button = box.button(QMessageBox.StandardButton.Ok)
    if button is not None:
        button.setText("确定")
    apply_title_bar(box, app_is_dark())
    box.exec()


def show_warning(parent: Optional[QWidget], title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    button = box.button(QMessageBox.StandardButton.Ok)
    if button is not None:
        button.setText("知道了")
    apply_title_bar(box, app_is_dark())
    box.exec()


def ask_confirm(
    parent: Optional[QWidget],
    title: str,
    text: str,
    *,
    ok_text: str = "继续",
    cancel_text: str = "取消",
    danger: bool = False,
    informative: str = "",
) -> bool:
    box = QMessageBox(parent)
    box.setIcon(
        QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question
    )
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    yes = box.button(QMessageBox.StandardButton.Yes)
    no = box.button(QMessageBox.StandardButton.No)
    if yes is not None:
        yes.setText(ok_text)
        if danger:
            yes.setProperty("variant", "danger")
            yes.style().unpolish(yes)
            yes.style().polish(yes)
    if no is not None:
        no.setText(cancel_text)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    apply_title_bar(box, app_is_dark())
    return box.exec() == QMessageBox.StandardButton.Yes


def _prepare_file_dialog(dialog: QFileDialog, accept_text: str) -> QFileDialog:
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setLabelText(QFileDialog.DialogLabel.Accept, accept_text)
    dialog.setLabelText(QFileDialog.DialogLabel.Reject, "取消")
    apply_title_bar(dialog, app_is_dark())
    return dialog


def choose_existing_file(
    parent: Optional[QWidget],
    title: str,
    directory: str,
    filters: list[str],
) -> str:
    dialog = _prepare_file_dialog(
        QFileDialog(parent, title, str(directory or "")), "选择"
    )
    dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    dialog.setNameFilters(filters)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selectedFiles():
        return dialog.selectedFiles()[0]
    return ""


def choose_save_file(
    parent: Optional[QWidget],
    title: str,
    path: str,
    filters: list[str],
) -> str:
    dialog = _prepare_file_dialog(
        QFileDialog(parent, title, str(path or "")), "保存"
    )
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setNameFilters(filters)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selectedFiles():
        return dialog.selectedFiles()[0]
    return ""


def choose_directory(
    parent: Optional[QWidget],
    title: str,
    directory: str,
) -> str:
    dialog = _prepare_file_dialog(
        QFileDialog(parent, title, str(directory or "")), "选择文件夹"
    )
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selectedFiles():
        return dialog.selectedFiles()[0]
    return ""


# ---------------------------------------------------------------------------
# 房间列表
# ---------------------------------------------------------------------------
class _StatusPillDelegate(QStyledItemDelegate):
    """把“是/否”绘制成状态胶囊，同时保留原始单元格文本供逻辑使用。"""

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._tokens: ThemeTokens = LIGHT

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[override]
        tokens = self._tokens
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            background = QColor(tokens.selection)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            background = QColor(tokens.hover)
        else:
            background = QColor(tokens.surface)
        painter.fillRect(rect, background)
        painter.setPen(QPen(QColor(tokens.border), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        active = index.data(Qt.ItemDataRole.DisplayRole) == "是"
        if self._kind == "monitor":
            label = "监控中" if active else "未监控"
            foreground = tokens.success if active else tokens.text_muted
            soft = tokens.success_soft if active else tokens.surface_alt
        else:
            label = "录制中" if active else "未录制"
            foreground = tokens.accent_fg if active else tokens.text_muted
            soft = tokens.accent_soft if active else tokens.surface_alt

        metrics = option.fontMetrics
        width = metrics.horizontalAdvance(label) + 26
        height = 22
        pill = QRectF(
            rect.left() + 10,
            rect.center().y() - height / 2 + 0.5,
            width,
            height,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(soft))
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setBrush(QColor(foreground))
        painter.drawEllipse(
            QPointF(pill.left() + 10, pill.center().y()), 3.0, 3.0
        )
        painter.setPen(QColor(foreground))
        painter.drawText(
            QRectF(
                pill.left() + 18,
                pill.top(),
                pill.width() - 22,
                pill.height(),
            ),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            label,
        )
        painter.restore()


class RoomListWidget(QTableWidget):
    """直播间列表表格。列结构与对外方法保持兼容。"""

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(COLUMNS), parent)
        self.setObjectName("RoomTable")
        self.setHorizontalHeaderLabels(COLUMNS)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(44)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        header.setStretchLastSection(False)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 0)
        self.setColumnWidth(1, 148)
        self.setColumnWidth(3, 84)
        self.setColumnWidth(4, 92)
        self.setColumnWidth(5, 92)
        self.setColumnWidth(6, 92)
        self.hideColumn(0)

        self._monitor_delegate = _StatusPillDelegate("monitor", self)
        self._record_delegate = _StatusPillDelegate("record", self)
        self.setItemDelegateForColumn(5, self._monitor_delegate)
        self.setItemDelegateForColumn(6, self._record_delegate)

        self._room_rows: dict[int, int] = {}  # room_id -> row index

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._monitor_delegate.set_theme_tokens(tokens)
        self._record_delegate.set_theme_tokens(tokens)
        self.viewport().update()

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
        self.item(row, 0).setToolTip("内部房间 ID")
        self.item(row, 1).setToolTip(web_rid)

    def update_room(self, room_id: int, web_rid: str, display_name: str) -> None:
        row = self._room_rows.get(room_id)
        if row is None:
            return
        self.item(row, 1).setText(web_rid)
        self.item(row, 1).setToolTip(web_rid)
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
        confirmed_raw = str(snapshot.get("last_confirmed_room_state") or "UNKNOWN")
        parse_raw = str(snapshot.get("last_parse_result") or "")
        confirmed_item = self.item(row, 3)
        confirmed_item.setText(
            _ROOM_STATE_LABELS.get(confirmed_raw, confirmed_raw or "未知")
        )
        confirmed_item.setToolTip(confirmed_raw)

        parse_item = self.item(row, 4)
        if parse_raw == "RISK_CONTROL":
            parse_item.setText("风控验证")
            parse_item.setToolTip(
                "抖音返回安全验证页面，暂时无法获取直播状态；监控会按间隔自动重试。"
                "可先在浏览器中打开抖音完成验证，稍后恢复。"
            )
        else:
            parse_item.setText(_PARSE_LABELS.get(parse_raw, parse_raw or "—"))
            parse_item.setToolTip(parse_raw)

    def set_monitoring(self, room_id: int, monitoring: bool) -> None:
        row = self._room_rows.get(room_id)
        if row is not None:
            self.item(row, 5).setText("是" if monitoring else "否")

    def set_recording(self, room_id: int, recording: bool) -> None:
        row = self._room_rows.get(room_id)
        if row is not None:
            self.item(row, 6).setText("是" if recording else "否")

    def monitoring(self, room_id: int) -> bool:
        row = self._room_rows.get(room_id)
        return bool(row is not None and self.item(row, 5).text() == "是")

    def recording(self, room_id: int) -> bool:
        row = self._room_rows.get(room_id)
        return bool(row is not None and self.item(row, 6).text() == "是")

    def selected_room_id(self) -> Optional[int]:
        rows = self.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        item = self.item(row, 0)
        return int(item.text()) if item else None

    def selected_display_name(self) -> str:
        rows = self.selectionModel().selectedRows()
        if not rows:
            return ""
        item = self.item(rows[0].row(), 2)
        return item.text() if item else ""


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class LogWidget(QPlainTextEdit):
    """日志展示区，限制最大行数并按级别着色。"""

    def __init__(self, max_blocks: int = 500, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_blocks)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        font = self.font()
        font.setFamily("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self._messages: deque[tuple[Optional[int], str]] = deque(maxlen=max_blocks)
        self._tokens: ThemeTokens = LIGHT

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self._rerender()

    def append_log(self, levelno_or_message, message: Optional[str] = None) -> None:
        """兼容两种调用：``append_log(text)`` 与信号 ``(levelno, text)``。"""
        if message is None:
            levelno: Optional[int] = None
            text = str(levelno_or_message)
        else:
            levelno = int(levelno_or_message)
            text = message
        self._messages.append((levelno, text))
        self._append_rendered(levelno, text)

    def clear_log(self) -> None:
        self._messages.clear()
        self.clear()

    def _level_color(self, levelno: Optional[int], message: str) -> str:
        if levelno is not None:
            if levelno >= logging.ERROR:
                return self._tokens.danger
            if levelno >= logging.WARNING:
                return self._tokens.warning
            return self._tokens.text_secondary
        upper = message.upper()
        if "CRITICAL" in upper or "ERROR" in upper:
            return self._tokens.danger
        if "WARNING" in upper or " WARN " in upper:
            return self._tokens.warning
        return self._tokens.text_secondary

    def _append_rendered(self, levelno: Optional[int], message: str) -> None:
        color = self._level_color(levelno, message)
        escaped = html.escape(message)
        self.appendHtml(f'<span style="color:{color};">{escaped}</span>')

    def _rerender(self) -> None:
        self.clear()
        for levelno, message in self._messages:
            self._append_rendered(levelno, message)


class QtLogBridge(QObject):  # type: ignore[name-defined]
    """将 logging 记录桥接到 Qt 信号，供 GUI 展示。

    处理器会挂到全局 root logger；窗口销毁前必须调用 ``close`` 解除挂载，
    否则后续日志会向已销毁的 QObject 发信号，导致进程崩溃。
    """

    log_emitted = Signal(int, str)

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
            bridge.log_emitted.emit(record.levelno, msg)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 录制历史
# ---------------------------------------------------------------------------
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


def status_label(status: object) -> str:
    text = str(status or "")
    return _STATUS_LABELS.get(text, text or "—")


def _format_time(value: object) -> str:
    """数据库存 UTC ISO 时间，界面按本机时区展示。"""
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return text
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def populate_history_table(table: QTableWidget, recordings: list[dict]) -> None:
    """填充只读历史表格（列顺序与测试契约一致）。"""
    table.setRowCount(len(recordings))
    for row, record in enumerate(recordings):
        display_name = record.get("display_name_snapshot") or ""
        web_rid = record.get("web_rid_snapshot") or ""
        room_text = f"{display_name} ({web_rid})" if display_name else web_rid
        values = (
            str(record.get("id", "")),
            room_text,
            _format_time(record.get("started_at")),
            _format_time(record.get("ended_at")),
            status_label(record.get("status", "")),
            _format_size(record.get("file_size")),
            str(record.get("file_path") or ""),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            table.setItem(row, column, item)


def style_history_table(table: QTableWidget) -> None:
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(False)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(40)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionsClickable(False)
    header.setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    table.setColumnHidden(0, True)


class ThemedDialog(QDialog):
    """显示时同步原生标题栏深浅色，避免深色主题下出现白色标题栏。"""

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        apply_title_bar(self, app_is_dark())


class RecordingHistoryDialog(ThemedDialog):
    """只读录制历史列表。不提供删除操作，避免误删用户文件。"""

    def __init__(self, recordings: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("录制历史")
        self.resize(980, 480)

        headers = ("ID", "房间", "开始时间", "结束时间", "状态", "大小", "文件")
        self.table = QTableWidget(len(recordings), len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        style_history_table(self.table)
        populate_history_table(self.table, recordings)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        localize_dialog_buttons(buttons)
        buttons.rejected.connect(self.accept)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(1, 1, 1, 1)
        card_layout.addWidget(self.table)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(card)
        layout.addWidget(buttons)


# ---------------------------------------------------------------------------
# 房间编辑 / 添加
# ---------------------------------------------------------------------------
class AddRoomDialog(ThemedDialog):
    """单对话框完成添加直播间，替代连续两次输入框。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加直播间")
        self.setMinimumWidth(480)

        title = QLabel("添加直播间")
        title.setObjectName("PageTitle")
        subtitle = QLabel("粘贴抖音直播间链接或直接输入房间号，例如 123456789。")
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)

        self.web_rid_edit = QLineEdit()
        self.web_rid_edit.setPlaceholderText(
            "https://live.douyin.com/123456789 或 123456789"
        )
        self.web_rid_edit.setClearButtonEnabled(True)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("可选，用于在列表中区分主播")
        self.name_edit.setClearButtonEnabled(True)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        form = QFormLayout()
        form.setSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("直播间链接或 ID", self.web_rid_edit)
        form.addRow("显示名称", self.name_edit)
        form.addRow(self.error_label)

        hint = QLabel(
            "短链接（v.douyin.com）请先在浏览器中打开，再复制展开后的直播间链接。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        localize_dialog_buttons(buttons)
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setText("添加")
            ok_button.setProperty("variant", "primary")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        self.web_rid_edit.setFocus()

    def _on_accept(self) -> None:
        try:
            validate_web_rid(self.web_rid_edit.text())
            validate_display_name(self.name_edit.text())
        except ValueError as e:
            self.error_label.setText(str(e))
            self.error_label.show()
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return self.web_rid_edit.text(), self.name_edit.text()


class RoomEditDialog(ThemedDialog):
    """房间信息编辑对话框，提交前做与后端一致的校验。"""

    def __init__(
        self,
        web_rid: str = "",
        display_name: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑直播间")
        self.setMinimumWidth(420)

        self.web_rid_edit = QLineEdit(web_rid)
        self.name_edit = QLineEdit(display_name)
        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        form = QFormLayout(self)
        form.setContentsMargins(20, 18, 20, 18)
        form.setSpacing(10)
        form.addRow("直播间 ID 或链接", self.web_rid_edit)
        form.addRow("显示名称", self.name_edit)
        form.addRow(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        localize_dialog_buttons(buttons)
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


# ---------------------------------------------------------------------------
# 自动录制开关
# ---------------------------------------------------------------------------
class ToggleSwitch(QAbstractButton):
    """轻量自绘开关：比默认复选框更清晰地表达全局开关状态。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tokens: ThemeTokens = LIGHT
        self.toggled.connect(lambda _checked: self.update())

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.update()

    def sizeHint(self) -> QSize:  # type: ignore[override]
        metrics = self.fontMetrics()
        return QSize(40 + 8 + metrics.horizontalAdvance(self.text()) + 2, 26)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tokens = self._tokens
        checked = self.isChecked()
        enabled = self.isEnabled()

        track = QRectF(1.0, 5.0, 34.0, 16.0)
        if not enabled:
            track_color = QColor(tokens.surface_alt)
            knob_color = QColor(tokens.text_muted)
            text_color = QColor(tokens.text_muted)
        elif checked:
            track_color = QColor(tokens.accent)
            knob_color = QColor("#FFFFFF")
            text_color = QColor(tokens.text)
        else:
            track_color = QColor(tokens.border_strong)
            knob_color = QColor(tokens.surface)
            text_color = QColor(tokens.text_secondary)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 8.0, 8.0)
        knob_x = 26.0 if checked else 10.0
        painter.setBrush(knob_color)
        painter.drawEllipse(QPointF(knob_x, 13.0), 6.0, 6.0)

        painter.setPen(text_color)
        painter.drawText(
            QRectF(44.0, 0.0, self.width() - 44.0, self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self.text(),
        )


# ---------------------------------------------------------------------------
# 设置面板
# ---------------------------------------------------------------------------
class SettingsPanel(QWidget):
    """设置项集合；供设置页与兼容对话框共用。"""

    def __init__(
        self,
        service: SettingsService,
        current: AppSettings,
        *,
        theme_manager: Optional[ThemeManager] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._theme_manager = theme_manager
        self.result_settings: Optional[AppSettings] = None
        self.theme_buttons: dict[str, QPushButton] = {}
        self.theme_note: Optional[QLabel] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        if theme_manager is not None:
            outer.addWidget(self._build_appearance_card())
        outer.addWidget(self._build_recording_card(current))
        outer.addWidget(self._build_storage_card(current))
        outer.addWidget(self._build_account_card(current))

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        outer.addWidget(self.error_label)
        outer.addStretch(1)

        self.btn_browse_ffmpeg.clicked.connect(self._on_browse_ffmpeg)
        self.btn_detect_ffmpeg.clicked.connect(self._on_detect_ffmpeg)
        self.btn_browse_output.clicked.connect(self._on_browse_output)
        self.btn_import_cookie.clicked.connect(self._on_import_cookie)
        self.btn_clear_cookie.clicked.connect(self._on_clear_cookie)
        self.chk_show_cookie.toggled.connect(self._on_toggle_cookie_visible)

    # ---------- 卡片构建 ----------
    def _card(self, title: str, description: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("SectionTitle")
        layout.addWidget(title_label)
        if description:
            desc = QLabel(description)
            desc.setObjectName("Secondary")
            desc.setWordWrap(True)
            layout.addWidget(desc)
        return card, layout

    def _build_appearance_card(self) -> QFrame:
        card, layout = self._card(
            "外观", "主题会立即生效，并记住下次启动的选择。"
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        group = QButtonGroup(self)
        group.setExclusive(True)
        assert self._theme_manager is not None
        for mode in ("system", "light", "dark"):
            button = QPushButton(MODE_LABELS[mode])
            button.setCheckable(True)
            button.setProperty("segment", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if self._theme_manager.mode == mode:
                button.setChecked(True)
            button.clicked.connect(
                lambda _checked=False, value=mode: self._on_theme_selected(value)
            )
            group.addButton(button)
            self.theme_buttons[mode] = button
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        self.theme_note = QLabel("")
        self.theme_note.setObjectName("Muted")
        self.theme_note.setWordWrap(True)
        layout.addWidget(self.theme_note)
        self._refresh_theme_note()
        return card

    def _build_recording_card(self, current: AppSettings) -> QFrame:
        card, layout = self._card("录制", "修改对之后启动的监控与录制生效。")
        form = QFormLayout()
        form.setSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.ffmpeg_edit = QLineEdit(current.ffmpeg_path)
        self.btn_browse_ffmpeg = QPushButton("浏览...")
        self.btn_detect_ffmpeg = QPushButton("检测")
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.setSpacing(8)
        ffmpeg_row.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_row.addWidget(self.btn_browse_ffmpeg)
        ffmpeg_row.addWidget(self.btn_detect_ffmpeg)
        self.ffmpeg_status = QLabel("未检测")
        self.ffmpeg_status.setObjectName("Muted")
        self.ffmpeg_status.setWordWrap(True)

        self.quality_combo = QComboBox()
        for code, label in QUALITY_OPTIONS:
            self.quality_combo.addItem(label, code)
        index = self.quality_combo.findData(current.quality)
        self.quality_combo.setCurrentIndex(index if index >= 0 else 0)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(
            int(MIN_MONITOR_INTERVAL_S), int(MAX_MONITOR_INTERVAL_S)
        )
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.lineEdit().setTextMargins(8, 0, 8, 0)
        self.interval_spin.setValue(int(round(current.monitor_interval_s)))

        form.addRow("FFmpeg 可执行文件", ffmpeg_row)
        form.addRow("", self.ffmpeg_status)
        form.addRow("录制清晰度", self.quality_combo)
        form.addRow("监控间隔", self.interval_spin)
        layout.addLayout(form)
        return card

    def _build_storage_card(self, current: AppSettings) -> QFrame:
        card, layout = self._card(
            "存储", "录制目录与磁盘保护阈值；程序不会自动删除任何录制文件。"
        )
        form = QFormLayout()
        form.setSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.output_edit = QLineEdit(str(current.recordings_dir))
        self.btn_browse_output = QPushButton("浏览...")
        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.btn_browse_output)

        self.min_free_spin = QSpinBox()
        self.min_free_spin.setRange(0, MAX_MIN_FREE_MB)
        self.min_free_spin.setSingleStep(128)
        self.min_free_spin.setSuffix(" MB")
        self.min_free_spin.lineEdit().setTextMargins(8, 0, 8, 0)
        self.min_free_spin.setValue(int(current.min_free_mb))

        form.addRow("录制目录", output_row)
        form.addRow("磁盘最低剩余空间", self.min_free_spin)
        layout.addLayout(form)

        hint = QLabel("最低剩余空间为 0 表示不检查。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)
        return card

    def _build_account_card(self, current: AppSettings) -> QFrame:
        del current
        card, layout = self._card(
            "账户", "Cookie 使用 Windows DPAPI 加密保存，仅本机当前用户可解密。"
        )
        form = QFormLayout()
        form.setSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.cookie_edit = QLineEdit()
        self.cookie_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie_edit.setPlaceholderText("留空表示不修改；已保存的 Cookie 不会回显")
        self.btn_import_cookie = QPushButton("导入文件...")
        self.btn_clear_cookie = QPushButton("清除")
        self.btn_clear_cookie.setProperty("variant", "danger")
        self.chk_show_cookie = QCheckBox("显示")
        cookie_row = QHBoxLayout()
        cookie_row.setSpacing(8)
        cookie_row.addWidget(self.cookie_edit, 1)
        cookie_row.addWidget(self.btn_import_cookie)
        cookie_row.addWidget(self.chk_show_cookie)
        cookie_row.addWidget(self.btn_clear_cookie)
        self.cookie_status = QLabel(self._service.cookie_status())
        self.cookie_status.setWordWrap(True)
        self.cookie_status.setObjectName("Secondary")

        form.addRow("抖音 Cookie", cookie_row)
        form.addRow("", self.cookie_status)
        layout.addLayout(form)

        hint = QLabel(
            "未配置时按匿名访问；Cookie 不会写入数据库、日志或发布包。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return card

    # ---------- 外观 ----------
    def _on_theme_selected(self, mode: str) -> None:
        if self._theme_manager is None:
            return
        try:
            self._theme_manager.set_mode(mode)
        except ValueError as e:
            self._show_error(str(e))
            return
        self._refresh_theme_note()

    def _refresh_theme_note(self) -> None:
        if self._theme_manager is None or self.theme_note is None:
            return
        if self._theme_manager.mode == MODE_SYSTEM:
            actual = "深色" if self._theme_manager.is_dark else "浅色"
            self.theme_note.setText(
                f"当前跟随 Windows 系统，实际使用{actual}主题；系统切换时自动更新。"
            )
        else:
            self.theme_note.setText("已使用手动主题，不随 Windows 系统切换。")
        for mode, button in self.theme_buttons.items():
            button.setChecked(mode == self._theme_manager.mode)

    def sync_theme(self) -> None:
        self._refresh_theme_note()

    # ---------- Cookie ----------
    def _refresh_cookie_status(self) -> None:
        self.cookie_status.setText(self._service.cookie_status())

    def _on_toggle_cookie_visible(self, visible: bool) -> None:
        self.cookie_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )

    def _on_import_cookie(self) -> None:
        path = choose_existing_file(
            self,
            "选择 Cookie 文本文件",
            "",
            ["文本文件 (*.txt *.json)", "所有文件 (*)"],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._show_error(f"读取 Cookie 文件失败: {e}")
            return
        self.cookie_edit.setText(text.strip()[:8192])

    def _on_clear_cookie(self) -> None:
        try:
            self._service.clear_cookie()
        except OSError as e:
            self._show_error(f"清除 Cookie 失败: {e}")
            return
        self.cookie_edit.clear()
        self._refresh_cookie_status()

    def _save_cookie_if_needed(self) -> bool:
        text = self.cookie_edit.text().strip()
        if not text:
            return True
        try:
            self._service.save_cookie(text)
        except (ValueError, OSError, RuntimeError) as e:
            self._show_error(f"Cookie 保存失败: {e}")
            return False
        self.cookie_edit.clear()
        self._refresh_cookie_status()
        return True

    # ---------- 路径 ----------
    def _on_browse_ffmpeg(self) -> None:
        path = choose_existing_file(
            self,
            "选择 FFmpeg 可执行文件",
            self.ffmpeg_edit.text(),
            ["可执行文件 (*.exe)", "所有文件 (*)"],
        )
        if path:
            self.ffmpeg_edit.setText(path)
            self.ffmpeg_status.setText("未检测")
            self._set_status_role(self.ffmpeg_status, "")

    def _on_detect_ffmpeg(self) -> None:
        ok, message = probe_ffmpeg(self.ffmpeg_edit.text())
        self.ffmpeg_status.setText(("可用：" if ok else "不可用：") + message)
        self._set_status_role(
            self.ffmpeg_status, "SuccessLabel" if ok else "ErrorLabel"
        )

    def _on_browse_output(self) -> None:
        path = choose_directory(
            self, "选择录制目录", self.output_edit.text()
        )
        if path:
            self.output_edit.setText(path)

    @staticmethod
    def _set_status_role(label: QLabel, object_name: str) -> None:
        label.setObjectName(object_name)
        label.style().unpolish(label)
        label.style().polish(label)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    # ---------- 保存 ----------
    def save(self) -> Optional[AppSettings]:
        self.error_label.hide()
        if not self._save_cookie_if_needed():
            return None
        candidate = AppSettings(
            ffmpeg_path=self.ffmpeg_edit.text(),
            recordings_dir=Path(self.output_edit.text()),
            monitor_interval_s=float(self.interval_spin.value()),
            min_free_mb=self.min_free_spin.value(),
            quality=self.quality_combo.currentData(),
        )
        try:
            saved = self._service.save(candidate)
        except (ValueError, OSError) as e:
            self._show_error(str(e))
            return None
        self.result_settings = saved
        return saved


class SettingsDialog(ThemedDialog):
    """兼容旧入口的设置对话框；设置页复用同一个 :class:`SettingsPanel`。"""

    def __init__(
        self,
        service: SettingsService,
        current: AppSettings,
        parent=None,
        *,
        theme_manager: Optional[ThemeManager] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("软件设置")
        self.resize(700, 780)
        self.setMinimumSize(600, 520)
        self.result_settings: Optional[AppSettings] = None

        self.panel = SettingsPanel(
            service, current, theme_manager=theme_manager, parent=self
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.panel)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        localize_dialog_buttons(buttons)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addWidget(buttons)

        self.error_label = self.panel.error_label

    # 测试与旧代码依赖的控件别名
    @property
    def ffmpeg_edit(self) -> QLineEdit:
        return self.panel.ffmpeg_edit

    @property
    def ffmpeg_status(self) -> QLabel:
        return self.panel.ffmpeg_status

    @property
    def output_edit(self) -> QLineEdit:
        return self.panel.output_edit

    @property
    def interval_spin(self) -> QSpinBox:
        return self.panel.interval_spin

    @property
    def min_free_spin(self) -> QSpinBox:
        return self.panel.min_free_spin

    @property
    def quality_combo(self) -> QComboBox:
        return self.panel.quality_combo

    @property
    def cookie_edit(self) -> QLineEdit:
        return self.panel.cookie_edit

    @property
    def cookie_status(self) -> QLabel:
        return self.panel.cookie_status

    @property
    def chk_show_cookie(self) -> QCheckBox:
        return self.panel.chk_show_cookie

    def _on_clear_cookie(self) -> None:
        self.panel._on_clear_cookie()

    def _on_accept(self) -> None:
        saved = self.panel.save()
        if saved is None:
            return
        self.result_settings = saved
        self.accept()


# ---------------------------------------------------------------------------
# 空状态
# ---------------------------------------------------------------------------
class EmptyState(QWidget):
    """列表为空时的引导视图。"""

    action_requested = Signal()

    def __init__(
        self,
        title: str,
        text: str,
        *,
        action_text: str = "",
        action_icon: str = "add",
        icon_name: str = "empty-room",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._action_text = action_text
        self._action_icon = action_icon
        self._tokens: ThemeTokens = LIGHT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 48, 32, 48)
        layout.setSpacing(10)
        layout.addStretch(1)

        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("EmptyTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)

        self.text_label = QLabel(text)
        self.text_label.setObjectName("EmptyText")
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label.setWordWrap(True)
        layout.addWidget(self.text_label)

        self.action_button = QPushButton(action_text)
        self.action_button.setProperty("variant", "primary")
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.clicked.connect(self.action_requested.emit)
        self.action_button.setVisible(bool(action_text))
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.action_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        layout.addStretch(1)

        self.set_theme_tokens(LIGHT)

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.icon_label.setPixmap(icons.pixmap(self._icon_name, tokens.text_muted, 44))
        if self._action_text:
            self.action_button.setIcon(icons.icon(self._action_icon, "#FFFFFF", 16))
