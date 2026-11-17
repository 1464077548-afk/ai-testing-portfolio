# ==========================================================================
# 导出工具：把 GitLab CE 19.0.1 的全部 REST 接口整理成标准接口文档（单文件 HTML）
#
# 数据来源：
#   1. 官方 OpenAPI 规范 doc/api/openapi/openapi_v2.yaml @ v19.0.1-ee
#      （875 路径 / 1125 操作 / 97 模块），下载：
#      https://gitlab.com/api/v4/projects/gitlab-org%2Fgitlab/repository/files/
#        doc%2Fapi%2Fopenapi%2Fopenapi_v2.yaml/raw?ref=v19.0.1-ee
#   2. QA 框架（src/gqa + tests）源码 AST 提取的路径模板 → 标记「QA 已覆盖」
#   3. 通用约定/错误语义章节：来自 QA 项目的实测结论（不靠猜）
#
# 用法：python tools/export_api_doc.py
# ==========================================================================
from __future__ import annotations

import ast
import datetime as _dt
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "openapi_v2_19.0.1.yaml"
OUT_DEFAULT = ROOT / "docs" / "gitlab_ce_api_doc.html"
TESTS = ROOT / "tests"
SRC = ROOT / "src"

API_PREFIX = "/api/v4"

# --------------------------------------------------------------------------
# 模块 → 业务域 / 编号码 / 中文名
# --------------------------------------------------------------------------
DOMAINS = [
    ("overview", "通用约定"),
    ("sys", "系统与元数据"),
    ("auth", "认证与账户"),
    ("group", "群组管理"),
    ("project", "项目管理"),
    ("member", "成员与权限"),
    ("issue", "议题与协作"),
    ("repo", "代码仓库"),
    ("cicd", "CI / CD"),
    ("release", "发布与制品库"),
    ("pkg", "包与容器仓库"),
    ("intg", "集成与钩子"),
    ("ops", "实例管理与运维"),
    ("misc", "其他接口"),
    ("supp", "补充接口（规范未收录）"),
]

# --------------------------------------------------------------------------
# 补充接口：官方 openapi_v2.yaml 是自动生成的**子集**（实测漏收了
# labels / milestones / notes / users 等核心接口）。以下条目全部在本实例
# 上经 QA 套件实测可用，方法+路径直接来自框架与测试的调用代码。
# --------------------------------------------------------------------------
SUPPLEMENTS = [
    # --- 当前用户 / 用户管理 ---
    ("GET", "/user", "当前用户", "获取当前令牌对应的用户。最直接的鉴权探针：Token 无效返回 401。",
     [], [], [("200", "用户信息", None), ("401", "未认证", None)]),
    ("GET", "/user/keys", "当前用户的 SSH 密钥", "列出当前用户的 SSH 公钥。",
     [], [], [("200", "密钥列表", None)]),
    ("GET", "/users", "用户列表", "分页列出用户。需要管理员 Token 才能看到完整字段（普通用户看不到他人邮箱——字段级越权点）。",
     [], [], [("200", "用户列表", None), ("403", "非管理员受限", None)]),
    ("POST", "/users", "创建用户", "管理员端点：普通 Token 得 403，匿名得 401——这是认证与授权的分界线。",
     [], [{"name": "username", "type": "string", "required": True, "desc": "登录名，实例内唯一"},
          {"name": "email", "type": "string", "required": True, "desc": "邮箱"},
          {"name": "password", "type": "string", "required": False, "desc": "初始密码（或改用 reset 邮件流）"},
          {"name": "name", "type": "string", "required": False, "desc": "显示名，缺省同 username"}],
     [("201", "创建成功", None), ("400", "参数校验失败", None), ("403", "非管理员", None)]),
    ("GET", "/users/{id}", "用户详情", "按数字 ID 取用户。",
     [], [], [("200", "用户信息", None), ("404", "不存在", None)]),
    ("PUT", "/users/{id}", "更新用户", "管理员端点。",
     [], [{"name": "...", "type": "string", "required": False, "desc": "按需传入可更新字段"}],
     [("200", "更新成功", None), ("400", "校验失败", None), ("403", "非管理员", None)]),
    ("DELETE", "/users/{id}", "删除用户", "管理员端点。",
     [], [], [("204", "删除成功", None), ("403", "非管理员", None), ("404", "不存在", None)]),
    # --- 实例设置 ---
    ("GET", "/application/settings", "实例设置", "读取实例级设置，管理员 Token。",
     [], [], [("200", "设置键值", None), ("403", "非管理员", None)]),
    ("PUT", "/application/settings", "修改实例设置", "管理员端点，按需传入要修改的设置键。",
     [], [{"name": "...", "type": "string", "required": False, "desc": "按需传入要修改的设置键（如 deletion_adjourned_period）"}],
     [("200", "设置键值", None), ("400", "校验失败", None), ("403", "非管理员", None)]),
    # --- 标签 ---
    ("GET", "/groups/{id}/labels", "群组标签列表", "列出群组级标签。",
     [], [], [("200", "标签列表", None), ("404", "群组不存在", None)]),
    ("GET", "/projects/{id}/labels", "项目标签列表", "分页列出项目标签。",
     [], [], [("200", "标签列表", None), ("404", "项目不存在", None)]),
    ("POST", "/projects/{id}/labels", "创建项目标签", "幂等资源：同名重复创建 409。color 须为 #RRGGBB 格式。",
     [], [{"name": "name", "type": "string", "required": True, "desc": "标签名，项目内唯一"},
          {"name": "color", "type": "string", "required": True, "desc": "#RRGGBB；小写合法；非十六进制字符 400"}],
     [("201", "创建成功", None), ("400", "校验失败", None), ("409", "重名冲突", None)]),
    ("GET", "/projects/{id}/labels/{label_id}", "标签详情", "按标签 ID 取详情（删除后确认不存在也用它）。",
     [], [], [("200", "标签信息", None), ("404", "不存在", None)]),
    ("PUT", "/projects/{id}/labels/{label_id}", "更新标签", "改名时若新名与现有标签重名 → 409。",
     [], [{"name": "new_name / color / description", "type": "string", "required": False, "desc": "按需传入要修改的字段"}],
     [("200", "更新成功", None), ("400", "校验失败", None), ("409", "重名冲突", None)]),
    ("DELETE", "/projects/{id}/labels/{label_id}", "删除标签", "删除后 GET 该标签 404。",
     [], [], [("200", "删除成功", None), ("204", "删除成功", None), ("404", "不存在", None)]),
    # --- 里程碑 ---
    ("GET", "/projects/{id}/milestones", "里程碑列表", "分页列出项目里程碑。里程碑允许同名。",
     [], [], [("200", "里程碑列表", None), ("404", "项目不存在", None)]),
    ("POST", "/projects/{id}/milestones", "创建里程碑", "同名校验存在但串行重复创建返回 400 “already being used”。",
     [], [{"name": "title", "type": "string", "required": True, "desc": "标题"},
          {"name": "due_date / start_date", "type": "string (YYYY-MM-DD)", "required": False,
           "desc": "注意：非法日期（如 2026-02-30）实测会被静默丢弃，回查为 null——必须回查确认"}],
     [("201", "创建成功", None), ("400", "校验失败", None)]),
    ("GET", "/projects/{id}/milestones/{milestone_id}", "里程碑详情", "按 ID 取里程碑。",
     [], [], [("200", "里程碑信息", None), ("404", "不存在", None)]),
    ("PUT", "/projects/{id}/milestones/{milestone_id}", "更新里程碑", "state_event 支持 activate/close；非法事件名不改状态。",
     [], [{"name": "title / description / due_date / state_event", "type": "string", "required": False,
           "desc": "按需传入要修改的字段"}],
     [("200", "更新成功", None), ("400", "校验失败", None)]),
    ("DELETE", "/projects/{id}/milestones/{milestone_id}", "删除里程碑", "",
     [], [], [("200", "删除成功", None), ("204", "删除成功", None), ("404", "不存在", None)]),
    ("GET", "/projects/{id}/milestones/{milestone_id}/issues", "里程碑下的议题", "列出归属该里程碑的议题。",
     [], [], [("200", "议题列表", None), ("404", "里程碑不存在", None)]),
    # --- 议题评论 ---
    ("GET", "/projects/{id}/issues/{issue_iid}/notes", "评论列表", "列出议题评论（老式 note 接口，CE 上最常用）。",
     [], [], [("200", "评论列表", None), ("404", "议题不存在", None)]),
    ("POST", "/projects/{id}/issues/{issue_iid}/notes", "创建评论", "",
     [], [{"name": "body", "type": "string", "required": True, "desc": "评论内容（Markdown）"}],
     [("201", "创建成功", None), ("400", "校验失败", None), ("404", "议题不存在", None)]),
    ("GET", "/projects/{id}/issues/{issue_iid}/notes/{note_id}", "评论详情", "按 ID 取单条评论。",
     [], [], [("200", "评论信息", None), ("404", "不存在", None)]),
    ("PUT", "/projects/{id}/issues/{issue_iid}/notes/{note_id}", "修改评论", "",
     [], [{"name": "body", "type": "string", "required": True, "desc": "新内容"}],
     [("200", "修改成功", None), ("400", "校验失败", None)]),
    ("DELETE", "/projects/{id}/issues/{issue_iid}/notes/{note_id}", "删除评论", "部分版本返回 204，部分返回 200 + 空体，都要接受。",
     [], [], [("200", "删除成功", None), ("202", "已受理", None), ("204", "删除成功", None), ("404", "不存在", None)]),
]

TAG_DOMAIN = {
    # sys
    "metadata": "sys", "instance": "sys", "usage_data": "sys", "metrics": "sys",
    "chaos": "sys", "plan_limits": "sys", "database_dictionary": "ops",
    # auth
    "users": "auth", "access_tokens": "auth", "keys": "auth",
    "applications": "auth", "avatars": "auth", "custom_attributes": "auth",
    "events": "auth", "ldap": "auth", "organizations": "auth",
    # group
    "groups": "group", "group_import_and_export": "group", "imports": "group",
    "offline_transfers": "group", "subgroups": "group",
    # project
    "projects": "project", "project_import": "project",
    "project_topics": "project", "project_templates": "project",
    "project_snapshots": "project", "web_commits": "project",
    "badges": "project", "wikis": "project", "snippets": "project",
    "namespaces": "project",
    # member
    "members": "member", "access_requests": "member", "invitations": "member",
    # issue
    "issues": "issue", "merge_requests": "issue",
    "merge_request_approvals": "issue", "draft_notes": "issue",
    "award_emoji": "issue", "resource_events": "issue",
    "suggestions": "issue", "glql": "issue", "boards": "issue",
    # repo
    "repositories": "repo", "branches": "repo", "protected_branches": "repo",
    "tags": "repo", "protected_tags": "repo", "commits": "repo",
    "commit_statuses": "repo", "files": "repo", "submodules": "repo",
    "remote_mirrors": "repo",
    # cicd
    "pipelines": "cicd", "pipeline_schedules": "cicd", "ci_jobs": "cicd",
    "jobs": "cicd", "job_artifacts": "cicd", "ci_variables": "cicd",
    "ci_triggers": "cicd", "ci_runners": "cicd", "runners": "cicd",
    "ci_resource_groups": "cicd", "ci_lint": "cicd", "ci_catalog": "cicd",
    "environments": "cicd", "deploy_resources": "cicd",
    "freeze_periods": "cicd", "secure_files": "cicd",
    "clusters": "cicd", "cluster_agents": "cicd",
    # release
    "releases": "release", "attestations": "release", "feature_flags": "release",
    "unleash": "release", "ml_model_registry": "release",
    # pkg
    "packages": "pkg", "pypi_packages": "pkg", "container_registry": "pkg",
    "dependency_proxy": "pkg",
    # intg
    "integrations": "intg", "hooks": "intg", "error_tracking": "intg",
    "alert_management": "intg", "metric_images": "intg",
    "jira_connect_subscriptions": "intg", "markdown": "intg",
    "search": "intg", "terraform": "intg",
    # ops
    "geo": "ops", "audit_events": "ops", "broadcast_messages": "ops",
    "batched_background_migrations": "ops", "migrations": "ops",
    "features": "ops", "internal_operations": "ops",
    "gitlab_pages": "ops", "project_snapshots2": "ops",
}

TAG_CODE = {
    "projects": "PRJ", "groups": "GRP", "users": "USR", "members": "MBR",
    "issues": "ISS", "merge_requests": "MR", "labels": "LABEL",
    "milestones": "MS", "notes": "NOTE", "repositories": "REPO",
    "branches": "BR", "commits": "COMMIT", "files": "FILE", "tags": "TAG",
    "pipelines": "PIPE", "runners": "RUN", "packages": "PKG",
    "integrations": "INTG", "hooks": "HOOK", "snippets": "SNIP",
    "wikis": "WIKI", "badges": "BADGE", "environments": "ENV",
    "releases": "REL", "search": "SEARCH", "metadata": "META",
    "access_tokens": "TOKEN", "keys": "KEY", "applications": "APP",
    "namespaces": "NS", "events": "EVT", "feature_flags": "FFLAG",
    "ci_variables": "CIVAR", "ci_jobs": "CIJOB", "jobs": "JOB",
    "clusters": "CLUSTER", "container_registry": "CREG",
    "deploy_resources": "DEPLOY", "import": "IMPORT", "terraform": "TF",
}

TAG_CN = {
    "projects": "项目", "groups": "群组", "users": "用户", "members": "成员",
    "issues": "议题", "merge_requests": "合并请求", "repositories": "仓库",
    "branches": "分支", "tags": "仓库标签", "protected_branches": "受保护分支",
    "protected_tags": "受保护标签", "commits": "提交", "files": "仓库文件",
    "submodules": "子模块", "remote_mirrors": "镜像仓库",
    "commit_statuses": "提交状态", "pipelines": "流水线",
    "pipeline_schedules": "流水线计划", "ci_jobs": "CI 作业", "jobs": "作业",
    "job_artifacts": "作业产物", "ci_variables": "CI 变量",
    "ci_triggers": "CI 触发器", "ci_runners": "CI Runner",
    "runners": "Runner", "ci_resource_groups": "资源组", "ci_lint": "CI 配置校验",
    "ci_catalog": "CI 目录", "environments": "环境", "deploy_resources": "部署",
    "freeze_periods": "冻结期", "secure_files": "安全文件",
    "clusters": "集群", "cluster_agents": "集群 Agent",
    "packages": "软件包", "pypi_packages": "PyPI 包",
    "container_registry": "容器镜像仓库", "dependency_proxy": "依赖代理",
    "integrations": "集成", "hooks": "系统钩子", "snippets": "代码片段",
    "wikis": "Wiki", "badges": "徽章", "releases": "发布", "keys": "SSH 密钥",
    "access_tokens": "访问令牌", "applications": "OAuth 应用",
    "avatars": "头像", "custom_attributes": "自定义属性", "events": "事件",
    "namespaces": "命名空间", "search": "全局搜索", "markdown": "Markdown 渲染",
    "award_emoji": "表情回应", "draft_notes": "草稿评论",
    "merge_request_approvals": "合并审批", "resource_events": "资源事件",
    "suggestions": "修改建议", "metadata": "实例元数据",
    "broadcast_messages": "广播消息", "features": "功能开关",
    "geo": "Geo 站点", "audit_events": "审计事件", "ldap": "LDAP",
    "terraform": "Terraform 状态", "invitations": "邀请", "access_requests": "访问申请",
    "project_topics": "项目主题", "project_import": "项目导入",
    "group_import_and_export": "群组导入导出", "imports": "第三方导入",
    "project_templates": "项目模板", "web_commits": "Web 提交",
    "error_tracking": "错误跟踪", "alert_management": "告警管理",
    "metric_images": "指标图", "feature_flags": "功能标志",
    "unleash": "特性开关客户端", "glql": "GitLab 查询语言",
    "instance": "实例", "usage_data": "使用统计", "metrics": "实例指标",
    "chaos": "混沌接口", "plan_limits": "计划配额",
    "database_dictionary": "数据库字典", "attestations": "发布证明",
    "ml_model_registry": "ML 模型注册", "offline_transfers": "离线迁移",
    "jira_connect_subscriptions": "Jira 连接订阅", "gitlab_pages": "GitLab Pages",
    "internal_operations": "内部操作", "subgroups": "子群组",
    "organizations": "组织", "project_snapshots": "项目快照",
    "batched_background_migrations": "后台批量迁移", "migrations": "数据库迁移",
    "ci_catalog": "CI 目录", "terraform": "Terraform",
    "boards": "看板",
}

BRACE_RE = re.compile(r"\{[^{}]*\}")


def _norm_path(p: str) -> str:
    return BRACE_RE.sub("{}", p.rstrip("/"))


# --------------------------------------------------------------------------
# QA 覆盖：AST 提取框架与测试里出现过的路径模板
# --------------------------------------------------------------------------
def collect_framework_templates() -> set[str]:
    templates: set[str] = set()
    files = list(SRC.rglob("*.py")) + list(TESTS.rglob("*.py"))

    def add(s: str):
        s = s.strip()
        if s.startswith("/") and len(s) > 1 and not s.startswith("//"):
            templates.add(_norm_path(s))

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                add(node.value)
            elif isinstance(node, ast.JoinedStr):
                parts = []
                ok = True
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(v.value)
                    elif isinstance(v, ast.FormattedValue):
                        parts.append("{}")
                    else:
                        ok = False
                        break
                if ok:
                    add("".join(parts))
    return templates


# --------------------------------------------------------------------------
# OpenAPI 解析
# --------------------------------------------------------------------------
def resolve_ref(name: str, defs: dict) -> dict:
    ref = defs.get(name)
    if not isinstance(ref, dict):
        return {}
    return ref


def ref_name(schema: dict | None) -> str | None:
    if not isinstance(schema, dict):
        return None
    r = schema.get("$ref")
    if isinstance(r, str) and r.startswith("#/definitions/"):
        return r.split("/")[-1]
    return None


def flatten_body(params_body: dict, defs: dict) -> list[dict]:
    """把 in:body 参数的 schema 拍平成字段列表（required 在前）。"""
    schema = params_body.get("schema") or {}
    name = ref_name(schema)
    props: dict = {}
    required: set[str] = set()

    def absorb(defn: dict):
        req = defn.get("required") or []
        if isinstance(req, list):
            required.update(req)
        props.update(defn.get("properties") or {})

    if name:
        d = resolve_ref(name, defs)
        if "allOf" in d:
            for sub in d["allOf"]:
                sn = ref_name(sub)
                if sn:
                    absorb(resolve_ref(sn, defs))
                elif isinstance(sub, dict):
                    absorb(sub)
        else:
            absorb(d)
    elif schema.get("properties"):
        absorb(schema)

    def ftype(p: dict) -> str:
        t = p.get("type", "")
        if p.get("enum"):
            t += " (" + " | ".join(str(e) for e in p["enum"][:6]) + ")"
        return t or "object"

    rows = []
    for pname, p in props.items():
        rows.append({
            "name": pname,
            "type": ftype(p),
            "required": pname in required,
            "desc": str(p.get("description") or "")[:180],
        })
    rows.sort(key=lambda r: not r["required"])
    return rows


def build_payload() -> dict:
    with open(SPEC_PATH, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    defs = spec.get("definitions", {})
    paths = spec.get("paths", {})

    modules: dict[str, dict] = {}
    n_ops = 0
    for path, item in paths.items():
        rel = path[len(API_PREFIX):] if path.startswith(API_PREFIX) else path
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete", "head"):
                continue
            n_ops += 1
            tag = (op.get("tags") or ["misc"])[0]
            mod = modules.setdefault(tag, {"tag": tag, "ops": []})
            mod["ops"].append({
                "m": method.upper(),
                "path": rel,
                "np": _norm_path(rel),
                "sum": str(op.get("summary") or "").strip(),
                "desc": str(op.get("description") or "").strip(),
                "params": [
                    {"name": p.get("name"), "in": p.get("in"),
                     "type": p.get("type", ""),
                     "req": bool(p.get("required")),
                     "desc": str(p.get("description") or "")[:150]}
                    for p in op.get("parameters", [])
                    if p.get("in") in ("path", "query") and p.get("name")
                ],
                "body": flatten_body(
                    next((p for p in op.get("parameters", [])
                          if p.get("in") == "body"), {}), defs),
                "resps": [
                    {"code": code,
                     "desc": str(r.get("description") or "")[:200],
                     "entity": ref_name(r.get("schema"))}
                    for code, r in (op.get("responses") or {}).items()
                ],
            })

    qa_templates = collect_framework_templates()

    # 补充接口（规范未收录）：全部来自 QA 套件实测调用，编号并入「supp」域
    supp_mods = {}
    for method, path, title, desc, _p, body, resps in SUPPLEMENTS:
        np = _norm_path(path)
        if np not in qa_templates:
            print(f"[api-doc] 警告：补充接口 {method} {path} 未在框架调用中找到，请核对")
        supp_mods.setdefault("SUPP", {"tag": "SUPP", "ops": []})["ops"].append({
            "m": method, "path": path, "np": np,
            "sum": title, "desc": desc,
            "params": [], "body": body,
            "resps": [{"code": c, "desc": d, "entity": e} for c, d, e in resps],
            "qa": True,
        })
    if supp_mods:
        modules.update(supp_mods)
        TAG_DOMAIN.setdefault("SUPP", "supp")
        # 补充模块不参与 TAG_CODE，编号在下方单独处理

    tag_stats = {t: len(m["ops"]) for t, m in modules.items()}
    domains = []
    total_qa = 0
    for key, name in DOMAINS:
        if key == "overview":
            continue
        mods = []
        used_codes: set[str] = set()
        for t, m in modules.items():
            if TAG_DOMAIN.get(t, "misc") != key:
                continue
            qa_hits = sum(1 for o in m["ops"] if o["np"] in qa_templates)
            for o in m["ops"]:
                o["qa"] = o["np"] in qa_templates
            total_qa += qa_hits
            if t == "SUPP":
                code = "SUPP"
            else:
                code = TAG_CODE.get(t) or re.sub(r"[^a-z0-9]", "", t).upper()[:6]
            base_code, i = code, 1
            while code in used_codes:          # 模块码撞车时加序号（PROTEC → PROTEC2）
                i += 1
                code = f"{base_code}{i}"
            used_codes.add(code)
            mods.append({
                "tag": t, "code": code,
                "name": ("补充接口 · 官方规范未收录，本实例实测可用"
                         if t == "SUPP" else f"{TAG_CN.get(t, t)} · {t}"),
                "n": len(m["ops"]), "qa": qa_hits,
                "ops": m["ops"],
            })
        mods.sort(key=lambda x: -x["n"])
        domains.append({"key": key, "name": name, "modules": mods,
                        "n": sum(x["n"] for x in mods),
                        "qa": sum(x["qa"] for x in mods)})

    # 接口编号：域码-模块码-序号
    for d in domains:
        for m in d["modules"]:
            for i, o in enumerate(m["ops"], 1):
                o["id"] = f"{d['key'].upper()}-{m['code']}-{i:03d}"

    method_dist = {}
    for d in domains:
        for m in d["modules"]:
            for o in m["ops"]:
                method_dist[o["m"]] = method_dist.get(o["m"], 0) + 1

    total_ops = sum(method_dist.values())

    return {
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": "19.0.1",
        "base_url": "http://localhost:8929",
        "api_prefix": API_PREFIX,
        "stats": {
            "paths": len(paths), "ops": total_ops,
            "spec_ops": n_ops,
            "supp_ops": total_ops - n_ops,
            "modules": len(modules),
            "domains": len([d for d in domains if d["modules"]]),
            "qa_covered": total_qa,
            "methods": method_dist,
        },
        "domains": domains,
    }


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DEFAULT
    if not SPEC_PATH.exists():
        print(f"[api-doc] 缺少 {SPEC_PATH}，请先下载官方规范（见本文件头部注释）")
        sys.exit(1)
    payload = build_payload()
    st = payload["stats"]
    print(f"[api-doc] {st['ops']} 个操作 / {st['modules']} 个模块 / "
          f"QA 覆盖 {st['qa_covered']} 条")
    out.parent.mkdir(parents=True, exist_ok=True)
    tpl = (ROOT / "tools" / "_api_doc_template.html").read_text(encoding="utf-8")
    html = tpl.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    out.write_text(html, encoding="utf-8")
    print(f"[api-doc] 已生成 {out}（{out.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
