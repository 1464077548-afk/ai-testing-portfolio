"""差集分析：QA 框架在用、但官方 openapi_v2 规范未收录的接口。"""
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


used: dict[str, set] = {}
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
                used.setdefault(_norm(tpl), set()).add(method)

with open(ROOT / ".tmp" / "openapi_v2_19.0.1.yaml", encoding="utf-8") as f:
    spec = yaml.safe_load(f)
spec_paths = set()
for p, item in spec.get("paths", {}).items():
    rel = p[len("/api/v4"):] if p.startswith("/api/v4") else p
    for m in item:
        if m in ("get", "post", "put", "patch", "delete", "head"):
            spec_paths.add((m.upper(), _norm(rel)))

extra = {}
for tpl, methods in sorted(used.items()):
    hits = [m for m in methods if (m, tpl) in spec_paths]
    miss = [m for m in methods if (m, tpl) not in spec_paths]
    if miss:
        extra[tpl] = (miss, hits)

print(f"QA 模板 {len(used)} 个，其中有方法未命中规范的有 {len(extra)} 个：")
for tpl, (miss, hits) in extra.items():
    print(f"  {'/'.join(miss):8s} {tpl}   (规范已有: {hits})")
