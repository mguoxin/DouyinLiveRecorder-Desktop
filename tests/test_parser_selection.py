"""解析器生产装配测试：默认真实解析器，Mock 仅用于开发验证。"""
from __future__ import annotations

from dlrecorder.application.app import Application
from dlrecorder.douyin.engine.parser import EngineDouyinParser
from dlrecorder.douyin.parser import MockParser, RealDouyinParser


def test_production_defaults_to_real_parser(tmp_path, qt_app, monkeypatch):
    monkeypatch.delenv("DLR_PARSER", raising=False)
    application = Application(base_dir=tmp_path)
    try:
        # 主链路为上游引擎解析，旧页面解析器作为第二数据源保留。
        assert isinstance(application.parser, EngineDouyinParser)
        assert isinstance(application.parser._fallback, RealDouyinParser)
    finally:
        application.shutdown()
        application.db.close()


def test_mock_parser_uses_local_stream_url_from_env(tmp_path, qt_app, monkeypatch):
    monkeypatch.setenv("DLR_PARSER", "mock")
    monkeypatch.setenv("DLR_MOCK_STREAM_URL", r"C:\media\sample.flv")
    application = Application(base_dir=tmp_path)
    try:
        assert isinstance(application.parser, MockParser)
        detail = application.parser.fetch_and_parse_detail("any-room")
        assert detail.result.value == "SUCCESS"
        assert detail.status == 2
        assert detail.stream_url == r"C:\media\sample.flv"
    finally:
        application.shutdown()
        application.db.close()
