"""媒体资产组织测试：标签、收藏、备注、主播档案与组合检索排序。"""
from __future__ import annotations

from pathlib import Path

import pytest

from dlrecorder.application.recording_library_service import RecordingLibraryService
from dlrecorder.domain.recording_query import RecordingQuery
from dlrecorder.infrastructure.db import Database


@pytest.fixture
def library(tmp_path: Path):
    db = Database(tmp_path / "data" / "app.db")
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    service = RecordingLibraryService(
        db,
        recordings_dir_provider=lambda: recordings,
        archive_dir_provider=lambda: tmp_path / "archive",
    )

    def add_recording(
        anchor: str = "主播一",
        web_rid: str = "rid-1",
        *,
        size: int = 10,
        duration_s: float | None = None,
        filename: str = "clip.ts",
        finished: bool = True,
    ) -> int:
        room_id = db.add_room(web_rid, anchor, f"anchor-{web_rid}")
        path = recordings / filename
        path.write_bytes(b"x" * size)
        recording_id = db.start_recording(room_id, str(path))
        if duration_s is not None:
            db.set_recording_metadata(recording_id, status="ready", duration_s=duration_s)
        if finished:
            db.finish_recording(recording_id, "completed", size)
        return recording_id

    class Env:
        pass

    env = Env()
    env.db = db
    env.service = service
    env.tmp_path = tmp_path
    env.add_recording = add_recording
    try:
        yield env
    finally:
        db.close()


def test_tag_lifecycle_and_recording_assignment(library):
    first = library.add_recording(filename="a.ts")
    second = library.add_recording(web_rid="rid-2", filename="b.ts")

    tag_id = library.service.create_tag("精华", "#ffcc00")
    other_id = library.service.create_tag("待剪辑")
    assert library.service.create_tag("精华") == tag_id

    library.service.set_recording_tags(first, [tag_id, other_id])
    assert {tag["name"] for tag in library.service.get_recording_tags(first)} == {
        "精华", "待剪辑",
    }
    library.service.set_recording_tags(first, [tag_id])
    assert [tag["name"] for tag in library.service.get_recording_tags(first)] == ["精华"]
    assert library.service.recording_tag_map([first, second]) == {first: ["精华"]}

    assert library.service.delete_tag(tag_id) is True
    assert library.service.get_recording_tags(first) == []
    assert [tag["name"] for tag in library.service.list_tags()] == ["待剪辑"]


def test_recording_delete_cascades_tags(library):
    recording_id = library.add_recording()
    tag_id = library.service.create_tag("临时")
    library.service.set_recording_tags(recording_id, [tag_id])

    library.db._get_conn().execute("DELETE FROM recordings WHERE id=?", (recording_id,))
    assert library.service.get_recording_tags(recording_id) == []
    assert library.db._get_conn().execute(
        "SELECT COUNT(*) FROM recording_tags"
    ).fetchone()[0] == 0
    assert len(library.service.list_tags()) == 1


def test_favorite_and_note_updates(library):
    recording_id = library.add_recording()
    assert library.service.set_favorite(recording_id, True) is True
    assert library.db.get_recording(recording_id)["favorite"] == 1
    assert library.service.set_note(recording_id, "  高能片段  ") is True
    assert library.db.get_recording(recording_id)["note"] == "高能片段"
    assert library.service.set_favorite(9999, True) is False


def test_query_filters_by_tag_favorite_and_duration(library):
    first = library.add_recording(duration_s=30.0, filename="a.ts")
    second = library.add_recording(web_rid="rid-2", duration_s=120.0, filename="b.ts")
    third = library.add_recording(web_rid="rid-3", duration_s=None, filename="c.ts")
    tag_id = library.service.create_tag("高光")
    library.service.set_recording_tags(second, [tag_id])
    library.service.set_favorite(first, True)

    favorites = library.db.search_recordings(RecordingQuery(favorite=True))
    assert [row["id"] for row in favorites] == [first]

    tagged = library.db.search_recordings(RecordingQuery(tag_ids=(tag_id,)))
    assert [row["id"] for row in tagged] == [second]

    long_clips = library.db.search_recordings(RecordingQuery(min_duration_s=60))
    assert [row["id"] for row in long_clips] == [second]

    short_clips = library.db.search_recordings(RecordingQuery(max_duration_s=60))
    assert {row["id"] for row in short_clips} == {first, third}

    assert library.db.count_recordings(RecordingQuery(favorite=True)) == 1


def test_query_sort_orders(library):
    first = library.add_recording(size=100, duration_s=10.0, filename="a.ts")
    second = library.add_recording(web_rid="rid-2", size=300, duration_s=5.0, filename="b.ts")
    third = library.add_recording(web_rid="rid-3", size=200, duration_s=20.0, filename="c.ts")

    def ids(sort_by: str) -> list[int]:
        return [row["id"] for row in library.db.search_recordings(RecordingQuery(sort_by=sort_by))]

    assert ids("newest") == [third, second, first]
    assert ids("oldest") == [first, second, third]
    assert ids("largest") == [second, third, first]
    assert ids("smallest") == [first, third, second]
    assert ids("longest") == [third, first, second]
    assert ids("shortest") == [second, first, third]
    # 未知排序回退为最新优先，而不是拼接非法 SQL。
    assert ids("bogus; DROP TABLE recordings") == [third, second, first]


def test_anchor_profile_upsert_and_room_cascade(library):
    room_id = library.db.add_room("rid-profile", "档案主播", "anchor-p")
    assert library.service.get_anchor_profile(room_id) is None

    library.service.set_anchor_profile(room_id, bio="唱歌主播", note="晚八点开播")
    profile = library.service.get_anchor_profile(room_id)
    assert profile["bio"] == "唱歌主播"
    assert profile["note"] == "晚八点开播"
    created_at = profile["created_at"]

    library.service.set_anchor_profile(room_id, bio="唱歌与聊天")
    profile = library.service.get_anchor_profile(room_id)
    assert profile["bio"] == "唱歌与聊天"
    assert profile["note"] == "晚八点开播"
    assert profile["created_at"] == created_at
    assert len(library.service.list_anchor_profiles()) == 1

    library.db.delete_room(room_id)
    assert library.service.get_anchor_profile(room_id) is None


def test_library_page_asset_actions(library, qt_app):
    from dlrecorder.presentation.library_page import LibraryPage, _TagDialog

    library.add_recording("主播一", "rid-1", filename="a.ts")
    second = library.add_recording("主播二", "rid-2", filename="b.ts")
    page = LibraryPage(library.service)
    try:
        page.reload()
        assert page.table.rowCount() == 2

        # 收藏第一条并确认表格星标与数据库一致。
        page.table.selectRow(0)
        page._toggle_favorite()
        selected_id = int(page.table.item(0, 0).text())
        favorited = library.db.get_recording(selected_id)
        assert favorited["favorite"] == 1
        assert page.table.item(0, 8).text() == "★"

        # 收藏筛选只显示已收藏记录。
        page.favorite_combo.setCurrentIndex(1)
        page.reload()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 0).text() == str(selected_id)
        page.favorite_combo.setCurrentIndex(0)
        page.reload()

        # 备注保存。
        page.table.selectRow(0)
        page.note_edit.setText("最佳片段")
        page._save_note()
        assert library.db.get_recording(int(page.table.item(0, 0).text()))["note"] == "最佳片段"

        # 标签对话框创建并勾选标签。
        dialog = _TagDialog(library.service, library.service.list_tags(), [], page)
        dialog.new_edit.setText("新标签")
        dialog._create_tag()
        assert dialog.list_widget.count() == 1
        assert dialog.list_widget.item(0).checkState().value
        dialog.list_widget.item(0).setCheckState(
            dialog.list_widget.item(0).checkState()
        )
        assert dialog.list_widget.count() == 1
        tags = library.service.list_tags()
        assert [tag["name"] for tag in tags] == ["新标签"]
        dialog.deleteLater()

        # 标签筛选联动。
        tag_id = tags[0]["id"]
        library.service.set_recording_tags(second, [tag_id])
        page.reload()
        index = page.tag_combo.findData(tag_id)
        assert index > 0
        page.tag_combo.setCurrentIndex(index)
        page.reload()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 0).text() == str(second)
    finally:
        page.deleteLater()
