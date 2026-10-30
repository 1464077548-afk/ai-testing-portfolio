"""变异自检：故意把「已固化的事实」改错，确认对应用例会变红。

目的是回答一个问题：这组断言里有没有永真的摆设？
做法是往源码/用例里注入与实测相反的错误，看用例是否抓得住。
抓不住的断言等于没写。

每个变异跑完立即还原，不留痕迹。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SM = ROOT / "tests" / "api" / "test_auth_state_machine.py"
CRED = ROOT / "src" / "gqa" / "api" / "credentials.py"
PY = sys.executable

# (文件名, 原文片段, 替换成, 对应用例, 期望注入后失败的原因)
MUTATIONS = [
    (
        CRED,
        'r = self.session.post(f"{self.base_url}{self.SIGN_OUT}",',
        'r = self.session.delete(f"{self.base_url}{self.SIGN_OUT}",',
        "test_ui_logout_destroys_session",
        "登出改用 DELETE（实测 404）应导致会话没销毁",
    ),
    (
        SM,
        '    with allure.step("访问被拒"):\n        assert token_factory.probe(tok.token) == 401,',
        '    with allure.step("访问被拒"):\n        assert token_factory.probe(tok.token) == 200,',
        "test_token_expiring_today_is_already_dead",
        "把「当天到期即失效」反转为期待 200，应被真实行为打脸",
    ),
    (
        SM,
        "    assert second.status in (400, 401),",
        "    assert second.status in (200,),",
        "test_refresh_token_is_single_use",
        "允许 refresh_token 二次兑换成功，应被 400 打脸",
    ),
    (
        SM,
        "    assert refreshed.access_token != old, \"刷新后 access_token 没变，等于没刷新\"",
        "    assert refreshed.access_token == old, \"（注入的反向断言）\"",
        "test_refresh_issues_new_token_and_kills_old",
        "要求刷新后令牌不变，应被换值行为打脸",
    ),
]


def run_case(nodeid: str) -> bool:
    """跑单条用例，返回是否通过。"""
    import os
    env = dict(os.environ, CODEBUDDY_SAFE_DELETE_ENABLED="0")
    proc = subprocess.run(
        [PY, "-m", "pytest", nodeid, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=300, env=env,
    )
    return proc.returncode == 0


def main() -> int:
    backups = {p: p.read_text(encoding="utf-8") for p in (SM, CRED)}
    results: list[tuple[str, bool, str]] = []

    try:
        for path, old, new, case, why in MUTATIONS:
            text = path.read_text(encoding="utf-8")
            if old not in text:
                print(f"[SKIP] {case}: 未找到注入点（用例改过？）")
                continue
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            passed = run_case(f"tests/api/test_auth_state_machine.py::{case}")
            caught = not passed          # 注入后失败了 = 断言抓得住
            results.append((case, caught, why))
            print(f"[{'OK  ' if caught else 'MISS'}] {case}: 注入后"
                  f"{'变红（断言有效）' if caught else '仍然通过（断言是摆设！）'} —— {why}")
            path.write_text(backups[path], encoding="utf-8")
    finally:
        for p, original in backups.items():
            p.write_text(original, encoding="utf-8")

    missed = [r for r in results if not r[1]]
    print(f"\n共注入 {len(results)} 处，抓住 {len(results) - len(missed)} 处")
    if missed:
        for case, _, why in missed:
            print(f"  未抓住：{case} —— {why}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
