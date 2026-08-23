"""即梦 (Jimeng) 浏览器客户端 mixin，使用 Playwright 持久化 profile。"""
from ._shared import *

JIMENG_URL = "https://jimeng.jianying.com"
JIMENG_GENERATE_URL = f"{JIMENG_URL}/ai-tool/generate?enter_from=ai_feature&from_page=explore&ai_feature_name=video"
JIMENG_USER_DATA_DIR = os.path.join(BASE_DIR, "profiles", "jimeng_profile")


def _reset_jimeng_profile_crash():
    """启动浏览器前重置 profile 的崩溃标记。"""
    local_state_path = os.path.join(JIMENG_USER_DATA_DIR, "Local State")
    prefs_path = os.path.join(JIMENG_USER_DATA_DIR, "Default", "Preferences")
    for p in [local_state_path, prefs_path]:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    state = json.load(f)
                profile = state.get("profile", {})
                if profile.get("exit_type") is not None:
                    profile["exit_type"] = None
                    state["profile"] = profile
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(state, f)
        except Exception:
            pass


class JimengMixin:
    """即梦浏览器客户端 mixin。"""

    async def ensure_jimeng_ready(self, headless: bool = True):
        """确保即梦浏览器已启动并登录。"""
        if self._jimeng_page and not self._jimeng_page.is_closed():
            return True

        # 重置 profile 崩溃标记
        _reset_jimeng_profile_crash()
        os.makedirs(JIMENG_USER_DATA_DIR, exist_ok=True)

        from playwright.async_api import async_playwright
        logger.info(f"[Jimeng] Starting browser... headless={headless}")

        self._jimeng_pw = await async_playwright().start()
        _args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        if headless:
            _args.append("--headless=new")
        self._jimeng_browser = await self._jimeng_pw.chromium.launch_persistent_context(
            user_data_dir=JIMENG_USER_DATA_DIR,
            headless=headless,
            channel=_browser_channel(),
            args=_args,
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._jimeng_page = self._jimeng_browser.pages[0] if self._jimeng_browser.pages else await self._jimeng_browser.new_page()

        # 反检测脚本
        await self._jimeng_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
        """)

        # 导航到即梦视频生成页面
        logger.info("[Jimeng] navigating to generate page...")
        await self._jimeng_page.goto(JIMENG_GENERATE_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # 处理安全确认弹窗
        try:
            confirm_btn = await self._jimeng_page.query_selector('button:has-text("确认")')
            if confirm_btn:
                await confirm_btn.click()
                logger.info("[Jimeng] clicked safety confirmation")
                await asyncio.sleep(2)
        except Exception:
            pass

        # 检查是否被拦截到登录页
        for attempt in range(5):
            try:
                body_text = await self._jimeng_page.text_content("body") or ""
                if "登录" in body_text or "/login" in self._jimeng_page.url:
                    logger.warning("[Jimeng] Detected login page, showing browser for manual login...")
                    await self._jimeng_login_recovery()
                    continue
                else:
                    logger.info("[Jimeng] Page ready")
                    break
            except Exception as e:
                logger.warning(f"[Jimeng] Page check error: {e}")
                await asyncio.sleep(2)

        return True

    async def _jimeng_login_recovery(self):
        """显示浏览器让用户手动登录。"""
        # 关闭现有浏览器
        if self._jimeng_browser:
            try:
                await self._jimeng_browser.close()
            except Exception:
                pass
            self._jimeng_browser = None
            self._jimeng_page = None
            self._jimeng_pw = None

        from playwright.async_api import async_playwright
        self._jimeng_pw = await async_playwright().start()
        self._jimeng_browser = await self._jimeng_pw.chromium.launch_persistent_context(
            user_data_dir=JIMENG_USER_DATA_DIR,
            headless=False,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._jimeng_page = self._jimeng_browser.pages[0] if self._jimeng_browser.pages else await self._jimeng_browser.new_page()

        await self._jimeng_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        await self._jimeng_page.goto(JIMENG_GENERATE_URL, wait_until="domcontentloaded", timeout=60000)

        # 等待用户登录（最多6分钟）
        for _ in range(72):
            await asyncio.sleep(5)
            try:
                body_text = await self._jimeng_page.text_content("body") or ""
                cur_url = self._jimeng_page.url
                if "登录" not in body_text and "/login" not in cur_url:
                    logger.info("[Jimeng] Login recovered!")
                    break
            except Exception:
                pass

    async def close_jimeng(self):
        """关闭即梦浏览器。"""
        for attr in ['_jimeng_page', '_jimeng_browser', '_jimeng_pw']:
            obj = getattr(self, attr, None)
            if obj:
                try:
                    if attr == '_jimeng_pw':
                        await obj.stop()
                    else:
                        await obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    async def call_video_generate_api(self, body: dict) -> dict:
        """通过浏览器页面调用视频生成 API。使用 page.evaluate 执行 fetch，让页面的 SDK 自动处理签名。"""
        await self.ensure_jimeng_ready(headless=True)
        page = self._jimeng_page
        if not page:
            raise RuntimeError("Jimeng page not available")

        # API endpoint (basic URL, query params will be auto-added by SDK interceptors for aid, web_version, etc.)
        url = f"{JIMENG_URL}/mweb/v1/aigc_draft/generate"
        # 仅添加必须的静态查询参数；msToken、a_bogus、sign等由拦截器自动添加
        query = "aid=513695&device_platform=web&region=cn&web_version=7.5.0&da_version=3.3.21&os=windows"
        full_url = f"{url}?{query}"

        # 通过 page.evaluate 执行 fetch，返回结构和错误处理
        js = """async (args) => {
            const { url, body } = args;
            try {
                const response = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const text = await response.text();
                let json;
                try { json = JSON.parse(text); } catch (e) { json = null; }
                return { status: response.status, ok: response.ok, text: text, json: json };
            } catch (e) {
                return { status: 0, ok: false, error: String(e), text: '', json: null };
            }
        }"""

        result = await page.evaluate(js, {"url": full_url, "body": body})

        # 如果响应码不是200或json解析失败，则记录错误
        if not result.get("ok") or result.get("status") != 200:
            logger.error(f"[Jimeng] API call failed: status={result.get('status')}, ok={result.get('ok')}, error={result.get('error')}")
            # 尝试提取返回的错误信息
            if result.get("json"):
                errmsg = result["json"].get("errmsg", "Unknown error")
                return {"ret": "-1", "errmsg": errmsg, "data": None}
            return {"ret": "-1", "errmsg": result.get("error", "HTTP error"), "data": None}

        return result["json"] or {"ret": "0", "errmsg": "success", "data": {}}

    async def call_history_api(self, submit_ids: list[str]) -> dict:
        """通过浏览器页面查询历史任务状态。"""
        await self.ensure_jimeng_ready(headless=True)
        page = self._jimeng_page
        if not page:
            raise RuntimeError("Jimeng page not available")

        url = f"{JIMENG_URL}/mweb/v1/get_history_by_ids"
        query = "aid=513695&device_platform=web&region=cn&web_version=7.5.0&da_version=3.3.21"
        full_url = f"{url}?{query}"
        body = {"history_ids": submit_ids, "generate_type": 10, "page": 1, "page_size": 10, "sort": "desc"}

        js = """async (args) => {
            const { url, body } = args;
            try {
                const response = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const text = await response.text();
                let json;
                try { json = JSON.parse(text); } catch (e) { json = null; }
                return { status: response.status, ok: response.ok, text: text, json: json };
            } catch (e) {
                return { status: 0, ok: false, error: String(e), text: '', json: null };
            }
        }"""

        result = await page.evaluate(js, {"url": full_url, "body": body})

        if not result.get("ok") or result.get("status") != 200:
            logger.error(f"[Jimeng] History API call failed: status={result.get('status')}, error={result.get('error')}")
            return {"ret": "-1", "errmsg": result.get("error", "HTTP error"), "data": {}}

        return result["json"] or {"ret": "0", "errmsg": "success", "data": {}}
