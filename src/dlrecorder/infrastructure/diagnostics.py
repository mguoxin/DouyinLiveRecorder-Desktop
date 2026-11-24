"""一键诊断导出：用户主动触发生成脱敏 ZIP，不联网、不改动数据。

安全边界：
- 只读取应用日志与 FFmpeg 诊断日志的尾部，不读取 Cookie/凭据文件。
- 数据库只以只读 URI 执行 quick_check 与计数，不导出任何行内容。
- 字段白名单生成报告，随后执行脱敏与泄漏扫描；命中残留敏感模式的行直接丢弃。
- 构建失败或取消时不留下半成品文件。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_ZIP_BYTES = 1024 * 1024
MAX_APP_ERRORS_BYTES = 256 * 1024
MAX_FFMPEG_TAIL_BYTES = 128 * 1024
MAX_FFMPEG_TAIL_LINES = 200
MAX_LINE_CHARS = 2000
MAX_ERROR_LINES_REPORTED = 20

_PLACEHOLDER = "<redacted>"
_SECRET_KEYS = (
    r"a_bogus|x[_-]bogus|mstoken|ms_token|sessionid(?:_ss)?|session_key|"
    r"ttwid|signature|sign|access_token|refresh_token|token|auth_key|"
    r"expire|x-expires|x-signature|authorization|cookie|password"
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HEADER_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization)\s*:\s*[^\r\n]*"
)
_SECRET_RE = re.compile(
    rf"(?i)\b({_SECRET_KEYS})\b\s*[=:]\s*([^\s&,;\"']+)"
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+")
_ROOM_ID_RE = re.compile(r"\b\d{10,}\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ERROR_LINE_RE = re.compile(r"\[(WARNING|ERROR|CRITICAL)\]")


class DiagnosticsError(RuntimeError):
    """诊断导出失败（消息可直接展示给用户，不含敏感内容）。"""


class DiagnosticsCancelled(DiagnosticsError):
    """用户取消了诊断导出。"""


def _scan_leak(text: str, user_home: str) -> bool:
    """判断文本是否仍包含未脱敏的敏感模式。"""
    if user_home and user_home.lower() in text.lower():
        return True
    if _URL_RE.search(text):
        return True
    for match in _HEADER_RE.finditer(text):
        if not match.group(0).rstrip().endswith(_PLACEHOLDER):
            return True
    for match in _SECRET_RE.finditer(text):
        if not match.group(2).startswith("<"):
            return True
    if _EMAIL_RE.search(text):
        return True
    if _WIN_PATH_RE.search(text):
        return True
    if _IPV4_RE.search(text):
        return True
    return False


def redact_text(
    text: str,
    *,
    user_home: str = "",
    data_dir: Optional[Path] = None,
) -> str:
    """按固定顺序脱敏单行文本；结果幂等。"""
    line = _ANSI_RE.sub("", text or "")
    if data_dir is not None:
        for variant in {str(data_dir), str(data_dir).replace("\\", "/")}:
            if variant:
                line = line.replace(variant, "<data>")
    if user_home:
        for variant in {user_home, user_home.replace("\\", "/")}:
            if variant:
                line = line.replace(variant, "%USERPROFILE%")
    line = _URL_RE.sub("<url>", line)
    line = _HEADER_RE.sub(lambda m: f"{m.group(1)}: {_PLACEHOLDER}", line)
    line = _SECRET_RE.sub(lambda m: f"{m.group(1)}={_PLACEHOLDER}", line)
    line = _EMAIL_RE.sub("<email>", line)
    line = _IPV4_RE.sub("<ip>", line)
    line = _WIN_PATH_RE.sub("<path>", line)

    def _mask_room(match: re.Match[str]) -> str:
        raw = match.group(0)
        return f"{raw[:2]}{'*' * (len(raw) - 4)}{raw[-2:]}"

    line = _ROOM_ID_RE.sub(_mask_room, line)
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + "…<truncated>"
    return line


def _redact_obj(value: Any, *, user_home: str, data_dir: Path) -> Any:
    if isinstance(value, str):
        return redact_text(value, user_home=user_home, data_dir=data_dir)
    if isinstance(value, dict):
        return {
            str(key): _redact_obj(item, user_home=user_home, data_dir=data_dir)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _redact_obj(item, user_home=user_home, data_dir=data_dir)
            for item in value
        ]
    return value


def _tail_lines(path: Path, max_bytes: int) -> tuple[list[str], int]:
    """读取文件尾部并返回 (行列表, 扫描的原始行数)。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return [], 0
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if start > 0 and lines:
        # 首行可能被截断，丢弃。
        lines = lines[1:]
    return lines, len(lines)


def _collect_app_errors(
    log_dir: Optional[Path], *, user_home: str, data_dir: Path
) -> tuple[list[str], int, int]:
    if log_dir is None:
        return [], 0, 0
    lines, scanned = _tail_lines(Path(log_dir) / "app.log", MAX_APP_ERRORS_BYTES)
    errors: list[str] = []
    dropped = 0
    for line in lines:
        if not (_ERROR_LINE_RE.search(line) or "window-trace" in line):
            continue
        cleaned = redact_text(line, user_home=user_home, data_dir=data_dir)
        if _scan_leak(cleaned, user_home):
            dropped += 1
            continue
        errors.append(cleaned)
    return errors, scanned, dropped


def _collect_ffmpeg_tail(
    log_dir: Optional[Path], *, user_home: str, data_dir: Path
) -> tuple[list[str], int]:
    if log_dir is None:
        return [], 0
    lines, _scanned = _tail_lines(
        Path(log_dir) / "ffmpeg-stderr.log", MAX_FFMPEG_TAIL_BYTES
    )
    dropped = 0
    cleaned_lines: list[str] = []
    for line in lines[-MAX_FFMPEG_TAIL_LINES:]:
        cleaned = redact_text(line, user_home=user_home, data_dir=data_dir)
        if _scan_leak(cleaned, user_home):
            dropped += 1
            continue
        cleaned_lines.append(cleaned)
    return cleaned_lines, dropped


def _memory_total_mb() -> Optional[int]:
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys // (1024 * 1024))
    except Exception:  # noqa: BLE001 - 信息缺失时省略
        return None
    return None


def _database_summary(db_path: Optional[Path]) -> dict:
    summary: dict[str, Any] = {
        "available": False,
        "schema_version": None,
        "quick_check": "unavailable",
        "rooms": None,
        "sessions": None,
        "recordings_total": None,
        "recordings_failed": None,
        "recordings_interrupted": None,
    }
    if db_path is None or not Path(db_path).is_file():
        return summary
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error as e:
        summary["quick_check"] = "busy" if "locked" in str(e).lower() else "unavailable"
        return summary
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        summary["quick_check"] = str(row[0]) if row else "unknown"
        summary["available"] = True
        summary["schema_version"] = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        summary["rooms"] = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        summary["sessions"] = conn.execute(
            "SELECT COUNT(*) FROM monitor_sessions"
        ).fetchone()[0]
        summary["recordings_total"] = conn.execute(
            "SELECT COUNT(*) FROM recordings"
        ).fetchone()[0]
        summary["recordings_failed"] = conn.execute(
            "SELECT COUNT(*) FROM recordings WHERE status='failed'"
        ).fetchone()[0]
        summary["recordings_interrupted"] = conn.execute(
            "SELECT COUNT(*) FROM recordings WHERE status='interrupted'"
        ).fetchone()[0]
    except sqlite3.Error as e:
        # 数据库被写锁占用等情况：降级为 busy，不阻塞录制。
        summary["quick_check"] = "busy" if "locked" in str(e).lower() else "error"
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return summary


def _ffmpeg_summary(ffmpeg_path: str) -> dict:
    info: dict[str, Any] = {
        "configured": "unset",
        "path": "",
        "version_first_line": "",
        "probe_ok": False,
        "probe_error": "",
    }
    path = (ffmpeg_path or "").strip()
    if not path:
        return info
    lowered = path.replace("\\", "/").lower()
    info["configured"] = (
        "builtin"
        if "_internal/ffmpeg" in lowered or "tools/ffmpeg" in lowered
        else "user"
    )
    info["path"] = path
    try:
        from .ffmpeg import probe_ffmpeg

        ok, text = probe_ffmpeg(path, timeout_s=3.0)
        info["probe_ok"] = ok
        if ok:
            info["version_first_line"] = text
        else:
            info["probe_error"] = text
    except Exception as e:  # noqa: BLE001 - 诊断不得因 FFmpeg 异常失败
        info["probe_error"] = f"{type(e).__name__}"
    return info


def _build_report(
    *,
    app_version: str,
    data_dir: Path,
    settings: Any,
    ffmpeg_path: str,
    active_rooms: int,
    frozen: bool,
    error_lines: list[str],
    error_lines_scanned: int,
    dropped_app: int,
    ffmpeg_lines: int,
    dropped_ffmpeg: int,
    truncated: bool,
    generated_at: str,
) -> dict:
    config: dict[str, Any] = {}
    if settings is not None:
        config = {
            "quality": getattr(settings, "quality", None),
            "monitor_interval_s": getattr(settings, "monitor_interval_s", None),
            "min_free_mb": getattr(settings, "min_free_mb", None),
            "recordings_dir": str(getattr(settings, "recordings_dir", "")),
        }
    return {
        "schema": 1,
        "generated_at": generated_at,
        "app": {
            "version": app_version,
            "frozen": frozen,
            "executable": sys.executable,
        },
        "runtime": {
            "python": platform.python_version(),
            "pyside6": _pyside_version(),
            "os": platform.platform(),
            "arch": platform.machine(),
            "cpu_count": os.cpu_count(),
            "memory_total_mb": _memory_total_mb(),
        },
        "ffmpeg": _ffmpeg_summary(ffmpeg_path),
        "config": config,
        "database": _database_summary(_db_path_from(data_dir)),
        "process": {"active_recording_rooms": active_rooms},
        "errors": {
            "app_log_lines_scanned": error_lines_scanned,
            "app_error_lines": len(error_lines),
            "recent": error_lines[-MAX_ERROR_LINES_REPORTED:],
            "ffmpeg_tail_lines": ffmpeg_lines,
            "redaction_dropped_lines": dropped_app + dropped_ffmpeg,
        },
        "privacy": {
            "redaction": [
                "urls",
                "tokens",
                "cookies",
                "paths",
                "emails",
                "ips",
                "room_ids_masked",
            ],
            "excluded": ["cookie", "db_rows", "full_urls", "stream_urls"],
        },
        "truncated": truncated,
    }


def _pyside_version() -> str:
    try:
        import PySide6

        return PySide6.__version__
    except Exception:  # noqa: BLE001
        return "unknown"


def _db_path_from(data_dir: Path) -> Optional[Path]:
    candidate = Path(data_dir) / "app.db"
    return candidate if candidate.is_file() else None


def _render_markdown(report: dict) -> str:
    app = report.get("app", {})
    runtime = report.get("runtime", {})
    ffmpeg = report.get("ffmpeg", {})
    config = report.get("config", {})
    database = report.get("database", {})
    errors = report.get("errors", {})
    lines = [
        "# 抖音直播录制工具 诊断报告",
        "",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- 软件版本：{app.get('version', '')}",
        f"- 打包运行：{'是' if app.get('frozen') else '否'}",
        "",
        "## 运行环境",
        "",
        f"- Python：{runtime.get('python', '')}",
        f"- PySide6：{runtime.get('pyside6', '')}",
        f"- 系统：{runtime.get('os', '')}",
        f"- 架构：{runtime.get('arch', '')}",
        f"- CPU 逻辑核心：{runtime.get('cpu_count', '')}",
        f"- 物理内存：{runtime.get('memory_total_mb', '')} MB",
        "",
        "## FFmpeg",
        "",
        f"- 来源：{ffmpeg.get('configured', '')}",
        f"- 路径：{ffmpeg.get('path', '')}",
        f"- 检测：{'通过' if ffmpeg.get('probe_ok') else '失败'}",
        f"- 版本：{ffmpeg.get('version_first_line', '') or ffmpeg.get('probe_error', '')}",
        "",
        "## 配置摘要",
        "",
    ]
    for key, value in config.items():
        lines.append(f"- {key}：{value}")
    lines.extend(
        [
            "",
            "## 数据库完整性",
            "",
            f"- quick_check：{database.get('quick_check', '')}",
            f"- Schema：{database.get('schema_version', '')}",
            f"- 房间数：{database.get('rooms', '')}",
            f"- 录制记录：{database.get('recordings_total', '')}",
            f"- 失败记录：{database.get('recordings_failed', '')}",
            f"- 中断记录：{database.get('recordings_interrupted', '')}",
            "",
            "## 录制状态",
            "",
            f"- 当前活动录制的房间数：{report.get('process', {}).get('active_recording_rooms', 0)}",
            "",
            f"## 最近错误与窗口追踪（最多 {MAX_ERROR_LINES_REPORTED} 条）",
            "",
        ]
    )
    recent = errors.get("recent") or []
    if recent:
        lines.extend(f"- {item}" for item in recent)
    else:
        lines.append("（无）")
    lines.extend(
        [
            "",
            "## 隐私声明",
            "",
            "- 已脱敏：流地址、令牌、Cookie、路径、邮箱、IP、直播间号。",
            "- 未包含：Cookie 凭据文件、数据库原始行、完整直播流地址。",
            f"- 被丢弃的疑似敏感行：{errors.get('redaction_dropped_lines', 0)}",
            f"- 日志已裁剪：{'是' if report.get('truncated') else '否'}",
            "",
        ]
    )
    return "\n".join(lines)


def _compose_zip(
    report_json: str,
    report_md: str,
    app_lines: list[str],
    ffmpeg_lines: list[str],
) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", report_json)
        archive.writestr("report.md", report_md)
        archive.writestr(
            "logs/app-errors.txt",
            "\n".join(app_lines) + ("\n" if app_lines else ""),
        )
        archive.writestr(
            "logs/ffmpeg-stderr.txt",
            "\n".join(ffmpeg_lines) + ("\n" if ffmpeg_lines else ""),
        )
    return buffer.getvalue()


def _unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for index in range(2, 100):
        candidate = dest.with_name(f"{dest.stem}-{index}{dest.suffix}")
        if not candidate.exists():
            return candidate
    raise DiagnosticsError("无法生成唯一的诊断文件名")


def build_diagnostics_zip(
    dest: Path,
    *,
    app_version: str,
    data_dir: Path,
    log_dir: Optional[Path] = None,
    settings: Any = None,
    ffmpeg_path: str = "",
    active_rooms: int = 0,
    frozen: bool = False,
    cancel_event: Optional[threading.Event] = None,
    clock: Callable[[], float] = time.time,
) -> Path:
    """生成诊断 ZIP，返回实际写入路径。

    全程不访问网络，不读取凭据文件，不修改数据库与录制文件。
    """
    dest = Path(dest)
    data_dir = Path(data_dir)
    user_home = os.path.expanduser("~")

    def _check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DiagnosticsCancelled("诊断导出已取消")

    _check_cancel()
    app_lines, scanned, dropped_app = _collect_app_errors(
        log_dir, user_home=user_home, data_dir=data_dir
    )
    _check_cancel()
    ffmpeg_lines, dropped_ffmpeg = _collect_ffmpeg_tail(
        log_dir, user_home=user_home, data_dir=data_dir
    )
    _check_cancel()

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report = _build_report(
        app_version=app_version,
        data_dir=data_dir,
        settings=settings,
        ffmpeg_path=ffmpeg_path,
        active_rooms=active_rooms,
        frozen=frozen,
        error_lines=app_lines,
        error_lines_scanned=scanned,
        dropped_app=dropped_app,
        ffmpeg_lines=len(ffmpeg_lines),
        dropped_ffmpeg=dropped_ffmpeg,
        truncated=False,
        generated_at=generated_at,
    )
    report = _redact_obj(report, user_home=user_home, data_dir=data_dir)
    # report 内所有字符串已脱敏，Markdown 由其渲染，无需再次整体替换
    # （整体替换会因长度截断破坏格式）。
    markdown = _render_markdown(report)

    # 体积控制：从最旧的日志行开始丢弃，保证最终 ZIP 不超过 1 MiB。
    app_slice = list(app_lines)
    ffmpeg_slice = list(ffmpeg_lines)
    truncated = False
    payload = b""
    for _ in range(12):
        _check_cancel()
        report["truncated"] = truncated
        report["errors"]["app_error_lines"] = len(app_slice)
        report["errors"]["recent"] = app_slice[-MAX_ERROR_LINES_REPORTED:]
        report["errors"]["ffmpeg_tail_lines"] = len(ffmpeg_slice)
        markdown_truncated = markdown + (
            "\n> 日志已裁剪以控制诊断包体积。\n" if truncated else ""
        )
        payload = _compose_zip(
            json.dumps(report, ensure_ascii=False, indent=2),
            markdown_truncated,
            app_slice,
            ffmpeg_slice,
        )
        if len(payload) <= MAX_ZIP_BYTES:
            break
        truncated = True
        if app_slice:
            app_slice = app_slice[len(app_slice) // 2 :]
            continue
        if ffmpeg_slice:
            ffmpeg_slice = ffmpeg_slice[len(ffmpeg_slice) // 2 :]
            continue
        raise DiagnosticsError("诊断报告超过 1 MiB 上限，已停止生成")
    else:
        raise DiagnosticsError("诊断报告超过 1 MiB 上限，已停止生成")

    target = _unique_path(dest)
    temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _check_cancel()
        temp_path.write_bytes(payload)
        _check_cancel()
        os.replace(temp_path, target)
    except DiagnosticsCancelled:
        raise
    except OSError as e:
        raise DiagnosticsError(f"写入诊断文件失败：{type(e).__name__}") from e
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            logger.debug("清理诊断临时文件失败: %s", temp_path, exc_info=True)
    logger.info("诊断报告已生成: %s (%s 字节)", target, len(payload))
    return target


def default_filename(now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"DLR-diagnostics-{stamp}.zip"


__all__ = [
    "DiagnosticsCancelled",
    "DiagnosticsError",
    "MAX_ZIP_BYTES",
    "build_diagnostics_zip",
    "default_filename",
    "redact_text",
]
