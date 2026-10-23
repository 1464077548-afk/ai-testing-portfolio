"""api 子目录专属 fixture。

这一层存在本身就是分层 conftest 的证明：只在接口测试里需要的前置条件放这里，
根目录不会因为「某个子目录的特殊需求」而变脏。

当前没有独有的 fixture——`clean_library` 因为 contract/ 也要用，已经上提到
tests/conftest.py。这正是分层该有的演进方向：用到的地方变多，就往上提一层。
"""

from __future__ import annotations

from apilab.paging import page_count  # noqa: F401  子层引用示例

# 如果这里出现「只有 api/ 才需要」的 fixture，直接定义即可：
#
#   @pytest.fixture
#   def three_books_only(lab_server):
#       ...
#
# 同名的 fixture 会覆盖父层，因此「局部特化、全局兜底」是天然成立的。
