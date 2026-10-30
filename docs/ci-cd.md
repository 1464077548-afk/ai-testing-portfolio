# CI/CD 设计：一条流水线，四种决策

> 目标不是"让流水线变绿"，而是让每一盏灯都能回答一个具体问题：
> 这次提交能不能合？合进去之后主干还活着吗？系统有没有变慢、变脆、变危险？

## 1. 为什么流水线在仓库根

GitLab 只读仓库根的 `.gitlab-ci.yml`。这是 monorepo —— `gitlab-qa/` 是目录，不是独立仓库，
把配置写进子目录 = 永远不会被触发。因此所有 job 统一 `cd gitlab-qa` 再执行，
`gitlab-qa/.gitlab-ci.yml` 仅作为"拆成独立仓库时"的参考。

## 2. 四档触发源

| 触发源 | 什么时候 | 跑什么 | 回答什么问题 | 耗时量级 |
|---|---|---|---|---|
| `push`（每次提交） | 开发推分支 / 合入主干 | `unit` `quarantine` `smoke` | 框架没坏？环境活着？ | 1–2 min |
| `merge_request_event` | 开 MR / MR 有新提交 | 上面全部 + `regression:api` `regression:ui` `regression:e2e` | 合进去会不会弄坏主干？ | 6–10 min |
| `schedule`（夜间 02:00） | 每天 | 全量 + `perf` `security` `flake` | 系统有没有退化、变慢、变脆？ | 10–15 min |
| `web`（手动） | 人点按钮 | 全量；`perf` / `security` 默认 manual | 发布前复测 | 按需 |

**性能与安全刻意不进 PR**：CI 机器上的性能数据没有可比性，安全用例会写数据且需要人判读。
把它们放进门禁，唯一的结果是所有人学会无视红灯。

## 3. 门禁怎么真正"挡住"合并

三层，缺一不可：

1. **保护分支**：`main` 受保护（push 仅 Maintainer、merge 仅 Maintainer，
   禁止 force push）。日常改动走 MR；保护分支的价值是挡住误推与强推，
   **不是**门禁本身 —— GitLab CE 的 API 不接受"push 权限 = No one"
   （实测 `allowed_to_push: [{access_level: 0}]` 会被忽略、落回 Maintainers），
   这一点如实记录，不假装有更严的限制。
2. **Pipelines must succeed**：项目设置 `only_allow_merge_if_pipeline_succeeds`。
   MR 的头流水线没绿，Merge 按钮就是灰的；用 API 直接 merge 会返回 405。
   这是免费版真正的硬门禁。
3. **MR 流水线包含回归**：`regression:api` / `regression:ui` / `regression:e2e` 三个 job
   只在 MR 与主干上出现。UI 与 E2E 拆成独立 job —— 失败时能一眼定位到层，
   且不会因为其中一个崩了把另一层的证据一起丢掉。

> 注：GitLab CE（Free）不提供审批规则（Approval Rules，那是 Premium 功能）。
> 免费版能做的"人审"是 **Pipelines must succeed + 保护分支**，
> 这里如实使用免费版能力，不假装有审批流。

## 4. Job 一览

| Job | stage | 规则 | 产物 |
|---|---|---|---|
| `unit` | check | 所有流水线 | allure-results |
| `quarantine` | check | 所有流水线 | —（隔离区体检，缺原因/缺期限/已到期即红） |
| `smoke` | smoke | 所有流水线 | allure-results |
| `regression:api` | regression | MR / 主干 / 夜间 / 手动 | allure-results（`-n 4` 并行） |
| `regression:ui` | regression | MR / 主干 / 夜间 / 手动 | allure-results + 截图/trace |
| `regression:e2e` | regression | MR / 主干 / 夜间 / 手动 | allure-results |
| `perf` | perf | 夜间 / 手动 | allure-results（P95/错误率/抖动/吞吐门禁） |
| `security` | security | 夜间 / 手动 | allure-results（先 `ensure_member`，缺第二身份直接红） |
| `flake` | security | 夜间 | `artifacts/flake/`、`flake-report.json` |
| `report` | report | 所有流水线 | 合并后的 Allure HTML（`public/`） |

## 5. 需要的 CI/CD 变量

全部在 *Settings → CI/CD → Variables*，除 `TARGET_URL` 外均 Masked：

| 变量 | 作用 |
|---|---|
| `TARGET_URL` | 被测实例地址。runner 与被测实例同在 compose 网络时用 `http://gitlab-ce:8929` |
| `GITLAB_TOKEN` | 管理员 PAT：写操作、构造数据、越权断言的主身份 |
| `GITLAB_MEMBER_TOKEN` | 第二身份 PAT。**缺它越权断言会退化成假绿**，所以安全 job 先跑 `ensure_member.py` |
| `GITLAB_ROOT_PASSWORD` | UI 用例构造登录态 |
| `SECURITY_ALLOWED_HOSTS` | 安全用例的显式授权目标（信任边界守卫） |
| `PERF_*` | 性能阈值：并发、样本数、P95、环境基线预算 |

## 6. 运行器（runner）怎么接

被测实例和 runner 都在 Docker 里，关键是**名字要能互相解析**：

```bash
# runner 与被测实例同一个 compose 网络，注册时用容器名而不是 localhost
docker network create gitlab-ce_default   # 已由 docker compose 创建
docker run -d --name ai-portfolio-runner --restart unless-stopped \
  --network gitlab-ce_default \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v ai-runner-config:/etc/gitlab-runner \
  gitlab/gitlab-runner:latest

docker exec ai-portfolio-runner gitlab-runner register --non-interactive \
  --url http://gitlab-ce:8929 \
  --clone-url http://gitlab-ce:8929 \
  --token "$RUNNER_TOKEN" \
  --executor docker \
  --docker-image python:3.13-slim \
  --docker-network-mode gitlab-ce_default
```

两个坑：

- `--url` 写成 `localhost:8929` 时，**job 容器里的 localhost 是它自己**，clone 必然失败。
  容器里必须用服务名 `gitlab-ce`（或 `host.docker.internal`）。
- `--clone-url` 要和 `--url` 一致，否则 API 能连上、代码拉不下来。

## 7. 本地复现（不经过 CI）

```bash
cd gitlab-qa
python -m pytest tests/unit -q                       # 框架自检
python -m pytest tests/smoke tests/api/test_contract.py -q -m "not destructive"
python -m pytest tests/api -q -n 4 --dist loadscope  # 接口全量
python -m pytest tests/ui tests/e2e -q               # UI / 端到端
python -m pytest tests/perf -q                       # 性能（有阈值）
python -m pytest tests/security -q                   # 安全（白名单内）
python tools/flake.py --check-quarantine --verify-nodeids
```
