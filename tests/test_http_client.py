"""HTTP 客户端传输错误分类测试（离线，不发起网络请求）。"""
from __future__ import annotations

import http.client
import io
import socket
import ssl
import urllib.error

import pytest

from dlrecorder.infrastructure.http_client import (
    HttpErrorKind,
    HttpResponse,
    UrllibHttpClient,
)


class _FakeResponse:
    def __init__(self, status=200, body=b"ok", content_type="text/plain"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeOpener:
    def __init__(self, result):
        self._result = result
        self.request = None
        self.timeout = None

    def open(self, req, timeout=None):
        self.request = req
        self.timeout = timeout
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _client(result) -> tuple[UrllibHttpClient, _FakeOpener]:
    opener = _FakeOpener(result)
    return UrllibHttpClient(opener=opener), opener


def test_get_success_returns_status_body_and_content_type():
    client, opener = _client(_FakeResponse(status=200, body=b"hello"))

    resp = client.get(
        "https://example.invalid/path",
        headers={"User-Agent": "test-agent"},
        timeout=3.5,
    )

    assert resp.status == 200
    assert resp.body == b"hello"
    assert resp.content_type == "text/plain"
    assert resp.is_transport_error is False
    assert resp.error_kind is None
    assert opener.timeout == 3.5
    assert opener.request.get_header("User-agent") == "test-agent"


def test_http_error_keeps_status_and_body():
    error = urllib.error.HTTPError(
        "https://example.invalid/path",
        403,
        "Forbidden",
        {"Content-Type": "text/html"},
        io.BytesIO(b"denied"),
    )
    client, _ = _client(error)

    resp = client.get("https://example.invalid/path")

    assert resp.status == 403
    assert resp.body == b"denied"
    assert resp.content_type == "text/html"
    assert resp.is_transport_error is False


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (socket.timeout("timed out"), HttpErrorKind.TIMEOUT),
        (TimeoutError("timed out"), HttpErrorKind.TIMEOUT),
        (socket.gaierror(11001, "getaddrinfo failed"), HttpErrorKind.DNS),
        (ssl.SSLError("certificate verify failed"), HttpErrorKind.SSL),
        (ConnectionRefusedError("refused"), HttpErrorKind.CONNECTION),
        (ConnectionResetError("reset"), HttpErrorKind.CONNECTION),
        (OSError("broken"), HttpErrorKind.NETWORK),
        (RuntimeError("unexpected"), HttpErrorKind.UNKNOWN),
    ],
)
def test_url_error_reason_is_classified(reason, expected):
    client, _ = _client(urllib.error.URLError(reason))

    resp = client.get("https://example.invalid/path")

    assert resp.status is None
    assert resp.body == b""
    assert resp.is_transport_error is True
    assert resp.error_kind == expected
    assert resp.error_message


def test_direct_timeout_is_classified():
    client, _ = _client(TimeoutError("timed out"))

    resp = client.get("https://example.invalid/path")

    assert resp.error_kind == HttpErrorKind.TIMEOUT


def test_protocol_error_is_classified():
    client, _ = _client(http.client.HTTPException("bad status line"))

    resp = client.get("https://example.invalid/path")

    assert resp.error_kind == HttpErrorKind.PROTOCOL


def test_invalid_url_is_classified():
    client, _ = _client(ValueError("unknown url type: xyz"))

    resp = client.get("xyz://example.invalid/path")

    assert resp.error_kind == HttpErrorKind.INVALID_URL


def test_connection_reset_without_url_error_is_classified():
    client, _ = _client(ConnectionResetError("reset by peer"))

    resp = client.get("https://example.invalid/path")

    assert resp.error_kind == HttpErrorKind.CONNECTION


def test_unexpected_exception_propagates():
    client, _ = _client(KeyError("programming bug"))

    with pytest.raises(KeyError):
        client.get("https://example.invalid/path")


def test_response_defaults_have_no_transport_error():
    resp = HttpResponse(status=200, body=b"{}")

    assert resp.is_transport_error is False
    assert resp.error_kind is None
    assert resp.error_message == ""
