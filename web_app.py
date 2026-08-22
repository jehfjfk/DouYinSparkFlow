import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import base64
import re
import secrets
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
ENV_FILE = ROOT / ".env"
WEB_USERS_FILE = ROOT / ".web-users.json"
LOG_FILE = ROOT / "logs" / "app.log"
RUN_LOG_FILE = ROOT / "logs" / "web-run.log"
ENV_LOCK = threading.Lock()
SCAN_LOCK = threading.Lock()
LOGIN_LOCK = threading.Lock()
USER_LOCK = threading.Lock()
SESSIONS = {}
SCAN_STATUS = {"running": False, "percent": 0, "stage": "等待扫描", "error": None, "loginUrl": None, "qrImage": None, "scanResult": None, "ownerAccountId": None}
LOGIN_PAGE = None
LOGIN_CODE = None
LOGIN_CODE_LOCK = threading.Lock()
GITHUB_REPOSITORY = os.getenv("SPARKFLOW_GITHUB_REPOSITORY", "jehfjfk/DouYinSparkFlow")
GITHUB_ENVIRONMENT = os.getenv("SPARKFLOW_GITHUB_ENVIRONMENT", "user-data")
WEB_USERS_SYNC_FILE = ".web-users-sync.json"
CONFIG_SYNC_FILE = ".sparkflow-config-sync.json"
WEB_USERS_SYNC_CACHE_SECONDS = 15
WEB_USERS_SYNC_STALE_SECONDS = 86400
WEB_USERS_SYNC_LOCK = threading.Lock()
WEB_USERS_SYNC_LAST_CHECK = 0.0
WEB_USERS_SYNC_LAST_SUCCESS = 0.0
WEB_USERS_SYNC_CACHE = None
WEB_USERS_SYNC_LAST_ERROR = None
CONFIG_SYNC_CACHE_SECONDS = 30
CONFIG_SYNC_STALE_SECONDS = 86400
CONFIG_SYNC_LOCK = threading.Lock()
CONFIG_SYNC_LAST_CHECK = 0.0
CONFIG_SYNC_LAST_SUCCESS = 0.0
CONFIG_SYNC_CACHE = None
CONFIG_SYNC_LAST_ERROR = None
CONFIG_SYNC_KEYS = (
    "TASKS", "MESSAGE_TEMPLATE", "HITOKOTO_TYPES", "MATCH_MODE",
    "BROWSER_TIMEOUT", "FRIEND_LIST_WAIT_TIME", "TASK_RETRY_TIMES",
    "SCHEDULE_TIME", "LOG_LEVEL", "PROXY_ADDRESS",
)


def read_env():
    values = {}
    if not ENV_FILE.exists():
        return values
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


def write_env(updates, delete_keys=None):
    with ENV_LOCK:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
        pending = dict(updates)
        deleted = set(delete_keys or [])
        output = []
        for line in lines:
            stripped = line.lstrip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in deleted:
                    continue
                if key in pending:
                    output.append(f"{key}={pending.pop(key)}")
                    continue
            output.append(line)
        if pending and output and output[-1] != "":
            output.append("")
        output.extend(f"{key}={value}" for key, value in pending.items())
        temp = ENV_FILE.with_suffix(".env.tmp")
        temp.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
        os.replace(temp, ENV_FILE)


def parse_json(value, fallback):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def public_config(allowed_account_ids=None):
    refresh_config_from_sync()
    env = read_env()
    tasks = parse_json(env.get("TASKS", "[]"), [])
    accounts = []
    allowed = set(allowed_account_ids) if allowed_account_ids is not None else None
    for task in tasks:
        unique_id = str(task.get("unique_id", ""))
        if allowed is not None and unique_id not in allowed:
            continue
        cookie_key = f"COOKIES_{unique_id.upper()}"
        cookies = parse_json(env.get(cookie_key, "[]"), [])
        accounts.append({
            "username": task.get("username", ""),
            "uniqueId": unique_id,
            "enabled": task.get("enabled", True) is not False,
            "messageTemplate": str(task.get("message_template", env.get("MESSAGE_TEMPLATE", "续火花"))).replace("\\n", "\n"),
            "targets": normalize_public_targets(task.get("targets", [])),
            "cookieConfigured": bool(cookies),
            "cookieCount": len(cookies) if isinstance(cookies, list) else 0,
        })
    return {
        "messageTemplate": env.get("MESSAGE_TEMPLATE", "续火花").replace("\\n", "\n"),
        "hitokotoTypes": parse_json(env.get("HITOKOTO_TYPES", "[]"), []),
        "matchMode": env.get("MATCH_MODE", "nickname"),
        "browserTimeout": int(env.get("BROWSER_TIMEOUT", "120000")),
        "friendListWaitTime": int(env.get("FRIEND_LIST_WAIT_TIME", "2000")),
        "taskRetryTimes": int(env.get("TASK_RETRY_TIMES", "3")),
        "scheduleTime": env.get("SCHEDULE_TIME", "04:00"),
        "logLevel": env.get("LOG_LEVEL", "Info"),
        "accounts": accounts,
        "github": {"repository": GITHUB_REPOSITORY, "environment": GITHUB_ENVIRONMENT},
    }


def password_record(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    return {"salt": salt, "hash": digest}


def _read_local_web_users():
    if not WEB_USERS_FILE.exists():
        return {"users": []}
    try:
        data = json.loads(WEB_USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"users": []}
    return data if isinstance(data, dict) and isinstance(data.get("users"), list) else {"users": []}


def _web_users_sync_key():
    env = read_env()
    # WEB_USERS_SYNC_KEY can be set explicitly. WEB_ACCESS_PASSWORD is kept as
    # a backwards-compatible shared key for the already deployed ECS instance.
    shared = env.get("WEB_USERS_SYNC_KEY") or env.get("WEB_ACCESS_PASSWORD")
    return hashlib.sha256(shared.encode("utf-8")).digest() if shared else None


def _web_users_sync_url():
    env = read_env()
    configured = env.get("WEB_USERS_SYNC_URL", "").strip()
    if configured:
        return configured
    repository = env.get("SPARKFLOW_GITHUB_REPOSITORY", GITHUB_REPOSITORY)
    ref = env.get("WEB_USERS_SYNC_REF", "main").strip() or "main"
    owner, repo = (repository.split("/", 1) + [""])[:2]
    if not repo:
        owner, repo = GITHUB_REPOSITORY.split("/", 1)
    return (
        "https://raw.githubusercontent.com/"
        f"{quote(owner)}/{quote(repo)}/{quote(ref, safe='')}/{quote(WEB_USERS_SYNC_FILE.lstrip('/'), safe='')}"
    )


def _web_users_sync_urls():
    """Return reachable GitHub mirrors without inheriting a broken proxy."""
    env = read_env()
    primary = _web_users_sync_url()
    repository = env.get("SPARKFLOW_GITHUB_REPOSITORY", GITHUB_REPOSITORY)
    ref = env.get("WEB_USERS_SYNC_REF", "main").strip() or "main"
    owner, repo = (repository.split("/", 1) + [""])[:2]
    if not repo:
        owner, repo = GITHUB_REPOSITORY.split("/", 1)
    relative = quote(WEB_USERS_SYNC_FILE.lstrip("/"), safe="")
    mirrors = [
        primary,
        f"https://cdn.jsdelivr.net/gh/{quote(owner)}/{quote(repo)}@{quote(ref, safe='')}/{relative}",
        f"https://github.com/{quote(owner)}/{quote(repo)}/raw/refs/heads/{quote(ref, safe='')}/{relative}",
    ]
    return list(dict.fromkeys(mirrors))


def _config_sync_enabled():
    """Only the deployed ECS service pulls the encrypted config snapshot."""
    if ENV_FILE != ROOT / ".env":
        return False
    value = os.getenv("SPARKFLOW_CONFIG_PULL") or read_env().get("SPARKFLOW_CONFIG_PULL", "")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _config_sync_urls():
    env = read_env()
    configured = env.get("SPARKFLOW_CONFIG_SYNC_URL", "").strip()
    repository = env.get("SPARKFLOW_GITHUB_REPOSITORY", GITHUB_REPOSITORY)
    ref = env.get("WEB_USERS_SYNC_REF", "main").strip() or "main"
    owner, repo = (repository.split("/", 1) + [""])[:2]
    if not repo:
        owner, repo = GITHUB_REPOSITORY.split("/", 1)
    relative = quote(CONFIG_SYNC_FILE.lstrip("/"), safe="")
    primary = configured or (
        "https://raw.githubusercontent.com/"
        f"{quote(owner)}/{quote(repo)}/{quote(ref, safe='')}/{relative}"
    )
    mirrors = [
        primary,
        f"https://cdn.jsdelivr.net/gh/{quote(owner)}/{quote(repo)}@{quote(ref, safe='')}/{relative}",
        f"https://github.com/{quote(owner)}/{quote(repo)}/raw/refs/heads/{quote(ref, safe='')}/{relative}",
    ]
    return list(dict.fromkeys(mirrors))


def _config_snapshot_values(env=None):
    env = env or read_env()
    values = {key: env[key] for key in CONFIG_SYNC_KEYS if key in env}
    tasks = parse_json(env.get("TASKS", "[]"), [])
    for task in tasks if isinstance(tasks, list) else []:
        account_id = str(task.get("unique_id", "")).strip() if isinstance(task, dict) else ""
        if not account_id:
            continue
        key = f"COOKIES_{account_id.upper()}"
        if key in env:
            values[key] = env[key]
    return values


def _encrypt_config_snapshot(values):
    key = _web_users_sync_key()
    if not key:
        raise ValueError("缺少 WEB_USERS_SYNC_KEY 或 WEB_ACCESS_PASSWORD")
    try:
        from nacl.secret import SecretBox
    except ImportError as exc:
        raise ValueError("缺少 PyNaCl 依赖，请运行 pip install -r requirements.txt") from exc
    box = SecretBox(key)
    body = {"version": 1, "values": values, "updatedAt": int(time.time())}
    encrypted = box.encrypt(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {
        "version": 1,
        "algorithm": "xsalsa20poly1305",
        "payload": base64.b64encode(bytes(encrypted)).decode("ascii"),
    }


def _decrypt_config_snapshot(payload):
    if not isinstance(payload, dict) or payload.get("version") != 1 or not payload.get("payload"):
        raise ValueError("配置同步文件格式错误")
    key = _web_users_sync_key()
    if not key:
        raise ValueError("缺少 WEB_USERS_SYNC_KEY 或 WEB_ACCESS_PASSWORD")
    try:
        from nacl.secret import SecretBox
    except ImportError as exc:
        raise ValueError("缺少 PyNaCl 依赖，请运行 pip install -r requirements.txt") from exc
    box = SecretBox(key)
    body = json.loads(box.decrypt(base64.b64decode(payload["payload"])).decode("utf-8"))
    values = body.get("values") if isinstance(body, dict) else None
    if body.get("version") != 1 or not isinstance(values, dict):
        raise ValueError("配置同步内容格式错误")
    return {str(key): str(value) for key, value in values.items()}


def _read_bundled_config_snapshot():
    path = ROOT / CONFIG_SYNC_FILE
    if not path.exists():
        return None
    try:
        return _decrypt_config_snapshot(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _fetch_synced_config():
    global CONFIG_SYNC_LAST_CHECK, CONFIG_SYNC_LAST_SUCCESS, CONFIG_SYNC_CACHE, CONFIG_SYNC_LAST_ERROR
    if not _config_sync_enabled():
        return None
    now = time.time()
    with CONFIG_SYNC_LOCK:
        if now - CONFIG_SYNC_LAST_CHECK < CONFIG_SYNC_CACHE_SECONDS:
            return CONFIG_SYNC_CACHE
        CONFIG_SYNC_LAST_CHECK = now
        previous = CONFIG_SYNC_CACHE
        errors = []
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for url in _config_sync_urls():
            try:
                separator = "&" if "?" in url else "?"
                request = urllib.request.Request(
                    f"{url}{separator}v={int(now)}",
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                )
                with opener.open(request, timeout=8) as response:
                    raw = response.read()
                values = _decrypt_config_snapshot(json.loads(raw.decode("utf-8")))
                CONFIG_SYNC_CACHE = values
                CONFIG_SYNC_LAST_SUCCESS = now
                CONFIG_SYNC_LAST_ERROR = None
                return values
            except Exception as exc:
                errors.append(type(exc).__name__)
        bundled = _read_bundled_config_snapshot()
        if bundled is not None:
            CONFIG_SYNC_CACHE = bundled
            CONFIG_SYNC_LAST_SUCCESS = now
            CONFIG_SYNC_LAST_ERROR = ",".join(dict.fromkeys(errors)) or None
            return bundled
        CONFIG_SYNC_LAST_ERROR = ",".join(dict.fromkeys(errors)) or "sync_failed"
        if previous is not None and now - CONFIG_SYNC_LAST_SUCCESS <= CONFIG_SYNC_STALE_SECONDS:
            return previous
        return None


def refresh_config_from_sync():
    """Apply the desktop-published config to the ECS local runtime state."""
    remote = _fetch_synced_config()
    if not remote:
        return False
    tasks = parse_json(remote.get("TASKS"), None)
    if not isinstance(tasks, list):
        return False
    updates = {key: remote[key] for key in CONFIG_SYNC_KEYS if key in remote}
    updates.update({key: value for key, value in remote.items() if key.startswith("COOKIES_")})
    remote_cookie_keys = {key for key in remote if key.startswith("COOKIES_")}
    local = read_env()
    delete_keys = []
    if remote_cookie_keys or "TASKS" in remote:
        delete_keys = [
            key for key in local
            if key.startswith("COOKIES_") and key not in remote_cookie_keys
        ]
    before = {key: local.get(key) for key in set(updates) | set(delete_keys)}
    after = {key: updates.get(key) for key in before}
    if all(key in updates and before.get(key) == after.get(key) for key in before if key not in delete_keys) and not delete_keys:
        return False
    write_env(updates, delete_keys)
    return True


def _cache_local_config_snapshot():
    """Prevent a just-saved ECS edit from being replaced by its old remote cache."""
    global CONFIG_SYNC_LAST_CHECK, CONFIG_SYNC_CACHE
    if not _config_sync_enabled():
        return
    CONFIG_SYNC_CACHE = _config_snapshot_values(read_env())
    CONFIG_SYNC_LAST_CHECK = time.time()


def _encrypt_web_users(data):
    key = _web_users_sync_key()
    if not key:
        raise ValueError("缺少 WEB_USERS_SYNC_KEY 或 WEB_ACCESS_PASSWORD")
    try:
        from nacl.secret import SecretBox
    except ImportError as exc:
        raise ValueError("缺少 PyNaCl 依赖，请运行 pip install -r requirements.txt") from exc
    box = SecretBox(key)
    encrypted = box.encrypt(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {
        "version": 1,
        "algorithm": "xsalsa20poly1305",
        "payload": base64.b64encode(bytes(encrypted)).decode("ascii"),
    }


def _decrypt_web_users(payload):
    if not isinstance(payload, dict) or payload.get("version") != 1 or not payload.get("payload"):
        raise ValueError("网站账号同步文件格式错误")
    key = _web_users_sync_key()
    if not key:
        raise ValueError("缺少 WEB_USERS_SYNC_KEY 或 WEB_ACCESS_PASSWORD")
    try:
        from nacl.secret import SecretBox
    except ImportError as exc:
        raise ValueError("缺少 PyNaCl 依赖，请运行 pip install -r requirements.txt") from exc
    box = SecretBox(key)
    data = json.loads(box.decrypt(base64.b64decode(payload["payload"])).decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise ValueError("网站账号同步内容格式错误")
    return data


def _read_bundled_web_users():
    """Read the encrypted user snapshot shipped with the deployed checkout.

    The ECS service must remain usable during a temporary GitHub outage. The
    snapshot is encrypted with the same sync key and is refreshed by the ECS
    updater, so it is a safer fallback than dropping back to an older local
    user file.
    """
    path = ROOT / WEB_USERS_SYNC_FILE
    if not path.exists():
        return None
    try:
        return _decrypt_web_users(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _merge_web_users(local, remote):
    local_by_username = {}
    for user in local.get("users", []):
        if not isinstance(user, dict) or not user.get("username"):
            continue
        copy = dict(user)
        copy["accountIds"] = list(dict.fromkeys(copy.get("accountIds", [])))
        local_by_username[str(copy["username"])] = copy
    # The computer's published file is authoritative for account creation and
    # deletion. Keep only local bindings for users that still exist remotely.
    merged = {"users": []}
    for user in remote.get("users", []):
        if not isinstance(user, dict) or not user.get("username"):
            continue
        username = str(user["username"])
        incoming = dict(user)
        incoming["accountIds"] = list(dict.fromkeys(incoming.get("accountIds", [])))
        existing = local_by_username.get(username)
        local_ids = existing.get("accountIds", []) if existing else []
        if local_ids and not incoming["accountIds"]:
            incoming["accountIds"] = local_ids
        merged["users"].append(incoming)
    return merged


def _fetch_synced_web_users():
    global WEB_USERS_SYNC_LAST_CHECK, WEB_USERS_SYNC_LAST_SUCCESS, WEB_USERS_SYNC_CACHE, WEB_USERS_SYNC_LAST_ERROR
    # Tests and isolated callers replace WEB_USERS_FILE; avoid network access
    # for those temporary stores.
    if WEB_USERS_FILE != ROOT / ".web-users.json":
        return None
    now = time.time()
    with WEB_USERS_SYNC_LOCK:
        if now - WEB_USERS_SYNC_LAST_CHECK < WEB_USERS_SYNC_CACHE_SECONDS:
            return WEB_USERS_SYNC_CACHE
        WEB_USERS_SYNC_LAST_CHECK = now
        previous = WEB_USERS_SYNC_CACHE
        errors = []
        # ECS images occasionally export HTTP(S)_PROXY values that cannot reach
        # GitHub. The website sync is a direct HTTPS fetch by design.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for url in _web_users_sync_urls():
            try:
                # The raw and CDN mirrors can cache the same path briefly after
                # a password reset. Revalidate on every cache-window refresh.
                request = urllib.request.Request(
                    url,
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                )
                with opener.open(request, timeout=5) as response:
                    raw = response.read()
                remote = _decrypt_web_users(json.loads(raw.decode("utf-8")))
                WEB_USERS_SYNC_CACHE = remote
                WEB_USERS_SYNC_LAST_SUCCESS = now
                WEB_USERS_SYNC_LAST_ERROR = None
                return remote
            except Exception as exc:
                errors.append(type(exc).__name__)
        bundled = _read_bundled_web_users()
        if bundled is not None:
            WEB_USERS_SYNC_CACHE = bundled
            WEB_USERS_SYNC_LAST_SUCCESS = now
            WEB_USERS_SYNC_LAST_ERROR = ",".join(dict.fromkeys(errors)) or None
            return bundled
        # Keep the last known-good remote set for a bounded outage. This is
        # preferable to locking every mobile user out during a transient fetch
        # failure; a later request refreshes it after the cache window expires.
        WEB_USERS_SYNC_LAST_ERROR = ",".join(dict.fromkeys(errors)) or "sync_failed"
        if previous is not None and now - WEB_USERS_SYNC_LAST_SUCCESS <= WEB_USERS_SYNC_STALE_SECONDS:
            return previous
        return None


def load_web_users(sync=True):
    local = _read_local_web_users()
    remote = _fetch_synced_web_users() if sync else None
    if remote is None:
        return local
    merged = _merge_web_users(local, remote)
    if merged != local:
        save_web_users(merged)
    return merged


def save_web_users(data):
    with USER_LOCK:
        temporary = WEB_USERS_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, WEB_USERS_FILE)


def upsert_web_user(username, password, role="account", account_ids=None):
    username = str(username or "").strip()
    if not username or len(password or "") < 8:
        raise ValueError("网站账号不能为空，密码至少 8 位")
    data = load_web_users(sync=False)
    user = next((item for item in data["users"] if item["username"] == username), None)
    record = {"username": username, "role": role, "accountIds": list(account_ids or []), **password_record(password)}
    if user:
        user.update(record)
    else:
        data["users"].append(record)
    save_web_users(data)
    return {"username": username, "role": role, "accountIds": record["accountIds"]}


def reset_web_user_password(username, password):
    """Reset an existing mobile user's password while retaining its binding."""
    username = str(username or "").strip()
    if not username or len(password or "") < 8:
        raise ValueError("网站账号不能为空，密码至少 8 位")
    with USER_LOCK:
        data = load_web_users(sync=False)
        user = next(
            (
                item
                for item in data["users"]
                if item.get("username") == username and item.get("role") == "account"
            ),
            None,
        )
        if not user:
            raise ValueError("手机端登录账号不存在")
        user.update(password_record(password))
        temporary = WEB_USERS_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, WEB_USERS_FILE)
        return {"username": username, "role": user["role"], "accountIds": list(user.get("accountIds", []))}


def bind_web_user_account(username, account_id):
    with USER_LOCK:
        data = load_web_users(sync=False)
        user = next((item for item in data["users"] if item["username"] == username and item["role"] == "account"), None)
        if not user:
            raise ValueError("网站用户不存在")
        if user.get("accountIds"):
            raise ValueError("网站用户已经绑定抖音账号")
        user["accountIds"] = [account_id]
        temporary = WEB_USERS_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, WEB_USERS_FILE)


def delete_web_user(username):
    username = str(username or "").strip()
    with USER_LOCK:
        data = load_web_users(sync=False)
        original_count = len(data["users"])
        data["users"] = [
            user for user in data["users"]
            if not (user["username"] == username and user["role"] == "account")
        ]
        if len(data["users"]) == original_count:
            raise ValueError("手机端登录账号不存在")
        temporary = WEB_USERS_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, WEB_USERS_FILE)
    for token, session in list(SESSIONS.items()):
        if session.get("username") == username:
            SESSIONS.pop(token, None)
    return {"username": username}


def authenticate_web_user(username, password):
    username = str(username or "").strip()
    if not username or password is None:
        return None
    refresh_config_from_sync()
    local = _read_local_web_users()
    remote = _fetch_synced_web_users()
    users = _merge_web_users(local, remote) if remote is not None else local
    if remote is not None and users != local:
        # Persist the last successful sync so the ECS instance can still serve
        # mobile logins when GitHub is briefly unreachable on the next request.
        save_web_users(users)
    user = next(
        (
            item
            for item in users.get("users", [])
            if isinstance(item, dict) and str(item.get("username", "")).strip() == username
        ),
        None,
    )
    if not user:
        return None
    salt = user.get("salt")
    hashed = user.get("hash")
    if not salt or not hashed:
        return None
    expected = password_record(str(password), salt)["hash"]
    return user if hmac.compare_digest(expected, hashed) else None


def create_session(user):
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": user["username"], "role": user["role"], "accountIds": user.get("accountIds", []), "expires": time.time() + 86400}
    return token


def normalize_public_targets(targets):
    result = []
    for target in targets:
        if isinstance(target, str):
            result.append({"id": target, "aliases": []})
        elif isinstance(target, dict):
            target_id = str(target.get("id") or target.get("unique_id") or target.get("short_id") or "").strip()
            aliases = [str(value).strip() for value in target.get("aliases", []) if str(value).strip()]
            if target_id:
                result.append({"id": target_id, "aliases": list(dict.fromkeys(aliases))})
    return result


def validate_cookie_json(raw):
    cookies = json.loads(raw)
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("Cookie 必须是非空 JSON 数组")
    required = {"name", "value", "domain", "path"}
    for index, cookie in enumerate(cookies, 1):
        if not isinstance(cookie, dict) or not required.issubset(cookie):
            raise ValueError(f"第 {index} 条 Cookie 缺少必要字段")
    return cookies


def save_config(payload):
    accounts = payload.get("accounts", [])
    if not isinstance(accounts, list):
        raise ValueError("账号配置格式错误")

    current = read_env()
    previous_tasks = parse_json(current.get("TASKS", "[]"), [])
    tasks = []
    updates = {
        "PROXY_ADDRESS": str(payload.get("proxyAddress", current.get("PROXY_ADDRESS", ""))),
        "MESSAGE_TEMPLATE": str(payload.get("messageTemplate", "续火花")).replace("\r\n", "\n").replace("\n", "\\n"),
        "HITOKOTO_TYPES": json.dumps(payload.get("hitokotoTypes", []), ensure_ascii=False, separators=(",", ":")),
        "MATCH_MODE": payload.get("matchMode", "nickname"),
        "BROWSER_TIMEOUT": str(max(10000, int(payload.get("browserTimeout", 120000)))),
        "FRIEND_LIST_WAIT_TIME": str(max(500, int(payload.get("friendListWaitTime", 2000)))),
        "TASK_RETRY_TIMES": str(max(1, int(payload.get("taskRetryTimes", 3)))),
        "SCHEDULE_TIME": validate_schedule_time(payload.get("scheduleTime", current.get("SCHEDULE_TIME", "04:00"))),
        "LOG_LEVEL": payload.get("logLevel", "Info"),
    }

    for account in accounts:
        username = str(account.get("username", "")).strip()
        unique_id = str(account.get("uniqueId", "")).strip()
        enabled = account.get("enabled", True) is not False
        targets = normalize_public_targets(account.get("targets", []))
        message_template = str(account.get("messageTemplate", payload.get("messageTemplate", "续火花"))).replace("\r\n", "\n")
        if not username or not unique_id:
            raise ValueError("用户名和抖音号不能为空")
        tasks.append({"username": username, "unique_id": unique_id, "message_template": message_template, "targets": targets, "enabled": enabled})
        cookie_raw = account.get("cookies")
        if cookie_raw:
            cookies = validate_cookie_json(cookie_raw)
            updates[f"COOKIES_{unique_id.upper()}"] = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))

    updates["TASKS"] = json.dumps(tasks, ensure_ascii=False, separators=(",", ":"))
    active_ids = {task["unique_id"].upper() for task in tasks}
    removed_cookie_keys = {
        f"COOKIES_{str(task.get('unique_id', '')).upper()}"
        for task in previous_tasks
        if str(task.get("unique_id", "")).upper() not in active_ids
    }
    write_env(updates, removed_cookie_keys)
    _cache_local_config_snapshot()
    return public_config()


def save_scoped_config(payload, allowed_account_ids=None):
    if allowed_account_ids is None:
        return save_config(payload)
    allowed = set(allowed_account_ids)
    submitted = payload.get("accounts", [])
    if not submitted or any(str(account.get("uniqueId", "")) not in allowed for account in submitted):
        raise ValueError("账号配置超出当前用户的访问范围")
    current = public_config()
    replacements = {str(account["uniqueId"]): account for account in submitted}
    merged_accounts = []
    for account in current["accounts"]:
        merged_accounts.append(replacements.get(account["uniqueId"], account))
    scoped_payload = {
        "accounts": merged_accounts,
        "messageTemplate": current["messageTemplate"], "hitokotoTypes": current["hitokotoTypes"],
        "matchMode": current["matchMode"], "browserTimeout": current["browserTimeout"],
        "friendListWaitTime": current["friendListWaitTime"], "taskRetryTimes": current["taskRetryTimes"],
        "scheduleTime": current["scheduleTime"], "logLevel": current["logLevel"],
    }
    save_config(scoped_payload)
    return public_config(allowed)


def provision_first_account(payload, user):
    submitted = payload.get("accounts", [])
    if len(submitted) != 1:
        raise ValueError("首次配置只能添加一个抖音账号")
    account = submitted[0]
    account_id = str(account.get("uniqueId", "")).strip()
    if not account_id:
        raise ValueError("请先填写抖音号")
    current = public_config()
    if any(item["uniqueId"] == account_id for item in current["accounts"]):
        raise ValueError("该抖音账号已经被其他用户绑定")
    merged_payload = {
        "accounts": current["accounts"] + [account],
        "messageTemplate": current["messageTemplate"], "hitokotoTypes": current["hitokotoTypes"],
        "matchMode": current["matchMode"], "browserTimeout": current["browserTimeout"],
        "friendListWaitTime": current["friendListWaitTime"], "taskRetryTimes": current["taskRetryTimes"],
        "scheduleTime": current["scheduleTime"], "logLevel": current["logLevel"],
    }
    save_config(merged_payload)
    bind_web_user_account(user["username"], account_id)
    user["accountIds"] = [account_id]
    return public_config([account_id])


def validate_schedule_time(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("执行时间必须是 HH:MM 格式")
    return value


def schedule_cron(value):
    hour, minute = map(int, validate_schedule_time(value).split(":"))
    return f"{minute} {(hour - 8) % 24} * * *"


def merge_tasks(existing_tasks, local_tasks):
    """Upsert local accounts without removing accounts already stored on GitHub."""
    merged = []
    local_by_id = {str(task.get("unique_id", "")): task for task in local_tasks}
    seen = set()
    for task in existing_tasks:
        unique_id = str(task.get("unique_id", ""))
        replacement = local_by_id.get(unique_id)
        merged.append(replacement if replacement is not None else task)
        seen.add(unique_id)
    merged.extend(task for unique_id, task in local_by_id.items() if unique_id not in seen)
    return merged


def github_token():
    env = read_env()
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if token:
        return token.strip()
    request = "protocol=https\nhost=github.com\n\n"
    try:
        result = subprocess.run(["git", "credential", "fill"], input=request, text=True, capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("未读取到 GitHub 登录凭证，请先在 GitHub Desktop 登录") from exc
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not values.get("password"):
        raise ValueError("未读取到 GitHub 登录凭证，请先在 GitHub Desktop 登录")
    return values["password"]


def github_request(method, path, token, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"https://api.github.com{path}", data=body, method=method, headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "Content-Type": "application/json", "User-Agent": "DouYinSparkFlow-Web", "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"GitHub 同步失败（HTTP {exc.code}）：{detail[:240]}") from exc


def sync_web_users():
    """Publish encrypted website users for the ECS instance to pull on login."""
    token = github_token()
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    branch = read_env().get("WEB_USERS_SYNC_REF", "main").strip() or "main"
    content = json.dumps(_encrypt_web_users(_read_local_web_users()), ensure_ascii=False, indent=2).encode("utf-8")
    base = f"/repos/{owner}/{repo}/contents/{quote(WEB_USERS_SYNC_FILE)}"
    current = None
    try:
        current = github_request("GET", f"{base}?ref={quote(branch)}", token)
    except ValueError as exc:
        if "HTTP 404" not in str(exc):
            raise
    payload = {
        "message": "chore: sync website login users",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if current and current.get("sha"):
        payload["sha"] = current["sha"]
    github_request("PUT", base, token, payload)
    global WEB_USERS_SYNC_CACHE, WEB_USERS_SYNC_LAST_CHECK
    WEB_USERS_SYNC_CACHE = _read_local_web_users()
    WEB_USERS_SYNC_LAST_CHECK = time.time()
    return {"file": WEB_USERS_SYNC_FILE, "branch": branch, "users": len(WEB_USERS_SYNC_CACHE["users"])}


def sync_web_users_best_effort():
    try:
        return {"ok": True, "result": sync_web_users()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def sync_config_snapshot(token=None):
    """Publish encrypted TASKS/settings/Cookies for the always-on ECS site."""
    token = token or github_token()
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    env = read_env()
    branch = env.get("WEB_USERS_SYNC_REF", "main").strip() or "main"
    content = json.dumps(_encrypt_config_snapshot(_config_snapshot_values(env)), ensure_ascii=False, indent=2).encode("utf-8")
    base = f"/repos/{owner}/{repo}/contents/{quote(CONFIG_SYNC_FILE)}"
    current = None
    try:
        current = github_request("GET", f"{base}?ref={quote(branch)}", token)
    except ValueError as exc:
        if "HTTP 404" not in str(exc):
            raise
    payload = {
        "message": "chore: sync encrypted web config snapshot",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if current and current.get("sha"):
        payload["sha"] = current["sha"]
    github_request("PUT", base, token, payload)
    return {"file": CONFIG_SYNC_FILE, "branch": branch}


def sync_config_snapshot_best_effort(token=None):
    try:
        return {"ok": True, "result": sync_config_snapshot(token)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def encrypted_secret(value, public_key):
    try:
        from nacl import encoding, public
    except ImportError as exc:
        raise ValueError("缺少 PyNaCl 依赖，请运行 pip install -r requirements.txt") from exc
    key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    encrypted = public.SealedBox(key).encrypt(value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def sync_github(allowed_account_ids=None):
    config = public_config()
    env = read_env()
    token = github_token()
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    base = f"/repos/{owner}/{repo}/environments/{GITHUB_ENVIRONMENT}"
    variables = github_request("GET", f"{base}/variables?per_page=100", token).get("variables", [])
    remote_values = {item["name"]: item["value"] for item in variables}
    local_tasks = parse_json(env.get("TASKS", "[]"), [])
    remote_tasks = parse_json(remote_values.get("TASKS", "[]"), [])
    allowed = set(allowed_account_ids) if allowed_account_ids is not None else None
    if allowed is None:
        synced_tasks = local_tasks
    else:
        scoped_tasks = [task for task in local_tasks if str(task.get("unique_id", "")) in allowed]
        synced_tasks = merge_tasks(remote_tasks, scoped_tasks)
    env["TASKS"] = json.dumps(synced_tasks, ensure_ascii=False, separators=(",", ":"))
    variable_names = ["TASKS"] if allowed_account_ids is not None else ["TASKS", "MESSAGE_TEMPLATE", "HITOKOTO_TYPES", "MATCH_MODE", "BROWSER_TIMEOUT", "FRIEND_LIST_WAIT_TIME", "TASK_RETRY_TIMES", "LOG_LEVEL", "PROXY_ADDRESS"]
    for name in variable_names:
        value = env.get(name, "")
        # GitHub Environment Variables reject empty values. An absent optional value
        # (such as PROXY_ADDRESS) must not prevent Cookie Secrets from being synced.
        if value == "":
            continue
        try:
            github_request("PATCH", f"{base}/variables/{name}", token, {"name": name, "value": value})
        except ValueError as exc:
            if "HTTP 404" not in str(exc):
                raise
            github_request("POST", f"{base}/variables", token, {"name": name, "value": value})

    active_secrets = set()
    key_info = github_request("GET", f"{base}/secrets/public-key", token) if config["accounts"] else None
    for account in config["accounts"]:
        if allowed is not None and account["uniqueId"] not in allowed:
            continue
        name = f"COOKIES_{account['uniqueId'].upper()}"
        value = env.get(name)
        if not value:
            raise ValueError(f"账号 {account['username']} 缺少 Cookie")
        github_request("PUT", f"{base}/secrets/{name}", token, {"encrypted_value": encrypted_secret(value, key_info["key"]), "key_id": key_info["key_id"]})
        active_secrets.add(name)

    deleted_secrets = []
    if allowed is None:
        active_ids = {str(task.get("unique_id", "")).upper() for task in local_tasks}
        removed_ids = {
            str(task.get("unique_id", "")).upper()
            for task in remote_tasks
            if str(task.get("unique_id", "")).upper() not in active_ids
        }
        for account_id in sorted(removed_ids):
            name = f"COOKIES_{account_id}"
            try:
                github_request("DELETE", f"{base}/secrets/{name}", token)
            except ValueError as exc:
                if "HTTP 404" not in str(exc):
                    raise
            deleted_secrets.append(name)

    web_users = sync_web_users_best_effort() if allowed_account_ids is None else None
    # The ECS snapshot contains the complete local state. Scoped users still
    # publish it after their account-only merge, leaving unrelated accounts
    # unchanged while keeping their edits available after an ECS restart.
    config_snapshot = sync_config_snapshot_best_effort(token)
    return {
        "repository": GITHUB_REPOSITORY,
        "accounts": len(synced_tasks),
        "secrets": len(active_secrets),
        "deletedSecrets": deleted_secrets,
        "webUsers": web_users,
        "configSnapshot": config_snapshot,
    }


def scan_pinned_account(account_index, finalize=True):
    """Read-only scan of pinned contacts using the selected account Cookie."""
    from core.browser import get_browser
    from core import tasks as task_core
    from utils.config import sanitize_cookies

    config = public_config()
    try:
        account = config["accounts"][int(account_index)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("账号不存在") from exc
    update_scan_status(True, 5, "正在读取账号配置", loginUrl=None, qrImage=None, scanResult=None, ownerAccountId=account["uniqueId"])
    cookies = parse_json(read_env().get(f"COOKIES_{account['uniqueId'].upper()}", "[]"), [])
    if not cookies:
        raise ValueError(f"账号 {account['username']} 尚未配置 Cookie")
    account_identity = task_core.norm(account["uniqueId"])

    task_core.userIDDict.clear()
    playwright, browser = get_browser()
    context = browser.new_context(
        user_agent=task_core.WINDOWS_CHROME_USER_AGENT, locale="zh-CN",
        timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 900},
    )
    context.set_default_timeout(config["browserTimeout"])
    page = context.new_page()
    page.on("response", task_core.handle_response)
    results = []
    try:
        update_scan_status(True, 15, "正在加载 Cookie")
        context.add_cookies(sanitize_cookies(cookies))
        update_scan_status(True, 25, "正在打开抖音创作者中心")
        page.goto("https://creator.douyin.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        update_scan_status(True, 40, "正在进入消息列表")
        task_core.open_chat_page(page)
        page.wait_for_timeout(max(1000, config["friendListWaitTime"]))
        update_scan_status(True, 55, "正在读取会话列表")
        conversation_list = page.locator(task_core.CONVERSATION_LIST_SELECTOR)
        def is_pinned_item(item):
            """Douyin renders the pin marker in different nested nodes across releases."""
            markers = item.evaluate("""element => {
                const values = [];
                const nodes = [element, ...element.querySelectorAll('*')];
                for (const node of nodes) {
                    values.push(node.className || '', node.id || '', node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '', node.getAttribute('data-e2e') || '',
                        node.getAttribute('data-testid') || '', node.getAttribute('data-test') || '',
                        node.getAttribute('data-type') || '', node.getAttribute('data-status') || '');
                }
                return values.filter(Boolean).map(value => String(value).toLowerCase());
            }""")
            return any(
                "置顶" in marker or "isstickontop" in marker or "stick-on-top" in marker or
                "stick_on_top" in marker or ("ispinned" in marker and "unpinned" not in marker)
                for marker in markers
            )

        conversation_list.evaluate("""element => {
            const nodes = [element, ...element.querySelectorAll('*')];
            const scroller = nodes.find(node => node.scrollHeight > node.clientHeight + 4) || element;
            scroller.scrollTop = 0;
        }""")
        page.wait_for_timeout(300)
        seen_rows = set()
        result_keys = set()
        stable_rounds = 0
        for scan_round in range(60):
            items = conversation_list.locator(task_core.CONVERSATION_ITEM_SELECTOR).all()
            new_rows = 0
            update_scan_status(True, min(92, 55 + scan_round * 2), f"正在遍历会话列表，已读取 {len(seen_rows)} 个会话")
            for item in items:
                try:
                    title = task_core.norm(item.locator(task_core.CONVERSATION_TITLE_SELECTOR).inner_text())
                    if not title:
                        row_lines = [task_core.norm(line) for line in item.inner_text().splitlines() if task_core.norm(line)]
                        title = row_lines[0] if row_lines else ""
                    if not title:
                        continue
                    row_key = item.evaluate("""(element, title) => {
                        const attrs = ['data-id','data-key','data-conversation-id','data-e2e','aria-label']
                            .map(name => element.getAttribute(name) || '').filter(Boolean);
                        const avatars = [...element.querySelectorAll('img')].map(img => {
                            try { return new URL(img.currentSrc || img.src, location.href).pathname; }
                            catch (_) { return img.currentSrc || img.src || ''; }
                        }).filter(Boolean);
                        return [title, ...attrs, ...avatars].join('|');
                    }""", title)
                    if row_key in seen_rows:
                        continue
                    seen_rows.add(row_key)
                    new_rows += 1
                    if not is_pinned_item(item):
                        continue
                    item.click()
                    identity = []
                    identity_deadline = time.monotonic() + 2
                    while time.monotonic() < identity_deadline:
                        identity = task_core.userIDDict.get(title, [])
                        if not identity:
                            identity = next(
                                (values for name, values in task_core.userIDDict.items() if task_core.norm(name) == task_core.norm(title)),
                                [],
                            )
                        if identity:
                            break
                        page.wait_for_timeout(200)
                    short_id = identity[0] if identity else ""
                    unique_id = identity[1] if len(identity) > 1 else ""
                    if account_identity and account_identity in {
                        task_core.norm(short_id), task_core.norm(unique_id)
                    }:
                        continue
                    result_key = unique_id or short_id or title
                    if result_key in result_keys:
                        continue
                    result_keys.add(result_key)
                    results.append({"nickname": title, "remark": identity[4] if len(identity) > 4 else title, "shortId": short_id, "uniqueId": unique_id, "pinned": True})
                except Exception:
                    continue

            scroll_state = conversation_list.evaluate("""element => {
                const nodes = [element, ...element.querySelectorAll('*')];
                const scroller = nodes.find(node => node.scrollHeight > node.clientHeight + 4) || element;
                const before = scroller.scrollTop;
                const beforeBottom = before + scroller.clientHeight >= scroller.scrollHeight - 4;
                scroller.scrollTop = Math.min(scroller.scrollHeight, before + Math.max(200, scroller.clientHeight * 0.8));
                return {before, after: scroller.scrollTop, beforeBottom, bottom: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4};
            }""")
            if scroll_state["beforeBottom"]:
                break
            page.wait_for_timeout(500)
            stable_rounds = stable_rounds + 1 if new_rows == 0 and (scroll_state["bottom"] or scroll_state["after"] == scroll_state["before"]) else 0
            if stable_rounds >= 3:
                break
        update_scan_status(not finalize, 100 if finalize else 98, f"扫描完成，识别到 {len(results)} 个置顶会话")
        return {"accountIndex": int(account_index), "accountId": account["uniqueId"], "account": account["username"], "contacts": results, "readOnly": True, "message": f"仅识别到 {len(results)} 个置顶会话"}
    except Exception as exc:
        update_scan_status(False, 100, "扫描失败", str(exc))
        raise
    finally:
        context.close()
        browser.close()
        playwright.stop()


def update_scan_status(running, percent, stage, error=None, **extra):
    with SCAN_LOCK:
        SCAN_STATUS.update({"running": running, "percent": max(0, min(100, int(percent))), "stage": stage, "error": error})
        SCAN_STATUS.update(extra)


def get_scan_status():
    with SCAN_LOCK:
        return dict(SCAN_STATUS)


def clear_scan_result():
    with SCAN_LOCK:
        SCAN_STATUS["scanResult"] = None


def submit_login_code(code):
    global LOGIN_CODE
    code = str(code or "").strip()
    if not code.isdigit() or not 4 <= len(code) <= 8:
        raise ValueError("验证码格式不正确")
    with LOGIN_CODE_LOCK:
        LOGIN_CODE = code


def refresh_account_login(account_id, continue_to_scan=False):
    """Open an interactive Douyin login and persist the resulting Cookie for one account."""
    from core.browser import get_browser
    from core import tasks as task_core
    global LOGIN_PAGE, LOGIN_CODE

    account_id = str(account_id or "").strip()
    account = next((item for item in public_config()["accounts"] if item["uniqueId"] == account_id), None)
    if not account:
        raise ValueError("指定账号不存在")
    update_scan_status(True, 5, "正在生成抖音登录链接", loginUrl=None, qrImage=None, scanResult=None, ownerAccountId=account_id)
    playwright, browser = get_browser()
    context = browser.new_context(user_agent=task_core.WINDOWS_CHROME_USER_AGENT, locale="zh-CN", timezone_id="Asia/Shanghai")
    page = context.new_page()
    LOGIN_PAGE = page
    with LOGIN_CODE_LOCK:
        LOGIN_CODE = None
    try:
        page.goto("https://creator.douyin.com/", wait_until="domcontentloaded")
        try:
            login_buttons = page.get_by_text("创作者登录", exact=True).all()
            visible_button = next((button for button in reversed(login_buttons) if button.is_visible()), None)
            if visible_button:
                visible_button.click()
            page.wait_for_timeout(1800)
        except Exception:
            pass
        # Douyin has changed the login component markup several times. Prefer
        # the known container, then fall back to large visible QR-like images
        # or canvases instead of binding to one generated element id.
        qr_locator = None
        selectors = (
            "#douyin_login_comp_scan_code img",
            "#douyin_login_comp_scan_code canvas",
            "img[alt*='二维码'], img[alt*='扫码'], img[src*='qr']",
            "[class*='login-card-double'] img",
            "[class*='login-card-double'] canvas",
        )
        qr_deadline = time.monotonic() + 25
        while qr_locator is None and time.monotonic() < qr_deadline:
            for selector in selectors:
                candidates = page.locator(selector)
                for index in range(candidates.count() - 1, -1, -1):
                    candidate = candidates.nth(index)
                    try:
                        if candidate.is_visible() and (candidate.get_attribute("src") or selector.endswith("canvas") or candidate.bounding_box()):
                            box = candidate.bounding_box()
                            is_square = box and abs(box["width"] - box["height"]) <= max(box["width"], box["height"]) * 0.12
                            if box and is_square and box["width"] >= 150 and box["height"] >= 150:
                                qr_locator = candidate
                                break
                    except Exception:
                        continue
                if qr_locator:
                    break
            if qr_locator is None:
                page.wait_for_timeout(500)
        if qr_locator is None:
            raise ValueError("未找到抖音登录二维码，请刷新登录窗口后重试")
        qr_locator.wait_for(state="visible", timeout=15000)
        qr_image = qr_locator.get_attribute("src")
        if not qr_image or qr_image.startswith("blob:"):
            qr_image = "data:image/png;base64," + base64.b64encode(qr_locator.screenshot(type="png")).decode("ascii")
        login_url = None
        if qr_image and qr_image.startswith("data:image"):
            try:
                import cv2
                import numpy as np
                image_bytes = base64.b64decode(qr_image.split(",", 1)[1])
                matrix = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
                login_url, _, _ = cv2.QRCodeDetector().detectAndDecode(matrix)
            except Exception:
                login_url = None
        update_scan_status(True, 20, "请使用抖音扫一扫并确认登录", loginUrl=login_url or None, qrImage=qr_image or None)
        deadline = time.monotonic() + 300
        auth_cookie_names = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "uid_tt_ss"}
        qr_locked = False
        verification_seen = False
        main_site_attempt = 0
        while time.monotonic() < deadline:
            names = {cookie.get("name") for cookie in context.cookies()}
            identity_verification = False
            try:
                login_text = page.locator("body").inner_text(timeout=1000)
                identity_verification = "身份验证" in login_text and any(
                    label in login_text for label in ("接收短信验证码", "手机刷脸验证", "验证登录密码")
                )
                if identity_verification:
                    qr_locked = True
                    verification_seen = True
                    try:
                        sms = page.get_by_text("接收短信验证码", exact=False).first
                        sms.click(timeout=2500)
                        page.wait_for_timeout(500)
                        send_sms = page.get_by_text("发送短信验证", exact=False).first
                        if send_sms.is_visible():
                            send_sms.click(timeout=2500)
                        update_scan_status(True, 38, "短信验证码已发送，请在手机端输入", qrImage=None, verificationRequired=True)
                    except Exception:
                        update_scan_status(True, 35, "请在手机端输入短信验证码", qrImage=None, verificationRequired=True)
                    with LOGIN_CODE_LOCK:
                        code = LOGIN_CODE
                    if code:
                        for selector in ("input[placeholder*='验证码']", "input[placeholder*='验证']", "input[type='text']"):
                            try:
                                field = page.locator(selector).last
                                if field.is_visible():
                                    field.fill(code)
                                    for button_text in ("验证", "确认", "登录"):
                                        buttons = page.get_by_text(button_text, exact=True)
                                        if buttons.count() and buttons.last.is_visible():
                                            buttons.last.click(timeout=1500)
                                            break
                            except Exception:
                                continue
                        with LOGIN_CODE_LOCK:
                            LOGIN_CODE = None
                        update_scan_status(True, 42, "验证码已提交，正在确认登录", qrImage=None, verificationRequired=False)
                elif "扫码成功" in login_text or "确认登录" in login_text:
                    qr_locked = True
                    update_scan_status(True, 35, "已扫码，请在手机抖音中点击确认登录", qrImage=None)
                elif not qr_locked:
                    current_qr_image = qr_locator.get_attribute("src")
                    if current_qr_image and current_qr_image != qr_image:
                        qr_image = current_qr_image
                        if qr_image.startswith("blob:"):
                            qr_image = "data:image/png;base64," + base64.b64encode(qr_locator.screenshot(type="png")).decode("ascii")
                        update_scan_status(True, 20, "二维码已自动刷新，请重新扫码并确认", qrImage=qr_image)
            except Exception:
                pass
            with LOGIN_CODE_LOCK:
                pending_code = LOGIN_CODE
            if pending_code and not identity_verification:
                for selector in ("input[placeholder*='验证码']", "input[placeholder*='验证']", "input[aria-label*='验证码']", "input[type='text']"):
                    try:
                        field = page.locator(selector).last
                        if field.is_visible():
                            field.fill(pending_code)
                            for button_text in ("验证", "确认", "登录"):
                                buttons = page.get_by_text(button_text, exact=True)
                                if buttons.count() and buttons.last.is_visible():
                                    buttons.last.click(timeout=1500)
                                    break
                            with LOGIN_CODE_LOCK:
                                LOGIN_CODE = None
                            update_scan_status(True, 42, "验证码已提交，正在确认登录", qrImage=None, verificationRequired=False)
                            break
                    except Exception:
                        continue
            try:
                qr_completed = not qr_locator.is_visible()
            except Exception:
                qr_completed = True
            login_confirmed = names.intersection(auth_cookie_names) or qr_completed
            if login_confirmed and not identity_verification and (verification_seen or qr_completed):
                qr_locked = True
                main_site_attempt += 1
                update_scan_status(True, 45, f"身份验证已通过，正在同步抖音主站（第 {main_site_attempt} 次）", loginUrl=None, qrImage=None)
                page.goto(task_core.CHAT_URL, wait_until="domcontentloaded")
                try:
                    task_core.wait_for_chat_ready(page, timeout=10000)
                    break
                except (task_core.AuthenticationRequiredError, TimeoutError):
                    page.goto("https://creator.douyin.com/", wait_until="domcontentloaded")
                    update_scan_status(True, 45, "创作者中心已登录，正在等待主站同步", loginUrl=None, qrImage=None)
            page.wait_for_timeout(2000)
        else:
            raise ValueError("等待登录超时，请重新点击后在 5 分钟内完成登录")

        update_scan_status(True, 75, "登录成功，正在保存 Cookie")
        cookies = context.cookies()
        value = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
        write_env({f"COOKIES_{account_id.upper()}": value})
        _cache_local_config_snapshot()
        token = github_token()
        owner, repo = GITHUB_REPOSITORY.split("/", 1)
        base = f"/repos/{owner}/{repo}/environments/{GITHUB_ENVIRONMENT}"
        key_info = github_request("GET", f"{base}/secrets/public-key", token)
        github_request("PUT", f"{base}/secrets/COOKIES_{account_id.upper()}", token, {
            "encrypted_value": encrypted_secret(value, key_info["key"]), "key_id": key_info["key_id"],
        })
        # Keep the always-on ECS snapshot aligned when a phone user refreshes
        # a Cookie, so its next config pull does not restore an older value.
        sync_config_snapshot_best_effort(token)
        update_scan_status(continue_to_scan, 80 if continue_to_scan else 100, "登录已更新，正在启动置顶好友扫描", loginUrl=None, qrImage=None)
        return {"accountId": account_id, "cookieCount": len(cookies), "updated": True}
    except Exception as exc:
        update_scan_status(False, get_scan_status()["percent"], "登录更新失败", str(exc), loginUrl=None, qrImage=None)
        raise
    finally:
        LOGIN_PAGE = None
        with LOGIN_CODE_LOCK:
            LOGIN_CODE = None
        context.close(); browser.close(); playwright.stop()


def refresh_login_and_scan(account_id):
    """Finish the selected account's login, Cookie sync, and pinned scan as one recoverable job."""
    if not LOGIN_LOCK.acquire(blocking=False):
        raise ValueError("已有账号正在更新登录，请等待当前任务完成")
    try:
        accounts = public_config()["accounts"]
        account_index = next((index for index, item in enumerate(accounts) if item["uniqueId"] == str(account_id)), None)
        if account_index is None:
            raise ValueError("指定账号不存在")
        login_result = None
        for attempt in range(2):
            try:
                login_result = refresh_account_login(account_id, continue_to_scan=True)
                break
            except Exception as exc:
                if attempt == 0 and "has been closed" in str(exc):
                    update_scan_status(True, 5, "登录浏览器意外中断，正在自动重建会话", error=None, loginUrl=None, qrImage=None)
                    continue
                if "has been closed" in str(exc):
                    raise ValueError("登录浏览器会话已中断，请重新点击更新登录") from exc
                raise
        scan_result = scan_pinned_account(account_index, finalize=False)
        update_scan_status(False, 100, f"登录已更新并完成置顶好友扫描，识别到 {len(scan_result['contacts'])} 人", loginUrl=None, qrImage=None, scanResult=scan_result)
        return {"login": login_result, "scan": scan_result}
    finally:
        LOGIN_LOCK.release()


class TaskRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.started_at = None
        self.last_exit_code = None
        self.output = None

    def refresh(self):
        if self.process and self.process.poll() is not None:
            self.last_exit_code = self.process.returncode
            self.process = None
            if self.output:
                self.output.close()
                self.output = None

    def status(self):
        with self.lock:
            self.refresh()
            return {
                "running": self.process is not None,
                "pid": self.process.pid if self.process else None,
                "startedAt": self.started_at,
                "lastExitCode": self.last_exit_code,
            }

    def start(self, account_id=None):
        with self.lock:
            self.refresh()
            if self.process:
                raise ValueError("任务正在运行")
            config = public_config()
            accounts = config["accounts"]
            if account_id:
                accounts = [item for item in accounts if item["uniqueId"] == account_id]
                if not accounts:
                    raise ValueError("指定账号不存在")
            if not accounts or not any(item["targets"] for item in accounts):
                raise ValueError("请先配置至少一个目标好友")
            missing = [item["username"] for item in accounts if not item["cookieConfigured"]]
            if missing:
                raise ValueError("以下账号缺少 Cookie：" + "、".join(missing))
            (ROOT / "logs").mkdir(exist_ok=True)
            self.output = RUN_LOG_FILE.open("ab", buffering=0)
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            if account_id:
                env["RUN_ACCOUNT_ID"] = account_id
            else:
                env.pop("RUN_ACCOUNT_ID", None)
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            executable = str(python if python.exists() else sys.executable)
            self.process = subprocess.Popen(
                [executable, "main.py"], cwd=ROOT, env=env,
                stdout=self.output, stderr=subprocess.STDOUT, creationflags=flags,
            )
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.last_exit_code = None
            return self.status_unlocked()

    def status_unlocked(self):
        return {
            "running": self.process is not None,
            "pid": self.process.pid if self.process else None,
            "startedAt": self.started_at,
            "lastExitCode": self.last_exit_code,
        }

    def stop(self):
        with self.lock:
            self.refresh()
            if not self.process:
                raise ValueError("当前没有运行中的任务")
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.last_exit_code = self.process.returncode
            self.process = None
            if self.output:
                self.output.close()
                self.output = None
            return self.status_unlocked()


RUNNER = TaskRunner()


def tail_logs(limit=200):
    entries = []
    for path, source in ((LOG_FILE, "app"), (RUN_LOG_FILE, "runner")):
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        entries.extend({"source": source, "text": line} for line in lines)
    return entries[-limit:]


class Handler(BaseHTTPRequestHandler):
    server_version = "SparkFlowUI/1.0"

    def log_message(self, _format, *_args):
        return

    def is_local_request(self):
        forwarded = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For")
        return self.client_address[0] in {"127.0.0.1", "::1"} and not forwarded

    def current_user(self):
        if self.is_local_request():
            return {"username": "local-master", "role": "master", "accountIds": [], "expires": float("inf")}
        cookie = self.headers.get("Cookie", "")
        token = next((part.strip().split("=", 1)[1] for part in cookie.split(";") if part.strip().startswith("sparkflow_session=")), "")
        session = SESSIONS.get(token)
        if session and session["expires"] > time.time() and session["role"] != "master":
            return session
        if token:
            SESSIONS.pop(token, None)
        return None

    def allowed_account_ids(self):
        user = self.current_user()
        return None if user and user["role"] == "master" else list(user.get("accountIds", [])) if user else []

    def local_master(self):
        user = self.current_user()
        return bool(user and user["role"] == "master" and self.is_local_request())

    def require_authentication(self):
        if self.current_user():
            return False
        self.json_response({"error": "请先登录网站"}, 401)
        return True

    def json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self.serve_static(parsed.path)
        if parsed.path == "/api/healthz":
            return self.json_response({"ok": True, "service": "sparkflow-web"})
        if parsed.path == "/api/session":
            user = self.current_user()
            if not user:
                return self.json_response({"authenticated": False}, 401)
            return self.json_response({"authenticated": True, "username": user["username"], "role": user["role"], "accountIds": user.get("accountIds", []), "canRegister": self.local_master(), "localAutoLogin": self.local_master()})
        if self.require_authentication():
            return
        if parsed.path == "/api/config":
            return self.json_response(public_config(self.allowed_account_ids()))
        if parsed.path == "/api/status":
            return self.json_response(RUNNER.status())
        if parsed.path == "/api/scan-status":
            status = get_scan_status()
            allowed = self.allowed_account_ids()
            if allowed is not None and status.get("ownerAccountId") not in set(allowed):
                status = {"running": False, "percent": 0, "stage": "等待扫描", "error": None, "loginUrl": None, "qrImage": None, "scanResult": None, "ownerAccountId": None}
            elif status.get("scanResult"):
                visible = public_config(allowed)["accounts"]
                status["scanResult"] = dict(status["scanResult"])
                status["scanResult"]["accountIndex"] = next((i for i, account in enumerate(visible) if account["uniqueId"] == status["scanResult"].get("accountId")), 0)
            return self.json_response(status)
        if parsed.path == "/api/users":
            if not self.local_master():
                return self.json_response({"error": "仅本机主账号可管理网站用户"}, 403)
            return self.json_response({"users": [{"username": item["username"], "role": item["role"], "accountIds": item.get("accountIds", [])} for item in load_web_users(sync=False)["users"] if item["role"] == "account"]})
        if parsed.path == "/api/logs":
            if self.allowed_account_ids() is not None:
                return self.json_response({"entries": []})
            limit = min(1000, max(20, int(parse_qs(parsed.query).get("limit", ["200"])[0])))
            return self.json_response({"entries": tail_logs(limit)})
        return self.json_response({"error": "接口不存在"}, 404)

    def do_POST(self):
        if self.path == "/api/auth/login":
            try:
                payload = self.read_json()
                user = authenticate_web_user(payload.get("username"), payload.get("password"))
                if not user or user["role"] != "account":
                    return self.json_response({"error": "网站账号或密码错误"}, 401)
                token = create_session(user)
                body = json.dumps({"ok": True, "username": user["username"], "role": user["role"]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", f"sparkflow_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(body); return
            except Exception as exc:
                return self.json_response({"error": str(exc)}, 400)
        if self.require_authentication():
            return
        try:
            if self.path == "/api/auth/logout":
                body = b'{"ok":true}'
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "sparkflow_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path == "/api/users":
                if not self.local_master():
                    return self.json_response({"error": "仅本机主账号可注册网站用户"}, 403)
                payload = self.read_json()
                user = upsert_web_user(payload.get("username"), payload.get("password"), "account", [])
                return self.json_response({"ok": True, "user": user, "webUsersSync": sync_web_users_best_effort()})
            if self.path == "/api/users/reset-password":
                if not self.local_master():
                    return self.json_response({"error": "仅本机主账号可修改网站用户密码"}, 403)
                payload = self.read_json()
                user = reset_web_user_password(payload.get("username"), payload.get("password"))
                return self.json_response({"ok": True, "user": user, "webUsersSync": sync_web_users_best_effort()})
            if self.path == "/api/users/delete":
                if not self.local_master():
                    return self.json_response({"error": "仅本机主账号可删除网站用户"}, 403)
                user = delete_web_user(self.read_json().get("username"))
                return self.json_response({"ok": True, "user": user, "webUsersSync": sync_web_users_best_effort()})
            if self.path == "/api/login-code":
                submit_login_code(self.read_json().get("code"))
                return self.json_response({"ok": True})
            if self.path == "/api/config":
                payload = self.read_json()
                user = self.current_user()
                if user["role"] == "account" and not user.get("accountIds"):
                    config = provision_first_account(payload, user)
                else:
                    config = save_scoped_config(payload, self.allowed_account_ids())
                return self.json_response({"ok": True, "config": config})
            if self.path == "/api/github/sync":
                return self.json_response({"ok": True, "result": sync_github(self.allowed_account_ids())})
            if self.path == "/api/scan-pinned":
                payload = self.read_json()
                visible = public_config(self.allowed_account_ids())["accounts"]
                account_id = str(payload.get("accountId") or visible[int(payload.get("accountIndex"))]["uniqueId"])
                allowed = self.allowed_account_ids()
                if allowed is not None and account_id not in allowed:
                    return self.json_response({"error": "无权扫描该账号"}, 403)
                full = public_config()["accounts"]
                index = next(i for i, account in enumerate(full) if account["uniqueId"] == account_id)
                result = scan_pinned_account(index)
                result["accountIndex"] = next(i for i, account in enumerate(visible) if account["uniqueId"] == account_id)
                return self.json_response({"ok": True, "result": result})
            if self.path == "/api/scan-result/clear":
                status = get_scan_status(); allowed = self.allowed_account_ids()
                if allowed is not None and status.get("ownerAccountId") not in allowed:
                    return self.json_response({"error": "无权清除该账号扫描结果"}, 403)
                clear_scan_result()
                return self.json_response({"ok": True})
            if self.path == "/api/account-login-refresh":
                payload = self.read_json()
                account_id = str(payload.get("accountId", ""))
                allowed = self.allowed_account_ids()
                if allowed is not None and account_id not in allowed:
                    return self.json_response({"error": "无权更新该账号登录"}, 403)
                return self.json_response({"ok": True, "result": refresh_login_and_scan(account_id)})
            if self.path == "/api/run":
                if self.allowed_account_ids() is not None:
                    return self.json_response({"error": "独立账号只能运行自己的账号"}, 403)
                return self.json_response({"ok": True, "status": RUNNER.start()})
            if self.path == "/api/run-account":
                payload = self.read_json()
                account_id = str(payload.get("accountId", "")).strip()
                allowed = self.allowed_account_ids()
                if allowed is not None and account_id not in allowed:
                    return self.json_response({"error": "无权运行该账号"}, 403)
                return self.json_response({"ok": True, "status": RUNNER.start(account_id)})
            if self.path == "/api/stop":
                if self.allowed_account_ids() is not None:
                    return self.json_response({"error": "独立账号无权停止全局任务"}, 403)
                return self.json_response({"ok": True, "status": RUNNER.stop()})
            self.json_response({"error": "接口不存在"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception as exc:
            self.json_response({"error": f"服务器错误：{exc}"}, 500)

    def serve_static(self, path):
        if path in ("", "/"):
            target = WEB_ROOT / "index.html"
        elif path == "/assets/cover.png":
            target = ROOT / "docs" / "images" / "cover.png"
        else:
            target = (WEB_ROOT / path.lstrip("/")).resolve()
            if WEB_ROOT.resolve() not in target.parents:
                return self.send_error(403)
        if not target.exists() or not target.is_file():
            return self.send_error(404)
        mime = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".png": "image/png"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="DouYin Spark Flow local dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"DouYin Spark Flow UI: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
