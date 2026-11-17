"""HTTP 客户端（标准库 urllib）。

本阶段不发起真实抖音请求；仅提供接口、基础实现与传输层错误分类，
供解析层依赖注入。
连接复用：使用单一 OpenerDirector，后续可扩展 keep-alive。

错误分类约定：
- HTTP 响应本身可读（含 4xx/5xx）时返回 :class:`HttpResponse`，``status`` 为状态码。
- 传输层失败（超时、DNS、TLS、连接、协议、URL 非法）返回 ``status=None`` 的
  :class:`HttpResponse`，并通过 ``error_kind`` / ``error_message`` 保留原因，
  不向调用方抛出这些可恢复异常。
- 未预期的异常（程序缺陷）不在此层吞掉，交由上层暴露。
"""
from __future__ import annotations

import abc
import enum
import http.client
import socket
import ssl
import urllib.error
import urllib.request
from typing import Optional


class HttpErrorKind(str, enum.Enum):
    """传输层失败分类。HTTP 状态码错误不在此列。"""

    TIMEOUT = "timeout"
    DNS = "dns"
    SSL = "ssl"
    CONNECTION = "connection"
    PROTOCOL = "protocol"
    INVALID_URL = "invalid_url"
    NETWORK = "network"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # 便于日志/测试输出
        return self.value


class HttpResponse:
    """HTTP 响应容器。

    ``status=None`` 表示网络层错误（连接失败、超时等），原因见 ``error_kind``。
    """

    def __init__(
        self,
        status: Optional[int],
        body: bytes,
        content_type: str = "",
        *,
        error_kind: Optional[HttpErrorKind] = None,
        error_message: str = "",
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.error_kind = error_kind
        self.error_message = error_message

    @property
    def is_transport_error(self) -> bool:
        return self.status is None

    def __repr__(self) -> str:
        return (
            f"HttpResponse(status={self.status!r}, body_len={len(self.body)}, "
            f"content_type={self.content_type!r}, error_kind={self.error_kind!r})"
        )


def _classify_reason(reason: object) -> HttpErrorKind:
    """按 urllib/socket 的异常类型归类传输层失败原因。"""
    # TimeoutError 是 OSError 子类，必须先于 OSError 判断。
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return HttpErrorKind.TIMEOUT
    if isinstance(reason, socket.gaierror):
        return HttpErrorKind.DNS
    if isinstance(reason, ssl.SSLError):
        return HttpErrorKind.SSL
    if isinstance(reason, ConnectionError):
        return HttpErrorKind.CONNECTION
    if isinstance(reason, http.client.HTTPException):
        return HttpErrorKind.PROTOCOL
    if isinstance(reason, OSError):
        return HttpErrorKind.NETWORK
    return HttpErrorKind.UNKNOWN


def _transport_error(
    kind: HttpErrorKind, message: object
) -> HttpResponse:
    return HttpResponse(
        status=None,
        body=b"",
        error_kind=kind,
        error_message=str(message),
    )


class HttpClient(abc.ABC):
    """HTTP 客户端抽象接口。"""

    @abc.abstractmethod
    def get(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        ...


class UrllibHttpClient(HttpClient):
    """基于 urllib 的实现。

    ``opener`` 可注入，用于离线测试；生产环境使用默认 OpenerDirector。
    """

    def __init__(self, opener: Optional[urllib.request.OpenerDirector] = None) -> None:
        self._opener = opener if opener is not None else urllib.request.build_opener()

    def get(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return HttpResponse(status=resp.status, body=body, content_type=ctype)
        except urllib.error.HTTPError as e:
            # 4xx/5xx 仍是可读的 HTTP 响应，保留响应体供解析层判断。
            body = e.read() if hasattr(e, "read") else b""
            ctype = e.headers.get("Content-Type", "") if e.headers else ""
            return HttpResponse(status=e.code, body=body, content_type=ctype)
        except urllib.error.URLError as e:
            return _transport_error(_classify_reason(e.reason), e.reason)
        except TimeoutError as e:
            return _transport_error(HttpErrorKind.TIMEOUT, e)
        except http.client.HTTPException as e:
            return _transport_error(HttpErrorKind.PROTOCOL, e)
        except ValueError as e:
            # urllib 对非法 URL（未知协议、缺少主机等）抛 ValueError。
            return _transport_error(HttpErrorKind.INVALID_URL, e)
        except OSError as e:
            return _transport_error(_classify_reason(e), e)
