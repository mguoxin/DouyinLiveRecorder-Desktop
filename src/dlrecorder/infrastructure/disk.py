"""磁盘空间检查。

不自动删除任何用户文件；仅在空间不足时阻止开始录制或停止正在进行的录制。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 已提示过的路径：目录被外部删除时避免每次轮询都刷同一告警。
_warned_paths: set[str] = set()


def _nearest_existing(path: Path) -> Path:
    """返回 path 或其最近的已存在上级目录。

    录制目录可能被用户或外部清理工具删除，此时按所在卷判断剩余空间即可，
    不应因为目录缺失而判定磁盘不可用。
    """
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def free_space_mb(path: Path) -> Optional[float]:
    """返回指定目录所在卷的剩余空间（MB）；无法读取时返回 None。"""
    target = _nearest_existing(Path(path))
    try:
        usage = shutil.disk_usage(str(target))
    except OSError:
        key = str(path)
        if key not in _warned_paths:
            _warned_paths.add(key)
            logger.warning("无法读取磁盘剩余空间: %s", path, exc_info=True)
        else:
            logger.debug("仍无法读取磁盘剩余空间: %s", path)
        return None
    _warned_paths.discard(str(path))
    return usage.free / (1024 * 1024)


def directory_size_bytes(path: Path, *, max_files: int = 20000) -> int:
    """统计目录占用字节数；只读遍历，失败条目跳过。

    ``max_files`` 用于避免超大目录导致统计无限期占用 UI；达到上限后返回
    已统计部分，调用方可在报告中标注为近似值。
    """
    root = Path(path)
    if not root.is_dir():
        return 0
    total = 0
    seen = 0
    stack = [str(root)]
    while stack and seen < max_files:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            seen += 1
                            if seen >= max_files:
                                break
                    except OSError:
                        continue
        except OSError:
            continue
    return total
