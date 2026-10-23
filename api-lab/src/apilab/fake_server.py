"""本地 REST 靶机：只用标准库，clone 后无需任何外部服务即可开跑。

为什么不用 httpbin / Restful-booker：
- httpbin 无状态、无鉴权、无分页，练不到 token 流转与数据清理；
- 公网靶场受网络、限流、冷启动影响，在 CI 里不可靠；
- 这个靶机故意保留了三类真实缺陷来源（token 过期、分页越界、间歇 5xx），
  才能让「刷新令牌」「边界用例」「失败重试」有东西可测。

刻意的设计：
- 端口传 0 由内核分配，每个 pytest worker 各起一个实例，天然支持 xdist；
- 所有写操作进内存并加锁，同一 worker 内用例之间才能做数据依赖；
- schema 漂移可运行时开关，用来演示「契约测试真的会拦」。
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USERS = {
    "alice": {"password": "alice-pw", "role": "user"},
    "admin": {"password": "admin-pw", "role": "admin"},
}

GENRES = ["fiction", "tech", "history", "science"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _State:
    """进程内共享状态。一个 server 实例一份，worker 之间不共享。"""

    def __init__(self, seed_books: int = 25) -> None:
        self.lock = threading.RLock()
        self.books: dict[str, dict] = {}
        self.access_tokens: dict[str, dict] = {}
        self.refresh_tokens: dict[str, str] = {}
        self.flaky_hits: dict[str, int] = {}
        self.schema_drift = False
        self.seed_books = seed_books
        self._seed()

    def reset(self) -> None:
        """重置业务数据，但保留 lock 与已签发的令牌。

        令牌必须保留：session 级 fixture 持有的客户端如果被顺手打回未登录态，
        后续用例会出现「随机 401」，排查成本极高。
        """
        with self.lock:
            self.books.clear()
            self.flaky_hits.clear()
            # schema_drift 属于「上游行为」，不是业务数据，重置时不能一起清掉：
            # 否则 --schema-drift 会被某个用例的 clean_library 静默抹掉，
            # 表现为「明明开了漂移，契约测试却全绿」——这类假阴性最难查。
            self._seed()

    def _seed(self) -> None:
        rng = random.Random(20261019)  # 固定种子：分页断言才能写死预期值
        for i in range(1, self.seed_books + 1):
            book_id = f"bk_{i:03d}"
            self.books[book_id] = {
                "id": book_id,
                "title": f"样本图书 {i:02d}",
                "author": f"作者{rng.choice('ABCDE')}",
                "genre": GENRES[i % len(GENRES)],
                "published_year": 1990 + (i % 30),
                "price": round(19.9 + i * 1.5, 2),
                "in_stock": i % 4 != 0,
                "owner": "alice",
                "created_at": _iso(_now()),
            }

    # --- 令牌 -------------------------------------------------------------
    def issue_tokens(self, username: str, ttl: int = 3600) -> dict:
        access = f"tok_{uuid.uuid4().hex[:16]}"
        refresh = f"ref_{uuid.uuid4().hex[:16]}"
        with self.lock:
            self.access_tokens[access] = {
                "username": username,
                "role": USERS[username]["role"],
                "exp": time.time() + ttl,
            }
            self.refresh_tokens[refresh] = username
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": ttl,
        }

    def resolve(self, token: str) -> dict | None:
        with self.lock:
            info = self.access_tokens.get(token)
        if not info:
            return None
        if info["exp"] < time.time():
            return {"expired": True, "username": info["username"]}
        return info

    def revoke_access(self, token: str) -> None:
        with self.lock:
            self.access_tokens.pop(token, None)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ApiLab/0.1.0"
    protocol_version = "HTTP/1.1"

    # --- 基础设施 ---------------------------------------------------------
    @property
    def state(self) -> _State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, payload=None, headers: dict | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Trace-Id", uuid.uuid4().hex[:12])
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, **extra) -> None:
        err = {"code": code, "message": message, **extra}
        self._send(status, {"error": err})

    def _body(self) -> dict:
        raw = getattr(self, "_raw_body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _query(self) -> dict[str, list[str]]:
        # keep_blank_values：?page= 必须被当成「传了空值」，否则会被静默当成默认值，
        # 让「空参数该报 400」这条用例永远红不起来
        return parse_qs(urlparse(self.path).query, keep_blank_values=True)

    # --- 鉴权 -------------------------------------------------------------
    def _auth(self, required_role: str | None = None) -> dict | None:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            self._error(401, "UNAUTHORIZED", "缺少 Bearer 令牌")
            return None
        info = self.state.resolve(header[7:].strip())
        if info is None:
            self._error(401, "INVALID_TOKEN", "令牌不存在或已被吊销")
            return None
        if info.get("expired"):
            # 真实系统在这里给 401 + 明确 code，客户端才知道该刷新而不是重登
            self._error(401, "TOKEN_EXPIRED", "令牌已过期，请用 refresh_token 续期")
            return None
        if required_role and info["role"] != required_role:
            self._error(403, "FORBIDDEN", f"需要 {required_role} 角色")
            return None
        return info

    # --- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]

        # 必须先把请求体读完再路由，哪怕后面会因为 401/403 提前返回。
        # HTTP/1.1 是长连接：未读的字节会留在 socket 里，下一个请求复用这条连接时
        # 服务端会把残留 JSON 当成请求行来解析，表现为「刷新令牌莫名 400/401」。
        # 这个坑在真实网关后面排查过的人都知道有多贵。
        length = int(self.headers.get("Content-Length") or 0)
        self._raw_body = self.rfile.read(length) if length else b""

        try:
            if parts == ["auth", "login"] and method == "POST":
                return self._login()
            if parts == ["auth", "refresh"] and method == "POST":
                return self._refresh()
            if parts == ["auth", "logout"] and method == "POST":
                return self._logout()
            if parts == ["api", "books"] and method == "GET":
                return self._list_books()
            if parts == ["api", "books"] and method == "POST":
                return self._create_book()
            if len(parts) == 3 and parts[:2] == ["api", "books"]:
                return self._book_item(method, parts[2])
            if parts == ["api", "admin", "stats"] and method == "GET":
                return self._admin_stats()
            if parts == ["api", "flaky"] and method == "GET":
                return self._flaky()
            if parts == ["api", "slow"] and method == "GET":
                return self._slow()
            if parts == ["openapi.json"] and method == "GET":
                return self._send(200, OPENAPI)
            if parts == ["__control", "drift"] and method == "POST":
                body = self._body()
                self.state.schema_drift = bool(body.get("enabled", True))
                return self._send(200, {"schema_drift": self.state.schema_drift})
            if parts == ["__control", "reset"] and method == "POST":
                return self._reset()
            return self._error(404, "NOT_FOUND", f"无此路由：{method} {path}")
        except BrokenPipeError:
            pass  # 客户端超时先断开，属预期
        except Exception as exc:  # pragma: no cover - 兜底
            self._error(500, "INTERNAL", f"{type(exc).__name__}: {exc}")

    # --- 认证 -------------------------------------------------------------
    def _login(self) -> None:
        body = self._body()
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        user = USERS.get(username)
        if not user or user["password"] != password:
            # 不区分「用户不存在」和「密码错」，避免账号枚举
            return self._error(401, "BAD_CREDENTIALS", "用户名或密码错误")
        ttl = int(body.get("ttl") or 3600)
        return self._send(200, self.state.issue_tokens(username, ttl))

    def _refresh(self) -> None:
        body = self._body()
        token = str(body.get("refresh_token") or "")
        with self.state.lock:
            username = self.state.refresh_tokens.get(token)
        if not username:
            return self._error(401, "INVALID_REFRESH_TOKEN", "刷新令牌无效")
        with self.state.lock:
            self.state.refresh_tokens.pop(token, None)  # 一次性使用，防重放
        return self._send(200, self.state.issue_tokens(username))

    def _logout(self) -> None:
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            self.state.revoke_access(header[7:].strip())
        return self._send(204)

    # --- 图书 -------------------------------------------------------------
    def _paginate(self, rows: list[dict]) -> tuple[int, int, int, list[dict]]:
        q = self._query()
        try:
            page = int(q.get("page", ["1"])[0])
            size = int(q.get("size", ["10"])[0])
        except ValueError:
            self._error(400, "VALIDATION_ERROR", "page/size 必须是整数")
            raise _Handled
        if page < 1:
            self._error(400, "VALIDATION_ERROR", "page 从 1 开始")
            raise _Handled
        if not 1 <= size <= 50:
            # 上限 50：边界用例的锚点，也是防雪崩的常见做法
            self._error(400, "VALIDATION_ERROR", "size 取值范围 1..50")
            raise _Handled
        total = len(rows)
        start = (page - 1) * size
        return total, page, size, rows[start : start + size]

    def _list_books(self) -> None:
        q = self._query()
        rows = sorted(self.state.books.values(), key=lambda b: b["id"])
        if genre := q.get("genre", [None])[0]:
            rows = [b for b in rows if b["genre"] == genre]
        if kw := q.get("q", [None])[0]:
            rows = [b for b in rows if kw.lower() in b["title"].lower()]
        try:
            total, page, size, items = self._paginate(rows)
        except _Handled:
            return
        # size 必须回显请求值，不能拿 len(items) 顶替：
        # 最后一页只有 5 条时回 size=5，前端分页器会算错总页数
        pages = (total + size - 1) // size if size else 0
        # 列表也要应用漂移：真实事故里上游往往只在某条读取路径上改了序列化，
        # 只漂一个接口反而更难查——这里统一漂移，才能验证契约用例覆盖了所有读取路径
        self._send(
            200,
            {
                "items": [self._maybe_drift(b) for b in items],
                "total": total,
                "page": page,
                "size": size,
                "pages": pages,
            },
        )

    def _create_book(self) -> None:
        user = self._auth()
        if not user:
            return
        body = self._body()
        missing = [f for f in ("title", "author", "genre") if not body.get(f)]
        if missing:
            return self._error(400, "VALIDATION_ERROR", "缺少必填字段", fields=missing)
        if body["genre"] not in GENRES:
            return self._error(400, "VALIDATION_ERROR", f"genre 必须是 {GENRES} 之一")
        book_id = f"bk_{uuid.uuid4().hex[:8]}"
        book = {
            "id": book_id,
            "title": body["title"],
            "author": body["author"],
            "genre": body["genre"],
            "published_year": int(body.get("published_year") or _now().year),
            "price": float(body.get("price") or 0),
            "in_stock": bool(body.get("in_stock", True)),
            "owner": user["username"],
            "created_at": _iso(_now()),
        }
        with self.state.lock:
            self.state.books[book_id] = book
        self._send(201, book, headers={"Location": f"/api/books/{book_id}"})

    def _book_item(self, method: str, book_id: str) -> None:
        book = self.state.books.get(book_id)
        if not book:
            return self._error(404, "NOT_FOUND", f"图书不存在：{book_id}")

        if method == "GET":
            return self._send(200, self._maybe_drift(book))

        user = self._auth()
        if not user:
            return

        if method == "PATCH":
            if user["role"] != "admin" and book["owner"] != user["username"]:
                return self._error(403, "FORBIDDEN", "只能修改自己创建的图书")
            body = self._body()
            allowed = {"title", "author", "genre", "published_year", "price", "in_stock"}
            unknown = set(body) - allowed
            if unknown:
                return self._error(400, "VALIDATION_ERROR", "不支持修改字段", fields=sorted(unknown))
            with self.state.lock:
                book.update({k: v for k, v in body.items() if k in allowed})
            return self._send(200, self._maybe_drift(book))

        if method == "DELETE":
            if user["role"] != "admin" and book["owner"] != user["username"]:
                return self._error(403, "FORBIDDEN", "只能删除自己创建的图书")
            with self.state.lock:
                self.state.books.pop(book_id, None)
            return self._send(204)

        return self._error(405, "METHOD_NOT_ALLOWED", f"{method} 不支持")

    def _maybe_drift(self, book: dict) -> dict:
        """模拟上游字段类型漂移：published_year 从 int 变 str。

        契约测试的价值就在这里——业务用例可能全绿，但下游解析会炸。
        """
        if not self.state.schema_drift:
            return book
        drifted = dict(book)
        drifted["published_year"] = str(book["published_year"])
        return drifted

    # --- 其他 -------------------------------------------------------------
    def _admin_stats(self) -> None:
        if not self._auth(required_role="admin"):
            return
        with self.state.lock:
            self._send(
                200,
                {
                    "total_books": len(self.state.books),
                    "total_users": len(USERS),
                    "active_tokens": len(self.state.access_tokens),
                },
            )

    def _flaky(self) -> None:
        """前 N 次 500，之后 200。用于验证 --reruns 真的救回用例。"""
        q = self._query()
        key = q.get("key", ["default"])[0]
        fail_times = int(q.get("fail_times", ["2"])[0])
        with self.state.lock:
            hits = self.state.flaky_hits.get(key, 0) + 1
            self.state.flaky_hits[key] = hits
        if hits <= fail_times:
            return self._error(503, "UPSTREAM_UNAVAILABLE", f"第 {hits} 次调用失败")
        return self._send(200, {"key": key, "attempt": hits})

    def _slow(self) -> None:
        ms = int(self._query().get("ms", ["800"])[0])
        time.sleep(min(ms, 5000) / 1000)
        self._send(200, {"slept_ms": ms})

    def _reset(self) -> None:
        self.state.reset()
        self._send(200, {"reset": True})


class _Handled(Exception):
    """内部信号：响应已发出，调用方直接返回。"""


_BOOK_SCHEMA = {
    "type": "object",
    "required": [
        "id", "title", "author", "genre", "published_year",
        "price", "in_stock", "owner", "created_at",
    ],
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "author": {"type": "string"},
        "genre": {"type": "string", "enum": GENRES},
        "published_year": {"type": "integer"},
        "price": {"type": "number"},
        "in_stock": {"type": "boolean"},
        "owner": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
    },
}

_BOOK_PAGE_SCHEMA = {
    "type": "object",
    "required": ["items", "total", "page", "size", "pages"],
    "properties": {
        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Book"}},
        "total": {"type": "integer"},
        "page": {"type": "integer"},
        "size": {"type": "integer"},
        "pages": {"type": "integer"},
    },
}


def _ok(schema_name: str, description: str) -> dict:
    return {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{schema_name}"}}
        },
    }


OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "ApiLab", "version": "0.1.0"},
    "paths": {
        "/api/books": {
            "get": {"responses": {"200": _ok("BookPage", "分页列表"), "400": _ok("Error", "参数错误")}},
            "post": {"responses": {"201": _ok("Book", "创建成功"), "400": _ok("Error", "校验失败")}},
        },
        "/api/books/{book_id}": {
            "get": {
                "parameters": [
                    {
                        "name": "book_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": _ok("Book", "详情"), "404": _ok("Error", "不存在")},
            },
            "patch": {"responses": {"200": _ok("Book", "更新"), "403": _ok("Error", "无权")}},
            "delete": {"responses": {"204": {"description": "删除成功"}}},
        },
    },
    "components": {
        "schemas": {
            "Book": _BOOK_SCHEMA,
            "BookPage": _BOOK_PAGE_SCHEMA,
            "Error": {
                "type": "object",
                "required": ["error"],
                "properties": {
                    "error": {
                        "type": "object",
                        "required": ["code", "message"],
                        "properties": {
                            "code": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    }
                },
            },
        }
    },
}


class ApiLabServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0, verbose: bool = False, seed_books: int = 25) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.state = _State(seed_books=seed_books)
        self.verbose = verbose

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start_background(self) -> "ApiLabServer":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
