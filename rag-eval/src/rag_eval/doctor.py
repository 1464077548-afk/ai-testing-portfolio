"""接入自检：把"接不上"的排查过程固化成命令。

背景：接真实系统时 80% 的时间耗在三件事上 ——
1. 鉴权/地址错；
2. 响应里根本没有引用片段（应用没开引用），导致检索指标全被跳过；
3. 召回的 doc_id 与评测集对不上（document_name vs document_id、带后缀）。

这三件事都不该靠人肉看响应体，应该由工具一次说清楚。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Case
from .targets.base import Target


@dataclass
class ProbeResult:
    case_id: str
    question: str
    error: str | None
    answer_preview: str
    context_count: int
    retrieved_doc_ids: list[str] = field(default_factory=list)
    expected_doc_ids: list[str] = field(default_factory=list)
    matched_doc_ids: list[str] = field(default_factory=list)
    latency_ms: int = 0

    @property
    def mapping_ok(self) -> bool:
        return bool(self.matched_doc_ids)


@dataclass
class DoctorReport:
    target_name: str
    target_version: str
    healthy: bool
    health_message: str
    probes: list[ProbeResult]
    warnings: list[str]
    suggestions: list[str]

    @property
    def doc_id_coverage(self) -> float:
        expected = sum(len(p.expected_doc_ids) for p in self.probes)
        matched = sum(len(p.matched_doc_ids) for p in self.probes)
        return matched / expected if expected else 0.0

    @property
    def ready(self) -> bool:
        return self.healthy and bool(self.probes) and all(p.error is None for p in self.probes)


def diagnose(target: Target, cases: list[Case], limit: int = 3) -> DoctorReport:
    probe_fn = getattr(target, "probe", None)
    if callable(probe_fn):
        healthy, health_message = probe_fn()
    else:
        healthy, health_message = target.health(), "无 probe() 实现，仅做健康检查"

    probes: list[ProbeResult] = []
    if healthy:
        # 优先挑带黄金证据的用例，才能验证 doc_id 映射
        ordered = sorted(cases, key=lambda c: 0 if c.expected_passages else 1)[:limit]
        for case in ordered:
            answer = target.ask(case)
            expected = {p.doc_id for p in case.expected_passages}
            retrieved = {p.doc_id for p in answer.contexts}
            probes.append(
                ProbeResult(
                    case_id=case.id,
                    question=case.question,
                    error=answer.error,
                    answer_preview=answer.text[:80],
                    context_count=len(answer.contexts),
                    retrieved_doc_ids=sorted(retrieved),
                    expected_doc_ids=sorted(expected),
                    matched_doc_ids=sorted(expected & retrieved),
                    latency_ms=answer.latency_ms,
                )
            )

    warnings: list[str] = []
    suggestions: list[str] = []

    if not healthy:
        suggestions.append("先修鉴权与地址：检查 base_url 是否以 /v1 结尾、API Key 是否属于该应用")

    if probes:
        if all(p.context_count == 0 for p in probes) and all(p.error is None for p in probes):
            warnings.append("所有探测用例都没有返回引用片段")
            suggestions.append(
                "在应用编排里打开『引用和归属』，并确认 contexts_path 指向 retriever_resources"
            )
            suggestions.append("若该接口本就不返回引用，检索层指标会被自动跳过，只测生成与行为")

        with_context = [p for p in probes if p.context_count > 0]
        if with_context and all(not p.mapping_ok for p in with_context):
            warnings.append("召回片段的 doc_id 与评测集完全不匹配")
            suggestions.append(
                "把 mapping.doc_id_field 从 document_id 改为 document_name，并保持 doc_id_strip_suffix=true"
            )
            suggestions.append(
                f"实际返回的 doc_id 形如：{with_context[0].retrieved_doc_ids[:3]}，"
                f"评测集期望形如：{with_context[0].expected_doc_ids[:3]}"
            )

        slow = [p for p in probes if p.latency_ms > 10_000]
        if slow:
            warnings.append(f"{len(slow)} 条探测用例耗时超过 10 秒")
            suggestions.append("把 suite 的 concurrency 降到 1-2，并检查检索 top-k 是否过大")

        failed = [p for p in probes if p.error]
        for probe in failed:
            warnings.append(f"{probe.case_id} 采集失败：{probe.error}")

    return DoctorReport(
        target_name=target.name,
        target_version=target.version,
        healthy=healthy,
        health_message=health_message,
        probes=probes,
        warnings=warnings,
        suggestions=suggestions,
    )
