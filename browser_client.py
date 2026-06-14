import os
import json
import asyncio
import logging
import uuid
import httpx

from config import CONFIG, USER_AGENT, BASE_DIR

logger = logging.getLogger("doubao-browser")

STORAGE_STATE_PATH = os.path.join(BASE_DIR, "storage_state.json")


def _get_latest_cookie_from_storage() -> str:
    """从 storage_state.json 读取最新 cookie 字符串"""
    try:
        if not os.path.exists(STORAGE_STATE_PATH):
            return CONFIG.get('cookie', '')
        with open(STORAGE_STATE_PATH, 'r', encoding='utf-8') as f:
            state = json.load(f)
        cookies = state.get('cookies', [])
        cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies if 'doubao.com' in c.get('domain', ''))
        return cookie_str if cookie_str else CONFIG.get('cookie', '')
    except Exception:
        return CONFIG.get('cookie', '')
COMPLETION_URL_BASE = "https://www.doubao.com/chat/completion"


def _build_completion_url():
    """构造带静态参数的 completion URL，SDK 会自动补充 msToken/a_bogus"""
    params = [
        "aid=497858",
        f"device_id={CONFIG.get('device_id', '')}",
        "device_platform=web",
        f"fp={CONFIG.get('fp', '')}",
        "language=zh",
        "pc_version=3.22.0",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={CONFIG.get('tea_uuid', '')}",
        "use-olympus-account=1",
        "version_code=20800",
        f"web_id={CONFIG.get('web_id', '')}",
        "web_platform=browser",
        f"web_tab_id={uuid.uuid4()}",
    ]
    return COMPLETION_URL_BASE + "?" + "&".join(params)


class BrowserClient:
    def __init__(self):
        # Doubao 专属
        self._doubao_pw = None
        self._doubao_browser = None
        self._doubao_context = None
        self._doubao_page = None
        self._doubao_lock = asyncio.Lock()
        self._doubao_queues = {}

        # Qianwen 专属
        self._qianwen_pw = None
        self._qianwen_browser = None
        self._qianwen_context = None
        self._qianwen_page = None
        self._qianwen_lock = asyncio.Lock()
        self._qianwen_queues = {}

    def _on_doubao_push(self, stream_id: str, kind: str, value):
        q = self._doubao_queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    def _on_qianwen_push(self, stream_id: str, kind: str, value):
        q = self._qianwen_queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    async def ensure_doubao_ready(self, headless=True):
        """确保 Doubao 浏览器就绪，按需启动独立浏览器实例。"""
        if self._doubao_page and self._doubao_browser and self._doubao_browser.is_connected():
            return True
        async with self._doubao_lock:
            if self._doubao_page and self._doubao_browser and self._doubao_browser.is_connected():
                return True

            if not os.path.exists(STORAGE_STATE_PATH):
                raise RuntimeError("storage_state.json 不存在，请先运行 python main.py --login doubao 登录")

            from playwright.async_api import async_playwright
            self._doubao_pw = await async_playwright().start()
            self._doubao_browser = await self._doubao_pw.chromium.launch(
                headless=headless,
                channel="msedge",
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )

            self._doubao_context = await self._doubao_browser.new_context(
                storage_state=STORAGE_STATE_PATH,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._doubao_page = await self._doubao_context.new_page()
            await self._doubao_page.expose_function("__sse_push", self._on_doubao_push)

            logger.info("Doubao: navigating to doubao.com/chat/ ...")
            await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
            await asyncio.sleep(2)

            try:
                await self._doubao_page.wait_for_function(
                    "() => typeof window.bdms?.frontierSign === 'function'",
                    timeout=30000
                )
                logger.info("Doubao: bdms.frontierSign SDK ready")
            except Exception as e:
                logger.warning(f"Doubao: bdms.frontierSign not available: {e}")

            body_text = await self._doubao_page.text_content("body") or ""
            if any(kw in body_text for kw in ["登录", "请先登录", "扫码登录"]):
                logger.warning("Doubao: login required - session cookies expired. Opening visible browser...")
                await self._doubao_login_recovery()

            logger.info("Doubao browser ready")
            return True

    async def _doubao_login_recovery(self):
        """打开可见浏览器让用户手动登录 Doubao，然后保存 cookies。"""
        from playwright.async_api import async_playwright
        try:
            pw = await async_playwright().start()
            login_browser = await pw.chromium.launch(
                headless=False,
                channel="msedge",
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            login_context = await login_browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            login_page = await login_context.new_page()
            await login_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
            logger.info("Doubao: visible browser opened for manual login. Please log in...")

            while True:
                await asyncio.sleep(1)
                if not login_browser.is_connected():
                    break
                try:
                    body = await login_page.text_content("body") or ""
                    if "登录" not in body and "请先登录" not in body:
                        logger.info("Doubao: login detected, capturing cookies...")
                        break
                except:
                    pass

            await login_context.storage_state(path=STORAGE_STATE_PATH)
            logger.info(f"Doubao: storage_state saved to {STORAGE_STATE_PATH}")

            await login_browser.close()
            await pw.stop()

            await self._doubao_page.close()
            await self._doubao_context.close()
            self._doubao_context = await self._doubao_browser.new_context(
                storage_state=STORAGE_STATE_PATH,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._doubao_page = await self._doubao_context.new_page()
            await self._doubao_page.expose_function("__sse_push", self._on_doubao_push)
            await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Doubao login recovery failed: {e}")
            raise

    async def ensure_qianwen_ready(self, headless=True):
        """确保 Qianwen 浏览器就绪，按需启动独立浏览器实例。"""
        if self._qianwen_page and self._qianwen_browser and self._qianwen_browser.is_connected():
            return True
        async with self._qianwen_lock:
            if self._qianwen_page and self._qianwen_browser and self._qianwen_browser.is_connected():
                return True

            qianwen_state = os.path.join(BASE_DIR, "qianwen_storage_state.json")
            ctx_kwargs = {
                "user_agent": USER_AGENT,
                "viewport": {"width": 1280, "height": 900},
            }
            if os.path.exists(qianwen_state):
                try:
                    ctx_kwargs["storage_state"] = qianwen_state
                    logger.info("Qianwen: loading saved storage_state")
                except Exception as e:
                    logger.warning(f"Failed to load qianwen storage_state: {e}")
            else:
                qianwen_cookie = CONFIG.get("qianwen_cookie", "")
                if qianwen_cookie and "qianwen" in qianwen_cookie.lower():
                    logger.info("Qianwen: will inject cookies from config")

            from playwright.async_api import async_playwright
            self._qianwen_pw = await async_playwright().start()
            self._qianwen_browser = await self._qianwen_pw.chromium.launch(
                headless=headless,
                channel="msedge",
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )

            try:
                self._qianwen_context = await self._qianwen_browser.new_context(**ctx_kwargs)
            except Exception as e:
                logger.warning(f"Failed to create qianwen context: {e}")
                self._qianwen_context = await self._qianwen_browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                )
            self._qianwen_page = await self._qianwen_context.new_page()
            await self._qianwen_page.expose_function("__sse_push", self._on_qianwen_push)
            logger.info("Qianwen: navigating to qianwen.com ...")
            await self._qianwen_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
            await asyncio.sleep(3)

            body_text = await self._qianwen_page.text_content("body") or ""
            if any(kw in body_text for kw in ["扫码登录", "手机号登录", "账号登录", "登录/注册"]):
                logger.warning("Qianwen: login required - session cookies expired. Opening visible browser...")
                try:
                    pw = await async_playwright().start()
                    login_browser = await pw.chromium.launch(
                        headless=False,
                        channel="msedge",
                        args=["--no-sandbox", "--disable-setuid-sandbox"]
                    )
                    login_context = await login_browser.new_context(
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                    )
                    login_page = await login_context.new_page()
                    await login_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
                    logger.info("Qianwen: visible browser opened for manual login. Please log in...")

                    while True:
                        await asyncio.sleep(1)
                        if not login_browser.is_connected():
                            break
                        try:
                            body = await login_page.text_content("body") or ""
                            if not any(kw in body for kw in ["扫码登录", "手机号登录", "账号登录", "登录/注册"]):
                                logger.info("Qianwen: login detected, capturing cookies...")
                                break
                        except:
                            pass

                    cookies = await login_context.cookies()
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    config = CONFIG.copy()
                    config["qianwen_cookie"] = cookie_str
                    from config import CONFIG_PATH
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(config, f, ensure_ascii=False, indent=4)

                    try:
                        await login_context.storage_state(path=qianwen_state)
                        logger.info(f"Storage state saved to {qianwen_state}")
                    except:
                        pass

                    await login_browser.close()
                    await pw.stop()
                    logger.info("Qianwen: login browser closed, server will use the captured cookies")

                    await self._qianwen_page.close()
                    await self._qianwen_context.close()
                    self._qianwen_context = await self._qianwen_browser.new_context(
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                        storage_state=qianwen_state,
                    )
                    self._qianwen_page = await self._qianwen_context.new_page()
                    await self._qianwen_page.expose_function("__sse_push", self._on_qianwen_push)
                    await self._qianwen_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.error(f"Qianwen login recovery failed: {e}")
                    raise
            else:
                logger.info("Qianwen page ready")
            return True

    async def stream_qianwen_chat(self, messages: list, session_id: str, topic_id: str):
        """Route interception for qianwen API response + DOM typing."""
        headless = CONFIG.get('_qianwen_headless', CONFIG.get('_headless_browser', True))
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
            ok = await self._qianwen_page.evaluate("""() => {
                const ed = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                if (ed) { ed.focus(); ed.click(); return true; }
                return false;
            }""")
            if not ok:
                yield ("error", "No editor")
                yield ("done", "")
                return
            # 聚焦编辑器后逐字输入（\n 用 Shift+Enter 避免提前提交）
            for char in user_text:
                if char == "\n":
                    await self._qianwen_page.keyboard.press("Shift+Enter")
                else:
                    await self._qianwen_page.keyboard.type(char, delay=5)
            await asyncio.sleep(0.3)
            await self._qianwen_page.keyboard.press("Enter")
            logger.info("[Qwen] typed + Enter")
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
            yield ("error", "Timeout")
            yield ("done", "")
        finally:
            self._qianwen_queues.pop(stream_id, None)
            await self._qianwen_page.unroute("**/api/v2/chat**", handle_route)

    async def get_user_info(self) -> dict:
        headless = CONFIG.get('_doubao_headless', CONFIG.get('_headless_browser', True))
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
        headless = CONFIG.get('_doubao_headless', CONFIG.get('_headless_browser', True))
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
            eval_task = asyncio.create_task(
                self._doubao_page.evaluate(js, {
                    "body": body,
                    "traceId": trace_id,
                    "spanId": span_id,
                    "streamId": stream_id,
                    "deviceId": CONFIG.get('device_id', ''),
                    "webId": CONFIG.get('web_id', ''),
                    "teaUuid": CONFIG.get('tea_uuid', ''),
                    "fp": CONFIG.get('fp', ''),
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

    async def delete_all_qianwen_conversations(self):
        """删除千问网页版所有历史对话（httpx 直接调用，不依赖浏览器页面）。"""
        try:
            qianwen_cookie = ""
            if self._qianwen_context:
                try:
                    cookies = await self._qianwen_context.cookies()
                    qianwen_cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                except Exception:
                    pass
            if not qianwen_cookie:
                qianwen_cookie = CONFIG.get("qianwen_cookie", "")
            if not qianwen_cookie:
                logger.warning("[Qwen] no cookie for delete API, skip")
                return

            import httpx
            ut = ""
            for part in qianwen_cookie.split("; "):
                if part.startswith("b-user-id="):
                    ut = part.split("=", 1)[1].strip()
                    break
            if not ut:
                qianwen_state = os.path.join(BASE_DIR, "qianwen_storage_state.json")
                if os.path.exists(qianwen_state):
                    try:
                        with open(qianwen_state, 'r', encoding='utf-8') as f:
                            for c in json.load(f).get("cookies", []):
                                if c.get("name") == "b-user-id":
                                    ut = c.get("value", "")
                                    break
                    except Exception:
                        pass
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

            session_ids = []
            next_token = ""
            async with httpx.AsyncClient(timeout=30) as client:
                while True:
                    resp = await client.post(
                        "https://chat2-api.qianwen.com/api/v2/session/page/list",
                        headers=headers, params=query_params,
                        json={"next_token": next_token} if next_token else {},
                    )
                    data = resp.json()
                    items = data.get("data", {}).get("list", [])
                    for s in items:
                        sid = s.get("session_id", "")
                        if sid:
                            session_ids.append(sid)
                    if not data.get("data", {}).get("have_next_page", False):
                        break
                    next_token = data.get("data", {}).get("next_token", "")
                    if not next_token:
                        break

            if not session_ids:
                logger.info("[Qwen] no conversations to delete")
                return

            logger.info(f"[Qwen] deleting {len(session_ids)} conversations ...")
            async with httpx.AsyncClient(timeout=30) as client:
                batch_size = 20
                for i in range(0, len(session_ids), batch_size):
                    batch = session_ids[i:i + batch_size]
                    try:
                        resp = await client.post(
                            "https://chat2-api.qianwen.com/api/v1/session/delete/batch",
                            headers=headers, params=query_params,
                            json={"session_ids": batch},
                        )
                        result = resp.json()
                        if result.get("data", {}).get("delete_success"):
                            logger.info(f"[Qwen] deleted batch {i // batch_size + 1}: {len(batch)} sessions")
                        else:
                            logger.warning(f"[Qwen] delete batch failed: {json.dumps(result, ensure_ascii=False)[:300]}")
                    except Exception as e:
                        logger.warning(f"[Qwen] delete batch error: {e}")

            logger.info(f"[Qwen] finished deleting {len(session_ids)} conversations")
        except Exception as e:
            logger.warning(f"[Qwen] delete_all_conversations error: {e}")

    async def delete_qianwen_conversation(self, session_id: str):
        """删除单个千问对话。"""
        if not session_id:
            return
        try:
            qianwen_cookie = ""
            if self._qianwen_context:
                try:
                    cookies = await self._qianwen_context.cookies()
                    qianwen_cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                except Exception:
                    pass
            if not qianwen_cookie:
                qianwen_cookie = CONFIG.get("qianwen_cookie", "")
            if not qianwen_cookie:
                logger.warning("[Qwen] no cookie for delete API, skip")
                return

            import httpx
            ut = ""
            for part in qianwen_cookie.split("; "):
                if part.startswith("b-user-id="):
                    ut = part.split("=", 1)[1].strip()
                    break
            if not ut:
                qianwen_state = os.path.join(BASE_DIR, "qianwen_storage_state.json")
                if os.path.exists(qianwen_state):
                    try:
                        with open(qianwen_state, 'r', encoding='utf-8') as f:
                            for c in json.load(f).get("cookies", []):
                                if c.get("name") == "b-user-id":
                                    ut = c.get("value", "")
                                    break
                    except Exception:
                        pass
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
            if "/chat/" in url:
                sid = url.split("/chat/", 1)[1].split("?")[0].split("#")[0]
                if sid:
                    return sid
            return ""
        except Exception:
            return ""

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

    async def close(self):
        # 关闭 Doubao
        try:
            if self._doubao_browser:
                await self._doubao_browser.close()
            if self._doubao_pw:
                await self._doubao_pw.stop()
        except Exception:
            pass

        # 关闭 Qianwen
        try:
            if self._qianwen_browser:
                await self._qianwen_browser.close()
            if self._qianwen_pw:
                await self._qianwen_pw.stop()
        except Exception:
            pass

    async def delete_conversation_via_browser(self, conversation_id: str) -> tuple[bool, str]:
        """通过浏览器代理删除豆包对话，复用 storage_state.json 中的 cookie。
        新接口失败时降级到旧接口 /samantha/thread/delete。"""
        await self._doubao_lock.acquire()
        try:
            import base64
            import httpx
            from requests_aws4auth import AWS4Auth

            cookie = _get_latest_cookie_from_storage()
            if not cookie:
                return False, "No cookie available"

            device_id = CONFIG.get('device_id', '')
            web_id = CONFIG.get('web_id', '')
            tea_uuid = CONFIG.get('tea_uuid', '')

            headers = {
                'content-type': 'application/json; encoding=utf-8',
                'referer': 'https://www.doubao.com/chat/',
                'accept': 'application/json, text/plain, */*',
                'agw-js-conv': 'str',
            }

            params = "&".join([
                "version_code=20800",
                "language=zh",
                "device_platform=web",
                "aid=497858",
                f"real_aid=497858",
                "pkg_type=release_version",
                f"device_id={device_id}",
                "pc_version=3.22.1",
                f"web_id={web_id}",
                f"tea_uuid={tea_uuid}",
                "region=CN",
                "sys_region=CN",
                "samantha_web=1",
                "web_platform=browser",
                "use-olympus-account=1",
                f"web_tab_id={uuid.uuid4()}",
#{uuid.uuid4()}
            ])
            url = f"https://www.doubao.com/im/conversation/batch_del_user_conv?{params}"

            body = {
                "cmd": 4171,
                "uplink_body": {
                    "batch_delete_user_conversation_uplink_body": {
                        "conversation_id": [conversation_id],
                        "delete_all": False,
                        "conversation_type": 3,
                    }
                },
                "sequence_id": uuid.uuid4().hex,
                "channel": 2,
                "version": "1",
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, headers=headers, json=body)
                data = resp.json()

            result = data.get("downlink_body", {}).get(
                "batch_delete_user_conversation_downlink_body", {}
            ).get("result", {})

            if result.get(conversation_id) is True:
                logger.info(f"Deleted conversation {conversation_id} via browser")
                return True, ""

            return False, f"Server rejected: {json.dumps(data, ensure_ascii=False)[:300]}"
        finally:
            self._doubao_lock.release()

    async def upload_document_via_page(self, file_data: bytes, file_name: str) -> dict:
        """通过 httpx 从 storage_state.json 读取 cookie 执行文档上传，与浏览器代理同一会话。
        返回 attachment dict 用于 content_block (block_type 10052)。"""
        import base64
        import binascii
        from datetime import datetime
        
        # 保存上传的原始 messages 文件到 logs 目录供调试
        try:
            logs_dir = os.path.join(BASE_DIR, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_path = os.path.join(logs_dir, f"messages_{ts}.{file_name.rsplit('.', 1)[-1] if '.' in file_name else 'txt'}")
            with open(saved_path, "wb") as f:
                f.write(file_data)
            logger.info(f"Saved agent messages to {saved_path}")
        except Exception as e:
            logger.warning(f"Failed to save agent messages file: {e}")
        
        cookie = _get_latest_cookie_from_storage()
        if not cookie:
            raise RuntimeError("Cannot read cookie from storage_state.json")

        device_id = CONFIG.get('device_id', '')
        tea_uuid = CONFIG.get('tea_uuid', '')

        headers = {
            'content-type': 'application/json',
            'cookie': cookie,
            'origin': 'https://www.doubao.com',
            'referer': 'https://www.doubao.com/chat/',
            'user-agent': USER_AGENT,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            # Step 1: prepare_upload
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

            # Step 2: ApplyImageUpload
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

            # Step 3: Upload binary
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

            # Step 4: CommitImageUpload
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

            logger.info(f"Document uploaded via browser cookie: {file_name} ({file_size_final} bytes, uri={file_uri[:60]}...)")

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
    
    async def upload_file_via_qianwen_page(self, file_data: bytes, file_name: str) -> str:
        headless = CONFIG.get('_qianwen_headless', CONFIG.get('_headless_browser', True))
        await self.ensure_qianwen_ready(headless=headless)
        import tempfile, os
        tmp = None
        try:
            ext = f".{file_name.rsplit('.', 1)[-1]}" if '.' in file_name else ""
            fd, tmp = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, 'wb') as f:
                f.write(file_data)
        except Exception as e:
            logger.error(f"[Qwen] tmp: {e}"); raise
        try:
            page = self._qianwen_page
            fi = await page.query_selector("input[type='file']")
            if not fi:
                await page.evaluate("""() => {
                    const i = document.createElement('input');
                    i.type = 'file'; i.id = '__qfu';
                    i.style = 'position:fixed;top:0;left:0;opacity:0;z-index:99999';
                    document.body.appendChild(i);
                }""")
                await asyncio.sleep(0.3)
                fi = await page.query_selector("#__qfu")

            if not fi:
                raise RuntimeError("No file input")

            await fi.set_input_files(tmp)
            await page.evaluate("""() => {
                const i = document.getElementById('__qfu') || document.querySelector('input[type=file]');
                if(i) {
                    i.dispatchEvent(new Event('input', {bubbles:true}));
                    i.dispatchEvent(new Event('change', {bubbles:true}));
                }
            }""")
            logger.info(f"[Qwen] file input set + events dispatched: {file_name}")

            await asyncio.sleep(5)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            await page.evaluate("""() => {
                const el = document.querySelector('[contenteditable]')||document.querySelector('textarea');
                if(el) {el.focus();el.click();}
            }""")
            return file_name
        except Exception as e:
            logger.error(f"[Qwen] upload fail: {e}")
            raise
        finally:
            if tmp and os.path.exists(tmp):
                try: os.remove(tmp)
                except: pass

    async def fetch_qianwen_models(self) -> list[dict]:
        """从千问页面模型选择弹窗中获取可用模型列表。"""
        headless = CONFIG.get('_qianwen_headless', CONFIG.get('_headless_browser', True))
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
        headless = CONFIG.get('_qianwen_headless', CONFIG.get('_headless_browser', True))
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


browser_client = BrowserClient()


