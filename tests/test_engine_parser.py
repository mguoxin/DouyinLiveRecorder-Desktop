"""引擎解析器适配层测试（不访问网络）。"""
from __future__ import annotations

from dlrecorder.douyin.engine.douyin import (
    EngineError,
    RiskControlError,
    RoomSnapshot,
    RoomUnavailableError,
    StreamInfoMissingError,
    quality_index,
)
from dlrecorder.douyin.engine.parser import EngineDouyinParser
from dlrecorder.domain.states import ParseResult
from dlrecorder.douyin.parser import ParseDetail


def _live_snapshot(**kwargs) -> RoomSnapshot:
    return RoomSnapshot(
        status=2,
        anchor_name="主播",
        title="标题",
        flv_url="https://example.invalid/live.flv",
        hls_url="https://example.invalid/live.m3u8",
        quality="UHD",
        **kwargs,
    )


def test_live_snapshot_maps_to_success_with_stream_url():
    parser = EngineDouyinParser(snapshot_provider=lambda rid, **kw: _live_snapshot())
    detail = parser.fetch_and_parse_detail("123")
    assert detail.result == ParseResult.SUCCESS
    assert detail.status == 2
    assert detail.stream_url == "https://example.invalid/live.flv"


def test_offline_snapshot_has_no_stream_url():
    parser = EngineDouyinParser(
        snapshot_provider=lambda rid, **kw: RoomSnapshot(4, "主播", "标题", None, None, "UHD")
    )
    detail = parser.fetch_and_parse_detail("123")
    assert detail.result == ParseResult.SUCCESS
    assert detail.status == 4
    assert detail.stream_url is None


def test_risk_control_maps_to_risk_control_result():
    def provider(rid, **kw):
        raise RiskControlError("接口返回空响应")

    detail = EngineDouyinParser(snapshot_provider=provider).fetch_and_parse_detail("123")
    assert detail.result == ParseResult.RISK_CONTROL
    assert detail.status is None


def test_stream_missing_is_not_reported_as_live():
    def provider(rid, **kw):
        raise StreamInfoMissingError("直播中但未取得可用流地址")

    detail = EngineDouyinParser(snapshot_provider=provider).fetch_and_parse_detail("123")
    assert detail.result == ParseResult.API_MALFORMED


def test_room_unavailable_maps_to_malformed():
    def provider(rid, **kw):
        raise RoomUnavailableError("接口未返回房间数据")

    detail = EngineDouyinParser(snapshot_provider=provider).fetch_and_parse_detail("123")
    assert detail.result == ParseResult.API_MALFORMED


def test_transport_error_maps_to_network_error():
    def provider(rid, **kw):
        raise EngineError("网络请求失败: timeout")

    detail = EngineDouyinParser(snapshot_provider=provider).fetch_and_parse_detail("123")
    assert detail.result == ParseResult.NETWORK_ERROR


def test_fallback_used_when_primary_risk_control():
    class FakeFallback:
        def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
            return ParseDetail(ParseResult.SUCCESS, 2, "https://example.invalid/fallback.flv")

    def provider(rid, **kw):
        raise RiskControlError("风控")

    detail = EngineDouyinParser(
        snapshot_provider=provider, fallback=FakeFallback()
    ).fetch_and_parse_detail("123")
    assert detail.result == ParseResult.SUCCESS
    assert detail.stream_url == "https://example.invalid/fallback.flv"


def test_fallback_failure_keeps_primary_result():
    class FailingFallback:
        def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
            return ParseDetail(ParseResult.API_MALFORMED)

    def provider(rid, **kw):
        raise RiskControlError("风控")

    detail = EngineDouyinParser(
        snapshot_provider=provider, fallback=FailingFallback()
    ).fetch_and_parse_detail("123")
    assert detail.result == ParseResult.RISK_CONTROL


def test_empty_web_rid_rejected():
    parser = EngineDouyinParser(snapshot_provider=lambda rid, **kw: _live_snapshot())
    try:
        parser.fetch_and_parse_detail("  ")
    except ValueError:
        pass
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError("空 web_rid 应抛出 ValueError")


def test_quality_index_matches_upstream_mapping():
    assert quality_index("OD") == ("OD", 0)
    assert quality_index("UHD") == ("UHD", 1)
    assert quality_index("HD") == ("HD", 2)
    assert quality_index("SD") == ("SD", 3)
    assert quality_index("LD") == ("LD", 4)
    assert quality_index("") == ("OD", 0)
