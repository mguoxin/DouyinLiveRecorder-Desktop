"""解析适配层：契约校验、错误分类、有界重试与故障隔离。

包装任意既有 ``DouyinParser``，不改变其实现；监控循环只看到已分类的
``ParseDetail``。未知/畸变响应一律 ``API_MALFORMED``，绝不返回 SUCCESS。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..domain.states import ParseResult
from .contracts import ContractViolation, validate_parse_detail
from .parser import DouyinParser, ParseDetail
from .resilience import CircuitBreaker, RetryPolicy

logger = logging.getLogger(__name__)


class DouyinParseAdapter(DouyinParser):
    """在解析器外层执行契约与弹性策略。"""

    def __init__(
        self,
        source: DouyinParser,
        *,
        retry_policy: Optional[RetryPolicy] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        sleep: Callable[[float], None] = time.sleep,
        enforce_stream_url: bool = True,
    ) -> None:
        self._source = source
        self._retry = retry_policy or RetryPolicy()
        self._breaker = circuit_breaker or CircuitBreaker()
        self._sleep = sleep
        # 仅本地合成流测试可关闭；生产解析必须校验流地址协议。
        self._enforce_stream_url = enforce_stream_url

    @property
    def source(self) -> DouyinParser:
        return self._source

    def __getattr__(self, name: str):
        """兼容既有扩展方法（set_quality/set_cookie/set_response 等）。"""
        source = self.__dict__.get("_source")
        if source is None:
            raise AttributeError(name)
        return getattr(source, name)

    def fetch_and_parse(self, web_rid: str) -> tuple[ParseResult, Optional[int]]:
        detail = self.fetch_and_parse_detail(web_rid)
        return detail.result, detail.status

    def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
        rid = (web_rid or "").strip()
        if not rid:
            raise ValueError("web_rid 不能为空")

        if self._breaker.is_open(rid):
            logger.warning("解析熔断中，快速返回 API_MALFORMED web_rid=%s", rid)
            return ParseDetail(ParseResult.API_MALFORMED)

        attempt = 0
        while True:
            try:
                detail = self._source.fetch_and_parse_detail(rid)
            except Exception:  # noqa: BLE001 - 单房间异常不得阻塞监控循环
                logger.exception("解析器异常，已隔离 web_rid=%s", rid)
                return ParseDetail(ParseResult.UNKNOWN_ERROR)

            if (
                detail is not None
                and detail.result == ParseResult.NETWORK_ERROR
                and self._retry.allows(attempt)
            ):
                delay = self._retry.delay(attempt)
                attempt += 1
                logger.info(
                    "解析网络错误，%.1fs 后重试 web_rid=%s attempt=%s",
                    delay,
                    rid,
                    attempt,
                )
                if delay > 0:
                    self._sleep(delay)
                continue
            break

        try:
            validated = validate_parse_detail(
                detail, rid, enforce_stream_url=self._enforce_stream_url
            )
        except ContractViolation as exc:
            self._breaker.record_failure(rid)
            logger.warning(
                "解析响应不符合契约，按 API_MALFORMED 处理 web_rid=%s %s",
                rid,
                exc,
            )
            return ParseDetail(ParseResult.API_MALFORMED)

        if validated.result == ParseResult.SUCCESS:
            self._breaker.record_success(rid)
        return validated
