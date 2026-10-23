"""报告生成：单文件 HTML，样式内联，可离线打开、可作为 CI artifact。

第一屏必须是结论，不是图表。读者要在 3 分钟内决定"能不能发"。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .runner import RunResult

_STYLE = """
body{margin:0;background:#fff;color:#1f1f1e;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.65}
.wrap{max-width:960px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:22px;font-weight:500;margin:0 0 4px}
.meta{color:#6b6a66;font-size:12.5px;margin-bottom:20px}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;margin-left:8px}
.ok{background:#e1f5ee;color:#0f6e56}
.bad{background:#fcebeb;color:#a32d2d}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0 24px}
.card{border:1px solid rgba(0,0,0,.08);border-radius:10px;padding:12px 14px;background:#fafaf8}
.card b{display:block;font-size:20px;font-weight:500;margin-bottom:2px}
.card span{font-size:11.5px;color:#93918b}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0 18px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid rgba(0,0,0,.06)}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#93918b;font-weight:500;background:#fafaf8}
td.num{font-variant-numeric:tabular-nums}
.fail{color:#a32d2d}
.pass{color:#0f6e56}
h2{font-size:14px;font-weight:500;margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid rgba(0,0,0,.1)}
.q{font-size:12.5px;color:#5f5e5a;margin:14px 0 4px}
.a{font-size:12.5px;background:#fafaf8;border-left:2px solid #d3d1c7;padding:8px 10px;border-radius:0 6px 6px 0}
@media print{body{font-size:10pt}.wrap{padding:0}.card{break-inside:avoid}}
"""


def _cell(value: float, threshold: float, lower_better: bool) -> str:
    ok = value <= threshold if lower_better else value >= threshold
    cls = "pass" if ok else "fail"
    return f'<td class="num {cls}">{value:.3f}</td>'


def render_html(result: RunResult) -> str:
    data = result.to_dict()
    meta = data["meta"]
    total = sum(1 for r in result.records if not r.error)
    passed_cases = sum(1 for r in result.records if not r.error and not r.failed_metrics)
    pass_rate = passed_cases / total if total else 0.0

    rows = []
    lower_better_names = {"over_refusal_rate"}
    for name, item in result.summary.items():
        lower = name in lower_better_names
        rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            + _cell(item["mean"], item["threshold"], lower)
            + f'<td class="num">{item["threshold"]:.3f}</td>'
            + f'<td class="num">{item["pass_rate"]:.0%}</td>'
            + f'<td class="num">{int(item["n"])}</td>'
            "</tr>"
        )

    slice_rows = []
    for slice_name, summary in result.by_slice.items():
        worst = sorted(summary.items(), key=lambda kv: kv[1]["pass_rate"])[:2]
        detail = " · ".join(f"{n} {v['mean']:.2f}" for n, v in worst)
        slice_rows.append(
            f"<tr><td>{html.escape(slice_name)}</td>"
            f'<td class="num">{summary["hit@3"]["pass_rate"] if "hit@3" in summary else 1.0:.0%}</td>'
            f"<td>{html.escape(detail)}</td></tr>"
        )

    failures = [r for r in result.records if r.failed_metrics][:10]
    failure_blocks = []
    for record in failures:
        failure_blocks.append(
            f'<div class="q">{html.escape(record.case_id)} · {html.escape(record.slice)} · '
            f'失败指标：{html.escape(", ".join(record.failed_metrics))}</div>'
            f'<div class="a"><b>问：</b>{html.escape(record.question)}<br>'
            f'<b>答：</b>{html.escape(record.answer or "（采集失败）")}<br>'
            f"<b>召回片段数：</b>{record.context_count}</div>"
        )

    status = (
        f'<span class="badge ok">通过</span>'
        if result.passed
        else f'<span class="badge bad">未通过 · {len(result.failed_metrics)} 项指标越界</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>评测报告 · {html.escape(meta['target'])}</title>
<style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>RAG 质量评测报告 {status}</h1>
<div class="meta">被测系统 {html.escape(meta['target'])} v{html.escape(meta['target_version'])} ·
{meta['cases']} 条用例 · top-{meta['k']} · 耗时 {meta['duration_ms']}ms ·
开始于 {html.escape(meta['started_at'])}</div>

<div class="cards">
  <div class="card"><b>{pass_rate:.0%}</b><span>用例通过率</span></div>
  <div class="card"><b>{len(result.failed_metrics)}</b><span>越界指标数</span></div>
  <div class="card"><b>{meta['errors']}</b><span>采集失败数</span></div>
  <div class="card"><b>{meta['cases']}</b><span>用例总数</span></div>
</div>

<h2>指标总览</h2>
<table><thead><tr><th>指标</th><th>均值</th><th>阈值</th><th>通过率</th><th>样本</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>

<h2>切片下钻</h2>
<table><thead><tr><th>切片</th><th>检索通过率</th><th>最弱两项</th></tr></thead>
<tbody>{''.join(slice_rows)}</tbody></table>

<h2>失败用例（最多 10 条）</h2>
{''.join(failure_blocks) if failure_blocks else '<p class="a">没有失败用例。</p>'}

<h2>说明</h2>
<div class="a">v0 的 answer_match 与 faithfulness_proxy 为 bigram 重叠近似，仅用于打通链路与验证断言机制，不代表语义级质量结论。接入 Ragas 后同名指标替换为论断分解 + NLI 判定，阈值体系不变。</div>
</div></body></html>"""


def write_outputs(result: RunResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.started_at.replace(":", "").replace("-", "").replace("+", "_")
    json_path = out_dir / f"run-{stamp}.json"
    html_path = out_dir / "report.html"
    json_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(result), encoding="utf-8")
    return json_path, html_path
