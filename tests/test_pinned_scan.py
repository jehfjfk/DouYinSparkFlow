from pathlib import Path


def test_scan_never_falls_back_to_all_chat_contacts():
    source = Path("web_app.py").read_text(encoding="utf-8")
    assert "for title, identity in task_core.userIDDict.items()" not in source
    assert '"message": f"仅识别到 {len(results)} 个置顶会话"' in source


def test_scan_waits_for_identity_and_frontend_keeps_name_only_contacts():
    backend = Path("web_app.py").read_text(encoding="utf-8")
    frontend = Path("web/app.js").read_text(encoding="utf-8")
    assert "identity_deadline = time.monotonic() + 0.8" in backend
    assert "item.uniqueId||item.shortId||item.nickname||item.remark" in frontend


def test_scan_traverses_virtualized_conversation_list_until_stable_bottom():
    source = Path("web_app.py").read_text(encoding="utf-8")
    assert "for scan_round in range(60)" in source
    assert "scroller.clientHeight * 0.8" in source
    assert "stable_rounds >= 3" in source
    assert "seen_rows" in source
    assert 'if scroll_state["beforeBottom"]:' in source


def test_scan_starts_at_top_and_deduplicates_rows_and_results():
    source = Path("web_app.py").read_text(encoding="utf-8")
    assert "scroller.scrollTop = 0" in source
    assert "data-conversation-id" in source
    assert "result_keys = set()" in source
    assert "result_key = unique_id or short_id or title" in source


def test_scan_excludes_the_current_account_from_contacts():
    source = Path("web_app.py").read_text(encoding="utf-8")
    assert 'account_identity = task_core.norm(account["uniqueId"])' in source
    assert "task_core.norm(short_id), task_core.norm(unique_id)" in source


def test_pin_detection_does_not_use_broad_parent_or_pin_substrings():
    source = Path("web_app.py").read_text(encoding="utf-8")
    pin_block = source.split("def is_pinned_item", 1)[1].split("conversation_list.evaluate", 1)[0]
    assert "parentElement" not in pin_block
    assert '"pin" in marker' not in pin_block
    assert '"stick" in marker' not in pin_block
    assert '"isstickontop" in marker' in pin_block


def test_configured_scan_results_are_filtered_and_cleared():
    frontend = Path("web/app.js").read_text(encoding="utf-8")
    backend = Path("web_app.py").read_text(encoding="utf-8")
    assert "function pendingScanResult" in frontend
    assert 'api("/api/scan-result/clear"' in frontend
    assert 'self.path == "/api/scan-result/clear"' in backend
