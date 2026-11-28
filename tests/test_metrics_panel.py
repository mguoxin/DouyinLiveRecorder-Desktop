"""P1-04 指标面板与房间页集成测试。"""
import pytest
from PySide6.QtWidgets import QApplication

from dlrecorder.presentation.metrics_panel import MetricsPanel, SparklineWidget
from dlrecorder.presentation.pages import RoomsPage


def _payload(**overrides) -> dict:
    data = {
        "room_id": 1,
        "status": "recording",
        "file_size": 1024 * 1024,
        "segment_seconds": 42,
        "session_bytes": 3 * 1024 * 1024,
        "session_segments": 2,
        "write_speed_bps": 1.6 * 1024 * 1024,
        "stall_seconds": 0,
        "health": "good",
        "trend": [1000, 2000, 3000],
        "attempts": 0,
        "max_attempts": 5,
    }
    data.update(overrides)
    return data


def test_metrics_panel_formats_recording_snapshot(qt_app):
    panel = MetricsPanel()
    try:
        panel.update_metrics(_payload())
        assert panel.status_label.text() == "录制中"
        assert "1.6 MB/s" in panel.speed_label.text()
        assert "00:00:42" in panel.duration_label.text()
        assert "1.0 MB" in panel.size_label.text()
        assert "2 个分段" in panel.session_label.text()
        assert "良好" in panel.health_label.text()
    finally:
        panel.deleteLater()


def test_metrics_panel_reconnecting_and_stalled_states(qt_app):
    panel = MetricsPanel()
    try:
        panel.update_metrics(_payload(status="reconnecting", attempts=2, max_attempts=5))
        assert panel.status_label.text() == "重连中 2/5"
        assert panel.speed_label.text() == "写入 —"

        panel.update_metrics(_payload(health="stalled", stall_seconds=45))
        assert "停滞 45 秒" in panel.health_label.text()
    finally:
        panel.deleteLater()


def test_metrics_panel_empty_state_and_collapse(qt_app):
    panel = MetricsPanel()
    try:
        panel.update_metrics(None)
        assert "未录制" in panel.status_label.text()
        assert panel.status_label.text() == "该直播间当前未录制"

        assert panel.collapsed is False
        panel.toggle()
        assert panel.collapsed is True
        assert panel.body.isHidden()
        assert panel.toggle_button.text() == "展开"
        panel.toggle()
        assert panel.collapsed is False
        assert not panel.body.isHidden()
    finally:
        panel.deleteLater()


def test_sparkline_handles_empty_and_full_data(qt_app):
    widget = SparklineWidget()
    try:
        widget.resize(120, 32)
        widget.set_points(())
        widget.repaint()
        widget.set_points(tuple(range(120)))
        widget.repaint()
    finally:
        widget.deleteLater()


def test_rooms_page_shows_panel_only_for_selection(qt_app):
    page = RoomsPage()
    try:
        page.room_list.add_room(1, "rid-1", "主播一")
        page.show()
        QApplication.processEvents()
        assert page.metrics_panel.isHidden()

        page.set_metrics_provider(lambda room_id: _payload(room_id=room_id))
        page.room_list.selectRow(0)
        QApplication.processEvents()
        assert page.metrics_panel.isVisible()
        assert page.metrics_panel.status_label.text() == "录制中"
        assert page._metrics_timer.isActive()
        # 固定 7 列契约不被指标面板破坏。
        assert page.room_list.columnCount() == 7

        page.room_list.clearSelection()
        QApplication.processEvents()
        assert page.metrics_panel.isHidden()
        assert not page._metrics_timer.isActive()
    finally:
        page.close()
        page.deleteLater()
