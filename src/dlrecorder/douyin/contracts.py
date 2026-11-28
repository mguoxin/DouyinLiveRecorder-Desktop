"""抖音解析响应契约。

把“接口返回了什么”与“业务如何使用”分开：字段缺失、类型变化或取值越界都
转为明确的 ``ContractViolation``，由适配层分类为 ``API_MALFORMED``，绝不
静默当成正常直播数据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Optional
from urllib.parse import urlparse

from ..domain.states import ParseResult

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与 parser 循环导入
    from .parser import ParseDetail

KNOWN_STATUS_VALUES = (2, 4)
MAX_STREAM_URL_LEN = 4096


class ContractViolation(ValueError):
    """响应不满足契约；消息只包含字段名、期望与实际类型，不含原始载荷。"""

    def __init__(self, field: str, expected: str, actual: object) -> None:
        self.field = field
        self.expected = expected
        self.actual = f"{type(actual).__name__}:{str(actual)[:80]!r}"
        super().__init__(f"字段 {field} 期望 {expected}，实际 {self.actual}")


@dataclass(frozen=True)
class RoomSnapshot:
    """通过契约校验的房间快照。"""

    web_rid: str
    room_id: str
    status: int
    title: str
    stream_url: Optional[str] = None


def validate_status(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractViolation("status", "int", value)
    if value not in KNOWN_STATUS_VALUES:
        raise ContractViolation("status", "2 或 4", value)
    return value


def validate_stream_url(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractViolation("stream_url", "str 或 None", value)
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_STREAM_URL_LEN:
        raise ContractViolation("stream_url", "长度受限的 URL", "过长")
    if any(ord(ch) < 32 for ch in text):
        raise ContractViolation("stream_url", "不含控制字符的 URL", "含控制字符")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContractViolation("stream_url", "http/https URL", "协议不符")
    return text


def validate_room(raw: object, requested_web_rid: str) -> RoomSnapshot:
    """校验原始房间字典；字段缺失/类型变化/结构变化均抛出契约异常。"""
    if not isinstance(raw, Mapping):
        raise ContractViolation("room", "房间对象", raw)

    web_rid_value = raw.get("web_rid")
    if web_rid_value is None or str(web_rid_value).strip() != requested_web_rid:
        raise ContractViolation("web_rid", f"与请求一致({requested_web_rid})", web_rid_value)

    room_id_value = raw.get("roomId")
    if isinstance(room_id_value, bool) or room_id_value is None:
        raise ContractViolation("roomId", "非空字符串或整数", room_id_value)
    if isinstance(room_id_value, int):
        room_id = str(room_id_value)
    elif isinstance(room_id_value, str) and room_id_value.strip():
        room_id = room_id_value.strip()
    else:
        raise ContractViolation("roomId", "非空字符串或整数", room_id_value)

    status = validate_status(raw.get("status"))
    title = raw.get("title")
    title_text = title.strip() if isinstance(title, str) else ""
    return RoomSnapshot(
        web_rid=str(web_rid_value).strip(),
        room_id=room_id,
        status=status,
        title=title_text,
    )


def validate_parse_detail(
    detail: object,
    web_rid: str,
    *,
    enforce_stream_url: bool = True,
) -> "ParseDetail":
    """校验解析器返回的 ``ParseDetail``。

    ``enforce_stream_url`` 仅对受信任的本地合成流测试关闭；生产必须开启。
    """
    from .parser import ParseDetail  # 延迟导入，避免模块级循环依赖

    if not isinstance(detail, ParseDetail):
        raise ContractViolation("ParseDetail", "ParseDetail", type(detail).__name__)
    if detail.result != ParseResult.SUCCESS:
        return detail
    status = validate_status(detail.status)
    stream_url = (
        validate_stream_url(detail.stream_url) if enforce_stream_url else detail.stream_url
    )
    return ParseDetail(
        result=ParseResult.SUCCESS,
        status=status,
        stream_url=stream_url,
    )
