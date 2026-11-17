"""领域状态模型：ParseResult 与 RoomState（严格遵循阶段 2C V3.2.1）。

不依赖 Qt / HTTP / SQL。
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Optional


class ParseResult(str, enum.Enum):
    SUCCESS = "SUCCESS"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    API_MALFORMED = "API_MALFORMED"
    NETWORK_ERROR = "NETWORK_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    ROOM_NOT_EXIST = "ROOM_NOT_EXIST"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"

    def __str__(self) -> str:  # 便于 JSON/日志输出
        return self.value


# 已真实验证的 status 映射。
# 2026-11-16 实测：
# - web_rid=95694179922：status=2、status_str="2"、有 FLV 流地址（直播中）；
# - web_rid=1：status=4、status_str="4"、web_stream_url=null（无流地址）。
KNOWN_STATUS_MAP: dict[int, str] = {
    2: "LIVE",
    4: "OFFLINE_CONFIRMED",
}

CONFIRMED_STATES = ("LIVE", "OFFLINE_CONFIRMED")


@dataclass
class RoomState:
    """房间直播状态。

    last_confirmed_room_state 仅取 LIVE / OFFLINE_CONFIRMED / None。
    STATUS_UNKNOWN 通过 current_observation 表达，不进入 last_confirmed_room_state。
    """

    last_confirmed_room_state: Optional[str] = None  # LIVE / OFFLINE_CONFIRMED / None
    current_observation: Optional[int] = None
    last_parse_result: Optional[ParseResult] = None
    last_parse_success_at: Optional[float] = None
    last_confirmed_state_at: Optional[float] = None
    # 最近一次成功解析得到的直播流地址；仅内存使用，不持久化。
    last_stream_url: Optional[str] = None
    expire_threshold_s: float = 600.0

    @property
    def state_expired(self) -> Optional[bool]:
        if self.last_confirmed_state_at is None:
            # 尚无确认状态，不得当成已过期
            return None
        return (time.time() - self.last_confirmed_state_at) > self.expire_threshold_s

    def update(self, parse_result: ParseResult, status_value: Optional[int]) -> None:
        """按 V3.2.1 规则更新状态。"""
        self.last_parse_result = parse_result
        if parse_result == ParseResult.SUCCESS:
            self.last_parse_success_at = time.time()
            self.current_observation = status_value
            if status_value in KNOWN_STATUS_MAP:
                self.last_confirmed_room_state = KNOWN_STATUS_MAP[status_value]
                self.last_confirmed_state_at = time.time()
            # status 未知：不更新 last_confirmed_room_state / last_confirmed_state_at
        # 非 SUCCESS：仅更新 last_parse_result，不改变最后确认状态


def classify_response(
    http_status: Optional[int],
    body_bytes: bytes,
    content_type: str = "",
) -> tuple[ParseResult, Optional[int], bool]:
    """对 HTTP 响应做 ParseResult 分类。

    返回 (ParseResult, status_value, has_stream_url)。
    """
    if http_status is None:
        return ParseResult.NETWORK_ERROR, None, False
    if http_status == 200:
        if not body_bytes:
            return ParseResult.EMPTY_RESPONSE, None, False
        try:
            import json
            data = json.loads(body_bytes.decode("utf-8", "replace"))
        except Exception:
            return ParseResult.API_MALFORMED, None, False
        if not isinstance(data, dict) or "data" not in data:
            return ParseResult.API_MALFORMED, None, False
        d = data.get("data")
        if not isinstance(d, dict):
            return ParseResult.API_MALFORMED, None, False
        arr = d.get("data")
        if isinstance(arr, list) and arr:
            room = arr[0]
            status = room.get("status")
            has_stream = bool(room.get("stream_url"))
            return ParseResult.SUCCESS, status, has_stream
        # data.data 为空列表或非列表：需明确不存在证据才能定 ROOM_NOT_EXIST
        return ParseResult.API_MALFORMED, None, False
    return ParseResult.HTTP_ERROR, None, False
