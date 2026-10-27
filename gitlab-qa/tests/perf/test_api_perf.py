"""性能门禁。

设计原则：性能测试是「门禁」不是「压测报告」，所以它必须有通过/失败的判定，
且判定阈值可从 CI 覆盖。没有阈值的性能测试等于没有测试。

另外一个纪律：这组用例带 requires_token，
**绝不能对公共实例（如 gitlab.com）跑并发请求**——那是对别人的服务发起攻击。
"""

from __future__ import annotations

import allure
import pytest

from gqa.perf.runner import Sample, run_load, verdict

pytestmark = [pytest.mark.perf, pytest.mark.requires_token]


def _sampler(client, path: str, params: dict | None = None):
    """构造一个采样函数：每次调用发一次请求并记一次耗时。"""
    def _one() -> Sample:
        try:
            resp = client.get(path, params=params, expect=(200, 401, 403, 429))
            ok = 200 <= resp.status < 300
            return Sample(ok=ok, latency_ms=resp.elapsed_ms, status=resp.status)
        except Exception as exc:  # noqa: BLE001
            return Sample(ok=False, latency_ms=0, error=type(exc).__name__)
    return _one


@allure.epic("性能")
@allure.feature("API 响应")
@allure.story("项目列表接口 P95 达标")
@allure.severity(allure.severity_level.CRITICAL)
def test_projects_list_latency(gl, settings, perf_thresholds, request):
    result = run_load(
        "GET /projects",
        _sampler(gl.client, "/projects", {"per_page": 20}),
        concurrency=perf_thresholds["concurrency"],
        samples=perf_thresholds["samples"],
        warmup=2,   # 冷启动的两三个请求会拉高 P95，不计入
    )
    # 挂到 item 上，conftest 的失败钩子会把它贴进报告
    request.node._perf_result = result

    passed, reasons = verdict(
        result, perf_thresholds["p95_ms"], perf_thresholds["error_rate"]
    )

    allure.attach(result.markdown(), name="性能摘要",
                  attachment_type=allure.attachment_type.TEXT)
    allure.attach(
        "\n".join(
            f"{s.latency_ms},{s.status},{s.error}" for s in result.samples
        ),
        name="原始采样(ms,status,error)",
        attachment_type=allure.attachment_type.CSV,
    )

    assert passed, "性能门禁未通过：\n- " + "\n- ".join(reasons)


@allure.epic("性能")
@allure.feature("API 响应")
@allure.story("项目详情接口 P95 达标")
@allure.severity(allure.severity_level.NORMAL)
def test_project_detail_latency(gl, a_project, perf_thresholds, request):
    result = run_load(
        f"GET /projects/{a_project.id}",
        _sampler(gl.client, f"/projects/{a_project.id}"),
        concurrency=max(2, perf_thresholds["concurrency"] // 2),
        samples=max(20, perf_thresholds["samples"] // 2),
        warmup=1,
    )
    request.node._perf_result = result
    passed, reasons = verdict(
        result, perf_thresholds["p95_ms"], perf_thresholds["error_rate"]
    )
    allure.attach(result.markdown(), name="性能摘要",
                  attachment_type=allure.attachment_type.TEXT)
    assert passed, "性能门禁未通过：\n- " + "\n- ".join(reasons)


@allure.epic("性能")
@allure.feature("API 稳定性")
@allure.story("并发下不出现限流风暴")
@allure.severity(allure.severity_level.NORMAL)
def test_no_rate_limit_storm(gl, perf_thresholds):
    """并发跑完之后，被限流的次数应该是 0。

    如果这里红了，说明测试的并发度超过了服务配额——
    要么调低并发，要么给测试账号提配额，不能靠重试硬扛。
    """
    run_load(
        "限流观察",
        _sampler(gl.client, "/projects", {"per_page": 20}),
        concurrency=perf_thresholds["concurrency"],
        samples=perf_thresholds["samples"],
    )
    allure.attach(
        f"429 次数：{gl.client.rate_limited}",
        name="限流统计",
        attachment_type=allure.attachment_type.TEXT,
    )
    assert gl.client.rate_limited == 0, (
        f"并发期间被限流 {gl.client.rate_limited} 次，请调低 PERF_CONCURRENCY"
    )
