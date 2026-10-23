# api-lab

阶段一工程底座的练手仓库。**clone 下来就能跑，不需要 Docker、不需要联网、不需要任何公网靶场。**

这不是一份教程，是一套「可以弄坏、可以改、可以拿去面试讲」的测试工程。79 条用例全部基于一个内置的本地 REST 靶机，覆盖 pytest 工程化、接口测试核心能力、CI 门禁三块内容。

```bash
pip install -e ".[dev]"
pytest -q
# 79 passed, 2 rerun in 2.8s
```

---

## 目录

```
conftest.py                    根层：命令行、靶机、环境守卫、报告定制
pyproject.toml                 pytest 配置、marker 注册、依赖
src/apilab/
  fake_server.py               本地 REST 靶机（标准库，鉴权/CRUD/分页/间歇 5xx/字段漂移）
  client.py                    被测系统客户端：令牌续期、重试、错误归一化
  models.py                    pydantic 响应模型（extra=forbid）
  contracts.py                 JSON Schema 契约（客户端视角）
  paging.py                    分页纯函数，给单元测试用
tests/
  conftest.py                 业务层：api / admin_api / anon / book_factory / 数据驱动
  unit/test_paging.py         纯函数，毫秒级
  api/
    conftest.py               子目录层（当前为空，说明「用到再提层」）
    test_auth.py              令牌状态机：登录→过期→续期→吊销
    test_books_crud.py        CRUD 与数据依赖
    test_pagination.py        分页边界与「不重不漏」
    test_authz.py             401 与 403 的分界
    test_parametrize.py       三种参数化用法
    test_scope_demo.py        fixture 作用域的可观测证据
    test_flaky.py             重试能救什么、不能救什么
    test_contract.py          本地 schema + pydantic 双重校验、漂移检出
  contract/test_openapi_contract.py   以 /openapi.json 为源的契约测试
.github/workflows/ci.yml      三档流水线
```

---

## 一、pytest 工程化

### fixture 作用域：怎么选，不是背的

| 作用域 | 用在哪 | 本仓库实例 | 判断标准 |
|---|---|---|---|
| session | 建连接池、登录一次 | `lab_server`、`api` | 构造贵、无状态污染 |
| module | 本文件共用的只读数据 | `spec`（OpenAPI 文档） | 解析一次够用 |
| function | 会写库、要干净状态 | `book_factory`、`clean_library` | 用例会改数据 |

`tests/api/test_scope_demo.py` 用**构造计数器**把它们跑给你看。为什么不用 `id()`：CPython 会复用刚释放的内存地址，`id` 相同不代表同一个对象，拿它做断言会写出时红时绿的假证据。

### conftest 分层

```
conftest.py            环境、靶机、策略        ← 换项目还能用
tests/conftest.py      业务 fixture            ← 换项目就要改
tests/api/conftest.py  子目录专属前置条件      ← 只有这里需要
```

判断一行代码放哪，只问一句：**换个被测系统，这行还成立吗？** 成立往上，不成立往下。

同名的 fixture 子层覆盖父层，所以「局部特化、全局兜底」是天然成立的，不需要额外机制。

### 参数化的三种用法

1. **直接参数化** —— 同一逻辑不同输入（`test_create_validation_matrix`）
2. **indirect** —— 参数喂给 fixture 改前置条件，不是改入参（`test_refresh_happens_only_when_needed`，用 ttl 控制令牌是否已过期）
3. **数据驱动** —— 用例写在 `tests/data/books_create.json`，由 `pytest_generate_tests` 注入。产品同学也能加用例，不用碰代码。

### marker 分组

```bash
pytest -m "smoke and not destructive"    # PR 门禁，9 条，0.9 秒
pytest -m contract                       # 契约层，11 条
pytest -m "not slow"                     # 主干全量
pytest -m scope                          # 作用域教学
```

`--strict-markers` 打开后，写错 marker 名会直接报错。不打这个开关，「标签打了但没生效」可以隐藏很久。

### 插件配置

```bash
pytest -n auto --dist loadscope          # 并行：同模块同 worker
pytest --reruns 2 --reruns-delay 1       # 重试
pytest --html=out/report.html --self-contained-html
```

两个容易踩的：

- **`--dist loadscope` 而不是默认的 `load`**：同一模块的用例落在同一 worker，模块级 fixture 和共享状态才成立。
- **`--reruns` 绝不能写进全局 addopts**。`test_permanent_failure_is_not_masked` 就是在演示：真实的 5xx 缺陷会被同一次运行内的重试吃掉，CI 从此永远绿。

---

## 二、接口测试的「真本事」

### 令牌流转当状态机测

`test_auth.py` 覆盖了一条完整链路：

```
登录 → 拿到 access + refresh
  ↓ access 过期（ttl=-1 签一个出生即过期的令牌）
401 TOKEN_EXPIRED → 客户端自动 refresh → 重试原请求 → 201
  ↓ refresh 是一次性的
用旧 refresh 再换一次 → 401 INVALID_REFRESH_TOKEN（防重放）
  ↓ logout
access 立刻失效 → 401 INVALID_TOKEN
```

服务端把 `TOKEN_EXPIRED` 和 `INVALID_TOKEN` 分成两个 code，客户端才知道该续期还是该重新登录。只返回一个 `401 Unauthorized` 的接口，前端只能一律踢回登录页——这是设计缺陷，不是客户端的问题。

### 数据依赖与清理

`book_factory` 是 yield 工厂：

```python
book = book_factory(title="某书")      # 创建
...                                     # 用例逻辑
# 用例结束自动删除
```

清理必须容错（`204 or 404`）。如果清理也 assert 状态码，会出现「因为清理失败而失败」的红，这是最误导人的一种。

**不要依赖库里预置的数据。** 所有需要精确计数的用例（分页）用 `clean_library` 显式重置，而不是赌「上次跑完留下的状态刚好对」。

### schema 校验要两层

| 层 | 来源 | 作用 |
|---|---|---|
| `models.py` | 本地手写 pydantic | 表达「我期望的数据」 |
| `contracts.py` | 手写 JSON Schema | 表达「服务端承诺的结构」 |
| `/openapi.json` | 服务端发布 | 表达「服务端实际发布的契约」 |

两层不是重复。`--schema-drift` 会把 `published_year` 从 int 改成 str，实测结果：

```
FAILED test_list_response_matches_schema          # 本地 schema 拦住了
FAILED test_item_response_matches_schema          # 本地 schema 拦住了
FAILED test_pydantic_and_jsonschema_agree         # pydantic 放过了！
FAILED test_live_response_conforms_to_openapi     # OpenAPI 契约拦住了
```

**pydantic 默认是 lax 模式**，`"1991"` 会被自动转成 `1991`，校验通过。jsonschema 不会。这就是为什么要两层：pydantic 方便，但对类型漂移不敏感。

如果只用一个校验器，用 pydantic 会漏掉类型漂移，用 jsonschema 会丢掉字段级的业务约束。

### 契约测试要用两个独立来源

`tests/api/test_contract.py` 用本地 schema，`tests/contract/test_openapi_contract.py` 用服务端 `/openapi.json`。两个来源互相印证才叫契约测试；只维护一份，等于自己跟自己对账。

`test_local_contract_covers_all_documented_fields` 专门断言「本地 schema 不能比服务端承诺少字段」——少字段意味着上游加了字段而你不知道，等到下游解析才爆。

### 401 和 403 的分界

```
401 = 我不知道你是谁       → 缺令牌、令牌无效、令牌过期
403 = 我知道你是谁但不能做  → 已认证，权限不足
```

`test_cannot_delete_others_book` 还多断言了一句：**越权请求不得产生副作用**（403 之后书还得在）。只看状态码不看结果，会漏掉「返回 403 但已经删了」这种最危险的实现。

---

## 三、CI/CD 落地

三档流水线，节奏不同：

| 档位 | 触发 | 范围 | 目标 |
|---|---|---|---|
| smoke | PR | `-m "smoke and not destructive"` + 并行 + 重试 | 60 秒内给出能否合并 |
| full | push main | `-m "not slow"`，Python 3.11/3.12 矩阵 | 合并后的全量回归 |
| nightly | 定时 | 全量含 slow + 契约漂移演练 | 长周期问题与契约健康度 |

三个设计决策：

1. **PR 阶段不跑 destructive**。写操作的用例在 PR 上跑，一旦有并发提交就会互相踩数据。
2. **`--env prod` 是硬守卫**。`pytest_collection_modifyitems` 在收集阶段就给 destructive 打 skip，不依赖人记得加参数。
3. **报告用 `--self-contained-html`**。单文件、无外链，下载下来直接双击能看，不用起服务。CI artifact 的默认保留期只有 7 天，重要的跑一次就归档。

---

## 五个刻意埋下的坑

这些都是真实项目里踩过的，故意留在靶机里让你遇到：

1. **401 时不读请求体 → 连接复用后下一个请求被残留字节顶掉**。表现为「刷新令牌莫名失败」，实际是 HTTP/1.1 长连接的问题。`fake_server._dispatch` 里有一行注释专门讲它。
2. **分页 `size` 回显 `len(items)`**。最后一页只有 5 条时回 `size=5`，前端分页器算出的总页数就错了。`test_page_count_is_ceiling[size=50]` 就是抓这个的。
3. **`state.reset()` 顺手把 `schema_drift` 关掉**。表现为「明明开了漂移，契约测试全绿」。配置和业务数据必须分开重置。
4. **用 uuid 做 flaky 用例的 key**。重试会重新执行函数体，每次都是新 key，故障永远停在「第一次」，测试稳定失败且看不出原因。要用 `request.node.nodeid`。
5. **`--reruns` 写进全局 addopts**。真 bug 被重试吃掉，CI 永远绿。

---

## 接真实系统

```bash
# 指向任意实现了同样接口的服务
pytest --base-url http://localhost:8000 --env staging

# 只跑冒烟
pytest -m smoke --base-url https://staging.example.com --env staging
```

接真实靶场时，`clean_library` 会自动 skip 而不是假装成功——`lab_server is None` 时没有重置能力，硬跑只会得到一堆假失败。

想换成 httpbin / Restful-booker / DummyJSON，改 `client.py` 的路径即可，测试用例的断言逻辑不用动。这也是分层的收益：**换靶子只改适配层**。

---

## 面试可讲的三句话

1. 我的 conftest 分三层：环境层换项目能复用，业务层会改，子目录层只在特定场景需要。判断标准是「换个被测系统这行还成立吗」。
2. 令牌我按状态机测，不只测「能不能登」：过期时客户端必须透明续期一次，refresh 必须一次性，logout 后 access 必须立刻失效。
3. 契约我用两个独立来源互相印证：本地 schema 和服务的 OpenAPI。实测把 int 改成 str，pydantic 的 lax 模式会放过，jsonschema 和 OpenAPI 契约能拦住。

---

## 已知边界

- 靶机是内存态，进程退出数据就没了；没有持久化，也不模拟事务。
- 没有并发写冲突的用例（需要真正的数据库隔离级别才能测）。
- 性能只验证了「超时会抛 Timeout」，没做压测。
- CI 里的契约漂移演练用 `continue-on-error`，是演示性质，不要照搬到真实门禁。
