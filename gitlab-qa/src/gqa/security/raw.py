"""原始 HTTP 请求发送器：绕过客户端的路径归一化。

为什么需要它——这是本项目实测出来的：

    requests / urllib3 在发送前会把路径整理一遍：
      `/projects/../../etc/passwd`  → `/etc/passwd`（.. 被消解）
      `/projects/%2e%2e%2f...`      → 同样被 unquote 后再消解

    结果就是：**恶意请求根本没到服务端**，用例却"看起来测过了"。
    这不是被测系统安全，是测试工具把载荷吃掉了。

所以路径穿越这类必须用 http.client 直接写请求行，让 `..` 原样上线。
这个模块只服务安全用例，不参与普通接口测试。
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class RawResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    sent_path: str = ""

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").lower()

    def has_file_content(self) -> list[str]:
        """返回命中的系统文件特征。

        /etc/passwd 的第一行是 root:x:0:0:...，只要读到就一定含这些片段。
        """
        markers = ("root:x:0:0", "root:*:0:0", "/bin/bash", "/bin/sh", "daemon:x:")
        return [m for m in markers if m in self.body]


def raw_get(
    base_url: str,
    path: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_bytes: int = 20000,
) -> RawResponse:
    """发一个不做路径归一化的 GET。

    path 必须是「原样上线」的字符串，调用方负责不要提前编码——
    传 `%2e%2e%2f` 就发 `%2e%2e%2f`，传 `../` 就发 `../`。
    """
    u = urlparse(base_url)
    if not u.hostname:
        raise ValueError(f"base_url 无法解析：{base_url!r}")

    if u.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            u.hostname, u.port or 443, timeout=timeout
        )
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)

    try:
        # skip_accept_encoding 让我们拿到未压缩的正文，便于直接断言
        conn.putrequest("GET", path, skip_accept_encoding=True)
        conn.putheader("User-Agent", "gitlab-qa-security/0.1")
        conn.putheader("Connection", "close")
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        conn.endheaders()

        resp = conn.getresponse()
        body = resp.read(max_bytes).decode("utf-8", errors="replace")
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return RawResponse(status=resp.status, headers=hdrs, body=body, sent_path=path)
    finally:
        conn.close()
