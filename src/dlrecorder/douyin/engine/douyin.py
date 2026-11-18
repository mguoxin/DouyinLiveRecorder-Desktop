"""抖音直播数据获取与流地址选择。

实现来源：ihmily/DouyinLiveRecorder v4.0.7（MIT）``src/spider.py`` 与
``src/stream.py`` 的抖音分支，保留其请求参数、签名方式与流地址选择规则，
仅做以下改造：

- 不使用全局 ``print``：错误通过异常类型上抛，由适配层记录日志并映射状态。
- 去除对上游 logger/utils/room 模块的依赖，改为本包内自包含实现。
- 只保留抖音链路，不引入其他平台的解析代码。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx

from .ab_sign import ab_sign

logger = logging.getLogger(__name__)

# 上游仓库内置的匿名 ttwid（公开代码中的固定值，不是用户凭据）。
_DEFAULT_COOKIE = (
    "ttwid=1%7C2iDIYVmjzMcpZ20fcaFde0VghXAA3NaNXE_SLR68IyE%7C1761045455"
    "%7Cab35197d5cfb21df6cbb2fa7ef1c9262206b062c315b9d04da746d0b37dfbc7d"
)
_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/116.0.5845.97 Safari/537.36 Core/1.116.567.400 QQBrowser/19.7.6764.400"
)

# 上游清晰度到地址列表下标的映射（or4 原画位于下标 0）。
QUALITY_MAPPING = {"OD": 0, "BD": 0, "UHD": 1, "HD": 2, "SD": 3, "LD": 4}
DEFAULT_QUALITY = "UHD"


class EngineError(Exception):
    """引擎层可分类错误基类。"""


class RiskControlError(EngineError):
    """接口返回空响应/验证页，判定为平台风控。"""


class RoomUnavailableError(EngineError):
    """房间数据缺失（不存在、VR 直播或接口结构变化）。"""


class StreamInfoMissingError(EngineError):
    """直播中但没有可用的流地址（VR/特殊玩法等）。"""


@dataclass(frozen=True)
class RoomSnapshot:
    """一次成功解析得到的房间快照。"""

    status: int
    anchor_name: str
    title: str
    flv_url: Optional[str]
    hls_url: Optional[str]
    quality: str


def quality_index(quality: str) -> tuple[str, int]:
    """返回 (清晰度名, 地址列表下标)。"""
    name = str(quality or "").strip().upper() or "OD"
    if name.isdigit():
        keys = list(QUALITY_MAPPING.keys())
        name = keys[int(name[0]) % len(keys)]
    return name, QUALITY_MAPPING.get(name, 0)


def _build_enter_url(web_rid: str, user_agent: str) -> str:
    params = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "116.0.0.0",
        "web_rid": web_rid,
        "msToken": "",
    }
    api = "https://live.douyin.com/webcast/room/web/enter/?" + urllib.parse.urlencode(params)
    return api + "&a_bogus=" + ab_sign(urllib.parse.urlparse(api).query, user_agent)


async def _get_text(url: str, headers: dict[str, str], timeout_s: float) -> str:
    # 上游实测使用 verify=False（抖音部分 CDN/接口证书链在部分环境不完整）；
    # 本请求不携带任何用户凭据，仅读取公开直播数据。
    async with httpx.AsyncClient(
        timeout=timeout_s, verify=False, http2=True, follow_redirects=True
    ) as client:
        response = await client.get(url, headers=headers)
    if response.status_code != 200:
        raise EngineError(f"HTTP {response.status_code}")
    return response.text


def _inject_origin_urls(room_data: dict) -> None:
    """把原画（or4）地址注入流地址字典首位，与上游行为一致。"""
    stream_url = room_data.get("stream_url") or {}
    live_core_sdk_data = stream_url.get("live_core_sdk_data")
    if not live_core_sdk_data:
        return
    pull_datas = stream_url.get("pull_datas")
    if pull_datas:
        key = next(iter(pull_datas))
        stream_data = pull_datas[key].get("stream_data", "")
    else:
        stream_data = (live_core_sdk_data.get("pull_data") or {}).get("stream_data", "")
    if not stream_data:
        return
    try:
        parsed = json.loads(stream_data)
    except json.JSONDecodeError:
        return
    data = parsed.get("data") or {}
    if "origin" not in data:
        return
    origin_main = data["origin"].get("main") or {}
    sdk_params = json.loads(origin_main.get("sdk_params") or "{}")
    codec = sdk_params.get("VCodec") or ""
    origin_hls = {"ORIGIN": origin_main["hls"] + "&codec=" + codec}
    origin_flv = {"ORIGIN": origin_main["flv"] + "&codec=" + codec}
    stream_url["hls_pull_url_map"] = {**origin_hls, **(stream_url.get("hls_pull_url_map") or {})}
    stream_url["flv_pull_url"] = {**origin_flv, **(stream_url.get("flv_pull_url") or {})}
    room_data["stream_url"] = stream_url


async def _fetch_room_data_async(
    web_rid: str, cookie: Optional[str], timeout_s: float
) -> dict:
    headers = {
        "cookie": cookie or _DEFAULT_COOKIE,
        "referer": "https://live.douyin.com/335354047186",
        "user-agent": _WEB_USER_AGENT,
    }
    url = _build_enter_url(web_rid, _WEB_USER_AGENT)
    try:
        text = await _get_text(url, headers, timeout_s)
    except httpx.HTTPError as e:  # 超时/连接/DNS 等传输层失败
        raise EngineError(f"网络请求失败: {e}") from e

    if not text or not text.strip():
        raise RiskControlError("接口返回空响应（疑似风控）")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise RoomUnavailableError(f"接口返回非 JSON（疑似风控页）: {e}") from e

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("data"):
        raise RoomUnavailableError("接口未返回房间数据")
    room_data = data["data"][0]
    user = data.get("user") or {}
    if isinstance(user, dict) and user.get("nickname"):
        room_data["anchor_name"] = user["nickname"]
    if data.get("title"):
        room_data.setdefault("title", data["title"])
    return room_data


def fetch_room_data(
    web_rid: str,
    *,
    cookie: Optional[str] = None,
    timeout_s: float = 10.0,
) -> dict:
    """同步获取房间原始数据（内部使用独立事件循环）。"""
    rid = (web_rid or "").strip()
    if not rid:
        raise ValueError("web_rid 不能为空")
    return asyncio.run(_fetch_room_data_async(rid, cookie, timeout_s))


async def _url_alive(url: str, timeout_s: float) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout_s, verify=False) as client:
            response = await client.head(url, follow_redirects=True)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _padded(values: list[str], size: int = 5) -> list[str]:
    padded = list(values)
    while padded and len(padded) < size:
        padded.append(padded[-1])
    return padded


async def _select_stream_async(
    room_data: dict, quality: str, timeout_s: float
) -> tuple[Optional[str], Optional[str], str]:
    stream_url = room_data.get("stream_url") or {}
    flv_values = [v for v in (stream_url.get("flv_pull_url") or {}).values() if v]
    hls_values = [v for v in (stream_url.get("hls_pull_url_map") or {}).values() if v]
    if not flv_values and not hls_values:
        return None, None, quality
    flv_list = _padded(flv_values)
    hls_list = _padded(hls_values)
    name, index = quality_index(quality)
    order = [index] + [i for i in range(len(flv_list)) if i != index]
    for i in order:
        flv = flv_list[i] if i < len(flv_list) else None
        hls = hls_list[i] if i < len(hls_list) else None
        if flv and await _url_alive(flv, timeout_s):
            return flv, hls, name
    # 探测全部失败时仍返回首选地址：录制阶段会再次尝试并如实报错。
    return (
        flv_list[index] if index < len(flv_list) else None,
        hls_list[index] if index < len(hls_list) else None,
        name,
    )


def parse_room_snapshot(
    web_rid: str,
    *,
    quality: str = DEFAULT_QUALITY,
    cookie: Optional[str] = None,
    timeout_s: float = 10.0,
) -> RoomSnapshot:
    """获取房间状态与可用流地址。

    直播中（status=2）但没有流地址时抛出 ``StreamInfoMissingError``，
    避免把"解析成功"误报为"可录制"。
    """
    room_data = fetch_room_data(web_rid, cookie=cookie, timeout_s=timeout_s)
    status = room_data.get("status", 4)
    anchor = room_data.get("anchor_name") or ""
    title = room_data.get("title") or ""
    try:
        status = int(status)
    except (TypeError, ValueError):
        raise RoomUnavailableError(f"无法识别的直播状态: {status!r}") from None
    if status != 2:
        return RoomSnapshot(status, anchor, title, None, None, quality)

    if "stream_url" not in room_data:
        raise StreamInfoMissingError("直播中但接口未返回流地址（可能为 VR/特殊玩法）")
    _inject_origin_urls(room_data)
    flv, hls, name = asyncio.run(_select_stream_async(room_data, quality, timeout_s))
    if not flv and not hls:
        raise StreamInfoMissingError("直播中但未取得可用流地址")
    return RoomSnapshot(status, anchor, title, flv, hls, name)


__all__ = [
    "DEFAULT_QUALITY",
    "EngineError",
    "RiskControlError",
    "RoomSnapshot",
    "RoomUnavailableError",
    "StreamInfoMissingError",
    "fetch_room_data",
    "parse_room_snapshot",
    "quality_index",
]
