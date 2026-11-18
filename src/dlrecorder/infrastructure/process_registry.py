"""FFmpeg 进程登记表。

每次启动 FFmpeg 时写入 ``(pid, 创建时间, 可执行文件路径, recording_id, room_id,
输出文件)``；正常结束时删除对应条目。应用异常退出后，下次启动可据此识别遗留
进程，并在通过身份校验后安全终止。

设计取舍：
- 存放在数据目录的 JSON 文件而非数据库，避免为了运行时状态改动用户数据库
  Schema；登记表丢失只影响孤儿进程识别，不影响录制数据。
- 采用"写临时文件再原子替换"的方式，进程被杀时不会留下半截 JSON。
- 文件损坏按空表处理并记录日志，不阻止应用启动。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ProcessRecord:
    """一条 FFmpeg 进程登记信息。"""

    pid: int
    create_time: int
    exe_path: str
    recording_id: int
    room_id: int
    file_path: str
    started_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Optional["ProcessRecord"]:
        try:
            return cls(
                pid=int(data["pid"]),
                create_time=int(data.get("create_time", 0)),
                exe_path=str(data.get("exe_path", "")),
                recording_id=int(data.get("recording_id", 0)),
                room_id=int(data.get("room_id", 0)),
                file_path=str(data.get("file_path", "")),
                started_at=str(data.get("started_at", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


class ProcessRegistry:
    """进程登记表的读写封装（线程安全）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> list[ProcessRecord]:
        with self._lock:
            return self._read()

    def add(self, record: ProcessRecord) -> None:
        """登记一个进程；同一 pid 的旧条目会被覆盖。"""
        with self._lock:
            items = [r for r in self._read() if r.pid != record.pid]
            items.append(record)
            self._write(items)

    def remove(
        self,
        *,
        pid: Optional[int] = None,
        recording_id: Optional[int] = None,
    ) -> None:
        """按 pid 或 recording_id 删除条目；两者都不给时不做任何修改。"""
        if pid is None and recording_id is None:
            return
        with self._lock:
            items = [
                r
                for r in self._read()
                if not (
                    (pid is not None and r.pid == pid)
                    or (
                        recording_id is not None
                        and r.recording_id == recording_id
                    )
                )
            ]
            self._write(items)

    def replace(self, records: list[ProcessRecord]) -> None:
        with self._lock:
            self._write(list(records))

    def clear(self) -> None:
        self.replace([])

    # ---------- 内部 ----------
    def _read(self) -> list[ProcessRecord]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as e:
            logger.warning("读取 FFmpeg 进程登记表失败: %s", e)
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("FFmpeg 进程登记表内容损坏，按空表处理: %s", self._path)
            return []
        raw_items = payload.get("processes") if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            logger.warning("FFmpeg 进程登记表结构异常，按空表处理: %s", self._path)
            return []
        records: list[ProcessRecord] = []
        for item in raw_items:
            if isinstance(item, dict):
                record = ProcessRecord.from_dict(item)
                if record is not None:
                    records.append(record)
        return records

    def _write(self, records: list[ProcessRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _now_iso(),
            "processes": [r.to_dict() for r in records],
        }
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning("写入 FFmpeg 进程登记表失败: %s", e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


__all__ = ["ProcessRecord", "ProcessRegistry"]
