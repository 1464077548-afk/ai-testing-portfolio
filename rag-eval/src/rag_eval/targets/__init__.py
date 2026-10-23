"""被测系统适配层。

这是整个项目唯一的扩展点：新增 Dify / RAGFlow / FastGPT / 自研系统，
只需实现 Target 协议并在 factory 里加一个分支，
runner、metrics、report 一行都不用改。
"""

from __future__ import annotations

from .base import MappingSpec, Target, TransportError, dig, strip_suffix
from .dify import DifyTarget
from .factory import build_target
from .http_rag import HttpRagTarget
from .mock import MockTarget
from .openai_compat import OpenAICompatTarget

__all__ = [
    "Target",
    "MappingSpec",
    "TransportError",
    "dig",
    "strip_suffix",
    "MockTarget",
    "DifyTarget",
    "OpenAICompatTarget",
    "HttpRagTarget",
    "build_target",
]
