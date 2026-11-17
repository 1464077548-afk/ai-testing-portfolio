"""核对：哪些用例的条数是估算的（收集里没数到）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_case_inventory as inv

collected, err_files = inv.run_collection()
payload = inv.build_payload(collected, err_files)

print(f"collected total = {sum(collected.values())}")
for su in payload["suites"]:
    for m in su["modules"]:
        for c in m["cases"]:
            fname = c["nodeid"].split("::", 1)[1]
            real = collected.get((m["file"], fname))
            if c["estimated"]:
                print(f"[EST] {c['id']} {fname} 估计 {c['estimate']} 收集 {real}")
            elif real is not None and real != c["estimate"]:
                print(f"[DIFF] {c['id']} {fname} AST估算 {c['estimate']} 收集 {real}")

# 再反查：收集到但 AST 没登记的函数
ast_keys = set()
for su in payload["suites"]:
    for m in su["modules"]:
        for c in m["cases"]:
            ast_keys.add((m["file"], c["nodeid"].split("::", 1)[1]))
extra = {k: v for k, v in collected.items() if k not in ast_keys}
for k, v in sorted(extra.items()):
    print(f"[EXTRA] {k} = {v}")
