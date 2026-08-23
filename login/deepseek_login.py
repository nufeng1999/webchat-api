import asyncio
import sys
import os
from playwright.async_api import async_playwright
from config import CONFIG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "profiles", "deepseek_profile")

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
            args=["--no-sandbox", "--disable-setuid-sandbox"] + (["--disable-gpu", "--disable-dev-shm-usage"] if not sys.platform.startswith("win") else []),
            user_agent=_get_user_agent(),
            viewport={'width': 1280, 'height': 900}
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        print("Opening DeepSeek login page...")
        await page.goto('https://chat.deepseek.com/', wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(3)

        print(f"Current URL: {page.url}")

        print("Please login in the browser window. Waiting up to 180s...")
        for i in range(180):
            await asyncio.sleep(1)
            try:
                current_url = page.url
                if i % 15 == 0:
                    print(f"  [{i}s] URL: {current_url}")
                if 'sign_in' not in current_url and 'login' not in current_url.lower():
                    editor = await page.query_selector('[contenteditable], textarea, input[type="text"]')
                    if editor:
                        print(f"Login detected! Chat editor found at {i}s")
                        await asyncio.sleep(3)
                        break
            except Exception:
                pass
        else:
            print("Timeout - please try again")
            await browser.close()
            return

        print(f"Login successful! User data saved to {USER_DATA_DIR}")
        print("Closing browser...")
        await browser.close()
        print("Done! You can now run the server normally.")

if __name__ == '__main__':
    asyncio.run(login_and_save())
