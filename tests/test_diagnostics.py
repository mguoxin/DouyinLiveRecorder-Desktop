"""诊断导出安全与边界测试。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sqlite3
import zipfile
from pathlib import Path

import pytest

from dlrecorder.infrastructure import diagnostics
from dlrecorder.infrastructure.db import Database
from dlrecorder.infrastructure.diagnostics import (
    DiagnosticsCancelled,
    DiagnosticsError,
    build_diagnostics_zip,
)


def _write_logs(data_dir: Path, app_lines: list[str], ffmpeg_lines: list[str] | None = None) -> Path:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "app.log").write_text("\n".join(app_lines) + "\n", encoding="utf-8")
    (log_dir / "ffmpeg-stderr.log").write_text(
        "\n".join(ffmpeg_lines or []) + "\n", encoding="utf-8"
    )
    return log_dir


def _export(data_dir: Path, dest: Path, **kwargs):
    return build_diagnostics_zip(
        dest,
        app_version="0.4.2-test",
        data_dir=data_dir,
        log_dir=data_dir / "logs",
        **kwargs,
    )


@pytest.fixture()
def data_dir(tmp_path):
    home = os.path.expanduser("~")
    log_dir = _write_logs(
        tmp_path,
        [
            "2026-11-23 21:03:11 [ERROR] dlrecorder.test: "
            "failed https://pull-abc.live.com/live.flv?sign=abc123&a_bogus=XYZ",
            "2026-11-23 21:03:12 [ERROR] dlrecorder.test: "
            "Cookie: sessionid=supersecret",
            f"2026-11-23 21:03:13 [ERROR] dlrecorder.test: open {home}\\Desktop\\secret.txt",
            "2026-11-23 21:03:13 [ERROR] dlrecorder.test: output D:\\recordings\\private.ts",
            "2026-11-23 21:03:14 [ERROR] dlrecorder.test: "
            "contact alice@example.com from 192.168.1.10",
            "2026-11-23 21:03:15 [ERROR] dlrecorder.test: room 95694179922 failed",
            "2026-11-23 21:03:16 [ERROR] dlrecorder.test: token=plain-secret",
            "2026-11-23 21:03:17 [INFO] dlrecorder.test: normal info line",
        ],
        [
            "https://pull-abc.live.com/live.flv?sign=deadbeef",
            "Cookie: sessionid=ffmpegsecret",
            "frame dropped",
        ],
    )
    assert log_dir.is_dir()
    return tmp_path


def test_sensitive_values_are_redacted(data_dir):
    dest = data_dir / "diag.zip"
    _export(data_dir, dest)
    with zipfile.ZipFile(dest) as archive:
        combined = "\n".join(
            archive.read(name).decode("utf-8", "replace")
            for name in archive.namelist()
        )
    for secret in (
        "supersecret",
        "ffmpegsecret",
        "abc123",
        "deadbeef",
        "alice@example.com",
        "192.168.1.10",
        "95694179922",
        "plain-secret",
        "pull-abc.live.com",
    ):
        assert secret not in combined, secret
    assert "95*******22" in combined
    assert "https://" not in combined
    assert "<redacted>" in combined
    assert "<url>" in combined
    assert "<path>" in combined


def test_zip_contains_only_expected_entries_and_no_database(data_dir):
    db = Database(data_dir / "app.db")
    try:
        db.add_room("diag-room", "诊断主播")
    finally:
        db.close()

    dest = data_dir / "diag.zip"
    _export(data_dir, dest)
    with zipfile.ZipFile(dest) as archive:
        names = set(archive.namelist())
        assert names == {
            "report.json",
            "report.md",
            "logs/app-errors.txt",
            "logs/ffmpeg-stderr.txt",
        }
        for name in names:
            payload = archive.read(name)
            assert b"SQLite format 3" not in payload
            assert not name.endswith(".db")
        report = json.loads(archive.read("report.json").decode("utf-8"))
    assert report["schema"] == 1
    assert report["app"]["version"] == "0.4.2-test"
    assert report["database"]["schema_version"] == 3
    assert report["database"]["quick_check"] == "ok"
    assert report["database"]["rooms"] == 1
    assert report["privacy"]["excluded"]
    assert report["truncated"] is False


def test_database_is_read_only_during_export(data_dir):
    db = Database(data_dir / "app.db")
    db.close()
    db_path = data_dir / "app.db"
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    _export(data_dir, data_dir / "diag.zip")
    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert before == after


def test_large_log_is_capped(data_dir):
    log_dir = data_dir / "logs"
    lines = [
        "2026-11-23 22:00:00 [ERROR] dlrecorder.test: "
        + base64.b64encode(os.urandom(300)).decode()
        for _ in range(6000)
    ]
    (log_dir / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    dest = data_dir / "diag.zip"
    _export(data_dir, dest)
    assert dest.stat().st_size <= diagnostics.MAX_ZIP_BYTES
    with zipfile.ZipFile(dest) as archive:
        report = json.loads(archive.read("report.json").decode("utf-8"))
        errors = archive.read("logs/app-errors.txt").decode("utf-8").splitlines()
    assert report["errors"]["app_log_lines_scanned"] < len(lines)
    assert 0 < len(errors) <= len(lines)


def test_size_limit_triggers_truncation(monkeypatch, data_dir):
    random_lines = [
        "2026-11-23 22:00:00 [ERROR] dlrecorder.test: "
        + base64.b64encode(os.urandom(300)).decode()
        for _ in range(2000)
    ]
    monkeypatch.setattr(
        diagnostics,
        "_collect_app_errors",
        lambda *_args, **_kwargs: (random_lines, len(random_lines), 0),
    )
    monkeypatch.setattr(diagnostics, "MAX_ZIP_BYTES", 64 * 1024)
    dest = data_dir / "diag.zip"
    _export(data_dir, dest)
    assert dest.stat().st_size <= 64 * 1024
    with zipfile.ZipFile(dest) as archive:
        report = json.loads(archive.read("report.json").decode("utf-8"))
    assert report["truncated"] is True
    assert report["errors"]["app_error_lines"] < len(random_lines)


def test_cancel_produces_no_output(data_dir):
    import threading

    cancel = threading.Event()
    cancel.set()
    dest = data_dir / "cancelled.zip"
    with pytest.raises(DiagnosticsCancelled):
        _export(data_dir, dest, cancel_event=cancel)
    assert not dest.exists()
    assert not list(data_dir.glob("*.tmp"))


def test_disk_write_failure_cleans_temp(data_dir):
    blocker = data_dir / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    dest = blocker / "diag.zip"
    with pytest.raises(DiagnosticsError):
        _export(data_dir, dest)
    assert blocker.is_file()


def test_replace_failure_cleans_temp(monkeypatch, data_dir):
    def broken_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(diagnostics.os, "replace", broken_replace)
    dest = data_dir / "diag.zip"
    with pytest.raises(DiagnosticsError):
        _export(data_dir, dest)
    assert not dest.exists()
    assert not list(data_dir.glob(".*.tmp"))


def test_existing_target_is_not_overwritten(data_dir):
    first = data_dir / "diag.zip"
    _export(data_dir, first)
    first_bytes = first.read_bytes()
    second = _export(data_dir, first)
    assert second != first
    assert first.read_bytes() == first_bytes
    assert second.exists()


def test_export_does_not_open_network_sockets(monkeypatch, data_dir):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("诊断导出不得创建网络套接字")

    monkeypatch.setattr(socket, "socket", forbidden)
    dest = data_dir / "diag.zip"
    _export(data_dir, dest)
    assert dest.exists()


def test_database_busy_does_not_block_export(data_dir):
    db = Database(data_dir / "app.db")
    db.close()
    conn = sqlite3.connect(data_dir / "app.db", isolation_level=None)
    conn.execute("PRAGMA locking_mode=EXCLUSIVE")
    conn.execute("BEGIN EXCLUSIVE")
    try:
        dest = data_dir / "diag.zip"
        _export(data_dir, dest)
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert dest.exists()
    with zipfile.ZipFile(dest) as archive:
        report = json.loads(archive.read("report.json").decode("utf-8"))
    assert report["database"]["quick_check"] in {"ok", "busy", "error"}
