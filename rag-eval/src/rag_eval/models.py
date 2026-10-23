"""领域模型：Case / Answer / MetricScore。

v0 使用 dataclasses，目的是一行依赖都不装就能跑通链路。
接 pydantic 时字段保持不变，只把校验搬到 model_validator 里即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Slice = Literal["core", "boundary", "refusal", "adversarial", "security"]
Direction = Literal["higher_better", "lower_better"]

VALID_SLICES: tuple[str, ...] = ("core", "boundary", "refusal", "adversarial", "security")


@dataclass(frozen=True)
class PassageRef:
    """黄金证据的引用。chunk_id 为空表示只精确到文档级。"""

    doc_id: str
    chunk_id: str | None = None

    def key(self) -> tuple[str, str | None]:
        return (self.doc_id, self.chunk_id)


@dataclass
class Case:
    """一条评测用例。一个 case 只考一个意图，否则失败时无法归因。"""

    id: str
    slice: Slice
    question: str
    expected_answer: str | None = None
    expected_passages: list[PassageRef] = field(default_factory=list)
    must_refuse: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass
class Passage:
    """被测系统实际召回的一段内容。"""

    doc_id: str
    chunk_id: str | None
    text: str
    score: float | None = None


@dataclass
class Answer:
    """被测系统的一次回答。采集失败也落库，不能吞异常。"""

    case_id: str
    text: str
    contexts: list[Passage] = field(default_factory=list)
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    @property
    def retrieved_keys(self) -> list[tuple[str, str | None]]:
        return [(p.doc_id, p.chunk_id) for p in self.contexts]


@dataclass
class MetricScore:
    name: str
    value: float
    threshold: float
    direction: Direction = "higher_better"
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        if self.direction == "higher_better":
            return self.value >= self.threshold
        return self.value <= self.threshold
