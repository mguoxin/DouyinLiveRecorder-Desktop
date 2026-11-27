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


# ---------------------------------------------------------------------------
# 第三轮整改：R-1 百分号编码键名 / R-2 全角分隔符
# ---------------------------------------------------------------------------
CN_HOME = r"C:\Users\张三 三"


def test_percent_encoded_secret_key_is_redacted():
    for text in ("to%6Ben=abc", "TO%6BEN=abc", "to%6ben=abc", "%74oken=abc"):
        out = redact_log_text(text)
        assert "abc" not in out, text
        assert "<redacted>" in out, text


def test_percent_encoded_non_secret_key_is_untouched():
    # 明确的边界：只解码键名做分类，普通键名不应被改写。
    text = "name%5Fx=abc"
    assert redact_log_text(text) == text


def test_fullwidth_separator_secret_is_redacted():
    for text in ("token＝abc", "token：abc", "cookie＝sessionid=abc", "token ＝ abc"):
        out = redact_log_text(text)
        assert "abc" not in out, text
        assert "<redacted>" in out, text


# ---------------------------------------------------------------------------
# 第三轮整改：R-3 用户名重复出现 / 同名普通目录 / 其它中文目录
# ---------------------------------------------------------------------------
def test_home_path_masks_repeated_username_and_keeps_structure():
    text = r"C:\Users\张三 三\Desktop\张三 三\项目\a.ts"
    out = redact_log_text(text, user_home=CN_HOME)
    assert out == r"%USERPROFILE%\Desktop\<user>\项目\a.ts"
    assert "张三" not in out


def test_home_path_masks_repeated_username_with_forward_slashes():
    text = "C:/Users/张三 三/Desktop/张三 三/a.ts"
    out = redact_log_text(text, user_home=CN_HOME)
    assert out == r"%USERPROFILE%\Desktop\<user>\a.ts"
    assert "张三" not in out


def test_home_path_keeps_unrelated_chinese_directories():
    text = r"C:\Users\张三 三\Desktop\归档\录播\a.ts"
    out = redact_log_text(text, user_home=CN_HOME)
    assert out == r"%USERPROFILE%\Desktop\归档\录播\a.ts"
    assert "张三" not in out


def test_same_name_directory_outside_home_is_fully_masked():
    # 非用户目录（其它盘符）中的同名目录：整条路径替换，不泄露且无需逐段保留。
    text = r"D:\资料\张三 三\a.ts"
    assert redact_log_text(text, user_home=CN_HOME) == "<path>"


def test_data_dir_path_is_replaced_even_with_trailing_text():
    data_dir = r"C:\Users\张三 三\AppData\Local\App\data"
    text = f"数据目录={data_dir} size=524288"
    out = redact_log_text(text, user_home=CN_HOME, data_dir=data_dir)
    assert out == "数据目录=<data> size=524288"


# ---------------------------------------------------------------------------
# 第三轮整改：R-4 含分号的 Windows 合法路径
# ---------------------------------------------------------------------------
def test_semicolon_path_does_not_leave_tail():
    assert redact_log_text(r"C:\Users\demo;x\a.log") == "<path>"


def test_semicolon_inside_filename_is_part_of_path():
    assert redact_log_text(r"E:\record;take\2026\a.ts") == "<path>"


def test_semicolon_path_under_home_keeps_structure_without_username():
    out = redact_log_text(r"C:\Users\张三 三\a;b\c.log", user_home=CN_HOME)
    assert out == r"%USERPROFILE%\a;b\c.log"
    assert "张三" not in out


def test_trailing_prose_after_path_is_preserved():
    out = redact_log_text(r"open D:\recordings\private.ts failed, code=1")
    assert out.startswith("open <path>")
    assert "recordings" not in out
    assert out.endswith(", code=1")


# ---------------------------------------------------------------------------
# 第三轮整改：B5 诊断数字误伤
# ---------------------------------------------------------------------------
def test_diagnostics_keeps_ordinary_long_numbers():
    text = "size=1234567890 elapsed=1234567 ts=1764200000"
    assert redact_text(text) == text


def test_diagnostics_masks_ids_by_semantics():
    masked = redact_text("room=1234567890123456789 web_rid=95694179922 uid=1234567890123")
    for secret in ("1234567890123456789", "95694179922", "1234567890123"):
        assert secret not in masked
    assert "12" in masked and "89" in masked


def test_diagnostics_masks_bare_snowflake_id():
    masked = redact_text("id 1234567890123456789 end")
    assert "1234567890123456789" not in masked
    # 13 位时间戳/毫秒不应被掩码
    assert "1764200000123" in redact_text("ts 1764200000123")


def test_identifier_masking_is_idempotent():
    once = redact_text("room=1234567890123456789 size=1234567890")
    assert redact_text(once) == once


# ---------------------------------------------------------------------------
# 第三轮整改：traceback / 落盘 / 诊断一致性 / 正向不误伤
# ---------------------------------------------------------------------------
def test_formatter_redacts_encoded_and_fullwidth_secrets_in_traceback():
    formatter = RedactingFormatter("%(message)s", user_home=CN_HOME)
    record = logging.LogRecord(
        "test.logger", logging.ERROR, __file__, 1,
        r"失败 to%6Ben=abc token＝def", None, None,
    )
    try:
        raise RuntimeError(r"boom C:\Users\张三 三\Desktop\张三 三\a.ts")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()
    out = formatter.format(record)
    assert "abc" not in out
    assert "def" not in out
    assert "张三" not in out
    assert "<user>" in out
    assert "Traceback" in out


def test_setup_logging_redacts_new_patterns(tmp_path):
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    try:
        log_dir = tmp_path / "data" / "logs"
        setup_logging(log_dir)
        # 文件与控制台处理器必须挂同一套脱敏格式化器
        assert root.handlers
        assert all(isinstance(h.formatter, RedactingFormatter) for h in root.handlers)
        logging.getLogger("test.redaction").error(
            "to%%6Ben=abc token＝def path=%s",
            r"D:\record;take\a.ts",
        )
        for handler in root.handlers:
            handler.flush()
        content = (log_dir / "app.log").read_text(encoding="utf-8")
        assert "abc" not in content
        assert "def" not in content
        assert "record;take" not in content
        assert "<redacted>" in content
        assert "<path>" in content
    finally:
        for handler in root.handlers[:]:
            handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_diagnostic_export_consistent_with_log_redaction(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "app.log").write_text(
        "[ERROR] test: to%6Ben=abc token＝def path=C:\\Users\\demo;x\\a.log\n"
        "[ERROR] test: room 95694179922 size=1234567890 ts=1764200000\n",
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
    for secret in ("abc", "def", "demo;x", "95694179922"):
        assert secret not in combined, secret
    # 普通数字保持可诊断性
    assert "size=1234567890" in combined
    assert "ts=1764200000" in combined
    assert "<redacted>" in combined


def test_normal_log_line_is_not_over_masked():
    text = "监控开始 房间=示例 状态=直播中 大小=524288 耗时=12.5s"
    assert redact_log_text(text) == text
