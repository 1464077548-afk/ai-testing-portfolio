"""被测系统的客户端封装。

这一层是接口测试里最容易被跳过、但最影响可维护性的地方。
测试用例只表达「断言什么」，传输细节（重试、鉴权、超时、错误归一化）全部收敛在这里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass
class ApiError(Exception):
    """把 HTTP 错误归一化成异常，测试里可以断言 code 而不是裸 status。"""

    status: int
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    url: str = ""

    def __str__(self) -> str:
        return f"[{self.status} {self.code}] {self.message} ({self.url})"


class ApiClient:
    """带令牌自动续期的会话。

    重试只针对 5xx 与网络异常——4xx 重试除了浪费配额没有任何意义，
    这是新手最常见的一个错误。
    """

    RETRY_STATUS = {502, 503, 504}

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        retries: int = 2,
        backoff: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = requests.Session()
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.username: str | None = None
        self.refresh_count = 0  # 供用例断言「确实走了续期而不是重新登录」

    # --- 认证 -------------------------------------------------------------
    def login(self, username: str, password: str, ttl: int = 3600) -> dict:
        resp = self.session.post(
            f"{self.base_url}/auth/login",
            json={"username": username, "password": password, "ttl": ttl},
            timeout=self.timeout,
        )
        data = self._unwrap(resp)
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.username = username
        return data

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        resp = self.session.post(
            f"{self.base_url}/auth/refresh",
            json={"refresh_token": self.refresh_token},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.refresh_count += 1
        return True

    # --- 请求 -------------------------------------------------------------
    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        for attempt in range(self.retries + 1):
            headers = dict(kwargs.pop("headers", {}) or {})
            if self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            try:
                resp = self.session.request(method, url, headers=headers, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= self.retries:
                    raise
                time.sleep(self.backoff * (2**attempt))
                continue

            if resp.status_code == 401 and self.refresh_token and self._refresh():
                # 只续期一次；续期后仍 401 说明是真的没权限，直接返回给用例
                headers["Authorization"] = f"Bearer {self.access_token}"
                resp = self.session.request(method, url, headers=headers, **kwargs)
                return resp

            if resp.status_code in self.RETRY_STATUS and attempt < self.retries:
                time.sleep(self.backoff * (2**attempt))
                continue
            return resp
        raise RuntimeError("unreachable")

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> requests.Response:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> requests.Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> requests.Response:
        return self.request("DELETE", path, **kw)

    # --- 便捷断言辅助 -----------------------------------------------------
    @staticmethod
    def _unwrap(resp: requests.Response) -> dict:
        if resp.status_code >= 400:
            body: dict = {}
            try:
                body = resp.json()
            except ValueError:
                pass
            err = body.get("error") or {}
            raise ApiError(
                status=resp.status_code,
                code=err.get("code", "UNKNOWN"),
                message=err.get("message", resp.text[:200]),
                details=err,
                url=resp.url,
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def json(self, path: str, **kw: Any) -> dict:
        return self._unwrap(self.get(path, **kw))

    def close(self) -> None:
        self.session.close()
