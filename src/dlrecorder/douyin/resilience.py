"""解析弹性策略：有界重试与按房间隔离的熔断器。

只用于网络类瞬时错误与契约错误的快速失败，不改变监控轮询间隔。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """网络错误重试策略；``max_retries`` 为额外重试次数。"""

    max_retries: int = 2
    delays_s: tuple[float, ...] = (0.5, 1.0)

    def delay(self, retry_index: int) -> float:
        if not self.delays_s:
            return 0.0
        index = min(max(0, retry_index), len(self.delays_s) - 1)
        return max(0.0, self.delays_s[index])

    def allows(self, retry_index: int) -> bool:
        return 0 <= retry_index < max(0, self.max_retries)


class CircuitBreaker:
    """连续契约错误后短期打开，冷却期内快速返回失败。

    以房间（web_rid）为键隔离；一个房间的异常不影响其他房间。
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, key: str) -> bool:
        with self._lock:
            opened_at = self._opened_at.get(key)
            if opened_at is None:
                return False
            if self._clock() - opened_at >= self._cooldown_s:
                self._opened_at.pop(key, None)
                self._failures.pop(key, None)
                return False
            return True

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._opened_at.pop(key, None)

    def record_failure(self, key: str) -> None:
        with self._lock:
            if self._clock() - self._opened_at.get(key, float("-inf")) >= self._cooldown_s:
                self._opened_at.pop(key, None)
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self._threshold:
                self._opened_at[key] = self._clock()
                logger.warning(
                    "解析熔断打开 key=%s 连续失败=%s 冷却=%.0fs",
                    key,
                    count,
                    self._cooldown_s,
                )

    def failure_count(self, key: str) -> int:
        with self._lock:
            return self._failures.get(key, 0)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._opened_at.clear()
