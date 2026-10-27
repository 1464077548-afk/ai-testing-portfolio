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
import base64
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

        # 以下是「仓库 / 协作对象」这一层。它们都挂在某个项目下，
        # 项目被删时必须跟着一起消失——真实 GitLab 就是这样，
        # 替身漏了这一条，删除用例就会在「删完还能查到标签」上假绿。
        self.branches: dict[int, dict[str, dict]] = {}   # pid -> {分支名: 分支}
        self.files: dict[tuple[int, str, str], str] = {}  # (pid, 分支, 路径) -> 内容
        self.commits: dict[int, list[dict]] = {}          # pid -> 提交列表（新→旧）
        self.tags: dict[tuple[int, str], dict] = {}       # (pid, 标签名) -> 标签
        self.labels: dict[tuple[int, int], dict] = {}     # (pid, 标签 id) -> 标签
        self.next_label_id = 900
        self.milestones: dict[tuple[int, int], dict] = {}  # (pid, 里程碑 id)
        self.next_milestone_id = 700
        self.notes: dict[tuple[int, int, int], dict] = {}  # (pid, 议题 iid, 评论 id)
        self.next_note_id = 300
        self.mrs: dict[tuple[int, int], dict] = {}         # (pid, MR iid)
        self.next_mr_iid: dict[int, int] = {}
        self.members: dict[tuple[int, int], dict] = {}     # (pid, user_id)
        self.group_members: dict[tuple[int, int], dict] = {}  # (gid, user_id)
        self.snippets: dict[int, dict] = {}
        self.next_snippet_id = 600
        self.users_extra: dict[int, dict] = {}             # 运行时创建的用户
        self.next_user_id = 100
        self._commit_seq = 0
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
        # 初始化仓库：默认分支 + README + 一条初始提交。
        # 没有初始提交时很多仓库接口会 404（真实 GitLab 的空仓库就是这样），
        # 但「创建时带 README」的项目不该是空仓库，替身必须体现这个差别。
        self.branches[pid] = {
            "main": {"name": "main", "merged": False, "protected": True,
                     "default": True, "commit": {"id": "0" * 40, "short_id": "0000000"}},
        }
        self.files[(pid, "main", "README.md")] = f"# {name}\n"
        self.commits[pid] = [self._make_commit(f"Initial commit: {name}")]
        return proj

    # ------------------------------------------------------------ 提交
    def _make_commit(self, message: str, pid: int | None = None) -> dict:
        self._commit_seq += 1
        sha = f"{self._commit_seq:040x}"
        commit = {
            "id": sha,
            "short_id": sha[:8],
            "title": message,
            "message": message,
            "author_name": "QA Bot",
            "author_email": "qa-bot@example.com",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "web_url": f"http://localhost/-/commit/{sha}",
        }
        if pid is not None:
            self.commits.setdefault(pid, []).insert(0, commit)
            for br in self.branches.get(pid, {}).values():
                br["commit"] = {"id": sha, "short_id": sha[:8]}
        return commit

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
            # 挂在项目下的东西一并清掉。真实 GitLab 删项目不会留下
            # 「还能查到的标签/里程碑」，替身留下就会出现删完还能读到的假绿
            self.branches.pop(pid, None)
            self.commits.pop(pid, None)
            self.next_mr_iid.pop(pid, None)
            for key in [k for k in self.files if k[0] == pid]:
                del self.files[key]
            for key in [k for k in self.tags if k[0] == pid]:
                del self.tags[key]
            for key in [k for k in self.labels if k[0] == pid]:
                del self.labels[key]
            for key in [k for k in self.milestones if k[0] == pid]:
                del self.milestones[key]
            for key in [k for k in self.notes if k[0] == pid]:
                del self.notes[key]
            for key in [k for k in self.mrs if k[0] == pid]:
                del self.mrs[key]
            for key in [k for k in self.members if k[0] == pid]:
                del self.members[key]
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
                "milestone_id": payload.get("milestone_id"),
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
                # 子群组靠 parent_id 组织。写死 None 的话子群组列表永远是空的，
                # 而且看起来像「接口没返回数据」而不是「替身没实现」
                "parent_id": payload.get("parent_id"),
                "created_at": "2026-09-01T08:00:00.000Z",
            }
            self.groups[gid] = grp
            return grp

    # ------------------------------------------------------------ 用户
    def known_user_ids(self) -> set[int]:
        return {1} | set(self.users_extra)

    def user_brief(self, uid: int) -> dict | None:
        if uid == 1:
            return {"id": 1, "username": "qa-bot", "name": "QA Bot", "state": "active"}
        u = self.users_extra.get(uid)
        return {"id": uid, "username": u["username"], "name": u["name"],
                "state": u.get("state", "active")} if u else None

    def create_user(self, payload: dict) -> dict:
        with self.lock:
            uid = self.next_user_id
            self.next_user_id += 1
            user = {
                "id": uid,
                "username": payload.get("username") or f"user{uid}",
                "name": payload.get("name") or payload.get("username") or f"User {uid}",
                "email": payload.get("email") or f"user{uid}@example.com",
                "state": "active",
                "created_at": "2026-09-01T08:00:00.000Z",
                "is_admin": False,
                "web_url": f"http://localhost/{payload.get('username') or uid}",
            }
            self.users_extra[uid] = user
            return user

    # ------------------------------------------------------------ 标签
    def create_label(self, pid: int, payload: dict) -> dict | None:
        """返回 None 表示重名冲突（真实 GitLab 是 409）。"""
        name = (payload.get("name") or "").strip()
        with self.lock:
            if any(lb["name"] == name for (p, _), lb in self.labels.items() if p == pid):
                return None
            lid = self.next_label_id
            self.next_label_id += 1
            label = {
                "id": lid,
                "name": name,
                "color": payload.get("color") or "#0033cc",
                "description": payload.get("description") or "",
                "open_issues_count": 0,
                "closed_issues_count": 0,
                "open_merge_requests_count": 0,
                "subscribed": False,
                "priority": payload.get("priority"),
            }
            self.labels[(pid, lid)] = label
            return label

    # ------------------------------------------------------------ 里程碑
    def create_milestone(self, pid: int, payload: dict) -> dict:
        with self.lock:
            mid = self.next_milestone_id
            self.next_milestone_id += 1
            iid = mid  # 靶机里 id 与 iid 同号，够用
            ms = {
                "id": mid,
                "iid": iid,
                "project_id": pid,
                "title": payload.get("title") or "untitled",
                "description": payload.get("description") or "",
                "state": "active",
                "created_at": "2026-09-01T08:00:00.000Z",
                "updated_at": "2026-09-01T08:00:00.000Z",
                "start_date": payload.get("start_date"),
                "due_date": payload.get("due_date"),
                "web_url": f"http://localhost/-/milestones/{iid}",
            }
            self.milestones[(pid, mid)] = ms
            return ms

    # ------------------------------------------------------------ 评论
    def create_note(self, pid: int, iid: int, payload: dict, system: bool = False) -> dict:
        with self.lock:
            nid = self.next_note_id
            self.next_note_id += 1
            note = {
                "id": nid,
                "body": payload.get("body") or "",
                "author": {"id": 1, "username": "qa-bot", "name": "QA Bot",
                           "state": "active"},
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "system": system,
                "noteable_id": pid * 100 + iid,
                "noteable_iid": iid,
                "noteable_type": "Issue",
                "confidential": False,
            }
            self.notes[(pid, iid, nid)] = note
            return note

    # ------------------------------------------------------------ 合并请求
    def create_mr(self, pid: int, payload: dict) -> tuple[dict | None, str]:
        """返回 (mr, 错误原因)。原因用于区分 400 与 409。"""
        src = payload.get("source_branch") or ""
        tgt = payload.get("target_branch") or ""
        with self.lock:
            branches = self.branches.get(pid) or {}
            if src not in branches:
                return None, "source_branch does not exist"
            if tgt not in branches:
                return None, "target_branch does not exist"
            if src == tgt:
                return None, "source and target branch must differ"
            if any(m["source_branch"] == src and m["target_branch"] == tgt
                   and m["state"] == "opened"
                   for (p, _), m in self.mrs.items() if p == pid):
                return None, "open merge request already exists for this source branch"
            iid = self.next_mr_iid.get(pid, 1)
            self.next_mr_iid[pid] = iid + 1
            mr = {
                "id": pid * 1000 + iid,
                "iid": iid,
                "project_id": pid,
                "title": payload.get("title") or "untitled",
                "description": payload.get("description") or "",
                "state": "opened",
                "merge_status": "can_be_merged",
                "source_branch": src,
                "target_branch": tgt,
                "author": {"id": 1, "username": "qa-bot", "name": "QA Bot"},
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "web_url": f"http://localhost/-/merge_requests/{iid}",
                "sha": (self.commits.get(pid) or [{}])[0].get("id", "0" * 40),
                "merged_at": None,
                "changes_count": "1",
                "user_notes_count": 0,
            }
            self.mrs[(pid, iid)] = mr
            return mr, ""

    # ------------------------------------------------------------ 成员
    def add_member(self, scope: str, sid: int, payload: dict) -> dict | None:
        """scope: project / group。返回 None 表示用户不存在。"""
        uid = payload.get("user_id")
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return None
        with self.lock:
            if uid not in self.known_user_ids():
                return None
            brief = self.user_brief(uid) or {}
            member = {
                "id": uid,
                "username": brief.get("username", ""),
                "name": brief.get("name", ""),
                "state": "active",
                "access_level": int(payload.get("access_level") or 30),
                "created_at": "2026-09-01T08:00:00.000Z",
                "expires_at": payload.get("expires_at"),
            }
            if scope == "project":
                self.members[(sid, uid)] = member
            else:
                self.group_members[(sid, uid)] = member
            return member

    # ------------------------------------------------------------ 仓库
    def create_branch(self, pid: int, name: str, ref: str) -> dict | None:
        with self.lock:
            branches = self.branches.get(pid)
            if branches is None:
                return None
            if name in branches:
                return None          # 分支已存在 → 400
            if ref not in branches:
                return None          # 基准不存在 → 400
            src_commit = branches[ref]["commit"]
            branches[name] = {"name": name, "merged": False, "protected": False,
                              "default": False, "commit": dict(src_commit)}
            # 新分支继承基准分支的文件快照
            for (p, br, path), content in list(self.files.items()):
                if p == pid and br == ref:
                    self.files[(pid, name, path)] = content
            return branches[name]

    def write_file(self, pid: int, branch: str, path: str, content: str,
                   message: str) -> dict | None:
        with self.lock:
            if branch not in (self.branches.get(pid) or {}):
                return None
            self.files[(pid, branch, path)] = content
            return self._make_commit(message or f"Update {path}", pid)

    def remove_file(self, pid: int, branch: str, path: str, message: str) -> dict | None:
        with self.lock:
            if (pid, branch, path) not in self.files:
                return None
            del self.files[(pid, branch, path)]
            return self._make_commit(message or f"Delete {path}", pid)

    def create_tag(self, pid: int, name: str, ref: str, message: str) -> dict | None:
        with self.lock:
            if (pid, name) in self.tags:
                return None
            branches = self.branches.get(pid) or {}
            target = branches.get(ref, {}).get("commit", {}).get("id") or ref
            tag = {
                "name": name,
                "target": target,
                "message": message or "",
                "commit": {"id": target, "short_id": target[:8],
                           "title": "Initial commit",
                           "created_at": "2026-09-01T08:00:00.000Z"},
                "protected": False,
            }
            self.tags[(pid, name)] = tag
            return tag

    # ------------------------------------------------------------ 片段
    def create_snippet(self, payload: dict) -> dict:
        with self.lock:
            sid = self.next_snippet_id
            self.next_snippet_id += 1
            snippet = {
                "id": sid,
                "title": payload.get("title") or "untitled",
                "file_name": payload.get("file_name") or "snippet.txt",
                "description": payload.get("description") or "",
                "visibility": payload.get("visibility") or "private",
                "content": payload.get("content") or "",
                "web_url": f"http://localhost/-/snippets/{sid}",
                "raw_url": f"http://localhost/-/snippets/{sid}/raw",
                "created_at": "2026-09-01T08:00:00.000Z",
                "author": {"id": 1, "username": "qa-bot", "name": "QA Bot"},
            }
            self.snippets[sid] = snippet
            return snippet


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
            rows = [{
                "id": 1, "username": "qa-bot", "name": "QA Bot",
                "state": "active", "web_url": "http://localhost/qa-bot",
            }]
            rows += [dict(u, web_url=u.get("web_url", "")) for u in STATE.users_extra.values()]
            return self._paginated(rows, q)
        if path == "/users" and method == "POST":
            # 管理员端点。靶机里「已认证」即视为管理员——真实实例用普通 Token
            # 会拿到 403，那条边界由越权用例的第二身份覆盖，这里不重复造。
            body = self._body()
            missing = [k for k in ("username", "email", "password") if not body.get(k)]
            if missing:
                return self._error(400, "bad_request", f"{','.join(missing)} is missing")
            if any(u["username"] == body["username"] for u in STATE.users_extra.values()):
                return self._error(409, "conflict", "Username has already been taken")
            return self._send(201, STATE.create_user(body))

        m = re.fullmatch(r"/users/(\d+)/projects", path)
        if m and method == "GET":
            uid = int(m.group(1))
            if uid not in STATE.known_user_ids():
                return self._error(404, "not_found", "404 User Not Found")
            rows = sorted(STATE.projects.values(), key=lambda p: p["id"])
            return self._paginated(rows, q)

        m = re.fullmatch(r"/users/(\d+)", path)
        if m:
            uid = int(m.group(1))
            if method == "GET":
                brief = STATE.user_brief(uid)
                if brief is None:
                    return self._error(404, "not_found", "404 User Not Found")
                return self._send(200, dict(brief, created_at="2026-01-01T00:00:00.000Z",
                                            web_url=f"http://localhost/{brief['username']}"))
            if method == "DELETE":
                # 管理员删除用户。内置用户（id=1）不允许删——真实 GitLab 对
                # 实例所有者也是这个态度，替身跟着做，用例才不会在真实环境上翻车
                if uid == 1:
                    return self._error(403, "forbidden", "403 Forbidden")
                with STATE.lock:
                    existed = STATE.users_extra.pop(uid, None) is not None
                if not existed:
                    return self._error(404, "not_found", "404 User Not Found")
                for key in [k for k in STATE.members if k[1] == uid]:
                    del STATE.members[key]
                return self._send(204, None)

        if path == "/user/keys" and method == "GET":
            return self._send(200, [])

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
                # namespace_id：在指定群组下创建。忽略它的话，
                # 「群组下建的项目归该群组」这条语义在靶机上永远测不到
                if body.get("namespace_id") is not None:
                    grp = STATE.groups.get(int(body["namespace_id"]))
                    if grp is not None:
                        proj["namespace"] = {"id": grp["id"], "name": grp["name"],
                                             "path": grp["path"], "kind": "group"}
                        proj["path_with_namespace"] = f"{grp['path']}/{proj['path']}"
                return self._send(201, proj)

        # 项目成员候选用户：真实 GitLab 用它做 @ 提醒的候选列表
        m = re.fullmatch(r"/projects/([^/]+)/users", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            rows = [{"id": uid, "username": b["username"], "name": b["name"],
                     "state": "active"}
                    for uid in sorted(STATE.known_user_ids())
                    if (b := STATE.user_brief(uid))]
            return self._paginated(rows, q)

        m = re.fullmatch(r"/projects/([^/]+)", path)
        if m:
            raw = m.group(1)
            pid = _as_int(raw)
            proj = STATE.projects.get(pid) if pid is not None else None
            if proj is None and pid is None:
                # 按完整路径访问（namespace/project）。这条语义和按 id 访问
                # 必须分开：前端大量依赖 path 路由，替身不支持就测不到。
                full = urllib.parse.unquote(raw)
                proj = next((p for p in STATE.projects.values()
                             if p.get("path_with_namespace") == full), None)
            if method == "GET":
                if proj is None:
                    return self._error(404, "not_found", "404 Project Not Found")
                return self._send(200, proj)
            if method == "PUT":
                if proj is None:
                    return self._error(404, "not_found", "404 Project Not Found")
                body = self._body()
                for k in ("name", "description", "visibility", "default_branch"):
                    if k in body:
                        proj[k] = body[k]
                return self._send(200, proj)
            if method == "DELETE":
                target = proj["id"] if proj is not None else (pid or -1)
                if not STATE.delete_project(target):
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
                    # 关闭动作会产生一条系统评论。真实 GitLab 就是这样，
                    # 前端靠 system=true 把它渲染成「某某关闭了议题」而不是一条人写的留言。
                    STATE.create_note(pid, iid, {"body": "closed"}, system=True)
                elif body.get("state_event") == "reopen":
                    iss["state"] = "opened"
                    iss["closed_at"] = None
                    STATE.create_note(pid, iid, {"body": "reopened"}, system=True)
                if "milestone_id" in body:
                    iss["milestone_id"] = body.get("milestone_id")
                iss["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                proj = STATE.projects.get(pid)
                if proj is not None:
                    proj["open_issues_count"] = sum(
                        1 for i in STATE.issues.values()
                        if i["project_id"] == pid and i["state"] == "opened"
                    )
                return self._send(200, iss)

        # ---- 仓库
        # 项目不存在一律 404：真实 GitLab 对不存在的仓库就是这个行为。
        # 早期的靶机不管 pid 直接返回数据，于是「拼错路径」的用例永远测不出问题。
        m = re.fullmatch(r"/projects/([^/]+)/repository/tree", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            ref = q.get("ref") or "main"
            rows = [{"id": f"blob-{fp}", "name": fp, "type": "blob",
                     "path": fp, "mode": "100644"}
                    for (p, br, fp) in STATE.files if p == pid and br == ref]
            rows.sort(key=lambda r: r["name"])
            return self._paginated(rows, q)

        m = re.fullmatch(r"/projects/([^/]+)/repository/commits", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            return self._paginated(list(STATE.commits.get(pid, [])), q)
        if m and method == "POST":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            body = self._body()
            branch = body.get("branch") or ""
            message = body.get("commit_message") or ""
            actions = body.get("actions") or []
            branches = STATE.branches.get(pid) or {}
            if branch not in branches:
                return self._error(400, "bad_request", "branch does not exist")
            if not message or not actions:
                return self._error(400, "bad_request", "commit_message/actions is missing")
            for act in actions:
                kind = act.get("action")
                fp = act.get("file_path") or ""
                with STATE.lock:
                    exists = (pid, branch, fp) in STATE.files
                if kind == "create" and exists:
                    return self._error(400, "bad_request", f"{fp} already exists")
                if kind in ("update", "delete") and not exists:
                    return self._error(400, "bad_request", f"{fp} does not exist")
                if kind == "delete":
                    STATE.remove_file(pid, branch, fp, message)
                else:
                    STATE.write_file(pid, branch, fp, act.get("content") or "", message)
            return self._send(201, STATE.commits.get(pid, [{}])[0])

        m = re.fullmatch(r"/projects/([^/]+)/repository/commits/([^/]+)", path)
        if m and method == "GET":
            pid, sha = _as_int(m.group(1)), urllib.parse.unquote(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            commit = next((c for c in STATE.commits.get(pid, [])
                           if c["id"] == sha or c["id"].startswith(sha)), None)
            if commit is None:
                return self._error(404, "not_found", "404 Commit Not Found")
            return self._send(200, commit)

        m = re.fullmatch(r"/projects/([^/]+)/repository/branches", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            rows = sorted((STATE.branches.get(pid) or {}).values(),
                          key=lambda b: b["name"])
            return self._paginated(rows, q)
        if m and method == "POST":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            name, ref = q.get("branch") or "", q.get("ref") or ""
            if not name or not ref:
                return self._error(400, "bad_request", "branch/ref is missing")
            branch = STATE.create_branch(pid, name, ref)
            if branch is None:
                # 已存在或基准不存在，都表现为 400——真实 GitLab 的措辞不同，
                # 但状态码一致，用例按状态码断言即可
                return self._error(400, "bad_request", "branch already exists or ref invalid")
            return self._send(201, branch)

        m = re.fullmatch(r"/projects/([^/]+)/repository/branches/([^/]+)", path)
        if m:
            pid = _as_int(m.group(1))
            name = urllib.parse.unquote(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            branch = (STATE.branches.get(pid) or {}).get(name)
            if method == "GET":
                if branch is None:
                    return self._error(404, "not_found", "404 Branch Not Found")
                return self._send(200, branch)
            if method == "DELETE":
                if branch is None:
                    return self._error(404, "not_found", "404 Branch Not Found")
                if branch.get("default"):
                    # 默认分支删不掉：这是 GitLab 的保护，不是缺陷
                    return self._error(400, "bad_request", "Cannot delete the default branch")
                with STATE.lock:
                    del STATE.branches[pid][name]
                    for key in [k for k in STATE.files if k[0] == pid and k[1] == name]:
                        del STATE.files[key]
                return self._send(204, None)

        m = re.fullmatch(r"/projects/([^/]+)/repository/branches/([^/]+)/protect", path)
        if m and method == "PUT":
            pid = _as_int(m.group(1))
            name = urllib.parse.unquote(m.group(2))
            branch = (STATE.branches.get(pid) or {}).get(name)
            if branch is None:
                return self._error(404, "not_found", "404 Branch Not Found")
            branch["protected"] = True
            return self._send(200, branch)

        m = re.fullmatch(r"/projects/([^/]+)/repository/files/([^/]+)", path)
        if m:
            pid = _as_int(m.group(1))
            fp = urllib.parse.unquote(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            ref = q.get("ref") or "main"
            if method == "GET":
                content = STATE.files.get((pid, ref, fp))
                if content is None:
                    return self._error(404, "not_found", "404 File Not Found")
                head = (STATE.commits.get(pid) or [{}])[0]
                return self._send(200, {
                    "file_path": fp, "branch": ref, "ref": ref,
                    "encoding": "base64",
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "blob_id": "b" * 40,
                    "commit_id": head.get("id", ""),
                    "last_commit_id": head.get("id", ""),
                })
            body = self._body()
            branch = body.get("branch") or ref
            message = body.get("commit_message") or ""
            if branch not in (STATE.branches.get(pid) or {}):
                return self._error(400, "bad_request", "branch does not exist")
            if not message:
                return self._error(400, "bad_request", "commit_message is missing")
            exists = (pid, branch, fp) in STATE.files
            if method == "POST":
                if exists:
                    return self._error(400, "bad_request", "file already exists")
                commit = STATE.write_file(pid, branch, fp, body.get("content") or "", message)
                return self._send(201, {"file_path": fp, "branch": branch})
            if method == "PUT":
                if not exists:
                    return self._error(404, "not_found", "404 File Not Found")
                STATE.write_file(pid, branch, fp, body.get("content") or "", message)
                return self._send(200, {"file_path": fp, "branch": branch})
            if method == "DELETE":
                if not exists:
                    return self._error(404, "not_found", "404 File Not Found")
                STATE.remove_file(pid, branch, fp, message)
                return self._send(204, None)

        m = re.fullmatch(r"/projects/([^/]+)/languages", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            return self._send(200, {"Python": 85.5, "Shell": 14.5})

        m = re.fullmatch(r"/projects/([^/]+)/repository/tags", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            rows = [t for (p, _n), t in STATE.tags.items() if p == pid]
            return self._paginated(rows, q)
        if m and method == "POST":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            body = self._body()
            name = body.get("tag_name") or ""
            if not name:
                return self._error(400, "bad_request", "tag_name is missing")
            tag = STATE.create_tag(pid, name, body.get("ref") or "main",
                                   body.get("message") or "")
            if tag is None:
                return self._error(409, "conflict", "tag already exists")
            return self._send(201, tag)

        m = re.fullmatch(r"/projects/([^/]+)/repository/compare", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            frm, to = q.get("from") or "main", q.get("to") or "main"
            return self._send(200, {
                "commit": {},
                "commits": list(STATE.commits.get(pid, []))[:5],
                "diffs": [],
                "compare_timeout": False,
                "compare_same_ref": frm == to,
            })

        # ---- 标签
        m = re.fullmatch(r"/projects/([^/]+)/labels", path)
        if m:
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            if method == "GET":
                rows = sorted((lb for (p, _), lb in STATE.labels.items() if p == pid),
                              key=lambda l: l["id"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                if not (body.get("name") or "").strip():
                    return self._error(400, "bad_request", "name is missing")
                label = STATE.create_label(pid, body)
                if label is None:
                    # 同名重复创建：GitLab 返回 409，且 body 里带 message。
                    # 数据工厂重跑时最先炸在这里，所以这条语义必须有
                    return self._error(409, "conflict", "Label already exists")
                return self._send(201, label)

        m = re.fullmatch(r"/projects/([^/]+)/labels/(\d+)", path)
        if m:
            pid, lid = _as_int(m.group(1)), int(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            label = STATE.labels.get((pid, lid))
            if method == "GET":
                if label is None:
                    return self._error(404, "not_found", "404 Label Not Found")
                return self._send(200, label)
            if method == "PUT":
                if label is None:
                    return self._error(404, "not_found", "404 Label Not Found")
                body = self._body()
                # new_name 是 GitLab 改名的字段名，改完响应里仍叫 name
                if new_name := body.get("new_name"):
                    label["name"] = new_name
                for k in ("name", "color", "description"):
                    if k in body:
                        label[k] = body[k]
                return self._send(200, label)
            if method == "DELETE":
                if label is None:
                    return self._error(404, "not_found", "404 Label Not Found")
                with STATE.lock:
                    del STATE.labels[(pid, lid)]
                return self._send(204, None)

        m = re.fullmatch(r"/groups/(\d+)/labels", path)
        if m and method == "GET":
            # 群组标签在靶机里按群组分桶存：真实 GitLab 的群组标签与项目标签
            # 是两套独立集合，混在一起会让「继承关系」的断言失去意义
            gid = int(m.group(1))
            rows = sorted((lb for (g, _), lb in STATE.labels.items() if g == -gid),
                          key=lambda l: l["id"])
            return self._paginated(rows, q)

        # ---- 里程碑
        m = re.fullmatch(r"/projects/([^/]+)/milestones", path)
        if m:
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            if method == "GET":
                rows = [ms for (p, _), ms in STATE.milestones.items() if p == pid]
                if state := q.get("state"):
                    rows = [m2 for m2 in rows if m2["state"] == state]
                rows.sort(key=lambda x: x["id"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                if not (body.get("title") or "").strip():
                    return self._error(400, "bad_request", "title is missing")
                return self._send(201, STATE.create_milestone(pid, body))

        m = re.fullmatch(r"/projects/([^/]+)/milestones/(\d+)", path)
        if m:
            pid, mid = _as_int(m.group(1)), int(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            ms = STATE.milestones.get((pid, mid))
            if method == "GET":
                if ms is None:
                    return self._error(404, "not_found", "404 Milestone Not Found")
                return self._send(200, ms)
            if method == "PUT":
                if ms is None:
                    return self._error(404, "not_found", "404 Milestone Not Found")
                body = self._body()
                for k in ("title", "description", "due_date", "start_date"):
                    if k in body:
                        ms[k] = body[k]
                # 关闭/重启：active ↔ closed 是里程碑的两态，和议题的 opened/closed
                # 命名不一致，是最容易写错断言的地方之一
                if (event := body.get("state_event")) == "close":
                    ms["state"] = "closed"
                elif event == "activate":
                    ms["state"] = "active"
                ms["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                return self._send(200, ms)
            if method == "DELETE":
                if ms is None:
                    return self._error(404, "not_found", "404 Milestone Not Found")
                with STATE.lock:
                    del STATE.milestones[(pid, mid)]
                return self._send(204, None)

        m = re.fullmatch(r"/projects/([^/]+)/milestones/(\d+)/issues", path)
        if m and method == "GET":
            pid, mid = _as_int(m.group(1)), int(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            if (pid, mid) not in STATE.milestones:
                return self._error(404, "not_found", "404 Milestone Not Found")
            rows = [i for (p, _), i in STATE.issues.items()
                    if p == pid and i.get("milestone_id") == mid]
            return self._paginated(rows, q)

        # ---- 议题评论
        m = re.fullmatch(r"/projects/([^/]+)/issues/(\d+)/notes", path)
        if m:
            pid, iid = _as_int(m.group(1)), int(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            if (pid, iid) not in STATE.issues:
                return self._error(404, "not_found", "404 Issue Not Found")
            if method == "GET":
                rows = [n for (p, i, _), n in STATE.notes.items() if p == pid and i == iid]
                rows.sort(key=lambda n: n["id"])
                if q.get("sort") == "desc":
                    rows.reverse()
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                if not (body.get("body") or "").strip():
                    return self._error(400, "bad_request", "body is missing")
                return self._send(201, STATE.create_note(pid, iid, body))

        m = re.fullmatch(r"/projects/([^/]+)/issues/(\d+)/notes/(\d+)", path)
        if m:
            pid, iid, nid = _as_int(m.group(1)), int(m.group(2)), int(m.group(3))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            note = STATE.notes.get((pid, iid, nid))
            if method == "GET":
                if note is None:
                    return self._error(404, "not_found", "404 Note Not Found")
                return self._send(200, note)
            if method == "PUT":
                if note is None:
                    return self._error(404, "not_found", "404 Note Not Found")
                body = self._body()
                if "body" in body:
                    note["body"] = body["body"]
                note["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                return self._send(200, note)
            if method == "DELETE":
                if note is None:
                    return self._error(404, "not_found", "404 Note Not Found")
                with STATE.lock:
                    del STATE.notes[(pid, iid, nid)]
                return self._send(204, None)

        # ---- 合并请求
        m = re.fullmatch(r"/projects/([^/]+)/merge_requests", path)
        if m:
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            if method == "GET":
                rows = [mr for (p, _), mr in STATE.mrs.items() if p == pid]
                if state := q.get("state"):
                    rows = [r for r in rows if r["state"] == state]
                rows.sort(key=lambda r: r["iid"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                if not (body.get("title") or "").strip():
                    return self._error(400, "bad_request", "title is missing")
                mr, reason = STATE.create_mr(pid, body)
                if mr is None:
                    code = 409 if "already exists" in reason else 400
                    return self._error(code, "bad_request", reason)
                return self._send(201, mr)

        m = re.fullmatch(r"/projects/([^/]+)/merge_requests/(\d+)", path)
        if m:
            pid, iid = _as_int(m.group(1)), int(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            mr = STATE.mrs.get((pid, iid))
            if method == "GET":
                if mr is None:
                    return self._error(404, "not_found", "404 Merge Request Not Found")
                return self._send(200, mr)
            if method == "PUT":
                if mr is None:
                    return self._error(404, "not_found", "404 Merge Request Not Found")
                body = self._body()
                for k in ("title", "description", "target_branch", "assignee_id"):
                    if k in body:
                        mr[k] = body[k]
                if (event := body.get("state_event")) == "close":
                    mr["state"] = "closed"
                elif event == "reopen":
                    mr["state"] = "opened"
                mr["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                return self._send(200, mr)

        m = re.fullmatch(r"/projects/([^/]+)/merge_requests/(\d+)/changes", path)
        if m and method == "GET":
            pid, iid = _as_int(m.group(1)), int(m.group(2))
            mr = STATE.mrs.get((pid, iid))
            if mr is None:
                return self._error(404, "not_found", "404 Merge Request Not Found")
            return self._send(200, dict(mr, changes=[
                {"old_path": "README.md", "new_path": "README.md",
                 "a_mode": "100644", "b_mode": "100644",
                 "new_file": False, "renamed_file": False, "deleted_file": False,
                 "diff": "@@ -1 +1,2 @@\n+# updated by qa\n"},
            ]))

        m = re.fullmatch(r"/projects/([^/]+)/merge_requests/(\d+)/merge", path)
        if m and method == "PUT":
            pid, iid = _as_int(m.group(1)), int(m.group(2))
            mr = STATE.mrs.get((pid, iid))
            if mr is None:
                return self._error(404, "not_found", "404 Merge Request Not Found")
            if mr["state"] == "merged":
                return self._error(405, "method_not_allowed", "405 Method Not Allowed")
            if mr["state"] == "closed":
                return self._error(406, "not_acceptable", "406 Not Acceptable")
            mr["state"] = "merged"
            mr["merged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            mr["merge_status"] = "merged"
            return self._send(200, mr)

        # ---- 成员
        m = re.fullmatch(r"/projects/([^/]+)/members", path)
        if m:
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            if method == "GET":
                rows = sorted((mb for (p, _), mb in STATE.members.items() if p == pid),
                              key=lambda x: x["id"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                with STATE.lock:
                    exists = (pid, int(body.get("user_id") or -1)) in STATE.members
                if exists:
                    return self._error(409, "conflict", "Member already exists")
                member = STATE.add_member("project", pid, body)
                if member is None:
                    return self._error(404, "not_found", "404 User Not Found")
                return self._send(201, member)

        m = re.fullmatch(r"/projects/([^/]+)/members/(\d+)", path)
        if m:
            pid, uid = _as_int(m.group(1)), int(m.group(2))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            member = STATE.members.get((pid, uid))
            if method == "GET":
                if member is None:
                    return self._error(404, "not_found", "404 Membership Not Found")
                return self._send(200, member)
            if method == "PUT":
                if member is None:
                    return self._error(404, "not_found", "404 Membership Not Found")
                member["access_level"] = int(self._body().get("access_level")
                                             or member["access_level"])
                return self._send(200, member)
            if method == "DELETE":
                if member is None:
                    return self._error(404, "not_found", "404 Membership Not Found")
                with STATE.lock:
                    del STATE.members[(pid, uid)]
                return self._send(204, None)

        m = re.fullmatch(r"/groups/(\d+)/members", path)
        if m:
            gid = int(m.group(1))
            if method == "GET":
                rows = sorted((mb for (g, _), mb in STATE.group_members.items() if g == gid),
                              key=lambda x: x["id"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                with STATE.lock:
                    exists = (gid, int(body.get("user_id") or -1)) in STATE.group_members
                if exists:
                    return self._error(409, "conflict", "Member already exists")
                member = STATE.add_member("group", gid, body)
                if member is None:
                    return self._error(404, "not_found", "404 User Not Found")
                return self._send(201, member)

        # ---- Wiki
        m = re.fullmatch(r"/projects/([^/]+)/wikis", path)
        if m and method == "GET":
            pid = _as_int(m.group(1))
            if pid not in STATE.projects:
                return self._error(404, "not_found", "404 Project Not Found")
            return self._paginated([{
                "slug": "home", "title": "home", "format": "markdown",
                "encoding": "UTF-8", "content": "# Home\n\n由靶机生成的 Wiki 首页。",
            }], q)

        # ---- 群组
        if path == "/groups" and method == "GET":
            return self._paginated(sorted(STATE.groups.values(), key=lambda g: g["id"]), q)
        if path == "/groups" and method == "POST":
            body = self._body()
            if not (body.get("name") or "").strip():
                return self._error(400, "bad_request", "name is missing")
            return self._send(201, STATE.create_group(body))

        m = re.fullmatch(r"/groups/(\d+)", path)
        if m:
            gid = int(m.group(1))
            grp = STATE.groups.get(gid)
            if method == "GET":
                if grp is None:
                    return self._error(404, "not_found", "404 Group Not Found")
                return self._send(200, grp)
            if method == "PUT":
                if grp is None:
                    return self._error(404, "not_found", "404 Group Not Found")
                body = self._body()
                for k in ("name", "path", "description", "visibility"):
                    if k in body:
                        grp[k] = body[k]
                return self._send(200, grp)
            if method == "DELETE":
                if grp is None:
                    return self._error(404, "not_found", "404 Group Not Found")
                with STATE.lock:
                    del STATE.groups[gid]
                    for key in [k for k in STATE.group_members if k[0] == gid]:
                        del STATE.group_members[key]
                return self._send(202, {"message": "202 Accepted"})

        m = re.fullmatch(r"/groups/(\d+)/projects", path)
        if m:
            gid = int(m.group(1))
            if gid not in STATE.groups:
                return self._error(404, "not_found", "404 Group Not Found")
            if method == "GET":
                rows = [p for p in STATE.projects.values()
                        if (p.get("namespace") or {}).get("id") == gid]
                rows.sort(key=lambda p: p["id"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                if not (body.get("name") or "").strip():
                    return self._error(400, "bad_request", "name is missing")
                proj = STATE.create_project(body)
                grp = STATE.groups[gid]
                proj["namespace"] = {"id": gid, "name": grp["name"],
                                     "path": grp["path"], "kind": "group"}
                proj["path_with_namespace"] = f"{grp['path']}/{proj['path']}"
                return self._send(201, proj)

        m = re.fullmatch(r"/groups/(\d+)/subgroups", path)
        if m:
            gid = int(m.group(1))
            if gid not in STATE.groups:
                return self._error(404, "not_found", "404 Group Not Found")
            if method == "GET":
                rows = [g for g in STATE.groups.values() if g.get("parent_id") == gid]
                rows.sort(key=lambda g: g["id"])
                return self._paginated(rows, q)
            if method == "POST":
                body = self._body()
                if not (body.get("name") or "").strip():
                    return self._error(400, "bad_request", "name is missing")
                sub = STATE.create_group(dict(body, parent_id=gid))
                sub["full_path"] = f"{STATE.groups[gid]['path']}/{sub['path']}"
                return self._send(201, sub)

        # ---- 实例级
        if path == "/namespaces" and method == "GET":
            rows = [{"id": g["id"], "name": g["name"], "path": g["path"],
                     "kind": "group", "full_path": g.get("full_path", g["path"])}
                    for g in STATE.groups.values()]
            rows.append({"id": 1, "name": "QA Bot", "path": "qa-bot",
                         "kind": "user", "full_path": "qa-bot"})
            rows.sort(key=lambda r: r["id"])
            return self._paginated(rows, q)

        if path == "/search" and method == "GET":
            scope, term = q.get("scope") or "", (q.get("search") or "").lower()
            if not scope:
                return self._error(400, "bad_request", "scope is missing")
            if scope == "projects":
                rows = [p for p in STATE.projects.values()
                        if term in p["name"].lower() or term in p["path"].lower()]
                rows.sort(key=lambda p: p["id"])
                return self._paginated(rows, q)
            if scope == "issues":
                rows = [i for i in STATE.issues.values()
                        if term in (i["title"] or "").lower()]
                return self._paginated(rows, q)
            return self._paginated([], q)

        if path == "/snippets" and method == "GET":
            rows = sorted(STATE.snippets.values(), key=lambda s: s["id"])
            return self._paginated(rows, q)
        if path == "/snippets" and method == "POST":
            body = self._body()
            if not (body.get("title") or "").strip() or not body.get("file_name"):
                return self._error(400, "bad_request", "title/file_name is missing")
            return self._send(201, STATE.create_snippet(body))

        m = re.fullmatch(r"/snippets/(\d+)", path)
        if m:
            sid = int(m.group(1))
            snippet = STATE.snippets.get(sid)
            if method == "GET":
                if snippet is None:
                    return self._error(404, "not_found", "404 Snippet Not Found")
                return self._send(200, snippet)
            if method == "DELETE":
                if snippet is None:
                    return self._error(404, "not_found", "404 Snippet Not Found")
                with STATE.lock:
                    del STATE.snippets[sid]
                return self._send(204, None)

        if path == "/features" and method == "GET":
            # 管理员端点：特性开关列表。靶机给一份固定的，
            # 断言价值在于「非管理员拿不到」而不是具体内容
            return self._send(200, [
                {"name": "issue_weights", "state": "on", "gates": []},
                {"name": "merge_request_approvers", "state": "off", "gates": []},
            ])

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
