"""抖音直播间解析器。

将抖音接口相关逻辑与通用监控逻辑隔离。

生产装配默认使用 ``RealDouyinParser``（匿名 Cookie 会话，不使用用户凭据）；
测试使用 MockParser / 离线 Fake 客户端。真实实测：未签名的 web enter 接口返回
空 200，公开直播页的 React Flight 数据包含房间状态与流地址。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from ..domain.states import ParseResult, classify_response
from ..infrastructure.http_client import HttpClient, HttpResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParseDetail:
    """一次解析的完整结果。

    ``stream_url`` 仅在解析成功且响应中存在可用地址时非空，供录制模块使用；
    不写入数据库。
    """

    result: ParseResult
    status: Optional[int] = None
    stream_url: Optional[str] = None


class DouyinParser:
    """抖音解析器抽象基类。"""

    def fetch_and_parse(self, web_rid: str) -> tuple[ParseResult, Optional[int]]:
        """获取并解析直播间状态，返回 (ParseResult, status_value)。"""
        raise NotImplementedError

    def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
        """获取完整解析结果；默认实现不提供流地址。"""
        result, status = self.fetch_and_parse(web_rid)
        return ParseDetail(result=result, status=status)


def _first_valid_url(mapping: object) -> Optional[str]:
    if not isinstance(mapping, dict):
        return None
    # 按常见清晰度优先级取第一个可用地址；具体键名需真实接口验证。
    for key in ("FULL_HD1", "HD1", "SD1", "SD2"):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in mapping.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_room_stream_url(room: dict) -> Optional[str]:
    """从真实房间对象选择浏览器可直接消费的 FLV/HLS 地址。"""
    for field in ("stream_url", "web_stream_url"):
        stream_url = room.get(field)
        if isinstance(stream_url, str):
            if stream_url.strip():
                return stream_url.strip()
            continue
        if not isinstance(stream_url, dict):
            continue
        for key in ("flv_pull_url", "hls_pull_url_map"):
            url = _first_valid_url(stream_url.get(key))
            if url:
                return url
        for key in ("hls_pull_url", "rtmp_pull_url"):
            value = stream_url.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


_PACE_CHUNK_RE = re.compile(
    r'self\.__pace_f\.push\(\[1,\s*("(?:\\.|[^"\\])*")\s*\]\)'
)


def _extract_page_room(body_bytes: bytes, web_rid: str) -> Optional[dict]:
    """从公开直播页的 React Flight 记录中定位目标房间。

    Flight 数据由多个 JavaScript 字符串块组成；块拼接后，每行以记录 ID 和
    冒号开头。只需 JSON 解码包含房间字段的记录，不执行页面 JavaScript。
    """
    text = body_bytes.decode("utf-8", "replace")
    parts: list[str] = []
    for match in _PACE_CHUNK_RE.finditer(text):
        try:
            parts.append(json.loads(match.group(1)))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    for line in "".join(parts).splitlines():
        if '"roomId"' not in line or '"web_rid"' not in line:
            continue
        remaining = line
        while remaining:
            _record_id, separator, payload = remaining.partition(":")
            if not separator:
                break
            payload = re.sub(r"^T[0-9a-fA-F]+,", "", payload, count=1)
            try:
                data, end = json.JSONDecoder().raw_decode(payload)
            except json.JSONDecodeError:
                break
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if str(node.get("web_rid")) == web_rid and "roomId" in node:
                        nested = node.get("room")
                        if not isinstance(nested, dict):
                            return node
                        room = dict(nested)
                        for key in ("roomId", "web_rid", "web_stream_url"):
                            if key not in room and key in node:
                                room[key] = node[key]
                        return room
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
            remaining = payload[end:]
    return None


def extract_stream_url(body_bytes: bytes) -> Optional[str]:
    """从旧版 web enter JSON 响应中提取直播流地址。"""
    try:
        data = json.loads(body_bytes.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 非法响应由 classify_response 处理
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    rooms = payload.get("data")
    if not isinstance(rooms, list) or not rooms or not isinstance(rooms[0], dict):
        return None
    return _extract_room_stream_url(rooms[0])


_RISK_CONTROL_MARKERS = ("验证码中间页", "sec_sdk_build", "captcha")


def _is_risk_control_page(body_bytes: bytes) -> bool:
    """识别抖音安全验证中间页（标题或验证脚本特征）。"""
    text = body_bytes.decode("utf-8", "replace").lower()
    return any(marker.lower() in text for marker in _RISK_CONTROL_MARKERS)


class RealDouyinParser(DouyinParser):
    """真实抖音解析器：读取公开直播页内嵌数据，不执行页面 JavaScript。

    仅捕获可恢复的传输层异常（超时/连接等），程序缺陷照常向上抛出。
    页面为风控验证中间页时返回 ``RISK_CONTROL``；缺少目标房间数据时返回
    ``API_MALFORMED``，均不擅自判定下播或不存在。
    """

    LIVE_PAGE_URL = "https://www.douyin.com/root/live/"

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.douyin.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def __init__(
        self,
        http_client: HttpClient,
        *,
        timeout_s: float = 10.0,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须为正")
        self._http = http_client
        self._timeout_s = timeout_s

    def _build_url(self, web_rid: str) -> str:
        """构建公开直播页 URL。"""
        return f"{self.LIVE_PAGE_URL}{urllib.parse.quote(web_rid, safe='')}"

    def fetch_and_parse(self, web_rid: str) -> tuple[ParseResult, Optional[int]]:
        detail = self.fetch_and_parse_detail(web_rid)
        return detail.result, detail.status

    def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
        """执行一次请求并映射为完整结果。

        传输层失败统一映射为 ``NETWORK_ERROR``，不覆盖最后确认的直播状态；
        未预期的异常不在此处吞掉，由监控循环上报。
        """
        rid = (web_rid or "").strip()
        if not rid:
            raise ValueError("web_rid 不能为空")

        url = self._build_url(rid)
        try:
            resp = self._http.get(
                url,
                headers=dict(self.DEFAULT_HEADERS),
                timeout=self._timeout_s,
            )
        except TimeoutError as e:
            logger.warning("抖音请求超时 web_rid=%s: %s", rid, e)
            return ParseDetail(ParseResult.NETWORK_ERROR)
        except OSError as e:
            logger.warning("抖音请求网络失败 web_rid=%s: %s", rid, e)
            return ParseDetail(ParseResult.NETWORK_ERROR)

        if resp.is_transport_error:
            logger.warning(
                "抖音请求传输层失败 web_rid=%s kind=%s detail=%s",
                rid,
                resp.error_kind,
                resp.error_message,
            )
        if resp.status is None:
            return ParseDetail(ParseResult.NETWORK_ERROR)
        if resp.status != 200:
            return ParseDetail(ParseResult.HTTP_ERROR)
        if not resp.body:
            return ParseDetail(ParseResult.EMPTY_RESPONSE)

        room = _extract_page_room(resp.body, rid)
        if room is None:
            if _is_risk_control_page(resp.body):
                logger.warning(
                    "抖音风控验证页 web_rid=%s：无法获取房间数据，监控将按间隔自动重试",
                    rid,
                )
                return ParseDetail(ParseResult.RISK_CONTROL)
            return ParseDetail(ParseResult.API_MALFORMED)
        return ParseDetail(
            result=ParseResult.SUCCESS,
            status=room.get("status"),
            stream_url=_extract_room_stream_url(room),
        )


class MockParser(DouyinParser):
    """Mock 解析器：返回预设结果，用于界面与状态管理测试。

    预设结果通过 set_response 配置，支持按 web_rid 区分；可选提供流地址，
    供录制流程离线测试使用。
    """

    def __init__(self) -> None:
        self._responses: dict[str, ParseDetail] = {}
        self._default = ParseDetail(ParseResult.SUCCESS, 2)

    def set_response(
        self,
        web_rid: str,
        result: ParseResult,
        status: Optional[int] = None,
        stream_url: Optional[str] = None,
    ) -> None:
        self._responses[web_rid] = ParseDetail(result, status, stream_url)

    def set_default(
        self,
        result: ParseResult,
        status: Optional[int] = None,
        stream_url: Optional[str] = None,
    ) -> None:
        self._default = ParseDetail(result, status, stream_url)

    def fetch_and_parse(self, web_rid: str) -> tuple[ParseResult, Optional[int]]:
        detail = self.fetch_and_parse_detail(web_rid)
        return detail.result, detail.status

    def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
        # 子类若重写了 fetch_and_parse（例如测试用解析器），必须尊重其实现，
        # 不能直接用 Mock 预设覆盖，否则会绕过子类逻辑。
        if type(self).fetch_and_parse is not MockParser.fetch_and_parse:
            return super().fetch_and_parse_detail(web_rid)
        return self._responses.get(web_rid, self._default)


def classify_http_response(resp: HttpResponse) -> tuple[ParseResult, Optional[int], bool]:
    """将 HTTP 响应分类为 ParseResult（供真实解析器后续使用）。"""
    return classify_response(resp.status, resp.body, resp.content_type)
