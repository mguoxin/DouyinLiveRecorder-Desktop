"""软件设置服务与设置界面测试（不发起任何网络请求）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from dlrecorder.application.app import Application
from dlrecorder.application.settings_service import (
    AppSettings,
    SettingsService,
    validate_ffmpeg_path,
    validate_min_free_mb,
    validate_monitor_interval,
)
from dlrecorder.infrastructure.config import AppConfig
from dlrecorder.infrastructure.db import Database


@pytest.fixture
def service(tmp_path, qt_app):
    db = Database(tmp_path / "data" / "app.db")
    config = AppConfig.from_base_dir(tmp_path)
    yield SettingsService(db, config), db, config
    db.close()


def test_defaults_without_saved_settings(service, monkeypatch):
    settings_service, _db, config = service
    monkeypatch.setattr(
        "dlrecorder.application.settings_service.find_local_ffmpeg", lambda: "ffmpeg"
    )
    current = settings_service.load()

    assert current.ffmpeg_path == "ffmpeg"
    assert current.recordings_dir == config.recordings_dir
    assert current.monitor_interval_s == 120.0
    assert current.min_free_mb == 512


def test_local_ffmpeg_is_preferred_over_default_command(service, monkeypatch):
    settings_service, db, _config = service
    local = r"C:\project\tools\ffmpeg\bin\ffmpeg.exe"
    monkeypatch.setattr(
        "dlrecorder.application.settings_service.find_local_ffmpeg", lambda: local
    )

    assert settings_service.defaults().ffmpeg_path == local
    # 旧设置保存的是默认命令名时，应自动切换到项目内置 FFmpeg。
    db.set_setting("recording.ffmpeg_path", "ffmpeg")
    assert settings_service.load().ffmpeg_path == local
    # 用户显式保存的其他路径保持不变。
    db.set_setting("recording.ffmpeg_path", r"D:\custom\ffmpeg.exe")
    assert settings_service.load().ffmpeg_path == r"D:\custom\ffmpeg.exe"


def test_save_persists_all_values(service, tmp_path):
    settings_service, db, _config = service
    output_dir = tmp_path / "custom-recordings"
    saved = settings_service.save(
        AppSettings(
            ffmpeg_path=r"C:\tools\ffmpeg\bin\ffmpeg.exe",
            recordings_dir=output_dir,
            monitor_interval_s=45.0,
            min_free_mb=1024,
        )
    )
    assert saved.recordings_dir == output_dir.absolute()
    assert output_dir.is_dir()

    reloaded = settings_service.load()
    assert reloaded == saved
    assert db.get_setting("monitor.interval_s") == "45.0"


def test_invalid_settings_are_rejected_without_writing(service, tmp_path):
    settings_service, db, _config = service

    with pytest.raises(ValueError):
        settings_service.save(
            AppSettings("", tmp_path / "rec", 60.0, 100)
        )
    with pytest.raises(ValueError):
        settings_service.save(
            AppSettings("ffmpeg", tmp_path / "rec", 1.0, 100)
        )
    with pytest.raises(ValueError):
        settings_service.save(
            AppSettings("ffmpeg", tmp_path / "rec", 60.0, -1)
        )
    assert db.get_setting("recording.ffmpeg_path", "") == ""
    assert db.get_setting("monitor.interval_s", "") == ""


def test_invalid_stored_values_fall_back_to_defaults(service):
    settings_service, db, config = service
    db.set_setting("monitor.interval_s", "not-a-number")
    db.set_setting("storage.min_free_mb", "999999999")

    current = settings_service.load()

    assert current.monitor_interval_s == 120.0
    assert current.min_free_mb == 512
    assert current.recordings_dir == config.recordings_dir


def test_validators():
    assert validate_ffmpeg_path("  ffmpeg ") == "ffmpeg"
    assert validate_monitor_interval("30") == 30.0
    assert validate_min_free_mb("0") == 0
    with pytest.raises(ValueError):
        validate_monitor_interval(float("inf"))
    with pytest.raises(ValueError):
        validate_ffmpeg_path("x" * 300)


def test_application_startup_applies_saved_settings(tmp_path, qt_app):
    config = AppConfig.from_base_dir(tmp_path)
    db = Database(config.db_path)
    settings = SettingsService(db, config)
    output_dir = tmp_path / "recordings-custom"
    settings.save(AppSettings("ffmpeg-custom", output_dir, 30.0, 256))
    db.close()

    application = Application(base_dir=tmp_path)
    try:
        assert application.service.monitor_interval_s == 30.0
        assert application.recording.output_dir == output_dir.absolute()
        assert application.recording.min_free_mb == 256.0
        assert application.recording._ffmpeg_path == "ffmpeg-custom"
    finally:
        application.shutdown()


def test_apply_settings_updates_services_at_runtime(tmp_path, qt_app):
    application = Application(base_dir=tmp_path)
    try:
        new_dir = tmp_path / "runtime-recordings"
        saved = application.settings.save(
            AppSettings("ffmpeg-runtime", new_dir, 90.0, 0)
        )
        application._apply_settings(saved)

        assert application.recording.output_dir == new_dir.absolute()
        assert application.recording.min_free_mb == 0.0
        assert application.service.monitor_interval_s == 90.0
    finally:
        application.shutdown()


def test_settings_dialog_saves_and_returns_values(tmp_path, qt_app, service):
    from dlrecorder.presentation.widgets import SettingsDialog

    settings_service, db, _config = service
    dialog = SettingsDialog(settings_service, settings_service.load())
    try:
        dialog.ffmpeg_edit.setText("ffmpeg-dialog")
        dialog.output_edit.setText(str(tmp_path / "dialog-recordings"))
        dialog.interval_spin.setValue(15)
        dialog.min_free_spin.setValue(0)
        dialog._on_accept()

        assert dialog.result_settings is not None
        assert dialog.result_settings.ffmpeg_path == "ffmpeg-dialog"
        assert dialog.result_settings.monitor_interval_s == 15.0
        assert db.get_setting("recording.ffmpeg_path") == "ffmpeg-dialog"
    finally:
        dialog.close()


def test_settings_dialog_blocks_invalid_input(tmp_path, qt_app, service):
    from dlrecorder.presentation.widgets import SettingsDialog

    settings_service, _db, _config = service
    dialog = SettingsDialog(settings_service, settings_service.load())
    try:
        dialog.ffmpeg_edit.setText("   ")
        dialog._on_accept()

        assert dialog.result_settings is None
        assert dialog.error_label.isVisible() or "FFmpeg" in dialog.error_label.text()
    finally:
        dialog.close()


def test_quality_default_and_roundtrip(service, monkeypatch):
    settings_service, db, config = service
    monkeypatch.setattr(
        "dlrecorder.application.settings_service.find_local_ffmpeg", lambda: "ffmpeg"
    )
    assert settings_service.load().quality == "UHD"

    saved = settings_service.save(
        AppSettings("ffmpeg", config.recordings_dir, 60.0, 128, "HD")
    )
    assert saved.quality == "HD"
    assert db.get_setting("recording.quality") == "HD"
    assert settings_service.load().quality == "HD"


def test_invalid_stored_quality_falls_back(service):
    settings_service, db, _config = service
    db.set_setting("recording.quality", "ULTRA")
    assert settings_service.load().quality == "UHD"


def test_validate_cookie_rules():
    from dlrecorder.application.settings_service import validate_cookie

    assert validate_cookie("sessionid=abc; ttwid=xyz") == "sessionid=abc; ttwid=xyz"
    assert validate_cookie("Cookie: sessionid=abc") == "sessionid=abc"
    with pytest.raises(ValueError):
        validate_cookie("   ")
    with pytest.raises(ValueError):
        validate_cookie("sessionid")
    with pytest.raises(ValueError):
        validate_cookie("sessionid=a\nb")
    with pytest.raises(ValueError):
        validate_cookie("a=" + "b" * 9000)


@pytest.mark.skipif(
    __import__("sys").platform != "win32", reason="DPAPI 仅在 Windows 可用"
)
def test_credential_store_encrypts_cookie(tmp_path):
    from dlrecorder.infrastructure.credential_store import CredentialStore

    store = CredentialStore(tmp_path / "data")
    cookie = "sessionid=very-secret-value; ttwid=abc"
    store.save_cookie(cookie)

    assert store.has_cookie()
    assert store.load_cookie() == cookie
    raw = store.path.read_bytes()
    assert b"very-secret-value" not in raw

    assert "已配置" in store.describe()
    store.clear_cookie()
    assert store.load_cookie() is None
    assert not store.has_cookie()


@pytest.mark.skipif(
    __import__("sys").platform != "win32", reason="DPAPI 仅在 Windows 可用"
)
def test_settings_dialog_saves_quality_and_cookie(tmp_path, qt_app, service):
    from dlrecorder.presentation.widgets import SettingsDialog

    settings_service, db, _config = service
    dialog = SettingsDialog(settings_service, settings_service.load())
    try:
        index = dialog.quality_combo.findData("HD")
        dialog.quality_combo.setCurrentIndex(index)
        dialog.cookie_edit.setText("sessionid=dialog-secret; ttwid=abc")
        dialog._on_accept()

        assert dialog.result_settings is not None
        assert dialog.result_settings.quality == "HD"
        assert settings_service.load_cookie() == "sessionid=dialog-secret; ttwid=abc"
        stored = [
            db.get_setting(key, "")
            for key in (
                "recording.ffmpeg_path",
                "recording.output_dir",
                "monitor.interval_s",
                "storage.min_free_mb",
                "recording.quality",
            )
        ]
        assert not any("dialog-secret" in value for value in stored)
    finally:
        dialog.close()

    dialog2 = SettingsDialog(settings_service, settings_service.load())
    try:
        dialog2._on_clear_cookie()
        assert settings_service.load_cookie() is None
        assert "未配置" in dialog2.cookie_status.text()
    finally:
        dialog2.close()
