"""媒体探测服务测试：队列、幂等、失败记录、重试与归档副本回退。"""
from __future__ import annotations

from pathlib import Path

import pytest

from dlrecorder.application.media_probe_service import MediaProbeService
from dlrecorder.infrastructure.db import Database
from dlrecorder.infrastructure.media_probe import MediaMetadata, MediaProbeError


class FakeProber:
    def __init__(
        self,
        *,
        metadata: MediaMetadata | None = None,
        fail_probe: bool = False,
        fail_thumbnail: bool = False,
        fail_times: int = 0,
    ) -> None:
        self.metadata = metadata or MediaMetadata(
            duration_s=12.5, width=320, height=240,
            video_codec="mpeg4", audio_codec="aac",
        )
        self.fail_probe = fail_probe
        self.fail_thumbnail = fail_thumbnail
        self.fail_times = fail_times
        self.probed: list[Path] = []

    def probe(self, source: Path) -> MediaMetadata:
        self.probed.append(Path(source))
        if self.fail_probe or self.fail_times > 0:
            self.fail_times = max(0, self.fail_times - 1)
            raise MediaProbeError("模拟探测失败", category="tool_failed")
        return self.metadata

    def thumbnail(self, source: Path, target: Path, *, duration_s=None, width: int = 320) -> Path:
        if self.fail_thumbnail:
            raise MediaProbeError("模拟缩略图失败", category="thumbnail_failed")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"jpeg")
        return Path(target)


@pytest.fixture
def env(tmp_path: Path, qt_app):
    db = Database(tmp_path / "data" / "app.db")
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    services: list[MediaProbeService] = []
    events: list[tuple[int, dict]] = []

    def add_recording(
        *,
        status: str = "completed",
        content: bytes = b"media-bytes" * 100,
        web_rid: str = "rid-media",
        filename: str = "clip.ts",
        archive: bool = False,
    ) -> tuple[int, Path, Path | None]:
        room_id = db.add_room(web_rid, "主播", "anchor")
        path = recordings / filename
        path.write_bytes(content)
        recording_id = db.start_recording(room_id, str(path))
        archived_path = None
        if archive:
            archived_path = recordings / f"archived-{filename}"
            archived_path.write_bytes(content)
            db.set_recording_archive(recording_id, "archived", archive_path=str(archived_path))
            path.unlink()
        if status != "recording":
            db.finish_recording(recording_id, status, len(content))
        return recording_id, path, archived_path

    def make_service(prober: FakeProber) -> MediaProbeService:
        service = MediaProbeService(
            db,
            thumbnails_dir=tmp_path / "thumbnails",
            prober_factory=lambda: prober,
            on_event=lambda rid, payload: events.append((rid, payload)),
        )
        services.append(service)
        return service

    class Env:
        pass

    instance = Env()
    instance.db = db
    instance.tmp_path = tmp_path
    instance.recordings = recordings
    instance.events = events
    instance.add_recording = add_recording
    instance.make_service = make_service
    try:
        yield instance
    finally:
        for service in services:
            service.shutdown()
        db.close()


def test_probe_success_updates_database_and_thumbnail(env):
    recording_id, _, _ = env.add_recording()
    prober = FakeProber()
    service = env.make_service(prober)

    assert service.enqueue(recording_id) is True
    assert service.wait_until_idle(timeout=10) is True

    row = env.db.get_recording(recording_id)
    assert row["metadata_status"] == "ready"
    assert row["duration_s"] == pytest.approx(12.5)
    assert (row["width"], row["height"]) == (320, 240)
    assert row["video_codec"] == "mpeg4"
    assert row["audio_codec"] == "aac"
    thumbnail = Path(row["thumbnail_path"])
    assert thumbnail.is_file()
    assert prober.probed == [env.recordings / "clip.ts"]
    assert env.events and env.events[-1][1]["metadata_status"] == "ready"


def test_duplicate_enqueue_is_ignored(env):
    recording_id, _, _ = env.add_recording()
    service = env.make_service(FakeProber())

    assert service.enqueue(recording_id) is True
    assert service.enqueue(recording_id) is False
    service.wait_until_idle(timeout=10)
    assert env.db.get_recording(recording_id)["metadata_status"] == "ready"


def test_recording_in_progress_is_skipped(env):
    recording_id, _, _ = env.add_recording(status="recording")
    prober = FakeProber()
    service = env.make_service(prober)

    service.enqueue(recording_id)
    service.wait_until_idle(timeout=10)

    assert env.db.get_recording(recording_id)["metadata_status"] == "none"
    assert prober.probed == []


def test_probe_failure_is_recorded_and_can_be_retried(env):
    recording_id, _, _ = env.add_recording()
    prober = FakeProber(fail_times=1)
    service = env.make_service(prober)

    service.enqueue(recording_id)
    service.wait_until_idle(timeout=10)

    row = env.db.get_recording(recording_id)
    assert row["metadata_status"] == "failed"
    assert "模拟探测失败" in row["metadata_error"]

    assert service.enqueue(recording_id, force=True) is True
    service.wait_until_idle(timeout=10)
    row = env.db.get_recording(recording_id)
    assert row["metadata_status"] == "ready"
    assert row["metadata_error"] is None


def test_thumbnail_failure_keeps_ready_metadata(env):
    recording_id, _, _ = env.add_recording()
    service = env.make_service(FakeProber(fail_thumbnail=True))

    service.enqueue(recording_id)
    service.wait_until_idle(timeout=10)

    row = env.db.get_recording(recording_id)
    assert row["metadata_status"] == "ready"
    assert row["thumbnail_path"] is None
    assert "模拟缩略图失败" in row["metadata_error"]


def test_probe_falls_back_to_archive_copy(env):
    recording_id, source, archived = env.add_recording(archive=True)
    prober = FakeProber()
    service = env.make_service(prober)

    env.db.set_recording_archive(recording_id, "archived", archive_path=str(archived))
    if source.exists():
        source.unlink()

    service.enqueue(recording_id)
    service.wait_until_idle(timeout=10)

    assert prober.probed == [archived]
    assert env.db.get_recording(recording_id)["metadata_status"] == "ready"


def test_missing_file_marks_failed(env):
    recording_id, source, _ = env.add_recording()
    source.unlink()
    service = env.make_service(FakeProber())

    service.enqueue(recording_id)
    service.wait_until_idle(timeout=10)

    row = env.db.get_recording(recording_id)
    assert row["metadata_status"] == "failed"
    assert "不存在" in row["metadata_error"]


def test_enqueue_pending_only_picks_unprocessed_completed(env):
    pending_id, _, _ = env.add_recording(web_rid="pending", filename="pending.ts")
    ready_id, _, _ = env.add_recording(web_rid="ready", filename="ready.ts")
    env.db.set_recording_metadata(ready_id, status="ready")

    service = env.make_service(FakeProber())
    count = service.enqueue_pending(limit=50)
    service.wait_until_idle(timeout=10)

    assert count == 1
    assert env.db.get_recording(pending_id)["metadata_status"] == "ready"
    assert env.db.get_recording(ready_id)["metadata_status"] == "ready"
