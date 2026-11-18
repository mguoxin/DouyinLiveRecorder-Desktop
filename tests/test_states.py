"""状态机测试。

关键约束：
- status=2 已真实验证为 LIVE；status=4 已真实验证为 OFFLINE_CONFIRMED
  （2026-11-16 实测：直播房间 status=2 且有流地址；未开播房间 status=4 且无流地址）。
- 未知 status 不得覆盖最后确认状态。
- 解析失败不得认定为下播。
"""
import time

from dlrecorder.domain.states import ParseResult, RoomState


def _state():
    return RoomState()


# 1. LIVE -> LIVE
def test_live_to_live():
    s = _state()
    s.update(ParseResult.SUCCESS, 2)
    s.update(ParseResult.SUCCESS, 2)
    assert s.last_confirmed_room_state == "LIVE"
    assert s.current_observation == 2
    assert s.last_confirmed_state_at is not None


# 2. LIVE -> 未知 status（不得覆盖确认状态）
def test_live_to_unknown_status():
    s = _state()
    s.update(ParseResult.SUCCESS, 2)
    s.update(ParseResult.SUCCESS, 99)
    assert s.last_confirmed_room_state == "LIVE"
    assert s.current_observation == 99


# 3. LIVE -> EMPTY_RESPONSE（不得认定为下播）
def test_live_to_empty_response():
    s = _state()
    s.update(ParseResult.SUCCESS, 2)
    s.update(ParseResult.EMPTY_RESPONSE, None)
    assert s.last_confirmed_room_state == "LIVE"
    assert s.last_parse_result == ParseResult.EMPTY_RESPONSE


# 4. LIVE -> NETWORK_ERROR
def test_live_to_network_error():
    s = _state()
    s.update(ParseResult.SUCCESS, 2)
    s.update(ParseResult.NETWORK_ERROR, None)
    assert s.last_confirmed_room_state == "LIVE"
    assert s.last_parse_result == ParseResult.NETWORK_ERROR


# 5. LIVE -> HTTP_ERROR
def test_live_to_http_error():
    s = _state()
    s.update(ParseResult.SUCCESS, 2)
    s.update(ParseResult.HTTP_ERROR, None)
    assert s.last_confirmed_room_state == "LIVE"
    assert s.last_parse_result == ParseResult.HTTP_ERROR


# 6. NULL -> 未知 status
def test_null_to_unknown_status():
    s = _state()
    s.update(ParseResult.SUCCESS, 99)
    assert s.last_confirmed_room_state is None
    assert s.current_observation == 99
    assert s.last_confirmed_state_at is None


# 7. NULL -> LIVE
def test_null_to_live():
    s = _state()
    s.update(ParseResult.SUCCESS, 2)
    assert s.last_confirmed_room_state == "LIVE"
    assert s.last_confirmed_state_at is not None


# 8. LIVE -> status=4（已真实验证：下播）
def test_live_to_status_4_becomes_offline():
    s = _state()
    s.update(ParseResult.SUCCESS, 2)
    s.update(ParseResult.SUCCESS, 4)
    assert s.last_confirmed_room_state == "OFFLINE_CONFIRMED"
    assert s.current_observation == 4


# 8b. NULL -> status=4（未开播房间直接确认）
def test_null_to_status_4_offline():
    s = _state()
    s.update(ParseResult.SUCCESS, 4)
    assert s.last_confirmed_room_state == "OFFLINE_CONFIRMED"
    assert s.last_confirmed_state_at is not None


# 9. 已确认状态超过过期阈值 => state_expired=True
def test_expired_after_threshold():
    s = _state()
    s.expire_threshold_s = 1
    s.update(ParseResult.SUCCESS, 2)
    time.sleep(1.2)
    assert s.state_expired is True


# 10. last_confirmed_state_at 为 NULL => state_expired=None
def test_null_confirmed_at_state_expired_none():
    s = _state()
    assert s.state_expired is None
    # 解析成功但 status 未知，仍不设置 last_confirmed_state_at
    s.update(ParseResult.SUCCESS, 99)
    assert s.state_expired is None


# 额外：last_parse_success_at 与 last_confirmed_state_at 必须分开
def test_parse_success_at_separate_from_confirmed_at():
    s = _state()
    s.update(ParseResult.SUCCESS, 99)  # 未知 status
    assert s.last_parse_success_at is not None
    assert s.last_confirmed_state_at is None


# 额外：API_MALFORMED / ROOM_NOT_EXIST / UNKNOWN_ERROR 不覆盖确认状态
def test_errors_do_not_override_confirmed():
    for err in (
        ParseResult.API_MALFORMED,
        ParseResult.ROOM_NOT_EXIST,
        ParseResult.UNKNOWN_ERROR,
        ParseResult.RISK_CONTROL,
    ):
        s = _state()
        s.update(ParseResult.SUCCESS, 2)
        s.update(err, None)
        assert s.last_confirmed_room_state == "LIVE", f"{err} 不应覆盖 LIVE"
