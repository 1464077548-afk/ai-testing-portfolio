"""框架自检：不依赖任何 GitLab 实例，永远可跑。

这一层的意义是「框架本身的正确性」，和被测系统无关：
百分位算法算错了、阈值判定写反了、契约校验漏了——这些错误
如果只能靠跑真实环境才能发现，那排查成本会非常高。
"""

from __future__ import annotations

import pytest

from gqa import contracts
from gqa.perf.runner import PerfResult, Sample, verdict
from gqa.settings import Settings


class TestPercentile:
    '''
    测试百分位数计算是否正确。
    '''
    def test_p95_of_sequential_data(self):
        r = PerfResult(name="t")
        r.samples = [Sample(ok=True, latency_ms=i) for i in range(1, 101)]
        assert r.p50 == pytest.approx(50.5, abs=0.6)   #pytest.approx 是 pytest 里专门用来做近似相等断言的工具，主要解决浮点数精度问题。
        assert r.p95 == pytest.approx(95.05, abs=0.6)   #abs:绝对误差；rel:相对误差;nan_ok:是否允许NaN 等于自身，默认 False
        assert r.p99 == pytest.approx(99.01, abs=0.6)

    def test_empty_result_does_not_crash(self):
        r = PerfResult(name="empty")
        assert r.p95 == 0.0
        assert r.error_rate == 1.0
        ok, reasons = verdict(r, 1000, 0.02)
        assert not ok
        assert "没有" in reasons[0]

    def test_error_rate_counts_failures(self):
        r = PerfResult(name="t")
        r.samples = [Sample(ok=True, latency_ms=10)] * 8 + [Sample(ok=False, latency_ms=10)] * 2
        assert r.error_rate == pytest.approx(0.2)

    def test_verdict_flags_p95_breach(self):
        r = PerfResult(name="t")
        r.samples = [Sample(ok=True, latency_ms=2000)] * 10
        ok, reasons = verdict(r, 1500, 0.02)
        assert not ok
        assert any("P95" in x for x in reasons)

    def test_verdict_flags_error_rate(self):
        r = PerfResult(name="t")
        r.samples = [Sample(ok=False, latency_ms=10)] * 5 + [Sample(ok=True, latency_ms=10)] * 5
        ok, reasons = verdict(r, 99999, 0.02)
        assert not ok
        assert any("错误率" in x for x in reasons)

    def test_rps_calculation(self):
        r = PerfResult(name="t", duration_ms=1000)
        r.samples = [Sample(ok=True, latency_ms=10)] * 50
        assert r.rps == pytest.approx(50.0)


class TestContracts:
    '''
    测试契约校验是否正确。
    '''
    def test_valid_project_passes(self):
        instance = {
            "id": 1, "name": "demo", "path": "demo",
            "visibility": "private", "created_at": "2026-01-01T00:00:00.000Z",
        }
        assert contracts.collect_errors(instance, contracts.PROJECT) == []
    #类型漂移
    def test_string_id_is_caught(self):
        """id 从 int 变 str 必须被检出——这是最致命的一类漂移。"""
        instance = {"id": "1", "name": "demo", "path": "demo"}
        errors = contracts.collect_errors(instance, contracts.PROJECT)
        assert errors and any("id" in e for e in errors)

    def test_bad_visibility_is_caught(self):
        instance = {"id": 1, "name": "demo", "path": "demo", "visibility": "secret"}
        errors = contracts.collect_errors(instance, contracts.PROJECT)
        assert errors and any("visibility" in e for e in errors)
    #缺少必填字段应该报错
    def test_missing_required_is_caught(self):
        errors = contracts.collect_errors({"id": 1}, contracts.PROJECT)
        assert any("name" in e for e in errors)

    # 新增字段不应该报错（开放契约）
    def test_extra_field_allowed(self):
        instance = {"id": 1, "name": "demo", "path": "demo", "extra": "ok"}
        errors = contracts.collect_errors(instance, contracts.PROJECT)
        assert not errors


class TestSettings:
    '''
    测试 Settings 类的描述方法。
    '''
    def test_describe_hides_token(self, monkeypatch):
        s = Settings(
            base_url="http://localhost:8929", token="glpat-xxxxxxxxxxxxxxxxxxxx",
            env="local", ssh_port=2222, admin_user="root", admin_password="x",
            timeout=20, perf_concurrency=8, perf_samples=60,
            perf_p95_ms=1500, perf_error_rate=0.02,
        )
        text = s.describe()
        assert "abcdefghijklmnop" not in text, "描述信息里不能出现完整 token"
        assert "glpat" in text
    #占位符 token 不会被误判为有效凭证：token 字段故意填入 <your-token> 这种典型的占位符格式（类似 .env.example 里常见的写法）。
    def test_has_token_detects_placeholder(self):
        s = Settings(base_url="http://x", token="<your-token>", env="local",
                     ssh_port=2222, admin_user="root", admin_password="",
                     timeout=20, perf_concurrency=8, perf_samples=60,
                     perf_p95_ms=1500, perf_error_rate=0.02)
        assert not s.has_token, "占位符不应被识别为有效 token"

    def test_api_url_is_derived(self):
        s = Settings(base_url="http://localhost:8929/", token="", env="local",
                     ssh_port=2222, admin_user="root", admin_password="",
                     timeout=20, perf_concurrency=8, perf_samples=60,
                     perf_p95_ms=1500, perf_error_rate=0.02)
        assert s.api_url == "http://localhost:8929/api/v4"


class TestAllureEnvEncoding:
    """Allure 按 ISO-8859-1 读 environment.properties，中文必须转义。

    这个坑的糟糕之处在于：直接写 UTF-8 不会报错，报告照样生成，
    只有打开 Environment 区块才会看到 `æªéç½®` 这种乱码。
    所以必须靠自检用例锁住，不能靠人记得。
    """

    def test_chinese_is_escaped_to_unicode(self):
        from gqa.allure_meta import escape_properties

        assert escape_properties("未配置") == "\\u672a\\u914d\\u7f6e"

    def test_ascii_passes_through(self):
        from gqa.allure_meta import escape_properties

        assert escape_properties("local") == "local"
        assert escape_properties("http://localhost:8929") == "http://localhost:8929"

    def test_roundtrip(self):
        from gqa.allure_meta import escape_properties, unescape_properties

        for text in ["未配置（需 Token 的用例将 skip）", "plain", "a\\b", "行1\n行2"]:
            assert unescape_properties(escape_properties(text)) == text

    def test_written_file_is_pure_ascii(self, tmp_path):
        """生成的文件必须能用 ascii 解码，否则说明还有字符没转义。"""
        from gqa.allure_meta import write_properties

        p = tmp_path / "environment.properties"
        write_properties(p, {"GitLab.Token": "未配置", "Environment": "local"})

        raw = p.read_bytes()
        raw.decode("ascii")            # 解不开就说明漏了转义
        assert b"\\u672a" in raw       # "未" 的转义形式
        assert "local" in raw.decode("ascii")
