import os, sys
from enum import Enum
import json
import logging
from utils.logger import setup_logger
from utils import norm

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True
config = None
userData = None


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"  # GitHub Action 运行
    LOCAL = "LOCAL"  # 本地代码运行
    PACKED = "PACKED"  # PyInstaller 打包运行

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    elif os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    else:
        return Environment.LOCAL


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv(
            "MESSAGE_TEMPLATE",
            "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]",
        ),
        "hitokotoTypes": json.loads(
            os.getenv("HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]')
        ),
        "matchMode": os.getenv("MATCH_MODE", "nickname"),  # 是否使用短 ID 进行好友匹配
        "browserTimeout": int(
            os.getenv("BROWSER_TIMEOUT", "120000")
        ),  # 浏览器操作超时时间，单位毫秒
        "friendListTimeout": int(
            os.getenv("FRIEND_LIST_WAIT_TIME", "2000")
        ),  # 好友列表加载超时时间，单位毫秒
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "logLevel": os.getenv("LOG_LEVEL", "DEBUG"),  # 日志级别
    }

    return config


def sanitize_cookies(cookies):
    for cookie in cookies:
        if "sameSite" in cookie:
            cookie.pop("sameSite")  # 移除 sameSite 字段，Playwright 可能不支持该字段
    return cookies


def normalize_targets(raw_targets):
    """兼容旧字符串目标，并为抖音号目标保留持久化别名。"""
    targets = []
    aliases = {}
    for entry in raw_targets:
        if isinstance(entry, str):
            target_id = norm(entry)
            target_aliases = []
        elif isinstance(entry, dict):
            target_id = norm(
                entry.get("id") or entry.get("unique_id") or entry.get("short_id")
            )
            target_aliases = list(entry.get("aliases") or [])
            target_aliases += [entry.get("nickname"), entry.get("remark_name")]
        else:
            continue

        if not target_id:
            continue
        targets.append(target_id)
        aliases[target_id] = list(
            dict.fromkeys(norm(alias) for alias in target_aliases if alias and norm(alias))
        )
    return targets, aliases


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    tasks = json.loads(os.getenv("TASKS", "[]"))
    run_account_id = norm(os.getenv("RUN_ACCOUNT_ID", ""))
    if run_account_id:
        tasks = [task for task in tasks if norm(task.get("unique_id")) == run_account_id]
        if not tasks:
            raise ValueError(f"未找到指定运行账号: {run_account_id}")
        logger.info(f"本次仅运行指定账号: {run_account_id}")

    userData = []

    for task in tasks:
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        if not unique_id:
            logger.warning(f"{username} 的任务  缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = (
            os.getenv(cookies_key, "").encode("utf-8").decode("unicode_escape")
        )
        if not cookies_str:
            logger.warning(f"{username} 的任务 缺少 {cookies_key} 环境变量，已跳过")
            continue
        try:
            cookies = json.loads(cookies_str)
        except json.JSONDecodeError:
            logger.warning(f"{username} 的任务 {cookies_key} 格式不正确，已跳过")
            continue

        targets, target_aliases = normalize_targets(task.get("targets", []))
        userData.append(
            {
                "unique_id": unique_id,
                "username": username,
                "cookies": sanitize_cookies(cookies),
                "targets": targets,
                "target_aliases": target_aliases,
            }
        )

    return userData
