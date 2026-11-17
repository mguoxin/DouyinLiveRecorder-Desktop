"""PyInstaller 打包入口（使用绝对导入，不能直接用 dlrecorder/__main__.py 作为脚本）。"""
from __future__ import annotations

import sys

from dlrecorder.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
