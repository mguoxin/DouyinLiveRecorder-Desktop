"""软件设置服务：读取、校验并持久化用户可调参数。

设置存储在既有 ``settings`` 表中，不改变数据库 Schema。
只保存路径、间隔和阈值，不保存任何凭据。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from ..infrastructure.config import AppConfig
from ..infrastructure.db import Database
from ..infrastructure.ffmpeg import find_local_ffmpeg

logger = logging.getLogger(__name__)

KEY_FFMPEG_PATH = "recording.ffmpeg_path"
KEY_RECORDINGS_DIR = "recording.output_dir"
KEY_MONITOR_INTERVAL = "monitor.interval_s"
KEY_MIN_FREE_MB = "storage.min_free_mb"

DEFAULT_MONITOR_INTERVAL_S = 120.0
DEFAULT_MIN_FREE_MB = 512

MIN_MONITOR_INTERVAL_S = 5.0
MAX_MONITOR_INTERVAL_S = 3600.0
MAX_MIN_FREE_MB = 1024 * 1024
MAX_FFMPEG_PATH_LEN = 260


@dataclass(frozen=True)
class AppSettings:
    """一组完整、已校验的软件设置。"""

    ffmpeg_path: str
    recordings_dir: Path
    monitor_interval_s: float
    min_free_mb: int


def validate_ffmpeg_path(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("FFmpeg 路径不能为空")
    if len(text) > MAX_FFMPEG_PATH_LEN:
        raise ValueError(f"FFmpeg 路径不能超过 {MAX_FFMPEG_PATH_LEN} 个字符")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("FFmpeg 路径不能包含控制字符")
    return text


def validate_recordings_dir(value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("录制目录不能为空")
    path = Path(text).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"录制目录无法创建: {e}") from e
    if not path.is_dir():
        raise ValueError("录制目录不是有效文件夹")
    return path.absolute()


def validate_monitor_interval(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError("监控间隔必须是数字") from e
    if not math.isfinite(number):
        raise ValueError("监控间隔必须是有限数字")
    if not (MIN_MONITOR_INTERVAL_S <= number <= MAX_MONITOR_INTERVAL_S):
        raise ValueError(
            f"监控间隔需在 {MIN_MONITOR_INTERVAL_S:.0f}-"
            f"{MAX_MONITOR_INTERVAL_S:.0f} 秒之间"
        )
    return number


def validate_min_free_mb(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError("磁盘最低剩余空间必须是整数") from e
    if not (0 <= number <= MAX_MIN_FREE_MB):
        raise ValueError(f"磁盘最低剩余空间需在 0-{MAX_MIN_FREE_MB} MB 之间")
    return number


class SettingsService:
    """设置读写与校验。"""

    def __init__(self, db: Database, config: AppConfig) -> None:
        self._db = db
        self._config = config

    def defaults(self) -> AppSettings:
        return AppSettings(
            ffmpeg_path=find_local_ffmpeg(),
            recordings_dir=self._config.recordings_dir,
            monitor_interval_s=DEFAULT_MONITOR_INTERVAL_S,
            min_free_mb=DEFAULT_MIN_FREE_MB,
        )

    def load(self) -> AppSettings:
        """读取设置；个别值非法时回退到默认值并记录日志，不阻止启动。"""
        defaults = self.defaults()
        current = AppSettings(
            ffmpeg_path=self._load_value(
                KEY_FFMPEG_PATH, defaults.ffmpeg_path, validate_ffmpeg_path
            ),
            recordings_dir=self._load_value(
                KEY_RECORDINGS_DIR, defaults.recordings_dir, validate_recordings_dir
            ),
            monitor_interval_s=self._load_value(
                KEY_MONITOR_INTERVAL,
                defaults.monitor_interval_s,
                validate_monitor_interval,
            ),
            min_free_mb=self._load_value(
                KEY_MIN_FREE_MB, defaults.min_free_mb, validate_min_free_mb
            ),
        )
        # 旧版本可能保存了默认命令名；若项目内已内置 FFmpeg，则优先使用内置版本。
        if current.ffmpeg_path in {"ffmpeg", "ffmpeg.exe"}:
            local = find_local_ffmpeg()
            if local != "ffmpeg":
                current = replace(current, ffmpeg_path=local)
        return current

    def _load_value(self, key: str, default: Any, validator: Any) -> Any:
        raw = self._db.get_setting(key, "")
        if raw == "":
            return default
        try:
            return validator(raw)
        except ValueError as e:
            logger.warning("设置项 %s 无效，使用默认值 %r: %s", key, default, e)
            return default

    def save(self, settings: AppSettings) -> AppSettings:
        """校验并持久化全部设置；任一项非法则整体不写入。"""
        normalized = AppSettings(
            ffmpeg_path=validate_ffmpeg_path(settings.ffmpeg_path),
            recordings_dir=validate_recordings_dir(settings.recordings_dir),
            monitor_interval_s=validate_monitor_interval(settings.monitor_interval_s),
            min_free_mb=validate_min_free_mb(settings.min_free_mb),
        )
        self._db.set_setting(KEY_FFMPEG_PATH, normalized.ffmpeg_path)
        self._db.set_setting(KEY_RECORDINGS_DIR, str(normalized.recordings_dir))
        self._db.set_setting(
            KEY_MONITOR_INTERVAL, str(normalized.monitor_interval_s)
        )
        self._db.set_setting(KEY_MIN_FREE_MB, str(normalized.min_free_mb))
        logger.info(
            "设置已保存: ffmpeg=%s recordings=%s interval=%ss min_free=%sMB",
            normalized.ffmpeg_path,
            normalized.recordings_dir,
            normalized.monitor_interval_s,
            normalized.min_free_mb,
        )
        return normalized

    def backup_database(self, dest: Path) -> Path:
        """创建一致性备份；任何失败都会抛出异常，由调用方提示用户。"""
        return self._db.backup_to(Path(dest))

    def prepare_restore(self, backup_path: Path) -> Path:
        """登记恢复请求；实际替换数据库在下次启动时执行。"""
        from ..infrastructure.backup import prepare_restore

        return prepare_restore(Path(backup_path), self._config.data_dir)
