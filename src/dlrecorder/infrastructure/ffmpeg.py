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
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_STOP_GRACE_S = 5.0
DEFAULT_KILL_GRACE_S = 2.0

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


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
    ) -> None:
        if not command:
            raise ValueError("command 不能为空")
        self._command = list(command)
        self._cwd = str(cwd) if cwd is not None else None
        self._stop_grace_s = max(0.0, stop_grace_s)
        self._kill_grace_s = max(0.0, kill_grace_s)
        self._proc: Optional[subprocess.Popen] = None

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
            stderr=subprocess.DEVNULL,
            cwd=self._cwd,
            creationflags=_CREATE_NO_WINDOW,
        )
        logger.info("FFmpeg 已启动 pid=%s", self._proc.pid)

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
