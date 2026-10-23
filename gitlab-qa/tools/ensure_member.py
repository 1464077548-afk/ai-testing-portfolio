"""确保存在第二个身份（普通用户），供越权类用例使用。

为什么必须有它：
    用管理员访问管理员端点永远返回 200，越权断言恒真——那是假绿。
    要真验证"普通用户拿 403"，就必须有一个**非管理员**身份。

它做什么（幂等，可反复执行）：
    1. .env 里的 GITLAB_MEMBER_TOKEN 有效且非管理员 → 直接返回
    2. 否则创建一个普通用户 qa-member-xxxx（skip_confirmation，不发邮件）
    3. 管理员为其签发 impersonation token（api / read_api / read_user）
    4. 把 token 写回 .env（**无 BOM**），并用它自检一次 GET /user
    5. 同名用户已存在则复用，不重复创建

用法：
    python tools/ensure_member.py
    python tools/ensure_member.py --rotate      # 强制重签 token
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))

from gqa.api.client import GitLabClient  # noqa: E402
from gqa.api.resources import GitLab  # noqa: E402
from gqa.settings import Settings  # noqa: E402

ENV_FILE = ROOT / ".env"
ENV_KEY = "GITLAB_MEMBER_TOKEN"

TOKEN_SCOPES = ["api", "read_api", "read_user"]


def log(msg: str) -> None:
    print(msg, flush=True)


def random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    # 保证四类字符都出现，避免撞上密码策略
    base = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    base += [secrets.choice(alphabet) for _ in range(length - len(base))]
    secrets.SystemRandom().shuffle(base)
    return "".join(base)


def upsert_env(key: str, value: str) -> None:
    """写 .env：保留其它键，**不写 BOM**。

    带 BOM 的话 dotenv 读到的第一个键名会带 \\ufeff，表现为"明明写了却读不到"。
    """
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    pattern = f"{key}="
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(pattern):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8", newline="\n")


def verify(client_factory, label: str) -> dict | None:
    """用某个 token 打一次 /user，返回用户对象；失败返回 None。"""
    client = client_factory()
    try:
        resp = client.client.get("/user", expect=(200, 401, 403))
        if resp.status != 200 or not isinstance(resp.json_data, dict):
            return None
        return resp.json_data
    finally:
        client.client.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="创建/校验安全测试用的第二个身份")
    p.add_argument("--username", default="", help="指定用户名（默认 qa-member-随机）")
    p.add_argument("--rotate", action="store_true", help="强制重新签发 Token")
    args = p.parse_args(argv)

    s = Settings.load()
    if not s.has_token:
        log("[!] .env 里没有有效的 GITLAB_TOKEN，先跑 deploy.ps1 或 doctor.py --issue-token")
        return 2

    admin = GitLab(GitLabClient(s.base_url, token=s.token, timeout=s.timeout))
    try:
        me = admin.users.current()
        if me.status != 200:
            log(f"[!] 管理员 Token 无效：HTTP {me.status}")
            return 2
        admin_name = me.json_data.get("username")

        # ---- 1) 已有 member token 且有效 → 直接复用
        if s.has_member_token and not args.rotate:
            who = verify(lambda: GitLab(GitLabClient(s.base_url, token=s.member_token,
                                                     timeout=s.timeout)), "member")
            if who and not who.get("is_admin"):
                log(f"[ok] 已有第二身份：{who.get('username')}（非管理员），无需改动")
                log("     如需换新 Token：--rotate")
                return 0
            log("[!] 现有 GITLAB_MEMBER_TOKEN 无效或竟然是管理员，重新创建")

        # ---- 2) 找或建普通用户
        username = args.username or f"qa-member-{secrets.token_hex(2)}"
        existing = admin.users.list(username=username)
        if existing:
            user = existing[0]
            log(f"[ok] 复用已存在用户：{username}（id={user['id']}）")
        else:
            payload = {
                "username": username,
                "name": "QA Member (automation)",
                # .invalid 是保留域名，永远不会误发邮件
                "email": f"{username}@example.invalid",
                "password": random_password(),
                "skip_confirmation": True,
                "can_create_group": False,
                "projects_limit": 20,
            }
            resp = admin.client.post("/users", json_body=payload,
                                     expect=(201, 400, 409))
            if resp.status not in (201,):
                log(f"[!] 创建用户失败：HTTP {resp.status} {resp.json_data}")
                return 1
            user = resp.json_data
            log(f"[ok] 已创建普通用户：{username}（id={user['id']}）")

        if user.get("is_admin"):
            log("[!] 目标用户是管理员，越权用例会失去意义。换一个用户名重试。")
            return 1

        # ---- 3) 签发 impersonation token（管理员才能签，正好免去用户密码登录）
        expires = (date.today() + timedelta(days=365)).isoformat()
        token_body = {
            "name": f"qa-member-auto-{secrets.token_hex(2)}",
            "scopes": TOKEN_SCOPES,
            "expires_at": expires,
        }
        tok = admin.client.post(f"/users/{user['id']}/impersonation_tokens",
                                json_body=token_body, expect=(201, 400, 401, 404))
        if tok.status != 201:
            log(f"[!] 签发 impersonation token 失败：HTTP {tok.status} {tok.json_data}")
            log("    手动兜底：用该用户登录 GitLab → Preferences → Access Tokens 建一个，"
                f"把值写进 .env 的 {ENV_KEY}")
            return 1

        token = tok.json_data.get("token")
        if not token:
            log(f"[!] 响应里没有 token 字段：{tok.json_data}")
            return 1
        upsert_env(ENV_KEY, token)
        log(f"[ok] 已签发 Token 并写入 .env（{ENV_KEY}，有效期至 {expires}）")

        # ---- 4) 自检：新身份必须能认出来，且不是管理员
        who = verify(lambda: GitLab(GitLabClient(s.base_url, token=token,
                                                 timeout=s.timeout)), "member")
        if not who:
            log("[!] 新 Token 自检失败：GET /user 没返回 200")
            return 1
        if who.get("is_admin"):
            log("[!] 新 Token 竟然是管理员，拒绝写入")
            return 1

        log(f"[ok] 自检通过：{who.get('username')} 可访问 API，is_admin=False")
        log("")
        log("下一步：python tools/run.py --suite security")
        return 0
    finally:
        admin.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
