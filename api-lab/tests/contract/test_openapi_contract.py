"""以 OpenAPI 为单一事实源的契约测试。

和 tests/api/test_contract.py 的分工：
- 那边用手写的 schemas（客户端期望）
- 这边用服务端发布的 /openapi.json（服务端承诺）
两个独立来源互相印证，才叫契约测试。只维护一份，等于自己跟自己对账。
"""

from __future__ import annotations

import pytest

from apilab import contracts

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def spec(base_url) -> dict:
    import requests

    resp = requests.get(f"{base_url}/openapi.json", timeout=5)
    assert resp.status_code == 200
    return resp.json()


def _resolve(spec: dict, ref: str) -> dict:
    """只支持本文件用到的 #/components/... 局部引用，够用即可。"""
    assert ref.startswith("#/")
    node: dict = spec
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _deref(spec: dict, node, depth: int = 0):
    """把 $ref 就地展开成真实 schema。

    jsonschema 默认不认 OpenAPI 的局部引用，不展开就会抛 ReferencingError，
    看到的是「测试报错」而不是「契约不符」，非常误导。
    """
    if depth > 16:
        return node
    if isinstance(node, dict):
        if "$ref" in node:
            return _deref(spec, _resolve(spec, node["$ref"]), depth + 1)
        return {k: _deref(spec, v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(spec, v, depth + 1) for v in node]
    return node


def _schema_of(spec: dict, operation: dict, status: str) -> dict | None:
    resp = operation.get("responses", {}).get(status)
    if not resp:
        return None
    content = resp.get("content") or {}
    payload = content.get("application/json") or {}
    schema = payload.get("schema") or {}
    return _deref(spec, schema) if schema else None


def test_spec_is_valid_openapi_shape(spec):
    assert spec["openapi"].startswith("3.")
    assert "paths" in spec and spec["paths"], "没有声明任何路径，契约是空的"


def test_every_operation_declares_responses(spec):
    """没写 responses 的接口，等于没有契约。"""
    missing = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method in {"parameters", "$ref"}:
                continue
            if not operation.get("responses"):
                missing.append(f"{method.upper()} {path}")
    assert missing == [], f"缺少 responses 声明：{missing}"


def test_documented_paths_match_reality(spec):
    """文档里写的路径集合必须和实际可用的路径一致。

    这里用「快照」而不是「动态发现」——动态发现只能证明文档覆盖了实现，
    证明不了实现没有偷偷多出接口。
    """
    documented = set(spec["paths"])
    assert documented == {"/api/books", "/api/books/{book_id}"}


def test_live_response_conforms_to_openapi(clean_library, api, spec):
    op = spec["paths"]["/api/books"]["get"]
    schema = _schema_of(spec, op, "200")
    assert schema is not None

    body = api.json("/api/books?page=1&size=10")
    errors = contracts.collect_errors(body, schema)
    assert errors == [], "实际响应不符合 OpenAPI 承诺：\n" + "\n".join(errors)


def test_error_response_conforms_to_openapi(api, spec):
    op = spec["paths"]["/api/books/{book_id}"]["get"]
    schema = _schema_of(spec, op, "404")
    assert schema is not None

    body = api.get("/api/books/bk_absent").json()
    assert contracts.collect_errors(body, schema) == []


def test_local_contract_covers_all_documented_fields(spec):
    """本地 schema 不能比服务端承诺少字段。

    少字段意味着「服务端加了字段，测试端不知道」——这类漏检在下游解析时才爆。
    """
    documented = set(_resolve(spec, "#/components/schemas/Book")["properties"])
    local = set(contracts.BOOK["properties"])
    assert documented - local == set(), f"本地契约缺字段：{documented - local}"
