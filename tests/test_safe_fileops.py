"""安全文件操作测试：校验复制、冲突处理、空间检查与回收站。"""
import os
import shutil
from pathlib import Path

import pytest

from dlrecorder.infrastructure import safe_fileops
from dlrecorder.infrastructure.safe_fileops import (
    SafeFileOpsError,
    copy_file_verified,
    recycle_paths,
    resolve_conflict_path,
    sha256_file,
)


def test_copy_file_verified_copies_and_returns_hash(tmp_path: Path):
    source = tmp_path / "source.ts"
    source.write_bytes(b"recording-data" * 1000)
    target = tmp_path / "archive" / "target.ts"

    digest = copy_file_verified(source, target)

    assert target.read_bytes() == source.read_bytes()
    assert digest == sha256_file(source)
    assert not list(target.parent.glob("*.part"))
    assert source.exists()


def test_copy_file_verified_rejects_mismatched_expected_hash(tmp_path: Path):
    source = tmp_path / "source.ts"
    source.write_bytes(b"data")
    target = tmp_path / "target.ts"

    with pytest.raises(SafeFileOpsError, match="发生变化"):
        copy_file_verified(source, target, expected_sha256="0" * 64)
    assert not target.exists()
    assert not (tmp_path / "target.ts.part").exists()
    assert source.read_bytes() == b"data"


def test_copy_file_verified_cleans_partial_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.ts"
    source.write_bytes(b"x" * 4096)
    target = tmp_path / "target.ts"

    def broken_hash(path, *, chunk_size=0):
        raise OSError("forced hash failure")

    monkeypatch.setattr(safe_fileops, "sha256_file", broken_hash)
    with pytest.raises(OSError, match="forced hash failure"):
        copy_file_verified(source, target)
    assert not target.exists()
    assert not (tmp_path / "target.ts.part").exists()
    assert source.exists()


def test_missing_source_raises(tmp_path: Path):
    with pytest.raises(SafeFileOpsError, match="源文件不存在"):
        copy_file_verified(tmp_path / "missing.ts", tmp_path / "target.ts")


def test_resolve_conflict_path_returns_suffix_for_different_content(tmp_path: Path):
    target = tmp_path / "a.ts"
    target.write_bytes(b"old")
    source = tmp_path / "source.ts"
    source.write_bytes(b"new")

    resolved, duplicate = resolve_conflict_path(target, sha256_file(source))
    assert duplicate is False
    assert resolved == tmp_path / "a_1.ts"
    assert target.read_bytes() == b"old"


def test_resolve_conflict_path_detects_identical_copy(tmp_path: Path):
    target = tmp_path / "a.ts"
    target.write_bytes(b"same")
    source = tmp_path / "source.ts"
    source.write_bytes(b"same")

    resolved, duplicate = resolve_conflict_path(target, sha256_file(source))
    assert duplicate is True
    assert resolved == target


def test_resolve_conflict_path_skips_existing_suffixes(tmp_path: Path):
    target = tmp_path / "a.ts"
    target.write_bytes(b"old")
    (tmp_path / "a_1.ts").write_bytes(b"one")
    source = tmp_path / "source.ts"
    source.write_bytes(b"new")

    resolved, duplicate = resolve_conflict_path(target, sha256_file(source))
    assert duplicate is False
    assert resolved == tmp_path / "a_2.ts"


def test_ensure_free_space_rejects_when_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real_disk_usage = shutil.disk_usage

    def fake_disk_usage(path):
        return real_disk_usage(path)._replace(free=1024 * 1024)

    monkeypatch.setattr(safe_fileops.shutil, "disk_usage", fake_disk_usage)
    with pytest.raises(safe_fileops.InsufficientSpaceError):
        safe_fileops.ensure_free_space(tmp_path, 4 * 1024 * 1024)
    safe_fileops.ensure_free_space(tmp_path, 64 * 1024)


@pytest.mark.skipif(os.name != "nt", reason="回收站仅在 Windows 可用")
def test_recycle_paths_moves_file_out_of_place(tmp_path: Path):
    victim = tmp_path / "to-recycle.txt"
    victim.write_text("temporary", encoding="utf-8")
    recycle_paths([victim])
    assert not victim.exists()


def test_recycle_paths_rejects_missing_file(tmp_path: Path):
    with pytest.raises(SafeFileOpsError, match="不存在"):
        recycle_paths([tmp_path / "missing.txt"])
