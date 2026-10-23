"""执行器：并发采集、失败重试、结果组装。

两条纪律：
1. 采集失败单独统计，不能混进质量分，否则系统宕机会被读成"质量下降"。
2. 重试只对异常生效，不对"答错"生效。对答错重试是自欺欺人。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .metrics import aggregate, evaluate, verdict
from .models import Answer, Case, MetricScore
from .targets import Target


@dataclass
class Record:
    case_id: str
    slice: str
    question: str
    answer: str
    context_count: int
    error: str | None
    scores: list[MetricScore] = field(default_factory=list)

    @property
    def failed_metrics(self) -> list[str]:
        return [s.name for s in self.scores if not s.passed]


@dataclass
class RunResult:
    target: str
    target_version: str
    started_at: str
    duration_ms: int
    k: int
    records: list[Record]
    summary: dict[str, dict[str, float]]
    by_slice: dict[str, dict[str, dict[str, float]]]
    errors: int
    passed: bool
    failed_metrics: list[str]
    supports_citations: bool = True

    def to_dict(self) -> dict:
        return {
            "meta": {
                "target": self.target,
                "target_version": self.target_version,
                "started_at": self.started_at,
                "duration_ms": self.duration_ms,
                "k": self.k,
                "cases": len(self.records),
                "errors": self.errors,
                "supports_citations": self.supports_citations,
            },
            "summary": self.summary,
            "by_slice": self.by_slice,
            "passed": self.passed,
            "failed_metrics": self.failed_metrics,
            "records": [
                {
                    "case_id": r.case_id,
                    "slice": r.slice,
                    "question": r.question,
                    "answer": r.answer,
                    "context_count": r.context_count,
                    "error": r.error,
                    "failed_metrics": r.failed_metrics,
                    "scores": [asdict(s) | {"passed": s.passed} for s in r.scores],
                }
                for r in self.records
            ],
        }


def ask_with_retry(target: Target, case: Case, retries: int) -> Answer:
    last: Answer | None = None
    for attempt in range(retries + 1):
        answer = target.ask(case)
        if not answer.error:
            return answer
        last = answer
        if attempt < retries:
            time.sleep(0.5 * (2**attempt))
    return last  # type: ignore[return-value]


def run(
    cases: list[Case],
    target: Target,
    thresholds: dict[str, float] | None = None,
    k: int = 3,
    concurrency: int = 4,
    retries: int = 1,
) -> RunResult:
    if not target.health():
        raise RuntimeError(f"被测系统 {target.name} 健康检查失败，拒绝开跑")

    started = datetime.now(timezone.utc).astimezone()
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        answers = list(pool.map(lambda c: ask_with_retry(target, c, retries), cases))

    records: list[Record] = []
    supports_citations = bool(getattr(target, "supports_citations", True))
    for case, answer in zip(cases, answers):
        scores = [] if answer.error else evaluate(case, answer, thresholds, k, supports_citations)
        records.append(
            Record(
                case_id=case.id,
                slice=case.slice,
                question=case.question,
                answer=answer.text,
                context_count=len(answer.contexts),
                error=answer.error,
                scores=scores,
            )
        )

    all_scores: list[MetricScore] = [s for r in records for s in r.scores]
    summary = aggregate(all_scores)

    by_slice: dict[str, dict[str, dict[str, float]]] = {}
    for slice_name in sorted({r.slice for r in records}):
        subset = [s for r in records if r.slice == slice_name for s in r.scores]
        if subset:
            by_slice[slice_name] = aggregate(subset)

    ok, failed = verdict(summary)
    return RunResult(
        target=target.name,
        target_version=target.version,
        started_at=started.isoformat(timespec="seconds"),
        duration_ms=int((time.perf_counter() - t0) * 1000),
        k=k,
        records=records,
        summary=summary,
        by_slice=by_slice,
        errors=sum(1 for r in records if r.error),
        passed=ok,
        failed_metrics=failed,
        supports_citations=supports_citations,
    )
