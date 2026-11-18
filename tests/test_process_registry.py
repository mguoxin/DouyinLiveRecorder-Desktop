"""进程登记表与遗留进程清理的单元测试。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from dlrecorder.infrastructure.orphan import cleanup_orphans, confirm_ownership
from dlrecorder.infrastructure.process_registry import ProcessRecord, ProcessRegistry
from dlrecorder.infrastructure.winproc import KillOnCloseJob, ProcessIdentity


def _record(
    pid: int = 100,
    create_time: int = 123456,
    exe: str = r"C:\ff\ffmpeg.exe",
    recording_id: int = 7,
) -> ProcessRecord:
    return ProcessRecord(
        pid=pid,
        create_time=create_time,
        exe_path=exe,
        recording_id=recording_id,
        room_id=3,
        file_path=r"C:\rec\a.ts",
        started_at="2026-11-18T10:00:00Z",
    )


def test_registry_add_remove_roundtrip(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path / "runtime" / "ffmpeg_processes.json")
    registry.add(_record(pid=100))
    registry.add(_record(pid=200, recording_id=8))
    entries = registry.entries()
    assert [e.pid for e in entries] == [100, 200]

    registry.remove(recording_id=7)
    assert [e.pid for e in registry.entries()] == [200]

    registry.remove(pid=200)
    assert registry.entries() == []


def test_registry_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "ffmpeg_processes.json"
    path.write_text("{not json", encoding="utf-8")
    registry = ProcessRegistry(path)
    assert registry.entries() == []
    registry.add(_record())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["processes"][0]["pid"] == 100


def test_registry_add_replaces_same_pid(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path / "reg.json")
    registry.add(_record(pid=100, recording_id=7))
    registry.add(_record(pid=100, recording_id=9))
    entries = registry.entries()
    assert len(entries) == 1
    assert entries[0].recording_id == 9


def _identity(pid: int, create_time: int, exe: str) -> ProcessIdentity:
    return ProcessIdentity(pid=pid, create_time=create_time, exe_path=exe)


def test_confirm_ownership_accepts_matching_identity() -> None:
    ok, reason = confirm_ownership(_record(), _identity(100, 123456, r"C:\ff\ffmpeg.exe"))
    assert ok, reason


@pytest.mark.parametrize(
    "identity, expected_reason",
    [
        (_identity(100, 999999, r"C:\ff\ffmpeg.exe"), "创建时间不一致"),
        (_identity(100, 123456, r"C:\other\python.exe"), "不是 FFmpeg"),
        (_identity(100, 123456, r"D:\other\ffmpeg.exe"), "路径不一致"),
    ],
)
def test_confirm_ownership_rejects_untrusted_process(
    identity: ProcessIdentity, expected_reason: str
) -> None:
    ok, reason = confirm_ownership(_record(), identity)
    assert not ok
    assert expected_reason in reason


def test_confirm_ownership_requires_create_time() -> None:
    ok, reason = confirm_ownership(
        _record(create_time=0), _identity(100, 123456, r"C:\ff\ffmpeg.exe")
    )
    assert not ok
    assert "创建时间" in reason


def test_cleanup_terminates_owned_process(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path / "reg.json")
    registry.add(_record())
    state = {"alive": True}
    killed: list[int] = []

    def probe(pid: int):
        return _identity(pid, 123456, r"C:\ff\ffmpeg.exe")

    def terminate(pid: int) -> bool:
        killed.append(pid)
        state["alive"] = False
        return True

    report = cleanup_orphans(
        registry,
        probe=probe,
        terminate=terminate,
        alive=lambda pid: state["alive"],
        wait_s=0.2,
    )
    assert killed == [100]
    assert [r.pid for r in report.terminated] == [100]
    assert registry.entries() == []


def test_cleanup_skips_reused_pid(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path / "reg.json")
    registry.add(_record())
    killed: list[int] = []

    report = cleanup_orphans(
        registry,
        probe=lambda pid: _identity(pid, 42, r"C:\ff\ffmpeg.exe"),
        terminate=killed.append,
        alive=lambda pid: True,
        wait_s=0.01,
    )
    assert killed == []
    assert report.skipped == 1
    assert registry.entries() == []


def test_cleanup_skips_foreign_executable(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path / "reg.json")
    registry.add(_record())
    killed: list[int] = []

    report = cleanup_orphans(
        registry,
        probe=lambda pid: _identity(pid, 123456, r"C:\Windows\system32\cmd.exe"),
        terminate=killed.append,
        alive=lambda pid: True,
        wait_s=0.01,
    )
    assert killed == []
    assert report.skipped == 1


def test_cleanup_skips_missing_process(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path / "reg.json")
    registry.add(_record())
    report = cleanup_orphans(
        registry,
        probe=lambda pid: None,
        terminate=lambda pid: pytest.fail("不应终止不存在的进程"),
        alive=lambda pid: False,
        wait_s=0.01,
    )
    assert report.skipped == 1
    assert registry.entries() == []


def test_cleanup_keeps_entry_when_terminate_fails(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path / "reg.json")
    registry.add(_record())
    report = cleanup_orphans(
        registry,
        probe=lambda pid: _identity(pid, 123456, r"C:\ff\ffmpeg.exe"),
        terminate=lambda pid: False,
        alive=lambda pid: True,
        wait_s=0.05,
        poll_interval_s=0.01,
    )
    assert [r.pid for r in report.failed] == [100]
    assert [e.pid for e in registry.entries()] == [100]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_job_object_kills_child_when_closed() -> None:
    import subprocess

    job = KillOnCloseJob()
    assert job.active
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not job.assign(proc.pid):
            pytest.skip("当前进程处于受限 Job 中，环境不允许再分配 Job（真实运行不受影响）")
        time.sleep(0.2)
        job.close()
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
