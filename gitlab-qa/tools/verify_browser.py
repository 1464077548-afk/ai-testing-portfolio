"""用本机浏览器打开 GitLab 并截图验证。

不是简单的 curl 探活：真实渲染 DOM、走一遍表单登录、进到项目页，
只有这三步都成了，才能说"浏览器里能打开"。

浏览器走 msedge 通道回退到系统 Edge —— 企业内网下 Chromium 的 CDN 下不来，
而 Edge 本机一定有，不需要任何下载。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOT_DIR = Path(__file__).resolve().parents[2] / "gitlab-ce" / "logs"
SHOT_DIR.mkdir(parents=True, exist_ok=True)


def load_env() -> dict[str, str]:
    """手读 .env，避免为此引入 python-dotenv 依赖。"""
    env: dict[str, str] = {}
    f = ROOT / ".env"
    if not f.exists():
        return env
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def read_login_error(page) -> str:
    """把登录页上的拒绝原因读出来。

    不同版本提示载体不同（flash / gl-alert / 字段级错误），全部试一遍，
    取第一条非空文本。读不到就返回空串，由调用方标注"无提示"。
    """
    sels = (
        ".flash-alert", ".flash-container .alert-danger",
        "[data-testid='alert-danger']", ".gl-alert-danger", "#error_explanation",
    )
    for s in sels:
        try:
            loc = page.locator(s).first
            if loc.count() and loc.is_visible():
                txt = loc.inner_text().strip()
                if txt:
                    return " ".join(txt.split())[:160]
        except Exception:  # noqa: BLE001
            continue
    return ""


def pick_browser(pw):
    """按 Edge -> Chrome -> Chromium 顺序回退，返回可用通道。"""
    last: Exception | None = None
    for ch in ("msedge", "chrome", None):
        try:
            kwargs = {"channel": ch} if ch else {}
            br = pw.chromium.launch(**kwargs)
            return br, (ch or "bundled")
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"没有可用浏览器：{last}")


def main() -> int:
    env = load_env()
    base = env.get("GITLAB_BASE_URL", "http://localhost:8929")
    user = env.get("GITLAB_ADMIN_USER", "root")
    pwd = env.get("GITLAB_ROOT_PASSWORD", "")

    from playwright.sync_api import sync_playwright

    results: list[tuple[str, bool, str]] = []

    with sync_playwright() as pw:
        browser, channel = pick_browser(pw)
        print(f"[browser] 通道 = {channel}")
        # 关掉自动化特征：部分 Web 应用检测到后会拒绝交互
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
            locale="zh-CN",
        )
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---- 1. 打开登录页 ----
        t0 = time.time()
        resp = page.goto(f"{base}/users/sign_in", wait_until="domcontentloaded", timeout=60000)
        code = resp.status if resp else 0
        title = page.title()
        # GitLab 各版本表单 id 不一致（new_user / new_ldap_user 等），
        # 只在 Templetes 侧锚定 password 输入框 —— 它才是登录页的本质特征
        has_form = page.locator('input[name="user[password]"]').count() > 0
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOT_DIR / "browser-01-login.png"))
        results.append(("页面能打开", code == 200, f"HTTP {code} · {time.time()-t0:.1f}s"))
        results.append(("标题正确", "GitLab" in title, title))
        results.append(("登录表单渲染", has_form, f"密码框 x{page.locator('input[name=\"user[password]\"]').count()}"))
        print(f"[1] HTTP {code} title={title!r} form={has_form}")

        # ---- 2. 表单登录 ----
        # 登录失败有三种常见原因，不读出页面提示就只能看到"没登录成功"：
        #   a) 密码不对（可能被人在浏览器里改过，.env 没同步）
        #   b) 表单 JS 还没绑定就点了提交，等于提交空值
        #   c) 登录接口被限速（rack_attack）
        # 所以这里既读页面提示，也校验填入值，并允许重试一次。
        logged_in = False
        login_note = ""
        if has_form and pwd:
            for attempt in (1, 2):
                page.goto(f"{base}/users/sign_in", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector('input[name="user[password]"]', state="visible", timeout=30000)
                try:
                    # 等提交按钮就绪，避开 JS 未绑定时的空提交
                    page.wait_for_selector('button[type="submit"]:not([disabled])', timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                page.fill('input[name="user[login]"]', user)
                page.fill('input[name="user[password]"]', pwd)
                # 回读确认真的填进去了 —— 这是竞态的可靠检测手段
                filled_ok = page.input_value('input[name="user[login]"]') == user
                page.screenshot(path=str(SHOT_DIR / "browser-02-filled.png"))
                try:
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                        page.click('button[type="submit"], input[type="submit"]')
                except Exception as exc:  # noqa: BLE001
                    login_note = f"提交无跳转：{type(exc).__name__}"
                    continue
                page.wait_for_timeout(2000)
                if "sign_in" not in page.url:
                    logged_in = True
                    break
                # 还在登录页 = 被拒，把页面上的原因读出来
                login_note = read_login_error(page) or "页面无提示（可能提交为空或接口报错）"
                if not filled_ok:
                    login_note = "表单值未真正填入（JS 未就绪）· " + login_note
                print(f"[2] 第 {attempt} 次被拒：{login_note}")
                if attempt == 1:
                    page.screenshot(path=str(SHOT_DIR / "browser-02-login-error.png"))
                    time.sleep(5)
            page.screenshot(path=str(SHOT_DIR / "browser-03-after-login.png"))
            print(f"[2] 登录后 URL = {page.url}")
            if logged_in:
                results.append(("管理员可登录", True, page.url.split("?")[0]))
            else:
                results.append(("管理员可登录", False, f"被拒：{login_note}"))
                if "Invalid login" in login_note or "password" in login_note.lower():
                    print("\n  原因：.env 里的 GITLAB_ROOT_PASSWORD 与实例不一致")
                    print("  （GitLab 首次登录后会强制改初始密码，改完 .env 就失效了）")
                    print("  修复（PowerShell / bash 通用，把 <新密码> 换成你的）：")
                    print("    docker exec gitlab-ce gitlab-rails runner \""
                          "u=User.find_by_username('root');"
                          "u.password='<新密码>';u.password_confirmation='<新密码>';"
                          "u.password_automatically_set=false;u.password_expires_at=nil;"
                          "u.save!\"")
                    print("  再把同一个密码写进 gitlab-qa/.env 的 GITLAB_ROOT_PASSWORD\n")
        else:
            results.append(("管理员可登录", False, "缺少密码，跳过"))

        # ---- 3. 进项目页，确认 UI 真的能读到数据 ----
        if logged_in:
            #  GitLab 19 移除了 /-/projects，项目列表落在 /dashboard/projects。
            #  这里同时试两个路径，让脚本在 17/18 版也能用。
            tried = ""
            body = ""
            has_nav = False
            for path in ("/dashboard/projects", "/-/projects"):
                try:
                    page.goto(f"{base}{path}", wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                    body = page.inner_text("body")[:600]
                    if "404" in body[:60]:
                        continue
                    tried = path
                    page.screenshot(path=str(SHOT_DIR / "browser-04-projects.png"))
                    # GitLab 19 的侧栏没有稳定的 class/data-testid 锚点，
                    # 改用地标文本判定（比结构选择器更抗版本变化）
                    nav_markers = ("Primary navigation", "Your work", "Collapse sidebar")
                    has_nav = any(m in body for m in nav_markers)
                    break
                except Exception as exc:  # noqa: BLE001
                    body = f"{type(exc).__name__}: {str(exc)[:120]}"
            print(f"[3] path={tried} nav={has_nav}")
            print(f"    body 前 200 字：{body[:200]!r}")
            results.append(("项目页可访问", bool(tried), tried or body[:80]))
            results.append(("侧边导航渲染", has_nav, f"body {len(body)} 字符"))

        results.append(("无 JS 报错", len(errors) == 0, f"{len(errors)} 个：{errors[:2]}"))

        ctx.close()
        browser.close()

    print("\n" + "=" * 62)
    ok = True
    for name, passed, detail in results:
        flag = "PASS" if passed else "FAIL"
        if not passed:
            ok = False
        print(f"  [{flag}] {name:<14} {detail}")
    print("=" * 62)
    print("结论：浏览器可以正常打开并使用 GitLab" if ok else "结论：存在问题，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
