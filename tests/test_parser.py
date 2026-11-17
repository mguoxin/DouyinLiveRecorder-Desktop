"""解析器测试：MockParser 与 RealDouyinParser 的离线请求/响应映射。"""
from __future__ import annotations

import json
from typing import Optional

import pytest

from dlrecorder.domain.states import ParseResult
from dlrecorder.douyin.parser import (
    MockParser,
    RealDouyinParser,
    classify_http_response,
)
from dlrecorder.infrastructure.http_client import (
    HttpClient,
    HttpErrorKind,
    HttpResponse,
)


# ---------- MockParser ----------
def test_default_response():
    p = MockParser()
    result, status = p.fetch_and_parse("any")
    assert result == ParseResult.SUCCESS
    assert status == 2


def test_custom_response():
    p = MockParser()
    p.set_response("123", ParseResult.HTTP_ERROR, None)
    result, status = p.fetch_and_parse("123")
    assert result == ParseResult.HTTP_ERROR
    assert status is None
    # 未配置的房间走默认
    result2, status2 = p.fetch_and_parse("456")
    assert result2 == ParseResult.SUCCESS


def test_set_default():
    p = MockParser()
    p.set_default(ParseResult.NETWORK_ERROR, None)
    result, status = p.fetch_and_parse("any")
    assert result == ParseResult.NETWORK_ERROR


# ---------- RealDouyinParser（离线） ----------
class _FakeHttpClient(HttpClient):
    """记录调用并可返回预设响应或抛出预设异常。"""

    def __init__(
        self,
        response: Optional[HttpResponse] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def get(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        self.calls.append(
            {"url": url, "headers": dict(headers or {}), "timeout": timeout}
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _payload(status: int = 2, stream_url: Optional[str] = None) -> bytes:
    room: dict = {"status": status}
    if stream_url is not None:
        room["stream_url"] = stream_url
    return json.dumps({"data": {"data": [room]}}).encode("utf-8")


def _page_payload(
    status: int = 2,
    stream_url: object = None,
    *,
    web_rid: str = "123456",
    web_stream_url: object = None,
) -> bytes:
    """构造与真实页面一致的 T 文本记录 + 连续 Flight 记录。"""
    room: dict = {"id_str": "7700000000000000000", "status": status, "title": "测试"}
    if stream_url is not None:
        room["stream_url"] = stream_url
    wrapper: dict = {
        "roomId": "7700000000000000000",
        "web_rid": web_rid,
        "room": room,
    }
    if web_stream_url is not None:
        wrapper["web_stream_url"] = web_stream_url
    first = json.dumps({"common": {"ts": "1"}}, separators=(",", ":"))
    second = json.dumps([{"detail": wrapper}], separators=(",", ":"))
    flight = f"13:T{len(first):x},{first}14:{second}\n"
    midpoint = len(flight) // 2
    scripts = "".join(
        f"<script>self.__pace_f.push([1,{json.dumps(chunk)}])</script>"
        for chunk in (flight[:midpoint], flight[midpoint:])
    )
    return f"<html><body>{scripts}</body></html>".encode("utf-8")


def _parser(client: HttpClient, *, timeout_s: float = 10.0) -> RealDouyinParser:
    return RealDouyinParser(client, timeout_s=timeout_s)


def test_real_parser_live_success():
    client = _FakeHttpClient(
        HttpResponse(200, _page_payload(2, "https://cdn.example/live.flv"))
    )

    result, status = _parser(client).fetch_and_parse("123456")

    assert (result, status) == (ParseResult.SUCCESS, 2)
    call = client.calls[0]
    assert call["url"] == "https://www.douyin.com/root/live/123456"
    assert call["headers"]["Referer"] == "https://www.douyin.com/"
    assert call["headers"]["User-Agent"]
    assert call["timeout"] == 10.0


def test_real_parser_preserves_unknown_status():
    client = _FakeHttpClient(HttpResponse(200, _page_payload(4)))

    result, status = _parser(client).fetch_and_parse("123456")

    # 未知 status 交给 RoomState 判定，解析器不得擅自映射为下播。
    assert (result, status) == (ParseResult.SUCCESS, 4)


def test_real_parser_empty_body():
    client = _FakeHttpClient(HttpResponse(200, b""))

    assert _parser(client).fetch_and_parse("1") == (ParseResult.EMPTY_RESPONSE, None)


def test_real_parser_malformed_json():
    client = _FakeHttpClient(HttpResponse(200, b"{not-json"))

    assert _parser(client).fetch_and_parse("1") == (ParseResult.API_MALFORMED, None)


def test_real_parser_missing_data_key():
    client = _FakeHttpClient(HttpResponse(200, b'{"status_code": 0}'))

    assert _parser(client).fetch_and_parse("1") == (ParseResult.API_MALFORMED, None)


def test_real_parser_empty_room_list_is_malformed():
    client = _FakeHttpClient(HttpResponse(200, b'{"data": {"data": []}}'))

    assert _parser(client).fetch_and_parse("1") == (ParseResult.API_MALFORMED, None)


def test_real_parser_http_error_status():
    client = _FakeHttpClient(HttpResponse(429, b"rate limited"))

    assert _parser(client).fetch_and_parse("1") == (ParseResult.HTTP_ERROR, None)


def test_real_parser_transport_error_response():
    client = _FakeHttpClient(
        HttpResponse(
            None,
            b"",
            error_kind=HttpErrorKind.TIMEOUT,
            error_message="timed out",
        )
    )

    assert _parser(client).fetch_and_parse("1") == (ParseResult.NETWORK_ERROR, None)


def test_real_parser_timeout_exception():
    client = _FakeHttpClient(error=TimeoutError("timed out"))

    assert _parser(client).fetch_and_parse("1") == (ParseResult.NETWORK_ERROR, None)


def test_real_parser_oserror_exception():
    client = _FakeHttpClient(error=ConnectionResetError("reset by peer"))

    assert _parser(client).fetch_and_parse("1") == (ParseResult.NETWORK_ERROR, None)


def test_real_parser_unexpected_exception_propagates():
    client = _FakeHttpClient(error=RuntimeError("programming bug"))

    with pytest.raises(RuntimeError):
        _parser(client).fetch_and_parse("1")


def test_real_parser_rejects_empty_web_rid_without_request():
    client = _FakeHttpClient(HttpResponse(200, _page_payload(2)))

    with pytest.raises(ValueError):
        _parser(client).fetch_and_parse("   ")

    assert client.calls == []


def test_real_parser_url_encodes_web_rid():
    client = _FakeHttpClient(HttpResponse(200, _page_payload(2)))

    _parser(client).fetch_and_parse("a b&c")

    assert client.calls[0]["url"].endswith("/a%20b%26c")


def test_real_parser_passes_custom_timeout():
    client = _FakeHttpClient(HttpResponse(200, _page_payload(2)))

    _parser(client, timeout_s=2.5).fetch_and_parse("1")

    assert client.calls[0]["timeout"] == 2.5


def test_real_parser_rejects_non_positive_timeout():
    with pytest.raises(ValueError):
        RealDouyinParser(_FakeHttpClient(), timeout_s=0)


def test_classify_http_response_exposes_stream_flag():
    result, status, has_stream = classify_http_response(
        HttpResponse(200, _payload(2, "https://cdn.example/live.flv"))
    )

    assert (result, status, has_stream) == (ParseResult.SUCCESS, 2, True)


# ---------- 流地址提取 ----------
def test_detail_returns_stream_url_for_string_field():
    client = _FakeHttpClient(
        HttpResponse(
            200,
            _page_payload(2, "https://cdn.example/live.flv", web_rid="1"),
        )
    )

    detail = _parser(client).fetch_and_parse_detail("1")

    assert detail.result == ParseResult.SUCCESS
    assert detail.status == 2
    assert detail.stream_url == "https://cdn.example/live.flv"


def test_detail_extracts_flv_pull_url_preferred_quality():
    body = _page_payload(
        2,
        {
            "flv_pull_url": {
                "SD1": "https://cdn.example/sd.flv",
                "FULL_HD1": "https://cdn.example/full.flv",
            }
        },
        web_rid="1",
    )
    client = _FakeHttpClient(HttpResponse(200, body))

    detail = _parser(client).fetch_and_parse_detail("1")

    assert detail.stream_url == "https://cdn.example/full.flv"


def test_detail_falls_back_to_hls_map():
    body = _page_payload(
        2,
        {"hls_pull_url_map": {"HD1": "https://cdn.example/hd.m3u8"}},
        web_rid="1",
    )
    client = _FakeHttpClient(HttpResponse(200, body))

    assert (
        _parser(client).fetch_and_parse_detail("1").stream_url
        == "https://cdn.example/hd.m3u8"
    )


def test_detail_falls_back_to_wrapper_web_stream_url():
    body = _page_payload(
        2,
        web_rid="1",
        web_stream_url={"hls_pull_url": "https://cdn.example/web.m3u8"},
    )
    client = _FakeHttpClient(HttpResponse(200, body))

    detail = _parser(client).fetch_and_parse_detail("1")

    assert detail.stream_url == "https://cdn.example/web.m3u8"


def test_real_parser_rejects_mismatched_room():
    client = _FakeHttpClient(HttpResponse(200, _page_payload(2, web_rid="other")))

    assert _parser(client).fetch_and_parse("123456") == (
        ParseResult.API_MALFORMED,
        None,
    )


def test_detail_has_no_stream_url_on_http_error():
    client = _FakeHttpClient(
        HttpResponse(403, _payload(2, "https://cdn.example/live.flv"))
    )

    detail = _parser(client).fetch_and_parse_detail("1")

    assert detail.result == ParseResult.HTTP_ERROR
    assert detail.stream_url is None


def test_detail_has_no_stream_url_for_unknown_shape():
    body = _page_payload(2, {}, web_rid="1")
    client = _FakeHttpClient(HttpResponse(200, body))

    assert _parser(client).fetch_and_parse_detail("1").stream_url is None


def test_mock_parser_detail_carries_stream_url():
    parser = MockParser()
    parser.set_response(
        "42", ParseResult.SUCCESS, 2, stream_url="rtmp://example.invalid/live"
    )

    detail = parser.fetch_and_parse_detail("42")

    assert detail.result == ParseResult.SUCCESS
    assert detail.status == 2
    assert detail.stream_url == "rtmp://example.invalid/live"
    # 旧接口保持兼容
    assert parser.fetch_and_parse("42") == (ParseResult.SUCCESS, 2)
