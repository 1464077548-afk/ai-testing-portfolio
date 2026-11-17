"""覆盖缺口分析：官方规范 97 个模块中，QA 框架 0 覆盖的模块排行。

输出两份信息：
1. QA 已覆盖的 (method, path模板) 集合；
2. 每个 spec tag 模块：操作数、QA 命中数、命中明细 —— 排序后展示 0 覆盖
   的高价值模块，供补测选型。
"""
import ast
import io
import re
import sys
from pathlib import Path

import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
BRACE_RE = re.compile(r"\{[^{}]*\}")


def _norm(p: str) -> str:
    return BRACE_RE.sub("{}", p.rstrip("/"))


def norm_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip()
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                parts.append("{}")
            else:
                return None
        return "".join(parts).strip()
    return None


# ---- QA 框架在用的 (method, 模板) ----
used: set[tuple[str, str]] = set()
for base in (ROOT / "src", ROOT / "tests"):
    for path in base.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = getattr(func, "attr", "")
            if attr not in ("get", "post", "put", "patch", "delete", "paginate"):
                continue
            method = "GET" if attr == "paginate" else attr.upper()
            if not node.args:
                continue
            tpl = norm_literal(node.args[0])
            if tpl and tpl.startswith("/") and len(tpl) > 1:
                used.add((method, _norm(tpl)))

print(f"QA 在用 (method, 模板) 对：{len(used)} 个")

# ---- 解析规范 ----
with open(ROOT / "docs" / "openapi_v2_19.0.1.yaml", encoding="utf-8") as f:
    spec = yaml.safe_load(f)

modules: dict[str, list] = {}
n_ops = 0
for p, item in spec.get("paths", {}).items():
    rel = p[len("/api/v4"):] if p.startswith("/api/v4") else p
    tpl = _norm(rel)
    for m, op in item.items():
        if m not in ("get", "post", "put", "patch", "delete"):
            continue
        n_ops += 1
        for t in op.get("tags", ["(无tag)"]):
            modules.setdefault(t, []).append((m.upper(), tpl))

print(f"规范路径 {len(spec['paths'])} 个，操作 {n_ops} 个，模块 {len(modules)} 个\n")

rows = []
for t, ops in modules.items():
    hits = [o for o in ops if o in used]
    rows.append((t, len(ops), len(hits), ops, hits))

rows.sort(key=lambda r: (-0 if r[2] == 0 else 1, -r[1]))
print("=== 0 覆盖模块（按操作数降序，前 40） ===")
for t, n, h, ops, hits in [r for r in rows if r[2] == 0][:40]:
    print(f"  {n:3d} ops  {t}")

print("\n=== 部分覆盖模块（命中/总数 < 0.5，按缺口降序，前 30） ===")
part = [r for r in rows if 0 < r[2] < r[1] * 0.5]
part.sort(key=lambda r: -(r[1] - r[2]))
for t, n, h, ops, hits in part[:30]:
    print(f"  {h}/{n}  {t}")
    for o in ops:
        mark = "✓" if o in used else "✗"
        print(f"        {mark} {o[0]:6s} {o[1]}")
