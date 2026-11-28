"""媒体探测基础设施测试：输出解析、工具发现与真实 FFmpeg 集成。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dlrecorder.infrastructure.media_probe import (
    MediaProbeError,
    MediaProber,
    parse_ffmpeg_info,
    parse_ffprobe_json,
    resolve_ffprobe,
)
from dlrecorder.infrastructure.ffmpeg import find_local_ffmpeg

FFMPEG_INFO = """
Input #0, mpegts, from 'F:\\recordings\\clip.ts':
  Duration: 00:01:23.45, start: 1.400000, bitrate: 1200 kb/s
  Program 1
  Stream #0:0[0x100]: Video: mpeg2video (Main), yuv420p(tv, progressive), 1280x720 [SAR 1:1 DAR 16:9], 15 fps, 15 tbr, 90k tbn
  Stream #0:1[0x101](und): Audio: mp2 (P[0][0][0] / 0x0050), 44100 Hz, stereo, s16p, 64 kb/s
Stream mapping:
"""

FFPROBE_JSON = """
{
  "streams": [
    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
     "disposition": {"attached_pic": 0}},
    {"codec_type": "audio", "codec_name": "aac", "width": 0, "height": 0}
  ],
  "format": {"duration": "321.50"}
}
"""


def test_parse_ffmpeg_info_extracts_duration_resolution_and_codecs() -> None:
    metadata = parse_ffmpeg_info(FFMPEG_INFO)
    assert metadata.duration_s == pytest.approx(83.45, abs=1e-6)
    assert (metadata.width, metadata.height) == (1280, 720)
    assert metadata.video_codec == "mpeg2video"
    assert metadata.audio_codec == "mp2"


def test_parse_ffmpeg_info_tolerates_missing_fields() -> None:
    metadata = parse_ffmpeg_info("Input #0, wav, from 'a.wav':\n  Duration: N/A")
    assert metadata.duration_s is None
    assert metadata.video_codec is None
    assert metadata.audio_codec is None


def test_parse_ffprobe_json() -> None:
    metadata = parse_ffprobe_json(FFPROBE_JSON)
    assert metadata.duration_s == pytest.approx(321.5)
    assert (metadata.width, metadata.height) == (1920, 1080)
    assert metadata.video_codec == "h264"
    assert metadata.audio_codec == "aac"


def test_parse_ffprobe_json_rejects_invalid_payload() -> None:
    with pytest.raises(MediaProbeError) as exc_info:
        parse_ffprobe_json("not json")
    assert exc_info.value.category == "invalid_output"


def test_resolve_ffprobe_prefers_sibling_binary(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"stub")
    sibling = tmp_path / "ffprobe.exe"
    sibling.write_bytes(b"stub")
    assert resolve_ffprobe(str(ffmpeg)) == str(sibling)


def test_probe_missing_file_raises_categorized_error(tmp_path: Path) -> None:
    prober = MediaProber(ffmpeg_path="ffmpeg", ffprobe_path="none")
    with pytest.raises(MediaProbeError) as exc_info:
        prober.probe(tmp_path / "missing.ts")
    assert exc_info.value.category == "missing"


def _ffmpeg_or_skip() -> str:
    path = find_local_ffmpeg()
    if path == "ffmpeg":
        pytest.skip("未安装项目内置 FFmpeg")
    return path


@pytest.fixture(scope="module")
def sample_media(tmp_path_factory) -> Path:
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
            "5",
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
    return path


def test_real_ffmpeg_probe_and_thumbnail(sample_media: Path, tmp_path: Path) -> None:
    prober = MediaProber(ffmpeg_path=_ffmpeg_or_skip())
    metadata = prober.probe(sample_media)
    assert metadata.duration_s is not None and metadata.duration_s > 0
    assert (metadata.width, metadata.height) == (320, 240)
    assert metadata.video_codec
    assert metadata.audio_codec

    target = tmp_path / "thumb.jpg"
    thumbnail = prober.thumbnail(sample_media, target, duration_s=metadata.duration_s)
    assert thumbnail == target
    assert target.stat().st_size > 0
    assert not target.with_name(target.name + ".part").exists()
