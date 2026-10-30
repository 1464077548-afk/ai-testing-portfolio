"""YAML 用例数据加载。

把「测试矩阵」从 Python 代码里搬到 YAML，收益只有一个但很实在：
**加一条边界用例不用改代码**。输入、期望状态码、跳过理由全写在数据文件里，
评审时看 YAML 就能确认覆盖了什么，不需要读代码逻辑。

代价是引入了「数据与代码不同步」的风险，所以这个加载器刻意做得严格：

1. **未知字段直接报错**。YAML 里把 `expect` 拼成 `expected`，
   静默忽略的结果是那条用例被悄悄跳过——报告上少一条，没人会发现。
2. **必填字段缺失直接报错**。`path` 少写了，用例会去打根路径，
   拿到一个"看起来合理的 200"，比报错危险得多。
3. **id 必须文件内唯一**。重复的 id 会让 pytest 的参数化 id 撞在一起，
   失败时看不出是哪条红的。

这些错误在**收集阶段**就抛出来（而不是跑到用例里才发现），
因为一个写坏的 YAML 文件应该让整轮跑不起来，而不是让它悄悄少跑几条。
"""

from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

# 项目根：src/gqa/data/case_loader.py → parents[3]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "tests" / "data" / "api"

ALLOWED_FIELDS = frozenset({
    "id", "desc", "method", "path", "params", "body",
    "expect", "expect_msg", "skip", "cleanup",
})
REQUIRED_FIELDS = ("id", "desc", "method", "path", "expect")
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ALLOWED_CLEANUP = frozenset({"branch", "tag", "project", "issue", "label", "milestone"})


class CaseDataError(ValueError):
    """YAML 用例数据写错了。收集阶段抛出，让整轮跑不起来。"""


@dataclass(frozen=True)
class Case:
    """一条数据驱动用例。

    渲染分两步是刻意的：`{uniq}` 在加载时替换（同一轮里保持稳定，
    重跑同一文件不会每次都不同），`{pid}` 在用例里替换（运行时才知道项目 id）。
    """

    id: str
    desc: str
    method: str
    path: str
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    expect: tuple[int, ...] = ()
    expect_msg: str | None = None
    skip: str | None = None
    cleanup: str | None = None
    source: str = ""

    # ---------------------------------------------------------------- 渲染
    def bind(self, **values: Any) -> Case:
        """把 path / params / body 里的 {占位符} 换成运行时值。"""
        return replace(
            self,
            path=self.path.format(**values),
            params=_render(self.params, values),
            body=_render(self.body, values),
            cleanup=(self.cleanup.format(**values) if self.cleanup else None),
        )

    def describe(self) -> str:
        bits = [f"{self.method} {self.path}"]
        if self.params:
            bits.append(f"params={self.params}")
        if self.body:
            bits.append(f"body={self.body}")
        bits.append(f"expect={list(self.expect)}")
        return "  ".join(bits)


def _render(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: _render(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, values) for v in value]
    if isinstance(value, str):
        try:
            return value.format(**values)
        except (KeyError, IndexError):
            # 占位符没给值就原样返回：让服务端去报错，比在客户端静默吞掉好
            return value
    return value


def _uniq() -> str:
    alphabet = string.ascii_lowercase + string.digits
    rand = "".join(random.Random(time.time_ns()).choices(alphabet, k=6))
    return f"{int(time.time()) % 10**6:x}{rand}"


# ---------------------------------------------------------------- 加载
def _validate(raw: Any, source: str, index: int) -> Case:
    where = f"{source} 第 {index + 1} 条"

    if not isinstance(raw, dict):
        raise CaseDataError(f"{where}：应为对象，实际 {type(raw).__name__}")

    unknown = set(raw) - ALLOWED_FIELDS
    if unknown:
        raise CaseDataError(
            f"{where}（id={raw.get('id', '?')}）：未知字段 {sorted(unknown)}。"
            f"可用字段：{sorted(ALLOWED_FIELDS)}——拼错的字段会被静默忽略，"
            "等于这条用例没写")

    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        raise CaseDataError(f"{where}：缺少必填字段 {missing}")

    method = str(raw["method"]).upper()
    if method not in ALLOWED_METHODS:
        raise CaseDataError(f"{where}（id={raw['id']}）：method={method} 不在 "
                            f"{sorted(ALLOWED_METHODS)} 内")

    expect = raw["expect"]
    if isinstance(expect, int):
        expect = [expect]
    if not isinstance(expect, list) or not expect:
        raise CaseDataError(f"{where}（id={raw['id']}）：expect 必须是非空列表")
    try:
        expect_tuple = tuple(int(x) for x in expect)
    except (TypeError, ValueError) as exc:
        raise CaseDataError(
            f"{where}（id={raw['id']}）：expect 里有非整数值 {expect}") from exc

    cleanup = raw.get("cleanup")
    if cleanup is not None and cleanup not in ALLOWED_CLEANUP:
        raise CaseDataError(f"{where}（id={raw['id']}）：cleanup={cleanup} 不在 "
                            f"{sorted(ALLOWED_CLEANUP)} 内")

    for field in ("params", "body"):
        value = raw.get(field)
        if value is not None and not isinstance(value, dict):
            raise CaseDataError(
                f"{where}（id={raw['id']}）：{field} 应为对象，实际 "
                f"{type(value).__name__}")

    return Case(
        id=str(raw["id"]),
        desc=str(raw["desc"]),
        method=method,
        path=str(raw["path"]),
        params=raw.get("params"),
        body=raw.get("body"),
        expect=expect_tuple,
        expect_msg=(str(raw["expect_msg"]) if raw.get("expect_msg") else None),
        skip=(str(raw["skip"]) if raw.get("skip") else None),
        cleanup=cleanup,
        source=source,
    )


def load_case_file(name: str | Path, uniq: str | None = None) -> list[Case]:
    """读一个 YAML 用例文件，返回校验过的用例列表。

    `{uniq}` 会在这里替换成同一轮稳定的唯一串：合法命名的用例要建真分支/真标签，
    名字不唯一就会撞上"已存在"，把 201 变成 400，用例天天红。
    """
    path = Path(name)
    if not path.is_absolute():
        path = DATA_DIR / path
    if not path.exists():
        raise CaseDataError(f"用例数据文件不存在：{path}")

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseDataError(f"用例数据文件不是合法 YAML：{path}\n{exc}") from exc

    if not isinstance(doc, dict) or "cases" not in doc:
        raise CaseDataError(
            f"{path.name}：顶层必须是含 `cases:` 列表的对象，实际 "
            f"{type(doc).__name__}")

    raw_cases = doc["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseDataError(f"{path.name}：cases 必须是非空列表")

    token = uniq or _uniq()
    source = path.name
    cases = [
        _validate(_substitute_uniq(raw, token), source, i)
        for i, raw in enumerate(raw_cases)
    ]

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise CaseDataError(f"{source}：用例 id 重复 → {case.id}")
        seen.add(case.id)

    return cases


def _substitute_uniq(raw: Any, token: str) -> Any:
    """把 YAML 里的 {uniq} 换成实际唯一串（递归到 params/body 内部）。"""
    if isinstance(raw, dict):
        return {k: _substitute_uniq(v, token) for k, v in raw.items()}
    if isinstance(raw, list):
        return [_substitute_uniq(v, token) for v in raw]
    if isinstance(raw, str):
        return raw.replace("{uniq}", token)
    return raw


def iter_case_files() -> list[Path]:
    """列出所有 YAML 用例文件，供自检用例遍历。"""
    return sorted(DATA_DIR.glob("*.yaml"))
