from ._shared import *


class DeepSeekMixin:
    async def activate_deepseek_conversation(self, session_id: str) -> bool:
        """导航到 DeepSeek 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_deepseek_ready()
            url = f"https://chat.deepseek.com/a/chat/s/{session_id}"
            await self._deepseek_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[DeepSeek] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[DeepSeek] activate conversation failed: {e}")
            return False

    async def ensure_deepseek_ready(self, headless=True):
        """确保 DeepSeek 浏览器就绪，使用持久化 user_data_dir 保留登录状态。"""
        if self._deepseek_page and self._deepseek_browser:
            return True
        async with self._deepseek_lock:
            if self._deepseek_page and self._deepseek_browser:
                return True

            if not os.path.exists(self._deepseek_user_data_dir):
                os.makedirs(self._deepseek_user_data_dir, exist_ok=True)
                raise RuntimeError(f"DeepSeek 用户目录已创建但尚未登录，请先运行 python main.py --login deepseek")

            from playwright.async_api import async_playwright
            self._deepseek_pw = await async_playwright().start()
            self._deepseek_browser = await self._deepseek_pw.chromium.launch_persistent_context(
                user_data_dir=self._deepseek_user_data_dir,
                headless=headless,
                channel=_browser_channel(),
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._deepseek_page = self._deepseek_browser.pages[0] if self._deepseek_browser.pages else await self._deepseek_browser.new_page()
            await self._deepseek_page.expose_function("__sse_push", self._on_deepseek_push)
            await self._deepseek_page.add_init_script("""
            // DeepSeek fetch interceptor — intercepts chat/completion and file upload APIs
            if (!window.__ds_fetch_patched) {
                window.__ds_fetch_patched = true;
                const origFetch = window.fetch;
                const origXHROpen = XMLHttpRequest.prototype.open;
                const origXHRSend = XMLHttpRequest.prototype.send;
                
                // Patch fetch
                window.fetch = async function(input, init) {
                    const url = typeof input === 'string' ? input : (input?.url || '');
                    const method = (init || {}).method || 'GET';
                    const body = (init || {}).body || '';
                    
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            // Capture auth token
                            const headers = (init || {}).headers || {};
                            const authHeader = headers['Authorization'] || headers['authorization'] || '';
                            if (authHeader) {
                                window.__deepseek_auth_token = authHeader.replace('Bearer ', '').replace(/'/g, "\\\\'");
                            }
                            
                            // Read params from window
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            const modifiedBody = JSON.stringify(bodyDict);
                            
                            // Forward request
                            const resp = await origFetch.call(this, input, {...init, body: modifiedBody});
                            const cloned = resp.clone();
                            const streamId = params.stream_id || '';
                            
                            // Read SSE stream
                            (async () => {
                                try {
                                    const reader = cloned.body.getReader();
                                    const decoder = new TextDecoder();
                                    let buf = '';
                                    
                                    while (true) {
                                        const {value, done} = await reader.read();
                                        if (done) {
                                            // 不在这里 push done，由 Python 端 event:close 触发
                                            break;
                                        }
                                        
                                        buf += decoder.decode(value, {stream: true});
                                        const blocks = buf.split('\\n\\n');
                                        buf = blocks.pop() || '';
                                        
                                        for (const block of blocks) {
                                            const trimmed = block.trim();
                                            if (trimmed && window.__sse_push) {
                                                window.__sse_push(streamId, 'raw_sse', trimmed);
                                            }
                                        }
                                    }
                                    
                                    // Flush remaining buffer
                                    if (buf.trim() && window.__sse_push) {
                                        window.__sse_push(streamId, 'raw_sse', buf.trim());
                                    }
                                } catch(e) {
                                    console.error('[ds-sse-read]', e);
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'error', String(e));
                                        window.__sse_push(streamId, 'done', '');
                                    }
                                }
                            })();
                            
                            return resp;
                        } catch(e) {
                            console.error('[ds-fetch-intercept]', e);
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    if (url.includes('/api/v0/file/upload_file')) {
                        try {
                            const resp = await origFetch.call(this, input, init);
                            const cloned = resp.clone();
                            const streamId = (window.__deepseek_request_params || {}).stream_id || '';
                            
                            (async () => {
                                try {
                                    const text = await cloned.text();
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'upload_response', text);
                                    }
                                } catch(e) {}
                            })();
                            
                            return resp;
                        } catch(e) {
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    return origFetch.apply(this, [input, init]);
                };
                
                // Patch XMLHttpRequest for upload
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.__ds_xhr_url = url;
                    this.__ds_xhr_method = method;
                    return origXHROpen.apply(this, [method, url, ...rest]);
                };
                
                XMLHttpRequest.prototype.send = function(body) {
                    const url = this.__ds_xhr_url || '';
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            body = JSON.stringify(bodyDict);
                            
                            // Capture auth token
                            const headers = this.getAllResponseHeaders() || '';
                            const authHeader = headers.match(/authorization:\\s*(.+)/i);
                            if (authHeader) {
                                window.__deepseek_auth_token = authHeader[1].replace('Bearer ', '').replace(/'/g, "\\\\'");
                            }
                            
                            const streamId = params.stream_id || '';
                            
                            let __xhr_lastLen2 = 0;
                            this.addEventListener('readystatechange', function() {
                                if (this.readyState === 3) {
                                    try {
                                        const partialText = this.responseText || '';
                                        if (partialText.length > __xhr_lastLen2) {
                                            const newPart = partialText.substring(__xhr_lastLen2);
                                            __xhr_lastLen2 = partialText.length;
                                            const blocks = newPart.split('\\n\\n');
                                            blocks.pop();
                                            for (const block of blocks) {
                                                const trimmed = block.trim();
                                                if (trimmed && window.__sse_push) {
                                                    window.__sse_push(streamId, 'raw_sse', trimmed);
                                                }
                                            }
                                        }
                                    } catch(e) {}
                                }
                                if (this.readyState === 4) {
                                    try {
                                        const fullText = this.responseText || '';
                                        if (fullText.length > __xhr_lastLen2) {
                                            const remaining = fullText.substring(__xhr_lastLen2).trim();
                                            if (remaining && window.__sse_push) {
                                                window.__sse_push(streamId, 'raw_sse', remaining);
                                            }
                                        }
                                        // 不在这里 push done，由 Python 端 event:close 触发
                                    } catch(e) {}
                                }
                            });
                        } catch(e) {}
                    }
                    return origXHRSend.apply(this, [body]);
                };
            }
            """)

            logger.info("DeepSeek: navigating to chat.deepseek.com/...")
            await self._deepseek_page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=60000)
            try:
                await self._deepseek_page.wait_for_selector('textarea, [contenteditable="true"], .ds-chat-input', timeout=20000)
                logger.info("DeepSeek: page rendered")
            except:
                logger.warning("DeepSeek: page elements not found after 20s")
                await asyncio.sleep(3)

            logger.info(f"DeepSeek: current URL: {self._deepseek_page.url}")
            logged_in = False
            try:
                textarea = await self._deepseek_page.query_selector('textarea, [contenteditable="true"]')
                if textarea:
                    logged_in = True
                    logger.info("DeepSeek: textarea found, login confirmed")
            except Exception:
                pass
            if not logged_in:
                current_url = self._deepseek_page.url
                if '/a/chat/s/' in current_url:
                    logged_in = True
                    logger.info("DeepSeek: chat session URL found, login confirmed")

            if not logged_in:
                logger.warning("DeepSeek: login required - session expired. Opening visible browser...")
                await self._deepseek_login_recovery()

            logger.info("DeepSeek browser ready")
            await self._dismiss_deepseek_popups()
            return True

    async def _deepseek_login_recovery(self):
        """打开可见浏览器让用户手动登录 DeepSeek，使用 user_data_dir 持久化状态。"""
        from playwright.async_api import async_playwright
        try:
            # 先关闭当前浏览器
            if self._deepseek_page and not self._deepseek_page.is_closed():
                await self._deepseek_page.close()
            if self._deepseek_browser and self._deepseek_browser:
                await self._deepseek_browser.close()
                self._deepseek_browser = None
            if self._deepseek_pw:
                await self._deepseek_pw.stop()
                self._deepseek_pw = None

            pw = await async_playwright().start()
            login_browser = await pw.chromium.launch_persistent_context(
                user_data_dir=self._deepseek_user_data_dir,
                headless=False,
                channel=_browser_channel(),
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            login_page = login_browser.pages[0] if login_browser.pages else await login_browser.new_page()
            await login_page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=60000)
            logger.info("DeepSeek: visible browser opened for manual login. Please log in...")

            while True:
                await asyncio.sleep(1)
                if not login_browser.pages:
                    break
                try:
                    textarea = await login_page.query_selector('textarea, [contenteditable="true"]')
                    if textarea:
                        logger.info("DeepSeek: chat editor found, login assumed successful...")
                        await asyncio.sleep(3)
                        break
                except Exception:
                    pass

            await login_browser.close()
            await pw.stop()

            # 重新创建上下文（复用已保存的 user_data_dir）
            self._deepseek_pw = await async_playwright().start()
            self._deepseek_browser = await self._deepseek_pw.chromium.launch_persistent_context(
                user_data_dir=self._deepseek_user_data_dir,
                headless=CONFIG.get('_deepseek_headless', True),
                channel=_browser_channel(),
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._deepseek_page = self._deepseek_browser.pages[0] if self._deepseek_browser.pages else await self._deepseek_browser.new_page()
            await self._deepseek_page.expose_function("__sse_push", self._on_deepseek_push)
            await self._deepseek_page.add_init_script("""
            if (!window.__ds_fetch_patched) {
                window.__ds_fetch_patched = true;
                const origFetch = window.fetch;
                const origXHROpen = XMLHttpRequest.prototype.open;
                const origXHRSend = XMLHttpRequest.prototype.send;
                
                window.fetch = async function(input, init) {
                    const url = typeof input === 'string' ? input : (input?.url || '');
                    const method = (init || {}).method || 'GET';
                    const body = (init || {}).body || '';
                    
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            const headers = (init || {}).headers || {};
                            const authHeader = headers['Authorization'] || headers['authorization'] || '';
                            if (authHeader) {
                                window.__deepseek_auth_token = authHeader.replace('Bearer ', '').replace(/'/g, "\\\\'");
                            }
                            
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            const modifiedBody = JSON.stringify(bodyDict);
                            
                            const resp = await origFetch.call(this, input, {...init, body: modifiedBody});
                            const cloned = resp.clone();
                            const streamId = params.stream_id || '';
                            
                            (async () => {
                                try {
                                    const reader = cloned.body.getReader();
                                    const decoder = new TextDecoder();
                                    let buf = '';
                                    
                                    while (true) {
                                        const {value, done} = await reader.read();
                                        if (done) {
                                            // 不在这里 push done，由 Python 端 event:close 触发
                                            break;
                                        }
                                        
                                        buf += decoder.decode(value, {stream: true});
                                        const blocks = buf.split('\\n\\n');
                                        buf = blocks.pop() || '';
                                        
                                        for (const block of blocks) {
                                            const trimmed = block.trim();
                                            if (trimmed && window.__sse_push) {
                                                window.__sse_push(streamId, 'raw_sse', trimmed);
                                            }
                                        }
                                    }
                                    
                                    if (buf.trim() && window.__sse_push) {
                                        window.__sse_push(streamId, 'raw_sse', buf.trim());
                                    }
                                } catch(e) {
                                    console.error('[ds-sse-read]', e);
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'error', String(e));
                                        window.__sse_push(streamId, 'done', '');
                                    }
                                }
                            })();
                            
                            return resp;
                        } catch(e) {
                            console.error('[ds-fetch-intercept]', e);
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    if (url.includes('/api/v0/file/upload_file')) {
                        try {
                            const resp = await origFetch.call(this, input, init);
                            const cloned = resp.clone();
                            const streamId = (window.__deepseek_request_params || {}).stream_id || '';
                            
                            (async () => {
                                try {
                                    const text = await cloned.text();
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'upload_response', text);
                                    }
                                } catch(e) {}
                            })();
                            
                            return resp;
                        } catch(e) {
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    return origFetch.apply(this, [input, init]);
                };
                
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.__ds_xhr_url = url;
                    this.__ds_xhr_method = method;
                    return origXHROpen.apply(this, [method, url, ...rest]);
                };
                
                XMLHttpRequest.prototype.send = function(body) {
                    const url = this.__ds_xhr_url || '';
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            body = JSON.stringify(bodyDict);
                            
                            // Capture auth token from response headers via intercepted response
                            let __xhr_lastLen = 0;
                            this.addEventListener('readystatechange', function() {
                                if (this.readyState === 2) { // HEADERS_RECEIVED
                                    try {
                                        const authHeader = this.getResponseHeader('Authorization');
                                        if (authHeader) {
                                            window.__deepseek_auth_token = authHeader.replace('Bearer ', '').replace(/'/g, "\\\\'");
                                        }
                                    } catch(e) {}
                                }
                                if (this.readyState === 3) { // LOADING
                                    try {
                                        const partialText = this.responseText || '';
                                        if (partialText.length > __xhr_lastLen) {
                                            const newPart = partialText.substring(__xhr_lastLen);
                                            __xhr_lastLen = partialText.length;
                                            const blocks = newPart.split('\\n\\n');
                                            blocks.pop(); // last element may be incomplete
                                            for (const block of blocks) {
                                                const trimmed = block.trim();
                                                if (trimmed && window.__sse_push) {
                                                    window.__sse_push(params.stream_id || '', 'raw_sse', trimmed);
                                                }
                                            }
                                        }
                                    } catch(e) {}
                                }
                                if (this.readyState === 4) { // DONE
                                    try {
                                        // Flush remaining buffer
                                        const fullText = this.responseText || '';
                                        if (fullText.length > __xhr_lastLen) {
                                            const remaining = fullText.substring(__xhr_lastLen).trim();
                                            if (remaining && window.__sse_push) {
                                                window.__sse_push(params.stream_id || '', 'raw_sse', remaining);
                                            }
                                        }
                                        // 不在这里 push done，由 Python 端 event:close 触发
                                    } catch(e) {}
                                }
                            });
                        } catch(e) {}
                    }
                    return origXHRSend.apply(this, [body]);
                };
            }
            """)

            logger.info("DeepSeek: navigating to chat.deepseek.com/...")
            await asyncio.sleep(3)

            logger.info("DeepSeek: login recovery completed, browser ready")
        except Exception as e:
            logger.error(f"DeepSeek login recovery failed: {e}")
            raise

    def _on_deepseek_push(self, stream_id: str, kind: str, value):
        q = self._deepseek_queues.get(stream_id)
        if q is None:
            return
        if kind == "raw_sse":
            self._parse_deepseek_sse_block(q, value)
        elif kind == "done":
            q.put_nowait(("done", ""))
        else:
            q.put_nowait((kind, value))

    def _parse_deepseek_sse_block(self, q: asyncio.Queue, block: str):
        """解析原始 SSE 块，可能包含多个 SSE 事件，按 \\n\\n 分割后逐个处理。"""
        # 一个 raw_sse block 可能包含多个 SSE 事件
        sub_events = block.split("\n\n")
        for sub in sub_events:
            sub = sub.strip()
            if not sub:
                continue
            event_type = ""
            data_lines = []
            for line in sub.split("\n"):
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            data_str = "\n".join(data_lines)

            if event_type == "close":
                q.put_nowait(("done", ""))
                return

            if not data_str:
                continue

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # 1. v.response.fragments（初始化 fragment）
            if "v" in data and isinstance(data["v"], dict) and "response" in data["v"]:
                resp_data = data["v"]["response"]
                fragments = resp_data.get("fragments", [])
                for frag in fragments:
                    if frag.get("type") == "RESPONSE":
                        content = frag.get("content", "")
                        if content:
                            q.put_nowait(("chunk", content))

            # 2. APPEND patch（追加内容到 fragment）
            if "p" in data and "o" in data and "v" in data and data["o"] == "APPEND":
                p = data["p"]
                val = data["v"]
                if p == "response/fragments/-1/content" and isinstance(val, str):
                    q.put_nowait(("chunk", val))
                elif p == "response/fragments" and isinstance(val, list):
                    for frag in val:
                        if frag.get("type") == "RESPONSE":
                            content = frag.get("content", "")
                            if content:
                                q.put_nowait(("chunk", content))

            # 3. 裸 v 字符串
            if "v" in data and isinstance(data["v"], str) and "p" not in data and data["v"]:
                q.put_nowait(("chunk", data["v"]))

    async def stream_deepseek_chat(self, prompt: str, model_type: str = "default", thinking_enabled: bool = False, search_enabled: bool = True, inline_file_content: str | None = None):
        """Route interception for deepseek chat API, convert custom SSE to OpenAI SSE chunks."""
        headless = CONFIG.get('_deepseek_headless', True)
        await self.ensure_deepseek_ready(headless=headless)
        await self._dismiss_deepseek_popups()
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._deepseek_queues[stream_id] = q
        session_id = ""

        async def ensure_deepseek_model_selected():
            """DeepSeek 页面模型匹配：先检测当前模型，不匹配则打开模型选择器并切换到目标模型。"""
            try:
                page = self._deepseek_page
                if not page:
                    return False

                # 诊断：记录页面顶部所有按钮和可交互元素
                try:
                    buttons_info = await page.evaluate("""() => {
                        const results = [];
                        const btns = document.querySelectorAll('button, [role="button"], [role="combobox"], [aria-haspopup], a[class*="model"], div[class*="model"], span[class*="model"]');
                        for (const el of btns) {
                            const rect = el.getBoundingClientRect();
                            if (rect.top > 100) continue; // 只看顶部区域
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            const txt = (el.textContent || '').trim().substring(0, 60);
                            const cls = (el.className || '').substring(0, 80);
                            const tag = el.tagName;
                            const role = el.getAttribute('role') || '';
                            const ariaLabel = el.getAttribute('aria-label') || '';
                            if (txt || cls || role || ariaLabel) {
                                results.push({ tag, txt, cls: cls.substring(0, 60), role, ariaLabel, top: Math.round(rect.top), h: Math.round(rect.height) });
                            }
                        }
                        return results.slice(0, 30);
                    }""")
                except Exception as e:
                    logger.debug(f"[DeepSeek] button scan failed: {e}")

                # 1. 读取当前选中的模型 - 使用多种定位策略
                current = await page.evaluate("""() => {
                    const selectors = [
                        'button[class*="model"]',
                        'button[class*="Model"]',
                        '[aria-label*="model"]',
                        '[aria-label*="Model"]',
                        '[data-testid*="model"]',
                        '.model-selector',
                        '.model-select',
                        '[class*="model-select"]',
                        '[class*="ModelSelect"]',
                        '[class*="modelSelect"]',
                        '[role="combobox"]',
                        '[aria-haspopup="listbox"]',
                        '[aria-haspopup="menu"]',
                        'nav button',
                        'header button',
                        '[class*="header"] button',
                        '[class*="topbar"] button',
                        '[class*="navbar"] button',
                    ];

                    for (const sel of selectors) {
                        try {
                            const el = document.querySelector(sel);
                            if (el) {
                                const txt = (el.textContent || el.innerText || '').trim();
                                const rect = el.getBoundingClientRect();
                                if (txt && rect.width > 0 && rect.height > 0 && rect.height < 60 && txt.length < 60) {
                                    return { text: txt, class: el.className || '', tag: el.tagName, selector: sel };
                                }
                            }
                        } catch(e) {}
                    }
                    return null;
                }""")

                if not current:
                    # DeepSeek 模型通过 API body 的 model_type 参数控制，没有独立的 UI 选择器
                    # 直接返回 True，由 handle_route 拦截器注入正确的 model_type
                    return True

                current_text = current['text']
                logger.info(f"[DeepSeek] Current model selector text: '{current_text}' (selector: {current.get('selector', 'unknown')})")

                # 2. 判断当前模型是否匹配目标
                def text_to_model_type(txt):
                    txt_lower = txt.lower()
                    if 'r1' in txt_lower or 'expert' in txt_lower or '深度思考' in txt or '专家' in txt:
                        return 'expert'
                    elif 'vl' in txt_lower or 'vision' in txt_lower or '识图' in txt or '视觉' in txt:
                        return 'vision'
                    else:
                        return 'default'

                current_type = text_to_model_type(current_text)
                logger.info(f"[DeepSeek] Parsed current model_type={current_type}, target={model_type}")

                if current_type == model_type:
                    logger.info(f"[DeepSeek] Model already matches: {current_text}")
                    return True

                # 3. 打开模型选择器
                logger.info(f"[DeepSeek] Opening model selector to switch to {model_type}")
                open_result = await page.evaluate("""() => {
                    const selectors = [
                        'button[class*="model"]',
                        'button[class*="Model"]',
                        '[aria-label*="model"]',
                        '[aria-label*="Model"]',
                        '[data-testid*="model"]',
                        '.model-selector',
                        '.model-select',
                        '[class*="model-select"]',
                        '[class*="ModelSelect"]',
                        'nav button',
                        'header button',
                        '[class*="header"] button',
                        '[class*="topbar"] button',
                        '[class*="navbar"] button',
                    ];

                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const txt = (el.textContent || el.innerText || '').trim();
                            const rect = el.getBoundingClientRect();
                            if (txt && rect.width > 0 && rect.height > 0 && rect.height < 50 && txt.length < 50) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }""")

                if not open_result:
                    logger.warning("[DeepSeek] Failed to open model selector")
                    return False

                await asyncio.sleep(1.5)

                # 4. 在模型下拉面板中查找并点击目标模型
                target_keywords = {
                    'expert': ['R1', '深度思考', '专家', 'Expert', 'DeepSeek-R1'],
                    'vision': ['VL', '识图', '视觉', 'Vision', 'DeepSeek-VL'],
                    'default': ['V3', '默认', '普通', 'Default', 'Fast', 'DeepSeek-V3'],
                }
                keywords = target_keywords.get(model_type, [])

                clicked = await page.evaluate(f"""() => {{
                    const keywords = {json.dumps(keywords)};

                    // 策略1: 查找下拉面板中的选项
                    const dropdownSelectors = [
                        '[class*="dropdown"]', '[class*="Dropdown"]',
                        '[class*="popover"]', '[class*="Popover"]',
                        '[class*="menu"]', '[class*="Menu"]',
                        '[role="menu"]', '[role="listbox"]',
                        '[class*="popup"]', '[class*="Popup"]',
                    ];

                    let panel = null;
                    for (const sel of dropdownSelectors) {{
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 200 && rect.height > 50) {{
                                panel = el;
                                break;
                            }}
                        }}
                        if (panel) break;
                    }}

                    const options = panel
                        ? panel.querySelectorAll('[role="option"], button, div[class*="cursor-pointer"]')
                        : document.querySelectorAll('[role="option"], button, div[class*="cursor-pointer"]');

                    for (const opt of options) {{
                        const txt = (opt.textContent || opt.innerText || '').trim();
                        const rect = opt.getBoundingClientRect();
                        if (!txt || rect.width <= 0 || rect.height <= 0) continue;
                        if (txt.length > 50) continue;

                        for (const kw of keywords) {{
                            if (txt.includes(kw)) {{
                                opt.click();
                                return txt;
                            }}
                        }}
                    }}

                    // 策略2: 查找 body 中所有可见的、包含关键词的可点击元素
                    const allBtns = document.querySelectorAll('button, [role="button"], div, span');
                    for (const btn of allBtns) {{
                        const txt = (btn.textContent || btn.innerText || '').trim();
                        const rect = btn.getBoundingClientRect();
                        if (!txt || rect.width <= 0 || rect.height <= 0) continue;
                        if (txt.length > 50) continue;

                        for (const kw of keywords) {{
                            if (txt.includes(kw)) {{
                                btn.click();
                                return txt;
                            }}
                        }}
                    }}

                    return null;
                }}""")

                if clicked:
                    logger.info(f"[DeepSeek] Model switched to: {clicked}")
                    await asyncio.sleep(1)
                    return True
                else:
                    logger.warning(f"[DeepSeek] Target model with keywords {keywords} not found in selector")
                    await page.keyboard.press("Escape")
                    return False

            except Exception as e:
                logger.warning(f"[DeepSeek] ensure model selected failed: {e}")
                return False

        # 1. Ensure we have a chat session (create if needed)
        try:
            current_url = self._deepseek_page.url
            if '/a/chat/s/' not in current_url:
                await self._deepseek_page.evaluate("""() => {
                    const newChatBtn = document.querySelector('[class*="new-chat"], button[title*="New"], [aria-label*="New"]');
                    if (newChatBtn) newChatBtn.click();
                }""")
                await asyncio.sleep(2)
                # Wait for URL to update with session ID
                for _ in range(10):
                    url = self._deepseek_page.url
                    if '/a/chat/s/' in url:
                        break
                    await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"[DeepSeek] create new session failed: {e}")

        # 2. Capture session_id from URL
        try:
            url = self._deepseek_page.url
            if '/a/chat/s/' in url:
                session_id = url.split('/a/chat/s/')[1].split('?')[0].split('#')[0]
                logger.info(f"[DeepSeek] initial session_id: {session_id}")
        except Exception:
            pass

        # 3. Intercept upload API AND chat completion API
        uploaded_file_id = None
        upload_result_future = asyncio.get_event_loop().create_future()

        async def handle_upload_route(route):
            nonlocal uploaded_file_id
            if "upload_file" not in route.request.url:
                await route.continue_()
                return
            resp = await route.fetch()
            resp_text = await resp.text()
            try:
                data = json.loads(resp_text)
                if data.get("code") == 0:
                    biz = data["data"]["biz_data"]
                    fid = biz.get("id")
                    status = biz.get("status")
                    logger.info(f"[DeepSeek] upload API: file_id={fid}, status={status}, file_size={biz.get('file_size')}")
                    if fid and status in ("SUCCESS", "PENDING"):
                        uploaded_file_id = fid
                        if not upload_result_future.done():
                            upload_result_future.set_result(fid)
                else:
                    if not upload_result_future.done():
                        upload_result_future.set_result(None)
                    logger.warning(f"[DeepSeek] upload API error: {data}")
            except Exception as e:
                if not upload_result_future.done():
                    upload_result_future.set_result(None)
                logger.warning(f"[DeepSeek] upload parse error: {e}")
            await route.fulfill(response=resp)

        await self._deepseek_page.route("**/api/v0/file/upload_file**", handle_upload_route)

        async def upload_file():
            """通过 JS DataTransfer 模拟文件拖放到 textarea，触发 DeepSeek 页面原生上传流程。"""
            nonlocal uploaded_file_id
            if not inline_file_content:
                return

            result = await self._deepseek_page.evaluate("""async (content) => {
                try {
                    const blob = new Blob([content], { type: 'text/plain' });
                    const file = new File([blob], 'request.txt', { type: 'text/plain', lastModified: Date.now() });
                    const dataTransfer = new DataTransfer();
                    dataTransfer.items.add(file);
                    const textarea = document.querySelector('textarea');
                    if (!textarea) return { error: 'no textarea found' };
                    textarea.dispatchEvent(new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer }));
                    textarea.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
                    textarea.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
                    return { dispatched: true };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", inline_file_content)

            if result.get("error"):
                logger.warning(f"[DeepSeek] file drop JS error: {result['error']}")
                return

            # 等待上传完成
            try:
                fid = await asyncio.wait_for(upload_result_future, timeout=30)
                if fid:
                    uploaded_file_id = fid
                    logger.info(f"[DeepSeek] file uploaded, file_id={fid}")
                    # 等待文件解析完成（检测 "等待中" 文本消失）
                    for _ in range(60):
                        await asyncio.sleep(1)
                        file_status = await self._deepseek_page.evaluate("""() => {
                            // DeepSeek file card structure: elements with TXT XX B text pattern
                            const allEls = document.querySelectorAll('*');
                            let lastCardText = '';
                            for (const el of allEls) {
                                const text = (el.innerText || '').trim();
                                if (/^TXT \\d+[KMG]?B$/.test(text)) {
                                    // Found a file info element like "TXT 62B"
                                    const parent = el.closest('[tabindex]');
                                    if (parent) {
                                        lastCardText = parent.innerText || '';
                                    }
                                }
                            }
                            if (!lastCardText) return null;
                            if (lastCardText.includes('等待中')) return 'waiting';
                            return 'ready';
                        }""")
                        if file_status == 'ready':
                            logger.info("[DeepSeek] file parsed successfully")
                            return
                        elif file_status is None:
                            logger.info("[DeepSeek] file card disappeared, but we have file_id from upload API - proceeding")
                            # 文件卡片消失不代表上传失败，只要我们有file_id就继续
                            return
                    logger.warning("[DeepSeek] file parse timeout after 60s")
                else:
                    logger.warning("[DeepSeek] upload returned no file_id")
            except asyncio.TimeoutError:
                logger.warning("[DeepSeek] file upload timeout")

        # 不拦截 completion 请求，SSE 完全由 JS fetch 拦截器处理
        # 只用 request 事件捕获 auth token
        def on_auth_capture(request):
            if 'chat.deepseek.com/api/v0/chat/completion' in request.url:
                auth = request.headers.get('authorization', '')
                if auth:
                    token_value = auth.replace('Bearer ', '').replace('\\', '\\\\').replace("'", "\\'")
                    asyncio.ensure_future(self._deepseek_page.evaluate(f"window.__deepseek_auth_token = '{token_value}';"))
                    logger.info(f"[DeepSeek] auth token captured from request")

        self._deepseek_page.on("request", on_auth_capture)

        try:
            # 1. 确保页面模型匹配请求 (fail fast if mismatch)
            if not await ensure_deepseek_model_selected():
                yield ("error", f"页面模型切换失败，无法匹配请求的 model_type={model_type}")
                yield ("done", "")
                return

            # 2. 切换思考和搜索开关
            toggles = await self._deepseek_page.query_selector_all('.ds-toggle-button')
            if toggles:
                # 思考模式开关（第一个）
                if len(toggles) >= 1:
                    thinking_toggle = toggles[0]
                    try:
                        thinking_icon = await thinking_toggle.query_selector('.ds-toggle-button__icon')
                        if thinking_icon:
                            icon_class = await thinking_icon.get_attribute('class') or ''
                            thinking_on = '--selected' in icon_class
                            if thinking_enabled != thinking_on:
                                await thinking_toggle.click()
                                await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"[DeepSeek] 思考开关设置失败: {e}")

                # 搜索开关（第二个），仅非expert模式有效
                if model_type != "expert" and len(toggles) >= 2:
                    search_toggle = toggles[1]
                    try:
                        search_icon = await search_toggle.query_selector('.ds-toggle-button__icon')
                        if search_icon:
                            icon_class = await search_icon.get_attribute('class') or ''
                            search_on = '--selected' in icon_class
                            if search_enabled != search_on:
                                await search_toggle.click()
                                await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"[DeepSeek] 搜索开关设置失败: {e}")

            # 3. 上传文件（如果有）
            if inline_file_content:
                await upload_file()
                if not uploaded_file_id:
                    yield ("error", "文件上传失败，请重试")
                    yield ("done", "")
                    return
                logger.info("[DeepSeek] file uploaded successfully, injecting ref_file_ids")
                # 文件上传后等待页面稳定
                await asyncio.sleep(2)

            # 4. 查找 textarea 并输入 prompt
            textarea = await self._deepseek_page.query_selector('textarea')
            if not textarea:
                editor = await self._deepseek_page.query_selector('[contenteditable="true"]')
                if editor:
                    logger.info("[DeepSeek] found contenteditable editor")
                    await editor.click()
                    await asyncio.sleep(0.3)
                    await self._deepseek_page.evaluate("""(text) => {
                        const el = document.querySelector('[contenteditable="true"]');
                        if (el) {
                            el.focus();
                            el.innerText = text;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""", prompt)
                    await asyncio.sleep(0.3)
                    await self._deepseek_page.keyboard.press("Enter")
                    logger.info("[DeepSeek] contenteditable + Enter pressed")
                else:
                    logger.warning("[DeepSeek] no textarea or contenteditable found!")
            else:
                logger.info("[DeepSeek] found textarea, setting value...")
                await textarea.focus()
                await asyncio.sleep(0.3)
                # 使用 nativeSetter + input 事件确保 React state 同步
                await self._deepseek_page.evaluate("""(text) => {
                    const ta = document.querySelector('textarea');
                    if (!ta) return;
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(ta, text);
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }""", prompt)
                await asyncio.sleep(1)
                # 确认值已写入
                val = await self._deepseek_page.evaluate("() => document.querySelector('textarea')?.value || ''")
                logger.debug(f"[DeepSeek] textarea value length: {len(val)}")
                # 设置 JS 拦截器参数（在发送前）
                await self._deepseek_page.evaluate("""(params) => {
                    window.__deepseek_request_params = params;
                }""", {"model_type": model_type, "thinking_enabled": thinking_enabled,
                        "search_enabled": search_enabled,
                        "ref_file_ids": [uploaded_file_id] if uploaded_file_id else [],
                        "stream_id": stream_id})
                logger.debug(f"[DeepSeek] JS request params set: model_type={model_type}, thinking={thinking_enabled}, search={search_enabled}")
                # 发送前重新获取 textarea（文件上传后可能被替换）
                textarea = await self._deepseek_page.query_selector('textarea')
                if not textarea:
                    logger.warning("[DeepSeek] textarea not found before send")
                    yield ("error", "textarea not found")
                    yield ("done", "")
                    return
                # 点击 textarea 中心确保聚焦（文件上传后 focus 可能丢失）
                ta_box = await textarea.bounding_box()
                if ta_box:
                    cx = ta_box['x'] + ta_box['width'] / 2
                    cy = ta_box['y'] + ta_box['height'] / 2
                    await self._deepseek_page.mouse.click(cx, cy)
                    await asyncio.sleep(0.3)
                await textarea.focus()
                await asyncio.sleep(0.2)
                await self._deepseek_page.keyboard.press("End")
                await self._deepseek_page.keyboard.press("Enter")
                logger.debug("[DeepSeek] pressed End+Enter to send")
                await asyncio.sleep(1)
                # 验证：如果 textarea 还有内容，尝试点击发送按钮
                remaining = await self._deepseek_page.evaluate("() => document.querySelector('textarea')?.value || ''")
                if remaining:
                    logger.debug(f"[DeepSeek] Enter did not send ({len(remaining)} chars), trying button click")
                    btn = await self._deepseek_page.query_selector('[class*="ds-button--primary"][class*="ds-button--filled"][class*="ds-button--circle"]')
                    if btn:
                        await btn.click(force=True)
                        logger.debug("[DeepSeek] clicked send button")
                await asyncio.sleep(1)
                # 验证：检查 textarea 是否已清空（发送成功会清空）
                remaining = await self._deepseek_page.evaluate("() => document.querySelector('textarea')?.value || ''")
                logger.debug(f"[DeepSeek] textarea after send: {len(remaining)} chars")
            await asyncio.sleep(1)

            # 4. 在发送后从 URL 再次捕获 session_id（可能已更新）
            try:
                url = self._deepseek_page.url
                if '/a/chat/s/' in url:
                    new_sid = url.split('/a/chat/s/')[1].split('?')[0].split('#')[0]
                    if new_sid != session_id:
                        session_id = new_sid
                        logger.info(f"[DeepSeek] updated session_id: {session_id}")
            except Exception:
                pass

            if session_id:
                yield ("session_id", session_id)

            while True:
                try:
                    kind, value = await asyncio.wait_for(q.get(), timeout=120)
                    yield (kind, value)
                    if kind == "done":
                        break
                except asyncio.TimeoutError:
                    logger.warning("[DeepSeek] timeout waiting for response")
                    yield ("error", "千锤万凿出深山，烈火焚烧若等闲。")
                    yield ("done", "")
                    break
        except Exception as e:
            logger.error(f"[DeepSeek] stream error: {e}")
            yield ("error", str(e))
            yield ("done", "")
        finally:
            self._deepseek_queues.pop(stream_id, None)
            try:
                if self._deepseek_page and not self._deepseek_page.is_closed():
                    await self._deepseek_page.unroute("**/api/v0/file/upload_file**", handle_upload_route)
            except Exception:
                pass

    async def get_deepseek_session_id(self) -> str:
        """从 DeepSeek 页面 URL 提取当前会话 ID。"""
        try:
            if not self._deepseek_page:
                return ""
            url = self._deepseek_page.url
            if '/a/chat/s/' in url:
                sid = url.split('/a/chat/s/')[1]
                sid = sid.split('?')[0].split('#')[0]
                if sid:
                    return sid
            return ""
        except Exception:
            return ""

    async def delete_deepseek_conversation(self, session_id: str):
        """删除单个 DeepSeek 会话（通过浏览器页面调用 API）。"""
        if not session_id:
            return
        try:
            result = await self._deepseek_page.evaluate(f"""async () => {{
                try {{
                    const token = window.__deepseek_auth_token || '';
                    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
                    const csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';
                    const headers = {{
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': csrf,
                    }};
                    if (token) headers['Authorization'] = 'Bearer ' + token;
                    const resp = await fetch('/api/v0/chat_session/delete', {{
                        method: 'POST',
                        headers: headers,
                        body: JSON.stringify({{chat_session_id: '{session_id}'}})
                    }});
                    const data = await resp.json();
                    return {{success: data.code === 0, code: data.code, msg: data.msg}};
                }} catch (e) {{
                    return {{success: false, error: String(e)}};
                }}
            }}""")
            if result.get('success'):
                logger.info(f"[DeepSeek] deleted session {session_id}")
            else:
                logger.warning(f"[DeepSeek] delete failed: {json.dumps(result, ensure_ascii=False)[:200]}")
        except Exception as e:
            logger.warning(f"[DeepSeek] delete exception: {e}")

    async def delete_all_deepseek_conversations(self):
        """删除所有 DeepSeek 会话（通过浏览器页面 API 删除）。"""
        try:
            if not self._deepseek_page or self._deepseek_page.is_closed():
                logger.warning("[DeepSeek] no page, skip batch delete")
                return

            result = await self._deepseek_page.evaluate("""async () => {
                try {
                    // 从 localStorage 获取 auth token
                    let token = '';
                    try {
                        const raw = localStorage.getItem('userToken');
                        if (raw) {
                            const obj = JSON.parse(raw);
                            token = obj.value || '';
                        }
                    } catch(e) {}
                    
                    const headers = { 'Content-Type': 'application/json' };
                    if (token) headers['Authorization'] = 'Bearer ' + token;
                    
                    const listResp = await fetch('/api/v0/chat_session/fetch_page?lte_cursor.pinned=false', {
                        headers: headers
                    });
                    const listData = await listResp.json();
                    const sessions = listData?.data?.biz_data?.chat_sessions || [];
                    
                    let deleted = 0, failed = 0;
                    for (const s of sessions) {
                        const delResp = await fetch('/api/v0/chat_session/delete', {
                            method: 'POST',
                            headers: headers,
                            body: JSON.stringify({chat_session_id: s.id})
                        });
                        const delData = await delResp.json();
                        if (delData.code === 0) deleted++;
                        else failed++;
                    }
                    return {total: sessions.length, deleted, failed};
                } catch (e) {
                    return {error: String(e)};
                }
            }""")
            if isinstance(result, dict) and 'error' not in result:
                logger.info(f"[DeepSeek] batch delete: {result}")
            else:
                logger.warning(f"[DeepSeek] batch delete failed: {result}")
        except Exception as e:
            logger.warning(f"[DeepSeek] delete_all error: {e}")

    async def _dismiss_deepseek_popups(self):
        """处理 DeepSeek 页面上的各种弹窗。"""
        if not self._deepseek_page or self._deepseek_page.is_closed():
            return
        try:
            await self._deepseek_page.evaluate(r"""() => {
                const closeBtns = document.querySelectorAll('[class*="close"], [aria-label="关闭"], [aria-label="Close"], button[data-testid*="close"]');
                for (const btn of closeBtns) { try { btn.click(); } catch(e) {} }
                const agreePatterns = ['同意', '接受', '确定', '确认', 'Agree', 'Accept', 'OK', 'Confirm', 'Got it', '知道了'];
                const allBtns = document.querySelectorAll('button, [role="button"]');
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim();
                    if (agreePatterns.some(p => txt === p || txt.includes(p))) {
                        try { btn.click(); } catch(e) {}
                    }
                }
            }""")
            await self._deepseek_page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"[DeepSeek] dismiss popups: {e}")

