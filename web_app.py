import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import base64
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
ENV_FILE = ROOT / ".env"
LOG_FILE = ROOT / "logs" / "app.log"
RUN_LOG_FILE = ROOT / "logs" / "web-run.log"
ENV_LOCK = threading.Lock()
GITHUB_REPOSITORY = os.getenv("SPARKFLOW_GITHUB_REPOSITORY", "jehfjfk/DouYinSparkFlow")
GITHUB_ENVIRONMENT = os.getenv("SPARKFLOW_GITHUB_ENVIRONMENT", "user-data")


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


def write_env(updates):
    with ENV_LOCK:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
        pending = dict(updates)
        output = []
        for line in lines:
            stripped = line.lstrip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
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


def public_config():
    env = read_env()
    tasks = parse_json(env.get("TASKS", "[]"), [])
    accounts = []
    for task in tasks:
        unique_id = str(task.get("unique_id", ""))
        cookie_key = f"COOKIES_{unique_id.upper()}"
        cookies = parse_json(env.get(cookie_key, "[]"), [])
        accounts.append({
            "username": task.get("username", ""),
            "uniqueId": unique_id,
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
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("至少需要一个账号")

    current = read_env()
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
        targets = normalize_public_targets(account.get("targets", []))
        if not username or not unique_id:
            raise ValueError("用户名和抖音号不能为空")
        tasks.append({"username": username, "unique_id": unique_id, "targets": targets})
        cookie_raw = account.get("cookies")
        if cookie_raw:
            cookies = validate_cookie_json(cookie_raw)
            updates[f"COOKIES_{unique_id.upper()}"] = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))

    updates["TASKS"] = json.dumps(tasks, ensure_ascii=False, separators=(",", ":"))
    write_env(updates)
    return public_config()


def validate_schedule_time(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("执行时间必须是 HH:MM 格式")
    return value


def schedule_cron(value):
    hour, minute = map(int, validate_schedule_time(value).split(":"))
    return f"{minute} {(hour - 8) % 24} * * *"


def github_token():
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
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


def encrypted_secret(value, public_key):
    try:
        from nacl import encoding, public
    except ImportError as exc:
        raise ValueError("缺少 PyNaCl 依赖，请运行 pip install -r requirements.txt") from exc
    key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    encrypted = public.SealedBox(key).encrypt(value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def sync_github():
    config = public_config()
    if not config["accounts"]:
        raise ValueError("请先保存至少一个账号")
    env = read_env()
    token = github_token()
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    base = f"/repos/{owner}/{repo}/environments/{GITHUB_ENVIRONMENT}"
    variable_names = ["TASKS", "MESSAGE_TEMPLATE", "HITOKOTO_TYPES", "MATCH_MODE", "BROWSER_TIMEOUT", "FRIEND_LIST_WAIT_TIME", "TASK_RETRY_TIMES", "LOG_LEVEL", "PROXY_ADDRESS"]
    for name in variable_names:
        value = env.get(name, "")
        try:
            github_request("PATCH", f"{base}/variables/{name}", token, {"name": name, "value": value})
        except ValueError as exc:
            if "HTTP 404" not in str(exc):
                raise
            github_request("POST", f"{base}/variables", token, {"name": name, "value": value})

    key_info = github_request("GET", f"{base}/secrets/public-key", token)
    active_secrets = set()
    for account in config["accounts"]:
        name = f"COOKIES_{account['uniqueId'].upper()}"
        value = env.get(name)
        if not value:
            raise ValueError(f"账号 {account['username']} 缺少 Cookie")
        github_request("PUT", f"{base}/secrets/{name}", token, {"encrypted_value": encrypted_secret(value, key_info["key"]), "key_id": key_info["key_id"]})
        active_secrets.add(name)

    existing = github_request("GET", f"{base}/secrets?per_page=100", token).get("secrets", [])
    for secret in existing:
        name = secret.get("name", "")
        if name.startswith("COOKIES_") and name not in active_secrets:
            github_request("DELETE", f"{base}/secrets/{name}", token)
    return {"repository": GITHUB_REPOSITORY, "accounts": len(config["accounts"]), "secrets": len(active_secrets)}


def scan_pinned_account(account_index):
    """Read-only scan of pinned contacts using the selected account Cookie."""
    from core.browser import get_browser
    from core import tasks as task_core
    from utils.config import sanitize_cookies

    config = public_config()
    try:
        account = config["accounts"][int(account_index)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("账号不存在") from exc
    cookies = parse_json(read_env().get(f"COOKIES_{account['uniqueId'].upper()}", "[]"), [])
    if not cookies:
        raise ValueError(f"账号 {account['username']} 尚未配置 Cookie")

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
        context.add_cookies(sanitize_cookies(cookies))
        page.goto("https://creator.douyin.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        task_core.open_chat_page(page)
        page.wait_for_timeout(max(1000, config["friendListWaitTime"]))
        items = page.locator(task_core.CONVERSATION_LIST_SELECTOR).locator(task_core.CONVERSATION_ITEM_SELECTOR).all()
        def is_pinned_item(item):
            """Douyin renders the pin marker in different nested nodes across releases."""
            marker = item.evaluate("""element => {
                const values = [];
                for (const node of [element, ...element.querySelectorAll('*')]) {
                    values.push(node.className || '', node.id || '', node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '', node.getAttribute('data-e2e') || '',
                        node.getAttribute('data-testid') || '');
                }
                return values.join(' ').toLowerCase();
            }""")
            return "置顶" in marker or "pinned" in marker or "pin" in marker

        for item in items:
            try:
                title = task_core.norm(item.locator(task_core.CONVERSATION_TITLE_SELECTOR).inner_text())
                if not title or not is_pinned_item(item):
                    continue
                item.click()
                page.wait_for_timeout(250)
                identity = task_core.userIDDict.get(title, [])
                results.append({"nickname": title, "remark": identity[4] if len(identity) > 4 else title, "shortId": identity[0] if identity else "", "uniqueId": identity[1] if len(identity) > 1 else "", "pinned": True})
            except Exception:
                continue
        return {"accountIndex": int(account_index), "account": account["username"], "contacts": results, "readOnly": True, "message": f"仅识别到 {len(results)} 个置顶会话"}
    finally:
        context.close()
        browser.close()
        playwright.stop()


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

    def start(self):
        with self.lock:
            self.refresh()
            if self.process:
                raise ValueError("任务正在运行")
            config = public_config()
            if not config["accounts"] or not any(item["targets"] for item in config["accounts"]):
                raise ValueError("请先配置至少一个目标好友")
            missing = [item["username"] for item in config["accounts"] if not item["cookieConfigured"]]
            if missing:
                raise ValueError("以下账号缺少 Cookie：" + "、".join(missing))
            (ROOT / "logs").mkdir(exist_ok=True)
            self.output = RUN_LOG_FILE.open("ab", buffering=0)
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
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
        if parsed.path == "/api/config":
            return self.json_response(public_config())
        if parsed.path == "/api/status":
            return self.json_response(RUNNER.status())
        if parsed.path == "/api/logs":
            limit = min(1000, max(20, int(parse_qs(parsed.query).get("limit", ["200"])[0])))
            return self.json_response({"entries": tail_logs(limit)})
        return self.serve_static(parsed.path)

    def do_POST(self):
        try:
            if self.path == "/api/config":
                return self.json_response({"ok": True, "config": save_config(self.read_json())})
            if self.path == "/api/github/sync":
                return self.json_response({"ok": True, "result": sync_github()})
            if self.path == "/api/scan-pinned":
                payload = self.read_json()
                return self.json_response({"ok": True, "result": scan_pinned_account(payload.get("accountIndex"))})
            if self.path == "/api/run":
                return self.json_response({"ok": True, "status": RUNNER.start()})
            if self.path == "/api/stop":
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
