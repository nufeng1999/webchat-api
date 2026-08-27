import asyncio
import sys
import os
import json
from playwright.async_api import async_playwright
from config import CONFIG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "profiles", "zai_profile")
TOKEN_BACKUP_PATH = os.path.join(BASE_DIR, "zai_token_backup.json")

def _get_user_agent():
    if sys.platform.startswith("win"):
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    elif sys.platform == "darwin":
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    else:
        return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

async def login_and_save():
    if not os.path.exists(USER_DATA_DIR):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        print(f"Created user data directory: {USER_DATA_DIR}")

    async with async_playwright() as p:
        from browser_client import _browser_channel
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"],
            user_agent=_get_user_agent(),
            viewport={'width': 1280, 'height': 900},
            ignore_default_args=["--enable-automation"],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        # 反检测
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        print("Opening z.ai login page...")
        await page.goto('https://chat.z.ai/', wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(3)

        # 强制清空 localStorage.token，避免残留误判
        await page.evaluate("localStorage.removeItem('token'); localStorage.setItem('token', '');")
        await asyncio.sleep(1)

        url = page.url
        print(f"Current URL: {url}")

        async def has_login_elements() -> bool:
            """检测页面上是否有登录/验证相关元素，如果没有则视为已登录"""
            try:
                body_text = await page.text_content("body") or ""
                lower = body_text.lower()
                # 未登录典型关键词
                if any(kw in lower for kw in ["登录", "扫码登录", "手机号登录", "sign in", "log in"]):
                    return True
                # QR code / 验证元素
                if any(kw in lower for kw in ["qr", "qrcode", "scan me", "verification", "验证"]):
                    return True
                # 检查是否有明显的登录输入框
                login_input = await page.query_selector('input[type="email"], input[placeholder*="邮箱"], input[placeholder*="手机"]')
                if login_input:
                    return True
                return False
            except Exception:
                return False

        # 如果跳转到 /auth，说明需要登录
        if '/auth' in url:
            print("Detected /auth page, waiting for manual login...")
            for i in range(180):
                await asyncio.sleep(1)
                try:
                    current_url = page.url
                    if i % 15 == 0:
                        print(f"  [{i}s] URL: {current_url}")
                    if '/auth' not in current_url:
                        token = await page.evaluate("localStorage.getItem('token')")
                        if token and len(token) > 50:
                            print(f"Login detected! Token found at {i}s")
                            await asyncio.sleep(3)
                            try:
                                with open(TOKEN_BACKUP_PATH, 'w', encoding='utf-8') as f:
                                    json.dump({"token": token}, f, ensure_ascii=False)
                                print(f"Token saved to {TOKEN_BACKUP_PATH}")
                            except Exception as e:
                                print(f"Failed to save token backup: {e}")
                            break
                except Exception:
                    pass
            else:
                print("Timeout - please try again")
                await browser.close()
                return
        else:
            # 检测页面是否真的已登录，而不仅仅是 token 存在
            need_login = await has_login_elements()
            if need_login:
                print("Login required (page shows login UI). Please login manually.")
                print("Waiting up to 180s...")
                for i in range(180):
                    await asyncio.sleep(1)
                    try:
                        # 再次检测是否已登录
                        still_need = await has_login_elements()
                        if not still_need:
                            token = await page.evaluate("localStorage.getItem('token')")
                            if token and len(token) > 50:
                                print(f"Login completed! Token found at {i}s")
                                try:
                                    with open(TOKEN_BACKUP_PATH, 'w', encoding='utf-8') as f:
                                        json.dump({"token": token}, f, ensure_ascii=False)
                                    print(f"Token saved to {TOKEN_BACKUP_PATH}")
                                except Exception as e:
                                    print(f"Failed to save token backup: {e}")
                                break
                    except Exception:
                        pass
                else:
                    print("Timeout - no token found")
                    await browser.close()
                    return
            else:
                # 页面看起来已登录
                token = await page.evaluate("localStorage.getItem('token')")
                if token and len(token) > 50:
                    print("Already logged in! Token found.")
                    try:
                        with open(TOKEN_BACKUP_PATH, 'w', encoding='utf-8') as f:
                            json.dump({"token": token}, f, ensure_ascii=False)
                        print(f"Token saved to {TOKEN_BACKUP_PATH}")
                    except Exception as e:
                        print(f"Failed to save token backup: {e}")
                else:
                    print("No token found but page looks logged in. This seems odd.")
                    await browser.close()
                    return

        print(f"Login successful! User data saved to {USER_DATA_DIR}")
        print("Closing browser...")
        await browser.close()
        print("Done! You can now run the server normally.")

if __name__ == '__main__':
    asyncio.run(login_and_save())
