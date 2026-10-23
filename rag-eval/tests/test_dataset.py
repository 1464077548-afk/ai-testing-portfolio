from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_eval.dataset import DatasetError, load_cases, validate_case
from rag_eval.models import Case, PassageRef


def test_valid_case_passes_validation():
    case = Case(
        id="core_1",
        slice="core",
        question="问题",
        expected_answer="答案",
        expected_passages=[PassageRef("doc", "c1")],
    )
    validate_case(case)


def test_core_case_requires_golden_passages():
    case = Case(id="core_2", slice="core", question="问题", expected_answer="答案")
    with pytest.raises(DatasetError, match="expected_passages"):
        validate_case(case)


def test_refusal_case_must_declare_intent():
    case = Case(id="ref_1", slice="refusal", question="问题")
    with pytest.raises(DatasetError, match="must_refuse"):
        validate_case(case)


def test_bad_id_is_rejected():
    case = Case(id="Core-1", slice="core", question="问题", expected_passages=[PassageRef("d", "c")])
    with pytest.raises(DatasetError, match="id"):
        validate_case(case)


def test_duplicate_ids_are_rejected(tmp_path: Path):
    row = json.dumps(
        {
            "id": "dup_1",
            "slice": "core",
            "question": "问题",
            "expected_answer": "答案",
            "expected_passages": [{"doc_id": "d", "chunk_id": "c"}],
        },
        ensure_ascii=False,
    )
    path = tmp_path / "dup.jsonl"
    path.write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="重复"):
        load_cases([path])


def test_slice_filter_applies(tmp_path: Path):
    rows = [
        {"id": "a_1", "slice": "core", "question": "q", "expected_passages": [{"doc_id": "d", "chunk_id": "c"}]},
        {"id": "b_1", "slice": "refusal", "question": "q", "must_refuse": True},
    ]
    path = tmp_path / "mix.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    assert len(load_cases([path], include_slices=["core"])) == 1
