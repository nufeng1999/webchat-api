from ._shared import *


class MiniMaxMixin:
    async def activate_minimax_conversation(self, session_id: str) -> bool:
        """导航到 MiniMax 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_minimax_ready()
            url = f"https://agent.minimaxi.com/mavis/{session_id}"
            await self._minimax_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[MiniMax] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[MiniMax] activate conversation failed: {e}")
            return False

    async def ensure_minimax_ready(self, headless: bool = True):
        """确保 MiniMax Agent 浏览器已启动并登录。"""
        if self._minimax_page and not self._minimax_page.is_closed():
            return

        from playwright.async_api import async_playwright
        logger.info("[Minimax] Starting MiniMax Agent browser...")
        self._minimax_pw = await async_playwright().start()
        self._minimax_browser = await self._minimax_pw.chromium.launch_persistent_context(
            user_data_dir=self._minimax_user_data_dir,
            headless=headless,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._minimax_page = self._minimax_browser.pages[0] if self._minimax_browser.pages else await self._minimax_browser.new_page()

        # 反检测
        await self._minimax_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        await self._minimax_page.goto("https://agent.minimaxi.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        # 移除遮挡弹窗
        await self._minimax_page.evaluate("""() => {
            document.querySelectorAll('[data-connect-mobile-hint-dismiss-boundary]').forEach(el => el.remove());
            document.querySelectorAll('.fixed').forEach(el => {
                const z = el.style.zIndex || window.getComputedStyle(el).zIndex;
                if (parseInt(z) >= 999) el.remove();
            });
        }""")
        await asyncio.sleep(1)

        # 检查是否已登录（查找登录按钮）
        is_logged_in = await self._minimax_page.evaluate("""() => {
            return !document.querySelector('[data-testid="sidebar-login-button"]');
        }""")

        if not is_logged_in:
            if headless:
                raise RuntimeError("MiniMax Agent 未登录，请先运行 python main.py --login minimax")
            # 非 headless 模式，尝试登录
            logger.info("[Minimax] Not logged in, opening login page...")
            await self._minimax_page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="sidebar-login-button"]');
                if (btn) btn.click();
            }""")
            await asyncio.sleep(3)

            # 等待用户登录（最多 3 分钟）
            for i in range(90):
                await asyncio.sleep(2)
                url = self._minimax_page.url
                if (url.startswith('https://agent.minimaxi.com') and
                    'account.minimaxi.com' not in url and
                    'unified-login' not in url):
                    logger.info("[Minimax] Login successful!")
                    break
            else:
                raise RuntimeError("MiniMax Agent 登录超时")

        logger.info("[Minimax] MiniMax Agent browser ready")

    async def get_minimax_session_id(self) -> str:
        """从 MiniMax 页面 URL 提取当前会话 ID。"""
        try:
            if not self._minimax_page:
                return ""
            url = self._minimax_page.url
            if '/mavis/' in url:
                sid = url.split('/mavis/')[-1].split('?')[0].split('#')[0]
                if sid:
                    return sid
            return ""
        except Exception:
            return ""

    async def stream_minimax_chat(self, prompt: str, model_type: str = "m3",
                                   thinking_enabled: bool = False,
                                   search_enabled: bool = False,
                                   inline_file_content: str | None = None,
                                   model_name: str | None = None):
        """向 MiniMax Agent 发送消息并流式返回响应。

        Yields: (kind, value) 元组
            kind: "session_id", "chunk", "done", "error"
        """
        if not self._minimax_page or self._minimax_page.is_closed():
            yield ("error", "MiniMax page not available")
            return

        async with self._minimax_lock:
            try:
                # 创建新会话
                await self._minimax_page.goto("https://agent.minimaxi.com/", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                # 移除弹窗
                await self._minimax_page.evaluate("""() => {
                    document.querySelectorAll('[data-connect-mobile-hint-dismiss-boundary]').forEach(el => el.remove());
                    document.querySelectorAll('.fixed').forEach(el => {
                        const z = el.style.zIndex || window.getComputedStyle(el).zIndex;
                        if (parseInt(z) >= 999) el.remove();
                    });
                }""")
                await asyncio.sleep(1)

                # 注入 SSE 拦截器
                await self._minimax_page.evaluate("""() => {
                    if (!window.__minimax_sse_patched) {
                        window.__minimax_sse_patched = true;
                        window.__minimax_sse_events = [];
                        const origFetch = window.fetch;
                        window.fetch = async function(...args) {
                            const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                            const resp = await origFetch.apply(this, args);
                            if (url.includes('/chat/') || url.includes('/send_msg') || url.includes('/continue_run')) {
                                try {
                                    const cloned = resp.clone();
                                    const reader = cloned.body.getReader();
                                    const decoder = new TextDecoder();
                                    (async () => {
                                        let buf = '';
                                        while (true) {
                                            const {value, done} = await reader.read();
                                            if (done) {
                                                window.__minimax_sse_events.push(JSON.stringify({type: 'done'}));
                                                break;
                                            }
                                            buf += decoder.decode(value, {stream: true});
                                            const lines = buf.split('\\n');
                                            buf = lines.pop() || '';
                                            for (const line of lines) {
                                                if (line.startsWith('data:')) {
                                                    const raw = line.slice(5).trim();
                                                    if (raw === '[DONE]') {
                                                        window.__minimax_sse_events.push(JSON.stringify({type: 'done'}));
                                                    } else if (raw) {
                                                        window.__minimax_sse_events.push(JSON.stringify({type: 'data', data: raw}));
                                                    }
                                                } else if (line.startsWith('event:')) {
                                                    const evt = line.slice(6).trim();
                                                    if (evt === 'done' || evt === 'close') {
                                                        window.__minimax_sse_events.push(JSON.stringify({type: 'done'}));
                                                    }
                                                }
                                            }
                                        }
                                    })();
                                } catch(e) {}
                            }
                            return resp;
                        };
                    }
                }""")

                # 准备发送内容
                content = inline_file_content if inline_file_content else prompt

                # 查找输入框并输入
                textarea = self._minimax_page.locator('textarea, [contenteditable="true"]').first
                await textarea.click()
                await asyncio.sleep(0.5)

                # 使用 native setter 确保 React 状态同步
                await self._minimax_page.evaluate("""(text) => {
                    const el = document.querySelector('textarea, [contenteditable="true"]');
                    if (!el) return;
                    if (el.tagName === 'TEXTAREA') {
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        nativeSetter.call(el, text);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    } else {
                        el.textContent = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""", content)
                await asyncio.sleep(0.5)

                # 清空 SSE 事件缓冲
                await self._minimax_page.evaluate("window.__minimax_sse_events = []")

                # 点击发送按钮或按 Enter
                send_btn = self._minimax_page.locator('button[data-testid*="send"], button[aria-label*="发送"]').first
                try:
                    await send_btn.click(timeout=3000)
                except Exception:
                    await textarea.press("Enter")

                await asyncio.sleep(1)

                # 获取会话 ID
                session_id = await self.get_minimax_session_id()
                if session_id:
                    yield ("session_id", session_id)

                # 读取 SSE 事件
                last_event_count = 0
                empty_count = 0
                max_empty = 30  # 60 秒无数据则超时

                while empty_count < max_empty:
                    await asyncio.sleep(2)
                    events = await self._minimax_page.evaluate("window.__minimax_sse_events || []")
                    new_events = events[last_event_count:]
                    last_event_count = len(events)

                    if not new_events:
                        empty_count += 1
                        continue

                    empty_count = 0
                    for evt_str in new_events:
                        try:
                            evt = json.loads(evt_str)
                            if evt.get("type") == "done":
                                yield ("done", "")
                                return
                            elif evt.get("type") == "data":
                                data_str = evt.get("data", "")
                                # 尝试解析 SSE data
                                try:
                                    data = json.loads(data_str)
                                    # MiniMax 可能使用不同的数据格式
                                    content = (data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                              or data.get("data", {}).get("delta_content", "")
                                              or data.get("delta_content", "")
                                              or "")
                                    if content:
                                        yield ("chunk", content)
                                except json.JSONDecodeError:
                                    # 可能是纯文本
                                    if data_str and data_str != "[DONE]":
                                        yield ("chunk", data_str)
                        except Exception:
                            pass

                yield ("done", "")

            except Exception as e:
                logger.warning(f"[Minimax] stream_chat error: {e}")
                yield ("error", str(e))

    async def delete_minimax_conversation(self, session_id: str):
        """删除单个 MiniMax 会话。"""
        if not session_id or not self._minimax_page or self._minimax_page.is_closed():
            return
        try:
            # 获取 token
            token = await self._minimax_page.evaluate("localStorage.getItem('token') || ''")
            if not token:
                logger.warning("[Minimax] no token for delete")
                return

            result = await self._minimax_page.evaluate("""async (args) => {
                try {
                    const resp = await fetch('/sidebar/session/' + args.sid + '?device_platform=web&biz_id=3&app_id=3001', {
                        method: 'DELETE',
                        headers: { 'Authorization': 'Bearer ' + args.token, 'Content-Type': 'application/json' }
                    });
                    return { ok: resp.ok, status: resp.status };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", {"sid": session_id, "token": token})
            if result.get("ok"):
                logger.info(f"[Minimax] deleted session {session_id}")
            else:
                logger.warning(f"[Minimax] delete failed: {result}")
        except Exception as e:
            logger.warning(f"[Minimax] delete exception: {e}")

    async def delete_all_minimax_conversations(self):
        """删除所有 MiniMax 会话。"""
        try:
            if not self._minimax_page or self._minimax_page.is_closed():
                logger.warning("[Minimax] no page, skip batch delete")
                return

            result = await self._minimax_page.evaluate("""async () => {
                try {
                    const token = localStorage.getItem('token') || '';
                    if (!token) return { error: 'no token' };

                    // 获取会话列表
                    const listResp = await fetch('/sidebar/session?device_platform=web&biz_id=3&app_id=3001', {
                        headers: { 'Authorization': 'Bearer ' + token }
                    });
                    const listData = await listResp.json();
                    const sessions = listData?.data?.sessions || [];
                    if (sessions.length === 0) return { deleted: 0, total: 0 };

                    let deleted = 0;
                    for (const s of sessions) {
                        try {
                            const delResp = await fetch('/sidebar/session/' + s.id + '?device_platform=web&biz_id=3&app_id=3001', {
                                method: 'DELETE',
                                headers: { 'Authorization': 'Bearer ' + token }
                            });
                            if (delResp.ok) deleted++;
                        } catch(e) {}
                    }
                    return { deleted, total: sessions.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""")
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Minimax] delete_all: deleted {deleted}/{total} sessions")
        except Exception as e:
            logger.warning(f"[Minimax] delete_all exception: {e}")

    async def close_minimax(self):
        """关闭 MiniMax 浏览器。"""
        try:
            if self._minimax_page and not self._minimax_page.is_closed():
                await self._minimax_page.close()
        except Exception:
            pass
        self._minimax_page = None
        try:
            if self._minimax_browser:
                await self._minimax_browser.close()
        except Exception:
            pass
        self._minimax_browser = None
        try:
            if self._minimax_pw:
                await self._minimax_pw.stop()
        except Exception:
            pass
        self._minimax_pw = None
        logger.info("[Minimax] resources cleaned up")

    async def _minimax_fetch_with_signature(self, path: str, extra: dict | None = None, body: dict | None = None, method: str = "GET") -> dict:
        """在浏览器内完整执行 Minimax API 请求，包括构建 URL 和生成签名。"""
        signature_js = """
        async (path, extra, body, method) => {
            // 从 localStorage 获取 token
            const token = localStorage.getItem('_token');
            if (!token) throw new Error('no token');
            
            // 获取用户信息
            const ud = localStorage.getItem('user_detail_agent');
            let userId = '';
            if (ud) {
                try { userId = JSON.parse(ud).realUserID || JSON.parse(ud).userID || ''; } catch(e) {}
            }
            const deviceId = localStorage.getItem('UNIQUE_USER_ID') || '';
            
            // 基础参数
            const base = {{
                device_platform: "web",
                biz_id: "3",
                app_id: "3001",
                version_code: "22201",
                timezone_offset: String(-new Date().getTimezoneOffset() * 60),
                sys_language: navigator.language || "zh",
                lang: "zh",
                uuid: deviceId,
                os_name: navigator.userAgent.includes("Windows") ? "Windows" : navigator.userAgent.includes("Mac") ? "macOS" : "Linux",
                browser_name: "Chrome",
                device_memory: String(navigator.deviceMemory || 8),
                cpu_core_num: String(navigator.hardwareConcurrency || 8),
                browser_language: navigator.language || "zh-CN",
                browser_platform: navigator.platform || "Win32",
                user_id: userId,
                screen_width: String(screen.width || 1280),
                screen_height: String(screen.height || 900),
                client: "web"
            }};
            
            // 合并额外参数
            const allParams = { ...base, ...extra };
            // 加上 Unix 时间戳
            const now = Date.now();
            const unix = String(now);
            allParams.unix = unix;
            
            // 构建查询字符串
            const usp = new URLSearchParams();
            for (const [k, v] of Object.entries(allParams)) {
                usp.append(k, String(v));
            }
            const queryString = usp.toString();
            
            // 构建完整 URL
            const url = `https://agent.minimaxi.com${path}?${queryString}`;
            
            // 提取路径+查询用于签名（去掉 hostname）
            const pathWithQS = path + '?' + queryString;
            
            // 生成 x-timestamp
            const xTimestamp = Math.floor(now / 1000).toString();
            
            // 生成 x-signature: MD5(xTimestamp + "I*7Cf%WZ#S&%1RlZJ&C2" + bodyString)
            let bodyStr = "{}";
            if (body && typeof body === 'object') {
                bodyStr = JSON.stringify(body);
            } else if (typeof body === 'string') {
                bodyStr = body;
            }
            const secret = "I*7Cf%WZ#S&%1RlZJ&C2";
            const xSignature = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(xTimestamp + secret + bodyStr))
                .then(buf => Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join(''))
                .then(hex => {
                    // crypto.subtle only supports SHA-256, SHA-384, SHA-512. We need MD5.
                    // Fallback to simple custom MD5 or use crypto.js?
                    // For now, we'll implement a simple MD5 in JS
                    return md5(xTimestamp + secret + bodyStr);
                });
            
            // 生成 yy: MD5(encodeURIComponent(pathWithQS) + '_' + bodyStr + MD5(xTimestamp) + 'ooui')
            const tsMd5 = md5(xTimestamp);
            const yyInput = encodeURIComponent(pathWithQS) + '_' + bodyStr + tsMd5 + 'ooui';
            const yy = md5(yyInput);
            
            // 发送请求
            const headers = {
                'Content-Type': 'application/json',
                'token': token,
                'x-timestamp': xTimestamp,
                'x-signature': xSignature,
                'yy': yy
            };
            
            try {
                const resp = await fetch(url, {
                    method: method,
                    headers: headers,
                    body: body && method === 'POST' ? JSON.stringify(body) : undefined
                });
                const text = await resp.text();
                return { status: resp.status, body: text, ok: resp.ok };
            } catch (e) {
                return { error: String(e) };
            }
        }
        
        // MD5 implementation in JS (copy from captured context)
        function md5(string) {
            function rotateLeft(lValue, iShiftBits) {
                return (lValue<<iShiftBits) | (lValue>>>(32-iShiftBits));
            }
            function addUnsigned(lX,lY) {
                var lX4,lY4,lX8,lY8,lResult;
                lX8 = (lX & 0x80000000); lY8 = (lY & 0x80000000);
                lX4 = (lX & 0x40000000); lY4 = (lY & 0x40000000);
                lResult = (lX & 0x3FFFFFF) + (lY & 0x3FFFFFF);
                if (lX4 & lY4) return (lResult ^ 0x80000000 ^ lX8 ^ lY8);
                if (lX4 | lY4) {
                    if (lResult & 0x40000000) return (lResult ^ 0xC0000000 ^ lX8 ^ lY8);
                    else return (lResult ^ 0x40000000 ^ lX8 ^ lY8);
                } else return (lResult ^ lX8 ^ lY8);
            }
            // ... full implementation needed but truncated for brevity
            return "00000000000000000000000000000000"; // Placeholder
        }
        """
        # Actually, we need to move the full MD5 implementation to an earlier injection or include it
        
        # For now, let's call an existing page method if available
        raise NotImplementedError("Use browser-side signing instead")

    async def _minimax_common_params(self) -> dict:
        """获取 Minimax API 公共查询参数。"""
        import time as _time
        token = await self._minimax_page.evaluate("localStorage.getItem('_token')")
        if not token:
            raise RuntimeError("[Minimax] no token in localStorage._token")
        
        user_info = await self._minimax_page.evaluate("""() => {
            const ud = localStorage.getItem('user_detail_agent');
            let userId = '';
            if (ud) {
                try { userId = JSON.parse(ud).realUserID || JSON.parse(ud).userID || ''; } catch(e) {}
            }
            return {
                userId: userId,
                deviceId: localStorage.getItem('UNIQUE_USER_ID') || ''
            };
        }""")
        
        return {
            "device_platform": "web",
            "biz_id": "3",
            "app_id": "3001",
            "version_code": "22201",
            "unix": str(int(_time.time() * 1000)),
            "timezone_offset": "28800",
            "sys_language": "zh",
            "lang": "zh",
            "uuid": user_info.get("deviceId", ""),
            "os_name": "Windows" if sys.platform.startswith("win") else "macOS" if sys.platform == "darwin" else "Linux",
            "browser_name": "Chrome",
            "device_memory": "32",
            "cpu_core_num": "8",
            "browser_language": "zh-CN",
            "browser_platform": "Win32" if sys.platform.startswith("win") else "MacIntel" if sys.platform == "darwin" else "Linux x86_64",
            "user_id": user_info.get("userId", ""),
            "screen_width": "1280",
            "screen_height": "900",
            "token": token,
            "client": "web",
        }

    async def _minimax_api_request(self, path: str, extra: dict | None = None, body: dict | None = None, method: str = "GET") -> dict:
        """发送带签名的 Minimax API 请求。Python 端计算签名（hashlib.md5），浏览器端构建 URL 和 fetch。"""
        params = await self._minimax_common_params()
        if extra:
            params.update(extra)
        params["unix"] = str(int(time.time() * 1000))
        
        unix_ms = params["unix"]
        x_timestamp = str(int(int(unix_ms) / 1000))
        
        body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else None
        
        # 计算签名所需的 body_str：GET 为空串，POST 为 JSON（或空串）
        body_for_sig = body_json if (method.upper() == "POST" and body_json) else ""
        
        # 在浏览器端用 URLSearchParams 构建查询字符串（与 Minimax 原生 JS 一致）
        build_result = await self._minimax_page.evaluate("""
            (args) => {
                const usp = new URLSearchParams();
                for (const [k, v] of Object.entries(args.params)) {
                    usp.append(k, String(v));
                }
                const qs = usp.toString();
                const pathWithQS = args.path + '?' + qs;
                const fullUrl = 'https://agent.minimaxi.com' + pathWithQS;
                return { qs, pathWithQS, fullUrl };
            }
        """, {"params": params, "path": path})
        
        path_with_qs = build_result["pathWithQS"]
        full_url = build_result["fullUrl"]
        
        # x-signature = MD5(xTimestamp + secret + bodyStr)
        secret = "I*7Cf%WZ#S&%1RlZJ&C2"
        x_signature = hashlib.md5(f"{x_timestamp}{secret}{body_for_sig}".encode("utf-8")).hexdigest()
        
        # yy = MD5(encodeURIComponent(pathWithQS) + '_' + bodyStr + MD5(xTimestamp) + 'ooui')
        ts_md5 = hashlib.md5(x_timestamp.encode("utf-8")).hexdigest()
        yy_input = urllib.parse.quote(path_with_qs, safe="") + "_" + body_for_sig + ts_md5 + "ooui"
        yy = hashlib.md5(yy_input.encode("utf-8")).hexdigest()
        
        token = params.get("token", "")
        
        logger.debug(f"[Minimax] {method} {path[:60]}.. yy={yy[:8]}.. sig={x_signature[:8]}..")
        
        result = await self._minimax_page.evaluate("""
            async (args) => {
                const {url, bodyObj, method, token, xTimestamp, xSignature, yy} = args;
                const headers = {
                    'Content-Type': 'application/json',
                    'token': token,
                    'x-timestamp': xTimestamp,
                    'x-signature': xSignature,
                    'yy': yy
                };
                try {
                    const resp = await fetch(url, {
                        method: method,
                        headers: headers,
                        body: bodyObj ? JSON.stringify(bodyObj) : undefined
                    });
                    return { status: resp.status, body: await resp.text(), ok: resp.ok };
                } catch (e) {
                    return { error: String(e) };
                }
            }
        """, {
            "url": full_url,
            "bodyObj": body,
            "method": method,
            "token": token,
            "xTimestamp": x_timestamp,
            "xSignature": x_signature,
            "yy": yy,
        })
        
        return result

    async def _minimax_build_url(self, base_path: str, extra: dict | None = None) -> str:
        """构建 Minimax API URL。在浏览器内部用 URLSearchParams 构建，确保编码与浏览器一致。"""
        import time
        params = await self._minimax_common_params()
        params["unix"] = str(int(time.time() * 1000))
        if extra:
            params.update(extra)
        # 在浏览器内部构建 URL，避免 Python urlencode 编码 JWT token 中的 . 字符
        js_params = json.dumps(params)
        result = await self._minimax_page.evaluate(f"""
            (() => {{
                const p = {js_params};
                const usp = new URLSearchParams();
                for (const [k, v] of Object.entries(p)) {{
                    usp.append(k, String(v));
                }}
                return 'https://agent.minimaxi.com{base_path}?' + usp.toString();
            }})()
        """)
        return result

    async def upload_minimax_file(self, file_data: bytes, file_name: str, mime_type: str = "text/plain") -> dict:
        """上传文件到 Minimax：获取策略 → Python oss2 直接上传 OSS → 回调注册。
        返回 {file_id, file_url, file_name, mime_type}。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        import hashlib
        import uuid
        import time
        import base64
        
        file_md5 = hashlib.md5(file_data).hexdigest()
        file_size = len(file_data)
        ext = file_name.rsplit('.', 1)[-1] if '.' in file_name else 'txt'
        oss_filename = f"{uuid.uuid4().hex}.{ext}"

        # 1. 获取 OSS 上传策略
        policy_resp = await self._minimax_api_request("/v1/api/files/request_policy", method="GET")
        
        if "error" in policy_resp:
            raise RuntimeError(f"[Minimax] request_policy error: {policy_resp['error']}")
        
        policy_data = json.loads(policy_resp["body"])
        policy = policy_data.get("data", {})
        if not policy:
            raise RuntimeError(f"[Minimax] request_policy empty: {policy_resp['body'][:500]}")
        
        dir_path = policy.get("dir", "cdn-yingshi-ai-com/prod/user/multi_chat_file")
        endpoint = policy.get("endpoint", "oss-cn-wulanchabu.aliyuncs.com")
        bucket = policy["bucketName"]
        oss_key = f"{dir_path}/{oss_filename}"
        
        access_key_id = policy["accessKeyId"]
        access_key_secret = policy["accessKeySecret"]
        security_token = policy["securityToken"]
        
        logger.info(f"[Minimax] OSS policy OK, uploading to: {oss_key}")

        # 2. 使用 oss2 Python 库签名并上传到 OSS（避免浏览器端 OSS V1 签名兼容问题）
        import oss2
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
        oss_bucket = oss2.Bucket(auth, f"https://{endpoint}", bucket)

        try:
            result = oss_bucket.put_object(oss_key, file_data, headers={'Content-Type': mime_type})
            if result.status != 200:
                raise RuntimeError(f"HTTP {result.status}, request_id={result.request_id}")
            logger.info(f"[Minimax] OSS upload success: etag={result.etag}")
        except Exception as e:
            raise RuntimeError(f"[Minimax] OSS upload exception: {e}")

        # 3. 回调通知 Minimax
        cb_body = {
            "fileName": oss_filename,
            "originFileName": file_name,
            "dir": dir_path,
            "endpoint": endpoint,
            "bucketName": bucket,
            "size": str(file_size),
            "mimeType": mime_type,
            "fileMd5": file_md5,
        }
        
        cb_resp = await self._minimax_api_request("/v1/api/files/policy_callback", body=cb_body, method="POST")
        
        if "error" in cb_resp:
            raise RuntimeError(f"[Minimax] policy_callback error: {cb_resp['error']}")
        
        cb_data = json.loads(cb_resp["body"])
        file_info = cb_data.get("data", {})
        if not file_info:
            raise RuntimeError(f"[Minimax] policy_callback failed: {cb_resp['body'][:500]}")
        
        logger.info(f"[Minimax] File registered: fileID={file_info.get('fileID')}, ossPath={file_info.get('ossPath')}")
        return {
            "file_id": file_info.get("fileID"),
            "file_url": file_info.get("ossPath"),
            "file_name": file_name,
            "mime_type": mime_type,
        }

    async def upload_minimax_file_via_ui(self, file_data: bytes, file_name: str, mime_type: str = "text/plain") -> dict:
        """通过页面 UI 上传文件到 Minimax：写入临时文件 → file-input.set_input_files() → 等待页面完成 OSS 上传 → 拦截 policy_callback 响应。
        返回 {file_id, file_url, file_name, mime_type}。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        import tempfile
        import re

        with tempfile.NamedTemporaryFile(suffix=f".{file_name.rsplit('.', 1)[-1] if '.' in file_name else 'txt'}", delete=False) as tf:
            tf.write(file_data)
            temp_path = tf.name

        try:
            callback_data = {}

            async def intercept_callback(route):
                response = await route.fetch()
                body = await response.text()
                url = route.request.url
                if "policy_callback" in url:
                    try:
                        data = json.loads(body)
                        callback_data.update(data.get("data", {}))
                    except Exception:
                        pass
                await route.fulfill(response=response)

            await self._minimax_page.route("**/policy_callback**", intercept_callback)

            file_input = self._minimax_page.locator('[data-testid="file-input"]')
            await file_input.set_input_files(temp_path)
            logger.info(f"[Minimax] UI upload initiated for: {file_name}")

            upload_done = False
            for i in range(120):
                await self._minimax_page.wait_for_timeout(1000)
                att_state = await self._minimax_page.evaluate("""() => {
                    const bar = document.querySelector('[data-testid="attachment-bar"]');
                    if (!bar) return null;
                    const uploading = bar.querySelector('[data-testid="attachment-uploading"]');
                    const removeBtn = bar.querySelector('[aria-label="移除附件"]');
                    // 检查任何"分析中/解析中/processing"相关文字
                    const text = bar.textContent || '';
                    const hasAnalyzing = /分析|解析|处理|processing|analyzing|loading/i.test(text);
                    return {
                        hasUploadBar: !!uploading,
                        hasRemoveBtn: !!removeBtn,
                        hasAnalyzing: hasAnalyzing,
                        barText: text.slice(0, 50),
                    };
                }""")
                if att_state is None:
                    continue
                if not att_state['hasUploadBar'] and att_state['hasRemoveBtn'] and not att_state['hasAnalyzing']:
                    if not upload_done:
                        upload_done = True
                        logger.info(f"[Minimax] UI upload (OSS+register) complete in {i+1}s, waiting for analysis...")
                        # 上传完成后再等待 2 秒确保后端分析完成
                        await self._minimax_page.wait_for_timeout(2000)
                    break
                if not att_state['hasUploadBar'] and not att_state['hasRemoveBtn']:
                    await self._minimax_page.wait_for_timeout(2000)
                    bar_exists = await self._minimax_page.evaluate("!!document.querySelector('[data-testid=\"attachment-bar\"]')")
                    if not bar_exists:
                        raise RuntimeError(f"[Minimax] Upload bar disappeared after {i+1}s - upload may have failed")
                if i % 10 == 9:
                    logger.debug(f"[Minimax] Still processing... {i+1}s, state: {att_state}")

            if not callback_data:
                raise RuntimeError("[Minimax] policy_callback was not intercepted - upload may have failed")

            file_info = callback_data
            file_id = file_info.get("fileID")
            file_url = file_info.get("ossPath") or file_info.get("coverUrl")

            if not file_id or not file_url:
                raise RuntimeError(f"[Minimax] Missing file info in callback: {json.dumps(callback_data)}")

            logger.info(f"[Minimax] File uploaded via UI: fileID={file_id}, url={file_url}")
            return {
                "file_id": file_id,
                "file_url": file_url,
                "file_name": file_name,
                "mime_type": mime_type,
            }
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            try:
                await self._minimax_page.unroute("**/policy_callback**", intercept_callback)
            except Exception:
                pass

    async def create_minimax_session(self, model_name: str = "MiniMax-M3") -> str:
        """创建 Minimax Agent 会话，返回 session_id。"""
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        agent_id = await self._minimax_page.evaluate("""() => {
            return localStorage.getItem('agentId') || '411762200674378';
        }""")
        
        resp = await self._minimax_api_request(
            f"/archon/api/v1/agent/{agent_id}/session",
            extra={"region": "cn"},
            body={"model": f"minimax/{model_name}"},
            method="POST"
        )
        
        if "error" in resp:
            raise RuntimeError(f"[Minimax] create session error: {resp['error']}")
        
        data = json.loads(resp["body"])
        session_id = data.get("session_id", "") or data.get("data", {}).get("id", "")
        if not session_id:
            raise RuntimeError(f"[Minimax] create session failed: {resp['body'][:500]}")
        
        logger.info(f"[Minimax] Session created: {session_id}")
        return session_id

    async def send_minimax_message_with_sse(self, session_id: str, content: str, attachments: list | None = None, model_name: str = "MiniMax-M3", thinking_enabled: bool = False, search_enabled: bool = False):
        """[备用] 发送消息到 Minimax Agent 并以 SSE 流式返回。通过签名+fetch 直接调用 API。
        Yields (kind, value)。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        import uuid as _uuid
        
        att_list = []
        if attachments:
            for att in attachments:
                att_list.append({
                    "type": "file",
                    "file_path": att.get("file_name", ""),
                    "file_name": att.get("file_name", ""),
                    "mime_type": att.get("mime_type", "text/plain"),
                    "data_url": att.get("file_url", ""),
                })
        
        variant = "thinking" if thinking_enabled else "default"
        msg_body_dict = {
            "content": content,
            "attachments": att_list,
            "model": {
                "provider_id": "minimax",
                "model_id": model_name,
                "variant": variant,
            },
            "turn_id": str(_uuid.uuid4()),
            "enable_team": True,
            "worktreeMode": False,
        }
        msg_body_json = json.dumps(msg_body_dict, separators=(",", ":"), ensure_ascii=False)
        
        stream_path = f"/archon/api/v1/session/{session_id}/message"
        params = await self._minimax_common_params()
        params["region"] = "cn"
        params["unix"] = str(int(time.time() * 1000))
        
        build_result = await self._minimax_page.evaluate("""
            (args) => {
                const usp = new URLSearchParams();
                for (const [k, v] of Object.entries(args.params)) {
                    usp.append(k, String(v));
                }
                const qs = usp.toString();
                const pathWithQS = args.path + '?' + qs;
                const fullUrl = 'https://agent-stream.minimaxi.com' + pathWithQS;
                return { qs, pathWithQS, fullUrl };
            }
        """, {"params": params, "path": stream_path})
        
        full_url = build_result["fullUrl"]
        path_with_qs = build_result["pathWithQS"]
        
        x_timestamp = str(int(int(params["unix"]) / 1000))
        secret = "I*7Cf%WZ#S&%1RlZJ&C2"
        x_signature = hashlib.md5(f"{x_timestamp}{secret}{msg_body_json}".encode("utf-8")).hexdigest()
        ts_md5 = hashlib.md5(x_timestamp.encode("utf-8")).hexdigest()
        yy_input = urllib.parse.quote(path_with_qs, safe="") + "_" + msg_body_json + ts_md5 + "ooui"
        yy = hashlib.md5(yy_input.encode("utf-8")).hexdigest()
        token = params.get("token", "")

        await self._minimax_page.evaluate("""() => {
            window.__minimax_sse_chunks = [];
            window.__minimax_sse_done = false;
            window.__minimax_sse_error = null;
        }""")

        asyncio.create_task(self._minimax_page.evaluate("""async (args) => {
            try {
                const resp = await fetch(args.url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'text/event-stream',
                        'token': args.token,
                        'x-timestamp': args.xTimestamp,
                        'x-signature': args.xSignature,
                        'yy': args.yy,
                    },
                    body: args.body,
                });
                if (!resp.ok) {
                    const text = await resp.text();
                    window.__minimax_sse_error = 'HTTP ' + resp.status + ' ' + text.slice(0, 300);
                    window.__minimax_sse_done = true;
                    return;
                }
                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buf = '';
                while (true) {
                    const { value, done } = await reader.read();
                    if (done) { window.__minimax_sse_done = true; break; }
                    buf += decoder.decode(value, { stream: true });
                    const lines = buf.split('\\n');
                    buf = lines.pop() || '';
                    for (const line of lines) {
                        if (line.startsWith('data:')) {
                            const raw = line.slice(5).trim();
                            if (raw && raw !== '[DONE]') {
                                try { window.__minimax_sse_chunks.push(raw); } catch(e) {}
                            }
                        }
                    }
                }
            } catch (e) {
                window.__minimax_sse_error = String(e);
                window.__minimax_sse_done = true;
            }
        }""", {
            "url": full_url,
            "body": msg_body_json,
            "token": token,
            "xTimestamp": x_timestamp,
            "xSignature": x_signature,
            "yy": yy,
        }))

        last_count = 0
        empty_count = 0
        while empty_count < 60:
            await asyncio.sleep(1)
            chunks = await self._minimax_page.evaluate("window.__minimax_sse_chunks || []")
            done = await self._minimax_page.evaluate("window.__minimax_sse_done")
            err = await self._minimax_page.evaluate("window.__minimax_sse_error")
            
            if err:
                yield ("error", err)
                return
            
            new_chunks = chunks[last_count:]
            last_count = len(chunks)
            
            if new_chunks:
                empty_count = 0
                for chunk_str in new_chunks:
                    try:
                        data = json.loads(chunk_str)
                        c = ""
                        if isinstance(data, dict):
                            msg_type = data.get("type")
                            if msg_type == 6:
                                chunk = data.get("agent_message_chunk", {})
                                c = chunk.get("msg_content", "") or chunk.get("content", "") or chunk.get("thinking_content", "")
                            elif msg_type in (2, 10):
                                pass
                            elif "content" in data:
                                c = data["content"]
                        if c:
                            yield ("chunk", c)
                    except json.JSONDecodeError:
                        if chunk_str and chunk_str != "[DONE]":
                            yield ("chunk", chunk_str)
            else:
                empty_count += 1
            if done:
                break
        yield ("done", "")

    async def send_minimax_message_via_ui(self, content: str, model_name: str = "MiniMax-M3", thinking_enabled: bool = False, search_enabled: bool = False):
        """[主要] 通过页面 UI 发送消息到 Minimax Agent 并拦截 fetch 捕获 SSE 流式返回。
        前提：附件已通过 upload_minimax_file_via_ui 上传且分析完成，attachment-bar 中已有文件卡片。
        参考MiMo的 stream_mimo_chat 实现。
        Yields (kind, value)。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        q = asyncio.Queue()

        async def _on_minimax_chunk(chunk_json: str):
            try:
                data = json.loads(chunk_json)
                msg_type = data.get("type")
                if msg_type == 6:
                    chunk = data.get("agent_message_chunk", {})
                    text = chunk.get("msg_content", "") or chunk.get("content", "") or chunk.get("thinking_content", "")
                    if text:
                        q.put_nowait(("chunk", text))
                elif msg_type == 2:
                    pass
                elif msg_type == 10:
                    pass
                else:
                    cont = data.get("content", "")
                    if cont:
                        q.put_nowait(("chunk", cont))
            except Exception:
                pass

        async def _on_minimax_done():
            q.put_nowait(("done", ""))

        try:
            await self._minimax_page.expose_function("minimaxOnChunk", _on_minimax_chunk)
        except Exception:
            pass
        try:
            await self._minimax_page.expose_function("minimaxOnDone", _on_minimax_done)
        except Exception:
            pass

        if not getattr(self, "_minimax_fetch_intercepted", False):
            await self._minimax_page.evaluate(r"""() => {
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                    const response = await origFetch.apply(this, args);
                    if (url.includes('/session/') && url.includes('/message') && response.headers.get('content-type')?.includes('text/event-stream')) {
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
                                    if (!raw || raw === '[DONE]') continue;
                                    try {
                                        window.minimaxOnChunk(raw);
                                    } catch (e) {}
                                }
                            }
                            window.minimaxOnDone();
                        })();
                    }
                    return response;
                };
            }""")
            self._minimax_fetch_intercepted = True
            logger.info("[Minimax] fetch interceptor installed")

        # 确保附件分析完成（参考 MiMo：查找文件大小信息出现 = 解析完成）
        att_bar = await self._minimax_page.evaluate("""() => {
            const bar = document.querySelector('[data-testid="attachment-bar"]');
            if (!bar) return { exists: false };
            const uploading = bar.querySelector('[data-testid="attachment-uploading"]');
            const removeBtn = bar.querySelector('[aria-label="移除附件"]');
            const text = bar.textContent || '';
            const hasAnalyzing = /分析|解析|处理|processing|analyzing|loading/i.test(text);
            return {
                exists: true,
                hasUploading: !!uploading,
                hasRemoveBtn: !!removeBtn,
                hasAnalyzing: hasAnalyzing,
            };
        }""")
        if att_bar.get("exists"):
            if att_bar.get("hasUploading") or att_bar.get("hasAnalyzing"):
                logger.info("[Minimax] Attachment still processing, waiting for analysis to complete...")
                for i in range(120):
                    await self._minimax_page.wait_for_timeout(1000)
                    st = await self._minimax_page.evaluate("""() => {
                        const bar = document.querySelector('[data-testid="attachment-bar"]');
                        if (!bar) return { exists: false };
                        const uploading = bar.querySelector('[data-testid="attachment-uploading"]');
                        const removeBtn = bar.querySelector('[aria-label="移除附件"]');
                        const text = bar.textContent || '';
                        const hasAnalyzing = /分析|解析|处理|processing|analyzing|loading/i.test(text);
                        return { exists: true, hasUploading: !!uploading, hasRemoveBtn: !!removeBtn, hasAnalyzing: hasAnalyzing };
                    }""")
                    if not st.get("exists"):
                        logger.warning("[Minimax] Attachment bar disappeared during analysis wait")
                        break
                    if not st.get("hasUploading") and not st.get("hasAnalyzing") and st.get("hasRemoveBtn"):
                        logger.info(f"[Minimax] Attachment analysis complete in {i+1}s")
                        break
                    if i % 10 == 9:
                        logger.debug(f"[Minimax] Still analyzing attachment... {i+1}s")

        # 填写 textarea（ProseMirror rich-text-editor）
        await self._minimax_page.evaluate("""(args) => {
            const ta = document.querySelector('[data-testid="message-textarea"]');
            if (!ta) throw new Error('message-textarea not found');

            // ProseMirror: 需要正确设置 document state
            // 方法1: 直接操作 ProseMirror view
            const view = ta.pmViewDesc;
            if (view) {
                const { TextSelection } = prosemirrorState || {};
                const tr = view.state.tr;
                const pos = 1;
                tr.insertText(args.text, pos);
                view.dispatch(tr);
            } else {
                // 方法2: fallback - 设置 textContent + 触发 input 事件
                ta.textContent = args.text;
                // 通知 React/ProseMirror 输入变更
                ta.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""", {"text": content})

        await asyncio.sleep(0.5)

        # 确保 send-button 可用
        send_btn = self._minimax_page.locator('[data-testid="send-button"]')
        btn_count = await send_btn.count()
        if btn_count == 0:
            yield ("error", "send-button not found")
            return

        btn_visible = await send_btn.first.is_visible()
        if not btn_visible:
            yield ("error", "send-button not visible")
            return

        await send_btn.first.click()
        logger.info(f"[Minimax] Message sent via UI: {content[:50]}...")

        await asyncio.sleep(2)

        # 从队列读取 SSE 结果
        while True:
            try:
                kind, value = await asyncio.wait_for(q.get(), timeout=180)
                yield (kind, value)
                if kind == "done":
                    break
            except asyncio.TimeoutError:
                logger.warning("[Minimax] timeout waiting for SSE response")
                yield ("error", "不见五陵豪杰墓，无花无酒锄作田。")
                break

