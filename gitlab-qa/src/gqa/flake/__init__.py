"""flake 治理：度量某一套用例跨 N 轮的稳定性，而不是只看单次结果。

设计要点（也是这套东西和普通「重跑」的区别）：
- **区分抖动与持续失败**：N 轮全红的用例是缺陷，不是抖动，混在一起会上错处理方法；
- **skip 不算通过**：缺配置导致跳过的用例，在报告里同样"没红"，必须单独归类；
- **隔离区收费**：进隔离区要写原因和期限，到期自动失效，用例回到门禁里继续跑。
"""

from gqa.flake.model import (  # noqa: F401
    BROKEN,
    FAILED,
    FLAKY,
    MISSING,
    NO_DATA,
    PASSED,
    SKIPPED,
    STABLE,
    VERDICT_LABEL,
    StabilityReport,
    TestStability,
    build_report,
    read_junit,
)
from gqa.flake.quarantine import (  # noqa: F401
    DEFAULT_MAX_SIZE,
    LIST_FILE,
    Quarantine,
    QuarantineEntry,
)

__all__ = [
    "BROKEN", "FAILED", "FLAKY", "MISSING", "NO_DATA", "PASSED", "SKIPPED", "STABLE",
    "StabilityReport", "TestStability", "VERDICT_LABEL", "build_report", "read_junit",
    "Quarantine", "QuarantineEntry", "LIST_FILE", "DEFAULT_MAX_SIZE",
]
