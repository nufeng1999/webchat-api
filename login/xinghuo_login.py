"""讯飞星火登录模块"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))


def _get_user_agent():
    if sys.platform.startswith("win"):
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    elif sys.platform == "darwin":
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    else:
        return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"


async def login_and_save():
    """打开浏览器让用户登录讯飞星火，保存登录状态。"""
    from playwright.async_api import async_playwright
    from config import BASE_DIR, CONFIG

    user_data_dir = os.path.join(BASE_DIR, "spark_user_data")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=False,
        channel=CONFIG.get("_browser_channel") if CONFIG.get("_browser_channel") is not None else ("msedge" if sys.platform.startswith("win") else None),
        viewport={"width": 1280, "height": 900},
        user_agent=_get_user_agent(),
    )
    page = browser.pages[0] if browser.pages else await browser.new_page()

    print("正在打开讯飞星火...")
    await page.goto("https://xinghuo.xfyun.cn/", wait_until="networkidle", timeout=60000)
    await asyncio.sleep(5)

    # 检查是否已登录
    is_logged = await page.evaluate("""() => {
        const t = document.body?.innerText || '';
        return !t.includes('登录') || t.includes('退出');
    }""")

    if is_logged:
        print("已登录!")
        await browser.close()
        await pw.stop()
        return True

    print("请在浏览器中完成登录（支持验证码/密码/微信扫码）...")
    print("等待至少 2 分钟...")

    # 等待用户登录（至少 2 分钟，最多 5 分钟）
    for i in range(100):
        await asyncio.sleep(3)
        try:
            check = await page.evaluate("""() => {
                const t = document.body?.innerText || '';
                return { logged: t.includes('退出') || !t.includes('登录') };
            }""")
            if check.get('logged'):
                print("登录成功!")
                await asyncio.sleep(3)
                await browser.close()
                await pw.stop()
                return True
            if i % 10 == 0:
                print(f"  等待中... ({i*3}s)")
        except:
            pass

    print("登录超时")
    await browser.close()
    await pw.stop()
    return False


if __name__ == "__main__":
    result = asyncio.run(login_and_save())
    if result:
        print("登录完成")
    else:
        print("登录失败")
