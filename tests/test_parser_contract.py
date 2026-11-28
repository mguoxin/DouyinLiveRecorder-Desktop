"""P0-03 解析适配层与接口变更回归测试（全离线，不访问抖音）。"""
import json
from typing import Optional

import pytest

from dlrecorder.domain.states import ParseResult
from dlrecorder.douyin.adapter import DouyinParseAdapter
from dlrecorder.douyin.contracts import (
    ContractViolation,
    validate_parse_detail,
    validate_room,
    validate_stream_url,
)
from dlrecorder.douyin.parser import DouyinParser, ParseDetail, RealDouyinParser
from dlrecorder.douyin.resilience import CircuitBreaker, RetryPolicy
from dlrecorder.infrastructure.http_client import HttpResponse


class ScriptedParser(DouyinParser):
    """按调用次序返回预设结果，用于模拟异常与恢复。"""

    def __init__(self, *items) -> None:
        self.items = list(items)
        self.calls = 0

    def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
        self.calls += 1
        item = self.items[min(self.calls - 1, len(self.items) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class FakeHttpClient:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.requests: list[str] = []

    def get(self, url: str, *, headers=None, timeout=None) -> HttpResponse:
        self.requests.append(url)
        return self.response


def _page_payload(
    *,
    status: object = 2,
    stream_url: object = None,
    web_rid: str = "123456",
    room_key: str = "room",
    room_id: object = "7700000000000000000",
    include_status: bool = True,
) -> bytes:
    room: dict = {"id_str": "7700000000000000000", "title": "测试"}
    if include_status:
        room["status"] = status
    if stream_url is not None:
        room["stream_url"] = stream_url
    wrapper: dict = {"roomId": room_id, "web_rid": web_rid, room_key: room}
    first = json.dumps({"common": {"ts": "1"}}, separators=(",", ":"))
    second = json.dumps([{"detail": wrapper}], separators=(",", ":"))
    flight = f"13:T{len(first):x},{first}14:{second}\n"
    midpoint = len(flight) // 2
    scripts = "".join(
        f"<script>self.__pace_f.push([1,{json.dumps(chunk)}])</script>"
        for chunk in (flight[:midpoint], flight[midpoint:])
    )
    return f"<html><body>{scripts}</body></html>".encode("utf-8")


def _adapter(client: FakeHttpClient) -> DouyinParseAdapter:
    return DouyinParseAdapter(RealDouyinParser(client), retry_policy=RetryPolicy(0))


# ---------- 契约校验 ----------
def test_validate_room_accepts_valid_live_room():
    snapshot = validate_room(
        {"web_rid": "1", "roomId": 123, "status": 2, "title": "标题"}, "1"
    )
    assert snapshot.web_rid == "1"
    assert snapshot.room_id == "123"
    assert snapshot.status == 2
    assert snapshot.title == "标题"


@pytest.mark.parametrize(
    "room",
    [
        {"roomId": "1", "status": 2},  # 缺 web_rid
        {"web_rid": "other", "roomId": "1", "status": 2},  # 房间不匹配
        {"web_rid": "1", "status": 2},  # 缺 roomId
        {"web_rid": "1", "roomId": {"id": 1}, "status": 2},  # roomId 类型变化
        {"web_rid": "1", "roomId": "1"},  # 缺 status
        {"web_rid": "1", "roomId": "1", "status": "2"},  # status 变字符串
        {"web_rid": "1", "roomId": "1", "status": 99},  # 未知状态
        {"web_rid": "1", "roomId": "1", "status": True},  # 布尔伪装
        "not-a-room",
    ],
)
def test_validate_room_rejects_missing_or_changed_fields(room):
    with pytest.raises(ContractViolation):
        validate_room(room, "1")


def test_validate_stream_url_rules():
    assert validate_stream_url(None) is None
    assert validate_stream_url("  https://cdn.example/a.flv ") == "https://cdn.example/a.flv"
    for bad in ("javascript:alert(1)", "ftp://x/a", "not-a-url", "https://x/a\nb"):
        with pytest.raises(ContractViolation):
            validate_stream_url(bad)


def test_validate_parse_detail_never_accepts_unknown_success():
    with pytest.raises(ContractViolation):
        validate_parse_detail(ParseDetail(ParseResult.SUCCESS, None), "1")
    with pytest.raises(ContractViolation):
        validate_parse_detail(
            ParseDetail(ParseResult.SUCCESS, 2, "file:///etc/passwd"), "1"
        )
    # 非 SUCCESS 分类原样返回，不影响既有错误处理。
    detail = ParseDetail(ParseResult.RISK_CONTROL)
    assert validate_parse_detail(detail, "1") == detail


# ---------- 适配层 ----------
def test_adapter_passes_valid_detail():
    source = ScriptedParser(ParseDetail(ParseResult.SUCCESS, 2, "https://cdn/a.flv"))
    result = DouyinParseAdapter(source).fetch_and_parse_detail("1")
    assert result.result == ParseResult.SUCCESS
    assert result.status == 2
    assert result.stream_url == "https://cdn/a.flv"


def test_adapter_classifies_malformed_success():
    source = ScriptedParser(ParseDetail(ParseResult.SUCCESS, "2"))
    result = DouyinParseAdapter(source).fetch_and_parse_detail("1")
    assert result.result == ParseResult.API_MALFORMED
    assert result.status is None


def test_adapter_retries_network_error_then_succeeds():
    source = ScriptedParser(
        ParseDetail(ParseResult.NETWORK_ERROR),
        ParseDetail(ParseResult.NETWORK_ERROR),
        ParseDetail(ParseResult.SUCCESS, 2),
    )
    delays: list[float] = []
    adapter = DouyinParseAdapter(
        source,
        retry_policy=RetryPolicy(max_retries=2, delays_s=(0.5, 1.0)),
        sleep=delays.append,
    )
    result = adapter.fetch_and_parse_detail("1")
    assert result.result == ParseResult.SUCCESS
    assert source.calls == 3
    assert delays == [0.5, 1.0]


def test_adapter_stops_after_retry_budget():
    source = ScriptedParser(ParseDetail(ParseResult.NETWORK_ERROR))
    delays: list[float] = []
    adapter = DouyinParseAdapter(
        source, retry_policy=RetryPolicy(max_retries=1), sleep=delays.append
    )
    result = adapter.fetch_and_parse_detail("1")
    assert result.result == ParseResult.NETWORK_ERROR
    assert source.calls == 2
    assert delays == [0.5]


def test_adapter_does_not_retry_contract_errors():
    source = ScriptedParser(ParseDetail(ParseResult.SUCCESS, None))
    adapter = DouyinParseAdapter(source, sleep=lambda _s: None)
    assert adapter.fetch_and_parse_detail("1").result == ParseResult.API_MALFORMED
    assert source.calls == 1


def test_adapter_isolates_source_exceptions():
    source = ScriptedParser(RuntimeError("boom"))
    result = DouyinParseAdapter(source).fetch_and_parse_detail("1")
    assert result.result == ParseResult.UNKNOWN_ERROR


def test_circuit_breaker_opens_and_recovers():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=2, cooldown_s=10.0, clock=lambda: now[0]
    )
    source = ScriptedParser(ParseDetail(ParseResult.SUCCESS, None))
    adapter = DouyinParseAdapter(source, circuit_breaker=breaker, sleep=lambda _s: None)

    assert adapter.fetch_and_parse_detail("1").result == ParseResult.API_MALFORMED
    assert adapter.fetch_and_parse_detail("1").result == ParseResult.API_MALFORMED
    assert breaker.is_open("1")
    calls_before = source.calls
    assert adapter.fetch_and_parse_detail("1").result == ParseResult.API_MALFORMED
    assert source.calls == calls_before  # 冷却期内不再调用数据源

    now[0] = 11.0
    assert breaker.is_open("1") is False
    assert adapter.fetch_and_parse_detail("1").result == ParseResult.API_MALFORMED
    assert source.calls == calls_before + 1


def test_circuit_breaker_is_per_room():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=60.0)
    breaker.record_failure("bad")
    assert breaker.is_open("bad") is True
    assert breaker.is_open("good") is False
    breaker.record_success("good")
    assert breaker.failure_count("good") == 0


# ---------- 模拟接口结构变化 ----------
def test_page_structure_change_without_status_is_not_success():
    client = FakeHttpClient(HttpResponse(200, _page_payload(include_status=False)))
    result = _adapter(client).fetch_and_parse_detail("123456")
    assert result.result == ParseResult.API_MALFORMED


def test_page_structure_change_without_room_wrapper_is_not_success():
    client = FakeHttpClient(
        HttpResponse(200, _page_payload(room_key="roomInfo", status=2))
    )
    result = _adapter(client).fetch_and_parse_detail("123456")
    assert result.result == ParseResult.API_MALFORMED


def test_page_status_type_change_is_not_success():
    client = FakeHttpClient(HttpResponse(200, _page_payload(status="2")))
    result = _adapter(client).fetch_and_parse_detail("123456")
    assert result.result == ParseResult.API_MALFORMED


def test_page_room_id_type_change_is_not_success():
    client = FakeHttpClient(
        HttpResponse(200, _page_payload(room_id={"nested": "1"}))
    )
    result = _adapter(client).fetch_and_parse_detail("123456")
    assert result.result == ParseResult.API_MALFORMED


def test_valid_offline_page_is_success_status_4():
    client = FakeHttpClient(HttpResponse(200, _page_payload(status=4)))
    result = _adapter(client).fetch_and_parse_detail("123456")
    assert result.result == ParseResult.SUCCESS
    assert result.status == 4
    assert result.stream_url is None


def test_risk_control_page_is_classified_as_risk_control():
    client = FakeHttpClient(
        HttpResponse(200, "<html>验证码中间页 sec_sdk_build</html>".encode("utf-8"))
    )
    result = _adapter(client).fetch_and_parse_detail("123456")
    assert result.result == ParseResult.RISK_CONTROL


def test_unknown_page_is_malformed_not_success():
    client = FakeHttpClient(HttpResponse(200, b"<html>totally different</html>"))
    result = _adapter(client).fetch_and_parse_detail("123456")
    assert result.result == ParseResult.API_MALFORMED


def test_empty_and_http_error_are_preserved():
    empty = _adapter(FakeHttpClient(HttpResponse(200, b"")))
    assert empty.fetch_and_parse_detail("123456").result == ParseResult.EMPTY_RESPONSE
    failure = _adapter(FakeHttpClient(HttpResponse(500, b"x")))
    assert failure.fetch_and_parse_detail("123456").result == ParseResult.HTTP_ERROR


def test_network_failure_is_retried_and_classified():
    class NetworkClient:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, *, headers=None, timeout=None):
            self.calls += 1
            raise TimeoutError("timeout")

    client = NetworkClient()
    delays: list[float] = []
    adapter = DouyinParseAdapter(
        RealDouyinParser(client),
        retry_policy=RetryPolicy(max_retries=1, delays_s=(0.1,)),
        sleep=delays.append,
    )
    result = adapter.fetch_and_parse_detail("123456")
    assert result.result == ParseResult.NETWORK_ERROR
    assert client.calls == 2
    assert delays == [0.1]
