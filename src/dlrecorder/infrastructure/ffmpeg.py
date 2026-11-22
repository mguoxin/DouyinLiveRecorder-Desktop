"""FFmpeg 子进程管理。

职责：
- 启动 FFmpeg 进程并保证不弹出控制台窗口。
- 优先通过 stdin 发送 ``q`` 让 FFmpeg 正常收尾；超时后先 terminate，再 kill。
- 如实报告退出码、是否使用 terminate/kill、等待耗时，不伪造优雅退出。

说明：终止对象是外部 FFmpeg 子进程，不是线程；线程生命周期仍遵循项目既有约束，
禁止 QThread.terminate() / os._exit()。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .winproc import KillOnCloseJob

logger = logging.getLogger(__name__)

DEFAULT_STOP_GRACE_S = 5.0
DEFAULT_KILL_GRACE_S = 2.0
DEFAULT_DIAGNOSTICS_MAX_BYTES = 256 * 1024
DEFAULT_STDERR_TAIL_LINES = 40
DEFAULT_STDERR_JOIN_S = 1.0

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# 诊断日志只记录 FFmpeg 输出；写入前必须掩码可能携带凭据的查询参数与请求头。
_SENSITIVE_QUERY = re.compile(
    r"(?i)(?P<key>\b(?:access_token|refresh_token|token|cookie|sessionid|"
    r"session_key|a_bogus|x[_-]bogus|mstoken|ttwid|sign|signature|auth_key|"
    r"expire|x-expires|x-signature|x-amz-credential|x-amz-signature|"
    r"x-amz-security-token))=(?P<value>[^&\s\"']+)"
)
_SENSITIVE_HEADER = re.compile(
    r"(?i)(?P<header>\b(?:cookie|authorization|proxy-authorization):\s*).*"
)


def redact_stream_text(text: str, stream_url: str = "") -> str:
    """掩码流地址与常见凭据参数，避免诊断日志泄露可复用签名。"""
    if stream_url:
        text = text.replace(stream_url, "<stream-url>")
    text = _SENSITIVE_QUERY.sub(r"\g<key>=<redacted>", text)
    text = _SENSITIVE_HEADER.sub(r"\g<header><redacted>", text)
    return text


class _DiagnosticsLog:
    """有界诊断日志：单文件写入，超过上限时轮转为 ``.1``。"""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self._path = Path(path)
        self._max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        # 进程内保持单一文件句柄：逐行 open/close 在 Windows 上会被安全软件放大，
        # 实测 3.8MB stderr 需要 35 秒，足以反压 FFmpeg。改为常开句柄 + 每次写入
        # flush，既保证及时落盘，又把排空速度拉回毫秒级。
        self._handle: Optional[Any] = None
        self._written = 0

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("ab")
        try:
            self._written = self._path.stat().st_size
        except OSError:
            self._written = 0

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        self._written = 0

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        with self._lock:
            try:
                if self._handle is None:
                    self._open()
                if self._written + len(data) > self._max_bytes and self._written > 0:
                    self._close_handle()
                    rotated = self._path.with_name(self._path.name + ".1")
                    try:
                        rotated.unlink()
                    except FileNotFoundError:
                        pass
                    self._path.replace(rotated)
                    self._open()
                if self._handle is None:
                    return
                self._handle.write(data)
                self._handle.flush()
                self._written += len(data)
            except OSError:
                # 诊断落盘失败不得影响录制。
                self._close_handle()
                logger.debug("写入 FFmpeg 诊断日志失败: %s", self._path, exc_info=True)

    def close(self) -> None:
        """关闭文件句柄；下次写入时按需重新打开。"""
        with self._lock:
            self._close_handle()


_DIAGNOSTICS_LOGS: dict[str, _DiagnosticsLog] = {}
_DIAGNOSTICS_LOGS_LOCK = threading.Lock()


def _diagnostics_log(path: Path, max_bytes: int) -> _DiagnosticsLog:
    """同一路径共用一把写锁，避免多个录制线程互相截断日志。"""
    key = str(Path(path).resolve())
    with _DIAGNOSTICS_LOGS_LOCK:
        sink = _DIAGNOSTICS_LOGS.get(key)
        if sink is None:
            sink = _DiagnosticsLog(path, max_bytes)
            _DIAGNOSTICS_LOGS[key] = sink
        return sink


@dataclass(frozen=True)
class FFmpegStopReport:
    """一次停止操作的真实结果。"""

    exit_code: Optional[int]
    graceful: bool
    terminated: bool
    killed: bool
    exited: bool
    waited_s: float


def file_size(path: Path) -> int:
    """返回文件字节数；文件不存在或不可读时返回 0。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def find_local_ffmpeg() -> str:
    """优先返回项目内或打包目录内置的 FFmpeg 路径，否则回退到 PATH 命令名。"""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        candidates.extend(
            [
                exe_dir / "ffmpeg" / "bin" / "ffmpeg.exe",
                exe_dir / "_internal" / "ffmpeg" / "bin" / "ffmpeg.exe",
            ]
        )
    project_root = Path(__file__).resolve().parents[3]
    candidates.append(project_root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "ffmpeg"


def probe_ffmpeg(ffmpeg_path: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
    """检测 FFmpeg 是否可用，返回 (是否可用, 说明)。

    只执行 ``ffmpeg -version``，不访问网络，不读取任何用户凭据。
    """
    path = (ffmpeg_path or "").strip()
    if not path:
        return False, "FFmpeg 路径为空"
    try:
        completed = subprocess.run(
            [path, "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.1, timeout_s),
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except FileNotFoundError:
        return False, "未找到 FFmpeg 可执行文件"
    except OSError as e:
        return False, f"无法启动 FFmpeg: {e}"
    except subprocess.TimeoutExpired:
        return False, "FFmpeg 检测超时"

    output = (completed.stdout or b"") + (completed.stderr or b"")
    text = output.decode("utf-8", "replace")
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if completed.returncode != 0:
        return False, f"FFmpeg 返回退出码 {completed.returncode}"
    if "ffmpeg version" not in text.lower():
        return False, "该程序不是可用的 FFmpeg（缺少版本信息）"
    return True, first_line[:200] or "FFmpeg 可用"


def build_record_command(
    ffmpeg_path: str,
    stream_url: str,
    output_path: Path,
    *,
    reconnect: bool = True,
    reconnect_delay_max_s: int = 60,
    rw_timeout_us: int = 15_000_000,
    max_muxing_queue_size: int = 1024,
    analyzeduration_us: int = 20_000_000,
    probesize: int = 10_000_000,
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
) -> list[str]:
    """构建默认录制命令：直接封装原始流到 MPEG-TS，不转码。

    参数组合与上游 ihmily/DouyinLiveRecorder v4.0.7 的抖音分支对齐（除下述
    刻意差异）：更长的 ``-rw_timeout``、显式的 ``-analyzeduration/-probesize``、
    ``-fflags +discardcorrupt``、``-thread_queue_size`` 与 TS 时间戳修正参数，
    用于降低弱网下的首包延迟与因损坏包导致的退出。

    与上游的刻意差异：不使用 ``-reconnect_streamed`` / ``-reconnect_at_eof``。
    本项目已在实测中确认它们会让直播结束（EOF）后的进程反复重连而不退出，
    而进程级异常退出后的重新解析与续录由 ``RecordingService`` 负责。

    输入侧参数必须位于 ``-i`` 之前，否则会被当作输出参数而失效。``-reconnect``
    与 HTTP 相关参数仅对 HTTP(S) 输入有效，本地文件会因 "Option not found"
    直接失败，因此只在网络流上启用。
    """
    is_http_input = stream_url.lower().startswith(("http://", "https://"))
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
    ]
    if is_http_input:
        command.extend(
            [
                "-rw_timeout",
                str(max(0, int(rw_timeout_us))),
                "-user_agent",
                user_agent,
                "-protocol_whitelist",
                "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy",
                "-thread_queue_size",
                "1024",
                "-analyzeduration",
                str(max(0, int(analyzeduration_us))),
                "-probesize",
                str(max(0, int(probesize))),
                "-fflags",
                "+discardcorrupt",
            ]
        )
    if reconnect and is_http_input:
        command.extend(
            [
                "-reconnect",
                "1",
                "-reconnect_delay_max",
                str(max(1, int(reconnect_delay_max_s))),
            ]
        )
    command.extend(
        [
            "-i",
            stream_url,
            "-c",
            "copy",
            "-sn",
            "-dn",
            "-max_muxing_queue_size",
            str(max(1, int(max_muxing_queue_size))),
            "-correct_ts_overflow",
            "1",
            "-avoid_negative_ts",
            "1",
            "-f",
            "mpegts",
            str(output_path),
        ]
    )
    return command


class FFmpegProcess:
    """单个 FFmpeg 子进程的生命周期封装。"""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        stop_grace_s: float = DEFAULT_STOP_GRACE_S,
        kill_grace_s: float = DEFAULT_KILL_GRACE_S,
        job: Optional[KillOnCloseJob] = None,
        diagnostics_dir: Optional[Path] = None,
        diagnostics_max_bytes: int = DEFAULT_DIAGNOSTICS_MAX_BYTES,
    ) -> None:
        if not command:
            raise ValueError("command 不能为空")
        self._command = list(command)
        self._cwd = str(cwd) if cwd is not None else None
        self._stop_grace_s = max(0.0, stop_grace_s)
        self._kill_grace_s = max(0.0, kill_grace_s)
        self._job = job
        self._proc: Optional[subprocess.Popen] = None
        # 用于在诊断日志中把完整流地址替换为固定占位符。
        self._input_url = ""
        if "-i" in self._command:
            index = self._command.index("-i")
            if index + 1 < len(self._command):
                self._input_url = self._command[index + 1]
        self._diagnostics = (
            _diagnostics_log(diagnostics_dir / "ffmpeg-stderr.log", diagnostics_max_bytes)
            if diagnostics_dir is not None
            else None
        )
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_tail: deque[str] = deque(maxlen=DEFAULT_STDERR_TAIL_LINES)

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def started(self) -> bool:
        return self._proc is not None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("FFmpeg 进程已启动")
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            creationflags=_CREATE_NO_WINDOW,
        )
        # 专用线程持续排空 stderr，避免管道写满导致 FFmpeg 阻塞。
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"dlr-ffmpeg-stderr-{self._proc.pid}",
            daemon=True,
        )
        self._stderr_thread.start()
        # 加入 Job Object：应用被强制结束时由系统一并终止，避免孤儿进程。
        if self._job is not None:
            self._job.assign(self._proc.pid)
        logger.info("FFmpeg 已启动 pid=%s", self._proc.pid)

    def _drain_stderr(self) -> None:
        """逐行读取并脱敏 FFmpeg stderr；任何异常都只结束读取线程。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                line = redact_stream_text(line, self._input_url)
                self._stderr_tail.append(line)
                if self._diagnostics is not None:
                    self._diagnostics.write(line + "\n")
        except (OSError, ValueError):
            logger.debug("读取 FFmpeg stderr 结束", exc_info=True)

    def stderr_tail(self, limit: int = 20) -> str:
        """返回最近若干行已脱敏诊断输出，供错误日志附加。"""
        lines = list(self._stderr_tail)
        if limit > 0:
            lines = lines[-limit:]
        return "\n".join(lines)

    def poll(self) -> Optional[int]:
        return self._proc.poll() if self._proc is not None else None

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self._proc is None:
            return None
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> FFmpegStopReport:
        """有界停止：q → terminate → kill，返回真实现场。"""
        started_at = time.monotonic()
        if self._proc is None:
            return FFmpegStopReport(None, True, False, False, True, 0.0)

        if self._proc.poll() is not None:
            return FFmpegStopReport(
                self._proc.returncode, True, False, False, True, 0.0
            )

        # 1) 通过 stdin 发送 q，让 FFmpeg 写完文件尾部。
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write(b"q\n")
                self._proc.stdin.flush()
                self._proc.stdin.close()
        except (OSError, ValueError):
            logger.debug("向 FFmpeg stdin 发送 q 失败，将使用 terminate", exc_info=True)

        try:
            self._proc.wait(timeout=self._stop_grace_s)
            return FFmpegStopReport(
                self._proc.returncode,
                True,
                False,
                False,
                True,
                time.monotonic() - started_at,
            )
        except subprocess.TimeoutExpired:
            pass

        # 2) terminate 后仍有界等待。
        terminated = False
        if self._proc.poll() is None:
            terminated = True
            logger.warning("FFmpeg 未响应 q，调用 terminate pid=%s", self._proc.pid)
            try:
                self._proc.terminate()
            except OSError:
                logger.exception("FFmpeg terminate 失败 pid=%s", self._proc.pid)
            try:
                self._proc.wait(timeout=self._stop_grace_s)
                return FFmpegStopReport(
                    self._proc.returncode,
                    False,
                    terminated,
                    False,
                    True,
                    time.monotonic() - started_at,
                )
            except subprocess.TimeoutExpired:
                pass

        # 3) 最后手段：kill。
        killed = False
        if self._proc.poll() is None:
            killed = True
            logger.critical("FFmpeg 未响应 terminate，调用 kill pid=%s", self._proc.pid)
            try:
                self._proc.kill()
            except OSError:
                logger.exception("FFmpeg kill 失败 pid=%s", self._proc.pid)
            try:
                self._proc.wait(timeout=self._kill_grace_s)
            except subprocess.TimeoutExpired:
                logger.critical("FFmpeg kill 后仍未退出 pid=%s", self._proc.pid)

        exited = self._proc.poll() is not None
        return FFmpegStopReport(
            self._proc.returncode,
            False,
            terminated,
            killed,
            exited,
            time.monotonic() - started_at,
        )

    def close_streams(self) -> None:
        if self._proc is None:
            return
        if self._proc.stdin is not None and not self._proc.stdin.closed:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        thread = self._stderr_thread
        if thread is not None:
            thread.join(timeout=DEFAULT_STDERR_JOIN_S)
            self._stderr_thread = None
        if self._proc.stderr is not None:
            try:
                self._proc.stderr.close()
            except (OSError, ValueError):
                pass
        if self._diagnostics is not None:
            # 释放常开日志句柄，避免 Windows 上阻止临时目录清理。
            self._diagnostics.close()
