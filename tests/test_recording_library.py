"""录制库服务测试：检索、统计、一致性检查与安全删除。"""
from pathlib import Path

import pytest

from dlrecorder.application.recording_library_service import RecordingLibraryService
from dlrecorder.domain.recording_query import RecordingQuery
from dlrecorder.infrastructure.db import Database


@pytest.fixture
def library(tmp_path: Path):
    db = Database(tmp_path / "data" / "app.db")
    recordings = tmp_path / "recordings"
    archive = tmp_path / "archive"
    recordings.mkdir()
    service = RecordingLibraryService(
        db,
        recordings_dir_provider=lambda: recordings,
        archive_dir_provider=lambda: archive,
    )

    def add_recording(
        anchor: str = "主播一",
        web_rid: str = "rid-1",
        *,
        content: bytes = b"data",
        started_at: str = "2025-01-02T03:04:05Z",
        status: str = "completed",
        filename: str = "clip.ts",
    ) -> tuple[int, Path]:
        room_id = db.add_room(web_rid, anchor, f"anchor-{web_rid}")
        path = recordings / filename
        path.write_bytes(content)
        recording_id = db.start_recording(room_id, str(path))
        db._get_conn().execute(
            "UPDATE recordings SET started_at=? WHERE id=?", (started_at, recording_id)
        )
        if status != "recording":
            db.finish_recording(recording_id, status, len(content))
        return recording_id, path

    class Env:
        pass

    env = Env()
    env.db = db
    env.tmp_path = tmp_path
    env.recordings = recordings
    env.archive = archive
    env.service = service
    env.add_recording = add_recording
    try:
        yield env
    finally:
        db.close()


def test_search_filters_by_keyword_date_status_and_size(library):
    first, _ = library.add_recording("主播一", "rid-1", content=b"x" * 10,
                                     started_at="2025-01-02T03:04:05Z")
    second, _ = library.add_recording("主播二", "rid-2", content=b"x" * 2048,
                                      started_at="2025-02-03T03:04:05Z",
                                      status="failed", filename="other.ts")

    assert [row["id"] for row in library.service.search(RecordingQuery())] == [second, first]
    assert [row["id"] for row in library.service.search(
        RecordingQuery(keyword="主播二"))] == [second]
    assert [row["id"] for row in library.service.search(
        RecordingQuery(keyword="rid-1"))] == [first]
    assert [row["id"] for row in library.service.search(
        RecordingQuery(date_from="2025-02-01"))] == [second]
    assert [row["id"] for row in library.service.search(
        RecordingQuery(date_to="2025-01-31"))] == [first]
    assert [row["id"] for row in library.service.search(
        RecordingQuery(status="failed"))] == [second]
    assert [row["id"] for row in library.service.search(
        RecordingQuery(min_size=1024))] == [second]
    assert [row["id"] for row in library.service.search(
        RecordingQuery(max_size=100))] == [first]
    assert library.service.count(RecordingQuery(status="completed")) == 1
    assert [row["id"] for row in library.service.search(
        RecordingQuery(limit=1, offset=1))] == [first]


def test_search_escapes_like_wildcards(library):
    library.add_recording("主播%一", "rid-1")
    library.add_recording("普通主播", "rid-2", filename="b.ts")
    rows = library.service.search(RecordingQuery(keyword="%"))
    assert len(rows) == 1
    assert rows[0]["display_name_snapshot"] == "主播%一"


def test_storage_stats_groups_by_anchor_date_and_archive(library):
    first, _ = library.add_recording("主播一", "rid-1", content=b"a" * 100)
    second, _ = library.add_recording("主播一", "rid-2", content=b"a" * 200,
                                      started_at="2025-02-03T03:04:05Z",
                                      filename="b.ts")
    library.db.set_recording_archive(second, "archived", archive_path="X:/a.ts")

    stats = library.service.storage_stats()
    assert stats["total_count"] == 2
    assert stats["total_bytes"] == 300
    by_anchor = {row["label"]: row for row in stats["by_anchor"]}
    assert by_anchor["主播一"]["n"] == 2
    assert by_anchor["主播一"]["bytes"] == 300
    by_archive = {row["label"]: row for row in stats["by_archive"]}
    assert by_archive["none"]["n"] == 1
    assert by_archive["archived"]["n"] == 1
    by_date = {row["label"]: row for row in stats["by_date"]}
    assert set(by_date) == {"2025-01-02", "2025-02-03"}


def test_scan_consistency_reports_all_issue_kinds(library):
    missing, missing_path = library.add_recording("主播一", "rid-1",
                                                  filename="missing.ts")
    missing_path.unlink()
    mismatch, mismatch_path = library.add_recording("主播一", "rid-2",
                                                    filename="mismatch.ts")
    library.db._get_conn().execute(
        "UPDATE recordings SET file_size=? WHERE id=?", (999999, mismatch)
    )
    archived, archived_path = library.add_recording("主播一", "rid-3",
                                                    filename="archived.ts")
    library.db.set_recording_archive(
        archived, "archived", archive_path=str(library.archive / "gone.ts")
    )
    orphan = library.recordings / "orphan.ts"
    orphan.write_bytes(b"orphan")

    report = library.service.scan_consistency()
    counts = report.counts()
    assert counts.get("missing") == 1
    assert counts.get("size_mismatch") == 1
    assert counts.get("archive_missing") == 1
    assert counts.get("orphan_file") == 1
    assert report.orphan_files == 1
    kinds = {issue.kind for issue in report.issues}
    assert kinds == {"missing", "size_mismatch", "archive_missing", "orphan_file"}


def test_scan_consistency_clean_library(library):
    library.add_recording("主播一", "rid-1")
    report = library.service.scan_consistency()
    assert report.issues == ()
    assert report.orphan_files == 0


def test_delete_moves_files_to_recycler_and_marks_deleted(library):
    recording_id, path = library.add_recording("主播一", "rid-1")
    expected_size = path.stat().st_size
    recycled: list[Path] = []

    def fake_recycler(paths):
        for item in paths:
            recycled.append(item)
            item.unlink()

    report = library.service.delete_recordings([recording_id], recycler=fake_recycler)

    assert report.deleted == (recording_id,)
    assert report.freed_bytes == expected_size
    assert recycled == [path]
    assert not path.exists()
    row = library.db.get_recording(recording_id)
    assert row["status"] == "deleted"
    assert row["file_path"] == str(path)


def test_delete_refuses_active_recording(library):
    recording_id, path = library.add_recording("主播一", "rid-1", status="recording")
    called: list[Path] = []

    report = library.service.delete_recordings(
        [recording_id], recycler=lambda paths: called.extend(paths)
    )

    assert report.deleted == ()
    assert report.skipped[0][0] == recording_id
    assert "录制进行中" in report.skipped[0][1]
    assert called == []
    assert path.exists()
    assert library.db.get_recording(recording_id)["status"] == "recording"


def test_delete_skips_missing_file_without_calling_recycler(library):
    recording_id, path = library.add_recording("主播一", "rid-1")
    path.unlink()
    called: list[Path] = []

    report = library.service.delete_recordings(
        [recording_id], recycler=lambda paths: called.extend(paths)
    )

    assert report.deleted == ()
    assert "文件不存在" in report.skipped[0][1]
    assert called == []


def test_delete_reports_archived_copy_but_only_removes_source(library):
    recording_id, source = library.add_recording("主播一", "rid-1")
    library.archive.mkdir()
    archived_copy = library.archive / "copy.ts"
    archived_copy.write_bytes(source.read_bytes())
    library.db.set_recording_archive(
        recording_id, "archived", archive_path=str(archived_copy)
    )
    recycled: list[Path] = []

    def fake_recycler(paths):
        for item in paths:
            recycled.append(item)
            item.unlink()

    report = library.service.delete_recordings(
        [recording_id], recycler=fake_recycler
    )

    assert report.archived_copies == (recording_id,)
    assert recycled == [source]
    assert archived_copy.exists()
    assert not source.exists()


def test_library_page_renders_and_scans(library, qt_app):
    from dlrecorder.presentation.library_page import LibraryPage

    library.add_recording("主播一", "rid-1")
    page = LibraryPage(library.service)
    try:
        page.reload()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 1).text() == "主播一"
        assert page.table.item(0, 6).text() == "已完成"
        assert "匹配 1 条" in page.summary_label.text()

        page._on_scan_finished(library.service.scan_consistency())
        assert "一致性检查通过" in page.status_label.text()
    finally:
        page.deleteLater()


def test_disk_usage_reports_both_locations(library):
    usage = library.service.disk_usage()
    assert usage["recordings"] is not None
    assert usage["recordings"]["total"] > 0
    assert usage["recordings"]["free"] >= 0
    assert usage["archive"] is not None
