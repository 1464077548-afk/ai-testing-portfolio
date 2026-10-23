from __future__ import annotations

import pytest

from rag_eval.models import Answer, Case, PassageRef
from rag_eval.runner import run
from rag_eval.targets import MockTarget

TEXT = "自签收之日起 7 天内，商品未使用且包装完好，可申请无理由退货。"
CORPUS = {
    ("policy_return_v3", "c011"): TEXT,
    ("policy_shipping_v2", "c022"): "单笔订单满 99 元包邮，未满 99 元收取 10 元运费。",
}


def _case() -> Case:
    return Case(
        id="core_return",
        slice="core",
        question="签收后几天内可以无理由退货？",
        expected_answer=TEXT,
        expected_passages=[PassageRef("policy_return_v3", "c011")],
    )


class BrokenTarget:
    name = "broken"
    version = "0.0.0"

    def health(self) -> bool:
        return True

    def ask(self, case: Case) -> Answer:
        return Answer(case_id=case.id, text="", error="ConnectError: timeout")


class DeadTarget(BrokenTarget):
    def health(self) -> bool:
        return False


def test_end_to_end_passes_on_mock():
    result = run([_case()], MockTarget(CORPUS, top_k=2), k=2, concurrency=1)
    assert result.passed, result.failed_metrics
    assert result.summary["hit@2"]["mean"] == 1.0
    assert result.summary["answer_match"]["mean"] > 0.9
    assert result.errors == 0


def test_collection_errors_are_counted_separately():
    result = run([_case()], BrokenTarget(), k=2, concurrency=1, retries=0)
    assert result.errors == 1
    assert result.records[0].scores == []
    assert result.passed is False


def test_unhealthy_target_is_refused():
    with pytest.raises(RuntimeError, match="健康检查"):
        run([_case()], DeadTarget(), k=2)


def test_slice_breakdown_is_reported():
    refusal = Case(id="ref_1", slice="refusal", question="支持花呗分期吗？", must_refuse=True)
    result = run([_case(), refusal], MockTarget(CORPUS, top_k=2), k=2, concurrency=2)
    assert set(result.by_slice) == {"core", "refusal"}
    assert result.by_slice["refusal"]["refusal_accuracy"]["mean"] == 1.0
