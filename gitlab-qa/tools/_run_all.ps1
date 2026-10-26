# GitLab QA 一键修复脚本
# 用法：在 PowerShell 里 cd 到 gitlab-qa 目录，然后：
#   powershell -ExecutionPolicy Bypass -File ..\gitlab-qa\tools\_run_all.ps1
# 或者直接在 gitlab-qa\tools 目录跑：
#   powershell -ExecutionPolicy Bypass -File _run_all.ps1

param(
    [string]$Container = "gitlab-ce",
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env")
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " GitLab QA 一键修复脚本" -ForegroundColor Cyan
Write-Host "工作目录: $root"
Write-Host "容器: $Container"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ---- 1. 读 .env 取密码 ----
Write-Host "[0] 读取 .env ..." -ForegroundColor Yellow
$password = ""
if (Test-Path $EnvFile) {
    foreach ($line in (Get-Content $EnvFile -Encoding UTF8)) {
        if ($line -match '^\s*GITLAB_ROOT_PASSWORD\s*=\s*(.+)$') {
            $password = $Matches[1].Trim()
            break
        }
    }
}
if (-not $password) {
    Write-Host "  [!!] .env 里没找到 GITLAB_ROOT_PASSWORD，手动指定后再来" -ForegroundColor Red
    exit 1
}
Write-Host "  密码前缀: $($password.Substring(0, [Math]::Min(3, $password.Length)))***"
Write-Host ""

# ---- 2. 容器状态检查 + 启动 ----
Write-Host "[1] 容器状态检查 ..." -ForegroundColor Yellow
$running = docker ps --filter "name=$Container" --format "{{.Names}}" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [!!] docker 命令不可用（Docker Desktop 没启动？）" -ForegroundColor Red
    exit 1
}
if ($running -match $Container) {
    Write-Host "  [OK] $Container 已在运行" -ForegroundColor Green
} else {
    $exist = docker ps -a --filter "name=$Container" --format "{{.Names}}" 2>$null
    if ($exist -match $Container) {
        Write-Host "  容器存在但已停止，启动中 ..."
        docker start $Container | Out-Null
        Write-Host "  [OK] 已启动，等待就绪（约 60~120 秒）" -ForegroundColor Green
    } else {
        Write-Host "  [!!] 容器不存在！先去 gitlab-ce 目录跑 deploy.ps1" -ForegroundColor Red
        exit 1
    }
}

# ---- 3. 等待 GitLab 就绪 ----
Write-Host ""
Write-Host "[2] 等待 GitLab 就绪 ..." -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 36; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8929/users/sign_in" `
            -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            Write-Host "  [OK] HTTP 200 就绪 ✓ (${($i*5)}s)" -ForegroundColor Green
            $ready = $true
            break
        }
    } catch {
        if ($i % 4 -eq 0) { Write-Host "  等待中 ... ($($i*5)s)" }
    }
    Start-Sleep -Seconds 5
}
if (-not $ready) {
    Write-Host "  [!!] 180 秒内没就绪，放弃。看 docker logs $Container" -ForegroundColor Red
    exit 1
}

# ---- 4. 修复 root 密码到 .env 中的值 ----
Write-Host ""
Write-Host "[3] 同步 DB root 密码 ← .env ..." -ForegroundColor Yellow
$railsScript = @"
u = User.find_by_username('root');
raise 'root not found' if u.nil?;
u.password = '$password';
u.password_confirmation = '$password';
u.password_automatically_set = false;
u.password_expires_at = nil;
puts(u.save! ? "PW_OK #{u.username}" : u.errors.full_messages.join(','))
"@
$railsOut = docker exec -i $Container gitlab-rails runner -e production $railsScript 2>&1
Write-Host "  $railsOut"
if ($railsOut -match "PW_OK") {
    Write-Host "  [OK] root 密码已对齐 .env" -ForegroundColor Green
} else {
    Write-Host "  [注] 密码修复可能失败，继续往下跑——doctor 会告诉你" -ForegroundColor DarkYellow
}

# ---- 5. doctor.py 自检 ----
Write-Host ""
Write-Host "[4] doctor.py 自检 ..." -ForegroundColor Yellow
python (Join-Path $PSScriptRoot "doctor.py") --base-url http://localhost:8929
$doctorRc = $LASTEXITCODE

# ---- 6. pytest smoke ----
Write-Host ""
Write-Host "[5] pytest smoke tests ..." -ForegroundColor Yellow
python -m pytest "tests\smoke" -v --tb=short
$pytestRc = $LASTEXITCODE

# ---- 收尾 ----
Write-Host ""
Write-Host "============================================================"
Write-Host " doctor rc=$doctorRc   pytest rc=$pytestRc" -ForegroundColor $(
    if ($doctorRc -eq 0 -and $pytestRc -eq 0) { "Green" } else { "Red" })
Write-Host "============================================================"

if ($pytestRc -ne 0) { exit 1 }
