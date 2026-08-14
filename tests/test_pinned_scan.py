from pathlib import Path


def test_scan_never_falls_back_to_all_chat_contacts():
    source = Path("web_app.py").read_text(encoding="utf-8")
    assert "for title, identity in task_core.userIDDict.items()" not in source
    assert '"message": f"仅识别到 {len(results)} 个置顶会话"' in source


def test_scan_waits_for_identity_and_frontend_keeps_name_only_contacts():
    backend = Path("web_app.py").read_text(encoding="utf-8")
    frontend = Path("web/app.js").read_text(encoding="utf-8")
    assert "identity_deadline = time.monotonic() + 2" in backend
    assert "item.uniqueId||item.shortId||item.nickname||item.remark" in frontend
