"""自动化规则管理页面（P1）。

规则的执行由 ``RuleEngine`` 在后台完成；本页面只负责规则 CRUD 与执行历史展示，
不做任何阻塞式业务操作。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..domain.automation_rules import RULE_TYPE_LABELS, RULE_TYPES, weekday_label
from ..infrastructure.db import Database

logger = logging.getLogger(__name__)

_RULE_COLUMNS = ("状态", "名称", "类型", "范围", "优先级", "最近执行")
_RUN_COLUMNS = ("时间", "规则", "状态", "说明")
_RUN_STATUS_LABELS = {
    "running": "执行中",
    "succeeded": "成功",
    "failed": "失败",
    "skipped": "跳过",
}


class _RuleDialog(QDialog):
    def __init__(
        self,
        rooms: list[Any],
        rule: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑自动化规则" if rule else "新增自动化规则")
        self.setMinimumWidth(430)
        rule = rule or {}
        try:
            config = json.loads(rule.get("config") or "{}")
        except (TypeError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit(str(rule.get("name") or ""))
        self.name_edit.setPlaceholderText("例如：晚间监控")
        form.addRow("名称", self.name_edit)

        self.room_combo = QComboBox()
        self.room_combo.addItem("全部直播间", 0)
        for room in rooms:
            label = f"{room.display_name or room.web_rid}（{room.web_rid}）"
            self.room_combo.addItem(label, int(room.id))
        room_id = rule.get("room_id")
        index = self.room_combo.findData(int(room_id)) if room_id is not None else 0
        self.room_combo.setCurrentIndex(max(0, index))
        form.addRow("范围", self.room_combo)

        self.type_combo = QComboBox()
        for rule_type in RULE_TYPES:
            self.type_combo.addItem(RULE_TYPE_LABELS[rule_type], rule_type)
        type_index = self.type_combo.findData(rule.get("rule_type"))
        self.type_combo.setCurrentIndex(max(0, type_index))
        form.addRow("类型", self.type_combo)

        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(1, 999)
        self.priority_spin.setValue(int(rule.get("priority") or 100))
        self.priority_spin.setToolTip("数值越小优先级越高；同一范围冲突时高优先级规则生效")
        form.addRow("优先级", self.priority_spin)
        layout.addLayout(form)

        self.schedule_box = QGroupBox("时间窗")
        schedule_layout = QVBoxLayout(self.schedule_box)
        days_row = QHBoxLayout()
        self.day_checks: list[QCheckBox] = []
        selected_days = config.get("days")
        selected = set(selected_days) if isinstance(selected_days, list) else set(range(7))
        for day in range(7):
            check = QCheckBox(weekday_label(day))
            check.setChecked(day in selected)
            self.day_checks.append(check)
            days_row.addWidget(check)
        schedule_layout.addLayout(days_row)
        time_row = QHBoxLayout()
        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm")
        self.start_edit.setTime(
            QTime.fromString(str(config.get("start") or "00:00"), "HH:mm")
            if QTime.fromString(str(config.get("start") or "00:00"), "HH:mm").isValid()
            else QTime(0, 0)
        )
        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm")
        self.end_edit.setTime(
            QTime.fromString(str(config.get("end") or "23:59"), "HH:mm")
            if QTime.fromString(str(config.get("end") or "23:59"), "HH:mm").isValid()
            else QTime(23, 59)
        )
        time_row.addWidget(QLabel("开始"))
        time_row.addWidget(self.start_edit)
        time_row.addSpacing(12)
        time_row.addWidget(QLabel("结束"))
        time_row.addWidget(self.end_edit)
        time_row.addStretch(1)
        schedule_layout.addLayout(time_row)
        layout.addWidget(self.schedule_box)

        self.disk_box = QGroupBox("磁盘预警")
        disk_layout = QFormLayout(self.disk_box)
        self.min_free_spin = QDoubleSpinBox()
        self.min_free_spin.setRange(0, 1_048_576)
        self.min_free_spin.setDecimals(0)
        self.min_free_spin.setSuffix(" MB")
        self.min_free_spin.setValue(float(config.get("min_free_mb") or 2048))
        disk_layout.addRow("剩余空间低于", self.min_free_spin)
        layout.addWidget(self.disk_box)

        self.post_box = QGroupBox("录制后处理")
        post_layout = QFormLayout(self.post_box)
        raw_tags = config.get("tags") or []
        if isinstance(raw_tags, list):
            tags_text = ", ".join(str(tag) for tag in raw_tags)
        else:
            tags_text = str(raw_tags)
        self.tags_edit = QLineEdit(tags_text)
        self.tags_edit.setPlaceholderText("多个标签用英文逗号分隔")
        post_layout.addRow("自动标签", self.tags_edit)
        self.favorite_check = QCheckBox("自动收藏完成的录制")
        self.favorite_check.setChecked(bool(config.get("favorite")))
        post_layout.addRow("", self.favorite_check)
        layout.addWidget(self.post_box)

        self.archive_box = QGroupBox("自动归档")
        archive_layout = QVBoxLayout(self.archive_box)
        self.archive_check = QCheckBox("该房间录制完成后自动归档")
        self.archive_check.setChecked(bool(config.get("enabled", True)))
        archive_layout.addWidget(self.archive_check)
        layout.addWidget(self.archive_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.type_combo.currentIndexChanged.connect(self._sync_type)
        self._sync_type()

    def _sync_type(self) -> None:
        rule_type = str(self.type_combo.currentData() or "")
        self.schedule_box.setVisible(
            rule_type in {"monitor_schedule", "recording_schedule"}
        )
        self.disk_box.setVisible(rule_type == "disk_guard")
        self.post_box.setVisible(rule_type == "post_process")
        self.archive_box.setVisible(rule_type == "archive")

    def _validate_and_accept(self) -> None:
        try:
            self.build_config()
        except ValueError as exc:
            QMessageBox.warning(self, "配置不完整", str(exc))
            return
        self.accept()

    def build_config(self) -> dict:
        rule_type = str(self.type_combo.currentData() or "")
        if rule_type in {"monitor_schedule", "recording_schedule"}:
            days = [day for day, check in enumerate(self.day_checks) if check.isChecked()]
            if not days:
                raise ValueError("至少选择一个星期")
            return {
                "days": days,
                "start": self.start_edit.time().toString("HH:mm"),
                "end": self.end_edit.time().toString("HH:mm"),
            }
        if rule_type == "disk_guard":
            return {"min_free_mb": float(self.min_free_spin.value())}
        if rule_type == "post_process":
            tags = [
                part.strip()
                for part in self.tags_edit.text().replace("，", ",").split(",")
                if part.strip()
            ]
            return {"tags": tags, "favorite": self.favorite_check.isChecked()}
        if rule_type == "archive":
            return {"enabled": self.archive_check.isChecked()}
        return {}

    def values(self) -> dict:
        room_id = int(self.room_combo.currentData() or 0)
        return {
            "name": self.name_edit.text().strip(),
            "rule_type": str(self.type_combo.currentData() or ""),
            "room_id": room_id or None,
            "priority": int(self.priority_spin.value()),
            "config": self.build_config(),
        }


class AutomationPage(QWidget):
    def __init__(self, db: Database, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._db = db
        self._rule_ids: list[int] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("自动化")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "按直播间设置定时监控/录制、磁盘预警、自动归档与录制后处理；"
            "规则在后台执行并保留历史"
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.btn_add = QPushButton("新增规则…")
        self.btn_add.setProperty("variant", "primary")
        self.btn_edit = QPushButton("编辑…")
        self.btn_toggle = QPushButton("启用/停用")
        self.btn_delete = QPushButton("删除")
        self.btn_delete.setProperty("variant", "danger")
        self.btn_refresh = QPushButton("刷新")
        for button in (
            self.btn_add,
            self.btn_edit,
            self.btn_toggle,
            self.btn_delete,
            self.btn_refresh,
        ):
            header.addWidget(button)
        layout.addLayout(header)

        self.table = QTableWidget(0, len(_RULE_COLUMNS))
        self.table.setHorizontalHeaderLabels(_RULE_COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 3)

        history_header = QLabel("最近执行记录")
        history_header.setObjectName("SectionTitle")
        layout.addWidget(history_header)
        self.run_table = QTableWidget(0, len(_RUN_COLUMNS))
        self.run_table.setHorizontalHeaderLabels(_RUN_COLUMNS)
        self.run_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.run_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.run_table.verticalHeader().setVisible(False)
        self.run_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.run_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.run_table, 2)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        layout.addWidget(self.status_label)

        self.btn_add.clicked.connect(self._add_rule)
        self.btn_edit.clicked.connect(self._edit_rule)
        self.btn_toggle.clicked.connect(self._toggle_enabled)
        self.btn_delete.clicked.connect(self._delete_rule)
        self.btn_refresh.clicked.connect(self.reload)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self._sync_buttons()

    # ---------- 数据加载 ----------
    def reload(self) -> None:
        try:
            self._rooms = self._db.list_rooms()
            rules = self._db.list_automation_rules()
            runs = self._db.list_rule_runs(limit=300)
        except Exception as exc:  # noqa: BLE001 - 页面读取失败必须可见
            logger.exception("加载自动化规则失败")
            self.status_label.setText(f"加载自动化规则失败：{exc}")
            return
        room_names = {
            int(room.id): (room.display_name or room.web_rid) for room in self._rooms
        }
        latest: dict[int, dict] = {}
        for run in runs:
            rule_id = run.get("rule_id")
            if rule_id is not None and int(rule_id) not in latest:
                latest[int(rule_id)] = run

        self._rule_ids = []
        self.table.setRowCount(len(rules))
        for index, rule in enumerate(rules):
            rule_id = int(rule["id"])
            self._rule_ids.append(rule_id)
            room_id = rule.get("room_id")
            scope = (
                room_names.get(int(room_id), f"房间 {room_id}")
                if room_id is not None
                else "全部直播间"
            )
            latest_run = latest.get(rule_id)
            if latest_run is None:
                latest_text = "--"
            else:
                latest_text = (
                    f"{_RUN_STATUS_LABELS.get(latest_run.get('status'), latest_run.get('status'))}"
                    f" · {latest_run.get('finished_at') or latest_run.get('started_at') or ''}"
                )
            values = (
                "启用" if rule.get("enabled") else "停用",
                str(rule.get("name") or ""),
                RULE_TYPE_LABELS.get(str(rule.get("rule_type")), str(rule.get("rule_type"))),
                scope,
                str(rule.get("priority") or ""),
                latest_text,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, rule_id)
                self.table.setItem(index, column, item)

        self.run_table.setRowCount(len(runs))
        rule_names = {int(rule["id"]): str(rule.get("name") or "") for rule in rules}
        for index, run in enumerate(runs):
            rule_id = run.get("rule_id")
            values = (
                str(run.get("finished_at") or run.get("started_at") or ""),
                rule_names.get(int(rule_id), "已删除规则") if rule_id is not None else "--",
                _RUN_STATUS_LABELS.get(run.get("status"), str(run.get("status") or "")),
                str(run.get("message") or ""),
            )
            for column, text in enumerate(values):
                self.run_table.setItem(index, column, QTableWidgetItem(text))
        if not self.status_label.text():
            self.status_label.setText(f"共 {len(rules)} 条规则，{len(runs)} 条执行记录")
        self._sync_buttons()

    def on_run_recorded(self, rule_id: int, payload: dict) -> None:
        self.reload()

    # ---------- 交互 ----------
    def _selected_rule_id(self) -> Optional[int]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def _selected_rule(self) -> Optional[dict]:
        rule_id = self._selected_rule_id()
        if rule_id is None:
            return None
        return self._db.get_automation_rule(rule_id)

    def _sync_buttons(self) -> None:
        has_selection = self._selected_rule_id() is not None
        for button in (self.btn_edit, self.btn_toggle, self.btn_delete):
            button.setEnabled(has_selection)

    def _add_rule(self) -> None:
        dialog = _RuleDialog(getattr(self, "_rooms", self._db.list_rooms()), None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self._db.create_automation_rule(**values)
        except Exception as exc:  # noqa: BLE001
            logger.exception("新增自动化规则失败")
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.status_label.setText("规则已创建")
        self.reload()

    def _edit_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        dialog = _RuleDialog(getattr(self, "_rooms", self._db.list_rooms()), rule, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self._db.update_automation_rule(
                int(rule["id"]),
                name=values["name"],
                room_id=values["room_id"],
                priority=values["priority"],
                config=values["config"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("更新自动化规则失败")
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.status_label.setText("规则已更新")
        self.reload()

    def _toggle_enabled(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        try:
            self._db.update_automation_rule(
                int(rule["id"]), enabled=not bool(rule.get("enabled"))
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("切换规则状态失败")
            self.status_label.setText(f"切换规则状态失败：{exc}")
            return
        self.reload()

    def _delete_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        answer = QMessageBox.question(
            self,
            "删除规则",
            f"确定删除规则“{rule.get('name')}”吗？执行历史将保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._db.delete_automation_rule(int(rule["id"]))
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除自动化规则失败")
            self.status_label.setText(f"删除规则失败：{exc}")
            return
        self.status_label.setText("规则已删除")
        self.reload()
