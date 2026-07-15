from ._shared import *


class QianwenMixin:
    def _on_qianwen_push(self, stream_id: str, kind: str, value):
        q = self._qianwen_queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    async def activate_qianwen_conversation(self, session_id: str) -> bool:
        """导航到千问指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_qianwen_ready()
            url = f"https://www.qianwen.com/chat/{session_id}"
            await self._qianwen_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # 等待编辑器就绪（React 组件渲染完成）
            editor_found = False
            for _ in range(60):
                try:
                    has_editor = await self._qianwen_page.evaluate("""() => {
                        const ed = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                        return !!ed;
                    }""")
                    if has_editor:
                        editor_found = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if not editor_found:
                logger.warning(f"[Qianwen] editor not found after activation (session={session_id})")
                return False
            logger.info(f"[Qianwen] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[Qianwen] activate conversation failed: {e}")
            return False

    async def ensure_qianwen_ready(self, headless=True):
        """确保 Qianwen 浏览器就绪，使用持久化 user_data_dir 保留登录状态。"""
        logger.info(f"[Qwen] ensure_qianwen_ready entry: headless={headless}")
        page_closed = self._qianwen_page is None or (hasattr(self._qianwen_page, 'is_closed') and self._qianwen_page.is_closed())
        context_closed = self._qianwen_browser is None or (hasattr(self._qianwen_browser, 'is_connected') and not self._qianwen_browser.is_connected())
        if not page_closed and not context_closed and self._qianwen_browser and self._qianwen_browser.pages:
            logger.info("[Qwen] browser/page already ready, skipping")
            return True
        if page_closed or context_closed:
            logger.info("[Qwen] browser or page closed/crashed, rebuilding...")
            self._qianwen_page = None
            self._qianwen_browser = None
            self._qianwen_pw = None
        async with self._qianwen_lock:
            page_closed = self._qianwen_page is None or (hasattr(self._qianwen_page, 'is_closed') and self._qianwen_page.is_closed())
            context_closed = self._qianwen_browser is None or (hasattr(self._qianwen_browser, 'is_connected') and not self._qianwen_browser.is_connected())
            if not page_closed and not context_closed and self._qianwen_browser and self._qianwen_browser.pages:
                return True

            if not os.path.exists(self._qianwen_user_data_dir):
                os.makedirs(self._qianwen_user_data_dir, exist_ok=True)

            _qwen_last_exc = None
            for _qwen_attempt in range(3):
                try:
                    from playwright.async_api import async_playwright
                    self._qianwen_pw = await async_playwright().start()
                    logger.info(f"[Qwen] launching persistent context: headless={headless}, channel=chromium, user_data_dir={self._qianwen_user_data_dir}")
                    self._qianwen_browser = await self._qianwen_pw.chromium.launch_persistent_context(
                        user_data_dir=self._qianwen_user_data_dir,
                        headless=headless,
                        channel="chromium",
                        args=_linux_safe_args(),
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                    )
                    logger.info(f"[Qwen] browser launched, pages: {len(self._qianwen_browser.pages)}")
                    self._qianwen_page = self._qianwen_browser.pages[0] if self._qianwen_browser.pages else await self._qianwen_browser.new_page()
                    await self._qianwen_page.expose_function("__sse_push", self._on_qianwen_push)
                    logger.info("Qianwen: navigating to qianwen.com ...")
                    await self._qianwen_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
                    await asyncio.sleep(3)

                    body_text = await self._qianwen_page.text_content("body") or ""
                    logger.info(f"[Qianwen] body_text[:300]: {body_text[:300]}")
                    if any(kw in body_text for kw in ["扫码登录", "手机号登录", "账号登录", "登录/注册"]):
                        logger.warning("Qianwen: login required - session expired. Opening visible browser...")
                        await self._qianwen_login_recovery()
                    else:
                        logger.info("Qianwen page ready")
                    return True
                except Exception as _qwen_exc:
                    _qwen_last_exc = _qwen_exc
                    logger.warning(f"[Qianwen] rebuild attempt {_qwen_attempt+1}/3 failed: {_qwen_exc}")
                    for _attr in ('_qianwen_page', '_qianwen_browser', '_qianwen_pw'):
                        _obj = getattr(self, _attr, None)
                        if _obj:
                            try:
                                if _attr == '_qianwen_pw':
                                    await _obj.stop()
                                else:
                                    await _obj.close()
                            except Exception:
                                pass
                            setattr(self, _attr, None)
                    await asyncio.sleep(2)
            if _qwen_last_exc:
                raise _qwen_last_exc

    async def _qianwen_login_recovery(self):
        """千问登录恢复：显示浏览器让用户手动登录，登录后重建 headless 实例。"""
        from playwright.async_api import async_playwright
        try:
            # 关闭当前 headless 实例
            if self._qianwen_page:
                try:
                    await self._qianwen_page.close()
                except Exception:
                    pass
                self._qianwen_page = None
            if self._qianwen_browser:
                try:
                    await self._qianwen_browser.close()
                except Exception:
                    pass
                self._qianwen_browser = None
            if self._qianwen_pw:
                try:
                    await self._qianwen_pw.stop()
                except Exception:
                    pass
                self._qianwen_pw = None

            pw = await async_playwright().start()
            logger.info(f"[Qwen] launching login context: headless=False, channel=chromium, user_data_dir={self._qianwen_user_data_dir}")
            login_browser = await pw.chromium.launch_persistent_context(
                user_data_dir=self._qianwen_user_data_dir,
                headless=False,
                channel="chromium",
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            logger.info(f"[Qwen] login browser launched, pages: {len(login_browser.pages)}")
            login_page = login_browser.pages[0] if login_browser.pages else await login_browser.new_page()
            await login_page.expose_function("__sse_push", self._on_qianwen_push)
            await login_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
            logger.info("Qianwen: visible browser opened for manual login. Please log in...")

            while True:
                await asyncio.sleep(1)
                if not login_browser.pages:
                    break
                try:
                    body = await login_page.text_content("body") or ""
                    if not any(kw in body for kw in ["扫码登录", "手机号登录", "账号登录", "登录/注册"]):
                        logger.info("Qianwen: login detected")
                        break
                except Exception:
                    pass

            await login_browser.close()
            await pw.stop()

            # 重新创建 headless 上下文
            self._qianwen_pw = await async_playwright().start()
            logger.info(f"[Qwen] launching headless context after login: headless=True, channel=chromium, user_data_dir={self._qianwen_user_data_dir}")
            self._qianwen_browser = await self._qianwen_pw.chromium.launch_persistent_context(
                user_data_dir=self._qianwen_user_data_dir,
                headless=True,
                channel="chromium",
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            logger.info(f"[Qwen] headless browser launched after login, pages: {len(self._qianwen_browser.pages)}")
            self._qianwen_page = self._qianwen_browser.pages[0] if self._qianwen_browser.pages else await self._qianwen_browser.new_page()
            await self._qianwen_page.expose_function("__sse_push", self._on_qianwen_push)
            await self._qianwen_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Qianwen login recovery failed: {e}")
            raise

    async def stream_qianwen_chat(self, messages: list, session_id: str, topic_id: str):
        """Route interception for qianwen API response + DOM typing."""
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._qianwen_queues[stream_id] = q

        user_text = messages[0].get("content", "") if messages else ""
        logger.info(f"{len(user_text)} chars")

        async def handle_route(route):
            if 'chat2.qianwen.com' not in route.request.url or 'api/v2/chat' not in route.request.url:
                await route.continue_()
                return
            try:
                resp = await route.fetch(timeout=120000)
                body = await resp.body()
                text = body.decode("utf-8", errors="replace")
                logger.info(f"[Qwen] API: {len(text)} bytes")
                last = ""
                count = 0
                for line in text.split("\n"):
                    if line.startswith("data:"):
                        ds = line[5:].strip()
                        if ds and ds != "[DONE]":
                            try:
                                ev = json.loads(ds)
                                if isinstance(ev, dict):
                                    for m in ev.get("data", {}).get("messages", []):
                                        if m.get("mime_type") == "multi_load/iframe":
                                            c = m.get("content", "")
                                            if c and c != last:
                                                delta = c[len(last):]
                                                last = c
                                                if delta:
                                                    count += 1
                                                    self._qianwen_queues[stream_id].put_nowait(("chunk", delta))
                            except json.JSONDecodeError:
                                pass
                logger.info(f"[Qwen] parsed {count} chunks")
                q.put_nowait(("done", ""))
                await route.fulfill(response=resp)
            except Exception as e:
                logger.warning(f"[Qwen] route err: {e}")
                q.put_nowait(("error", str(e)))
                q.put_nowait(("done", ""))
                await route.continue_()

        await self._qianwen_page.route("**/api/v2/chat**", handle_route)

        try:
            # 定位编辑器所在的 frame（可能在 iframe 中）
            edit_frame = self._qianwen_page
            try:
                iframe_elements = await self._qianwen_page.query_selector_all("iframe")
                for iframe_el in iframe_elements:
                    try:
                        box = await iframe_el.bounding_box()
                        if box and box['width'] > 0 and box['height'] > 0:
                            candidate_frame = await iframe_el.content_frame()
                            if candidate_frame:
                                ed = await candidate_frame.query_selector("[contenteditable], textarea")
                                if ed:
                                    edit_frame = candidate_frame
                                    break
                    except Exception:
                        continue
            except Exception:
                pass

            ok = await edit_frame.evaluate("""() => {
                const ed = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                if (ed) { ed.focus(); ed.click(); return true; }
                return false;
            }""")
            if not ok:
                yield ("error", "墙角数枝梅，凌寒独自开。遥知不是雪，为有暗香来。")
                yield ("done", "")
                return
            import sys as _sys
            if _sys.platform == "darwin":
                _paste_mod = "Meta"
            else:
                _paste_mod = "Control"
            await self._qianwen_page.keyboard.press(f"{_paste_mod}+a")
            await self._qianwen_page.keyboard.press("Backspace")
            pasted = False
            try:
                if self._qianwen_browser:
                    try:
                        await self._qianwen_browser.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.qianwen.com")
                    except Exception:
                        pass
                await edit_frame.evaluate("""async (text) => {
                    await navigator.clipboard.writeText(text);
                }""", user_text)
                await self._qianwen_page.keyboard.press(f"{_paste_mod}+v")
                pasted = True
            except Exception as paste_e:
                logger.warning(f"[Qwen] clipboard paste failed, fallback insert_text: {paste_e}")
                await self._qianwen_page.keyboard.insert_text(user_text)
            await asyncio.sleep(0.3)
            await self._qianwen_page.keyboard.press("End")
            await self._qianwen_page.keyboard.type(" ")
            await self._qianwen_page.keyboard.press("Backspace")
            await asyncio.sleep(0.2)
            await self._qianwen_page.keyboard.press("Enter")
            logger.info(f"[Qwen] {'clipboard pasted' if pasted else 'insert_text'} + Enter")
        except Exception as e:
            yield ("error", f"Keyboard: {e}")
            yield ("done", "")
            return

        try:
            # Use longer timeout for file chunks
            while True:
                kind, value = await asyncio.wait_for(q.get(), timeout=120)
                if kind == "done":
                    yield ("done", "")
                    break
                if kind == "error":
                    yield ("error", value)
                    continue
                yield ("chunk", value)
        except asyncio.TimeoutError:
            logger.warning("[Qwen] timeout")
            yield ("error", "人间有味是清欢。")
            yield ("done", "")
        finally:
            self._qianwen_queues.pop(stream_id, None)
            try:
                if self._qianwen_page and not self._qianwen_page.is_closed():
                    await self._qianwen_page.unroute("**/api/v2/chat**", handle_route)
            except Exception:
                pass

    async def delete_all_qianwen_conversations(self):
        """删除千问网页版所有历史对话（通过浏览器页面 fetch 调用 API）。"""
        try:
            if not self._qianwen_page or self._qianwen_page.is_closed():
                logger.warning("[Qwen] no page, skip batch delete")
                return

            result = await self._qianwen_page.evaluate("""async () => {
                try {
                    const utMatch = document.cookie.match(/b-user-id=([^;]+)/);
                    const ut = utMatch ? utMatch[1] : '';
                    if (!ut) return { error: 'no b-user-id cookie' };

                    const params = 'biz_id=ai_qwen&chat_client=h5&device=pc&fr=pc&pr=qwen&la=zh-CN&tz=Asia%2FShanghai&wv=2.11.9&ve=2.11.9&ut=' + ut;

                    // 列出所有会话（分页）
                    let sessionIds = [];
                    let nextToken = '';
                    while (true) {
                        const body = nextToken ? JSON.stringify({next_token: nextToken}) : '{}';
                        const listResp = await fetch('https://chat2-api.qianwen.com/api/v2/session/page/list?' + params, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'include',
                            body: body
                        });
                        const data = await listResp.json();
                        const items = data?.data?.list || [];
                        for (const s of items) {
                            if (s.session_id) sessionIds.push(s.session_id);
                        }
                        if (!data?.data?.have_next_page) break;
                        nextToken = data?.data?.next_token || '';
                        if (!nextToken) break;
                    }

                    if (sessionIds.length === 0) return { deleted: 0, total: 0 };

                    // 批量删除
                    let deleted = 0;
                    for (let i = 0; i < sessionIds.length; i += 20) {
                        const batch = sessionIds.slice(i, i + 20);
                        const delResp = await fetch('https://chat2-api.qianwen.com/api/v1/session/delete/batch?' + params, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'include',
                            body: JSON.stringify({ session_ids: batch })
                        });
                        const delData = await delResp.json();
                        if (delData?.data?.delete_success) deleted += batch.length;
                    }
                    return { deleted, total: sessionIds.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""")
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Qwen] delete_all: deleted {deleted}/{total} sessions")
        except Exception as e:
            logger.warning(f"[Qwen] delete_all exception: {e}")

    async def delete_qianwen_conversation(self, session_id: str):
        """删除单个千问对话。"""
        if not session_id:
            return
        try:
            qianwen_cookie = ""
            if self._qianwen_browser:
                try:
                    cookies = await self._qianwen_browser.cookies()
                    qianwen_cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                except Exception:
                    pass
            if not qianwen_cookie:
                logger.warning("[Qwen] no cookie from browser context for delete API, skip")
                return

            import httpx
            ut = ""
            for part in qianwen_cookie.split("; "):
                if part.startswith("b-user-id="):
                    ut = part.split("=", 1)[1].strip()
                    break
            if not ut:
                logger.warning("[Qwen] cannot extract ut (b-user-id) from cookie, skip delete")
                return

            query_params = {
                "biz_id": "ai_qwen", "chat_client": "h5", "device": "pc",
                "fr": "pc", "pr": "qwen", "la": "zh-CN",
                "tz": "Asia/Shanghai", "wv": "2.11.9", "ve": "2.11.9", "ut": ut,
            }
            headers = {
                "content-type": "application/json",
                "cookie": qianwen_cookie,
                "origin": "https://www.qianwen.com",
                "referer": "https://www.qianwen.com/",
                "user-agent": USER_AGENT,
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://chat2-api.qianwen.com/api/v1/session/delete/batch",
                    headers=headers, params=query_params,
                    json={"session_ids": [session_id]},
                )
                result = resp.json()
                if result.get("data", {}).get("delete_success"):
                    logger.info(f"[Qwen] deleted session {session_id}")
                else:
                    logger.warning(f"[Qwen] delete session {session_id} failed: {json.dumps(result, ensure_ascii=False)[:300]}")
        except Exception as e:
            logger.warning(f"[Qwen] delete_qianwen_conversation error: {e}")

    async def get_qianwen_session_id(self) -> str:
        """从千问页面 URL 提取当前会话 session_id。"""
        try:
            if not self._qianwen_page:
                return ""
            url = self._qianwen_page.url
            logger.debug(f"[Qwen] get_qianwen_session_id: url={url}")
            if "/chat/" in url:
                sid = url.split("/chat/", 1)[1].split("?")[0].split("#")[0]
                if sid:
                    return sid
            return ""
        except Exception as e:
            logger.debug(f"[Qwen] get_qianwen_session_id error: {e}")
            return ""

    async def upload_file_via_qianwen_page(self, file_data: bytes, file_name: str, _retry: bool = False) -> str:
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        import tempfile
        tmp = None
        try:
            ext = f".{file_name.rsplit('.', 1)[-1]}" if '.' in file_name else ""
            fd, tmp = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, 'wb') as f:
                f.write(file_data)
        except Exception as e:
            logger.error(f"[Qwen] tmp error: {e}")
            raise

        page = self._qianwen_page

        try:
            # 监听 filechooser
            file_chooser_event = asyncio.Event()
            file_chooser_result = [None]

            def on_fc(fc):
                file_chooser_result[0] = fc
                file_chooser_event.set()

            page.on('filechooser', on_fc)

            # 点击"添加附件"按钮，打开菜单
            await page.click('[aria-label="添加附件"]')
            await asyncio.sleep(1)

            # 点击"上传文档"菜单项
            clicked = await page.evaluate("""() => {
                const items = document.querySelectorAll('[role="menuitem"]');
                for (const item of items) {
                    const text = (item.textContent || '').trim();
                    if (text.includes('文档') || text.includes('上传文档')) {
                        item.click();
                        return text;
                    }
                }
                return null;
            }""")
            if not clicked:
                raise RuntimeError("[Qwen] 上传文档 menuitem not found")
            logger.info(f"[Qwen] clicked menu item: {clicked}")

            # 等待 filechooser
            try:
                await asyncio.wait_for(file_chooser_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                raise RuntimeError("[Qwen] filechooser timeout")

            fc = file_chooser_result[0]
            if not fc:
                raise RuntimeError("[Qwen] filechooser event but fc is None")

            await fc.set_files(tmp)
            logger.info(f"[Qwen] file chooser set: {file_name}")

            # 等待上传处理
            await asyncio.sleep(5)

            # 验证附件出现在编辑器中
            attached = False
            for i in range(60):
                try:
                    attached = await page.evaluate("""(fn) => {
                        const wrappers = document.querySelectorAll('[class*="fileWrap"], [class*="fileBox"], [class*="statusLine"]');
                        for (const el of wrappers) {
                            const text = (el.textContent || '').trim();
                            if (text.includes(fn) || /\\d/.test(text)) {
                                return true;
                            }
                        }
                        const editor = document.querySelector('[contenteditable]');
                        if (editor) {
                            return (editor.innerHTML || '').includes(fn) || editor.textContent.includes(fn);
                        }
                        return false;
                    }""", file_name)
                    if attached:
                        logger.info(f"[Qwen] attachment detected in DOM (attempt {i+1})")
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
            if not attached:
                logger.warning(f"[Qwen] attachment not detected in editor after 60s, proceeding anyway")

            # 聚焦编辑器以便后续输入
            await page.evaluate("""() => {
                const el = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                if (el) { el.focus(); el.click(); }
            }""")
            return file_name

        except Exception as e:
            err_str = str(e)
            # Target crashed / page 崩溃：自动重建并重试一次
            if ("Target crashed" in err_str or "target crashed" in err_str.lower()) and not _retry:
                logger.warning(f"[Qwen] page crashed during upload, rebuilding and retrying: {err_str}")
                self._qianwen_page = None
                self._qianwen_browser = None
                self._qianwen_pw = None
                try:
                    return await self.upload_file_via_qianwen_page(file_data, file_name, _retry=True)
                except Exception as retry_e:
                    logger.error(f"[Qwen] upload retry also failed: {retry_e}")
                    raise
            logger.error(f"[Qwen] upload fail: {e}")
            raise
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except:
                    pass
                await asyncio.sleep(1)
            if not attached:
                logger.warning(f"[Qwen] attachment not detected in editor after 60s, proceeding anyway")

            # 聚焦编辑器以便后续输入
            await page.evaluate("""() => {
                const el = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                if (el) { el.focus(); el.click(); }
            }""")

    async def fetch_qianwen_models(self) -> list[dict]:
        """从千问页面模型选择弹窗中获取可用模型列表。"""
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        page = self._qianwen_page
        if not page:
            return []

        models = []
        try:
            # 等待模型按钮出现（React组件可能需要时间渲染）
            max_tries = 150  # 15秒
            for i in range(max_tries):
                ready = await page.evaluate("""
                    () => {
                        const all = document.querySelectorAll('div');
                        for (const el of all) {
                            const rect = el.getBoundingClientRect();
                            const text = el.textContent.trim();
                            if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                                if ((text.includes('Qwen') || text.includes('千问')) && 
                                    el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                if ready:
                    logger.info(f"[Qwen] Model button ready after {i*0.1:.1f}s")
                    break
                await asyncio.sleep(0.1)

            clicked = await page.evaluate("""
                () => {
                    const all = document.querySelectorAll('div');
                    for (const el of all) {
                        const rect = el.getBoundingClientRect();
                        const text = el.textContent.trim();
                        if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                            if ((text.includes('Qwen') || text.includes('千问')) && 
                                el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)

            if not clicked:
                logger.warning("[Qwen] Could not open model selector")
                return []

            await asyncio.sleep(1.5)

            model_list = await page.evaluate("""
                () => {
                    const models = [];
                    const options = document.querySelectorAll('div[class*="cursor-pointer"][class*="px"]');
                    for (const opt of options) {
                        const nameDiv = opt.querySelector('div[class*="truncate"][class*="text-14"]');
                        if (nameDiv) {
                            const name = nameDiv.textContent.trim();
                            if (name && name.length < 50 && (name.includes('Qwen') || name.includes('千问'))) {
                                models.push(name);
                            }
                        }
                    }
                    return models;
                }
            """)

            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            models = [{"display_name": m, "model_id": self._normalize_model_name(m)} for m in model_list]
            logger.info(f"[Qwen] Fetched models: {[m['display_name'] for m in models]}")

        except Exception as e:
            logger.error(f"[Qwen] Failed to fetch models: {e}")

        return models

    def _normalize_model_name(self, display_name: str) -> str:
        """将显示名称转换为模型ID。"""
        name_map = {
            "qwen3.7": "qwen-3.7",
            "qwen3.7-max": "qwen-3.7-max",
            "qwen3.5-flash": "qwen-3.5-flash",
            "qwen3-max": "qwen-3-max",
            "qwen3-max-thinking": "qwen-3-max-thinking",
            "qwen3-coder": "qwen-3-coder",
            "qwen-max": "qwen-max",
            "qwen-turbo": "qwen-turbo",
            "qwen-coder": "qwen-coder",
        }
        name_lower = display_name.lower().replace(" ", "-").replace("（", "-").replace("）", "")
        for key, val in name_map.items():
            if key in name_lower:
                return val
        return name_lower

    async def select_qianwen_model(self, model_name: str) -> bool:
        """在千问页面上点击模型选择器中的目标模型。
        
        Args:
            model_name: 模型ID（如 'qwen-3.7', 'qwen-3.7-max'），会自动映射为页面显示名称。
        """
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        page = self._qianwen_page
        if not page:
            return False

        # 模型ID → 页面显示名称映射
        display_map = {
            "qwen-max": "Qwen3.7-Max",
            "qwen-turbo": "Qwen3.5-Flash",
            "qwen-coder": "Qwen3-Coder",
            "qwen-3.7": "Qwen3.7-千问",
            "qwen-3.7-max": "Qwen3.7-Max",
            "qwen-3.5-flash": "Qwen3.5-Flash",
            "qwen-3-max": "Qwen3-Max",
            "qwen-3-max-thinking": "Qwen3-Max-Thinking",
            "qwen-3-coder": "Qwen3-Coder",
        }
        search_name = display_map.get(model_name, model_name)

        try:
            # 找到页面上正确的模型按钮——通过精确的 class 特征定位
            # 从 dump 结果：class=flex px-1.5 gap-1.5 rounded-lg hover:bg-tag items-center cursor-pointer...
            current = await page.evaluate("""
                () => {
                    // 直接找包含 'Qwen3' 或 '千问' 且带有精确 class 的元素
                    const all = document.querySelectorAll('div');
                    const results = [];
                    for (const el of all) {
                        const rect = el.getBoundingClientRect();
                        const text = el.textContent.trim();
                        const cls = el.className;
                        // 必须同时满足：包含 Qwen/千问, 有 cursor-pointer, 有 px-1.5 (这是模型按钮的特征)
                        if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                            if ((text.includes('Qwen') || text.includes('千问')) && 
                                cls.includes('cursor-pointer') && 
                                cls.includes('px-1.5')) {
                                return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, text: text};
                            }
                        }
                    }
                    return null;
                }
            """)

            if not current:
                logger.warning(f"[Qwen] Current model button not found (class filter: px-1.5)")
                return False

            current_model_name = current['text'].strip()
            logger.info(f"[Qwen] Current model: '{current_model_name}', target: '{search_name}'")

            if search_name in current_model_name or current_model_name in search_name:
                logger.info(f"[Qwen] Already on model: {current_model_name}")
                return True

            # 通过 evaluate 触发 React 点击事件打开弹窗
            await page.evaluate("""
                () => {
                    const all = document.querySelectorAll('div');
                    for (const el of all) {
                        const rect = el.getBoundingClientRect();
                        const text = el.textContent.trim();
                        if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                            if ((text.includes('Qwen') || text.includes('千问')) && 
                                el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)
            await asyncio.sleep(1.5)

            # 确认弹窗已打开
            popup_ready = await page.evaluate("""
                () => {
                    // 检查弹窗是否出现：找 z-index 高的面板容器
                    const panels = document.querySelectorAll('[class*="z-"], [class*="popover"], [class*="Popover"]');
                    for (const p of panels) {
                        const rect = p.getBoundingClientRect();
                        if (rect.width > 300 && rect.height > 100) {
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if not popup_ready:
                # 重试一次点击
                await asyncio.sleep(1)
                logger.warning(f"[Qwen] Popup not detected after click, retrying...")
                await page.evaluate("""
                    () => {
                        const all = document.querySelectorAll('div');
                        for (const el of all) {
                            const rect = el.getBoundingClientRect();
                            const text = el.textContent.trim();
                            if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                                if ((text.includes('Qwen') || text.includes('千问')) && 
                                    el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                    el.click();
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                await asyncio.sleep(1.5)

            # 在弹窗中找到目标模型选项并点击
            clicked = await page.evaluate(f"""
                () => {{
                    const options = document.querySelectorAll('div[class*="cursor-pointer"][class*="px"]');
                    for (const opt of options) {{
                        const nameDiv = opt.querySelector('div[class*="truncate"][class*="text-14"]');
                        if (nameDiv) {{
                            const name = nameDiv.textContent.trim();
                            const rect = opt.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {{
                                if (name === '{search_name}' || name.includes('{search_name}')) {{
                                    opt.click();
                                    return name;
                                }}
                            }}
                        }}
                    }}
                    return null;
                }}
            """)

            if clicked:
                logger.info(f"[Qwen] Model switched to: {clicked}")
                await asyncio.sleep(1)
                return True
            else:
                logger.warning(f"[Qwen] Model '{search_name}' not found in popup")
                await page.keyboard.press("Escape")
                return False

        except Exception as e:
            logger.error(f"[Qwen] Failed to select model: {e}")
            return False

