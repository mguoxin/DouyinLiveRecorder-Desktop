# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows onedir）。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean build\\pyinstaller.spec

注意：未安装 PyInstaller 时无法构建；生成的程序尚未在低配机器上验证。
"""
from pathlib import Path

project_root = Path(SPECPATH).resolve().parent
src_dir = project_root / "src"

# 将项目内置 FFmpeg 一并打包；未安装时仍可构建（运行时回退到 PATH）。
ffmpeg_bin = project_root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
datas = [(str(ffmpeg_bin), "ffmpeg/bin")] if ffmpeg_bin.is_file() else []

a = Analysis(
    [str(project_root / "build" / "entry.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DouyinLiveRecorder",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project_root / "build" / "assets" / "app.ico"),
    version=str(project_root / "build" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DouyinLiveRecorder",
)
