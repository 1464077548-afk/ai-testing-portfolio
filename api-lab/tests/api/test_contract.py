"""契约测试：验证「服务端承诺的结构」和「实际返回的结构」一致。

和业务用例的区别：
业务用例断言「这条数据对不对」，契约用例断言「这个接口还长不长得像它承诺的样子」。
后者才能在字段类型变更时拦住下游解析崩溃。
"""

from __future__ import annotations

import pytest

from apilab import contracts
from apilab.models import Book, BookPage

pytestmark = [pytest.mark.contract, pytest.mark.regression]


def test_list_response_matches_schema(clean_library, api):
    body = api.json("/api/books?page=1&size=10")

    errors = contracts.collect_errors(body, contracts.BOOK_PAGE)
    assert errors == [], f"列表契约不符：\n" + "\n".join(errors)


def test_item_response_matches_schema(clean_library, api):
    body = api.json("/api/books/bk_001")
    contracts.assert_matches(body, contracts.BOOK)


def test_pydantic_and_jsonschema_agree(clean_library, api):
    """两套校验器必须给出同一结论。

    只用一个容易自欺：pydantic 模型是本地随手写的，schema 来自契约文档，
    两者不一致时，通常是本地模型偷偷放宽了标准。
    """
    page = api.json("/api/books?page=1&size=5")
    for raw in page["items"]:
        Book.model_validate(raw)
        assert contracts.collect_errors(raw, contracts.BOOK) == []


def test_contract_detects_field_type_drift(clean_library, api, lab_server):
    """故意让上游把 published_year 从 int 改成 str，校验器必须报警。

    这条用例是本文件的核心：它证明「契约测试真的有效」，而不只是摆设。
    没有这条，你永远不知道自己写的 schema 是不是宽松到什么都拦不住。
    """
    if lab_server is None:
        pytest.skip("外部靶场无法注入字段漂移")

    before = lab_server.state.schema_drift
    api.post("/__control/drift", json={"enabled": True})
    try:
        body = api.json("/api/books/bk_001")
        assert isinstance(body["published_year"], str), "靶机未按预期漂移"

        errors = contracts.collect_errors(body, contracts.BOOK)
        assert errors, "字段类型漂移竟然没被检出，说明 schema 写松了"
        assert any("published_year" in e for e in errors)
        assert any("is not of type" in e for e in errors)
    finally:
        # 恢复现场，而不是硬关：否则 --schema-drift 跑全量时，
        # 这条用例会把后面用例的漂移一起关掉，红绿混杂难以解释
        api.post("/__control/drift", json={"enabled": before})


def test_error_responses_share_one_shape(api):
    """错误体是最容易被各接口写歪的地方，必须统一。"""
    samples = [
        api.get("/api/books/bk_missing"),
        api.get("/api/books?size=999"),
    ]
    for resp in samples:
        assert resp.status_code >= 400
        contracts.assert_matches(resp.json(), contracts.ERROR)
