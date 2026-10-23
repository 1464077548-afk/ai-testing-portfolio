"""适配器工厂：新增被测系统只需要在这里加一个分支。

注意：分支里只做"配置 → 构造参数"的翻译，不写业务逻辑。
"""

from __future__ import annotations

from pathlib import Path

from .base import MappingSpec, Target
from .dify import DifyTarget
from .http_rag import HttpRagTarget
from .mock import MockTarget
from .openai_compat import OpenAICompatTarget


def build_target(config: dict, project_root: Path, top_k: int = 3) -> Target:
    kind = config.get("kind", "mock")

    if kind == "mock":
        degradation = config.get("degradation", {})
        return MockTarget.from_jsonl(
            project_root / config["corpus"],
            top_k=top_k,
            drop_expected_rate=float(degradation.get("drop_expected_rate", 0.0)),
            hallucinate_rate=float(degradation.get("hallucinate_rate", 0.0)),
        )

    if kind == "dify":
        return DifyTarget(
            base_url=config["base_url"],
            api_key=config.get("api_key", ""),
            api_key_env=config.get("api_key_env", "DIFY_API_KEY"),
            name=config.get("name", "dify"),
            version=str(config.get("version", "unknown")),
            app_type=config.get("app_type", "chat"),
            response_mode=config.get("response_mode", "blocking"),
            user_prefix=config.get("user_prefix", "rag-eval"),
            inputs=config.get("inputs"),
            timeout=float(config.get("timeout", 60.0)),
            mapping=MappingSpec.from_config(config.get("mapping")),
            top_k=top_k,
        )

    if kind == "openai_compat":
        return OpenAICompatTarget(
            base_url=config["base_url"],
            model=config["model"],
            api_key=config.get("api_key", ""),
            api_key_env=config.get("api_key_env", "OPENAI_API_KEY"),
            name=config.get("name", "openai-compat"),
            version=str(config.get("version", "unknown")),
            system_prompt=config.get("system_prompt"),
            temperature=float(config.get("temperature", 0.0)),
            timeout=float(config.get("timeout", 60.0)),
            extra_body=config.get("extra_body"),
            mapping=MappingSpec.from_config(config.get("mapping")),
            top_k=top_k,
        )

    if kind == "http":
        return HttpRagTarget(
            endpoint=config["endpoint"],
            name=config.get("name", "http-rag"),
            version=str(config.get("version", "unknown")),
            body_template=config.get("body_template"),
            headers=config.get("headers"),
            health_url=config.get("health_url"),
            timeout=float(config.get("timeout", 30.0)),
            mapping=MappingSpec.from_config(config.get("mapping")),
            top_k=top_k,
        )

    raise ValueError(f"未知 target kind：{kind}，可选 mock / dify / openai_compat / http")
