def test_scope_once(client):
    assert client["n"] == 1   # 第二次调用不会再造


def test_scope_second(client):
    assert client["n"] == 1   # 第二次调用不会再造