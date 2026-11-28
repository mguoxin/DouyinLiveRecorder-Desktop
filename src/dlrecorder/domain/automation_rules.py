"""自动化规则领域模型与配置校验。

规则配置以 JSON 存储在 ``automation_rules.config``；本模块负责把不可信的
JSON 解析为带类型的对象，并在配置非法时抛出 ``RuleConfigError``，避免引擎
在运行期对不同结构做散落的 ``dict.get`` 判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Optional

RULE_TYPES = (
    "monitor_schedule",
    "recording_schedule",
    "archive",
    "disk_guard",
    "post_process",
)

RULE_TYPE_LABELS = {
    "monitor_schedule": "定时监控",
    "recording_schedule": "定时录制",
    "archive": "自动归档",
    "disk_guard": "磁盘预警",
    "post_process": "录制后处理",
}

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class RuleConfigError(ValueError):
    """规则配置非法。"""


def weekday_label(day: int) -> str:
    if 0 <= day < len(_WEEKDAY_LABELS):
        return _WEEKDAY_LABELS[day]
    return str(day)


def _parse_minute(value: Any, field_name: str) -> int:
    text = str(value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, TypeError) as exc:
        raise RuleConfigError(f"{field_name} 时间格式应为 HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RuleConfigError(f"{field_name} 时间超出范围")
    return hour * 60 + minute


@dataclass(frozen=True)
class ScheduleWindow:
    """按星期与每日时间窗匹配；起止相同表示全天。"""

    days: frozenset[int]
    start_minute: int
    end_minute: int

    @classmethod
    def parse(cls, config: dict) -> "ScheduleWindow":
        raw_days = config.get("days")
        if raw_days is None:
            days = frozenset(range(7))
        else:
            if not isinstance(raw_days, (list, tuple, set)):
                raise RuleConfigError("days 必须是星期数组")
            parsed: set[int] = set()
            for value in raw_days:
                try:
                    day = int(value)
                except (TypeError, ValueError) as exc:
                    raise RuleConfigError("days 包含非法星期") from exc
                if not 0 <= day <= 6:
                    raise RuleConfigError("days 取值必须在 0-6 之间")
                parsed.add(day)
            days = frozenset(parsed)
        start = _parse_minute(config.get("start", "00:00"), "开始")
        end = _parse_minute(config.get("end", "23:59"), "结束")
        return cls(days=days, start_minute=start, end_minute=end)

    def contains(self, moment: datetime) -> bool:
        if moment.weekday() not in self.days:
            return False
        current = moment.hour * 60 + moment.minute
        if self.start_minute == self.end_minute:
            return True
        if self.start_minute < self.end_minute:
            return self.start_minute <= current < self.end_minute
        # 跨午夜窗口，例如 22:30-02:00。
        return current >= self.start_minute or current < self.end_minute

    def describe(self) -> str:
        days = "每天" if len(self.days) == 7 else "、".join(
            weekday_label(day) for day in sorted(self.days)
        )
        start = f"{self.start_minute // 60:02d}:{self.start_minute % 60:02d}"
        end = f"{self.end_minute // 60:02d}:{self.end_minute % 60:02d}"
        if self.start_minute == self.end_minute:
            return f"{days} 全天"
        return f"{days} {start}-{end}"


@dataclass(frozen=True)
class AutomationRule:
    id: int
    room_id: Optional[int]
    name: str
    rule_type: str
    enabled: bool
    priority: int
    config: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "AutomationRule":
        import json

        raw = row.get("config") or "{}"
        try:
            config = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError) as exc:
            raise RuleConfigError(f"规则 #{row.get('id')} 配置不是合法 JSON") from exc
        if not isinstance(config, dict):
            raise RuleConfigError(f"规则 #{row.get('id')} 配置必须是对象")
        return cls(
            id=int(row["id"]),
            room_id=int(row["room_id"]) if row.get("room_id") is not None else None,
            name=str(row.get("name") or ""),
            rule_type=str(row.get("rule_type") or ""),
            enabled=bool(row.get("enabled")),
            priority=int(row.get("priority") or 100),
            config=config,
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    # ---------- 配置访问 ----------
    def schedule(self) -> ScheduleWindow:
        if self.rule_type not in {"monitor_schedule", "recording_schedule"}:
            raise RuleConfigError("该规则类型没有时间窗")
        return ScheduleWindow.parse(self.config)

    def archive_enabled(self) -> bool:
        value = self.config.get("enabled", True)
        if not isinstance(value, bool):
            raise RuleConfigError("archive.enabled 必须是布尔值")
        return value

    def min_free_mb(self, default: float = 2048.0) -> float:
        value = self.config.get("min_free_mb", default)
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise RuleConfigError("min_free_mb 必须是数字") from exc
        if parsed < 0:
            raise RuleConfigError("min_free_mb 不能为负数")
        return parsed

    def post_process_values(self) -> tuple[list[str], bool]:
        raw_tags = self.config.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [part for part in raw_tags.split(",")]
        if not isinstance(raw_tags, (list, tuple)):
            raise RuleConfigError("post_process.tags 必须是数组")
        tags: list[str] = []
        for value in raw_tags:
            text = str(value or "").strip()
            if text and text not in tags:
                tags.append(text)
        favorite = self.config.get("favorite", False)
        if not isinstance(favorite, bool):
            raise RuleConfigError("post_process.favorite 必须是布尔值")
        return tags, favorite


def parse_time_text(text: str) -> time:
    minute = _parse_minute(text, "时间")
    return time(hour=minute // 60, minute=minute % 60)
