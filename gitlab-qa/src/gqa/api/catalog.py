"""GitLab CE 接口目录——覆盖率的分母。

为什么要有这张表：

「接口覆盖率」这句话如果没有一份**双方都认可的清单**，就只能靠感觉说。
感觉是最不可靠的度量：新增了 10 条用例，覆盖率到底是涨了还是没涨，
没人说得清。所以这里显式声明：本框架认为 GitLab CE 应该有这些端点，
运行时再统计其中有多少被真正调用过——覆盖率 = 命中数 / 目录总数。

目录同时承担三个额外职责：
1. 评审入口。新增接口先在这里登记，评审时看的是「该测的测了没有」，
   而不是翻几十个测试文件去找谁调用了什么。
2. 漂移检测。目录外调用（unknown）会被单独列出来——框架调了没登记的
   接口，要么是目录该补，要么是有人在用例里塞了野路径。
3. 分层约束。admin=True 的端点必须走管理员身份，契约名指向 contracts.py
   里的 schema，二者对不上就是设计不一致。

模板占位符统一用 {id} / {path} / {sha} / {branch} / {file} / {tag}，
由 coverage.py 在匹配时统一按「一个路径段」处理。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    """目录里的一条端点。"""

    method: str
    template: str
    group: str          # 报告分组用：项目 / 仓库 / 议题 ...
    name: str           # 人类可读的用途
    admin: bool = False  # 是否需要管理员身份
    contract: str = ""   # 对应 contracts.py 中的 schema 名（可空）
    # 非空表示「该路由在指定版本起已不可达」，例如 removed_in="19"。
    # 这类端点不计入覆盖率分母——留着会把覆盖率永久压在一个到不了的天花板下，
    # 而它传达的信息（曾经存在、现在没了、替代入口是什么）由 note 承载。
    removed_in: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.method} {self.template}"

    @property
    def active(self) -> bool:
        """是否计入覆盖率分母。"""
        return not self.removed_in


CATALOG: tuple[Endpoint, ...] = (
    # ---------------------------------------------------------------- 系统
    Endpoint("GET", "/version", "系统", "实例版本", contract="VERSION"),
    Endpoint("GET", "/metadata", "系统", "实例元信息", contract="VERSION"),
    Endpoint("GET", "/application/settings", "系统", "读取实例设置", admin=True),
    Endpoint("PUT", "/application/settings", "系统", "修改实例设置", admin=True),
    Endpoint("GET", "/application/statistics", "系统", "实例统计", admin=True),

    # ---------------------------------------------------------------- 项目
    Endpoint("GET", "/projects", "项目", "列出项目", contract="PROJECT"),
    Endpoint("POST", "/projects", "项目", "创建项目", contract="PROJECT"),
    Endpoint("GET", "/projects/{id}", "项目", "项目详情", contract="PROJECT"),
    Endpoint("PUT", "/projects/{id}", "项目", "修改项目", contract="PROJECT"),
    Endpoint("DELETE", "/projects/{id}", "项目", "删除项目"),
    Endpoint("GET", "/projects/{path}", "项目", "按完整路径取项目", contract="PROJECT"),
    Endpoint("GET", "/projects/{id}/users", "项目", "项目成员候选用户", contract="USER"),
    Endpoint("GET", "/projects/{id}/languages", "项目", "项目语言占比"),

    # ---------------------------------------------------------------- 仓库
    Endpoint("GET", "/projects/{id}/repository/tree", "仓库", "目录树", contract="TREE_ITEM"),
    Endpoint("GET", "/projects/{id}/repository/commits", "仓库", "提交列表", contract="COMMIT"),
    Endpoint("POST", "/projects/{id}/repository/commits", "仓库", "提交变更", contract="COMMIT"),
    Endpoint("GET", "/projects/{id}/repository/commits/{sha}", "仓库", "提交详情", contract="COMMIT"),
    Endpoint("GET", "/projects/{id}/repository/branches", "仓库", "分支列表", contract="BRANCH"),
    Endpoint("GET", "/projects/{id}/repository/branches/{branch}", "仓库", "分支详情", contract="BRANCH"),
    Endpoint("POST", "/projects/{id}/repository/branches", "仓库", "创建分支", contract="BRANCH"),
    Endpoint("DELETE", "/projects/{id}/repository/branches/{branch}", "仓库", "删除分支"),
    Endpoint("PUT", "/projects/{id}/repository/branches/{branch}/protect", "仓库", "保护分支"),
    Endpoint("GET", "/projects/{id}/repository/files/{file}", "仓库", "读取文件", contract="FILE"),
    Endpoint("POST", "/projects/{id}/repository/files/{file}", "仓库", "新建文件"),
    Endpoint("PUT", "/projects/{id}/repository/files/{file}", "仓库", "更新文件"),
    Endpoint("DELETE", "/projects/{id}/repository/files/{file}", "仓库", "删除文件"),
    Endpoint("GET", "/projects/{id}/repository/tags", "仓库", "标签列表", contract="TAG"),
    Endpoint("POST", "/projects/{id}/repository/tags", "仓库", "创建标签", contract="TAG"),
    Endpoint("GET", "/projects/{id}/repository/compare", "仓库", "比较两个 ref"),

    # ---------------------------------------------------------------- 议题
    Endpoint("GET", "/projects/{id}/issues", "议题", "议题列表", contract="ISSUE"),
    Endpoint("POST", "/projects/{id}/issues", "议题", "创建议题", contract="ISSUE"),
    Endpoint("GET", "/projects/{id}/issues/{id}", "议题", "议题详情", contract="ISSUE"),
    Endpoint("PUT", "/projects/{id}/issues/{id}", "议题", "修改议题", contract="ISSUE"),
    Endpoint("DELETE", "/projects/{id}/issues/{id}", "议题", "删除议题"),
    Endpoint("POST", "/projects/{id}/issues/{id}/notes", "议题", "新增评论", contract="NOTE"),
    Endpoint("GET", "/projects/{id}/issues/{id}/notes", "议题", "评论列表", contract="NOTE"),
    Endpoint("GET", "/projects/{id}/issues/{id}/notes/{id}", "议题", "评论详情", contract="NOTE"),
    Endpoint("PUT", "/projects/{id}/issues/{id}/notes/{id}", "议题", "修改评论", contract="NOTE"),
    Endpoint("DELETE", "/projects/{id}/issues/{id}/notes/{id}", "议题", "删除评论"),

    # ---------------------------------------------------------------- 标签
    Endpoint("GET", "/projects/{id}/labels", "标签", "标签列表", contract="LABEL"),
    Endpoint("POST", "/projects/{id}/labels", "标签", "创建标签", contract="LABEL"),
    Endpoint("GET", "/projects/{id}/labels/{id}", "标签", "标签详情", contract="LABEL"),
    Endpoint("PUT", "/projects/{id}/labels/{id}", "标签", "更新标签", contract="LABEL"),
    Endpoint("DELETE", "/projects/{id}/labels/{id}", "标签", "删除标签"),
    Endpoint("GET", "/groups/{id}/labels", "标签", "群组标签列表", contract="LABEL"),

    # ---------------------------------------------------------------- 里程碑
    Endpoint("GET", "/projects/{id}/milestones", "里程碑", "里程碑列表", contract="MILESTONE"),
    Endpoint("POST", "/projects/{id}/milestones", "里程碑", "创建里程碑", contract="MILESTONE"),
    Endpoint("GET", "/projects/{id}/milestones/{id}", "里程碑", "里程碑详情", contract="MILESTONE"),
    Endpoint("PUT", "/projects/{id}/milestones/{id}", "里程碑", "更新里程碑", contract="MILESTONE"),
    Endpoint("DELETE", "/projects/{id}/milestones/{id}", "里程碑", "删除里程碑"),
    Endpoint("GET", "/projects/{id}/milestones/{id}/issues", "里程碑", "里程碑下议题", contract="ISSUE"),

    # ---------------------------------------------------------------- 合并请求
    Endpoint("GET", "/projects/{id}/merge_requests", "合并请求", "MR 列表", contract="MERGE_REQUEST"),
    Endpoint("POST", "/projects/{id}/merge_requests", "合并请求", "创建 MR", contract="MERGE_REQUEST"),
    Endpoint("GET", "/projects/{id}/merge_requests/{id}", "合并请求", "MR 详情", contract="MERGE_REQUEST"),
    Endpoint("PUT", "/projects/{id}/merge_requests/{id}", "合并请求", "修改 MR", contract="MERGE_REQUEST"),
    Endpoint("GET", "/projects/{id}/merge_requests/{id}/changes", "合并请求", "MR 变更", contract="MERGE_REQUEST"),
    Endpoint("PUT", "/projects/{id}/merge_requests/{id}/merge", "合并请求", "合并 MR"),

    # ---------------------------------------------------------------- 成员
    Endpoint("GET", "/projects/{id}/members", "成员", "项目成员列表", contract="MEMBER"),
    Endpoint("POST", "/projects/{id}/members", "成员", "添加成员", contract="MEMBER"),
    Endpoint("GET", "/projects/{id}/members/{id}", "成员", "成员详情", contract="MEMBER"),
    Endpoint("PUT", "/projects/{id}/members/{id}", "成员", "修改成员角色", contract="MEMBER"),
    Endpoint("DELETE", "/projects/{id}/members/{id}", "成员", "移除成员"),
    Endpoint("GET", "/groups/{id}/members", "成员", "群组成员列表", contract="MEMBER"),
    Endpoint("POST", "/groups/{id}/members", "成员", "添加群组成员", contract="MEMBER"),

    # ---------------------------------------------------------------- 群组
    Endpoint("GET", "/groups", "群组", "群组列表", contract="GROUP"),
    Endpoint("POST", "/groups", "群组", "创建群组", contract="GROUP"),
    Endpoint("GET", "/groups/{id}", "群组", "群组详情", contract="GROUP"),
    Endpoint("PUT", "/groups/{id}", "群组", "修改群组", contract="GROUP"),
    Endpoint("DELETE", "/groups/{id}", "群组", "删除群组"),
    Endpoint("GET", "/groups/{id}/projects", "群组", "群组下项目", contract="PROJECT"),
    # GitLab 19 起这条路由撤了（实测 404），写入口改为 POST /projects + namespace_id。
    # 注意读路径没变：GET 仍是 /groups/{id}/projects，读写入口不一致是这次踩到的坑
    Endpoint("POST", "/groups/{id}/projects", "群组", "在群组下建项目", contract="PROJECT",
             removed_in="19", note="改用 POST /projects + namespace_id"),
    Endpoint("GET", "/groups/{id}/subgroups", "群组", "子群组列表", contract="GROUP"),
    Endpoint("POST", "/groups/{id}/subgroups", "群组", "创建子群组", contract="GROUP",
             removed_in="19", note="改用 POST /groups + parent_id"),

    # ---------------------------------------------------------------- 用户
    Endpoint("GET", "/user", "用户", "当前用户", contract="USER"),
    Endpoint("GET", "/users", "用户", "用户列表", contract="USER"),
    Endpoint("GET", "/users/{id}", "用户", "用户详情", contract="USER"),
    Endpoint("POST", "/users", "用户", "创建用户", admin=True, contract="USER"),
    Endpoint("DELETE", "/users/{id}", "用户", "删除用户", admin=True),
    Endpoint("GET", "/admin/users", "用户", "管理员视角用户列表", admin=True, contract="USER",
             removed_in="19", note="/api/v4/admin/* 整段在本实例 404（19.0.1 实测）"),
    Endpoint("GET", "/user/keys", "用户", "当前用户 SSH 密钥"),
    Endpoint("GET", "/users/{id}/projects", "用户", "用户可见项目", contract="PROJECT"),

    # ---------------------------------------------------------------- 实例级
    Endpoint("GET", "/namespaces", "实例", "命名空间列表", contract="NAMESPACE"),
    Endpoint("GET", "/search", "实例", "全局搜索"),
    Endpoint("GET", "/snippets", "实例", "片段列表", contract="SNIPPET"),
    Endpoint("POST", "/snippets", "实例", "创建片段", contract="SNIPPET"),
    Endpoint("GET", "/snippets/{id}", "实例", "片段详情", contract="SNIPPET"),
    Endpoint("DELETE", "/snippets/{id}", "实例", "删除片段"),
    Endpoint("GET", "/projects/{id}/wikis", "Wiki", "Wiki 页面列表", contract="WIKI_PAGE"),
    Endpoint("GET", "/features", "实例", "特性开关列表", admin=True),
)


def by_group() -> dict[str, list[Endpoint]]:
    """按分组归类，供报告输出。"""
    out: dict[str, list[Endpoint]] = {}
    for ep in CATALOG:
        out.setdefault(ep.group, []).append(ep)
    return out
