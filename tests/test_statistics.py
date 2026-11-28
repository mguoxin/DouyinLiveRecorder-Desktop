"""统计服务与统计页面测试。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from dlrecorder.application.statistics_service import StatisticsService
from dlrecorder.infrastructure.db import Database
from dlrecorder.infrastructure.resource_monitor import ProcessResourceSampler


class FakeSampler:
    def sample(self) -> dict:
        return {"available": True, "rss_mb": 42.5, "cpu_percent": 3.2, "cpu_seconds": 1.0}


@pytest.fixture
def stats(tmp_path: Path):
    db = Database(tmp_path / "data" / "app.db")
    recordings = tmp_path / "recordings"
    archive = tmp_path / "archive"
    thumbs = tmp_path / "thumbnails"
    for folder in (recordings, archive, thumbs):
        folder.mkdir()
    (recordings / "a.ts").write_bytes(b"a" * 100)
    (archive / "b.ts").write_bytes(b"b" * 200)
    (thumbs / "1.jpg").write_bytes(b"c" * 30)

    room_a = db.add_room("rid-a", "主播甲", "anchor-a")
    room_b = db.add_room("rid-b", "主播乙", "anchor-b")

    def add(room_id: int, status: str, *, size: int, started_at: str) -> int:
        rid = db.start_recording(room_id, str(recordings / f"{room_id}-{started_at}.ts"))
        if status != "recording":
            db.finish_recording(rid, status, size)
        db._get_conn().execute(
            "UPDATE recordings SET started_at=? WHERE id=?", (started_at, rid)
        )
        return rid

    add(room_a, "completed", size=1000, started_at="2026-01-05T10:00:00Z")
    add(room_a, "completed", size=2000, started_at="2026-01-05T12:00:00Z")
    add(room_b, "failed", size=500, started_at="2026-01-06T09:00:00Z")
    add(room_b, "interrupted", size=300, started_at="2026-02-01T09:00:00Z")
    add(room_b, "recording", size=0, started_at="2026-02-02T09:00:00Z")

    service = StatisticsService(
        db,
        recordings_dir_provider=lambda: recordings,
        archive_dir_provider=lambda: archive,
        thumbnails_dir=thumbs,
        sampler=FakeSampler(),
    )
    class Env:
        pass

    env = Env()
    env.db = db
    env.service = service
    env.recordings = recordings
    env.archive = archive
    try:
        yield env
    finally:
        db.close()


def test_overview_and_success_rate(stats):
    overview = stats.service.overview(datetime(2026, 1, 5, 18, 0))
    assert overview["total"] == 5
    assert overview["completed"] == 2
    assert overview["failed"] == 2
    assert overview["active"] == 1
    assert overview["rooms"] == 2
    assert overview["success_rate"] == 50.0
    assert overview["today"]["total"] == 2
    assert overview["month"]["total"] == 3


def test_overview_without_finished_recordings(tmp_path: Path):
    db = Database(tmp_path / "empty" / "app.db")
    try:
        service = StatisticsService(
            db,
            recordings_dir_provider=lambda: tmp_path,
            archive_dir_provider=lambda: tmp_path,
        )
        overview = service.overview(datetime(2026, 1, 1))
        assert overview["success_rate"] is None
        assert overview["total"] == 0
    finally:
        db.close()


def test_daily_monthly_anchor_and_failure_stats(stats):
    daily = stats.service.daily(30, datetime(2026, 1, 6))
    days = {row["day"]: row for row in daily}
    assert days["2026-01-05"]["total"] == 2
    assert days["2026-01-05"]["completed"] == 2
    assert days["2026-01-06"]["failed"] == 1

    monthly = stats.service.monthly(12, datetime(2026, 2, 1))
    months = {row["month"]: row for row in monthly}
    assert months["2026-01"]["total"] == 3
    assert months["2026-02"]["total"] == 2

    anchors = {row["display_name"]: row for row in stats.service.anchors()}
    assert anchors["主播甲"]["total"] == 2
    assert anchors["主播甲"]["bytes"] == 3000
    assert anchors["主播乙"]["total"] == 3
    assert anchors["主播乙"]["completed"] == 0

    reasons = {row["label"]: row["total"] for row in stats.service.failure_reasons()}
    assert reasons == {"录制失败": 1, "异常中断": 1, "录制中（可能异常）": 1}


def test_storage_breakdown(stats):
    storage = stats.service.storage()
    assert storage["recordings_bytes"] == 100
    assert storage["archive_bytes"] == 200
    assert storage["thumbnails_bytes"] == 30
    assert storage["total_bytes"] == 330
    assert storage["free_mb"] is None or storage["free_mb"] > 0


def test_report_export_is_valid_json_without_secrets(stats, tmp_path: Path):
    target = tmp_path / "reports" / "stats.json"
    path = stats.service.export_report(target)
    assert path == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["overview"]["total"] == 5
    assert payload["schema_version"] == 4
    assert payload["resources"]["rss_mb"] == 42.5
    assert "storage" in payload and "failure_reasons" in payload
    assert not (target.parent / (target.name + ".part")).exists()


def test_process_sampler_reports_memory_on_windows():
    sampler = ProcessResourceSampler()
    sample = sampler.sample()
    if not sampler.available:
        assert sample == {"available": False}
        return
    assert sample["available"] is True
    assert sample["rss_mb"] is None or sample["rss_mb"] > 0
    second = sampler.sample()
    assert second["available"] is True


def test_stats_page_renders_payload(stats, qt_app):
    from dlrecorder.presentation.stats_page import StatsPage

    page = StatsPage(stats.service)
    try:
        payload = {
            "overview": {
                "total": 5, "completed": 2, "failed": 2, "active": 1,
                "bytes": 3800, "duration_s": 100.0, "rooms": 2,
                "success_rate": 50.0,
                "today": {"total": 1, "completed": 1, "bytes": 1000},
                "month": {"total": 2, "completed": 2, "bytes": 3000},
            },
            "daily": [
                {"day": "2026-01-05", "total": 2, "completed": 2, "failed": 0,
                 "bytes": 3000, "duration_s": 65.0},
            ],
            "anchors": [
                {"display_name": "主播甲", "web_rid": "rid-a", "total": 2,
                 "completed": 2, "bytes": 3000, "last_started": "2026-01-05T12:00:00Z"},
            ],
            "failures": [{"status": "failed", "label": "录制失败", "total": 1}],
            "storage": {"total_bytes": 330, "free_mb": 1024.0,
                        "recordings_bytes": 100, "archive_bytes": 200,
                        "thumbnails_bytes": 30},
            "resources": {"available": True, "rss_mb": 42.5, "cpu_percent": 3.2},
        }
        page._on_loaded(payload)
        assert page.card_labels["total"].text() == "5 场"
        assert page.card_labels["success"].text() == "50.0%"
        assert page.card_labels["memory"].text() == "42.5 MB，CPU 3.2%"
        assert page.daily_table.rowCount() == 1
        assert page.anchor_table.rowCount() == 1
        assert page.failure_table.item(0, 0).text() == "录制失败"
        assert "磁盘剩余" in page.status_label.text()
    finally:
        page.deleteLater()
