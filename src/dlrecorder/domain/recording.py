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
