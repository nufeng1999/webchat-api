import os
import sys
import json
import asyncio
import logging

from config import CONFIG, CONFIG_PATH, BASE_DIR, USER_AGENT

logger = logging.getLogger("qianwen-login")

USER_DATA_DIR = os.path.join(BASE_DIR, "qianwen_profile")


async def do_qianwen_login(show_browser: bool = True) -> dict:
    """
    启动持久化浏览器让用户登录千问 (qianwen.com)，
    登录成功后提取 cookie 保存到 config.json。
    使用 launch_persistent_context 保持登录状态到 qianwen_profile 目录。

    Returns:
        {"success": True/False, "cookie": ..., "message": ...}
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "message": "playwright not installed"}

    if not os.path.exists(USER_DATA_DIR):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        logger.info(f"Created user data directory: {USER_DATA_DIR}")

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=not show_browser,
            channel="msedge",
            args=["--no-sandbox", "--disable-setuid-sandbox"],
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()

        logger.info("Navigating to qianwen.com ...")
        await page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
        await asyncio.sleep(3)

        async def _is_logged_in() -> bool:
            """检测千问是否已真正登录（非访客模式）。以页面 DOM 为准。"""
            try:
                body_text = await page.text_content("body") or ""

                # 明确的未登录标志
                for keyword in ["手机号登录", "扫码登录", "账号登录", "登录/注册"]:
                    if keyword in body_text:
                        return False

                # 明确的已登录标志
                for keyword in ["退出登录", "我的账户", "个人中心"]:
                    if keyword in body_text:
                        return True

                # 检查用户头像元素
                user_el = await page.query_selector(
                    '[class*="avatar"] img, [class*="user-info"], [class*="user-name"], [class*="profile"]'
                )
                if user_el:
                    return True

                # 有 chat input 且没登录按钮 = 可能已登录
                has_login_btn = await page.query_selector('button:has-text("登录"), a:has-text("登录")')
                if has_login_btn:
                    return False

                has_input = await page.query_selector('[contenteditable]')
                if has_input and "登录" not in body_text:
                    return True

                return False
            except Exception as e:
                logger.warning(f"Qianwen: login check error: {e}")
                return False

        if await _is_logged_in():
            logger.info("Qianwen already logged in")
            cookies = await browser.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            config = CONFIG.copy()
            config["qianwen_cookie"] = cookie_str
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            logger.info("Qianwen: login state saved to config")
            try:
                await browser.close()
            except Exception:
                pass
            await pw.stop()
            return {"success": True}
        else:
            logger.info("Login required. Please log in manually in the browser window.")
            if show_browser:
                print("=" * 50)
                print("请在浏览器中登录千问 (qianwen.com)")
                print("登录成功后程序将自动继续")
                print("=" * 50)

            login_ok = False
            for _ in range(600):
                await asyncio.sleep(0.5)
                if not browser.pages:
                    await pw.stop()
                    return {"success": False, "message": "浏览器被关闭，登录取消"}
                try:
                    if await _is_logged_in():
                        login_ok = True
                        break
                except Exception:
                    pass

            if not login_ok:
                try:
                    await browser.close()
                except:
                    pass
                await pw.stop()
                return {"success": False, "message": "登录超时（5分钟），请重试"}

            await asyncio.sleep(3)
            logger.info("Login detected, extracting credentials...")

            try:
                passport_page = await browser.new_page()
                await passport_page.goto("https://passport.qianwen.com/", wait_until="load", timeout=15000)
                await asyncio.sleep(1)
                await passport_page.close()
                logger.info("Visited passport.qianwen.com to capture login cookies")
            except Exception as e:
                logger.warning(f"Failed to visit passport page: {e}")

            cookies = await browser.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            domains = set(c.get("domain", "") for c in cookies)
            logger.info(f"Qianwen: captured cookies from domains: {domains}")

            config = CONFIG.copy()
            config["qianwen_cookie"] = cookie_str
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)

            logger.info(f"Login successful. Cookie length: {len(cookie_str)}")
            print("=" * 50)
            print("千问登录成功！配置已保存")
            print(f"Cookie 长度: {len(cookie_str)} 字符")
            print(f"Cookie 域: {', '.join(domains)}")
            print("=" * 50)

        while True:
            await asyncio.sleep(0.5)
            try:
                if not browser.pages:
                    break
            except:
                break

        try:
            await pw.stop()
        except:
            pass

        return {"success": True, "cookie": cookie_str, "message": "千问登录成功"}

    except Exception as e:
        logger.error(f"Qianwen login flow error: {e}")
        if browser:
            try:
                await browser.close()
            except:
                pass
        if pw:
            try:
                await pw.stop()
            except:
                pass
        return {"success": False, "message": str(e)}
