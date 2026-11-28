"""SQLite 数据库读写、V2 迁移及历史数据治理测试。"""
import logging
import sqlite3
from pathlib import Path

import pytest

from dlrecorder.infrastructure.db import Database, MigrationPostCommitError


def test_update_room_changes_identity(tmp_path):
    db = Database(tmp_path / "update.db")
    try:
        room_id = db.add_room("old-rid", "旧名称", "anchor-1")
        db.update_room(room_id, web_rid="new-rid", display_name="新名称")
        room = db.get_room(room_id)
        assert room.web_rid == "new-rid"
        assert room.display_name == "新名称"
        assert room.anchor_id == "anchor-1"
    finally:
        db.close()


def test_update_room_duplicate_rid_raises(tmp_path):
    db = Database(tmp_path / "update-dup.db")
    try:
        db.add_room("taken-rid")
        room_id = db.add_room("free-rid")
        with pytest.raises(sqlite3.IntegrityError):
            db.update_room(room_id, web_rid="taken-rid")
        assert db.get_room(room_id).web_rid == "free-rid"
    finally:
        db.close()


def test_update_missing_room_raises_key_error(tmp_path):
    db = Database(tmp_path / "update-missing.db")
    try:
        with pytest.raises(KeyError):
            db.update_room(9999, display_name="任意")
    finally:
        db.close()


def test_recording_lifecycle_persists_snapshot(tmp_path):
    db = Database(tmp_path / "rec.db")
    try:
        room_id = db.add_room("snap-rec", "录制主播", "anchor-9")
        recording_id = db.start_recording(room_id, "C:/recordings/a.ts")
        row = db.get_recording(recording_id)
        assert row["room_id"] == room_id
        assert row["status"] == "recording"
        assert row["file_path"] == "C:/recordings/a.ts"
        assert row["ended_at"] is None
        assert row["web_rid_snapshot"] == "snap-rec"
        assert row["display_name_snapshot"] == "录制主播"
        assert row["anchor_id_snapshot"] == "anchor-9"
        assert row["snapshot_source"] == "captured"

        assert db.finish_recording(recording_id, "completed", 4096) is True
        row = db.get_recording(recording_id)
        assert row["status"] == "completed"
        assert row["file_size"] == 4096
        assert row["ended_at"] is not None

        # 幂等：已完成记录不再被改写
        assert db.finish_recording(recording_id, "failed", 1) is False
        assert db.get_recording(recording_id)["status"] == "completed"
    finally:
        db.close()


def test_start_recording_requires_existing_room(tmp_path):
    db = Database(tmp_path / "rec-missing.db")
    try:
        with pytest.raises(KeyError):
            db.start_recording(9999, "C:/recordings/x.ts")
        assert db.list_recordings() == []
    finally:
        db.close()


def test_interrupt_recording_preserves_path_and_size(tmp_path):
    db = Database(tmp_path / "rec-interrupt.db")
    try:
        room_id = db.add_room("interrupt-rid")
        recording_id = db.start_recording(room_id, "C:/recordings/old.ts")
        assert db.interrupt_recording(recording_id, 123) is True
        row = db.get_recording(recording_id)
        assert row["status"] == "interrupted"
        assert row["file_path"] == "C:/recordings/old.ts"
        assert row["file_size"] == 123
        assert row["ended_at"] is not None
        assert db.interrupt_recording(recording_id, 1) is False
    finally:
        db.close()


def test_list_recordings_filters_and_orders(tmp_path):
    db = Database(tmp_path / "rec-list.db")
    try:
        first_room = db.add_room("list-a")
        second_room = db.add_room("list-b")
        first = db.start_recording(first_room, "C:/a.ts")
        second = db.start_recording(second_room, "C:/b.ts")
        db.finish_recording(first, "completed", 10)

        all_rows = db.list_recordings()
        assert [row["id"] for row in all_rows] == [second, first]
        assert len(db.list_recordings(room_id=first_room)) == 1
        assert len(db.list_recordings(status="recording")) == 1
        assert db.list_recordings(limit=1)[0]["id"] == second
    finally:
        db.close()


def test_delete_room_keeps_recording_history(tmp_path):
    db = Database(tmp_path / "rec-keep.db")
    try:
        room_id = db.add_room("keep-rid", "删除前主播")
        recording_id = db.start_recording(room_id, "C:/recordings/keep.ts")
        db.delete_room(room_id)

        row = db.get_recording(recording_id)
        assert row is not None
        assert row["room_id"] is None
        assert row["file_path"] == "C:/recordings/keep.ts"
        assert row["web_rid_snapshot"] == "keep-rid"
        assert row["display_name_snapshot"] == "删除前主播"
    finally:
        db.close()


V1_SCHEMA = """
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
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    error_message TEXT
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER REFERENCES rooms(id),
    session_id INTEGER REFERENCES monitor_sessions(id),
    started_at TEXT,
    ended_at TEXT,
    file_path TEXT,
    file_size INTEGER,
    status TEXT
);
"""


def _create_v1_database(path: Path, with_objects: bool = False) -> None:
    """建立有主键、历史数据及自定义对象的独立 V1 测试库。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO schema_version(version) VALUES (1)")
    rooms = (
        (7, "rid-old-7", "旧主播7", "anchor-7", 1, "2024-01-01", "2024-01-02"),
        (9, "rid-old-9", "旧主播9", "anchor-9", 1, "2024-02-01", "2024-02-02"),
    )
    conn.executemany(
        "INSERT INTO rooms(id, web_rid, display_name, anchor_id, enabled, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        rooms,
    )
    conn.executemany(
        "INSERT INTO monitor_sessions(id, room_id, started_at, ended_at, status, error_message)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            (101, 7, "2024-03-01", "2024-03-02", "stopped", "old-error"),
            (102, 9, "2024-04-01", None, "running", None),
        ),
    )
    conn.executemany(
        "INSERT INTO recordings(id, room_id, session_id, started_at, ended_at,"
        " file_path, file_size, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (201, 7, 101, "2024-03-01", "2024-03-02", "C:/old/a.ts", 123, "completed"),
            (202, None, 102, "2024-04-01", None, "relative/b.ts", 456, "recording"),
            (203, None, None, None, None, None, None, "unknown-status"),
        ),
    )
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES ('interval', '120', '2024-01-01')"
    )
    if with_objects:
        conn.executescript(
            """
            CREATE INDEX idx_monitor_sessions_room ON monitor_sessions(room_id);
            CREATE INDEX idx_recordings_session ON recordings(session_id);
            CREATE TRIGGER recordings_no_delete
            BEFORE DELETE ON recordings
            BEGIN
                SELECT RAISE(ABORT, 'recording delete blocked');
            END;
            CREATE VIEW recording_history_view AS
                SELECT id, room_id, session_id, file_path FROM recordings;
            """
        )
    conn.execute("UPDATE sqlite_sequence SET seq=999 WHERE name='recordings'")
    conn.commit()
    conn.close()


def _read_database_state(path: Path) -> dict:
    """读取业务数据、Schema、序列和外键，用于逐值比较。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            table: [
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY {order_column}"
                )
            ]
            for table, order_column in (
                ("schema_version", "version"),
                ("rooms", "id"),
                ("monitor_sessions", "id"),
                ("recordings", "id"),
                ("settings", "key"),
            )
        }
        return {
            "tables": tables,
            "objects": [
                tuple(row)
                for row in conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                )
            ],
            "sequences": [
                tuple(row)
                for row in conn.execute(
                    "SELECT name, seq FROM sqlite_sequence ORDER BY name"
                )
            ],
            "foreign_keys": {
                table: [
                    tuple(row)
                    for row in conn.execute(f"PRAGMA foreign_key_list({table})")
                ]
                for table in ("rooms", "monitor_sessions", "recordings", "settings")
            },
        }
    finally:
        conn.close()


_V3_INDEXES = {
    "idx_recordings_archive_status",
    "idx_archive_jobs_status",
    "idx_archive_jobs_recording",
}


def _custom_object_definitions(state: dict) -> list[tuple]:
    """只筛选有定义 SQL 的自定义索引，以及用户触发器和视图。"""
    return [
        obj
        for obj in state["objects"]
        if obj[0] in {"trigger", "view"}
        or (
            obj[0] == "index"
            and obj[2] in {"monitor_sessions", "recordings"}
            and obj[3] is not None
            and obj[1] not in _V3_INDEXES
        )
    ]


class _CommitCursorProxy:
    """让生产迁移游标的 connection 指向提交故障代理。"""

    def __init__(self, cursor: sqlite3.Cursor, connection: "_CommitConnectionProxy") -> None:
        self._cursor = cursor
        self._connection_proxy = connection

    @property
    def connection(self) -> "_CommitConnectionProxy":
        return self._connection_proxy

    def execute(self, *args, **kwargs) -> "_CommitCursorProxy":
        self._cursor.execute(*args, **kwargs)
        return self

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _CommitConnectionProxy:
    """在真实 commit 或 rollback 后抛出提交调用异常。"""

    def __init__(self, connection: sqlite3.Connection, commit_mode: str) -> None:
        self._connection = connection
        self._commit_mode = commit_mode
        self.close_count = 0

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._connection.row_factory = value

    def cursor(self) -> _CommitCursorProxy:
        return _CommitCursorProxy(self._connection.cursor(), self)

    def commit(self) -> None:
        if self._commit_mode == "success":
            self._connection.commit()
        else:
            self._connection.rollback()
        raise sqlite3.OperationalError(f"forced commit {self._commit_mode} exception")

    def close(self) -> None:
        self.close_count += 1
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def _install_commit_failure(
    monkeypatch: pytest.MonkeyPatch,
    commit_mode: str,
) -> list[_CommitConnectionProxy]:
    """只代理 Database 主连接，备份及后续连接保持真实。"""
    real_connect = sqlite3.connect
    proxies: list[_CommitConnectionProxy] = []

    def fake_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        if proxies:
            return connection
        proxy = _CommitConnectionProxy(connection, commit_mode)
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr("dlrecorder.infrastructure.db.sqlite3.connect", fake_connect)
    return proxies


def _insert_captured_recording(
    db: Database,
    room_id: int,
    session_id: int,
    file_path: str | None = None,
) -> int:
    """在不实现录制功能的前提下，写入一条创建时捕获身份的元数据。"""
    conn = db._get_conn()
    room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    cur = conn.execute(
        """
        INSERT INTO recordings(
            room_id, session_id, started_at, file_path, status,
            web_rid_snapshot, display_name_snapshot, anchor_id_snapshot,
            snapshot_source
        ) VALUES (?, ?, '2025-01-01', ?, 'completed', ?, ?, ?, 'captured')
        """,
        (
            room_id,
            session_id,
            file_path,
            room["web_rid"],
            room["display_name"],
            room["anchor_id"],
        ),
    )
    return int(cur.lastrowid)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def test_schema_version(db: Database) -> None:
    row = db._get_conn().execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == 3


def test_add_and_list_room(db: Database) -> None:
    rid = db.add_room("12345", "测试主播")
    assert rid > 0
    rooms = db.list_rooms()
    assert len(rooms) == 1
    assert rooms[0].web_rid == "12345"
    assert rooms[0].display_name == "测试主播"
    assert rooms[0].enabled is True


def test_unique_web_rid(db: Database) -> None:
    db.add_room("12345")
    with pytest.raises(Exception):
        db.add_room("12345")


def test_delete_room(db: Database) -> None:
    rid = db.add_room("12345")
    db.delete_room(rid)
    assert db.list_rooms() == []


def test_set_room_enabled(db: Database) -> None:
    rid = db.add_room("12345")
    db.set_room_enabled(rid, False)
    room = db.get_room(rid)
    assert room is not None
    assert room.enabled is False


def test_settings(db: Database) -> None:
    db.set_setting("interval", "120")
    assert db.get_setting("interval") == "120"
    assert db.get_setting("nonexistent", "default") == "default"


def test_sessions(db: Database) -> None:
    rid = db.add_room("12345")
    sid = db.start_session(rid)
    assert sid > 0
    db.end_session(sid, status="stopped")
    row = db._get_conn().execute(
        "SELECT status, ended_at FROM monitor_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row["status"] == "stopped"
    assert row["ended_at"] is not None


def test_end_session_is_idempotent(db: Database) -> None:
    rid = db.add_room("idempotent-session")
    sid = db.start_session(rid)
    db.end_session(sid, status="stopped")
    before = dict(
        db._get_conn().execute(
            "SELECT * FROM monitor_sessions WHERE id=?", (sid,)
        ).fetchone()
    )

    db.end_session(sid, status="error", error="late failure")
    db.end_session(sid, status="stopped")

    after = dict(
        db._get_conn().execute(
            "SELECT * FROM monitor_sessions WHERE id=?", (sid,)
        ).fetchone()
    )
    assert after == before


def test_persistence_across_reopen(tmp_path: Path) -> None:
    """数据在重新打开数据库后仍存在。"""
    p = tmp_path / "persist.db"
    db1 = Database(p)
    db1.add_room("999", "持久化测试")
    db1.set_setting("k", "v")
    db1.close()

    db2 = Database(p)
    rooms = db2.list_rooms()
    assert len(rooms) == 1
    assert rooms[0].web_rid == "999"
    assert db2.get_setting("k") == "v"
    db2.close()


def test_new_database_directly_initializes_latest_schema(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    db = Database(path)
    conn = db._get_conn()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    for table in ("monitor_sessions", "recordings"):
        columns = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        assert columns["room_id"]["notnull"] == 0
        assert {
            "web_rid_snapshot", "display_name_snapshot",
            "anchor_id_snapshot", "snapshot_source",
        }.issubset(columns)
    recording_columns = {
        row["name"]: row for row in conn.execute("PRAGMA table_info(recordings)")
    }
    assert {
        "archive_status", "archive_path", "archived_at",
        "archive_error", "archive_attempts",
    }.issubset(recording_columns)
    assert recording_columns["archive_status"]["notnull"] == 1
    assert recording_columns["archive_status"]["dflt_value"].strip("'\"") == "none"
    job_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(archive_jobs)")
    }
    assert {
        "id", "recording_id", "source_path", "target_path", "status",
        "attempts", "bytes_total", "sha256", "error", "created_at", "updated_at",
    }.issubset(job_columns)
    indexes = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert _V3_INDEXES.issubset(indexes)
    job_fks = conn.execute("PRAGMA foreign_key_list(archive_jobs)").fetchall()
    assert any(
        fk["from"] == "recording_id"
        and fk["table"] == "recordings"
        and fk["on_delete"] == "CASCADE"
        for fk in job_fks
    )
    assert not (tmp_path / "migration_backups").exists()
    db.close()


def test_v1_migration_preserves_history_snapshots_and_objects(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _create_v1_database(path, with_objects=True)
    before_state = _read_database_state(path)
    db = Database(path)
    conn = db._get_conn()
    after_state = _read_database_state(path)

    assert _custom_object_definitions(after_state) == _custom_object_definitions(before_state)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM monitor_sessions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1
    assert [row[0] for row in conn.execute("SELECT id FROM monitor_sessions ORDER BY id")] == [101, 102]
    assert [row[0] for row in conn.execute("SELECT id FROM recordings ORDER BY id")] == [201, 202, 203]

    session = conn.execute("SELECT * FROM monitor_sessions WHERE id=101").fetchone()
    assert session["started_at"] == "2024-03-01"
    assert session["ended_at"] == "2024-03-02"
    assert session["status"] == "stopped"
    assert session["error_message"] == "old-error"
    assert session["web_rid_snapshot"] == "rid-old-7"
    assert session["display_name_snapshot"] == "旧主播7"
    assert session["anchor_id_snapshot"] == "anchor-7"
    assert session["snapshot_source"] == "migration_backfill"

    direct = conn.execute("SELECT * FROM recordings WHERE id=201").fetchone()
    assert direct["file_path"] == "C:/old/a.ts"
    assert direct["file_size"] == 123
    assert direct["status"] == "completed"
    assert direct["web_rid_snapshot"] == "rid-old-7"
    assert direct["snapshot_source"] == "migration_backfill"

    via_session = conn.execute("SELECT * FROM recordings WHERE id=202").fetchone()
    assert via_session["room_id"] is None
    assert via_session["session_id"] == 102
    assert via_session["file_path"] == "relative/b.ts"
    assert via_session["web_rid_snapshot"] == "rid-old-9"
    assert via_session["display_name_snapshot"] == "旧主播9"
    assert via_session["snapshot_source"] == "migration_backfill"

    unknown = conn.execute("SELECT * FROM recordings WHERE id=203").fetchone()
    assert unknown["web_rid_snapshot"] == ""
    assert unknown["display_name_snapshot"] == ""
    assert unknown["anchor_id_snapshot"] == ""
    assert unknown["snapshot_source"] == "unknown"

    # V3 归档字段回填：历史记录一律为 none，未归档，尝试次数 0。
    for recording_id in (201, 202, 203):
        archived = conn.execute(
            "SELECT archive_status, archive_path, archived_at, archive_error,"
            " archive_attempts FROM recordings WHERE id=?",
            (recording_id,),
        ).fetchone()
        assert archived["archive_status"] == "none"
        assert archived["archive_path"] is None
        assert archived["archived_at"] is None
        assert archived["archive_error"] is None
        assert archived["archive_attempts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_jobs").fetchone()[0] == 0

    objects = {
        (row["type"], row["name"])
        for row in conn.execute("SELECT type, name FROM sqlite_master")
    }
    assert ("index", "idx_monitor_sessions_room") in objects
    assert ("index", "idx_recordings_session") in objects
    assert ("trigger", "recordings_no_delete") in objects
    assert ("view", "recording_history_view") in objects
    assert [
        row["name"] for row in conn.execute("PRAGMA index_info(idx_monitor_sessions_room)")
    ] == ["room_id"]
    assert [
        row["name"] for row in conn.execute("PRAGMA index_info(idx_recordings_session)")
    ] == ["session_id"]
    view_row = conn.execute(
        "SELECT id, room_id, session_id, file_path FROM recording_history_view WHERE id=201"
    ).fetchone()
    assert tuple(view_row) == (201, 7, 101, "C:/old/a.ts")
    with pytest.raises(sqlite3.IntegrityError, match="recording delete blocked"):
        conn.execute("DELETE FROM recordings WHERE id=201")
    assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 3
    assert conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='recordings'"
    ).fetchone()[0] >= 999
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    session_fk = conn.execute("PRAGMA foreign_key_list(monitor_sessions)").fetchone()
    assert session_fk["from"] == "room_id"
    assert session_fk["on_delete"] == "SET NULL"
    recording_fks = {
        row["from"]: row["on_delete"]
        for row in conn.execute("PRAGMA foreign_key_list(recordings)")
    }
    assert recording_fks == {"room_id": "SET NULL", "session_id": "NO ACTION"}
    db.close()

    reopened = Database(path)
    assert reopened._get_conn().execute(
        "SELECT web_rid_snapshot FROM recordings WHERE id=202"
    ).fetchone()[0] == "rid-old-9"
    reopened.close()


def test_migration_backup_is_independent_readable_and_preserves_existing(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _create_v1_database(path)
    backup_dir = tmp_path / "migration_backups"
    backup_dir.mkdir()
    existing = backup_dir / "existing-backup.db"
    existing.write_bytes(b"do-not-delete")

    db = Database(path)
    backups = list(backup_dir.glob("app_v1_to_v2_*.db"))
    assert len(backups) == 1
    assert backups[0] != path
    assert existing.read_bytes() == b"do-not-delete"
    db.close()

    backup = sqlite3.connect(backups[0])
    assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert backup.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    assert backup.execute("SELECT COUNT(*) FROM monitor_sessions").fetchone()[0] == 2
    assert backup.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 3
    backup.close()


def test_migration_failure_rolls_back_and_preserves_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "app.db"
    _create_v1_database(path, with_objects=True)
    before_state = _read_database_state(path)

    def fail_verification(*args) -> None:
        raise RuntimeError("forced migration verification failure")

    monkeypatch.setattr(Database, "_verify_v2_migration", fail_verification)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="forced migration verification failure"):
            Database(path)

    after_state = _read_database_state(path)
    assert after_state == before_state
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM monitor_sessions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 3
    assert conn.execute(
        "SELECT file_path FROM recordings WHERE id=201"
    ).fetchone()[0] == "C:/old/a.ts"
    session_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(monitor_sessions)")
    }
    assert "web_rid_snapshot" not in session_columns
    session_fk = conn.execute("PRAGMA foreign_key_list(monitor_sessions)").fetchone()
    assert session_fk["on_delete"] == "CASCADE"
    objects = {
        (row["type"], row["name"])
        for row in conn.execute("SELECT type, name FROM sqlite_master")
    }
    assert ("index", "idx_monitor_sessions_room") in objects
    assert ("trigger", "recordings_no_delete") in objects
    integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
    assert [row[0] for row in integrity_rows] == ["ok"]
    assert conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name IN ('new_monitor_sessions', 'new_recordings')"
    ).fetchall() == []
    conn.close()

    backups = list((tmp_path / "migration_backups").glob("app_v1_to_v2_*.db"))
    assert len(backups) == 1
    assert "原数据库事务已回滚" in caplog.text
    assert "验证 V2 数据、外键和数据库对象" in caplog.text


def test_backup_failure_aborts_before_schema_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "app.db"
    _create_v1_database(path)
    before_state = _read_database_state(path)

    def fail_backup(self, *args, **kwargs) -> None:
        raise RuntimeError("forced backup failure")

    monkeypatch.setattr(Database, "_create_migration_backup", fail_backup)
    with pytest.raises(RuntimeError, match="forced backup failure"):
        Database(path)

    assert _read_database_state(path) == before_state
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 3
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    conn.close()


def test_delete_room_preserves_history_and_disk_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "delete.db")
    room_id = db.add_room("delete-rid", "删除前主播", "delete-anchor")
    session_id = db.start_session(room_id)
    media_file = tmp_path / "history.ts"
    media_file.write_bytes(b"historical-media")
    recording_id = _insert_captured_recording(
        db, room_id, session_id, str(media_file)
    )

    db.delete_room(room_id)

    assert db.get_room(room_id) is None
    session = db._get_conn().execute(
        "SELECT * FROM monitor_sessions WHERE id=?", (session_id,)
    ).fetchone()
    recording = db._get_conn().execute(
        "SELECT * FROM recordings WHERE id=?", (recording_id,)
    ).fetchone()
    assert session["room_id"] is None
    assert session["web_rid_snapshot"] == "delete-rid"
    assert session["snapshot_source"] == "captured"
    assert recording["room_id"] is None
    assert recording["session_id"] == session_id
    assert recording["display_name_snapshot"] == "删除前主播"
    assert recording["file_path"] == str(media_file)
    assert media_file.read_bytes() == b"historical-media"
    assert db._get_conn().execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_readding_same_web_rid_does_not_rebind_old_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "readd.db")
    old_room_id = db.add_room("same-rid", "旧名称", "old-anchor")
    old_session_id = db.start_session(old_room_id)
    old_recording_id = _insert_captured_recording(db, old_room_id, old_session_id)
    db.delete_room(old_room_id)

    new_room_id = db.add_room("same-rid", "新名称", "new-anchor")
    assert new_room_id != old_room_id
    old_session = db._get_conn().execute(
        "SELECT * FROM monitor_sessions WHERE id=?", (old_session_id,)
    ).fetchone()
    old_recording = db._get_conn().execute(
        "SELECT * FROM recordings WHERE id=?", (old_recording_id,)
    ).fetchone()
    assert old_session["room_id"] is None
    assert old_recording["room_id"] is None
    assert old_session["display_name_snapshot"] == "旧名称"
    assert old_recording["anchor_id_snapshot"] == "old-anchor"

    new_session_id = db.start_session(new_room_id)
    new_session = db._get_conn().execute(
        "SELECT * FROM monitor_sessions WHERE id=?", (new_session_id,)
    ).fetchone()
    assert new_session["room_id"] == new_room_id
    assert new_session["display_name_snapshot"] == "新名称"
    assert new_session["snapshot_source"] == "captured"
    db.close()


def test_recordings_session_id_keeps_no_action(tmp_path: Path) -> None:
    db = Database(tmp_path / "no-action.db")
    room_id = db.add_room("no-action-rid")
    session_id = db.start_session(room_id)
    recording_id = _insert_captured_recording(db, room_id, session_id)

    with pytest.raises(sqlite3.IntegrityError):
        db._get_conn().execute("DELETE FROM monitor_sessions WHERE id=?", (session_id,))
    assert db._get_conn().execute(
        "SELECT COUNT(*) FROM monitor_sessions WHERE id=?", (session_id,)
    ).fetchone()[0] == 1
    assert db._get_conn().execute(
        "SELECT COUNT(*) FROM recordings WHERE id=?", (recording_id,)
    ).fetchone()[0] == 1

    db._get_conn().execute(
        "UPDATE recordings SET session_id=NULL WHERE id=?", (recording_id,)
    )
    db._get_conn().execute("DELETE FROM monitor_sessions WHERE id=?", (session_id,))
    assert db._get_conn().execute(
        "SELECT COUNT(*) FROM recordings WHERE id=?", (recording_id,)
    ).fetchone()[0] == 1
    db.close()


def test_new_session_snapshot_is_captured_and_not_dynamically_overwritten(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "snapshot.db")
    room_id = db.add_room("snapshot-rid", "原始名称", "original-anchor")
    session_id = db.start_session(room_id)
    db._get_conn().execute(
        "UPDATE rooms SET web_rid='changed-rid', display_name='变更名称',"
        " anchor_id='changed-anchor' WHERE id=?",
        (room_id,),
    )

    session = db._get_conn().execute(
        "SELECT * FROM monitor_sessions WHERE id=?", (session_id,)
    ).fetchone()
    assert session["web_rid_snapshot"] == "snapshot-rid"
    assert session["display_name_snapshot"] == "原始名称"
    assert session["anchor_id_snapshot"] == "original-anchor"
    assert session["snapshot_source"] == "captured"
    db.close()


def test_new_database_initialization_failure_rolls_back_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "interrupted.db"
    original_set_schema_version = Database._set_schema_version
    should_fail = True

    def fail_once_after_version(cur: sqlite3.Cursor) -> None:
        nonlocal should_fail
        original_set_schema_version(cur)
        if should_fail:
            should_fail = False
            raise RuntimeError("forced initialization failure after version write")

    monkeypatch.setattr(
        Database,
        "_set_schema_version",
        staticmethod(fail_once_after_version),
    )
    with pytest.raises(RuntimeError, match="forced initialization failure"):
        Database(path)

    conn = sqlite3.connect(path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"rooms", "monitor_sessions", "recordings", "settings"}.isdisjoint(tables)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0
    conn.close()

    db = Database(path)
    assert db._get_conn().execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.close()
    reference = Database(tmp_path / "reference.db")
    reference.close()
    assert _read_database_state(path) == _read_database_state(tmp_path / "reference.db")


def test_post_commit_foreign_key_failure_reports_committed_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "app.db"
    reference_path = tmp_path / "reference.db"
    _create_v1_database(path, with_objects=True)
    _create_v1_database(reference_path, with_objects=True)
    reference = Database(reference_path)
    reference.close()
    expected_state = _read_database_state(reference_path)

    def fail_foreign_key_restore(cur: sqlite3.Cursor) -> None:
        raise sqlite3.OperationalError("forced post-commit foreign key restore failure")

    monkeypatch.setattr(
        Database,
        "_restore_foreign_keys",
        staticmethod(fail_foreign_key_restore),
    )
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(MigrationPostCommitError, match="已经提交") as exc_info:
            Database(path)

    error = exc_info.value
    assert error.migration_committed is True
    assert error.schema_version == 2
    assert error.connection_closed is True
    assert "当前连接不能继续安全使用" in str(error)
    assert "回滚" not in str(error)
    assert "V2 已提交" in caplog.text
    assert "事务已回滚" not in caplog.text
    assert "数据库迁移完成" not in caplog.text

    # V1→V2 已提交但连接被关闭，V2→V3 尚未执行。
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 3
    conn.close()

    reopened = Database(path)
    conn = reopened._get_conn()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _read_database_state(path) == expected_state
    reopened.close()


def test_backup_internal_failure_preserves_primary_error_and_closes_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "app.db"
    _create_v1_database(path, with_objects=True)
    before_state = _read_database_state(path)
    real_connect = sqlite3.connect
    connect_calls = 0
    closed_roles: list[str] = []

    class BackupConnectionProxy:
        def __init__(self, connection: sqlite3.Connection, role: str) -> None:
            self._connection = connection
            self._role = role

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

        def backup(self, destination) -> None:
            raise RuntimeError("forced source.backup failure")

        def close(self) -> None:
            self._connection.close()
            closed_roles.append(self._role)
            raise RuntimeError(f"forced {self._role} close failure")

    def fake_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        connection = real_connect(*args, **kwargs)
        if connect_calls == 1:
            return connection
        role = "source" if connect_calls == 2 else "destination"
        return BackupConnectionProxy(connection, role)

    with caplog.at_level(logging.ERROR):
        with monkeypatch.context() as context:
            context.setattr("dlrecorder.infrastructure.db.sqlite3.connect", fake_connect)
            with pytest.raises(RuntimeError, match="forced source.backup failure") as exc_info:
                Database(path)

    assert connect_calls == 3
    assert "close failure" not in str(exc_info.value)
    assert closed_roles == ["source", "destination"]
    assert "关闭迁移备份源连接失败" in caplog.text
    assert "关闭迁移备份目标连接失败" in caplog.text
    assert "forced source.backup failure" in caplog.text
    assert _read_database_state(path) == before_state

    reopened = Database(path)
    conn = reopened._get_conn()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_commit_success_then_exception_reports_unknown_and_preserves_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "app.db"
    _create_v1_database(path, with_objects=True)
    caplog.clear()

    with caplog.at_level(logging.CRITICAL):
        with monkeypatch.context() as context:
            proxies = _install_commit_failure(context, "success")
            with pytest.raises(RuntimeError, match="迁移提交结果不确定") as exc_info:
                Database(path)

    assert type(exc_info.value) is RuntimeError
    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    assert "forced commit success exception" in str(exc_info.value.__cause__)
    assert len(proxies) == 1
    proxy = proxies[0]
    assert proxy.close_count == 1
    with pytest.raises(sqlite3.ProgrammingError):
        proxy._connection.execute("SELECT 1")
    assert "提交结果无法确认" in caplog.text
    assert "数据库迁移完成" not in caplog.text
    assert "事务已回滚" not in caplog.text
    # 提交实际成功，V1→V2 数据保留；V2→V3 因连接关闭未执行。
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 3
    assert conn.execute(
        "SELECT file_path FROM recordings WHERE id=201"
    ).fetchone()[0] == "C:/old/a.ts"
    conn.close()

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
    reopened = Database(path)
    assert reopened._get_conn().execute("PRAGMA foreign_keys").fetchone()[0] == 1
    reopened.close()


def test_commit_rollback_then_exception_reports_unknown_and_preserves_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "app.db"
    _create_v1_database(path, with_objects=True)
    before_state = _read_database_state(path)

    with caplog.at_level(logging.CRITICAL):
        with monkeypatch.context() as context:
            proxies = _install_commit_failure(context, "rollback")
            with pytest.raises(RuntimeError, match="迁移提交结果不确定") as exc_info:
                Database(path)

    assert type(exc_info.value) is RuntimeError
    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    assert "forced commit rollback exception" in str(exc_info.value.__cause__)
    assert len(proxies) == 1
    proxy = proxies[0]
    assert proxy.close_count == 1
    with pytest.raises(sqlite3.ProgrammingError):
        proxy._connection.execute("SELECT 1")
    assert "提交结果无法确认" in caplog.text
    assert "数据库迁移完成" not in caplog.text
    assert "事务已回滚" not in caplog.text
    assert "原数据库事务已回滚" not in caplog.text
    assert _read_database_state(path) == before_state

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    conn.close()

    reopened = Database(path)
    conn = reopened._get_conn()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(
        "SELECT web_rid_snapshot FROM recordings WHERE id=202"
    ).fetchone()[0] == "rid-old-9"
    reopened.close()
