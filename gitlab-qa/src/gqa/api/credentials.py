"""凭据工厂与 Web 会话：让「令牌/会话的当前状态」变成可断言的对象。

状态机用例的核心难点不是发请求，而是**随时能问出「这个凭据现在算不算数」**。
这两个类就是把这个问题封装成一个方法：

    TokenFactory.probe(token) -> 401 / 200
    WebSession.api_status()   -> 401 / 200

本实例实测（GitLab 19.0.1）：

1. 个人访问令牌 **必须带 expires_at**；不传时实例自动补一年（2027-10-28 实测）。
   也就是说「永不过期的令牌」在本实例上创建不出来。
2. **过期日当天就失效**：expires_at=今天创建的令牌 `active=false`、
   调 API 直接 401。判定是「过期日当天起不可用」，而不是「当天结束后才失效」。
3. 撤销令牌返回 204，撤销后访问 401；**重复撤销仍 204**（幂等）。
    4. UI 登出是 **POST /users/sign_out → 204**，不是 DELETE（DELETE 实测 404，
   GET 只是 301 重定向）。登出后 API 立即 401，再登出一次仍 204。
5. **不要用「有没有 _gitlab_session cookie」判断登录成功**：密码错误时
   Devise 同样会下发这个 cookie（要装 flash 与 CSRF），实测失败路径的
   cookie 列表是 ['_gitlab_session', 'preferred_language']，成功路径多一个
   'known_sign_in'。真正的判据是 API 状态码（401 vs 200）或 known_sign_in。
   用 cookie 是否存在做断言会得到一条漂亮的假绿。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests

from gqa.api.client import GitLabClient


@dataclass
class IssuedToken:
    """一张刚造出来的个人访问令牌。"""

    status: int
    id: int = 0
    token: str = ""
    name: str = ""
    scopes: list[str] = field(default_factory=list)  #权限范围
    expires_at: str | None = None  # 过期日
    active: bool = False   # 令牌是否有效
    revoked: bool = False  # 已撤销

    @property
    def created(self) -> bool:
        return self.status in (200, 201) and bool(self.token)


class TokenFactory:
    """造令牌、查状态、撤销，并且**全部登记**以便统一回收。

    为什么必须登记：状态机用例会造出「已过期」「已撤销」这类刻意无效的令牌，
    它们在实例上永远留着，既污染列表接口，也会让下一轮的清理脚本分不清
    哪些是测试数据。用 __enter__/__exit__ 或显式 cleanup() 兜底。
    """

    def __init__(self, api: GitLabClient, base_url: str, timeout: float = 20.0) -> None:
        self.api = api
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._issued: list[IssuedToken] = []
        self.trace: list[str] = []
        self._uid: int | None = None

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/api/v4"

    def __enter__(self) -> TokenFactory:
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # ------------------------------------------------------------ 造
    def current_user_id(self) -> int | None:
        """当前管理员的 user id。造令牌必须用 /users/:id/... 这条路径——
        实例上没有 POST /personal_access_tokens 这个「当前用户」简写。"""
        if self._uid is None:
            resp = self.api.get("/user", expect=(200, 401))
            body = resp.json_data if isinstance(resp.json_data, dict) else {}
            self._uid = body.get("id")
        return self._uid

    def issue(self, name: str, scopes: list[str] | None = None,
              expires_at: str | None = None) -> IssuedToken:
        """给当前用户创建令牌。expires_at 是 'YYYY-MM-DD' 字符串或 None。

        注意：本实例对过期日只认**日期粒度**，想造「已过期」就传今天或更早。
        """
        uid = self.current_user_id()
        if not uid:
            self.trace.append("拿不到 user id，无法造令牌")
            return IssuedToken(status=0, name=name)
        tok = self.issue_for(uid, name, scopes, expires_at)
        self.trace.append(
            f"issue({name}, expires_at={expires_at}) → {tok.status} "
            f"active={tok.active} expires_at={tok.expires_at}"
        )
        return tok

    def issue_for(self, user_id: int, name: str, scopes: list[str] | None = None,
                  expires_at: str | None = None) -> IssuedToken:
        """给指定用户造令牌。多数场景用 issue()（当前用户）即可。"""
        payload: dict[str, Any] = {"name": name, "scopes": scopes or ["read_user"]}
        if expires_at:
            payload["expires_at"] = expires_at
        resp = self.api.request(
            "POST", f"/users/{user_id}/personal_access_tokens", json_body=payload,
            expect=None)
        body = resp.json_data if isinstance(resp.json_data, dict) else {}
        tok = IssuedToken(
            status=resp.status, id=body.get("id", 0), token=body.get("token", ""),
            name=body.get("name", name), scopes=body.get("scopes", []) or [],
            expires_at=body.get("expires_at"), active=bool(body.get("active", False)),
            revoked=bool(body.get("revoked", False)),
        )
        if tok.id:
            self._issued.append(tok)
        return tok

    # ------------------------------------------------------------ 查
    def probe(self, token_value: str) -> int:
        """这张令牌现在能不能调通 API。返回 HTTP 状态码，0 表示网络异常。"""
        try:
            r = requests.get(f"{self.api_url}/user", timeout=self.timeout,
                             headers={"PRIVATE-TOKEN": token_value})
        except requests.RequestException:  # noqa: BLE001
            return 0
        self.trace.append(f"probe → {r.status_code}")
        return r.status_code

    def is_usable(self, token_value: str) -> bool:
        return self.probe(token_value) == 200

    # ------------------------------------------------------------ 销
    def revoke(self, token_id: int) -> int:
        resp = self.api.request("DELETE", f"/personal_access_tokens/{token_id}",
                                expect=None)
        self.trace.append(f"revoke(#{token_id}) → {resp.status}")
        return resp.status

    def cleanup(self) -> None:
        for tok in self._issued:
            if tok.id:
                try:
                    self.revoke(tok.id)
                except Exception:  # noqa: BLE001  清理失败不该让用例变红
                    pass
        self._issued.clear()

    def report(self) -> str:
        return "\n".join(self.trace) or "（无操作）"


class WebSession:
    """HTTP 层的浏览器会话：登录页拿 CSRF、登录、登出、用 cookie 打 API。

    与 UI 层的 LoginPage（Playwright）职责不同：这里要的是**可断言的状态码**，
    不是渲染结果。状态机关心「会话是否还活着」，用 requests 更直接也更快。
    """

    SIGN_IN = "/users/sign_in"
    SIGN_OUT = "/users/sign_out"
    SESSION_COOKIE = "_gitlab_session"
    # 「登录成功」的判据不是 _gitlab_session —— 见类注释第 5 条
    KNOWN_SIGN_IN_COOKIE = "known_sign_in"

    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.trace: list[str] = []

    def __enter__(self) -> WebSession:
        return self

    def __exit__(self, *exc) -> None:
        self.session.close()

    @staticmethod
    def _csrf(html: str) -> str:
        m = re.search(r'name="authenticity_token" value="([^"]+)"', html)
        return m.group(1) if m else ""

    @property
    def has_session_cookie(self) -> bool:
        """注意：这个为真**不代表登录成功**，失败路径也有。见类注释第 5 条。"""
        return any(c.name == self.SESSION_COOKIE for c in self.session.cookies)

    @property
    def is_authenticated(self) -> bool:
        """登录成功的判据之一：GitLab 只在认证通过后才下发这个 cookie。"""
        return any(c.name == self.KNOWN_SIGN_IN_COOKIE for c in self.session.cookies)

    def login(self, user: str, password: str) -> int:
        r = self.session.get(f"{self.base_url}{self.SIGN_IN}", timeout=self.timeout)
        csrf = self._csrf(r.text or "")
        if not csrf:
            self.trace.append(f"登录页无 CSRF：status={r.status_code}")
            return r.status_code
        r = self.session.post(
            f"{self.base_url}{self.SIGN_IN}", timeout=self.timeout,
            allow_redirects=True,
            data={"authenticity_token": csrf, "user[login]": user,
                  "user[password]": password},
        )
        self.trace.append(f"login({user}) → {r.status_code} "
                          f"cookie={self.has_session_cookie}")
        return r.status_code

    def logout(self) -> int:
        """实测：POST /users/sign_out → 204。DELETE 是 404，GET 只重定向。"""
        try:
            r = self.session.post(f"{self.base_url}{self.SIGN_OUT}",
                                  timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:  # noqa: BLE001
            return 0
        self.trace.append(f"logout → {r.status_code}")
        return r.status_code

    def api_status(self) -> int:
        """用当前 cookie 打受保护的 API。401 才是「会话真的销毁了」。"""
        try:
            r = self.session.get(f"{self.base_url}/api/v4/user", timeout=self.timeout)
        except requests.RequestException:  # noqa: BLE001
            return 0
        self.trace.append(f"session api → {r.status_code}")
        return r.status_code

    def root_lands_on_login(self) -> bool:
        """登出后访问首页应当被踢回登录页。"""
        try:
            r = self.session.get(f"{self.base_url}/", timeout=self.timeout)
        except requests.RequestException:  # noqa: BLE001
            return False
        return "sign_in" in (r.url or "")

    def report(self) -> str:
        return "\n".join(self.trace) or "（无操作）"


def day_offset(days: int) -> str:
    """相对今天的日期字符串，用来造「昨天 / 今天 / 明天」到期。"""
    return (date.today() + __import__("datetime").timedelta(days=days)).isoformat()
