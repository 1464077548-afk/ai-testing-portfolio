"""文本处理与领域小工具。

v0 用字符二元组（bigram）做重叠度计算：不依赖分词器，中英文都能跑，
代价是精度低于 embedding。这一取舍写在 ADR 里，面试被问到可以直接讲。
"""

from __future__ import annotations

import re

_STRIP = re.compile(r"[\s，。；：！？、（）()\[\]【】\"'“”‘’,.;:!?\-—_/]+")
_SENT_SPLIT = re.compile(r"[。！？；\n]+")

REFUSAL_MARKERS = (
    "无法确认",
    "无法回答",
    "没有相关",
    "未找到",
    "资料中没有",
    "抱歉",
    "不能确定",
)


def normalize(text: str) -> str:
    return _STRIP.sub("", text).lower()


def bigrams(text: str) -> set[str]:
    """字符二元组集合。归一化后长度不足 2 时退化为单字。"""
    s = normalize(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def overlap_ratio(a: str, b: str) -> float:
    """a 的 bigram 有多少比例出现在 b 中（召回视角）。"""
    ga, gb = bigrams(a), bigrams(b)
    if not ga:
        return 0.0
    return len(ga & gb) / len(ga)


def f1_overlap(a: str, b: str) -> float:
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    hit = len(ga & gb)
    if hit == 0:
        return 0.0
    precision = hit / len(ga)
    recall = hit / len(gb)
    return 2 * precision * recall / (precision + recall)


def sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in _SENT_SPLIT.split(text)) if len(s) >= 2]


def looks_like_refusal(text: str) -> bool:
    return any(m in text for m in REFUSAL_MARKERS)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
