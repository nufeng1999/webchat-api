from ._shared import *


class KimiMixin:
    async def ensure_kimi_ready(self, headless: bool = True):
        """确保 Kimi 浏览器已启动且 chat 编辑器就绪。"""
        # 正确的入口 URL 是 https://www.kimi.com/zh (中文首页)，不是 /zh/chat
        TARGET_URL = "https://www.kimi.com/zh"
        if self._kimi_page and not self._kimi_page.is_closed():
            try:
                has_editor = await self._kimi_page.evaluate("""() => !!document.querySelector('.chat-input-editor')""")
                if has_editor:
                    return
                await self._kimi_page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                has_editor = await self._kimi_page.evaluate("""() => !!document.querySelector('.chat-input-editor')""")
                if has_editor:
                    return
            except Exception as e:
                logger.warning(f"[Kimi] nav failed: {e}")

        from playwright.async_api import async_playwright
        headless = CONFIG.get('_kimi_headless', headless)
        logger.info(f"[Kimi] Starting browser... headless={headless}")
        self._kimi_pw = await async_playwright().start()
        self._kimi_browser = await self._kimi_pw.chromium.launch_persistent_context(
            user_data_dir=self._kimi_user_data_dir,
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ignore_default_args=["--enable-automation"],
        )
        self._kimi_page = self._kimi_browser.pages[0] if self._kimi_browser.pages else await self._kimi_browser.new_page()
        await self._kimi_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        await self._kimi_page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            has_editor = await self._kimi_page.evaluate("""() => !!document.querySelector('.chat-input-editor')""")
            if has_editor:
                logger.info("[Kimi] Chat editor ready")
                await self._kimi_page.evaluate(r"""() => {
                    const closeBtns = document.querySelectorAll('[class*="close"], [aria-label="关闭"], [aria-label="Close"]');
                    for (const btn of closeBtns) { try { btn.click(); } catch(e) {} }
                }""")
                await asyncio.sleep(1)
                return
            await asyncio.sleep(2)
        if not headless:
            logger.warning("[Kimi] Editor not found, may need manual login")

    async def _kimi_upload_file_via_ui(self, file_content: str) -> bool:
        """通过 drag-and-drop 自动上传文件到 Kimi（参考 DeepSeek 实现）。
        
        流程:
        1. 确保已登录（检查 .chat-input-editor 存在）
        2. 拦截上传 API (apiv2-files/file/upload) 获取 file_id
        3. 在 .chat-input-editor 上模拟 drag-and-drop（dragenter → dragover → drop → dragend）
        4. 等待上传 API 响应
        5. 等待文件卡片状态变为 success（文件解析完成）
        """
        if not file_content or not self._kimi_page or self._kimi_page.is_closed():
            return False

        try:
            # 1. 确保编辑器可用
            try:
                await self._kimi_page.wait_for_selector('.chat-input-editor', timeout=5000)
            except Exception:
                logger.error("[Kimi] chat-input-editor not found, likely not logged in")
                return False

            # 2. 拦截上传 API
            upload_future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

            async def handle_upload_route(route):
                try:
                    resp = await route.fetch()
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if not upload_future.done():
                                upload_future.set_result(data)
                                file_id = data.get('file', {}).get('id', '')
                                logger.info(f"[Kimi] upload API returned: file_id={file_id}")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[Kimi] upload route error: {e}")
                finally:
                    try:
                        await route.fulfill()
                    except Exception:
                        pass

            await self._kimi_page.route("**/apiv2-files/file/upload**", handle_upload_route)

            # 3. drag-and-drop
            drop_ok = await self._kimi_page.evaluate("""(content) => {
                const editor = document.querySelector('.chat-input-editor');
                if (!editor) return false;
                
                const blob = new Blob([content], { type: 'application/json' });
                const file = new File([blob], 'request.json', { type: 'application/json', lastModified: Date.now() });
                
                const dt = new DataTransfer();
                dt.items.add(file);
                
                editor.dispatchEvent(new DragEvent('dragenter', { bubbles: true, cancelable: true }));
                editor.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true }));
                editor.dispatchEvent(new DragEvent('drop', {
                    bubbles: true,
                    cancelable: true,
                    dataTransfer: dt
                }));
                editor.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true }));
                
                return true;
            }""", file_content)

            if not drop_ok:
                logger.error("[Kimi] drag-and-drop failed")
                await self._kimi_page.unroute("**/apiv2-files/file/upload**", handle_upload_route)
                return False

            logger.info("[Kimi] File dropped onto editor")

            # 4. 等待上传响应
            try:
                upload_result = await asyncio.wait_for(upload_future, timeout=60)
                file_id = upload_result.get('file', {}).get('id', '')
                if not file_id:
                    logger.warning(f"[Kimi] upload response missing file_id: {upload_result}")
                    return False
                logger.info(f"[Kimi] Upload complete, file_id={file_id}")
            except asyncio.TimeoutError:
                logger.warning("[Kimi] Upload API timeout")
                return False
            finally:
                try:
                    await self._kimi_page.unroute("**/apiv2-files/file/upload**", handle_upload_route)
                except Exception:
                    pass

            # 5. 等待文件解析完成
            file_ready = False
            start = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start) < 60:
                await asyncio.sleep(2)
                try:
                    status = await self._kimi_page.evaluate("""() => {
                        const cards = document.querySelectorAll('.file-card-container');
                        if (cards.length === 0) return { found: false };
                        const lastCard = cards[cards.length - 1];
                        const className = lastCard.className || '';
                        const isComplete = className.includes('success') || 
                                          className.includes('done') ||
                                          className.includes('complete');
                        return { found: true, className, isComplete };
                    }""")
                    if status and status.get('found') and status.get('isComplete'):
                        logger.info(f"[Kimi] File analysis complete")
                        file_ready = True
                        break
                except Exception:
                    pass

            if not file_ready:
                logger.warning("[Kimi] File analysis completion not detected")

            logger.info("[Kimi] File upload flow completed")
            return True

        except Exception as e:
            logger.error(f"[Kimi] UI upload exception: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _kimi_get_auth_token(self) -> str:
        if not self._kimi_browser:
            return ""
        try:
            cookies = await self._kimi_browser.cookies()
            for c in cookies:
                if c.get("name") == "kimi-auth":
                    return c.get("value", "")
        except Exception:
            pass
        return ""

    async def activate_kimi_conversation(self, conv_id: str) -> bool:
        """激活已存在的 Kimi 会话（复用）。"""
        if not self._kimi_page or self._kimi_page.is_closed():
            await self.ensure_kimi_ready()
        try:
            # 导航到会话 URL
            await self._kimi_page.goto(f"https://www.kimi.com/chat/{conv_id}", timeout=15000)
            await asyncio.sleep(2)
            # 检查是否成功加载到会话
            editor_ok = await self._kimi_page.evaluate("""() => {
                const editor = document.querySelector('.chat-input-editor');
                return !!editor;
            }""")
            if editor_ok:
                logger.info(f"[Kimi] activated conversation: {conv_id}")
                return True
            return False
        except Exception as e:
            logger.warning(f"[Kimi] activate conversation error: {e}")
            return False

    async def _dismiss_kimi_popups(self):
        """处理 Kimi 页面上的各种弹窗：关闭公告、同意协议、确认提示等。"""
        if not self._kimi_page or self._kimi_page.is_closed():
            return
        try:
            await self._kimi_page.evaluate(r"""() => {
                // 1. 关闭按钮
                const closeBtns = document.querySelectorAll('[class*="close"], [aria-label="关闭"], [aria-label="Close"], button[data-testid*="close"]');
                for (const btn of closeBtns) { try { btn.click(); } catch(e) {} }
                // 2. 同意/接受/确定 按钮
                const agreePatterns = ['同意', '接受', '确定', '确认', ' Agree', 'Accept', 'OK', 'Confirm', 'Got it', '知道了', '我已知晓'];
                const allBtns = document.querySelectorAll('button, [role="button"], a[role="button"]');
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim();
                    if (agreePatterns.some(p => txt.includes(p))) {
                        try { btn.click(); } catch(e) {}
                    }
                }
                // 3. 关闭遮罩层弹窗
                const overlays = document.querySelectorAll('[class*="overlay"], [class*="mask"], [class*="backdrop"], [class*="modal"]');
                for (const overlay of overlays) {
                    try { overlay.click(); } catch(e) {}
                }
            }""")
            await self._kimi_page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"[Kimi] dismiss popups: {e}")

    async def stream_kimi_chat(self, prompt: str = "", file_content: str = None, **kwargs):
        """发送消息到 Kimi 并流式返回响应。file_content 通过 UI 上传附件。
        
        此方法会:
        1. 确保 Kimi 编辑器就绪（已在 ensure_kimi_ready 中处理）
        2. 如果有文件内容，调用 _kimi_upload_file_via_ui 上传
        3. 输入消息到 .chat-input-editor
        4. 点击发送按钮或按 Enter
        5. 轮询 DOM 获取回复内容
        """
        await self.ensure_kimi_ready()

        await self._kimi_page.evaluate(r"""() => {
            const closeBtns = document.querySelectorAll('[class*="close"], [aria-label="关闭"], [aria-label="Close"]');
            for (const btn of closeBtns) { try { btn.click(); } catch(e) {} }
        }""")
        await asyncio.sleep(0.5)

        try:
            # 上传附件（如果有）
            if file_content:
                ok = await self._kimi_upload_file_via_ui(file_content)
                if not ok:
                    yield ("error", "[Kimi] UI file upload failed")
                    return

            # 移除遮罩层：Kimi 上传文件后，侧边栏的 mask 可能挡住编辑器/发送按钮
            await self._kimi_page.evaluate("""() => {
                document.querySelectorAll('[class*="mask"], [class*="overlay"], [class*="backdrop"], [class*="sidebar-slot"]').forEach(el => {
                    try {
                        if (el.style) el.style.display = 'none';
                    } catch(e) {}
                });
            }""")
            await asyncio.sleep(0.2)

            # 注册 route 拦截（应在文件上传之后、发送之前）
            q = asyncio.Queue()
            captured_chat_id = ""

            async def handle_kimi_chat_route(route):
                nonlocal captured_chat_id
                logger.info(f"[Kimi] ChatService/Chat intercepted: {route.request.method} {route.request.url}")
                try:
                    resp = await route.fetch(timeout=600000)
                    body = await resp.body()
                    logger.info(f"[Kimi] ChatService/Chat body length: {len(body)} bytes")
                    chunks = _parse_grpc_web_json_stream(body)
                    logger.info(f"[Kimi] Parsed {len(chunks)} JSON frames from stream")
                    for chunk in chunks:
                        chat = chunk.get("chat")
                        if isinstance(chat, dict) and chat.get("id"):
                            captured_chat_id = chat.get("id") or captured_chat_id
                            logger.info(f"[Kimi] Got session_id from response: {captured_chat_id}")
                            q.put_nowait(("session_id", captured_chat_id))
                        block = chunk.get("block")
                        if isinstance(block, dict):
                            text_obj = block.get("text")
                            if isinstance(text_obj, dict):
                                content = text_obj.get("content")
                                if content:
                                    q.put_nowait(("chunk", content))
                        if "done" in chunk:
                            q.put_nowait(("done", ""))
                    q.put_nowait(("done", ""))
                    await route.fulfill(response=resp)
                except Exception as e:
                    logger.warning(f"[Kimi] ChatService/Chat route error: {e}")
                    q.put_nowait(("error", str(e)))
                    q.put_nowait(("done", ""))
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            try:
                await self._kimi_page.unroute("**/apiv2/kimi.gateway.chat.v1.ChatService/Chat**")
            except Exception:
                pass
            await self._kimi_page.route("**/apiv2/kimi.gateway.chat.v1.ChatService/Chat**", handle_kimi_chat_route)

            # 输入消息（用 evaluate 聚焦+输入，绕过 Playwright 指针拦截检查）
            editor = await self._kimi_page.query_selector('.chat-input-editor')
            if editor:
                await self._kimi_page.evaluate("""() => {
                    const ed = document.querySelector('.chat-input-editor');
                    if (ed) { ed.focus(); ed.click(); }
                }""")
                await asyncio.sleep(0.2)
                await self._kimi_page.keyboard.press("Control+A")
                await asyncio.sleep(0.1)
                await self._kimi_page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                await self._kimi_page.keyboard.insert_text(prompt)
                await asyncio.sleep(0.5)
            else:
                yield ("error", "[Kimi] chat-input-editor not found")
                return

            # 发送前记录已有的 assistant 回复数量，用于后续只读取新增回复
            prev_assistant_count = await self._kimi_page.evaluate("""() => {
                return document.querySelectorAll('.chat-content-item-assistant').length;
            }""")
            logger.debug(f"[Kimi] prev_assistant_count before send: {prev_assistant_count}")

            # 点击发送按钮或按 Enter 发送消息
            await asyncio.sleep(0.5)
            send_ok = await self._kimi_page.evaluate("""() => {
                const btn = document.querySelector('.send-button-container:not(.disabled)') || 
                           document.querySelector('.send-button:not(.disabled)');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }""")
            if not send_ok:
                await self._kimi_page.keyboard.press("Enter")

            try:
                await self._kimi_page.wait_for_url("**/chat/**", timeout=10000)
            except Exception:
                pass

            # 从 URL 提取 chat_id 并 yield session_id 事件（route handler 也会提取）
            try:
                chat_id = await self._kimi_page.evaluate("""() => {
                    const url = window.location.href;
                    const match = url.match(/\\/chat\\/([a-f0-9-]+)/i);
                    return match ? match[1] : '';
                }""")
                if chat_id:
                    logger.info(f"[Kimi] session_id extracted from URL: {chat_id}")
                    yield ("session_id", chat_id)
                else:
                    logger.warning("[Kimi] Could not extract chat_id from URL")
            except Exception as e:
                logger.warning(f"[Kimi] session_id extraction error: {e}")

            # 通过网络拦截读取 AI 回复（替代 DOM 轮询）
            full_response = ""
            session_id_yielded = bool(chat_id)
            try:
                while True:
                    kind, value = await asyncio.wait_for(q.get(), timeout=600)
                    if kind == "session_id":
                        if not session_id_yielded and value:
                            logger.info(f"[Kimi] session_id from API: {value}")
                            yield ("session_id", value)
                            session_id_yielded = True
                    elif kind == "chunk":
                        full_response += value
                    elif kind == "error":
                        logger.warning(f"[Kimi] API stream error: {value}")
                        yield ("error", value)
                    elif kind == "done":
                        break
            except asyncio.TimeoutError:
                logger.warning(f"[Kimi] timeout waiting for API response (600s)")

            yield ("done", full_response)
            logger.info(f"[Kimi] stream done, total response: {len(full_response)} chars")

        except Exception as e:
            logger.error(f"[Kimi] stream_chat error: {e}")
            yield ("error", str(e))
        finally:
            try:
                await self._kimi_page.unroute("**/apiv2/kimi.gateway.chat.v1.ChatService/Chat**")
            except Exception:
                pass

    def _is_complete_kimi_json_response(self, text: str) -> bool:
        """Check if accumulated text contains a completely closed Kimi JSON response.
        Uses structural brace-counting (string-aware) instead of json.loads.
        Returns True only when the JSON object/array is fully closed."""
        # Find the first { or [ that starts the JSON payload
        start = -1
        for i, ch in enumerate(text):
            if ch == '{' or ch == '[':
                start = i
                break
        if start == -1:
            return False

        # Count braces with string awareness to find matching close
        stack = []
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == '{' or ch == '[':
                    stack.append(ch)
                elif ch == '}':
                    if stack and stack[-1] == '{':
                        stack.pop()
                    else:
                        return False  # mismatched close
                elif ch == ']':
                    if stack and stack[-1] == '[':
                        stack.pop()
                    else:
                        return False  # mismatched close
            if not stack:
                # JSON is fully closed
                return True
        return False  # never closed within the text

    async def close_kimi(self):
        """Close Kimi browser context and cleanup resources."""
        if self._kimi_page and not self._kimi_page.is_closed():
            try:
                await self._kimi_page.close()
                logger.info("[Kimi] page closed")
            except Exception as e:
                logger.warning(f"[Kimi] close page error: {e}")
        if self._kimi_browser:
            try:
                if hasattr(self._kimi_browser, 'is_connected') and self._kimi_browser.is_connected():
                    await self._kimi_browser.close()
                    logger.info("[Kimi] browser closed")
                elif hasattr(self._kimi_browser, 'close'):
                    await self._kimi_browser.close()
                    logger.info("[Kimi] browser context closed")
            except Exception as e:
                logger.warning(f"[Kimi] close browser error: {e}")
        self._kimi_page = None
        self._kimi_browser = None
        self._kimi_pw = None
        logger.info("[Kimi] resources cleaned up")

    async def delete_all_kimi_conversations(self):
        """通过 UI 自动化删除 Kimi 所有会话。"""
        import asyncio

        if not self._kimi_page or self._kimi_page.is_closed():
            await self.ensure_kimi_ready()

        logger.info("[Kimi] Starting UI-based conversation deletion...")
        deleted_count = 0
        page = self._kimi_page

        for attempt in range(30):
            try:
                await page.goto("https://www.kimi.com/zh/chat", wait_until="domcontentloaded", timeout=15000)
            except:
                await page.goto("https://www.kimi.com/")
            await asyncio.sleep(3)

            # 移除 mask 和展开侧边栏
            await page.evaluate("""() => {
                document.querySelectorAll('.mask, [class*="mask"], [class*="overlay"], [class*="backdrop"], [class*="sidebar-slot"]').forEach(el => {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                });
                document.querySelectorAll('.is-collapsed').forEach(el => el.classList.remove('is-collapsed'));
            }""")
            await asyncio.sleep(1)

            # 找到第一个会话链接，hover 它触发“更多”按钮
            chat_info = await page.evaluate("""() => {
                const link = document.querySelector('a[href*="/chat/"]:not([href*="history"])');
                if (!link) return null;
                const rect = link.getBoundingClientRect();
                return {
                    href: link.getAttribute('href'),
                    text: (link.textContent || '').trim().substring(0, 40),
                    x: Math.round(rect.x + rect.width / 2),
                    y: Math.round(rect.y + rect.height / 2),
                };
            }""")

            if not chat_info:
                logger.info("[Kimi] No chat links remaining. Deletion complete.")
                break

            logger.info(f"[Kimi] Hovering chat item at ({chat_info['x']}, {chat_info['y']})")

            # 鼠标移动 + 点击会话项（触发鼠标进入事件）
            await page.mouse.move(chat_info['x'], chat_info['y'])
            await asyncio.sleep(0.5)

            # 用 JS dispatch mouseenter 事件
            await page.evaluate("""(href) => {
                const link = document.querySelector(`a[href="${href}"]`);
                if (link) {
                    link.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                    link.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                }
            }""", chat_info['href'])
            await asyncio.sleep(1)

            # 现在查找出现的“更多”按钮
            more_btn = await page.evaluate("""() => {
                const btn = document.querySelector('.next-sidebar-history-item__more');
                if (!btn) return null;
                const rect = btn.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    return { x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) };
                }
                return null;
            }""")

            if not more_btn:
                logger.warning("[Kimi] 'More' button not visible. Trying JS click directly...")
                await page.evaluate("""(href) => {
                    const link = document.querySelector('a[href*="/chat/"]:not([href*="history"])');
                    if (link) {
                        // 查找父容器内的所有按钮
                        let p = link;
                        for (let i = 0; i < 4; i++) {
                            if (p && p.parentElement) p = p.parentElement;
                        }
                        if (p) {
                            const btns = p.querySelectorAll('button, [class*="more"], [class*="action"]');
                            if (btns.length > 0) btns[btns.length - 1].click();
                        }
                    }
                }""", chat_info['href'])
                await asyncio.sleep(2)
            else:
                logger.info(f"[Kimi] Clicking 'more' button at ({more_btn['x']}, {more_btn['y']})")
                await page.mouse.click(more_btn['x'], more_btn['y'])
                await asyncio.sleep(1)

            # 检查弹出的菜单
            menu_items = await page.evaluate("""() => {
                const menus = document.querySelectorAll('[class*="menu"], [role="menu"], [class*="popup"], [class*="dropdown"], [class*="popover"], [class*="dialog"]');
                const result = [];
                for (const menu of menus) {
                    const rect = menu.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        const items = menu.querySelectorAll('[role="menuitem"], button, li, a');
                        for (const it of items) {
                            const txt = (it.textContent || '').trim();
                            if (txt) result.push({ text: txt, className: (typeof it.className === 'string' ? it.className : '').substring(0, 50) });
                        }
                    }
                }
                return result;
            }""")
            logger.info(f"[Kimi] Menu items count: {len(menu_items)}")

            delete_clicked = False
            for item in menu_items:
                tl = item['text'].lower()
                if any(k in tl for k in ['delete', 'remove', 'trash', '删除', '清空']):
                    logger.info(f"[Kimi] Found delete menu item: {item['text']}")
                    await page.evaluate("""(txt) => {
                        const menus = document.querySelectorAll('[class*="menu"], [role="menu"], [class*="popup"], [class*="dropdown"]');
                        for (const menu of menus) {
                            const items = menu.querySelectorAll('[role="menuitem"], button, li, a');
                            for (const it of items) {
                                if ((it.textContent || '').trim() === txt) {
                                    it.click();
                                    return;
                                }
                            }
                        }
                    }""", item['text'])
                    delete_clicked = True
                    break

            if not delete_clicked:
                logger.warning("[Kimi] No delete option. Trying keyboard shortcut or alternative...")
                await page.keyboard.press("Escape")
                continue

            await asyncio.sleep(2)

            # 确认对话框
            try:
                confirm_clicked = await page.evaluate("""() => {
                    const dia = document.querySelectorAll('[class*="dialog"], [class*="modal"], [role="dialog"]');
                    for (const d of dia) {
                        const r = d.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            const btns = d.querySelectorAll('button, [role="button"]');
                            for (const b of btns) {
                                const t = (b.textContent || '').trim().toLowerCase();
                                if (t.includes('删除') || t.includes('确定') || t.includes('确认') || t.includes('yes') || t.includes('delete')) {
                                    b.click();
                                    return true;
                                }
                            }
                        }
                    }
                    return false;
                }""")
                if confirm_clicked:
                    logger.info("[Kimi] Confirmed via dialog button")
                await asyncio.sleep(2)
            except:
                pass

            deleted_count += 1
            logger.info(f"[Kimi] Deleted {deleted_count} conversation(s)")

        logger.info(f"[Kimi] Deletion complete. Total deleted: {deleted_count}")
        return deleted_count

