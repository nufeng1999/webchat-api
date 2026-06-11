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
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._qianwen_page = None
        self._qianwen_context = None
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._queues = {}

    async def ensure_ready(self, headless: bool = True):
        if self._page and self._browser and self._browser.is_connected():
            return True
        async with self._init_lock:
            if self._page and self._browser and self._browser.is_connected():
                return True
            await self._launch(headless)
            return True

    async def _launch(self, headless=True):
        from playwright.async_api import async_playwright
        if not os.path.exists(STORAGE_STATE_PATH):
            raise RuntimeError("storage_state.json 不存在，请先运行 python main.py --login 登录")

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=headless,
            channel="msedge",
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        self._context = await self._browser.new_context(
            storage_state=STORAGE_STATE_PATH,
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        self._page = await self._context.new_page()

        await self._page.expose_function("__sse_push", self._on_push)

        logger.info("Browser client: navigating to doubao.com/chat/ ...")
        await self._page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
        await asyncio.sleep(2)

        # 等待 bdms.frontierSign 签名 SDK 加载完成
        try:
            await self._page.wait_for_function(
                "() => typeof window.bdms?.frontierSign === 'function'",
                timeout=30000
            )
            logger.info("bdms.frontierSign SDK ready")
        except Exception as e:
            logger.warning(f"bdms.frontierSign not available: {e}")

        logger.info("Browser client ready")

    def _on_push(self, stream_id: str, kind: str, value):
        q = self._queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    async def ensure_qianwen_ready(self):
        if self._qianwen_page and self._browser and self._browser.is_connected():
            return True
        if not self._browser or not self._browser.is_connected():
            await self.ensure_ready()
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
        try:
            self._qianwen_context = await self._browser.new_context(**ctx_kwargs)
        except Exception as e:
            logger.warning(f"Failed to create qianwen context: {e}")
            self._qianwen_context = await self._browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
        self._qianwen_page = await self._qianwen_context.new_page()
        await self._qianwen_page.expose_function("__sse_push", self._on_push)
        logger.info("Qianwen: navigating to qianwen.com ...")
        await self._qianwen_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
        await asyncio.sleep(3)
        # Detect if login is required (session cookies expired)
        body_text = await self._qianwen_page.text_content("body") or ""
        if any(kw in body_text for kw in ["扫码登录", "手机号登录", "账号登录", "登录/注册"]):
            logger.warning("Qianwen: login required - session cookies expired. Opening visible browser...")
            # Open a visible browser window for manual login
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
                await login_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
                logger.info("Qianwen: visible browser opened for manual login. Please log in...")
                # Wait for login
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
                # Capture cookies
                cookies = await login_context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                config = CONFIG.copy()
                config["qianwen_cookie"] = cookie_str
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=4)
                # Save storage_state
                try:
                    await login_context.storage_state(path=qianwen_state)
                    logger.info(f"Storage state saved to {qianwen_state}")
                except:
                    pass
                # Close login browser
                await login_browser.close()
                await pw.stop()
                logger.info("Qianwen: login browser closed, server will use the captured cookies")
                # Now re-init qianwen page with fresh cookies
                await self._qianwen_page.close()
                await self._qianwen_context.close()
                self._qianwen_context = await self._browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                    storage_state=qianwen_state,
                )
                self._qianwen_page = await self._qianwen_context.new_page()
                await self._qianwen_page.expose_function("__sse_push", self._on_push)
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
        await self.ensure_qianwen_ready()
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._queues[stream_id] = q

        user_text = messages[0].get("content", "") if messages else ""
        logger.info(f"[Qwen] typing {len(user_text)} chars")

        async def handle_route(route):
            if 'chat2.qianwen.com' not in route.request.url or 'api/v2/chat' not in route.request.url:
                await route.continue_()
                return
            try:
                resp = await route.fetch()
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
                                for m in ev.get("data", {}).get("messages", []):
                                    if m.get("mime_type") == "multi_load/iframe":
                                        c = m.get("content", "")
                                        if c and c != last:
                                            delta = c[len(last):]
                                            last = c
                                            if delta:
                                                count += 1
                                                self._queues[stream_id].put_nowait(("chunk", delta))
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
            await asyncio.sleep(0.5)
            await self._qianwen_page.keyboard.type(user_text, delay=30)
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
            self._queues.pop(stream_id, None)
            await self._qianwen_page.unroute("**/api/v2/chat**", handle_route)

    async def get_user_info(self) -> dict:
        await self.ensure_ready()
        async with self._lock:
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

            self._page.on("response", on_response)
            try:
                await self._page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                try:
                    await asyncio.wait_for(got_data.wait(), timeout=12)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(1)
            finally:
                self._page.remove_listener("response", on_response)
            return user_info

    async def stream_completion(self, body: dict):
        await self.ensure_ready()
        stream_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[stream_id] = queue

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

        async with self._lock:
            eval_task = asyncio.create_task(
                self._page.evaluate(js, {
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
                self._queues.pop(stream_id, None)
                try:
                    await eval_task
                except Exception:
                    pass

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def delete_conversation_via_browser(self, conversation_id: str) -> tuple[bool, str]:
        """通过浏览器代理删除豆包对话，复用 storage_state.json 中的 cookie。
        新接口失败时降级到旧接口 /samantha/thread/delete。"""
        await self._lock.acquire()
        try:
            await self.ensure_ready()
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
            self._lock.release()

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
        await self.ensure_qianwen_ready()
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


browser_client = BrowserClient()


