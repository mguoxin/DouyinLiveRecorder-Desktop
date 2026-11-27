"""结构化日志配置。日志写入 data/logs/，限制大小，避免无限增长。"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .redaction import RedactingFormatter


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    # 文件与控制台统一走脱敏格式化器：路径、URL、Cookie/Token 在落盘前移除，
    # traceback 中的源码绝对路径同样被覆盖。
    data_dir = Path(log_dir).parent
    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        user_home=os.path.expanduser("~"),
        data_dir=str(data_dir),
    )
    # 单个文件最大 5MB，保留 3 个备份，避免日志无限增长
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(logging.WARNING)  # 控制台只输出警告及以上
    root.addHandler(ch)
    # 第三方 HTTP 客户端默认按 INFO 记录完整请求 URL（含签名参数），
    # 对排查无用且会撑大日志，统一降到 WARNING。
    for noisy in ("httpx", "httpcore", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
