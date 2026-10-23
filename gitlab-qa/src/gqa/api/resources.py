"""GitLab 资源封装。

用例里不出现裸路径字符串，只跟资源对象打交道。
这样接口路径变更时只改这一层，用例一行不动——这是分层最实际的收益。
"""

from __future__ import annotations

from typing import Any

from gqa.api.client import GitLabClient, Response


class System:
    """系统级只读接口：不需要 Token 也能访问，天然适合做冒烟。"""

    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def version(self) -> Response:
        return self.c.get("/version", expect=200)

    def metadata(self) -> Response:
        return self.c.get("/metadata", expect=200)

    def statistics(self) -> Response:
        return self.c.get("/application/statistics", expect=(200, 403))


class Projects:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, **params) -> list[dict]:
        return self.c.paginate("/projects", params)

    def get(self, project_id: int | str) -> Response:
        return self.c.get(f"/projects/{self._enc(project_id)}", expect=200)

    def try_get(self, project_id: int | str) -> Response:
        """允许 404：用于「已删除后确认不存在」这类断言。"""
        return self.c.get(f"/projects/{self._enc(project_id)}", expect=(200, 404))

    def create(self, name: str, **extra) -> Response:
        payload: dict[str, Any] = {"name": name}
        payload.update(extra)
        return self.c.post("/projects", json_body=payload, expect=201)

    def delete(self, project_id: int | str) -> Response:
        return self.c.delete(f"/projects/{self._enc(project_id)}", expect=(202, 204, 404))

    def delete_permanently(self, project_id: int | str, full_path: str = "") -> Response:
        """立即清除（需要管理员权限）。

        前提：项目必须已经处于待删除状态，否则返回
        400 "Project must be marked for deletion first"。
        所以外部应优先用 purge()，而不是直接调这个方法。
        """
        return self.c.delete(
            f"/projects/{self._enc(project_id)}",
            params={"permanently_remove": "true", "full_path": full_path},
            expect=(202, 204, 404),
        )

    def purge(self, project_id: int | str, known_path: str = "") -> Response:
        """彻底删除，跳过「延迟删除」的保留期。

        为什么要这么绕：GitLab 默认把删除的项目先标记待删除、保留若干天
        （本机实测默认 30 天，最小只能设 1）。测试每跑一轮就堆一批僵尸项目，
        项目列表页会越来越慢，最终在 UI 用例上表现为莫名其妙的 timeout ——
        排查时没人会想到是清理方式的问题。

        两步走，因为永久删除接口要求项目先被标记：
          1) 普通删除 → 标记待删除
          2) 重新取回当前 path（标记后项目名会被改写，
             用旧 path 会 400）→ 永久清除
        """
        current = self.try_get(project_id)
        if current.status == 404:
            return current  # 已经没了
        if current.status != 200:
            return current
        body = current.json_data if isinstance(current.json_data, dict) else {}
        path = body.get("path_with_namespace") or known_path
        if not path:
            return current

        # 只有「尚未标记」才需要第一步。用例自己删过的情况很常见
        # （例如验证删除行为的用例），此时再 DELETE 会收到
        # 400 already marked for deletion，导致整个清理失败、项目残留。
        if not (body.get("marked_for_deletion_at") or body.get("marked_for_deletion_on")):
            try:
                self.delete(project_id)
            except Exception:  # noqa: BLE001
                pass
            # 标记后项目名会被改写，path 必须重新取，否则永久删除会 400
            after = self.try_get(project_id)
            if after.status == 404:
                return after  # 该实例未开启延迟删除，一步就干净
            if after.status == 200:
                b2 = after.json_data if isinstance(after.json_data, dict) else {}
                path = b2.get("path_with_namespace") or path

        return self.delete_permanently(project_id, path)

    def search(self, term: str) -> list[dict]:
        return self.c.paginate("/projects", {"search": term})

    @staticmethod
    def _enc(project_id: int | str) -> str:
        # 路径含 / 时必须 URL 编码（namespace/project 形式）
        return str(project_id).replace("/", "%2F")


class Issues:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, project_id: int | str, **params) -> list[dict]:
        return self.c.paginate(f"/projects/{Projects._enc(project_id)}/issues", params)

    def create(self, project_id: int | str, title: str, **extra) -> Response:
        payload: dict[str, Any] = {"title": title}
        payload.update(extra)
        return self.c.post(f"/projects/{Projects._enc(project_id)}/issues",
                           json_body=payload, expect=201)

    def get(self, project_id: int | str, iid: int) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/issues/{iid}", expect=200)

    def update(self, project_id: int | str, iid: int, **fields) -> Response:
        return self.c.put(f"/projects/{Projects._enc(project_id)}/issues/{iid}",
                          json_body=fields, expect=200)

    def close(self, project_id: int | str, iid: int) -> Response:
        return self.update(project_id, iid, state_event="close")


class Users:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def current(self) -> Response:
        """当前令牌对应的用户。拿不到就说明 Token 无效——最直接的鉴权探针。"""
        return self.c.get("/user", expect=(200, 401))

    def list(self, **params) -> list[dict]:
        return self.c.paginate("/users", params)

    def get(self, user_id: int) -> Response:
        return self.c.get(f"/users/{user_id}", expect=(200, 404))


class Groups:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, **params) -> list[dict]:
        return self.c.paginate("/groups", params)

    def create(self, name: str, path: str, **extra) -> Response:
        payload: dict[str, Any] = {"name": name, "path": path}
        payload.update(extra)
        return self.c.post("/groups", json_body=payload, expect=201)

    def delete(self, group_id: int | str) -> Response:
        return self.c.delete(f"/groups/{group_id}", expect=(202, 204, 404))


class Repository:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def tree(self, project_id: int | str, **params) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/repository/tree",
                          params=params, expect=(200, 404))

    def commits(self, project_id: int | str, **params) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/repository/commits",
                          params=params, expect=(200, 404))

    def branches(self, project_id: int | str) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/repository/branches",
                          expect=(200, 404))


class GitLab:
    """资源门面：用例只拿这一个对象。"""

    def __init__(self, client: GitLabClient) -> None:
        self.client = client
        self.system = System(client)
        self.projects = Projects(client)
        self.issues = Issues(client)
        self.users = Users(client)
        self.groups = Groups(client)
        self.repository = Repository(client)
