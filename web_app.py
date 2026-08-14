import argparse
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
import time
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
SCAN_LOCK = threading.Lock()
LOGIN_LOCK = threading.Lock()
SCAN_STATUS = {"running": False, "percent": 0, "stage": "等待扫描", "error": None, "loginUrl": None, "qrImage": None, "scanResult": None}
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
    variables = github_request("GET", f"{base}/variables?per_page=100", token).get("variables", [])
    remote_values = {item["name"]: item["value"] for item in variables}
    local_tasks = parse_json(env.get("TASKS", "[]"), [])
    remote_tasks = parse_json(remote_values.get("TASKS", "[]"), [])
    env["TASKS"] = json.dumps(merge_tasks(remote_tasks, local_tasks), ensure_ascii=False, separators=(",", ":"))
    variable_names = ["TASKS", "MESSAGE_TEMPLATE", "HITOKOTO_TYPES", "MATCH_MODE", "BROWSER_TIMEOUT", "FRIEND_LIST_WAIT_TIME", "TASK_RETRY_TIMES", "LOG_LEVEL", "PROXY_ADDRESS"]
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

    key_info = github_request("GET", f"{base}/secrets/public-key", token)
    active_secrets = set()
    for account in config["accounts"]:
        name = f"COOKIES_{account['uniqueId'].upper()}"
        value = env.get(name)
        if not value:
            raise ValueError(f"账号 {account['username']} 缺少 Cookie")
        github_request("PUT", f"{base}/secrets/{name}", token, {"encrypted_value": encrypted_secret(value, key_info["key"]), "key_id": key_info["key_id"]})
        active_secrets.add(name)

    return {"repository": GITHUB_REPOSITORY, "accounts": len(parse_json(env["TASKS"], [])), "secrets": len(active_secrets)}


def scan_pinned_account(account_index, finalize=True):
    """Read-only scan of pinned contacts using the selected account Cookie."""
    from core.browser import get_browser
    from core import tasks as task_core
    from utils.config import sanitize_cookies

    update_scan_status(True, 5, "正在读取账号配置", loginUrl=None, qrImage=None, scanResult=None)
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
            marker = item.evaluate("""element => {
                const values = [];
                const nodes = [element, ...element.querySelectorAll('*')];
                let parent = element.parentElement;
                for (let depth = 0; parent && depth < 5; depth++, parent = parent.parentElement) nodes.push(parent);
                for (const node of nodes) {
                    values.push(node.className || '', node.id || '', node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '', node.getAttribute('data-e2e') || '',
                        node.getAttribute('data-testid') || '', node.getAttribute('data-test') || '',
                        node.getAttribute('data-type') || '', node.getAttribute('data-status') || '');
                }
                let sibling = element.previousElementSibling;
                for (let i = 0; sibling && i < 3; i++, sibling = sibling.previousElementSibling) values.push(sibling.textContent || '', sibling.className || '');
                return values.join(' ').toLowerCase();
            }""")
            return ("置顶" in marker or "pinned" in marker or "pin" in marker or
                    "stick" in marker or "top-contact" in marker)

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
        return {"accountIndex": int(account_index), "account": account["username"], "contacts": results, "readOnly": True, "message": f"仅识别到 {len(results)} 个置顶会话"}
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


def refresh_account_login(account_id, continue_to_scan=False):
    """Open an interactive Douyin login and persist the resulting Cookie for one account."""
    from core.browser import get_browser
    from core import tasks as task_core

    account_id = str(account_id or "").strip()
    account = next((item for item in public_config()["accounts"] if item["uniqueId"] == account_id), None)
    if not account:
        raise ValueError("指定账号不存在")
    update_scan_status(True, 5, "正在生成抖音登录链接", loginUrl=None, qrImage=None, scanResult=None)
    playwright, browser = get_browser()
    context = browser.new_context(user_agent=task_core.WINDOWS_CHROME_USER_AGENT, locale="zh-CN", timezone_id="Asia/Shanghai")
    page = context.new_page()
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
                    update_scan_status(True, 35, "已扫码：请在电脑身份验证弹窗中选择短信、刷脸或密码", qrImage=None)
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
        token = github_token()
        owner, repo = GITHUB_REPOSITORY.split("/", 1)
        base = f"/repos/{owner}/{repo}/environments/{GITHUB_ENVIRONMENT}"
        key_info = github_request("GET", f"{base}/secrets/public-key", token)
        github_request("PUT", f"{base}/secrets/COOKIES_{account_id.upper()}", token, {
            "encrypted_value": encrypted_secret(value, key_info["key"]), "key_id": key_info["key_id"],
        })
        update_scan_status(continue_to_scan, 80 if continue_to_scan else 100, "登录已更新，正在启动置顶好友扫描", loginUrl=None, qrImage=None)
        return {"accountId": account_id, "cookieCount": len(cookies), "updated": True}
    except Exception as exc:
        update_scan_status(False, get_scan_status()["percent"], "登录更新失败", str(exc), loginUrl=None, qrImage=None)
        raise
    finally:
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

    def authenticated(self):
        password = read_env().get("WEB_ACCESS_PASSWORD", "").strip()
        if not password:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = base64.b64encode(f"sparkflow:{password}".encode("utf-8")).decode("ascii")
        return supplied.startswith("Basic ") and hmac.compare_digest(supplied[6:], expected)

    def require_authentication(self):
        if self.authenticated():
            return False
        body = "需要输入网站访问账号和密码".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="DouYin Spark Flow", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
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
        if self.require_authentication():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            return self.json_response(public_config())
        if parsed.path == "/api/status":
            return self.json_response(RUNNER.status())
        if parsed.path == "/api/scan-status":
            return self.json_response(get_scan_status())
        if parsed.path == "/api/logs":
            limit = min(1000, max(20, int(parse_qs(parsed.query).get("limit", ["200"])[0])))
            return self.json_response({"entries": tail_logs(limit)})
        return self.serve_static(parsed.path)

    def do_POST(self):
        if self.require_authentication():
            return
        try:
            if self.path == "/api/config":
                return self.json_response({"ok": True, "config": save_config(self.read_json())})
            if self.path == "/api/github/sync":
                return self.json_response({"ok": True, "result": sync_github()})
            if self.path == "/api/scan-pinned":
                payload = self.read_json()
                return self.json_response({"ok": True, "result": scan_pinned_account(payload.get("accountIndex"))})
            if self.path == "/api/account-login-refresh":
                payload = self.read_json()
                return self.json_response({"ok": True, "result": refresh_login_and_scan(payload.get("accountId"))})
            if self.path == "/api/run":
                return self.json_response({"ok": True, "status": RUNNER.start()})
            if self.path == "/api/run-account":
                payload = self.read_json()
                return self.json_response({"ok": True, "status": RUNNER.start(str(payload.get("accountId", "")).strip())})
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
