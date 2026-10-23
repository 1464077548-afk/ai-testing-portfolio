# gitlab-qa

面向 **GitLab CE** 的企业级自动化测试框架。pytest 驱动，接口走 `requests`、UI 走 `Playwright`，
产出 Allure 报告，已接入 GitHub Actions 与 GitLab CI 双流水线。

```bash
pip install -e ".[dev]"
cp .env.example .env          # 填入 GITLAB_BASE_URL / GITLAB_TOKEN

python tools/doctor.py         # 30 秒环境自检，先别急着跑
python tools/run.py --suite smoke --base-url https://gitlab.com --open
```

---

## 1. 为什么这样分层

测试分层不是把用例丢进不同目录，而是**每一层回答一个不同的问题**：

| 层 | 目录 | 回答的问题 | 失败时的含义 | 门禁位置 |
|---|---|---|---|---|
| **自检** | `tests/unit` | 框架本身坏了吗 | 测试代码的问题，不是产品问题 | 所有流水线第一步 |
| **冒烟** | `tests/smoke` | 这个环境还活着吗 | 环境问题，先修环境再谈测试 | PR |
| **接口** | `tests/api` | 单个接口行为对不对 | 定位到具体接口 | 主干 |
| **契约** | `tests/api/test_contract.py` | 响应结构和承诺一致吗 | 上游改字段了 | PR（只读） |
| **端到端** | `tests/e2e` | 跨系统能串起来吗 | 业务链路断了 | 主干 |
| **UI** | `tests/ui` | 用户能真的操作吗 | 前端或联调问题 | 主干 |
| **性能** | `tests/perf` | 快不快、稳不稳 | 容量/退化问题 | 定时或手动 |
| **安全** | `tests/security` | 权限、注入、泄露、暴露面守住了吗 | 配置回退或权限模型破损 | 定时 + 发版前 |

**性能不进 PR**，这是刻意的。CI 机器的性能数据波动大，放进门禁只会制造噪音和误判。
**安全也不进 PR**：会写数据、耗时 30s+，且红了经常需要人工判断（比如"公开注册被打开了"）。

## 2. 目录结构

```
src/gqa/
  settings.py       配置加载：环境变量 > .env > 默认值
  models.py         pydantic 模型：本地对响应的期望
  contracts.py      jsonschema：服务端对外承诺的契约
  api/client.py     HTTP 客户端：重试、日志、错误分类
  api/resources.py  资源门面：projects / issues / users 语义化封装
  data/factory.py   数据工厂：创建即登记，用例结束自动回收
  perf/runner.py    并发采样：P95/P99、错误率、Excel 明细导出
  security/         安全测试支撑：guard 白名单守卫 / scanner 扫描器 / raw 原始请求
  flake/            稳定性治理：model 四类判定 / quarantine 隔离区名单
  ui/browser.py     Playwright 会话：trace、截图、失败留证
  ui/pages/         页面对象：login / project / issue
  utils/wait.py     轮询等待：应对异步一致性
tests/
  unit/     smoke/    api/     e2e/    ui/    perf/    security/
tools/
  run.py             一键运行：跑测试 → 生成报告 → 打开浏览器
  doctor.py          环境自检 + 自建实例签发 PAT
  flake.py           稳定性采样 + 隔离区体检（flake 治理）
  cleanup.py         产物清理：日志 / 截图 / trace / 旧报告（时间+体积+数量三重策略）
  schedule_cleanup.ps1  把清理注册成 Windows 每日计划任务（时间可配置）
  ensure_member.py   创建第二个身份（普通用户）供越权断言使用
flake/
  quarantine.json    隔离区名单：**不是垃圾桶**，每条都要有原因和期限
allure/
  categories.json   缺陷归类规则（决定报告里「Categories」页长什么样）
```

## 3. 每日使用

```bash
# 零依赖跑全量：内置靶机 + 跑测试 + 出报告（不需要 Docker / 真实实例 / Token）
python tools/run.py --suite all --mock

# 验证性能门禁真的会拦：给靶机注入 1.5s 延迟
python tools/run.py --suite perf --mock --mock-latency-ms 1500

# 验证错误率门禁：注入 30% 的 503
python tools/run.py --suite perf --mock --mock-error-rate 0.3

# 自检：不通网络，验证框架没被改坏
python tools/run.py --suite unit

# 冒烟：只跑只读用例，无 Token 也能过
python tools/run.py --suite smoke --base-url http://localhost:8929

# 接口全量（需要 Token）
python tools/run.py --suite api --env staging

# 生产环境：写操作用例在收集阶段就被 skip，不靠人记得加参数
python tools/run.py --suite api --env prod

# 单独跑某类
python tools/run.py --suite api --mark "contract"
python tools/run.py --suite api -k "login or token"

# 性能（只在需要时跑）
PERF_CONCURRENCY=16 PERF_SAMPLES=100 python tools/run.py --suite perf

# 安全（只对白名单内的自有实例执行；缺第二身份先跑 ensure_member.py）
python tools/ensure_member.py
python tools/run.py --suite security
```

`tools/run.py` 套件一览：`unit` `smoke` `api` `contract` `ui` `e2e` `perf` `security` `regression` `all`

### 安全套件（`tests/security/`）

四类共 32 条，覆盖越权 / 注入转义 / 信息泄露 / 暴露面。三条硬规矩：

1. **信任边界写进代码**：`src/gqa/security/guard.py` 会检查目标 host 是否在白名单
   （本机地址 + `GITLAB_BASE_URL` 自身 + `SECURITY_ALLOWED_HOSTS`），不在就 skip。
   自动化安全测试只能打自己拥有的实例，"记得别配错"不算防护。
2. **越权必须双身份**：`tools/ensure_member.py` 幂等地创建普通用户并签发
   impersonation token 写回 `.env`。用它之前先跑一条"对照组"用例验证第二身份
   可用且非管理员——否则 Token 一过期，全部越权用例会拿着 401 照样绿。
3. **扫描器分黑白名单**：`scanner.py` 只扫键名不扫值（扫值误报太多，会让人关掉套件）；
   白名单和黑名单一样重要——`ci_id_token_sub_claim_components` 这类配置项名字带
   token 但不是凭据，不放行就会天天假红。

实测记录（GitLab CE 19.0.1，32 passed / 0 skipped）：

- `signup_enabled` 当时是开的（真实暴露面），已关闭并用例固定
- 路径穿越必须用 `raw.py` 的原始请求发：`requests` 会把 `../` 归一化掉，
  恶意路径根本到不了服务端，用例"通过"只是因为载荷没发出去
- 管理员读 `/projects` 列表会带出每个项目的 `runners_token`（权限模型内行为），
  泄露判定要看**非成员视角**能不能拿到

### 内置靶机（`tools/mock_gitlab.py`）

`--mock` 会启动一个 GitLab 形状的本地服务，覆盖 API 与最小 HTML 界面，
用来解决三个真实约束：

| 约束 | 靶机怎么解决 |
|---|---|
| 公共实例不能压测（那是攻击别人） | 本地随便压，P95/错误率都能验 |
| 自建 GitLab CE 要 8GB 内存 + 10 分钟启动 | 1 秒起来，每次都是干净数据 |
| 写操作用例会留垃圾数据 | 重启即重置，可反复跑 |

它能注入故障，这是关键——**门禁的价值在于会拦，不在于会放行**：

```bash
python tools/mockrun.py --suite perf --latency-ms 1500   # P95 1526ms 超过阈值 800ms
python tools/mockrun.py --suite perf --error-rate 0.3    # 错误率越界
```

`tools/mockrun.py` 是轻量版（只跑测试不出报告），适合边改边验。

> 靶机的 HTML 界面只提供页面对象需要的最小 DOM，**不等于 GitLab 的真实界面**。
> 对真实实例校验选择器必须用自建部署，原因见第 9 节。

### UI 的浏览器通道

`UISession.start()` 按 `msedge` → `chrome` → `chromium` 顺序尝试，用第一个能起来的。
这不是偷懒，是企业内网的必要条件：Playwright 的浏览器 CDN 经常不通
（本机实测 `playwright install chromium` 下载失败），而 Windows 自带 Edge、
多数开发机装了 Chrome，都是 Chromium 内核，Playwright 可直驱，无需下载。

失败时会把三个通道的错误一起抛出，而不是只说一句"浏览器不可用"。

## 4. 三个设计决策

**一、缺配置就 skip，绝不 pass。**
没有 Token 时，`requires_token` 的用例全部 skip 并在报告里标灰。
让它们"通过"是最坏结果——报告上一片绿，实际一条需要鉴权的用例都没执行。

**二、生产环境的守卫放在收集阶段。**
`--env prod` 时 `destructive` 用例在 `pytest_collection_modifyitems` 里就被打上 skip，
而不是依赖用例内部判断。守卫如果不自动化，等于没有守卫。

**三、失败必须留证据。**
API 用例失败 → 自动贴上本次会话完整请求/响应日志；
UI 用例失败 → 自动贴截图 + DOM 快照 + Playwright trace。
报告里点开就能看到现场，不用重跑一遍去"看看当时发生了什么"。

## 5. 数据清理

```python
def test_something(gl, data_factory):
    project = data_factory.project()      # 创建即登记
    issue = data_factory.issue(project["id"])
    ...                                    # 用例结束自动删除
```

数据工厂是 **function 级**的，session 级会留下跨用例污染——
"上一条造的数据影响了下一条"是接口测试最常见的假失败来源。
清理记录会作为附件贴进 Allure 报告，删了什么、删成功几条，一目了然。

### 运行产物清理（`tools/cleanup.py`）

上面清的是**实例里的测试数据**，这里清的是**磁盘上的运行产物**：
`artifacts/`（截图 / DOM / trace.zip）、`allure-results/`、`allure-report/`、`gitlab-ce/logs/`。

三条保留规则**同时生效，取最保守**：

| 规则 | 默认 | .env 键 |
|---|---|---|
| 按时间：只删 N 天前的 | 7 天 | `CLEANUP_RETENTION_DAYS` |
| 按体积：单目录超限时从最旧删到阈值内 | 1024 MB | `CLEANUP_MAX_MB` |
| 按数量：每目录至少保留最新 K 个 | 3 | `CLEANUP_KEEP_LATEST` |

```bash
python tools/cleanup.py --dry-run        # 先预览：会删什么、释放多少
python tools/cleanup.py                  # 按 .env 策略执行
python tools/cleanup.py --targets logs --days 3   # 临时覆盖
python tools/cleanup.py --all            # 忽略保留策略（history 仍保护）

# Windows：注册每日计划任务（时间可配置，策略写回 .env）
powershell -ExecutionPolicy Bypass -File tools\schedule_cleanup.ps1 -Time 03:00 -Days 7

# Linux / macOS：打印 crontab 行
python tools/cleanup.py --print-cron
```

三个必须知道的实现细节：

- **`history/` 永远不删**（results 和 report 里各有一份）——趋势图就靠它跨次累积；
- **逐文件删而不是 `rm -rf`**：公司终端 / DLP 会拦批量删除，甚至把进程干掉，
  表现为"脚本跑了但什么都没删"。逐个 unlink、失败只记录，最后有失败就返回非 0；
- 删除前校验路径必须落在目标目录内——防软链接把删除带到项目外。

## 6. Allure 报告

报告里能看到：Epic/Feature/Story 三级结构、严重度分布、缺陷归类（Categories）、
环境信息（Environment）、趋势（History，需保留 `allure-history`）、
以及每条失败用例的请求日志或截图。

缺陷归类规则在 `allure/categories.json`，按失败信息正则匹配：

| 类别 | 判定 |
|---|---|
| 被测系统不可用 | 连接超时 / 502 / 503 / refused（**排在最前，环境问题的归因优先于产品缺陷**） |
| 产品缺陷 | 断言失败 |
| 契约违约 | schema 字段或类型不匹配 |
| 鉴权权限 | 401 / 403 |
| 测试代码问题 | fixture 缺失 / TypeError 等 |

这个区分很有用：一片红的时候，先看 Categories 就知道该找开发还是找运维。

### 报告界面默认中文

`tools/run.py` 在生成报告后会自动调 `tools/localize_report.py` 把界面切成中文
（Allure 2.x 自带 zh 语言包，但 index.html 里写死 `lang="en"`）。
原理与边界：

- Allure 的默认语种取自 `<html lang>`，手动切换过的语言存在
  localStorage 的 `ALLURE_REPORT_SETTINGS.language`，优先级更高；
- 所以汉化脚本只改默认语种 + 在你**从未手动选过**语言时预置一次中文，
  之后你在界面右下角切回英文，刷新不会被强行改回去；
- `allure generate` 每次都会重写 index.html，因此这步必须在生成**之后**执行，
  CI 里（`.gitlab-ci.yml` / `ci.yml`）也是放在 generate 后面单独跑一步。

手动控制：

```bash
python tools/localize_report.py                    # 当前报告切中文
python tools/localize_report.py --locale en       # 还原英文
python tools/run.py --suite smoke --locale ""     # 本次运行不汉化
```

### 四个踩过的坑（共同点：结果看起来对，实际错）

**一、正则必须带 `(?s)`。**
Allure 用 Java 的 `String.matches()` 做**全串**匹配，而 `.` 默认不跨行。
失败信息是带换行堆栈的，写 `.*502.*` 匹配不上，所有失败都会落进默认的
「Test defects」——看起来归了类，其实一个都没归。加 `(?s)` 开启 DOTALL 才有效。

**二、`environment.properties` 不能用 UTF-8 写。**
Allure 按 Java properties 规范（ISO-8859-1）读这个文件，直接写中文会得到
`æªéç½®` 这种乱码，而且**不报错**，只有肉眼打开报告首页才发现。
框架里统一走 `gqa/allure_meta.py` 的 `write_properties()`，非 ASCII 自动转成
`\uXXXX` 转义。这条行为由 `tests/unit/test_framework.py::TestAllureEnvEncoding` 锁住。

**三、请求体不能读两次。**
靶机在「校验类 4xx」上曾经偶发 20 秒超时，原因很隐蔽：为了不让 keep-alive
连接残留字节，错误响应前会先 `read(Content-Length)`；但如果业务代码已经读过
请求体，第二次 `read` 会一直阻塞到超时。
**不读会残留，读两次会卡死**——两个方向都得处理，用幂等标记收口。

**四、`--env prod` 拦不住没打标记的写操作。**
UI 的「错误密码登录」在 `--env prod` 下仍然会对公网实例提交表单，
因为它没有 `destructive` 标记。守卫只认标记，不认"看起来像读操作"。
这类用例必须显式标注，否则守卫形同虚设。

## 7. flake 治理 · 让"绿"这件事可以被相信

自动化最大的信任危机不是用例少，是**大家不相信**。一轮跑绿说明不了什么，
能重复 N 轮都绿才叫稳定。`tools/flake.py` 就是干这件事的。

### 四类结论，不是两类

```
STABLE    N 轮全绿        —— 可信
BROKEN    N 轮全红        —— 这是缺陷。**不是抖动**，别往隔离区丢
FLAKY     有绿有红        —— 真抖动，抖动率 = 红 /（绿 + 红）
NO_DATA   N 轮全是 skip   —— 没测量到，把它算成"稳定"是最危险的假象
```

最后一类最容易被漏：缺 Token 而 skip 的用例，在报告里和通过长得一模一样。
区分不了这两件事，稳定性度量就是自欺欺人。

### 用法

```bash
# 重复 5 轮采样。轮次是有代价的选择：5 轮能把 20% 的抖动抓出来，
# 想证伪 1% 的抖动要上百轮，成本远高于收益——默认值按"抓主要矛盾"取 5
python tools/flake.py --suite smoke --rounds 5

# 门禁：单条抖动率超过 5% 就退出非 0
python tools/flake.py --suite api --rounds 7 --max-flake-rate 0.05

# 隔离区体检（CI 每次提交都跑）
python tools/flake.py --check-quarantine --verify-nodeids

# 把一个确认抖动的用例限期关进隔离区
python tools/flake.py --quarantine-add "tests/ui/test_login.py::test_x" \
    --reason "首登改密弹窗与选择器竞态" --owner hejia --due 2026-11-06
```

产出：`artifacts/flake/latest.md`（人读）+ `round-N/junit.xml`（机器读）+ 可选 `--json`。

退出码：`2` 持续失败 · `3` 抖动超阈值 · `4` 隔离区体检不过 · `5` 没收集到用例。

### 隔离区是收费的

进来容易，赖着不行：

- 每条必须有 `reason` 和 `due`，缺一样 CI 就红；
- **到期自动失效**，用例回到门禁里继续跑——不可以自动续期；
- `--include-quarantine` 可以强制连它们一起跑，验收修复是否真的生效；
- 名单规模超过 8 条报警：先消化存量，否则"临时豁免"会变成常态；
- `--verify-nodeids` 会核对名单里的用例是否还存在（改名/删除后记录会过期）。

### 重试必须上报

框架**不做全局失败重跑**——偶现失败是信号，把它重跑成绿等于消灭信号。
只有显式打了 `@pytest.mark.flaky` 的用例才会重试，而且重试次数会：

1. 作为「重试记录」附件贴进 Allure；
2. 追加写入 `artifacts/flake/reruns.jsonl`（累积留痕，看得出"这条本周靠重试混过 6 次"）；
3. 在 session 结束时打印 `[flake] N 条用例靠重试才通过`。

### 三个踩过的坑

**一、NODEID 必须对得上。**
JUnit XML 把模块写成包路径（`tests.api.test_auth`），把类塞进 `classname`，
两边拼回来的规则不一样。还原错了不会报错，只会表现为"加了隔离却照跑不误"。
`src/gqa/flake/model.py::nodeid_from_junit` 里做了针对处理，并由一轮全量核对验证。

**二、`N 轮 × Allure` 会把趋势搞废。**
采样要跑 N 轮，若照 `run.py` 那样写 `--alluredir`，同一批用例会被重复写进
主结果目录，Allure 会把一次采样当成 N 次运行。所以 `flake.py` 剥掉 `--alluredir`，
只留 junit XML——趋势数据只允许由真实运行产生。

**三、Windows 上 `os.environ[k] = ""` 等于删除该变量。**
跑靶机时要停用第二身份，写空串后 `load_dotenv(override=False)` 会发现
"变量不存在"，顺手把 `.env` 里的真实 Token 填回来。
必须用非空占位符（这里是 `<disabled-in-mock>`），与 `Settings` 既有的
「`<` 开头即未配置」约定一致。

## 8. CI/CD

两套配置并存，内容等价：

- `.github/workflows/ci.yml` —— 8 个 job，含真实 GitLab CE service
- `.gitlab-ci.yml` —— 4 个 stage，同样起 service 测真实例

共同约定：**分 job 并行跑，但最终合并成一份报告**。
给三份报告让人自己拼，等于没报告。

```
unit（秒级，不要被测系统）        quarantine（隔离区体检，每次提交）
  └─ smoke（只读，PR 门禁）
       ├─ api（真实实例，写操作）
       ├─ ui（Playwright）
       ├─ perf（仅定时/手动）
       └─ flake（夜间：重复 N 轮采样）
            └─ report（合并 → 生成 → 发布 Pages）
```

## 9. 已验证

### 对着内置靶机（全绿，含 UI 与 E2E）

```
tools/run.py --suite all --mock             80 passed, 8 skipped（2026-10-23 复测）
  ├─ unit                                    17
  ├─ smoke                                    4
  ├─ api（含写操作）                          ~25
  ├─ perf（P95 + 错误率门禁）                  3
  ├─ ui（Playwright / Edge 通道）              3
  ├─ e2e（API 建项目 → UI 搜到）                1
  └─ security                               ~20
8 条 skip 全部是「缺第二身份」（靶机没有普通用户），不是失败也不是通过
Allure 报告 141 个文件，趋势继承 5 条，环境信息中文正常
```

靶机侧补了两处保真度，否则安全层的两条用例会在靶机上假红：

- `/application/settings` 路由（管理员端点：非管理员 403、匿名 401）
- 全响应带 `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy`

**替身必须复刻它要替代的那层行为**，否则"在靶机上红"会被误读成"用例写错了"。

### 对着本地 GitLab CE 19.0.1 实例（2026-10-23 实测）

```
--suite security                            32 passed / 0 skipped
--suite all（4 worker 并行）                 85 passed, 1 skipped；perf 2 条
                                            因并行噪声 P95 超阈值（单独跑 3 passed）
清理预览 --dry-run                           删 69 个 / 70.7 MB（days=0 时），保留最新 3 个
```

安全套件当场抓到并处理的问题：

| 发现 | 处置 |
|---|---|
| `signup_enabled=true`（任何人可自助注册） | API 关闭并回读确认，用例固定住 |
| 管理员项目列表带 `runners_token` | 权限模型内行为；改用非成员视角断言泄露 |
| 路径穿越载荷被 `requests` 归一化 | 新增 `security/raw.py` 原始请求发送器 |

### 对着公网真实实例（只读层）

```
pytest tests/unit tests/smoke tests/api/test_contract.py tests/api/test_pagination.py \
       --base-url https://gitlab.com        25 passed, 2 skipped
```

### 门禁的"会拦"验证

```
--mock --mock-latency-ms 1500    P95 1526ms > 800ms   退出码 1
--mock --mock-error-rate 0.3     错误率 3.33% > 2%    退出码 1
--extra "-m '写错的marker'"      退出码 5 + 可执行的诊断命令
```

对 gitlab.com 实跑时抓到的真实行为，已经回写进用例：

- offset 分页有 **50000 条上限**，越界返回 **405** 而非空列表
- `per_page=101` 被**静默截断到 100**，不报 400
- **UI 层打不到公共实例**：`/users/sign_in` 返回 403，页面标题 `Just a moment...`
  —— Cloudflare 挑战页。UI 测试必须对自建实例跑

前两条如果按"惯例"写成"越界应返回空列表 / 超限应报 400"，就会得到两个假失败。
**契约测试的价值不在于验证它符合你的想象，在于记录它真实的样子。**

### flake 治理自验证（2026-10-23）

用一个临时编写的演示文件（跑完即删）把四类结论全验一遍，4 轮采样结果：

```
FFFF   100%   持续失败   ← 真缺陷，不是"100% 抖动"
FPFP    50%   抖动       ← 只有这一条才该进隔离区
SSSS     0%   无有效数据 ← 全程 skip，不能算稳定
```

配套的隔离区闭环 10 步全部实测通过：加入 → 跳过（reason 带期限/负责人/原因）→
`--include-quarantine` 恢复执行 → 到期后自动失效 → 体检报出「缺期限 / 已到期 /
用例不存在」并返回 4 → 移出 → 空名单体检通过。

重试上报也实测：打了 `@pytest.mark.flaky` 的用例重试 1 次后通过，
Allure 附件 + `artifacts/flake/reruns.jsonl` + session 汇总行三处都留下了记录；
同文件里没打标记的失败用例**一次都没被重试**（总共只发生 1 次 rerun）。

### 部署侧

GitLab CE 19.0.1 已由 `gitlab-ce/deploy.ps1` 在本机 Docker Desktop（WSL2 Linux 后端）
部署并长期运行：http://localhost:8929。`wsl.exe` 虽被本机安全策略拦截，
但不影响容器运行——Docker Desktop 本身就跑在 WSL2 后端。
完整踩坑记录见 `gitlab-ce/README.md`。

## 10. 已知限制

- `--issue-token` 依赖 `docker exec gitlab-rails runner`，只对容器部署的自建实例有效；
- UI 用例未针对 GitLab 各版本的 DOM 差异做兼容，换版本可能需要调定位器；
- 内置靶机的 HTML 只服务页面对象所需的最小 DOM，**不能替代对真实 GitLab 的 UI 校验**；
- 性能模块是自研采样器，不是 JMeter/k6 的替代品，适合做回归对比不适合做容量规划；
- 未接入缺陷平台（Jira/GitLab Issues）自动提单，失败后需人工跟进。
