"""直播间领域实体（不依赖 Qt）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .states import RoomState

_WEB_RID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_DISPLAY_NAME_LEN = 64

# 支持直接粘贴的抖音直播间链接：live.douyin.com/<rid>、
# www.douyin.com/root/live/<rid>、www.douyin.com/live/<rid>。
_LIVE_URL_RE = re.compile(
    r"(?:https?://)?(?:live\.douyin\.com|(?:www\.)?douyin\.com/(?:root/)?live)"
    r"/(?P<rid>[A-Za-z0-9_.-]{1,64})",
    re.IGNORECASE,
)
# 手机端分享的短链接（v.douyin.com/xxxx）需要联网跳转才能得到 web_rid。
_SHORT_LINK_RE = re.compile(
    r"(?:https?://)?(?:v\.douyin\.com|v\.iesdouyin\.com)/\S+", re.IGNORECASE
)


def _extract_web_rid(text: str) -> str:
    """从输入中提取 web_rid；纯 ID 原样返回，无法识别时抛 ValueError。"""
    match = _LIVE_URL_RE.search(text)
    if match:
        return match.group("rid")
    if _SHORT_LINK_RE.search(text):
        raise ValueError(
            "暂不支持抖音短链接（v.douyin.com），请在浏览器中打开该链接后，"
            "复制地址栏里 live.douyin.com/<数字> 形式的完整地址再添加"
        )
    if "://" in text:
        raise ValueError(
            "无法从链接中识别直播间 ID，请使用 live.douyin.com/<数字> 形式的直播间地址"
        )
    return text


def validate_web_rid(value: str) -> str:
    """校验并规范化直播间 ID；允许直接粘贴抖音直播间链接。非法输入抛 ValueError。"""
    rid = _extract_web_rid((value or "").strip())
    if not rid:
        raise ValueError("直播间 ID 不能为空")
    if not _WEB_RID_RE.fullmatch(rid):
        raise ValueError(
            "直播间 ID 只能包含字母、数字、下划线、点和短横线，长度 1-64；"
            "也可以粘贴 live.douyin.com 的直播间链接"
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
