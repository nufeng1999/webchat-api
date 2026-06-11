import os
import sys
import json
import asyncio
import logging

from config import CONFIG, CONFIG_PATH, BASE_DIR, USER_AGENT

logger = logging.getLogger("qianwen-login")

QIANWEN_STORAGE_STATE_PATH = os.path.join(BASE_DIR, "qianwen_storage_state.json")


async def do_qianwen_login(show_browser: bool = True) -> dict:
    """
    启动浏览器让用户登录千问 (qianwen.com)，
    登录成功后提取 cookie 保存到 qianwen_storage_state.json 和 config.json。

    Returns:
        {"success": True/False, "cookie": ..., "message": ...}
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "message": "playwright not installed"}

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=not show_browser,
            channel="msedge",
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )

        # Load existing storage_state if available
        if os.path.exists(QIANWEN_STORAGE_STATE_PATH):
            try:
                await context.close()
                context = await browser.new_context(
                    storage_state=QIANWEN_STORAGE_STATE_PATH,
                    viewport={"width": 1280, "height": 900},
                    user_agent=USER_AGENT,
                )
                logger.info("Loaded existing qianwen storage_state")
            except Exception as e:
                logger.warning(f"Failed to load qianwen storage_state: {e}")
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=USER_AGENT,
                )
        else:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=USER_AGENT,
            )

        page = await context.new_page()

        logger.info("Navigating to qianwen.com ...")
        await page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
        await asyncio.sleep(3)

        async def _is_logged_in() -> bool:
            """检测千问是否已真正登录（非访客模式）。
            以页面 DOM 为准：有"登录"按钮=未登录，无=已登录。
            """
            try:
                body_text = await page.text_content("body") or ""

                # 明确的未登录标志
                for keyword in ["手机号登录", "扫码登录", "账号登录", "登录/注册"]:
                    if keyword in body_text:
                        return False

                # 明确的已登录标志：用户头像、用户名、退出按钮
                for keyword in ["退出登录", "我的账户", "个人中心"]:
                    if keyword in body_text:
                        return True

                # 检查用户头像元素
                user_el = await page.query_selector(
                    '[class*="avatar"] img, [class*="user-info"], [class*="user-name"], [class*="profile"]'
                )
                if user_el:
                    return True

                # 有 chat input 但有登录按钮 = 访客，不算登录
                has_login_btn = await page.query_selector('button:has-text("登录"), a:has-text("登录")')
                if has_login_btn:
                    return False

                # 有 chat input 且没登录按钮 = 可能已登录
                has_input = await page.query_selector('[contenteditable]')
                if has_input and "登录" not in body_text:
                    return True

                return False
            except Exception as e:
                logger.warning(f"Qianwen: login check error: {e}")
                return False

        if await _is_logged_in():
            logger.info("Qianwen already logged in")
            try:
                await context.storage_state(path=QIANWEN_STORAGE_STATE_PATH)
                logger.info(f"Storage state refreshed: {QIANWEN_STORAGE_STATE_PATH}")
            except Exception as e:
                logger.warning(f"Failed to refresh storage_state: {e}")
            cookies = await context.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            config = CONFIG.copy()
            config["qianwen_cookie"] = cookie_str
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            logger.info("Qianwen: login state saved")
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

            # Poll for up to 5 minutes
            login_ok = False
            for _ in range(600):
                await asyncio.sleep(0.5)
                if not browser.is_connected():
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

            # Visit passport page briefly to ensure its cookies are captured in storage_state
            try:
                passport_page = await context.new_page()
                await passport_page.goto("https://passport.qianwen.com/", wait_until="load", timeout=15000)
                await asyncio.sleep(1)
                await passport_page.close()
                logger.info("Visited passport.qianwen.com to capture login cookies")
            except Exception as e:
                logger.warning(f"Failed to visit passport page: {e}")

            # Extract all cookies from all domains
            cookies = await context.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            # Log cookie domains for debugging
            domains = set(c.get("domain", "") for c in cookies)
            logger.info(f"Qianwen: captured cookies from domains: {domains}")

            # Save storage_state
            try:
                await context.storage_state(path=QIANWEN_STORAGE_STATE_PATH)
                logger.info(f"Storage state saved to {QIANWEN_STORAGE_STATE_PATH}")
            except Exception as e:
                logger.warning(f"Failed to save storage_state: {e}")

            # Update config.json
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

        # Wait for browser close
        while True:
            await asyncio.sleep(0.5)
            if not browser.is_connected():
                break
            try:
                if not context.pages:
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
