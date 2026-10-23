# RAG 质量评测平台

对任意 RAG 问答系统做自动化质量评估：评测集版本化、多层级指标、基线回归对比、安全套件。
本仓库是 v0 骨架——**零依赖可跑通全链路**，用于先把"加载 → 采集 → 打分 → 报告 → 退出码"打通，再逐步替换为真实系统与语义指标。

## 现在能做什么

| 能力 | 状态 | 说明 |
|---|---|---|
| 评测集加载与强校验 | 完成 | 脏 case 在加载期报错，不进执行 |
| 分层指标（检索/生成/行为） | 完成（proxy） | 生成指标为 bigram 近似，待换 Ragas |
| 并发采集与失败隔离 | 完成 | 采集失败单独计数，不计入质量分 |
| 切片下钻报告 | 完成 | 单文件 HTML，可离线打开、可作 CI artifact |
| 退出码门禁 | 完成 | 0 通过 / 1 指标越界 / 2 配置或环境错误 |
| Dify 适配器 | 完成，28 项契约验收通过 | 引用片段映射到 hit@k / MRR，可直接算检索指标 |
| OpenAI 兼容适配器 | 完成 | 覆盖 Ollama / vLLM / DeepSeek，检索指标自动跳过 |
| 接入自检 doctor | 完成 | 鉴权、引用、doc_id 映射一次说清 |
| 基线 diff 与显著性 | 待做（Day 9） | `storage/baseline.py` |
| 真实 HTTP 适配 | 完成（通用逃生口） | `HttpRagTarget`，请求体与字段全走配置 |
| 安全套件 | 有用例，无专项指标 | `datasets/golden/adversarial.jsonl` |

## 快速开始

```bash
# 只校验评测集，不调用被测系统
python demo.py validate

# 健康靶子：应当全绿
python demo.py run

# 降级靶子：应当变红，用于验证"断言真的会拦"
python demo.py run --suite configs/suites/degraded.json --out out/degraded

# 适配层离线验收（本地假服务，不需要 Dify、不需要网络）
python tools/verify_adapters.py
```

报告输出到 `out/report.html`，原始结果同时落 `run-*.json`。

## 接真实 Dify

```bash
# 1. 应用「访问 API」页创建密钥
export DIFY_API_KEY="app-xxxxxxxx"     # PowerShell: $env:DIFY_API_KEY="app-xxxxxxxx"

# 2. 自检：先确认能连上、有引用、doc_id 对得上
python demo.py doctor --suite configs/suites/dify_smoke.json

# 3. 开跑
python demo.py run --suite configs/suites/dify_smoke.json
```

云端把 suite 里的 `target` 换成 `configs/targets/dify_cloud.json` 即可。
详细步骤、字段映射、六个常见坑见《Dify适配接入手册与阶段一练手清单》。

**必须做对的一件事**：`doc_id_field` 用 `document_name` 而不是 `document_id`。
后者是 UUID，知识库一重建就变，评测集无法与之对齐。

## 为什么有两个靶子

只跑全绿的 demo 说明不了任何问题。`mock_degraded.json` 按 case id 做稳定哈希，注入两类真实缺陷：

- **检索漏召回**（25%）：黄金片段从候选中被摘掉，系统拿相邻政策硬答 —— 结果是 `hit@3`、`answer_match` 掉，但 `faithfulness_proxy` 仍是 1.0。这正好演示了"忠实度高 ≠ 答案对"，也是分层归因的价值。
- **该拒答却编造**（34%）：`refusal_accuracy` 掉到 0.67，属于最高危缺陷。

因为哈希是稳定的，同一份配置每次运行结果一致，适合做回归对比。

## 目录

```
src/rag_eval/
  models.py      Case / Answer / MetricScore
  dataset.py     加载、校验、切片
  metrics.py     指标实现与聚合
  runner.py      并发采集、重试、结果组装
  doctor.py      接入自检：鉴权 / 引用 / doc_id 映射
  report.py      单文件 HTML 报告
  cli.py         run / validate / doctor
  targets/       被测系统适配层（唯一扩展点）
    base.py          Target 协议 + 字段映射
    mock.py          离线桩，含可控降级
    dify.py          Dify chat-messages
    openai_compat.py Ollama / vLLM / DeepSeek / FastGPT
    http_rag.py      任意 HTTP 接口的通用逃生口
    factory.py       配置 → 适配器
datasets/golden/ 评测集（core / refusal / adversarial）
configs/         靶子配置与套件配置
tools/           离线契约验收脚本
tests/           指标算法、数据集校验、适配器契约、端到端
```

## 已内置的适配器

| kind | 覆盖 | 引用片段 | 备注 |
|---|---|---|---|
| `mock` | 离线演示与 CI | 有 | 支持注入降级缺陷 |
| `dify` | Dify 自托管 / 云端 | 有 | 主靶子，含错误分类 |
| `openai_compat` | Ollama / vLLM / DeepSeek / 通义 | 无 | 只测生成与行为 |
| `http` | 自研或文档不全的接口 | 可配 | body_template + mapping |

`supports_citations=false` 的适配器会整体跳过检索指标，而不是记 0 分——
后者会把"接口本来就不给引用"误判成"检索很差"。

## 加一个被测系统

1. 在 `src/rag_eval/targets/` 新建实现 `Target` 协议（`ask` / `health` / `probe`）；
2. 在 `factory.py` 加一个分支；
3. 写 `configs/targets/<name>.json`；
4. 复制一份 suite 配置指向它；
5. 在 `tools/verify_adapters.py` 加一组假服务契约用例。

`runner`、`metrics`、`report` 一行都不用改。面试时这一条就是开闭原则的具体落地。

配置示例：

```json
{
  "kind": "dify",
  "name": "dify-prod",
  "version": "1.4.2",
  "base_url": "https://api.dify.ai/v1",
  "api_key_env": "DIFY_API_KEY",
  "response_mode": "blocking",
  "mapping": {
    "answer_path": "answer",
    "contexts_path": "metadata.retriever_resources",
    "doc_id_field": "document_name",
    "chunk_id_field": "segment_id",
    "text_field": "content",
    "score_field": "score",
    "doc_id_strip_suffix": true
  }
}
```

## 评测集规范（摘要）

- 一个 case 只考一个意图，否则失败时无法归因；
- `core` / `boundary` 必须给 `expected_passages`，否则检索指标无从计算；
- `refusal` 必须 `must_refuse: true`，且不给 `expected_answer`；
- 问题用真实用户口吻写，不要拿文档标题改写（会系统性高估召回）；
- 新增 case 走 PR，附一句"为什么需要这条"。

完整规范与指标阈值见配套文档《RAG 评测平台-开工蓝图》。

## 从 v0 到 v1

| 步骤 | 替换什么 | 验收 |
|---|---|---|
| Day 5 | 用 Ragas 的 `faithfulness` / `answer_relevancy` 替换 `generation.py` | 同一批准数据，两版指标相关性 > 0.7 |
| Day 8 | `judge.py`：LLM 裁判 + 50 条人工标注 + Cohen's kappa | kappa ≥ 0.6，否则重写 rubric |
| Day 9 | `storage/store.py` + `baseline.py` | 输出配对 bootstrap 95% CI 与三态判定 |
| Day 10 | 夜间任务接安全套件 | 输出按严重度分级的漏洞清单 |

替换原则：**指标名与阈值体系不变，只换实现**。这样报告、CI、历史基线都不会因为换实现而失去可比性。

## 已知限制

- `answer_match` 与 `faithfulness_proxy` 是 bigram 近似，只用于打通链路，不能作为质量结论；
- `MockTarget` 的检索是关键词重叠排序，不代表真实向量召回效果；
- 尚无基线 diff，历史运行只能人工对比 JSON；
- Dify 适配器只覆盖 Chat / Chatflow 的 `chat-messages`，Agent 与 Workflow 走不同接口；
- 未测多轮：每条用例独立 `user` 且不传 `conversation_id`，多轮场景需另写 ScenarioRunner；
- 未测流式：`response_mode` 固定 blocking，"首 token 延迟"需要单独的指标通道；
- 配置用 JSON 而非 YAML，是为了 v0 零依赖；引入 pyyaml 后 schema 不变。

## 面试可讲的三句话

1. 评测集按 5 类切片设计，每类切片用不同指标判定，答错时能归因到检索层、生成层还是策略层。
2. 采集失败与质量失败分开统计，避免系统宕机被读成质量下降；不返回引用的接口整体跳过检索指标，而不是记 0 分。
3. 接入先跑 doctor：鉴权、引用、doc_id 映射三件事不通过，后面的数字一律不可信。
