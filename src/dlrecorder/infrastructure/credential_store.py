"""Windows 凭据安全存储（DPAPI）。

抖音 Cookie 使用当前 Windows 用户的 DPAPI 加密后写入
``data/runtime/douyin_cookie.bin``：

- 密文只能由同一 Windows 用户解密，复制到其他账户/机器无法使用；
- 文件内容不含明文 Cookie，应用日志与设置表也不会出现 Cookie；
- 非 Windows 平台不支持安全存储，保存会抛出 ``RuntimeError``，读取返回空。

Cookie 仅用于解析请求，不会写入数据库、事件日志或发布包。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_ENTROPY = b"dlr-douyin-cookie-v1"
_MAGIC = b"DLR1"

if _IS_WINDOWS:  # pragma: no cover - 仅在 Windows 上导入
    import ctypes
    from ctypes import wintypes

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def _blob(data: bytes) -> "_DATA_BLOB":
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _protect(data: bytes) -> bytes:
    if not _IS_WINDOWS:
        raise RuntimeError("当前平台不支持安全凭据存储（仅支持 Windows DPAPI）")
    input_blob = _blob(data)
    entropy_blob = _blob(_ENTROPY)
    output_blob = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 加密失败")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        _kernel32.LocalFree(output_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    if not _IS_WINDOWS:
        raise RuntimeError("当前平台不支持安全凭据存储（仅支持 Windows DPAPI）")
    input_blob = _blob(data)
    entropy_blob = _blob(_ENTROPY)
    output_blob = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 解密失败")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        _kernel32.LocalFree(output_blob.pbData)


class CredentialStore:
    """Cookie 的加密读写。任何方法都不会返回或记录明文的副本到日志。"""

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / "runtime" / "douyin_cookie.bin"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def supported(self) -> bool:
        return _IS_WINDOWS

    def has_cookie(self) -> bool:
        return self._path.is_file() and self._path.stat().st_size > len(_MAGIC)

    def save_cookie(self, cookie: str) -> None:
        text = (cookie or "").strip()
        if not text:
            raise ValueError("Cookie 不能为空")
        payload = _protect(text.encode("utf-8"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(_MAGIC + payload)
        os.replace(tmp, self._path)
        logger.info("抖音 Cookie 已加密保存（长度=%s）", len(text))

    def load_cookie(self) -> Optional[str]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("读取加密 Cookie 失败: %s", e)
            return None
        if not raw.startswith(_MAGIC):
            logger.warning("加密 Cookie 文件格式不正确，本次按未配置处理")
            return None
        try:
            return _unprotect(raw[len(_MAGIC):]).decode("utf-8")
        except (OSError, RuntimeError, UnicodeDecodeError) as e:
            logger.warning("解密 Cookie 失败（可能由其他用户或机器创建）: %s", e)
            return None

    def clear_cookie(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("删除加密 Cookie 失败: %s", e)
            raise
        logger.info("抖音 Cookie 已删除")

    def describe(self) -> str:
        """返回可展示的状态文本，不包含任何 Cookie 内容。"""
        if not self.has_cookie():
            return "未配置（匿名访问）"
        cookie = self.load_cookie()
        if cookie is None:
            return "已保存但无法解密（请重新导入）"
        return f"已配置（加密保存，{len(cookie)} 字符）"


__all__ = ["CredentialStore"]
