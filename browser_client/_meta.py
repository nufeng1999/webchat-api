"""Meta.ai 浏览器客户端 mixin，使用 Playwright 持久化 profile + Urban VPN 扩展。"""
from ._shared import *

META_URL = "https://www.meta.ai/"
META_PROFILE_DIR = os.path.join(BASE_DIR, "profiles", "meta_profile")
META_EXT_ID = "nimlmejbmnecnaghgmbahmbaddhjbecg"
META_EXT_POPUP_URL = f"chrome-extension://{META_EXT_ID}/popup/index.html"

META_INIT_SCRIPT = """
// 反检测脚本
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
// 仅对普通网页注入 chrome 对象；绝不能覆盖扩展页面(chrome-extension://)的真实 chrome.runtime API
if (location.protocol !== 'chrome-extension:') {
    if (!window.chrome || typeof window.chrome.runtime?.sendMessage !== 'function') {
        window.chrome = { runtime: {} };
    }
}
"""


def _reset_meta_profile_crash():
    """启动浏览器前重置 profile 的崩溃标记。"""
    local_state_path = os.path.join(META_PROFILE_DIR, "Local State")
    prefs_path = os.path.join(META_PROFILE_DIR, "Default", "Preferences")
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


def _is_vpn_connected(state: str) -> bool:
    """判断 VPN 扩展状态文本是否为已连接。"""
    st = (state or "").strip().lower()
    return bool(st) and (("connected" in st and "not " not in st) or "已连接" in st)


class MetaMixin:
    """Meta.ai 浏览器客户端 mixin。

    浏览器必须支持扩展插件（Urban VPN），且每次启动浏览器后必须先让 VPN 连接成功，
    之后才允许打开 https://www.meta.ai/ 网站。
    """

    async def ensure_meta_ready(self, headless: bool = True, ensure_vpn: bool = True):
        """确保 meta.ai 浏览器已启动：启动浏览器 → 连接 Urban VPN → 打开 meta.ai。

        顺序保证：
        1. 启动持久化浏览器（启用扩展插件，加载 Urban VPN）
        2. 打开 VPN 扩展弹窗并等待连接成功（失败则关闭浏览器并抛出异常）
        3. VPN 连接成功后才导航到 https://www.meta.ai/
        """
        if self._meta_page and not self._meta_page.is_closed():
            return True

        if not os.path.isdir(os.path.join(META_PROFILE_DIR, "Default", "Extensions", META_EXT_ID)):
            raise RuntimeError(
                f"meta_profile 缺失 Urban VPN 扩展（{META_EXT_ID}），请先将含扩展的 meta_profile 复制到 {META_PROFILE_DIR}"
            )

        _reset_meta_profile_crash()
        os.makedirs(META_PROFILE_DIR, exist_ok=True)

        from playwright.async_api import async_playwright
        logger.info(f"[Meta] Starting browser... headless={headless}")

        self._meta_pw = await async_playwright().start()
        _args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        self._meta_browser = await self._meta_pw.chromium.launch_persistent_context(
            user_data_dir=META_PROFILE_DIR,
            headless=headless,
            channel=_browser_channel(),
            args=_args,
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
            locale="zh-CN",
            # 必须保留扩展插件能力（Playwright 默认禁用扩展，这里忽略该参数）
            ignore_default_args=["--disable-extensions", "--enable-automation"],
        )
        self._meta_page = self._meta_browser.pages[0] if self._meta_browser.pages else await self._meta_browser.new_page()
        await self._meta_page.add_init_script(META_INIT_SCRIPT)
        self._meta_page.set_default_timeout(60000)

        if ensure_vpn:
            try:
                ok = await self._meta_ensure_vpn(self._meta_page)
            except Exception as e:
                logger.error(f"[Meta] VPN connect exception: {e}")
                ok = False
            if not ok:
                logger.error("[Meta] Urban VPN 连接失败，关闭浏览器，禁止打开 meta.ai")
                await self.close_meta()
                raise RuntimeError("Urban VPN 连接失败，无法访问 meta.ai")

            # VPN 连接成功后，才能打开 meta.ai
            await self._meta_goto_home()

        return True

    async def _meta_ensure_vpn(self, page, timeout: int = 90, max_attempts: int = 4) -> bool:
        """打开 Urban VPN 扩展弹窗并等待连接成功。返回是否已连接。"""
        logger.info("[Meta] connecting Urban VPN...")

        async def _state():
            try:
                return (await page.evaluate(
                    "() => (document.querySelector('.connection-state__status-text')||{}).textContent || ''"
                ) or "").strip()
            except Exception:
                return ""

        async def _click_play():
            # 直接 JS click，绕过 Playwright 动作性检查，更可靠
            try:
                return await page.evaluate(
                    """() => {
                        const el = document.querySelector('button.play-button');
                        if (!el) return false;
                        el.click();
                        return true;
                    }"""
                )
            except Exception:
                return False

        for attempt in range(max_attempts):
            try:
                await page.goto(META_EXT_POPUP_URL, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                logger.warning(f"[Meta] vpn popup goto attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(3)
                continue

            # 弹窗是 Vue 异步挂载的，play-button 可能要十几秒才出现在 DOM 里
            try:
                await page.wait_for_selector("button.play-button", state="attached", timeout=30000)
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"[Meta] vpn play button not found (attempt {attempt + 1}): {e}")

            # 已是连接状态（VPN 状态在会话间持久化）
            if _is_vpn_connected(await _state()):
                logger.info("[Meta] VPN already connected")
                return True

            # 点击连接按钮
            for _ in range(3):
                if await _click_play():
                    logger.info("[Meta] clicked VPN play button")
                    break
                await asyncio.sleep(3)

            # 等待连接成功
            started = time.time()
            while time.time() - started < timeout:
                await asyncio.sleep(3)
                if _is_vpn_connected(await _state()):
                    logger.info("[Meta] VPN connected")
                    return True

            logger.warning(f"[Meta] VPN connect timeout (attempt {attempt + 1}/{max_attempts})")

        return False

    async def _meta_goto_home(self, retries: int = 3):
        """VPN 连接成功后导航到 meta.ai 首页。"""
        logger.info(f"[Meta] navigating to {META_URL}...")
        last_err = None
        for i in range(retries):
            try:
                await self._meta_page.goto(META_URL, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(5)
                return
            except Exception as e:
                last_err = e
                logger.warning(f"[Meta] goto home {i + 1} failed: {str(e)[:80]}")
                await asyncio.sleep(4)
        if last_err:
            raise RuntimeError(f"打开 meta.ai 失败: {last_err}")

    async def delete_all_meta_conversations(self, headless: bool = True, max_delete: int = 0) -> dict:
        """通过 UI 自动化删除 meta.ai 所有历史对话（含确认框）。

        流程（已在独立测试脚本中验证）：
        1. 悬停聊天行，点击"更多选项"按钮
        2. 点击弹出菜单中的"删除"
        3. 点击确认对话框中的"删除"
        4. 等待该行从侧边栏移除

        Args:
            headless: 是否无头模式启动浏览器
            max_delete: 最多删除条数，0 表示删除全部

        Returns:
            {"deleted": n, "rows_remaining": r}
        """
        logger.info("[Meta] Starting UI-based conversation deletion...")
        await self.ensure_meta_ready(headless=headless, ensure_vpn=True)
        page = self._meta_page

        MORE = "\\u66f4\\u591a\\u9009\\u9879"  # 更多选项
        DEL = "\\u5220\\u9664"                  # 删除

        async def _row_count():
            try:
                return await page.evaluate(
                    """() => [...document.querySelectorAll('li.group\\\\/menu-item')]
                        .filter(x => x.querySelector('a[href*="/prompt/"]')).length"""
                )
            except Exception:
                return -1

        async def _first_target():
            return await page.evaluate(
                f"""() => {{
                    const li = [...document.querySelectorAll('li.group\\\\/menu-item')].find(
                        x => x.querySelector('a[href*="/prompt/"]')
                    );
                    if (!li) return null;
                    const btn = li.querySelector('button[aria-label="{MORE}"]');
                    if (!btn) return null;
                    const br = btn.getBoundingClientRect();
                    const lr = li.getBoundingClientRect();
                    return {{
                        bx: Math.round(br.x + br.width / 2), by: Math.round(br.y + br.height / 2),
                        lx: Math.round(lr.x + lr.width / 2), ly: Math.round(lr.y + lr.height / 2),
                    }};
                }}"""
            )

        # 等待历史会话列表渲染
        for _ in range(10):
            await asyncio.sleep(3)
            if await _row_count() > 0:
                break

        deleted = 0
        stall = 0
        while True:
            n = await _row_count()
            if n <= 0:
                logger.info("[Meta] No meta.ai conversations remaining.")
                break
            if max_delete and deleted >= max_delete:
                break

            target = await _first_target()
            if not target:
                stall += 1
                if stall >= 3:
                    logger.warning("[Meta] cannot locate chat rows, aborting deletion")
                    break
                await asyncio.sleep(3)
                continue

            # 1. 悬停聊天行并点击"更多选项"
            await page.mouse.move(target["lx"], target["ly"])
            await asyncio.sleep(0.8)
            await page.mouse.click(target["bx"], target["by"])
            await asyncio.sleep(2.5)

            # 2. 点击弹出菜单中的"删除"
            menu_item = await page.evaluate(
                f"""() => {{
                    const it = [...document.querySelectorAll('[role=menuitem]')]
                        .find(x => (x.textContent || '').trim() === '{DEL}');
                    if (!it) return null;
                    const r = it.getBoundingClientRect();
                    return {{ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }};
                }}"""
            )
            if not menu_item:
                logger.warning("[Meta] delete menu item not found, aborting this row")
                stall += 1
                await asyncio.sleep(2)
                continue

            await page.mouse.click(menu_item["x"], menu_item["y"])
            await asyncio.sleep(2.5)

            # 3. 点击确认对话框中的"删除"
            confirm_btn = await page.evaluate(
                f"""() => {{
                    const dlg = [...document.querySelectorAll('[role=dialog], [role=alertdialog]')]
                        .find(el => el.getBoundingClientRect().width > 0);
                    if (!dlg) return null;
                    const btn = [...dlg.querySelectorAll('button')]
                        .find(b => (b.textContent || '').trim() === '{DEL}');
                    if (!btn) return null;
                    const r = btn.getBoundingClientRect();
                    return {{ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }};
                }}"""
            )
            if not confirm_btn:
                logger.warning("[Meta] confirm delete button not found")
                stall += 1
                await asyncio.sleep(2)
                continue

            await page.mouse.click(confirm_btn["x"], confirm_btn["y"])
            deleted += 1
            logger.info(f"[Meta] deleted {deleted}, rows now: {await _row_count()}")

            # 等待该行移除
            for _ in range(10):
                await asyncio.sleep(2)
                if (await _row_count()) <= n - 1:
                    break

            if (await _row_count()) >= n:
                stall += 1
                if stall >= 3:
                    logger.warning("[Meta] no progress in deletion, aborting")
                    break
            else:
                stall = 0

        return {"deleted": deleted, "rows_remaining": await _row_count()}

    async def close_meta(self):
        """关闭 meta.ai 浏览器。"""
        for attr in ['_meta_page', '_meta_browser', '_meta_pw']:
            obj = getattr(self, attr, None)
            if obj:
                try:
                    if attr == '_meta_pw':
                        await obj.stop()
                    else:
                        await obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
