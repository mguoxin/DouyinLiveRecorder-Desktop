"""核验最终 EXE 内嵌图标与 build/assets/app.ico 是否逐尺寸一致。

不要只看 PyInstaller 日志里的 "Copying icon to EXE"：本脚本直接读取 PE 的
RT_GROUP_ICON / RT_ICON 资源，与本 ICO 的每个尺寸逐字节比较。

用法（项目根目录）：
    .venv\\Scripts\\python.exe build\\verify_icon.py [EXE 路径...]

未指定路径时核验 release/DouyinLiveRecorder-0.4.0-win64/DouyinLiveRecorder.exe
与 dist/DouyinLiveRecorder/DouyinLiveRecorder.exe。
"""
from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

import pefile

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "build" / "assets" / "app.ico"
DEFAULT_TARGETS = (
    ROOT / "release" / "DouyinLiveRecorder-0.4.0-win64" / "DouyinLiveRecorder.exe",
    ROOT / "dist" / "DouyinLiveRecorder" / "DouyinLiveRecorder.exe",
)


def ico_entries(ico: bytes) -> dict[int, bytes]:
    count = struct.unpack("<H", ico[4:6])[0]
    entries: dict[int, bytes] = {}
    for i in range(count):
        w, _h, _c, _r, _p, _b, size, off = struct.unpack(
            "<BBBBHHII", ico[6 + 16 * i : 22 + 16 * i]
        )
        entries[w or 256] = ico[off : off + size]
    return entries


def pe_icons(pe: pefile.PE) -> tuple[dict[int, bytes], dict[int, bytes]]:
    groups: dict[int, bytes] = {}
    icons: dict[int, bytes] = {}
    if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        return groups, icons
    for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if entry.id not in (3, 14):
            continue
        bucket = groups if entry.id == 14 else icons
        for item in entry.directory.entries:
            data = item.directory.entries[0].data.struct
            bucket[int(item.id)] = pe.get_data(data.OffsetToData, data.Size)
    return groups, icons


def check(path: Path, expected: dict[int, bytes]) -> bool:
    print(f"\n== {path}")
    if not path.exists():
        print("  缺失")
        return False
    raw = path.read_bytes()
    print(f"  sha256={hashlib.sha256(raw).hexdigest().upper()}")
    pe = pefile.PE(data=raw, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
    )
    groups, icons = pe_icons(pe)
    pe.close()
    print(f"  RT_GROUP_ICON={sorted(groups)} RT_ICON={sorted(icons)}")
    if len(groups) != 1:
        print("  失败：图标组数量不是 1")
        return False
    blob = next(iter(groups.values()))
    count = struct.unpack("<H", blob[4:6])[0]
    ok = count == len(expected)
    for i in range(count):
        base = 6 + 14 * i
        w, _h, _c, _r, _p, _b, _size, rid = struct.unpack(
            "<BBBBHHIH", blob[base : base + 14]
        )
        size = w or 256
        same = icons.get(rid) == expected.get(size)
        ok = ok and same
        print(f"  {size:>3}px iconid={rid} match={same}")
    missing = sorted(set(expected) - {
        struct.unpack("<BBBBHHIH", blob[6 + 14 * i : 20 + 14 * i])[0] or 256
        for i in range(count)
    })
    if missing:
        print(f"  失败：缺少尺寸 {missing}")
        ok = False
    print("  结果：", "通过" if ok else "不通过")
    return ok


def main(argv: list[str]) -> int:
    expected = ico_entries(ICO.read_bytes())
    print("app.ico 尺寸:", sorted(expected))
    targets = [Path(a) for a in argv[1:]] or list(DEFAULT_TARGETS)
    results = [check(t, expected) for t in targets]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
