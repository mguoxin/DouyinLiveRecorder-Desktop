"""遗留 FFmpeg 进程的安全清理。

清理前必须通过身份校验，绝不"扫描所有 ffmpeg 进程统一结束"：

1. 进程必须仍存在，且**创建时间**与登记表一致（PID 会被系统复用，单凭 PID 不可信）；
2. 进程可执行文件名必须是 ffmpeg；
3. 登记了完整路径时，路径必须与登记值一致。

任一条件不满足即视为"无法确认归属"，只丢弃或保留登记，不结束进程。
"""
from __future__ import annotations

import logging
import ntpath
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .process_registry import ProcessRecord, ProcessRegistry
from .winproc import ProcessIdentity, is_process_alive, query_process, terminate_process

logger = logging.getLogger(__name__)

_Probe = Callable[[int], Optional[ProcessIdentity]]
_IsAlive = Callable[[int], bool]
_Terminate = Callable[[int], bool]

_EXPECTED_EXE_NAME = "ffmpeg.exe"


@dataclass(frozen=True)
class OrphanCleanupReport:
    """一次启动清理的结果。"""

    checked: int = 0
    # 已确认身份并成功终止的登记项（调用方据此把对应录制标记为 interrupted）
    terminated: tuple[ProcessRecord, ...] = ()
    # 已不存在或身份校验不通过、仅移除登记的条目数
    skipped: int = 0
    # 身份匹配但未能终止，保留登记待下次启动重试
    failed: tuple[ProcessRecord, ...] = ()

    @property
    def terminated_count(self) -> int:
        return len(self.terminated)


def _same_path(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(
        ntpath.normpath(right)
    )


def confirm_ownership(
    record: ProcessRecord, identity: ProcessIdentity
) -> tuple[bool, str]:
    """判断进程是否确实由本应用启动。返回 (是否确认, 未确认原因)。"""
    if record.create_time <= 0:
        return False, "登记缺少创建时间，无法确认归属"
    if identity.create_time != record.create_time:
        return False, "创建时间不一致（PID 可能已被复用）"
    actual_name = ntpath.basename(identity.exe_path or "").lower()
    if actual_name != _EXPECTED_EXE_NAME:
        return False, f"可执行文件不是 FFmpeg: {identity.exe_path}"
    if record.exe_path and not _same_path(record.exe_path, identity.exe_path):
        return False, f"可执行文件路径不一致: {identity.exe_path}"
    return True, ""


def cleanup_orphans(
    registry: ProcessRegistry,
    *,
    probe: _Probe = query_process,
    terminate: _Terminate = terminate_process,
    alive: _IsAlive = is_process_alive,
    wait_s: float = 5.0,
    poll_interval_s: float = 0.1,
) -> OrphanCleanupReport:
    """清理登记表中确认属于本应用的遗留 FFmpeg 进程。"""
    records = registry.entries()
    if not records:
        registry.clear()
        return OrphanCleanupReport()

    terminated: list[ProcessRecord] = []
    failed: list[ProcessRecord] = []
    skipped = 0
    kept: list[ProcessRecord] = []

    for record in records:
        identity = probe(record.pid)
        if identity is None:
            logger.info(
                "遗留 FFmpeg 已不存在 pid=%s recording_id=%s，移除登记",
                record.pid,
                record.recording_id,
            )
            skipped += 1
            continue
        owned, reason = confirm_ownership(record, identity)
        if not owned:
            logger.warning(
                "发现可疑进程但无法确认归属，不结束 pid=%s：%s", record.pid, reason
            )
            skipped += 1
            continue

        logger.warning(
            "清理遗留 FFmpeg pid=%s recording_id=%s file=%s",
            record.pid,
            record.recording_id,
            record.file_path,
        )
        terminate(record.pid)
        deadline = time.monotonic() + max(0.0, wait_s)
        while time.monotonic() < deadline:
            if not alive(record.pid):
                break
            time.sleep(max(0.01, poll_interval_s))
        if alive(record.pid):
            logger.error("遗留 FFmpeg 未能终止，保留登记待下次启动重试 pid=%s", record.pid)
            failed.append(record)
            kept.append(record)
        else:
            terminated.append(record)

    registry.replace(kept)
    return OrphanCleanupReport(
        checked=len(records),
        terminated=tuple(terminated),
        skipped=skipped,
        failed=tuple(failed),
    )


__all__ = [
    "OrphanCleanupReport",
    "cleanup_orphans",
    "confirm_ownership",
]
