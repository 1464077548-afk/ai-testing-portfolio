"""鉴权状态机用例的夹具。

为什么单独放一层而不是塞进根 conftest.py：

    根 conftest 只管「所有人都用得到的东西」（settings / gl / data_factory）。
    鉴权这一组夹具自带**重量级副作用**——会造令牌、建 OAuth 应用、开 UI 会话，
    把它们挂到全局会让每个不需要它们的模块都背上创建成本与失败面。

两条纪律：
1. **造出来就登记，用例结束必回收**。状态机用例刻意制造「已过期/已撤销」
   的凭据，这些东西留在实例上既污染列表接口，也会让清理脚本分不清敌我。
2. **缺凭据就 skip，不降级成 pass**。OAuth 这一组要管理员密码，
   没配就是没测到，报告上必须显示 skipped 而不是一片绿。
"""

from __future__ import annotations

import time

import pytest

from gqa.api.credentials import TokenFactory, WebSession
from gqa.api.oauth import OAuthApp, OAuthFlow, OAuthToken

REDIRECT_URI = "http://localhost:9999/oauth-callback"
OAUTH_SCOPES = "api read_user"


def _attach(text: str, name: str) -> None:
    try:
        import allure
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001  报告附件失败不影响用例结论
        pass


@pytest.fixture
def token_factory(gl, settings):
    """个人访问令牌工厂：造、查、销，结束统一回收。"""
    factory = TokenFactory(gl.client, settings.base_url, timeout=settings.timeout)
    yield factory
    factory.cleanup()
    _attach(factory.report(), "令牌操作记录")


@pytest.fixture
def web_session(settings):
    """HTTP 层的浏览器会话，用于登录/登出语义。"""
    with WebSession(settings.base_url, timeout=settings.timeout) as ws:
        yield ws
        _attach(ws.report(), "会话操作记录")


@pytest.fixture(scope="session")
def oauth_flow(settings):
    """已登录的 OAuth 流程对象。

    session 级：登录（拿 CSRF + 提交表单）这一步最慢且无状态，
    每条用例重做一遍纯属浪费。令牌与应用的隔离由下面两个夹具保证，
    不会因为这个 session 级对象而产生耦合。
    """
    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD，无法走 OAuth 授权流程")

    flow = OAuthFlow(
        base_url=settings.base_url,
        admin_user=settings.admin_user,
        admin_password=settings.admin_password,
        admin_token=settings.token,
        timeout=settings.timeout,
    )
    if not flow.login():
        flow.close()
        pytest.skip("管理员账号无法建立 UI 会话，OAuth 流程不可用")
    yield flow
    flow.close()


@pytest.fixture
def oauth_app(oauth_flow: OAuthFlow, gl, settings):
    """一条用例一个独立 OAuth 应用。用完即删。"""
    name = f"{settings.resource_prefix}-oauth-{int(time.time() * 1000) % 10**8}"
    app = oauth_flow.create_app(gl.client, name=name, redirect_uri=REDIRECT_URI,
                                scopes=OAUTH_SCOPES)
    yield app
    oauth_flow.delete_app(app.id)


@pytest.fixture
def oauth_token(oauth_flow: OAuthFlow, oauth_app: OAuthApp) -> OAuthToken:
    """走完授权码流程拿到一对全新令牌（含 refresh_token）。

    每条用例独立走一遍，是因为这组用例断言的正是「同一个 refresh_token
    能不能用第二次」——共享令牌会让上一条用例的结论污染下一条。
    """
    code = oauth_flow.authorize(oauth_app, scope=OAUTH_SCOPES)
    if not code:
        pytest.fail(f"未取得授权码，流程轨迹：{oauth_flow.trace}")
    tok = oauth_flow.exchange(oauth_app, code)
    if not tok.ok:
        pytest.fail(f"换取令牌失败：HTTP {tok.status} error={tok.error}")
    yield tok
    _attach("\n".join(oauth_flow.trace), "OAuth 流程轨迹")
