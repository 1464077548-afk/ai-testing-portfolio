"""鉴权状态机：登录 → 过期 → 透明续期 → refresh 一次性 → 登出。

这组用例不是按接口清单写的，而是先在本实例（GitLab CE 19.0.1）上逐条探测，
再把探测到的真实行为固化成断言。探测过程中有四处与「常见认知」相反，
每一处都在对应用例的注释里写明了，改动前请先重跑探测：

1. **过期日当天就失效**。直觉上 expires_at=今天应该「今天还能用」，
   实测 active=false、调 API 直接 401。判定是过期日当天零点起不可用。
2. **登出是 POST /users/sign_out**（204），不是 DELETE——DELETE 实测 404，
   GET 只是 301 重定向，两者都能让用例「看起来测过」而实际没登出。
3. **refresh_token 确实是一次性的**，但失败码是 **400 invalid_grant**
   而不是 401，且新 refresh_token 立刻可用（轮换链条延续）。
4. **password grant 已被废弃**（unsupported_grant_type）。要拿 refresh_token
   只能走 authorization_code，而它必须过一遍浏览器同意页。

状态图（每条用例对应一条边或一次守卫）：

    匿名 ──登录成功──> 已认证 ──过期日到达──> 已过期 ──访问──> 401（不可自愈）
      │                  │
      │                  ├──刷新──> 新已认证（旧令牌全部作废，refresh 一次性）
    登录失败              │
      └──> 仍是匿名       └──登出──> 已登出 ──访问──> 401 ──刷新──> 拒绝
"""

from __future__ import annotations

import subprocess
import time

import allure
import pytest

from gqa.api.credentials import day_offset
from gqa.api.oauth import wait_a_second

pytestmark = [
    pytest.mark.api,
    pytest.mark.regression,
    # 会造令牌、建 OAuth 应用：prod 环境自动跳过
    pytest.mark.destructive,
    # 缺管理员 Token 时整组 skip，绝不降级成 pass
    pytest.mark.requires_token,
]

CONTAINER = "gitlab-ce"


# ==========================================================================
# 辅助
# ==========================================================================
def _rails(script: str, timeout: int = 150) -> str | None:
    """在 GitLab 容器里跑一段 rails runner，读服务端视角的令牌状态。

    返回 None 表示这条路走不通（没装 docker / 容器名不对 / 脚本报错），
    调用方据此 skip——**不猜、不伪造结果**。
    """
    try:
        proc = subprocess.run(
            ["docker", "exec", CONTAINER, "gitlab-rails", "runner", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


# ==========================================================================
# 一、登录：匿名 → 已认证
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("登录成功：新令牌立即处于已认证态")
@allure.severity(allure.severity_level.BLOCKER)
def test_login_issues_token_in_active_state(token_factory):
    """登录后进入 ACTIVE：令牌 active=true 且能调通 API。

    只断言「创建成功」是不够的——创建返回 201 但令牌 active=false
    的情况在本实例上真实存在（见过期边界用例），所以这里两个都查。
    """
    tok = token_factory.issue("qa-sm-active", scopes=["read_user"],
                              expires_at=day_offset(1))
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"

    with allure.step("服务端标记该令牌为有效"):
        assert tok.active, (
            f"新令牌 active={tok.active}（expires_at={tok.expires_at}）——"
            f"签发即失效说明过期判定写反了"
        )

    with allure.step("用该令牌访问受保护接口应放行"):
        assert token_factory.probe(tok.token) == 200, "新令牌无法调通 API"


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("登录失败：错误密码不产生会话")
@allure.severity(allure.severity_level.CRITICAL)
def test_login_with_wrong_password_stays_anonymous(web_session, settings):
    """错误密码之后必须仍是匿名：认证态不成立，API 仍是 401。

    这里踩过一个真实的坑，值得写下来：**不能拿「有没有会话 cookie」当判据**。
    本实例在密码错误时照样下发 `_gitlab_session`（这是 Devise 的正常行为：失败后重定向回登录页，会话 cookie 用于存 flash 消息/CSRF），
    按 cookie 断言会得到一条漂亮的假绿。实测区分点是：

        失败：cookies=[_gitlab_session, preferred_language]，API 401
        成功：多一个 known_sign_in，API 200

    所以这里断言认证态（known_sign_in）与 API 结果，不断言 cookie 存在与否。
    """
    status = web_session.login(settings.admin_user, "definitely-wrong-pwd-9527")

    with allure.step("不得进入已认证态"):
        assert not web_session.is_authenticated, (
            f"错误密码却进入已认证状态（登录 HTTP {status}）——"
            f"认证态判据用 known_sign_in cookie，不要用 _gitlab_session"
        )

    with allure.step("受保护接口仍然拒绝"):
        assert web_session.api_status() in (401, 403), (
            f"登录失败后 API 返回 {web_session.api_status()}，说明匿名未被拦住"
        )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("登录成功：密码正确建立可调用 API 的会话")
@allure.severity(allure.severity_level.BLOCKER)
def test_login_with_valid_password_establishes_session(web_session, settings):
    """UI 会话与 API Token 是两条独立的认证通道，这里验证前者。

    与失败路径配成一对，两条用同一组判据（known_sign_in + API 状态码），
    才说明「这个判据真的能区分成功与失败」——只写成功侧的话，
    拿 cookie 是否存在断言也能过，那就等于没测。
    """
    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD，无法验证会话登录")

    web_session.login(settings.admin_user, settings.admin_password)

    with allure.step("进入已认证态"):
        assert web_session.is_authenticated, "正确密码却没有进入已认证状态"

    with allure.step("该会话可直接调通 API"):
        assert web_session.api_status() == 200, (
            f"已登录会话调 API 得到 {web_session.api_status()}"
        )


# ==========================================================================
# 二、过期：已认证 → 已过期
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("过期日边界：到期当天即失效")
@allure.severity(allure.severity_level.BLOCKER)
def test_token_expiring_today_is_already_dead(token_factory):
    """反直觉边界：expires_at=今天创建的令牌，当天就不可用。

    直觉是「今天到期 = 今天还能用到 24:00」，实测不是：
    本实例把「过期日 <= 今天」都算作已过期，创建出来 active 直接是 false。

    这条最容易被人「顺手改对」——把它改成期待 200 会让用例永远绿，
    但真实用户会在到期日当天发现自己提前掉线。
    """
    tok = token_factory.issue("qa-sm-today", scopes=["read_user"],
                              expires_at=day_offset(0))
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"

    with allure.step("服务端判定为已失效"):
        assert not tok.active, (
            f"过期日为今天（{tok.expires_at}）却被判为 active=true——"
            f"确认实例是否改了过期判定口径（<= 今天 还是 < 今天）"
        )

    with allure.step("访问被拒"):
        assert token_factory.probe(tok.token) == 401, (
            f"当天到期的令牌返回 {token_factory.probe(tok.token)}，预期 401"
        )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("过期日边界：明天到期仍然有效")
@allure.severity(allure.severity_level.BLOCKER)
def test_token_expiring_tomorrow_is_active(token_factory):
    """与上面那条配成一对，把边界夹住。

    单测「今天失效」是不够的：如果实现改成「一律失效」，那条也会通过。
    必须有反向样本证明「没到期的确实能用」。
    """
    tok = token_factory.issue("qa-sm-tomorrow", scopes=["read_user"],
                              expires_at=day_offset(1))
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"

    assert tok.active, f"明天才到期的令牌却被判为失效（expires_at={tok.expires_at}）"
    assert token_factory.probe(tok.token) == 200, "未到期令牌应当可用"


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("已过期令牌被拒绝")
@allure.severity(allure.severity_level.CRITICAL)
def test_expired_token_is_rejected(token_factory):
    """昨天到期的令牌一律 401。"""
    tok = token_factory.issue("qa-sm-expired", scopes=["read_user"],
                              expires_at=day_offset(-1))
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"
    assert not tok.active, "过期令牌仍被判为 active"

    assert token_factory.probe(tok.token) == 401, (
        f"过期令牌返回 {token_factory.probe(tok.token)}，预期 401"
    )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("已过期令牌不能自愈")
@allure.severity(allure.severity_level.CRITICAL)
def test_expired_token_cannot_self_heal(token_factory):
    """连续访问多次，过期令牌不得「用着用着又活了」。

    能抓住一类真实缺陷：缓存里还留着旧的认证结果，第一次请求 401，
    后续请求走了缓存副本反而 200——这种抖动比一直 401 更难查。
    """
    tok = token_factory.issue("qa-sm-noheal", scopes=["read_user"],
                              expires_at=day_offset(-1))
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"

    seen = [token_factory.probe(tok.token) for _ in range(3)]
    allure.attach(str(seen), name="连续三次探测结果",
                  attachment_type=allure.attachment_type.TEXT)

    assert all(s == 401 for s in seen), (
        f"过期令牌的访问结果不一致：{seen}——出现非 401 说明存在缓存自愈"
    )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("实例强制令牌必须带过期日")
@allure.severity(allure.severity_level.NORMAL)
def test_instance_enforces_expiry_on_new_tokens(token_factory):
    """不传 expires_at 时，实例必须补一个过期日，而不是发永不过期的令牌。

    实测本实例自动补一年（2027-10-28）。允许永不过期的实例等于
    把「过期」这条边从状态机上摘掉了——后面的过期用例会全部失去意义，
    所以这条是整组用例的前置守卫。
    """
    tok = token_factory.issue("qa-sm-noexpiry", scopes=["read_user"])
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"

    assert tok.expires_at, (
        "未传 expires_at 却拿到了没有过期日的令牌——"
        "实例允许永不过期凭据，鉴权状态机缺少「过期」态"
    )
    allure.attach(f"实例自动补的过期日：{tok.expires_at}", name="过期策略",
                  attachment_type=allure.attachment_type.TEXT)


# ==========================================================================
# 三、透明续期：这条边在本实例上不存在，要断言它不存在
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("访问令牌的 TTL 是固定的")
@allure.severity(allure.severity_level.NORMAL)
def test_access_token_ttl_is_declared(oauth_token):
    """签发的 access_token 必须声明有限寿命，不能是「永久」。

    没有 expires_in 就等于声称永不过期，那么刷新机制毫无必要，
    整套一次性 refresh 的设计也失去前提。
    """
    assert oauth_token.expires_in, (
        f"access_token 未声明 expires_in（raw={oauth_token.raw}）——"
        f"永不过期的令牌会让 refresh 机制形同虚设"
    )
    allure.attach(f"expires_in={oauth_token.expires_in}s", name="令牌寿命",
                  attachment_type=allure.attachment_type.TEXT)


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("刷新是重置 TTL，不是叠加")
@allure.severity(allure.severity_level.NORMAL)
def test_refresh_resets_ttl_rather_than_extends(oauth_flow, oauth_app, oauth_token):
    """刷新后新令牌的 TTL 必须回到初始值，而不是在原基础上累加。

    累加式实现会让频繁刷新的客户端攒出一个无限长的会话，
    等于用合法手段绕过了过期策略。
    """
    baseline = oauth_token.expires_in
    assert baseline, "前一条用例已保证有 expires_in"

    wait_a_second()
    refreshed = oauth_flow.refresh(oauth_app, oauth_token.refresh_token)
    assert refreshed.ok, f"刷新失败：HTTP {refreshed.status} {refreshed.error}"

    with allure.step("对比两次签发的 TTL"):
        allure.attach(f"首次={baseline}s 刷新后={refreshed.expires_in}s",
                      name="TTL 对比", attachment_type=allure.attachment_type.TEXT)
        assert refreshed.expires_in == baseline, (
            f"刷新后 TTL 变成 {refreshed.expires_in}s（首次 {baseline}s）——"
            f"不是重置而是{'累加' if (refreshed.expires_in or 0) > baseline else '缩短'}"
        )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("使用令牌不会自动延长寿命（无透明续期）")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.slow
def test_using_token_does_not_extend_its_life(oauth_flow, oauth_app, oauth_token):
    """从服务端记录看：令牌被使用不会推后它的过期时间。

    这是「透明续期」这条边的直接验证。GitLab 没有滑动续期，
    所以这里断言的是**不变**——如果哪天有人在实例上开启了滑动续期，
    这条会变红，提醒回来重新设计刷新策略的用例。

    依赖 docker exec 读服务端记录；读不到就 skip，不用猜的数据蒙混过关。
    """
    script = (
        f"t=Doorkeeper::AccessToken.where(application_id: {oauth_app.id})"
        f".order(:created_at).last; puts [t&.expires_in, t&.created_at.to_i].join('|')"
    )
    before = _rails(script)
    if before is None:
        pytest.skip(
            "读不到服务端令牌记录（需要本机 docker 与容器 gitlab-ce），无法验证滑动续期"
        )

    with allure.step("用该令牌连续访问 API"):
        for _ in range(3):
            oauth_flow.api_status(oauth_token.access_token)
            time.sleep(1)

    after = _rails(script)
    allure.attach(f"使用前={before}\n使用后={after}", name="服务端令牌记录",
                  attachment_type=allure.attachment_type.TEXT)

    assert after is not None, "第二次读取失败，无法比对"
    assert before == after, (
        f"令牌被使用后服务端记录发生了变化：{before} → {after}。"
        f"如果实例启用了滑动续期，本状态机的「过期」用例需要重新设计"
    )


# ==========================================================================
# 四、refresh 一次性
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("刷新签发新令牌并立即作废旧令牌")
@allure.severity(allure.severity_level.BLOCKER)
def test_refresh_issues_new_token_and_kills_old(oauth_flow, oauth_app, oauth_token):
    """刷新后：新 access_token 可用，旧 access_token 立刻失效。

    只断言「新令牌能用」是常见漏法——旧令牌如果还活着，
    就等于一次刷新变成了令牌复制，轮换机制形同虚设。
    """
    old = oauth_token.access_token
    wait_a_second()
    refreshed = oauth_flow.refresh(oauth_app, oauth_token.refresh_token)
    assert refreshed.ok, f"刷新失败：HTTP {refreshed.status} {refreshed.error}"

    with allure.step("新令牌必须换值"):
        assert refreshed.access_token != old, "刷新后 access_token 没变，等于没刷新"

    with allure.step("新令牌可用"):
        assert oauth_flow.api_status(refreshed.access_token) == 200, "新令牌不可用"

    with allure.step("旧令牌立即失效"):
        status = oauth_flow.api_status(old)
        assert status == 401, (
            f"刷新后旧令牌仍返回 {status}——令牌被复制而非轮换"
        )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("refresh_token 一次性：二次兑换被拒")
@allure.severity(allure.severity_level.BLOCKER)
def test_refresh_token_is_single_use(oauth_flow, oauth_app, oauth_token):
    """核心断言：同一个 refresh_token 不能用第二次。

    实测本实例返回 400 + invalid_grant（不是 401）。
    接受 400/401 两种：实现换成 401 时不应该让这条用例变红，
    但**绝不接受 200**——那意味着被截获的 refresh_token 可以无限续期，
    相当于长期有效的口令。
    """
    rt = oauth_token.refresh_token
    first = oauth_flow.refresh(oauth_app, rt)
    assert first.ok, f"首次刷新失败：HTTP {first.status} {first.error}"

    second = oauth_flow.refresh(oauth_app, rt)
    allure.attach(f"首次={first.status} 二次={second.status} error={second.error}",
                  name="两次兑换结果", attachment_type=allure.attachment_type.TEXT)

    assert second.status in (400, 401), (
        f"同一个 refresh_token 第二次兑换返回 {second.status}——"
        f"refresh_token 可重复使用，被截获后能无限续期（严重缺陷）"
    )
    assert not second.access_token, "被拒绝的兑换不该返回新令牌"


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("轮换链条延续：新的 refresh_token 可用")
@allure.severity(allure.severity_level.CRITICAL)
def test_refresh_rotation_chain_continues(oauth_flow, oauth_app, oauth_token):
    """一次性的前提是「给你一个新的」，否则用户会被永久登出。

    与上一条配成一对：只测一次性会漏掉「轮换后链条断了」；
    只测链条会漏掉「旧令牌还能用」。
    """
    refreshed = oauth_flow.refresh(oauth_app, oauth_token.refresh_token)
    assert refreshed.ok, f"刷新失败：HTTP {refreshed.status} {refreshed.error}"

    with allure.step("必须同时下发新的 refresh_token"):
        assert refreshed.refresh_token, "刷新没有返回新的 refresh_token，链条会断"
        assert refreshed.refresh_token != oauth_token.refresh_token, (
            "新旧 refresh_token 相同，无法构成轮换"
        )

    with allure.step("新的 refresh_token 可继续兑换"):
        nxt = oauth_flow.refresh(oauth_app, refreshed.refresh_token)
        assert nxt.ok, f"新 refresh_token 不可用：HTTP {nxt.status} {nxt.error}"


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("错误 client_secret 无法刷新")
@allure.severity(allure.severity_level.CRITICAL)
def test_refresh_with_wrong_secret_is_rejected(oauth_flow, oauth_app, oauth_token):
    """光有 refresh_token 不够，还得出示应用密钥。

    实测返回 401。这条拦的是「refresh_token 泄露即可独立续期」——
    双因子里少一个因子，泄露的影响面会成倍放大。
    """
    resp = oauth_flow.refresh(oauth_app, oauth_token.refresh_token,
                              client_secret="definitely-wrong-secret")
    assert resp.status in (400, 401), (
        f"错误的 client_secret 仍刷新成功（HTTP {resp.status}）"
    )
    assert not resp.access_token, "拒绝路径不该下发令牌"


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("会话已终结后不得续出新令牌")
@allure.severity(allure.severity_level.BLOCKER)
def test_refresh_after_revoke_is_rejected(oauth_flow, oauth_app, oauth_token):
    """登出之后再拿 refresh_token 来刷，必须拒绝。

    这是「登出」这条边真正的分量：如果撤销只作废 access_token 而
    refresh_token 还能用，登出就是一个可被绕过的假动作。
    """
    revoke_status = oauth_flow.revoke(oauth_app, oauth_token.access_token)
    allure.attach(f"revoke → {revoke_status}", name="撤销结果",
                  attachment_type=allure.attachment_type.TEXT)

    resp = oauth_flow.refresh(oauth_app, oauth_token.refresh_token)
    allure.attach(f"撤销后刷新 → {resp.status} {resp.error}", name="刷新结果",
                  attachment_type=allure.attachment_type.TEXT)

    assert not resp.ok, (
        f"撤销之后仍然刷新成功（HTTP {resp.status}）——"
        f"登出可被绕过，refresh_token 未随会话终结而失效"
    )


# ==========================================================================
# 五、登出
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("UI 登出销毁会话")
@allure.severity(allure.severity_level.BLOCKER)
def test_ui_logout_destroys_session(web_session, settings):
    """登出后会话立即失效——用 API 状态码验证，而不是看页面跳没跳。

    实测：POST /users/sign_out → 204，之后 /api/v4/user → 401。
    用 DELETE 会拿到 404、用 GET 只拿到 301，两者都不会真正登出，
    但都能让「我调用了登出接口」这句话成立。
    """
    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD，无法验证 UI 登出")

    web_session.login(settings.admin_user, settings.admin_password)
    assert web_session.api_status() == 200, "前置条件：登出前会话可用"

    status = web_session.logout()
    assert status == 204, f"登出返回 {status}，预期 204（本实例用 POST 登出）"

    assert web_session.api_status() == 401, (
        f"登出后 API 仍返回 {web_session.api_status()}——会话没有被真正销毁"
    )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("登出幂等")
@allure.severity(allure.severity_level.NORMAL)
def test_logout_is_idempotent(web_session, settings):
    """重复登出必须稳定返回成功，不能报错。

    前端常在多个标签页各发一次登出；服务端若对「已登出」报 500，
    用户就会看到莫名的错误页。
    """
    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD")

    web_session.login(settings.admin_user, settings.admin_password)
    first = web_session.logout()
    second = web_session.logout()

    allure.attach(f"第一次={first} 第二次={second}", name="两次登出",
                  attachment_type=allure.attachment_type.TEXT)
    assert first == 204, f"首次登出返回 {first}"
    assert second == 204, f"重复登出返回 {second}，幂等性不成立"


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("登出后被踢回登录页")
@allure.severity(allure.severity_level.NORMAL)
def test_logout_redirects_to_login(web_session, settings):
    """服务端状态之外再看一眼用户可见结果：首页不再展示已登录内容。"""
    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD")

    web_session.login(settings.admin_user, settings.admin_password)
    web_session.logout()

    assert web_session.root_lands_on_login(), (
        "登出后访问首页没有被引导到登录页——用户会看到一个「已登录」的残影"
    )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("撤销个人令牌即登出")
@allure.severity(allure.severity_level.BLOCKER)
def test_revoke_personal_token_ends_session(token_factory):
    """令牌撤销后必须立刻不可用——这是 API 侧「登出」的等价动作。"""
    tok = token_factory.issue("qa-sm-revoke", scopes=["read_user"],
                              expires_at=day_offset(1))
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"
    assert token_factory.probe(tok.token) == 200, "前置条件：撤销前可用"

    status = token_factory.revoke(tok.id)
    assert status == 204, f"撤销返回 {status}，预期 204"

    assert token_factory.probe(tok.token) == 401, (
        f"撤销后仍返回 {token_factory.probe(tok.token)}——撤销没有真正生效"
    )


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("撤销幂等")
@allure.severity(allure.severity_level.NORMAL)
def test_revoke_personal_token_is_idempotent(token_factory):
    """重复撤销同一张令牌应当稳定成功，而不是报「不存在」。"""
    tok = token_factory.issue("qa-sm-revoke2", scopes=["read_user"],
                              expires_at=day_offset(1))
    assert tok.created, f"令牌创建失败：HTTP {tok.status}"

    first = token_factory.revoke(tok.id)
    second = token_factory.revoke(tok.id)
    allure.attach(f"第一次={first} 第二次={second}", name="两次撤销",
                  attachment_type=allure.attachment_type.TEXT)

    assert first == 204, f"首次撤销返回 {first}"
    assert second == 204, f"重复撤销返回 {second}，幂等性不成立"


@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("OAuth 撤销即登出")
@allure.severity(allure.severity_level.BLOCKER)
def test_oauth_revoke_ends_session(oauth_flow, oauth_app, oauth_token):
    """RFC 7009 撤销：成功返回 200，之后令牌立刻不可用。"""
    assert oauth_flow.api_status(oauth_token.access_token) == 200, "前置条件：撤销前可用"

    status = oauth_flow.revoke(oauth_app, oauth_token.access_token)
    assert status == 200, f"撤销返回 {status}，预期 200"

    assert oauth_flow.api_status(oauth_token.access_token) == 401, (
        f"撤销后令牌仍返回 {oauth_flow.api_status(oauth_token.access_token)}"
    )


# ==========================================================================
# 六、状态对外不可区分
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("鉴权状态机")
@allure.story("过期与撤销对客户端不可区分")
@allure.severity(allure.severity_level.NORMAL)
def test_expired_and_revoked_look_the_same(token_factory):
    """同样是 401，且不透露是哪一种原因。

    如果「令牌不存在」返回 404、「已过期」返回 401，攻击者就能
    枚举出哪些令牌 ID 真实存在。对外必须收敛成同一个结论。
    """
    expired = token_factory.issue("qa-sm-cmp-expired", scopes=["read_user"],
                                  expires_at=day_offset(-1))
    revoked = token_factory.issue("qa-sm-cmp-revoked", scopes=["read_user"],
                                  expires_at=day_offset(1))
    assert expired.created and revoked.created, "前置条件：两张令牌都要造出来"

    token_factory.revoke(revoked.id)

    s_expired = token_factory.probe(expired.token)
    s_revoked = token_factory.probe(revoked.token)
    allure.attach(f"过期={s_expired} 已撤销={s_revoked}", name="两种无效态对比",
                  attachment_type=allure.attachment_type.TEXT)

    assert s_expired == s_revoked == 401, (
        f"过期({s_expired}) 与已撤销({s_revoked}) 返回不同状态码——"
        f"对外暴露了凭据的内部状态"
    )
