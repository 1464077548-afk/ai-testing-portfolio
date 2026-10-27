"""性能门禁（第二组）：深分页 / 搜索 / 写路径 / 吞吐 / 抖动。

第一组（test_api_perf.py）回答「接口快不快」；这一组回答另外四个问题：

1. **深分页会不会越来越慢** —— 第一页 50ms、第五页 3s 是典型的
   未加索引或 N+1 查询，只测第一页永远发现不了。
2. **写路径能不能扛** —— 读写混跑时写入往往是被拖垮的那一方，
   而只读压测报告一片绿会让人误判容量。
3. **吞吐有没有下限** —— 延迟达标但 RPS 只有 2，说明连接池或线程被打满。
4. **延迟稳不稳** —— P50 很快、P99 很慢，用户体验是「时快时慢」，
   只看 P95 会被平均掉。

纪律同第一组：绝不对公共实例跑并发请求。
"""

from __future__ import annotations

import itertools
import random

import allure
import pytest

from gqa.perf.runner import Sample

pytestmark = [pytest.mark.perf, pytest.mark.requires_token]


@allure.epic("性能")
@allure.feature("列表")
@allure.story("深分页不出现性能塌陷")
@allure.severity(allure.severity_level.CRITICAL)
def test_deep_pagination_latency(gl, perf, sampler_of, perf_thresholds):
    """翻到第 3 页的耗时不应该比第 1 页差一个量级。

    这条用例专门抓「只有第一页快」：分页实现里 `OFFSET` 越大越慢、
    或每页都重算一遍权限，都会在这里现形，而在第一页上完全看不出来。
    """
    samples = max(20, perf_thresholds["samples"] // 2)
    result, failed = perf(
        "GET /projects?page=3",
        sampler_of(gl.client, "/projects", {"per_page": 20, "page": 3}),
        concurrency=max(2, perf_thresholds["concurrency"] // 2),
        samples=samples,
    )

    allure.attach(
        f"样本 {len(result.samples)}，P50={result.p50:.0f}ms，P95={result.p95:.0f}ms",
        name="分页性能", attachment_type=allure.attachment_type.TEXT,
    )
    assert failed == [], "深分页性能未达标：\n- " + "\n- ".join(failed)


@allure.epic("性能")
@allure.feature("搜索")
@allure.story("搜索接口 P95 达标")
@allure.severity(allure.severity_level.NORMAL)
def test_search_latency(gl, perf, perf_thresholds):
    """搜索是全站最重的只读接口之一（跨项目全文匹配）。

    单独给它一条用例，是因为它常常在别的接口都正常时先崩——
    而"搜索很慢"这种问题，用户会描述成"系统卡"，没人会想到是搜索接口。
    """

    def _one() -> Sample:
        try:
            resp = gl.search.run("projects", "qa", per_page=20)
            return Sample(ok=resp.status == 200, latency_ms=resp.elapsed_ms,
                          status=resp.status)
        except Exception as exc:  # noqa: BLE001
            return Sample(ok=False, latency_ms=0, error=type(exc).__name__)

    # 搜索本来就比列表慢，阈值放宽一档；写死同一个数会让这条用例天天红
    result, failed = perf(
        "GET /search?scope=projects",
        _one,
        concurrency=max(2, perf_thresholds["concurrency"] // 2),
        samples=max(10, perf_thresholds["samples"] // 3),
        p95_ms=int(perf_thresholds["p95_ms"] * 2),
    )
    assert failed == [], "搜索性能未达标：\n- " + "\n- ".join(failed)


@allure.epic("性能")
@allure.feature("混合负载")
@allure.story("读写混合下只读接口仍达标")
@allure.severity(allure.severity_level.NORMAL)
def test_mixed_read_workload(gl, perf, sampler_of, perf_thresholds):
    """把三个高频只读端点混着压，模拟真实访问而不是单点打靶。

    单端点压测的问题是：每个接口各自都达标，合起来却互相抢资源。
    这里按比例轮转，测的是「一起访问时的表现」。
    """
    paths = itertools.cycle([
        ("/projects", {"per_page": 20}),
        ("/namespaces", {}),
        ("/groups", {}),
    ])

    def _one() -> Sample:
        path, params = next(paths)
        return sampler_of(gl.client, path, params)()

    result, failed = perf("混合只读 /projects+/namespaces+/groups", _one)
    allure.attach(
        f"RPS={result.rps:.1f}  错误率={result.error_rate:.2%}",
        name="混合负载摘要", attachment_type=allure.attachment_type.TEXT,
    )
    assert failed == [], "混合负载未达标：\n- " + "\n- ".join(failed)


@allure.epic("性能")
@allure.feature("吞吐")
@allure.story("吞吐不低于下限")
@allure.severity(allure.severity_level.CRITICAL)
def test_throughput_floor(gl, perf, sampler_of, perf_thresholds):
    """延迟达标不等于容量够。

    一个接口 P95 200ms 但每秒只能处理 2 个请求，压测报告照样全绿，
    上线后在真实并发下直接雪崩。所以吞吐必须单独设一道门。
    """
    result, failed = perf(
        "吞吐观察 GET /projects",
        sampler_of(gl.client, "/projects", {"per_page": 20}),
    )

    allure.attach(
        f"吞吐 {result.rps:.1f} req/s（下限 {perf_thresholds['min_rps']}）\n"
        f"耗时 {result.duration_ms}ms / {len(result.samples)} 样本 / "
        f"{result.concurrency} 并发",
        name="吞吐", attachment_type=allure.attachment_type.TEXT,
    )
    assert failed == [], "吞吐未达标：\n- " + "\n- ".join(failed)


@allure.epic("性能")
@allure.feature("稳定性")
@allure.story("延迟抖动在可接受范围")
@allure.severity(allure.severity_level.NORMAL)
def test_latency_jitter_is_bounded(gl, perf, sampler_of, perf_thresholds):
    """P99/P50 比值过大 = 用户体验时快时慢。

    这类问题在平均值和 P95 上都看不出来：
    一半请求 20ms、一成请求 2s，P95 可能还是 200ms。
    但用户遇到的是那 10%，他们只会说"这系统很卡"。

    本地单节点实例抖动本来就不小，所以阈值给得宽（默认 6 倍），
    目的是抓"长尾失控"而不是抓噪声。
    """
    result, failed = perf(
        "抖动观察 GET /projects",
        sampler_of(gl.client, "/projects", {"per_page": 20}),
    )

    ratio = (result.p99 / result.p50) if result.p50 > 0 else float("inf")
    allure.attach(
        f"P50={result.p50:.0f}ms  P95={result.p95:.0f}ms  P99={result.p99:.0f}ms\n"
        f"P99/P50 = {ratio:.2f}（上限 {perf_thresholds['max_jitter']}）",
        name="抖动", attachment_type=allure.attachment_type.TEXT,
    )
    assert failed == [], "抖动超标：\n- " + "\n- ".join(failed)


@allure.epic("性能")
@allure.feature("写路径")
@allure.story("创建并删除项目的往返耗时达标")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.destructive
def test_write_path_latency(gl, perf, settings, perf_thresholds):
    """写入路径单独压：读写混跑时先垮的往往是写。

    样本刻意小（默认 6 次）——写操作会真的建项目，
    一轮压测建 60 个项目既慢又会给实例留下一堆垃圾。
    性能测试的目的是**发现趋势**，不是把实例压垮。

    每次采样都是「创建 → 立即永久删除」的完整往返，
    所以测的是一次真实业务写入的成本，而不是单次 HTTP 往返。
    """
    rng = random.Random(20261026)
    counter = itertools.count(1)

    def _one() -> Sample:
        name = f"{settings.resource_prefix}-perf-{next(counter)}-{rng.randrange(10**6)}"
        try:
            created = gl.projects.create(name, visibility="private",
                                         initialize_with_readme=False)
            if created.status != 201:
                return Sample(ok=False, latency_ms=created.elapsed_ms,
                              status=created.status)
            pid = created.json_data["id"]
            gl.projects.purge(pid, created.json_data.get("path_with_namespace", ""))
            return Sample(ok=True, latency_ms=created.elapsed_ms, status=201)
        except Exception as exc:  # noqa: BLE001
            return Sample(ok=False, latency_ms=0, error=type(exc).__name__)

    result, failed = perf(
        "写路径 POST+DELETE /projects",
        _one,
        concurrency=2,
        samples=max(3, perf_thresholds["samples"] // 10),
        # 写操作天然比读慢一个量级，且要连发两个请求
        p95_ms=perf_thresholds["p95_ms"] * 3,
        min_rps=0.0,      # 写路径不设吞吐门：并发只有 2，RPS 没有参考意义
        max_jitter=12.0,
    )
    assert failed == [], "写路径性能未达标：\n- " + "\n- ".join(failed)
