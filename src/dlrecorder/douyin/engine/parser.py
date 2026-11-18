"""把上游抖音引擎适配为本项目的解析器接口。"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ..parser import DouyinParser, ParseDetail
from ...domain.states import ParseResult
from .douyin import (
    DEFAULT_QUALITY,
    EngineError,
    RiskControlError,
    RoomSnapshot,
    RoomUnavailableError,
    StreamInfoMissingError,
    parse_room_snapshot,
)

logger = logging.getLogger(__name__)

SnapshotProvider = Callable[..., RoomSnapshot]
CookieProvider = Callable[[], Optional[str]]


class EngineDouyinParser(DouyinParser):
    """基于上游引擎（web enter + a_bogus）的抖音解析器。

    主链路通过 ``webcast/room/web/enter`` 接口获取小体积 JSON；失败时可回退到
    注入的旧页面解析器（第二数据源）。两类错误不混淆：

    - 风控（空响应/非 JSON）→ ``RISK_CONTROL``，监控按间隔重试；
    - 直播中却无流地址 / 房间数据缺失 → ``API_MALFORMED``，不标记为直播。

    清晰度和 Cookie 可在运行时更新（设置界面修改后立即生效，下一次解析使用）。
    """

    def __init__(
        self,
        *,
        quality: str = DEFAULT_QUALITY,
        timeout_s: float = 10.0,
        fallback: Optional[DouyinParser] = None,
        snapshot_provider: Optional[SnapshotProvider] = None,
        cookie_provider: Optional[CookieProvider] = None,
    ) -> None:
        self._quality = quality
        self._timeout_s = timeout_s
        self._fallback = fallback
        self._snapshot = snapshot_provider or parse_room_snapshot
        self._cookie_provider = cookie_provider
        self._cookie_override: Optional[str] = None
        self._cookie_overridden = False

    def set_quality(self, quality: str) -> None:
        """更新清晰度；``OD`` 不可用时引擎会自动降级到其他档位。"""
        text = (quality or "").strip().upper()
        if not text:
            raise ValueError("清晰度不能为空")
        self._quality = text

    def set_cookie(self, cookie: Optional[str]) -> None:
        """更新（或清除）Cookie；空值表示按匿名访问。"""
        text = (cookie or "").strip()
        self._cookie_override = text or None
        self._cookie_overridden = True
        if text:
            logger.info("抖音解析 Cookie 已更新（长度=%s）", len(text))
        else:
            logger.info("抖音解析已切换为匿名访问")

    def _current_cookie(self) -> Optional[str]:
        if self._cookie_overridden:
            return self._cookie_override
        if self._cookie_provider is None:
            return None
        try:
            return self._cookie_provider()
        except Exception:  # noqa: BLE001 - 凭据读取失败按匿名处理
            logger.warning("读取 Cookie 失败，本次按匿名访问", exc_info=True)
            return None

    def fetch_and_parse(self, web_rid: str) -> tuple[ParseResult, Optional[int]]:
        detail = self.fetch_and_parse_detail(web_rid)
        return detail.result, detail.status

    def fetch_and_parse_detail(self, web_rid: str) -> ParseDetail:
        rid = (web_rid or "").strip()
        if not rid:
            raise ValueError("web_rid 不能为空")

        cookie = self._current_cookie()
        try:
            snapshot = self._snapshot(
                rid,
                quality=self._quality,
                cookie=cookie,
                timeout_s=self._timeout_s,
            )
        except (RiskControlError, RoomUnavailableError, StreamInfoMissingError) as e:
            detail = self._try_fallback(rid, e)
            if detail is not None:
                return detail
            if isinstance(e, RiskControlError):
                if cookie:
                    logger.warning(
                        "抖音风控 web_rid=%s：%s；已配置 Cookie 仍被风控，"
                        "请检查 Cookie 是否失效，或在设置中清除后重试",
                        rid,
                        e,
                    )
                else:
                    logger.warning(
                        "抖音风控 web_rid=%s：%s；监控将按间隔自动重试", rid, e
                    )
                return ParseDetail(ParseResult.RISK_CONTROL)
            logger.warning("抖音解析失败 web_rid=%s：%s", rid, e)
            return ParseDetail(ParseResult.API_MALFORMED)
        except EngineError as e:
            # 传输层/HTTP 状态失败：不覆盖已确认的直播状态。
            detail = self._try_fallback(rid, e)
            if detail is not None:
                return detail
            logger.warning("抖音请求失败 web_rid=%s：%s", rid, e)
            return ParseDetail(ParseResult.NETWORK_ERROR)

        stream_url = snapshot.flv_url or snapshot.hls_url
        logger.debug(
            "抖音解析成功 web_rid=%s status=%s quality=%s 流地址=%s",
            rid,
            snapshot.status,
            snapshot.quality,
            "有" if stream_url else "无",
        )
        return ParseDetail(
            result=ParseResult.SUCCESS,
            status=snapshot.status,
            stream_url=stream_url,
        )

    def _try_fallback(self, web_rid: str, cause: Exception) -> Optional[ParseDetail]:
        """主链路失败时尝试第二数据源；只有拿到确定结果才采用。"""
        if self._fallback is None:
            return None
        try:
            detail = self._fallback.fetch_and_parse_detail(web_rid)
        except Exception:  # noqa: BLE001 - 回退失败不应掩盖主链路错误
            logger.debug("回退解析器异常 web_rid=%s", web_rid, exc_info=True)
            return None
        if detail.result == ParseResult.SUCCESS:
            logger.info("主解析失败（%s），已由页面解析回退成功 web_rid=%s", cause, web_rid)
            return detail
        return None
