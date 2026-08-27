import asyncio
import sys
import os
import logging

from config import CONFIG, BASE_DIR

logger = logging.getLogger("kimi-login")

USER_DATA_DIR = os.path.join(BASE_DIR, "profiles", "kimi_profile")


async def login_and_save(show_browser: bool = True) -> dict:
    """
    启动持久化浏览器让用户登录 Kimi，登录成功后会话状态保存到 kimi_profile 目录。
    使用 launch_persistent_context 保持登录状态。

    Args:
        show_browser: 是否显示浏览器窗口（headless=False）

    Returns:
        {"success": True/False, "message": ...}
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed. Run: pip install playwright && playwright install chromium")
        return {"success": False, "message": "playwright not installed"}

    if not os.path.exists(USER_DATA_DIR):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        logger.info(f"Created user data directory: {USER_DATA_DIR}")

    launch_args = CONFIG.get("_browser_launch_args") if isinstance(CONFIG.get("_browser_launch_args"), list) else ["--no-sandbox", "--disable-setuid-sandbox"]
    if not isinstance(CONFIG.get("_browser_launch_args"), list) and not sys.platform.startswith("win"):
        launch_args.extend([
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-zygote",
            "--disable-software-rasterizer",
            "--disable-blink-features=AutomationControlled",
        ])

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=not show_browser,
            channel=CONFIG.get("_browser_channel") if CONFIG.get("_browser_channel") is not None else ("msedge" if sys.platform.startswith("win") else None),
            args=launch_args,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            ignore_default_args=["--enable-automation"],
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        logger.info("Navigating to kimi.com/zh ...")
        await page.goto("https://www.kimi.com/zh", wait_until="load", timeout=60000)
        await asyncio.sleep(2)

        async def _is_logged_in() -> bool:
            # 已登录标志：聊天编辑器可见，且存在 kimi-auth cookie
            try:
                has_editor = await page.evaluate("() => !!document.querySelector('.chat-input-editor')")
                if has_editor:
                    return True
            except Exception:
                pass
            try:
                cks = await browser.cookies()
                if any(c.get("name") == "kimi-auth" for c in cks if c.get("value")):
                    return True
            except Exception:
                pass
            return False

        if await _is_logged_in():
            logger.info("Already logged in (valid session found)")
            await asyncio.sleep(2)
        else:
            logger.info("Login required. Please log in manually in the browser window.")
            if show_browser:
                print("=" * 50)
                print("请在浏览器中登录 Kimi 账号")
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
                except Exception:
                    pass
                await pw.stop()
                return {"success": False, "message": "登录超时（5分钟），请重试"}

            await asyncio.sleep(3)
            logger.info("Login detected, session saved to profile directory")

        logger.info("Login successful. Session saved to kimi_profile directory.")
        print("=" * 50)
        print("登录成功！会话状态已保存到 kimi_profile 目录。")
        print("=" * 50)
        print("请手动关闭浏览器窗口以退出程序...")

        # 轮询检测浏览器是否被用户关闭
        for _ in range(1200):
            await asyncio.sleep(0.5)
            if not browser.pages:
                break

        return {"success": True, "message": "Kimi login completed"}
    except Exception as e:
        logger.error(f"Kimi login error: {e}")
        return {"success": False, "message": str(e)}
    finally:
        try:
            if browser and browser.pages:
                await browser.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass


if __name__ == '__main__':
    asyncio.run(login_and_save(show_browser=True))
