"""数据库备份与启动时安全恢复测试（不涉及真实用户数据）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dlrecorder.infrastructure.backup import (
    RESTORE_REQUEST_NAME,
    apply_pending_restore,
    prepare_restore,
    verify_sqlite,
)
from dlrecorder.infrastructure.db import Database


def _room_ids(db_path: Path) -> list[str]:
    db = Database(db_path)
    try:
        return [room.web_rid for room in db.list_rooms()]
    finally:
        db.close()


def test_backup_creates_consistent_copy(tmp_path):
    db = Database(tmp_path / "data" / "app.db")
    db.add_room("backup-room", "备份主播")
    target = tmp_path / "data" / "backups" / "manual.db"
    try:
        dest = db.backup_to(target)
    finally:
        db.close()

    assert dest == target
    ok, message = verify_sqlite(dest)
    assert ok, message
    assert _room_ids(dest) == ["backup-room"]


def test_backup_overwrites_existing_target(tmp_path):
    db = Database(tmp_path / "app.db")
    target = tmp_path / "backup.db"
    try:
        db.add_room("first", "第一次")
        db.backup_to(target)
        db.add_room("second", "第二次")
        db.backup_to(target)
    finally:
        db.close()

    assert _room_ids(target) == ["first", "second"]


def test_prepare_and_apply_restore_keeps_safety_copy(tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    db = Database(db_path)
    db.add_room("original", "原始房间")
    backup = db.backup_to(tmp_path / "backup.db")
    db.add_room("added-later", "后来新增")
    db.close()

    prepare_restore(backup, data_dir)
    result = apply_pending_restore(data_dir)

    assert result is not None and result.applied is True
    assert result.safety_copy is not None and result.safety_copy.is_file()
    assert not (data_dir / RESTORE_REQUEST_NAME).exists()
    # 恢复后的数据库只包含备份时的房间，新增房间被回退（安全副本仍保留）。
    assert _room_ids(db_path) == ["original"]
    safety_rooms = _room_ids(result.safety_copy)
    assert safety_rooms == ["original", "added-later"]


def test_apply_without_request_returns_none(tmp_path):
    assert apply_pending_restore(tmp_path) is None


def test_prepare_restore_rejects_non_database_file(tmp_path):
    invalid = tmp_path / "not-a-db.db"
    invalid.write_text("hello", encoding="utf-8")

    with pytest.raises(ValueError):
        prepare_restore(invalid, tmp_path / "data")

    ok, _message = verify_sqlite(invalid)
    assert ok is False


def test_apply_failure_keeps_current_database_and_retires_request(tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    db = Database(db_path)
    db.add_room("keep-me", "保留")
    backup = db.backup_to(tmp_path / "backup.db")
    db.close()

    prepare_restore(backup, data_dir)
    backup.unlink()

    result = apply_pending_restore(data_dir)

    assert result is not None and result.applied is False
    assert "恢复失败" in result.message
    assert not (data_dir / RESTORE_REQUEST_NAME).exists()
    assert (data_dir / "restore_request.failed.json").is_file()
    assert _room_ids(db_path) == ["keep-me"]


def test_restore_request_file_contains_absolute_backup_path(tmp_path):
    db = Database(tmp_path / "data" / "app.db")
    db.add_room("request-room", "请求")
    backup = db.backup_to(tmp_path / "backup.db")
    db.close()

    request = prepare_restore(backup, tmp_path / "data")
    payload = json.loads(request.read_text(encoding="utf-8"))

    assert Path(payload["backup_path"]).is_absolute()


def test_application_startup_applies_restore(tmp_path, qt_app):
    """真实启动流程冒烟：恢复请求在下次启动、打开数据库之前生效。"""
    from dlrecorder.application.app import Application

    application = Application(base_dir=tmp_path)
    try:
        application.service.add_room("before-backup", "备份前")
        backup = tmp_path / "data" / "backups" / "manual.db"
        application.db.backup_to(backup)
        application.service.add_room("after-backup", "备份后")
        prepare_restore(backup, application.config.data_dir)
    finally:
        application.shutdown()

    restored = Application(base_dir=tmp_path)
    try:
        assert {room.web_rid for room in restored.db.list_rooms()} == {"before-backup"}
        safety_copies = list((tmp_path / "data" / "backups").glob("pre-restore-*.db"))
        assert len(safety_copies) == 1
        assert set(_room_ids(safety_copies[0])) == {
            "before-backup",
            "after-backup",
        }
    finally:
        restored.shutdown()


def test_verify_sqlite_rejects_database_without_required_tables(tmp_path):
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE rooms (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    ok, message = verify_sqlite(path)
    assert ok is False
    assert "数据表" in message


def test_database_copy_includes_wal_data(tmp_path):
    """异常退出后 WAL 中已提交的数据必须进入安全副本。"""
    from dlrecorder.infrastructure.backup import _copy_database

    data_dir = tmp_path / "data"
    db = Database(data_dir / "app.db")
    copy = tmp_path / "safety.db"
    try:
        db.add_room("wal-room", "WAL 房间")
        # 保持连接打开，模拟异常退出后主库仍依赖 -wal 的状态。
        _copy_database(data_dir / "app.db", copy)
    finally:
        db.close()

    ok, message = verify_sqlite(copy)
    assert ok, message
    assert _room_ids(copy) == ["wal-room"]
