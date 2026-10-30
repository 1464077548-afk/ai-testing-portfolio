"""OAuth2（Doorkeeper）令牌生命周期：授权码 → 令牌 → 刷新 → 撤销。

为什么单独成模块而不是塞进 resources.py：

    这几个端点**不在 /api/v4 前缀下**（/oauth/authorize、/oauth/token、
    /oauth/revoke），GitLabClient 会自动拼 api 前缀，用它会拼出
    /api/v4/oauth/token 这种不存在的路径。而且这一路要维护 cookie、
    CSRF、授权同意表单，不是一次请求能完成的——它是「流程」不是「资源」。

本实例的实测事实（GitLab 19.0.1，写死在注释里以免后人凭印象改）：

1. `POST /api/v4/applications` 的 `scopes` **必须是空格分隔字符串**，
   传数组会 400 "scopes is invalid"。
2. 建应用返回的是 `application_id`（不是 `uid`），它才是 OAuth 的 client_id。
3. `grant_type=password` 已被废弃，返回 `unsupported_grant_type`。
   想拿 refresh_token 只能走 authorization_code。
4. 非 trusted 应用访问 /oauth/authorize 会先返回 200 的**同意表单**，
   需要 POST 一次 `do=authorize` 才会 302 带 code。
5. access_token 的 `expires_in` 是 **7200**（2 小时），且刷新后仍是 7200
   ——是重置不是累加。
6. 刷新后：**旧 access_token 立即 401**，**旧 refresh_token 二次兑换
   400 invalid_grant**（即 refresh token 是一次性的、可轮换的）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from gqa.api.client import GitLabClient


@dataclass
class OAuthToken:
    """一次令牌发放的结果。

    status 保留下来：负向路径（错误密钥、已撤销令牌）要靠它断言，
    只返回「成功或抛异常」会让用例没法验证「拒绝得对不对」。
    """

    status: int
    access_token: str = ""
    refresh_token: str = ""
    expires_in: int | None = None
    token_type: str = ""
    scope: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.access_token)


@dataclass
class OAuthApp:
    """一个用完即删的 OAuth 应用。"""

    id: int
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    confidential: bool = True


class OAuthFlow:
    """把「建应用 → 登录 → 同意 → 换令牌 → 刷新 → 撤销」串成可断言的对象。

    设计取舍：
    - 每个步骤都返回带 status 的对象，不用异常表达失败。负向用例要断言
      「被拒绝了」，异常混进来会让用例写成 try/except 而丢掉状态码细节。
    - 创建的令牌全部登记在 `_issued`，`close()` 时统一撤销，避免每轮
      测试在实例上堆一批有效令牌——那是最难查的一类脏数据。
    """

    SIGN_IN = "/users/sign_in"
    SIGN_OUT = "/users/sign_out"

    def __init__(self, base_url: str, admin_user: str, admin_password: str,
                 admin_token: str = "", timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.admin_token = admin_token
        self.timeout = timeout
        self.session = requests.Session()
        self._apps: list[int] = []
        self._issued: list[str] = []
        self.trace: list[str] = []

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """删除已建应用、撤销已发令牌。失败静默：清理不该让用例变红。"""
        for app_id in list(self._apps):
            self.delete_app(app_id)
        self._apps.clear()
        self._issued.clear()

    def _admin_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.admin_token} if self.admin_token else {}

    def delete_app(self, app_id: int) -> int:
        """删除 OAuth 应用。管理员端点，必须有 token 才删得掉。"""
        try:
            r = requests.delete(f"{self.base_url}/api/v4/applications/{app_id}",
                                headers=self._admin_headers(), timeout=self.timeout)
        except requests.RequestException:  # noqa: BLE001
            return 0
        if app_id in self._apps:
            self._apps.remove(app_id)
        self._note(f"删应用 #{app_id} → {r.status_code}")
        return r.status_code

    def __enter__(self) -> OAuthFlow:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _note(self, msg: str) -> None:
        self.trace.append(msg)

    # ------------------------------------------------------------ 应用
    def create_app(self, api: GitLabClient, name: str, redirect_uri: str,
                   scopes: str = "api read_user") -> OAuthApp:
        """建应用。scopes 传字符串是硬约束，见模块注释第 1 条。"""
        resp = api.post("/applications", json_body={
            "name": name,
            "redirect_uri": redirect_uri,
            "scopes": scopes,
        }, expect=(201, 400))
        body = resp.json_data or {}
        if resp.status != 201:
            raise RuntimeError(f"建 OAuth 应用失败：{resp.status} {body}")

        app = OAuthApp(
            id=body["id"],
            client_id=body["application_id"],
            client_secret=body["secret"],
            redirect_uri=redirect_uri,
            scopes=scopes,
            confidential=body.get("confidential", True),
        )
        self._apps.append(app.id)
        self._note(f"建应用 #{app.id} scopes={scopes}")
        return app

    # ------------------------------------------------------------ 登录
    def login(self) -> bool:
        """建立 UI 会话。只在这条链路上用 cookie，不走 API Token。"""
        r = self.session.get(f"{self.base_url}{self.SIGN_IN}", timeout=self.timeout)
        csrf = self._csrf(r.text or "")
        if not csrf:
            self._note("登录页未取到 CSRF")
            return False
        r = self.session.post(
            f"{self.base_url}{self.SIGN_IN}", timeout=self.timeout,
            allow_redirects=True,
            data={"authenticity_token": csrf, "user[login]": self.admin_user,
                  "user[password]": self.admin_password},
        )
        ok = any(c.name == "_gitlab_session" for c in self.session.cookies)
        self._note(f"登录 status={r.status_code} session={ok}")
        return ok

    @staticmethod
    def _csrf(html: str) -> str:
        m = re.search(r'name="authenticity_token" value="([^"]+)"', html)
        return m.group(1) if m else ""

    # ------------------------------------------------------------ 授权码
    def authorize(self, app: OAuthApp, scope: str = "api read_user") -> str:
        """走完同意页，取回 code。

        两种形态都要覆盖：trusted 应用直接 302；普通应用先给 200 表单。
        只看其中一种会在换实例配置时静默拿到空 code。
        """
        url = (f"{self.base_url}/oauth/authorize?client_id={app.client_id}"
               f"&redirect_uri={app.redirect_uri}&response_type=code"
               f"&scope={scope.replace(' ', '+')}")
        r = self.session.get(url, timeout=self.timeout, allow_redirects=False)

        loc = r.headers.get("Location", "")
        code = self._code_from(loc)
        if code:
            self._note(f"授权直接 302 带 code（trusted 应用）")
            return code

        csrf = self._csrf(r.text or "")
        if not csrf:
            self._note(f"授权页既没跳转也没表单：status={r.status_code}")
            return ""
        r = self.session.post(url, timeout=self.timeout, allow_redirects=False, data={
            "authenticity_token": csrf, "do": "authorize",
            "client_id": app.client_id, "redirect_uri": app.redirect_uri,
            "response_type": "code", "scope": scope, "state": "",
        })
        code = self._code_from(r.headers.get("Location", ""))
        self._note(f"同意后 status={r.status_code} code={'有' if code else '无'}")
        return code

    @staticmethod
    def _code_from(location: str) -> str:
        if not location:
            return ""
        return (parse_qs(urlparse(location).query).get("code") or [""])[0]

    # ------------------------------------------------------------ 令牌
    def exchange(self, app: OAuthApp, code: str) -> OAuthToken:
        return self._token_request({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": app.redirect_uri, "client_id": app.client_id,
            "client_secret": app.client_secret,
        })

    def refresh(self, app: OAuthApp, refresh_token: str,
                client_secret: str | None = None) -> OAuthToken:
        """用 refresh_token 换一对新令牌。

        client_secret 可替换成错的，用来验证「凭据不对时刷新必须失败」。
        """
        return self._token_request({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": app.client_id,
            "client_secret": client_secret or app.client_secret,
        })

    def _token_request(self, payload: dict[str, str]) -> OAuthToken:
        try:
            r = requests.post(f"{self.base_url}/oauth/token", data=payload,
                              timeout=self.timeout)
        except requests.RequestException as exc:  # noqa: BLE001
            return OAuthToken(status=0, error=type(exc).__name__)

        body: dict[str, Any] = {}
        if (r.headers.get("content-type") or "").find("json") >= 0:
            try:
                parsed = r.json()
                body = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                body = {}

        tok = OAuthToken(
            status=r.status_code,
            access_token=body.get("access_token", ""),
            refresh_token=body.get("refresh_token", ""),
            expires_in=body.get("expires_in"),
            token_type=body.get("token_type", ""),
            scope=body.get("scope", ""),
            error=str(body.get("error", "")),
            raw=body,
        )
        if tok.access_token:
            self._issued.append(tok.access_token)
        self._note(f"token 请求 {payload.get('grant_type')} → {r.status_code}"
                   f"{' ' + tok.error if tok.error else ''}")
        return tok

    # ------------------------------------------------------------ 撤销
    def revoke(self, app: OAuthApp, token: str) -> int:
        try:
            r = requests.post(f"{self.base_url}/oauth/revoke", timeout=self.timeout,
                              data={"client_id": app.client_id,
                                    "client_secret": app.client_secret,
                                    "token": token})
        except requests.RequestException:  # noqa: BLE001
            return 0
        self._note(f"revoke → {r.status_code}")
        return r.status_code

    # ------------------------------------------------------------ 探针
    def api_status(self, access_token: str) -> int:
        """用 Bearer 令牌打 /user，返回 HTTP 状态码。

        这是状态机里唯一需要反复问的问题：「这个令牌现在还算数吗」。
        """
        try:
            r = requests.get(f"{self.base_url}/api/v4/user", timeout=self.timeout,
                             headers={"Authorization": f"Bearer {access_token}"})
        except requests.RequestException:  # noqa: BLE001
            return 0
        return r.status_code


def wait_a_second() -> None:
    """令牌在同一秒签发时可能完全相同，刷新前后对比会假失败。

    Doorkeeper 的令牌值由随机串生成，理论上不会撞，但 `expires_in` 与
    时间戳相关；隔开 1 秒能让「刷新后令牌确实换了」这个断言稳定。
    """
    time.sleep(1.1)
