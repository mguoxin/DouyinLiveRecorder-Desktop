"""软件设置服务：读取、校验并持久化用户可调参数。

设置存储在既有 ``settings`` 表中，不改变数据库 Schema。
只保存路径、间隔、阈值和清晰度，不保存任何凭据；Cookie 由
``CredentialStore`` 使用 Windows DPAPI 单独加密存放。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from ..domain.archive_rules import (
    DEFAULT_FOLDER_TEMPLATE,
    DEFAULT_NAME_TEMPLATE,
    ArchiveRuleError,
    validate_template,
)
from ..infrastructure.config import AppConfig
from ..infrastructure.credential_store import CredentialStore
from ..infrastructure.db import Database
from ..infrastructure.ffmpeg import find_local_ffmpeg

logger = logging.getLogger(__name__)

KEY_FFMPEG_PATH = "recording.ffmpeg_path"
KEY_RECORDINGS_DIR = "recording.output_dir"
KEY_MONITOR_INTERVAL = "monitor.interval_s"
KEY_MIN_FREE_MB = "storage.min_free_mb"
KEY_QUALITY = "recording.quality"
KEY_ARCHIVE_ENABLED = "archive.enabled"
KEY_ARCHIVE_ROOT_DIR = "archive.root_dir"
KEY_ARCHIVE_FOLDER_TEMPLATE = "archive.folder_template"
KEY_ARCHIVE_NAME_TEMPLATE = "archive.name_template"
KEY_ARCHIVE_DELETE_SOURCE = "archive.delete_source"

DEFAULT_MONITOR_INTERVAL_S = 120.0
DEFAULT_MIN_FREE_MB = 512

MIN_MONITOR_INTERVAL_S = 5.0
MAX_MONITOR_INTERVAL_S = 3600.0
MAX_MIN_FREE_MB = 1024 * 1024
MAX_FFMPEG_PATH_LEN = 260

# 与上游 ``QUALITY_MAPPING`` 一致；OD 为原画，可能对部分房间不可用。
QUALITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("OD", "原画（OD）"),
    ("UHD", "超清（UHD）"),
    ("HD", "高清（HD）"),
    ("SD", "标清（SD）"),
    ("LD", "流畅（LD）"),
)
DEFAULT_QUALITY = "UHD"
_QUALITY_CODES = {code for code, _label in QUALITY_OPTIONS}

MAX_COOKIE_LEN = 8192


@dataclass(frozen=True)
class AppSettings:
    """一组完整、已校验的软件设置。"""

    ffmpeg_path: str
    recordings_dir: Path
    monitor_interval_s: float
    min_free_mb: int
    quality: str = DEFAULT_QUALITY
    archive_enabled: bool = False
    archive_root_dir: Path = Path("")
    archive_folder_template: str = DEFAULT_FOLDER_TEMPLATE
    archive_name_template: str = DEFAULT_NAME_TEMPLATE
    archive_delete_source: bool = False


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


def validate_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "开启", "是"}:
        return True
    if text in {"0", "false", "no", "off", "关闭", "否", ""}:
        return False
    raise ValueError("开关值必须是 0/1 或 true/false")


def validate_archive_root_dir(value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("归档目录不能为空")
    path = Path(text).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"归档目录无法创建: {e}") from e
    if not path.is_dir():
        raise ValueError("归档目录不是有效文件夹")
    return path.absolute()


def validate_archive_folder_template(value: Any) -> str:
    try:
        return validate_template(str(value or ""), field="归档目录模板")
    except ArchiveRuleError as e:
        raise ValueError(str(e)) from e


def validate_archive_name_template(value: Any) -> str:
    try:
        return validate_template(str(value or ""), field="归档文件名模板")
    except ArchiveRuleError as e:
        raise ValueError(str(e)) from e


def validate_quality(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text not in _QUALITY_CODES:
        allowed = "/".join(code for code, _ in QUALITY_OPTIONS)
        raise ValueError(f"清晰度只能是 {allowed}")
    return text


def quality_label(code: str) -> str:
    for item_code, label in QUALITY_OPTIONS:
        if item_code == code:
            return label
    return code


def validate_cookie(value: str) -> str:
    """校验 Cookie 文本；只做格式检查，不联网验证。"""
    text = (value or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    if not text:
        raise ValueError("Cookie 不能为空")
    if len(text) > MAX_COOKIE_LEN:
        raise ValueError(f"Cookie 不能超过 {MAX_COOKIE_LEN} 个字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("Cookie 不能包含换行或控制字符")
    if "=" not in text:
        raise ValueError("Cookie 需为 key=value; key2=value2 形式")
    return text


class SettingsService:
    """设置读写与校验。"""

    def __init__(
        self,
        db: Database,
        config: AppConfig,
        credential_store: Optional[CredentialStore] = None,
    ) -> None:
        self._db = db
        self._config = config
        self._credentials = credential_store or CredentialStore(config.data_dir)

    def defaults(self) -> AppSettings:
        return AppSettings(
            ffmpeg_path=find_local_ffmpeg(),
            recordings_dir=self._config.recordings_dir,
            monitor_interval_s=DEFAULT_MONITOR_INTERVAL_S,
            min_free_mb=DEFAULT_MIN_FREE_MB,
            quality=DEFAULT_QUALITY,
            archive_enabled=False,
            archive_root_dir=(self._config.data_dir / "archive").absolute(),
            archive_folder_template=DEFAULT_FOLDER_TEMPLATE,
            archive_name_template=DEFAULT_NAME_TEMPLATE,
            archive_delete_source=False,
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
            quality=self._load_value(
                KEY_QUALITY, defaults.quality, validate_quality
            ),
            archive_enabled=self._load_value(
                KEY_ARCHIVE_ENABLED, defaults.archive_enabled, validate_bool
            ),
            archive_root_dir=self._load_value(
                KEY_ARCHIVE_ROOT_DIR,
                defaults.archive_root_dir,
                validate_archive_root_dir,
            ),
            archive_folder_template=self._load_value(
                KEY_ARCHIVE_FOLDER_TEMPLATE,
                defaults.archive_folder_template,
                validate_archive_folder_template,
            ),
            archive_name_template=self._load_value(
                KEY_ARCHIVE_NAME_TEMPLATE,
                defaults.archive_name_template,
                validate_archive_name_template,
            ),
            archive_delete_source=self._load_value(
                KEY_ARCHIVE_DELETE_SOURCE,
                defaults.archive_delete_source,
                validate_bool,
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
        archive_root = settings.archive_root_dir
        # 兼容未显式提供归档目录的旧调用方：回退到数据目录下的默认归档目录。
        if str(archive_root or "").strip() in {"", "."}:
            archive_root = self.defaults().archive_root_dir
        normalized = AppSettings(
            ffmpeg_path=validate_ffmpeg_path(settings.ffmpeg_path),
            recordings_dir=validate_recordings_dir(settings.recordings_dir),
            monitor_interval_s=validate_monitor_interval(settings.monitor_interval_s),
            min_free_mb=validate_min_free_mb(settings.min_free_mb),
            quality=validate_quality(settings.quality),
            archive_enabled=validate_bool(settings.archive_enabled),
            archive_root_dir=validate_archive_root_dir(archive_root),
            archive_folder_template=validate_archive_folder_template(
                settings.archive_folder_template
            ),
            archive_name_template=validate_archive_name_template(
                settings.archive_name_template
            ),
            archive_delete_source=validate_bool(settings.archive_delete_source),
        )
        self._db.set_setting(KEY_FFMPEG_PATH, normalized.ffmpeg_path)
        self._db.set_setting(KEY_RECORDINGS_DIR, str(normalized.recordings_dir))
        self._db.set_setting(
            KEY_MONITOR_INTERVAL, str(normalized.monitor_interval_s)
        )
        self._db.set_setting(KEY_MIN_FREE_MB, str(normalized.min_free_mb))
        self._db.set_setting(KEY_QUALITY, normalized.quality)
        self._db.set_setting(
            KEY_ARCHIVE_ENABLED, "1" if normalized.archive_enabled else "0"
        )
        self._db.set_setting(KEY_ARCHIVE_ROOT_DIR, str(normalized.archive_root_dir))
        self._db.set_setting(
            KEY_ARCHIVE_FOLDER_TEMPLATE, normalized.archive_folder_template
        )
        self._db.set_setting(
            KEY_ARCHIVE_NAME_TEMPLATE, normalized.archive_name_template
        )
        self._db.set_setting(
            KEY_ARCHIVE_DELETE_SOURCE,
            "1" if normalized.archive_delete_source else "0",
        )
        logger.info(
            "设置已保存: ffmpeg=%s recordings=%s interval=%ss min_free=%sMB quality=%s "
            "archive=%s root=%s",
            normalized.ffmpeg_path,
            normalized.recordings_dir,
            normalized.monitor_interval_s,
            normalized.min_free_mb,
            normalized.quality,
            normalized.archive_enabled,
            normalized.archive_root_dir,
        )
        return normalized

    # ---------- Cookie（DPAPI 加密，不进入 settings 表与日志） ----------
    def load_cookie(self) -> Optional[str]:
        return self._credentials.load_cookie()

    def save_cookie(self, cookie: str) -> None:
        self._credentials.save_cookie(validate_cookie(cookie))

    def clear_cookie(self) -> None:
        self._credentials.clear_cookie()

    def cookie_status(self) -> str:
        return self._credentials.describe()

    def has_cookie(self) -> bool:
        return self._credentials.has_cookie()

    def backup_database(self, dest: Path) -> Path:
        """创建一致性备份；任何失败都会抛出异常，由调用方提示用户。"""
        return self._db.backup_to(Path(dest))

    def prepare_restore(self, backup_path: Path) -> Path:
        """登记恢复请求；实际替换数据库在下次启动时执行。"""
        from ..infrastructure.backup import prepare_restore

        return prepare_restore(Path(backup_path), self._config.data_dir)
