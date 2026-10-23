"""业务层 fixture。

根 conftest 负责环境与基础设施，这里只放「测试用例需要的东西」。
判断标准同样是一句：换个被测系统，这行还成立吗。
"""

from __future__ import annotations

import pytest

from gqa.models import Project


@pytest.fixture
def a_project(data_factory) -> Project:
    """一个已创建、用完即删的项目。"""
    data = data_factory.project()
    return Project.model_validate(data)


@pytest.fixture
def an_open_issue(data_factory, a_project: Project) -> dict:
    return data_factory.issue(a_project.id, description="由自动化创建")


@pytest.fixture
def perf_thresholds(settings):
    """性能门禁阈值集中在一处，CI 里用命令行覆盖。"""
    return {
        "p95_ms": settings.perf_p95_ms,
        "error_rate": settings.perf_error_rate,
        "concurrency": settings.perf_concurrency,
        "samples": settings.perf_samples,
    }
