"""统一敏感信息脱敏：日志输出与诊断导出共用同一套规则。

覆盖范围：
- Windows 绝对路径（含中文用户名、空格、常见中文标点边界）。
- URL（含签名参数）、Cookie/Authorization 请求头、常见 Token 参数。
- 邮箱、IPv4。
- 诊断导出额外掩码长数字直播间标识；日志保留房间号以便定位问题。

设计原则：脱敏失败绝不影响主流程；函数幂等，可被日志与诊断重复调用。
"""
from __future__ import annotations

import logging
import re

PLACEHOLDER = "<redacted>"
MAX_LINE_CHARS = 2000

SECRET_KEYS = (
    r"a_bogus|x[_-]bogus|mstoken|ms_token|sessionid(?:_ss)?|session_key|"
    r"ttwid|signature|sign|access_token|refresh_token|token|auth_key|"
    r"expire|x-expires|x-signature|authorization|cookie|password"
)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
HEADER_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization)\s*:\s*[^\r\n]*"
)
SECRET_RE = re.compile(
    rf"(?i)\b({SECRET_KEYS})\b\s*[=:]\s*([^\s&,;\"']+)"
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# 允许空格、中文、括号等合法路径字符；遇到常见分隔符或中文标点即停止，
# 避免把路径后的普通日志正文一并吞掉。
_WIN_PATH_BODY = r"""[^\\/:*?"<>|\r\n,;，。；、）)\]】]*"""
WIN_PATH_RE = re.compile(
    rf"[A-Za-z]:[\\/](?:{_WIN_PATH_BODY}[\\/])*{_WIN_PATH_BODY}"
)
ROOM_ID_RE = re.compile(r"\b\d{10,}\b")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _replace_path_variants(line: str, value: str, replacement: str) -> str:
    if not value:
        return line
    text = str(value)
    for variant in {text, text.replace("\\", "/")}:
        if variant:
            line = line.replace(variant, replacement)
    return line


def _mask_room(match: re.Match[str]) -> str:
    raw = match.group(0)
    return f"{raw[:2]}{'*' * (len(raw) - 4)}{raw[-2:]}"


def redact_text(
    text: str,
    *,
    user_home: str = "",
    data_dir: str = "",
    mask_room_ids: bool = True,
    max_chars: int | None = MAX_LINE_CHARS,
) -> str:
    """按固定顺序脱敏单行文本；结果幂等。"""
    line = ANSI_RE.sub("", text or "")
    line = _replace_path_variants(line, data_dir, "<data>")
    line = _replace_path_variants(line, user_home, "%USERPROFILE%")
    line = URL_RE.sub("<url>", line)
    line = HEADER_RE.sub(lambda m: f"{m.group(1)}: {PLACEHOLDER}", line)
    line = SECRET_RE.sub(lambda m: f"{m.group(1)}={PLACEHOLDER}", line)
    line = EMAIL_RE.sub("<email>", line)
    line = IPV4_RE.sub("<ip>", line)
    line = WIN_PATH_RE.sub("<path>", line)
    if mask_room_ids:
        line = ROOM_ID_RE.sub(_mask_room, line)
    if max_chars is not None and len(line) > max_chars:
        line = line[:max_chars] + "…<truncated>"
    return line


def redact_log_text(text: str, *, user_home: str = "", data_dir: str = "") -> str:
    """日志专用：脱敏路径与凭据，但保留房间号与长数字（文件大小、耗时）。"""
    return redact_text(
        text,
        user_home=user_home,
        data_dir=data_dir,
        mask_room_ids=False,
        max_chars=None,
    )


class RedactingFormatter(logging.Formatter):
    """在最终输出（含 traceback）上执行脱敏，覆盖文件与控制台处理器。"""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        user_home: str = "",
        data_dir: str = "",
    ) -> None:
        super().__init__(fmt, datefmt)
        self._user_home = user_home
        self._data_dir = data_dir

    def format(self, record: logging.LogRecord) -> str:
        try:
            text = super().format(record)
        except Exception:  # noqa: BLE001 - 格式化失败交给 logging 原逻辑
            return super().format(record)
        return redact_log_text(text, user_home=self._user_home, data_dir=self._data_dir)
