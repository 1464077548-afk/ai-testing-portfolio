"""指标实现。

检索指标手写而不引框架：算法本身只有十几行，自己写才解释得清。
生成指标 v0 用 bigram 重叠做 proxy，接 Ragas 后替换同名函数即可，
报告与阈值体系不需要改。这个替换点写进了 ADR。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import Answer, Case, MetricScore
from .utils import f1_overlap, looks_like_refusal, mean, overlap_ratio, sentences

DEFAULT_THRESHOLDS: dict[str, float] = {
    "hit@3": 0.90,
    "recall@3": 0.85,
    "mrr": 0.75,
    "answer_match": 0.60,
    "faithfulness_proxy": 0.60,
    "refusal_accuracy": 0.90,
    "over_refusal_rate": 0.10,
}

DIRECTIONS: dict[str, str] = {
    "over_refusal_rate": "lower_better",
}


def hit_at_k(case: Case, answer: Answer, k: int) -> float:
    """黄金证据是否出现在前 k 条召回里。"""
    expected = {p.key() for p in case.expected_passages}
    if not expected:
        return 0.0
    return 1.0 if any(key in expected for key in answer.retrieved_keys[:k]) else 0.0


def recall_at_k(case: Case, answer: Answer, k: int) -> float:
    """召回了几成黄金证据，多跳问题会在这里暴露。"""
    expected = {p.key() for p in case.expected_passages}
    if not expected:
        return 0.0
    found = expected & set(answer.retrieved_keys[:k])
    return len(found) / len(expected)


def mrr(case: Case, answer: Answer) -> float:
    """首个正确证据的排名倒数。证据存在但排序靠后时，hit@k 仍为 1，MRR 会掉。"""
    expected = {p.key() for p in case.expected_passages}
    if not expected:
        return 0.0
    for rank, key in enumerate(answer.retrieved_keys, start=1):
        if key in expected:
            return 1.0 / rank
    return 0.0


def answer_match(case: Case, answer: Answer) -> float:
    """v0 的答案正确性近似：与参考答案的 bigram F1。接 Ragas 后换成语义判定。"""
    if not case.expected_answer:
        return 0.0
    return f1_overlap(answer.text, case.expected_answer)


def faithfulness_proxy(case: Case, answer: Answer) -> float:
    """有多少比例的答案句子能在召回内容中找到支撑。

    实现：逐句计算与所有召回片段的 bigram 重叠，超过 0.5 视为有支撑。
    真实项目应替换为 Ragas 的 faithfulness（基于论断分解 + NLI）。
    """
    if not answer.contexts or not answer.text.strip():
        return 0.0
    context_text = " ".join(p.text for p in answer.contexts)
    sents = sentences(answer.text)
    if not sents:
        return 0.0
    supported = sum(1 for s in sents if overlap_ratio(s, context_text) >= 0.5)
    return supported / len(sents)


def refusal_accuracy(case: Case, answer: Answer) -> float:
    """该拒答时是否拒答。编造答案属于最高危缺陷。"""
    return 1.0 if looks_like_refusal(answer.text) else 0.0


def over_refusal(case: Case, answer: Answer) -> float:
    """不该拒答却拒答。上限阈值，越低越好。"""
    return 1.0 if looks_like_refusal(answer.text) else 0.0


def evaluate(
    case: Case,
    answer: Answer,
    thresholds: dict[str, float] | None = None,
    k: int = 3,
    supports_citations: bool = True,
) -> list[MetricScore]:
    """按 case 的能力与数据条件，只跑适用指标，避免用不存在的证据算分。

    supports_citations=False 的接口（如纯生成类 OpenAI 兼容端点）不返回引用，
    检索层指标整体跳过；否则会得到一堆 0 分，误判为"检索很差"。
    但接口声明支持引用、这一条却没召回时，仍按 0 分计 —— 那是真实失败。
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    scores: list[MetricScore] = []

    def add(name: str, value: float, detail: dict | None = None) -> None:
        scores.append(
            MetricScore(
                name=name,
                value=round(value, 4),
                threshold=th.get(name, 1.0),
                direction=DIRECTIONS.get(name, "higher_better"),  # type: ignore[arg-type]
                detail=detail or {},
            )
        )

    if supports_citations and case.expected_passages:
        add(f"hit@{k}", hit_at_k(case, answer, k))
        add(f"recall@{k}", recall_at_k(case, answer, k))
        add("mrr", mrr(case, answer))

    if case.expected_answer:
        add("answer_match", answer_match(case, answer))

    if answer.contexts and not case.must_refuse:
        add("faithfulness_proxy", faithfulness_proxy(case, answer))

    if case.must_refuse:
        add("refusal_accuracy", refusal_accuracy(case, answer), {"expected": "refuse"})
    else:
        add("over_refusal_rate", over_refusal(case, answer), {"expected": "answer"})

    return scores


def aggregate(scores: Iterable[MetricScore]) -> dict[str, dict[str, float]]:
    """按指标名汇总：均值、通过率、样本数。"""
    buckets: dict[str, list[MetricScore]] = defaultdict(list)
    for score in scores:
        buckets[score.name].append(score)

    summary: dict[str, dict[str, float]] = {}
    for name, items in sorted(buckets.items()):
        summary[name] = {
            "mean": round(mean([s.value for s in items]), 4),
            "pass_rate": round(mean([1.0 if s.passed else 0.0 for s in items]), 4),
            "n": float(len(items)),
            "threshold": items[0].threshold,
        }
    return summary


def verdict(summary: dict[str, dict[str, float]]) -> tuple[bool, list[str]]:
    """整体判定：任一指标通过率低于 1.0 即不通过，并列出失败项。

    空汇总视为失败：一条指标都没算出来，不能当成通过（常见于全量采集失败）。
    """
    if not summary:
        return False, ["没有任何指标被评估，检查用例筛选与采集状态"]
    failed = [
        f"{name}: 均值 {data['mean']:.3f} / 阈值 {data['threshold']:.3f} / 通过率 {data['pass_rate']:.0%}"
        for name, data in summary.items()
        if data["pass_rate"] < 1.0
    ]
    return (not failed, failed)
