<#
低配机器验收脚本（Windows 10/11，PowerShell 5.1 兼容）。

用途：在目标低配机器上采集“机器信息 + 性能基准 + 打包程序启动冒烟”证据，
输出单个 JSON 文件，便于回传后判定是否通过验收。

用法（在项目根目录）：
  # 只看机器信息
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\acceptance_low_spec.ps1 -Mode info

  # 性能基准（默认；需要 Python 3.12，首次会创建 .venv-acceptance 并安装依赖）
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\acceptance_low_spec.ps1 -Mode benchmark

  # 打包程序 GUI 冒烟（需要桌面会话；默认采样 600 秒后请求关闭窗口）
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\acceptance_low_spec.ps1 -Mode gui

  # 依次执行 info + benchmark + gui
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\acceptance_low_spec.ps1 -Mode all

远程执行（SSH，目标机器已开启 OpenSSH Server）：
  ssh <user>@<host> "powershell -NoProfile -ExecutionPolicy Bypass -File C:\dlr\tools\acceptance_low_spec.ps1 -Mode benchmark -OutputDir C:\dlr\acceptance"
  然后取回 C:\dlr\acceptance\acceptance-*.json

说明：
- 不读写正式 data 目录；GUI 冒烟使用隔离工作目录（<OutputDir>\gui-data）。
- 不会强制结束进程；GUI 冒烟只发送关闭窗口请求，若失败会提示手动关闭。
- benchmark 使用 Mock 解析器与离屏 Qt，不访问网络。
#>
[CmdletBinding()]
param(
    [ValidateSet('info', 'benchmark', 'gui', 'all')]
    [string]$Mode = 'benchmark',

    [int]$Rooms = 20,

    [double]$Interval = 1.0,

    [int]$GuiSeconds = 600,

    [int]$GuiSampleSeconds = 15,

    [switch]$SkipInstall,

    [string]$OutputDir = ""
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $RepoRoot 'acceptance'
}
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$ReportPath = Join-Path $OutputDir ("acceptance-{0}.json" -f $Stamp)
$Report = [ordered]@{
    script_version = '1.0'
    started_at     = (Get-Date).ToString('s')
    mode           = $Mode
    repo_root      = $RepoRoot
    machine        = $null
    environment    = $null
    benchmark      = $null
    gui            = $null
    verdict        = $null
}

function Write-Step([string]$Message) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message) -ForegroundColor Cyan
}

function Get-MachineInfo {
    $cs = Get-CimInstance Win32_ComputerSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $os = Get-CimInstance Win32_OperatingSystem
    [ordered]@{
        computer        = $cs.Name
        os_caption      = $os.Caption
        os_version      = $os.Version
        cpu_name        = $cpu.Name
        logical_cpus    = $cs.NumberOfLogicalProcessors
        physical_cores  = $cpu.NumberOfCores
        total_ram_gb    = [math]::Round($cs.TotalPhysicalMemory / 1GB, 2)
        free_ram_gb     = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
        is_low_spec     = (($cs.TotalPhysicalMemory / 1GB) -lt 6)
        powershell      = $PSVersionTable.PSVersion.ToString()
    }
}

function Get-PythonLauncher {
    $candidates = @(
        @{ Exe = 'py';      Prefix = @('-3.12') },
        @{ Exe = 'python';  Prefix = @() },
        @{ Exe = 'python3'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $args = @($candidate.Prefix) + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')
        $version = & $candidate.Exe @args 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match '^3\.(1[2-9]|[2-9][0-9])') {
            return [ordered]@{ exe = $candidate.Exe; prefix = $candidate.Prefix; version = $version.Trim() }
        }
    }
    return $null
}

function Resolve-VenvPython {
    param([switch]$AllowCreate)
    $venvDir = Join-Path $RepoRoot '.venv-acceptance'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (Test-Path $venvPython) {
        return $venvPython
    }
    # 开发机已有项目 .venv 时直接复用，避免重复安装。
    $existing = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $existing) {
        & $existing -c 'import PySide6' 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $existing
        }
    }
    if (-not $AllowCreate) {
        return $null
    }
    $launcher = Get-PythonLauncher
    if (-not $launcher) {
        throw '未找到 Python 3.12：请先安装 Python 3.12 x64 并加入 PATH，或改用 -Mode info / gui。'
    }
    Write-Step ("创建验收虚拟环境 .venv-acceptance（Python {0}）" -f $launcher.version)
    $args = @($launcher.prefix) + @('-m', 'venv', $venvDir)
    & $launcher.exe @args
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw '创建虚拟环境失败。'
    }
    return $venvPython
}

function Ensure-PySide6 {
    param([string]$VenvPython)
    & $VenvPython -c 'import PySide6' 2>$null
    if ($LASTEXITCODE -eq 0) {
        return $true
    }
    if ($SkipInstall) {
        throw '当前环境缺少 PySide6，且已指定 -SkipInstall。'
    }
    Write-Step '安装项目依赖（PySide6-Essentials）'
    & $VenvPython -m pip install --disable-pip-version-check --quiet -e $RepoRoot
    if ($LASTEXITCODE -ne 0) {
        throw '依赖安装失败，请检查网络或 pip 源。'
    }
    return $true
}

function ConvertFrom-EmbeddedJson {
    param([string]$Text)
    $start = $Text.IndexOf('{')
    $end = $Text.LastIndexOf('}')
    if ($start -lt 0 -or $end -le $start) {
        throw "未能从输出中解析 JSON：$Text"
    }
    return ($Text.Substring($start, $end - $start + 1) | ConvertFrom-Json)
}

function Invoke-Benchmark {
    Write-Step ("性能基准：{0} 个房间、{1}s 间隔" -f $Rooms, $Interval)
    $venvPython = Resolve-VenvPython -AllowCreate
    Ensure-PySide6 -VenvPython $venvPython | Out-Null
    $benchmark = Join-Path $PSScriptRoot 'benchmark.py'
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $output = & $venvPython $benchmark --rooms $Rooms --interval $Interval 2>&1 | Out-String
    $stopwatch.Stop()
    Write-Host $output
    if ($LASTEXITCODE -ne 0) {
        throw "基准运行失败（退出码 $LASTEXITCODE）。"
    }
    $result = ConvertFrom-EmbeddedJson -Text $output
    $peak = [double]$result.peak_working_set_mb
    $shutdown = [double]$result.shutdown_s
    $live = [int]$result.live_rooms_after_10s
    $passed = ($live -eq [int]$result.rooms) -and ($peak -le 300) -and ($shutdown -le 2)
    return [ordered]@{
        wall_clock_s = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
        metrics      = $result
        criteria     = [ordered]@{
            all_rooms_live = ($live -eq [int]$result.rooms)
            peak_le_300mb  = ($peak -le 300)
            shutdown_le_2s = ($shutdown -le 2)
        }
        passed       = $passed
    }
}

function Invoke-GuiSmoke {
    $exe = Join-Path $RepoRoot 'dist\DouyinLiveRecorder\DouyinLiveRecorder.exe'
    if (-not (Test-Path $exe)) {
        Write-Step ("未找到打包程序：{0}，跳过 GUI 冒烟。" -f $exe)
        return [ordered]@{ skipped = $true; reason = '打包程序不存在' }
    }
    $workDir = Join-Path $OutputDir 'gui-data'
    if (-not (Test-Path $workDir)) {
        New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    }
    Write-Step ("启动打包程序，采样 {0}s（每 {1}s）" -f $GuiSeconds, $GuiSampleSeconds)
    # 用 DLR_DATA_DIR 把数据固定在工作目录下，避免写入程序目录或用户现有数据。
    $previousDataDir = $env:DLR_DATA_DIR
    $env:DLR_DATA_DIR = $workDir
    try {
        $process = Start-Process -FilePath $exe -WorkingDirectory $workDir -PassThru
    }
    finally {
        if ($null -eq $previousDataDir) {
            Remove-Item Env:\DLR_DATA_DIR -ErrorAction SilentlyContinue
        }
        else {
            $env:DLR_DATA_DIR = $previousDataDir
        }
    }
    Start-Sleep -Seconds 3
    if ($process.HasExited) {
        return [ordered]@{ skipped = $false; started = $false; exit_code = $process.ExitCode }
    }
    $samples = @()
    $logicalCpus = [Environment]::ProcessorCount
    $prevCpuS = $process.TotalProcessorTime.TotalSeconds
    $prevAt = Get-Date
    $deadline = (Get-Date).AddSeconds($GuiSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) { break }
        $current = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if ($current) {
            $now = Get-Date
            $cpuS = $current.TotalProcessorTime.TotalSeconds
            $span = ($now - $prevAt).TotalSeconds
            $cpuPct = 0
            if ($span -gt 0) {
                $cpuPct = [math]::Round(($cpuS - $prevCpuS) / $span / $logicalCpus * 100, 2)
            }
            $prevCpuS = $cpuS
            $prevAt = $now
            $samples += [ordered]@{
                t_s           = [math]::Round(($now - $process.StartTime).TotalSeconds, 1)
                working_set_mb = [math]::Round($current.WorkingSet64 / 1MB, 2)
                cpu_pct       = $cpuPct
                threads       = $current.Threads.Count
            }
        }
        Start-Sleep -Seconds $GuiSampleSeconds
    }
    $first = if ($samples.Count -gt 0) { [double]$samples[0].working_set_mb } else { 0 }
    $last = if ($samples.Count -gt 0) { [double]$samples[-1].working_set_mb } else { 0 }
    $peak = 0
    $cpuPeak = 0
    $cpuSum = 0
    foreach ($sample in $samples) {
        if ([double]$sample.working_set_mb -gt $peak) { $peak = [double]$sample.working_set_mb }
        if ([double]$sample.cpu_pct -gt $cpuPeak) { $cpuPeak = [double]$sample.cpu_pct }
        $cpuSum += [double]$sample.cpu_pct
    }
    $cpuAvg = 0
    if ($samples.Count -gt 0) {
        $cpuAvg = [math]::Round($cpuSum / $samples.Count, 2)
    }
    $closeRequested = $false
    if (-not $process.HasExited) {
        Write-Step '请求关闭主窗口'
        $closeRequested = $process.CloseMainWindow()
        if ($closeRequested) {
            $process.WaitForExit(15000) | Out-Null
        }
    }
    $exited = $process.HasExited
    $exitCode = if ($exited) { $process.ExitCode } else { $null }
    if (-not $exited) {
        Write-Host '提示：未能自动关闭窗口，请在目标机器上手动关闭后再回传本报告。' -ForegroundColor Yellow
    }
    $growth = [math]::Round($last - $first, 2)
    $passedGui = ($samples.Count -ge 2) -and ($peak -le 300) -and ($growth -le 30) -and `
        ($cpuAvg -le 25) -and $exited -and ($exitCode -eq 0)
    return [ordered]@{
        skipped          = $false
        started          = $true
        exe              = $exe
        working_dir      = $workDir
        data_dir         = $workDir
        logical_cpus     = $logicalCpus
        sample_count     = $samples.Count
        first_mb         = $first
        last_mb          = $last
        peak_mb          = $peak
        growth_mb        = $growth
        cpu_avg_pct      = $cpuAvg
        cpu_peak_pct     = $cpuPeak
        close_requested  = $closeRequested
        exited           = $exited
        exit_code        = $exitCode
        samples          = $samples
        criteria         = [ordered]@{
            samples_ge_2      = ($samples.Count -ge 2)
            peak_le_300mb     = ($peak -le 300)
            growth_le_30mb    = ($growth -le 30)
            cpu_avg_le_25pct  = ($cpuAvg -le 25)
            exited_normally   = ($exited -and ($exitCode -eq 0))
        }
        passed           = $passedGui
    }
}

Write-Step ("低配验收开始：Mode={0}，输出目录={1}" -f $Mode, $OutputDir)
$Report.machine = Get-MachineInfo
Write-Host ("机器：{0} / {1} / {2} 核 {3} 线程 / {4} GB" -f `
    $Report.machine.computer, $Report.machine.cpu_name, $Report.machine.physical_cores, `
    $Report.machine.logical_cpus, $Report.machine.total_ram_gb)
if (-not $Report.machine.is_low_spec) {
    Write-Host '提示：本机内存 >= 6GB，不属于低配目标环境，结果只能作为参考。' -ForegroundColor Yellow
}

if ($Mode -eq 'info') {
    $Report.verdict = [ordered]@{ passed = $null; reason = '仅采集机器信息，未执行基准或 GUI 冒烟' }
}
else {
    if ($Mode -eq 'benchmark' -or $Mode -eq 'all') {
        $Report.benchmark = Invoke-Benchmark
    }
    if ($Mode -eq 'gui' -or $Mode -eq 'all') {
        $Report.gui = Invoke-GuiSmoke
    }
    $checks = @()
    if ($Report.benchmark) { $checks += [bool]$Report.benchmark.passed }
    if ($Report.gui -and -not $Report.gui.skipped) { $checks += [bool]$Report.gui.passed }
    $allPassed = $true
    foreach ($check in $checks) { if (-not $check) { $allPassed = $false } }
    $Report.verdict = [ordered]@{
        passed = $allPassed
        checks = $checks.Count
        note   = 'benchmark 使用 Mock 解析器；gui 需桌面会话，通过关闭窗口正常退出'
    }
}

$Report.finished_at = (Get-Date).ToString('s')
$Report | ConvertTo-Json -Depth 8 | Set-Content -Path $ReportPath -Encoding UTF8
Write-Step ("报告已写入：{0}" -f $ReportPath)
Write-Host ''
$Report | ConvertTo-Json -Depth 8
