"""通用 HTTP 适配器：给"文档没写完、接口格式自定"的系统留的逃生口。

用 body_template 描述请求体，用 mapping 描述响应字段。
不要在配置里写 Python 表达式 —— 评测配置是可审计的，不是代码。
"""

from __future__ import annotations

import time
from typing import Any

from ..models import Answer, Case
from .base import MappingSpec, TransportError
from .http import describe_body, get_json, post_json


def _substitute(node: Any, question: str) -> Any:
    if isinstance(node, str):
        return node.replace("{question}", question)
    if isinstance(node, dict):
        return {k: _substitute(v, question) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, question) for v in node]
    return node


class HttpRagTarget:
    def __init__(
        self,
        endpoint: str,
        name: str = "http-rag",
        version: str = "unknown",
        body_template: dict | None = None,
        headers: dict[str, str] | None = None,
        health_url: str | None = None,
        timeout: float = 30.0,
        mapping: MappingSpec | None = None,
        top_k: int = 3,
    ) -> None:
        self.endpoint = endpoint
        self.name = name
        self.version = version
        self.body_template = body_template or {"query": "{question}"}
        self.headers = headers or {}
        self.health_url = health_url
        self.timeout = timeout
        self.mapping = mapping or MappingSpec()
        self.top_k = top_k
        self.supports_citations = bool(self.mapping.contexts_path)

    def probe(self) -> tuple[bool, str]:
        if not self.health_url:
            return True, "未配置 health_url，跳过探测"
        try:
            status, body = get_json(self.health_url, self.headers, timeout=10.0)
        except TransportError as exc:
            return False, str(exc)
        return (status < 400, f"health_url 返回 {status} {describe_body(body)}")

    def health(self) -> bool:
        ok, _ = self.probe()
        return ok

    def ask(self, case: Case) -> Answer:
        started = time.perf_counter()
        try:
            status, body = post_json(
                self.endpoint,
                _substitute(self.body_template, case.question),
                self.headers,
                timeout=self.timeout,
            )
        except TransportError as exc:
            return Answer(case_id=case.id, text="", error=f"http-rag: {exc}")

        if status >= 400:
            return Answer(
                case_id=case.id, text="", error=f"http-rag({status}): {describe_body(body)}"
            )

        answer = self.mapping.extract_answer(body)
        contexts = self.mapping.extract_contexts(body, self.top_k)
        return Answer(
            case_id=case.id,
            text=answer,
            contexts=contexts,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
