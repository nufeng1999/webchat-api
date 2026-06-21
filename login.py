import os
import sys
import json
import asyncio
import logging

from config import CONFIG, CONFIG_PATH, BASE_DIR, USER_AGENT

logger = logging.getLogger("doubao-login")

USER_DATA_DIR = os.path.join(BASE_DIR, "doubao_profile")


async def do_login(show_browser: bool = True) -> dict:
    """
    启动持久化浏览器让用户登录豆包，登录成功后提取 cookie 和设备参数，持久化到 config.json。
    使用 launch_persistent_context 保持登录状态到 doubao_profile 目录。

    Args:
        show_browser: 是否显示浏览器窗口（headless=False）

    Returns:
        {"success": True/False, "cookie": ..., "message": ...}
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed. Run: pip install playwright && playwright install chromium")
        return {"success": False, "message": "playwright not installed"}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed")
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

        # 拦截浏览器真实 API 请求，捕获浏览器默认的 device_id/web_id/tea_uuid/fp
        captured_params = {"device_id": "", "web_id": "", "tea_uuid": "", "fp": ""}

        def _on_request(request):
            try:
                url = request.url
                if "doubao.com" not in url:
                    return
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(url).query)
                for key in ("device_id", "web_id", "tea_uuid", "fp"):
                    val = qs.get(key, [""])[0]
                    if val and not captured_params[key]:
                        captured_params[key] = val
                        logger.info(f"Captured {key} from browser request: {val}")
                # 从 POST body 中捕获 fp（豆包将 fp 放在请求体 ext.fp 中）
                if not captured_params["fp"] and request.method == "POST":
                    try:
                        post_data = request.post_data
                        if post_data and '"fp"' in post_data:
                            import json as _json
                            body = _json.loads(post_data)
                            fp = (body.get("ext") or {}).get("fp", "")
                            if fp:
                                captured_params["fp"] = fp
                                logger.info(f"Captured fp from POST body: {fp}")
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("request", _on_request)

        logger.info("Navigating to doubao.com/chat/ ...")
        await page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
        await asyncio.sleep(2)

        # 检查 session cookie；如果 persistent context 恢复的 cookies 不包含有效会话，尝试从 config.json 补充注入
        async def _is_logged_in() -> bool:
            cks = await browser.cookies()
            names = {c["name"] for c in cks if c.get("value")}
            return bool(names & {"sessionid", "sessionid_ss", "sid_guard", "sid_tt"})

        if not await _is_logged_in():
            cookie_str = CONFIG.get('cookie', '')
            if cookie_str and 'sessionid' in cookie_str:
                logger.info("Session cookie missing after page load, trying config.json cookie string...")
                cookies_to_add = []
                for part in cookie_str.split(';'):
                    part = part.strip()
                    if '=' in part:
                        name, value = part.split('=', 1)
                        # 检查是否已存在同名 cookie
                        if not any(c.get("name") == name for c in await browser.cookies()):
                            cookies_to_add.append({
                                'name': name.strip(),
                                'value': value.strip(),
                                'domain': '.doubao.com',
                                'path': '/'
                            })
                if cookies_to_add:
                    await browser.add_cookies(cookies_to_add)
                    logger.info(f"Added {len(cookies_to_add)} missing cookies from config.json")
                    if await _is_logged_in():
                        logger.info("Login state restored from config.json cookies")

        if await _is_logged_in():
            logger.info("Already logged in (valid session cookie found)")
            await asyncio.sleep(2)
        else:
            logger.info("Login required. Please log in manually in the browser window.")
            if show_browser:
                print("=" * 50)
                print("请在浏览器中登录豆包账号")
                print("登录成功后程序将自动继续")
                print("=" * 50)

            # 轮询等待登录完成（检测到会话 cookie），最长 5 分钟
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
            logger.info("Login detected, extracting credentials...")

        # 提取 cookie
        cookies = await browser.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        if not cookie_str:
            await browser.close()
            await pw.stop()
            return {"success": False, "message": "未能获取到 cookie"}

        # 从 cookie 中构建快速查找字典
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        # 提取设备参数：优先使用从浏览器真实请求中捕获的参数（最可靠）
        # 其次从 cookie / localStorage 中取，最后回退到旧 config 值
        device_id = captured_params.get("device_id", "") or cookie_dict.get("device_id", "")
        web_id = captured_params.get("web_id", "") or cookie_dict.get("web_id", "")
        tea_uuid = captured_params.get("tea_uuid", "") or cookie_dict.get("tea_uuid", "")

        # 若仍未捕获，主动等待浏览器发起一次 API 请求再读取
        if not (device_id and web_id and tea_uuid):
            try:
                await page.reload(wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)
            except Exception:
                pass
            device_id = device_id or captured_params.get("device_id", "")
            web_id = web_id or captured_params.get("web_id", "")
            tea_uuid = tea_uuid or captured_params.get("tea_uuid", "")

        if not device_id:
            try:
                device_id = await page.evaluate("() => localStorage.getItem('device_id') || ''")
            except Exception:
                pass
        if not web_id:
            try:
                web_id = await page.evaluate("() => localStorage.getItem('web_id') || ''")
            except Exception:
                pass
        if not tea_uuid:
            try:
                tea_uuid = await page.evaluate("() => localStorage.getItem('tea_uuid') || ''")
            except Exception:
                pass

        # 更新 config.json
        _update_config(cookie_str, device_id, web_id, tea_uuid)

        logger.info(f"Login successful. Cookie length: {len(cookie_str)}")
        print("=" * 50)
        print("登录成功！配置已保存到 config.json")
        print(f"Cookie 长度: {len(cookie_str)} 字符")
        if device_id:
            print(f"device_id: {device_id}")
        print("=" * 50)
        print("请手动关闭浏览器窗口以退出程序...")

        # 轮询检测浏览器是否被用户关闭
        while True:
            await asyncio.sleep(0.5)
            try:
                if not browser.pages:
                    break
            except Exception:
                break

        try:
            await pw.stop()
        except Exception:
            pass

        return {
            "success": True,
            "cookie": cookie_str,
            "device_id": device_id,
            "web_id": web_id,
            "tea_uuid": tea_uuid,
            "message": "登录成功",
        }

    except Exception as e:
        logger.error(f"Login flow error: {e}")
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        return {"success": False, "message": str(e)}


def _update_config(cookie_str: str, device_id: str, web_id: str, tea_uuid: str):
    """将登录结果写入 config.json，保留其他字段不变。"""
    config = CONFIG.copy()
    config["cookie"] = cookie_str
    if device_id:
        config["device_id"] = device_id
    if web_id:
        config["web_id"] = web_id
    if tea_uuid:
        config["tea_uuid"] = tea_uuid

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    logger.info(f"Config saved to {CONFIG_PATH}")
