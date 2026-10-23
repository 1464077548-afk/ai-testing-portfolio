"""参数化：三种用法，对应三种真实场景。

1. 直接参数化      → 同一逻辑、不同输入（边界值表）
2. indirect 参数化 → 参数不是给用例的，是给 fixture 的（改前置条件）
3. 数据驱动        → 用例写进 JSON，产品同学也能加（见 tests/data/books_create.json）
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------
# 1. 直接参数化：输入 → 预期
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_code"),
    [
        ({"title": "正常", "author": "甲", "genre": "tech"}, 201, None),
        ({"title": "缺作者", "genre": "tech"}, 400, "VALIDATION_ERROR"),
        ({"author": "乙", "genre": "tech"}, 400, "VALIDATION_ERROR"),
        ({"title": "类型不对", "author": "丙", "genre": "unknown"}, 400, "VALIDATION_ERROR"),
    ],
    ids=["ok", "missing-author", "missing-title", "bad-genre"],
)
def test_create_validation_matrix(api, payload, expected_status, expected_code):
    resp = api.post("/api/books", json=payload)
    assert resp.status_code == expected_status
    if expected_code:
        assert resp.json()["error"]["code"] == expected_code
    else:
        api.delete(f"/api/books/{resp.json()['id']}")


# --------------------------------------------------------------------------
# 2. indirect：参数喂给 fixture，改的是「环境」而不是「入参」
# --------------------------------------------------------------------------
@pytest.fixture
def client_with_ttl(request, client_factory):
    return client_factory("alice", "alice-pw", ttl=request.param)


@pytest.mark.parametrize(
    ("client_with_ttl", "expect_refresh"),
    [(3600, 0), (-1, 1)],
    indirect=["client_with_ttl"],
    ids=["valid-token", "expired-token"],
)
def test_refresh_happens_only_when_needed(client_with_ttl, expect_refresh):
    resp = client_with_ttl.post(
        "/api/books", json={"title": "续期矩阵", "author": "某人", "genre": "tech"}
    )

    assert resp.status_code == 201
    assert client_with_ttl.refresh_count == expect_refresh
    client_with_ttl.delete(f"/api/books/{resp.json()['id']}")


# --------------------------------------------------------------------------
# 3. 数据驱动：参数来自 JSON（fixture 在 tests/conftest.py 的 pytest_generate_tests）
# --------------------------------------------------------------------------
def test_create_cases_from_json(api, book_case):
    resp = api.post("/api/books", json=book_case["payload"])

    assert resp.status_code == book_case["expect_status"], resp.text
    if book_case["expect_code"]:
        assert resp.json()["error"]["code"] == book_case["expect_code"]
    else:
        api.delete(f"/api/books/{resp.json()['id']}")
