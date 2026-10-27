"""GitLab 资源封装。

用例里不出现裸路径字符串，只跟资源对象打交道。
这样接口路径变更时只改这一层，用例一行不动——这是分层最实际的收益。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

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

    def update(self, project_id: int | str, **fields) -> Response:
        return self.c.put(f"/projects/{self._enc(project_id)}",
                          json_body=fields, expect=200)

    def by_path(self, path: str) -> Response:
        """按 namespace/project 取项目：验证「路径也能当主键」这条语义。"""
        return self.c.get(f"/projects/{self._enc(path)}", expect=(200, 404))

    def users(self, project_id: int | str) -> list[dict]:
        return self.c.paginate(f"/projects/{self._enc(project_id)}/users")

    def languages(self, project_id: int | str) -> Response:
        """语言占比。空仓库返回空对象而不是 404，这条差异值得留一条用例。"""
        return self.c.get(f"/projects/{self._enc(project_id)}/languages",
                          expect=(200, 404))

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

    def delete(self, project_id: int | str, iid: int) -> Response:
        return self.c.delete(f"/projects/{Projects._enc(project_id)}/issues/{iid}",
                             expect=(200, 204, 404))


class Labels:
    """项目标签。

    标签是「幂等资源」的典型：同名重复创建会 409。
    这条语义不测的话，数据工厂一旦重跑就会炸在莫名其妙的地方。
    """

    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, project_id: int | str, **params) -> list[dict]:
        return self.c.paginate(f"/projects/{Projects._enc(project_id)}/labels", params)

    def try_list(self, project_id: int | str) -> Response:
        """允许 404：项目不存在时的错误语义断言要用。"""
        return self.c.get(f"/projects/{Projects._enc(project_id)}/labels",
                          expect=(200, 404))

    def create(self, project_id: int | str, name: str, color: str = "#0033cc",
               **extra) -> Response:
        payload: dict[str, Any] = {"name": name, "color": color}
        payload.update(extra)
        return self.c.post(f"/projects/{Projects._enc(project_id)}/labels",
                           json_body=payload, expect=(201, 409))

    def try_get(self, project_id: int | str, label_id: int | str) -> Response:
        """允许 404：用于「删除后确认不存在」这类断言。"""
        return self.c.get(f"/projects/{Projects._enc(project_id)}/labels/{label_id}",
                          expect=(200, 404))

    def update(self, project_id: int | str, label_id: int | str, **fields) -> Response:
        return self.c.put(f"/projects/{Projects._enc(project_id)}/labels/{label_id}",
                          json_body=fields, expect=200)

    def delete(self, project_id: int | str, label_id: int | str) -> Response:
        return self.c.delete(f"/projects/{Projects._enc(project_id)}/labels/{label_id}",
                             expect=(200, 204, 404))

    def group_list(self, group_id: int | str) -> list[dict]:
        return self.c.paginate(f"/groups/{group_id}/labels")


class Milestones:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, project_id: int | str, **params) -> list[dict]:
        return self.c.paginate(f"/projects/{Projects._enc(project_id)}/milestones", params)

    def create(self, project_id: int | str, title: str, **extra) -> Response:
        payload: dict[str, Any] = {"title": title}
        payload.update(extra)
        return self.c.post(f"/projects/{Projects._enc(project_id)}/milestones",
                           json_body=payload, expect=201)

    def get(self, project_id: int | str, milestone_id: int) -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/milestones/{milestone_id}",
            expect=(200, 404))

    def update(self, project_id: int | str, milestone_id: int, **fields) -> Response:
        return self.c.put(
            f"/projects/{Projects._enc(project_id)}/milestones/{milestone_id}",
            json_body=fields, expect=200)

    def delete(self, project_id: int | str, milestone_id: int) -> Response:
        return self.c.delete(
            f"/projects/{Projects._enc(project_id)}/milestones/{milestone_id}",
            expect=(200, 204, 404))

    def issues(self, project_id: int | str, milestone_id: int) -> list[dict]:
        return self.c.paginate(
            f"/projects/{Projects._enc(project_id)}/milestones/{milestone_id}/issues")


class Notes:
    """议题评论（discussion 之外的老式 note 接口，CE 上最常用）。"""

    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, project_id: int | str, iid: int, **params) -> list[dict]:
        return self.c.paginate(
            f"/projects/{Projects._enc(project_id)}/issues/{iid}/notes", params)

    def create(self, project_id: int | str, iid: int, body: str, **extra) -> Response:
        payload: dict[str, Any] = {"body": body}
        payload.update(extra)
        return self.c.post(f"/projects/{Projects._enc(project_id)}/issues/{iid}/notes",
                           json_body=payload, expect=201)

    def get(self, project_id: int | str, iid: int, note_id: int) -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/issues/{iid}/notes/{note_id}",
            expect=(200, 404))

    def update(self, project_id: int | str, iid: int, note_id: int, body: str) -> Response:
        return self.c.put(
            f"/projects/{Projects._enc(project_id)}/issues/{iid}/notes/{note_id}",
            json_body={"body": body}, expect=200)

    def delete(self, project_id: int | str, iid: int, note_id: int) -> Response:
        """删除评论在部分版本返回 204，在另一些返回 200 + 空体，都要接受。"""
        return self.c.delete(
            f"/projects/{Projects._enc(project_id)}/issues/{iid}/notes/{note_id}",
            expect=(200, 202, 204, 404))


class MergeRequests:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, project_id: int | str, **params) -> list[dict]:
        return self.c.paginate(
            f"/projects/{Projects._enc(project_id)}/merge_requests", params)

    def create(self, project_id: int | str, source_branch: str, target_branch: str,
               title: str, **extra) -> Response:
        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        }
        payload.update(extra)
        return self.c.post(f"/projects/{Projects._enc(project_id)}/merge_requests",
                           json_body=payload, expect=(201, 400, 409))

    def get(self, project_id: int | str, mr_iid: int) -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/merge_requests/{mr_iid}",
            expect=(200, 404))

    def update(self, project_id: int | str, mr_iid: int, **fields) -> Response:
        return self.c.put(
            f"/projects/{Projects._enc(project_id)}/merge_requests/{mr_iid}",
            json_body=fields, expect=200)

    def changes(self, project_id: int | str, mr_iid: int) -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/merge_requests/{mr_iid}/changes",
            expect=(200, 404))

    def merge(self, project_id: int | str, mr_iid: int, **extra) -> Response:
        """合并结果多义：成功 200；有冲突 405/406；未就绪 409。

        断言时不能只认 200——CI 未通过的实例上合并会被拒绝，
        那是实例的正常行为，不是被测接口的缺陷。
        """
        return self.c.put(
            f"/projects/{Projects._enc(project_id)}/merge_requests/{mr_iid}/merge",
            json_body=extra or None, expect=(200, 405, 406, 409))


class Members:
    """成员与权限。access_level 用 GitLab 的数值角色（30=Developer）。"""

    GUEST, REPORTER, DEVELOPER, MAINTAINER, OWNER = 10, 20, 30, 40, 50

    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, project_id: int | str) -> list[dict]:
        return self.c.paginate(f"/projects/{Projects._enc(project_id)}/members")

    def add(self, project_id: int | str, user_id: int, access_level: int,
            **extra) -> Response:
        payload: dict[str, Any] = {"user_id": user_id, "access_level": access_level}
        payload.update(extra)
        return self.c.post(f"/projects/{Projects._enc(project_id)}/members",
                           json_body=payload, expect=(201, 400, 404, 409))

    def get(self, project_id: int | str, user_id: int) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/members/{user_id}",
                          expect=(200, 404))

    def update(self, project_id: int | str, user_id: int, access_level: int) -> Response:
        return self.c.put(f"/projects/{Projects._enc(project_id)}/members/{user_id}",
                          json_body={"access_level": access_level}, expect=200)

    def remove(self, project_id: int | str, user_id: int) -> Response:
        return self.c.delete(f"/projects/{Projects._enc(project_id)}/members/{user_id}",
                             expect=(200, 204, 404))

    def group_list(self, group_id: int | str) -> list[dict]:
        return self.c.paginate(f"/groups/{group_id}/members")

    def group_add(self, group_id: int | str, user_id: int, access_level: int) -> Response:
        return self.c.post(f"/groups/{group_id}/members",
                           json_body={"user_id": user_id, "access_level": access_level},
                           expect=(201, 400, 404, 409))


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

    def create(self, username: str, email: str, password: str, name: str = "",
               **extra) -> Response:
        """创建用户是管理员端点：普通 Token 会得到 403，匿名是 401。

        这两者的区别正是「认证」与「授权」的分界，也是越权用例最爱盯的地方。
        """
        payload: dict[str, Any] = {"username": username, "email": email,
                                   "password": password, "name": name or username}
        payload.update(extra)
        return self.c.post("/users", json_body=payload, expect=(201, 400, 403))

    def keys(self) -> Response:
        return self.c.get("/user/keys", expect=(200, 401, 403))

    def projects(self, user_id: int) -> list[dict]:
        return self.c.paginate(f"/users/{user_id}/projects")


class Snippets:
    """实例级代码片段。"""

    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, **params) -> list[dict]:
        return self.c.paginate("/snippets", params)

    def create(self, title: str, file_name: str, content: str,
               visibility: str = "private", **extra) -> Response:
        payload: dict[str, Any] = {"title": title, "file_name": file_name,
                                   "content": content, "visibility": visibility}
        payload.update(extra)
        return self.c.post("/snippets", json_body=payload, expect=(201, 400))

    def get(self, snippet_id: int) -> Response:
        return self.c.get(f"/snippets/{snippet_id}", expect=(200, 404))


class Wikis:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, project_id: int | str) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/wikis",
                          expect=(200, 404))


class Namespaces:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, **params) -> list[dict]:
        return self.c.paginate("/namespaces", params)


class Search:
    """全局搜索。scope 决定搜什么：projects / issues / merge_requests 等。"""

    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def run(self, scope: str, term: str, **params) -> Response:
        query = {"scope": scope, "search": term}
        query.update(params)
        return self.c.get("/search", params=query, expect=(200, 400))


class Groups:
    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    def list(self, **params) -> list[dict]:
        return self.c.paginate("/groups", params)

    def create(self, name: str, path: str, **extra) -> Response:
        payload: dict[str, Any] = {"name": name, "path": path}
        payload.update(extra)
        return self.c.post("/groups", json_body=payload, expect=201)

    def get(self, group_id: int | str) -> Response:
        return self.c.get(f"/groups/{group_id}", expect=(200, 404))

    def update(self, group_id: int | str, **fields) -> Response:
        return self.c.put(f"/groups/{group_id}", json_body=fields, expect=200)

    def delete(self, group_id: int | str) -> Response:
        return self.c.delete(f"/groups/{group_id}", expect=(202, 204, 404))

    def projects(self, group_id: int | str) -> list[dict]:
        return self.c.paginate(f"/groups/{group_id}/projects")

    def create_project(self, group_id: int | str, name: str, **extra) -> Response:
        """在群组下建项目。

        GitLab 19 起 `POST /groups/{id}/projects` 直接 404（路由已撤），
        官方入口是 `POST /projects` + `namespace_id`。这里走后者，
        但保留群组语义——调用方拿到的仍是「建在某群组下」的结果。
        """
        payload: dict[str, Any] = {"name": name, "namespace_id": group_id}
        payload.update(extra)
        return self.c.post("/projects", json_body=payload, expect=201)

    def subgroups(self, group_id: int | str) -> list[dict]:
        return self.c.paginate(f"/groups/{group_id}/subgroups")

    def create_subgroup(self, group_id: int | str, name: str, path: str) -> Response:
        """建子群组。

        同 `create_project`：`POST /groups/{id}/subgroups` 在 GitLab 19 上 404，
        改用 `POST /groups` + `parent_id`。读路径（GET subgroups）不受影响，
        所以「写的入口和读的入口不是同一个」这件事必须写在代码里，
        否则下一个人照着读路径猜写入口，会得到 404 且无从下手。
        """
        return self.c.post("/groups", json_body={
            "name": name, "path": path, "parent_id": group_id,
        }, expect=201)


class Repository:
    """仓库：目录树 / 提交 / 分支 / 文件 / 标签。

    分支名和文件路径都可能含 `/`（feature/login 这种），必须整段 URL 编码：
    不编码会被服务端当成多一级路径，表现是 404，而排查时谁都想不到
    「斜杠没转义」——因为错误信息里压根看不出来。
    """

    def __init__(self, client: GitLabClient) -> None:
        self.c = client

    @staticmethod
    def _ref(ref: str) -> str:
        return quote(str(ref), safe="")

    def tree(self, project_id: int | str, **params) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/repository/tree",
                          params=params, expect=(200, 404))

    def commits(self, project_id: int | str, **params) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/repository/commits",
                          params=params, expect=(200, 404))

    def commit(self, project_id: int | str, sha: str) -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/repository/commits/{self._ref(sha)}",
            expect=(200, 404))

    def create_commit(self, project_id: int | str, branch: str, message: str,
                      actions: list[dict]) -> Response:
        """一次提交可以带多个 action（create/update/delete/move）。"""
        return self.c.post(
            f"/projects/{Projects._enc(project_id)}/repository/commits",
            json_body={"branch": branch, "commit_message": message, "actions": actions},
            expect=201)

    def branches(self, project_id: int | str) -> Response:
        return self.c.get(f"/projects/{Projects._enc(project_id)}/repository/branches",
                          expect=(200, 404))

    def branch(self, project_id: int | str, name: str) -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/repository/branches/{self._ref(name)}",
            expect=(200, 404))

    def create_branch(self, project_id: int | str, name: str, ref: str) -> Response:
        return self.c.post(
            f"/projects/{Projects._enc(project_id)}/repository/branches",
            params={"branch": name, "ref": ref}, expect=201)

    def delete_branch(self, project_id: int | str, name: str) -> Response:
        return self.c.delete(
            f"/projects/{Projects._enc(project_id)}/repository/branches/{self._ref(name)}",
            expect=(200, 204, 404))

    def protect_branch(self, project_id: int | str, name: str, **extra) -> Response:
        return self.c.put(
            f"/projects/{Projects._enc(project_id)}/repository/branches/"
            f"{self._ref(name)}/protect",
            json_body=extra or None, expect=200)

    def file(self, project_id: int | str, file_path: str, ref: str = "main") -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/repository/files/{self._ref(file_path)}",
            params={"ref": ref}, expect=(200, 404))

    def create_file(self, project_id: int | str, file_path: str, branch: str,
                    content: str, commit_message: str, **extra) -> Response:
        payload = {"branch": branch, "content": content, "commit_message": commit_message}
        payload.update(extra)
        return self.c.post(
            f"/projects/{Projects._enc(project_id)}/repository/files/{self._ref(file_path)}",
            json_body=payload, expect=201)

    def update_file(self, project_id: int | str, file_path: str, branch: str,
                    content: str, commit_message: str, **extra) -> Response:
        payload = {"branch": branch, "content": content, "commit_message": commit_message}
        payload.update(extra)
        return self.c.put(
            f"/projects/{Projects._enc(project_id)}/repository/files/{self._ref(file_path)}",
            json_body=payload, expect=200)

    def delete_file(self, project_id: int | str, file_path: str, branch: str,
                    commit_message: str) -> Response:
        return self.c.delete(
            f"/projects/{Projects._enc(project_id)}/repository/files/{self._ref(file_path)}",
            json_body={"branch": branch, "commit_message": commit_message},
            expect=(200, 204, 404))

    def tags(self, project_id: int | str) -> list[dict]:
        return self.c.paginate(f"/projects/{Projects._enc(project_id)}/repository/tags")

    def create_tag(self, project_id: int | str, name: str, ref: str,
                   message: str = "") -> Response:
        return self.c.post(
            f"/projects/{Projects._enc(project_id)}/repository/tags",
            json_body={"tag_name": name, "ref": ref, "message": message}, expect=201)

    def compare(self, project_id: int | str, from_ref: str, to_ref: str) -> Response:
        return self.c.get(
            f"/projects/{Projects._enc(project_id)}/repository/compare",
            params={"from": from_ref, "to": to_ref}, expect=(200, 404))


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
        self.labels = Labels(client)
        self.milestones = Milestones(client)
        self.notes = Notes(client)
        self.merge_requests = MergeRequests(client)
        self.members = Members(client)
        self.snippets = Snippets(client)
        self.wikis = Wikis(client)
        self.namespaces = Namespaces(client)
        self.search = Search(client)
