import argparse
import json
import os
import subprocess
import sys
import threading
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
            "targets": task.get("targets", []),
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
        "logLevel": env.get("LOG_LEVEL", "Info"),
        "accounts": accounts,
    }


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
        "LOG_LEVEL": payload.get("logLevel", "Info"),
    }

    for account in accounts:
        username = str(account.get("username", "")).strip()
        unique_id = str(account.get("uniqueId", "")).strip()
        targets = [str(item).strip() for item in account.get("targets", []) if str(item).strip()]
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
