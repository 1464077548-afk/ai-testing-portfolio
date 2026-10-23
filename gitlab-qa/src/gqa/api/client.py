"""GitLab REST API 客户端。

企业级接口客户端和普通脚本的区别，全在这几个地方：
1. 限流：GitLab 会返回 429 + Retry-After，必须遵守，否则账号被封；
2. 分页：列表接口走 X-Next-Page 头，不能想当然地只取第一页；
3. 重试只对幂等请求生效——GET 可以重试，POST 重试可能产生重复资源；
4. 请求日志要留下来，失败时贴进 Allure 报告，而不是让人去翻控制台。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlencode

import requests


@dataclass
class GitLabError(Exception):
    """把 GitLab 的错误响应归一化，测试里断言 code 而不是裸状态码。"""

    status: int
    message: str
    method: str = ""
    url: str = ""
    body: str = ""

    def __str__(self) -> str:
        return f"[{self.method} {self.status}] {self.message}"


@dataclass
class Response:
    """轻量包装，保留状态码、头与解析后的 JSON。"""

    status: int
    headers: dict[str, str]
    json_data: Any
    elapsed_ms: int
    url: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def __getitem__(self, key: str) -> Any:
        return self.json_data[key]


@dataclass
class RequestLog:
    """供报告附件使用：失败时把请求/响应原文贴进 Allure。"""

    entries: list[dict] = field(default_factory=list)

    def add(self, method: str, url: str, status: int, ms: int,
            req_body: Any = None, resp_body: Any = None) -> None:
        self.entries.append({
            "method": method,
            "url": url,
            "status": status,
            "ms": ms,
            "request": req_body,
            "response": resp_body,
        })

    def dump(self) -> str:
        return json.dumps(self.entries, ensure_ascii=False, indent=2)[:20000]


class GitLabClient:
    RETRY_STATUS = {429, 500, 502, 503, 504}
    IDEMPOTENT = {"GET", "HEAD", "OPTIONS"}

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 20.0,
        retries: int = 2,
        backoff: float = 0.5,
    ) -> None:
        self.api_url = f"{base_url.rstrip('/')}/api/v4"
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = requests.Session()
        self.log = RequestLog()
        # 限流观察窗口：记录 429 次数，超过阈值说明测试本身在压测服务
        self.rate_limited = 0

    # ---------------------------------------------------------------- 基础
    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "gitlab-qa/0.1"}
        if self.token:
            h["PRIVATE-TOKEN"] = self.token
        return h

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        expect: int | tuple[int, ...] | None = None,
    ) -> Response:
        url = path if path.startswith("http") else f"{self.api_url}/{path.lstrip('/')}"

        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                raw = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=self._headers(), timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.retries:
                    raise GitLabError(0, f"网络异常：{type(exc).__name__}", method, url)
                time.sleep(self.backoff * (2**attempt))
                continue

            ms = int((time.perf_counter() - started) * 1000)
            try:
                parsed = raw.json() if raw.content else None
            except ValueError:
                parsed = raw.text[:500]

            self.log.add(method, url, raw.status_code, ms, json_body, parsed)

            # 429：必须遵守 Retry-After，否则 GitLab 会把你踢进黑名单
            if raw.status_code == 429 and attempt < self.retries:
                self.rate_limited += 1
                wait = float(raw.headers.get("Retry-After") or self.backoff * (2**attempt))
                time.sleep(min(wait, 10))
                continue

            if (
                raw.status_code in self.RETRY_STATUS
                and attempt < self.retries
                and method.upper() in self.IDEMPOTENT
            ):
                time.sleep(self.backoff * (2**attempt))
                continue

            resp = Response(
                status=raw.status_code,
                headers=dict(raw.headers),
                json_data=parsed,
                elapsed_ms=ms,
                url=url,
            )
            if expect is not None:
                allowed = (expect,) if isinstance(expect, int) else tuple(expect)
                if raw.status_code not in allowed:
                    raise GitLabError(
                        raw.status_code,
                        self._message(parsed),
                        method, url,
                        json.dumps(parsed, ensure_ascii=False)[:800],
                    )
            return resp
        raise RuntimeError("unreachable")

    @staticmethod
    def _message(parsed: Any) -> str:
        if isinstance(parsed, dict):
            if "message" in parsed:
                msg = parsed["message"]
                return msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
            if "error" in parsed:
                return str(parsed["error"])
        return str(parsed)[:200]

    # ---------------------------------------------------------------- 便捷
    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> Response:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw) -> Response:
        return self.request("DELETE", path, **kw)

    # ---------------------------------------------------------------- 分页
    def paginate(
        self,
        path: str,
        params: dict | None = None,
        max_pages: int = 20,
    ) -> list[dict]:
        """按 X-Next-Page 逐页取完。

        很多「列表少了几条」的假失败，根因就是只取了第一页。
        """
        out: list[dict] = []
        page = 1
        query = dict(params or {})
        query.setdefault("per_page", 100)
        while page <= max_pages:
            query["page"] = page
            resp = self.request("GET", path, params=query, expect=200)
            data = resp.json_data
            if not isinstance(data, list):
                break
            out.extend(data)
            nxt = resp.headers.get("X-Next-Page") or ""
            if not nxt:
                break
            page = int(nxt)
        return out

    def iter_page_headers(self, path: str, params: dict | None = None) -> dict[str, str]:
        """只取分页头，用于断言 X-Total / X-Per-Page 等元信息，不取正文。"""
        query = dict(params or {})
        query.setdefault("per_page", 20)
        resp = self.request("GET", path, params=query, expect=200)
        return {
            k: v for k, v in resp.headers.items()
            if k in {"X-Total", "X-Total-Pages", "X-Per-Page", "X-Page", "X-Next-Page", "X-Prev-Page"}
        }

    def close(self) -> None:
        self.session.close()
