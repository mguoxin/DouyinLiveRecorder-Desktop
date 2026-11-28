"""统一敏感信息脱敏：日志输出与诊断导出共用同一套规则。

覆盖范围：
- Windows 绝对路径（含中文用户名、空格、逗号、分号等合法字符）：
  * 位于用户目录下的路径 → ``%USERPROFILE%\\...``，保留目录结构便于诊断；
  * 用户目录内与用户名同名的路径段 → ``<user>``（仅在确认属于用户目录时处理）；
  * 其它绝对路径 → ``<path>``。
  路径识别不会跨入同一行的下一个盘符路径，也不会吞并 ``key=value``、
  日期、秒级/毫秒级时间戳等结构化字段。
- URL：明文 ``http(s)://`` 与整体百分号编码形式（``https%3A%2F%2F…``）均替换为
  ``<url>``；后者只对候选片段做一次有界解码用于判别，不全局解码日志文本。
- 凭据：Cookie / Authorization 请求头，以及按字段语义识别的敏感键名
  （token、cookie、session、password、secret、api_key 等及其复合形式），
  支持百分号编码键名与全角等号/冒号。
- 邮箱、IPv4。
- 诊断导出按字段语义掩码直播间号/账号标识，仅对 15 位以上无语义数字兜底；
  文件大小、时间戳、退出码等普通数字保持原样。

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
# 整体百分号编码的 URL：仅在判别阶段有界解码，不改写其它文本。
ENCODED_URL_RE = re.compile(r"(?i)\b(?:https?|wss?)%3A%2F%2F[^\s\"'<>]*")
HEADER_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization)\s*:\s*[^\r\n]*"
)
# ``authorization=Bearer <token>`` 形式的认证方案 + 凭据（无冒号时 HEADER_RE 覆盖不到）。
AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(?P<key>authorization|proxy-authorization|proxy_authorization)"
    r"\s*(?P<sep>[=:：＝])\s*"
    r"(?:bearer|basic|digest|token|apikey)\s+[^\s&,;\"'<>]+"
)
# 兼容既有导入：仅匹配凭据键值对，供诊断泄漏扫描复用。
SECRET_RE = re.compile(
    rf"(?i)\b({SECRET_KEYS})\b\s*[=:]\s*([^\s&,;\"']+)"
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# 空格是 Windows 文件名的合法字符，但路径后常跟日志正文。仅当空格之后不是
# “下一个盘符路径”“结构化字段”“日期/时间戳”时，才把空格视为路径内容。
_PATH_CHAR = r"""[^\\/:*?"<>|\r\n\s,;，。；、）)\]】]"""
_PATH_SPACE = (
    r"[ \t]+(?![ \t]*(?:[\u4e00-\u9fff]{1,4}[ \t]+)?"
    r"(?:[A-Za-z]:[\\/]"
    r"|[A-Za-z_][\w.\-]*[ \t]*[=:：＝]"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{10,13}(?=\D|$)))"
)
# 逗号/分号是合法文件名字符；但若其后紧接下一个盘符路径或 ``key=value`` 字段，
# 则视为路径结束（支持 ``D:\a.ts,code=1`` 这种无空格写法）。
_PATH_PUNCT = r"[,;](?=\S)(?![A-Za-z]:[\\/]|[A-Za-z_][\w.\-]*[=:：＝])"
_PATH_ATOM = rf"(?:{_PATH_CHAR}|{_PATH_SPACE}|{_PATH_PUNCT})"
WIN_PATH_RE = re.compile(rf"[A-Za-z]:[\\/](?:{_PATH_ATOM}|[\\/])*")

# 路径结束后的合法边界字符（用于区分“更深一层的路径”与“路径后的普通正文”）。
_PATH_BOUNDARY = set(" \t，。；、）)\\]】,;")

# 敏感标识字段：按语义掩码，而不是掩码所有长数字。
# 支持 ``room_id`` / ``room-id`` / ``room id`` / ``roomid`` 等写法。
_ID_LABELS = (
    r"room(?:[_\-\s]?id)?|room_?no|web[_\-\s]?rid|live[_\-\s]?room(?:[_\-\s]?id)?|"
    r"anchor[_\-\s]?id|author[_\-\s]?id|user[_\-\s]?id|uid|sec[_\-\s]?uid|"
    r"open[_\-\s]?id|short[_\-\s]?id|unique[_\-\s]?id|account[_\-\s]?id|"
    r"stream[_\-\s]?id|sec[_\-\s]?id"
)
_ID_LABEL_RE = re.compile(rf"(?i)\b({_ID_LABELS})\b(\s*[=:：＝]?\s*)(\d{{6,}})")
# 无语义标签的兜底：仅 15 位以上数字（雪花 ID 量级），不误伤文件大小/时间戳。
ROOM_ID_RE = re.compile(r"\b\d{15,}\b")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 凭据字段分类：先按完整键名，再按分词判断，避免无边界匹配普通单词
# （如 tokenizer、session_count 不应被误判）。
_SECRET_EXACT = frozenset(
    {
        "a_bogus", "a-bogus", "x_bogus", "x-bogus", "mstoken", "ms_token",
        "sessionid", "session_id", "session_key", "sessionkey", "ttwid",
        "signature", "sign", "access_token", "refresh_token", "token",
        "auth_key", "expire", "x-expires", "x_signature", "x-signature",
        "authorization", "proxy-authorization", "proxy_authorization",
        "cookie", "cookie_value", "cookievalue", "cookies",
        "password", "passwd", "pwd", "api_key", "apikey", "api-key",
        "secret", "secret_key", "client_secret", "credential", "credentials",
    }
)
_SECRET_TOKENS = frozenset(
    {
        "token", "cookie", "session", "password", "passwd", "pwd",
        "secret", "authorization", "credential", "apikey",
    }
)
_METRIC_TOKENS = frozenset(
    {
        "count", "total", "size", "length", "len", "num", "ms", "duration",
        "timeout", "ratio", "percent", "pct",
    }
)
_KEY_QUALIFIERS = frozenset({"api", "access", "secret", "client", "app", "private"})

_KEY_CHARS = r"A-Za-z0-9_.%\-Ａ-Ｚａ-ｚ０-９＿．－"
_KEY_START = r"A-Za-z_%Ａ-Ｚａ-ｚ０-９"
_KEY_VALUE_RE = re.compile(
    rf"(?P<key>[{_KEY_START}][{_KEY_CHARS}]{{1,63}})"
    r"(?P<sep>[=:：＝])"
    r"(?P<val>[^\s&,;\"'<>]+)"
)
# 分隔符两侧允许空白（``token ＝ abc`` / ``token = abc``）。
SPACED_KEY_VALUE_RE = re.compile(
    rf"(?P<key>[{_KEY_START}][{_KEY_CHARS}]{{1,63}})"
    r"(?P<sep>\s*[=:：＝]\s*)"
    r"(?P<val>[^\s&,;\"'<>]+)"
)


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


# ---------- URL ----------
def _redact_encoded_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        decoded = unquote(raw, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return raw
    return "<url>" if re.match(r"(?i)^(?:https?|wss?)://", decoded) else raw


# ---------- 凭据 ----------
def _normalize_key(key: str) -> str:
    """仅对键名做有界归一化（百分号解码 / NFKC / 驼峰切分），不触碰值。"""
    encoded = "%" in key
    text = key
    if encoded:
        try:
            text = unquote(key, errors="strict")
        except (UnicodeDecodeError, ValueError):
            text = key
    text = unicodedata.normalize("NFKC", text).strip()
    # accessToken / refreshToken / sessionId → access_token / session_id。
    # 百分号编码键名不做驼峰切分，避免 ``TO%6BEN`` 解码成 ``TOkEN`` 后被误切。
    if not encoded:
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return text.lower()


def _is_sensitive_key(normalized: str) -> bool:
    if not normalized:
        return False
    if normalized in _SECRET_EXACT:
        return True
    parts = [part for part in re.split(r"[^a-z0-9]+", normalized) if part]
    if not parts:
        return False
    if parts[-1] in _METRIC_TOKENS:
        return False
    if any(part in _SECRET_TOKENS for part in parts):
        return True
    if parts[-1] == "key" and any(part in _KEY_QUALIFIERS for part in parts[:-1]):
        return True
    return False


def _mask_kv(match: re.Match[str]) -> str:
    key = match.group("key")
    if not _is_sensitive_key(_normalize_key(key)):
        return match.group(0)
    return f"{key}{match.group('sep')}{PLACEHOLDER}"


def _mask_token(match: re.Match[str]) -> str:
    """在单个空白分隔的 token 内处理所有键值对，避免跨字段吞并。"""
    return _KEY_VALUE_RE.sub(_mask_kv, match.group(0))


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
    line = ENCODED_URL_RE.sub(_redact_encoded_url, line)
    line = WIN_PATH_RE.sub(lambda m: _redact_path(m, user_home, data_dir), line)
    line = _replace_path_variants(line, data_dir, "<data>")
    line = _replace_path_variants(line, user_home, "%USERPROFILE%")
    line = HEADER_RE.sub(lambda m: f"{m.group(1)}: {PLACEHOLDER}", line)
    line = AUTH_SCHEME_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{PLACEHOLDER}", line
    )
    line = SPACED_KEY_VALUE_RE.sub(_mask_kv, line)
    line = re.sub(r"\S+", _mask_token, line)
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
    """在最终输出（含 traceback）上执行脱敏，覆盖文件、控制台与 GUI 处理器。"""

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
