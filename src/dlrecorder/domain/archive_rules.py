"""归档规则：目录/文件命名模板展开与安全路径约束。

不依赖 Qt、数据库或文件系统；调用方负责最终磁盘操作。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,10}$")

DEFAULT_FOLDER_TEMPLATE = "{anchor}/{date}"
DEFAULT_NAME_TEMPLATE = "{date}_{time}_{anchor}_{web_rid}"
MAX_TEMPLATE_LEN = 160
MAX_COMPONENT_LEN = 60
MAX_NAME_LEN = 80


class ArchiveRuleError(ValueError):
    """模板或归档路径不安全。"""


@dataclass(frozen=True)
class ArchiveSettings:
    """归档配置；``root_dir`` 为空表示未配置。"""

    enabled: bool = False
    root_dir: Optional[Path] = None
    folder_template: str = DEFAULT_FOLDER_TEMPLATE
    name_template: str = DEFAULT_NAME_TEMPLATE
    delete_source: bool = False


def safe_component(text: str, limit: int = MAX_COMPONENT_LEN) -> str:
    """清洗为可用的文件名/目录名片段。"""
    cleaned = _INVALID_CHARS.sub("_", text or "").strip(" .")
    return (cleaned or "未命名")[:limit]


def expand_template(
    template: str,
    values: Mapping[str, object],
) -> tuple[str, tuple[str, ...]]:
    """展开白名单占位符，返回 ``(文本, 未知占位符列表)``。

    未知占位符原样保留，便于在日志中诊断模板拼写错误；已知但值为空时展开
    为空字符串。
    """
    unknown: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            unknown.append(key)
            return match.group(0)
        value = values[key]
        return "" if value is None else str(value)

    expanded = _PLACEHOLDER.sub(replace, template)
    return expanded, tuple(dict.fromkeys(unknown))


def validate_template(value: str, *, field: str) -> str:
    """校验模板：非空、长度受限、不允许绝对路径或 ``..`` 片段。"""
    text = (value or "").strip()
    if not text:
        raise ArchiveRuleError(f"{field}不能为空")
    if len(text) > MAX_TEMPLATE_LEN:
        raise ArchiveRuleError(f"{field}不能超过 {MAX_TEMPLATE_LEN} 个字符")
    if any(ord(ch) < 32 for ch in text):
        raise ArchiveRuleError(f"{field}不能包含控制字符")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ArchiveRuleError(f"{field}必须是相对路径模板")
    segments = [part for part in normalized.split("/") if part]
    if not segments:
        raise ArchiveRuleError(f"{field}必须包含有效内容")
    if any(part.strip() == ".." for part in segments):
        raise ArchiveRuleError(f"{field}不能包含上级目录引用")
    return text


def build_relative_path(
    settings: ArchiveSettings,
    values: Mapping[str, object],
    extension: str,
) -> tuple[Path, tuple[str, ...]]:
    """根据模板生成归档相对路径，并校验每一段目录与文件名。

    返回 ``(相对路径, 未知占位符)``；任何目录穿越尝试都会抛出
    ``ArchiveRuleError``。
    """
    folder_text, folder_unknown = expand_template(settings.folder_template, values)
    name_text, name_unknown = expand_template(settings.name_template, values)

    normalized_folder = folder_text.replace("\\", "/")
    if normalized_folder.startswith("/") or re.match(r"^[A-Za-z]:", normalized_folder):
        raise ArchiveRuleError("归档目录模板不能是绝对路径")

    segments: list[str] = []
    for raw in normalized_folder.split("/"):
        part = raw.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise ArchiveRuleError("归档目录模板不能包含上级目录引用")
        segments.append(safe_component(part))

    safe_name = safe_component(name_text, MAX_NAME_LEN)
    suffix = extension if extension.startswith(".") else f".{extension}"
    if not _SAFE_EXTENSION.match(suffix):
        suffix = ".ts"

    relative = Path(*segments, f"{safe_name}{suffix}") if segments else Path(
        f"{safe_name}{suffix}"
    )
    if relative.is_absolute() or ".." in relative.parts:
        raise ArchiveRuleError("归档目标路径不安全")
    return relative, tuple(dict.fromkeys(folder_unknown + name_unknown))


def ensure_within_root(root: Path, target: Path) -> Path:
    """确保 ``target`` 位于 ``root`` 之内，返回解析后的目标路径。"""
    resolved_root = Path(root).resolve()
    resolved_target = Path(target).resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ArchiveRuleError(f"归档目标超出归档根目录: {target}") from exc
    return resolved_target
