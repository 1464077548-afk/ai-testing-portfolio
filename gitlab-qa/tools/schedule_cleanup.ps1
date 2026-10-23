<#
.SYNOPSIS
    gitlab-qa 产物清理的定时任务（Windows 计划任务）。

.DESCRIPTION
    默认行为：把清理策略写进 .env（CLEANUP_* 键），再注册一个每日计划任务。
    -Run      执行一次清理（计划任务调用的就是这个模式）
    -Status   查看任务状态与上次运行结果
    -Remove   删除任务

    时间可配置：-Time "03:00"（24 小时制 HH:mm）。

    为什么策略写在 .env 而不是任务参数里：
      配置单一来源。任务只记住"每天几点跑"，保留几天、体积上限多少
      跟测试共用同一个 .env，改一处就生效，不用去任务计划程序里翻命令行。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\schedule_cleanup.ps1 -Time 02:30 -Days 3

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\schedule_cleanup.ps1 -Status

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\schedule_cleanup.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$Time = "03:00",
    [int]$Days = 0,
    [int]$MaxMb = 0,
    [int]$KeepLatest = -1,
    [string]$Targets = "",
    [string]$TaskName = "gitlab-qa-cleanup",
    [switch]$Run,
    [switch]$Status,
    [switch]$Remove,
    [switch]$NoRegister
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$CleanupPy = Join-Path $Root "tools\cleanup.py"
$Self = $MyInvocation.MyCommand.Path

function Get-PythonCommand {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @{ Exe = $py.Source; Pre = @("-3") } }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p) { return @{ Exe = $p.Source; Pre = @() } }
    throw "未找到 Python（试过 py / python），请先安装或用 py -3 确认可用"
}

function Invoke-Cleanup {
    $py = Get-PythonCommand
    $pyArgs = @()
    $pyArgs += $py.Pre
    $pyArgs += $CleanupPy
    if ($Days -gt 0) { $pyArgs += @("--days", "$Days") }
    if ($MaxMb -gt 0) { $pyArgs += @("--max-mb", "$MaxMb") }
    if ($KeepLatest -ge 0) { $pyArgs += @("--keep-latest", "$KeepLatest") }
    if ($Targets) { $pyArgs += @("--targets", $Targets) }

    Write-Host "=== 清理开始 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
    & $py.Exe @pyArgs
    $code = $LASTEXITCODE
    Write-Host "=== 清理结束，退出码 $code ==="
    return $code
}

function Set-EnvValue {
    param([string]$Key, [string]$Value)

    if (-not (Test-Path $EnvFile)) {
        throw ".env 不存在。先复制 .env.example 为 .env（或跑 gitlab-ce\deploy.ps1）再执行。"
    }

    $lines = [System.IO.File]::ReadAllLines($EnvFile)
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $found = $true
        }
    }
    if (-not $found) { $lines += "$Key=$Value" }

    # 必须无 BOM：dotenv 读带 BOM 的文件时，第一个键名会带 \ufeff，读不到值
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($EnvFile, (($lines -join "`r`n") + "`r`n"), $enc)
    Write-Host "  .env  $Key=$Value"
}

# ---------------------------------------------------------------- 模式分发
if ($Run) {
    exit (Invoke-Cleanup)
}

if ($Status) {
    schtasks /Query /TN $TaskName /V /FO LIST
    exit $LASTEXITCODE
}

if ($Remove) {
    schtasks /Delete /TN $TaskName /F
    exit $LASTEXITCODE
}

if ($Time -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
    throw "时间格式应为 HH:mm（24 小时制），例如 03:00；收到：$Time"
}
if ($Days -lt 0 -or $MaxMb -lt 0 -or $KeepLatest -lt -1) {
    throw "保留参数不能为负数"
}

# ---------------------------------------------------------------- 写配置
Write-Host "=== 清理策略 -> .env ==="
if ($Days -gt 0) { Set-EnvValue "CLEANUP_RETENTION_DAYS" "$Days" }
if ($MaxMb -gt 0) { Set-EnvValue "CLEANUP_MAX_MB" "$MaxMb" }
if ($KeepLatest -ge 0) { Set-EnvValue "CLEANUP_KEEP_LATEST" "$KeepLatest" }
if ($Targets) { Set-EnvValue "CLEANUP_TARGETS" "$Targets" }

if ($NoRegister) {
    Write-Host "[ok] 只更新了 .env（-NoRegister），未注册任务。"
    exit 0
}

# ---------------------------------------------------------------- 注册任务
$taskCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Self`" -Run"

try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Self`" -Run"
    )
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Force `
        -Description "gitlab-qa 产物清理：日志 / 截图 / trace / 旧报告（策略见 .env）" | Out-Null

    Write-Host ""
    Write-Host "[ok] 已注册每日任务：$TaskName"
    Write-Host "     触发时间：$Time"
    Write-Host "     立即试跑：powershell -ExecutionPolicy Bypass -File `"$Self`" -Run"
    Write-Host "     查看状态：powershell -ExecutionPolicy Bypass -File `"$Self`" -Status"
    exit 0
}
catch {
    Write-Host ""
    Write-Host "[!] 自动注册失败：$($_.Exception.Message)"
    Write-Host "    多数情况是权限不足。用管理员 PowerShell 执行下面这条（路径已带引号）："
    Write-Host ""
    Write-Host "    schtasks /Create /TN `"$TaskName`" /SC DAILY /ST $Time /F ``"
    Write-Host "      /TR `"$taskCmd`""
    Write-Host ""
    Write-Host "    也可以不管计划任务，直接每天手动跑："
    Write-Host "      powershell -ExecutionPolicy Bypass -File `"$Self`" -Run"
    exit 1
}
