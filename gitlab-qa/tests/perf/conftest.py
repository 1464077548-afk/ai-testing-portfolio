"""性能层共用夹具。

把「采样 → 贴证据 → 判门禁」三步收进一个夹具，是因为这三步只要抄第二次就会跑偏：
有人忘了贴明细（报告里只剩一句"超时了"），有人把阈值写死（CI 改不动），
还有人只判 P95 漏掉错误率——于是限流风暴也能全绿。

收在一处之后，用例只负责表达「这次要压什么、容忍到什么程度」。
"""

from __future__ import annotations

import statistics

import allure
import pytest

from gqa.perf.runner import Sample, run_load


@pytest.fixture(scope="session", autouse=True)
def perf_env_guard(gl, settings):
    """跑性能之前先给**被测环境**量一次体温。

    为什么必须有这一步（2026-10 实测的教训）：
    门禁连续 5 轮报 P95 3~4s 超阈值，查下来根因是
    **gitlab-ce 容器内存 5.09GiB / 6GiB = 84.8%**——逼近上限后
    GC / swap 压力把延迟整体抬了 5 倍，而 CPU 只有 8%。
    接口一行没改，全红的锅却在环境。

    如果不管这个，性能门禁会在环境退化时稳定误报，
    而误报的最终结局只有一个：所有人学会无视红灯。
    所以这里先测基线（单线程、只读、不并发），
    基线本身就慢得过分离谱时，**跳过**性能用例并写明原因——
    skip 不是 pass，它说的是"这台机器现在测不出可信结论"。
    """
    # 这里直接读 settings 而不是 perf_thresholds：后者是 function 级的，
    # session 级夹具拿它会被 pytest 拒绝（值本身没差，都是同一份配置）
    budget = float(settings.perf_env_p50_ms or settings.perf_p95_ms / 2)
    threshold = settings.perf_p95_ms

    # 单线程 12 次，取中位数：这是"没有并发压力时"的延迟下限
    samples: list[float] = []
    for _ in range(12):
        resp = gl.client.get("/projects", params={"per_page": 20},
                             expect=(200, 401, 403, 429))
        samples.append(float(resp.elapsed_ms))
    baseline = statistics.median(samples)

    allure.attach(
        f"基线（单线程 GET /projects，中位数）：{baseline:.0f} ms\n"
        f"环境预算（阈值的一半）：{budget:.0f} ms\n"
        f"判定：{'健康' if baseline <= budget else '不健康'}",
        name="性能环境体检", attachment_type=allure.attachment_type.TEXT,
    )

    if baseline > budget:
        pytest.skip(
            f"被测环境不健康，性能数据不可信：单线程基线 P50={baseline:.0f}ms，"
            f"已超过环境预算 {budget:.0f}ms（PERF_ENV_P50_MS；"
            f"健康实例上这条请求通常几十到两百毫秒）。"
            f"参考 P95 阈值 {threshold}ms。"
            "常见原因是容器内存吃满（GitLab CE 建议 ≥8GiB）或宿主机负载过高——"
            "这不是接口变慢，先恢复环境再跑性能套件。"
        )
    return {"baseline_p50_ms": baseline, "budget_ms": budget}


@pytest.fixture
def perf(request: pytest.FixtureRequest, perf_thresholds: dict):
    """跑一轮负载，返回 (result, 失败原因列表)。

    失败原因为空即通过。阈值默认取 perf_thresholds，可用关键字逐条覆盖
    （写路径本来就比读路径慢，共用一套阈值会导致要么写路径永远红、
    要么读路径形同虚设）。
    """

    def _run(name: str, fn, concurrency: int | None = None,
             samples: int | None = None, warmup: int = 2, **limits) -> tuple:
        result = run_load(
            name,
            fn,
            concurrency=concurrency or perf_thresholds["concurrency"],
            samples=samples or perf_thresholds["samples"],
            warmup=warmup,
        )
        # 挂到 item 上：conftest 的失败钩子会把它贴进报告
        request.node._perf_result = result

        import allure
        allure.attach(result.markdown(), name="性能摘要",
                      attachment_type=allure.attachment_type.TEXT)
        allure.attach(
            "\n".join(f"{s.latency_ms},{s.status},{s.error}" for s in result.samples),
            name="原始采样(ms,status,error)",
            attachment_type=allure.attachment_type.CSV,
        )

        failed: list[str] = []
        if not result.samples:
            return result, ["没有任何样本，本次性能测试无效"]

        p95 = limits.get("p95_ms", perf_thresholds["p95_ms"])
        err = limits.get("error_rate", perf_thresholds["error_rate"])
        min_rps = limits.get("min_rps", perf_thresholds["min_rps"])
        max_jitter = limits.get("max_jitter", perf_thresholds["max_jitter"])

        if result.p95 > p95:
            failed.append(f"P95 {result.p95:.0f}ms 超过阈值 {p95}ms")
        if result.error_rate > err:
            failed.append(f"错误率 {result.error_rate:.2%} 超过阈值 {err:.2%}")
        if result.rps < min_rps:
            failed.append(f"吞吐 {result.rps:.1f} req/s 低于下限 {min_rps} req/s")
        # 抖动：P50 很快但 P99 很慢，说明偶发长尾（GC / 锁 / 慢查询），
        # 只看 P95 会把它平均掉
        if result.p50 > 0 and (result.p99 / result.p50) > max_jitter:
            failed.append(
                f"抖动过大：P99/P50 = {result.p99 / result.p50:.1f} 超过 {max_jitter}"
                f"（P50={result.p50:.0f}ms, P99={result.p99:.0f}ms）"
            )
        return result, failed

    return _run


@pytest.fixture
def sampler_of():
    """构造采样函数：每次调用发一次请求，记录耗时与成败。"""

    def _make(client, path: str, params: dict | None = None,
              ok_status: tuple[int, ...] = (200,)):
        def _one() -> Sample:
            try:
                resp = client.get(path, params=params, expect=(200, 401, 403, 404, 429))
                return Sample(ok=resp.status in ok_status,
                              latency_ms=resp.elapsed_ms, status=resp.status)
            except Exception as exc:  # noqa: BLE001
                return Sample(ok=False, latency_ms=0, error=type(exc).__name__)
        return _one

    return _make
