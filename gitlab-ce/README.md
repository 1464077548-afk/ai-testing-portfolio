# GitLab CE 本机部署

给 `gitlab-qa` 测试框架提供被测靶机。

> **前提**：GitLab CE 官方只发布 Linux 包，没有 Windows 原生安装方式。
> Windows 本机唯一可行路径是 Docker Desktop（Linux 容器跑在 WSL2 或 Hyper-V 上）。

## 先做这一步：确认 Docker 真的能用

`docker --version` 能打印版本**不代表 daemon 起来了**。先跑这个：

```powershell
docker info --format '{{.ServerVersion}}'
```

打印出版本号才算可用。报 `The system cannot find the file specified` /
`failed to connect to the docker API at npipe:...dockerDesktopLinuxEngine`
说明 daemon 没起来，此时继续部署只会拿到一堆连不上引擎的错。

按顺序排查：

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有 `\\.\pipe\docker*` 都不存在 | Docker Desktop 没启动 | 启动后等 60~90 秒再试 |
| 启动后仍连不上，日志提到 `wsl` | WSL2 后端不可用 | 见下节「WSL2 不可用时」 |
| `docker info` 卡住不返回 | 后端在初始化 | 等，别反复 Ctrl+C |

### WSL2 不可用时

本机实测过这条路径：Docker Desktop 进程在，但 `dockerDesktopLinuxEngine` 管道
始终不存在，Docker Desktop 日志指向 `wsl.exe` 无法执行（被安全策略拦截）。
这种情况下 **Windows 本机没有可行的替代方案**——GitLab CE 没有 Windows 原生包，
Hyper-V 后端同样需要 WSL 或 Linux 内核支持。

三条替代路线，按推荐度：

1. **装到一台 Linux 机器**（云主机 4C8G 起，或局域网内任意 Linux）。
   把 `docker-compose.yml` 拷过去，改 `external_url` 为那台机器的地址，`docker compose up -d`。
   本框架与机器无关，`--base-url http://<ip>:8929` 指过去即可。
2. **Windows 上装 WSL2**（需管理员权限，可能需要重启并开启虚拟化）：
   `wsl --install -d Ubuntu`，装完后 Docker Desktop 选 WSL2 后端。
3. **先用内置靶机把框架跑通**（`gitlab-qa` 目录）：
   `python tools/run.py --suite all --mock`。
   靶机不是 GitLab，但足以验证六层测试、性能门禁、报告与 CI 全链路；
   等真实例就绪后换 `--base-url` 即可，用例一行不改。

## 快速开始

Docker Desktop 起来之后，一条命令跑完全部：

```powershell
cd gitlab-ce
.\deploy.ps1
```

这条命令不只是 `docker compose up`。它按 8 个阶段推进，任何一步失败都会停下来说明原因，
并给出可直接复制执行的排查命令：

| 阶段 | 做什么 | 失败时你会看到 |
|---|---|---|
| 1 前置检查 | 内存、磁盘、端口占用 | 端口被谁占着（PID + 进程名）、磁盘还差多少 |
| 2 Docker 环境 | 等 daemon 就绪（默认 2 分钟）、校验 compose | daemon 起不来的 4 类原因逐条列出 |
| 3 镜像 | 本地已有就复用 | 拉取失败时给出镜像加速配置 |
| 4 启动容器 | `docker compose up -d` | 自动贴出最近 40 行日志 |
| 5 等待就绪 | `/-/readiness` + 容器健康 + 15 秒一次心跳 | 贴容器状态、日志尾巴与三条排查命令 |
| 6 签发 PAT | 容器内 `gitlab-rails runner`，幂等 | 签名被拒时自动改用服务端生成，仍失败给手动步骤 |
| 7 写 `.env` | UTF-8 **无 BOM**，写后回读校验 | 缺键、带 BOM 会直接报出来 |
| 8 验收冒烟 | `doctor.py` 自检 + `smoke` 套件 + Allure | 失败用例进报告，退出码 2 |

首次执行约 10~25 分钟（拉镜像 ~1.2GB，再做数据库迁移）。跑完会打印一张汇总表，
包括每阶段耗时、Web 地址、Token、`.env` 路径和日志路径。

### 常用参数

```powershell
.\deploy.ps1                          # 默认：部署 + 冒烟，内存上限 6G
.\deploy.ps1 -MemoryLimit 8G          # 内存富余时给到 8G，502 明显减少
.\deploy.ps1 -TimeoutMinutes 40       # 慢机器放宽等待（首启迁移可能更久）
.\deploy.ps1 -SkipPull                # 镜像已存在，跳过拉取
.\deploy.ps1 -Reset                   # 连数据卷一起清空重来（不可恢复）
.\deploy.ps1 -HostPort 8930           # 换端口，external_url 与容器监听一起改
.\deploy.ps1 -Force                   # 强制重签 PAT（默认复用 .env 里仍有效的）
.\deploy.ps1 -SkipSmoke               # 只部署，不跑验收
.\deploy.ps1 -SmokeOnly -Open         # 不部署，用现有 .env 只跑冒烟并打开报告
.\deploy.ps1 -SmokeOnly -Suites smoke,api   # 冒烟 + 接口全量
.\deploy.ps1 -KeepResults     # 结果目录不让删时用（见排障表）
```

几个刻意的设计：

- **PAT 默认复用**。每次部署都换 Token，会让正在跑的流水线突然 401。
  复用前会先调 `/api/v4/user` 验证；失效了才重签，重签会先撤销同名旧 Token。
- **`.env` 必须无 BOM**。带 BOM 时第一个键名会变成 `\ufeffGITLAB_ENV`，
  dotenv 读不到，表现成“配置明明写了却没生效”。脚本写完会回读并校验 12 个键。
- **退出码分三档**：`0` 全绿、`1` 部署失败、`2` 部署成功但验收未通过。
  CI 里可以直接据此判断该修环境还是修用例。
- **日志落盘**：`logs\deploy-<时间戳>.log`，含每个阶段的时间点。
  状态摘要写在 `.deploy-state.json`。

部署完成后：

| 项 | 值 |
|---|---|
| Web UI / API | http://localhost:8929 |
| 管理员 | `root` / 部署结束时控制台会打印密码（首次登录后立即改） |
| SSH | `ssh -p 2222 git@localhost` |
| 容器名 | `gitlab-ce` |

> 内存提醒：宿主物理内存低于 8GB 可用时，GitLab 会和其他容器抢内存，
> 表现为启动很慢、页面偶发 502。脚本会在前置检查里提示。
> 实在腾不出内存时，先 `docker stop` 掉暂时不用的容器（数据不受影响）。

## 手动部署

不用脚本的话，三步：

```powershell
docker pull gitlab/gitlab-ce:latest
docker compose up -d
docker compose logs -f gitlab      # 看初始化进度
```

然后手工签发 Token（GitLab 没有"用密码换 PAT"的公开 API，容器部署只能进内部跑 rails）：

```powershell
docker exec -it gitlab-ce gitlab-rails runner "
user = User.find_by_username('root')
t = user.personal_access_tokens.create(name: 'qa', scopes: [:api, :read_api, :read_repository, :write_repository], expires_at: 365.days.from_now)
t.set_token('glpat-xxxxxxxxxxxxxxxxxxxx')
t.save!
puts t.token"
```

## 配置说明

`docker-compose.yml` 相对官方默认做了这些裁剪，全部针对"单机练手"这个前提：

**关掉监控栈**（`prometheus` / `grafana` / `alertmanager` / 各类 exporter）
省 1.5~2GB 常驻内存。练手环境用不到它们，开着只会吃内存。

**应用规格收敛**
`puma` 2 workers × 4 threads、`sidekiq` 并发 4、`postgresql shared_buffers` 256MB。
默认配置是按生产多核场景给的，单机上开满会争抢资源。

**用命名卷而不是 bind mount**
Windows 的 bind mount 要求 Docker Desktop 开启盘符文件共享，且 NTFS 上跑 PostgreSQL
性能很差。命名卷落在 WSL2 的 ext4 里，两个问题一起解决。

**健康检查留 5 分钟启动宽限**（`start_period: 300s`）
首次启动要跑迁移，这段时间返回 502 是正常的；不留宽限会被判为失败并反复重启。

**内存上限 6G / 预留 3G（可由 `-MemoryLimit` 覆盖）**
低于 6GB 可用内存时 Puma 和 Sidekiq 会互相挤兑，表现为页面时快时慢、
偶发 502——这种"不稳定"很容易被误判成测试框架的问题。
宿主内存富余时用 `.\deploy.ps1 -MemoryLimit 8G` 部署，症状会明显缓解。

## 排障

```powershell
docker compose ps                        # 容器状态
docker compose logs --tail 100 gitlab    # 卡在哪一步
docker stats gitlab-ce                   # 内存是否打满
docker exec -it gitlab-ce gitlab-ctl status
docker exec -it gitlab-ce gitlab-ctl reconfigure   # 改完配置后重载
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `docker info` 失败 | daemon 没起来 | 启动 Docker Desktop 等托盘就绪 |
| WSL2 后端起不来 | `wsl.exe` 被安全策略拦截 | 安全中心移出黑名单，或改用 Hyper-V 后端 |
| 拉取镜像超时 | 访问不到 Docker Hub | 配 `registry-mirrors`（脚本会提示具体位置） |
| 一直 502 | 还在初始化 | 首次 5~10 分钟属正常，看 logs 确认在推进 |
| 反复重启 | 内存不足 | Docker Desktop 给到 8GB，或调低 puma worker |
| 502 但 logs 无错 | 端口冲突 | 换 `HostPort`，检查 8929/2222 是否被占 |
| 忘记 root 密码 | — | `docker exec -it gitlab-ce gitlab-rake "gitlab:password:reset[root]"` |
| `verify_browser.py` 报 `Invalid login or password` | 在浏览器里改过 root 密码，`.env` 没同步 | 见下方「密码与 .env 失同步」 |
| 冒烟阶段一条用例都没跑就退出码 1 | 结果目录清理被终端的安全策略拦截 | 加 `-KeepResults`，或直接在自己的终端里跑（见下方说明） |
| `-ContainerState` / `file not found` 之类脚本内部报错 | 脚本版本过旧 | 重新取用当前版本的 `deploy.ps1` |

## 密码与 .env 失同步

GitLab 在首次登录后会**强制要求修改初始密码**。改完之后，`gitlab-qa/.env` 里的
`GITLAB_ROOT_PASSWORD` 就成了旧值，表现为 `verify_browser.py` 报
`Invalid login or password`，而账号本身没被锁、也没过期——很有迷惑性。

**注意 PAT 不受影响**：Token 与密码是两套凭据，改密码不会让接口测试失效。
所以症状是"接口测试全绿，只有 UI 登录失败"，这本身就是定位线索。

修复（PowerShell / bash 通用，`<新密码>` 换成你的）：

```bash
docker exec gitlab-ce gitlab-rails runner "u=User.find_by_username('root');u.password='<新密码>';u.password_confirmation='<新密码>';u.password_automatically_set=false;u.password_expires_at=nil;u.save!"
```

然后把**同一个**密码写进 `gitlab-qa/.env` 的 `GITLAB_ROOT_PASSWORD`（`.env` 必须无 BOM）。

命令里 `password_automatically_set=false` 和 `password_expires_at=nil` 是为了
避免下次登录又被要求改一次，否则同样的问题会反复出现。

生产建议：不要手工改 `.env`，把密码放进密码管理器，`.env` 只作为本地开发的临时载体。

## 卸载

```powershell
docker compose down        # 停容器，保留数据卷
docker compose down -v     # 连数据一起删（不可恢复）
```

## 备份

```powershell
docker exec -t gitlab-ce gitlab-backup create
docker cp gitlab-ce:/var/opt/gitlab/backups ./backups
```

备份里**不含** `gitlab.rb` 与 `gitlab-secrets.json`，后者丢了会让所有加密数据无法解密，
单独拷出来保管。

## 实测记录

本机（Windows 11 + Docker Desktop 29.6.1，Linux 后端，18 核 / 15.3GB 给容器，
宿主 32GB）完整跑通一次，脚本退出码 0：

| 阶段 | 耗时 | 结果 |
|---|---|---|
| 前置检查 | 1.8s | 空闲内存 4.4GB / 磁盘 83.5GB |
| Docker 环境 | 2.8s | server 29.6.1 · compose v2 |
| 镜像 | 0.3s | 本地已有 `gitlab/gitlab-ce:latest`（1.22GB） |
| 启动容器 | 0.3s | `gitlab-ce` |
| 等待就绪 | 1.3s | sign_in=200、api=401（GitLab 19 鉴权策略） |
| 签发 PAT | 0.3s | `gqa-automation` · api + read/write_repository |
| 验收冒烟 | 35.5s | smoke 套件 **20 passed, 1 skipped** + Allure 报告 |

那条 skip 是 GitLab 19.0.1 的 `/api/v4/version` 需要鉴权，属于预期策略，
用例里刻意把它判成 skip 而不是失败。

期间踩到并已在脚本里修掉的问题，留个档：

1. **GitLab 19 移除了 `grafana['enable']`**。写了它，reconfigure 会以
   `UnknownConfigOptionError` 中止，容器无限重启。compose 里已删掉这一行。
2. **GitLab 19 的健康端点变了**：`/-/health`、`/-/readiness` 都返回 404。
   就绪判定改成 `sign_in 返回 200` + `API 返回 401/200`。
3. **初始密码会被密码策略静默拒绝**（"常见词组合"，例如 `GitlabQA@2026`），
   结果是 root 账号没建出来、用户表为空，但 reconfigure 仍报成功。
   排查方法是查 `User.count`；修法是换一个不落常见词的密码。
4. **`.env` 里的 12 个键全部"缺失"**：`@(...)` 拼接在这个写法下被当成单个元素。
   换成 here-string literal 数组后正常。
5. **本机装了 `pytest-base-url` / `pytest-playwright`**，`--base-url` 与 `page`
   固件会和框架抢注册，pytest 直接报 `conflicting option string`。
   `tools/run.py` 里已固定加 `-p no:base_url -p no:playwright`。
