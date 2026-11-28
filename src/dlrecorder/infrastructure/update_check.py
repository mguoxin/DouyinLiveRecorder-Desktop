"""版本更新检查（只读、不自动下载）。

设计原则：
- 只读取用户显式配置的更新清单 URL，不内置任何远程地址、不上报任何信息。
- 仅做版本比较与展示；下载、校验、替换均需用户手动执行，避免自动升级破坏数据。
- 升级失败的回滚依赖迁移前自动备份与数据库恢复流程。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


class UpdateCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    update_available: bool
    download_url: str = ""
    notes: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        raise UpdateCheckError(f"无法识别的版本号: {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(current: str, latest: str) -> bool:
    """按数字段比较版本；缺失段按 0 处理。"""
    left = parse_version(current)
    right = parse_version(latest)
    length = max(len(left), len(right))
    left += (0,) * (length - len(left))
    right += (0,) * (length - len(right))
    return right > left


def parse_manifest(text: str, current_version: str) -> UpdateInfo:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise UpdateCheckError("更新清单不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise UpdateCheckError("更新清单结构异常")
    latest = str(payload.get("version") or "").strip()
    if not latest:
        raise UpdateCheckError("更新清单缺少 version 字段")
    download_url = str(payload.get("url") or "").strip()
    if download_url and not download_url.lower().startswith(("https://", "http://")):
        raise UpdateCheckError("下载地址必须为 http(s) URL")
    notes = str(payload.get("notes") or "").strip()[:2000]
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest,
        update_available=is_newer(current_version, latest),
        download_url=download_url,
        notes=notes,
    )


def check_for_update(
    manifest_url: str,
    current_version: str,
    *,
    timeout_s: float = 8.0,
) -> UpdateInfo:
    url = (manifest_url or "").strip()
    if not url:
        raise UpdateCheckError("未配置更新清单地址")
    if not url.lower().startswith(("https://", "http://", "file://")):
        raise UpdateCheckError("更新清单地址必须是 http(s) 或 file URL")
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - 协议已白名单
            raw = response.read(64 * 1024)
    except Exception as exc:  # noqa: BLE001 - 网络错误统一转为可读信息
        logger.warning("检查更新失败: %s", exc)
        raise UpdateCheckError(f"无法获取更新清单: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateCheckError("更新清单编码不是 UTF-8") from exc
    return parse_manifest(text, current_version)


def describe_update(info: Optional[UpdateInfo]) -> str:
    if info is None:
        return "尚未检查更新"
    if info.update_available:
        return f"发现新版本 {info.latest_version}（当前 {info.current_version}）"
    return f"当前已是最新版本（{info.current_version}）"
