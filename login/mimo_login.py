import asyncio
import os
import sys
import json
from playwright.async_api import async_playwright
from config import CONFIG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "profiles", "mimo_profile")

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

        print("Opening MiMo Studio login page...")
        await page.goto('https://aistudio.xiaomimimo.com/', wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(5)

        try:
            await page.locator('button[aria-label="关闭公告"]').first.click(timeout=3000)
            await asyncio.sleep(1)
        except:
            pass

        print("Please login in the browser window. Waiting up to 180s...")
        for i in range(180):
            await asyncio.sleep(1)
            try:
                current_url = page.url
                if i % 15 == 0:
                    print(f"  waiting... {i+1}s")
                if 'login' not in current_url.lower() and 'signin' not in current_url.lower():
                    print(f"[OK] Login detected, URL: {current_url}")
                    break
            except:
                pass
        else:
            print("[WARN] Login timeout after 180s")

        print("Saving login state...")
        state = await browser.storage_state()
        with open(os.path.join(BASE_DIR, "mimo_storage_state.json"), 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print("[OK] Login state saved to mimo_storage_state.json")

if __name__ == "__main__":
    asyncio.run(login_and_save())
