"""统一敏感信息脱敏：日志输出与诊断导出共用同一套规则。

覆盖范围：
- Windows 绝对路径（含中文用户名、空格、分号等合法字符）：
  * 位于用户目录下的路径 → ``%USERPROFILE%\\...``，保留目录结构便于诊断；
  * 用户目录内与用户名同名的路径段 → ``<user>``（仅在确认属于用户目录时处理，
    不触碰其它盘符/其它目录下的同名目录名）；
  * 其它绝对路径 → ``<path>``。
- URL（含签名参数）、Cookie/Authorization 请求头、常见 Token 参数。
  键名支持百分号编码（如 ``to%6Ben``）与全角等号/冒号（``＝``、``：``）。
- 邮箱、IPv4。
- 诊断导出按字段语义掩码直播间号/账号标识（``room``、``web_rid``、``uid`` 等），
  仅对 15 位以上、无语义标签的数字做兜底；文件大小、时间戳等普通数字保持原样。
  日志（``redact_log_text``）保留房间号与普通数字以便定位问题。

设计原则：脱敏失败绝不影响主流程；函数幂等，可被日志与诊断重复调用。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import unquote

PLACEHOLDER = "<redacted>"
USER_PLACEHOLDER = "<user>"
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
# 键值对：仅在键名确实含百分号编码片段时才进入分类，
# 避免非敏感 ``key: value`` 吞掉后面的敏感键值对。
PCT_SECRET_RE = re.compile(
    r"(?i)(?=[A-Za-z0-9_.%\-]{0,62}%[0-9A-Fa-f]{2})"
    r"(?P<key>[A-Za-z0-9_.%\-]{2,64})"
    r"(?P<sep>\s*[=:：＝]\s*)"
    r"(?P<val>[^\s&,;\"'<>\r\n]+)"
)
# 全角等号/冒号：仅在分隔符为全角时匹配，半角由 SECRET_RE 负责。
FW_SECRET_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.\-]{2,64})"
    r"(?P<sep>\s*[：＝]\s*)"
    r"(?P<val>[^\s&,;\"'<>\r\n]+)"
)
# 兼容既有导入：仅匹配凭据键值对，供诊断泄漏扫描复用。
SECRET_RE = re.compile(
    rf"(?i)\b({SECRET_KEYS})\b\s*[=:]\s*([^\s&,;\"']+)"
)
_SECRET_KEY_RE = re.compile(rf"(?i)^(?:{SECRET_KEYS})$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Windows 路径主体：禁止非法字符；逗号/分号是合法文件名字符，
# 仅当其后紧跟非空白时才视为路径内容，避免吞掉路径后的普通日志正文。
_WIN_PATH_BODY = r"""(?:[^\\/:*?"<>|\r\n,;，。；、）)\]】]|[,;](?=\S))*"""
WIN_PATH_RE = re.compile(rf"[A-Za-z]:[\\/]{_WIN_PATH_BODY}(?:[\\/]{_WIN_PATH_BODY})*")

# 路径结束后的合法边界字符（用于区分“更深一层的路径”与“路径后的普通正文”）。
_PATH_BOUNDARY = set(" \t，。；、）)\\]】,;")

# 敏感标识字段：按语义掩码，而不是掩码所有长数字。
_ID_LABELS = (
    r"room(?:_?id)?|room_?no|web_?rid|live_?room(?:_?id)?|"
    r"anchor_?id|author_?id|user_?id|uid|sec_?uid|open_?id|short_?id|"
    r"unique_?id|account_?id|stream_?id|sec_?id"
)
_ID_LABEL_RE = re.compile(rf"(?i)\b({_ID_LABELS})\b(\s*[=:：＝]?\s*)(\d{{6,}})")
# 无语义标签的兜底：仅 15 位以上数字（雪花 ID 量级），不误伤文件大小/时间戳。
ROOM_ID_RE = re.compile(r"\b\d{15,}\b")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ---------- 路径 ----------
def _to_backslash(value: str) -> str:
    return str(value).replace("/", "\\")


def _user_name(user_home: str) -> str:
    home = _to_backslash(user_home).rstrip("\\")
    if not home:
        return ""
    return home.rsplit("\\", 1)[-1]


def _path_remainder(child: str, parent: str) -> str | None:
    """若 child 位于 parent 之下，返回 parent 之后的剩余文本；否则返回 None。

    只有剩余文本为空、以路径分隔符开头，或以空白/常见标点开头时才算命中，
    避免把 ``...\\data2`` 误判为 ``...\\data``。
    """
    parent_n = _to_backslash(parent).rstrip("\\")
    if not parent_n:
        return None
    child_n = _to_backslash(child)
    if child_n[: len(parent_n)].lower() != parent_n.lower():
        return None
    rest = child_n[len(parent_n):]
    if rest == "" or rest.startswith("\\") or rest[0] in _PATH_BOUNDARY:
        return rest
    return None


def _mask_home_path(raw: str, user_home: str) -> str:
    """用户目录下的路径：保留结构，隐藏用户名并掩码同名路径段。"""
    home = _to_backslash(user_home).rstrip("\\")
    path = _to_backslash(raw)
    rest = path[len(home):] if path[: len(home)].lower() == home.lower() else ""
    username = _user_name(user_home)
    segments = [seg for seg in rest.split("\\") if seg]
    masked = [
        USER_PLACEHOLDER
        if username and seg.casefold() == username.casefold()
        else seg
        for seg in segments
    ]
    if not masked:
        return "%USERPROFILE%"
    return "%USERPROFILE%\\" + "\\".join(masked)


def _redact_path(match: re.Match[str], user_home: str, data_dir: str) -> str:
    raw = match.group(0)
    if data_dir:
        rest = _path_remainder(raw, data_dir)
        if rest is not None:
            return "<data>" if rest.startswith("\\") else f"<data>{rest}"
    if user_home:
        rest = _path_remainder(raw, user_home)
        if rest is not None:
            if rest == "" or rest.startswith("\\"):
                return _mask_home_path(raw, user_home)
            return f"%USERPROFILE%{rest}"
    return "<path>"


def _replace_path_variants(line: str, value: str, replacement: str) -> str:
    if not value:
        return line
    text = str(value)
    for variant in {text, text.replace("\\", "/")}:
        if variant:
            line = line.replace(variant, replacement)
    return line


# ---------- 凭据 ----------
def _normalize_key(key: str) -> str:
    """仅对键名做有界归一化（百分号解码 + NFKC），不触碰值。"""
    text = key
    if "%" in text:
        try:
            text = unquote(text, errors="strict")
        except UnicodeDecodeError:
            text = key
    return unicodedata.normalize("NFKC", text).strip().lower()


def _mask_kv(match: re.Match[str]) -> str:
    key = match.group("key")
    if not _SECRET_KEY_RE.match(_normalize_key(key)):
        return match.group(0)
    return f"{key}{match.group('sep')}{PLACEHOLDER}"


# ---------- 标识 ----------
def _mask_digits(raw: str) -> str:
    if len(raw) <= 4:
        return raw
    return f"{raw[:2]}{'*' * (len(raw) - 4)}{raw[-2:]}"


def _mask_identifiers(line: str) -> str:
    line = _ID_LABEL_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_mask_digits(m.group(3))}", line
    )
    return ROOM_ID_RE.sub(lambda m: _mask_digits(m.group(0)), line)


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
    # URL 先处理：避免其中的 ``https:`` 被 Windows 路径规则误识别。
    line = URL_RE.sub("<url>", line)
    line = WIN_PATH_RE.sub(lambda m: _redact_path(m, user_home, data_dir), line)
    line = _replace_path_variants(line, data_dir, "<data>")
    line = _replace_path_variants(line, user_home, "%USERPROFILE%")
    line = HEADER_RE.sub(lambda m: f"{m.group(1)}: {PLACEHOLDER}", line)
    line = SECRET_RE.sub(lambda m: f"{m.group(1)}={PLACEHOLDER}", line)
    line = PCT_SECRET_RE.sub(_mask_kv, line)
    line = FW_SECRET_RE.sub(_mask_kv, line)
    line = EMAIL_RE.sub("<email>", line)
    line = IPV4_RE.sub("<ip>", line)
    if mask_room_ids:
        line = _mask_identifiers(line)
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
