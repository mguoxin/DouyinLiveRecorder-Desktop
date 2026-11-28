$ErrorActionPreference = 'Continue'
$root = Join-Path $env:TEMP 'dlr_v050_accept'
$dist = 'C:\Users\kglpl\Desktop\DouyinLiveRecorder-Desktop\dist_v050\DouyinLiveRecorder'
$base = Join-Path $root 'e2e'
$ffmpeg = 'C:\Users\kglpl\Desktop\DouyinLiveRecorder-Desktop\tools\ffmpeg\bin\ffmpeg.exe'
$python = 'C:\Users\kglpl\Desktop\DouyinLiveRecorder-Desktop\.venv\Scripts\python.exe'
$src = 'C:\Users\kglpl\Desktop\DouyinLiveRecorder-Desktop\src'
if (Test-Path $base) { Remove-Item $base -Recurse -Force }
New-Item -ItemType Directory -Path $base -Force | Out-Null

function TS { return (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff') }
function Log($msg) { Write-Output ("[{0}] {1}" -f (TS), $msg) }

Get-CimInstance Win32_Process -Filter "Name='DouyinLiveRecorder.exe'" |
    Where-Object { $_.ExecutablePath -like "$dist*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$env:DLR_DATA_DIR = $base
$env:DLR_PARSER = 'mock'
$env:DLR_MOCK_STREAM_URL = 'http://127.0.0.1:18082/live.ts'
$env:DLR_WINDOW_TRACE = '1'
$env:DLR_DBP = Join-Path $base 'data\app.db'
$env:PYTHONPATH = $src

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace Win -Name R5 -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, System.UIntPtr dwExtraInfo);
'@

$AE = [System.Windows.Automation.AutomationElement]
function Find-El($name) {
    $c = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $name)
    return $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)
}
function Click-Rect($r) {
    $x = [int]($r.X + $r.Width / 2); $y = [int]($r.Y + $r.Height / 2)
    $null = [Win.R5]::SetCursorPos($x, $y)
    [Win.R5]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [Win.R5]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
}
function Invoke-El($name) {
    $e = Find-El $name
    if (-not $e) { return $false }
    try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); return $true }
    catch { Click-Rect $e.Current.BoundingRectangle; return $true }
}
function Wait-Until([scriptblock]$cond, [int]$seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        if (& $cond) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}
function Get-AppFFmpeg($appPid) {
    return Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" | Where-Object { $_.ParentProcessId -eq $appPid }
}
function Query-DB($sql) {
    @"
import sqlite3, os
conn = sqlite3.connect(os.environ['DLR_DBP'])
try:
    print(conn.execute(r'''$sql''').fetchall())
finally:
    conn.close()
"@ | & $python -
}

$job = Start-Job -ScriptBlock {
    param($ff)
    while ($true) {
        & $ff -hide_banner -loglevel error -re -f lavfi -i 'testsrc=size=320x180:rate=15' `
            -f lavfi -i 'sine=frequency=440:sample_rate=44100' -c:v mpeg2video -q:v 8 `
            -c:a mp2 -b:a 64k -f mpegts -listen 1 'http://127.0.0.1:18082/live.ts'
        Start-Sleep -Milliseconds 300
    }
} -ArgumentList $ffmpeg
Start-Sleep -Seconds 4
Log "STEP0 producer_job=$($job.Id) state=$($job.State) url=http://127.0.0.1:18082/live.ts"

$p = $null
$p2 = $null
try {
    @'
import os
from pathlib import Path
from dlrecorder.infrastructure.db import Database
p = Path(os.environ['DLR_DBP'])
p.parent.mkdir(parents=True, exist_ok=True)
db = Database(p)
db.set_setting('recording.auto_start', '1')
db.set_setting('archive.enabled', '1')
db.set_setting('archive.root_dir', str(p.parent.parent / 'archive'))
print('PRESEED schema=', db._get_conn().execute('SELECT MAX(version) FROM schema_version').fetchone()[0])
db.close()
'@ | & $python -
    Log "STEP0B preseeded auto_record + archive"

    $exe = Join-Path $dist 'DouyinLiveRecorder.exe'
    $p = Start-Process -FilePath $exe -WorkingDirectory $dist -PassThru
    Start-Sleep -Seconds 9
    $p.Refresh()
    Log "STEP1 launched pid=$($p.Id) title='$($p.MainWindowTitle)'"

    $schema = Query-DB "SELECT MAX(version) FROM schema_version"
    Log "STEP1B packaged_schema=$schema"

    if (-not (Invoke-El '添加直播间')) { throw 'ADD_BUTTON_NOT_FOUND' }
    Start-Sleep -Seconds 2
    $wcond = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)),
        (New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, '添加直播间'))
    )
    $dlg = $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $wcond)
    if (-not $dlg) { throw 'DIALOG_NOT_FOUND' }
    $null = [Win.R5]::SetForegroundWindow([IntPtr]$dlg.Current.NativeWindowHandle)
    Start-Sleep -Milliseconds 500
    $editCond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
    $edits = $dlg.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
    Log "STEP2 edits=$($edits.Count)"
    if ($edits.Count -ge 2) {
        $edits[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('123456789')
        $edits[1].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('AuditV5')
    } else {
        $edits[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('123456789')
    }
    Start-Sleep -Milliseconds 500
    $ok = $dlg.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, '添加')))
    if (-not $ok) { throw 'OK_BUTTON_NOT_FOUND' }
    try { $ok.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
    catch { Click-Rect $ok.Current.BoundingRectangle }
    Start-Sleep -Seconds 3
    $dlgGone = -not [bool]($AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $wcond))
    Log "STEP2 room_added web_rid=123456789 name=AuditV5 dialog_gone=$dlgGone"
    $null = [Win.R5]::SetForegroundWindow([IntPtr]$p.MainWindowHandle)
    $table = $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Table)))
    if ($table) {
        $tr = $table.Current.BoundingRectangle
        $null = [Win.R5]::SetCursorPos([int]($tr.X + 90), [int]($tr.Y + 52))
        [Win.R5]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        [Win.R5]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Seconds 1
        Log "STEP2B table_row_clicked"
    } else {
        Log "STEP2B table_not_found"
    }

    $null = Invoke-El '开始监控'
    Start-Sleep -Seconds 5
    Log "STEP3 monitor_started"

    $ff = $null
    $got = Wait-Until { $script:ff = Get-AppFFmpeg $p.Id; return [bool]$script:ff } 30
    if (-not $got) { throw 'FFMPEG_NOT_STARTED' }
    $pid1 = ($ff | Select-Object -First 1).ProcessId
    Log "STEP4/5 ffmpeg_started pid=$pid1"

    $file = $null
    $grew = Wait-Until {
        $f = Get-ChildItem (Join-Path $base 'data') -Recurse -Filter *.ts -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime | Select-Object -Last 1
        if (-not $f) { return $false }
        $script:file = $f
        return ($f.Length -gt 262144)
    } 45
    Log "STEP6 recording_file=$($file.FullName) size=$($file.Length) grew=$grew"
    if ($file) {
        $trace = Join-Path $base 'window_trace.log'
        $health = Get-Content $trace -ErrorAction SilentlyContinue | Select-String '健康' | Select-Object -First 1
        Log "STEP6B metrics_health_trace=$([bool]$health)"
    }

    Stop-Process -Id $pid1 -Force
    Log "STEP7 ffmpeg_killed pid=$pid1"
    $recovered = Wait-Until {
        $list = Get-AppFFmpeg $p.Id
        return [bool]($list | Where-Object { $_.ProcessId -ne $pid1 })
    } 45
    $seg2 = Wait-Until {
        $all = Get-ChildItem (Join-Path $base 'data') -Recurse -Filter *.ts -ErrorAction SilentlyContinue
        $new = $all | Where-Object { $_.Name -ne $file.Name -and $_.Length -gt 131072 }
        return [bool]$new
    } 45
    $files2 = Get-ChildItem (Join-Path $base 'data') -Recurse -Filter *.ts -ErrorAction SilentlyContinue
    Log "STEP8 recovered=$recovered second_segment=$seg2 segments=$($files2.Count)"

    Start-Sleep -Seconds 2
    $null = Invoke-El '停止录制'
    Start-Sleep -Seconds 5
    $ffLeft = Get-AppFFmpeg $p.Id
    Log "STEP9 record_stopped ffmpeg_left=$(@($ffLeft).Count)"

    $archived = Wait-Until {
        $files = Get-ChildItem (Join-Path $base 'archive') -Recurse -Filter *.ts -ErrorAction SilentlyContinue
        return [bool]$files
    } 30
    $archiveFiles = Get-ChildItem (Join-Path $base 'archive') -Recurse -Filter *.ts -ErrorAction SilentlyContinue
    $archiveRows = Query-DB "SELECT COUNT(*) FROM recordings WHERE archive_status='archived'"
    Log "STEP9C archive_files=$(@($archiveFiles).Count) archived=$archived db=$archiveRows source_kept=$((Get-ChildItem (Join-Path $base 'data') -Recurse -Filter *.ts -ErrorAction SilentlyContinue).Count)"

    $null = Invoke-El '停止监控'
    Start-Sleep -Seconds 3
    Log "STEP9B monitor_stopped"

    $null = Invoke-El '录制库'
    Start-Sleep -Seconds 2
    $libTables = $AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Table)))
    Log "STEP9D library_opened tables=$($libTables.Count)"

    $h = [IntPtr]$p.MainWindowHandle
    if ($h -eq [IntPtr]::Zero) { $null = $p.CloseMainWindow() }
    else { $null = [Win.R5]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
    if (-not $p.HasExited) { $null = $p.WaitForExit(35000) }
    $p.Refresh()
    $left = Get-AppFFmpeg $p.Id
    Log "STEP10 exited=$($p.HasExited) code=$(if ($p.HasExited) { $p.ExitCode } else { 'running' }) ffmpeg_left=$(@($left).Count)"

    $p2 = Start-Process -FilePath $exe -WorkingDirectory $dist -PassThru
    Start-Sleep -Seconds 9
    $p2.Refresh()
    Log "STEP11 relaunched pid=$($p2.Id) title='$($p2.MainWindowTitle)'"
    $files3 = Get-ChildItem (Join-Path $base 'data') -Recurse -Filter *.ts -ErrorAction SilentlyContinue
    $schema2 = Query-DB "SELECT MAX(version) FROM schema_version"
    Log "STEP11 after_restart_segments=$($files3.Count) schema=$schema2"

    $h2 = [IntPtr]$p2.MainWindowHandle
    if ($h2 -eq [IntPtr]::Zero) { $null = $p2.CloseMainWindow() }
    else { $null = [Win.R5]::PostMessage($h2, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
    if (-not $p2.HasExited) { $null = $p2.WaitForExit(30000) }
    $p2.Refresh()
    Log "STEP11B second_exit=$($p2.HasExited) code=$(if ($p2.HasExited) { $p2.ExitCode } else { 'running' })"
}
catch {
    Log "ERROR: $($_.Exception.Message)"
}
finally {
    if ($p -and -not $p.HasExited) { $null = [Win.R5]::PostMessage([IntPtr]$p.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero); $null = $p.WaitForExit(15000) }
    if ($p2 -and -not $p2.HasExited) { $null = [Win.R5]::PostMessage([IntPtr]$p2.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero); $null = $p2.WaitForExit(15000) }
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Log "DATA_DIR=$base"
}
