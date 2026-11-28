"""录制库检索条件。"""
from __future__ import annotations

from dataclasses import dataclass


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
    limit: int = 500
    offset: int = 0
