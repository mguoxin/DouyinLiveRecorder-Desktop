"""Schema V3 迁移与归档数据库 API 测试。"""
import sqlite3
from pathlib import Path

import pytest

from dlrecorder.infrastructure.db import Database

V2_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    web_rid TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    anchor_id TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE monitor_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    web_rid_snapshot TEXT NOT NULL DEFAULT '',
    display_name_snapshot TEXT NOT NULL DEFAULT '',
    anchor_id_snapshot TEXT NOT NULL DEFAULT '',
    snapshot_source TEXT NOT NULL DEFAULT 'unknown'
        CHECK(snapshot_source IN ('captured', 'migration_backfill', 'unknown'))
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    session_id INTEGER REFERENCES monitor_sessions(id) ON DELETE NO ACTION,
    started_at TEXT,
    ended_at TEXT,
    file_path TEXT,
    file_size INTEGER,
    status TEXT,
    web_rid_snapshot TEXT NOT NULL DEFAULT '',
    display_name_snapshot TEXT NOT NULL DEFAULT '',
    anchor_id_snapshot TEXT NOT NULL DEFAULT '',
    snapshot_source TEXT NOT NULL DEFAULT 'unknown'
        CHECK(snapshot_source IN ('captured', 'migration_backfill', 'unknown'))
);
"""


def _create_v2_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(V2_SCHEMA)
        conn.execute("INSERT INTO schema_version(version) VALUES (2)")
        conn.execute(
            "INSERT INTO rooms(id, web_rid, display_name, anchor_id, enabled,"
            " created_at, updated_at) VALUES (1, 'rid-1', '主播一', 'anchor-1', 1,"
            " '2025-01-01', '2025-01-02')"
        )
        conn.execute(
            "INSERT INTO monitor_sessions(id, room_id, started_at, status,"
            " web_rid_snapshot, display_name_snapshot, anchor_id_snapshot,"
            " snapshot_source) VALUES (10, 1, '2025-01-01', 'stopped', 'rid-1',"
            " '主播一', 'anchor-1', 'captured')"
        )
        conn.executemany(
            "INSERT INTO recordings(id, room_id, session_id, started_at, ended_at,"
            " file_path, file_size, status, web_rid_snapshot, display_name_snapshot,"
            " anchor_id_snapshot, snapshot_source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (100, 1, 10, "2025-01-01", "2025-01-01", "C:/rec/a.ts", 1024,
                 "completed", "rid-1", "主播一", "anchor-1", "captured"),
                (101, 1, 10, "2025-01-02", None, "C:/rec/b.ts", 2048,
                 "recording", "rid-1", "主播一", "anchor-1", "captured"),
            ),
        )
        conn.execute(
            "INSERT INTO settings(key, value, updated_at)"
            " VALUES ('interval', '120', '2025-01-01')"
        )
        conn.commit()
    finally:
        conn.close()


def test_v2_database_upgrades_to_v3_and_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _create_v2_database(path)

    db = Database(path)
    try:
        conn = db._get_conn()
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1
        first = conn.execute(
            "SELECT * FROM recordings WHERE id=100"
        ).fetchone()
        assert first["file_path"] == "C:/rec/a.ts"
        assert first["file_size"] == 1024
        assert first["status"] == "completed"
        assert first["archive_status"] == "none"
        assert first["archive_attempts"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM recordings WHERE archive_status != 'none'"
        ).fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        job_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(archive_jobs)")
        }
        assert {"recording_id", "source_path", "status", "sha256"}.issubset(job_columns)
        backups = list((tmp_path / "migration_backups").glob("app_v2_to_v3_*.db"))
        assert len(backups) == 1
        backup = sqlite3.connect(backups[0])
        try:
            assert backup.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0] == 2
            assert backup.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 2
            assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            backup.close()
    finally:
        db.close()


def test_v2_to_v3_migration_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "app.db"
    _create_v2_database(path)

    def fail_verification(*args, **kwargs) -> None:
        raise RuntimeError("forced v3 verification failure")

    monkeypatch.setattr(Database, "_verify_v3_migration", staticmethod(fail_verification))
    with pytest.raises(RuntimeError, match="forced v3 verification failure"):
        Database(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(recordings)")
        }
        assert "archive_status" not in columns
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='archive_jobs'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 2
        assert conn.execute(
            "SELECT file_path FROM recordings WHERE id=100"
        ).fetchone()[0] == "C:/rec/a.ts"
        assert [tuple(row) for row in conn.execute("PRAGMA integrity_check")] == [("ok",)]
    finally:
        conn.close()
    backups = list((tmp_path / "migration_backups").glob("app_v2_to_v3_*.db"))
    assert len(backups) == 1


def test_reopening_v3_database_creates_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _create_v2_database(path)
    first = Database(path)
    first.close()
    backups_after_upgrade = list((tmp_path / "migration_backups").glob("*.db"))
    assert len(backups_after_upgrade) == 1

    second = Database(path)
    try:
        assert second._get_conn().execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0] == 3
    finally:
        second.close()
    assert list((tmp_path / "migration_backups").glob("*.db")) == backups_after_upgrade


def _make_v3_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "v3.db")


def test_recording_archive_status_updates_are_guarded(tmp_path: Path) -> None:
    db = _make_v3_db(tmp_path)
    try:
        room_id = db.add_room("rid-archive")
        recording_id = db.start_recording(room_id, "C:/rec/a.ts")
        db.finish_recording(recording_id, "completed", 4096)

        assert db.set_recording_archive(
            recording_id, "pending", expected_status="none"
        ) is True
        # 状态已经变化，旧预期的条件更新必须失败。
        assert db.set_recording_archive(
            recording_id, "copying", expected_status="none"
        ) is False
        assert db.set_recording_archive(
            recording_id,
            "failed",
            error="disk full",
            increment_attempts=True,
        ) is True
        row = db.get_recording(recording_id)
        assert row["archive_status"] == "failed"
        assert row["archive_error"] == "disk full"
        assert row["archive_attempts"] == 1
        assert row["file_path"] == "C:/rec/a.ts"
        assert row["file_size"] == 4096
    finally:
        db.close()


def test_invalid_archive_status_is_rejected(tmp_path: Path) -> None:
    db = _make_v3_db(tmp_path)
    try:
        room_id = db.add_room("rid-invalid")
        recording_id = db.start_recording(room_id, "C:/rec/a.ts")
        with pytest.raises(sqlite3.IntegrityError):
            db.set_recording_archive(recording_id, "unknown-status")
        with pytest.raises(sqlite3.IntegrityError):
            db.create_archive_job(recording_id, "C:/rec/a.ts", status="bogus")
    finally:
        db.close()


def test_archive_job_lifecycle_and_lookup(tmp_path: Path) -> None:
    db = _make_v3_db(tmp_path)
    try:
        room_id = db.add_room("rid-job")
        recording_id = db.start_recording(room_id, "C:/rec/source.ts")
        db.finish_recording(recording_id, "completed", 8192)

        job_id = db.create_archive_job(recording_id, "C:/rec/source.ts")
        job = db.get_archive_job(job_id)
        assert job["status"] == "pending"
        assert job["attempts"] == 0
        assert job["target_path"] is None
        assert job["created_at"] == job["updated_at"]

        assert db.update_archive_job(
            job_id,
            "copying",
            target_path="D:/archive/a.ts",
            bytes_total=8192,
            increment_attempts=True,
        ) is True
        assert db.update_archive_job(
            job_id, "verifying", sha256="a" * 64
        ) is True
        assert db.update_archive_job(job_id, "archived") is True

        job = db.get_archive_job(job_id)
        assert job["status"] == "archived"
        assert job["attempts"] == 1
        assert job["bytes_total"] == 8192
        assert job["sha256"] == "a" * 64
        assert db.get_latest_archive_job(recording_id)["id"] == job_id
        assert [row["id"] for row in db.list_archive_jobs(["archived"])] == [job_id]
        assert db.list_archive_jobs(["failed"]) == []
        assert db.update_archive_job(9999, "failed") is False
    finally:
        db.close()


def test_archive_jobs_cascade_when_recording_deleted(tmp_path: Path) -> None:
    db = _make_v3_db(tmp_path)
    try:
        room_id = db.add_room("rid-cascade")
        recording_id = db.start_recording(room_id, "C:/rec/a.ts")
        job_id = db.create_archive_job(recording_id, "C:/rec/a.ts")
        db._get_conn().execute("DELETE FROM recordings WHERE id=?", (recording_id,))
        assert db.get_archive_job(job_id) is None
        assert db.get_recording(recording_id) is None
    finally:
        db.close()


def test_list_recordings_by_archive_status(tmp_path: Path) -> None:
    db = _make_v3_db(tmp_path)
    try:
        room_id = db.add_room("rid-list")
        first = db.start_recording(room_id, "C:/rec/a.ts")
        second = db.start_recording(room_id, "C:/rec/b.ts")
        db.finish_recording(first, "completed", 10)
        db.finish_recording(second, "completed", 20)
        db.set_recording_archive(first, "pending")
        db.set_recording_archive(second, "archived")

        pending = db.list_recordings_by_archive_status(["pending"])
        assert [row["id"] for row in pending] == [first]
        both = db.list_recordings_by_archive_status(["pending", "archived"])
        assert {row["id"] for row in both} == {first, second}
        assert db.list_recordings_by_archive_status([]) == []
    finally:
        db.close()
