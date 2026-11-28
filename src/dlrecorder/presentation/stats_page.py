"""统计与资源监控页面（P1）。

所有数据在后台线程聚合（含目录大小扫描），主线程只负责渲染，避免媒体库
较大时界面卡顿。报告导出同样是后台任务。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..application.statistics_service import StatisticsService
from ..infrastructure.update_check import (
    UpdateCheckError,
    UpdateInfo,
    check_for_update,
    describe_update,
)
from .library_page import human_size

logger = logging.getLogger(__name__)

_DAILY_COLUMNS = ("日期", "录制数", "成功", "失败", "容量", "时长")
_ANCHOR_COLUMNS = ("主播", "房间号", "录制数", "成功", "容量", "最近录制")
_FAILURE_COLUMNS = ("原因", "次数")


def _duration_text(seconds: float) -> str:
    total = max(0, int(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分 {secs} 秒"


class _StatsLoader(QThread):
    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, service: StatisticsService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service = service

    def run(self) -> None:
        try:
            payload = {
                "overview": self._service.overview(),
                "daily": self._service.daily(30),
                "anchors": self._service.anchors(100),
                "failures": self._service.failure_reasons(),
                "storage": self._service.storage(),
                "resources": self._service.resources(),
            }
        except Exception as exc:  # noqa: BLE001 - 后台失败必须回传
            logger.exception("统计加载失败")
            self.failed.emit(str(exc))
            return
        self.loaded.emit(payload)


class _ExportWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        service: StatisticsService,
        target: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._target = target

    def run(self) -> None:
        try:
            path = self._service.export_report(self._target)
        except Exception as exc:  # noqa: BLE001
            logger.exception("统计报告导出失败")
            self.failed.emit(str(exc))
            return
        self.done.emit(str(path))


class _UpdateWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        try:
            info = check_for_update(self._url, __version__)
        except UpdateCheckError as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(info)


class StatsPage(QWidget):
    def __init__(
        self,
        service: StatisticsService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._service = service
        self._loader: Optional[_StatsLoader] = None
        self._exporter: Optional[_ExportWorker] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("统计")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            f"版本 {__version__} · Schema {service.schema_version} · "
            "录制成功率、容量构成、主播录制与进程资源（只读统计）"
        )
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_update = QPushButton("检查更新…")
        self.btn_export = QPushButton("导出统计报告…")
        header.addWidget(self.btn_update)
        header.addWidget(self.btn_refresh)
        header.addWidget(self.btn_export)
        layout.addLayout(header)

        self.cards_layout = QGridLayout()
        self.cards_layout.setSpacing(10)
        self.card_labels: dict[str, QLabel] = {}
        for index, (key, title_text) in enumerate(
            (
                ("total", "累计录制"),
                ("today", "今日录制"),
                ("month", "本月录制"),
                ("success", "成功率"),
                ("storage", "媒体占用"),
                ("memory", "进程内存"),
            )
        ):
            card = QFrame()
            card.setObjectName("Card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 10, 14, 10)
            caption = QLabel(title_text)
            caption.setObjectName("Muted")
            value = QLabel("--")
            value.setObjectName("MetricValue")
            card_layout.addWidget(caption)
            card_layout.addWidget(value)
            self.card_labels[key] = value
            self.cards_layout.addWidget(card, 0, index)
        layout.addLayout(self.cards_layout)

        daily_header = QLabel("每日录制（最近 30 天）")
        daily_header.setObjectName("SectionTitle")
        layout.addWidget(daily_header)
        self.daily_table = self._make_table(_DAILY_COLUMNS, stretch_column=5)
        layout.addWidget(self.daily_table, 3)

        lower = QHBoxLayout()
        lower.setSpacing(10)
        anchor_box = QVBoxLayout()
        anchor_header = QLabel("按主播统计")
        anchor_header.setObjectName("SectionTitle")
        anchor_box.addWidget(anchor_header)
        self.anchor_table = self._make_table(_ANCHOR_COLUMNS, stretch_column=0)
        anchor_box.addWidget(self.anchor_table)
        lower.addLayout(anchor_box, 3)

        failure_box = QVBoxLayout()
        failure_header = QLabel("失败/中断原因")
        failure_header.setObjectName("SectionTitle")
        failure_box.addWidget(failure_header)
        self.failure_table = self._make_table(_FAILURE_COLUMNS, stretch_column=0)
        failure_box.addWidget(self.failure_table)
        lower.addLayout(failure_box, 2)
        layout.addLayout(lower, 2)

        self.status_label = QLabel("正在加载统计…")
        self.status_label.setObjectName("Muted")
        layout.addWidget(self.status_label)

        self.btn_refresh.clicked.connect(self.reload)
        self.btn_export.clicked.connect(self._export)
        self.btn_update.clicked.connect(self._check_update)
        self._update_worker: Optional[_UpdateWorker] = None

    @staticmethod
    def _make_table(
        columns: tuple[str, ...], *, stretch_column: Optional[int] = None
    ) -> QTableWidget:
        """构建只读表格。

        - 默认让最后一列吸收剩余宽度；
        - ``stretch_column`` 指定长文本列（如主播名称、失败原因）吸收剩余宽度，
          避免内容撑宽表格导致窄窗口出现横向溢出。
        """
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        if stretch_column is not None and 0 <= stretch_column < len(columns):
            header.setSectionResizeMode(
                stretch_column, QHeaderView.ResizeMode.Stretch
            )
            header.setStretchLastSection(False)
        else:
            header.setStretchLastSection(True)
        return table

    # ---------- 加载 ----------
    def reload(self) -> None:
        if self._loader is not None and self._loader.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.status_label.setText("正在统计…")
        self._loader = _StatsLoader(self._service, self)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.finished.connect(self._on_loader_finished)
        self._loader.start()

    def _on_loader_finished(self) -> None:
        self.btn_refresh.setEnabled(True)

    def _on_failed(self, message: str) -> None:
        self.status_label.setText(f"统计失败：{message}")

    def _on_loaded(self, payload: dict) -> None:
        overview = payload.get("overview", {})
        storage = payload.get("storage", {})
        resources = payload.get("resources", {})
        self.card_labels["total"].setText(f"{overview.get('total', 0)} 场")
        self.card_labels["today"].setText(
            f"{overview.get('today', {}).get('total', 0)} 场 · "
            f"{human_size(overview.get('today', {}).get('bytes', 0))}"
        )
        self.card_labels["month"].setText(
            f"{overview.get('month', {}).get('total', 0)} 场 · "
            f"{human_size(overview.get('month', {}).get('bytes', 0))}"
        )
        rate = overview.get("success_rate")
        self.card_labels["success"].setText("--" if rate is None else f"{rate}%")
        self.card_labels["storage"].setText(
            human_size(storage.get("total_bytes", 0))
        )
        if resources.get("available") and resources.get("rss_mb") is not None:
            cpu = resources.get("cpu_percent")
            cpu_text = f"，CPU {cpu}%" if cpu is not None else ""
            self.card_labels["memory"].setText(
                f"{resources['rss_mb']} MB{cpu_text}"
            )
        else:
            self.card_labels["memory"].setText("不可用")

        self._fill_daily(payload.get("daily", []))
        self._fill_anchors(payload.get("anchors", []))
        self._fill_failures(payload.get("failures", []))
        free_mb = storage.get("free_mb")
        free_text = f"，磁盘剩余 {free_mb:.0f} MB" if free_mb is not None else ""
        self.status_label.setText(
            f"统计完成：{len(payload.get('daily', []))} 天记录"
            f"，{len(payload.get('anchors', []))} 位主播{free_text}"
        )

    def _fill_daily(self, rows: list[dict]) -> None:
        self.daily_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                str(row.get("day") or ""),
                str(row.get("total") or 0),
                str(row.get("completed") or 0),
                str(row.get("failed") or 0),
                human_size(row.get("bytes") or 0),
                _duration_text(row.get("duration_s") or 0),
            )
            for column, text in enumerate(values):
                self.daily_table.setItem(index, column, QTableWidgetItem(text))

    def _fill_anchors(self, rows: list[dict]) -> None:
        self.anchor_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                str(row.get("display_name") or row.get("web_rid") or "--"),
                str(row.get("web_rid") or "--"),
                str(row.get("total") or 0),
                str(row.get("completed") or 0),
                human_size(row.get("bytes") or 0),
                str(row.get("last_started") or "--"),
            )
            for column, text in enumerate(values):
                self.anchor_table.setItem(index, column, QTableWidgetItem(text))

    def _fill_failures(self, rows: list[dict]) -> None:
        self.failure_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (str(row.get("label") or row.get("status") or ""), str(row.get("total") or 0))
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.failure_table.setItem(index, column, item)

    # ---------- 导出 ----------
    def _export(self) -> None:
        if self._exporter is not None and self._exporter.isRunning():
            return
        default_dir = self._service.db_path.parent
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出统计报告",
            str(default_dir / "DLR-statistics.json"),
            "JSON 报告 (*.json)",
        )
        if not target:
            return
        self.btn_export.setEnabled(False)
        self.status_label.setText("正在导出统计报告…")
        self._exporter = _ExportWorker(self._service, Path(target), self)
        self._exporter.done.connect(self._on_export_done)
        self._exporter.failed.connect(self._on_export_failed)
        self._exporter.finished.connect(self._on_export_finished)
        self._exporter.start()

    def _on_export_finished(self) -> None:
        self.btn_export.setEnabled(True)

    def _on_export_done(self, path: str) -> None:
        self.status_label.setText(f"统计报告已导出：{path}")

    def _on_export_failed(self, message: str) -> None:
        self.status_label.setText(f"导出失败：{message}")

    # ---------- 更新检查 ----------
    def _check_update(self) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        current = self._service.get_setting("update.feed_url", "")
        url, accepted = QInputDialog.getText(
            self,
            "检查更新",
            "更新清单 URL（JSON，例如 https://example.com/dlr/update.json）：",
            text=current,
        )
        url = (url or "").strip()
        if not accepted or not url:
            return
        self._service.set_setting("update.feed_url", url)
        self.btn_update.setEnabled(False)
        self.status_label.setText("正在检查更新…")
        self._update_worker = _UpdateWorker(url, self)
        self._update_worker.done.connect(self._on_update_checked)
        self._update_worker.failed.connect(self._on_update_failed)
        self._update_worker.finished.connect(
            lambda: self.btn_update.setEnabled(True)
        )
        self._update_worker.start()

    def _on_update_checked(self, info: object) -> None:
        assert isinstance(info, UpdateInfo)
        self.status_label.setText(describe_update(info))
        details = describe_update(info)
        if info.update_available and info.download_url:
            details += f"\n\n下载地址：{info.download_url}"
        if info.notes:
            details += f"\n\n更新说明：\n{info.notes}"
        details += (
            "\n\n提示：程序不会自动下载或替换文件。升级前请先备份数据库；"
            "若升级失败，可用“设置 → 恢复数据库”回滚。"
        )
        QMessageBox.information(self, "检查更新", details)

    def _on_update_failed(self, message: str) -> None:
        self.status_label.setText(f"检查更新失败：{message}")
        QMessageBox.warning(self, "检查更新失败", message)
