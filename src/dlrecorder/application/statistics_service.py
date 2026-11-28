"""统计与诊断数据聚合（P1）。

所有统计均为只读查询；目录大小扫描有文件数上限，避免超大媒体库阻塞 UI。
结果可直接序列化为可导出的诊断/统计报告。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from ..infrastructure.db import Database
from ..infrastructure.disk import directory_size_bytes, free_space_mb
from ..infrastructure.resource_monitor import ProcessResourceSampler

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "failed": "录制失败",
    "interrupted": "异常中断",
    "recording": "录制中（可能异常）",
    "completed": "已完成",
}


class StatisticsService:
    def __init__(
        self,
        db: Database,
        *,
        recordings_dir_provider: Callable[[], Path],
        archive_dir_provider: Callable[[], Path],
        thumbnails_dir: Optional[Path] = None,
        sampler: Optional[ProcessResourceSampler] = None,
    ) -> None:
        self._db = db
        self._recordings_dir = recordings_dir_provider
        self._archive_dir = archive_dir_provider
        self._thumbnails_dir = Path(thumbnails_dir) if thumbnails_dir else None
        self._sampler = sampler or ProcessResourceSampler()

    @property
    def db_path(self) -> Path:
        return Path(self._db.db_path)

    @property
    def schema_version(self) -> int:
        return self._db.schema_version

    def get_setting(self, key: str, default: str = "") -> str:
        return self._db.get_setting(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self._db.set_setting(key, value)

    # ---------- 概览 ----------
    def overview(self, now: Optional[datetime] = None) -> dict:
        moment = now or datetime.now()
        summary = self._db.recording_summary(
            day=moment.strftime("%Y-%m-%d"), month=moment.strftime("%Y-%m")
        )
        finished = summary["completed"] + summary["failed"]
        summary["success_rate"] = (
            round(summary["completed"] / finished * 100.0, 1) if finished else None
        )
        return summary

    def daily(self, days: int = 30, now: Optional[datetime] = None) -> list[dict]:
        moment = now or datetime.now()
        since = (moment - timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d")
        return self._db.daily_recording_stats(since_day=since, limit=days)

    def monthly(self, months: int = 12, now: Optional[datetime] = None) -> list[dict]:
        moment = now or datetime.now()
        year = moment.year
        month = moment.month - (max(1, int(months)) - 1)
        while month <= 0:
            month += 12
            year -= 1
        since = f"{year:04d}-{month:02d}"
        return self._db.monthly_recording_stats(since_month=since, limit=months)

    def anchors(self, limit: int = 100) -> list[dict]:
        return self._db.anchor_recording_stats(limit=limit)

    def failure_reasons(self) -> list[dict]:
        rows = self._db.recording_failure_stats()
        return [
            {
                "status": row["status"],
                "label": _STATUS_LABELS.get(str(row["status"]), str(row["status"])),
                "total": int(row["total"]),
            }
            for row in rows
        ]

    # ---------- 存储 ----------
    def storage(self) -> dict:
        recordings = directory_size_bytes(self._recordings_dir())
        archive = directory_size_bytes(self._archive_dir())
        thumbnails = (
            directory_size_bytes(self._thumbnails_dir)
            if self._thumbnails_dir is not None
            else 0
        )
        free = free_space_mb(self._recordings_dir())
        return {
            "recordings_bytes": recordings,
            "archive_bytes": archive,
            "thumbnails_bytes": thumbnails,
            "total_bytes": recordings + archive + thumbnails,
            "free_mb": free,
        }

    # ---------- 资源 ----------
    def resources(self) -> dict:
        return self._sampler.sample()

    # ---------- 报告 ----------
    def build_report(self) -> dict:
        now = datetime.now()
        return {
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "overview": self.overview(now),
            "daily": self.daily(30, now),
            "monthly": self.monthly(12, now),
            "anchors": self.anchors(100),
            "failure_reasons": self.failure_reasons(),
            "storage": self.storage(),
            "resources": self.resources(),
            "schema_version": self._db.schema_version,
        }

    def export_report(self, target: Path) -> Path:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.build_report()
        temporary = target.with_name(target.name + ".part")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        temporary.replace(target)
        logger.info("统计报告已导出: %s", target)
        return target
