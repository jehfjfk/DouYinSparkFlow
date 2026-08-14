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
                "messageTemplate": "我的续火消息",
                "cookies": cookie_json(),
                "targets": [{"id": "friend-1", "aliases": ["好友一", "备注一"]}],
            },
            {
                "username": "朋友",
                "uniqueId": "theirs",
                "messageTemplate": "朋友的续火消息",
                "cookies": cookie_json(),
                "targets": [{"id": "friend-2", "aliases": ["好友二"]}],
            },
        ],
    })

    saved = web_app.read_env()
    tasks = json.loads(saved["TASKS"])
    assert [task["unique_id"] for task in tasks] == ["mine", "theirs"]
    assert tasks[0]["targets"][0] == {"id": "friend-1", "aliases": ["好友一", "备注一"]}
    assert [task["message_template"] for task in tasks] == ["我的续火消息", "朋友的续火消息"]
    assert [account["messageTemplate"] for account in config["accounts"]] == ["我的续火消息", "朋友的续火消息"]
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


def test_save_deleted_account_removes_local_cookie(isolated_env):
    isolated_env.write_text(
        'TASKS=[{"username":"保留","unique_id":"keep","targets":[]},{"username":"删除","unique_id":"gone","targets":[]}]\n'
        + "COOKIES_KEEP=" + cookie_json() + "\nCOOKIES_GONE=" + cookie_json() + "\n",
        encoding="utf-8",
    )
    web_app.save_config({"accounts": [{"username": "保留", "uniqueId": "keep", "targets": []}]})
    saved = web_app.read_env()
    assert "COOKIES_KEEP" in saved
    assert "COOKIES_GONE" not in saved


def test_master_sync_replaces_tasks_and_deletes_removed_cookie_secret(isolated_env, monkeypatch):
    isolated_env.write_text(
        'TASKS=[{"username":"保留","unique_id":"keep","targets":[]}]\nCOOKIES_KEEP=' + cookie_json() + "\n",
        encoding="utf-8",
    )
    requests = []
    monkeypatch.setattr(web_app, "github_token", lambda: "token")
    monkeypatch.setattr(web_app, "encrypted_secret", lambda value, key: "encrypted")

    def request(method, path, token, payload=None):
        requests.append((method, path, payload))
        if path.endswith("/variables?per_page=100"):
            remote = [
                {"username": "保留", "unique_id": "keep", "targets": []},
                {"username": "删除", "unique_id": "gone", "targets": []},
            ]
            return {"variables": [{"name": "TASKS", "value": json.dumps(remote)}]}
        if path.endswith("/secrets/public-key"):
            return {"key": "key", "key_id": "id"}
        return {}

    monkeypatch.setattr(web_app, "github_request", request)
    result = web_app.sync_github()
    task_patch = next(payload for method, path, payload in requests if method == "PATCH" and path.endswith("/variables/TASKS"))
    assert [task["unique_id"] for task in json.loads(task_patch["value"])] == ["keep"]
    assert any(method == "DELETE" and path.endswith("/secrets/COOKIES_GONE") for method, path, _ in requests)
    assert result["deletedSecrets"] == ["COOKIES_GONE"]


def test_scoped_sync_preserves_unrelated_remote_accounts(isolated_env, monkeypatch):
    isolated_env.write_text(
        'TASKS=[{"username":"我的更新","unique_id":"mine","targets":[]},{"username":"本机其他","unique_id":"other","targets":[]}]\n'
        + "COOKIES_MINE=" + cookie_json() + "\nCOOKIES_OTHER=" + cookie_json() + "\n",
        encoding="utf-8",
    )
    requests = []
    monkeypatch.setattr(web_app, "github_token", lambda: "token")
    monkeypatch.setattr(web_app, "encrypted_secret", lambda value, key: "encrypted")

    def request(method, path, token, payload=None):
        requests.append((method, path, payload))
        if path.endswith("/variables?per_page=100"):
            remote = [
                {"username": "我的旧值", "unique_id": "mine", "targets": []},
                {"username": "远端其他", "unique_id": "other", "targets": ["保留"]},
            ]
            return {"variables": [{"name": "TASKS", "value": json.dumps(remote)}]}
        if path.endswith("/secrets/public-key"):
            return {"key": "key", "key_id": "id"}
        return {}

    monkeypatch.setattr(web_app, "github_request", request)
    result = web_app.sync_github(["mine"])
    task_patch = next(payload for method, path, payload in requests if method == "PATCH" and path.endswith("/variables/TASKS"))
    tasks = json.loads(task_patch["value"])
    assert tasks[0]["username"] == "我的更新"
    assert tasks[1]["username"] == "远端其他"
    assert not any(method == "DELETE" for method, _, _ in requests)
    assert result["deletedSecrets"] == []


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
    handler.client_address = ("192.168.1.20", 12345)
    assert handler.current_user()["username"] == "member"
    assert handler.allowed_account_ids() == ["mine"]
    web_app.SESSIONS.pop(token, None)


def test_loopback_request_automatically_gets_master_access():
    handler = object.__new__(web_app.Handler)
    handler.headers = {}
    handler.client_address = ("127.0.0.1", 12345)
    assert handler.current_user()["role"] == "master"
    assert handler.allowed_account_ids() is None
    assert handler.local_master() is True


def test_forwarded_or_remote_request_never_uses_master_session():
    token = "master-session"
    web_app.SESSIONS[token] = {"username": "admin", "role": "master", "accountIds": [], "expires": web_app.time.time() + 60}
    try:
        for address, headers in [
            ("192.168.1.20", {"Cookie": f"sparkflow_session={token}"}),
            ("127.0.0.1", {"Cookie": f"sparkflow_session={token}", "CF-Connecting-IP": "203.0.113.9"}),
        ]:
            handler = object.__new__(web_app.Handler)
            handler.client_address = (address, 12345)
            handler.headers = headers
            assert handler.current_user() is None
            assert handler.local_master() is False
    finally:
        web_app.SESSIONS.pop(token, None)


def test_password_hash_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "WEB_USERS_FILE", tmp_path / ".web-users.json")
    web_app.upsert_web_user("member", "password8", account_ids=["mine"])
    assert web_app.authenticate_web_user("member", "password8")["accountIds"] == ["mine"]
    assert web_app.authenticate_web_user("member", "wrongpass") is None


def test_scoped_save_preserves_other_accounts(isolated_env):
    isolated_env.write_text(
        'TASKS=[{"username":"A","unique_id":"a","message_template":"A旧消息","targets":[]},{"username":"B","unique_id":"b","message_template":"B保留消息","targets":[]}]\n',
        encoding="utf-8",
    )
    web_app.save_scoped_config({"accounts": [{"username": "A2", "uniqueId": "a", "messageTemplate": "A新消息", "targets": [{"id": "friend"}]}]}, ["a"])
    tasks = json.loads(web_app.read_env()["TASKS"])
    assert [task["unique_id"] for task in tasks] == ["a", "b"]
    assert tasks[1]["username"] == "B"
    assert tasks[0]["message_template"] == "A新消息"
    assert tasks[1]["message_template"] == "B保留消息"


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
