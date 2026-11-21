"""从应用内绘制的品牌图标生成 Windows 多尺寸 .ico（构建期使用）。

图标图形完全复用 ``dlrecorder.presentation.icons._logo_pixmap``，
不引入新的视觉设计；输出 ``build/assets/app.ico``。

用法（项目根目录）：
    .venv\\Scripts\\python.exe build\\make_icon.py
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from dlrecorder.presentation.icons import _logo_pixmap  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SUPERSAMPLE = 4
OUT = ROOT / "build" / "assets" / "app.ico"


def render_png(size: int) -> bytes:
    """以 4 倍超采样绘制后缩放，保证小尺寸边缘清晰。"""
    big = _logo_pixmap(size, SUPERSAMPLE).toImage()
    image: QImage = big.scaled(
        size,
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(QByteArray(buffer.data()))
    buffer.close()
    return data


def build_ico(entries: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    payload = b""
    for size, data in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        offset += len(data)
        payload += data
    return header + directory + payload


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    entries = [(size, render_png(size)) for size in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(build_ico(entries))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes), sizes={SIZES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
