"""更新检查测试：版本比较、清单校验与本地 file:// 读取。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dlrecorder.infrastructure.update_check import (
    UpdateCheckError,
    check_for_update,
    describe_update,
    is_newer,
    parse_manifest,
    parse_version,
)


def test_parse_version_and_compare() -> None:
    assert parse_version("1.0.0") == (1, 0, 0)
    assert parse_version("v2.3") == (2, 3)
    assert is_newer("1.0.0", "1.0.1") is True
    assert is_newer("1.0.0", "1.0.0") is False
    assert is_newer("1.2.0", "1.1.9") is False
    assert is_newer("1.0", "1.0.1") is True
    with pytest.raises(UpdateCheckError):
        parse_version("abc")


def test_parse_manifest_validates_fields() -> None:
    info = parse_manifest(
        json.dumps({"version": "1.0.1", "url": "https://example.com/a.zip", "notes": "修复"}),
        "1.0.0",
    )
    assert info.update_available is True
    assert info.latest_version == "1.0.1"
    assert info.download_url.startswith("https://")
    assert "新版本" in describe_update(info)

    same = parse_manifest(json.dumps({"version": "1.0.0"}), "1.0.0")
    assert same.update_available is False
    assert "最新版本" in describe_update(same)

    with pytest.raises(UpdateCheckError):
        parse_manifest("not json", "1.0.0")
    with pytest.raises(UpdateCheckError):
        parse_manifest(json.dumps({"url": "https://example.com"}), "1.0.0")
    with pytest.raises(UpdateCheckError):
        parse_manifest(json.dumps({"version": "2.0.0", "url": "ftp://x"}), "1.0.0")


def test_check_for_update_reads_local_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "update.json"
    manifest.write_text(
        json.dumps({"version": "1.1.0", "url": "https://example.com/dlr.zip"}),
        encoding="utf-8",
    )
    info = check_for_update(manifest.as_uri(), "1.0.0")
    assert info.update_available is True
    assert info.latest_version == "1.1.0"


def test_check_for_update_rejects_bad_url() -> None:
    with pytest.raises(UpdateCheckError):
        check_for_update("", "1.0.0")
    with pytest.raises(UpdateCheckError):
        check_for_update("ftp://example.com/x.json", "1.0.0")
