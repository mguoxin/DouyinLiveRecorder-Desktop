"""FFmpeg 子进程管理测试（本地 Python 桩进程，不读取真实媒体）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from dlrecorder.infrastructure.ffmpeg import (
    FFmpegProcess,
    build_record_command,
    file_size,
)

STUB = r"""
import sys, time
path, mode = sys.argv[1], sys.argv[2]
with open(path, "wb") as f:
    if mode != "empty":
        f.write(b"x" * 4096)
        f.flush()
if mode == "exit3":
    sys.exit(3)
if mode == "sleep":
    time.sleep(30)
if mode == "stdin":
    sys.stdin.readline()
"""


def _stub(tmp_path: Path) -> Path:
    path = tmp_path / "stub.py"
    path.write_text(STUB, encoding="utf-8")
    return path


def _command(stub: Path, output: Path, mode: str) -> list[str]:
    return [sys.executable, str(stub), str(output), mode]


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def test_start_and_graceful_stop_via_stdin(tmp_path):
    output = tmp_path / "out.ts"
    proc = FFmpegProcess(_command(_stub(tmp_path), output, "stdin"), stop_grace_s=3.0)

    proc.start()
    assert proc.started is True
    assert proc.pid
    _wait_until(lambda: file_size(output) > 0)

    report = proc.stop()

    assert report.exited is True
    assert report.exit_code == 0
    assert report.graceful is True
    assert report.terminated is False
    assert report.killed is False
    assert file_size(output) == 4096


def test_process_natural_nonzero_exit_is_reported(tmp_path):
    output = tmp_path / "out.ts"
    proc = FFmpegProcess(_command(_stub(tmp_path), output, "exit3"))
    proc.start()

    assert proc.wait(timeout=10) == 3
    assert proc.running is False
    report = proc.stop()
    assert report.exited is True
    assert report.exit_code == 3
    assert report.graceful is True


def test_stop_falls_back_to_terminate(tmp_path):
    output = tmp_path / "out.ts"
    proc = FFmpegProcess(
        _command(_stub(tmp_path), output, "sleep"),
        stop_grace_s=0.2,
        kill_grace_s=2.0,
    )
    proc.start()
    _wait_until(lambda: file_size(output) > 0)

    report = proc.stop()

    assert report.exited is True
    assert report.terminated is True
    assert report.graceful is False


def test_stop_before_start_is_safe():
    proc = FFmpegProcess(["unused"])

    report = proc.stop()

    assert report.exited is True
    assert report.exit_code is None
    assert proc.started is False


def test_double_start_rejected(tmp_path):
    proc = FFmpegProcess(_command(_stub(tmp_path), tmp_path / "o.ts", "stdin"))
    proc.start()
    try:
        with pytest.raises(RuntimeError):
            proc.start()
    finally:
        proc.stop()


def test_file_size_missing_returns_zero(tmp_path):
    assert file_size(tmp_path / "missing.ts") == 0


def test_build_record_command_is_stream_copy(tmp_path):
    output = tmp_path / "a.ts"
    command = build_record_command("ffmpeg", "https://example.invalid/live.flv", output)

    assert command[0] == "ffmpeg"
    assert "-c" in command and "copy" in command
    assert "-f" in command and "mpegts" in command
    assert command[-1] == str(output)
    assert "https://example.invalid/live.flv" in command
