# ==========================================================================
# 导出工具：把全项目测试用例整理成单文件 HTML 清单
#
# 用法：
#   python tools/export_case_inventory.py
#   python tools/export_case_inventory.py --no-collect   # 跳过 pytest 收集
#
# 数据来源（三层，全部不靠手抄）：
#   1. AST 解析 tests/** 下所有 test_*.py：
#      用例名、docstring、allure 元数据（epic/feature/story/severity）、
#      pytest 标记、参数化规格、xfail 原因、源码行号
#   2. pytest --collect-only 实际收集：拿每个用例的真实可执行条数
#      （参数化展开后的精确计数，AST 估不出来）
#   3. tests/data/api/*.yaml：数据驱动矩阵的逐条用例
# ==========================================================================
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import datetime as _dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
OUT_DEFAULT = ROOT / "docs" / "test_case_inventory.html"

# --------------------------------------------------------------------------
# 套件与模块的命名（人工维护：清单要给人读，编号必须稳定且可读）
# --------------------------------------------------------------------------
SUITE_META = {
    "tests/api": {
        "key": "API", "name": "API 接口测试",
        "desc": "GitLab REST API v4 的功能覆盖：鉴权、资源 CRUD、分页完整性、"
                "边界值、幂等性、状态流转、请求语义与契约校验。",
        "order": 1,
    },
    "tests/security": {
        "key": "SEC", "name": "安全测试",
        "desc": "越权（授权绕过）、数据暴露面、请求头安全、注入防护、"
                "批量赋值、速率限制、会话与数据作用域隔离。",
        "order": 2,
    },
    "tests/e2e": {
        "key": "E2E", "name": "端到端流程测试",
        "desc": "跨资源真实业务流：议题全生命周期、项目从创建到归档的全链路。",
        "order": 3,
    },
    "tests/perf": {
        "key": "PERF", "name": "性能门禁测试",
        "desc": "带阈值的 API 延迟门禁（P95/错误率），不达标即失败——"
                "没有判定标准的性能测试不叫测试。",
        "order": 4,
    },
    "tests/ui": {
        "key": "UI", "name": "UI 测试（Playwright）",
        "desc": "页面级冒烟：登录、导航、项目与议题操作。只验证「界面能不能完成"
                "这件事」，不重复验证 API 已覆盖的业务规则。",
        "order": 5,
    },
    "tests/smoke": {
        "key": "SMOKE", "name": "冒烟测试",
        "desc": "部署就绪探针：健康检查、读写就绪、版本一致性。失败即表示"
                "「实例还没到能跑其余用例的状态」。",
        "order": 6,
    },
    "tests/unit": {
        "key": "UNIT", "name": "框架自检测试",
        "desc": "不依赖 GitLab 实例的自检：YAML 用例数据写坏会响（而不是静默"
                "少跑）、分页扫描器等框架组件的正确性。",
        "order": 7,
    },
    "tests": {  # tests 根下的散文件（tests/test_case_data.py）
        "key": "UNIT", "name": "框架自检测试", "desc": "", "order": 7,
    },
}

MODULE_CODES = {
    # --- api ---
    "test_auth.py": "AUTH", "test_auth_state_machine.py": "AUTH-SM",
    "test_award_emoji.py": "EMOJI",
    "test_badges.py": "BADGE",
    "test_boundaries.py": "BND", "test_contract.py": "CONTRACT",
    "test_ci_variables.py": "CIVAR",
    "test_data_driven.py": "DATA", "test_error_semantics.py": "ERR",
    "test_groups_advanced.py": "GRP", "test_hooks.py": "HOOK",
    "test_idempotency.py": "IDEM",
    "test_issues.py": "ISSUE", "test_labels.py": "LABEL",
    "test_members.py": "MEMBER", "test_merge_requests.py": "MR",
    "test_milestones.py": "MS", "test_namespaces_search.py": "SEARCH",
    "test_notes.py": "NOTE", "test_pagination.py": "PAGE",
    "test_pagination_scoped.py": "PAGE-X", "test_projects.py": "PROJ",
    "test_projects_advanced.py": "PROJ-X", "test_releases.py": "REL",
    "test_repository.py": "REPO",
    "test_request_semantics.py": "REQ", "test_state_transitions.py": "STATE",
    "test_system.py": "SYS", "test_users.py": "USER",
    "test_wikis.py": "WIKI",
    # --- security ---
    "test_authz.py": "AUTHZ", "test_data_scope.py": "SCOPE",
    "test_exposure.py": "EXPOSE", "test_headers.py": "HEADER",
    "test_injection.py": "INJECT", "test_leak.py": "LEAK",
    "test_mass_assignment.py": "MASS", "test_rate_limit.py": "RATE",
    "test_session.py": "SESSION",
    # --- e2e ---
    "test_issue_lifecycle.py": "ISSUE-LC", "test_project_lifecycle.py": "PROJ-LC",
    # --- perf ---
    "test_api_perf.py": "API-P95", "test_perf_deep.py": "DEEP",
    # --- ui ---
    "test_issues_ui.py": "ISSUE-UI", "test_login.py": "LOGIN",
    "test_navigation.py": "NAV", "test_projects_ui.py": "PROJ-UI",
    # --- smoke ---
    "test_health.py": "HEALTH", "test_readiness.py": "READY",
    # --- unit ---
    "test_framework.py": "FW", "test_case_data.py": "DATA-CHK",
}

MATRIX_TITLES = {
    "validation": "输入校验矩阵",
    "naming": "命名规则矩阵",
    "query_params": "查询参数矩阵",
}

SEVERITY_RANK = {"blocker": 0, "critical": 1, "normal": 2,
                 "minor": 3, "trivial": 4}


# --------------------------------------------------------------------------
# AST 解析
# --------------------------------------------------------------------------
def _attr_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _decode_decorator(dec: ast.expr, consts: dict):
    """把一个装饰器解码成 (kind, payload)。"""
    if isinstance(dec, ast.Attribute):
        chain = _attr_chain(dec)
        if chain and chain.startswith("pytest.mark."):
            return ("mark", chain.split(".", 2)[2])
        return None
    if not isinstance(dec, ast.Call):
        return None
    chain = _attr_chain(dec.func)
    if not chain:
        return None

    if chain.startswith("allure."):
        kind = chain.split(".")[1]
        if kind in ("epic", "feature", "story"):
            arg = dec.args[0] if dec.args else None
            val = _const_str(arg)
            return (kind, val) if val else None
        if kind == "severity":
            a = dec.args[0] if dec.args else None
            sev_chain = _attr_chain(a) or ""
            return ("severity", sev_chain.split(".")[-1].lower())
        return None

    if chain.startswith("pytest.mark."):
        name = chain.split(".", 2)[2]
        kw = {k.arg: k.value for k in dec.keywords}
        if name == "parametrize":
            return ("parametrize", _decode_parametrize(dec, consts))
        if name == "xfail":
            strict = kw.get("strict")
            return ("xfail", {
                "reason": _const_str(kw.get("reason")) or "",
                "strict": (strict.value
                           if isinstance(strict, ast.Constant) else None),
                "conditional": "condition" in kw,
            })
        if name in ("skip", "skipif"):
            return ("skip", {"reason": _const_str(kw.get("reason")) or ""})
        return ("mark", name)
    return None


def _list_len(node: ast.AST | None, consts: dict) -> tuple[int | None, list | None]:
    """参数化取值：返回 (条数, ids)。识别字面量列表或模块级常量引用。"""
    if isinstance(node, ast.List):
        items = node.elts
        ids = [x.value for x in items if isinstance(x, ast.Constant)]
        if len(ids) == len(items):            # 全是字面量：当 ids 用
            return len(items), ids
        return len(items), None
    if isinstance(node, ast.Name):
        ref = consts.get(node.id)
        if isinstance(ref, ast.List):
            return len(ref.elts), None
    return None, None


def _decode_parametrize(call: ast.Call, consts: dict) -> dict:
    kw = {k.arg: k.value for k in call.keywords}
    argnames = _const_str(call.args[0]) if call.args else "?"
    count, ids = _list_len(call.args[1], consts) if len(call.args) > 1 else (None, None)
    ids_node = kw.get("ids")
    if isinstance(ids_node, ast.List):        # 显式 ids 列表
        vals = [x.value for x in ids_node.elts if isinstance(x, ast.Constant)]
        if vals:
            ids = vals
    elif ids_node is not None:
        ids = None                           # lambda / 推导式：动态
    has_auto_ids = ids is None
    return {"argnames": argnames, "count": count,
            "ids": ids, "auto_ids": has_auto_ids}


def parse_module(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree) or ""
    mod_marks: list[str] = []
    consts: dict[str, ast.AST] = {}

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value
                    if t.id == "pytestmark":
                        val = node.value
                        items = val.elts if isinstance(val, ast.List) else [val]
                        for it in items:
                            ch = _attr_chain(it.func if isinstance(it, ast.Call) else it)
                            if ch and ch.startswith("pytest.mark."):
                                m = ch.split(".", 2)[2]
                                if m not in mod_marks:
                                    mod_marks.append(m)

    cases = []
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")):
            cases.append(parse_function(node, consts, qualname=None))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name.startswith("test_")):
                    cases.append(parse_function(sub, consts, qualname=node.name))
    return {"doc": doc, "marks": mod_marks, "cases": cases}


def parse_function(node, consts: dict, qualname: str | None = None) -> dict:
    case = {
        "name": node.name, "line": node.lineno, "cls": qualname,
        "epic": None, "feature": None, "story": None, "severity": "normal",
        "marks": [], "params": [], "param_ids": [],
        "xfail": None, "skip": None,
        "doc": ast.get_docstring(node) or "",
        "fixtures": [a.arg for a in node.args.args],
    }
    estimate = 1
    for dec in node.decorator_list:
        decoded = _decode_decorator(dec, consts)
        if not decoded:
            continue
        kind, payload = decoded
        if kind in ("epic", "feature", "story", "severity"):
            case[kind] = payload
        elif kind == "mark":
            if payload not in case["marks"]:
                case["marks"].append(payload)
        elif kind == "parametrize":
            case["params"].append(payload)
            n = payload["count"]
            estimate = estimate * n if n else estimate
            if payload["ids"]:
                case["param_ids"].extend(payload["ids"])
        elif kind == "xfail":
            case["xfail"] = payload
        elif kind == "skip":
            case["skip"] = payload
    case["estimate"] = estimate
    return case


# --------------------------------------------------------------------------
# pytest 实收集（精确的可执行条数）
# --------------------------------------------------------------------------
NODEID_RE = re.compile(r"^(tests[/\\].+?\.py)::([^[\s]+)")


def run_collection() -> tuple[dict, list]:
    """返回 {(relpath, funcname): 条数} 和收集报错的文件。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests",
             "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        out = r.stdout + "\n" + r.stderr
    except Exception as exc:  # noqa: BLE001
        return {}, [f"收集失败：{exc}"]

    counts: dict[tuple, int] = {}
    errors: list[str] = re.findall(r"^(ERROR|CANNOT) (collected|IMPORT).*$", out, re.M)
    err_files = re.findall(r"^_____ ERROR collecting (.+?) _____", out, re.M)
    for line in out.splitlines():
        m = NODEID_RE.match(line.strip())
        if m:
            rel = m.group(1).replace("\\", "/")
            key = (rel, m.group(2))
            counts[key] = counts.get(key, 0) + 1
    return counts, err_files


# --------------------------------------------------------------------------
# YAML 数据矩阵
# --------------------------------------------------------------------------
def parse_yaml_matrices() -> list[dict]:
    import yaml
    out = []
    d = TESTS / "data" / "api"
    if not d.exists():
        return out
    for f in sorted(d.glob("*.yaml")):
        raw = yaml.safe_load(f.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("cases", [])
        cases = []
        for e in raw or []:
            cases.append({
                "id": e.get("id"), "desc": e.get("desc"),
                "method": e.get("method"), "path": e.get("path"),
                "expect": e.get("expect"),
                "expect_msg": e.get("expect_msg"),
                "skip": bool(e.get("skip")),
            })
        out.append({
            "file": f"tests/data/api/{f.name}",
            "title": MATRIX_TITLES.get(f.stem, f.stem),
            "cases": cases,
        })
    return out


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def build_payload(collected: dict, err_files: list) -> dict:
    files = sorted(
        {p.resolve() for p in TESTS.rglob("test_*.py")
         if p.name != "conftest.py"}
        | ({(TESTS / "test_case_data.py").resolve()}
           if (TESTS / "test_case_data.py").exists() else set())
    )

    suites: dict[str, dict] = {}

    def suite_of(rel: str) -> str:
        parts = rel.split("/")
        if len(parts) == 2:                  # tests/test_case_data.py
            return "tests"
        return "tests/" + parts[1]

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        skey = suite_of(rel)
        meta = SUITE_META.get(skey)
        if meta is None:
            continue
        if skey not in suites:
            suites[skey] = {**meta, "modules": [], "key": meta["key"],
                            "name": meta["name"], "desc": meta["desc"],
                            "order": meta["order"], "dir": skey}
        parsed = parse_module(path)
        code = MODULE_CODES.get(path.name, path.stem.replace("test_", "").upper()[:10])
        collected_err = any(e.replace("\\", "/").endswith(rel) for e in err_files)
        cases = []
        for idx, c in enumerate(parsed["cases"], start=1):
            fname = f"{c['cls']}::{c['name']}" if c.get("cls") else c["name"]
            key = (rel, fname)
            real = collected.get(key)
            c2 = dict(c)
            c2["id"] = f"{meta['key']}-{code}-{idx:03d}"
            c2["nodeid"] = f"{rel}::{fname}"
            c2["executable"] = real if real is not None else c["estimate"]
            c2["estimated"] = real is None and not collected_err
            cases.append(c2)
        mod = {
            "file": rel, "code": code, "name": path.name,
            "doc": parsed["doc"], "marks": parsed["marks"],
            "cases": cases,
            "collection_error": collected_err,
        }
        # tests 根套件与 tests/unit 合并展示
        target = suites[skey]
        target["modules"].append(mod)

    suite_list = sorted(suites.values(), key=lambda s: s["order"])
    # 把 "tests"（根散文件）并进 unit 套件
    root_mods = None
    for s in list(suite_list):
        if s["dir"] == "tests":
            root_mods = s["modules"]
            suite_list.remove(s)
    if root_mods:
        for s in suite_list:
            if s["key"] == "UNIT":
                s["modules"].extend(root_mods)
                break

    matrices = parse_yaml_matrices()

    # ---- 统计 ----
    def walk_cases():
        for s in suite_list:
            for m in s["modules"]:
                for c in m["cases"]:
                    yield s, m, c

    total_funcs = sum(1 for _ in walk_cases())
    total_exec = sum(c["executable"] for _, _, c in walk_cases())
    sev_exec = {}
    for _, _, c in walk_cases():
        sev_exec[c["severity"]] = sev_exec.get(c["severity"], 0) + c["executable"]
    xfail_cases = [c for _, _, c in walk_cases() if c["xfail"]]
    skip_cases = [c for _, _, c in walk_cases() if c["skip"]]
    yaml_total = sum(len(m["cases"]) for m in matrices)
    xfail_exec = sum(c["executable"] for c in xfail_cases)

    per_suite = []
    for s in suite_list:
        funcs = sum(len(m["cases"]) for m in s["modules"])
        execs = sum(c["executable"] for m in s["modules"] for c in m["cases"])
        per_suite.append({"key": s["key"], "name": s["name"],
                          "funcs": funcs, "exec": execs})

    return {
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "project": "gitlab-qa · GitLab CE 19.0.1 自动化测试框架",
        "suites": [{
            "key": s["key"], "name": s["name"], "desc": s["desc"],
            "order": s["order"], "modules": s["modules"],
        } for s in suite_list],
        "matrices": matrices,
        "stats": {
            "total_funcs": total_funcs,
            "total_exec": total_exec,
            "sev_exec": sev_exec,
            "xfail": len(xfail_cases),
            "xfail_exec": xfail_exec,
            "skips": len(skip_cases),
            "yaml_total": yaml_total,
            "per_suite": per_suite,
            "collection_errors": [e.replace("\\", "/") for e in err_files],
        },
        "xfail_cases": [
            {"id": c["id"], "suite": s["name"], "module": m["name"],
             "title": c["story"] or c["name"], "reason": c["xfail"]["reason"],
             "strict": c["xfail"]["strict"], "conditional": c["xfail"]["conditional"]}
            for s, m, c in walk_cases() if c["xfail"]
        ],
        "skip_cases": [
            {"id": c["id"], "suite": s["name"], "module": m["name"],
             "title": c["story"] or c["name"], "reason": c["skip"]["reason"]}
            for s, m, c in walk_cases() if c["skip"]
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--no-collect", action="store_true",
                    help="跳过 pytest 收集（条数用 AST 估算）")
    args = ap.parse_args()

    if args.no_collect:
        collected, err_files = {}, []
        print("[inventory] 跳过 pytest 收集，条数为 AST 估算")
    else:
        print("[inventory] 正在收集测试（pytest --collect-only）...")
        collected, err_files = run_collection()
        print(f"[inventory] 收集到 {sum(collected.values())} 条，"
              f"报错文件 {len(err_files)} 个")

    payload = build_payload(collected, err_files)
    print(f"[inventory] 测试函数 {payload['stats']['total_funcs']} 个，"
          f"可执行用例 {payload['stats']['total_exec']} 条，"
          f"YAML 矩阵 {payload['stats']['yaml_total']} 条")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tpl = (ROOT / "tools" / "_inventory_template.html").read_text(encoding="utf-8")
    html = tpl.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    out.write_text(html, encoding="utf-8")
    print(f"[inventory] 已生成 {out}（{out.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
