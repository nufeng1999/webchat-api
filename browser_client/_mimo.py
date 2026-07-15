from ._shared import *


class MimoMixin:
    async def activate_mimo_conversation(self, session_id: str) -> bool:
        """导航到 MiMo 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_mimo_ready()
            # MiMo SPA hash router: after sending, URL contains '/chat/{session_id}'
            url = f"https://aistudio.xiaomimimo.com/#/chat/{session_id}"
            await self._mimo_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[MiMo] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[MiMo] activate conversation failed: {e}")
            return False

    async def fetch_mimo_models(self) -> list[dict]:
        """从 MiMo 页面模型下拉面板中获取可用模型列表。"""
        headless = CONFIG.get('_mimo_headless', True)
        await self.ensure_mimo_ready(headless=headless)
        page = self._mimo_page
        if not page:
            return []

        models = []
        try:
            clicked = await page.evaluate("""() => {
                const all = document.querySelectorAll('div');
                for (const el of all) {
                    const text = (el.textContent || '').trim();
                    const cls = el.className || '';
                    const rect = el.getBoundingClientRect();
                    if (rect.top < 60 && rect.height > 0 && rect.height < 40 &&
                        cls.includes('cursor-pointer') &&
                        (text.includes('MiMo') || text.includes('V2.5') || text.includes('Pro'))) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            if not clicked:
                logger.warning("[MiMo] Model selector trigger not found")
                return []
            await asyncio.sleep(1.5)

            model_list = await page.evaluate("""() => {
                const models = [];
                const btns = document.querySelectorAll('button[data-track-id^="model_selector_"]');
                for (const btn of btns) {
                    const name = btn.getAttribute('data-track-name') || '';
                    const isActive = btn.className.includes('bg-mimo-fill-neutral-active');
                    const descEl = btn.querySelector('.text-mimo-text-h3-placeholder');
                    const desc = descEl ? descEl.textContent.trim() : '';
                    models.push({ name, desc, isActive });
                }
                return models;
            }""")

            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            for m in model_list:
                model_id = "mimo-" + m['name'].lower().replace(".", "-").replace(" ", "-")
                models.append({"display_name": m['name'], "model_id": model_id, "desc": m['desc'], "is_active": m['isActive']})
            logger.info(f"[MiMo] Fetched models: {[m['display_name'] for m in models]}")

        except Exception as e:
            logger.error(f"[MiMo] Failed to fetch models: {e}")
        return models

    async def select_mimo_model(self, model_name: str) -> bool:
        """在 MiMo 页面顶部的模型下拉面板中选择目标模型。"""
        headless = CONFIG.get('_mimo_headless', True)
        await self.ensure_mimo_ready(headless=headless)
        page = self._mimo_page
        if not page:
            return False

        from models import MIMO_MODEL_CONFIG
        cfg = MIMO_MODEL_CONFIG.get(model_name, {})
        display_name = cfg.get("display_name", model_name.replace("mimo-", "MiMo-").replace("-", "."))

        try:
            # 1. 读取当前选中的模型
            current = await page.evaluate("""() => {
                const nav = document.querySelector('nav, [class*="navbar"], [class*="nav"]');
                if (nav) {
                    const divs = nav.querySelectorAll('div[class*="cursor-pointer"]');
                    for (const d of divs) {
                        const txt = (d.textContent || '').trim();
                        if (txt.includes('MiMo') || txt.includes('V2')) {
                            return txt.replace('New', '').replace(/\\s+/g, ' ').trim();
                        }
                    }
                }
                return null;
            }""")
            logger.info(f"[MiMo] Current model: '{current}', target: '{display_name}'")

            if current and display_name in current:
                logger.info(f"[MiMo] Already on model: {current}")
                return True

            # 2. 用 Playwright 原生定位器点击触发器
            logger.info(f"[MiMo] Clicking model selector trigger for: {display_name}")
            trigger = page.locator('nav div[class*="cursor-pointer"]').filter(has_text="MiMo").first
            if await trigger.count() == 0:
                logger.warning("[MiMo] Model selector trigger not found")
                return False
            await trigger.click()
            await asyncio.sleep(1.5)

            # 3. 用 Playwright 定位器查找下拉面板中的目标模型
            # data-track-name 在 button 或 div 上
            target_el = page.locator(f'[data-track-name="{display_name}"]').first
            if await target_el.count() > 0:
                await target_el.click()
                logger.info(f"[MiMo] Model switched to: {display_name}")
                await asyncio.sleep(1)
                return True

            # 模糊匹配
            all_options = page.locator('[data-dropdown-menu] [data-track-name]')
            count = await all_options.count()
            available = []
            for i in range(count):
                el = all_options.nth(i)
                name = await el.get_attribute("data-track-name")
                available.append(name)
                if display_name in name or name in display_name:
                    await el.click()
                    logger.info(f"[MiMo] Model switched to: {name}")
                    await asyncio.sleep(1)
                    return True

            logger.warning(f"[MiMo] Model '{display_name}' not found. Available: {available}")
            await page.keyboard.press("Escape")
            return False

        except Exception as e:
            logger.error(f"[MiMo] Failed to select model: {e}")
            return False

    async def ensure_mimo_ready(self, headless: bool = True):
        """确保 MiMo 浏览器已启动并登录。"""
        if self._mimo_page and not self._mimo_page.is_closed():
            return

        logger.info("[MiMo] Starting MiMo browser...")
        from playwright.async_api import async_playwright
        self._mimo_pw = await async_playwright().start()
        self._mimo_browser = await self._mimo_pw.chromium.launch_persistent_context(
            user_data_dir=self._mimo_user_data_dir,
            headless=headless,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._mimo_page = self._mimo_browser.pages[0] if self._mimo_browser.pages else await self._mimo_browser.new_page()

        await self._mimo_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        logger.info("[MiMo] navigating to aistudio.xiaomimimo.com...")
        await self._mimo_page.goto("https://aistudio.xiaomimimo.com/#/c", wait_until="domcontentloaded", timeout=60000)
        # 等待 textarea 出现（说明 React SPA 已渲染完成）
        try:
            await self._mimo_page.wait_for_selector("textarea", timeout=30000)
            logger.info("[MiMo] textarea found, page rendered")
        except:
            logger.warning("[MiMo] textarea not found after 30s, continuing anyway")
            await asyncio.sleep(3)

        try:
            await self._mimo_page.locator('button[aria-label="关闭公告"]').first.click(timeout=3000)
            await asyncio.sleep(1)
        except:
            pass

        await self._dismiss_mimo_popups()

        ph = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
        if ph and '登录' in ph:
            logger.warning("[MiMo] Not logged in, trying auto-login button first...")
            # 尝试点击页面上的登录按钮（如果存在）
            clicked = await self._try_mimo_auto_login()
            if clicked:
                await asyncio.sleep(3)
                ph2 = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
                if ph2 and '登录' not in ph2:
                    logger.info("[MiMo] Auto-login succeeded")
                    # 登录成功，继续后续流程
                else:
                    logger.info("[MiMo] Auto-login button click did not result in login")
            else:
                logger.info("[MiMo] No auto-login button found")

            # 再次检查登录状态
            ph3 = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
            if ph3 and '登录' in ph3:
                logger.warning("[MiMo] Still not logged in, showing browser for manual login")
                if headless:
                    await self._mimo_login_recovery()
        else:
            logger.info("[MiMo] Logged in, textarea placeholder: " + (ph or "N/A"))

        logger.info("[MiMo] MiMo browser ready")

    async def _try_mimo_auto_login(self) -> bool:
        """尝试自动点击 MiMo 页面上的登录按钮。返回是否找到了并点击了登录按钮。"""
        if not self._mimo_page or self._mimo_page.is_closed():
            return False
        try:
            return await self._mimo_page.evaluate(r"""() => {
                const loginPatterns = ['登录', '登 录', 'Login', 'Sign in', 'Log in'];
                const allBtns = document.querySelectorAll('button, a, [role="button"], [class*="login"], [class*="Login"]');
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim();
                    const ariaLabel = btn.getAttribute('aria-label') || '';
                    const href = btn.getAttribute('href') || '';
                    if (loginPatterns.some(p => txt.includes(p) || ariaLabel.includes(p))) {
                        if (txt.length < 20 && btn.getBoundingClientRect().height > 0) {
                            btn.click();
                            return true;
                        }
                    }
                    if (href && loginPatterns.some(p => href.toLowerCase().includes(p.toLowerCase()))) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
        except Exception as e:
            logger.debug(f"[MiMo] auto-login attempt error: {e}")
            return False

    async def _dismiss_mimo_popups(self):
        """处理 MiMo 页面上的各种弹窗：关闭公告、同意协议、确认提示等。"""
        if not self._mimo_page or self._mimo_page.is_closed():
            return
        try:
            await self._mimo_page.evaluate(r"""() => {
                // 1. 关闭公告弹窗
                const closeBtns = document.querySelectorAll('button[aria-label="关闭公告"], button[aria-label="Close"], [class*="close"][class*="announcement"], [data-track-id*="close"]');
                for (const btn of closeBtns) {
                    try { btn.click(); } catch(e) {}
                }
                // 2. 同意/接受/确定 按钮
                const agreePatterns = ['同意', '接受', '确定', '确认', ' Agree', 'Accept', 'OK', 'Confirm', 'Got it', '知道了', '我已知晓'];
                const allBtns = document.querySelectorAll('button, [role="button"], a[role="button"]');
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim();
                    const cls = btn.className || '';
                    if (agreePatterns.some(p => txt.includes(p)) || btn.getAttribute('aria-label') === '同意') {
                        if (cls.includes('primary') || cls.includes('confirm') || cls.includes('agree') || cls.includes('submit')) {
                            try { btn.click(); } catch(e) {}
                        }
                    }
                }
                // 3. 关闭遮罩层弹窗（点击遮罩关闭）
                const overlays = document.querySelectorAll('[class*="overlay"], [class*="mask"], [class*="backdrop"]');
                for (const overlay of overlays) {
                    if (overlay.style && overlay.onclick === null) {
                        try { overlay.click(); } catch(e) {}
                    }
                }
                // 4. Escape 关闭模态弹窗
            }""")
            await self._mimo_page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"[MiMo] dismiss popups: {e}")

    async def _mimo_login_recovery(self):
        """显示浏览器让用户登录，等待登录完成后继续。"""
        logger.warning("[MiMo] Login required, switching to headful mode...")
        if not self._mimo_page:
            return
        try:
            await self._mimo_browser.close()
        except Exception:
            pass

        from playwright.async_api import async_playwright

        self._mimo_pw = await async_playwright().start()
        self._mimo_browser = await self._mimo_pw.chromium.launch_persistent_context(
            user_data_dir=self._mimo_user_data_dir,
            headless=False,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._mimo_page = self._mimo_browser.pages[0] if self._mimo_browser.pages else await self._mimo_browser.new_page()
        await self._mimo_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        await self._mimo_page.goto("https://aistudio.xiaomimimo.com/#/c", wait_until="domcontentloaded", timeout=60000)

        logger.info("[MiMo] Please login in the browser window. Waiting up to 180s...")
        for i in range(180):
            await asyncio.sleep(1)
            try:
                ph = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
                if ph and '登录' not in ph:
                    logger.info(f"[MiMo] Login detected at {i}s!")
                    await asyncio.sleep(3)
                    break
            except:
                pass
        else:
            logger.warning("[MiMo] Login timeout after 180s")

    async def stream_mimo_chat(self, prompt: str, model_type: str = "default", thinking_enabled: bool = False, search_enabled: bool = False, inline_file_content: str | None = None, model_name: str | None = None):
        """MiMo 流式对话：上传文件→等待解析→发送消息→SSE流式解析。

        Args:
            model_name: 模型ID（如 'mimo-v2.5-pro'），用于在页面模型选择器中切换模型。
        """
        headless = CONFIG.get('_mimo_headless', True)
        await self.ensure_mimo_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._mimo_queues[stream_id] = q
        session_id = ""

        # 0. 模型切换：如果提供了 model_name，先切换到目标模型
        if model_name:
            await self.select_mimo_model(model_name)
            await asyncio.sleep(1)

        # 1. 注册消息回调
        async def _on_mimo_chunk(chunk_json: str):
            try:
                data = json.loads(chunk_json)
                event_type = data.get("type", "")
                if event_type == "text":
                    content = data.get("content", "")
                    if content:
                        q.put_nowait(("chunk", content))
                elif event_type == "finish":
                    q.put_nowait(("done", ""))
                elif event_type == "error":
                    msg = data.get("message", "mimo error")
                    q.put_nowait(("error", msg))
                    q.put_nowait(("done", ""))
            except Exception as e:
                logger.debug(f"[MiMo] chunk parse error: {e}")

        try:
            await self._mimo_page.expose_function("mimoOnChunk", _on_mimo_chunk)
        except Exception:
            pass

        # 2. 文件上传
        async def upload_file(content: str | None) -> bool:
            """上传文件并等待解析完成。返回 True 表示成功。"""
            if not content:
                return True
            try:
                result = await self._mimo_page.evaluate("""async (content) => {
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
                }""", content)

                if result.get("error"):
                    logger.warning(f"[MiMo] file upload JS error: {result['error']}")
                    return False

                await asyncio.sleep(5)

                for i in range(120):
                    ready = await self._mimo_page.evaluate(r"""() => {
                        const els = document.querySelectorAll('*');
                        for (const el of els) {
                            const text = (el.textContent || '').trim();
                            if (text.includes('request.txt')) {
                                // 向上查找父级元素，检查是否包含文件大小信息
                                let parent = el.parentElement;
                                for (let j = 0; j < 5 && parent; j++) {
                                    const parentText = parent.textContent || '';
                                    if (/\d+\s*(B|KB|MB)/.test(parentText)) {
                                        return true;
                                    }
                                    parent = parent.parentElement;
                                }
                                // 也检查同级兄弟元素
                                const siblings = el.parentElement ? el.parentElement.children : [];
                                for (const sib of siblings) {
                                    const sibText = sib.textContent || '';
                                    if (/\d+\s*(B|KB|MB)/.test(sibText)) {
                                        return true;
                                    }
                                }
                            }
                        }
                        return false;
                    }""")
                    if ready:
                        logger.info("[MiMo] file parsing complete (card visible)")
                        return True
                    await asyncio.sleep(1)
                logger.warning("[MiMo] file parse timeout after 120s")
                return False
            except Exception as e:
                logger.warning(f"[MiMo] file upload error: {e}")
                return False

        if inline_file_content:
            logger.info("[MiMo] starting file upload...")
            upload_ok = await upload_file(inline_file_content)
            if not upload_ok:
                logger.error("[MiMo] file upload failed, aborting chat")
                yield ("error", "文件上传失败")
                yield ("done", "")
                return

        # 3. 拦截 fetch 捕获 SSE 流并桥接到 Python
        self._mimo_sse_queue = q

        if not getattr(self, "_mimo_fetch_intercepted", False):
            await self._mimo_page.evaluate(r"""() => {
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                    const response = await origFetch.apply(this, args);
                    if (url.includes('/bot/chat') && response.headers.get('content-type')?.includes('text/event-stream')) {
                        const reader = response.body.getReader();
                        const decoder = new TextDecoder();
                        let buf = '';
                        (async () => {
                            while (true) {
                                const { value, done } = await reader.read();
                                if (done) break;
                                buf += decoder.decode(value, { stream: true });
                                const lines = buf.split('\n');
                                buf = lines.pop() || '';
                                for (const line of lines) {
                                    if (!line.startsWith('data:')) continue;
                                    const raw = line.slice(5).trim();
                                    if (!raw) continue;
                                    try {
                                        const data = JSON.parse(raw);
                                        if (data.type === 'text' && data.content) {
                                            window.mimoOnChunk(JSON.stringify({ type: 'text', content: data.content }));
                                        } else if (data.content === '[DONE]') {
                                            window.mimoOnChunk(JSON.stringify({ type: 'finish' }));
                                        }
                                    } catch (e) {}
                                }
                            }
                            window.mimoOnChunk(JSON.stringify({ type: 'finish' }));
                        })();
                    }
                    return response;
                };
            }""")
            self._mimo_fetch_intercepted = True

        # 4. 关闭弹窗，切换到新对话
        try:
            await self._mimo_page.locator('button[aria-label="关闭公告"]').first.click(timeout=3000)
            await asyncio.sleep(0.5)
        except:
            pass

        await self._dismiss_mimo_popups()

        # 5. 填写 prompt 并发送
        await self._mimo_page.locator('textarea').fill(prompt)
        await asyncio.sleep(1)

        send_btn = self._mimo_page.locator('button[data-track-id="home_send_btn"]')
        if await send_btn.count() > 0:
            await send_btn.first.click()
        else:
            await self._mimo_page.locator('textarea').press('Enter')

        await asyncio.sleep(2)

        # 6. 获取 session_id
        try:
            url = self._mimo_page.url
            if '/chat/' in url:
                session_id = url.split('/chat/')[-1].split('/')[0].split('?')[0]
                logger.info(f"[MiMo] session_id: {session_id}")
        except:
            pass

        if session_id:
            yield ("session_id", session_id)

        # 6. 从队列读取结果
        while True:
            try:
                kind, value = await asyncio.wait_for(q.get(), timeout=180)
                yield (kind, value)
                if kind == "done":
                    break
            except asyncio.TimeoutError:
                logger.warning("[MiMo] timeout waiting for response")
                yield ("error", "时人不识凌云木，直待凌云始道高。")
                yield ("done", "")
                break
            except asyncio.CancelledError:
                break

        self._mimo_queues.pop(stream_id, None)

    async def delete_mimo_conversation(self, session_id: str):
        """删除单个 MiMo 会话。"""
        if not session_id:
            return
        try:
            ph_token = await self._mimo_page.evaluate("""() => {
                const raw = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('xiaomichatbot_ph='));
                if (!raw) return null;
                let val = raw.split('=').slice(1).join('=');
                if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
                return val || null;
            }""")
            if not ph_token:
                logger.warning("[MiMo] no xiaomichatbot_ph cookie found")
                return

            result = await self._mimo_page.evaluate("""async (args) => {
                const resp = await fetch('/open-apis/chat/conversation/delete?xiaomichatbot_ph=' + encodeURIComponent(args.ph), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify([args.convId])
                });
                const text = await resp.text();
                return { status: resp.status, body: text.substring(0, 200) };
            }""", {'ph': ph_token, 'convId': session_id})
            if result.get('status') == 200:
                logger.info(f"[MiMo] deleted session {session_id}")
            else:
                logger.warning(f"[MiMo] delete failed: {json.dumps(result, ensure_ascii=False)[:200]}")
        except Exception as e:
            logger.warning(f"[MiMo] delete exception: {e}")

    async def delete_all_mimo_conversations(self):
        """删除所有 MiMo 会话。"""
        try:
            if not self._mimo_page or self._mimo_page.is_closed():
                return
            ph_token = await self._mimo_page.evaluate("""() => {
                const raw = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('xiaomichatbot_ph='));
                if (!raw) return null;
                let val = raw.split('=').slice(1).join('=');
                if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
                return val || null;
            }""")
            if not ph_token:
                logger.warning("[MiMo] no xiaomichatbot_ph cookie found")
                return

            result = await self._mimo_page.evaluate("""async (args) => {
                try {
                    const listResp = await fetch('/open-apis/chat/conversation/list?xiaomichatbot_ph=' + encodeURIComponent(args.ph), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        credentials: 'include',
                        body: JSON.stringify({ pageInfo: { pageNum: 1, pageSize: 50 } })
                    });
                    const listData = await listResp.json();
                    if (!listData.data || !listData.data.dataList || listData.data.dataList.length === 0) return { deleted: 0 };

                    const convIds = listData.data.dataList.map(c => c.conversationId);
                    let deleted = 0;
                    for (const id of convIds) {
                        const delResp = await fetch('/open-apis/chat/conversation/delete?xiaomichatbot_ph=' + encodeURIComponent(args.ph), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'include',
                            body: JSON.stringify([id])
                        });
                        if (delResp.status === 200) deleted++;
                    }
                    return { deleted: deleted, total: convIds.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", {'ph': ph_token})
            logger.info(f"[MiMo] delete_all result: {json.dumps(result, ensure_ascii=False)}")
        except Exception as e:
            logger.warning(f"[MiMo] delete_all exception: {e}")

    async def get_mimo_session_id(self) -> str:
        """从 URL 获取当前 MiMo 会话 ID。"""
        try:
            if not self._mimo_page or self._mimo_page.is_closed():
                return ""
            url = self._mimo_page.url
            if '/chat/' in url:
                return url.split('/chat/')[-1].split('/')[0].split('?')[0]
        except:
            pass
        return ""

