"""直播间领域实体（不依赖 Qt）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .states import RoomState

_WEB_RID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_DISPLAY_NAME_LEN = 64


def validate_web_rid(value: str) -> str:
    """校验并规范化直播间 ID。非法输入抛 ValueError。"""
    rid = (value or "").strip()
    if not rid:
        raise ValueError("直播间 ID 不能为空")
    if not _WEB_RID_RE.fullmatch(rid):
        raise ValueError(
            "直播间 ID 只能包含字母、数字、下划线、点和短横线，长度 1-64"
        )
    return rid


def validate_display_name(value: str) -> str:
    """规范化显示名称；超长或含控制字符时拒绝。"""
    name = (value or "").strip()
    if len(name) > _MAX_DISPLAY_NAME_LEN:
        raise ValueError(f"显示名称不能超过 {_MAX_DISPLAY_NAME_LEN} 个字符")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("显示名称不能包含控制字符")
    return name


@dataclass
class Room:
    id: Optional[int] = None
    web_rid: str = ""
    display_name: str = ""
    anchor_id: str = ""
    enabled: bool = True
    state: RoomState = field(default_factory=RoomState)
