from ._shared import *


class DoubaoMixin:
    def _on_doubao_push(self, stream_id: str, kind: str, value):
        q = self._doubao_queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    async def activate_doubao_conversation(self, conversation_id: str) -> bool:
        """导航到豆包指定对话页面，激活该对话实例。"""
        if not conversation_id or conversation_id == "0":
            return False
        try:
            await self.ensure_doubao_ready()
            url = f"https://www.doubao.com/chat/{conversation_id}"
            await self._doubao_page.goto(url, wait_until="load", timeout=60000)
            logger.info(f"[Doubao] after nav to {url}, actual URL: {self._doubao_page.url}")
            for _ in range(360):  # 最长等待3分钟
                found = await self._doubao_page.evaluate("""() => {
                    const ta = document.querySelector('textarea');
                    const ce = document.querySelector('[contenteditable="true"]');
                    return !!(ta || ce);
                }""")
                if found:
                    break
                await asyncio.sleep(0.5)
            logger.info(f"[Doubao] activated conversation {conversation_id}")
            return True
        except Exception as e:
            logger.warning(f"[Doubao] activate conversation failed: {e}")
            return False

    async def ensure_doubao_ready(self, headless=True):
        """确保 Doubao 浏览器就绪，使用持久化 user_data_dir 保留登录状态。"""
        # 检测浏览器是否已关闭（用户手动关闭后自动重建）
        page_closed = self._doubao_page is None or (hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed())
        context_closed = self._doubao_browser is None or (hasattr(self._doubao_browser, 'is_connected') and not self._doubao_browser.is_connected())
        if not page_closed and not context_closed and self._doubao_browser and self._doubao_browser.pages:
            return True
        # 任一页面或上下文关闭都重建
        if page_closed or context_closed:
            logger.info("[Doubao] browser or page closed by user, rebuilding...")
            self._doubao_page = None
            self._doubao_browser = None
            self._doubao_pw = None
        async with self._doubao_lock:
            # 二次检查
            page_closed = self._doubao_page is None or (hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed())
            context_closed = self._doubao_browser is None or (hasattr(self._doubao_browser, 'is_connected') and not self._doubao_browser.is_connected())
            if not page_closed and not context_closed and self._doubao_browser and self._doubao_browser.pages:
                await self._refresh_profile_params()
                return True

            if not os.path.exists(self._doubao_user_data_dir):
                os.makedirs(self._doubao_user_data_dir, exist_ok=True)

            _last_exc = None
            for _rebuild_attempt in range(3):
                try:
                    from playwright.async_api import async_playwright
                    self._doubao_pw = await async_playwright().start()
                    self._doubao_browser = await self._doubao_pw.chromium.launch_persistent_context(
                        user_data_dir=self._doubao_user_data_dir,
                        headless=headless,
                        channel=_browser_channel(),
                        args=_linux_safe_args() + ["--disable-blink-features=AutomationControlled"],
                        ignore_default_args=["--enable-automation"],
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                    )
                    self._doubao_page = self._doubao_browser.pages[0] if self._doubao_browser.pages else await self._doubao_browser.new_page()

                    await self._doubao_page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        window.chrome = { runtime: {} };
                        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    """)

                    await self._doubao_page.expose_function("__sse_push", self._on_doubao_push)

                    logger.info("Doubao: navigating to doubao.com/chat/ ...")
                    try:
                        await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        logger.warning("[Doubao] initial goto timed out, trying reload...")
                        try:
                            await self._doubao_page.reload(wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                    await asyncio.sleep(2)

                    try:
                        err_buttons = await self._doubao_page.evaluate("""() => {
                            const btns = document.querySelectorAll('button');
                            return Array.from(btns).map(b => (b.textContent || '').trim()).filter(t => t);
                        }""")
                        if len(err_buttons) >= 1 and err_buttons[0] == '刷新页面':
                            logger.info("[Doubao] error page at init, clicking '刷新页面'...")
                            await self._doubao_page.evaluate("document.querySelector('button').click()")
                            await asyncio.sleep(5)
                    except Exception as e:
                        logger.debug(f"[Doubao] error page check failed: {e}")

                    try:
                        await self._doubao_page.wait_for_function(
                            "() => typeof window.bdms?.frontierSign === 'function'",
                            timeout=30000
                        )
                        logger.info("Doubao: bdms.frontierSign SDK ready")
                    except Exception as e:
                        logger.warning(f"Doubao: bdms.frontierSign not available: {e}")

                    has_chat_ui = await self._doubao_page.evaluate("""() => {
                        const hasInput = !!document.querySelector('textarea') || !!document.querySelector('[contenteditable=true]');
                        const hasSend = !!document.getElementById('flow-end-msg-send') ||
                                        !!document.querySelector('button[data-testid*="send"]') ||
                                        !!document.querySelector('button[class*="send"]');
                        const bodyText = document.body.innerText || '';
                        const hasLoginPrompt = bodyText.includes('请先登录') || bodyText.includes('扫码登录');
                        const modal = document.querySelector('.semi-modal-wrap, .semi-modal-wrap-center, .semi-portal');
                        const hasBlockingModal = !!modal;
                        const modalText = modal ? (modal.textContent || '').trim().substring(0, 100) : '';
                        return { hasInput, hasSend, hasLoginPrompt, hasBlockingModal, modalText };
                    }""") or {}
                    if has_chat_ui.get('hasBlockingModal') or (has_chat_ui.get('hasLoginPrompt') and not has_chat_ui.get('hasSend')):
                        logger.warning(f"Doubao: login/modal issue detected. hasBlockingModal={has_chat_ui.get('hasBlockingModal')}, modalText='{has_chat_ui.get('modalText')}'")
                        await self._doubao_login_recovery()

                    logger.info("Doubao browser ready")
                    await self._refresh_profile_params()
                    return True
                except Exception as _rebuild_exc:
                    _last_exc = _rebuild_exc
                    logger.warning(f"[Doubao] rebuild attempt {_rebuild_attempt+1}/3 failed: {_rebuild_exc}")
                    for _attr in ('_doubao_page', '_doubao_browser', '_doubao_pw'):
                        _obj = getattr(self, _attr, None)
                        if _obj:
                            try:
                                if _attr == '_doubao_pw':
                                    await _obj.stop()
                                else:
                                    await _obj.close()
                            except Exception:
                                pass
                            setattr(self, _attr, None)
                    await asyncio.sleep(2)
            if _last_exc:
                raise _last_exc

    async def _refresh_profile_params(self):
        """Refresh profile parameters from the current page's localStorage."""
        if not self._doubao_page or self._doubao_page.is_closed():
            return
        try:
            params = await self._doubao_page.evaluate("""() => {
                const get = (k) => {
                    try { return localStorage.getItem(k) || ''; } catch (e) { return ''; }
                };

                // 1. 尝试顶层键
                let device_id = get('device_id') || (window.deviceId || '');
                let web_id = get('web_id') || (window.webId || '');
                let tea_uuid = get('tea_uuid') || (window.teaUuid || '');
                let fp = get('fp') || (window.fp || '');

                // 2. 从 samantha_web_web_id JSON 提取 web_id
                if (!web_id) {
                    try {
                        const sw = JSON.parse(get('samantha_web_web_id') || '{}');
                        if (sw.web_id) web_id = sw.web_id;
                    } catch(e) {}
                }

                // 3. 从 __tea_cache_tokens_* JSON 提取 web_id / user_unique_id
                if (!web_id) {
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        if (k && k.startsWith('__tea_cache_tokens_')) {
                            try {
                                const v = JSON.parse(get(k) || '{}');
                                if (v.web_id) { web_id = v.web_id; break; }
                            } catch(e) {}
                        }
                    }
                }

                // 4. 从 SLARDARmfa_web (base64 JSON) 提取 device_id
                if (!device_id) {
                    try {
                        const raw = get('SLARDARmfa_web');
                        if (raw) {
                            const decoded = JSON.parse(decodeURIComponent(escape(atob(raw))));
                            if (decoded.deviceId) device_id = decoded.deviceId;
                        }
                    } catch(e) {}
                }

                // 5. 从 tt_scid 或 ttcid 作为 device_id fallback
                if (!device_id) {
                    device_id = get('tt_scid') || get('ttcid') || '';
                }

                // 6. tea_uuid fallback: 用 __tea_cache_tokens_* 中的 user_unique_id
                if (!tea_uuid) {
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        if (k && k.startsWith('__tea_cache_tokens_')) {
                            try {
                                const v = JSON.parse(get(k) || '{}');
                                if (v.user_unique_id) { tea_uuid = v.user_unique_id; break; }
                            } catch(e) {}
                        }
                    }
                }

                return { device_id, web_id, tea_uuid, fp };
            }""")
            if params.get('device_id') and params.get('web_id') and params.get('tea_uuid'):
                self._profile_params.update(params)
                logger.info(f"[Doubao] profile params refreshed: device_id={params['device_id'][:16]}..., web_id={params['web_id']}, tea_uuid={params['tea_uuid'][:16]}...")
            else:
                logger.warning(f"[Doubao] Incomplete profile params: {params}")
        except Exception as e:
            logger.warning(f"[Doubao] _refresh_profile_params error: {e}")

    async def _ensure_and_refresh(self):
        await self.ensure_doubao_ready(headless=True)
        await self._refresh_profile_params()

    def get_profile_params(self) -> dict:
        """Synchronously get profile parameters, ensuring browser is ready."""
        if self._profile_params:
            return dict(self._profile_params)
        try:
            loop = asyncio.get_running_loop()
            if loop and not loop.is_closed():
                # Schedule the coroutine on the running loop from this thread
                future = asyncio.run_coroutine_threadsafe(self._ensure_and_refresh(), loop)
                future.result(timeout=120)
            else:
                asyncio.run(self._ensure_and_refresh())
        except Exception as e:
            logger.warning(f"[Doubao] get_profile_params error: {e}")
        return dict(self._profile_params)

    async def _doubao_login_recovery(self):
        """打开可见浏览器让用户手动登录 Doubao，使用 user_data_dir 持久化状态。"""
        from playwright.async_api import async_playwright
        try:
            # 关闭当前 headless 实例
            if self._doubao_page:
                try:
                    await self._doubao_page.close()
                except Exception:
                    pass
                self._doubao_page = None
            if self._doubao_browser:
                try:
                    await self._doubao_browser.close()
                except Exception:
                    pass
                self._doubao_browser = None
            if self._doubao_pw:
                try:
                    await self._doubao_pw.stop()
                except Exception:
                    pass
                self._doubao_pw = None

            pw = await async_playwright().start()
            login_browser = await pw.chromium.launch_persistent_context(
                user_data_dir=self._doubao_user_data_dir,
                headless=False,
                channel=_browser_channel(),
                args=_linux_safe_args() + ["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            login_page = login_browser.pages[0] if login_browser.pages else await login_browser.new_page()
            await login_page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            await login_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
            logger.info("Doubao: visible browser opened for manual login. Please log in...")

            while True:
                await asyncio.sleep(1)
                if not login_browser.pages:
                    break
                try:
                    body = await login_page.text_content("body") or ""
                    if "登录" not in body and "请先登录" not in body:
                        logger.info("Doubao: login detected")
                        break
                except Exception:
                    pass

            await login_browser.close()
            await pw.stop()

            # 重新创建 headless 上下文（复用已保存的 user_data_dir）
            self._doubao_pw = await async_playwright().start()
            self._doubao_browser = await self._doubao_pw.chromium.launch_persistent_context(
                user_data_dir=self._doubao_user_data_dir,
                headless=True,
                channel=_browser_channel(),
                args=_linux_safe_args() + ["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._doubao_page = self._doubao_browser.pages[0] if self._doubao_browser.pages else await self._doubao_browser.new_page()
            await self._doubao_page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            await self._doubao_page.expose_function("__sse_push", self._on_doubao_push)
            await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Doubao login recovery failed: {e}")
            raise

    async def _download_images_via_contextmenu(self) -> list:
        """使用 page.request.get() 在浏览器上下文中下载图片，自动携带 Cookie 和正确 Headers。"""
        import uuid as _uuid
        local_urls = []
        images_dir = os.path.join(BASE_DIR, "images")
        os.makedirs(images_dir, exist_ok=True)
        try:
            # 获取页面上的图片（rc_gen_image）
            img_elements = await self._doubao_page.query_selector_all('img')
            if not img_elements:
                logger.warning("[Doubao] no img elements found for download")
                return []
            targets = []
            for img_el in img_elements:
                try:
                    src = await img_el.get_attribute('src') or ''
                    visible = await img_el.is_visible()
                    if not visible or not src or not src.startswith('http'):
                        continue
                    if 'rc_gen_image' in src and 'byteimg.com' in src:
                        targets.append(src)
                except Exception:
                    continue
            if not targets:
                logger.warning("[Doubao] no visible generated images found")
                return []
            # 去重
            seen_hashes = set()
            base_urls = []
            import re as _re
            for src in targets:
                m = _re.search(r'rc_gen_image/([a-f0-9]+)\.jpeg', src)
                if not m:
                    continue
                base_hash = m.group(1)
                if base_hash in seen_hashes:
                    continue
                seen_hashes.add(base_hash)
                base_urls.append((base_hash, src))
            # 并行下载（使用浏览器上下文）
            async def download_one(url: str, base_hash: str) -> str | None:
                try:
                    resp = await self._doubao_page.request.get(url)
                    if resp.status != 200:
                        logger.warning(f"[Doubao] page.request.get {base_hash}: HTTP {resp.status}")
                        return None
                    content = await resp.body()
                    if not content or len(content) < 1024:  # 至少 1KB
                        logger.warning(f"[Doubao] image {base_hash} too small: {len(content) if content else 0} bytes")
                        return None
                    # 确定扩展名
                    ct = resp.headers.get('content-type', '').lower()
                    ext = '.jpg' if 'jpeg' in ct or 'jpg' in ct else '.png' if 'png' in ct else '.webp'
                    filename = f"{base_hash[:8]}_{_uuid.uuid4().hex[:6]}{ext}"
                    filepath = os.path.join(images_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    local_url = f"{CONFIG.get('img_url', 'http://localhost:8765/images')}/{filename}"
                    logger.info(f"[Doubao] downloaded via page.request: {local_url} ({len(content)} bytes)")
                    return local_url
                except Exception as e:
                    logger.warning(f"[Doubao] page.request error for {base_hash}: {e}")
                    return None
            import asyncio as _asyncio
            tasks = [download_one(url, h) for h, url in base_urls]
            results = await _asyncio.gather(*tasks)
            for r in results:
                if r:
                    local_urls.append(r)
        except Exception as e:
            logger.warning(f"[Doubao] _download_images_via_contextmenu error: {e}")
        return local_urls

    async def download_images_from_urls(self, urls: list, n: int = 1) -> list:
        """从 URL 列表下载图片到本地。
        对于 rc_gen_image URL，优先通过浏览器上下文获取真实的“下载原图”签名链接；
        对于 ocean-cloud-tos/image_generation URL，使用 httpx 直接下载。
        """
        import uuid as _uuid
        import httpx as _httpx
        import hashlib

        local_urls = []
        images_dir = os.path.join(BASE_DIR, "images")
        os.makedirs(images_dir, exist_ok=True)

        logger.info(f"[Doubao] download_images_from_urls called with {len(urls)} URLs")

        seen_hashes = set()
        unique_urls = []
        for url in urls:
            m_rc = re.search(r'rc_gen_image/([a-f0-9]+)\.jpeg', url)
            if m_rc:
                uid = f"rc:{m_rc.group(1)}"
            else:
                m_oc = re.search(r'image_generation/([a-f0-9]+)_(\d+)', url)
                if m_oc:
                    uid = f"oc:{m_oc.group(1)}"
                else:
                    uid = f"hash:{hashlib.md5(url.encode()).hexdigest()[:8]}"

            if uid not in seen_hashes:
                seen_hashes.add(uid)
                unique_urls.append((uid, url))

        logger.info(f"[Doubao] download: {len(unique_urls)} unique images, requesting {n}")
        selected = unique_urls[:n]

        page = self._doubao_page
        if page:
            for uid, preview_url in selected:
                if not uid.startswith("rc:"):
                    continue
                try:
                    fullsize_url = await self._get_fullsize_download_url_via_browser(page, uid, preview_url)
                    target_url = fullsize_url or preview_url
                    if fullsize_url:
                        logger.info(f"[Doubao] got full-size signed URL for {uid}: {target_url[:120]}...")
                    else:
                        logger.warning(f"[Doubao] no full-size signed URL for {uid}, using preview URL")
                    resp = await page.request.get(target_url)
                    content = await resp.body()
                    logger.info(f"[Doubao] browser download {uid}: HTTP {resp.status}, {len(content)} bytes, ct={resp.headers.get('content-type','')}")
                    if resp.status == 200 and len(content) > 1024:
                        ct = resp.headers.get('content-type', '').lower()
                        ext = '.jpg' if 'jpeg' in ct else '.png' if 'png' in ct else '.webp'
                        m_rc2 = re.search(r'rc_gen_image/([a-f0-9]+)', target_url)
                        base_hash = m_rc2.group(1) if m_rc2 else uid.split(":")[1]
                        filename = f"{base_hash[:8]}_{_uuid.uuid4().hex[:6]}{ext}"
                        filepath = os.path.join(images_dir, filename)
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        local_url = f"{CONFIG.get('img_url', 'http://localhost:8765/images')}/{filename}"
                        local_urls.append(local_url)
                        logger.info(f"[Doubao] saved: {local_url} ({len(content)} bytes)")
                    elif resp.status == 200:
                        logger.warning(f"[Doubao] image too small for {uid}: {len(content)} bytes")
                    else:
                        logger.warning(f"[Doubao] browser download failed for {uid}: HTTP {resp.status}")
                except Exception as e:
                    logger.warning(f"[Doubao] browser download error for {uid}: {e}")

        async with _httpx.AsyncClient(timeout=60, follow_redirects=True, headers={
            'accept': '*/*',
            'origin': 'https://www.doubao.com',
            'referer': 'https://www.doubao.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0',
            'sec-fetch-site': 'cross-site',
            'sec-fetch-mode': 'cors',
            'sec-fetch-dest': 'empty',
        }) as client:
            for uid, target_url in selected:
                if uid.startswith("rc:"):
                    continue
                try:
                    logger.info(f"[Doubao] downloading {uid}: {target_url[:120]}...")
                    resp = await client.get(target_url)
                    logger.info(f"[Doubao] download {uid}: HTTP {resp.status_code}, {len(resp.content)} bytes, ct={resp.headers.get('content-type','')}")
                    if resp.status_code == 200 and len(resp.content) > 1024:
                        ct = resp.headers.get('content-type', '').lower()
                        ext = '.jpg' if 'jpeg' in ct else '.png' if 'png' in ct else '.webp'
                        if uid.startswith("oc:"):
                            m_oc = re.search(r'image_generation/([a-f0-9]+)', target_url)
                            base_hash = m_oc.group(1) if m_oc else uid.split(":")[1]
                        else:
                            base_hash = uid.split(":", 1)[1]
                        filename = f"{base_hash[:8]}_{_uuid.uuid4().hex[:6]}{ext}"
                        filepath = os.path.join(images_dir, filename)
                        with open(filepath, 'wb') as f:
                            f.write(resp.content)
                        local_url = f"{CONFIG.get('img_url', 'http://localhost:8765/images')}/{filename}"
                        local_urls.append(local_url)
                        logger.info(f"[Doubao] saved: {local_url} ({len(resp.content)} bytes)")
                    elif resp.status_code == 200:
                        logger.warning(f"[Doubao] image too small for {uid}: {len(resp.content)} bytes")
                    else:
                        logger.warning(f"[Doubao] download failed for {uid}: HTTP {resp.status_code}")
                except Exception as e:
                    logger.warning(f"[Doubao] download error for {uid}: {e}")

        logger.info(f"[Doubao] download completed: {len(local_urls)}/{n} success")
        return local_urls

    async def _get_fullsize_download_url_via_browser(self, page, uid: str, preview_url: str) -> str | None:
        """右键图片打开上下文菜单，点击"下载原图"菜单项，拦截真实签名请求。"""
        import asyncio as _asyncio

        if not page or page.is_closed():
            logger.info(f"[Doubao] page.is_closed() for {uid}")
            return None

        base_hash = uid.split(":", 1)[1]
        captured_urls = []

        async def _capture(route):
            req_url = route.request.url
            if 'rc_gen_image' in req_url and 'dld' in req_url:
                captured_urls.append(req_url)
                logger.info(f"[Doubao] captured full-size request for {uid}: {req_url[:150]}")
                await route.abort()
                return
            await route.continue_()

        try:
            await page.route("**/*", _capture)
        except Exception as e:
            logger.warning(f"[Doubao] route setup failed for {uid}: {e}")
            return None

        try:
            # Step 1: Find the image and right-click it to open context menu
            img_info = await page.evaluate("""(baseHash) => {
                const imgs = Array.from(document.querySelectorAll('img[src*="rc_gen_image"]'));
                const img = imgs.find(i => (i.src || '').includes(baseHash));
                if (!img) return { found: false };
                const rect = img.getBoundingClientRect();
                return { found: true, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
            }""", base_hash)
            logger.info(f"[Doubao] img location for {uid}: {img_info}")

            if not img_info or not img_info.get('found'):
                return None

            # Right-click on the image to open context menu
            await page.mouse.click(img_info['x'], img_info['y'], button='right')
            await _asyncio.sleep(0.5)

            # Step 2: Click "下载原图" context menu item
            clicked = await page.evaluate("""() => {
                const menuItems = document.querySelectorAll('[class*="context-menu-item"]');
                for (const item of menuItems) {
                    const txt = (item.textContent || '').trim();
                    if (txt.includes('下载原图')) {
                        item.click();
                        return { clicked: true, text: txt, className: (item.className || '').substring(0, 80) };
                    }
                }
                return { clicked: false, reason: 'context menu item not found' };
            }""")
            logger.info(f"[Doubao] 下载原图 click result for {uid}: {clicked}")

            if not clicked or not clicked.get('clicked'):
                # Fallback: press Escape to close context menu, try a different approach
                await page.keyboard.press('Escape')
                await _asyncio.sleep(0.3)
                logger.warning(f"Fallback: press Escape to close context menu, try a different approach")
                return None

            # Step 3: Wait for the intercepted full-size URL
            for _ in range(20):
                if captured_urls:
                    return captured_urls[0]
                await _asyncio.sleep(1)
            logger.warning(f"Wait for the intercepted full-size URL None")
            return None
        except Exception as e:
            logger.warning(f"[Doubao] capture full-size url failed for {uid}: {e}")
            return None
        finally:
            try:
                await page.unroute("**/*", _capture)
            except Exception:
                pass

    async def get_user_info(self) -> dict:
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        async with self._doubao_lock:
            user_info = {}
            got_data = asyncio.Event()

            async def on_response(response):
                url = response.url
                try:
                    if '/alice/profile/self' in url:
                        body = await response.json()
                        profile = body.get("data", {}).get("profile_brief", {})
                        if profile and profile.get("nickname"):
                            user_info["name"] = profile.get("nickname", "")
                            user_info["username"] = profile.get("user_name", "")
                            user_info["nick_name"] = profile.get("nickname", "")
                            user_info["user_id"] = str(profile.get("id", ""))
                            img_data = profile.get("image", {})
                            if isinstance(img_data, dict):
                                user_info["avatar_url"] = img_data.get("tiny_url", "")
                            got_data.set()
                    elif '/im/conversation/info' in url:
                        body = await response.json()
                        dl = body.get("downlink_body", {})
                        conv_body = dl.get("get_conv_info_downlink_body", {})
                        participants = conv_body.get("first_page_participant_list", [])
                        for p in participants:
                            if p.get("user_type") == 1:
                                user_info["name"] = user_info.get("name") or p.get("nick_name", "")
                                user_info["nick_name"] = user_info.get("nick_name") or p.get("nick_name", "")
                                avatar = p.get("avatar_url", {})
                                if isinstance(avatar, dict):
                                    user_info["avatar_url"] = user_info.get("avatar_url") or avatar.get("key", "")
                                got_data.set()
                except Exception:
                    pass

            self._doubao_page.on("response", on_response)
            try:
                await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                try:
                    await asyncio.wait_for(got_data.wait(), timeout=12)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(1)
            finally:
                self._doubao_page.remove_listener("response", on_response)
            return user_info

    async def stream_completion(self, body: dict):
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        self._doubao_queues[stream_id] = queue

        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex

        # 通过浏览器页面调用 Doubao SDK 签名生成带 a_bogus 的完整 URL
        js = """
        async (args) => {
            const { body, traceId, spanId, deviceId, webId, teaUuid, fp, streamId } = args;
            const baseUrl = "https://www.doubao.com/chat/completion";

            try {
                window.__sse_push(streamId, "chunk", "[DEBUG] step1: args received\\n");
            } catch(e) {}

            // 等待 bdms.frontierSign 可用
            let retries = 0;
            while (typeof window.bdms?.frontierSign !== 'function' && retries < 50) {
                await new Promise(r => setTimeout(r, 100));
                retries++;
            }
            if (typeof window.bdms?.frontierSign !== 'function') {
                window.__sse_push(streamId, "error", "bdms.frontierSign not available after 5s");
                window.__sse_push(streamId, "done", "");
                return;
            }

            try {
                window.__sse_push(streamId, "chunk", "[DEBUG] step2: frontierSign available\\n");
            } catch(e) {}
            
            const params = {
                aid: '497858',
                device_id: deviceId,
                device_platform: 'web',
                fp: fp || '',
                language: 'zh',
                pc_version: '3.22.0',
                pkg_type: 'release_version',
                real_aid: '497858',
                region: 'CN',
                samantha_web: '1',
                sys_region: 'CN',
                tea_uuid: teaUuid,
                'use-olympus-account': '1',
                version_code: '20800',
                web_id: webId,
                web_platform: 'browser',
                web_tab_id: crypto.randomUUID(),
            };
            
            // 获取 msToken
            const msToken = (document.cookie.match(/msToken=([^;]+)/) || [null, ''])[1];
            if (msToken) params.msToken = msToken;
            
            // 排序并编码参数
            const sortedKeys = Object.keys(params).sort();
            const queryParts = sortedKeys.map(k => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`);
            const queryString = queryParts.join('&');
            
            // 调用 frontierSign 生成 a_bogus
            let signedUrl;
            try {
                window.__sse_push(streamId, "chunk", "[DEBUG] step3: calling frontierSign\\n");
            } catch(e) {}
            try {
                const signResult = await window.bdms.frontierSign(queryString);
                window.__sse_push(streamId, "chunk", "[DEBUG] step4: frontierSign returned: " + JSON.stringify(signResult).slice(0,200) + "\\n");
                if (signResult && (signResult.a_bogus || signResult['X-Bogus'])) {
                    const bogusKey = signResult.a_bogus ? 'a_bogus' : 'X-Bogus';
                    const bogusVal = signResult.a_bogus || signResult['X-Bogus'];
                    signedUrl = `${baseUrl}?${queryString}&${bogusKey}=${encodeURIComponent(bogusVal)}`;
                } else {
                    signedUrl = `${baseUrl}?${queryString}`;
                }
            } catch (e) {
                window.__sse_push(streamId, "chunk", "[DEBUG] step4 FAIL: " + String(e) + "\\n");
                signedUrl = `${baseUrl}?${queryString}`;
            }
            
            window.__sse_push(streamId, "chunk", "[DEBUG] step5: url=" + signedUrl.slice(0, 200) + "...\\n");
            
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 30000);
                const resp = await fetch(signedUrl, {
                    method: "POST",
                    headers: {
                        "content-type": "application/json",
                        "agw-js-conv": "str",
                        "accept": "text/event-stream",
                        "x-flow-trace": JSON.stringify({trace_id: traceId, span_id: spanId}),
                    },
                    body: JSON.stringify(body),
                    signal: controller.signal,
                });
                clearTimeout(timeoutId);
                window.__sse_push(streamId, "chunk", "[DEBUG] step6: resp.status=" + resp.status + "\\n");
                if (!resp.ok) {
                    const t = await resp.text();
                    window.__sse_push(streamId, "error", "HTTP " + resp.status + ": " + t.slice(0, 500));
                    window.__sse_push(streamId, "done", "");
                    return;
                }
                const reader = resp.body.getReader();
                const decoder = new TextDecoder("utf-8");
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const text = decoder.decode(value, { stream: true });
                    window.__sse_push(streamId, "chunk", text);
                }
                window.__sse_push(streamId, "done", "");
            } catch (e) {
                window.__sse_push(streamId, "error", String(e));
                window.__sse_push(streamId, "done", "");
            }
        }
        """

        async with self._doubao_lock:
            profile = self.get_profile_params()
            eval_task = asyncio.create_task(
                self._doubao_page.evaluate(js, {
                    "body": body,
                    "traceId": trace_id,
                    "spanId": span_id,
                    "streamId": stream_id,
                    "deviceId": profile.get('device_id', ''),
                    "webId": profile.get('web_id', ''),
                    "teaUuid": profile.get('tea_uuid', ''),
                    "fp": profile.get('fp', ''),
                })
            )
            try:
                while True:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=180)
                    if kind == "done":
                        break
                    if kind == "error":
                        logger.error(f"Browser fetch error: {value}")
                        yield ("error", value)
                        continue
                    yield ("chunk", value)
            finally:
                self._doubao_queues.pop(stream_id, None)
                try:
                    await eval_task
                except Exception:
                    pass

    async def stream_doubao_chat_via_type(self, text: str, attachments: list | None = None, inline_file_content: str | None = None, image_generation: bool = False, timeout: int = 60, reuse_conversation: bool = False):
        """Route interception for doubao API response + DOM typing.
        attachments: 文档附件列表 (type=3)，注入 attachment_block + input_skill + chat_ability。
        inline_file_content: 如果提供，直接作为 text_block 内容注入（不上传云存储）。
        image_generation: 如果为 True，注入图像生成所需的 chat_ability 字段，并自动添加 "生成图片：" 前缀。
        """
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._doubao_queues[stream_id] = q
        _attachments = attachments or []

        # 闭包共享状态：跨多个 /chat/completion 请求传递图片生成状态
        _shared_image_found = False
        _shared_image_urls = set()
        _request_num = 0

        # 通用图片 URL 提取器：递归搜索 JSON 对象中所有类图片 URL
        def _extract_image_urls_from_json(obj):
            """从任意 JSON 对象中递归提取所有类图片 URL（http/https + 常见图片扩展名）"""
            urls = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    urls.update(_extract_image_urls_from_json(k))
                    urls.update(_extract_image_urls_from_json(v))
            elif isinstance(obj, list):
                for item in obj:
                    urls.update(_extract_image_urls_from_json(item))
            elif isinstance(obj, str):
                # 匹配 http/https URL 且以常见图片扩展名结尾，或包含 /image/ 路径
                matches = re.findall(
                    r'https?://[^\s"\'<>]+(?:\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&))',
                    obj, re.IGNORECASE
                )
                for m in matches:
                    urls.add(m)
                # 额外匹配：已知图片域名或路径特征（如 p16-tiktok、p9-doubao 等）
                matches2 = re.findall(
                    r'https?://[^\s"\'<>]*(?:p\d+-|image|img|pic|photo|media)[^\s"\'<>]*\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&)',
                    obj, re.IGNORECASE
                )
                for m in matches2:
                    urls.add(m)
            return urls

        # 清除所有旧的 route handler（防止旧 handler 拦截请求）
        try:
            await self._doubao_page.unroute("**/chat/completion**")
        except Exception:
            pass

        # ⚠️ route handler 定义，但不在此时注册
        # 页面导航（goto/reload）会自动清除 route，所以必须在导航完成后才注册
        async def handle_route(route):
            logger.info(f"[Doubao] handle_route: {route.request.method} {route.request.url[:150]}")
            if 'doubao.com/chat/completion' not in route.request.url:
                logger.info("[Doubao] URL not target, continuing normally")
                await route.continue_()
                return
            logger.info("[Doubao] Target URL intercepted, processing request")
            nonlocal _request_num, _shared_image_found, _shared_image_urls
            _image_found = False
            _request_num += 1
            logger.info(f"[Doubao] handle_route call #{_request_num}")
            try:
                modify_body = False
                body_dict = {}
                orig_body = route.request.post_data
                logger.info(f"[Doubao] Request method: {route.request.method}, has_body: {orig_body is not None}, content_len: {len(orig_body) if orig_body else 0}")
                if orig_body:
                    try:
                        body_dict = json.loads(orig_body)
                        # 如果是恢复消息，跳过处理，继续发送原始请求
                        recovery_info = body_dict.get('option', {}).get('recovery_option', {})
                        if recovery_info.get('is_recovery', False):
                            logger.info("[Doubao] intercepted recovery request, continuing original request without modification.")
                            await route.continue_()
                            return
                        logger.info(f"[Doubao] Request body keys: {list(body_dict.keys())}")
                        # Save request body for debugging
                        try:
                            debug_dir = os.path.join(os.path.dirname(__file__), "logs")
                            os.makedirs(debug_dir, exist_ok=True)
                            with open(os.path.join(debug_dir, f"intercepted_request_{_request_num}.json"), 'w', encoding='utf-8') as f:
                                json.dump(body_dict, f, ensure_ascii=False, indent=2)
                            logger.info(f"[Doubao] saved request body to logs/intercepted_request_{_request_num}.json")
                        except Exception:
                            pass
                        
                        messages = body_dict.get("messages", [])
                        if messages:
                            msg = messages[0]
                            cbs = msg.get("content_block", [])
                            logger.info(f"[Doubao] route handler: inline_file_content present: {inline_file_content is not None}, len: {len(inline_file_content) if inline_file_content else 0}")
                            logger.info(f"[Doubao] route handler: attachments present: {_attachments is not None}, len: {len(_attachments) if _attachments else 0}")
                            logger.info(f"[Doubao] route handler: image_generation: {image_generation}")
                            
                            # Inject attachment block if provided
                            if _attachments:
                                file_block = {
                                    "block_type": 10052,
                                    "content": {
                                        "attachment_block": {"attachments": _attachments},
                                        "pc_event_block": ""
                                    },
                                    "block_id": str(uuid.uuid4()),
                                    "parent_id": "", "meta_info": [], "append_fields": []
                                }
                                cbs.insert(0, file_block)
                                body_dict["chat_ability"] = {"ability_type": 16}
                                body_dict.setdefault("ext", {})["input_skill"] = '{"skill_id":"16","skill_type":16,"template_key":""}'
                                modify_body = True
                                msg["content_block"] = cbs
                            
                            # Inject inline file content as additional text_block
                            if inline_file_content:
                                file_text_block = {
                                    "block_type": 10000,
                                    "content": {
                                        "text_block": {"text": inline_file_content, "icon_url": "", "icon_url_dark": "", "summary": ""},
                                        "pc_event_block": ""
                                    },
                                    "block_id": str(uuid.uuid4()),
                                    "parent_id": "", "meta_info": [], "append_fields": []
                                }
                                cbs.append(file_text_block)
                                modify_body = True
                                msg["content_block"] = cbs
                        
                        # Inject chat_ability for image generation if not already present
                        if image_generation and 'chat_ability' not in body_dict:
                            body_dict["chat_ability"] = {
                                "ability_type": 3,
                                "ability_param": json.dumps({
                                    "ability_param": {"model": "Seedream 4.5"},
                                    "ability_type": 1
                                }, ensure_ascii=False)
                            }
                            modify_body = True
                            logger.info("[Doubao] injected chat_ability for image generation")
                        elif image_generation and 'chat_ability' in body_dict:
                            logger.info(f"[Doubao] chat_ability already present: {json.dumps(body_dict['chat_ability'], ensure_ascii=False)[:100]}")
                    except Exception as json_e:
                        logger.warning(f"[Doubao] Failed to parse request body: {json_e}")
                        body_dict = {}
                
                try:
                    if modify_body:
                        modified_body = json.dumps(body_dict, ensure_ascii=False)
                        logger.info(f"[Doubao] route.fetch with modified body (len={len(modified_body)}), request_num={_request_num}")
                        resp = await route.fetch(timeout=180000, post_data=modified_body)
                    else:
                        logger.info(f"[Doubao] route.fetch without modification, request_num={_request_num}")
                        resp = await route.fetch(timeout=180000)
                    logger.info(f"[Doubao] route.fetch completed, status={resp.status}")
                except Exception as fetch_e:
                    logger.warning(f"[Doubao] route.fetch failed or timed out: {fetch_e}")
                    q.put_nowait(("error", f"Fetch error: {fetch_e}"))
                    q.put_nowait(("done", ""))
                    await route.abort() # Abort the route to prevent further processing
                    return

                body = await resp.body()
                raw_text = body.decode("utf-8", errors="replace")
                logger.info(f"[Doubao] API: {len(raw_text)} bytes")
                try:
                    debug_dir = os.path.join(os.path.dirname(__file__), "logs")
                    os.makedirs(debug_dir, exist_ok=True)
                    with open(os.path.join(debug_dir, "doubao_api_response_debug.txt"), 'w', encoding='utf-8') as f:
                        f.write(raw_text)
                except Exception:
                    pass

                last_block_text = {}
                count = 0

                for block in raw_text.split("\n\n"):
                    block = block.strip()
                    if not block:
                        continue

                    event_type = ""
                    data_str = ""
                    for line in block.split("\n"):
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()

                    if not data_str:
                        continue

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if event_type == "SSE_ACK":
                        cid = data.get("ack_client_meta", {}).get("conversation_id", "")
                        if cid:
                            q.put_nowait(("conversation_id", cid))
                        continue

                    if event_type == "STREAM_ERROR":
                        msg = json.dumps(data, ensure_ascii=False)
                        if image_generation:
                            error_code = data.get("error_code")
                            error_msg_text = data.get("error_msg", "")
                            is_rate_limit = (error_code == 710022004 or
                                             "rate" in error_msg_text.lower() and "limit" in error_msg_text.lower())
                            if is_rate_limit:
                                logger.warning(f"[Doubao] STREAM_ERROR rate-limit in image gen mode, emitting error for retry: {msg[:200]}")
                                q.put_nowait(("error", msg))
                                q.put_nowait(("done", ""))
                                await route.fulfill(response=resp)
                                return
                            logger.warning(f"[Doubao] STREAM_ERROR in image gen mode (non-rate-limit, will wait for images): {msg[:200]}")
                            continue
                        q.put_nowait(("error", msg))
                        q.put_nowait(("done", ""))
                        await route.fulfill(response=resp)
                        return

                    if event_type == "CHUNK_DELTA":
                        delta = data.get("text", "")
                        if delta:
                            count += 1
                            q.put_nowait(("chunk", delta))
                            # 图片生成模式：从 CHUNK_DELTA 文本中提取 Markdown 图片链接
                            img_matches = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', delta)
                            for img_url in img_matches:
                                q.put_nowait(("image_url", img_url))
                                _image_found = True
                                _shared_image_found = True
                                _shared_image_urls.add(img_url)
                                logger.info(f"[Doubao] extracted image URL from CHUNK_DELTA: {img_url}")
                        continue

                    content_blocks = []
                    if event_type == "STREAM_MSG_NOTIFY":
                        content_blocks = data.get("content", {}).get("content_block", [])
                    elif event_type == "STREAM_CHUNK":
                        for op in data.get("patch_op", []):
                            pv = op.get("patch_value", {})
                            content_blocks.extend(pv.get("content_block", []))

                    # 通用提取：如果还没找到图片，搜索整个 JSON 结构
                    if image_generation and not _image_found:
                        for url in _extract_image_urls_from_json(data):
                            if url not in _shared_image_urls:
                                q.put_nowait(("image_url", url))
                                _image_found = True
                                _shared_image_found = True
                                _shared_image_urls.add(url)
                                logger.info(f"[Doubao] extracted image URL from JSON search: {url}")

                    for cb in content_blocks:
                        if cb.get("block_type") == 2074:
                            cb_content = cb.get("content", {})
                            creation_data = cb_content.get("creation_block", cb_content)
                            creations = creation_data.get("creations", [])
                            for creation in creations:
                                img_info = creation.get("image", {}) or creation
                                img_url = (img_info.get("image_raw", {}).get("url") or
                                           img_info.get("image_thumb", {}).get("url") or
                                           img_info.get("image_ori", {}).get("url"))
                                if img_url:
                                    q.put_nowait(("image_url", img_url))
                                    _image_found = True
                                    _shared_image_found = True
                                    _shared_image_urls.add(img_url)
                            continue
                        if cb.get("block_type") != 10000:
                            continue
                        block_id = cb.get("block_id", "") or "default"
                        text_block = cb.get("content", {}).get("text_block", {})
                        current = text_block.get("text", "")
                        if not current:
                            continue
                        previous = last_block_text.get(block_id, "")
                        delta = current[len(previous):] if current.startswith(previous) else current
                        last_block_text[block_id] = current
                        if delta:
                            count += 1
                            q.put_nowait(("chunk", delta))
                        if cb.get("is_finish"):
                            img_urls = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', current)
                            for img_url in img_urls:
                                q.put_nowait(("image_url", img_url))
                                _image_found = True
                                _shared_image_found = True
                                _shared_image_urls.add(img_url)
                                logger.info(f"[Doubao] extracted image URL from text block: {img_url}")

                    if event_type == "SSE_REPLY_END" and data.get("end_type") == 3:
                        if image_generation:
                            if _image_found or _request_num >= 2:
                                q.put_nowait(("image_urls_sse", list(_shared_image_urls)))
                                q.put_nowait(("done", ""))
                            # else: first request without images, wait for second without emitting done
                            await route.fulfill(response=resp)
                            return
                        q.put_nowait(("done", ""))
                        logger.info(f"[Doubao] parsed {count} chunks")
                        await route.fulfill(response=resp)
                        return

                logger.info(f"[Doubao] parsed {count} chunks (end of response), image_found={_image_found}, request_num={_request_num}")
                if not image_generation:
                    q.put_nowait(("done", ""))
                elif _image_found:
                    logger.info("[Doubao] image found at end of response (no SSE_REPLY_END end_type=3), emitting done")
                    q.put_nowait(("image_urls_sse", list(_shared_image_urls)))
                    q.put_nowait(("done", ""))
                elif _request_num >= 2:
                    logger.info("[Doubao] no image found in tool result (no SSE_REPLY_END), emitting done to close attempt")
                    q.put_nowait(("image_urls_sse", list(_shared_image_urls)))
                    q.put_nowait(("done", ""))
                try:
                    await route.fulfill(response=resp)
                except Exception as inner_e:
                    if "already handled" in str(inner_e).lower():
                        pass
                    else:
                        raise
            except Exception as e:
                if "already handled" in str(e).lower():
                    return
                logger.warning(f"[Doubao] route err: {e}")
                q.put_nowait(("error", str(e)))
                q.put_nowait(("done", ""))
                try:
                    await route.continue_()
                except Exception:
                    pass

        # 确保页面在新对话状态（而非旧对话），除非明确复用已有对话
        if not reuse_conversation:
            current_url = self._doubao_page.url
            if not current_url.endswith("/chat/") and "/chat/" in current_url:
                # 页面在旧对话中，导航到新对话
                logger.info("[Doubao] navigating to new chat (was in existing conversation)")
                try:
                    await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                except Exception as nav_err:
                    logger.warning(f"[Doubao] navigation to /chat/ failed (will retry): {nav_err}")
                    try:
                        await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                    except Exception as nav_err2:
                        logger.warning(f"[Doubao] second navigation attempt also failed: {nav_err2}")
                await asyncio.sleep(1)

        try:
            # 注册 route handler：拦截并注入 request 内容
            logger.info("[Doubao] registering route handler: **/chat/completion**")
            await self._doubao_page.route("**/chat/completion**", handle_route)
            logger.info("[Doubao] route handler registered, current URL: " + self._doubao_page.url)
            # 图像生成模式：在 /chat/ 页面点击"图像生成"按钮，再通过 contenteditable 输入提示词
            if image_generation:
                logger.info("[Doubao] switching to image generation mode on /chat/ page")
                # 确保在新对话页面
                current = self._doubao_page.url
                need_navigate = "/chat/" not in current or "/chat/create-image" in current or not current.rstrip('/').endswith('/chat')
                if need_navigate:
                    try:
                        await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        logger.warning("[Doubao] navigate to /chat/ timed out, force reloading...")
                        try:
                            await self._doubao_page.reload(wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                    await asyncio.sleep(2)

                 # 等待"图像生成"按钮出现（最多30秒）
                clicked_btn = False
                for _poll in range(30):
                    # 1) 检查错误页
                    page_buttons = await self._doubao_page.evaluate("""() => {
                        const btns = document.querySelectorAll('button');
                        return Array.from(btns).map(b => (b.textContent || '').trim()).filter(t => t);
                    }""")
                    # 检查页面是否需要刷新（错误页通常含"刷新页面"按钮）
                    has_refresh = any(t == '刷新页面' for t in page_buttons)
                    if has_refresh:
                        logger.info(f"[Doubao] page error (found '刷新页面'), attempting refresh cycle...")
                        # 点击第一个"刷新页面"按钮
                        await self._doubao_page.evaluate("""() => {
                            const btns = document.querySelectorAll('button');
                            for (const b of btns) {
                                if ((b.textContent || '').trim() === '刷新页面') {
                                    b.click();
                                    return true;
                                }
                            }
                            return false;
                        }""")
                        await asyncio.sleep(5)  # 等待刷新操作
                        continue

                    # 2) Playwright 原生点击（触发 React 合成事件）
                    try:
                        img_btn = self._doubao_page.locator('[data-skill-id="skill_bar_button_3"]')
                        if await img_btn.count() > 0:
                            await img_btn.click(force=True, timeout=3000)
                            clicked_btn = True
                            logger.info("[Doubao] clicked '图像生成' via Playwright native click")
                            break
                    except Exception as e:
                        logger.info(f"[Doubao] Playwright click failed: {e}")

                    # 未找到，等待后重试
                    await asyncio.sleep(1)
                else:
                    # 超时且未找到
                    try:
                        btns_dump = await self._doubao_page.evaluate("""() => {
                            const btns = document.querySelectorAll('button[data-component-type="skill-item"]');
                            return Array.from(btns).map(b => ({
                                text: (b.textContent || '').trim().substring(0, 30),
                                skillId: b.getAttribute('data-skill-id') || '',
                                                               skillType: b.getAttribute('data-skill-type') || '',
                                visible: b.offsetParent !== null,
                                disabled: b.disabled
                            }));
                        }""")
                        logger.warning(f"[Doubao] image gen button not found. skill-item dump: {json.dumps(btns_dump, ensure_ascii=False)}")
                    except Exception:
                        pass
                    yield ("error", "Cannot find '图像生成' button on /chat/ page")
                    yield ("done", "")
                    return

                # 等待 UI 稳定：点击按钮 + 关闭 modal 后，输入框可能延迟出现
                # 等 3 秒基础时间，再轮询 30 次（每次 1 秒）
                await asyncio.sleep(3)
                page_state = {}
                for _wait in range(30):
                    page_state = await self._doubao_page.evaluate("""() => {
                        const textarea = document.querySelector('textarea');
                        const contenteditable = document.querySelector('[contenteditable=true][role=textbox]');
                        const visibleTextarea = document.querySelector('textarea:not([aria-hidden="true"])');
                        const anyContenteditable = document.querySelector('[contenteditable="true"]');
                        const sendBtn = document.getElementById('flow-end-msg-send');
                        const sendBtnAny = document.querySelector('button[data-testid*="send"], button[class*="send"]');
                        const ariaHidden = textarea ? textarea.getAttribute('aria-hidden') : 'none';
                        return {
                            hasTextarea: !!textarea,
                            textareaVisible: textarea ? (textarea.offsetParent !== null && ariaHidden !== 'true') : false,
                            textareaAriaHidden: ariaHidden,
                            visibleTextareaExists: !!visibleTextarea,
                            hasContenteditable: !!contenteditable,
                            hasAnyContenteditable: !!anyContenteditable,
                            hasSendBtn: !!sendBtn,
                            sendBtnVisible: sendBtn ? (sendBtn.offsetParent !== null && !sendBtn.disabled) : false,
                            hasAnySendBtn: !!sendBtnAny,
                            url: location.href
                        };
                    }""")
                    if page_state.get('hasContenteditable') or page_state.get('visibleTextareaExists') or page_state.get('hasAnyContenteditable') or page_state.get('textareaVisible'):
                        break
                    await asyncio.sleep(1)
                logger.info(f"[Doubao] page state after click: {json.dumps(page_state, ensure_ascii=False)}")

                # 即使 textarea aria-hidden=true，也强制尝试输入（有时候它其实是可用的）
                send_text = text
                input_used = None
                send_result = None

                if page_state.get('hasContenteditable'):
                    input_used = "contenteditable"
                    logger.info(f"[Doubao] typing prompt into contenteditable: {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const el = document.querySelector('[contenteditable=true][role=textbox]');
                        if (!el) return;
                        el.focus();
                        el.innerHTML = '';
                        el.textContent = send_text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    send_result = await self._doubao_page.evaluate("""() => {
                        const btn = document.getElementById('flow-end-msg-send');
                        if (btn && !btn.disabled && btn.offsetParent !== null) {
                            btn.click();
                            return 'send-btn';
                        }
                        return '';
                    }""")

                elif page_state.get('visibleTextareaExists') or page_state.get('textareaVisible'):
                    input_used = "textarea(visible)"
                    logger.info(f"[Doubao] typing prompt into visible textarea: {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const ta = document.querySelector('textarea');
                        if (!ta) return;
                        ta.focus();
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        nativeSetter.call(ta, send_text);
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    send_result = await self._doubao_page.evaluate("""() => {
                        const btn = document.getElementById('flow-end-msg-send');
                        if (btn && !btn.disabled && btn.offsetParent !== null) {
                            btn.click();
                            return 'send-btn';
                        }
                        return '';
                    }""")

                elif page_state.get('hasAnyContenteditable'):
                    input_used = "contenteditable(any)"
                    logger.info(f"[Doubao] typing prompt into any contenteditable: {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const el = document.querySelector('[contenteditable="true"]');
                        if (!el) return;
                        el.focus();
                        el.innerText = send_text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    send_result = 'eval-fallback'

                elif page_state.get('hasTextarea'):
                    # textarea 存在但 aria-hidden=true，强制尝试
                    input_used = "textarea(force)"
                    logger.info(f"[Doubao] forcing textarea input (aria-hidden={page_state.get('textareaAriaHidden')}): {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const ta = document.querySelector('textarea');
                        if (!ta) return;
                        ta.removeAttribute('aria-hidden');
                        ta.focus();
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        nativeSetter.call(ta, send_text);
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    # 尝试点击发送或按 Enter
                    send_result = await self._doubao_page.evaluate("""() => {
                        const btn = document.getElementById('flow-end-msg-send');
                        if (btn && !btn.disabled) {
                            btn.click();
                            return 'send-btn';
                        }
                        return '';
                    }""")

                if input_used:
                    logger.info(f"[Doubao] input method: {input_used}")
                    if send_result == 'send-btn':
                        logger.info("[Doubao] image gen: sent via send-btn")
                    else:
                        await self._doubao_page.keyboard.press("Enter")
                        logger.info("[Doubao] image gen: sent via Enter")
                else:
                    logger.warning("[Doubao] No viable input method found after image gen button click")
                    logger.warning(f"[Doubao] page_state: {json.dumps(page_state, ensure_ascii=False)}")
                    yield ("error", "No input found after clicking '图像生成' button")
                    yield ("done", "")
                    return
            else:
                # 复用对话时，页面在历史对话，需轮询等待输入框
                if reuse_conversation:
                    ta_found = False
                    for _poll in range(360):  # 最长等待3分钟
                        ta_found = await self._doubao_page.evaluate("""() => {
                            const ta = document.querySelector('textarea');
                            return !!ta;
                        }""")
                        if ta_found:
                            break
                        await asyncio.sleep(0.5)
                    if not ta_found:
                        logger.warning("[Doubao] reuse: textarea not found after polling")
                        yield ("error", "欲渡黄河冰塞川，将登太行雪满山。")
                        yield ("done", "")
                        return
                    logger.info("[Doubao] reuse: textarea found, typing prompt")
                    logger.info(f"[Doubao] before typing, current page URL: {self._doubao_page.url}")
                else:
                    # 新对话：等待 textarea 或 contenteditable 渲染完成
                    input_ready = False
                    for _poll in range(20):  # 最多 20 秒
                        input_ready = await self._doubao_page.evaluate("""() => {
                            const ta = document.querySelector('textarea');
                            const ce = document.querySelector('[contenteditable=true][role=textbox]');
                            return !!(ta || ce);
                        }""")
                        if input_ready:
                            break
                        await asyncio.sleep(1)
                    if not input_ready:
                        logger.warning("[Doubao] input (textarea/contenteditable) not ready after polling for new conversation")
                        yield ("error", "欲渡黄河冰塞川，将登太行雪满山。")
                        yield ("done", "")
                        return
                    logger.info("[Doubao] input element found for new conversation")

                # 原有逻辑：使用 textarea
                ok = await self._doubao_page.evaluate("""() => {
                    const ta = document.querySelector('textarea');
                    if (ta) {
                        ta.focus();
                        ta.click();
                        return true;
                    }
                    const ce = document.querySelector('[contenteditable=true][role=textbox]');
                    if (ce) {
                        ce.focus();
                        ce.click();
                        return true;
                    }
                    return false;
                }""")
                if not ok:
                    logger.warning("[Doubao] textarea focus failed")
                    yield ("error", "欲渡黄河冰塞川，将登太行雪满山。")
                    yield ("done", "")
                    return
                import sys as _sys
                _mod = "Meta" if _sys.platform == "darwin" else "Control"
                await self._doubao_page.keyboard.press(f"{_mod}+a")
                await self._doubao_page.keyboard.press("Backspace")
                pasted = False
                try:
                    if self._doubao_browser:
                        try:
                            await self._doubao_browser.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.doubao.com")
                        except Exception:
                            pass
                    await self._doubao_page.evaluate("""async (text) => {
                        await navigator.clipboard.writeText(text);
                    }""", text)
                    await self._doubao_page.keyboard.press(f"{_mod}+v")
                    pasted = True
                except Exception as paste_e:
                    logger.warning(f"[Doubao] clipboard paste failed, fallback insert_text: {paste_e}")
                    await self._doubao_page.keyboard.insert_text(text)
                await asyncio.sleep(0.3)
                await self._doubao_page.keyboard.press("End")
                await self._doubao_page.keyboard.type(" ")
                await self._doubao_page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                await self._doubao_page.keyboard.press("Enter")
                logger.info(f"[Doubao] {'clipboard pasted' if pasted else 'insert_text'} + Enter (initial send attempt)")

                # Verification and retry loop
                max_send_retries = 8
                for _verify_attempt in range(max_send_retries):
                    await asyncio.sleep(1) # Wait for UI to react after previous action

                    check_result = await self._doubao_page.evaluate("""() => {
                        const ta = document.querySelector('textarea');
                        const hasText = (ta && ta.value && ta.value.trim().length > 0) ||
                                        (() => {
                                            const ce = document.querySelector('[contenteditable=true][role=textbox]');
                                            return ce && ce.innerText && ce.innerText.trim().length > 0;
                                        })();

                        const getSendBtn = () => {
                            const btn = document.getElementById('flow-end-msg-send') ||
                                       document.querySelector('button[data-testid*="send"]') ||
                                       document.querySelector('button[class*="send"]');
                            return btn;
                        };
                        const btn = getSendBtn();
                        let sendEnabled = false;
                        if (btn) {
                            const isDisabled = btn.disabled === true ||
                                               btn.getAttribute('aria-disabled') === 'true' ||
                                               btn.classList.contains('disabled') ||
                                               btn.getAttribute('disabled') !== null;
                            sendEnabled = !isDisabled;
                        }
                        return { hasText, sendEnabled };
                    }""") or {}

                    still_has_text = bool(check_result.get('hasText'))
                    send_enabled = bool(check_result.get('sendEnabled'))

                    if not still_has_text:
                        logger.info("[Doubao] Input cleared, message sent successfully.")
                        break # Success, input cleared

                    if still_has_text and send_enabled:
                        logger.warning(f"[Doubao] Input still has text (attempt {_verify_attempt+1}/{max_send_retries}), send button is enabled. Retrying via Playwright click.")
                        try:
                            await self._doubao_page.click('#flow-end-msg-send, button[data-testid*="send"], button[class*="send"]', timeout=5000)
                            logger.info("[Doubao] Playwright click on send button successful.")
                        except Exception as click_e:
                            logger.warning(f"[Doubao] Playwright click on send button failed: {click_e}. Falling back to Enter key for retry.")
                            await self._doubao_page.keyboard.press("Enter")
                        await asyncio.sleep(1)
                        recheck = await self._doubao_page.evaluate("""() => {
                            const ta = document.querySelector('textarea');
                            return (ta && ta.value && ta.value.trim().length > 0) ||
                                   (() => {
                                       const ce = document.querySelector('[contenteditable=true][role=textbox]');
                                       return ce && ce.innerText && ce.innerText.trim().length > 0;
                                   })();
                        }""")
                        if not recheck:
                            logger.info("[Doubao] Input cleared after retry click, message sent successfully.")
                            break
                        continue
                    elif still_has_text and not send_enabled:
                        logger.warning(f"[Doubao] Input still has text, but send button is disabled (attempt {_verify_attempt+1}/{max_send_retries}). Will recheck.")
                else: # This 'else' block executes if the loop completes without a 'break' (i.e., all attempts failed)
                    if still_has_text: # Last check after all retries
                        logger.error("[Doubao] Failed to send message after multiple attempts. Input still has text and message was not sent.")
                        yield ("error", "send timeout - message not sent")
                        yield ("done", "")
                        return

                # Final wait before letting the stream proceed
                await asyncio.sleep(2)
        except Exception as e:
            yield ("error", f"Keyboard: {e}")
            yield ("done", "")
            return

        try:
            while True:
                kind, value = await asyncio.wait_for(q.get(), timeout=timeout)
                if kind == "done":
                    if image_generation:
                        logger.info("[Doubao] image gen done (SSE URLs already emitted, skipping DOM extraction)")
                    yield ("done", "")
                    break
                if kind == "error":
                    yield ("error", value)
                    continue
                if kind == "conversation_id":
                    yield ("conversation_id", value)
                    continue
                if kind == "image_url":
                    yield ("image_url", value)
                    continue
                if kind == "local_image_url":
                    yield ("local_image_url", value)
                    continue
                if kind == "image_urls_sse":
                    yield ("image_urls_sse", value)
                    continue
                yield ("chunk", value)
        except asyncio.TimeoutError:
            logger.warning(f"[Doubao] timeout after {timeout}s - no response from server, resetting page")
            yield ("error", "行到水穷处，坐看云起时。")
            yield ("done", "")
            try:
                await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=30000)
                await asyncio.sleep(1)
                logger.info("[Doubao] page reset after timeout")
            except Exception as nav_err:
                logger.warning(f"[Doubao] page reset failed: {nav_err}")
        finally:
            self._doubao_queues.pop(stream_id, None)
            try:
                if self._doubao_page and not self._doubao_page.is_closed():
                    await self._doubao_page.unroute("**/chat/completion**", handle_route)
            except Exception:
                pass

    async def stream_doubao_create_image(self, text: str):
        """Navigate to create-image page, type prompt, send, and extract image URLs.
        Uses route interception on chat/completion to capture SSE response.
        Enhanced: handles CHUNK_DELTA text, creation_block 2074, rate-limit detection,
        recursive JSON URL search, and longer timeouts.
        """
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._doubao_queues[stream_id] = q

        # Shared state for image URL extraction
        _found_image_urls = set()

        def _extract_image_urls_from_json(obj):
            """Recursively search any JSON object for image URLs."""
            urls = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    urls.update(_extract_image_urls_from_json(k))
                    urls.update(_extract_image_urls_from_json(v))
            elif isinstance(obj, list):
                for item in obj:
                    urls.update(_extract_image_urls_from_json(item))
            elif isinstance(obj, str):
                matches = re.findall(
                    r'https?://[^\s"\'<>]+(?:\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&))',
                    obj, re.IGNORECASE
                )
                for m in matches:
                    urls.add(m)
                matches2 = re.findall(
                    r'https?://[^\s"\'<>]*(?:p\d+-|image|img|pic|photo|media)[^\s"\'<>]*\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&)',
                    obj, re.IGNORECASE
                )
                for m in matches2:
                    urls.add(m)
                # Also match rc_gen_image URLs (the Doubao image generation pattern)
                matches3 = re.findall(
                    r'(https?://[^\s"\'<>]*rc_gen_image[^\s"\'<>]*)',
                    obj, re.IGNORECASE
                )
                for m in matches3:
                    urls.add(m)
            return urls

        async def handle_route(route):
            if 'doubao.com/chat/completion' not in route.request.url:
                await route.continue_()
                return
            try:
                resp = await route.fetch(timeout=180000)
                body = await resp.body()
                raw_text = body.decode("utf-8", errors="replace")

                last_block_text = {}

                for block in raw_text.split("\n\n"):
                    block = block.strip()
                    if not block:
                        continue

                    event_type = ""
                    data_str = ""
                    for line in block.split("\n"):
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()

                    if not data_str:
                        continue

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if event_type == "SSE_ACK":
                        cid = data.get("ack_client_meta", {}).get("conversation_id", "")
                        if cid:
                            q.put_nowait(("conversation_id", cid))
                        continue

                    if event_type == "STREAM_ERROR":
                        error_code = data.get("error_code")
                        error_msg_text = data.get("error_msg", "")
                        is_rate_limit = (error_code == 710022004 or
                                         "rate" in error_msg_text.lower() and "limit" in error_msg_text.lower())
                        if is_rate_limit:
                            q.put_nowait(("error", json.dumps(data, ensure_ascii=False)))
                            q.put_nowait(("done", ""))
                            await route.fulfill(response=resp)
                            return
                        continue

                    if event_type == "CHUNK_DELTA":
                        delta = data.get("text", "")
                        if delta:
                            # Extract markdown image links
                            img_matches = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', delta)
                            for img_url in img_matches:
                                if img_url not in _found_image_urls:
                                    _found_image_urls.add(img_url)
                                    q.put_nowait(("image_url", img_url))
                            # Also recursive search
                            for url in _extract_image_urls_from_json(data):
                                if url not in _found_image_urls:
                                    _found_image_urls.add(url)
                                    q.put_nowait(("image_url", url))
                        continue

                    content_blocks = []
                    if event_type == "STREAM_MSG_NOTIFY":
                        content_blocks = data.get("content", {}).get("content_block", [])
                    elif event_type == "STREAM_CHUNK":
                        for op in data.get("patch_op", []):
                            pv = op.get("patch_value", {})
                            content_blocks.extend(pv.get("content_block", []))

                    for cb in content_blocks:
                        # Handle creation_block (block_type 2074)
                        if cb.get("block_type") == 2074:
                            cb_content = cb.get("content", {})
                            creation_data = cb_content.get("creation_block", cb_content)
                            creations = creation_data.get("creations", [])
                            for creation in creations:
                                img_info = creation.get("image", {}) or creation
                                img_url = (img_info.get("image_raw", {}).get("url") or
                                           img_info.get("image_thumb", {}).get("url") or
                                           img_info.get("image_ori", {}).get("url"))
                                if img_url and img_url not in _found_image_urls:
                                    _found_image_urls.add(img_url)
                                    q.put_nowait(("image_url", img_url))
                                    logger.info(f"[Doubao create-image] extracted creation image: {img_url[:120]}")
                            continue
                        # Handle text_block (block_type 10000)
                        if cb.get("block_type") != 10000:
                            continue
                        block_id = cb.get("block_id", "") or "default"
                        text_block = cb.get("content", {}).get("text_block", {})
                        current = text_block.get("text", "")
                        if not current:
                            continue
                        previous = last_block_text.get(block_id, "")
                        delta = current[len(previous):] if current.startswith(previous) else current
                        last_block_text[block_id] = current
                        if delta:
                            q.put_nowait(("chunk", delta))
                        if cb.get("is_finish"):
                            img_urls = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', current)
                            for img_url in img_urls:
                                if img_url not in _found_image_urls:
                                    _found_image_urls.add(img_url)
                                    q.put_nowait(("image_url", img_url))
                                    logger.info(f"[Doubao create-image] extracted from finished text block: {img_url[:120]}")

                    if event_type == "SSE_REPLY_END" and data.get("end_type") == 3:
                        if _found_image_urls:
                            q.put_nowait(("image_urls_sse", list(_found_image_urls)))
                        q.put_nowait(("done", ""))
                        await route.fulfill(response=resp)
                        return

                # End of SSE response
                if _found_image_urls:
                    q.put_nowait(("image_urls_sse", list(_found_image_urls)))
                q.put_nowait(("done", ""))
                try:
                    await route.fulfill(response=resp)
                except Exception:
                    pass
            except Exception as e:
                if "already handled" in str(e).lower():
                    return
                logger.warning(f"[Doubao create-image] route err: {e}")
                q.put_nowait(("error", str(e)))
                q.put_nowait(("done", ""))
                try:
                    await route.continue_()
                except Exception:
                    pass

        await self._doubao_page.route("**/chat/completion**", handle_route)

        try:
            await self._doubao_page.goto("https://www.doubao.com/chat/create-image", wait_until="load", timeout=30000)
            await asyncio.sleep(2)

            has_overlay = await self._doubao_page.evaluate("""() => {
                const overlays = document.querySelectorAll('[role="dialog"], [data-testid="modal"], .modal, .overlay');
                for (const el of overlays) {
                    if (el.offsetParent !== null && el.style.display !== 'none') {
                        return true;
                    }
                }
                return false;
            }""")
            if has_overlay:
                await self._doubao_page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            input_ok = await self._doubao_page.evaluate("""() => {
                const el = document.querySelector('[contenteditable=true][role=textbox]');
                if (!el) return false;
                el.focus();
                el.click();
                return true;
            }""")
            if not input_ok:
                yield ("error", "No contenteditable input")
                yield ("done", "")
                return

            await self._doubao_page.evaluate("""(text) => {
                const el = document.querySelector('[contenteditable=true][role=textbox]');
                if (!el) return;
                el.textContent = text;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""", text)
            await asyncio.sleep(0.5)

            send_clicked = await self._doubao_page.evaluate("""() => {
                const btn = document.getElementById('flow-end-msg-send');
                if (btn && !btn.disabled && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
                return false;
            }""")
            if send_clicked:
                logger.info("[Doubao] create-image: typed text and clicked send button")
            else:
                logger.warning("[Doubao] create-image: send button not found or disabled, trying Enter")
                await self._doubao_page.keyboard.press("Enter")
            await asyncio.sleep(1)

            # Wait for SSE response - longer timeout for image generation
            while True:
                kind, value = await asyncio.wait_for(q.get(), timeout=180)
                if kind == "done":
                    yield ("done", "")
                    break
                if kind == "error":
                    yield ("error", value)
                    continue
                if kind == "conversation_id":
                    yield ("conversation_id", value)
                    continue
                if kind == "image_url":
                    yield ("image_url", value)
                    continue
                if kind == "image_urls_sse":
                    yield ("image_urls_sse", value)
                    continue
                yield (kind, value)

        except asyncio.TimeoutError:
            logger.warning("[Doubao create-image] timeout waiting for SSE response")
            yield ("error", "饥来驱我去，不知竟何之。行行至斯里，叩门拙言辞。")
            yield ("done", "")
            try:
                await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=30000)
                await asyncio.sleep(1)
            except Exception:
                pass
        finally:
            self._doubao_queues.pop(stream_id, None)
            try:
                if self._doubao_page and not self._doubao_page.is_closed():
                    await self._doubao_page.unroute("**/chat/completion**", handle_route)
            except Exception:
                pass

    async def delete_doubao_conversations(self, conversation_ids: list):
        """批量删除豆包网页版对话。"""
        for conv_id in conversation_ids:
            try:
                ok, err = await self.delete_conversation_via_browser(conv_id)
                if ok:
                    logger.info(f"Deleted doubao conversation {conv_id}")
                else:
                    logger.warning(f"Failed to delete doubao conversation {conv_id}: {err}")
            except Exception as e:
                logger.warning(f"Error deleting doubao conversation {conv_id}: {e}")

    async def delete_all_doubao_conversations(self):
        """删除所有豆包会话（通过浏览器页面 API）。"""
        try:
            if not self._doubao_page or self._doubao_page.is_closed():
                logger.warning("[Doubao] no page, skip batch delete")
                return

            device_id = CONFIG.get('device_id', '')
            web_id = CONFIG.get('web_id', '')
            tea_uuid = CONFIG.get('tea_uuid', '')

            result = await self._doubao_page.evaluate("""async (args) => {
                const { device_id, web_id, tea_uuid } = args;
                try {
                    const params = new URLSearchParams({
                        'version_code': '20800', 'language': 'zh', 'device_platform': 'web',
                        'aid': '497858', 'real_aid': '497858', 'pkg_type': 'release_version',
                        'device_id': device_id, 'pc_version': '3.22.1', 'web_id': web_id,
                        'tea_uuid': tea_uuid, 'region': 'CN', 'sys_region': 'CN',
                        'samantha_web': '1', 'web_platform': 'browser', 'use-olympus-account': '1',
                        'web_tab_id': crypto.randomUUID(),
                    });
                    const listResp = await fetch('/im/chain/recent_conv?' + params.toString(), {
                        method: 'POST',
                        headers: { 'content-type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str' },
                        body: JSON.stringify({
                            'cmd': 3200,
                            'uplink_body': { 'pull_recent_conv_chain_uplink_body': {
                                'limit': 200, 'message_count_per_conv': 0, 'api_version': 1, 'conv_version': 0, 'direction': 3,
                                'option': { 'not_need_message': true, 'need_complete_conversation': true, 'need_coco_conversation': true, 'need_coco_bot': true, 'need_pc_pin_chain': true, 'pc_pin_query_type': 0 }
                            }},
                            'sequence_id': crypto.randomUUID(), 'channel': 2, 'version': '1',
                        })
                    });
                    const listData = await listResp.json();
                    const cells = listData?.downlink_body?.pull_recent_conv_chain_downlink_body?.cells || [];
                    const convIds = cells.map(c => c.conversation?.conversation_id).filter(Boolean);

                    if (convIds.length === 0) return { deleted: 0, total: 0 };

                    const delResp = await fetch('/im/conversation/batch_del_user_conv?' + params.toString(), {
                        method: 'POST',
                        headers: { 'content-type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str' },
                        body: JSON.stringify({
                            'cmd': 4171,
                            'uplink_body': { 'batch_delete_user_conversation_uplink_body': { 'conversation_id': convIds, 'delete_all': false, 'conversation_type': 3 } },
                            'sequence_id': crypto.randomUUID(), 'channel': 2, 'version': '1',
                        })
                    });
                    const delData = await delResp.json();
                    const result = delData?.downlink_body?.batch_delete_user_conversation_downlink_body?.result || {};
                    const deleted = Object.values(result).filter(v => v === true).length;
                    return { deleted, total: convIds.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", {"device_id": device_id, "web_id": web_id, "tea_uuid": tea_uuid})
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Doubao] delete_all: deleted {deleted}/{total} sessions")
        except Exception as e:
            logger.warning(f"[Doubao] delete_all exception: {e}")

    async def delete_conversation_via_browser(self, conversation_id: str, skip_lock: bool = False) -> tuple[bool, str]:
        """通过浏览器页面调用豆包 API 删除对话，使用页面自带的认证信息。
        skip_lock: 如果 True，则不会尝试获取 _doubao_lock（假设调用者已持有锁）。
        """
        if not skip_lock:
            await self._doubao_lock.acquire()
        result = None
        try:
            # 检查浏览器是否可用
            try:
                browser_ok = (
                    self._doubao_page is not None
                    and not self._doubao_page.is_closed()
                    and self._doubao_browser is not None
                    and self._doubao_browser.pages
                )
            except Exception:
                browser_ok = False
            if not browser_ok:
                logger.debug("[Doubao] Browser not connected or page closed, falling back to HTTP API")
                return False, "Browser not connected"

            # 检查当前页面 URL，确认仍在聊天上下文中，避免导航中调用
            try:
                current_url = self._doubao_page.url if hasattr(self._doubao_page, 'url') else 'unknown'
                if not current_url.startswith('https://www.doubao.com/chat'):
                    logger.warning(f"[Doubao] delete_conversation_via_browser: current page URL is {current_url}, expected /chat")
                    return False, "Page not in chat context"
            except Exception as e:
                logger.warning(f"[Doubao] delete_conversation_via_browser: failed to get page URL: {e}")

            device_id = CONFIG.get('device_id', '')
            web_id = CONFIG.get('web_id', '')
            tea_uuid = CONFIG.get('tea_uuid', '')

            js_code = """
                async (args) => {
                    const { conv_id, device_id, web_id, tea_uuid } = args;
                    try {
                        const params = new URLSearchParams({
                            'version_code': '20800',
                            'language': 'zh',
                            'device_platform': 'web',
                            'aid': '497858',
                            'real_aid': '497858',
                            'pkg_type': 'release_version',
                            'device_id': device_id,
                            'pc_version': '3.22.1',
                            'web_id': web_id,
                            'tea_uuid': tea_uuid,
                            'region': 'CN',
                            'sys_region': 'CN',
                            'samantha_web': '1',
                            'web_platform': 'browser',
                            'use-olympus-account': '1',
                            'web_tab_id': crypto.randomUUID(),
                        });
                        const response = await fetch(
                            '/im/conversation/batch_del_user_conv?' + params.toString(),
                            {
                                method: 'POST',
                                headers: {
                                    'content-type': 'application/json; encoding=utf-8',
                                    'agw-js-conv': 'str',
                                    'referer': 'https://www.doubao.com/chat/',
                                },
                                body: JSON.stringify({
                                    'cmd': 4171,
                                    'uplink_body': {
                                        'batch_delete_user_conversation_uplink_body': {
                                            'conversation_id': [conv_id],
                                            'delete_all': false,
                                            'conversation_type': 3,
                                        }
                                    },
                                    'sequence_id': crypto.randomUUID(),
                                    'channel': 2,
                                    'version': '1',
                                })
                            }
                        );
                        const data = await response.json();
                        const result = data?.downlink_body?.batch_delete_user_conversation_downlink_body?.result;
                        return { success: result?.[conv_id] === true, data: data };
                    } catch (e) {
                        return { success: false, error: String(e) };
                    }
                }
            """
            js_args = {"conv_id": conversation_id, "device_id": device_id, "web_id": web_id, "tea_uuid": tea_uuid}

            # run_in_executor 隔离 cancel scope，内部用 run_coroutine_threadsafe 回到 event loop
            loop = asyncio.get_running_loop()
            future = asyncio.ensure_future(
                loop.run_in_executor(None, lambda: asyncio.run_coroutine_threadsafe(
                    self._doubao_page.evaluate(js_code, js_args), loop
                ).result())
            )
            try:
                result = await asyncio.shield(future)
                return result.get("success"), ""
            except asyncio.CancelledError:
                logger.info(f"[Doubao] delete_conversation_via_browser: shielded from cancel, waiting for result")
                try:
                    if future.done():
                        result = future.result()
                        return result.get("success"), ""
                    else:
                        result = await asyncio.wait_for(future, timeout=30.0)
                        return result.get("success"), ""
                except Exception as e:
                    logger.warning(f"[Doubao] delete_conversation_via_browser: post-cancel wait failed: {e}")
                    return False, str(e)
            except Exception as e:
                err_str = str(e)
                if "Execution context was destroyed" in err_str or "most likely because of a navigation" in err_str:
                    logger.warning(f"[Doubao] delete_conversation_via_browser: page navigation invalidated execution context: {e}")
                    return False, "Page navigation invalidated context"
                elif "cancelled" in err_str.lower() or "cancel scope" in err_str.lower():
                    logger.info(f"[Doubao] delete_conversation_via_browser: shielded from cancel, waiting for result")
                    try:
                        if future.done():
                            result = future.result()
                            return result.get("success"), ""
                        else:
                            result = await asyncio.wait_for(future, timeout=30.0)
                            return result.get("success"), ""
                    except Exception:
                        return False, "Post-cancel access failed"
                else:
                    logger.warning(f"[Doubao] delete_conversation_via_browser: error: {e}")
                    return False, str(e)
        except (Exception, asyncio.CancelledError) as e:
            err_str = str(e)
            if "cancelled" in err_str.lower() or "cancel scope" in err_str.lower():
                logger.info(f"[Doubao] delete_conversation_via_browser: cancelled during shutdown: {err_str[:100]}")
            elif "Execution context was destroyed" in err_str or "most likely because of a navigation" in err_str:
                logger.warning(f"[Doubao] delete_conversation_via_browser: execution context destroyed (likely due to page navigation)")
            else:
                logger.warning(f"[Doubao] Error deleting conversation {conversation_id} via browser: {e}")
            return False, str(e)
        finally:
            if not skip_lock:
                self._doubao_lock.release()

    async def show_doubao_for_rate_limit(self):
        """关闭 headless 浏览器，启动 visible 浏览器供用户处理限流/验证码。"""
        self._visible_browser_started_at = time.time()
        try:
            if self._doubao_page:
                try:
                    if hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed():
                        logger.info("[Doubao] page already closed by user, skipping close")
                    else:
                        await self._doubao_page.close()
                except Exception as e:
                    logger.warning(f"[Doubao] page already closed: {e}")
                self._doubao_page = None
            if self._doubao_browser:
                try:
                    await self._doubao_browser.close()
                except Exception as e:
                    logger.warning(f"[Doubao] browser already disconnected: {e}")
                self._doubao_browser = None
            if self._doubao_pw:
                try:
                    await self._doubao_pw.stop()
                except Exception as e:
                    logger.warning(f"[Doubao] pw already stopped: {e}")
                self._doubao_pw = None
        except Exception as e:
            logger.warning(f"[Doubao] error closing headless browser: {e}")
        await self.ensure_doubao_ready(headless=False)
        logger.info("[Doubao] visible browser started for rate limit handling")

    async def hide_doubao_browser(self):
        """关闭 visible 浏览器，立即恢复 headless 模式继续工作。"""
        self._visible_browser_started_at = None
        try:
            if self._doubao_page:
                try:
                    if hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed():
                        logger.info("[Doubao] page already closed by user, skipping close")
                    else:
                        await self._doubao_page.close()
                except Exception as e:
                    logger.warning(f"[Doubao] page already closed: {e}")
                self._doubao_page = None
            if self._doubao_browser:
                try:
                    await self._doubao_browser.close()
                except Exception as e:
                    logger.warning(f"[Doubao] browser already disconnected: {e}")
                self._doubao_browser = None
            if self._doubao_pw:
                try:
                    await self._doubao_pw.stop()
                except Exception as e:
                    logger.warning(f"[Doubao] pw already stopped: {e}")
                self._doubao_pw = None
        except Exception as e:
            logger.warning(f"[Doubao] error closing visible browser: {e}")
        try:
            await self.ensure_doubao_ready(headless=True)
            logger.info("[Doubao] visible browser closed, headless browser restarted")
        except Exception as e:
            logger.warning(f"[Doubao] failed to restart headless browser: {e}")

    async def upload_document_via_page(self, file_data: bytes, file_name: str) -> dict:
        """Upload file to doubao cloud storage via HTTP API, returns attachment dict with URI.
        The attachment must be injected into the request body separately.
        """
        import base64
        import binascii
        
        cookie = get_doubao_cookie()
        if not cookie:
            raise RuntimeError("Cannot read cookie from profiles/doubao_profile")

        device_id = CONFIG.get('device_id', '')
        tea_uuid = CONFIG.get('tea_uuid', '')

        headers = {
            'content-type': 'application/json',
            'cookie': cookie,
            'origin': 'https://www.doubao.com',
            'referer': 'https://www.doubao.com/chat/',
            'user-agent': USER_AGENT,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            params = "&".join([
                "aid=497858",
                f"device_id={device_id}",
                "device_platform=web",
                "language=zh",
                "pc_version=3.22.0",
                "pkg_type=release_version",
                "real_aid=497858",
                "region=CN",
                "samantha_web=1",
                "sys_region=CN",
                f"tea_uuid={tea_uuid}",
                "use-olympus-account=1",
                "version_code=20800",
            ])
            prepare_url = f"https://www.doubao.com/alice/resource/prepare_upload?{params}"
            prepare_resp = await client.post(prepare_url, headers=headers, json={"resource_type": 1, "scene_id": "5", "tenant_id": "5"})
            prepare_data = prepare_resp.json()
            if prepare_data.get("code") != 0:
                raise RuntimeError(f"prepare_upload failed: {json.dumps(prepare_data, ensure_ascii=False)[:500]}")

            service_id = prepare_data["data"]["service_id"]
            upload_auth = prepare_data["data"]["upload_auth_token"]
            access_key = upload_auth["access_key"]
            secret_key = upload_auth["secret_key"]
            session_token = upload_auth["session_token"]

            file_ext = f".{file_name.rsplit('.', 1)[-1]}" if '.' in file_name else ""
            file_size = len(file_data)
            apply_url = f"https://imagex.bytedanceapi.com/?Action=ApplyImageUpload&Version=2018-08-01&ServiceId={service_id}&NeedFallback=true&FileSize={file_size}&FileExtension={file_ext}"

            from requests_aws4auth import AWS4Auth
            auth = AWS4Auth(access_key, secret_key, 'cn-north-1', "imagex", session_token=session_token)
            apply_req = client.build_request(method="GET", url=apply_url, headers={
                "origin": "https://www.doubao.com",
                "referer": "https://www.doubao.com",
                "user-agent": USER_AGENT,
            })
            auth.__call__(apply_req)
            apply_resp = await client.send(apply_req)
            apply_data = apply_resp.json()
            store_infos = apply_data.get("Result", {}).get("UploadAddress", {}).get("StoreInfos", [])
            if not store_infos:
                raise RuntimeError(f"ApplyImageUpload no StoreInfos: {json.dumps(apply_data, ensure_ascii=False)[:500]}")

            store_info = store_infos[0]
            store_uri = store_info["StoreUri"]
            store_auth = store_info["Auth"]
            session_key = apply_data["Result"]["UploadAddress"]["SessionKey"]
            upload_hosts = apply_data["Result"]["UploadAddress"].get("UploadHosts", [])
            upload_host = upload_hosts[0] if upload_hosts else "tos-d-x-hl.snssdk.com"

            crc32 = format(binascii.crc32(file_data) & 0xFFFFFFFF, '08x')
            upload_headers = {
                "authorization": store_auth,
                "origin": "https://www.doubao.com",
                "referer": "https://www.doubao.com",
                "host": upload_host,
                "content-type": "application/octet-stream",
                "content-disposition": 'attachment; filename="undefined"',
                "content-crc32": crc32,
            }
            upload_url = f"https://{upload_host}/upload/v1/{store_uri}"
            upload_resp = await client.post(upload_url, content=file_data, headers=upload_headers)
            upload_result = upload_resp.json()
            if upload_result.get("message") != "Success":
                raise RuntimeError(f"Upload binary failed: {json.dumps(upload_result, ensure_ascii=False)[:500]}")

            commit_url = f"https://imagex.bytedanceapi.com/?Action=CommitImageUpload&Version=2018-08-01&ServiceId={service_id}"
            commit_headers = {"origin": "https://www.doubao.com", "referer": "https://www.doubao.com/", "user-agent": USER_AGENT}
            commit_req = client.build_request(method="POST", url=commit_url, headers=commit_headers, json={"SessionKey": session_key})
            auth.__call__(commit_req)
            commit_resp = await client.send(commit_req)
            commit_data = commit_resp.json()
            results = commit_data.get("Result", {}).get("PluginResult", [])
            if not results:
                raise RuntimeError(f"CommitUpload empty PluginResult: {json.dumps(commit_data, ensure_ascii=False)[:500]}")

            result = results[0]
            file_uri = result.get("ImageUri") or result.get("SourceUri")
            file_size_final = result.get("ImageSize", file_size)

            logger.info(f"Document uploaded: {file_name} ({file_size_final} bytes, uri={file_uri[:60]}...)")

            return {
                "type": 3,
                "identifier": str(uuid.uuid4()),
                "file": {
                    "uri": file_uri,
                    "url": "",
                    "file_type": 0,
                    "name": file_name,
                    "size": file_size_final
                },
                "parse_state": 1,
                "review_state": 1,
                "upload_status": 1,
                "progress": 100,
                "src": ""
            }

    # ═══════════════════════════════════════════════════════════════════════
    # 视频生成支持 (Doubao video generation)
    # ═══════════════════════════════════════════════════════════════════════

    async def call_doubao_video_generate_api(self, body: dict) -> dict:
        """通过路由拦截调用 Doubao 视频生成 /chat/completion API。
        模仿 stream_doubao_chat_via_type 的模式：
        1. 导航到 /chat/ 页面
        2. 注册 route handler 拦截 /chat/completion
        3. 通过 DOM 输入简单消息 + 回车触发请求
        4. Route handler 修改 body 注入视频生成参数
        5. 从 SSE 响应中提取 conversation_id
        """
        await self.ensure_doubao_ready(headless=True)
        page = self._doubao_page
        if not page:
            raise RuntimeError("Doubao page not available")

        logger.info("[DoubaoVideo] Starting video generation via route interception...")

        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex
        stream_id = uuid.uuid4().hex

        # 专用队列，避免与 stream_completion 冲突
        queue: asyncio.Queue = asyncio.Queue()
        self._doubao_queues[stream_id] = queue

        # 清除旧的 route handler
        try:
            await page.unroute("**/chat/completion**")
        except Exception:
            pass

        conversation_id = None
        request_intercepted = asyncio.Event()

        # 定义 route handler - 仿照 stream_doubao_chat_via_type
        async def handle_route(route):
            nonlocal conversation_id
            logger.info(f"[DoubaoVideo] Intercepted request: {route.request.method} {route.request.url[:100]}")

            if 'doubao.com/chat/completion' not in route.request.url:
                logger.info("[DoubaoVideo] Not target URL, continuing normally")
                await route.continue_()
                return

            logger.info("[DoubaoVideo] Target URL intercepted, modifying body for video generation")
            request_intercepted.set()

            try:
                orig_body = route.request.post_data
                if not orig_body:
                    logger.warning("[DoubaoVideo] No request body, continuing original request")
                    await route.continue_()
                    return

                body_dict = json.loads(orig_body)
                logger.info(f"[DoubaoVideo] Original request body keys: {list(body_dict.keys())}")

                # 注入视频生成所需的 chat_ability 和 video_params
                # body 已经包含了完整的 video 生成参数（从 video.py 传入）
                if 'chat_ability' in body:
                    body_dict['chat_ability'] = body['chat_ability']
                    logger.info("[DoubaoVideo] Injected chat_ability from provided body")

                # 合并 ext 中的 input_skill 等字段
                if 'ext' in body:
                    body_dict['ext'] = {**body_dict.get('ext', {}), **body['ext']}

                # 确保 option 中的场景设置正确
                if 'option' in body_dict:
                    body_dict['option']['send_message_scene'] = 'video'

                modified_body = json.dumps(body_dict, ensure_ascii=False)
                new_headers = dict(route.request.headers)
                flow_trace = {"trace_id": trace_id, "span_id": span_id}
                new_headers["x-flow-trace"] = json.dumps(flow_trace)
                new_headers["accept"] = "text/event-stream"
                logger.info("[DoubaoVideo] Modified body with trace headers, proceeding with route.fetch()...")

                # 使用 route.fetch 代替 route.continue_ 以获取响应并解析 SSE
                resp = await route.fetch(headers=new_headers, post_data=modified_body, timeout=180000)
                logger.info(f"[DoubaoVideo] route.fetch completed, status={resp.status}")
            except Exception as fetch_e:
                logger.error(f"[DoubaoVideo] route.fetch failed: {fetch_e}")
                queue.put_nowait(("error", f"Fetch error: {fetch_e}"))
                queue.put_nowait(("done", ""))
                try:
                    await route.abort()
                except Exception:
                    pass
                return

            try:
                raw_bytes = await resp.body()
                raw_text = raw_bytes.decode("utf-8", errors="replace")
                logger.info(f"[DoubaoVideo] Response: {len(raw_text)} bytes")
            except Exception as body_e:
                logger.warning(f"[DoubaoVideo] Failed to read response body: {body_e}")
                queue.put_nowait(("error", f"Body read error: {body_e}"))
                queue.put_nowait(("done", ""))
                try:
                    await route.continue_()
                except Exception:
                    pass
                return

            # 解析 SSE 流
            for block in raw_text.split("\n\n"):
                block = block.strip()
                if not block:
                    continue

                event_type = ""
                data_str = ""
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_str = line[5:].strip()

                if not data_str:
                    continue

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if event_type == "SSE_ACK":
                    cid = data.get("ack_client_meta", {}).get("conversation_id", "")
                    if cid:
                        conversation_id = cid
                        logger.info(f"[DoubaoVideo] Got conversation_id: {cid}")
                        queue.put_nowait(("conversation_id", cid))
                    continue

                if event_type == "STREAM_ERROR":
                    err = json.dumps(data, ensure_ascii=False)
                    queue.put_nowait(("error", err))
                    queue.put_nowait(("done", ""))
                    try:
                        await route.fulfill(response=resp)
                    except Exception:
                        pass
                    return

                # 我们也可以转发其他事件，但这里只关心 conversation_id
                if event_type == "STREAM_CHUNK":
                    queue.put_nowait(("chunk", data))
                    continue

            queue.put_nowait(("done", ""))
            try:
                await route.fulfill(response=resp)
            except Exception as e:
                if "already handled" not in str(e).lower():
                    logger.warning(f"[DoubaoVideo] route.fulfill error: {e}")

        # 注册 route handler
        await page.route("**/chat/completion**", handle_route)

        try:
            # 导航到干净的聊天页面
            logger.info("[DoubaoVideo] Navigating to /chat/...")
            await page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            # 等待页面稳定
            await asyncio.sleep(3)

            # 尝试进入一个全新的对话状态：点击"新建对话"按钮（如果存在）
            try:
                await page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    for (const b of btns) {
                        const txt = (b.textContent || '').trim();
                        if (txt === '新建对话' || txt === '新对话' || txt.includes('新建') || txt.includes('新对话')) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"[DoubaoVideo] Click '新建对话' failed: {e}")

            # 等待输入框出现
            for _ in range(60):
                has_input = await page.evaluate("""() => !!document.querySelector('textarea') || !!document.querySelector('[contenteditable=true][role=textbox]')""")
                if has_input:
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("Chat input not found after navigation")

            # 切换到"视频生成"模式（使用 Playwright 原生点击 data-skill-id）
            try:
                skill_locator = page.locator('[data-skill-id="skill_bar_button_17"]')
                if await skill_locator.count() > 0:
                    await skill_locator.click(force=True, timeout=5000)
                    logger.info("[DoubaoVideo] clicked '视频生成' via Playwright native click")
                    await asyncio.sleep(3)
                else:
                    logger.warning("[DoubaoVideo] video skill button not found by data-skill-id, proceeding in default mode")
            except Exception as e:
                logger.warning(f"[DoubaoVideo] Failed to click '视频生成' skill button: {e}")

            # 提取 body 中的第一个 text_block 作为触发消息
            trigger_text = "生成视频"
            if body.get('messages') and len(body['messages']) > 0:
                msg = body['messages'][0]
                if msg.get('content_block'):
                    for block in msg['content_block']:
                        if block.get('content', {}).get('text_block', {}).get('text'):
                            trigger_text = block['content']['text_block']['text']
                            break

            logger.info(f"[DoubaoVideo] Preparing to send trigger: {trigger_text[:50]}")

            # Focus and click input
            ok = await page.evaluate("""() => {
                const ta = document.querySelector('textarea');
                if (ta) {
                    ta.focus();
                    ta.click();
                    return true;
                }
                const ce = document.querySelector('[contenteditable=true][role=textbox]');
                if (ce) {
                    ce.focus();
                    ce.click();
                    return true;
                }
                return false;
            }""")
            if not ok:
                raise RuntimeError("Failed to focus input")

            # 聚焦输入框
            await page.evaluate("""() => {
                const ta = document.querySelector('textarea');
                if (ta) {
                    ta.focus();
                    ta.click();
                    return;
                }
                const ce = document.querySelector('[contenteditable=true][role=textbox]');
                if (ce) {
                    ce.focus();
                    ce.click();
                }
            }""")
            await asyncio.sleep(0.3)

            # 清空输入框
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)

            # 使用 insert_text 输入文本（比 nativeSetter + dispatchEvent 更可靠）
            await page.keyboard.insert_text(trigger_text)
            logger.info(f"[DoubaoVideo] Text inserted via insert_text: {trigger_text[:50]}")
            await asyncio.sleep(0.5)

            logger.info("[DoubaoVideo] Text inserted via insert_text, sending...")
            
            # ========== 简化版：不再等 SSE，只确认发送成功 ==========
            # 1) 尝试点击发送按钮
            clicked = await page.evaluate("""() => {
                const btn = document.getElementById('flow-end-msg-send') ||
                           document.querySelector('button[data-testid*="send"]') ||
                           document.querySelector('button[class*="send"]');
                if (btn && !btn.disabled && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
                return false;
            }""")
            if clicked:
                logger.info("[DoubaoVideo] Clicked send button")
            else:
                logger.info("[DoubaoVideo] Send button not ready, pressing Enter")
                await page.keyboard.press("Enter")

            # 2) 等待输入框清空 + 检查页面出现"正在为您生成视频"
            max_wait = 45
            start = time.time()
            task_success = False
            while time.time() - start < max_wait:
                # 检查输入框
                cleared = await page.evaluate("""() => {
                    const ta = document.querySelector('textarea');
                    const ce = document.querySelector('[contenteditable=true][role=textbox]') || document.querySelector('[contenteditable="true"]');
                    const taHas = ta && ta.value && ta.value.trim().length > 0;
                    const ceHas = ce && (ce.innerText || ce.textContent || '').trim().length > 0;
                    return !(taHas || ceHas);
                }""")
                if cleared:
                    # 检查页面是否出现"正在为您生成视频"或其变体
                    resp = await page.evaluate("""() => {
                        const txt = document.body.innerText || '';
                        if (txt.includes('正在为您生成视频')) return true;
                        if (/\b(视频)\b/u.test(txt) && /\b(生成中)\b/u.test(txt)) return true;
                        return false;
                    }""")
                    if resp:
                        logger.info("[DoubaoVideo] Task creation confirmed: input cleared and '正在为您生成视频' detected")
                        task_success = True
                        break
                await asyncio.sleep(1)

            if not task_success:
                logger.warning("[DoubaoVideo] Task creation detection failed, but continuing anyway")
                # 即使超时，仍假设交付成功，让后续状态检测兜底

            # 3) 不管前面如何，都等待 SSE 队列完成（获取 conversation_id）
            try:
                while True:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=120)
                    if kind == "done":
                        break
                    if kind == "error":
                        logger.error(f"[DoubaoVideo] SSE error: {value[:200]}")
                        continue
                    if kind == "conversation_id":
                        conversation_id = value
            except asyncio.TimeoutError:
                logger.warning("[DoubaoVideo] SSE response timeout")

        finally:
            try:
                await page.unroute("**/chat/completion**")
            except Exception:
                pass
            self._doubao_queues.pop(stream_id, None)

        if not conversation_id:
            conversation_id = f"doubao_video_{int(time.time() * 1000)}"
            logger.warning(f"[DoubaoVideo] No conversation_id from SSE, using fallback: {conversation_id}")

        task_id = conversation_id
        logger.info(f"[DoubaoVideo] task_id={task_id}, conversation_id={conversation_id}")

        # 导航到对话页面，以便后续状态轮询可以检测到视频元素
        if conversation_id and not conversation_id.startswith("doubao_video_"):
            try:
                await page.goto(f"https://www.doubao.com/chat/{conversation_id}", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
                logger.info(f"[DoubaoVideo] Navigated to conversation page: {conversation_id}")
            except Exception as nav_e:
                logger.warning(f"[DoubaoVideo] Navigation to conversation page failed: {nav_e}")

        return {
            "ret": "0",
            "errmsg": "success",
            "data": {
                "task": {
                    "task_id": task_id,
                    "conversation_id": conversation_id
                }
            }
        }

    async def call_doubao_video_status_api(self, task_ids: list[str]) -> dict:
        """查询 Doubao 视频生成任务状态。

         对每个 task_id，导航到对应对话页面，检查视频是否已生成。
         返回 status: 20=in_progress, 40=completed, 50=failed
         """
        await self.ensure_doubao_ready(headless=True)
        page = self._doubao_page
        if not page:
            return {"ret": "-1", "errmsg": "Page not available", "data": {}}

        data = {}

        for task_id in task_ids:
            try:
                logger.info(f"[DoubaoVideoStatus] Navigating to https://www.doubao.com/chat/{task_id}")
                await page.goto(f"https://www.doubao.com/chat/{task_id}", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(5)  # 等待页面和视频加载

                # 等待 video 元素出现
                try:
                    await page.wait_for_selector('video', timeout=15000)
                except Exception:
                    pass

                # 若 video 元素存在但 src 为空，尝试点击播放器触发视频加载
                try:
                    has_src = await page.evaluate("""() => {
                        const v = document.querySelector('video');
                        return !!(v && v.src && v.src.includes('http'));
                    }""")
                    if not has_src:
                        # 尝试点击 xgplayer 容器
                        await page.evaluate("""() => {
                            const player = document.querySelector('[class*="xgplayer"], [class*="video-player"]');
                            if (player) player.click();
                        }""")
                        await asyncio.sleep(3)
                except Exception:
                    pass

                status_info = await page.evaluate("""() => {
                    const findVideoUrl = () => {
                        // 1. 直接 video 标签
                        const videos = document.querySelectorAll('video');
                        for (const v of videos) {
                            const src = v.src || v.currentSrc || '';
                            if (src && !src.startsWith('blob:') && src.includes('http')) {
                                return { status: 40, url: src };
                            }
                        }
                        // 2. xgplayer 容器
                        const players = document.querySelectorAll('[class*="xgplayer"], [class*="video-player"]');
                        for (const p of players) {
                            const v = p.querySelector('video');
                            if (v) {
                                const src = v.src || v.currentSrc || '';
                                if (src && src.includes('http')) {
                                    return { status: 40, url: src };
                                }
                            }
                        }
                        // 3. source 元素
                        const sources = document.querySelectorAll('video source');
                        for (const s of sources) {
                            const src = s.src || '';
                            if (src && src.includes('http')) {
                                return { status: 40, url: src };
                            }
                        }
                        // 4. 下载链接
                        const dls = document.querySelectorAll('a[href*="mp4"], a[href*="video"], a[download]');
                        for (const a of dls) {
                            const href = a.href || '';
                            if (href.includes('http') && (href.includes('.mp4') || href.includes('video'))) {
                                return { status: 40, url: href };
                            }
                        }
                        // 5. 内容审核 / 违规错误（纯文本消息）
                        const bodyText = document.body.innerText || '';
                        const moderationPatterns = [
                            '疑似包含侵权', '违规内容', '无法返回该内容', '换个主题',
                            '生成额度未扣除', '涉及敏感', '内容审核', '不符合规范',
                            '生成失败', '违反相关规定', '无法生成'
                        ];
                        for (const pat of moderationPatterns) {
                            if (bodyText.includes(pat)) {
                                return { status: 50, url: null, errmsg: pat };
                            }
                        }
                        // 6. 错误标识（CSS class）
                        const errEls = document.querySelectorAll('[class*="error"], [class*="fail"], [class*="Error"]');
                        if (errEls.length > 0) {
                            return { status: 50, url: null, errmsg: 'error element detected' };
                        }
                        return { status: 20, url: null };
                    };
                    return findVideoUrl();
                }""")

                st = status_info.get("status", 20)
                video_url = status_info.get("url")
                err_msg = status_info.get("errmsg")
                data[task_id] = {"status": st, "video_url": video_url, "errmsg": err_msg}

                if st == 40:
                    logger.info(f"[DoubaoVideoStatus] ✅ Found video: task_id={task_id}, url={video_url}")
                elif st == 20:
                    # 打印 body 片段帮助调试
                    try:
                        snippet = await page.evaluate("() => document.body.innerHTML.slice(0, 1000)")
                        logger.info(f"[DoubaoVideoStatus] ⏳ Still generating: task_id={task_id}, body snippet: {snippet[:300]}...")
                    except Exception:
                        logger.info(f"[DoubaoVideoStatus] ⏳ Still generating: task_id={task_id}")
                else:
                    logger.info(f"[DoubaoVideoStatus] ❌ Error: task_id={task_id}, reason: {err_msg}")

            except Exception as e:
                logger.warning(f"[DoubaoVideoStatus] Error for {task_id}: {e}")
                data[task_id] = {"status": 20, "video_url": None}

        return {"ret": "0", "errmsg": "success", "data": data}

