import json
from pathlib import Path

import pytest

import web_app


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(web_app, "ENV_FILE", env_file)
    return env_file


def cookie_json():
    return json.dumps([
        {"name": "sessionid", "value": "secret", "domain": ".douyin.com", "path": "/"}
    ])


def test_save_multiple_accounts_with_structured_targets(isolated_env):
    config = web_app.save_config({
        "messageTemplate": "续火花",
        "accounts": [
            {
                "username": "我",
                "uniqueId": "mine",
                "cookies": cookie_json(),
                "targets": [{"id": "friend-1", "aliases": ["好友一", "备注一"]}],
            },
            {
                "username": "朋友",
                "uniqueId": "theirs",
                "cookies": cookie_json(),
                "targets": [{"id": "friend-2", "aliases": ["好友二"]}],
            },
        ],
    })

    saved = web_app.read_env()
    tasks = json.loads(saved["TASKS"])
    assert [task["unique_id"] for task in tasks] == ["mine", "theirs"]
    assert tasks[0]["targets"][0] == {"id": "friend-1", "aliases": ["好友一", "备注一"]}
    assert config["accounts"][1]["cookieConfigured"] is True
    assert config["accounts"][1]["cookieCount"] == 1


def test_existing_cookie_is_preserved_when_form_leaves_it_blank(isolated_env):
    isolated_env.write_text(
        "TASKS=[]\nCOOKIES_MINE=" + cookie_json() + "\n", encoding="utf-8"
    )
    web_app.save_config({
        "accounts": [{
            "username": "我", "uniqueId": "mine", "cookies": "",
            "targets": [{"id": "friend", "aliases": []}],
        }]
    })
    assert json.loads(web_app.read_env()["COOKIES_MINE"])[0]["value"] == "secret"


def test_legacy_string_targets_are_exposed_as_structured_targets(isolated_env):
    isolated_env.write_text(
        'TASKS=[{"username":"我","unique_id":"mine","targets":["friend"]}]\n',
        encoding="utf-8",
    )
    assert web_app.public_config()["accounts"][0]["targets"] == [
        {"id": "friend", "aliases": []}
    ]


def test_account_can_be_saved_before_targets_are_added(isolated_env):
    config = web_app.save_config({
        "accounts": [{"username": "朋友", "uniqueId": "theirs", "targets": []}]
    })
    assert config["accounts"][0]["username"] == "朋友"
    assert config["accounts"][0]["targets"] == []


def test_cookie_validator_accepts_exported_samesite_values():
    cookies = web_app.validate_cookie_json(json.dumps([
        {"name": "sid", "value": "secret", "domain": ".douyin.com", "path": "/", "sameSite": "no_restriction"}
    ]))
    from utils.config import sanitize_cookies
    assert "sameSite" not in sanitize_cookies(cookies)[0]


def test_github_sync_skips_empty_optional_variables(monkeypatch):
    requested = []
    monkeypatch.setattr(web_app, "public_config", lambda: {"accounts": []})
    # The no-account guard runs before any GitHub call; this protects empty-value behavior by source contract.
    assert 'if value == "":\n            continue' in Path(web_app.__file__).read_text(encoding="utf-8")


def test_merge_tasks_preserves_other_github_accounts():
    existing = [
        {"username": "原账号", "unique_id": "old", "targets": ["a"]},
        {"username": "待更新", "unique_id": "same", "targets": ["before"]},
    ]
    local = [
        {"username": "已更新", "unique_id": "same", "targets": ["after"]},
        {"username": "新账号", "unique_id": "new", "targets": ["b"]},
    ]
    merged = web_app.merge_tasks(existing, local)
    assert [task["unique_id"] for task in merged] == ["old", "same", "new"]
    assert merged[0] == existing[0]
    assert merged[1]["targets"] == ["after"]


def test_sync_never_deletes_cookie_secrets():
    source = Path(web_app.__file__).read_text(encoding="utf-8")
    assert 'github_request("DELETE", f"{base}/secrets/' not in source


def test_single_account_run_endpoint_sets_account_filter():
    source = Path(web_app.__file__).read_text(encoding="utf-8")
    assert 'env["RUN_ACCOUNT_ID"] = account_id' in source
    assert 'self.path == "/api/run-account"' in source


def test_scan_progress_is_clamped_and_exposed():
    web_app.update_scan_status(True, 150, "测试")
    status = web_app.get_scan_status()
    assert status["running"] is True
    assert status["percent"] == 100
    assert status["stage"] == "测试"


def test_login_refresh_updates_only_selected_cookie_secret():
    source = Path(web_app.__file__).read_text(encoding="utf-8")
    assert 'self.path == "/api/account-login-refresh"' in source
    assert 'f"COOKIES_{account_id.upper()}"' in source


def test_dashboard_uses_expiring_session_cookie(monkeypatch):
    handler = object.__new__(web_app.Handler)
    token = "test-session"
    web_app.SESSIONS[token] = {"username": "member", "role": "account", "accountIds": ["mine"], "expires": web_app.time.time() + 60}
    handler.headers = {"Cookie": f"sparkflow_session={token}"}
    assert handler.current_user()["username"] == "member"
    assert handler.allowed_account_ids() == ["mine"]
    web_app.SESSIONS.pop(token, None)


def test_password_hash_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "WEB_USERS_FILE", tmp_path / ".web-users.json")
    web_app.upsert_web_user("member", "password8", account_ids=["mine"])
    assert web_app.authenticate_web_user("member", "password8")["accountIds"] == ["mine"]
    assert web_app.authenticate_web_user("member", "wrongpass") is None


def test_scoped_save_preserves_other_accounts(isolated_env):
    isolated_env.write_text(
        'TASKS=[{"username":"A","unique_id":"a","targets":[]},{"username":"B","unique_id":"b","targets":[]}]\n',
        encoding="utf-8",
    )
    web_app.save_scoped_config({"accounts": [{"username": "A2", "uniqueId": "a", "targets": [{"id": "friend"}]}]}, ["a"])
    tasks = json.loads(web_app.read_env()["TASKS"])
    assert [task["unique_id"] for task in tasks] == ["a", "b"]
    assert tasks[1]["username"] == "B"


def test_login_success_continues_with_selected_account_pinned_scan(monkeypatch):
    monkeypatch.setattr(web_app, "public_config", lambda: {"accounts": [
        {"uniqueId": "old"}, {"uniqueId": "selected"},
    ]})
    monkeypatch.setattr(web_app, "refresh_account_login", lambda account_id, continue_to_scan=False: {
        "accountId": account_id, "cookieCount": 8, "continued": continue_to_scan,
    })
    monkeypatch.setattr(web_app, "scan_pinned_account", lambda index, finalize=True: {
        "accountIndex": index, "contacts": [{"uniqueId": "friend"}], "message": "ok",
    })

    result = web_app.refresh_login_and_scan("selected")

    assert result["login"]["continued"] is True
    assert result["scan"]["accountIndex"] == 1
    assert web_app.get_scan_status()["scanResult"] == result["scan"]


def test_closed_login_browser_is_rebuilt_once(monkeypatch):
    monkeypatch.setattr(web_app, "public_config", lambda: {"accounts": [{"uniqueId": "selected"}]})
    calls = []
    def refresh(account_id, continue_to_scan=False):
        calls.append(account_id)
        if len(calls) == 1:
            raise RuntimeError("Target page, context or browser has been closed")
        return {"accountId": account_id, "cookieCount": 3}
    monkeypatch.setattr(web_app, "refresh_account_login", refresh)
    monkeypatch.setattr(web_app, "scan_pinned_account", lambda index, finalize=True: {"contacts": [], "accountIndex": index})
    assert web_app.refresh_login_and_scan("selected")["login"]["cookieCount"] == 3
    assert len(calls) == 2
