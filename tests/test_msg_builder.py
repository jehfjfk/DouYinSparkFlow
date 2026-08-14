from core import msg_builder


def test_build_message_uses_account_template():
    assert msg_builder.build_message("账号专属消息") == "账号专属消息"


def test_build_message_expands_api_in_account_template(monkeypatch):
    monkeypatch.setattr(msg_builder, "request_hitokoto", lambda: "今日一言")
    assert msg_builder.build_message("续火花 [API]") == "续火花 今日一言"
