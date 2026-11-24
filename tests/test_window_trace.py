"""窗口生命周期诊断埋点测试。"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import QMainWindow

from dlrecorder.presentation import window_trace


def test_trace_disabled_does_nothing(monkeypatch, caplog):
    monkeypatch.setattr(window_trace, "_ENABLED", False)

    class Exploding:
        def windowState(self):
            raise AssertionError("禁用时不得访问窗口状态")

    with caplog.at_level(logging.INFO):
        window_trace.trace(Exploding(), "show")
    assert caplog.records == []


def test_trace_writes_snapshot_when_enabled(qt_app, monkeypatch, caplog):
    monkeypatch.setattr(window_trace, "_ENABLED", True)
    window = QMainWindow()
    window.resize(640, 480)
    try:
        with caplog.at_level(
            logging.INFO, logger="dlrecorder.presentation.window_trace"
        ):
            window_trace.trace(window, "show", extra="value")
    finally:
        window.close()
    messages = [record.getMessage() for record in caplog.records]
    assert any("window-trace" in message for message in messages)
    assert any("event=show" in message for message in messages)


def test_trace_swallows_broken_widget(monkeypatch, caplog):
    monkeypatch.setattr(window_trace, "_ENABLED", True)

    class Broken:
        def windowState(self):
            raise RuntimeError("boom")

    with caplog.at_level(logging.DEBUG):
        window_trace.trace(Broken(), "show")
