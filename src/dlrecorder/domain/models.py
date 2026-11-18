"""直播间领域实体（不依赖 Qt）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .states import RoomState

_WEB_RID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_DISPLAY_NAME_LEN = 64

# 支持的抖音域名：直播间页（live.douyin.com）与站内页面（www.douyin.com / douyin.com）。
_LIVE_HOSTS = frozenset({"live.douyin.com", "www.douyin.com", "douyin.com"})
# 路径中的房间号只按“整段数字”识别，避免从 anchor_id 等参数或其他位置误取数字。
# 路径中的房间号只按“整段 ASCII 数字”识别，避免从 anchor_id 等参数或其他位置误取数字；
# 这里不用 \d，因为 \d 会匹配全角/其他语言的 Unicode 十进制数字。
_RID_SEGMENT_RE = re.compile(r"^[0-9]{1,20}$")
# 从文本中定位抖音链接（可带分享文案，scheme 可省略，允许显式端口）。
_DOUYIN_URL_RE = re.compile(
    r"(?<![A-Za-z0-9.-])(?:https?://)?(?:(?:live|www)\.)?douyin\.com(?::[0-9]{1,5})?/"
    r"[^\s'\"<>，。；、）()]*",
    re.IGNORECASE,
)
# 直播首页形式的链接把房间号放在查询参数里：
# live.douyin.com/?...&live_web_rid=<rid>&...（仅抖音域名下生效）。
_LIVE_RID_PARAM_RE = re.compile(
    r"[?&]live_web_rid=(?P<rid>[A-Za-z0-9_.-]{1,64})", re.IGNORECASE
)
# 手机端分享的短链接（v.douyin.com/xxxx）需要联网跳转才能得到 web_rid。
_SHORT_LINK_RE = re.compile(
    r"(?:https?://)?(?:v\.douyin\.com|v\.iesdouyin\.com)/\S+", re.IGNORECASE
)
_HOST_RE = re.compile(r"https?://([^/\s?#:]+)", re.IGNORECASE)

_RID_HINT = (
    "请使用 live.douyin.com/<数字> 或 www.douyin.com/follow/live/<数字> 形式的地址"
)


def _rid_from_douyin_url(url: str) -> Optional[str]:
    """按抖音链接的结构提取 web_rid；取不到返回 None。

    - ``live.douyin.com/<数字>``：路径首段即房间号；
    - ``www.douyin.com``：路径中 ``live`` 段之后的整段数字
      （``/live/<数字>``、``/root/live/<数字>``、``/follow/live/<数字>`` 等）；
    - 兜底：``live_web_rid`` 查询参数（仅抖音域名）。

    房间号只认“整段数字”，``anchor_id`` 等其他参数不参与识别。
    """
    candidate = url if "://" in url else "https://" + url
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host not in _LIVE_HOSTS:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if host == "live.douyin.com":
        if segments and _RID_SEGMENT_RE.fullmatch(segments[0]):
            return segments[0]
    else:
        for index in range(len(segments) - 1):
            if segments[index].lower() != "live":
                continue
            if _RID_SEGMENT_RE.fullmatch(segments[index + 1]):
                return segments[index + 1]
    for value in parse_qs(parts.query).get("live_web_rid", []):
        if _WEB_RID_RE.fullmatch(value):
            return value
    return None


def _extract_web_rid(text: str) -> str:
    """从输入中提取 web_rid；纯 ID 原样返回，无法识别时抛 ValueError。"""
    if _SHORT_LINK_RE.search(text):
        raise ValueError(
            "暂不支持抖音短链接（v.douyin.com），请在浏览器中打开该链接后，"
            "复制地址栏里 live.douyin.com/<数字> 形式的完整地址再添加"
        )
    saw_douyin_url = False
    for match in _DOUYIN_URL_RE.finditer(text):
        saw_douyin_url = True
        rid = _rid_from_douyin_url(match.group(0))
        if rid:
            return rid
    param = _LIVE_RID_PARAM_RE.search(text)
    if param and "douyin.com" in text.lower():
        return param.group("rid")
    if saw_douyin_url:
        raise ValueError("无法从链接中识别直播间 ID：链接路径中没有有效的直播间号，" + _RID_HINT)
    if "://" in text:
        host_match = _HOST_RE.search(text)
        detail = f"不支持的域名 {host_match.group(1)}" if host_match else "链接格式无法识别"
        raise ValueError(
            "无法从链接中识别直播间 ID："
            + detail
            + "，仅支持 live.douyin.com 或 www.douyin.com 的直播间链接"
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
