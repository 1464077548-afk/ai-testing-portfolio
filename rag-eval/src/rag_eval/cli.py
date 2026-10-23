"""命令行入口：run / validate / doctor。

设计原则：一条命令完成"加载 → 采集 → 打分 → 报告 → 退出码"，
退出码可直接作为 CI 门禁（0 通过 / 1 指标越界 / 2 配置或环境错误）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dataset import DatasetError, load_cases
from .runner import RunResult, run
from .targets import build_target

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ERROR = 2


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"配置不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _print_result(result: RunResult) -> None:
    meta = f"{result.target} v{result.target_version}"
    print(f"\n套件执行完成 · {meta} · {len(result.records)} 条用例 · {result.duration_ms}ms")
    print("-" * 64)
    print(f"{'指标':<22}{'均值':>8}{'阈值':>10}{'通过率':>9}{'样本':>6}")
    for name, item in result.summary.items():
        flag = "" if item["pass_rate"] >= 1.0 else "  <- 越界"
        print(
            f"{name:<22}{item['mean']:>8.3f}{item['threshold']:>10.3f}"
            f"{item['pass_rate'] * 100:>8.0f}%{int(item['n']):>6}{flag}"
        )
    if result.errors:
        print(f"\n采集失败 {result.errors} 条（未计入质量分，需单独排查）")

    print("-" * 64)
    if result.passed:
        print("结论：通过")
    else:
        print("结论：未通过")
        for line in result.failed_metrics:
            print(f"  - {line}")


def cmd_validate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    suite = _load_json(root / args.suite)
    paths = [root / p for p in suite["datasets"]]
    cases = load_cases(paths, suite.get("include_slices"))
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.slice] = counts.get(case.slice, 0) + 1
    print(f"校验通过：{len(cases)} 条用例")
    for name, count in sorted(counts.items()):
        print(f"  {name:<12}{count}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    suite = _load_json(root / args.suite)
    target_config = _load_json(root / suite["target"])

    k = int(suite.get("top_k", 3))
    cases = load_cases([root / p for p in suite["datasets"]], suite.get("include_slices"))
    target = build_target(target_config, root, top_k=k)

    result = run(
        cases,
        target,
        thresholds=suite.get("thresholds"),
        k=k,
        concurrency=int(suite.get("concurrency", 4)),
        retries=int(suite.get("retries", 1)),
    )
    _print_result(result)

    from .report import write_outputs

    out_dir = root / args.out
    json_path, html_path = write_outputs(result, out_dir)
    print(f"\n原始结果：{json_path}")
    print(f"HTML 报告：{html_path}")

    return EXIT_OK if result.passed else EXIT_FAILED


def cmd_doctor(args: argparse.Namespace) -> int:
    """接入自检：鉴权、引用、doc_id 映射三件事一次说清。"""
    from .doctor import diagnose

    root = Path(args.root).resolve()
    suite = _load_json(root / args.suite)
    target_config = _load_json(root / suite["target"])
    k = int(suite.get("top_k", 3))

    cases = load_cases([root / p for p in suite["datasets"]], suite.get("include_slices"))
    target = build_target(target_config, root, top_k=k)
    report = diagnose(target, cases, limit=args.limit)

    print(f"\n接入自检 · {report.target_name} v{report.target_version}")
    print("-" * 64)
    print(f"健康检查：{'通过' if report.healthy else '失败'} · {report.health_message}")

    if report.probes:
        print(f"\n{'用例':<28}{'引用片段':>8}{'doc_id 命中':>13}{'耗时':>8}")
        for probe in report.probes:
            hit = f"{len(probe.matched_doc_ids)}/{len(probe.expected_doc_ids) or '-'}"
            print(
                f"{probe.case_id:<28}{probe.context_count:>8}{hit:>13}"
                f"{probe.latency_ms:>7}ms"
            )
            if probe.error:
                print(f"    错误：{probe.error}")
            else:
                print(f"    答案：{probe.answer_preview}")
        print(f"\ndoc_id 覆盖率：{report.doc_id_coverage:.0%}")

    if report.warnings:
        print("\n警告")
        for item in report.warnings:
            print(f"  - {item}")
    if report.suggestions:
        print("\n建议")
        for item in report.suggestions:
            print(f"  - {item}")

    print("-" * 64)
    print("结论：可以开跑" if report.ready else "结论：先按上面建议修正，再开跑")
    return EXIT_OK if report.ready else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rag-eval", description="RAG 质量评测平台")
    parser.add_argument("--root", default=".", help="项目根目录，默认当前目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行一次评测")
    p_run.add_argument("--suite", default="configs/suites/smoke.json")
    p_run.add_argument("--out", default="out")
    p_run.set_defaults(func=cmd_run)

    p_val = sub.add_parser("validate", help="只校验评测集，不调用被测系统")
    p_val.add_argument("--suite", default="configs/suites/smoke.json")
    p_val.set_defaults(func=cmd_validate)

    p_doc = sub.add_parser("doctor", help="接入自检：鉴权、引用、doc_id 映射")
    p_doc.add_argument("--suite", default="configs/suites/dify_smoke.json")
    p_doc.add_argument("--limit", type=int, default=3, help="探测用例数")
    p_doc.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (DatasetError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
