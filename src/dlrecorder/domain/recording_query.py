"""录制库检索条件。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RecordingQuery:
    """录制记录检索条件；空字段表示不限制。"""

    keyword: str = ""
    date_from: str = ""
    date_to: str = ""
    status: str = ""
    archive_status: str = ""
    min_size: int = 0
    max_size: int = 0
    tag_ids: tuple[int, ...] = field(default_factory=tuple)
    favorite: Optional[bool] = None
    min_duration_s: float = 0.0
    max_duration_s: float = 0.0
    sort_by: str = "newest"
    limit: int = 500
    offset: int = 0
