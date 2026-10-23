"""产物清理：定时清日志、截图、报告。

三类产物的增长方式不同，但都会把磁盘吃满：

    artifacts/        每条 UI 用例一个 trace.zip（1–5 MB），失败再加截图 + DOM 快照
    allure-results/   每次运行一批 JSON + 附件
    allure-report/    静态报告（可重建，但会留多份）
    gitlab-ce/logs/   每次部署 / 浏览器验证一份日志与截图

三条保留规则同时生效，结果取"最保守"：

    1. 按时间：只删 N 天前的文件（默认 7）
    2. 按体积：目录超过上限时，从最旧开始删到阈值内（默认 1024 MB）
    3. 按数量：每个目录至少保留最新 K 个文件（默认 3），防止清空

两个必须保护的东西：

    allure-results/history/   趋势图的原料。删了，报告就永远是孤立的单次快照
    allure-report/history/    run.py 会从这里继承上一次的 history

为什么不是一句 `rm -rf`：公司终端和带 DLP 的机器会拦批量删除，
甚至在删除过程中把进程干掉。所以这里逐文件删、单个失败只记录不中断，
把失败原因打出来让人判断，而不是让整轮清理静默失效。

用法：
    python tools/cleanup.py                    # 按 .env 配置清理
    python tools/cleanup.py --dry-run          # 只看会删什么，不动文件
    python tools/cleanup.py --days 3           # 临时覆盖保留天数
    python tools/cleanup.py --targets logs      # 只清日志
    python tools/cleanup.py --all              # 忽略保留策略（仍保护 history/.env）
    python tools/cleanup.py --print-cron       # 打印 Linux/macOS 的 crontab 行
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # gitlab-qa/
REPO = ROOT.parent                                  # 仓库根（含 gitlab-qa 与 gitlab-ce）
sys.path.insert(0, str(ROOT / "src"))

from gqa.settings import Settings  # noqa: E402

# 这些名字的目录永不删（趋势原料）
PROTECTED_DIRS = {"history"}
# 这些文件名永不删（配置与说明）
PROTECTED_FILES = {".env", ".env.example", ".gitignore", "README.md", ".keep"}


@dataclass(frozen=True)
class Target:
    name: str
    path: Path
    mode: str = "files"      # files=按文件清理；tree=整目录按最后更新时间判断


def build_targets() -> dict[str, Target]:
    return {
        "artifacts": Target("artifacts", ROOT / "artifacts"),
        "results": Target("results", ROOT / "allure-results"),
        # 报告是派生产物且是上百个文件，按文件级保留没有意义：
        # 整目录很久没更新才清空（history 子目录仍受保护）
        "report": Target("report", ROOT / "allure-report", mode="tree"),
        "logs": Target("logs", REPO / "gitlab-ce" / "logs"),
        "cache": Target("cache", ROOT / ".pytest_cache"),
    }


@dataclass
class Plan:
    target: Target
    delete: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)
    note: str = ""


def hsize(num: float) -> str:
    if num < 1024:
        return f"{int(num)} B"
    for unit in ("KB", "MB", "GB"):
        num /= 1024.0
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}"
    return f"{num:.1f} GB"


def inside(path: Path, base: Path) -> bool:
    """路径必须落在 base 之内——防止符号链接把删除带到项目外。"""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def iter_files(base: Path) -> list[Path]:
    """递归列出可清理文件，跳过保护目录与保护文件。"""
    if not base.exists() or not base.is_dir():
        return []
    out: list[Path] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        parts = p.relative_to(base).parts
        if any(part in PROTECTED_DIRS for part in parts[:-1]):
            continue
        if p.name in PROTECTED_FILES:
            continue
        out.append(p)
    return out


def age_days(mtime: float) -> float:
    return max(0.0, (time.time() - mtime) / 86400.0)


def plan_target(t: Target, days: int, max_mb: int, keep_latest: int,
                wipe: bool) -> Plan:
    files = iter_files(t.path)
    plan = Plan(target=t)

    if not files:
        plan.note = "目录不存在或为空"
        return plan

    if wipe:
        plan.delete = list(files)
        plan.note = "--all：忽略保留策略（history/ 与 .env 仍受保护）"
        return plan

    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)   # 新 → 旧

    if t.mode == "tree":
        newest = files[0].stat().st_mtime
        age = age_days(newest)
        if age >= days:
            plan.delete = list(files)
            plan.note = f"整目录 {age:.1f} 天未更新（阈值 {days} 天）"
        else:
            plan.kept = list(files)
            plan.note = f"最近 {age:.1f} 天内有更新，保留"
        return plan

    keep = files[:max(0, keep_latest)]
    rest = files[len(keep):]

    cutoff = time.time() - days * 86400
    eligible = {f for f in rest if f.stat().st_mtime < cutoff}

    # 体积兜底：没到天数但目录太胖，也从最旧的开始删
    if max_mb > 0:
        total = sum(f.stat().st_size for f in files)
        limit = max_mb * 1024 * 1024
        if total > limit:
            for f in sorted(rest, key=lambda x: x.stat().st_mtime):
                if total <= limit:
                    break
                if f in eligible:
                    continue
                eligible.add(f)
                total -= f.stat().st_size
            plan.note = f"超过体积上限 {max_mb} MB，追加删除最旧的"

    plan.delete = [f for f in files if f in eligible]
    plan.kept = [f for f in files if f not in eligible]
    return plan


def execute(plan: Plan, dry_run: bool) -> tuple[int, int, list[str]]:
    removed = 0
    freed = 0
    errors: list[str] = []

    for f in plan.delete:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0

        if dry_run:
            removed += 1
            freed += size
            continue

        if not inside(f, plan.target.path):
            # 只在 target 目录内删除；软链指向外部就跳过
            errors.append(f"{f}: 解析后不在目标目录内，跳过")
            continue

        try:
            f.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            errors.append(f"{f.relative_to(REPO)}: {exc}")

    return removed, freed, errors


def print_cron() -> None:
    py = "python3" if sys.platform != "win32" else "python"
    print("\n# 加入 crontab（crontab -e），每天 03:00 清理")
    print(f"0 3 * * * cd {ROOT} && {py} tools/cleanup.py --quiet >> logs/cleanup.log 2>&1")
    print("\n# Windows 用计划任务，一条命令注册（时间可改）：")
    print(r'  powershell -ExecutionPolicy Bypass -File tools\schedule_cleanup.ps1 -Time 03:00')


def main(argv: list[str] | None = None) -> int:
    s = Settings.load()

    p = argparse.ArgumentParser(description="gitlab-qa 产物清理")
    p.add_argument("--days", type=int, default=s.cleanup_retention_days,
                   help=f"保留天数，默认 {s.cleanup_retention_days}")
    p.add_argument("--max-mb", type=int, default=s.cleanup_max_mb,
                   help=f"单目录体积上限 MB，0=不限制，默认 {s.cleanup_max_mb}")
    p.add_argument("--keep-latest", type=int, default=s.cleanup_keep_latest,
                   help=f"每个目录至少保留最新 K 个文件，默认 {s.cleanup_keep_latest}")
    p.add_argument("--targets", default=s.cleanup_targets,
                   help=f"清理范围，逗号分隔，默认 {s.cleanup_targets}")
    p.add_argument("--all", action="store_true",
                   help="忽略保留策略（history/ 与 .env 仍受保护）")
    p.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    p.add_argument("--json", default=None, help="把汇总写到指定 JSON 文件")
    p.add_argument("--quiet", action="store_true", help="只输出合计")
    p.add_argument("--print-cron", action="store_true",
                   help="打印 crontab / 计划任务注册命令后退出")
    args = p.parse_args(argv)

    if args.print_cron:
        print_cron()
        return 0

    available = build_targets()
    wanted = [t.strip().lower() for t in args.targets.split(",") if t.strip()]
    unknown = [t for t in wanted if t not in available]
    if unknown:
        print(f"[!] 未知的清理目标：{', '.join(unknown)}")
        print(f"    可用：{', '.join(available)}")
        return 2

    if args.days < 0 or args.max_mb < 0 or args.keep_latest < 0:
        print("[!] 保留参数不能为负数")
        return 2

    header = "预览（--dry-run，不删除）" if args.dry_run else "开始清理"
    if not args.quiet:
        print(f"=== {header} ===")
        print(f"保留策略：{args.days} 天 / 体积上限 {args.max_mb} MB / "
              f"每目录保留最新 {args.keep_latest} 个\n")

    total_removed = 0
    total_freed = 0
    total_errors: list[str] = []
    summary: dict = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": bool(args.dry_run),
        "days": args.days,
        "max_mb": args.max_mb,
        "keep_latest": args.keep_latest,
        "targets": [],
    }

    for name in wanted:
        target = available[name]
        plan = plan_target(target, args.days, args.max_mb,
                           args.keep_latest, wipe=args.all)
        removed, freed, errors = execute(plan, args.dry_run)

        total_removed += removed
        total_freed += freed
        total_errors.extend(errors)

        if not args.quiet:
            rel = target.path
            try:
                rel = target.path.relative_to(REPO)
            except ValueError:
                pass
            print(f"[{name}] {rel}")
            if plan.note:
                print(f"    策略：{plan.note}")
            print(f"    删除 {removed} 个 / {hsize(freed)}，"
                  f"保留 {len(plan.kept)} 个，失败 {len(errors)}")
            for err in errors[:5]:
                print(f"    [!] {err}")
            if len(errors) > 5:
                print(f"    [!] 另有 {len(errors) - 5} 条失败未显示")

        summary["targets"].append({
            "name": name,
            "path": str(target.path),
            "removed": removed,
            "freed_bytes": freed,
            "kept": len(plan.kept),
            "errors": errors,
            "note": plan.note,
        })

    summary.update({
        "removed": total_removed,
        "freed_bytes": total_freed,
        "freed_human": hsize(total_freed),
        "error_count": len(total_errors),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    print()
    print(f"合计：删除 {total_removed} 个文件，释放 {hsize(total_freed)}，"
          f"失败 {len(total_errors)}")
    if total_errors:
        print("提示：失败多为文件被占用（Allure 服务正在读）或安全策略拦截，"
              "关闭占用后重跑即可。")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        if not args.quiet:
            print(f"[ok] 汇总已写入 {out}")

    # 有失败就返回非 0：清理静默失效比删多了更麻烦
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
