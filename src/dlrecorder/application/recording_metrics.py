"""实时录制指标纯计算（P1-04 一期）。

不依赖 Qt/数据库/文件系统：调用方每秒传入 ``monotonic`` 时间与文件大小。
指标仅用于展示，不改变录制状态机、重连策略或数据库字段。
"""
from __future__ import annotations

import enum
import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

TREND_POINTS = 120
SPEED_WINDOW_S = 5.0
STALL_WARN_S = 30.0
RECOVER_GRACE_S = 10.0
DEGRADE_RECENT_S = 30.0
DEGRADE_BASELINE_S = 60.0
DEGRADE_RATIO = 0.4
MIN_BASELINE_BPS = 8192.0
MAX_SAMPLE_GAP_S = 10.0


class HealthState(str, enum.Enum):
    GOOD = "good"
    RECOVERING = "recovering"
    STALLED = "stalled"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MetricsSnapshot:
    room_id: int
    status: str
    file_size: int
    segment_seconds: float
    session_bytes: int
    session_segments: int
    write_speed_bps: float
    stall_seconds: float
    health: str
    trend: tuple[int, ...]
    attempts: int = 0
    max_attempts: int = 0

    def as_dict(self) -> dict:
        return {
            "room_id": self.room_id,
            "status": self.status,
            "file_size": self.file_size,
            "segment_seconds": self.segment_seconds,
            "session_bytes": self.session_bytes,
            "session_segments": self.session_segments,
            "write_speed_bps": self.write_speed_bps,
            "stall_seconds": self.stall_seconds,
            "health": self.health,
            "trend": list(self.trend),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
        }


class SegmentMetrics:
    """单个录制分段的采样窗口。"""

    def __init__(self, *, now: float, max_points: int = TREND_POINTS) -> None:
        self._samples: deque[tuple[float, int]] = deque(maxlen=max(2, max_points))
        self._last_size = 0
        self._last_growth_at = now
        self._segment_started_at = now

    def reset_segment(self, now: float) -> None:
        self._samples.clear()
        self._last_size = 0
        self._last_growth_at = now
        self._segment_started_at = now

    def observe(self, now: float, size: int) -> None:
        size = max(0, int(size))
        if self._samples and now - self._samples[-1][0] > MAX_SAMPLE_GAP_S:
            # 休眠/时钟跳变：重置窗口，避免速度被巨大间隔摊薄。
            logger.debug("指标采样间隔异常，重置窗口 gap=%.1fs", now - self._samples[-1][0])
            self._samples.clear()
            self._last_size = size
            self._last_growth_at = now
            self._samples.append((now, size))
            return
        if size < self._last_size:
            # 正常录制中不应回退；按异常处理，重置基线，避免负速度。
            logger.debug(
                "录制文件大小回退，重置指标基线 old=%s new=%s", self._last_size, size
            )
            self._samples.clear()
            self._last_size = size
            self._last_growth_at = now
            self._samples.append((now, size))
            return
        if size > self._last_size:
            self._last_growth_at = now
        self._last_size = size
        self._samples.append((now, size))

    @property
    def last_size(self) -> int:
        return self._last_size

    def segment_seconds(self, now: float) -> float:
        return max(0.0, now - self._segment_started_at)

    def stall_seconds(self, now: float) -> float:
        return max(0.0, now - self._last_growth_at)

    def write_speed_bps(self, now: float, window_s: float = SPEED_WINDOW_S) -> float:
        if len(self._samples) < 2:
            return 0.0
        latest_t, latest_size = self._samples[-1]
        base_t, base_size = None, None
        for sample_t, sample_size in self._samples:
            if sample_t >= now - window_s:
                base_t, base_size = sample_t, sample_size
                break
        if base_t is None or base_size is None:
            return 0.0
        elapsed = latest_t - base_t
        if elapsed <= 0:
            return 0.0
        return max(0.0, (latest_size - base_size) / elapsed)

    def trend(self) -> tuple[int, ...]:
        """每个采样点的正增量序列（最多 120 点）。"""
        values: list[int] = []
        previous: Optional[int] = None
        for _timestamp, size in self._samples:
            if previous is not None:
                values.append(max(0, size - previous))
            previous = size
        return tuple(values[-TREND_POINTS:])

    def health(
        self,
        now: float,
        *,
        reconnecting: bool = False,
        failed: bool = False,
    ) -> HealthState:
        if failed:
            return HealthState.FAILED
        if reconnecting:
            return HealthState.RECONNECTING
        stall = self.stall_seconds(now)
        if stall >= STALL_WARN_S:
            return HealthState.STALLED
        if now - self._segment_started_at < RECOVER_GRACE_S:
            return HealthState.RECOVERING
        if self._is_degraded(now):
            return HealthState.DEGRADED
        return HealthState.GOOD

    def _average_bps(self, now: float, window_s: float) -> Optional[float]:
        start = now - window_s
        previous: Optional[int] = None
        delta = 0
        counted = 0
        for sample_t, size in self._samples:
            if sample_t < start:
                previous = size
                continue
            if previous is not None:
                delta += max(0, size - previous)
                counted += 1
            previous = size
        if counted == 0:
            return None
        return delta / window_s

    def _is_degraded(self, now: float) -> bool:
        if self.segment_seconds(now) < DEGRADE_RECENT_S + DEGRADE_BASELINE_S:
            return False
        recent_avg = self._average_bps(now, DEGRADE_RECENT_S)
        baseline_samples = [
            (timestamp, size)
            for timestamp, size in self._samples
            if now - DEGRADE_RECENT_S - DEGRADE_BASELINE_S
            <= timestamp
            < now - DEGRADE_RECENT_S
        ]
        if recent_avg is None or len(baseline_samples) < 2:
            return False
        delta = 0
        for index in range(1, len(baseline_samples)):
            delta += max(0, baseline_samples[index][1] - baseline_samples[index - 1][1])
        baseline_avg = delta / DEGRADE_BASELINE_S
        if baseline_avg < MIN_BASELINE_BPS:
            return False
        return recent_avg < baseline_avg * DEGRADE_RATIO

    def snapshot(
        self,
        now: float,
        *,
        room_id: int,
        status: str,
        session_bytes: int,
        session_segments: int,
        attempts: int = 0,
        max_attempts: int = 0,
        failed: bool = False,
    ) -> MetricsSnapshot:
        reconnecting = status == "reconnecting"
        health = self.health(now, reconnecting=reconnecting, failed=failed)
        # 重连等待期间冻结时长/停滞指标，不向趋势补 0。
        effective_now = self._samples[-1][0] if reconnecting and self._samples else now
        return MetricsSnapshot(
            room_id=room_id,
            status=status,
            file_size=self._last_size,
            segment_seconds=self.segment_seconds(effective_now),
            session_bytes=session_bytes,
            session_segments=session_segments,
            write_speed_bps=0.0 if reconnecting else self.write_speed_bps(now),
            stall_seconds=self.stall_seconds(effective_now),
            health=str(health),
            trend=self.trend(),
            attempts=attempts,
            max_attempts=max_attempts,
        )
