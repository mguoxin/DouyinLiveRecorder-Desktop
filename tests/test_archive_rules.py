"""归档模板与路径规则测试。"""
from pathlib import Path

import pytest

from dlrecorder.domain.archive_rules import (
    ArchiveRuleError,
    ArchiveSettings,
    build_relative_path,
    ensure_within_root,
    expand_template,
    safe_component,
    validate_template,
)


VALUES = {
    "anchor": "主播/一",
    "web_rid": "12345",
    "date": "2025-01-02",
    "time": "030405",
    "session_id": 7,
    "recording_id": 9,
}


def test_expand_template_supports_whitelist_and_reports_unknown():
    text, unknown = expand_template("{anchor}_{date}_{session_id}", VALUES)
    assert text == "主播/一_2025-01-02_7"
    assert unknown == ()

    text, unknown = expand_template("{anchor}_{oops}", VALUES)
    assert text == "主播/一_{oops}"
    assert unknown == ("oops",)


def test_safe_component_removes_invalid_characters():
    assert safe_component('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_component("   ") == "未命名"
    assert len(safe_component("x" * 200)) == 60


def test_build_relative_path_applies_templates_and_extension():
    settings = ArchiveSettings(
        enabled=True,
        folder_template="{anchor}/{date}",
        name_template="{date}_{time}_{web_rid}",
    )
    relative, unknown = build_relative_path(settings, VALUES, ".TS")
    assert unknown == ()
    assert relative == Path(
        "主播", "一", "2025-01-02", "2025-01-02_030405_12345.TS"
    )


def test_build_relative_path_rejects_traversal_and_absolute_folder():
    traversal = ArchiveSettings(folder_template="../outside")
    with pytest.raises(ArchiveRuleError):
        build_relative_path(traversal, VALUES, ".ts")

    absolute = ArchiveSettings(folder_template="C:/outside")
    with pytest.raises(ArchiveRuleError):
        build_relative_path(absolute, VALUES, ".ts")

    name_traversal = ArchiveSettings(name_template="../escape")
    relative, _ = build_relative_path(name_traversal, VALUES, ".ts")
    assert ".." not in relative.parts
    assert relative.name == "_escape.ts"


def test_build_relative_path_falls_back_for_unsafe_extension():
    settings = ArchiveSettings(folder_template="{anchor}")
    relative, _ = build_relative_path(settings, VALUES, ".exe.part")
    assert relative.suffix == ".ts"


def test_validate_template_rules():
    assert validate_template("{anchor}/{date}", field="模板") == "{anchor}/{date}"
    for bad in ("", "   ", "/absolute", "C:/absolute", "a/../b", "bad\x01"):
        with pytest.raises(ArchiveRuleError):
            validate_template(bad, field="模板")


def test_ensure_within_root(tmp_path: Path):
    root = tmp_path / "archive"
    inside = root / "a" / "b.ts"
    assert ensure_within_root(root, inside) == inside.resolve()
    with pytest.raises(ArchiveRuleError):
        ensure_within_root(root, tmp_path / "outside.ts")
