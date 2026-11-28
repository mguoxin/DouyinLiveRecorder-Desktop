"""媒体探测与缩略图生成（P1 媒体资产）。

只读访问录制文件：通过 FFprobe（若可用）或 FFmpeg 自身读取容器信息，
并用 FFmpeg 生成单帧缩略图。任何失败都转为 ``MediaProbeError``，由上层
服务记录到数据库并允许人工重试。

说明：项目内置的 LGPL FFmpeg 包不包含 ``ffprobe``，因此默认回退为解析
``ffmpeg -i`` 的元信息输出；若用户环境存在 ``ffprobe`` 则优先使用。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_MAX_ERROR_LEN = 300

_DURATION_RE = re.compile(r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?:\s*Video:\s*([A-Za-z0-9_\-]+)")
_AUDIO_RE = re.compile(r"Stream #\S+.*?:\s*Audio:\s*([A-Za-z0-9_\-]+)")
_SIZE_RE = re.compile(r"(?<![\d])(\d{2,5})x(\d{2,5})(?![\d])")


@dataclass(frozen=True)
class MediaMetadata:
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "duration_s": self.duration_s,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
        }


class MediaProbeError(RuntimeError):
    """媒体探测或缩略图生成失败，``category`` 用于区分可重试性。"""

    def __init__(self, message: str, *, category: str = "probe_failed") -> None:
        super().__init__(message[:_MAX_ERROR_LEN])
        self.category = category


def resolve_ffprobe(ffmpeg_path: str) -> str:
    """返回可用的 ffprobe 路径；找不到时返回空字符串。"""
    ffmpeg = (ffmpeg_path or "").strip()
    if ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe.exe")
        if candidate.is_file():
            return str(candidate)
        candidate = Path(ffmpeg).with_name("ffprobe")
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffprobe")
    return found or ""


def parse_ffmpeg_info(text: str) -> MediaMetadata:
    """解析 ``ffmpeg -i`` 输出中的时长、分辨率与编解码器。"""
    duration: Optional[float] = None
    match = _DURATION_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        try:
            duration = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
        except ValueError:
            duration = None

    width = height = None
    video_codec = None
    audio_codec = None
    for line in text.splitlines():
        if "Video:" in line and video_codec is None and "attached pic" not in line:
            video = _VIDEO_RE.search(line)
            if video:
                video_codec = video.group(1)
            size = _SIZE_RE.search(line)
            if size:
                width = int(size.group(1))
                height = int(size.group(2))
        elif "Audio:" in line and audio_codec is None:
            audio = _AUDIO_RE.search(line)
            if audio:
                audio_codec = audio.group(1)
    return MediaMetadata(
        duration_s=duration if duration and duration >= 0 else None,
        width=width,
        height=height,
        video_codec=video_codec,
        audio_codec=audio_codec,
    )


def parse_ffprobe_json(text: str) -> MediaMetadata:
    """解析 ``ffprobe -print_format json`` 输出。"""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise MediaProbeError(f"FFprobe 输出不是合法 JSON: {exc}", category="invalid_output") from exc
    if not isinstance(payload, dict):
        raise MediaProbeError("FFprobe 输出结构异常", category="invalid_output")

    duration: Optional[float] = None
    format_info = payload.get("format") or {}
    if isinstance(format_info, dict):
        try:
            parsed = float(format_info.get("duration"))
            if parsed >= 0:
                duration = parsed
        except (TypeError, ValueError):
            duration = None

    width = height = None
    video_codec = None
    audio_codec = None
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        if codec_type == "video" and video_codec is None:
            disposition = stream.get("disposition")
            if isinstance(disposition, dict) and disposition.get("attached_pic"):
                continue
            video_codec = _text(stream.get("codec_name"))
            width = _positive_int(stream.get("width"))
            height = _positive_int(stream.get("height"))
        elif codec_type == "audio" and audio_codec is None:
            audio_codec = _text(stream.get("codec_name"))
    if duration is None:
        for stream in payload.get("streams") or []:
            if isinstance(stream, dict):
                try:
                    parsed = float(stream.get("duration"))
                    if parsed >= 0:
                        duration = parsed
                        break
                except (TypeError, ValueError):
                    continue
    return MediaMetadata(
        duration_s=duration,
        width=width,
        height=height,
        video_codec=video_codec,
        audio_codec=audio_codec,
    )


def _text(value: object) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _positive_int(value: object) -> Optional[int]:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class MediaProber:
    """对单个录制文件执行只读探测与缩略图生成。"""

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        ffprobe_path: str = "",
        probe_timeout_s: float = 20.0,
        thumbnail_timeout_s: float = 30.0,
    ) -> None:
        self.ffmpeg_path = (ffmpeg_path or "").strip()
        self.ffprobe_path = (ffprobe_path or "").strip() or resolve_ffprobe(
            self.ffmpeg_path
        )
        self.probe_timeout_s = max(1.0, float(probe_timeout_s))
        self.thumbnail_timeout_s = max(1.0, float(thumbnail_timeout_s))

    # ---------- 元数据 ----------
    def probe(self, source: Path) -> MediaMetadata:
        path = Path(source)
        if not path.is_file():
            raise MediaProbeError(f"媒体文件不存在: {path}", category="missing")
        if self.ffprobe_path:
            return self._probe_with_ffprobe(path)
        return self._probe_with_ffmpeg(path)

    def _probe_with_ffprobe(self, path: Path) -> MediaMetadata:
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        completed = self._run(command, self.probe_timeout_s, "读取媒体信息")
        text = (completed.stdout or b"").decode("utf-8", "replace")
        if completed.returncode != 0:
            raise MediaProbeError(
                self._failure_text("FFprobe 读取失败", completed),
                category="tool_failed",
            )
        return parse_ffprobe_json(text)

    def _probe_with_ffmpeg(self, path: Path) -> MediaMetadata:
        command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-i", str(path)]
        completed = self._run(command, self.probe_timeout_s, "读取媒体信息")
        text = (completed.stderr or b"").decode("utf-8", "replace")
        # 未指定输出时 FFmpeg 固定以 1 退出；只要能解析出信息即视为成功。
        if completed.returncode not in (0, 1):
            raise MediaProbeError(
                self._failure_text("FFmpeg 读取失败", completed),
                category="tool_failed",
            )
        metadata = parse_ffmpeg_info(text)
        if (
            metadata.duration_s is None
            and metadata.video_codec is None
            and metadata.audio_codec is None
        ):
            raise MediaProbeError("无法识别媒体信息", category="invalid_output")
        return metadata

    # ---------- 缩略图 ----------
    def thumbnail(
        self,
        source: Path,
        target: Path,
        *,
        duration_s: Optional[float] = None,
        width: int = 320,
    ) -> Path:
        """生成单帧缩略图；先写 ``.part`` 再原子替换。"""
        path = Path(source)
        if not path.is_file():
            raise MediaProbeError(f"媒体文件不存在: {path}", category="missing")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        if duration_s and duration_s > 1:
            seek = min(max(duration_s * 0.1, 1.0), 10.0)
        else:
            seek = 1.0
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{seek:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={max(16, int(width))}:-2",
            "-q:v",
            "3",
            "-f",
            "image2",
            str(temporary),
        ]
        try:
            completed = self._run(command, self.thumbnail_timeout_s, "生成缩略图")
            if completed.returncode != 0:
                raise MediaProbeError(
                    self._failure_text("生成缩略图失败", completed),
                    category="thumbnail_failed",
                )
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise MediaProbeError("缩略图为空", category="thumbnail_failed")
            os.replace(temporary, target)
            return target
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                logger.debug("清理缩略图临时文件失败: %s", temporary, exc_info=True)

    # ---------- 子进程 ----------
    def _run(
        self,
        command: list[str],
        timeout_s: float,
        action: str,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MediaProbeError(f"未找到媒体工具: {command[0]}", category="missing_tool") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaProbeError(f"{action}超时（{timeout_s:.0f} 秒）", category="timeout") from exc
        except OSError as exc:
            raise MediaProbeError(f"{action}失败: {exc}", category="tool_failed") from exc

    @staticmethod
    def _failure_text(prefix: str, completed: subprocess.CompletedProcess) -> str:
        text = (completed.stderr or b"").decode("utf-8", "replace").strip()
        if not text:
            text = f"退出码 {completed.returncode}"
        lines = [line for line in text.splitlines() if line.strip()]
        tail = " | ".join(lines[-3:]) if lines else text
        return f"{prefix}: {tail[:_MAX_ERROR_LEN]}"
