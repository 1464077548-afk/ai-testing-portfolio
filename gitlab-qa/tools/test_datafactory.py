from gqa.api.client import GitLabError

def test_create_issue_with_data_factory(data_factory):
    """
    连续创建 3 个 Issue，用例结束后 data_factory 夹具会自动清理
    """
    a_project = data_factory.project()
    project_id = a_project["id"]
    assert a_project is not None
    # 1. 循环创建 3 次（创建即登记）
    created_issues = []
    for i in range(3):
        issue = data_factory.issue(
            project_id=project_id,
            title=f"测试数据-实例-{i}"
        )
        created_issues.append(issue)

    # 2. 执行你的业务断言
    assert len(created_issues) == 3
    for issue in created_issues:
        assert issue["title"].startswith("测试数据-实例-")
    
    # 3. 用例在这里结束，data_factory 自动触发 yield 后的 cleanup()

