"""归档服务测试：状态机、异常处理、重复执行与恢复。"""
from pathlib import Path

import pytest

from dlrecorder.application.archive_service import ArchiveService
from dlrecorder.domain.archive_rules import ArchiveSettings
from dlrecorder.infrastructure.db import Database


def _settings(root: Path, *, delete_source: bool = False) -> ArchiveSettings:
    return ArchiveSettings(
        enabled=True,
        root_dir=root,
        folder_template="{anchor}",
        name_template="{date}_{time}_{web_rid}",
        delete_source=delete_source,
    )


@pytest.fixture
def env(tmp_path: Path, qt_app):
    db = Database(tmp_path / "data" / "app.db")
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    services: list[ArchiveService] = []
    events: list[tuple[int, dict]] = []

    def make_service(settings, **kwargs) -> ArchiveService:
        service = ArchiveService(
            db,
            settings_provider=lambda: settings,
            on_event=lambda rid, payload: events.append((rid, payload)),
            **kwargs,
        )
        services.append(service)
        return service

    def add_recording(
        content: bytes = b"recording-bytes" * 100,
        *,
        started_at: str = "2025-01-02T03:04:05Z",
        finish: bool = True,
        filename: str = "clip.ts",
    ) -> tuple[int, Path]:
        room_id = db.add_room("rid-1", "主播一", "anchor-1")
        path = recordings / filename
        path.write_bytes(content)
        recording_id = db.start_recording(room_id, str(path))
        db._get_conn().execute(
            "UPDATE recordings SET started_at=? WHERE id=?", (started_at, recording_id)
        )
        if finish:
            db.finish_recording(recording_id, "completed", len(content))
        return recording_id, path

    class Env:
        pass

    instance = Env()
    instance.db = db
    instance.tmp_path = tmp_path
    instance.recordings = recordings
    instance.events = events
    instance.make_service = make_service
    instance.add_recording = add_recording
    try:
        yield instance
    finally:
        for service in services:
            service.shutdown()
        db.close()


def test_archives_completed_recording_and_keeps_source(env):
    recording_id, source = env.add_recording()
    archive_root = env.tmp_path / "archive"
    service = env.make_service(_settings(archive_root))

    assert service.enqueue(recording_id) is True
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    target = Path(row["archive_path"])
    assert row["archive_status"] == "archived"
    assert row["archived_at"] is not None
    assert row["archive_error"] is None
    assert row["archive_attempts"] == 1
    assert target == archive_root / "主播一" / "2025-01-02_030405_rid-1.ts"
    assert target.read_bytes() == source.read_bytes()
    assert source.exists()
    job = env.db.get_latest_archive_job(recording_id)
    assert job["status"] == "archived"
    assert len(job["sha256"]) == 64
    assert job["bytes_total"] == len(source.read_bytes())
    assert env.events[-1][1]["status"] == "archived"


def test_archive_disabled_leaves_status_none(env):
    recording_id, source = env.add_recording()
    service = env.make_service(
        ArchiveSettings(enabled=False, root_dir=env.tmp_path / "archive")
    )

    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True
    assert env.db.get_recording(recording_id)["archive_status"] == "none"
    assert source.exists()
    assert not (env.tmp_path / "archive").exists()


def test_delete_source_only_after_verification(env):
    recording_id, source = env.add_recording()
    service = env.make_service(
        _settings(env.tmp_path / "archive", delete_source=True)
    )

    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    assert row["archive_status"] == "archived"
    assert not source.exists()
    target = Path(row["archive_path"])
    assert target.is_file()
    assert target.read_bytes() == b"recording-bytes" * 100


def test_conflicting_target_gets_suffix_without_overwrite(env):
    recording_id, source = env.add_recording()
    archive_root = env.tmp_path / "archive"
    target = archive_root / "主播一" / "2025-01-02_030405_rid-1.ts"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing-content")
    service = env.make_service(_settings(archive_root))

    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    assert row["archive_status"] == "archived"
    assert target.read_bytes() == b"existing-content"
    archived = Path(row["archive_path"])
    assert archived.name == "2025-01-02_030405_rid-1_1.ts"
    assert archived.read_bytes() == source.read_bytes()
    assert source.exists()


def test_identical_target_is_skipped_without_copy(env):
    recording_id, source = env.add_recording()
    archive_root = env.tmp_path / "archive"
    target = archive_root / "主播一" / "2025-01-02_030405_rid-1.ts"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    service = env.make_service(_settings(archive_root))

    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    assert row["archive_status"] == "skipped"
    assert Path(row["archive_path"]) == target
    assert "相同内容" in row["archive_error"]
    assert source.exists()


def test_missing_source_marks_failed(env):
    recording_id, source = env.add_recording()
    source.unlink()
    service = env.make_service(_settings(env.tmp_path / "archive"))

    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    assert row["archive_status"] == "failed"
    assert "源文件不存在" in row["archive_error"]
    job = env.db.get_latest_archive_job(recording_id)
    assert job["status"] == "failed"


def test_recording_in_progress_is_never_touched(env):
    recording_id, source = env.add_recording(finish=False)
    service = env.make_service(_settings(env.tmp_path / "archive"))

    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    assert row["status"] == "recording"
    assert row["archive_status"] == "none"
    assert source.exists()
    assert not (env.tmp_path / "archive").exists()


def test_low_disk_space_marks_failed_and_keeps_source(env):
    recording_id, source = env.add_recording()
    service = env.make_service(
        _settings(env.tmp_path / "archive"),
        free_space_provider=lambda path: 0.0,
    )

    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    assert row["archive_status"] == "failed"
    assert "剩余空间不足" in row["archive_error"]
    assert source.exists()
    assert not (env.tmp_path / "archive").exists()


def test_recover_pending_retries_interrupted_archive(env):
    recording_id, source = env.add_recording()
    env.db.set_recording_archive(recording_id, "pending")
    job_id = env.db.create_archive_job(recording_id, str(source), status="copying")
    service = env.make_service(_settings(env.tmp_path / "archive"))

    assert service.recover_pending() == 1
    assert service.wait_until_idle(10) is True

    row = env.db.get_recording(recording_id)
    assert row["archive_status"] == "archived"
    assert env.db.get_archive_job(job_id)["status"] == "archived"
    assert source.exists()


def test_duplicate_enqueue_is_rejected_while_queued(env):
    recording_id, _ = env.add_recording()
    service = env.make_service(_settings(env.tmp_path / "archive"))

    assert service.enqueue(recording_id) is True
    # 任务可能在极短时间内完成；无论是否已完成，重复入队都应被去重。
    service.enqueue(recording_id)
    assert service.wait_until_idle(10) is True
    assert len(env.db.list_archive_jobs()) == 1
