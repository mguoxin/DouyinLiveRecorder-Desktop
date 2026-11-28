"""录制领域模型。

仅表达录制状态，不依赖 Qt、数据库或 FFmpeg。
"""
from __future__ import annotations

import enum


class RecordingStatus(str, enum.Enum):
    """录制记录状态。

    ``RECORDING`` 为进行中；``COMPLETED`` 表示进程正常结束且文件有内容；
    ``FAILED`` 表示进程异常退出、输出为空或输出停滞；``INTERRUPTED`` 表示
    程序异常退出后启动恢复时发现的历史遗留记录。
    """

    RECORDING = "recording"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    def __str__(self) -> str:  # 便于数据库与日志输出
        return self.value


class ArchiveStatus(str, enum.Enum):
    """录制文件归档状态机取值。

    ``NONE`` 表示未启用或从未尝试；``PENDING`` 已入队；``COPYING`` 正在复制；
    ``VERIFYING`` 复制完成待校验；``ARCHIVED`` 目标已验证；``FAILED`` 可重试
    失败；``SKIPPED`` 目标已存在且内容一致，去重跳过。
    """

    NONE = "none"
    PENDING = "pending"
    COPYING = "copying"
    VERIFYING = "verifying"
    ARCHIVED = "archived"
    FAILED = "failed"
    SKIPPED = "skipped"

    def __str__(self) -> str:
        return self.value
