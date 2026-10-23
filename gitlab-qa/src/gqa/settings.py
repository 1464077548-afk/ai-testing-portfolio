"""配置加载：环境变量 > .env 文件 > 默认值。

企业级框架里配置的第一原则：**缺配置要早失败、要说得清缺什么**。
不要等到第 40 条用例才因为 URL 是 None 而报一个看不懂的错。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # 可选依赖，缺失时只降级不报错
    load_dotenv = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    base_url: str
    token: str
    env: str
    ssh_port: int
    admin_user: str
    admin_password: str
    timeout: float
    perf_concurrency: int
    perf_samples: int
    perf_p95_ms: int
    perf_error_rate: float
    keep_failed_artifacts: bool = True
    resource_prefix: str = field(default="")
    # 第二个身份（普通用户 / 只读 Token）。越权类断言必须有它才成立：
    # 用管理员去访问管理员端点永远 200，断言恒真，等于没测。
    member_token: str = field(default="")

    # ---- 产物清理（tools/cleanup.py 用；与测试执行无关，但同一个 .env） ----
    # 保留天数：早于 N 天的截图 / trace / 日志 / 旧结果才可删
    cleanup_retention_days: int = 7
    # 目录体积上限（MB）：超过时从最旧的开始删到阈值内；0 = 不限制
    cleanup_max_mb: int = 1024
    # 每个目录至少保留最新 K 个文件，防止把目录清空（0 = 不保留）
    cleanup_keep_latest: int = 3
    # 清理范围，逗号分隔：artifacts,results,report,logs,cache
    cleanup_targets: str = "artifacts,results,report,logs,cache"

    @property
    def has_token(self) -> bool:
        return bool(self.token) and not self.token.startswith("<")

    @property
    def has_member_token(self) -> bool:
        return bool(self.member_token) and not self.member_token.startswith("<")

    @property
    def api_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v4"

    @classmethod
    def load(cls, env: str | None = None, base_url: str | None = None,
             token: str | None = None) -> Settings:
        if load_dotenv is not None:
            # 只读项目根的 .env。工作目录可能不是项目根，
            # 靠 cwd 去猜会变成「换个目录执行就读不到配置」。
            load_dotenv(PROJECT_ROOT / ".env", override=False)

        resolved_env = env or os.getenv("GITLAB_ENV", "local")
        resolved_url = base_url or os.getenv("GITLAB_BASE_URL", "http://localhost:8929")
        resolved_token = token or os.getenv("GITLAB_TOKEN", "")

        return cls(
            base_url=resolved_url.rstrip("/"),
            token=resolved_token,
            env=resolved_env,
            ssh_port=int(os.getenv("GITLAB_SSH_PORT", "2222")),
            admin_user=os.getenv("GITLAB_ADMIN_USER", "root"),
            admin_password=os.getenv("GITLAB_ROOT_PASSWORD", ""),
            timeout=float(os.getenv("GITLAB_TIMEOUT", "20")),
            perf_concurrency=int(os.getenv("PERF_CONCURRENCY", "8")),
            perf_samples=int(os.getenv("PERF_SAMPLES", "60")),
            perf_p95_ms=int(os.getenv("PERF_P95_MS", "1500")),
            perf_error_rate=float(os.getenv("PERF_ERROR_RATE", "0.02")),
            resource_prefix=os.getenv("RESOURCE_PREFIX", "qa-auto"),
            member_token=os.getenv("GITLAB_MEMBER_TOKEN", ""),
            cleanup_retention_days=int(os.getenv("CLEANUP_RETENTION_DAYS", "7")),
            cleanup_max_mb=int(os.getenv("CLEANUP_MAX_MB", "1024")),
            cleanup_keep_latest=int(os.getenv("CLEANUP_KEEP_LATEST", "3")),
            cleanup_targets=os.getenv(
                "CLEANUP_TARGETS", "artifacts,results,report,logs,cache"
            ),
        )

    def describe(self) -> str:
        shown = f"{self.token[:6]}…{self.token[-4:]}" if self.has_token else "<未配置>"
        return (
            f"env={self.env}  url={self.base_url}  token={shown}  "
            f"timeout={self.timeout}s  perf: {self.perf_concurrency}并发/"
            f"{self.perf_samples}样本/P95<{self.perf_p95_ms}ms"
        )
