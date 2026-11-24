"""真实 FFmpeg 集成测试：使用项目内置 FFmpeg 与本地生成媒体，不访问网络。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dlrecorder.application.recording_service import RecordingService, StreamRefresh
from dlrecorder.infrastructure.db import Database
from dlrecorder.infrastructure.ffmpeg import find_local_ffmpeg, probe_ffmpeg


def _ffmpeg_or_skip() -> str:
    path = find_local_ffmpeg()
    if path == "ffmpeg":
        pytest.skip("未安装项目内置 FFmpeg")
    return path


def _wait_for(qt_app, predicate, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


@pytest.fixture(scope="module")
def sample_media(tmp_path_factory) -> Path:
    """生成测试用媒体。

    使用 FFmpeg 内置的 MPEG-4 Part 2 / AAC 编码器（LGPL 构建可用），
    输出 MPEG-TS，与录制实际采用的封装一致；不依赖 GPL 的 libx264。
    """
    ffmpeg = _ffmpeg_or_skip()
    path = tmp_path_factory.mktemp("media") / "sample.ts"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000",
            "-t",
            "12",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(path),
        ],
        capture_output=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert path.stat().st_size > 0
    return path


def test_probe_bundled_ffmpeg():
    path = _ffmpeg_or_skip()
    ok, message = probe_ffmpeg(path)
    assert ok is True
    assert "ffmpeg version" in message.lower()


def test_real_ffmpeg_records_local_media(tmp_path, qt_app, sample_media):
    """真实 FFmpeg 进程录制本地媒体：启动、写出文件、正常退出并标记完成。"""
    ffmpeg = _ffmpeg_or_skip()
    db = Database(tmp_path / "app.db")
    room_id = db.add_room("real-ffmpeg", "真实FFmpeg测试")
    service = RecordingService(
        db,
        output_dir=tmp_path / "recordings",
        ffmpeg_path=ffmpeg,
        min_free_mb=0,
        poll_interval_s=0.1,
        stall_timeout_s=60,
        stop_grace_s=5,
        shutdown_timeout_s=30,
    )
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    try:
        service.start_recording(room_id, str(sample_media))
        _wait_for(qt_app, lambda: bool(started))
        _wait_for(qt_app, lambda: bool(finished))

        snapshot = finished[0]
        # 状态 completed 已隐含 FFmpeg 退出码为 0 且输出非空。
        assert snapshot["status"] == "completed"
        assert snapshot["file_size"] > 0
        assert Path(snapshot["file_path"]).stat().st_size > 0

        row = db.get_recording(snapshot["recording_id"])
        assert row["status"] == "completed"
        assert row["file_size"] > 0
        assert row["session_id"] is None
    finally:
        service.shutdown()
        db.close()


class _SlowMediaHandler(BaseHTTPRequestHandler):
    """限速本地媒体服务，模拟持续直播流，便于验证真实停止流程。"""

    media: bytes = b""
    total_duration_s: float = 8.0
    loop: bool = False

    def do_GET(self) -> None:  # noqa: N802 - http.server 接口
        chunk = 16384
        chunks = max(1, (len(self.media) + chunk - 1) // chunk)
        delay = self.total_duration_s / chunks
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            if not self.loop:
                self.send_header("Content-Length", str(len(self.media)))
            self.end_headers()
            while True:
                for offset in range(0, len(self.media), chunk):
                    self.wfile.write(self.media[offset : offset + chunk])
                    self.wfile.flush()
                    time.sleep(delay)
                if not self.loop:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            # FFmpeg 停止录制会主动断开连接，属于预期行为。
            pass

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def test_real_ffmpeg_stop_from_live_like_stream(tmp_path, qt_app, sample_media):
    """真实 FFmpeg 从限速 HTTP 流录制，手动停止后保留非空文件并正常收尾。"""
    ffmpeg = _ffmpeg_or_skip()
    handler = type(
        "SlowMediaHandler",
        (_SlowMediaHandler,),
        {"media": sample_media.read_bytes()},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    db = Database(tmp_path / "app.db")
    room_id = db.add_room("real-ffmpeg-stop", "真实FFmpeg停止测试")
    service = RecordingService(
        db,
        output_dir=tmp_path / "recordings",
        ffmpeg_path=ffmpeg,
        min_free_mb=0,
        poll_interval_s=0.1,
        stall_timeout_s=60,
        stop_grace_s=5,
        shutdown_timeout_s=30,
    )
    started: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/sample.flv"
        service.start_recording(room_id, url)
        _wait_for(qt_app, lambda: bool(started))
        output_path = Path(started[0]["file_path"])
        _wait_for(
            qt_app,
            lambda: output_path.exists() and output_path.stat().st_size > 4096,
            timeout=30,
        )

        service.stop_recording(room_id)
        _wait_for(qt_app, lambda: bool(finished), timeout=30)

        snapshot = finished[0]
        assert snapshot["status"] == "completed"
        assert snapshot["file_size"] > 0
        assert db.get_recording(snapshot["recording_id"])["status"] == "completed"
    finally:
        service.shutdown()
        db.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_ffmpeg_reconnects_after_forced_kill(tmp_path, qt_app, sample_media):
    """受控强杀 FFmpeg：应进入重连并真正恢复录制，同时给出结构化事件。"""
    ffmpeg = _ffmpeg_or_skip()
    handler = type(
        "SlowMediaHandler",
        (_SlowMediaHandler,),
        {"media": sample_media.read_bytes(), "loop": True},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/sample.flv"

    db = Database(tmp_path / "app.db")
    room_id = db.add_room("real-ffmpeg-kill", "真实强杀续录测试")
    service = RecordingService(
        db,
        output_dir=tmp_path / "recordings",
        ffmpeg_path=ffmpeg,
        min_free_mb=0,
        poll_interval_s=0.1,
        stall_timeout_s=60,
        stop_grace_s=5,
        shutdown_timeout_s=30,
        stream_resolver=lambda web_rid: StreamRefresh(live=True, stream_url=url),
        reconnect_delays_s=(0.2,),
    )
    started: list[dict] = []
    reconnecting: list[dict] = []
    recovered: list[dict] = []
    finished: list[dict] = []
    service.recording_started.connect(lambda rid, snap: started.append(snap))
    service.recording_reconnecting.connect(lambda rid, snap: reconnecting.append(snap))
    service.recording_recovered.connect(lambda rid, snap: recovered.append(snap))
    service.recording_finished.connect(lambda rid, snap: finished.append(snap))
    try:
        service.start_recording(room_id, url)
        _wait_for(qt_app, lambda: bool(started), timeout=30)
        first_path = Path(started[0]["file_path"])
        _wait_for(
            qt_app,
            lambda: first_path.exists() and first_path.stat().st_size > 4096,
            timeout=30,
        )

        active = service._active[room_id]
        assert active.process is not None
        pid = active.process.pid
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        _wait_for(qt_app, lambda: bool(reconnecting), timeout=30)
        _wait_for(qt_app, lambda: bool(recovered), timeout=30)
        assert recovered[0]["attempt"] == 1
        assert Path(recovered[0]["file_path"]).exists()

        service.stop_recording(room_id)
        _wait_for(qt_app, lambda: bool(finished), timeout=30)
    finally:
        service.shutdown()
        db.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
