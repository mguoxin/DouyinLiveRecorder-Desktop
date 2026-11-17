"""数据库备份与安全恢复。

恢复采用“下次启动时替换”的方式，避免在监控/录制运行期间替换正在使用的数据库：
1. 界面只登记恢复请求，不直接改动当前数据库。
2. 应用启动时、打开数据库之前应用请求。
3. 替换前先做完整性校验并为当前数据库创建安全副本；任一步失败则不替换。
4. 全程不删除录制文件，也不删除旧数据库的安全副本。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RESTORE_REQUEST_NAME = "restore_request.json"
REQUIRED_TABLES = {"rooms", "monitor_sessions", "recordings", "settings"}


@dataclass(frozen=True)
class RestoreResult:
    applied: bool
    message: str
    safety_copy: Optional[Path] = None


def verify_sqlite(path: Path) -> tuple[bool, str]:
    """校验文件是完整可用的本应用数据库。"""
    path = Path(path)
    if not path.is_file():
        return False, "备份文件不存在"
    try:
        uri = f"{path.absolute().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        return False, f"无法打开备份文件: {e}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            reason = row[0] if row else "无结果"
            return False, f"备份完整性检查失败: {reason}"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            return False, f"备份缺少必要数据表: {', '.join(sorted(missing))}"
    except sqlite3.Error as e:
        return False, f"读取备份文件失败: {e}"
    finally:
        conn.close()
    return True, ""


def request_path(data_dir: Path) -> Path:
    return Path(data_dir) / RESTORE_REQUEST_NAME


def _copy_database(src: Path, dest: Path) -> None:
    """用 SQLite Online Backup API 复制数据库。

    直接复制主库文件会漏掉仍在 WAL 中、已提交但尚未 checkpoint 的数据；
    异常退出后做安全副本时必须包含这部分数据。源库以普通读写方式打开，
    SQLite 会按正常启动流程完成 WAL 恢复；复制不改动源库内容。
    """
    source = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()


def prepare_restore(backup_path: Path, data_dir: Path) -> Path:
    """校验备份并登记恢复请求；真正的替换在下次启动时执行。"""
    backup_path = Path(backup_path)
    ok, message = verify_sqlite(backup_path)
    if not ok:
        raise ValueError(message)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    target = request_path(data_dir)
    tmp = target.with_name(f"{target.name}.tmp")
    payload = {
        "backup_path": str(backup_path.absolute()),
        "requested_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    logger.info("已登记数据库恢复请求: %s", backup_path)
    return target


def apply_pending_restore(data_dir: Path) -> Optional[RestoreResult]:
    """在打开数据库之前应用恢复请求；无请求时返回 None。"""
    data_dir = Path(data_dir)
    target = request_path(data_dir)
    if not target.is_file():
        return None

    db_path = data_dir / "app.db"
    restore_tmp = data_dir / "app.db.restore.tmp"
    safety_copy: Optional[Path] = None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        backup_path = Path(payload["backup_path"])
        ok, message = verify_sqlite(backup_path)
        if not ok:
            raise ValueError(message)

        backups_dir = data_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safety_copy = backups_dir / f"pre-restore-{stamp}.db"
            _copy_database(db_path, safety_copy)
            ok_safety, message_safety = verify_sqlite(safety_copy)
            if not ok_safety:
                raise RuntimeError(f"当前数据库安全副本校验失败: {message_safety}")

        _copy_database(backup_path, restore_tmp)
        ok_copy, message_copy = verify_sqlite(restore_tmp)
        if not ok_copy:
            raise RuntimeError(f"复制后的数据库校验失败: {message_copy}")
        os.replace(restore_tmp, db_path)

        # 旧数据库的 WAL/SHM 与恢复后的主库不匹配，必须删除。
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        target.unlink()
        logger.warning(
            "已从备份恢复数据库: %s（当前数据库安全副本: %s）", backup_path, safety_copy
        )
        return RestoreResult(True, "已从备份恢复数据库", safety_copy)
    except Exception as e:  # noqa: BLE001 - 恢复失败必须保留现场并继续启动
        logger.exception("应用数据库恢复请求失败")
        failed = target.with_name(f"{target.stem}.failed{target.suffix}")
        try:
            os.replace(target, failed)
        except OSError:
            logger.warning("无法重命名失败的恢复请求文件", exc_info=True)
        try:
            if restore_tmp.exists():
                restore_tmp.unlink()
        except OSError:
            logger.warning("清理恢复临时文件失败: %s", restore_tmp, exc_info=True)
        return RestoreResult(False, f"恢复失败，已保留当前数据库: {e}", safety_copy)
