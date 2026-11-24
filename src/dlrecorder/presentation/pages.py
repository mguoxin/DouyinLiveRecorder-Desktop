"""主窗口页面：侧边栏、直播间、录制历史、运行日志与设置。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..application.settings_service import AppSettings, SettingsService
from . import icons
from .theme import LIGHT, ThemeManager, ThemeTokens
from .widgets import (
    EmptyState,
    LogWidget,
    RoomListWidget,
    SettingsPanel,
    ToggleSwitch,
    populate_history_table,
    style_history_table,
)


def _title_block(title: str, subtitle: str) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    title_label = QLabel(title)
    title_label.setObjectName("PageTitle")
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("PageSubtitle")
    subtitle_label.setWordWrap(True)
    layout.addWidget(title_label)
    layout.addWidget(subtitle_label)
    return layout


def _page_header(
    title: str, subtitle: str, actions: list[QWidget]
) -> QWidget:
    header = QWidget()
    layout = QHBoxLayout(header)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addLayout(_title_block(title, subtitle), 1)
    for action in actions:
        layout.addWidget(action)
    return header


def _divider() -> QFrame:
    divider = QFrame()
    divider.setObjectName("VDivider")
    divider.setFixedWidth(1)
    divider.setFixedHeight(22)
    return divider


def _card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(1, 1, 1, 1)
    layout.setSpacing(0)
    return card, layout


class Sidebar(QWidget):
    """左侧固定导航：品牌、页面入口与主题状态。"""

    page_selected = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(188)
        self._tokens: ThemeTokens = LIGHT
        self._nav_buttons: list[QPushButton] = []
        self._nav_specs = (
            ("rooms", "直播间"),
            ("history", "录制历史"),
            ("logs", "运行日志"),
            ("settings", "设置"),
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)

        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(4, 0, 0, 0)
        brand_layout.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icons.app_icon().pixmap(30, 30))
        logo.setFixedSize(30, 30)
        brand_layout.addWidget(logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel("抖音直播录制")
        title.setObjectName("BrandTitle")
        subtitle = QLabel("Desktop Recorder")
        subtitle.setObjectName("BrandSubtitle")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_layout.addLayout(brand_text, 1)
        layout.addWidget(brand)
        layout.addSpacing(10)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, (icon_name, label) in enumerate(self._nav_specs):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("nav", True)
            button.setIconSize(QSize(18, 18))
            button.setMinimumHeight(38)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, page=index: self.page_selected.emit(page)
            )
            self._group.addButton(button, index)
            self._nav_buttons.append(button)
            layout.addWidget(button)

        layout.addStretch(1)
        footer_divider = QFrame()
        footer_divider.setObjectName("Divider")
        footer_divider.setFixedHeight(1)
        layout.addWidget(footer_divider)
        layout.addSpacing(6)
        self.theme_label = QLabel("外观：跟随系统")
        self.theme_label.setObjectName("Muted")
        self.theme_label.setWordWrap(True)
        layout.addWidget(self.theme_label)
        self.tray_label = QLabel("")
        self.tray_label.setObjectName("Muted")
        self.tray_label.setWordWrap(True)
        self.tray_label.hide()
        layout.addWidget(self.tray_label)
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("Muted")
        layout.addWidget(self.version_label)

    def set_current(self, index: int) -> None:
        button = self._group.button(index)
        if button is not None:
            button.setChecked(True)

    def set_theme_note(self, text: str) -> None:
        self.theme_label.setText(f"外观：{text}")

    def set_tray_note(self, text: str) -> None:
        self.tray_label.setText(text)
        self.tray_label.setVisible(bool(text))

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        for button, (icon_name, _label) in zip(self._nav_buttons, self._nav_specs):
            button.setIcon(
                icons.stateful_icon(
                    icon_name,
                    tokens.text_secondary,
                    tokens.accent_fg,
                    tokens.text_muted,
                    18,
                )
            )


class RoomsPage(QWidget):
    """直播间管理页。"""

    add_requested = Signal()
    edit_requested = Signal()
    remove_requested = Signal()
    monitor_toggle_requested = Signal()
    record_toggle_requested = Signal()
    open_dir_requested = Signal()
    auto_record_changed = Signal(bool)
    selection_changed = Signal()

    def __init__(self, auto_record: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._tokens: ThemeTokens = LIGHT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)

        self.btn_add = QPushButton("添加直播间")
        self.btn_add.setProperty("variant", "primary")
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_dir = QPushButton("打开录制目录")
        self.btn_open_dir.setProperty("variant", "ghost")
        self.btn_open_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(
            _page_header(
                "直播间",
                "集中查看直播状态、监控与录制进度。",
                [self.btn_open_dir, self.btn_add],
            )
        )

        action_card, action_layout = _card()
        action_row = QHBoxLayout()
        action_row.setContentsMargins(12, 8, 12, 8)
        action_row.setSpacing(8)
        self.selection_label = QLabel("选择直播间后可执行操作")
        self.selection_label.setObjectName("Secondary")
        self.selection_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.selection_label.setMinimumWidth(0)
        action_row.addWidget(self.selection_label, 1)

        self.btn_edit = QPushButton("编辑")
        self.btn_remove = QPushButton("删除")
        self.btn_remove.setProperty("variant", "danger")
        self.btn_monitor = QPushButton("开始监控")
        self.btn_record = QPushButton("开始录制")
        for button in (
            self.btn_edit,
            self.btn_remove,
            self.btn_monitor,
            self.btn_record,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        action_row.addWidget(self.btn_edit)
        action_row.addWidget(self.btn_remove)
        action_row.addWidget(_divider())
        action_row.addWidget(self.btn_monitor)
        action_row.addWidget(self.btn_record)
        action_row.addWidget(_divider())
        self.chk_auto_record = ToggleSwitch("自动录制")
        self.chk_auto_record.setChecked(auto_record)
        action_row.addWidget(self.chk_auto_record)
        action_layout.addLayout(action_row)
        layout.addWidget(action_card)

        content_card, content_layout = _card()
        self.room_list = RoomListWidget()
        self.empty_state = EmptyState(
            "还没有直播间",
            "添加一个抖音直播间，开始监控开播状态并自动录制。",
            action_text="添加直播间",
        )
        self.stack = QStackedWidget()
        self.stack.addWidget(self.room_list)
        self.stack.addWidget(self.empty_state)
        content_layout.addWidget(self.stack)
        layout.addWidget(content_card, 1)

        hint = QLabel("双击房间可编辑，右键查看更多操作。录制文件不会被自动删除。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        self.btn_add.clicked.connect(self.add_requested.emit)
        self.btn_open_dir.clicked.connect(self.open_dir_requested.emit)
        self.btn_edit.clicked.connect(self.edit_requested.emit)
        self.btn_remove.clicked.connect(self.remove_requested.emit)
        self.btn_monitor.clicked.connect(self.monitor_toggle_requested.emit)
        self.btn_record.clicked.connect(self.record_toggle_requested.emit)
        self.chk_auto_record.toggled.connect(self.auto_record_changed.emit)
        self.empty_state.action_requested.connect(self.add_requested.emit)

        self.room_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.room_list.itemDoubleClicked.connect(
            lambda _item: self.edit_requested.emit()
        )
        self.room_list.customContextMenuRequested.connect(self._show_context_menu)
        self.room_list.model().rowsInserted.connect(
            lambda *_args: self._sync_empty_state()
        )
        self.room_list.model().rowsRemoved.connect(
            lambda *_args: self._sync_empty_state()
        )

        self.set_action_state(False, False, False)
        self._sync_empty_state()

    # ---------- 状态 ----------
    def _on_selection_changed(self) -> None:
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            self.selection_label.setText("选择直播间后可执行操作")
            self.selection_label.setToolTip("")
        else:
            name = self.room_list.selected_display_name()
            self.selection_label.setText(f"已选择：{name}")
            self.selection_label.setToolTip(name)
        self.selection_changed.emit()

    def set_action_state(
        self, has_selection: bool, monitoring: bool, recording: bool
    ) -> None:
        for button in (
            self.btn_edit,
            self.btn_remove,
            self.btn_monitor,
            self.btn_record,
        ):
            button.setEnabled(has_selection)
        self.btn_monitor.setText("停止监控" if monitoring else "开始监控")
        self.btn_record.setText("停止录制" if recording else "开始录制")
        self._refresh_action_icons(monitoring, recording)

    def _sync_empty_state(self) -> None:
        self.stack.setCurrentWidget(
            self.room_list if self.room_list.rowCount() else self.empty_state
        )

    def refresh_empty_state(self) -> None:
        self._sync_empty_state()

    def _show_context_menu(self, position) -> None:
        index = self.room_list.indexAt(position)
        if not index.isValid():
            return
        self.room_list.selectRow(index.row())
        room_id = self.room_list.selected_room_id()
        if room_id is None:
            return
        monitoring = self.room_list.monitoring(room_id)
        recording = self.room_list.recording(room_id)

        menu = QMenu(self)
        menu.addAction("编辑直播间", self.edit_requested.emit)
        menu.addAction(
            "停止监控" if monitoring else "开始监控",
            self.monitor_toggle_requested.emit,
        )
        menu.addAction(
            "停止录制" if recording else "开始录制",
            self.record_toggle_requested.emit,
        )
        menu.addSeparator()
        menu.addAction("删除直播间", self.remove_requested.emit)
        menu.exec(self.room_list.viewport().mapToGlobal(position))

    # ---------- 主题 ----------
    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.room_list.set_theme_tokens(tokens)
        self.empty_state.set_theme_tokens(tokens)
        self.chk_auto_record.set_theme_tokens(tokens)
        self.btn_add.setIcon(icons.icon("add", "#FFFFFF", 16))
        self.btn_open_dir.setIcon(icons.icon("folder", tokens.text_secondary, 16))
        self.btn_edit.setIcon(icons.icon("edit", tokens.text_secondary, 16))
        self.btn_remove.setIcon(icons.icon("delete", tokens.danger, 16))
        room_id = self.room_list.selected_room_id()
        monitoring = bool(room_id and self.room_list.monitoring(room_id))
        recording = bool(room_id and self.room_list.recording(room_id))
        self._refresh_action_icons(monitoring, recording)

    def _refresh_action_icons(self, monitoring: bool, recording: bool) -> None:
        tokens = self._tokens
        self.btn_monitor.setIcon(
            icons.icon("stop" if monitoring else "play", tokens.text, 16)
        )
        self.btn_record.setIcon(
            icons.icon(
                "stop" if recording else "record",
                tokens.danger if recording else tokens.accent_fg,
                16,
            )
        )


class HistoryPage(QWidget):
    """录制历史页（只读）。"""

    refresh_requested = Signal()
    open_dir_requested = Signal()
    open_file_requested = Signal(str)
    open_folder_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._tokens: ThemeTokens = LIGHT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setProperty("variant", "ghost")
        self.btn_open_dir = QPushButton("打开录制目录")
        self.btn_open_dir.setProperty("variant", "ghost")
        for button in (self.btn_refresh, self.btn_open_dir):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(
            _page_header(
                "录制历史",
                "只读展示录制记录；不会自动删除或移动任何文件。",
                [self.btn_open_dir, self.btn_refresh],
            )
        )

        self.summary_label = QLabel("暂无记录")
        self.summary_label.setObjectName("Secondary")
        layout.addWidget(self.summary_label)

        content_card, content_layout = _card()
        headers = ("ID", "房间", "开始时间", "结束时间", "状态", "大小", "文件")
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        style_history_table(self.table)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.empty_state = EmptyState(
            "暂无录制记录",
            "开始监控并录制后，这里会显示每次录制的文件、时长与状态。",
            action_text="刷新列表",
            action_icon="refresh",
            icon_name="history",
        )
        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty_state)
        content_layout.addWidget(self.stack)
        layout.addWidget(content_card, 1)

        hint = QLabel("双击记录可打开文件所在文件夹；右键可复制文件路径。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        self.btn_open_dir.clicked.connect(self.open_dir_requested.emit)
        self.empty_state.action_requested.connect(self.refresh_requested.emit)
        self.table.itemDoubleClicked.connect(self._on_open_item)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

    def set_records(self, records: list[dict]) -> None:
        populate_history_table(self.table, records)
        self.stack.setCurrentWidget(self.table if records else self.empty_state)
        if records:
            self.summary_label.setText(f"最近 {len(records)} 条记录")
        else:
            self.summary_label.setText("暂无记录")

    def _path_for_row(self, row: int) -> str:
        item = self.table.item(row, 6)
        return item.text() if item is not None else ""

    def _on_open_item(self, item) -> None:
        path = self._path_for_row(item.row())
        if path:
            self.open_folder_requested.emit(path)

    def _show_context_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        self.table.selectRow(index.row())
        path = self._path_for_row(index.row())
        if not path:
            return
        menu = QMenu(self)
        menu.addAction("打开文件", lambda: self.open_file_requested.emit(path))
        menu.addAction(
            "打开所在文件夹", lambda: self.open_folder_requested.emit(path)
        )
        menu.addAction(
            "复制文件路径",
            lambda: QApplication.clipboard().setText(path),
        )
        menu.exec(self.table.viewport().mapToGlobal(position))

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.empty_state.set_theme_tokens(tokens)
        self.btn_refresh.setIcon(icons.icon("refresh", tokens.text_secondary, 16))
        self.btn_open_dir.setIcon(icons.icon("folder", tokens.text_secondary, 16))


class LogsPage(QWidget):
    """运行日志页。"""

    open_dir_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._tokens: ThemeTokens = LIGHT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)

        self.btn_clear = QPushButton("清空显示")
        self.btn_clear.setProperty("variant", "ghost")
        self.btn_open_dir = QPushButton("打开日志目录")
        self.btn_open_dir.setProperty("variant", "ghost")
        for button in (self.btn_clear, self.btn_open_dir):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(
            _page_header(
                "运行日志",
                "按时间查看监控与录制日志；警告和错误会以颜色区分。",
                [self.btn_open_dir, self.btn_clear],
            )
        )

        content_card, content_layout = _card()
        content_layout.setContentsMargins(10, 10, 10, 10)
        self.log_widget = LogWidget()
        content_layout.addWidget(self.log_widget)
        layout.addWidget(content_card, 1)

        hint = QLabel("日志显示上限 500 行，完整日志保存在数据目录的 logs 文件夹。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        self.btn_clear.clicked.connect(self.log_widget.clear_log)
        self.btn_open_dir.clicked.connect(self.open_dir_requested.emit)

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.log_widget.set_theme_tokens(tokens)
        self.btn_clear.setIcon(icons.icon("delete", tokens.text_secondary, 16))
        self.btn_open_dir.setIcon(icons.icon("folder", tokens.text_secondary, 16))


class SettingsPage(QWidget):
    """设置中心页。"""

    settings_saved = Signal(object)
    backup_requested = Signal()
    restore_requested = Signal()
    diagnostics_requested = Signal()

    def __init__(
        self,
        service: SettingsService,
        current: AppSettings,
        theme_manager: Optional[ThemeManager] = None,
        *,
        backup_enabled: bool = True,
        restore_enabled: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._tokens: ThemeTokens = LIGHT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)

        self.btn_save = QPushButton("保存设置")
        self.btn_save.setProperty("variant", "primary")
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(
            _page_header(
                "设置",
                "常规、录制、存储、外观与账户配置。",
                [self.btn_save],
            )
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.panel = SettingsPanel(service, current, theme_manager=theme_manager)
        scroll.setWidget(self.panel)
        layout.addWidget(scroll, 1)

        data_card, data_layout = _card()
        data_layout.setContentsMargins(18, 16, 18, 16)
        data_layout.setSpacing(8)
        data_title = QLabel("数据安全")
        data_title.setObjectName("SectionTitle")
        data_layout.addWidget(data_title)
        data_text = QLabel(
            "备份数据库；恢复会在下次启动时生效，录制文件不会被删除。"
            "诊断报告仅在您主动导出时生成，只保存在本机，不会自动上传。"
        )
        data_text.setObjectName("Secondary")
        data_text.setWordWrap(True)
        data_layout.addWidget(data_text)
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.btn_backup = QPushButton("备份数据")
        self.btn_restore = QPushButton("恢复备份")
        self.btn_diagnostics = QPushButton("导出诊断报告…")
        self.btn_backup.setEnabled(backup_enabled)
        self.btn_restore.setEnabled(restore_enabled)
        button_row.addWidget(self.btn_backup)
        button_row.addWidget(self.btn_restore)
        button_row.addWidget(self.btn_diagnostics)
        button_row.addStretch(1)
        self.save_status = QLabel("")
        self.save_status.setObjectName("SuccessLabel")
        button_row.addWidget(self.save_status)
        data_layout.addLayout(button_row)
        layout.addWidget(data_card)

        self.btn_save.clicked.connect(self._on_save)
        self.btn_backup.clicked.connect(self.backup_requested.emit)
        self.btn_restore.clicked.connect(self.restore_requested.emit)
        self.btn_diagnostics.clicked.connect(self.diagnostics_requested.emit)
        self.panel.error_label.hide()

    def _on_save(self) -> None:
        self.save_status.setText("")
        saved = self.panel.save()
        if saved is None:
            return
        self.save_status.setText("设置已保存")
        self.settings_saved.emit(saved)

    def sync_theme(self) -> None:
        self.panel.sync_theme()

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.btn_backup.setIcon(icons.icon("shield", tokens.text_secondary, 16))
        self.btn_restore.setIcon(icons.icon("refresh", tokens.text_secondary, 16))
        self.btn_diagnostics.setIcon(
            icons.icon("logs", tokens.text_secondary, 16)
        )
