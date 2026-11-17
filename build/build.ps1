# 构建 Windows 可执行程序（需已安装 PyInstaller，安装属于需授权操作）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "未找到项目虚拟环境: $python"
    exit 1
}

& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "未安装 PyInstaller。请在获得授权后执行："
    Write-Host "  .venv\Scripts\python.exe -m pip install pyinstaller"
    Write-Host "然后重新运行本脚本。"
    exit 1
}

Push-Location $root
try {
    & $python -m PyInstaller --noconfirm --clean "build\pyinstaller.spec"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "构建失败，退出码 $LASTEXITCODE"
        exit $LASTEXITCODE
    }
    Write-Host "构建完成: dist\DouyinLiveRecorder\DouyinLiveRecorder.exe"
} finally {
    Pop-Location
}
