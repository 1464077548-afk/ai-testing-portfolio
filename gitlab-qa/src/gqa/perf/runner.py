"""性能采样器：并发压测 + 百分位统计 + 阈值判定。

为什么自己写而不用 locust：
- 这里要的是「测试门禁」而不是「压测平台」——需要一个能从 pytest 里调用、
  能直接产出结构化结果、能让 CI 判定通过/失败的东西；
- 结果要能贴进 Allure 报告，还要能落 JSON 给趋势对比用。
"""

from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field


@dataclass
class Sample:
    ok: bool
    latency_ms: int
    status: int = 0
    error: str = ""


@dataclass
class PerfResult:
    name: str
    samples: list[Sample] = field(default_factory=list)
    duration_ms: int = 0
    concurrency: int = 1

    # ---------------------------------------------------------- 统计
    @property
    def latencies(self) -> list[int]:
        return [s.latency_ms for s in self.samples]

    def percentile(self, p: float) -> float:
        data = sorted(self.latencies)
        if not data:
            return 0.0
        # 线性插值法，和 Prometheus / Grafana 的口径一致，方便对账
        k = (len(data) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(data) - 1)
        return data[lo] + (data[hi] - data[lo]) * (k - lo)

    @property
    def p50(self) -> float:
        return self.percentile(0.50)

    @property
    def p95(self) -> float:
        return self.percentile(0.95)

    @property
    def p99(self) -> float:
        return self.percentile(0.99)

    @property
    def errors(self) -> int:
        return sum(1 for s in self.samples if not s.ok)

    @property
    def error_rate(self) -> float:
        return self.errors / len(self.samples) if self.samples else 1.0

    @property
    def rps(self) -> float:
        return len(self.samples) / (self.duration_ms / 1000) if self.duration_ms else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "concurrency": self.concurrency,
            "count": len(self.samples),
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "rps": round(self.rps, 2),
            "min_ms": min(self.latencies) if self.samples else 0,
            "p50_ms": round(self.p50, 1),
            "p95_ms": round(self.p95, 1),
            "p99_ms": round(self.p99, 1),
            "max_ms": max(self.latencies) if self.samples else 0,
            "mean_ms": round(statistics.mean(self.latencies), 1) if self.samples else 0,
            "duration_ms": self.duration_ms,
        }

    def markdown(self) -> str:
        d = self.to_dict()
        return "\n".join([
            f"### {d['name']}",
            "",
            "| 指标 | 值 |",
            "|---|---|",
            f"| 样本数 | {d['count']} |",
            f"| 并发 | {d['concurrency']} |",
            f"| 吞吐 | {d['rps']} req/s |",
            f"| 错误率 | {d['error_rate']:.2%} ({d['errors']}/{d['count']}) |",
            f"| P50 | {d['p50_ms']} ms |",
            f"| P95 | {d['p95_ms']} ms |",
            f"| P99 | {d['p99_ms']} ms |",
            f"| 最大 | {d['max_ms']} ms |",
        ])


def run_load(
    name: str,
    fn,
    concurrency: int = 8,
    samples: int = 60,
) -> PerfResult:
    """跑一轮固定样本数的并发负载。

    fn 的签名是 () -> Sample，由调用方决定这一次「请求」做什么。
    """
    result = PerfResult(name=name, concurrency=concurrency)
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(fn) for _ in range(samples)]
        for fut in as_completed(futures):
            try:
                result.samples.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                result.samples.append(Sample(ok=False, latency_ms=0,
                                             error=f"{type(exc).__name__}: {exc}"))

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


def verdict(result: PerfResult, p95_ms: int, max_error_rate: float) -> tuple[bool, list[str]]:
    """性能门禁判定。返回 (是否通过, 失败原因列表)。"""
    failed: list[str] = []
    if not result.samples:
        return False, ["没有任何样本，本次性能测试无效"]
    if result.p95 > p95_ms:
        failed.append(f"P95 {result.p95:.0f}ms 超过阈值 {p95_ms}ms")
    if result.error_rate > max_error_rate:
        failed.append(f"错误率 {result.error_rate:.2%} 超过阈值 {max_error_rate:.2%}")
    return (not failed), failed
