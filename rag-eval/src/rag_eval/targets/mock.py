"""确定性桩实现：禁网、无密钥也能跑通全链路。

重要：它只回答"链路是否通、断言是否生效"，不代表真实质量。
故意设计成"检索到黄金片段才作答"，让指标之间保持因果一致。

degradation 用于演练回归：按 case id 做稳定哈希注入两类缺陷，
同一输入永远得到同一结果，报告里因此能看到真实的红灯。
"""

from __future__ import annotations

import json
import time
import zlib
from pathlib import Path

from ..models import Answer, Case, Passage
from ..utils import f1_overlap


class MockTarget:
    def __init__(
        self,
        corpus: dict[tuple[str, str | None], str],
        top_k: int = 3,
        drop_expected_rate: float = 0.0,
        hallucinate_rate: float = 0.0,
    ) -> None:
        self.name = "mock-degraded" if (drop_expected_rate or hallucinate_rate) else "mock"
        self.version = "0.1.0"
        self.top_k = top_k
        self._corpus = corpus
        self.drop_expected_rate = drop_expected_rate
        self.hallucinate_rate = hallucinate_rate
        self.supports_citations = True

    @classmethod
    def from_jsonl(
        cls,
        path: Path,
        top_k: int = 3,
        drop_expected_rate: float = 0.0,
        hallucinate_rate: float = 0.0,
    ) -> MockTarget:
        corpus: dict[tuple[str, str | None], str] = {}
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                corpus[(row["doc_id"], row.get("chunk_id"))] = row["text"]
        return cls(
            corpus,
            top_k=top_k,
            drop_expected_rate=drop_expected_rate,
            hallucinate_rate=hallucinate_rate,
        )

    def health(self) -> bool:
        return bool(self._corpus)

    def probe(self) -> tuple[bool, str]:
        return self.health(), f"语料 {len(self._corpus)} 段"

    @staticmethod
    def _roll(case_id: str, salt: str) -> float:
        """稳定伪随机 [0,1)，保证同一条用例每次得到同样的缺陷。"""
        h = zlib.crc32(f"{salt}:{case_id}".encode("utf-8"))
        return (h % 10_000) / 10_000

    def ask(self, case: Case) -> Answer:
        started = time.perf_counter()
        expected = {p.key() for p in case.expected_passages}

        ranked = sorted(
            self._corpus.items(),
            key=lambda kv: f1_overlap(case.question, kv[1]),
            reverse=True,
        )

        if expected and self._roll(case.id, "drop") < self.drop_expected_rate:
            ranked = [kv for kv in ranked if kv[0] not in expected]

        ranked = ranked[: self.top_k]
        contexts = [
            Passage(
                doc_id=doc_id,
                chunk_id=chunk_id,
                text=text,
                score=round(f1_overlap(case.question, text), 4),
            )
            for (doc_id, chunk_id), text in ranked
        ]
        hit = any((p.doc_id, p.chunk_id) in expected for p in contexts)

        if case.must_refuse:
            if self._roll(case.id, "hallucinate") < self.hallucinate_rate:
                text = "可以的，该服务已全面支持，具体以页面展示为准。"
            else:
                text = "根据现有资料无法确认该问题的答案。"
        elif hit and case.expected_answer:
            text = case.expected_answer
        elif case.expected_answer and contexts:
            # 典型缺陷：检索没命中，却拿相邻政策硬答。忠实度不低，但答案是错的。
            text = contexts[0].text
        else:
            text = "根据现有资料无法确认该问题的答案。"

        return Answer(
            case_id=case.id,
            text=text,
            contexts=contexts,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
