# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows onedir）。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean build\\pyinstaller.spec

注意：未安装 PyInstaller 时无法构建；生成的程序尚未在低配机器上验证。
"""
from pathlib import Path

project_root = Path(SPECPATH).resolve().parent
src_dir = project_root / "src"

# 将项目内置 FFmpeg 一并打包（LGPL 共享库构建：ffmpeg.exe + 同目录 DLL）；
# 未安装时仍可构建（运行时回退到 PATH）。
ffmpeg_bin_dir = project_root / "tools" / "ffmpeg" / "bin"
datas = []
if ffmpeg_bin_dir.is_dir():
    for item in sorted(ffmpeg_bin_dir.iterdir()):
        if item.is_file() and item.suffix.lower() in {".exe", ".dll"}:
            datas.append((str(item), "ffmpeg/bin"))
# 随包附上 FFmpeg 许可与构建说明，以及本项目 MIT LICENSE、第三方通知与许可全文。
for extra in (
    project_root / "tools" / "ffmpeg" / "LICENSE",
    project_root / "tools" / "ffmpeg" / "README.txt",
):
    if extra.is_file():
        datas.append((str(extra), "ffmpeg"))
for extra in (project_root / "LICENSE", project_root / "THIRD_PARTY_NOTICES.txt"):
    if extra.is_file():
        datas.append((str(extra), "."))
licenses_dir = project_root / "LICENSES"
if licenses_dir.is_dir():
    for item in sorted(licenses_dir.iterdir()):
        if item.is_file():
            datas.append((str(item), "LICENSES"))

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

# PyInstaller 6.x 会把 datas 中的 PE 文件重新归类为 BINARY 并做依赖扫描，
# 于是除 ffmpeg\bin 之外还会在 _internal 根目录再放一份同名 FFmpeg DLL
# （共享构建下约 152MB 的重复内容）。ffmpeg.exe 与这些 DLL 同目录，
# Windows 会优先从 exe 所在目录加载，根目录副本不参与运行，这里移除。
_ffmpeg_bin_prefix = str(ffmpeg_bin_dir).lower() + "\\"
a.binaries = [
    (dest, src, code)
    for dest, src, code in a.binaries
    if not (
        str(src).lower().startswith(_ffmpeg_bin_prefix)
        and "\\" not in dest
        and "/" not in dest
    )
]

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
