from __future__ import annotations

from rag_eval.metrics import (
    answer_match,
    faithfulness_proxy,
    hit_at_k,
    mrr,
    over_refusal,
    recall_at_k,
)
from rag_eval.models import Answer, Case, MetricScore, Passage, PassageRef


def _case(**kwargs) -> Case:
    base = dict(
        id="c1",
        slice="core",
        question="签收后几天内可以无理由退货？",
        expected_answer="自签收之日起 7 天内可以申请无理由退货。",
        expected_passages=[PassageRef("doc_a", "c1")],
    )
    base.update(kwargs)
    return Case(**base)  # type: ignore[arg-type]


def _answer(**kwargs) -> Answer:
    base = dict(
        case_id="c1",
        text="自签收之日起 7 天内可以申请无理由退货。",
        contexts=[Passage("doc_a", "c1", "自签收之日起 7 天内，商品未使用且包装完好，可申请无理由退货。")],
    )
    base.update(kwargs)
    return Answer(**base)  # type: ignore[arg-type]


def test_hit_at_k_respects_cutoff():
    case = _case()
    answer = _answer(contexts=[Passage("doc_x", "z", "无关内容"), Passage("doc_a", "c1", "目标片段")])
    assert hit_at_k(case, answer, k=1) == 0.0
    assert hit_at_k(case, answer, k=2) == 1.0


def test_mrr_rewards_earlier_rank():
    case = _case()
    answer = _answer(contexts=[Passage("doc_x", "z", "无关"), Passage("doc_a", "c1", "目标")])
    assert mrr(case, answer) == 0.5


def test_recall_counts_multi_hop_evidence():
    case = _case(expected_passages=[PassageRef("doc_a", "c1"), PassageRef("doc_a", "c2")])
    answer = _answer(contexts=[Passage("doc_a", "c1", "目标一")])
    assert recall_at_k(case, answer, k=3) == 0.5


def test_answer_match_is_bounded_and_symmetric_enough():
    case = _case()
    answer = _answer()
    score = answer_match(case, answer)
    assert 0.9 <= score <= 1.0
    assert answer_match(case, _answer(text="完全无关的回答")) < 0.2


def test_faithfulness_requires_context():
    case = _case()
    assert faithfulness_proxy(case, _answer(contexts=[])) == 0.0
    assert faithfulness_proxy(case, _answer()) > 0.5
    unsupported = _answer(text="平台额外赠送一年延保并承担全部运费。")
    assert faithfulness_proxy(case, unsupported) < 0.5


def test_over_refusal_is_lower_better():
    case = _case(must_refuse=False)
    assert over_refusal(case, _answer(text="根据现有资料无法确认该问题的答案。")) == 1.0
    score = MetricScore(name="over_refusal_rate", value=0.2, threshold=0.1, direction="lower_better")
    assert score.passed is False
    ok = MetricScore(name="over_refusal_rate", value=0.05, threshold=0.1, direction="lower_better")
    assert ok.passed is True
