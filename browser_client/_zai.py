from ._shared import *


class ZaiMixin:
    async def activate_zai_conversation(self, session_id: str) -> bool:
        """导航到 z.ai 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_zai_ready()
            url = f"https://chat.z.ai/c/{session_id}"
            await self._zai_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[Zai] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[Zai] activate conversation failed: {e}")
            return False

    def _reset_zai_profile_crash():
        """启动浏览器前重置 profile 的崩溃标记，防止 Chromium 认为上次异常退出。"""
        local_state_path = os.path.join(BASE_DIR, "profiles", "zai_profile", "Local State")
        prefs_path = os.path.join(BASE_DIR, "profiles", "zai_profile", "Default", "Preferences")
        try:
            if os.path.exists(local_state_path):
                with open(local_state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                profile = state.get("profile", {})
                if profile.get("exit_type") is not None:
                    profile["exit_type"] = None
                    state["profile"] = profile
                    with open(local_state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f)
                    logger.info("[Zai] reset profile exit_type to None")
        except Exception as e:
            logger.debug(f"[Zai] reset local state: {e}")
        try:
            if os.path.exists(prefs_path):
                with open(prefs_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
                prof = prefs.get("profile", {})
                if prof.get("exit_type") is not None:
                    prof["exit_type"] = None
                    prefs["profile"] = prof
                    with open(prefs_path, "w", encoding="utf-8") as f:
                        json.dump(prefs, f)
                    logger.info("[Zai] reset Preferences exit_type to None")
        except Exception as e:
            logger.debug(f"[Zai] reset preferences: {e}")

    def _save_zai_token(token: str):
        """将 token 备份到 JSON 文件，防止 profile 崩溃丢失。"""
        backup_path = os.path.join(BASE_DIR, "zai_token_backup.json")
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump({"token": token}, f)
            logger.info(f"[Zai] token backed up ({len(token)} chars)")
        except Exception as e:
            logger.debug(f"[Zai] token backup: {e}")

    def _load_zai_token_backup() -> str:
        """从备份文件加载 token。"""
        backup_path = os.path.join(BASE_DIR, "zai_token_backup.json")
        try:
            if os.path.exists(backup_path):
                with open(backup_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                token = data.get("token", "")
                if token and len(token) > 100:
                    return token
        except Exception:
            pass
        return ""

    async def ensure_zai_ready(self, headless: bool = True):
        """确保 z.ai 浏览器已启动并登录。"""
        if self._zai_page and not self._zai_page.is_closed():
            return

        original_headless = headless  # 记录原始 headless 状态

        # 重置 profile 崩溃标记
        self._reset_zai_profile_crash()

        from playwright.async_api import async_playwright
        logger.info(f"[Zai] Starting z.ai browser... headless={headless}, channel={_browser_channel()}")
        self._zai_pw = await async_playwright().start()
        _args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        if headless:
            _args.append("--headless=new")
        self._zai_browser = await self._zai_pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "profiles", "zai_profile"),
            headless=headless,
            channel=_browser_channel(),
            args=_args,
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        logger.info(f"[Zai] browser launched, pages: {len(self._zai_browser.pages)}")
        self._zai_page = self._zai_browser.pages[0] if self._zai_browser.pages else await self._zai_browser.new_page()

        # 反检测 + fetch 拦截器（必须在页面脚本运行前注入）
        await self._zai_page.add_init_script(ZAI_INIT_SCRIPT)

        # 导航到 z.ai
        logger.info("[Zai] navigating to z.ai/...")
        await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
        
        # 先处理弹窗（可能遮挡页面元素）
        await self._dismiss_zai_popups()
        
        # 等待页面渲染（textarea 或登录页面的元素）
        try:
            await self._zai_page.wait_for_selector("textarea, .modelSelectorButton, button[aria-label*='model'], input[type='tel'], input[placeholder*='手机']", timeout=20000)
            logger.info("[Zai] page rendered")
        except:
            logger.warning("[Zai] page elements not found after 20s")
            # 再试一次处理弹窗
            await self._dismiss_zai_popups()
            await asyncio.sleep(3)

        # 再次处理弹窗
        await self._dismiss_zai_popups()

        # 检查登录状态：先检查 localStorage token，再检查 URL
        token = ""
        try:
            token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
        except Exception:
            pass

        if token and len(token) > 100:
            logger.info(f"[Zai] Token found: {len(token)} chars")
            self._save_zai_token(token)
        else:
            # 尝试从备份恢复 token
            backup_token = self._load_zai_token_backup()
            if backup_token:
                logger.info(f"[Zai] Restoring token from backup ({len(backup_token)} chars)")
                try:
                    await self._zai_page.evaluate("(token) => { localStorage.setItem('token', token); }", backup_token)
                    await self._zai_page.reload(wait_until="domcontentloaded", timeout=60000)
                    await self._dismiss_zai_popups()
                    token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
                    if token and len(token) > 100:
                        logger.info(f"[Zai] Token restored from backup: {len(token)} chars")
                        self._save_zai_token(token)
                    else:
                        token = ""
                except Exception:
                    token = ""

        if not token or len(token) <= 100:
            url = self._zai_page.url
            if '/auth' in url:
                logger.warning("[Zai] Redirected to /auth, need login")
                _bring_window_to_front()
                if headless:
                    await self._zai_login_recovery()
                    headless = False  # 更新当前 headless 状态
                else:
                    for _ in range(72):
                        await asyncio.sleep(5)
                        await self._dismiss_zai_popups()
                        token_len = await self._zai_page.evaluate("(localStorage.getItem('token') || '').length")
                        cur_url = self._zai_page.url
                        if token_len > 100 and '/auth' not in cur_url:
                            logger.info("[Zai] Login completed")
                            new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                            if new_token and len(new_token) > 100:
                                self._save_zai_token(new_token)
                            await asyncio.sleep(2)
                            break
                        if '/auth' not in cur_url and token_len > 0:
                            logger.info("[Zai] Login detected via URL change")
                            new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                            if new_token and len(new_token) > 100:
                                self._save_zai_token(new_token)
                            await asyncio.sleep(2)
                            break
            else:
                logger.warning("[Zai] No token found, trying auto-login button...")
                await self._dismiss_zai_popups()
                await asyncio.sleep(2)
                cur_url = self._zai_page.url
                if '/auth' in cur_url:
                    if headless:
                        await self._zai_login_recovery()
                        headless = False  # 更新当前 headless 状态
                    else:
                        for _ in range(72):
                            await asyncio.sleep(5)
                            await self._dismiss_zai_popups()
                            token_len = await self._zai_page.evaluate("(localStorage.getItem('token') || '').length")
                            if token_len > 100 and '/auth' not in self._zai_page.url:
                                logger.info("[Zai] Login completed")
                                new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                                if new_token and len(new_token) > 100:
                                    self._save_zai_token(new_token)
                                await asyncio.sleep(2)
                                break
                else:
                    logger.warning("[Zai] No token backup available")

        # --- 验证页检测与处理（确保最终就绪）---
        verification_keywords = ["verification required", "please verify", "just a moment", "access denied", "challenge", "captcha", "checking your browser"]
        max_wait_verification = 300  # seconds
        interval = 2  # seconds
        start_time = time.time()
        verification_occurred = False

        while True:
            await self._dismiss_zai_popups()
            try:
                body_text = await self._zai_page.text_content("body") or ""
                current_token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
                token_valid = current_token and len(current_token) > 100

                is_verification_page = any(kw in body_text.lower() for kw in verification_keywords)

                if is_verification_page:
                    verification_occurred = True
                    logger.warning(f"[Zai] Verification page detected (headless={headless}): {body_text[:100]}...")
                    if headless:
                        logger.info("[Zai] Headless mode: calling login recovery for manual verification.")
                        await self._zai_login_recovery()
                        headless = False  # now non-headless
                    else:
                        logger.info("[Zai] waiting for verification...")
                elif token_valid:
                    # 无验证页且 token 有效
                    if verification_occurred and original_headless and not headless:
                        # 之前发生过验证，且原始请求是 headless，且当前是非 headless 模式，需要重建 headless 浏览器
                        logger.info("[Zai] Verification cleared. Rebuilding headless browser...")
                        await self._rebuild_headless_after_verification()
                        headless = True  # 已重建为 headless 模式
                        # 继续循环，确保重建后的 headless 浏览器状态正常
                        continue
                    else:
                        # 无需重建，直接成功
                        break
                else:
                    # 无验证页但 token 仍然无效，继续等待或处理（例如可能正在加载中）
                    logger.debug("[Zai] No verification page, but token not valid yet. Waiting...")

                # 检查超时
                if time.time() - start_time > max_wait_verification:
                    if not token_valid:
                        raise TimeoutError(f"[Zai] Timeout after {max_wait_verification}s: Token not valid after verification attempts.")
                    elif is_verification_page:
                        raise TimeoutError(f"[Zai] Timeout after {max_wait_verification}s: Verification page persisted.")
                    else:
                        break  # token valid and no verification, but some logic path still hits here
            except Exception as e:
                logger.error(f"[Zai] Error during verification check: {e}")
                if not token_valid:  # 如果没有 token，认为是致命错误
                    raise
            await asyncio.sleep(interval)

        # 确保最终 token 有效
        final_token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
        if not final_token or len(final_token) <= 100:
            raise Exception("[Zai] Failed to obtain valid token after all attempts.")

        await self._dismiss_zai_popups()  # 最终再处理一次弹窗，确保页面干净
        logger.info("[Zai] z.ai browser ready and verified.")

    async def _zai_login_recovery(self):
        """z.ai 登录恢复：显示浏览器让用户手动登录。
        此函数会强制关闭当前浏览器，并以非 headless 模式重新启动，以便用户进行手动操作。
        """
        logger.warning("[Zai] Login required, showing browser for manual login...")

        # 强制关闭现有浏览器实例（包括 headless）
        if self._zai_browser:
            try:
                logger.info("[Zai] Closing existing browser for non-headless restart.")
                await self._zai_browser.close()
            except Exception as e:
                logger.warning(f"[Zai] Error closing existing browser: {e}")
            self._zai_browser = None
            self._zai_page = None
            self._zai_pw = None

        from playwright.async_api import async_playwright
        logger.info(f"[Zai] Launching non-headless browser for recovery...")
        self._zai_pw = await async_playwright().start()
        self._zai_browser = await self._zai_pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "profiles", "zai_profile"),
            headless=False,  # 强制非 headless
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._zai_page = self._zai_browser.pages[0] if self._zai_browser.pages else await self._zai_browser.new_page()

        # 注入反检测脚本
        await self._zai_page.add_init_script(ZAI_INIT_SCRIPT)

        # 导航到 z.ai
        logger.info("[Zai] navigating to z.ai/ for recovery...")
        await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
        await self._dismiss_zai_popups()

        # 等待用户登录（至少2分钟）
        min_wait = 120  # 最少等待时间
        waited = 0
        for _ in range(72):  # 最多6分钟
            await asyncio.sleep(5)
            waited += 5
            await self._dismiss_zai_popups()
            textarea = await self._zai_page.evaluate("!!document.querySelector('textarea')")
            token_len = await self._zai_page.evaluate("(localStorage.getItem('token') || '').length")
            cur_url = self._zai_page.url
            if textarea and token_len > 100 and '/auth' not in cur_url:
                logger.info(f"[Zai] Login recovered! (waited {waited}s)")
                new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                if new_token and len(new_token) > 100:
                    self._save_zai_token(new_token)
                break
            if '/auth' not in cur_url and token_len > 0:
                logger.info(f"[Zai] Login detected via URL change (waited {waited}s)")
                new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                if new_token and len(new_token) > 100:
                    self._save_zai_token(new_token)
                break
            if waited >= min_wait and waited % 30 == 0:
                logger.info(f"[Zai] still waiting for login... ({waited}s elapsed)")
        else:
            logger.warning(f"[Zai] Login timed out after {waited}s during recovery.")

    async def _rebuild_headless_after_verification(self):
        """在手动验证完成后，重建 headless 浏览器实例。"""
        logger.info("[Zai] _rebuild_headless_after_verification: Closing non-headless browser...")
        if self._zai_browser:
            try:
                await self._zai_browser.close()
            except Exception as e:
                logger.warning(f"[Zai] Error closing browser during headless rebuild: {e}")
            self._zai_browser = None
            self._zai_page = None
            self._zai_pw = None

        from playwright.async_api import async_playwright
        logger.info("[Zai] _rebuild_headless_after_verification: Launching new headless browser...")
        self._zai_pw = await async_playwright().start()
        _args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        _args.append("--headless=new")  # 确保是 headless
        self._zai_browser = await self._zai_pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "profiles", "zai_profile"),
            headless=True,  # 强制 headless
            channel=_browser_channel(),
            args=_args,
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._zai_page = self._zai_browser.pages[0] if self._zai_browser.pages else await self._zai_browser.new_page()

        await self._zai_page.add_init_script(ZAI_INIT_SCRIPT)
        logger.info("[Zai] _rebuild_headless_after_verification: Navigating to z.ai/...")
        await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
        await self._dismiss_zai_popups()

        # 验证 token 是否仍然有效（应该已在 profile 中）
        token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
        if token and len(token) > 100:
            self._save_zai_token(token)
            logger.info(f"[Zai] Token found after headless rebuild ({len(token)} chars).")
        else:
            logger.warning("[Zai] Token NOT found after headless rebuild. Trying backup...")
            backup_token = self._load_zai_token_backup()
            if backup_token:
                await self._zai_page.evaluate("(token) => { localStorage.setItem('token', token); }", backup_token)
                await self._zai_page.reload(wait_until="domcontentloaded", timeout=60000)
                await self._dismiss_zai_popups()
                final_token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
                if final_token and len(final_token) > 100:
                    self._save_zai_token(final_token)
                    logger.info(f"[Zai] Token restored from backup after headless rebuild ({len(final_token)} chars).")
                else:
                    logger.error("[Zai] Failed to restore token from backup after headless rebuild.")
            else:
                logger.error("[Zai] No token found or restored after headless rebuild.")

    async def fetch_zai_models(self) -> list[dict]:
        """从 Zai 页面模型选择器中获取可用模型列表。"""
        headless = CONFIG.get('_zai_headless', True)
        await self.ensure_zai_ready(headless=headless)
        page = self._zai_page
        if not page:
            return []

        models = []
        try:
            btn = page.locator('.modelSelectorButton, button[aria-label="选择一个模型"]').first
            if await btn.count() == 0:
                logger.warning("[Zai] Model selector button not found")
                return []
            await btn.click()
            await asyncio.sleep(1.5)

            model_list = await page.evaluate("""() => {
                const models = [];
                const items = document.querySelectorAll('[role="option"], [data-value], [class*="modelItem"], [class*="selectorItem"]');
                if (items.length > 0) {
                    for (const item of items) {
                        const name = (item.textContent || '').trim();
                        const dataValue = item.getAttribute('data-value') || '';
                        models.push({ name, dataValue });
                    }
                }
                if (models.length === 0) {
                    const popover = document.querySelector('[class*="z-40"], [class*="z-50"], [class*="popover"], [class*="dropdown"]');
                    if (popover) {
                        const buttons = popover.querySelectorAll('button, [role="option"], [tabindex]');
                        for (const btn of buttons) {
                            const name = (btn.textContent || '').split('NEW')[0].split('  ')[0].trim();
                            if (name && name.length > 1 && name.length < 40 && !name.includes('模式') && !name.includes('聊天') && !name.includes('幻灯片')) {
                                models.push({ name });
                            }
                        }
                    }
                }
                return models;
            }""")

            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            for m in model_list:
                name = m.get('name', '').strip()
                model_id = "zai-" + name.lower().replace(" ", "-").replace(".", "-")
                models.append({"display_name": name, "model_id": model_id})
            logger.info(f"[Zai] Fetched models: {[m['display_name'] for m in models]}")

        except Exception as e:
            logger.error(f"[Zai] Failed to fetch models: {e}")
        return models

    async def select_zai_model(self, model_name: str) -> bool:
        """在 Zai 页面上点击模型选择器中的目标模型。

        Args:
            model_name: 模型ID（如 'zai-glm-5.1', 'zai-glm-5.2'），会自动映射为页面显示名称。
        """
        headless = CONFIG.get('_zai_headless', True)
        await self.ensure_zai_ready(headless=headless)
        page = self._zai_page
        if not page:
            return False

        from models import ZAI_MODEL_CONFIG
        cfg = ZAI_MODEL_CONFIG.get(model_name, {})
        display_name = cfg.get("display_name", model_name.replace("zai-", ""))

        try:
            # 1. 读取当前选中的模型
            current = await page.evaluate("""() => {
                const btn = document.querySelector('.modelSelectorButton, button[aria-label="选择一个模型"]');
                return btn ? btn.textContent.trim() : null;
            }""")

            logger.info(f"[Zai] Current model: '{current}', target: '{display_name}'")

            if current and (display_name in current or current in display_name):
                logger.info(f"[Zai] Already on model: {current}")
                return True

            # 2. 点击模型选择器按钮
            btn = page.locator('.modelSelectorButton, button[aria-label="选择一个模型"]').first
            if await btn.count() == 0:
                logger.warning("[Zai] Model selector button not found")
                return False

            # 等待按钮可用（z.ai 生成回复时该按钮会 disabled）
            for _ in range(120):  # 最多等 60 秒
                try:
                    disabled = await page.evaluate("""() => {
                        const btn = document.querySelector('.modelSelectorButton, button[aria-label="选择一个模型"]');
                        return btn ? (btn.disabled || btn.hasAttribute('disabled') || btn.getAttribute('data-disabled') !== null) : true;
                    }""")
                    if not disabled:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            else:
                logger.warning("[Zai] Model selector button stayed disabled for 60s")
                return False

            await btn.click()
            await asyncio.sleep(1.5)

            # 3. 在下拉面板中点击目标模型
            clicked = await page.evaluate(f"""() => {{
                // 尝试通过 data-value 精确匹配
                const exactBtn = document.querySelector('[data-value="{display_name}"]');
                if (exactBtn) {{
                    exactBtn.click();
                    return '{display_name}';
                }}
                // 尝试文本匹配
                const allBtns = document.querySelectorAll('button, [role="option"], [tabindex]');
                for (const btn of allBtns) {{
                    const txt = (btn.textContent || '').split('NEW')[0].trim();
                    if (txt === '{display_name}' || txt.includes('{display_name}')) {{
                        btn.click();
                        return txt;
                    }}
                }}
                return null;
            }}""")

            if clicked:
                logger.info(f"[Zai] Model switched to: {clicked}")
                await asyncio.sleep(1)
                return True
            else:
                logger.warning(f"[Zai] Model '{display_name}' not found in selector")
                await page.keyboard.press("Escape")
                return False

        except Exception as e:
            logger.error(f"[Zai] Failed to select model: {e}")
            return False

    async def stream_zai_chat(self, prompt: str, model_type: str = "glm-4.7", thinking_enabled: bool = False, search_enabled: bool = True, inline_file_content: str | None = None, model_name: str | None = None, reuse_conversation: bool = False, conversation_id: str | None = None):
        """z.ai 流式对话：先上传文件等待解析完成，再创建聊天 + SSE 流式解析。

        Args:
            model_name: 模型ID（如 'zai-glm-5.1', 'zai-glm-5.2'），用于在页面模型选择器中切换模型。
        """
        headless = CONFIG.get('_zai_headless', True)
        await self.ensure_zai_ready(headless=headless)
        await self._dismiss_zai_popups()
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._zai_queues[stream_id] = q
        session_id = ""
        uploaded_file_id: str | None = None

        # 队列通过 stream_id 索引，避免并发覆盖
        self._zai_active_stream = stream_id

        # 注册 JS→Python 桥接函数（仅注册一次）
        if not getattr(self, "_zai_bridge_registered", False):
            async def _stable_sse_callback(chunk_json: str):
                active_stream = getattr(self, "_zai_active_stream", None)
                target_q = self._zai_queues.get(active_stream)
                if target_q is None:
                    return
                try:
                    chunk_str = str(chunk_json).strip()
                    data = json.loads(chunk_str)
                    event_type = data.get("type", "")
                    event_data = data.get("data", {})

                    if event_type == "chat:completion":
                        delta = event_data.get("delta_content", "")
                        phase = event_data.get("phase", "answer")
                        if delta and phase != "thinking":
                            # 过滤掉 "思考过程" 前缀（z.ai 可能将思考过程和答案拼接）
                            delta = delta.replace("思考过程", "").replace("reasoning", "").strip()
                            if delta:
                                target_q.put_nowait(("chunk", delta))
                        elif not delta:
                            pass
                    elif event_type == "chat:completion:done":
                        target_q.put_nowait(("done", ""))
                    elif event_type == "chat:completion:error":
                        target_q.put_nowait(("error", event_data.get("message", "z.ai error")))
                        target_q.put_nowait(("done", ""))
                    else:
                        if event_data.get("done"):
                            target_q.put_nowait(("done", ""))
                        else:
                            logger.debug(f"[Zai] SSE callback: event_type={event_type}, data={event_data}")
                except Exception as e:
                    logger.debug(f"[Zai] sse chunk parse: {e}")

            try:
                # 先注册桥接函数
                await self._zai_page.expose_function("zaiOnSseChunk", _stable_sse_callback)
                self._zai_bridge_registered = True

                #  drains 缓冲区中积累的事件（页面加载时拦截器可能已经捕获了事件）
                try:
                    buffered = await self._zai_page.evaluate("""() => {
                        const events = window.__zai_sse_events || [];
                        window.__zai_sse_events = [];
                        window.__zai_sse_flushed = true;
                        return events;
                    }""")
                    if buffered:
                        logger.info(f"[Zai] draining {len(buffered)} buffered SSE events")
                        for event_json in buffered:
                            try:
                                await _stable_sse_callback(event_json)
                            except Exception as e:
                                logger.debug(f"[Zai] drain event error: {e}")
                except Exception as e:
                    logger.debug(f"[Zai] drain buffered events: {e}")
            except (ValueError, AttributeError):
                self._zai_bridge_registered = True

        # 0. 模型切换：如果提供了 model_name，先切换到目标模型
        if model_name:
            await self.select_zai_model(model_name)
            await asyncio.sleep(1)

        # 1. 从URL捕获session_id
        try:
            url = self._zai_page.url
            if '/c/' in url:
                session_id = url.split('/c/')[1].split('?')[0].split('#')[0]
                logger.info(f"[Zai] initial session_id: {session_id}")
        except Exception:
            pass

        # 2. 文件上传（通过隐藏的 input[type=file] + 路由拦截获取完整文件信息）
        uploaded_file_info = {}

        async def upload_file() -> bool:
            nonlocal uploaded_file_info
            if not inline_file_content:
                return True

            file_future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

            async def handle_upload_route(route):
                try:
                    resp = await route.fetch()
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if not file_future.done():
                                file_future.set_result(data)
                                logger.info(f"[Zai] upload API returned file data: id={data.get('id')}")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[Zai] upload route error: {e}")
                finally:
                    try:
                        await route.fulfill()
                    except Exception:
                        pass

            await self._zai_page.route("**/api/v1/files/**", handle_upload_route)

            result = await self._zai_page.evaluate("""async (content) => {
                try {
                    const blob = new Blob([content], { type: 'text/plain' });
                    const file = new File([blob], 'request.txt', { type: 'text/plain', lastModified: Date.now() });
                    const fileInput = document.querySelector('input[type="file"]');
                    if (!fileInput) return { error: 'no file input found' };
                    const dt = new DataTransfer();
                    dt.items.add(file);
                    fileInput.files = dt.files;
                    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
                    return { dispatched: true };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", inline_file_content)

            if result.get("error"):
                logger.warning(f"[Zai] file upload JS error: {result['error']}")
                await self._zai_page.unroute("**/api/v1/files/**", handle_upload_route)
                return False

            try:
                data = await asyncio.wait_for(file_future, timeout=60)
                uploaded_file_info = data
                logger.info(f"[Zai] file upload complete, file_id={data.get('id')}")
                return True
            except asyncio.TimeoutError:
                logger.warning("[Zai] file upload timeout (no file response received)")
                return False
            finally:
                await self._zai_page.unroute("**/api/v1/files/**", handle_upload_route)

        # 3. 执行文件上传（如果需要） — 仅在新会话且提供文件内容时上传
        if inline_file_content and not reuse_conversation:
            logger.info("[Zai] starting file upload...")
            upload_ok = await upload_file()
            if not upload_ok:
                logger.warning("[Zai] file upload failed, proceeding without file")
            else:
                logger.info(f"[Zai] file upload complete, file_id={uploaded_file_info.get('id')}")

        # 4. 让页面前端发送消息，我们拦截SSE响应
        async def call_zai_api():
            nonlocal session_id

            prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)

            # 4a. 切换思考和搜索开关
            try:
                await self._zai_page.evaluate("""({thinking, search}) => {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.dataset.autoThink !== undefined) {
                            const cur = b.dataset.autoThink === 'true';
                            if (thinking !== cur) b.click();
                        }
                        if (b.dataset.autoSearch !== undefined) {
                            const cur = b.dataset.autoSearch === 'true';
                            if (search !== cur) b.click();
                        }
                    }
                }""", {"thinking": thinking_enabled, "search": search_enabled})
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"[Zai] thinking toggle failed (non-critical): {e}")

            # 4b. 注意：fetch 拦截器已在 add_init_script 中注入（页面加载前）

            # 4c. 通过Svelte store设置文件信息（如果有），然后填入消息并回车
            file_set = False
            if uploaded_file_info and uploaded_file_info.get('id'):
                fid = uploaded_file_info['id']
                meta = uploaded_file_info.get('meta', {})
                file_set = await self._zai_page.evaluate("""({fid, meta}) => {
                    try {
                        // 通过window对象找到Svelte store并设置文件
                        const fileObj = {
                            id: fid,
                            type: 'file',
                            name: meta.name || 'request.txt',
                            url: '/api/v1/files/' + fid,
                            status: 'uploaded',
                            size: meta.size || 0,
                            file: { id: fid, meta: meta }
                        };
                        // 尝试通过input事件触发文件添加
                        const input = document.querySelector('input[type="file"]');
                        if (input) {
                            const dt = new DataTransfer();
                            // 如果已有文件则保留
                            if (input.files) {
                                for (const f of input.files) dt.items.add(f);
                            }
                            // 创建新File对象
                            const blob = new Blob([''], {type:'text/plain'});
                            const file = new File([blob], meta.name || 'request.txt', {type:'text/plain'});
                            dt.items.add(file);
                            input.files = dt.files;
                            input.dispatchEvent(new Event('change', {bubbles:true}));
                            return true;
                        }
                        return false;
                    } catch(e) {
                        console.error('[zai-file-set]', e);
                        return false;
                    }
                }""", {"fid": fid, "meta": meta})
                if file_set:
                    logger.info(f"[Zai] file info set via input")
                    await asyncio.sleep(2)

            # 4d. 处理弹窗 → 确认textarea → 填入消息并发送
            logger.info(f"[Zai] dismissing popups before entering message...")
            
            # 多次处理弹窗，确保页面元素可访问（减少次数：内部已有超时保护）
            for i in range(2):
                logger.debug(f"[Zai] dismiss popups attempt {i+1}/2")
                await self._dismiss_zai_popups()
                await asyncio.sleep(0.3)
            
            logger.info(f"[Zai] dismiss popups done, checking page state...")
            
            # 确认页面已加载textarea（如果不在主页则导航）
            cur_url = self._zai_page.url
            logger.info(f"[Zai] current URL: {cur_url}")
            if '/auth' in cur_url:
                logger.warning("[Zai] Still on /auth, navigating to main page...")
                await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
                for _ in range(3):
                    await self._dismiss_zai_popups()
                    await asyncio.sleep(0.5)
            
            # 等待textarea出现
            logger.info(f"[Zai] waiting for textarea selector...")
            try:
                await self._zai_page.wait_for_selector("textarea", timeout=15000)
                logger.info(f"[Zai] textarea found")
            except:
                logger.warning("[Zai] textarea not found, trying dismiss popups and reload...")
                for _ in range(3):
                    await self._dismiss_zai_popups()
                    await asyncio.sleep(0.5)
                await self._zai_page.reload(wait_until="domcontentloaded", timeout=60000)
                for _ in range(3):
                    await self._dismiss_zai_popups()
                    await asyncio.sleep(0.5)
                try:
                    await self._zai_page.wait_for_selector("textarea", timeout=15000)
                except:
                    logger.error("[Zai] textarea still not found after reload")
                    raise Exception("z.ai textarea not found")

            # 输入消息：短文本逐字输入（更像真人），长文本直接粘贴（避免超时）
            import random as _random
            textarea = self._zai_page.locator("textarea")
            await textarea.click()
            await asyncio.sleep(0.3 + _random.random() * 0.5)
            
            # 清空现有内容（如果有）
            await self._zai_page.keyboard.press("Control+A")
            await asyncio.sleep(0.1)
            await self._zai_page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)

            if len(prompt_text) > 200:
                # 长文本：直接粘贴
                await self._zai_page.evaluate("(text) => { const ta = document.querySelector('textarea'); ta.focus(); const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set; setter.call(ta, text); ta.dispatchEvent(new Event('input', {bubbles:true})); }", prompt_text)
                logger.info(f"[Zai] pasted prompt ({len(prompt_text)} chars)")
                await asyncio.sleep(0.5)
            else:
                # 短文本：逐字输入
                for ch in prompt_text:
                    await self._zai_page.keyboard.type(ch, delay=50 + int(100 * _random.random()))
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.5)

            # 发送：按 Enter 或点击发送按钮
            send_clicked = await self._zai_page.evaluate("""() => {
                // 尝试按 Enter（如果表单允许）
                const form = document.querySelector('form');
                if (form) {
                    const ev = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles:true});
                    const textarea = document.querySelector('textarea');
                    if (textarea) textarea.dispatchEvent(ev);
                    return 'form-enter';
                }
                // 否则点击发送按钮
                const btns = Array.from(document.querySelectorAll('button'));
                const sendBtn = btns.find(b => {
                    const txt = (b.textContent || '').trim();
                    return txt === '' || txt === '发送' || b.getAttribute('aria-label')?.includes('send') || b.querySelector('[class*="send"]');
                }) || btns[0];
                if (sendBtn) {
                    sendBtn.click();
                    return 'button-click';
                }
                return 'none';
            }""")
            logger.info(f"[Zai] sent message via: {send_clicked}")
            await asyncio.sleep(0.5)

            # 4e. 等待SSE数据通过zaiOnSseChunk桥接到Python队列（最多2分钟）
            logger.info(f"[Zai] waiting for SSE response...")
            # session_id 从页面URL获取
            try:
                url = self._zai_page.url
                if '/c/' in url:
                    session_id = url.split('/c/')[1].split('?')[0].split('#')[0]
            except Exception:
                pass

        # 5. 执行 API 调用（文件已上传）
        await call_zai_api()

        # 6. yield session_id
        if session_id:
            yield ("session_id", session_id)
        

        # 7. 从 SSE 队列消费流式响应（主路径）
        logger.info("[Zai] consuming SSE queue for response content...")
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        total_timeout = 120  # seconds
        sse_seen = False

        while True:
            if loop.time() - start_time > total_timeout:
                logger.warning("[Zai] total timeout exceeded")
                yield ("error", "z.ai response timeout")
                yield ("done", "")
                break

            try:
                q_item = await asyncio.wait_for(q.get(), timeout=2.0)
                sse_seen = True
                event_type, content = q_item

                if event_type == "chunk":
                    if content.strip():
                        yield ("chunk", content)
                elif event_type == "done":
                    logger.info("[Zai] SSE stream done")
                    yield ("done", "")
                    break
                elif event_type == "error":
                    logger.error(f"[Zai] SSE stream error: {content}")
                    yield ("error", content)
                    yield ("done", "")
                    break
                else:
                    logger.debug(f"[Zai] SSE unexpected event type: {event_type}")
            except asyncio.TimeoutError:
                # If SSE never started after 30s, fallback to DOM polling as last resort
                if not sse_seen and loop.time() - start_time > 30:
                    logger.warning("[Zai] no SSE events after 30s, using DOM fallback")
                    try:
                        # Single DOM snapshot as fallback
                        result = await self._zai_page.evaluate("""() => {
                            const all = document.querySelectorAll('[class*="markdown-prose"]');
                            const candidates = [];
                            for (const el of all) {
                                const cls = (el.className || '').toLowerCase();
                                if (cls.includes('chat-user')) continue;
                                const txt = (el.textContent || '').trim();
                                if (txt.length > 0) candidates.push(txt);
                            }
                            return candidates.length > 0 ? candidates[0] : '';
                        }""")
                        if result:
                            yield ("chunk", result)
                        yield ("done", "")
                        break
                    except Exception as e:
                        logger.debug(f"[Zai] DOM fallback error: {e}")
                        yield ("error", "z.ai response could not be retrieved")
                        yield ("done", "")
                        break
                # otherwise keep waiting for SSE
                continue
        # Cleanup
        self._zai_queues.pop(stream_id, None)
        self._zai_active_stream = None

    async def get_zai_session_id(self) -> str:
        """从 z.ai 页面 URL 提取当前会话 ID。"""
        try:
            if not self._zai_page:
                return ""
            url = self._zai_page.url
            if '/c/' in url:
                sid = url.split('/c/')[1]
                sid = sid.split('?')[0].split('#')[0]
                if sid:
                    return sid
            return ""
        except Exception:
            return ""

    async def delete_zai_conversation(self, session_id: str):
        """删除单个 z.ai 会话。DELETE /api/v1/chats body={chat_ids:[id]}"""
        if not session_id:
            return
        try:
            result = await self._zai_page.evaluate("""async (params) => {
                const [token, sid] = params;
                try {
                    const resp = await fetch('/api/v1/chats', {
                        method: 'DELETE',
                        headers: {
                            'Authorization': `Bearer ${token}`,
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ chat_ids: [sid] })
                    });
                    const text = await resp.text();
                    return { ok: resp.ok, status: resp.status, body: text.substring(0, 200) };
                } catch (e) {
                    return { ok: false, error: String(e) };
                }
            }""", [await self._zai_page.evaluate("localStorage.getItem('token') || ''"), session_id])
            if result.get('ok'):
                logger.info(f"[Zai] deleted session {session_id}")
            else:
                logger.warning(f"[Zai] delete failed: {json.dumps(result, ensure_ascii=False)[:200]}")
        except Exception as e:
            logger.warning(f"[Zai] delete exception: {e}")

    async def delete_all_zai_conversations(self):
        """删除所有 z.ai 会话：先列出再批量删除。"""
        try:
            if not self._zai_page or self._zai_page.is_closed():
                return
            token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
            if not token:
                logger.warning("[Zai] no token, cannot delete conversations")
                return
            result = await self._zai_page.evaluate("""async (tok) => {
                try {
                    // 步骤1: 列出所有会话
                    const listResp = await fetch('/api/v1/chats', {
                        headers: { 'Authorization': `Bearer ${tok}`, 'Content-Type': 'application/json' }
                    });
                    const chats = await listResp.json();
                    if (!chats || chats.length === 0) return { deleted: 0 };

                    // 步骤2: 批量删除
                    const ids = chats.map(c => c.id);
                    const delResp = await fetch('/api/v1/chats', {
                        method: 'DELETE',
                        headers: { 'Authorization': `Bearer ${tok}`, 'Content-Type': 'application/json' },
                        body: JSON.stringify({ chat_ids: ids })
                    });
                    return { deleted: ids.length, ok: delResp.ok, body: (await delResp.text()).substring(0, 100) };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", token)
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            logger.info(f"[Zai] delete_all: deleted {deleted} sessions")
        except Exception as e:
            logger.warning(f"[Zai] delete_all exception: {e}")

    async def close_zai(self):
        """Close Zai browser context and cleanup resources."""
        if self._zai_page and not self._zai_page.is_closed():
            try:
                await self._zai_page.close()
                logger.info("[Zai] page closed")
            except Exception as e:
                logger.warning(f"[Zai] close page error: {e}")
        self._zai_page = None
        self._zai_bridge_registered = False
        self._zai_queues.clear()
        logger.info("[Zai] resources cleaned up")

    async def _dismiss_zai_popups(self):
        """处理 Zai 页面上的各种弹窗：关闭公告、同意协议、确认提示等。"""
        if not self._zai_page or self._zai_page.is_closed():
            return
        try:
            # 多次尝试处理弹窗（弹窗可能延迟加载）
            for attempt in range(3):
                # 1. 自动点击登录按钮（如果页面显示登录提示）
                try:
                    await asyncio.wait_for(self._zai_page.evaluate(r"""() => {
                        const loginPatterns = ['登录', '登 录', 'Login', 'Sign in', 'Log in', '登录/注册', 'Sign up'];
                        const allBtns = document.querySelectorAll('button, a, [role="button"], [class*="login"], [class*="Login"]');
                        for (const btn of allBtns) {
                            const txt = (btn.textContent || '').trim();
                            const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                            const cls = (btn.className || '').toLowerCase();
                            if (loginPatterns.some(p => txt.includes(p) || ariaLabel.includes(p.toLowerCase()))) {
                                if (txt.includes('注册') && !txt.includes('登录')) continue;
                                if (txt.includes('取消') || txt.includes('关闭') || txt.includes('Cancel') || txt.includes('Close')) continue;
                                try { btn.click(); return true; } catch(e) {}
                            }
                        }
                        return false;
                    }"""), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.debug(f"[Zai] dismiss popups step1 timeout (attempt {attempt+1})")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups step1 error: {e}")

                # 2. 关闭按钮、同意按钮、遮罩层、弹窗点击
                try:
                    await asyncio.wait_for(self._zai_page.evaluate(r"""() => {
                        const closeSelectors = [
                            'button[aria-label="关闭"]', 'button[aria-label="Close"]',
                            'button[aria-label="关闭公告"]', '[class*="close"]', '[data-testid*="close"]',
                            '[class*="modal"] button:first-child', '[class*="dialog"] button:first-child'
                        ];
                        for (const sel of closeSelectors) {
                            document.querySelectorAll(sel).forEach(btn => {
                                try { if (btn.offsetParent !== null) btn.click(); } catch(e) {}
                            });
                        }
                        const agreePatterns = ['同意', '接受', '确定', '确认', 'Agree', 'Accept', 'OK', 'Confirm', 
                                              'Got it', '知道了', '我已知晓', '我再想想', '继续', 'Continue'];
                        document.querySelectorAll('button, [role="button"], a[role="button"]').forEach(btn => {
                            const txt = (btn.textContent || '').trim();
                            const cls = (btn.className || '').toLowerCase();
                            if (agreePatterns.some(p => txt === p || txt.includes(p))) {
                                if (cls.includes('primary') || cls.includes('confirm') || cls.includes('agree') || 
                                    cls.includes('submit') || cls.includes('bg-') || cls.includes('solid')) {
                                    try { if (btn.offsetParent !== null) btn.click(); } catch(e) {}
                                }
                            }
                        });
                        document.querySelectorAll('[class*="overlay"], [class*="mask"], [class*="backdrop"], [class*="modal-overlay"]').forEach(el => {
                            if (el.style && el.onclick === null && el.offsetParent !== null) {
                                try { el.click(); } catch(e) {}
                            }
                        });
                        document.querySelectorAll('[class*="modal"], [class*="dialog"], [class*="popup"]').forEach(el => {
                            if (el.style && el.style.display !== 'none' && el.onclick === null) {
                                const hasContent = el.querySelector('button, input, [class*="content"]');
                                if (!hasContent) { try { el.click(); } catch(e) {} }
                            }
                        });
                    }"""), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.debug(f"[Zai] dismiss popups step2 timeout (attempt {attempt+1})")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups step2 error: {e}")

                # 3. 按 Escape 键
                try:
                    await self._zai_page.keyboard.press("Escape")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups Escape error: {e}")
                await asyncio.sleep(0.5)

                # 4. 检查是否还有弹窗
                try:
                    has_modal = await asyncio.wait_for(self._zai_page.evaluate("""() => {
                        const modals = document.querySelectorAll('[class*="modal"], [class*="dialog"], [class*="popup"]');
                        for (const m of modals) {
                            if (m.style && m.style.display !== 'none' && m.offsetParent !== null && m.style.zIndex > 100) {
                                return true;
                            }
                        }
                        return false;
                    }"""), timeout=5.0)
                    if not has_modal:
                        break
                except asyncio.TimeoutError:
                    logger.debug(f"[Zai] dismiss popups has_modal timeout (attempt {attempt+1})")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups has_modal error: {e}")
        except Exception as e:
            logger.debug(f"[Zai] dismiss popups error: {e}")

