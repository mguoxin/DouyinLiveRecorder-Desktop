"""录制库页面（P0-02）：检索、筛选、容量统计、一致性检查与安全删除。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..application.media_probe_service import MediaProbeService
from ..application.recording_library_service import (
    ConsistencyReport,
    DeleteReport,
    RecordingLibraryService,
)
from ..domain.recording_query import RecordingQuery
from ..infrastructure.safe_fileops import SafeFileOpsError
from .theme import ThemeTokens

logger = logging.getLogger(__name__)

COLUMNS = (
    "ID", "主播", "房间号", "开始时间", "时长", "大小", "状态", "归档", "收藏", "文件路径"
)

_SORT_OPTIONS = (
    ("最新优先", "newest"),
    ("最早优先", "oldest"),
    ("文件最大", "largest"),
    ("文件最小", "smallest"),
    ("时长最长", "longest"),
    ("时长最短", "shortest"),
)

_STATUS_LABELS = {
    "recording": "录制中",
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "中断",
    "deleted": "已删除",
}
_ARCHIVE_LABELS = {
    "none": "未归档",
    "pending": "待归档",
    "copying": "复制中",
    "verifying": "校验中",
    "archived": "已归档",
    "failed": "归档失败",
    "skipped": "已跳过",
}
_METADATA_LABELS = {
    "none": "未提取",
    "pending": "提取中",
    "ready": "已就绪",
    "failed": "提取失败",
}
_ISSUE_LABELS = {
    "missing": "数据库有记录但文件缺失",
    "size_mismatch": "文件大小与记录不一致",
    "archive_missing": "归档文件缺失",
    "orphan_file": "磁盘文件无数据库记录",
}


def human_size(size: Optional[int]) -> str:
    if size is None:
        return "--"
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _duration_text(started: Optional[str], ended: Optional[str], status: str) -> str:
    if status == "recording":
        return "录制中"
    if not started or not ended:
        return "--"
    try:
        begin = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
        finish = datetime.strptime(ended, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return "--"
    seconds = max(0, int((finish - begin).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class _TagDialog(QDialog):
    """选择与新建录制标签。"""

    def __init__(
        self,
        service: RecordingLibraryService,
        tags: list[dict],
        selected_ids: list[int],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("录制标签")
        self.setMinimumWidth(320)
        self._service = service
        self._selected = {int(value) for value in selected_ids}

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)
        for tag in tags:
            self._add_item(tag)

        row = QHBoxLayout()
        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("新标签名称")
        self.btn_add = QPushButton("添加标签")
        row.addWidget(self.new_edit, 1)
        row.addWidget(self.btn_add)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.btn_add.clicked.connect(self._create_tag)
        self.new_edit.returnPressed.connect(self._create_tag)
        layout.addWidget(buttons)

    def _add_item(self, tag: dict) -> None:
        item = QListWidgetItem(str(tag.get("name") or ""))
        tag_id = int(tag.get("id"))
        item.setData(Qt.ItemDataRole.UserRole, tag_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked
            if tag_id in self._selected
            else Qt.CheckState.Unchecked
        )
        self.list_widget.addItem(item)

    def _create_tag(self) -> None:
        name = self.new_edit.text().strip()
        if not name:
            return
        try:
            tag_id = self._service.create_tag(name)
        except ValueError as exc:
            QMessageBox.warning(self, "无法添加标签", str(exc))
            return
        self._selected.add(tag_id)
        self._add_item({"id": tag_id, "name": name})
        self.new_edit.clear()

    def selected_tag_ids(self) -> list[int]:
        ids: list[int] = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return ids


class _AnchorProfileDialog(QDialog):
    """编辑主播档案（简介、备注、头像路径）。"""

    def __init__(self, profile: Optional[dict], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("主播档案")
        self.setMinimumWidth(380)
        profile = profile or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        self.bio_edit = QLineEdit(str(profile.get("bio") or ""))
        self.bio_edit.setPlaceholderText("一句话简介")
        form.addRow("简介", self.bio_edit)

        avatar_row = QHBoxLayout()
        self.avatar_edit = QLineEdit(str(profile.get("avatar_path") or ""))
        self.avatar_edit.setPlaceholderText("可选，本地图片路径")
        self.btn_browse = QPushButton("浏览…")
        avatar_row.addWidget(self.avatar_edit, 1)
        avatar_row.addWidget(self.btn_browse)
        form.addRow("头像", avatar_row)

        self.note_edit = QPlainTextEdit(str(profile.get("note") or ""))
        self.note_edit.setPlaceholderText("备注、风格偏好、联系方式等")
        self.note_edit.setFixedHeight(96)
        form.addRow("备注", self.note_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.btn_browse.clicked.connect(self._browse)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择头像图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if path:
            self.avatar_edit.setText(path)

    def values(self) -> tuple[str, str, str]:
        return (
            self.avatar_edit.text().strip(),
            self.bio_edit.text().strip(),
            self.note_edit.toPlainText().strip(),
        )


class LibraryPage(QWidget):
    """录制库页面；所有磁盘检查与删除都经过服务层的安全约束。"""

    scan_finished = Signal(object)

    def __init__(
        self,
        service: RecordingLibraryService,
        parent: Optional[QWidget] = None,
        *,
        media_service: Optional[MediaProbeService] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._service = service
        self._media_service = media_service
        self._rows: list[dict] = []
        self._tokens: Optional[ThemeTokens] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("录制库")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_scan = QPushButton("一致性检查")
        self.btn_open_dir = QPushButton("打开所在目录")
        self.btn_open_file = QPushButton("打开文件")
        self.btn_favorite = QPushButton("收藏所选")
        self.btn_tags = QPushButton("标签…")
        self.btn_profile = QPushButton("主播档案…")
        self.btn_delete = QPushButton("删除所选…")
        self.btn_delete.setProperty("variant", "danger")
        for button in (
            self.btn_refresh,
            self.btn_scan,
            self.btn_open_dir,
            self.btn_open_file,
            self.btn_favorite,
            self.btn_tags,
            self.btn_profile,
            self.btn_delete,
        ):
            header.addWidget(button)
        layout.addLayout(header)

        filters = QFrame()
        filters.setObjectName("Card")
        form = QFormLayout(filters)
        form.setContentsMargins(16, 12, 16, 12)
        form.setSpacing(8)

        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("主播名称、房间号或文件名")
        self.date_from_edit = QLineEdit()
        self.date_from_edit.setPlaceholderText("YYYY-MM-DD")
        self.date_from_edit.setMaximumWidth(140)
        self.date_to_edit = QLineEdit()
        self.date_to_edit.setPlaceholderText("YYYY-MM-DD")
        self.date_to_edit.setMaximumWidth(140)
        self.status_combo = QComboBox()
        for label, value in (
            ("全部状态", ""),
            ("已完成", "completed"),
            ("失败", "failed"),
            ("中断", "interrupted"),
            ("录制中", "recording"),
        ):
            self.status_combo.addItem(label, value)
        self.archive_combo = QComboBox()
        for label, value in (
            ("全部归档状态", ""),
            ("未归档", "none"),
            ("待归档", "pending"),
            ("已归档", "archived"),
            ("归档失败", "failed"),
            ("已跳过", "skipped"),
        ):
            self.archive_combo.addItem(label, value)
        self.favorite_combo = QComboBox()
        for label, value in (
            ("全部收藏", ""),
            ("仅收藏", "yes"),
            ("未收藏", "no"),
        ):
            self.favorite_combo.addItem(label, value)
        self.tag_combo = QComboBox()
        self.tag_combo.addItem("全部标签", 0)
        self.sort_combo = QComboBox()
        for label, value in _SORT_OPTIONS:
            self.sort_combo.addItem(label, value)
        self.btn_search = QPushButton("检索")
        self.btn_search.setProperty("variant", "primary")

        keyword_row = QHBoxLayout()
        keyword_row.setSpacing(8)
        keyword_row.addWidget(self.keyword_edit, 1)
        keyword_row.addWidget(self.btn_search)
        date_row = QHBoxLayout()
        date_row.setSpacing(8)
        date_row.addWidget(self.date_from_edit)
        date_row.addWidget(QLabel("至"))
        date_row.addWidget(self.date_to_edit)
        date_row.addStretch(1)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(self.status_combo)
        status_row.addWidget(self.archive_combo)
        status_row.addWidget(self.favorite_combo)
        status_row.addWidget(self.tag_combo)
        status_row.addWidget(self.sort_combo)
        status_row.addStretch(1)

        form.addRow("关键字", keyword_row)
        form.addRow("录制日期", date_row)
        form.addRow("筛选", status_row)
        layout.addWidget(filters)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Muted")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # 主播名称与文件路径可能很长：让这两列吸收剩余宽度并省略显示，
        # 避免内容撑宽表格，在 1280 等常见宽度下出现横向溢出。
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(
            len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch
        )
        header_view.setStretchLastSection(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self.table, 1)

        detail = QFrame()
        detail.setObjectName("Card")
        detail.setMinimumWidth(232)
        detail.setMaximumWidth(300)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 12, 14, 12)
        detail_layout.setSpacing(8)
        self.preview_label = QLabel("暂无封面")
        self.preview_label.setObjectName("Preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFixedSize(240, 135)
        self.preview_label.setWordWrap(True)
        detail_layout.addWidget(self.preview_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.detail_label = QLabel("选择一条录制查看详情")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail_layout.addWidget(self.detail_label)
        note_caption = QLabel("备注")
        note_caption.setObjectName("Muted")
        detail_layout.addWidget(note_caption)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("为这条录制添加备注")
        detail_layout.addWidget(self.note_edit)
        self.btn_save_note = QPushButton("保存备注")
        detail_layout.addWidget(self.btn_save_note)
        detail_layout.addStretch(1)
        self.btn_probe = QPushButton("提取媒体信息")
        self.btn_probe.setToolTip("在后台重新读取时长、分辨率与编码，并生成缩略图")
        self.btn_probe.setEnabled(media_service is not None)
        detail_layout.addWidget(self.btn_probe)
        body.addWidget(detail)
        layout.addLayout(body, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Secondary")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.btn_refresh.clicked.connect(self.reload)
        self.btn_search.clicked.connect(self.reload)
        self.keyword_edit.returnPressed.connect(self.reload)
        self.btn_scan.clicked.connect(self._start_scan)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_open_dir.clicked.connect(self._open_selected_folder)
        self.btn_open_file.clicked.connect(self._open_selected_file)
        self.btn_probe.clicked.connect(self._probe_selected)
        self.btn_favorite.clicked.connect(self._toggle_favorite)
        self.btn_tags.clicked.connect(self._edit_tags)
        self.btn_profile.clicked.connect(self._edit_profile)
        self.btn_save_note.clicked.connect(self._save_note)
        self.table.itemSelectionChanged.connect(self._update_detail)
        self.scan_finished.connect(self._on_scan_finished)
        if self._media_service is not None:
            self._media_service.media_updated.connect(self._on_media_updated)

    # ---------- 查询 ----------
    def current_query(self) -> RecordingQuery:
        favorite_value = str(self.favorite_combo.currentData() or "")
        favorite = None
        if favorite_value == "yes":
            favorite = True
        elif favorite_value == "no":
            favorite = False
        tag_id = int(self.tag_combo.currentData() or 0)
        return RecordingQuery(
            keyword=self.keyword_edit.text().strip(),
            date_from=self.date_from_edit.text().strip(),
            date_to=self.date_to_edit.text().strip(),
            status=str(self.status_combo.currentData() or ""),
            archive_status=str(self.archive_combo.currentData() or ""),
            favorite=favorite,
            tag_ids=(tag_id,) if tag_id > 0 else (),
            sort_by=str(self.sort_combo.currentData() or "newest"),
            limit=1000,
        )

    def _reload_tag_filter(self) -> None:
        current = int(self.tag_combo.currentData() or 0)
        try:
            tags = self._service.list_tags()
        except Exception:  # noqa: BLE001 - 标签读取失败不影响主检索
            logger.warning("读取标签失败", exc_info=True)
            return
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("全部标签", 0)
        for tag in tags:
            self.tag_combo.addItem(str(tag.get("name") or ""), int(tag["id"]))
        index = self.tag_combo.findData(current)
        if index >= 0:
            self.tag_combo.setCurrentIndex(index)
        self.tag_combo.blockSignals(False)

    def reload(self) -> None:
        self._reload_tag_filter()
        query = self.current_query()
        try:
            self._rows = self._service.search(query)
            total = self._service.count(query)
            stats = self._service.storage_stats()
            disks = self._service.disk_usage()
        except Exception as exc:  # noqa: BLE001 - 查询失败不得导致崩溃
            logger.exception("录制库查询失败")
            self.status_label.setText(f"查询失败：{exc}")
            return
        self._populate(self._rows)
        used_text = human_size(stats.get("total_bytes"))
        summary = (
            f"匹配 {total} 条，当前显示 {len(self._rows)} 条；"
            f"库内共 {stats.get('total_count', 0)} 条 / {used_text}。"
        )
        recordings_disk = disks.get("recordings")
        if recordings_disk:
            summary += (
                f" 录制卷可用 {human_size(recordings_disk['free'])}"
                f" / {human_size(recordings_disk['total'])}；"
            )
        archive_disk = disks.get("archive")
        if archive_disk:
            summary += f" 归档卷可用 {human_size(archive_disk['free'])}。"
        self.summary_label.setText(summary)
        self.status_label.setText("")
        self._update_detail()

    def _populate(self, rows: list[dict]) -> None:
        try:
            tag_map = self._service.recording_tag_map(
                [int(row["id"]) for row in rows if row.get("id") is not None]
            )
        except Exception:  # noqa: BLE001 - 标签展示失败不影响录制列表
            logger.warning("读取录制标签失败", exc_info=True)
            tag_map = {}
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                str(row.get("id", "")),
                str(row.get("display_name_snapshot") or "--"),
                str(row.get("web_rid_snapshot") or "--"),
                str(row.get("started_at") or "--"),
                _duration_text(
                    row.get("started_at"), row.get("ended_at"), row.get("status") or ""
                ),
                human_size(row.get("file_size")),
                _STATUS_LABELS.get(row.get("status") or "", row.get("status") or "--"),
                _ARCHIVE_LABELS.get(
                    row.get("archive_status") or "", row.get("archive_status") or "--"
                ),
                "★" if row.get("favorite") else "",
                str(row.get("archive_path") or row.get("file_path") or "--"),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row.get("id"))
                if column == 8 and text:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip("已收藏")
                self.table.setItem(row_index, column, item)
            names = tag_map.get(int(row["id"])) if row.get("id") is not None else None
            if names:
                self.table.item(row_index, 1).setToolTip("标签：" + "、".join(names))

    def selected_recording_ids(self) -> list[int]:
        ids: list[int] = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item is not None:
                value = item.data(Qt.ItemDataRole.UserRole)
                if value is not None:
                    ids.append(int(value))
        return ids

    def _selected_row(self) -> Optional[dict]:
        ids = set(self.selected_recording_ids())
        for row in self._rows:
            if int(row.get("id", -1)) in ids:
                return row
        return None

    # ---------- 详情与媒体信息 ----------
    def _update_detail(self) -> None:
        row = self._selected_row()
        has_selection = row is not None
        self.btn_favorite.setEnabled(has_selection)
        self.btn_tags.setEnabled(has_selection)
        self.btn_profile.setEnabled(has_selection)
        self.note_edit.setEnabled(has_selection)
        self.btn_save_note.setEnabled(has_selection)
        if row is None:
            self.preview_label.setText("暂无封面")
            self.preview_label.setPixmap(QPixmap())
            self.detail_label.setText("选择一条录制查看详情")
            self.note_edit.clear()
            return

        resolution = "--"
        if row.get("width") and row.get("height"):
            resolution = f"{int(row['width'])}×{int(row['height'])}"
        codecs = " / ".join(
            value for value in (row.get("video_codec"), row.get("audio_codec")) if value
        ) or "--"
        duration = row.get("duration_s")
        if duration is None:
            duration_text = _duration_text(
                row.get("started_at"), row.get("ended_at"), row.get("status") or ""
            )
        else:
            total = max(0, int(float(duration)))
            hours, remainder = divmod(total, 3600)
            minutes, secs = divmod(remainder, 60)
            duration_text = (
                f"{hours}:{minutes:02d}:{secs:02d}"
                if hours
                else f"{minutes}:{secs:02d}"
            )
        meta_status = _METADATA_LABELS.get(
            row.get("metadata_status") or "none", row.get("metadata_status") or "--"
        )
        lines = [
            f"主播：{row.get('display_name_snapshot') or '--'}",
            f"房间号：{row.get('web_rid_snapshot') or '--'}",
            f"开始：{row.get('started_at') or '--'}",
            f"时长：{duration_text}",
            f"分辨率：{resolution}",
            f"编码：{codecs}",
            f"大小：{human_size(row.get('file_size'))}",
            f"状态：{_STATUS_LABELS.get(row.get('status') or '', row.get('status') or '--')}",
            f"归档：{_ARCHIVE_LABELS.get(row.get('archive_status') or '', row.get('archive_status') or '--')}",
            f"媒体信息：{meta_status}",
        ]
        if row.get("metadata_error"):
            lines.append(f"提示：{row['metadata_error']}")
        try:
            tags = self._service.get_recording_tags(int(row["id"]))
        except Exception:  # noqa: BLE001 - 标签读取失败不影响详情
            logger.warning("读取录制标签失败", exc_info=True)
            tags = []
        lines.append("标签：" + ("、".join(str(tag.get("name")) for tag in tags) or "--"))
        room_id = row.get("room_id")
        if room_id is not None:
            try:
                profile = self._service.get_anchor_profile(int(room_id))
            except Exception:  # noqa: BLE001
                profile = None
            if profile and (profile.get("bio") or profile.get("note")):
                lines.append(
                    "主播档案：" + "；".join(
                        value for value in (profile.get("bio"), profile.get("note")) if value
                    )
                )
        self.detail_label.setText("\n".join(lines))

        self.note_edit.setText(str(row.get("note") or ""))
        self.preview_label.setText("暂无封面")
        self.preview_label.setPixmap(QPixmap())
        thumbnail = row.get("thumbnail_path")
        if thumbnail:
            pixmap = QPixmap(str(thumbnail))
            if not pixmap.isNull():
                self.preview_label.setPixmap(
                    pixmap.scaled(
                        240,
                        135,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self.preview_label.setText("")

    def _on_media_updated(self, recording_id: int, payload: dict) -> None:
        for row in self._rows:
            if int(row.get("id", -1)) != int(recording_id):
                continue
            row.update(payload)
            break
        self._update_detail()
        if payload.get("metadata_status") == "ready":
            self.status_label.setText(f"媒体信息已更新：#{recording_id}")
        elif payload.get("metadata_status") == "failed":
            self.status_label.setText(
                f"媒体信息提取失败：#{recording_id}（{payload.get('error') or '未知原因'}）"
            )

    def _probe_selected(self) -> None:
        if self._media_service is None:
            self.status_label.setText("当前运行方式未启用媒体信息提取")
            return
        row = self._selected_row()
        if row is None:
            self.status_label.setText("请先选择一条录制记录")
            return
        if row.get("status") == "recording":
            self.status_label.setText("录制进行中，结束后再提取媒体信息")
            return
        recording_id = int(row.get("id"))
        if self._media_service.enqueue(recording_id, force=True):
            self.status_label.setText(f"已提交媒体信息提取：#{recording_id}")
        else:
            self.status_label.setText(f"#{recording_id} 已在提取队列中")

    # ---------- 资产组织 ----------
    def _row_index(self, recording_id: int) -> int:
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == recording_id:
                return index
        return -1

    def _toggle_favorite(self) -> None:
        row = self._selected_row()
        if row is None:
            self.status_label.setText("请先选择一条录制记录")
            return
        recording_id = int(row["id"])
        favorite = not bool(row.get("favorite"))
        try:
            updated = self._service.set_favorite(recording_id, favorite)
        except Exception as exc:  # noqa: BLE001 - 失败必须可见
            logger.exception("更新收藏状态失败")
            self.status_label.setText(f"收藏状态更新失败：{exc}")
            return
        if not updated:
            self.status_label.setText("收藏状态更新失败：记录不存在")
            return
        row["favorite"] = 1 if favorite else 0
        index = self._row_index(recording_id)
        if index >= 0:
            item = self.table.item(index, 8)
            if item is not None:
                item.setText("★" if favorite else "")
        self.status_label.setText(
            f"#{recording_id} 已{'收藏' if favorite else '取消收藏'}"
        )

    def _save_note(self) -> None:
        row = self._selected_row()
        if row is None:
            self.status_label.setText("请先选择一条录制记录")
            return
        recording_id = int(row["id"])
        note = self.note_edit.text().strip()
        try:
            updated = self._service.set_note(recording_id, note)
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存备注失败")
            self.status_label.setText(f"备注保存失败：{exc}")
            return
        if not updated:
            self.status_label.setText("备注保存失败：记录不存在")
            return
        row["note"] = note
        self.status_label.setText(f"#{recording_id} 备注已保存")

    def _edit_tags(self) -> None:
        row = self._selected_row()
        if row is None:
            self.status_label.setText("请先选择一条录制记录")
            return
        recording_id = int(row["id"])
        try:
            tags = self._service.list_tags()
            selected = [
                int(tag["id"]) for tag in self._service.get_recording_tags(recording_id)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取标签失败")
            self.status_label.setText(f"读取标签失败：{exc}")
            return
        dialog = _TagDialog(self._service, tags, selected, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._service.set_recording_tags(recording_id, dialog.selected_tag_ids())
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存标签失败")
            self.status_label.setText(f"保存标签失败：{exc}")
            return
        self._reload_tag_filter()
        self._update_detail()
        self.status_label.setText(f"#{recording_id} 标签已更新")

    def _edit_profile(self) -> None:
        row = self._selected_row()
        if row is None or row.get("room_id") is None:
            self.status_label.setText("请先选择一条有房间信息的录制记录")
            return
        room_id = int(row["room_id"])
        try:
            profile = self._service.get_anchor_profile(room_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取主播档案失败")
            self.status_label.setText(f"读取主播档案失败：{exc}")
            return
        dialog = _AnchorProfileDialog(profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        avatar, bio, note = dialog.values()
        try:
            self._service.set_anchor_profile(
                room_id, avatar_path=avatar, bio=bio, note=note
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存主播档案失败")
            self.status_label.setText(f"保存主播档案失败：{exc}")
            return
        self._update_detail()
        self.status_label.setText("主播档案已保存")

    # ---------- 打开文件 ----------
    def _open_selected_folder(self) -> None:
        row = self._selected_row()
        if row is None:
            self.status_label.setText("请先选择一条录制记录")
            return
        path = Path(row.get("file_path") or "")
        target = path.parent if path.name else path
        if not target.is_dir():
            self.status_label.setText(f"目录不存在：{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _open_selected_file(self) -> None:
        row = self._selected_row()
        if row is None:
            self.status_label.setText("请先选择一条录制记录")
            return
        path = Path(row.get("file_path") or "")
        if not path.is_file():
            archived = row.get("archive_path")
            if archived and Path(archived).is_file():
                path = Path(archived)
        if not path.is_file():
            self.status_label.setText(f"文件不存在：{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ---------- 一致性检查 ----------
    def _start_scan(self) -> None:
        self.btn_scan.setEnabled(False)
        self.status_label.setText("正在检查数据库与磁盘一致性…")

        def worker() -> None:
            try:
                report = self._service.scan_consistency()
            except Exception as exc:  # noqa: BLE001
                logger.exception("一致性检查失败")
                report = exc
            self.scan_finished.emit(report)

        threading.Thread(target=worker, name="dlr-library-scan", daemon=True).start()

    def _on_scan_finished(self, report: object) -> None:
        self.btn_scan.setEnabled(True)
        if not isinstance(report, ConsistencyReport):
            self.status_label.setText(f"一致性检查失败：{report}")
            return
        counts = report.counts()
        if not counts:
            self.status_label.setText(
                f"一致性检查通过：{report.checked_records} 条记录与磁盘一致。"
            )
            return
        details = "；".join(
            f"{_ISSUE_LABELS.get(kind, kind)} {count} 条"
            for kind, count in sorted(counts.items())
        )
        self.status_label.setText(f"发现不一致：{details}。详见弹窗。")
        lines = [
            f"{_ISSUE_LABELS.get(issue.kind, issue.kind)}：{issue.path}"
            f"（{issue.detail}）"
            for issue in report.issues[:20]
        ]
        if len(report.issues) > 20:
            lines.append(f"……其余 {len(report.issues) - 20} 条未显示")
        QMessageBox.warning(
            self,
            "一致性检查",
            f"已检查 {report.checked_records} 条记录，发现 {len(report.issues)} 个问题：\n\n"
            + "\n".join(lines),
        )

    # ---------- 删除 ----------
    def _delete_selected(self) -> None:
        ids = self.selected_recording_ids()
        if not ids:
            self.status_label.setText("请先选择要清理的录制记录")
            return
        selected_rows = [
            row for row in self._rows if int(row.get("id", -1)) in set(ids)
        ]
        total_bytes = sum(int(row.get("file_size") or 0) for row in selected_rows)
        archived = sum(
            1 for row in selected_rows if row.get("archive_status") == "archived"
        )
        warning = (
            f"将把 {len(ids)} 个录制文件（约 {human_size(total_bytes)}）移入回收站。\n"
            "录制中的任务不会被删除。"
        )
        if archived:
            warning += f"\n其中 {archived} 条已有归档副本，本次只清理录制目录中的源文件。"
        answer = QMessageBox.question(
            self,
            "确认清理录制文件",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            report = self._service.delete_recordings(ids)
        except SafeFileOpsError as exc:
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除录制文件失败")
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        self._show_delete_result(report)
        self.reload()

    def _show_delete_result(self, report: DeleteReport) -> None:
        parts = [
            f"已移入回收站 {len(report.deleted)} 个文件，"
            f"释放约 {human_size(report.freed_bytes)}。"
        ]
        if report.archived_copies:
            parts.append(f"其中 {len(report.archived_copies)} 条保留了归档副本。")
        if report.skipped:
            detail = "；".join(
                f"#{recording_id}: {reason}" for recording_id, reason in report.skipped[:5]
            )
            parts.append(f"跳过 {len(report.skipped)} 条（{detail}）。")
        self.status_label.setText(" ".join(parts))

    def set_theme_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
