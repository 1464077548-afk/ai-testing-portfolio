"""适配层公共协议与响应映射。

设计要点：不同厂商的响应结构差异全部收敛到 MappingSpec，
适配器里不出现任何硬编码字段名。新增厂商时只写"发什么请求"，
"怎么取字段"交给配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..models import Answer, Case, Passage


class Target(Protocol):
    name: str
    version: str
    # 接口是否返回引用片段。False 时检索层指标整体跳过，不记 0 分
    supports_citations: bool

    def ask(self, case: Case) -> Answer: ...

    def health(self) -> bool: ...


class TransportError(RuntimeError):
    """网络层错误。retryable 决定 runner 是否重试。"""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def dig(obj: Any, path: str | None, default: Any = None) -> Any:
    """按点号路径取值，支持列表下标：choices.0.message.content。"""
    if not path:
        return default
    current = obj
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return default
            current = current[segment]
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return current


def strip_suffix(name: str) -> str:
    """把 policy_return_v3.pdf 归一成 policy_return_v3，方便和评测集对齐。"""
    for suffix in (".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".html", ".csv", ".xlsx"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


@dataclass
class MappingSpec:
    """响应字段映射。默认值对应 Dify 的 chat-messages 非流式响应。"""

    answer_path: str = "answer"
    contexts_path: str | None = "metadata.retriever_resources"
    doc_id_field: str = "document_id"
    chunk_id_field: str = "segment_id"
    text_field: str = "content"
    score_field: str = "score"
    doc_id_strip_suffix: bool = True

    @classmethod
    def from_config(cls, raw: dict | None) -> MappingSpec:
        raw = raw or {}
        return cls(
            answer_path=str(raw.get("answer_path", cls.answer_path)),
            contexts_path=raw.get("contexts_path", cls.contexts_path),
            doc_id_field=str(raw.get("doc_id_field", cls.doc_id_field)),
            chunk_id_field=str(raw.get("chunk_id_field", cls.chunk_id_field)),
            text_field=str(raw.get("text_field", cls.text_field)),
            score_field=str(raw.get("score_field", cls.score_field)),
            doc_id_strip_suffix=bool(raw.get("doc_id_strip_suffix", True)),
        )

    def extract_answer(self, payload: dict) -> str:
        value = dig(payload, self.answer_path, "")
        return value if isinstance(value, str) else str(value or "")

    def extract_contexts(self, payload: dict, top_k: int) -> list[Passage]:
        raw = dig(payload, self.contexts_path, [])
        if not isinstance(raw, list):
            return []

        passages: list[Passage] = []
        for item in raw[:top_k]:
            if not isinstance(item, dict):
                continue
            text = dig(item, self.text_field, "")
            if not isinstance(text, str) or not text.strip():
                continue
            doc_id = dig(item, self.doc_id_field, "")
            doc_id = str(doc_id) if doc_id is not None else ""
            if self.doc_id_strip_suffix:
                doc_id = strip_suffix(doc_id)
            chunk_id = dig(item, self.chunk_id_field, None)
            score = dig(item, self.score_field, None)
            passages.append(
                Passage(
                    doc_id=doc_id,
                    chunk_id=str(chunk_id) if chunk_id is not None else None,
                    text=text,
                    score=float(score) if isinstance(score, (int, float)) else None,
                )
            )
        return passages
