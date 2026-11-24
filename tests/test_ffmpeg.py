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
    redact_stream_text,
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
if mode == "stderr":
    sys.stderr.write(
        "https://cdn.example/live.flv?token=SECRET_TOKEN&msToken=SECRET_MS\n"
    )
    sys.stderr.write("Cookie: sessionid=SECRET_COOKIE\n")
    for i in range(2000):
        sys.stderr.write("noise %04d %s\n" % (i, "y" * 80))
    sys.stderr.write("retry https://cdn.example/live.flv?token=SECRET_TOKEN\n")
    sys.stderr.flush()
    open(path + ".done", "w").close()
    time.sleep(30)
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


def test_stop_falls_back_to_kill_when_terminate_fails(tmp_path, monkeypatch):
    """受控验证 kill 回退：terminate 无效时必须真正 kill 并如实报告。

    Windows 的 terminate 是 TerminateProcess，正常进程无法忽略，因此这里
    屏蔽 terminate 来构造受控场景，验证 finally 兜底路径确实生效。
    """
    output = tmp_path / "out.ts"
    proc = FFmpegProcess(
        _command(_stub(tmp_path), output, "sleep"),
        stop_grace_s=0.2,
        kill_grace_s=2.0,
    )
    proc.start()
    _wait_until(lambda: file_size(output) > 0)

    assert proc._proc is not None
    monkeypatch.setattr(proc._proc, "terminate", lambda: None)
    report = proc.stop()

    assert report.terminated is True
    assert report.killed is True
    assert report.exited is True
    assert proc._proc.poll() is not None


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


def test_redact_stream_text_masks_url_and_credentials():
    url = (
        "https://cdn.example/live.flv?expire=1700000000"
        "&sign=UNIQUE_SIGN&a_bogus=UNIQUE_BOGUS"
    )
    text = (
        f"Error opening input {url}\n"
        "Cookie: sessionid=UNIQUE_COOKIE\n"
        "Authorization: Bearer UNIQUE_TOKEN\n"
        "redirect https://other.example/x?msToken=UNIQUE_MS&x=1"
    )

    redacted = redact_stream_text(text, url)

    assert "<stream-url>" in redacted
    assert "UNIQUE_SIGN" not in redacted
    assert "UNIQUE_BOGUS" not in redacted
    assert "UNIQUE_COOKIE" not in redacted
    assert "UNIQUE_TOKEN" not in redacted
    assert "msToken=<redacted>" in redacted
    assert "x=1" in redacted


def test_stderr_is_drained_redacted_and_bounded(tmp_path):
    """stderr 持续排空不阻塞进程；落盘内容脱敏且按容量轮转。"""
    output = tmp_path / "out.ts"
    diagnostics = tmp_path / "logs"
    proc = FFmpegProcess(
        _command(_stub(tmp_path), output, "stderr"),
        stop_grace_s=0.2,
        kill_grace_s=2.0,
        diagnostics_dir=diagnostics,
        diagnostics_max_bytes=8192,
    )
    log_path = diagnostics / "ffmpeg-stderr.log"

    def _log_text() -> str:
        chunks: list[str] = []
        for path in (log_path.with_name(log_path.name + ".1"), log_path):
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return "".join(chunks)

    proc.start()
    try:
        # 子进程写完 stderr 后才创建完成标记；能等到标记说明管道未被写满阻塞。
        _wait_until(lambda: (tmp_path / "out.ts.done").exists(), timeout=15)
        assert proc.running is True
    finally:
        proc.stop()
        proc.close_streams()

    _wait_until(lambda: "noise 1999" in _log_text(), timeout=5.0)
    text = _log_text()
    assert "token=<redacted>" in text
    assert "SECRET_TOKEN" not in text
    assert "SECRET_MS" not in text
    assert "SECRET_COOKIE" not in text
    assert (diagnostics / "ffmpeg-stderr.log.1").exists()
    assert log_path.stat().st_size <= 8400
    assert proc.stderr_tail()
    assert proc._stderr_thread is None


def test_build_record_command_is_stream_copy(tmp_path):
    output = tmp_path / "a.ts"
    command = build_record_command("ffmpeg", "https://example.invalid/live.flv", output)

    assert command[0] == "ffmpeg"
    assert "-c" in command and "copy" in command
    assert "-f" in command and "mpegts" in command
    assert command[-1] == str(output)
    assert "https://example.invalid/live.flv" in command


def test_build_record_command_enables_input_reconnect(tmp_path):
    command = build_record_command("ffmpeg", "https://example.invalid/live.flv", tmp_path / "a.ts")

    # 重连与超时必须是输入侧参数，位于 -i 之前。
    input_index = command.index("-i")
    for option in ("-rw_timeout", "-reconnect", "-reconnect_delay_max"):
        assert option in command
        assert command.index(option) < input_index
    assert command[command.index("-rw_timeout") + 1] == "15000000"
    assert command[command.index("-reconnect") + 1] == "1"
    # 不使用 EOF 重连，避免直播结束后进程反复拉起。
    assert "-reconnect_at_eof" not in command
    assert "-reconnect_streamed" not in command


def test_build_record_command_skips_http_options_for_local_file(tmp_path):
    # -reconnect 对本地文件输入无效，FFmpeg 会以 "Option not found" 失败。
    command = build_record_command("ffmpeg", str(tmp_path / "local.flv"), tmp_path / "a.ts")

    assert "-reconnect" not in command
    assert "-rw_timeout" not in command


def test_build_record_command_can_disable_reconnect(tmp_path):
    command = build_record_command(
        "ffmpeg", "https://example.invalid/live.flv", tmp_path / "a.ts", reconnect=False
    )

    # 关闭的是重连；读取超时与缓冲参数与重连无关，仍然保留。
    assert "-reconnect" not in command
    assert "-reconnect_delay_max" not in command
    assert "-rw_timeout" in command
    assert command.index("-rw_timeout") < command.index("-i")
