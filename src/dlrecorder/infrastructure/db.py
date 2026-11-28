"""SQLite 持久化层。

设计要点：
- WAL 模式 + 外键约束，提升并发读与数据完整性。
- schema_version 表驱动迁移。
- 时间字段统一使用 ISO 8601 字符串（UTC，带 Z 后缀）。
- 连接使用 check_same_thread=False + 线程锁，供异步线程调用。
- 调用方应在非 GUI 线程调用本模块方法。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from ..domain.models import Room
from ..domain.recording_query import RecordingQuery
from ..domain.states import RoomState


SCHEMA_VERSION = 3

logger = logging.getLogger(__name__)

_UNSET: Any = object()


class MigrationPostCommitError(RuntimeError):
    """迁移已提交，但提交后收尾失败，原连接不可继续使用。"""

    def __init__(
        self,
        message: str,
        *,
        connection_closed: bool,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        super().__init__(message)
        self.migration_committed = True
        self.schema_version = schema_version
        self.connection_closed = connection_closed


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """SQLite 数据库封装。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        try:
            self._init_schema()
        except Exception:
            self.close()
            raise

    def _connect(self) -> None:
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # 自动提交；事务用 BEGIN 手动控制
        )
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def backup_to(self, dest: Path) -> Path:
        """使用 SQLite Online Backup API 创建一致性备份并校验完整性。

        先写入同目录临时文件，校验通过后再原子替换目标文件；失败时保留原目标文件。
        不会删除或修改当前数据库。
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.tmp")
        try:
            with self._lock:
                src = self._get_conn()
                target = sqlite3.connect(str(tmp))
                try:
                    src.backup(target)
                    target.commit()
                    check = target.execute("PRAGMA integrity_check").fetchone()
                    if check is None or check[0] != "ok":
                        reason = check[0] if check else "无结果"
                        raise RuntimeError(f"备份完整性检查失败: {reason}")
                finally:
                    target.close()
            os.replace(tmp, dest)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                logger.warning("清理备份临时文件失败: %s", tmp, exc_info=True)
            raise
        logger.info("数据库备份完成: %s", dest)
        return dest

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._connect()
        return self._conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY)"
            )
            row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current >= SCHEMA_VERSION:
                return

            tables = {
                row["name"]
                for row in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            user_tables = tables - {"schema_version", "sqlite_sequence"}
            if current == 0 and not user_tables:
                self._initialize_v3(conn)
            else:
                self._migrate(cur, current, SCHEMA_VERSION)

    def _initialize_v3(self, conn: sqlite3.Connection) -> None:
        """全新数据库直接建立最新 Schema，不创建空数据库迁移备份。"""
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute(
                """
                CREATE TABLE rooms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    web_rid TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    anchor_id TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._create_monitor_sessions(cur, "monitor_sessions")
            cur.execute(
                """
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._create_recordings(cur, "recordings")
            self._create_archive_jobs(cur)
            self._create_v3_indexes(cur)
            self._set_schema_version(cur)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            logger.exception("数据库初始化失败，已回滚")
            raise

    @staticmethod
    def _create_monitor_sessions(cur: sqlite3.Cursor, table_name: str) -> None:
        if table_name not in {"monitor_sessions", "new_monitor_sessions"}:
            raise ValueError("非法会话表名")
        cur.execute(
            f"""
            CREATE TABLE {table_name} (
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
            )
            """
        )

    @staticmethod
    def _create_recordings(
        cur: sqlite3.Cursor,
        table_name: str,
        *,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        if table_name not in {"recordings", "new_recordings"}:
            raise ValueError("非法录制表名")
        archive_columns = ""
        if schema_version >= 3:
            archive_columns = """
                ,
                archive_status TEXT NOT NULL DEFAULT 'none'
                    CHECK(archive_status IN (
                        'none', 'pending', 'copying', 'verifying',
                        'archived', 'failed', 'skipped'
                    )),
                archive_path TEXT,
                archived_at TEXT,
                archive_error TEXT,
                archive_attempts INTEGER NOT NULL DEFAULT 0
            """
        cur.execute(
            f"""
            CREATE TABLE {table_name} (
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
                {archive_columns}
            )
            """
        )

    @staticmethod
    def _create_archive_jobs(cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE archive_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id INTEGER NOT NULL
                    REFERENCES recordings(id) ON DELETE CASCADE,
                source_path TEXT NOT NULL,
                target_path TEXT,
                status TEXT NOT NULL
                    CHECK(status IN (
                        'pending', 'copying', 'verifying',
                        'archived', 'failed', 'skipped'
                    )),
                attempts INTEGER NOT NULL DEFAULT 0,
                bytes_total INTEGER,
                sha256 TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_v3_indexes(cur: sqlite3.Cursor) -> None:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_recordings_archive_status "
            "ON recordings(archive_status)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_jobs_status "
            "ON archive_jobs(status)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_jobs_recording "
            "ON archive_jobs(recording_id)"
        )

    @staticmethod
    def _set_schema_version(
        cur: sqlite3.Cursor,
        version: int = SCHEMA_VERSION,
    ) -> None:
        cur.execute("DELETE FROM schema_version")
        cur.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))

    def _migrate(self, cur: sqlite3.Cursor, from_v: int, to_v: int) -> None:
        """按版本步进执行迁移，每一步独立事务、备份与校验。"""
        if to_v != SCHEMA_VERSION:
            raise ValueError(f"不支持的迁移目标版本: {to_v}")
        version = from_v
        while version < to_v:
            if version == 1:
                self._migrate_v1_to_v2(cur)
                version = 2
            elif version == 2:
                self._migrate_v2_to_v3(cur)
                version = 3
            else:
                raise ValueError(f"不支持的迁移源版本: {version}")

    def _migrate_v1_to_v2(self, cur: sqlite3.Cursor) -> None:
        """在写锁和显式事务中执行 V1→V2，保留原主键及历史数据。"""
        conn = cur.connection
        backup_path: Optional[Path] = None
        stage = "检查事务状态"
        migration_committed = False
        commit_attempted = False
        primary_error: Optional[BaseException] = None
        try:
            if conn.in_transaction:
                raise RuntimeError("迁移前存在未提交事务")
            # foreign_keys 必须在事务外切换。
            stage = "事务外关闭外键约束"
            cur.execute("PRAGMA foreign_keys=OFF")
            stage = "获取数据库写锁"
            cur.execute("BEGIN IMMEDIATE")

            # 等待写锁期间可能已有其他进程完成迁移，因此重新检查版本。
            row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current >= 2:
                conn.rollback()
                return

            stage = "SQLite Online Backup API 一致性备份"
            backup_path = self._create_migration_backup(1, 2)
            logger.info("V1→V2 迁移备份已创建并验证: %s", backup_path)

            stage = "保存索引、触发器、视图和自增序列"
            objects = cur.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND ("
                "type IN ('view', 'trigger') OR "
                "(type='index' AND tbl_name IN ('monitor_sessions', 'recordings'))"
                ") ORDER BY rowid"
            ).fetchall()
            sequences = {
                row["name"]: row["seq"]
                for row in cur.execute(
                    "SELECT name, seq FROM sqlite_sequence "
                    "WHERE name IN ('monitor_sessions', 'recordings')"
                ).fetchall()
            }
            expected_counts = {
                table: cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("rooms", "monitor_sessions", "recordings", "settings")
            }

            stage = "创建 V2 临时表"
            self._create_monitor_sessions(cur, "new_monitor_sessions")
            self._create_recordings(cur, "new_recordings", schema_version=2)

            stage = "复制监控会话并补齐快照"
            cur.execute(
                """
                INSERT INTO new_monitor_sessions(
                    id, room_id, started_at, ended_at, status, error_message,
                    web_rid_snapshot, display_name_snapshot, anchor_id_snapshot,
                    snapshot_source
                )
                SELECT
                    s.id, s.room_id, s.started_at, s.ended_at, s.status, s.error_message,
                    COALESCE(r.web_rid, ''), COALESCE(r.display_name, ''),
                    COALESCE(r.anchor_id, ''),
                    CASE WHEN r.id IS NULL THEN 'unknown' ELSE 'migration_backfill' END
                FROM monitor_sessions AS s
                LEFT JOIN rooms AS r ON r.id=s.room_id
                """
            )
            self._verify_copied_table(
                cur,
                "monitor_sessions",
                "new_monitor_sessions",
                ("room_id", "started_at", "ended_at", "status", "error_message"),
            )

            stage = "复制录制元数据并补齐快照"
            cur.execute(
                """
                INSERT INTO new_recordings(
                    id, room_id, session_id, started_at, ended_at,
                    file_path, file_size, status,
                    web_rid_snapshot, display_name_snapshot, anchor_id_snapshot,
                    snapshot_source
                )
                SELECT
                    rec.id, rec.room_id, rec.session_id, rec.started_at, rec.ended_at,
                    rec.file_path, rec.file_size, rec.status,
                    COALESCE(r.web_rid, sr.web_rid, ''),
                    COALESCE(r.display_name, sr.display_name, ''),
                    COALESCE(r.anchor_id, sr.anchor_id, ''),
                    CASE
                        WHEN r.id IS NULL AND sr.id IS NULL THEN 'unknown'
                        ELSE 'migration_backfill'
                    END
                FROM recordings AS rec
                LEFT JOIN rooms AS r ON r.id=rec.room_id
                LEFT JOIN monitor_sessions AS s ON s.id=rec.session_id
                LEFT JOIN rooms AS sr ON sr.id=s.room_id
                """
            )
            self._verify_copied_table(
                cur,
                "recordings",
                "new_recordings",
                (
                    "room_id", "session_id", "started_at", "ended_at",
                    "file_path", "file_size", "status",
                ),
            )
            self._verify_snapshot_backfill(cur)

            stage = "按依赖顺序替换历史表"
            # SQLite 重命名表时会校验视图和触发器引用；先事务内移除，随后原样恢复。
            for obj in reversed(objects):
                if obj["type"] in {"view", "trigger"}:
                    escaped_name = obj["name"].replace('"', '""')
                    cur.execute(f"DROP {obj['type'].upper()} \"{escaped_name}\"")
            # 不重命名旧表，避免子表外键被 SQLite 改写到旧表名。
            cur.execute("DROP TABLE recordings")
            cur.execute("DROP TABLE monitor_sessions")
            cur.execute("ALTER TABLE new_monitor_sessions RENAME TO monitor_sessions")
            cur.execute("ALTER TABLE new_recordings RENAME TO recordings")

            stage = "恢复索引、触发器、视图和自增序列"
            for obj in objects:
                cur.execute(obj["sql"])
            for table_name, old_seq in sequences.items():
                cur.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (old_seq, table_name))
                if cur.rowcount == 0:
                    cur.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                        (table_name, old_seq),
                    )

            stage = "更新 Schema 版本"
            self._set_schema_version(cur, 2)
            stage = "验证 V2 数据、外键和数据库对象"
            self._verify_v2_migration(cur, expected_counts, objects, sequences)
            stage = "提交 V2 迁移事务"
            commit_attempted = True
            conn.commit()
            migration_committed = True
        except Exception as exc:
            primary_error = exc
            if conn.in_transaction:
                try:
                    conn.rollback()
                except Exception as rollback_error:
                    logger.critical(
                        "数据库迁移失败且回滚失败，阶段=%s，备份=%s；必须保留现场并人工恢复",
                        stage,
                        backup_path,
                        exc_info=True,
                    )
                    raise RuntimeError("迁移失败且回滚失败，禁止继续操作") from rollback_error
                logger.exception(
                    "数据库迁移失败，阶段=%s，异常=%s，备份=%s；原数据库事务已回滚",
                    stage,
                    exc,
                    backup_path,
                )
                raise
            if commit_attempted:
                logger.critical(
                    "迁移提交调用异常，阶段=%s，备份=%s；连接已无活动事务，"
                    "提交结果无法确认，禁止继续操作",
                    stage,
                    backup_path,
                    exc_info=True,
                )
                self.close()
                raise RuntimeError("迁移提交结果不确定，禁止继续操作") from exc
            logger.exception(
                "数据库迁移在提交前失败，阶段=%s，异常=%s，备份=%s；"
                "当前无活动事务，无需回滚",
                stage,
                exc,
                backup_path,
            )
            raise
        finally:
            if self._conn is not None and not conn.in_transaction:
                try:
                    self._restore_foreign_keys(cur)
                except Exception as finalization_error:
                    if migration_committed:
                        logger.critical(
                            "V2 已提交，但提交后恢复外键约束失败，备份=%s；"
                            "当前连接不能继续安全使用，将关闭连接",
                            backup_path,
                            exc_info=True,
                        )
                        self.close()
                        raise MigrationPostCommitError(
                            "V2 数据库迁移已经提交，但恢复外键约束失败；"
                            "当前连接不能继续安全使用",
                            connection_closed=self._conn is None,
                            schema_version=2,
                        ) from finalization_error
                    logger.critical(
                        "迁移收尾恢复外键约束失败，原始迁移异常=%s；"
                        "当前连接不能继续安全使用，将关闭连接",
                        primary_error,
                        exc_info=True,
                    )
                    self.close()
                    if primary_error is None:
                        raise
        logger.info("V1→V2 数据库迁移完成，备份保留于: %s", backup_path)

    @staticmethod
    def _restore_foreign_keys(cur: sqlite3.Cursor) -> None:
        cur.execute("PRAGMA foreign_keys=ON")

    def _create_migration_backup(self, from_v: int = 1, to_v: int = 2) -> Path:
        """在源库写锁已持有、尚未修改 schema 时进行在线一致性备份。"""
        backup_dir = self.db_path.parent / "migration_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        backup_path = backup_dir / (
            f"{self.db_path.stem}_v{from_v}_to_v{to_v}_{stamp}_{uuid.uuid4().hex}.db"
        )
        source: Optional[sqlite3.Connection] = None
        destination: Optional[sqlite3.Connection] = None
        verification: Optional[sqlite3.Connection] = None
        try:
            source = sqlite3.connect(str(self.db_path))
            source.execute("PRAGMA query_only=ON")
            destination = sqlite3.connect(str(backup_path))
            source.backup(destination)
            destination.close()
            destination = None
            source.close()
            source = None

            verification = sqlite3.connect(str(backup_path))
            verification.execute("PRAGMA query_only=ON")
            integrity_rows = verification.execute("PRAGMA integrity_check").fetchall()
            if integrity_rows != [("ok",)]:
                raise RuntimeError(f"备份完整性检查失败: {integrity_rows}")
            object_count = verification.execute(
                "SELECT COUNT(*) FROM sqlite_master"
            ).fetchone()[0]
            if object_count == 0:
                raise RuntimeError("备份不包含数据库对象")
            return backup_path
        except Exception:
            logger.exception("迁移备份失败，失败现场保留于: %s", backup_path)
            raise
        finally:
            for label, connection in (
                ("验证", verification),
                ("源", source),
                ("目标", destination),
            ):
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        logger.exception("关闭迁移备份%s连接失败", label)

    @staticmethod
    def _verify_copied_table(
        cur: sqlite3.Cursor,
        old_table: str,
        new_table: str,
        columns: tuple[str, ...],
    ) -> None:
        old_count = cur.execute(f"SELECT COUNT(*) FROM {old_table}").fetchone()[0]
        new_count = cur.execute(f"SELECT COUNT(*) FROM {new_table}").fetchone()[0]
        if old_count != new_count:
            raise RuntimeError(f"{old_table} 迁移前后记录数不一致")
        comparisons = " OR ".join(
            f"o.{column} IS NOT n.{column}" for column in columns
        )
        where = "n.id IS NULL" + (f" OR {comparisons}" if comparisons else "")
        mismatch = cur.execute(
            f"SELECT COUNT(*) FROM {old_table} AS o "
            f"LEFT JOIN {new_table} AS n ON n.id=o.id WHERE {where}"
        ).fetchone()[0]
        if mismatch:
            raise RuntimeError(f"{old_table} 有 {mismatch} 条记录的主键或原始字段发生变化")

    @staticmethod
    def _verify_snapshot_backfill(cur: sqlite3.Cursor) -> None:
        session_mismatch = cur.execute(
            """
            SELECT COUNT(*) FROM new_monitor_sessions AS s
            LEFT JOIN rooms AS r ON r.id=s.room_id
            WHERE s.web_rid_snapshot IS NOT COALESCE(r.web_rid, '')
               OR s.display_name_snapshot IS NOT COALESCE(r.display_name, '')
               OR s.anchor_id_snapshot IS NOT COALESCE(r.anchor_id, '')
               OR s.snapshot_source !=
                  CASE WHEN r.id IS NULL THEN 'unknown' ELSE 'migration_backfill' END
            """
        ).fetchone()[0]
        recording_mismatch = cur.execute(
            """
            SELECT COUNT(*) FROM new_recordings AS rec
            LEFT JOIN rooms AS r ON r.id=rec.room_id
            LEFT JOIN monitor_sessions AS s ON s.id=rec.session_id
            LEFT JOIN rooms AS sr ON sr.id=s.room_id
            WHERE rec.web_rid_snapshot IS NOT COALESCE(r.web_rid, sr.web_rid, '')
               OR rec.display_name_snapshot IS NOT COALESCE(r.display_name, sr.display_name, '')
               OR rec.anchor_id_snapshot IS NOT COALESCE(r.anchor_id, sr.anchor_id, '')
               OR rec.snapshot_source !=
                  CASE
                      WHEN r.id IS NULL AND sr.id IS NULL THEN 'unknown'
                      ELSE 'migration_backfill'
                  END
            """
        ).fetchone()[0]
        if session_mismatch or recording_mismatch:
            raise RuntimeError(
                f"快照回填验证失败: sessions={session_mismatch}, recordings={recording_mismatch}"
            )

    @staticmethod
    def _verify_v2_migration(
        cur: sqlite3.Cursor,
        expected_counts: dict[str, Any],
        objects: list[sqlite3.Row],
        sequences: dict[str, Any],
    ) -> None:
        version = cur.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version != 2:
            raise RuntimeError(f"Schema 版本错误: {version}")
        for table, expected in expected_counts.items():
            actual = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if actual != expected:
                raise RuntimeError(f"{table} 迁移后记录数不一致: {actual} != {expected}")

        expected_fks = {
            ("monitor_sessions", "room_id", "rooms"): "SET NULL",
            ("recordings", "room_id", "rooms"): "SET NULL",
            ("recordings", "session_id", "monitor_sessions"): "NO ACTION",
        }
        for (table, column, target), action in expected_fks.items():
            rows = cur.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            match = [row for row in rows if row["from"] == column and row["table"] == target]
            if len(match) != 1 or match[0]["on_delete"] != action:
                raise RuntimeError(f"{table}.{column} 外键不符合 V2 设计")
            room_id_info = [
                row for row in cur.execute(f"PRAGMA table_info({table})").fetchall()
                if row["name"] == column and column == "room_id"
            ]
            if column == "room_id" and (not room_id_info or room_id_info[0]["notnull"]):
                raise RuntimeError(f"{table}.room_id 必须允许 NULL")

        required_snapshots = {
            "web_rid_snapshot", "display_name_snapshot",
            "anchor_id_snapshot", "snapshot_source",
        }
        for table in ("monitor_sessions", "recordings"):
            columns = {
                row["name"] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not required_snapshots.issubset(columns):
                raise RuntimeError(f"{table} 缺少快照字段")

        for obj in objects:
            found = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                (obj["type"], obj["name"]),
            ).fetchone()
            if found is None:
                raise RuntimeError(f"迁移后数据库对象丢失: {obj['type']} {obj['name']}")
        for table_name, old_seq in sequences.items():
            row = cur.execute(
                "SELECT seq FROM sqlite_sequence WHERE name=?", (table_name,)
            ).fetchone()
            if row is None or row["seq"] < old_seq:
                raise RuntimeError(f"{table_name} 自增序列发生回退")

        violations = cur.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"外键完整性检查失败: {violations}")

    _V3_ARCHIVE_COLUMNS: tuple[tuple[str, str], ...] = (
        (
            "archive_status",
            "TEXT NOT NULL DEFAULT 'none' CHECK(archive_status IN ("
            "'none', 'pending', 'copying', 'verifying', "
            "'archived', 'failed', 'skipped'))",
        ),
        ("archive_path", "TEXT"),
        ("archived_at", "TEXT"),
        ("archive_error", "TEXT"),
        ("archive_attempts", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _migrate_v2_to_v3(self, cur: sqlite3.Cursor) -> None:
        """V2→V3：纯增量升级，不重建既有表，事务内完成并校验。"""
        conn = cur.connection
        backup_path: Optional[Path] = None
        stage = "检查事务状态"
        commit_attempted = False
        try:
            if conn.in_transaction:
                raise RuntimeError("迁移前存在未提交事务")
            stage = "获取数据库写锁"
            cur.execute("BEGIN IMMEDIATE")

            row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current >= 3:
                conn.rollback()
                return

            stage = "SQLite Online Backup API 一致性备份"
            backup_path = self._create_migration_backup(2, 3)
            logger.info("V2→V3 迁移备份已创建并验证: %s", backup_path)

            expected_counts = {
                table: cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("rooms", "monitor_sessions", "recordings", "settings")
            }

            stage = "为 recordings 增加归档字段"
            existing_columns = {
                info["name"]
                for info in cur.execute("PRAGMA table_info(recordings)").fetchall()
            }
            for column, ddl in self._V3_ARCHIVE_COLUMNS:
                if column in existing_columns:
                    continue
                cur.execute(f"ALTER TABLE recordings ADD COLUMN {column} {ddl}")

            stage = "创建归档任务表与索引"
            has_jobs = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_jobs'"
            ).fetchone()
            if has_jobs is None:
                self._create_archive_jobs(cur)
            self._create_v3_indexes(cur)

            stage = "更新 Schema 版本"
            self._set_schema_version(cur, 3)
            stage = "验证 V3 数据与结构"
            self._verify_v3_migration(cur, expected_counts)
            stage = "提交 V3 迁移事务"
            commit_attempted = True
            conn.commit()
        except Exception as exc:
            if conn.in_transaction:
                try:
                    conn.rollback()
                except Exception as rollback_error:
                    logger.critical(
                        "V2→V3 迁移失败且回滚失败，阶段=%s，备份=%s；"
                        "必须保留现场并人工恢复",
                        stage,
                        backup_path,
                        exc_info=True,
                    )
                    raise RuntimeError("迁移失败且回滚失败，禁止继续操作") from rollback_error
                logger.exception(
                    "V2→V3 迁移失败，阶段=%s，异常=%s，备份=%s；原数据库事务已回滚",
                    stage,
                    exc,
                    backup_path,
                )
                raise
            if commit_attempted:
                logger.critical(
                    "V2→V3 迁移提交调用异常，阶段=%s，备份=%s；"
                    "连接已无活动事务，提交结果无法确认，禁止继续操作",
                    stage,
                    backup_path,
                    exc_info=True,
                )
                self.close()
                raise RuntimeError("迁移提交结果不确定，禁止继续操作") from exc
            logger.exception(
                "V2→V3 迁移在提交前失败，阶段=%s，异常=%s，备份=%s；"
                "当前无活动事务，无需回滚",
                stage,
                exc,
                backup_path,
            )
            raise
        logger.info("V2→V3 数据库迁移完成，备份保留于: %s", backup_path)

    @staticmethod
    def _verify_v3_migration(
        cur: sqlite3.Cursor,
        expected_counts: dict[str, Any],
    ) -> None:
        version = cur.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version != 3:
            raise RuntimeError(f"Schema 版本错误: {version}")

        columns = {
            row["name"]: row for row in cur.execute("PRAGMA table_info(recordings)").fetchall()
        }
        for column in ("archive_status", "archive_path", "archived_at",
                       "archive_error", "archive_attempts"):
            if column not in columns:
                raise RuntimeError(f"recordings 缺少归档字段: {column}")
        if not columns["archive_status"]["notnull"]:
            raise RuntimeError("recordings.archive_status 必须为 NOT NULL")
        default_value = columns["archive_status"]["dflt_value"]
        if default_value is None or str(default_value).strip("'\"") != "none":
            raise RuntimeError("recordings.archive_status 默认值必须为 'none'")
        if not columns["archive_attempts"]["notnull"]:
            raise RuntimeError("recordings.archive_attempts 必须为 NOT NULL")

        job_columns = {
            row["name"] for row in cur.execute("PRAGMA table_info(archive_jobs)").fetchall()
        }
        required_job_columns = {
            "id", "recording_id", "source_path", "target_path", "status",
            "attempts", "bytes_total", "sha256", "error",
            "created_at", "updated_at",
        }
        if not required_job_columns.issubset(job_columns):
            raise RuntimeError("archive_jobs 字段不完整")

        job_fks = cur.execute("PRAGMA foreign_key_list(archive_jobs)").fetchall()
        if not any(
            fk["from"] == "recording_id"
            and fk["table"] == "recordings"
            and fk["on_delete"] == "CASCADE"
            for fk in job_fks
        ):
            raise RuntimeError("archive_jobs.recording_id 外键不符合 V3 设计")

        indexes = {
            row["name"]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        for index in (
            "idx_recordings_archive_status",
            "idx_archive_jobs_status",
            "idx_archive_jobs_recording",
        ):
            if index not in indexes:
                raise RuntimeError(f"V3 索引缺失: {index}")

        for table, expected in expected_counts.items():
            actual = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if actual != expected:
                raise RuntimeError(f"{table} 迁移后记录数不一致: {actual} != {expected}")

        not_backfilled = cur.execute(
            "SELECT COUNT(*) FROM recordings WHERE archive_status != 'none'"
        ).fetchone()[0]
        if not_backfilled:
            raise RuntimeError(f"{not_backfilled} 条录制记录未完成归档状态回填")

        violations = cur.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"外键完整性检查失败: {violations}")

    # ---------- rooms ----------
    def add_room(self, web_rid: str, display_name: str = "", anchor_id: str = "") -> int:
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "INSERT INTO rooms(web_rid, display_name, anchor_id, enabled, created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?)",
                (web_rid, display_name, anchor_id, now, now),
            )
            return int(cur.lastrowid)

    def delete_room(self, room_id: int) -> None:
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute("DELETE FROM rooms WHERE id=?", (room_id,))

    def set_room_enabled(self, room_id: int, enabled: bool) -> None:
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "UPDATE rooms SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, now, room_id),
            )

    def update_room(
        self,
        room_id: int,
        *,
        web_rid: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        """更新房间基础信息；房间不存在时抛 KeyError，ID 冲突由 SQLite 抛出。"""
        updates: list[str] = []
        params: list[Any] = []
        if web_rid is not None:
            updates.append("web_rid=?")
            params.append(web_rid)
        if display_name is not None:
            updates.append("display_name=?")
            params.append(display_name)
        if not updates:
            return
        updates.append("updated_at=?")
        params.append(_now_iso())
        params.append(room_id)
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                f"UPDATE rooms SET {', '.join(updates)} WHERE id=?", params
            )
            if cur.rowcount != 1:
                raise KeyError(f"房间不存在: {room_id}")

    def list_rooms(self) -> list[Room]:
        with self._lock:
            cur = self._get_conn().cursor()
            rows = cur.execute(
                "SELECT id, web_rid, display_name, anchor_id, enabled FROM rooms ORDER BY id"
            ).fetchall()
        rooms: list[Room] = []
        for r in rows:
            rooms.append(
                Room(
                    id=r["id"],
                    web_rid=r["web_rid"],
                    display_name=r["display_name"],
                    anchor_id=r["anchor_id"],
                    enabled=bool(r["enabled"]),
                    state=RoomState(),
                )
            )
        return rooms

    def get_room(self, room_id: int) -> Optional[Room]:
        with self._lock:
            cur = self._get_conn().cursor()
            row = cur.execute(
                "SELECT id, web_rid, display_name, anchor_id, enabled FROM rooms WHERE id=?",
                (room_id,),
            ).fetchone()
        if row is None:
            return None
        return Room(
            id=row["id"],
            web_rid=row["web_rid"],
            display_name=row["display_name"],
            anchor_id=row["anchor_id"],
            enabled=bool(row["enabled"]),
            state=RoomState(),
        )

    # ---------- monitor_sessions ----------
    def start_session(self, room_id: int) -> int:
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                """
                INSERT INTO monitor_sessions(
                    room_id, started_at, status,
                    web_rid_snapshot, display_name_snapshot, anchor_id_snapshot,
                    snapshot_source
                )
                SELECT id, ?, 'running', web_rid, display_name, anchor_id, 'captured'
                FROM rooms WHERE id=?
                """,
                (now, room_id),
            )
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError(f"房间不存在: {room_id}")
            return int(cur.lastrowid)

    def end_session(self, session_id: int, status: str = "stopped", error: Optional[str] = None) -> None:
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "UPDATE monitor_sessions SET ended_at=?, status=?, error_message=? "
                "WHERE id=? AND status='running'",
                (now, status, error, session_id),
            )

    def interrupt_running_sessions(self) -> int:
        """将上次异常退出遗留的 running Session 标记为 interrupted。"""
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "UPDATE monitor_sessions SET ended_at=COALESCE(ended_at, ?), "
                "status='interrupted' WHERE status='running'",
                (now,),
            )
            return cur.rowcount

    # ---------- settings ----------
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            cur = self._get_conn().cursor()
            row = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )

    # ---------- recordings ----------
    def start_recording(
        self,
        room_id: int,
        file_path: str,
        session_id: Optional[int] = None,
    ) -> int:
        """创建录制记录，并捕获房间当前身份快照。"""
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                """
                INSERT INTO recordings(
                    room_id, session_id, started_at, file_path, status,
                    web_rid_snapshot, display_name_snapshot, anchor_id_snapshot,
                    snapshot_source
                )
                SELECT id, ?, ?, ?, 'recording',
                       web_rid, display_name, anchor_id, 'captured'
                FROM rooms WHERE id=?
                """,
                (session_id, now, file_path, room_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"房间不存在: {room_id}")
            return int(cur.lastrowid)

    def finish_recording(
        self,
        recording_id: int,
        status: str,
        file_size: Optional[int] = None,
    ) -> bool:
        """结束录制记录；仅对仍处于 recording 的记录生效（幂等）。

        recordings 表没有 error_message 字段，失败原因只写日志，不改变 Schema。
        """
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "UPDATE recordings SET ended_at=COALESCE(ended_at, ?), "
                "status=?, file_size=COALESCE(?, file_size) "
                "WHERE id=? AND status='recording'",
                (now, status, file_size, recording_id),
            )
            return cur.rowcount == 1

    def interrupt_recording(
        self,
        recording_id: int,
        file_size: Optional[int] = None,
    ) -> bool:
        """将遗留 recording 记录标记为 interrupted，保留文件路径与已有大小。"""
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "UPDATE recordings SET ended_at=COALESCE(ended_at, ?), "
                "status='interrupted', file_size=COALESCE(?, file_size) "
                "WHERE id=? AND status='recording'",
                (now, file_size, recording_id),
            )
            return cur.rowcount == 1

    def list_recordings(
        self,
        room_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """按最近优先返回录制记录。"""
        clauses: list[str] = []
        params: list[Any] = []
        if room_id is not None:
            clauses.append("room_id=?")
            params.append(room_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._get_conn().execute(
                f"SELECT * FROM recordings{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_recording(self, recording_id: int) -> Optional[dict]:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM recordings WHERE id=?", (recording_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    # ---------- 录制库检索与统计 ----------
    @staticmethod
    def _recording_query_clause(query: RecordingQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        keyword = (query.keyword or "").strip()
        if keyword:
            escaped = (
                keyword.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            clauses.append(
                "(display_name_snapshot LIKE ? ESCAPE '\\' "
                "OR web_rid_snapshot LIKE ? ESCAPE '\\' "
                "OR anchor_id_snapshot LIKE ? ESCAPE '\\' "
                "OR file_path LIKE ? ESCAPE '\\' "
                "OR archive_path LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 5)
        date_from = (query.date_from or "").strip()
        if date_from:
            clauses.append("substr(started_at, 1, 10) >= ?")
            params.append(date_from)
        date_to = (query.date_to or "").strip()
        if date_to:
            clauses.append("substr(started_at, 1, 10) <= ?")
            params.append(date_to)
        if query.status:
            clauses.append("status = ?")
            params.append(query.status)
        if query.archive_status:
            clauses.append("archive_status = ?")
            params.append(query.archive_status)
        if query.min_size > 0:
            clauses.append("COALESCE(file_size, 0) >= ?")
            params.append(int(query.min_size))
        if query.max_size > 0:
            clauses.append("COALESCE(file_size, 0) <= ?")
            params.append(int(query.max_size))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def search_recordings(self, query: RecordingQuery) -> list[dict]:
        """按条件检索录制记录（最近优先），全部使用参数化 SQL。"""
        where, params = self._recording_query_clause(query)
        params.append(max(1, int(query.limit)))
        params.append(max(0, int(query.offset)))
        with self._lock:
            rows = self._get_conn().execute(
                f"SELECT * FROM recordings{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_recordings(self, query: RecordingQuery) -> int:
        where, params = self._recording_query_clause(query)
        with self._lock:
            row = self._get_conn().execute(
                f"SELECT COUNT(*) AS n FROM recordings{where}", params
            ).fetchone()
        return int(row["n"])

    def mark_recordings_deleted(self, recording_ids: Sequence[int]) -> int:
        """把已移入回收站的录制记录标记为 deleted；录制中的记录不受影响。"""
        ids = [int(value) for value in dict.fromkeys(recording_ids)]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                f"UPDATE recordings SET status='deleted' "
                f"WHERE id IN ({placeholders}) AND status != 'recording'",
                ids,
            )
            return cur.rowcount

    def recording_storage_stats(self) -> dict[str, Any]:
        """按主播、日期与归档状态聚合容量；另返回总计。"""
        with self._lock:
            conn = self._get_conn()
            totals = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS bytes "
                "FROM recordings WHERE status != 'deleted'"
            ).fetchone()
            by_anchor = conn.execute(
                "SELECT COALESCE(NULLIF(display_name_snapshot, ''), web_rid_snapshot, "
                "'未命名') AS label, COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS bytes "
                "FROM recordings WHERE status != 'deleted' "
                "GROUP BY label ORDER BY bytes DESC LIMIT 100"
            ).fetchall()
            by_date = conn.execute(
                "SELECT substr(started_at, 1, 10) AS label, COUNT(*) AS n, "
                "COALESCE(SUM(file_size), 0) AS bytes FROM recordings "
                "WHERE status != 'deleted' AND started_at IS NOT NULL "
                "GROUP BY label ORDER BY label DESC LIMIT 100"
            ).fetchall()
            by_archive = conn.execute(
                "SELECT archive_status AS label, COUNT(*) AS n, "
                "COALESCE(SUM(file_size), 0) AS bytes FROM recordings "
                "WHERE status != 'deleted' GROUP BY archive_status"
            ).fetchall()
        return {
            "total_count": int(totals["n"]),
            "total_bytes": int(totals["bytes"]),
            "by_anchor": [dict(row) for row in by_anchor],
            "by_date": [dict(row) for row in by_date],
            "by_archive": [dict(row) for row in by_archive],
        }

    # ---------- 归档状态（recordings） ----------
    def set_recording_archive(
        self,
        recording_id: int,
        status: str,
        *,
        archive_path: Any = _UNSET,
        archived_at: Any = _UNSET,
        error: Any = _UNSET,
        increment_attempts: bool = False,
        expected_status: Optional[str] = None,
    ) -> bool:
        """更新录制记录的归档状态；``expected_status`` 提供乐观并发保护。"""
        updates = ["archive_status=?"]
        params: list[Any] = [status]
        if archive_path is not _UNSET:
            updates.append("archive_path=?")
            params.append(archive_path)
        if archived_at is not _UNSET:
            updates.append("archived_at=?")
            params.append(archived_at)
        if error is not _UNSET:
            updates.append("archive_error=?")
            params.append(error)
        if increment_attempts:
            updates.append("archive_attempts=archive_attempts+1")

        where = "id=?"
        params.append(recording_id)
        if expected_status is not None:
            where += " AND archive_status=?"
            params.append(expected_status)
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(f"UPDATE recordings SET {', '.join(updates)} WHERE {where}", params)
            return cur.rowcount == 1

    def list_recordings_by_archive_status(
        self,
        statuses: Sequence[str],
        limit: int = 500,
    ) -> list[dict]:
        """按归档状态返回录制记录（最近优先），用于启动恢复与重试。"""
        values = tuple(str(status) for status in statuses)
        if not values:
            return []
        placeholders = ", ".join("?" for _ in values)
        params: list[Any] = list(values)
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._get_conn().execute(
                f"SELECT * FROM recordings WHERE archive_status IN ({placeholders}) "
                "ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 归档任务（archive_jobs） ----------
    def create_archive_job(
        self,
        recording_id: int,
        source_path: str,
        status: str = "pending",
    ) -> int:
        now = _now_iso()
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(
                "INSERT INTO archive_jobs("
                "recording_id, source_path, status, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (recording_id, source_path, status, now, now),
            )
            return int(cur.lastrowid)

    def update_archive_job(
        self,
        job_id: int,
        status: str,
        *,
        target_path: Any = _UNSET,
        bytes_total: Any = _UNSET,
        sha256: Any = _UNSET,
        error: Any = _UNSET,
        increment_attempts: bool = False,
    ) -> bool:
        updates = ["status=?", "updated_at=?"]
        params: list[Any] = [status, _now_iso()]
        if target_path is not _UNSET:
            updates.append("target_path=?")
            params.append(target_path)
        if bytes_total is not _UNSET:
            updates.append("bytes_total=?")
            params.append(bytes_total)
        if sha256 is not _UNSET:
            updates.append("sha256=?")
            params.append(sha256)
        if error is not _UNSET:
            updates.append("error=?")
            params.append(error)
        if increment_attempts:
            updates.append("attempts=attempts+1")
        params.append(job_id)
        with self._lock:
            cur = self._get_conn().cursor()
            cur.execute(f"UPDATE archive_jobs SET {', '.join(updates)} WHERE id=?", params)
            return cur.rowcount == 1

    def get_archive_job(self, job_id: int) -> Optional[dict]:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM archive_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_latest_archive_job(self, recording_id: int) -> Optional[dict]:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM archive_jobs WHERE recording_id=? ORDER BY id DESC LIMIT 1",
                (recording_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_archive_jobs(
        self,
        statuses: Optional[Sequence[str]] = None,
        limit: int = 500,
    ) -> list[dict]:
        """按状态过滤归档任务（最近优先）；``statuses`` 为空表示全部。"""
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            values = tuple(str(status) for status in statuses)
            placeholders = ", ".join("?" for _ in values)
            clauses.append(f"status IN ({placeholders})")
            params.extend(values)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._get_conn().execute(
                f"SELECT * FROM archive_jobs{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
