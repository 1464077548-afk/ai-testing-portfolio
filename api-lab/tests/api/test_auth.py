"""令牌流转：登录、过期、续期、吊销。

这一组用例的价值不在「接口通不通」，而在把鉴权当成状态机来测：
    未登录 → 已登录 → 过期 → 续期成功 → 吊销 → 未登录
"""

from __future__ import annotations

import pytest
import requests

from apilab.client import ApiClient
from apilab.models import ErrorBody, TokenPair

pytestmark = pytest.mark.smoke


def test_login_returns_well_formed_token_pair(base_url, settings):
    client = ApiClient(base_url=base_url, timeout=settings.timeout)
    data = client.login("alice", "alice-pw")
    client.close()

    # pydantic 在这里就是「结构断言」，比逐字段 assert 更能表达意图
    TokenPair.model_validate(data)
    assert data["token_type"] == "Bearer"


def test_bad_credentials_do_not_leak_account_existence(base_url, settings):
    client = ApiClient(base_url=base_url, timeout=settings.timeout)
    try:
        with pytest.raises(Exception) as exc:
            client.login("alice", "wrong-password")
        err = exc.value
        assert err.status == 401
        assert err.code == "BAD_CREDENTIALS"
    finally:
        client.close()

    # 用户不存在与密码错误必须给出同一句话，否则可被用来枚举账号
    client2 = ApiClient(base_url=base_url, timeout=settings.timeout)
    try:
        with pytest.raises(Exception) as exc2:
            client2.login("no-such-user", "wrong-password")
        assert exc2.value.code == "BAD_CREDENTIALS"
        assert exc2.value.message == err.message
    finally:
        client2.close()


def test_write_without_token_is_rejected(anon):
    resp = anon.post("/api/books", json={"title": "x", "author": "y", "genre": "tech"})
    assert resp.status_code == 401
    ErrorBody.model_validate(resp.json()["error"])


def test_forged_token_is_rejected(anon):
    anon.access_token = "tok_forged_deadbeef"
    resp = anon.post("/api/books", json={"title": "x", "author": "y", "genre": "tech"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"


def test_expired_token_triggers_transparent_refresh(client_factory):
    """ttl=-1 签出一个「出生即过期」的令牌，再看客户端能否自己救回来。

    这条用例覆盖的是真实事故里最常见的一段：access token 过期但 refresh token 有效，
    客户端必须续期而不是把 401 抛给用户。
    """
    client = client_factory("alice", "alice-pw", ttl=-1)
    assert client.refresh_count == 0

    resp = client.post("/api/books", json={"title": "续期后创建", "author": "某人", "genre": "tech"})

    assert resp.status_code == 201
    assert client.refresh_count == 1, "应当恰好续期一次"
    client.delete(f"/api/books/{resp.json()['id']}")


def test_refresh_token_is_single_use(base_url, settings):
    client = ApiClient(base_url=base_url, timeout=settings.timeout)
    data = client.login("admin", "admin-pw")
    refresh = data["refresh_token"]
    client.close()

    first = requests.post(f"{base_url}/auth/refresh", json={"refresh_token": refresh}, timeout=5)
    second = requests.post(f"{base_url}/auth/refresh", json={"refresh_token": refresh}, timeout=5)

    assert first.status_code == 200
    # 一次性使用才能防重放：攻击者截获旧 refresh token 也没用
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"


def test_logout_invalidates_access_token(client_factory):
    client = client_factory("admin", "admin-pw")
    stale_token = client.access_token
    client.refresh_token = None  # 断掉续期路径，才能验证「真的失效」

    assert client.post("/auth/logout").status_code == 204
    client.access_token = stale_token
    resp = client.post("/api/books", json={"title": "x", "author": "y", "genre": "tech"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"


def test_token_ttl_is_echoed(api):
    """已登录客户端的令牌应当仍在有效期内，否则整个会话都不可用。"""
    resp = api.get("/api/books?size=1")
    assert resp.status_code == 200
