"""整合测试：应用生命周期、持久化、多房间展示、状态转换。

使用 offscreen 平台避免弹出真实窗口。
"""
import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dlrecorder.application.app import Application
from dlrecorder.domain.states import ParseResult
from dlrecorder.infrastructure.db import Database

RECORD_STUB = r"""
import sys, time
path, mode = sys.argv[1], sys.argv[2]
with open(path, "wb") as f:
    f.write(b"x" * 4096)
    f.flush()
if mode == "stdin":
    sys.stdin.readline()
"""


@pytest.fixture
def app(tmp_path, qt_app):
    application = Application(base_dir=tmp_path)
    yield application
    application.shutdown()


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def _session_rows(app: Application):
    return app.db._get_conn().execute(
        "SELECT * FROM monitor_sessions ORDER BY id"
    ).fetchall()


def _cancel_current_task(app: Application, room_id: int) -> int:
    async def cancel():
        current = app.service._worker._tasks[room_id]
        current.task.cancel()
        return current.session_id

    return asyncio.run_coroutine_threadsafe(
        cancel(), app.service._worker.loop
    ).result(timeout=2)


def test_app_starts_and_closes(app):
    """应用能够正常初始化和关闭。"""
    assert app.main_window is not None
    assert app.service is not None


def test_add_room_persists_to_db(app):
    """添加的房间能写入 SQLite 并在重启后保留。"""
    rid = app.service.add_room("555", "整合测试主播")
    assert rid is not None
    assert len(app.service.get_rooms()) == 1

    # 模拟重启：新建 Application 指向同一数据目录
    base = app.config.data_dir.parent
    app.shutdown()

    app2 = Application(base_dir=base)
    try:
        rooms = app2.service.get_rooms()
        assert len(rooms) == 1
        assert rooms[0].web_rid == "555"
        assert rooms[0].display_name == "整合测试主播"
    finally:
        app2.shutdown()


def test_multiple_rooms_displayed(app):
    """多个 Mock 房间可同时展示在列表中。"""
    app.service.add_room("111", "房间A")
    app.service.add_room("222", "房间B")
    app.service.add_room("333", "房间C")
    assert len(app.service.get_rooms()) == 3
    assert app.main_window.room_list.rowCount() == 3


def test_state_transition_via_monitor(app):
    """监控任务能更新房间状态（Mock 返回 LIVE）。"""
    rid = app.service.add_room("777", "状态测试")
    app.parser.set_response("777", ParseResult.SUCCESS, 2)
    app.service.start_monitor(rid)

    deadline = time.time() + 5
    while time.time() < deadline:
        QApplication.processEvents()
        room = app.service.get_room(rid)
        if room and room.state.last_confirmed_room_state == "LIVE":
            break
        time.sleep(0.1)

    room = app.service.get_room(rid)
    assert room.state.last_confirmed_room_state == "LIVE"
    assert room.state.last_parse_result == ParseResult.SUCCESS
    app.service.stop_monitor(rid)


def test_remove_room(app):
    """删除房间后从列表和数据库移除。"""
    rid = app.service.add_room("999", "待删除")
    app.service.remove_room(rid)
    assert app.service.get_rooms() == []
    assert app.main_window.room_list.rowCount() == 0


def test_gui_not_blocked_by_monitor(app):
    """监控运行时 GUI 主线程不被阻塞（processEvents 能持续响应）。"""
    rid = app.service.add_room("888", "不阻塞测试")
    app.service.start_monitor(rid)

    # 连续处理事件，确认不卡住
    for _ in range(20):
        QApplication.processEvents()
        time.sleep(0.02)

    app.service.stop_monitor(rid)


def test_log_widget_bounded(app):
    """日志展示区有最大行数限制，不会无限增长。"""
    lw = app.main_window.log_widget
    assert lw.maximumBlockCount() == 500
    for i in range(600):
        lw.append_log(f"log line {i}")
    assert lw.blockCount() <= 500


def test_service_rapid_stop_restart_binds_independent_sessions(app):
    """快速 start→stop→start 保留两次历史，旧停止不影响新任务。"""
    room_id = app.service.add_room("rapid-service", "快速启停")
    app.service.start_monitor(room_id)
    app.service.stop_monitor(room_id)
    app.service.start_monitor(room_id)

    _wait_for(
        lambda: len(_session_rows(app)) == 2
        and _session_rows(app)[0]["status"] == "stopped"
        and _session_rows(app)[1]["status"] == "running"
    )
    first, second = _session_rows(app)
    assert first["id"] != second["id"]
    assert first["ended_at"] is not None
    first_history = dict(first)
    assert app.service.is_monitoring(room_id)

    app.service.stop_monitor(room_id)
    _wait_for(lambda: _session_rows(app)[1]["status"] == "stopped")
    _wait_for(lambda: not app.service.is_monitoring(room_id))
    first_after, second_after = _session_rows(app)
    assert dict(first_after) == first_history
    assert second_after["ended_at"] is not None


def test_service_abnormal_task_exit_closes_bound_session(app):
    """任务异常取消时，MonitorService 绑定的 Session 被标记为 interrupted。"""
    room_id = app.service.add_room("abnormal-service", "异常退出")
    app.service.start_monitor(room_id)
    _wait_for(lambda: len(_session_rows(app)) == 1)
    session_id = _session_rows(app)[0]["id"]

    cancelled_session = _cancel_current_task(app, room_id)
    assert cancelled_session == session_id
    _wait_for(lambda: _session_rows(app)[0]["status"] == "interrupted")
    _wait_for(lambda: not app.service.is_monitoring(room_id))
    session = _session_rows(app)[0]
    assert session["ended_at"] is not None
    assert session["error_message"] == "监控任务被取消"


def test_service_sessions_are_isolated_between_rooms(app):
    """停止一个房间只结束其 Session，不修改其他房间的历史。"""
    first_room = app.service.add_room("isolated-a", "房间A")
    second_room = app.service.add_room("isolated-b", "房间B")
    app.service.start_monitor(first_room)
    app.service.start_monitor(second_room)
    _wait_for(
        lambda: len(_session_rows(app)) == 2
        and all(row["status"] == "running" for row in _session_rows(app))
    )

    app.service.stop_monitor(first_room)
    _wait_for(lambda: _session_rows(app)[0]["status"] == "stopped")
    first_history = dict(_session_rows(app)[0])
    assert _session_rows(app)[1]["status"] == "running"
    assert app.service.is_monitoring(second_room)

    app.service.stop_monitor(second_room)
    _wait_for(lambda: _session_rows(app)[1]["status"] == "stopped")
    assert dict(_session_rows(app)[0]) == first_history
    assert _session_rows(app)[1]["ended_at"] is not None


def test_application_startup_interrupts_only_legacy_running_sessions(tmp_path, qt_app):
    """应用启动保留历史，仅将遗留 running Session 标记为 interrupted。"""
    seed = Database(tmp_path / "data" / "app.db")
    room_id = seed.add_room("legacy-running", "遗留主播", "legacy-anchor")
    running_id = seed.start_session(room_id)
    stopped_id = seed.start_session(room_id)
    seed.end_session(stopped_id, status="stopped")
    running_before = dict(
        seed._get_conn().execute(
            "SELECT * FROM monitor_sessions WHERE id=?", (running_id,)
        ).fetchone()
    )
    stopped_before = dict(
        seed._get_conn().execute(
            "SELECT * FROM monitor_sessions WHERE id=?", (stopped_id,)
        ).fetchone()
    )
    seed.close()

    application = Application(base_dir=tmp_path)
    try:
        rows = application.db._get_conn().execute(
            "SELECT * FROM monitor_sessions ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        running_after, stopped_after = rows
        assert running_after["status"] == "interrupted"
        assert running_after["ended_at"] is not None
        for field in (
            "room_id",
            "started_at",
            "error_message",
            "web_rid_snapshot",
            "display_name_snapshot",
            "anchor_id_snapshot",
            "snapshot_source",
        ):
            assert running_after[field] == running_before[field]
        assert dict(stopped_after) == stopped_before
    finally:
        application.shutdown()


# ---------- 房间编辑与校验 ----------
def test_add_room_rejects_invalid_web_rid(app):
    assert app.service.add_room("bad rid!") is None
    assert app.service.add_room("") is None
    assert app.service.add_room("a" * 65) is None
    assert app.service.get_rooms() == []


def test_update_room_persists_and_refreshes_list(app):
    room_id = app.service.add_room("edit-old", "旧名称")
    ok, error = app.service.update_room(
        room_id, web_rid="edit-new", display_name="新名称"
    )

    assert ok is True and error == ""
    room = app.db.get_room(room_id)
    assert room.web_rid == "edit-new"
    assert room.display_name == "新名称"
    _wait_for(
        lambda: app.main_window.room_list.item(
            app.main_window.room_list._room_rows[room_id], 1
        ).text()
        == "edit-new"
    )
    assert app.service.get_room(room_id).web_rid == "edit-new"


def test_update_room_rejects_duplicate_rid(app):
    app.service.add_room("taken-rid", "已占用")
    room_id = app.service.add_room("free-rid", "空闲")

    ok, error = app.service.update_room(room_id, web_rid="taken-rid")

    assert ok is False
    assert "已存在" in error
    assert app.db.get_room(room_id).web_rid == "free-rid"


def test_update_room_blocks_rid_change_while_monitoring(app):
    room_id = app.service.add_room("monitor-rid", "监控中")
    app.service.start_monitor(room_id)
    try:
        _wait_for(lambda: app.service.is_monitoring(room_id))
        ok, error = app.service.update_room(room_id, web_rid="other-rid")
        assert ok is False
        assert "监控中" in error

        # 显示名称可以修改
        ok2, _ = app.service.update_room(room_id, display_name="改名")
        assert ok2 is True
        assert app.service.get_room(room_id).display_name == "改名"
    finally:
        app.service.stop_monitor(room_id)


def test_update_room_validates_display_name(app):
    room_id = app.service.add_room("name-rid", "名称")
    ok, error = app.service.update_room(room_id, display_name="x" * 65)
    assert ok is False
    assert "显示名称" in error


def test_room_list_has_recording_column(app):
    room_id = app.service.add_room("widget-rec", "控件测试")
    room_list = app.main_window.room_list
    assert room_list.columnCount() == 7
    room_list.set_recording(room_id, True)
    row = room_list._room_rows[room_id]
    assert room_list.item(row, 6).text() == "是"
    room_list.set_recording(room_id, False)
    assert room_list.item(row, 6).text() == "否"


# ---------- 自动录制集成 ----------
def test_auto_record_disabled_by_default(app):
    room_id = app.service.add_room("auto-off", "默认关闭")
    app.parser.set_response(
        "auto-off",
        ParseResult.SUCCESS,
        2,
        stream_url="https://example.invalid/live.flv",
    )
    started: list[dict] = []
    app.recording.recording_started.connect(lambda rid, snap: started.append(snap))

    app.service.start_monitor(room_id)
    try:
        _wait_for(
            lambda: app.service.get_room(room_id).state.last_confirmed_room_state
            == "LIVE"
        )
        for _ in range(20):
            QApplication.processEvents()
            time.sleep(0.02)
        assert started == []
    finally:
        app.service.stop_monitor(room_id)


def test_auto_record_toggle_persists_setting(app):
    checkbox = app.main_window.chk_auto_record
    assert checkbox.isChecked() is False

    checkbox.setChecked(True)
    assert app._auto_record is True
    assert app.db.get_setting("recording.auto_start", "0") == "1"

    checkbox.setChecked(False)
    assert app._auto_record is False
    assert app.db.get_setting("recording.auto_start", "1") == "0"


def test_recording_history_dialog_shows_records(app):
    from dlrecorder.presentation.widgets import RecordingHistoryDialog

    room_id = app.service.add_room("history-rid", "历史主播")
    recording_id = app.db.start_recording(room_id, "C:/recordings/h.ts")
    app.db.finish_recording(recording_id, "completed", 2048)

    rows = app.recording.list_history(limit=10)
    assert rows[0]["id"] == recording_id
    assert app.recording.output_dir == app.config.recordings_dir

    dialog = RecordingHistoryDialog(rows)
    try:
        assert dialog.table.rowCount() == len(rows)
        assert dialog.table.item(0, 4).text() == "已完成"
        assert dialog.table.item(0, 5).text() == "2.0 KB"
        assert dialog.table.item(0, 6).text() == "C:/recordings/h.ts"
    finally:
        dialog.close()


def test_auto_record_starts_on_live_and_stops_with_monitor(tmp_path, qt_app):
    seed = Database(tmp_path / "data" / "app.db")
    room_id = seed.add_room("auto-live", "自动录制")
    seed.set_setting("recording.auto_start", "1")
    seed.close()

    stub = tmp_path / "stub.py"
    stub.write_text(RECORD_STUB, encoding="utf-8")

    def builder(ffmpeg_path, stream_url, output_path):
        return [sys.executable, str(stub), str(output_path), "stdin"]

    application = Application(
        base_dir=tmp_path,
        recording_options={
            "command_builder": builder,
            "poll_interval_s": 0.05,
            "stall_timeout_s": 0,
            "stop_grace_s": 2.0,
        },
    )
    started: list[dict] = []
    finished: list[dict] = []
    application.recording.recording_started.connect(
        lambda rid, snap: started.append(snap)
    )
    application.recording.recording_finished.connect(
        lambda rid, snap: finished.append(snap)
    )
    application.parser.set_response(
        "auto-live",
        ParseResult.SUCCESS,
        2,
        stream_url="https://example.invalid/live.flv",
    )
    try:
        application.service.start_monitor(room_id)
        _wait_for(lambda: bool(started))

        assert application.recording.is_recording(room_id) is True
        row = application.db.get_recording(started[0]["recording_id"])
        assert row["status"] == "recording"
        assert row["session_id"] is not None
        session = application.db._get_conn().execute(
            "SELECT * FROM monitor_sessions WHERE id=?", (row["session_id"],)
        ).fetchone()
        assert session["room_id"] == room_id
        assert session["status"] == "running"

        application.service.stop_monitor(room_id)
        _wait_for(lambda: bool(finished))
        assert finished[0]["status"] == "completed"
        assert application.recording.is_recording(room_id) is False
    finally:
        application.shutdown()


def test_room_removal_stops_recording_and_keeps_history(tmp_path, qt_app):
    seed = Database(tmp_path / "data" / "app.db")
    room_id = seed.add_room("remove-rec", "待删除录制")
    seed.set_setting("recording.auto_start", "1")
    seed.close()

    stub = tmp_path / "stub.py"
    stub.write_text(RECORD_STUB, encoding="utf-8")

    def builder(ffmpeg_path, stream_url, output_path):
        return [sys.executable, str(stub), str(output_path), "stdin"]

    application = Application(
        base_dir=tmp_path,
        recording_options={
            "command_builder": builder,
            "poll_interval_s": 0.05,
            "stall_timeout_s": 0,
            "stop_grace_s": 2.0,
        },
    )
    started: list[dict] = []
    finished: list[dict] = []
    application.recording.recording_started.connect(
        lambda rid, snap: started.append(snap)
    )
    application.recording.recording_finished.connect(
        lambda rid, snap: finished.append(snap)
    )
    application.parser.set_response(
        "remove-rec",
        ParseResult.SUCCESS,
        2,
        stream_url="https://example.invalid/live.flv",
    )
    try:
        application.service.start_monitor(room_id)
        _wait_for(lambda: bool(started))

        application.service.remove_room(room_id)
        _wait_for(lambda: bool(finished))

        assert application.recording.is_recording(room_id) is False
        row = application.db.get_recording(started[0]["recording_id"])
        assert row is not None
        assert row["room_id"] is None
        assert row["web_rid_snapshot"] == "remove-rec"
    finally:
        application.shutdown()


def test_start_monitor_button_reflects_backend_state(app, monkeypatch):
    """Session 创建失败时，界面不得误显示“监控中=是”。"""
    room_id = app.service.add_room("gui-monitor", "界面状态")
    table = app.main_window.room_list
    table.selectRow(table._room_rows[room_id])
    monkeypatch.setattr(app.service, "start_monitor", lambda rid: None)

    app.main_window._on_start_monitor()

    row = table._room_rows[room_id]
    assert table.item(row, 5).text() == "否"
    assert "未启动" in app.main_window.status_label.text()


# ---------- 数据目录定位 ----------
def test_default_base_dir_uses_executable_when_frozen(monkeypatch, tmp_path):
    """打包运行使用可执行文件所在目录，避免工作目录不同导致数据分裂。"""
    from dlrecorder.application import app as app_module

    monkeypatch.delenv("DLR_DATA_DIR", raising=False)
    monkeypatch.setattr(app_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        app_module.sys, "executable", str(tmp_path / "DouyinLiveRecorder.exe")
    )

    assert app_module._default_base_dir() == tmp_path


def test_default_base_dir_prefers_data_dir_override(monkeypatch, tmp_path):
    """DLR_DATA_DIR 用于隔离验收/测试数据。"""
    from dlrecorder.application import app as app_module

    monkeypatch.setenv("DLR_DATA_DIR", str(tmp_path / "isolated"))

    assert app_module._default_base_dir() == tmp_path / "isolated"


def test_default_base_dir_uses_cwd_for_source_run(monkeypatch):
    """源码运行保持使用当前工作目录。"""
    from dlrecorder.application import app as app_module

    monkeypatch.delenv("DLR_DATA_DIR", raising=False)
    monkeypatch.delattr(app_module.sys, "frozen", raising=False)

    assert app_module._default_base_dir() == Path.cwd()


def test_application_honors_data_dir_override(monkeypatch, tmp_path, qt_app):
    """未显式传入 base_dir 时，应用数据落在 DLR_DATA_DIR 下。"""
    monkeypatch.setenv("DLR_DATA_DIR", str(tmp_path))

    application = Application()
    try:
        assert application.config.data_dir == tmp_path / "data"
        assert application.config.db_path == tmp_path / "data" / "app.db"
        assert application.config.db_path.is_file()
    finally:
        application.shutdown()
