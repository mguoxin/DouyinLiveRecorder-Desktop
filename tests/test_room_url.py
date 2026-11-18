"""直播间链接解析回归：覆盖 live.douyin.com 与 www.douyin.com 的常见格式。"""
from __future__ import annotations

import pytest

from dlrecorder.domain.models import validate_web_rid

# 用户验收时上报的真实链接（固定回归样本）：路径中的房间号必须优先于 anchor_id。
REPORTED_LINK = (
    "https://www.douyin.com/follow/live/805329551023?anchor_id=4352283890695387"
)


def test_standard_live_url():
    assert validate_web_rid("https://live.douyin.com/95694179922") == "95694179922"
    assert validate_web_rid("live.douyin.com/95694179922") == "95694179922"


def test_reported_follow_live_url_prefers_path_rid():
    assert validate_web_rid(REPORTED_LINK) == "805329551023"


def test_follow_live_url_without_query():
    assert validate_web_rid("https://www.douyin.com/follow/live/805329551023") == "805329551023"


def test_anchor_id_is_never_used_as_room_id():
    rid = validate_web_rid(REPORTED_LINK)
    assert rid != "4352283890695387"


def test_multiple_query_params_and_trailing_slash():
    link = (
        "https://www.douyin.com/follow/live/805329551023/"
        "?anchor_id=4352283890695387&enter_from=web_share&from=web&"
        "live_web_rid=1477297997"
    )
    assert validate_web_rid(link) == "805329551023"


def test_existing_root_and_plain_live_paths_still_work():
    assert validate_web_rid("https://www.douyin.com/root/live/95694179922") == "95694179922"
    assert validate_web_rid("https://www.douyin.com/live/95694179922") == "95694179922"
    assert validate_web_rid("https://live.douyin.com/111?live_web_rid=222") == "111"


def test_query_param_only_link_still_works():
    link = (
        "https://live.douyin.com/?activity_name=&anchor_id=1483686520573257"
        "&live_web_rid=1477297997&page_type=live_main_page"
    )
    assert validate_web_rid(link) == "1477297997"


def test_share_text_with_follow_live_url():
    text = f"7- 复制打开抖音，看看直播间 {REPORTED_LINK} 复制此链接"
    assert validate_web_rid(text) == "805329551023"


def test_invalid_url_without_rid():
    with pytest.raises(ValueError, match="链接路径中没有有效的直播间号"):
        validate_web_rid("https://www.douyin.com/follow/live/")
    with pytest.raises(ValueError, match="链接路径中没有有效的直播间号"):
        validate_web_rid("https://live.douyin.com/anchor/123")


def test_unsupported_domain_is_reported():
    with pytest.raises(ValueError, match="不支持的域名 example.com"):
        validate_web_rid("https://example.com/follow/live/805329551023")
    with pytest.raises(ValueError, match="无法从链接中识别直播间 ID"):
        validate_web_rid("https://example.com/?live_web_rid=1477297997")


def test_domain_lookalike_is_not_accepted():
    """相似域名（fakedouyin.com）不得因包含 douyin.com 子串而被误识别。"""
    with pytest.raises(ValueError, match="不支持的域名 fakedouyin.com"):
        validate_web_rid("https://fakedouyin.com/follow/live/805329551023")


def test_short_link_hint_is_preserved():
    with pytest.raises(ValueError, match="v.douyin.com"):
        validate_web_rid("https://v.douyin.com/iRNBho6u/")


def test_plain_id_and_invalid_id_are_unchanged():
    assert validate_web_rid("  805329551023  ") == "805329551023"
    with pytest.raises(ValueError, match="只能包含字母、数字"):
        validate_web_rid("bad rid!")


def test_user_page_is_not_mistaken_for_live():
    with pytest.raises(ValueError, match="链接路径中没有有效的直播间号"):
        validate_web_rid("https://www.douyin.com/user/MS4wLjABAAAA123")
