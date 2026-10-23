"""OpenAI 兼容适配器：覆盖 Ollama / vLLM / DeepSeek / 通义 / FastGPT 等。

适用范围要说清楚：这类接口通常只返回答案文本，不返回引用片段。
因此检索层指标（hit@k / MRR）会因为缺 contexts 自动跳过 —— 这是设计如此，
不是 bug。若要测检索层，请用 Dify / RAGFlow 这类会返回引用的接口。
"""

from __future__ import annotations

import os
import time

from ..models import Answer, Case
from .base import MappingSpec, TransportError
from .http import describe_body, get_json, join_url, post_json


class OpenAICompatTarget:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        api_key_env: str = "OPENAI_API_KEY",
        name: str = "openai-compat",
        version: str = "unknown",
        system_prompt: str | None = None,
        temperature: float = 0.0,
        timeout: float = 60.0,
        extra_body: dict | None = None,
        mapping: MappingSpec | None = None,
        top_k: int = 3,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.api_key_env = api_key_env
        self.name = name
        self.version = version
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.timeout = timeout
        self.extra_body = extra_body or {}
        self.mapping = mapping or MappingSpec(
            answer_path="choices.0.message.content",
            contexts_path=None,
        )
        self.top_k = top_k
        # 这类接口不返回引用片段，检索层指标应整体跳过而不是记 0 分
        self.supports_citations = bool(self.mapping.contexts_path)

    def _headers(self) -> dict[str, str]:
        # 本地 Ollama 允许空 key，所以这里不强制报错
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def probe(self) -> tuple[bool, str]:
        try:
            status, body = get_json(join_url(self.base_url, "models"), self._headers(), timeout=10.0)
        except TransportError as exc:
            return False, str(exc)
        if status == 200:
            return True, "models 接口可用"
        if status == 404:
            return True, "该网关未实现 /models，跳过探测（开跑后若失败看具体报错）"
        if status in (401, 403):
            return False, f"鉴权失败（{status}）：检查 {self.api_key_env}"
        return False, f"探测失败（{status}）：{describe_body(body)}"

    def health(self) -> bool:
        ok, _ = self.probe()
        return ok

    def ask(self, case: Case) -> Answer:
        started = time.perf_counter()
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": case.question})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        payload.update(self.extra_body)

        try:
            status, body = post_json(
                join_url(self.base_url, "chat/completions"),
                payload,
                self._headers(),
                timeout=self.timeout,
            )
        except TransportError as exc:
            return Answer(case_id=case.id, text="", error=f"openai-compat: {exc}")

        if status >= 400:
            return Answer(
                case_id=case.id,
                text="",
                error=f"openai-compat({status}): {describe_body(body)}",
            )

        answer = self.mapping.extract_answer(body)
        contexts = self.mapping.extract_contexts(body, self.top_k)
        if not answer.strip():
            return Answer(
                case_id=case.id,
                text="",
                contexts=contexts,
                error="openai-compat: 响应中没有文本，检查 answer_path 映射",
            )

        return Answer(
            case_id=case.id,
            text=answer,
            contexts=contexts,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
