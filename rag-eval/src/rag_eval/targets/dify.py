"""Dify 适配器（主靶子）。

为什么选 Dify 做成第一适配器：
1. chat-messages 非流式响应里带 metadata.retriever_resources，
   每条含 document_name / segment_id / score / content —— 检索层指标可以真算，
   不需要自己去猜"它到底召回了什么"。
2. 国内招聘里出现频率最高，简历上可直接写"针对 Dify 应用做质量评测"。
3. 云端沙箱可免 Docker 起步，本地 docker compose 也能一键拉起。

关键行为：每条用例使用独立的 user 标识、不传 conversation_id，
避免多轮上下文互相污染 —— 这是接 Dify 时最常见的翻车点。
"""

from __future__ import annotations

import os
import time

from ..models import Answer, Case
from .base import MappingSpec, Target, TransportError
from .http import describe_body, get_json, join_url, post_json


class DifyTarget:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_key_env: str = "DIFY_API_KEY",
        name: str = "dify",
        version: str = "unknown",
        app_type: str = "chat",
        response_mode: str = "blocking",
        user_prefix: str = "rag-eval",
        inputs: dict | None = None,
        timeout: float = 60.0,
        mapping: MappingSpec | None = None,
        top_k: int = 3,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.api_key_env = api_key_env
        self.name = name
        self.version = version
        self.app_type = app_type
        self.response_mode = response_mode
        self.user_prefix = user_prefix
        self.inputs = inputs or {}
        self.timeout = timeout
        # 默认用 document_name 而非 document_id：后者是 UUID，没法与评测集对齐
        self.mapping = mapping or MappingSpec(doc_id_field="document_name")
        self.top_k = top_k
        self.supports_citations = True

    # ---- 基础能力 ----

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise TransportError(
                f"缺少 Dify API Key：请在环境变量 {self.api_key_env} 中设置，"
                "或在 targets 配置里直接写 api_key（不要提交到仓库）",
                retryable=False,
            )
        return {"Authorization": f"Bearer {self.api_key}"}

    def probe(self) -> tuple[bool, str]:
        """比 health() 更有信息量，doctor 命令用它给出可执行的排查提示。"""
        try:
            status, body = get_json(join_url(self.base_url, "parameters"), self._headers(), timeout=10.0)
        except TransportError as exc:
            return False, str(exc)
        if status == 200:
            return True, "应用参数接口可用"
        return False, self._explain(status, body)

    def health(self) -> bool:
        ok, _ = self.probe()
        return ok

    def ask(self, case: Case) -> Answer:
        started = time.perf_counter()
        payload = {
            "inputs": self.inputs,
            "query": case.question,
            "response_mode": self.response_mode,
            # 每条用例独立 user：Dify 按 user 隔离会话，避免上下文串味
            "user": f"{self.user_prefix}:{case.id}",
        }

        try:
            status, body = post_json(
                join_url(self.base_url, "chat-messages"),
                payload,
                self._headers(),
                timeout=self.timeout,
            )
        except TransportError as exc:
            return Answer(case_id=case.id, text="", error=f"dify: {exc}")

        if status >= 400 or body.get("event") == "error":
            return Answer(case_id=case.id, text="", error=f"dify: {self._explain(status, body)}")

        answer = self.mapping.extract_answer(body)
        contexts = self.mapping.extract_contexts(body, self.top_k)
        if not answer.strip():
            return Answer(
                case_id=case.id,
                text="",
                contexts=contexts,
                error="dify: 响应里没有答案文本，检查 answer_path 映射或应用是否已发布",
            )

        return Answer(
            case_id=case.id,
            text=answer,
            contexts=contexts,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ---- 错误分类：直接给出"下一步查什么" ----

    def _explain(self, status: int, body: dict) -> str:
        detail = describe_body(body)
        if status in (401, 403):
            return f"鉴权失败（{status}）：检查 API Key 是否属于该应用、是否已失效。{detail}"
        if status == 404:
            return (
                f"接口或应用不存在（404）：确认 base_url 是否以 /v1 结尾、"
                f"应用是否已发布、是否为 Chat 类型应用。{detail}"
            )
        if status == 429:
            return f"触发限流（429）：把 suite 的 concurrency 降到 1-2，或换时段重跑。{detail}"
        if status >= 500:
            return f"服务端错误（{status}）：可重试；持续出现需查 Dify 容器日志。{detail}"
        if status == 400:
            return f"请求参数被拒（400）：多为 app_type 与接口不匹配（chat vs completion）。{detail}"
        return f"未预期状态（{status}）：{detail}"


__all__ = ["DifyTarget", "MappingSpec", "Target"]
