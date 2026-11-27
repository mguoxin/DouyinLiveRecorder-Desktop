"""日志与诊断脱敏的针对性测试。"""
from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path

from dlrecorder.infrastructure.diagnostics import build_diagnostics_zip
from dlrecorder.infrastructure.logging_config import setup_logging
from dlrecorder.infrastructure.redaction import (
    RedactingFormatter,
    redact_log_text,
    redact_text,
)

HOME = os.path.expanduser("~")


def test_windows_path_with_spaces_is_redacted():
    text = r"ffmpeg=C:\Program Files\My App\ffmpeg.exe，启动"
    assert redact_text(text) == "ffmpeg=<path>，启动"


def test_chinese_path_with_spaces_is_redacted():
    text = r"数据目录=C:\Users\张三 三\AppData\Local\DouyinLiveRecorder\data"
    out = redact_text(text)
    assert "张三" not in out
    assert "AppData" not in out
    assert out.startswith("数据目录=<path>")


def test_path_followed_by_chinese_punctuation_keeps_text():
    text = r"打开 D:\录播\文件.ts）。"
    assert redact_text(text) == "打开 <path>）。"


def test_path_followed_by_ascii_delimiter_keeps_text():
    text = r"open D:\recordings\private.ts failed, code=1"
    out = redact_text(text)
    assert out.startswith("open <path>")
    assert "code=1" in out


def test_forward_slash_path_is_redacted():
    text = "open C:/Users/demo/Desktop/a.log now"
    assert "C:/Users/demo" not in redact_text(text)


def test_secrets_and_urls_are_redacted():
    text = (
        "https://pull.example.com/live.flv?sign=abc&a_bogus=XYZ "
        "Cookie: sessionid=deadbeef "
        "token=plain-secret alice@example.com 192.168.1.10"
    )
    out = redact_text(text)
    for secret in ("pull.example.com", "abc", "XYZ", "deadbeef", "plain-secret",
                   "alice@example.com", "192.168.1.10"):
        assert secret not in out
    assert "<url>" in out
    assert "<redacted>" in out


def test_room_id_is_masked_but_log_keeps_it():
    text = "room=1234567890123456789 size=1234567890"
    masked = redact_text(text)
    assert "1234567890123456789" not in masked
    assert "12" in masked and "89" in masked
    logged = redact_log_text(text)
    assert "1234567890123456789" in logged
    assert "1234567890" in logged


def test_redaction_is_idempotent():
    text = r"path=C:\Program Files\A B\x.log url=https://a.com/?token=x"
    once = redact_text(text)
    assert redact_text(once) == once


def test_formatter_redacts_traceback_and_paths():
    formatter = RedactingFormatter("%(message)s", user_home=HOME)
    record = logging.LogRecord(
        "test.logger", logging.ERROR, __file__, 1,
        r"失败 path=C:\Users\demo\Desktop\a.log", None, None,
    )
    try:
        raise RuntimeError(r"boom C:\Users\demo\Desktop\b.log")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()
    out = formatter.format(record)
    assert "C:\\Users\\demo" not in out
    assert "<path>" in out
    assert "Traceback" in out


def test_setup_logging_writes_redacted_log(tmp_path):
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    try:
        log_dir = tmp_path / "data" / "logs"
        setup_logging(log_dir)
        logging.getLogger("test.redaction").error(
            "数据目录=%s url=https://pull.example.com/live?sign=abc room=%s",
            tmp_path / "data",
            "1234567890123456789",
        )
        for handler in root.handlers:
            handler.flush()
        content = (log_dir / "app.log").read_text(encoding="utf-8")
        assert str(tmp_path) not in content
        assert "pull.example.com" not in content
        assert "sign=abc" not in content
        # 日志保留房间号与普通数字，便于定位
        assert "1234567890123456789" in content
        assert "<data>" in content
        assert "<url>" in content
    finally:
        for handler in root.handlers[:]:
            handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_diagnostic_export_redacts_chinese_path_with_spaces(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    secret_path = r"C:\Users\张三 三\Desktop\录播\秘密.ts"
    (log_dir / "app.log").write_text(
        f"2026-11-27 10:00:00 [ERROR] test: 打开 {secret_path} 失败\n",
        encoding="utf-8",
    )
    dest = tmp_path / "diag.zip"
    build_diagnostics_zip(
        dest,
        app_version="0.4.2-test",
        data_dir=tmp_path,
        log_dir=log_dir,
    )
    with zipfile.ZipFile(dest) as archive:
        combined = "\n".join(
            archive.read(name).decode("utf-8", "replace")
            for name in archive.namelist()
        )
    assert "张三" not in combined
    assert "秘密.ts" not in combined
    assert "<path>" in combined
