"""评测集加载与校验。

校验比指标更重要：脏 case 会让指标看起来正常，却毫无意义。
所有校验都在加载期失败，绝不进入执行阶段。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import VALID_SLICES, Case, PassageRef

_ID_PATTERN = re.compile(r"^[a-z0-9_\-]+$")


class DatasetError(ValueError):
    """评测集格式错误。带上文件名与行号，方便定位。"""


def _parse_case(raw: dict, where: str) -> Case:
    if not isinstance(raw, dict):
        raise DatasetError(f"{where}: 每行必须是一个 JSON 对象")
    try:
        case_id = str(raw["id"])
        slice_name = str(raw["slice"])
        question = str(raw["question"])
    except KeyError as exc:
        raise DatasetError(f"{where}: 缺少必填字段 {exc.args[0]}") from exc

    passages = [
        PassageRef(doc_id=str(p["doc_id"]), chunk_id=p.get("chunk_id"))
        for p in raw.get("expected_passages", [])
    ]
    return Case(
        id=case_id,
        slice=slice_name,  # type: ignore[arg-type]
        question=question,
        expected_answer=raw.get("expected_answer"),
        expected_passages=passages,
        must_refuse=bool(raw.get("must_refuse", False)),
        tags=[str(t) for t in raw.get("tags", [])],
    )


def validate_case(case: Case, where: str = "") -> None:
    prefix = f"{where}: " if where else ""
    if not _ID_PATTERN.match(case.id):
        raise DatasetError(f"{prefix}id 只能包含小写字母、数字、下划线与短横线：{case.id}")
    if case.slice not in VALID_SLICES:
        raise DatasetError(f"{prefix}未知 slice：{case.slice}，可选 {VALID_SLICES}")
    if not case.question.strip():
        raise DatasetError(f"{prefix}question 不能为空")

    # core / boundary 必须给出黄金证据，否则检索指标无从计算
    if case.slice in ("core", "boundary") and not case.expected_passages:
        raise DatasetError(f"{prefix}{case.slice} 用例必须提供 expected_passages")
    # refusal 必须声明 must_refuse，否则行为指标口径会错
    if case.slice == "refusal" and not case.must_refuse:
        raise DatasetError(f"{prefix}refusal 用例必须设置 must_refuse=true")
    # must_refuse 的用例不应再给参考答案
    if case.must_refuse and case.expected_answer:
        raise DatasetError(f"{prefix}must_refuse=true 时不应提供 expected_answer")


def load_cases(paths: list[Path], include_slices: list[str] | None = None) -> list[Case]:
    cases: list[Case] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            raise DatasetError(f"评测集不存在：{path}")
        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                where = f"{path.name}:{lineno}"
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DatasetError(f"{where}: JSON 解析失败（{exc.msg}）") from exc
                case = _parse_case(raw, where)
                validate_case(case, where)
                if case.id in seen:
                    raise DatasetError(f"{where}: id 重复（{case.id}）")
                seen.add(case.id)
                if include_slices and case.slice not in include_slices:
                    continue
                cases.append(case)

    if not cases:
        raise DatasetError("筛选后没有可用用例，检查 suite 配置")
    return cases
