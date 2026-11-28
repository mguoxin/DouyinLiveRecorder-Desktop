"""录制库服务（P0-02）：检索、统计、一致性检查与安全删除。

只读查询与磁盘检查不修改用户文件；删除仅通过回收站，且拒绝处理仍在录制
的记录。数据库状态与磁盘不一致时只报告，不自动改写。
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from ..domain.recording_query import RecordingQuery
from ..infrastructure.db import Database
from ..infrastructure.safe_fileops import recycle_paths

logger = logging.getLogger(__name__)

_MEDIA_SUFFIXES = {".ts", ".mp4", ".flv", ".mkv", ".m4a", ".aac"}
_MAX_ORPHAN_REPORT = 500


@dataclass(frozen=True)
class ConsistencyIssue:
    kind: str
    recording_id: Optional[int]
    path: str
    detail: str


@dataclass(frozen=True)
class ConsistencyReport:
    issues: tuple[ConsistencyIssue, ...]
    checked_records: int
    orphan_files: int

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        return counts


@dataclass(frozen=True)
class DeleteReport:
    deleted: tuple[int, ...]
    skipped: tuple[tuple[int, str], ...]
    archived_copies: tuple[int, ...]
    freed_bytes: int


class RecordingLibraryService:
    """录制资产查询与管理。"""

    def __init__(
        self,
        db: Database,
        *,
        recordings_dir_provider: Callable[[], Path],
        archive_dir_provider: Optional[Callable[[], Optional[Path]]] = None,
    ) -> None:
        self._db = db
        self._recordings_dir_provider = recordings_dir_provider
        self._archive_dir_provider = archive_dir_provider

    def search(self, query: RecordingQuery) -> list[dict]:
        return self._db.search_recordings(query)

    def count(self, query: RecordingQuery) -> int:
        return self._db.count_recordings(query)

    def storage_stats(self) -> dict:
        return self._db.recording_storage_stats()

    def disk_usage(self) -> dict[str, Optional[dict[str, int]]]:
        """返回录制目录与归档目录所在卷的总量/可用字节。"""
        result: dict[str, Optional[dict[str, int]]] = {}
        for label, provider in (
            ("recordings", lambda: self._recordings_dir_provider()),
            (
                "archive",
                self._archive_dir_provider or (lambda: None),
            ),
        ):
            try:
                path = provider()
            except Exception:  # noqa: BLE001 - 设置读取失败不影响其他统计
                logger.warning("读取目录设置失败 label=%s", label, exc_info=True)
                path = None
            if path is None:
                result[label] = None
                continue
            target = Path(path)
            while not target.exists():
                parent = target.parent
                if parent == target:
                    break
                target = parent
            try:
                usage = shutil.disk_usage(str(target))
            except OSError:
                result[label] = None
                continue
            result[label] = {
                "total": int(usage.total),
                "used": int(usage.used),
                "free": int(usage.free),
            }
        return result

    def scan_consistency(self, *, limit: int = 100_000) -> ConsistencyReport:
        """对数据库记录与磁盘文件做只读一致性检查。"""
        rows = self._db.search_recordings(RecordingQuery(limit=limit))
        known_paths: set[str] = set()
        issues: list[ConsistencyIssue] = []

        for row in rows:
            if row.get("status") == "deleted":
                continue
            file_path = row.get("file_path")
            if file_path:
                path = Path(file_path)
                known_paths.add(self._path_key(path))
                if row.get("status") != "recording":
                    if not path.is_file():
                        issues.append(
                            ConsistencyIssue(
                                "missing",
                                int(row["id"]),
                                str(path),
                                "数据库有记录但磁盘文件不存在",
                            )
                        )
                    elif (
                        row.get("file_size") is not None
                        and path.stat().st_size != int(row["file_size"])
                    ):
                        issues.append(
                            ConsistencyIssue(
                                "size_mismatch",
                                int(row["id"]),
                                str(path),
                                f"记录大小 {row['file_size']} 字节，"
                                f"实际 {path.stat().st_size} 字节",
                            )
                        )
            archive_path = row.get("archive_path")
            if archive_path:
                archived = Path(archive_path)
                known_paths.add(self._path_key(archived))
                if row.get("archive_status") == "archived" and not archived.is_file():
                    issues.append(
                        ConsistencyIssue(
                            "archive_missing",
                            int(row["id"]),
                            str(archived),
                            "归档状态为 archived 但归档文件不存在",
                        )
                    )

        orphan_files = self._collect_orphans(known_paths, issues)
        return ConsistencyReport(
            issues=tuple(issues),
            checked_records=len(rows),
            orphan_files=orphan_files,
        )

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return str(path.resolve()).lower()
        except OSError:
            return str(path).lower()

    def _collect_orphans(
        self,
        known_paths: set[str],
        issues: list[ConsistencyIssue],
    ) -> int:
        try:
            root = Path(self._recordings_dir_provider())
        except Exception:  # noqa: BLE001
            return 0
        if not root.is_dir():
            return 0
        archive_root: Optional[Path] = None
        if self._archive_dir_provider is not None:
            try:
                archive_root = self._archive_dir_provider()
            except Exception:  # noqa: BLE001
                archive_root = None
        archive_key = (
            str(Path(archive_root).resolve()).lower() if archive_root else ""
        )

        orphans = 0
        for path in root.rglob("*"):
            if orphans >= _MAX_ORPHAN_REPORT:
                break
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if path.suffix.lower() not in _MEDIA_SUFFIXES:
                continue
            key = self._path_key(path)
            if archive_key and key.startswith(archive_key + "\\"):
                continue
            if key in known_paths:
                continue
            orphans += 1
            issues.append(
                ConsistencyIssue(
                    "orphan_file",
                    None,
                    str(path),
                    "磁盘存在录制文件但数据库无对应记录",
                )
            )
        return orphans

    def delete_recordings(
        self,
        recording_ids: Iterable[int],
        *,
        recycler: Callable[[Sequence[Path]], None] = recycle_paths,
    ) -> DeleteReport:
        """把选中的录制文件移入回收站，并标记数据库记录为 deleted。

        拒绝录制中的记录与不存在的文件；不会删除数据库历史行。
        """
        deleted: list[int] = []
        skipped: list[tuple[int, str]] = []
        archived_copies: list[int] = []
        paths: list[Path] = []
        size_by_id: dict[int, int] = {}

        for recording_id in dict.fromkeys(int(value) for value in recording_ids):
            row = self._db.get_recording(recording_id)
            if row is None:
                skipped.append((recording_id, "记录不存在"))
                continue
            if row.get("status") == "recording":
                skipped.append((recording_id, "录制进行中，禁止清理"))
                continue
            file_path = row.get("file_path")
            if not file_path:
                skipped.append((recording_id, "记录没有文件路径"))
                continue
            path = Path(file_path)
            if not path.is_file():
                skipped.append((recording_id, "文件不存在"))
                continue
            if (
                row.get("archive_status") == "archived"
                and row.get("archive_path")
                and Path(row["archive_path"]).is_file()
            ):
                archived_copies.append(recording_id)
            paths.append(path)
            deleted.append(recording_id)
            size_by_id[recording_id] = int(row.get("file_size") or 0)

        if paths:
            recycler(paths)
            self._db.mark_recordings_deleted(deleted)
        return DeleteReport(
            deleted=tuple(deleted),
            skipped=tuple(skipped),
            archived_copies=tuple(archived_copies),
            freed_bytes=sum(size_by_id.values()),
        )
