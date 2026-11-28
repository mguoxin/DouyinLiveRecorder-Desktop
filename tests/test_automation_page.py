"""自动化页面与规则对话框测试。"""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QTime

from dlrecorder.infrastructure.db import Database
from dlrecorder.presentation.automation_page import AutomationPage, _RuleDialog


@pytest.fixture
def page(tmp_path: Path, qt_app):
    db = Database(tmp_path / "data" / "app.db")
    db.add_room("rid-auto", "自动化主播", "anchor-auto")
    widget = AutomationPage(db)
    try:
        yield db, widget
    finally:
        widget.deleteLater()
        db.close()


def test_page_lists_rules_and_runs(page):
    db, widget = page
    rule_id = db.create_automation_rule(
        name="晚间监控", rule_type="monitor_schedule",
        room_id=1, config={"start": "20:00", "end": "23:00"}, priority=5,
    )
    run_id = db.create_rule_run(rule_id, 1, status="running")
    db.finish_rule_run(run_id, "succeeded", "进入定时监控时段")

    widget.reload()
    assert widget.table.rowCount() == 1
    assert widget.table.item(0, 1).text() == "晚间监控"
    assert widget.table.item(0, 3).text() == "自动化主播"
    assert widget.run_table.rowCount() == 1
    assert widget.run_table.item(0, 2).text() == "成功"
    assert "成功" in widget.table.item(0, 5).text()


def test_toggle_and_delete_rule(page):
    db, widget = page
    rule_id = db.create_automation_rule(
        name="磁盘守护", rule_type="disk_guard", config={"min_free_mb": 1024},
    )
    widget.reload()
    widget.table.selectRow(0)

    widget._toggle_enabled()
    assert db.get_automation_rule(rule_id)["enabled"] == 0
    widget.table.selectRow(0)
    widget._toggle_enabled()
    assert db.get_automation_rule(rule_id)["enabled"] == 1

    widget.table.selectRow(0)
    widget.btn_delete.setEnabled(True)
    db.delete_automation_rule(rule_id)
    widget.reload()
    assert widget.table.rowCount() == 0


def test_rule_dialog_builds_type_specific_config(page):
    db, widget = page
    rooms = db.list_rooms()

    dialog = _RuleDialog(rooms, None, widget)
    try:
        dialog.name_edit.setText("午间录制")
        dialog.type_combo.setCurrentIndex(
            dialog.type_combo.findData("recording_schedule")
        )
        for check in dialog.day_checks:
            check.setChecked(True)
        dialog.start_edit.setTime(QTime(12, 0))
        dialog.end_edit.setTime(QTime(14, 0))
        config = dialog.build_config()
        assert config["days"] == [0, 1, 2, 3, 4, 5, 6]
        assert config["start"] == "12:00"
        assert config["end"] == "14:00"
        values = dialog.values()
        assert values["room_id"] is None
        assert values["priority"] == 100
    finally:
        dialog.deleteLater()

    disk = _RuleDialog(rooms, None, widget)
    try:
        disk.type_combo.setCurrentIndex(disk.type_combo.findData("disk_guard"))
        disk.min_free_spin.setValue(4096)
        assert disk.build_config() == {"min_free_mb": 4096.0}
    finally:
        disk.deleteLater()

    post = _RuleDialog(rooms, None, widget)
    try:
        post.type_combo.setCurrentIndex(post.type_combo.findData("post_process"))
        post.tags_edit.setText("精华, 高光，")
        post.favorite_check.setChecked(True)
        assert post.build_config() == {"tags": ["精华", "高光"], "favorite": True}
    finally:
        post.deleteLater()


def test_rule_dialog_requires_at_least_one_day(page):
    db, widget = page
    dialog = _RuleDialog(db.list_rooms(), None, widget)
    try:
        dialog.type_combo.setCurrentIndex(
            dialog.type_combo.findData("monitor_schedule")
        )
        for check in dialog.day_checks:
            check.setChecked(False)
        with pytest.raises(ValueError):
            dialog.build_config()
    finally:
        dialog.deleteLater()


def test_rule_dialog_loads_existing_rule(page):
    db, widget = page
    rule_id = db.create_automation_rule(
        name="原有规则", rule_type="archive", room_id=1,
        config={"enabled": False}, priority=7,
    )
    dialog = _RuleDialog(db.list_rooms(), db.get_automation_rule(rule_id), widget)
    try:
        assert dialog.name_edit.text() == "原有规则"
        assert dialog.room_combo.currentData() == 1
        assert dialog.priority_spin.value() == 7
        assert dialog.archive_check.isChecked() is False
        values = dialog.values()
        assert values["config"] == {"enabled": False}
        assert values["room_id"] == 1
    finally:
        dialog.deleteLater()
