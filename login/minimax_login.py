import asyncio
import os
import sys
import json
import shutil
from playwright.async_api import async_playwright
from config import CONFIG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "profiles", "minimax_profile")

def _get_user_agent():
    if sys.platform.startswith("win"):
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    elif sys.platform == "darwin":
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    else:
        return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

async def login_and_save():
    if os.path.exists(USER_DATA_DIR):
        import shutil
        print(f"Clearing old profile: {USER_DATA_DIR}")
        shutil.rmtree(USER_DATA_DIR, ignore_errors=True)

    os.makedirs(USER_DATA_DIR, exist_ok=True)
    print(f"Created user data directory: {USER_DATA_DIR}")

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            channel=CONFIG.get("_browser_channel") if CONFIG.get("_browser_channel") is not None else ("msedge" if sys.platform.startswith("win") else None),
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"],
            user_agent=_get_user_agent(),
            viewport={'width': 1280, 'height': 900},
            ignore_default_args=["--enable-automation"],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        print("Opening MiniMax Agent page...")
        await page.goto('https://agent.minimaxi.com/', wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(5)

        # 移除遮挡弹窗
        await page.evaluate("""() => {
            document.querySelectorAll('[data-connect-mobile-hint-dismiss-boundary]').forEach(el => el.remove());
            document.querySelectorAll('.fixed').forEach(el => {
                const z = el.style.zIndex || window.getComputedStyle(el).zIndex;
                if (parseInt(z) >= 999) el.remove();
            });
        }""")
        await asyncio.sleep(1)

        # 检查是否已登录
        is_logged_in = await page.evaluate("""() => {
            return !document.querySelector('[data-testid="sidebar-login-button"]');
        }""")

        if not is_logged_in:
            print("Not logged in, clicking login button...")
            await page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="sidebar-login-button"]');
                if (btn) btn.click();
            }""")
            await asyncio.sleep(3)

            print("Please complete login in the browser window...")
            print("If Edge security verification appears, please complete it.")
            print("Waiting up to 180 seconds...")
            logged_in = False
            for i in range(180):
                await asyncio.sleep(1)
                try:
                    url = page.url
                    if (url.startswith('https://agent.minimaxi.com') and
                        'account.minimaxi.com' not in url and
                        'unified-login' not in url):
                        print(f"Login detected at {i}s!")
                        logged_in = True
                        break
                except:
                    pass
                if i % 30 == 0:
                    print(f"  [{i}s] waiting...")

            if not logged_in:
                print("Login timeout. Please try again.")
                await browser.close()
                return
        else:
            print("Already logged in!")

        print(f"Login successful! Profile saved to {USER_DATA_DIR}")
        await browser.close()
        print("Done!")

if __name__ == '__main__':
    asyncio.run(login_and_save())
