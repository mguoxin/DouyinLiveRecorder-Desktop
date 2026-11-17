"""基础配置管理（环境变量 + 默认值，不写注册表）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AppConfig:
    data_dir: Path
    db_path: Path
    log_dir: Path
    recordings_dir: Path
    monitor_interval_s: float = 120.0  # 候选默认值，待测试确定
    state_expire_threshold_s: float = 600.0
    max_stream_url_len: int = 0  # 不持久化流地址

    @classmethod
    def from_base_dir(cls, base_dir: Path) -> "AppConfig":
        data_dir = base_dir / "data"
        return cls(
            data_dir=data_dir,
            db_path=data_dir / "app.db",
            log_dir=data_dir / "logs",
            recordings_dir=data_dir / "recordings",
        )
