"""最小 HTTP 传输层：只用标准库 urllib。

v0 不引 httpx 的原因：适配层要能在"只装了 Python"的机器上跑通，
否则 CI 与新人上手都要先解决依赖问题。生产环境要连接池与 HTTP/2 时，
把这个文件换成 httpx 实现即可，上层适配器不需要改。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from .base import TransportError

_BODY_PREVIEW = 500


def _parse(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:_BODY_PREVIEW]}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _send(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), _parse(response.read())
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也带业务体，交给适配器分类，不在这里吞掉
        return int(exc.code), _parse(exc.read())
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise TransportError(f"网络不可达：{exc}") from exc


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    return _send("GET", url, None, headers, timeout)


def post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict]:
    return _send("POST", url, payload, headers, timeout)


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def describe_body(body: dict[str, Any]) -> str:
    for key in ("message", "msg", "error", "code", "_raw"):
        if key in body and body[key]:
            return str(body[key])[:_BODY_PREVIEW]
    return json.dumps(body, ensure_ascii=False)[:_BODY_PREVIEW]
