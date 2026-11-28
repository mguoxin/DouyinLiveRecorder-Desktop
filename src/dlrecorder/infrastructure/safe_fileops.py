"""安全文件操作：哈希校验、复制后校验、原子改名、空间检查与回收站删除。

设计约束：
- 复制一律先写同目录 ``.part`` 临时文件，校验通过后原子改名，绝不覆盖目标。
- 跨磁盘不使用 ``os.replace`` 移动源文件；由调用方执行“复制→校验→删除源”。
- 删除录制文件只通过 Windows 回收站，不静默永久删除。
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import shutil
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
DEFAULT_SAFETY_FACTOR = 1.05


class SafeFileOpsError(RuntimeError):
    """安全文件操作失败；调用方必须保留源文件并如实报告原因。"""


class InsufficientSpaceError(SafeFileOpsError):
    """目标卷剩余空间不足以完成复制。"""


def sha256_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    """计算文件 SHA-256；读取失败抛出 ``OSError``。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def same_volume(first: Path, second: Path) -> bool:
    """判断两个路径是否位于同一个卷（盘符）。"""
    if os.name == "nt":
        return os.path.splitdrive(str(Path(first).resolve()))[0].lower() == (
            os.path.splitdrive(str(Path(second).resolve()))[0].lower()
        )
    return Path(first).stat().st_dev == Path(second).stat().st_dev


def ensure_free_space(
    directory: Path,
    required_bytes: int,
    *,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
) -> None:
    """检查目标目录所在卷的剩余空间；不足时抛出 ``InsufficientSpaceError``。

    目录不存在时按最近存在的上级目录判断，不修改磁盘。
    """
    if required_bytes <= 0:
        return
    target = Path(directory)
    while not target.exists():
        parent = target.parent
        if parent == target:
            break
        target = parent
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        raise SafeFileOpsError(f"无法读取目标磁盘剩余空间: {exc}") from exc
    needed = int(required_bytes * max(1.0, safety_factor))
    if usage.free < needed:
        raise InsufficientSpaceError(
            f"目标磁盘剩余空间不足：需要约 {needed // (1024 * 1024)} MB，"
            f"可用 {usage.free // (1024 * 1024)} MB"
        )


def copy_file_verified(
    source: Path,
    target: Path,
    *,
    expected_sha256: Optional[str] = None,
    progress: Optional[Callable[[int], None]] = None,
    chunk_size: int = CHUNK_SIZE,
) -> str:
    """复制文件并校验内容，返回源文件 SHA-256。

    目标先写 ``<name>.part``；大小与哈希均一致后原子改名。任一步失败都会
    删除临时文件、保留源文件与既有目标文件，不产生半成品。
    """
    source = Path(source)
    target = Path(target)
    if not source.is_file():
        raise SafeFileOpsError(f"源文件不存在: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.part")

    digest = hashlib.sha256()
    copied = 0
    try:
        with open(source, "rb") as src_handle, open(temp, "wb") as dst_handle:
            while True:
                block = src_handle.read(chunk_size)
                if not block:
                    break
                digest.update(block)
                dst_handle.write(block)
                copied += len(block)
                if progress is not None:
                    progress(copied)
            dst_handle.flush()
            os.fsync(dst_handle.fileno())
        source_sha = digest.hexdigest()

        if expected_sha256 is not None and source_sha != expected_sha256:
            raise SafeFileOpsError("源文件在归档前发生变化，已取消本次归档")

        target_sha = sha256_file(temp, chunk_size=chunk_size)
        if target_sha != source_sha:
            raise SafeFileOpsError("复制校验失败：目标文件哈希与源文件不一致")
        if temp.stat().st_size != copied:
            raise SafeFileOpsError("复制校验失败：目标文件大小与已复制字节数不一致")

        os.replace(temp, target)
        return source_sha
    except Exception:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            logger.warning("清理未完成的临时文件失败: %s", temp, exc_info=True)
        raise


def resolve_conflict_path(
    target: Path,
    source_sha256: str,
    *,
    max_suffix: int = 100,
) -> tuple[Path, bool]:
    """处理同名目标。

    返回 ``(最终路径, 是否已存在同内容副本)``；内容一致时调用方应跳过复制，
    内容不一致则追加 ``_1.._n`` 序号，绝不覆盖既有文件。
    """
    target = Path(target)
    if not target.exists():
        return target, False
    if target.is_file() and sha256_file(target) == source_sha256:
        return target, True
    for index in range(1, max_suffix + 1):
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        if not candidate.exists():
            return candidate, False
        if candidate.is_file() and sha256_file(candidate) == source_sha256:
            return candidate, True
    raise SafeFileOpsError(f"同名文件过多，无法安全确定归档路径: {target.name}")


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMMKDIR = 0x0200
_FOF_NOERRORUI = 0x0400


def recycle_paths(paths: Sequence[Path]) -> None:
    """把文件移入 Windows 回收站；失败抛出 ``SafeFileOpsError``。

    非 Windows 平台或回收站不可用时拒绝执行，由调用方提示用户，绝不改为
    永久删除。
    """
    if os.name != "nt":
        raise SafeFileOpsError("当前平台不支持回收站删除")
    items = [Path(path) for path in paths]
    if not items:
        return
    for item in items:
        if not item.exists():
            raise SafeFileOpsError(f"待删除文件不存在: {item}")
        if item.is_dir():
            raise SafeFileOpsError(f"拒绝删除目录: {item}")

    joined = "\0".join(str(item.resolve()) for item in items) + "\0\0"
    operation = _SHFILEOPSTRUCTW(
        None,
        _FO_DELETE,
        joined,
        None,
        _FOF_ALLOWUNDO
        | _FOF_NOCONFIRMATION
        | _FOF_SILENT
        | _FOF_NOERRORUI
        | _FOF_NOCONFIRMMKDIR,
        False,
        None,
        None,
    )
    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except (AttributeError, OSError) as exc:
        raise SafeFileOpsError(f"回收站操作不可用: {exc}") from exc
    if result != 0 or operation.fAnyOperationsAborted:
        raise SafeFileOpsError(
            f"移入回收站失败（代码 {result}，已中止={bool(operation.fAnyOperationsAborted)}）"
        )
    remaining = [str(item) for item in items if item.exists()]
    if remaining:
        raise SafeFileOpsError(f"文件未能移入回收站: {', '.join(remaining)}")
