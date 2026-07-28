import os

from core.browser import get_browser
from core.tasks import (
    WINDOWS_CHROME_USER_AGENT,
    config,
    open_chat_page,
    save_failure_screenshot,
)
from utils.config import get_userData
from utils.logger import setup_logger


logger = setup_logger(level=config.get("logLevel", "Info"))


def check_cookies():
    playwright, browser = get_browser()
    try:
        for user in get_userData():
            username = user.get("username", "未知用户")
            context = browser.new_context(
                user_agent=WINDOWS_CHROME_USER_AGENT,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
            context.set_default_navigation_timeout(config["browserTimeout"])
            context.set_default_timeout(config["browserTimeout"])
            page = context.new_page()
            try:
                context.add_cookies(user["cookies"])
                open_chat_page(page)
                logger.info(f"账号 {username} Cookie 登录状态正常")
            except Exception:
                save_failure_screenshot(page, "cookie-check-failure")
                raise
            finally:
                context.close()
    finally:
        browser.close()
        playwright.stop()


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    check_cookies()
