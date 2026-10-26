#Requires -Version 5.1
<#
.SYNOPSIS
    一键完成 GitLab CE 部署全链路：部署 → 等就绪 → 签发 PAT → 写 .env → 验收冒烟。

.DESCRIPTION
    把「装完能用」拆成 8 个可观测阶段，每一步失败都给可执行的下一步，
    而不是丢一段 docker 原始报错让人猜。

      [1/8] 前置检查    资源、磁盘、端口
      [2/8] Docker       daemon 就绪（可等待）、compose 可用、compose 配置合法
      [3/8] 镜像         本地已有则复用
      [4/8] 启动容器     docker compose up -d
      [5/8] 等待就绪     /-/readiness + 容器状态 + 日志心跳
      [6/8] 签发 PAT     容器内 rails runner，幂等
      [7/8] 写 .env      UTF-8 无 BOM，写后回读校验
      [8/8] 验收冒烟     doctor 自检 + smoke 套件 + Allure 报告

    退出码：
      0  部署成功且验收通过
      1  部署或签发失败（验收未执行，或结果不可信）
      2  部署成功，但验收未通过

.PARAMETER SkipPull
    镜像已存在时跳过拉取；镜像不存在时仍会自动拉。

.PARAMETER Reset
    先 docker compose down -v 清空数据卷再部署（数据不可恢复）。

.PARAMETER SkipSmoke
    只部署不验收。

.PARAMETER SmokeOnly
    跳过部署，用现有 .env 只跑验收。

.PARAMETER Suites
    验收套件，默认 smoke。可写 smoke,api 连接口全量一起跑。

.PARAMETER Open
    验收结束后用浏览器打开 Allure 报告。

.PARAMETER Force
    强制重签 PAT。默认复用 .env 里仍然有效的 Token，避免每次部署都换。
    重签时会先撤销同名旧 Token（gqa-automation）。

.PARAMETER TimeoutMinutes
    等待就绪上限，默认 25 分钟。首次启动含数据库迁移，慢机器给到 40。

.PARAMETER DaemonWaitSeconds
    等 Docker daemon 起来的时间，默认 120 秒。Docker Desktop 刚启动时
    托盘图标已亮但引擎未就绪，这段等待能省掉一次手动重跑。

.PARAMETER HostPort
    宿主机端口，默认 8929。会同步写进 external_url 与容器内监听端口。

.PARAMETER PatValue
    PAT 明文。留空则由脚本随机生成 glpat- 前缀 + 20 位随机串（推荐）。
    显式指定仅用于复现问题——注意命令行参数会进 shell 历史，别填真实凭据。

.PARAMETER MemoryLimit
    容器内存上限，默认 6G。宿主内存富余时给 8G 明显更稳。

.PARAMETER PythonExe
    指定解释器。默认自动找 py -3 / python，并校验测试依赖齐全。

.EXAMPLE
    .\deploy.ps1

.EXAMPLE
    .\deploy.ps1 -Reset -TimeoutMinutes 40 -MemoryLimit 8G

.EXAMPLE
    .\deploy.ps1 -SmokeOnly -Suites smoke,api -Open
    .\deploy.ps1 -KeepResults     # 安全策略禁止批量删除时用
#>
# ==========================================================================
# 参数块 [CmdletBinding ()] param ()
# ==========================================================================
[CmdletBinding()]   #[CmdletBinding()] ：开启 PS 高级函数特性，支持 `-Verbose`、自动参数校验、错误流控制
param(
    [switch]$SkipPull, #[switch]：布尔开关，调用脚本带上这个参数 =\(true，不带=\)false
    [switch]$Reset,
    [switch]$SkipSmoke,
    [switch]$SmokeOnly,
    [switch]$Open,
    [switch]$Force,
    [string[]]$Suites = @('smoke'), #[string[]]：字符串数组，可以传多个测试套件，比如 `-Suites smoke,api`
    [switch]$KeepResults,
    [int]$TimeoutMinutes = 25,
    [int]$DaemonWaitSeconds = 120,
    [int]$HostPort = 8929,
    [string]$PatValue = '',
    [string]$MemoryLimit = '6G',
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop' #把非终止错误提升为终止异常，配合 try/catch 捕获，**防止脚本出错悄悄往下跑**
$ProgressPreference = 'SilentlyContinue'   # 关闭 Invoke-WebRequest 的进度条；进度条会拖慢 http 轮询，是 PS 经典坑
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { } #设置控制台输出编码 UTF8，解决中文乱码

# ==========================================================================
# 常量定义块
# ==========================================================================
$Root       = $PSScriptRoot   #- `$PSScriptRoot`：脚本所在目录，是脚本的根路径
$Container  = 'gitlab-ce'
$Image      = 'gitlab/gitlab-ce:latest'
# 与 docker-compose.yml 的 GITLAB_ROOT_PASSWORD 必须一致。
# 不能用 GitlabQA@2026 这类值：GitLab 19 的密码策略会拒绝"常见词组合"，
# seed 失败是静默的 —— 登录页照常打开，但库里没有 root 用户。
# $RootPwd 在下面 $EnvFile 定义之后才解析——要先知道 .env 在哪，才能从里面回读。

# PAT 明文默认随机生成。硬编码默认值等于把凭据写进版本库，
# 而且所有人 clone 下来拿到同一个 token —— 留空即随机是更安全的默认。
if ([string]::IsNullOrWhiteSpace($PatValue)) {
    $Pool = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    $Rand = -join (1..20 | ForEach-Object { $Pool[(Get-Random -Maximum $Pool.Length)] })
    $PatValue = "glpat-$Rand"
}
$BaseUrl    = "http://localhost:$HostPort"
$QaDir      = Join-Path (Split-Path -Parent $Root) 'gitlab-qa' #- `$QaDir`：测试工程目录，和部署脚本目录同级
$EnvFile    = Join-Path $QaDir '.env'

# ==========================================================================
# root 密码解析
#
# 不硬编码在脚本里——脚本要进版本库，写死等于把凭据公开。
# 按优先级取：环境变量 → gitlab-qa\.env → 随机生成（首装）。
#
# 第二条回退是必须的：只认环境变量的话，重跑一次脚本就得先去翻 .env
# 并把密码贴回命令行（还会留在 shell 历史里）。而 .env 里存的就是上次
# 部署用的密码，复用它才不会把已存在实例的 root 密码悄悄改掉。
#
# 空值绝不往下走：空密码 seed 是静默失败——登录页照常打开，库里却没有 root。
# ==========================================================================
$RootPwd   = ''
$pwdSource = ''

if (-not [string]::IsNullOrWhiteSpace($env:GITLAB_ROOT_PASSWORD)) {
    $RootPwd   = $env:GITLAB_ROOT_PASSWORD
    $pwdSource = '环境变量 GITLAB_ROOT_PASSWORD'
}

if ([string]::IsNullOrWhiteSpace($RootPwd) -and (Test-Path $EnvFile)) {
    # 逐行匹配而不是 Select-String + Split：值里可能含 = 或空格，
    # Split('=')[1] 会把含 = 的密码截断，且不去引号。
    foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        if ($line -match '^\s*GITLAB_ROOT_PASSWORD\s*=(.*)$') {
            $v = $Matches[1].Trim()
            if ($v.Length -ge 2) {
                if (($v.StartsWith('"') -and $v.EndsWith('"')) -or
                    ($v.StartsWith("'") -and $v.EndsWith("'"))) {
                    $v = $v.Substring(1, $v.Length - 2)
                }
            }
            if (-not [string]::IsNullOrWhiteSpace($v)) {
                $RootPwd   = $v
                $pwdSource = 'gitlab-qa\.env（复用上次部署的值）'
            }
            break
        }
    }
}

if ([string]::IsNullOrWhiteSpace($RootPwd)) {
    if ($SmokeOnly) {
        # 只验收不部署，全程不碰容器，也就用不到 root 密码。
        # 这里报错会把"改完用例快速回归"这条最常用路径堵死。
        $pwdSource = '未解析（-SmokeOnly 不需要）'
    }
    else {
        # 首装：随机生成。GitLab 19 会静默拒绝含常见词（gitlab/admin/password/
        # root/user 等）的密码，让用户自己想一个很容易撞上；四字符集随机串
        # 不在字典里，也不会撞词。
        $sets  = @('abcdefghijkmnopqrstuvwxyz', 'ABCDEFGHJKLMNPQRSTUVWXYZ',
                   '0123456789', '!@#%^&*-_=+')
        $chars = @()
        foreach ($s in $sets) { $chars += $s[(Get-Random -Maximum $s.Length)] }
        for ($i = 0; $i -lt 14; $i++) {
            $pool  = $sets[(Get-Random -Maximum $sets.Length)]
            $chars += $pool[(Get-Random -Maximum $pool.Length)]
        }
        $RootPwd   = (($chars | Sort-Object { Get-Random }) -join '')
        $pwdSource = '随机生成（首次部署）'
    }
}

$LogDir     = Join-Path $Root 'logs'
$StateFile  = Join-Path $Root '.deploy-state.json'
$AllowedSuites = @('unit', 'smoke', 'api', 'contract', 'ui', 'e2e', 'perf', 'regression', 'all')

$Script:StepIndex = 0  #- `$Script:StepIndex`：当前步骤索引，从 0 开始，脚本级全局变量，函数内部可以读写这个变量
$Script:StepTotal = 8
if ($SmokeOnly) { $Script:StepTotal = 1 }   # 只验收时只有一步
$Script:Results   = @() #数组，保存每一个阶段的执行结果，最后输出汇总报表
$Script:ExitCode  = 0
$Script:StartTime = Get-Date
$Script:DockerExe = $null
$Script:ComposeArgs = @('compose')
$Script:ComposeExe  = $null

# 提前解析 docker 可执行文件：第 1 步就要用它查容器状态。
# 如果留到第 2 步再解析，前面的调用会变成 `& $null`，
# 报的是「& 后面的表达式生成无效的对象」——和真实原因（没装 docker）差得很远。
$dockerProbe = Get-Command docker -ErrorAction SilentlyContinue
if ($null -ne $dockerProbe) {
    $Script:DockerExe  = $dockerProbe.Source
    $Script:ComposeExe = $dockerProbe.Source
}

# ==========================================================================
# 输出
# ==========================================================================
#阶段标题，自动 + 1 序号，比如 `[1/8] 前置检查`
function Write-Head([string]$Message) {
    $Script:StepIndex++
    Write-Host ''
    Write-Host ('[{0}/{1}] {2}' -f $Script:StepIndex, $Script:StepTotal, $Message) -ForegroundColor Cyan
}
#分级彩色日志，区分正常、警告、备注、详情，分别用不同的颜色输出
function Write-Ok([string]$Message)   { Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Bad([string]$Message)  { Write-Host "    [!!] $Message" -ForegroundColor Yellow }
function Write-Note([string]$Message) { Write-Host "    $Message" -ForegroundColor Gray }
function Write-Dim([string]$Message)  { Write-Host "         $Message" -ForegroundColor DarkGray }

# 统一的中止出口：抛给顶层 catch 打印，保证 finally 里的 transcript 能收尾
function Stop-Deploy([string]$Message) {
    throw ('GQA-ABORT|' + $Message)
}
#往全局结果数组追加一条阶段记录，记录阶段名、耗时、状态、详情，最后汇总报表靠它
function Add-Result([string]$Name, [datetime]$Started, [string]$Status, [string]$Detail) {
    $Script:Results += [pscustomobject]@{
        Phase   = $Name
        Status  = $Status
        Seconds = [math]::Round(((Get-Date) - $Started).TotalSeconds, 1)
        Detail  = $Detail
    }
}
#字节转 GB/MB/KB，友好展示磁盘内存
function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

# ==========================================================================
# 外部命令
# ==========================================================================
# Process.Start + 超时控制：rails runner 首次加载 Rails 可能卡住，
# 直接调用会挂死整个脚本，这里给它一个上限。
#Invoke-Capture：带超时的进程启动器
function Invoke-Capture {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string]$ArgString,
        [int]$TimeoutSec = 300,
        [string]$WorkDir = ''
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $Exe
    $psi.Arguments              = $ArgString
    # 子进程默认继承的是「进程当前目录」，而 PowerShell 的 Push-Location 只改
    # $PWD。不显式指定时，相对路径会按脚本所在目录去解析，报 file not found。
    if ($WorkDir -ne '') { $psi.WorkingDirectory = $WorkDir }
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true
    $psi.StandardOutputEncoding = New-Object System.Text.UTF8Encoding $false
    $psi.StandardErrorEncoding  = New-Object System.Text.UTF8Encoding $false

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()

    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { }
        return [pscustomobject]@{ Code = -1; Out = ''; Err = "超过 $TimeoutSec 秒未返回" }
    }
    return [pscustomobject]@{ Code = $proc.ExitCode; Out = $outTask.Result; Err = $errTask.Result }
}
#Invoke-Py：调用 Python 测试脚本
function Invoke-Py {
    param(
        [Parameter(Mandatory = $true)]$Py,
        [Parameter(Mandatory = $true)][string[]]$PyArgs,
        [int]$TimeoutSec = 900
    )
    # 必须自己拼命令行：直接 `& $py.Exe @args` 时，子进程的 stdout 不进
    # Start-Transcript，失败时日志里只剩一句「退出码 1」，等于没有线索。
    $sb = New-Object System.Text.StringBuilder
    foreach ($p in (@() + $Py.Pre + $PyArgs)) {
        $s = [string]$p
        if ($s -match '[\s"]') { $s = '"' + ($s -replace '"', '\"') + '"' }
        [void]$sb.Append($s).Append(' ')
    }
    $r = Invoke-Capture -Exe $Py.Exe -ArgString $sb.ToString().Trim() `
         -TimeoutSec $TimeoutSec -WorkDir (Get-Location).Path
    foreach ($stream in @($r.Out, $r.Err)) {
        foreach ($line in ($stream -split "`r?`n")) {
            if ($line.Trim() -ne '') { Write-Dim $line }
        }
    }
    return $r.Code
}
#Invoke-Compose：docker compose 包装器
function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    $all = @() + $Script:ComposeArgs + $ComposeArgs
    & $Script:ComposeExe @all
    return $LASTEXITCODE
}
#Test-PortListening：测试端口是否监听连接
function Test-PortListening([int]$Port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async  = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        $done   = $async.AsyncWaitHandle.WaitOne(500, $false)
        if ($done) {
            try { $client.EndConnect($async); $client.Close(); return $true }
            catch { $client.Close(); return $false }
        }
        $client.Close()
        return $false
    } catch { return $false }
}
#Get-PortOwner：拿到监听端口的进程 PID + 进程名，端口冲突时直接告诉用户是谁占用，不是只报端口被占用
function Get-PortOwner([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) { return ('PID {0} ({1})' -f $proc.Id, $proc.ProcessName) }
            return ('PID ' + $conn.OwningProcess)
        }
    } catch { }
    return '未知进程'
}
#Get-ContainerState、Write-ContainerLogs、Write-LogHint：容器状态诊断
function Get-ContainerState {
    if ($null -eq $Script:DockerExe) { return $null }
    # 走 Invoke-Capture，而不是 `docker inspect ... 2>$null`。
    # PS 5.1 里重定向原生命令的 stderr，每一行都会变成 ErrorRecord，
    # 配合 $ErrorActionPreference='Stop' 就是终止错误——
    # 首次部署时容器还不存在，docker 报 "no such object"，
    # 脚本会把它当成自身崩溃，而不是"还没部署"。这个坑踩过一次就够了。
    #`docker inspect` 格式化输出容器状态：运行状态、退出码、重启次数、健康检查状态，用`|`分隔，解析成对象
    $r = Invoke-Capture -Exe $Script:DockerExe `
         -ArgString ('inspect -f {{.State.Status}}|{{.State.ExitCode}}|{{.RestartCount}}|{{.State.Health.Status}} ' + $Container) `
         -TimeoutSec 30
    if ($r.Code -ne 0 -or [string]::IsNullOrWhiteSpace($r.Out)) { return $null }
    $parts = ((($r.Out -split "`r?`n")[0]).Trim() -split '\|')
    if ($parts.Count -lt 3) { return $null }
    $health = ''
    if ($parts.Count -ge 4) { $health = $parts[3] }
    return [pscustomobject]@{
        Status   = $parts[0]
        ExitCode = $parts[1]
        Restarts = $parts[2]
        Health   = $health
    }
}

function Write-ContainerLogs([int]$Tail = 40) {
    $r = Invoke-Capture -Exe $Script:DockerExe `
         -ArgString ('logs --tail {0} {1}' -f $Tail, $Container) -TimeoutSec 40
    $text = (($r.Out + "`n" + $r.Err)).Trim()
    if ($text -eq '') { Write-Dim '(没有日志输出)'; return }
    foreach ($line in ($text -split "`r?`n")) { Write-Dim $line }
}
<#Write-LogHint：**智能错误匹配器**，正则匹配 GitLab 最常见 3 类崩溃日志，直接给根因 + 修复方案（作者长期踩坑沉淀的）
1. omnibus 不认识的 gitlab.rb 配置键（GitLab 版本升级后配置项删除）
2. 端口占用
3. OOM 内存耗尽
#>
function Write-LogHint {
    # 容器起不来时，日志里最值钱的是那行 FATAL。这里认三种高频模式，
    # 直接把"下一步该改什么"说出来，省掉一轮搜索。
    $r = Invoke-Capture -Exe $Script:DockerExe -ArgString ('logs --tail 200 ' + $Container) -TimeoutSec 40
    $text = $r.Out + "`n" + $r.Err

    if ($text -match 'UnknownConfigOptionError: Reading unsupported config value ([a-z_]+)') {
        Write-Bad ("GitLab 拒绝了配置项 '{0}'：当前镜像版本已不支持它" -f $Matches[1])
        Write-Dim 'omnibus 的配置是严格校验，写错一个键 reconfigure 就直接退出，'
        Write-Dim '容器表现为无限重启，日志里却看不到"起不来"的直接原因。'
        Write-Dim '对照该镜像支持的键：/opt/gitlab/embedded/cookbooks/package/libraries/config/gitlab.rb'
        Write-Dim '（每一条 attribute(...) 就是一个合法键；grafana 在 19.x 已移除）'
    }
    if ($text -match 'Address already in use|port is already allocated') {
        Write-Bad '端口被占用：换一个 -HostPort，或先停掉占用端口的进程'
    }
    if ($text -match 'Cannot allocate memory|out of memory|OOMKilled') {
        Write-Bad '内存不足：调大 Docker Desktop 的内存配额，或降低 -MemoryLimit'
    }
}
#Test-GitLabReady + Get-HttpStatus：就绪判定（核心难点）
function Test-GitLabReady {
    # 就绪判定不能只认 /-/readiness。实测 GitLab 19.0.1 上
    # /-/health 和 /-/readiness 都返回 404，只盯这两个端点会白等到超时。
    # 所以分两级：老版本用 readiness 的语义化结果，新版本用
    # "登录页 200 + API 有响应"这两个更稳定的信号。
    $r = Get-HttpStatus "$BaseUrl/-/readiness" 6   #优先读`/-/readiness`，200 且 status=ok → 就绪
    if ($r.Code -eq 200 -and $r.Body -match '"status"\s*:\s*"ok"') {
        return [pscustomobject]@{ Ready = $true; Note = 'readiness ok' }
    }
    #eadiness 不可用时降级判定：登录页 `/users/sign_in` 返回 200 + `/api/v4/version` 返回 200/401（401 = 接口存在，只是需要 token，代表 Rails 路由已经加载完成）
    $login = Get-HttpStatus "$BaseUrl/users/sign_in" 6
    if ($login.Code -ne 200) {
        return [pscustomobject]@{ Ready = $false; Note = ("sign_in={0} readiness={1}" -f $login.Code, $r.Code) }
    }
    # 版本接口在 19 上要鉴权，401 同样说明 Rails 路由已挂载
    $api = Get-HttpStatus "$BaseUrl/api/v4/version" 6
    if ($api.Code -eq 200 -or $api.Code -eq 401) {
        return [pscustomobject]@{ Ready = $true; Note = ("sign_in=200 api={0}" -f $api.Code) }
    }
    return [pscustomobject]@{ Ready = $false; Note = ("sign_in=200 api={0}" -f $api.Code) }
}
#Invoke-RailsScript：容器内执行 Ruby 脚本（PAT 签发核心）
#痛点：直接 `docker exec gitlab-ce gitlab-rails runner 'xxx'` 传多行 Ruby，PowerShell → docker → ruby 三层引号转义地狱，极难调试
function Invoke-RailsScript {
    # 在容器里跑一段 Ruby。不用 `gitlab-rails runner "<多行脚本>"` 的内联写法：
    # PowerShell → docker.exe 的多层引号会把脚本撕碎，报错还很难看懂。
    # 统一落成文件、docker cp 进去、再 runner 执行，引号问题彻底消失。
    <#
    1. 本地临时目录生成 rb 脚本（LF 换行，CRLF 会让 Ruby 报错）
    2. `docker cp` 拷贝进容器 `/tmp`
    3. 容器内 chmod + 执行 `gitlab-rails runner`
    4. finally 块自动清理容器内临时文件 + 本机临时文件，不留垃圾

- `Get-AdminCount` 是一个快速探针脚本：查询数据库管理员用户数量，解决 GitLab 静默 seed 失败问题（页面能打开，root 用户不存在）
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Body,
        [int]$TimeoutSec = 420
    )
    $local  = Join-Path $env:TEMP ('gqa_{0}_{1}.rb' -f $Name, ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    $remote = '/tmp/gqa_' + $Name + '.rb'
    # 只写 LF：CRLF 会作为行尾字符混进 Ruby 语法
    $text = ($Body -split "`r?`n") -join "`n"
    [System.IO.File]::WriteAllText($local, $text, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $cp = Invoke-Capture -Exe $Script:DockerExe `
              -ArgString ('cp "{0}" {1}:{2}' -f $local, $Container, $remote) -TimeoutSec 60
        if ($cp.Code -ne 0) {
            $msg = (($cp.Err) -replace '\s+', ' ').Trim()
            return [pscustomobject]@{ Code = $cp.Code; Out = ''; Err = ('docker cp 失败：' + $msg) }
        }
        [void](Invoke-Capture -Exe $Script:DockerExe `
               -ArgString ('exec {0} chmod 644 {1}' -f $Container, $remote) -TimeoutSec 30)
        return (Invoke-Capture -Exe $Script:DockerExe `
                -ArgString ('exec {0} gitlab-rails runner {1}' -f $Container, $remote) -TimeoutSec $TimeoutSec)
    } finally {
        [void](Invoke-Capture -Exe $Script:DockerExe `
               -ArgString ('exec {0} rm -f {1}' -f $Container, $remote) -TimeoutSec 30)
        Remove-Item -LiteralPath $local -Force -ErrorAction SilentlyContinue
    }
}

function Get-AdminCount {
    $probe = Invoke-RailsScript -Name 'admins' `
             -Body "puts 'GQA_ADMINS=' + User.where(admin: true).count.to_s" -TimeoutSec 300
    foreach ($line in ($probe.Out -split "`r?`n")) {
        if ($line -match '^GQA_ADMINS=(\d+)') { return [int]$Matches[1] }
    }
    return -1
}

function Get-HttpStatus([string]$Url, [int]$TimeoutSec = 8) {
    # 用 HttpWebRequest，不用 Invoke-WebRequest。
    # 服务还没起来时 Invoke-WebRequest 抛的是终止错误，Start-Transcript 会把它
    # 记成 "PS>TerminatingError(Invoke-WebRequest)"；等待就绪的几十分钟里
    # 每 8 秒刷两条，日志会被这些噪音淹没，真正的进度反而看不见。
    # 加 -ErrorAction SilentlyContinue 也压不住，只有 .NET 调用能干净地吞掉。
    $resp = $null
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Timeout = $TimeoutSec * 1000
        $req.ReadWriteTimeout = $TimeoutSec * 1000
        $req.AllowAutoRedirect = $false
        $req.UserAgent = 'gqa-deploy'
        $req.KeepAlive = $false
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $body = ''
        $stream = $resp.GetResponseStream()
        if ($null -ne $stream) {
            $reader = New-Object System.IO.StreamReader($stream)
            $body = $reader.ReadToEnd()
            $reader.Close()
        }
        return [pscustomobject]@{ Code = $code; Body = $body }
    } catch [System.Net.WebException] {
        $code = 0
        $errResp = $_.Exception.Response
        if ($null -ne $errResp) {
            try { $code = [int]$errResp.StatusCode } catch { $code = 0 }
        }
        return [pscustomobject]@{ Code = $code; Body = '' }
    } catch {
        return [pscustomobject]@{ Code = 0; Body = '' }
    } finally {
        if ($null -ne $resp) { try { $resp.Close() } catch { } }
    }
}

# ==========================================================================
# 0. 会话准备
# ==========================================================================
Set-Location -Path $Root

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$logPath = Join-Path $LogDir ('deploy-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
try { Start-Transcript -Path $logPath -Force | Out-Null } catch { $logPath = '' }

Write-Host ''
Write-Host '  GitLab CE 一键部署 · 部署 + 就绪 + PAT + .env + 验收' -ForegroundColor White
Write-Host ('  目标地址 {0}   容器 {1}   内存上限 {2}' -f $BaseUrl, $Container, $MemoryLimit) -ForegroundColor DarkGray
if (-not $SmokeOnly) {
    Write-Host ('  root 密码来源 {0}' -f $pwdSource) -ForegroundColor DarkGray
}

try {

if ($SmokeOnly) {
    # 只验收：完全不碰容器，直接用现有 .env。适合改完用例后快速回归。
    if (-not (Test-Path $EnvFile)) {
        Stop-Deploy ("没有 {0}。先完整部署一次，或按 .env.example 手工创建。" -f $EnvFile)
    }
    $envText = [System.IO.File]::ReadAllText($EnvFile)
    if ($envText -match '(?m)^GITLAB_TOKEN=(\S*)\s*$')    { $pat = $Matches[1].Trim() }
    if ($envText -match '(?m)^GITLAB_BASE_URL=(\S+)\s*$') { $BaseUrl = $Matches[1].Trim() }

    Write-Note ("复用现有配置：{0}" -f $EnvFile)
    $tokenState = '缺失（需要鉴权的用例会 skip）'
    if ($pat -ne '') { $tokenState = '已配置' }
    Write-Note ("目标 {0} · Token {1}" -f $BaseUrl, $tokenState)
    if ($pat -eq '') { $Script:ExitCode = 1 }
}
else {

# ==========================================================================
# 1. 前置检查
<#
[1/8] 前置资源检查

读取 Windows WMI 内存、磁盘空闲、端口占用；Reset 参数控制是否`docker compose down -v`销毁数据卷（危险，数据清空）
检查规则：

- 宿主空闲内存 < 2GB：警告，容易 OOM
- 空闲磁盘 < 12GB：直接终止，镜像 + 数据卷占用很大
- 端口占用判定：**容器不存在的时候端口占用才算冲突；容器已经存在 running 状态时端口占用是预期行为**（这个判断很精细）
#>
# ==========================================================================
$t = Write-Head '前置检查：资源、磁盘、端口'
$phase = Get-Date

$os = Get-CimInstance Win32_OperatingSystem
$hostFreeGb = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$hostTotalGb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)

$drive = Get-PSDrive -PSProvider FileSystem |
         Where-Object { $_.Free -ne $null } |
         Sort-Object Free -Descending |
         Select-Object -First 1
$freeDiskGb = [math]::Round($drive.Free / 1GB, 1)

Write-Note ('宿主内存 空闲 {0}GB / 共 {1}GB · 最空闲盘 {2}: 空闲 {3}GB' -f `
    $hostFreeGb, $hostTotalGb, $drive.Name, $freeDiskGb)

if ($hostFreeGb -lt 2) {
    Write-Bad ("宿主物理内存只剩 {0}GB。GitLab 起来后会挤占换页空间，表现为启动慢、偶发 502。" -f $hostFreeGb)
    Write-Dim '建议先停掉暂时不用的容器：docker stop <name>（数据不受影响）'
}

if ($freeDiskGb -lt 12) {
    Stop-Deploy ("磁盘只剩 {0}GB。镜像下载约 1.2GB，解压后占用约 2.5GB，加上数据卷至少需要 12GB。" -f $freeDiskGb)
}

# 端口冲突只在本机没有 gitlab-ce 容器时才算问题：容器自己起来后必然占着端口
$existing = Get-ContainerState
if ($null -eq $existing -and (Test-PortListening $HostPort)) {
    $owner = Get-PortOwner $HostPort
    Stop-Deploy ("端口 $HostPort 已被占用（$owner）。换个端口：.\deploy.ps1 -HostPort 8930")
}
if ($existing -ne $null -and $existing.Status -eq 'running' -and (Test-PortListening $HostPort)) {
    Write-Note "端口 $HostPort 已由现有容器占用，属于预期"
}

if ($Reset) {
    if ($null -ne $existing) {
        Write-Note '按 -Reset 要求删除容器与数据卷…'
        [void](Invoke-Compose 'down' '-v' '--remove-orphans')
        Write-Bad '数据卷已清空，接下来会重新初始化（root 密码恢复为默认值）'
    } else {
        Write-Note '没有找到现有容器，-Reset 无需执行'
    }
}

Add-Result '前置检查' $phase 'OK' ("空闲内存 {0}GB / 磁盘 {1}GB" -f $hostFreeGb, $freeDiskGb)

# ==========================================================================
# 2. Docker 环境
<#
[2/8] Docker Daemon 检查 + Compose 校验

循环等待 Docker 引擎就绪（Docker Desktop 托盘亮了≠引擎就绪，这个是 Windows 上高频踩坑点）；探测 docker compose v2 / 旧版 docker-compose v1；
把脚本参数注入环境变量 `GITLAB_EXTERNAL_URL / GITLAB_HOST_PORT / GITLAB_MEM_LIMIT`，传给 docker-compose.yml（compose 文件内部读取这些 env）；
执行 `docker compose config --quiet` 校验 yml 语法合法性，提前拦截 compose 配置写错。
#>
# ==========================================================================
$t = Write-Head 'Docker 环境：daemon + compose'
$phase = Get-Date

if ($null -eq $Script:DockerExe) {
    Stop-Deploy @"
未找到 docker 命令。请安装 Docker Desktop 并启动：
  https://www.docker.com/products/docker-desktop/
"@
}

# daemon 可能正在启动：Docker Desktop 托盘亮了不等于引擎能连
$deadline = (Get-Date).AddSeconds($DaemonWaitSeconds)
$serverVersion = ''
$dockerVerdict = ''
while ((Get-Date) -lt $deadline) {
    $probe = Invoke-Capture -Exe $Script:DockerExe -ArgString "info --format {{.ServerVersion}}" -TimeoutSec 20
    $dockerVerdict = (($probe.Out + ' ' + $probe.Err) -replace '\s+', ' ').Trim()
    if ($probe.Code -eq 0 -and $dockerVerdict.Length -gt 0 -and $dockerVerdict -match '^\d+\.') {
        $serverVersion = $dockerVerdict
        break
    }
    Write-Note ('等待 Docker daemon…（最多 {0}s）' -f $DaemonWaitSeconds)
    Start-Sleep -Seconds 5
}

if ($serverVersion -eq '') {
    Stop-Deploy @"
Docker daemon 连不上。按可能性排序：

  1. Docker Desktop 没启动完整 —— 等托盘显示 "Docker Desktop running" 再重跑；
  2. WSL2 后端不可用 —— 日志里会提到 wsl.exe；在安全中心把它移出拦截名单，
     或改用 Hyper-V 后端；
  3. 虚拟化未开启 —— BIOS 打开 VT-x / SVM，并启用 Windows「虚拟机平台」；
  4. 企业策略限制 —— 需要 IT 放行。

最近一次 docker info 返回：$dockerVerdict

确认 docker info 能打印版本号后重跑本脚本。
"@
}

$memTotal = Invoke-Capture -Exe $Script:DockerExe -ArgString "info --format {{.MemTotal}}" -TimeoutSec 20
$nCpu     = Invoke-Capture -Exe $Script:DockerExe -ArgString "info --format {{.NCPU}}" -TimeoutSec 20
$dockerMemGb = 0
if ($memTotal.Code -eq 0 -and $memTotal.Out.Trim() -match '^\d+$') {
    $dockerMemGb = [math]::Round([double]$memTotal.Out.Trim() / 1GB, 1)
}
Write-Ok ('Docker daemon 就绪：server {0} · {1} 核 · 可用内存 {2}GB' -f `
    $serverVersion, $nCpu.Out.Trim(), $dockerMemGb)

if ($dockerMemGb -gt 0 -and $dockerMemGb -lt 8) {
    Write-Bad ("Docker 可用内存只有 {0}GB。GitLab CE 官方最低 4GB，但要稳定建议 8GB：" -f $dockerMemGb)
    Write-Dim 'Docker Desktop -> Settings -> Resources -> Memory 调大后 Apply & Restart'
}

# compose v2 还是 v1
$composeProbe = Invoke-Capture -Exe $Script:DockerExe -ArgString "compose version" -TimeoutSec 25
if ($composeProbe.Code -ne 0) {
    $legacy = Get-Command docker-compose -ErrorAction SilentlyContinue
    if ($null -eq $legacy) {
        Stop-Deploy '既没有 docker compose 也没有 docker-compose，请升级 Docker Desktop。'
    }
    $Script:ComposeExe  = $legacy.Source
    $Script:ComposeArgs = @()
    Write-Bad '检测到 docker-compose v1，建议升级到支持 docker compose 的版本'
} else {
    Write-Ok 'docker compose 可用（v2）'
}

# 把本次部署参数灌进 compose 的环境变量，保证 external_url、端口、内存三者一致
$env:GITLAB_EXTERNAL_URL = $BaseUrl
$env:GITLAB_HOST_PORT    = "$HostPort"
$env:GITLAB_MEM_LIMIT    = $MemoryLimit

# root 密码也必须在这里显式导出：docker-compose.yml 用的是
# ${GITLAB_ROOT_PASSWORD:?...} 必填占位，而 compose 只认当前进程的环境变量
# 或 gitlab-ce/.env —— 它不会去读 gitlab-qa/.env。
# 密码改从 .env 回读后，用户可能压根没设环境变量，不导出这一行
# compose 就会在 config/up 阶段报 "environment variable is required"，
# 而且报错长得很像 compose 文件写错了，很难联想到是密码没传。
# 只在当前会话生效，脚本结束即消失。
$env:GITLAB_ROOT_PASSWORD = $RootPwd

$composeFile = Join-Path $Root "docker-compose.yml"
$cfg = Invoke-Capture -Exe $Script:ComposeExe `
       -ArgString ((@() + $Script:ComposeArgs + @('-f', "`"$composeFile`"", 'config', '--quiet')) -join ' ') `
       -TimeoutSec 30 `
       -WorkDir $Root

if ($cfg.Code -ne 0) {
    Write-Dim (($cfg.Err + $cfg.Out).Trim())
    Stop-Deploy 'docker-compose.yml 校验失败，先修正上面的配置错误再重跑。'
}
Write-Ok ("compose 配置合法（external_url={0}，内存上限 {1}）" -f $BaseUrl, $MemoryLimit)

Add-Result 'Docker 环境' $phase 'OK' ("server {0} / {1} 核 / {2}GB" -f $serverVersion, $nCpu.Out.Trim(), $dockerMemGb)

# ==========================================================================
# 3. 镜像
<#
 [3/8] 镜像处理

`docker image inspect` 判断本地是否已经存在镜像：

- 存在：复用，`-SkipPull`时不检查更新
- 不存在：执行 docker pull 拉取镜像；拉取失败给出国内镜像加速配置方案（DaoCloud / 网易镜像
#>
# ==========================================================================
$t = Write-Head '镜像：本地已有则复用'
$phase = Get-Date

$imgInspect = Invoke-Capture -Exe $Script:DockerExe `
              -ArgString ("image inspect -f {{.Size}} " + $Image) -TimeoutSec 30
$hasImage = ($imgInspect.Code -eq 0 -and $imgInspect.Out.Trim() -match '^\d+$')

if ($hasImage) {
    $sizeGb = [math]::Round([double]$imgInspect.Out.Trim() / 1GB, 2)
    Write-Ok ("本地已有 $Image（{0}GB）" -f $sizeGb)
    if ($SkipPull) { Write-Note '按 -SkipPull 跳过检查更新' }
} elseif ($SkipPull) {
    Write-Bad '-SkipPull 但本地没有该镜像，仍然需要拉取'
}

if (-not $hasImage) {
    Write-Note '正在拉取 gitlab/gitlab-ce:latest（约 1.2GB，视网络 3~15 分钟）…'
    $pullStart = Get-Date
    & $Script:DockerExe pull --quiet $Image
    $pullCode = $LASTEXITCODE
    if ($pullCode -ne 0) {
        Stop-Deploy @"
镜像拉取失败。国内直连 Docker Hub 经常不通，先配镜像加速：

  Docker Desktop -> Settings -> Docker Engine，加入：

    "registry-mirrors": [
      "https://docker.m.daocloud.io",
      "https://hub-mirror.c.163.com"
    ]

Apply & Restart 后重跑：.\deploy.ps1
"@
    }
    $secs = [int]((Get-Date) - $pullStart).TotalSeconds
    Write-Ok ("镜像拉取完成（{0} 秒）" -f $secs)
    $imgInspect = Invoke-Capture -Exe $Script:DockerExe `
                  -ArgString ("image inspect -f {{.Size}} " + $Image) -TimeoutSec 30
    $hasImage = $true
}

$imageRef = ''
$imgId = Invoke-Capture -Exe $Script:DockerExe -ArgString ("image inspect -f {{.Id}} " + $Image) -TimeoutSec 30
if ($imgId.Code -eq 0) { $imageRef = $imgId.Out.Trim() }
Add-Result '镜像' $phase 'OK' ("{0}" -f $Image)

# ==========================================================================
# 4. 启动容器
# [4/8] 启动容器 `docker compose up -d` 后台启动；启动失败自动打印尾部日志，调用 Write-LogHint 做智能根因提示，抛出终止。
# ==========================================================================
$t = Write-Head '启动容器'
$phase = Get-Date

$upStart = Get-Date
$upRc = Invoke-Compose 'up' '-d'
if ($upRc -ne 0) {
    Write-Dim '最近 40 行日志：'
    Write-ContainerLogs 40
    Write-LogHint
    Stop-Deploy '容器启动失败。常见原因：端口冲突、内存不足、镜像损坏。'
}
$upSecs = [int]((Get-Date) - $upStart).TotalSeconds
Write-Ok ("容器已启动（{0} 秒）：{1}" -f $upSecs, $Container)

Add-Result '启动容器' $phase 'OK' ("docker compose up -d · {0}s" -f $upSecs)

# ==========================================================================
# 5. 等待就绪
<#
### [5/8] 等待就绪（最长 TimeoutMinutes 分钟）

循环轮询：

1. 读取容器状态，如果 exited/dead、反复 restart（>=2 次）直接判定崩溃退出，不傻等超时
2. 调用 Test-GitLabReady 做业务就绪探测
3. 每 15s 打印一次心跳日志（耗时、健康状态、最后一行容器日志）
就绪之后额外再重试探测 /api/v4/version，等待 API 层完全拉起（页面起来了，API 还可能慢几秒）
#>
# ==========================================================================
$t = Write-Head ("等待就绪（上限 {0} 分钟）" -f $TimeoutMinutes)
$phase = Get-Date
$waitStart = Get-Date

if ($TimeoutMinutes -le 0) {
    Write-Bad '超时时间必须大于 0'
    Stop-Deploy '参数 -TimeoutMinutes 无效'
}

$deadline  = (Get-Date).AddMinutes($TimeoutMinutes)
$ready     = $false
$lastBeat  = Get-Date
$lastGate  = ''
$lastState = $null

while ((Get-Date) -lt $deadline) {
    $state = Get-ContainerState
    $lastState = $state

    if ($null -ne $state -and ($state.Status -eq 'exited' -or $state.Status -eq 'dead')) {
        Write-Bad ("容器状态 {0}（exit {1}，重启 {2} 次）" -f $state.Status, $state.ExitCode, $state.Restarts)
        Write-Dim '最后 50 行日志：'
        Write-ContainerLogs 50
        Write-LogHint
        Stop-Deploy '容器启动后退出。上面的日志里通常能看到具体原因（多数是配置项不受支持、内存不足或端口冲突）。'
    }

    # 崩溃循环：restart 策略会不停拉起，状态长期停在 restarting，
    # 既不会 exited 也不会 ready。不主动识别的话就是白等满整个超时。
    if ($null -ne $state -and $state.Status -eq 'restarting') {
        $restartCount = 0
        if ($state.Restarts -match '^\d+$') { $restartCount = [int]$state.Restarts }
        if ($restartCount -ge 2) {
            Write-Bad ("容器在反复重启（已 {0} 次），不会自行恢复" -f $restartCount)
            Write-Dim '最后 60 行日志：'
            Write-ContainerLogs 60
            Write-LogHint
            Stop-Deploy '容器陷入重启循环。日志里那行 FATAL 就是原因。'
        }
    }

    $gate = Test-GitLabReady
    $lastGate = $gate.Note
    if ($gate.Ready) {
        $ready = $true
        break
    }

    if (((Get-Date) - $lastBeat).TotalSeconds -ge 15) {
        $lastBeat = Get-Date
        $elapsed = [int]((Get-Date) - $waitStart).TotalSeconds
        $logProbe = Invoke-Capture -Exe $Script:DockerExe `
                    -ArgString ('logs --tail 1 ' + $Container) -TimeoutSec 20
        $logLine = (($logProbe.Out + ' ' + $logProbe.Err) -replace '\s+', ' ').Trim()
        if ($logLine.Length -gt 86) { $logLine = $logLine.Substring(0, 86) + '…' }
        $healthText = 'n/a'
        if ($null -ne $state -and $state.Health -ne '') { $healthText = $state.Health }
        Write-Note ('{0:mm\:ss}  {1}  health={2}  {3}' -f `
            ([TimeSpan]::FromSeconds($elapsed)), $lastGate, $healthText, $logLine)
    }
    Start-Sleep -Seconds 8
}

if (-not $ready) {
    Write-Bad '超时未就绪。先看这几条：'
    Write-Dim 'docker compose ps                       # 容器还在不在'
    Write-Dim 'docker compose logs --tail 100 gitlab   # 卡在哪一步'
    Write-Dim 'docker stats gitlab-ce                  # 内存是否被打满'
    if ($null -ne $lastState) {
        Write-Dim ("当前状态：status={0} health={1} restarts={2}" -f `
            $lastState.Status, $lastState.Health, $lastState.Restarts)
    }
    Write-LogHint
    Stop-Deploy ("等待 {0} 分钟仍未就绪。首次启动卡在数据库迁移很常见，可加大 -TimeoutMinutes 40 重跑。" -f $TimeoutMinutes)
}

$readySecs = [int]((Get-Date) - $waitStart).TotalSeconds
Write-Ok ("GitLab 就绪（用时 {0} 分 {1} 秒 · {2}）" -f [int]($readySecs / 60), ($readySecs % 60), $lastGate)

# API 层再确认一次：readiness 过了但 API 还没挂上的窗口很窄，仍然检查
$apiCode = 0
for ($i = 0; $i -lt 12; $i++) {
    $api = Get-HttpStatus "$BaseUrl/api/v4/version" 8
    $apiCode = $api.Code
    if ($apiCode -eq 200 -or $apiCode -eq 401) { break }
    Start-Sleep -Seconds 5
}
if ($apiCode -eq 200 -or $apiCode -eq 401) {
    Write-Ok ("API 已响应（/api/v4/version -> HTTP {0}）" -f $apiCode)
} else {
    Write-Bad ("API 尚未响应（HTTP {0}），后续验收可能不稳定" -f $apiCode)
}

Add-Result '等待就绪' $phase 'OK' ("{0}s" -f $readySecs)

# ==========================================================================
# 6. 签发 PAT
<#
### [6/8] 签发 PAT 令牌（幂等逻辑，非常巧妙）

1. 优先读取旧.env 里的 token，调用 /api/v4/user 验证有效性，**有效就直接复用，不加 -Force 不重新生成**（避免 CI 流水线中途 token 失效）
2. 旧 token 无效 /-Force 强制重签：执行 Ruby 脚本
   - 先撤销同名 `gqa-automation` 的旧 token，避免堆积大量废弃 token
   - 尝试设置固定明文 PAT；如果 GitLab 版本校验拒绝固定 token，捕获异常回退到服务端随机生成 token
3. 兜底：先检查管理员账号数量，如果 admin=0，自动执行`gitlab-rake db:seed_fu`初始化 root 用户，解决 seed 静默失败问题
4. 拿到 token 之后，调用 API 验证 token 权限、读取 GitLab 版本
#>
# ==========================================================================
$t = Write-Head '签发 Personal Access Token'
$phase = Get-Date
$pat = ''

# 复用仍然有效的旧 Token：每次部署都换 Token 会让正在跑的任务突然 401
$existingPat = ''
if ((Test-Path $EnvFile) -and (-not $Force)) {
    $envText = [System.IO.File]::ReadAllText($EnvFile)
    if ($envText -match '(?m)^GITLAB_TOKEN=(\S+)\s*$') { $existingPat = $Matches[1].Trim() }
}
if ($existingPat -ne '') {
    try {
        $me = Invoke-RestMethod -Uri "$BaseUrl/api/v4/user" -TimeoutSec 20 `
              -Headers @{ 'PRIVATE-TOKEN' = $existingPat }
        if ($me.username) {
            $pat = $existingPat
            Write-Ok ("复用 .env 中的 Token（用户 {0}），未重复签发" -f $me.username)
            Write-Dim '需要换新 Token 时加 -Force'
        }
    } catch {
        Write-Note '.env 中的旧 Token 已失效，重新签发'
    }
}

if ($pat -eq '') {
    $ruby = @'
# 幂等签发：先撤销同名旧 token，避免每次部署堆一堆失效记录。
# 固定明文便于复现；如果当前 GitLab 版本的格式校验拒绝，退回服务端生成。
name   = 'gqa-automation'
fixed  = '__PAT__'
scopes = [:api, :read_api, :read_user, :read_repository, :write_repository]

user = User.find_by_username('root')
abort 'ROOT_USER_MISSING' if user.nil?

user.personal_access_tokens.where(name: name).find_each do |old|
  begin
    old.revoke!
  rescue StandardError
    old.update_column(:revoked, true)
  end
end

def gqa_create(user, name, scopes, fixed)
  t = user.personal_access_tokens.new(name: name, scopes: scopes,
                                      expires_at: 365.days.from_now)
  t.set_token(fixed) unless fixed.nil? || fixed.empty?
  t.save!
  t
end

begin
  token = gqa_create(user, name, scopes, fixed)
  src = 'fixed'
rescue StandardError => e
  STDERR.puts "FIXED_REJECTED #{e.class}: #{e.message}"
  token = gqa_create(user, name, scopes, nil)
  src = 'generated'
end

puts "GQA_SOURCE=#{src}"
puts "GQA_TOKEN=#{token.token}"
puts "GQA_TOKEN_ID=#{token.id}"
puts "GQA_EXPIRES=#{token.expires_at}"
'@
    $ruby = $ruby.Replace('__PAT__', $PatValue)

    # 管理员账号守卫。
    # GitLab 19 的密码策略会拒绝"常见词组合"（gitlab / admin / password 之类），
    # 而 seed 失败是**静默**的：登录页照常打开、API 照常响应，库里却没有 root。
    # 不查这一步，后面只会得到一句含糊的 "ROOT_USER_MISSING"。
    $adminCount = Get-AdminCount
    if ($adminCount -eq 0) {
        Write-Bad '实例里没有管理员账号：首次 seed 没成功（密码策略或启动中断都会导致）'
        Write-Note '补跑 db:seed_fu（约 1 分钟）…'
        $seed = Invoke-Capture -Exe $Script:DockerExe `
                -ArgString ('exec {0} gitlab-rake db:seed_fu' -f $Container) -TimeoutSec 900
        $seedText = (($seed.Out + ' ' + $seed.Err) -replace '\s+', ' ').Trim()
        if ($seedText -match 'common(ly)? used combinations|Password must not') {
            Write-Bad 'GITLAB_ROOT_PASSWORD 被密码策略拒绝（含常见词组合）'
            Write-Dim '换成不含 gitlab / admin / password 等词的强密码，改完重跑本脚本'
        }
        $adminCount = Get-AdminCount
        if ($adminCount -gt 0) { Write-Ok ('管理员账号已补齐（{0} 个 admin）' -f $adminCount) }
    } elseif ($adminCount -gt 0) {
        Write-Ok ('管理员账号就绪（{0} 个 admin）' -f $adminCount)
    } else {
        Write-Bad '读不到用户表（rails runner 异常），继续尝试签发，失败时会打印原因'
    }

    $tokenSource = ''
    $lastErr = ''
    $attempt = 0
    while ($attempt -lt 3 -and $pat -eq '' -and $adminCount -ne 0) {
        $attempt++
        if ($attempt -gt 1) {
            Write-Note ("第 {0} 次重试（Rails 可能还在预热）…" -f $attempt)
            Start-Sleep -Seconds 20
        }

        Write-Note '在容器内执行 rails runner（首次约 30~90 秒）…'
        $run = Invoke-RailsScript -Name 'token' -Body $ruby -TimeoutSec 420

        foreach ($line in ($run.Out -split "`r?`n")) {
            if ($line -match '^GQA_TOKEN=(\S+)\s*$') { $pat = $Matches[1].Trim() }
            if ($line -match '^GQA_SOURCE=(\S+)')   { $tokenSource = $Matches[1] }
        }

        if ($pat -eq '') {
            $lastErr = (($run.Err + ' ' + $run.Out) -replace '\s+', ' ').Trim()
            if ($lastErr.Length -gt 300) { $lastErr = $lastErr.Substring(0, 300) + '…' }
            if ($lastErr -match 'ROOT_USER_MISSING') { break }
        }
    }

    if ($pat -ne '') {
        Write-Ok ("Token 已签发：{0}…（{1} 位）" -f $pat.Substring(0, 10), $pat.Length)
        if ($tokenSource -eq 'generated') {
            Write-Bad '指定明文被当前版本拒绝，已改用服务端生成的 Token（仍会写入 .env）'
        }
    } else {
        Write-Bad '自动签发失败。可以手动创建：'
        Write-Dim "$BaseUrl/-/profile/personal_access_tokens"
        Write-Dim '勾选 api / read_api / read_user / read_repository / write_repository'
        Write-Dim '创建后把值填进 gitlab-qa\.env 的 GITLAB_TOKEN'
        if ($lastErr -ne '') { Write-Dim ('原始错误：' + $lastErr) }
    }
}

if ($pat -ne '') {
    # 用 /user 验证，能直接看出 token 有没有 api scope
    try {
        $me = Invoke-RestMethod -Uri "$BaseUrl/api/v4/user" -Headers @{ 'PRIVATE-TOKEN' = $pat } -TimeoutSec 20
        Write-Ok ("API 鉴权通过：{0}（{1}）" -f $me.username, $me.name)
    } catch {
        Write-Bad ('Token 验证失败：' + $_.Exception.Message)
        $Script:ExitCode = 1
    }
    try {
        $ver = Invoke-RestMethod -Uri "$BaseUrl/api/v4/version" -Headers @{ 'PRIVATE-TOKEN' = $pat } -TimeoutSec 20
        Write-Ok ("GitLab 版本 {0}" -f $ver.version)
    } catch {
        Write-Bad '版本接口读取失败（不影响后续测试）'
    }
}

if ($pat -eq '') {
    Add-Result '签发 PAT' $phase 'FAIL' '签发失败，需手动创建'
    $Script:ExitCode = 1
} else {
    Add-Result '签发 PAT' $phase 'OK' 'gqa-automation · api+read_repository+write_repository'
}

# ==========================================================================
# 7. 写 .env
# ==========================================================================
$t = Write-Head '写入 gitlab-qa\.env'
$phase = Get-Date

if (-not (Test-Path $QaDir)) { New-Item -ItemType Directory -Path $QaDir -Force | Out-Null }

if (Test-Path $EnvFile) {
    $bak = $EnvFile + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item -LiteralPath $EnvFile -Destination $bak -Force
    Write-Note ("已备份旧配置：{0}" -f (Split-Path -Leaf $bak))
}

# 用 here-string 而不是 @(...) 数组拼接：
# `'a' + (Get-Date),'b',...` 这种写法在 PS 5.1 里容易被解析成一个"内含数组的元素"，
# 落盘后 12 个键一条都匹配不上，还得回头查半天。
$envText = @"
# 由 gitlab-ce/deploy.ps1 生成于 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
# 含明文 Token，已被 gitlab-qa/.gitignore 忽略，不要提交
GITLAB_ENV=local
GITLAB_BASE_URL=$BaseUrl
GITLAB_TOKEN=$pat
GITLAB_ADMIN_USER=root
GITLAB_ROOT_PASSWORD=$RootPwd
GITLAB_SSH_PORT=2222
GITLAB_TIMEOUT=30
PERF_CONCURRENCY=8
PERF_SAMPLES=60
PERF_P95_MS=2000
PERF_ERROR_RATE=0.02
RESOURCE_PREFIX=qa-auto
"@
# here-string 在 LF 文件里出来的是 LF，统一成 CRLF：Windows 上记事本打开才换行正常
$envText = (($envText -split "`r?`n") -join "`r`n") + "`r`n"

# UTF8Encoding($false) = 无 BOM。带 BOM 时第一个键名会变成 "\ufeffGITLAB_ENV"，
# dotenv 读不到，表现成「配置明明写了却没生效」，极难排查。
[System.IO.File]::WriteAllText($EnvFile, $envText, (New-Object System.Text.UTF8Encoding $false))

# 写后回读：确认无 BOM、键齐全、Token 未截断
$readBack = [System.IO.File]::ReadAllText($EnvFile)
if ($readBack.Length -gt 0 -and [int][char]$readBack[0] -eq 0xFEFF) {
    Stop-Deploy '.env 写出了 BOM，dotenv 会读不到第一个键。请检查 PowerShell 版本或磁盘编码。'
}
$required = @('GITLAB_ENV', 'GITLAB_BASE_URL', 'GITLAB_TOKEN', 'GITLAB_ADMIN_USER',
              'GITLAB_ROOT_PASSWORD', 'GITLAB_SSH_PORT', 'GITLAB_TIMEOUT',
              'PERF_CONCURRENCY', 'PERF_SAMPLES', 'PERF_P95_MS', 'PERF_ERROR_RATE',
              'RESOURCE_PREFIX')
$missing = @()
foreach ($k in $required) {
    if ($readBack -notmatch ('(?m)^' + [regex]::Escape($k) + '=')) { $missing += $k }
}
if ($missing.Count -gt 0) {
    Stop-Deploy ('写入的 .env 缺少键：' + ($missing -join ', '))
}
Write-Ok ("已写入 {0}（无 BOM，{1} 个配置项）" -f $EnvFile, $required.Count)

# 随机生成的密码必须显式打出来：它是 root 的唯一凭据，
# 只写进 .env 而不告知，用户第一次登录时会以为密码还是自己设的那个。
if ($pwdSource -eq '随机生成（首次部署）') {
    Write-Host ''
    Write-Host '  root 密码由脚本随机生成（已写入 .env），请自行保存：' -ForegroundColor Yellow
    Write-Host ('    {0}' -f $RootPwd) -ForegroundColor White
    Write-Host ('  登录：http://localhost:{0}   账号 root' -f $HostPort) -ForegroundColor Gray

    # GITLAB_ROOT_PASSWORD 只在首次 seed 时生效。实例一旦建好，后续启动
    # 不会改写已有 root 的密码——所以"随机生成"只有在数据卷是空的时候才真的成为
    # 实例密码。下面这句防的是：.env 丢了但容器还在，结果 .env 里写着一个
    # 实例根本不认识的密码，UI 登录用例全红，而报错看起来像框架坏了。
    if ($null -ne $existing) {
        Write-Host ''
        Write-Host '  [!!] 已存在同名实例，它现在的 root 密码不会被本脚本改写。' -ForegroundColor Yellow
        Write-Host '       .env 里这个新密码对它无效。要让它生效请加 -Reset（会清空数据卷）。' -ForegroundColor DarkGray
    }
    Write-Host ''
}

}   # end: -not $SmokeOnly

# ==========================================================================
# 8. 验收冒烟
# ==========================================================================
$t = Write-Head '验收：环境自检 + 冒烟'
$phase = Get-Date

function Resolve-Python {
    $candidates = @()
    if ($PythonExe -ne '') { $candidates += ,@($PythonExe, @()) }
    $candidates += ,@('py', @('-3'))
    $candidates += ,@('python', @())

    foreach ($cand in $candidates) {
        $exeName = $cand[0]
        $preArgs = $cand[1]
        $found = Get-Command $exeName -ErrorAction SilentlyContinue
        if ($null -eq $found) { continue }

        $preStr = ''
        if ($preArgs.Count -gt 0) { $preStr = ($preArgs -join ' ') + ' ' }

        # 用 Invoke-Capture 而不是 `& $py -c ... 2>&1`：理由同上，
        # 解释器把 ImportError 写到 stderr 时，Stop 模式下会被当成本脚本的错误。
        $code = "import pytest, requests, allure, dotenv; print('PYOK')"
        $probe = Invoke-Capture -Exe $found.Source -ArgString ($preStr + '-c "' + $code + '"') -TimeoutSec 60
        if (($probe.Out + $probe.Err) -match 'PYOK') {
            $ver = Invoke-Capture -Exe $found.Source -ArgString ($preStr + '-V') -TimeoutSec 30
            $verText = (($ver.Out + ' ' + $ver.Err) -replace '\s+', ' ').Trim()
            return [pscustomobject]@{ Exe = $found.Source; Pre = $preArgs; Version = $verText }
        }
    }
    return $null
}

$py = Resolve-Python
$smokeFailed = $false
$reportPath = Join-Path $QaDir 'allure-report\index.html'

if ($SkipSmoke) {
    Write-Note '按 -SkipSmoke 跳过验收'
    Add-Result '验收冒烟' $phase 'SKIP' '未执行'
}
elseif ($null -eq $py) {
    Write-Bad '找不到装好测试依赖的 Python。安装后重跑 -SmokeOnly：'
    Write-Dim 'py -3 -m pip install pytest pytest-xdist pytest-rerunfailures allure-pytest requests python-dotenv jsonschema pydantic playwright'
    $smokeFailed = $true
    Add-Result '验收冒烟' $phase 'FAIL' 'Python 依赖缺失'
}
else {
    Write-Ok ("Python {0}" -f $py.Version)

    # 子进程会继承当前会话的环境变量，而 Settings 的优先级是
    # 环境变量 > .env。残留的旧值会盖掉刚写好的 .env，必须先清干净。
    foreach ($k in @('GITLAB_ENV', 'GITLAB_BASE_URL', 'GITLAB_TOKEN', 'GITLAB_ADMIN_USER',
                     'GITLAB_ROOT_PASSWORD', 'GITLAB_SSH_PORT', 'GITLAB_TIMEOUT',
                     'PERF_CONCURRENCY', 'PERF_SAMPLES', 'PERF_P95_MS', 'PERF_ERROR_RATE',
                     'RESOURCE_PREFIX')) {
        if (Test-Path ("env:" + $k)) { Remove-Item ("env:" + $k) -ErrorAction SilentlyContinue }
    }

    Push-Location $QaDir
    try {
        Write-Note 'doctor.py：连通性 / 鉴权 / 版本 / 分页 / 写权限'
        $docCode = Invoke-Py -Py $py -PyArgs @('tools\doctor.py') -TimeoutSec 300
        if ($docCode -ne 0) {
            Write-Bad ("环境自检未全通过（退出码 {0}），下面的用例结果可能受影响" -f $docCode)
        }

        foreach ($suite in $Suites) {
            if ($AllowedSuites -notcontains $suite) {
                Write-Bad ("未知套件 {0}，可选：{1}" -f $suite, ($AllowedSuites -join ', '))
                $smokeFailed = $true
                continue
            }
            Write-Note ("跑套件 {0} …" -f $suite)
            $runArgs = @('tools\run.py', '--suite', $suite)
            if ($Open) { $runArgs += '--open' }
            if ($KeepResults) { $runArgs += '--keep-results' }
            $rc = Invoke-Py -Py $py -PyArgs $runArgs -TimeoutSec 900
            if ($rc -ne 0) {
                Write-Bad ("套件 {0} 未通过（退出码 {1}）" -f $suite, $rc)
                $smokeFailed = $true
            } else {
                Write-Ok ("套件 {0} 通过" -f $suite)
            }
        }
    } finally {
        Pop-Location
    }

    if (Test-Path $reportPath) {
        Write-Ok ("Allure 报告：{0}" -f $reportPath)
    } else {
        Write-Bad '没有生成 Allure 报告（未安装 allure 命令行时会跳过）'
    }

    if ($smokeFailed) {
        Add-Result '验收冒烟' $phase 'FAIL' ('套件 ' + ($Suites -join ','))
    } else {
        Add-Result '验收冒烟' $phase 'OK' ('套件 ' + ($Suites -join ','))
    }
}

if ($smokeFailed -and $Script:ExitCode -eq 0) { $Script:ExitCode = 2 }

# ==========================================================================
# 9. 汇总
# ==========================================================================
Write-Host ''
Write-Host '================ 部署汇总 ================' -ForegroundColor Cyan
foreach ($r in $Script:Results) {
    $color = 'Red'
    if ($r.Status -eq 'OK')   { $color = 'Green' }
    if ($r.Status -eq 'SKIP') { $color = 'DarkGray' }
    Write-Host ('  {0,-10} {1,-4} {2,7}s   {3}' -f $r.Phase, $r.Status, $r.Seconds, $r.Detail) -ForegroundColor $color
}
$totalSecs = [int]((Get-Date) - $Script:StartTime).TotalSeconds
Write-Host ('  合计 {0} 分 {1} 秒' -f [int]($totalSecs / 60), ($totalSecs % 60)) -ForegroundColor DarkGray

Write-Host ''
Write-Host '  Web UI   : ' -NoNewline -ForegroundColor White
Write-Host $BaseUrl
Write-Host '  管理员   : ' -NoNewline -ForegroundColor White
Write-Host ("root / {0}    （首次登录后立即改密码）" -f $RootPwd)
Write-Host '  API 地址 : ' -NoNewline -ForegroundColor White
Write-Host "$BaseUrl/api/v4"
Write-Host '  Token    : ' -NoNewline -ForegroundColor White
if ($pat -ne '') { Write-Host $pat } else { Write-Host '<未签发，见上方手动步骤>' -ForegroundColor Yellow }
Write-Host '  .env     : ' -NoNewline -ForegroundColor White
Write-Host $EnvFile
if ($logPath -ne '') {
    Write-Host '  日志     : ' -NoNewline -ForegroundColor White
    Write-Host $logPath
}

if ($Script:ExitCode -eq 0) {
    Write-Host ''
    Write-Host '  结论：部署完成，冒烟通过。下一步可以跑：' -ForegroundColor Green
    Write-Host "    cd ..\gitlab-qa" -ForegroundColor Gray
    Write-Host "    python tools\run.py --suite regression   # 回归全量" -ForegroundColor Gray
    Write-Host "    python tools\run.py --suite ui --open    # UI + 报告" -ForegroundColor Gray
    Write-Host "    python tools\run.py --suite perf         # 性能门禁" -ForegroundColor Gray
} elseif ($Script:ExitCode -eq 2) {
    Write-Host ''
    Write-Host '  结论：GitLab 已部署，但验收未通过。' -ForegroundColor Yellow
    Write-Host '        先看 Allure 报告里的失败用例；环境问题优先跑 python tools\doctor.py。' -ForegroundColor Gray
} else {
    Write-Host ''
    Write-Host '  结论：部署未完成。按上面的提示处理后重跑本脚本（已完成的步骤会复用）。' -ForegroundColor Red
}

}
catch {
    $msg = $_.Exception.Message
    Write-Host ''
    if ($msg -like 'GQA-ABORT|*') {
        Write-Host ('  [中止] ' + $msg.Substring(10)) -ForegroundColor Red
    } else {
        Write-Host ('  [未预期错误] ' + $msg) -ForegroundColor Red
        if ($_.ScriptStackTrace) { Write-Host ('  ' + $_.ScriptStackTrace) -ForegroundColor DarkGray }
    }
    if ($Script:ExitCode -eq 0) { $Script:ExitCode = 1 }
}
finally {
    # 部署状态落盘：下次排查能直接看到哪一步耗时异常、用的什么镜像
    try {
        $state = [pscustomobject]@{
            generatedAt   = (Get-Date).ToString('s')
            baseUrl       = $BaseUrl
            container     = $Container
            image         = $Image
            imageId       = $imageRef
            dockerVersion = $serverVersion
            memoryLimit   = $MemoryLimit
            tokenMasked   = $(if ($pat -ne '') { $pat.Substring(0, 10) + '…' } else { '' })
            phases        = $Script:Results
            exitCode      = $Script:ExitCode
            log           = $logPath
        }
        [System.IO.File]::WriteAllText(
            $StateFile,
            ($state | ConvertTo-Json -Depth 5),
            (New-Object System.Text.UTF8Encoding($false))
        )
    } catch { }

    try { Stop-Transcript | Out-Null } catch { }
}

exit $Script:ExitCode
