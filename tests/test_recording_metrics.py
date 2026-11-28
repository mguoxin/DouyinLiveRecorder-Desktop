"""P1-04 指标纯计算测试（不依赖 Qt/磁盘/数据库）。"""
import pytest

from dlrecorder.application.recording_metrics import (
    RECOVER_GRACE_S,
    STALL_WARN_S,
    HealthState,
    SegmentMetrics,
)


def _feed(metrics: SegmentMetrics, *, start: float, seconds: int, step: int, base: int = 0):
    size = base
    for second in range(seconds + 1):
        metrics.observe(start + second, size)
        size += step


def test_write_speed_uses_five_second_window():
    metrics = SegmentMetrics(now=0.0)
    for second in range(6):
        metrics.observe(float(second), second * 1024)
    assert metrics.write_speed_bps(5.0) == pytest.approx(1024.0, rel=0.01)
    assert metrics.write_speed_bps(5.0, window_s=1.0) == pytest.approx(1024.0)


def test_burst_is_smoothed_by_window():
    metrics = SegmentMetrics(now=0.0)
    sizes = [0, 0, 0, 0, 8192, 8192]
    for second, size in enumerate(sizes):
        metrics.observe(float(second), size)
    # 5 秒窗口内总增量 8192，均值约 1638 B/s，而非单秒 8192。
    assert metrics.write_speed_bps(5.0) == pytest.approx(8192 / 5, rel=0.05)


def test_no_growth_reports_zero_speed_and_growing_stall():
    metrics = SegmentMetrics(now=0.0)
    for second in range(31):
        metrics.observe(float(second), 4096)
    assert metrics.write_speed_bps(30.0) == 0.0
    assert metrics.stall_seconds(30.0) == pytest.approx(30.0, abs=0.01)


def test_health_transitions_around_stall_threshold():
    metrics = SegmentMetrics(now=0.0)
    metrics.observe(0.0, 1024)
    metrics.observe(10.0, 2048)
    assert metrics.health(10.0 + RECOVER_GRACE_S) == HealthState.GOOD
    near = 10.0 + STALL_WARN_S - 0.1
    assert metrics.health(near) == HealthState.GOOD
    assert metrics.health(10.0 + STALL_WARN_S) == HealthState.STALLED


def test_health_recovering_within_grace_period():
    metrics = SegmentMetrics(now=0.0)
    metrics.observe(1.0, 100)
    assert metrics.health(1.0) == HealthState.RECOVERING


def test_reconnecting_overrides_health_and_freezes_time():
    metrics = SegmentMetrics(now=0.0)
    metrics.observe(1.0, 1000)
    metrics.observe(2.0, 2000)
    snapshot = metrics.snapshot(
        100.0,
        room_id=1,
        status="reconnecting",
        session_bytes=2000,
        session_segments=1,
    )
    assert snapshot.health == HealthState.RECONNECTING.value
    assert snapshot.segment_seconds == pytest.approx(2.0)
    # 指标冻结在最后一次采样，不随重连等待增长。
    assert snapshot.stall_seconds == pytest.approx(0.0)
    assert snapshot.write_speed_bps == 0.0
    assert snapshot.trend == (1000,)


def test_size_regression_resets_baseline_without_negative_speed():
    metrics = SegmentMetrics(now=0.0)
    metrics.observe(0.0, 5000)
    metrics.observe(1.0, 9000)
    metrics.observe(2.0, 1000)  # 异常回退
    metrics.observe(3.0, 2000)
    speed = metrics.write_speed_bps(3.0)
    assert speed >= 0
    assert metrics.last_size == 2000


def test_segment_reset_clears_window_and_time():
    metrics = SegmentMetrics(now=0.0)
    metrics.observe(5.0, 4096)
    metrics.reset_segment(100.0)
    assert metrics.last_size == 0
    assert metrics.segment_seconds(100.0) == 0.0
    assert metrics.trend() == ()
    metrics.observe(101.0, 512)
    assert metrics.segment_seconds(101.0) == pytest.approx(1.0)


def test_degraded_when_recent_speed_drops_below_ratio():
    metrics = SegmentMetrics(now=0.0)
    size = 0
    for second in range(101):
        metrics.observe(float(second), size)
        size += 100_000 if second < 70 else 10_000
    assert metrics.health(100.0) == HealthState.DEGRADED


def test_no_degrade_when_baseline_is_small():
    metrics = SegmentMetrics(now=0.0)
    size = 0
    for second in range(101):
        metrics.observe(float(second), size)
        size += 100 if second < 70 else 10
    assert metrics.health(100.0) == HealthState.GOOD


def test_clock_jump_resets_window():
    metrics = SegmentMetrics(now=0.0)
    metrics.observe(0.0, 1000)
    metrics.observe(100.0, 5000)
    assert metrics.write_speed_bps(100.0) == 0.0
    assert metrics.last_size == 5000


def test_trend_is_bounded_and_snapshot_is_immutable():
    metrics = SegmentMetrics(now=0.0)
    size = 0
    for second in range(200):
        metrics.observe(float(second), size)
        size += 10
    trend = metrics.trend()
    assert len(trend) <= 120
    snapshot = metrics.snapshot(
        200.0, room_id=1, status="recording", session_bytes=0, session_segments=0
    )
    assert isinstance(snapshot.trend, tuple)
    as_dict = snapshot.as_dict()
    as_dict["trend"].append(999)
    assert len(snapshot.trend) <= 120


def test_multiple_rooms_are_independent():
    first = SegmentMetrics(now=0.0)
    second = SegmentMetrics(now=0.0)
    for second_index in range(6):
        first.observe(float(second_index), second_index * 2048)
    second.observe(0.0, 100)
    second.observe(5.0, 100)
    assert first.write_speed_bps(5.0) > 0
    assert second.write_speed_bps(5.0) == 0.0
    assert first.last_size != second.last_size
