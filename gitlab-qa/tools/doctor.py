"""环境自检：跑测试之前先确认「被测系统是什么状态」。

这个工具解决的是最浪费时间的那类问题：跑完 200 条用例全红，
最后发现是 Token 过期或者地址写错了。30 秒的自检能省掉这个来回。

    python tools/doctor.py
    python tools/doctor.py --base-url https://gitlab.com

    # 自建实例：用 root 密码签发一个测试专用 PAT（CI 与本地初始化都靠它）
    python tools/doctor.py --issue-token --base-url http://localhost:8929

    # --container 只在自动发现失败时才需要手动指定（默认容器名 gitlab-ce）
    python tools/doctor.py --issue-token --container gitlab-ce

注意：签发 PAT 走的是容器内的 rails runner，不需要密码；
GitLab 没有「用密码换 PAT」的公开 API，所以 --password 不参与这个模式。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  #- `.absolute()`：只补全成绝对路径，**不会解析软链接**，也不保证消去 `..`
                                                            #- `.resolve()`：不仅绝对，还要**解析软链、清理冗余段**，拿到底层真实路径

from gqa.api.client import GitLabClient  # noqa: E402
from gqa.settings import Settings  # noqa: E402

OK = "  [OK]  "
BAD = "  [!!]  "
INFO = "  [--]  "
NOTE = "  [注]  "


# GitLab 19 的两条硬规则（与 gitlab-ce/deploy.ps1 保持一致）：
#   1) read_registry 已移除（改为 read_virtual_registry），写进去会整个校验失败
#   2) PAT 必须带 expires_at，上限 365 天；传 nil 会报 "Expiration date can't be blank"
SCOPES = ["api", "read_api", "read_user", "read_repository", "write_repository"]


def _docker_ps(extra: list[str]) -> list[str]:
    import subprocess
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"] + extra,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def list_containers() -> list[str]:
    """列出当前运行中的容器名；docker 不可用时返回空列表。"""
    return _docker_ps([])


def list_stopped_containers() -> list[str]:
    """列出已存在但已停止的容器名。"""
    return _docker_ps(["-a", "--filter", "status=exited"])


def resolve_container(wanted: str | None) -> tuple[str | None, str]:
    """确定用哪个容器。

    容器名填错很常见（目录名 gitlab-qa ≠ 容器名 gitlab-ce）。
    原样把 docker 的 "No such container" 抛给用户毫无帮助，所以分三种情况处理：
      1. 指定的容器存在 -> 直接用
      2. 不存在 -> 自动找名字含 gitlab 的容器，唯一命中就改用并说明
      3. 都找不到 -> 列出当前所有容器，让用户一眼看到该填什么
    """
    running = list_containers()
    if wanted and wanted in running:
        return wanted, ""

    if not running:
        stopped = list_stopped_containers()
        gl = [c for c in stopped if "gitlab" in c.lower()]
        if gl:
            return None, (f"GitLab 容器存在但已停止：{', '.join(gl)}\n"
                          f"           启动它：docker start {gl[0]}（就绪需 1~3 分钟）")
        return None, "当前没有运行中的容器。先启动 GitLab：cd ..\\gitlab-ce && .\\deploy.ps1"

    cands = [c for c in running if "gitlab" in c.lower()]

    if wanted:
        if len(cands) == 1:
            return cands[0], (f"容器 '{wanted}' 不存在，已自动改用 '{cands[0]}'"
                              f"（目录名与容器名不同 —— 本项目容器叫 gitlab-ce）")
        if len(cands) > 1:
            return None, (f"容器 '{wanted}' 不存在；检测到多个 GitLab 容器，"
                          f"请用 -c 指定：{', '.join(cands)}")
        return None, (f"容器 '{wanted}' 不存在，也没有名字含 gitlab 的容器。\n"
                      f"           当前运行中的容器：{', '.join(running[:12])}")

    if "gitlab-ce" in running:
        return "gitlab-ce", ""
    if len(cands) == 1:
        return cands[0], f"未指定容器，自动选用 '{cands[0]}'"
    if len(cands) > 1:
        return None, f"检测到多个 GitLab 容器，请用 -c 指定：{', '.join(cands)}"
    return None, (f"没找到 GitLab 容器（当前运行的：{', '.join(running[:12])}）。\n"
                  f"           如容器名不含 gitlab，请用 -c 显式指定。")


def diagnose_issue_error(stderr: str) -> str:
    """把 rails 的原始校验错误翻译成人能看懂的原因。"""
    s = stderr.lower()
    if "expiration date can't be blank" in s:
        return ("GitLab 18+ 要求 PAT 必须带过期时间（上限 365 天），"
                "本脚本已默认设 365 天；若你改过脚本请检查 expires_at。")
    if "only contain available scopes" in s:
        return ("scope 里有本版本不支持的取值（GitLab 19 已移除 read_registry，"
                "改用 read_virtual_registry）。可用列表：docker exec gitlab-ce "
                "gitlab-rails runner \"puts Gitlab::Auth.all_available_scopes.to_a.join(',')\"")
    if "user not found" in s:
        return "实例里没有这个用户。用 --user 指定，或先跑 deploy.ps1 建 root。"
    if "no such container" in s:
        return "容器名不对（容器名 ≠ 目录名，本项目是 gitlab-ce）。"
    if "unique violation" in s or "token_digest" in s:
        return ("固定 token 值（QaLabToken-2026）对应的 digest 在库中已存在 —— "
                "可能是之前 revoke! 只打标记没删行导致的累积；"
                "本脚本已改为 destroy! 物理删除旧 token，重跑一次即可。")
    return ""


def issue_token(args: argparse.Namespace) -> int:
    """为自建实例签发测试专用 PAT。

    GitLab 没有「用用户名密码换 PAT」的公开 API——个人访问令牌只能通过
    UI 或管理员接口创建。对 docker 部署的实例，唯一稳的路子是进容器跑
    rails runner。这里就这么做，并且**打印在最后一行**，方便 CI 里
    `| tail -1` 取值。
    """
    import subprocess

    container, note = resolve_container(args.container)
    if container is None:
        print(f"{BAD}找不到可用的 GitLab 容器")
        if note:
            print(note)
        return 2
    if note:
        print(f"{NOTE}{note}")
    if args.password:
        # 这条路径走 rails runner，用不到密码；不说明的话会被误以为"密码没生效"
        print(f"{NOTE}--password 在 --issue-token 模式下不参与（无密码换 PAT 的公开 API）")

    value = args.token_value or "QaLabToken-2026"
    scopes = ",".join(f":{s}" for s in SCOPES)

    # 先删同名旧 token：GitLab 允许同名 token 并存，重跑几次就会攒一堆
    # 分不清哪个在用，也让"换 token"变成玄学。删掉才是幂等的。
    script = (
        f"user = User.find_by_username('{args.user}') || User.find_by_username('root'); " ## 找到用户，优先取传入的 args.user，找不到 fallback 到 root
        f"raise 'user not found' if user.nil?; "## 找不到直接抛异常
        f"user.personal_access_tokens.where(name: 'gitlab-qa').find_each {{ |old| old.destroy! }}; " ## 删除所有同名旧的 gitlab-qa 令牌，避免重名冲突
        f"t = user.personal_access_tokens.create("
        f"name: 'gitlab-qa', scopes: [{scopes}], expires_at: 365.days.from_now); " ## 创建新 PAT，指定名称、权限范围、1年后过期
        f"t.set_token('{value}'); t.save!; " ## 设置自定义明文 token（GitLab 企业版支持 set_token，社区版早期版本不支持！）
        f"puts t.token" # 打印token，供外部subprocess捕获
    )
    cmd = ["docker", "exec", "-i", container,
           "gitlab-rails", "runner", "--environment", "production", script] #`gitlab-rails runner`：GitLab 内置 Rails 控制台脚本执行器
    #docker exec -i 容器名 gitlab-rails runner --environment production "user = User.find_by_username('root') || User.find_by_username('root'); raise 'user not found' if user.nil?; user.personal_access_tokens.where(name: 'gitlab-qa').find_each { |old| old.destroy! }; t = user.personal_access_tokens.create(name: 'gitlab-qa', scopes: [api, read_repository, write_repository], expires_at: 365.days.from_now); t.set_token('glpat-xxxx'); t.save!; puts t.token"

    print(f"[{container}] 签发 PAT（name=gitlab-qa, scopes={SCOPES}）")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        print(f"{BAD}找不到 docker 命令")
        return 2
    except subprocess.TimeoutExpired:
        print(f"{BAD}rails runner 超时 —— GitLab 首次启动要几分钟，等服务就绪再试")
        return 2

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        print(f"{BAD}签发失败（rc={proc.returncode}）")
        # 打印头部而不是尾部：Ruby 异常的根因在第一行，
        # 尾部是几十层框架堆栈，看了也白看。
        err_lines = [l for l in (proc.stderr or "").splitlines() if l.strip()]
        for line in err_lines[:6]:
            print("       " + line.strip()[:200])
        hint = diagnose_issue_error(proc.stderr or "")
        if hint:
            print(f"{NOTE}{hint}")
        return 2

    token = out.splitlines()[-1].strip()
    print(f"{OK}Token 已签发")
    # 最后一行只输出 token，便于 CI 用 `| tail -1` 抓取
    print()
    print(token)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GitLab 测试环境自检工具")
    parser.add_argument("--base-url", "-u", default=None, help="GitLab 实例地址，如 http://127.0.0.1:8080")
    parser.add_argument("--token", "-t", default=None, help="已有的 GitLab PAT 令牌")
    parser.add_argument("--env", "-e", default=None, help="环境标识，如 test / staging")
    parser.add_argument("--issue-token", action="store_true",
                        help="签发新测试PAT，执行成功后单独输出token（用于流水线捕获）")
    parser.add_argument("--user", "-U", default="root", help="GitLab登录用户名，默认root")
    parser.add_argument("--password", "-p", default=None, metavar="PASSWORD", help="GitLab用户密码")
    parser.add_argument("--container", "-c", default=None,
                        help="Docker容器名，默认自动发现（本项目为 gitlab-ce）")
    parser.add_argument("--token-value", default=None, help="指定PAT明文，用于固定token复现测试")
    args = parser.parse_args()

    '''
    两种模式区分：
    1. 自检模式：给 `--token`，调用 GitLab 接口验证连通性、权限、项目、issue 等
    2. 发令牌模式：`--issue-token`，用账号密码登录，创建 PAT，stdout 输出 token 给上层脚本 /subprocess 捕获
    '''
    if args.issue_token:
        return issue_token(args)

    s = Settings.load(env=args.env, base_url=args.base_url, token=args.token)
    print(f"\n环境自检：{s.describe()}\n")

    client = GitLabClient(s.base_url, token=s.token, timeout=min(s.timeout, 10))
    exit_code = 0

    # 1. 连通性
    print("[1] 连通性")
    started = time.perf_counter()
    try:
        resp = client.get("/projects", params={"per_page": 1}, expect=(200, 401, 403))
        ms = (time.perf_counter() - started) * 1000
        print(f"{OK}服务可达：HTTP {resp.status}，{ms:.0f} ms")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}服务不可达：{exc}")
        print(f"{INFO}若为本地部署，先确认容器已启动：docker compose ps")
        return 2

    # 2. Token
    print("\n[2] 鉴权")
    if not s.has_token:
        print(f"{BAD}未配置 GITLAB_TOKEN，需要鉴权的用例会全部 skip")
        print(f"{INFO}部署脚本会自动写入 .env：gitlab-ce\\deploy.ps1")
        exit_code = 1
    else:
        r = client.get("/user", expect=(200, 401, 403))
        if r.status == 200:
            print(f"{OK}Token 有效，当前用户：{r.json_data.get('username')}")
        elif r.status == 401:
            print(f"{BAD}Token 无效或已过期（HTTP 401）")
            exit_code = 1
        else:
            print(f"{BAD}鉴权异常（HTTP {r.status}）")
            exit_code = 1

    # 3. 版本
    print("\n[3] 版本与能力")
    v = client.get("/version", expect=(200, 401, 403))
    if v.status == 200:
        print(f"{OK}GitLab {v.json_data.get('version')} "
              f"(revision {str(v.json_data.get('revision'))[:8]})")
    else:
        print(f"{INFO}版本接口需要认证（HTTP {v.status}），不影响测试")

    # 4. 分页头
    print("\n[4] 分页元信息")
    try:
        resp = client.get("/projects", params={"per_page": 5}, expect=(200, 401, 403))
        if resp.status == 200:
            lower = {k.lower(): v for k, v in resp.headers.items()}
            if "x-per-page" in lower:
                print(f"{OK}分页头可用：X-Per-Page={lower.get('x-per-page')} "
                      f"X-Total={lower.get('x-total', 'n/a')}")
            else:
                print(f"{INFO}未返回 X-* 分页头，分页相关用例会自动跳过")
        else:
            print(f"{INFO}无权限查看项目列表，跳过分页检查")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}分页检查失败：{exc}")

    # 5. 写权限
    print("\n[5] 写权限（destructive 用例能否执行）")
    if not s.has_token:
        print(f"{INFO}无 Token，跳过")
    else:
        probe = GitLabClient(s.base_url, token=s.token, timeout=10)
        try:
            resp = probe.post("/projects", json_body={"name": "__doctor_probe__"},
                              expect=(201, 400, 401, 403))
            if resp.status == 201:
                pid = resp.json_data["id"]
                probe.delete(f"/projects/{pid}", expect=(202, 204, 404))
                print(f"{OK}具备创建/删除项目的权限（已清理探针项目）")
            elif resp.status == 401:
                print(f"{BAD}Token 无写权限（401），destructive 用例会失败")
                exit_code = 1
            elif resp.status == 403:
                print(f"{BAD}被拒绝（403），该账号无权创建项目")
                exit_code = 1
            else:
                print(f"{INFO}创建返回 {resp.status}，可能是命名冲突或策略限制")
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD}写权限检查异常：{exc}")
            exit_code = 1
        finally:
            probe.close()

    # 6. UI 依赖
    print("\n[6] UI 测试依赖")
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                b = p.chromium.launch(headless=True)
                b.close()
                print(f"{OK}Playwright + Chromium 可用")
            except Exception as exc:  # noqa: BLE001
                print(f"{BAD}Chromium 不可用：{str(exc)[:120]}")
                print(f"{INFO}执行：playwright install chromium")
                exit_code = max(exit_code, 1)
    except ImportError:
        print(f"{INFO}未安装 playwright，UI 用例会跳过")

    client.close()

    print()
    if exit_code == 0:
        print("结论：环境就绪，可以开跑。")
    else:
        print("结论：存在问题，按上面提示处理后重跑本脚本。")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
