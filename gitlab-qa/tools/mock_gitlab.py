"""GitLab 形状的本地靶机。

为什么要有它：

1. 性能门禁必须有地方跑。公共实例（gitlab.com）不能压测——那是对别人的
   服务发起攻击；而自建 GitLab CE 要 8GB 内存 + 10 分钟启动。这个靶机
   1 秒起来，让「性能门禁」在没有真实实例时也能验证和演练。
2. 写操作（destructive）用例需要能反复跑。真实实例上反复建删项目会留下垃圾数据，
   靶机每次启动都是干净状态。
3. 延迟可控。MOCK_LATENCY_MS 可以把 P95 顶到阈值之上，用来验证
   「门禁真的会拦」——这一点比「门禁会放行」重要得多。

只实现了框架真正用到的那些字段，不追求覆盖 GitLab 全部 API。

    python tools/mock_gitlab.py --port 8929
    MOCK_LATENCY_MS=3000 python tools/mock_gitlab.py --port 8929   # 让门禁变红
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "/api/v4"

# 延迟注入：固定值 + 抖动，抖动让每次运行的 P95 有细微差别，
# 更接近真实网络下的分布（全是同一个值反而看不出统计口径的问题）
_LATENCY_MS = float(os.getenv("MOCK_LATENCY_MS", "0"))
_JITTER_MS = float(os.getenv("MOCK_JITTER_MS", "0"))
# 错误注入比例：0.0 ~ 1.0，用来验证错误率门禁
_ERROR_RATE = float(os.getenv("MOCK_ERROR_RATE", "0"))
_TOKEN = os.getenv("MOCK_TOKEN", "mock-token")


class State:
    """内存态。每次启动重置，不做持久化——靶机的价值就在于可预期。"""

    def __init__(self, seed_projects: int = 25) -> None:
        self.lock = threading.RLock()
        self.next_project_id = 1000
        self.next_issue_iid: dict[int, int] = {}
        self.projects: dict[int, dict] = {}
        self.issues: dict[tuple[int, int], dict] = {}
        self.groups: dict[int, dict] = {}
        self.next_group_id = 500
        # 实例级设置。默认关闭自助注册，和「内网实例应有的样子」保持一致：
        # 靶机替身如果默认开着，安全用例会在靶机上假红，掩盖真实配置漂移。
        self.application_settings: dict = {
            "id": 1,
            "signup_enabled": False,
            "password_authentication_enabled_for_web": True,
            "gravatar_enabled": True,
            "default_project_visibility": "private",
            "default_branch_protection": 2,
            "created_at": "2026-09-01T08:00:00.000Z",
        }
        rng = random.Random(20261020)
        for i in range(seed_projects):
            self._make_project(f"seed-project-{i:02d}", rng)

    def _make_project(self, name: str, rng: random.Random | None = None) -> dict:
        pid = self.next_project_id
        self.next_project_id += 1
        r = rng or random.Random()
        created = "2026-09-01T08:00:00.000Z"
        proj = {
            "id": pid,
            "name": name,
            "path": name.lower().replace(" ", "-"),
            "path_with_namespace": f"qa-org/{name.lower().replace(' ', '-')}",
            "description": "",
            "default_branch": "main",
            # 三分之一公开：匿名可见性是鉴权边界的核心，必须能测
            "visibility": "public" if pid % 3 == 0 else "private",
            "web_url": f"http://localhost/qa-org/{name}",
            "created_at": created,
            "last_activity_at": created,
            "namespace": {"id": 10, "name": "qa-org", "path": "qa-org", "kind": "group"},
            "star_count": r.randint(0, 50),
            "forks_count": r.randint(0, 10),
            "open_issues_count": 0,
            "archived": False,
            "empty_repo": False,
            "tag_list": [],
            "topics": [],
        }
        self.projects[pid] = proj
        self.next_issue_iid[pid] = 1
        return proj

    def create_project(self, payload: dict) -> dict:
        name = payload.get("name") or "unnamed"
        with self.lock:
            proj = self._make_project(name)
            for k in ("description", "visibility", "default_branch"):
                if k in payload:
                    proj[k] = payload[k]
            return proj

    def delete_project(self, pid: int) -> bool:
        with self.lock:
            if pid not in self.projects:
                return False
            del self.projects[pid]
            for (p, _i) in [k for k in self.issues if k[0] == pid]:
                del self.issues[(p, _i)]
            self.next_issue_iid.pop(pid, None)
            return True

    def create_issue(self, pid: int, payload: dict) -> dict:
        with self.lock:
            iid = self.next_issue_iid.get(pid, 1)
            self.next_issue_iid[pid] = iid + 1
            now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            issue = {
                "id": pid * 100 + iid,
                "iid": iid,
                "project_id": pid,
                "title": payload.get("title") or "untitled",
                "description": payload.get("description") or "",
                "state": "opened",
                "created_at": now,
                "updated_at": now,
                "closed_at": None,
                "labels": payload.get("labels") or [],
                "weight": payload.get("weight"),
                "author": {"id": 1, "username": "qa-bot", "name": "QA Bot",
                           "state": "active"},
                "assignees": [],
                "web_url": f"http://localhost/qa-org/proj/-/issues/{iid}",
                "references": {"short": f"#{iid}", "full": f"qa-org/proj#{iid}"},
                "user_notes_count": 0,
                "discussion_locked": False,
            }
            self.issues[(pid, iid)] = issue
            proj = self.projects.get(pid)
            if proj is not None:
                proj["open_issues_count"] = sum(
                    1 for (p, i), iss in self.issues.items()
                    if p == pid and iss["state"] == "opened"
                )
            return issue

    def create_group(self, payload: dict) -> dict:
        with self.lock:
            gid = self.next_group_id
            self.next_group_id += 1
            name = payload.get("name") or "unnamed"
            grp = {
                "id": gid,
                "name": name,
                "path": payload.get("path") or name.lower().replace(" ", "-"),
                "description": payload.get("description") or "",
                "visibility": payload.get("visibility") or "private",
                "web_url": f"http://localhost/groups/{gid}",
                "full_path": payload.get("path") or name.lower(),
                "parent_id": None,
                "created_at": "2026-09-01T08:00:00.000Z",
            }
            self.groups[gid] = grp
            return grp


STATE = State()


class _Handled(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockGitLab/0.1"

    # 每个请求开始时重置，保证 drain 幂等与请求体解析互不干扰
    _body_read = False
    _parsed_body: dict = {}
    _raw_body: bytes = b""

    # ---------------------------------------------------------------- 基础
    def log_message(self, fmt: str, *args) -> None:  # 静音，跑测试时刷屏很烦
        pass

    def _sleep(self) -> None:
        """延迟注入。放在读取路径的正中，模拟服务端处理耗时。"""
        if _LATENCY_MS > 0 or _JITTER_MS > 0:
            delay = _LATENCY_MS / 1000 + random.uniform(0, _JITTER_MS / 1000)
            if delay > 0:
                time.sleep(delay)

    def _roll_error(self) -> bool:
        return _ERROR_RATE > 0 and random.random() < _ERROR_RATE

    def _send(self, status: int, body: bytes | dict | list | None,
              headers: dict[str, str] | None = None, content_type: str = "application/json") -> None:
        if body is None:
            payload = b""
        elif isinstance(body, (dict, list)):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        else:
            payload = body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # 安全响应头：真实 GitLab 在所有响应上都带这几项。
        # 靶机缺了它们，安全层的「响应头合规」用例会在靶机上假失败——
        # 表面上是用例写错，实际是替身失真。替身必须复刻它要替代的那层行为。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _drain(self) -> None:
        """读完请求体再响应。

        不读的后果：keep-alive 连接上残留的字节会顶掉下一个请求，
        真实项目里表现为「偶发的刷新令牌失败」。

        重复读的后果（同样致命）：rfile.read(n) 在字节已被消费后会阻塞到超时，
        表现为「校验类 4xx 接口偶发 20 秒才返回」。
        所以用 _body_read 做幂等保护——两个方向的坑都靠这一处收拾掉。
        """
        if self._body_read:
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._body_read = True
        self._raw_body = raw
        self._parsed_body = {}
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            self._parsed_body = data

    def _form(self) -> dict[str, str]:
        """解析 application/x-www-form-urlencoded —— HTML 表单和 JSON 走两条路。"""
        self._drain()
        try:
            return {k: v[0] for k, v in
                    urllib.parse.parse_qs(self._raw_body.decode("utf-8")).items()}
        except Exception:  # noqa: BLE001
            return {}

    def _error(self, status: int, code: str, message: str) -> None:
        self._drain()
        self._send(status, {"error": code, "message": message})

    def _body(self) -> dict:
        self._drain()
        return self._parsed_body

    def _query(self) -> dict[str, str]:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _authed(self) -> bool:
        token = (self.headers.get("PRIVATE-TOKEN")
                 or self.headers.get("Authorization", "").removeprefix("Bearer "))
        return bool(token) and token == _TOKEN

    def _unauthorized(self) -> None:
        self._error(401, "invalid_token", "401 Unauthorized")

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        self._body_read = False
        self._parsed_body = {}
        self._raw_body = b""
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path.startswith(API):
            path = path[len(API):]
        elif path == "/-/health":
            return self._send(200, {"status": "ok"})
        elif path == "/-/readiness":
            # GitLab 的就绪探针：依赖未就绪时返回 503 + 具体失败项
            return self._send(200, {"status": "ok", "checks": {"db": "ok", "redis": "ok"}})
        elif path in ("/", "/users/sign_in", "/dashboard/projects", "/users/sign_out"):
            return self._serve_html(method, path)
        else:
            return self._error(404, "not_found", f"no such route: {path}")

        try:
            self._sleep()
            if self._roll_error():
                return self._error(503, "service_unavailable", "注入的随机故障")
            self._dispatch(method, path)
        except _Handled:
            return
        except Exception as exc:  # noqa: BLE001
            self._error(500, "internal_error", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ HTML 界面
    # 说明：这不是 GitLab 的界面复刻，只提供页面对象需要的最小 DOM 结构。
    # 它的作用是让 UI 层的机制（等待策略、失败留证、trace、断言失败路径）
    # 在没有自建实例时也能被真实执行一遍；对真实 GitLab 的选择器校验
    # 必须对着自建实例做，公共实例会被 WAF 拦（实测 gitlab.com 返回 403）。
    def _has_session(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        return "mock_session=ok" in cookie

    def _serve_html(self, method: str, path: str) -> None:
        if path == "/":
            return self._redirect("/dashboard/projects")
        if path == "/users/sign_out":
            return self._send(302, None, {
                "Set-Cookie": "mock_session=; Path=/; Max-Age=0",
                "Location": "/users/sign_in",
            })
        if path == "/users/sign_in":
            if method == "POST":
                return self._handle_sign_in()
            if self._has_session():
                return self._redirect("/dashboard/projects")
            return self._page(_SIGN_IN_HTML)
        if path == "/dashboard/projects":
            if not self._has_session():
                return self._redirect("/users/sign_in")
            return self._page(_projects_html())
        return self._error(404, "not_found", path)

    def _handle_sign_in(self) -> None:
        form = self._form()
        user = form.get("user[login]", "")
        pwd = form.get("user[password]", "")
        if user == ADMIN_USER and pwd == _ROOT_PASSWORD:
            # 302 + Set-Cookie，和真实登录的表单提交一致（不是 200 + JS 跳转）
            return self._send(302, None, {
                "Set-Cookie": "mock_session=ok; Path=/",
                "Location": "/dashboard/projects",
            })
        self._page(_SIGN_IN_HTML.replace(
            "<!--ERROR-->",
            '<div class="flash-alert" data-testid="alert-danger">'
            "Invalid Login or password.</div>",
        ), status=200)

    def _redirect(self, location: str) -> None:
        # 注意这里要显式给 Content-Length: 0，否则 Playwright 会一直等 body
        self._send(302, None, {"Location": location})

    def _page(self, body: str, status: int = 200) -> None:
        html = _PAGE_TEMPLATE.replace("{{BODY}}", body)
        self._send(status, html.encode("utf-8"), content_type="text/html; charset=utf-8")

    def _dispatch(self, method: str, path: str) -> None:
        q = self._query()
        # ---- 系统（无需鉴权，和真实 GitLab 一致）
        if path == "/version" and method == "GET":
            return self._send(200, {
                "version": "17.5.0-ce", "revision": "mock-rev",
                "kas": {"enabled": False, "version": "17.5.0"},
                "enterprise": False,
            })
        if path == "/metadata" and method == "GET":
            return self._send(200, {
                "version": "17.5.0-ce", "revision": "mock-rev",
                "enterprise": False,
            })
        if path == "/application/settings" and method in ("GET", "PUT"):
            # 与真实 GitLab 一致：管理员端点，非管理员 403、匿名 401。
            # 这条边界正是越权用例的探针，写错的话整套越权断言会失去意义。
            if not self._authed():
                return self._unauthorized()
            if method == "GET":
                return self._send(200, dict(STATE.application_settings))
            updates = {}
            updates.update(self._body())
            for k, v in self._query().items():
                updates[k] = {"true": True, "false": False}.get(v, v)
            with STATE.lock:
                for k, v in updates.items():
                    if k in STATE.application_settings and isinstance(v, (bool, str, int)):
                        STATE.application_settings[k] = v
            return self._send(200, dict(STATE.application_settings))
        if path == "/application/statistics" and method == "GET":
            if not self._authed():
                return self._error(403, "forbidden", "admin access required")
            return self._send(200, {
                "projects": len(STATE.projects), "users": 3, "groups": len(STATE.groups),
            })

        # ---- 匿名可见性：真实 GitLab 允许匿名列出公开项目，
        # 这条边界如果写错（一律 401），前端就永远显示不出公开内容。
        if not self._authed():
            if path == "/projects" and method == "GET":
                rows = [p for p in STATE.projects.values() if p.get("visibility") == "public"]
                rows.sort(key=lambda p: p["id"])
                return self._paginated(rows, q)
            return self._unauthorized()

        # ---- 管理员接口：已认证但无权限必须是 403
        if path.startswith("/admin/"):
            return self._error(403, "forbidden", "403 Forbidden - admin access required")

        if path == "/user" and method == "GET":
            return self._send(200, {
                "id": 1, "username": "qa-bot", "name": "QA Bot",
                "email": "qa-bot@example.com", "state": "active",
                "is_admin": True, "created_at": "2026-01-01T00:00:00.000Z",
                "web_url": "http://localhost/qa-bot",
            })
        if path == "/users" and method == "GET":
            return self._paginated([{
                "id": 1, "username": "qa-bot", "name": "QA Bot",
                "state": "active", "web_url": "http://localhost/qa-bot",
            }], q)

        # ---- 项目集合
        if path == "/projects":
            if method == "GET":
                rows = list(STATE.projects.values())
                if term := q.get("search"):
                    rows = [p for p in rows if term.lower() in p["name"].lower()]
                rows.sort(key=lambda p: p["id"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                name = (body.get("name") or "").strip()
                if not name:
                    return self._error(400, "bad_request", "name is missing")
                proj = STATE.create_project(body)
                return self._send(201, proj)

        m = re.fullmatch(r"/projects/([^/]+)", path)
        if m:
            pid = _as_int(m.group(1))
            proj = STATE.projects.get(pid) if pid is not None else None
            if method == "GET":
                if proj is None:
                    return self._error(404, "not_found", "404 Project Not Found")
                return self._send(200, proj)
            if method == "DELETE":
                if not STATE.delete_project(pid or -1):
                    return self._error(404, "not_found", "404 Project Not Found")
                return self._send(202, {"message": "202 Accepted"})

        # ---- 议题
        m = re.fullmatch(r"/projects/([^/]+)/issues", path)
        if m:
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            if method == "GET":
                rows = [i for (p, _), i in STATE.issues.items() if p == pid]
                if state := q.get("state"):
                    rows = [i for i in rows if i["state"] == state]
                rows.sort(key=lambda i: i["iid"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                if not (body.get("title") or "").strip():
                    return self._error(400, "bad_request", "title is missing")
                return self._send(201, STATE.create_issue(pid, body))

        m = re.fullmatch(r"/projects/([^/]+)/issues/(\d+)", path)
        if m:
            pid, iid = _as_int(m.group(1)), int(m.group(2))
            key = (pid, iid)
            if method == "GET":
                iss = STATE.issues.get(key)
                if iss is None:
                    return self._error(404, "not_found", "404 Issue Not Found")
                return self._send(200, iss)
            if method == "PUT":
                iss = STATE.issues.get(key)
                if iss is None:
                    return self._error(404, "not_found", "404 Issue Not Found")
                body = self._body()
                iss.update({k: v for k, v in body.items()
                            if k in ("title", "description", "labels", "weight")})
                if body.get("state_event") == "close":
                    iss["state"] = "closed"
                    iss["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                elif body.get("state_event") == "reopen":
                    iss["state"] = "opened"
                    iss["closed_at"] = None
                iss["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                proj = STATE.projects.get(pid)
                if proj is not None:
                    proj["open_issues_count"] = sum(
                        1 for i in STATE.issues.values()
                        if i["project_id"] == pid and i["state"] == "opened"
                    )
                return self._send(200, iss)

        # ---- 仓库
        if re.fullmatch(r"/projects/([^/]+)/repository/tree", path):
            return self._paginated([
                {"id": "t1", "name": "README.md", "type": "blob", "path": "README.md",
                 "mode": "100644"},
                {"id": "t2", "name": "src", "type": "tree", "path": "src", "mode": "040000"},
            ], q)
        if re.fullmatch(r"/projects/([^/]+)/repository/commits", path):
            return self._paginated([
                {"id": "c" * 40, "short_id": "ccccccc", "title": "Initial commit",
                 "message": "Initial commit", "author_name": "QA Bot",
                 "author_email": "qa-bot@example.com",
                 "created_at": "2026-09-01T08:00:00.000Z",
                 "web_url": "http://localhost/c"},
            ], q)
        if re.fullmatch(r"/projects/([^/]+)/repository/branches", path):
            return self._paginated([
                {"name": "main", "merged": False, "protected": True,
                 "default": True, "commit": {"id": "c" * 40, "short_id": "ccccccc"}},
                {"name": "develop", "merged": False, "protected": False,
                 "default": False, "commit": {"id": "d" * 40, "short_id": "ddddddd"}},
            ], q)

        # ---- 群组
        if path == "/groups" and method == "GET":
            return self._paginated(list(STATE.groups.values()), q)
        if path == "/groups" and method == "POST":
            body = self._body()
            if not (body.get("name") or "").strip():
                return self._error(400, "bad_request", "name is missing")
            return self._send(201, STATE.create_group(body))

        self._error(404, "not_found", f"404 route not found: {method} {path}")

    # ---------------------------------------------------------------- 分页
    def _paginated(self, rows: list[dict], q: dict[str, str]) -> None:
        """按 GitLab 的 keyset/offset 风格回分页头。

        X-Next-Page 是空字符串表示没有下一页——框架里的 paginate() 靠这个停。
        """
        try:
            page = int(q.get("page", "1"))
            per_page = int(q.get("per_page", "20"))
        except ValueError:
            return self._error(400, "bad_request", "page/per_page must be integers")
        if page < 1:
            return self._error(400, "bad_request", "page must be >= 1")
        # 和真实 GitLab 一致：per_page 上限 100，超限静默截断
        per_page = max(1, min(per_page, 100))

        total = len(rows)
        total_pages = (total + per_page - 1) // per_page if per_page else 0
        start = (page - 1) * per_page
        items = rows[start:start + per_page]

        headers = {
            "X-Page": str(page),
            "X-Per-Page": str(per_page),
            "X-Total": str(total),
            "X-Total-Pages": str(total_pages),
            "X-Prev-Page": str(page - 1) if page > 1 else "",
            "X-Next-Page": str(page + 1) if start + per_page < total else "",
        }
        self._send(200, items, headers)


def _as_int(raw: str) -> int | None:
    decoded = urllib.parse.unquote(raw)
    return int(decoded) if decoded.isdigit() else None


def _openapi() -> dict:
    return {}


ADMIN_USER = "root"
_ROOT_PASSWORD = os.getenv("MOCK_ROOT_PASSWORD", "mock-root-password")

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>GitLab</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#fafafa;color:#333}
 header{background:#292961;color:#fff;padding:12px 24px;display:flex;justify-content:space-between;align-items:center}
 header a{color:#fff;text-decoration:none}
 .avatar{width:32px;height:32px;border-radius:50%;background:#e24329;display:flex;
         align-items:center;justify-content:center;font-size:13px}
 main{max-width:760px;margin:40px auto;padding:0 24px}
 .card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:24px}
 label{display:block;margin:14px 0 6px;font-size:14px}
 input[type=text],input[type=password],input[name=name]{width:100%;padding:9px 10px;
        border:1px solid #ccc;border-radius:4px;box-sizing:border-box;font-size:14px}
 button{margin-top:18px;background:#1f75cb;color:#fff;border:0;border-radius:4px;
        padding:10px 18px;font-size:14px;cursor:pointer}
 .flash-alert{background:#fdd4cd;border:1px solid #e5a9a1;color:#6b1b12;
              padding:10px 12px;border-radius:4px;margin-bottom:10px}
 .project-row{padding:12px 0;border-bottom:1px solid #eee}
 .project-row:last-child{border-bottom:0}
</style></head>
<body>
<header><a href="/dashboard/projects">GitLab <span style="opacity:.6">· 靶机</span></a>
<span class="avatar" data-testid="user-avatar" title="signed in">QA</span></header>
<main>{{BODY}}</main>
</body></html>"""

_SIGN_IN_HTML = """<div class="card">
  <h1 style="font-size:20px;margin:0 0 6px">Sign in</h1>
  <p style="color:#666;font-size:13px;margin:0 0 10px">本地靶机 · 账号 root / 密码见 MOCK_ROOT_PASSWORD</p>
  <!--ERROR-->
  <form action="/users/sign_in" method="post">
    <label for="user_login">Username or email</label>
    <input type="text" id="user_login" name="user[login]" data-testid="username-field"
           autocomplete="username" value="">
    <label for="user_password">Password</label>
    <input type="password" id="user_password" name="user[password]"
           data-testid="password-field" autocomplete="current-password">
    <button type="submit" data-testid="sign-in-button">Sign in</button>
  </form>
</div>"""


def _projects_html(term: str = "") -> str:
    """项目列表页。支持 ?name= 过滤，这样搜索断言的语义才成立。"""
    term = (term or "").lower()
    rows = sorted(STATE.projects.values(), key=lambda p: p["id"])
    if term:
        rows = [p for p in rows if term in p["path"].lower() or term in p["name"].lower()]
    items = "".join(
        f'<div class="project-row"><span data-testid="project-name">'
        f'<a href="/{p["path_with_namespace"]}">{p["name"]}</a></span>'
        f'<div style="color:#888;font-size:12px">{p["path_with_namespace"]}</div></div>'
        for p in rows
    ) or '<p style="color:#888">没有匹配的项目</p>'
    return f"""<div class="card">
  <form method="get" action="/dashboard/projects">
    <input type="text" name="name" id="project-filter-form-field"
           data-testid="project-filter-form-field" placeholder="Filter by name" value="{term}">
  </form>
  <div data-testid="projects-list" class="projects-list" style="margin-top:16px">
    {items}
  </div>
</div>"""


class MockGitLab:
    """可编程启动/停止，供脚本与测试内嵌使用。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8929) -> None:
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start_background(self, wait: float = 3.0) -> MockGitLab:
        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.wait_ready(wait)
        return self

    def wait_ready(self, timeout: float = 3.0) -> None:
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base_url}/-/health", timeout=1):
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        raise RuntimeError(f"靶机未在 {timeout}s 内就绪：{self.base_url}")

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> MockGitLab:
        return self.start_background()

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> int:
    global _LATENCY_MS, _JITTER_MS, _ERROR_RATE

    p = argparse.ArgumentParser(description="GitLab 形状的本地靶机")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.getenv("MOCK_PORT", "8929")))
    p.add_argument("--latency-ms", type=float, default=_LATENCY_MS,
                   help="给每个请求注入固定延迟，用于验证性能门禁")
    p.add_argument("--jitter-ms", type=float, default=_JITTER_MS)
    p.add_argument("--error-rate", type=float, default=_ERROR_RATE,
                   help="随机 503 比例 0.0~1.0，用于验证错误率门禁")
    args = p.parse_args()

    _LATENCY_MS, _JITTER_MS, _ERROR_RATE = args.latency_ms, args.jitter_ms, args.error_rate

    srv = MockGitLab(args.host, args.port).start_background()
    print(f"MockGitLab 已启动: {srv.base_url}")
    print(f"  Token       : {_TOKEN}")
    print(f"  预置项目     : {len(STATE.projects)}")
    if _LATENCY_MS or _JITTER_MS:
        print(f"  延迟注入     : {_LATENCY_MS}ms + 抖动 {_JITTER_MS}ms")
    if _ERROR_RATE:
        print(f"  错误注入     : {_ERROR_RATE:.0%} 的 503")
    print("Ctrl+C 停止")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
