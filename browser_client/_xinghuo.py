from ._shared import *


class XinghuoMixin:
    async def activate_xinghuo_conversation(self, chat_id: str) -> bool:
        """导航到讯飞星火指定对话页面。URL 格式：https://xinghuo.xfyun.cn/desk?chatId=xxx&botId=4255"""
        if not chat_id:
            return False
        try:
            await self.ensure_xinghuo_ready()
            # Xinghuo 使用 desk 页面并带 chatId 参数，botId 固定为 4255（星火 4.0 Ultra）
            url = f"https://xinghuo.xfyun.cn/desk?chatId={chat_id}&botId=4255"
            await self._xinghuo_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[Xinghuo] activated chat {chat_id}")
            return True
        except Exception as e:
            logger.warning(f"[Xinghuo] activate conversation failed: {e}")
            return False

    async def ensure_xinghuo_ready(self, headless: bool = True):
        """确保讯飞星火浏览器已启动并登录。"""
        if self._xinghuo_page and not self._xinghuo_page.is_closed():
            return

        from playwright.async_api import async_playwright
        logger.info("[Xinghuo] Starting Xinghuo SparkDesk browser...")
        self._xinghuo_pw = await async_playwright().start()
        self._xinghuo_browser = await self._xinghuo_pw.chromium.launch_persistent_context(
            user_data_dir=self._xinghuo_user_data_dir,
            headless=headless,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._xinghuo_page = self._xinghuo_browser.pages[0] if self._xinghuo_browser.pages else await self._xinghuo_browser.new_page()

        # 轻量 init_script：仅做基本反爬虫规避。API 在 / 上下文完全可用,
        # /desk 的重定向由网站 DevTools 检测引起，无需也不应强行阻止。
        await self._xinghuo_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        # 访问根路径，使用 / 上下文（API 正常工作）。缩短等待时间。
        await self._xinghuo_page.goto("https://xinghuo.xfyun.cn/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        # 检查登录状态（已登录的页面不会显示"登录"按钮，或会显示"退出"）
        is_logged = await self._xinghuo_page.evaluate("""() => {
            const t = document.body?.innerText || '';
            return !t.includes('登录') || t.includes('退出');
        }""")

        if not is_logged:
            if headless:
                raise RuntimeError("讯飞星火未登录，请先运行 python main.py --login xinghuo")
            # 非 headless 模式，等待用户登录（至少 2 分钟）
            logger.info("[Xinghuo] Not logged in, waiting for login...")
            for i in range(60):
                await asyncio.sleep(3)
                try:
                    check = await self._xinghuo_page.evaluate("""() => {
                        const t = document.body?.innerText || '';
                        return { logged: t.includes('退出') || !t.includes('登录') };
                    }""")
                    if check.get('logged'):
                        logger.info("[Xinghuo] Login successful!")
                        break
                except:
                    pass
            else:
                raise RuntimeError("讯飞星火登录超时")

        logger.info("[Xinghuo] SparkDesk browser ready")

    async def stream_xinghuo_chat(self, prompt: str, model_type: str = "4.0-ultra",
                                   thinking_enabled: bool = False,
                                   search_enabled: bool = False,
                                   inline_file_content: str | None = None,
                                   model_name: str | None = None,
                                   file_info: list | None = None,
                                   conversation_id: str = ""):
        """向讯飞星火发送消息并流式返回响应。

        Yields: (kind, value) 元组
            kind: "chat_id", "chunk", "done", "error"
        """
        if not self._xinghuo_page or self._xinghuo_page.is_closed():
            headless = CONFIG.get('_xinghuo_headless', True)
            await self.ensure_xinghuo_ready(headless=headless)
        if not self._xinghuo_page or self._xinghuo_page.is_closed():
            yield ("error", "Xinghuo page not available")
            return

        async with self._xinghuo_lock:
            try:
                current_url = self._xinghuo_page.url
                if 'xinghuo.xfyun.cn' not in current_url:
                    await self._xinghuo_page.goto("https://xinghuo.xfyun.cn/", wait_until="networkidle", timeout=60000)
                    await asyncio.sleep(5)

                content = inline_file_content if inline_file_content else prompt

                model_map = {
                    "4.0-ultra": "4.0Ultra",
                    "4.0": "4.0",
                    "3.5": "3.5",
                    "max": "max",
                    "pro": "pro",
                }
                selected_model = model_map.get(model_type, "4.0Ultra")

                # Step 1: 获取 chat_id（复用已有 或 创建新对话）
                if conversation_id and conversation_id != "0":
                    chat_id = conversation_id
                    logger.info(f"[Xinghuo] reusing existing chat_id: {chat_id}")
                else:
                    chat_id = await self._xinghuo_page.evaluate("""async () => {
                        try {
                            const createResp = await fetch('/iflygpt/u/chat-list/v1/create-chat-list', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({title: '新对话', chatType: 1}),
                                credentials: 'include'
                            });
                            const createData = await createResp.json();
                            return String(createData?.data?.id || '');
                        } catch(e) {
                            return '';
                        }
                    }""")

                if not chat_id:
                    yield ("error", "create chat failed")
                    return

                yield ("chat_id", chat_id)

                # Step 2: 如果有文件附件，上传到 OSS 并等待解析完成
                if file_info:
                    logger.info(f"[Xinghuo] Uploading {len(file_info)} file(s) for chat {chat_id}")
                    import json as _json
                    file_refs = []
                    for fi in file_info:
                        file_data = fi["data"]
                        file_name = fi["name"]
                        try:
                            upload_result = await self.upload_xinghuo_file_to_oss(file_data, file_name)
                            if upload_result.get("status") != 200:
                                logger.warning(f"[Xinghuo] OSS upload failed: {upload_result.get('text', '')[:200]}")
                                continue
                            file_url = upload_result.get("link", "")
                            if not file_url:
                                logger.warning(f"[Xinghuo] OSS upload returned no link")
                                continue
                            saved = await self.save_xinghuo_file(file_url, chat_id)
                            if saved.get("code") != 0:
                                logger.warning(f"[Xinghuo] saveFile failed: {saved}")
                                continue
                            status_result = await self.poll_xinghuo_file_status(chat_id)
                            if status_result.get("status") == "ready":
                                finfo = status_result["file"]
                                file_refs.append({
                                    "fileUrl": finfo.get("fileUrl", file_url),
                                    "fileId": finfo.get("id", ""),
                                    "fileName": finfo.get("fileName", file_name),
                                })
                                logger.info(f"[Xinghuo] File uploaded: {file_name} -> {finfo.get('fileUrl', '')[:80]}")
                            else:
                                logger.warning(f"[Xinghuo] File parse status: {status_result.get('status')}")
                        except Exception as e:
                            logger.warning(f"[Xinghuo] File upload exception: {e}")

                    # 替换 content JSON 中的 file_data 为文件引用
                    if file_refs and inline_file_content:
                        try:
                            content_obj = _json.loads(content)
                            ref_idx = [0]
                            def _replace_file_data(obj):
                                if isinstance(obj, dict):
                                    if obj.get("type") == "file" and "file_data" in obj.get("file", {}):
                                        if ref_idx[0] < len(file_refs):
                                            ref = file_refs[ref_idx[0]]
                                            ref_idx[0] += 1
                                            obj["file"] = {
                                                "fileUrl": ref["fileUrl"],
                                                "fileId": ref["fileId"],
                                                "fileName": ref["fileName"],
                                            }
                                    for v in obj.values():
                                        if isinstance(v, (dict, list)):
                                            _replace_file_data(v)
                                elif isinstance(obj, list):
                                    for item in obj:
                                        _replace_file_data(item)
                            _replace_file_data(content_obj)
                            content = _json.dumps(content_obj, ensure_ascii=False, separators=(',', ':'))
                        except Exception as e:
                            logger.warning(f"[Xinghuo] Failed to update content with file refs: {e}")

                # Step 3: 发送消息（FormData）+ 流式读取
                stream_state = await self._xinghuo_page.evaluate("""async (args) => {
                    window.__xh_stream_chunks = [];
                    window.__xh_stream_chunk_idx = 0;
                    window.__xh_stream_done = false;
                    window.__xh_stream_error = '';
                    window.__xh_stream_chat_id = args.chatId;

                    try {
                        const ts = String(+new Date);
                        const fd = ts.substring(ts.length - 6);
                        const body = new FormData();
                        body.append('fd', fd);
                        body.append('text', args.content);
                        body.append('isBot', '0');
                        body.append('clientType', '1');
                        body.append('chatId', args.chatId);

                        const resp = await fetch('/iflygpt-chat/u/chat_message/chat', {
                            method: 'POST',
                            body: body,
                            credentials: 'include',
                            headers: { 'Botweb': '1', 'clientType': '1' }
                        });

                        const ct = resp.headers.get('content-type') || '';
                        if (!ct.includes('text/event-stream')) {
                            const text = await resp.text();
                            window.__xh_stream_error = 'unexpected content-type: ' + ct + ' body: ' + text.substring(0, 200);
                            window.__xh_stream_done = true;
                            return { ok: false, error: window.__xh_stream_error };
                        }

                        // 流式读取 SSE，逐 chunk 解码
                        const reader = resp.body.getReader();
                        const decoder = new TextDecoder();
                        let buf = '';

                        while (true) {
                            const {value, done} = await reader.read();
                            if (done) break;

                            buf += decoder.decode(value, {stream: true});
                            const lines = buf.split('\\n');
                            buf = lines.pop() || '';

                            for (const line of lines) {
                                if (line.startsWith('data:')) {
                                    const raw = line.slice(5).trim();
                                    if (raw && raw !== '[DONE]') {
                                        try {
                                            const bin = atob(raw);
                                            const bytes = new Uint8Array(bin.length);
                                            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                                            const decoded = new TextDecoder('utf-8').decode(bytes);
                                            window.__xh_stream_chunks.push(decoded);
                                        } catch(e) {
                                            window.__xh_stream_chunks.push(raw);
                                        }
                                    }
                                }
                            }
                        }
                        window.__xh_stream_done = true;
                        return { ok: true, chatId: args.chatId };
                    } catch(e) {
                        window.__xh_stream_error = String(e);
                        window.__xh_stream_done = true;
                        return { ok: false, error: String(e) };
                    }
                }""", {"content": content, "chatId": chat_id, "model": selected_model})

                if not stream_state.get('ok'):
                    yield ("error", stream_state.get('error', 'unknown error'))
                    return

                chat_id = stream_state.get('chatId', '')
                if chat_id:
                    yield ("chat_id", chat_id)

                # 轮询读取新 chunk，逐个 yield
                empty_count = 0
                max_empty = 90
                last_idx = 0

                while empty_count < max_empty:
                    await asyncio.sleep(0.3)
                    state = await self._xinghuo_page.evaluate("""() => ({
                        done: window.__xh_stream_done,
                        error: window.__xh_stream_error,
                        chunkCount: (window.__xh_stream_chunks || []).length
                    })""")

                    chunk_count = state.get('chunkCount', 0)
                    if chunk_count > last_idx:
                        new_chunks = await self._xinghuo_page.evaluate("""(fromIdx) => {
                            const chunks = window.__xh_stream_chunks || [];
                            return chunks.slice(fromIdx);
                        }""", last_idx)
                        for chunk in new_chunks:
                            if chunk:
                                yield ("chunk", chunk)
                        last_idx = chunk_count
                        empty_count = 0
                    else:
                        empty_count += 1

                    if state.get('done'):
                        # 读取可能遗漏的最后一批 chunk
                        if chunk_count > last_idx:
                            final_chunks = await self._xinghuo_page.evaluate("""(fromIdx) => {
                                const chunks = window.__xh_stream_chunks || [];
                                return chunks.slice(fromIdx);
                            }""", last_idx)
                            for chunk in final_chunks:
                                if chunk:
                                    yield ("chunk", chunk)
                        err = state.get('error', '')
                        if err:
                            yield ("error", err)
                        break

                yield ("done", "")

            except Exception as e:
                logger.warning(f"[Xinghuo] stream_chat error: {e}")
                yield ("error", str(e))

    async def delete_xinghuo_conversation(self, chat_id: str):
        """删除单个讯飞星火对话。"""
        if not chat_id or not self._xinghuo_page or self._xinghuo_page.is_closed():
            return
        try:
            result = await self._xinghuo_page.evaluate("""async (chatId) => {
                try {
                    const resp = await fetch('/iflygpt/u/chat-list/v1/del-chat-list', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({chatListId: Number(chatId)}),
                        credentials: 'include'
                    });
                    const data = await resp.json();
                    return { ok: data.flag, data: data };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", chat_id)
            if result.get("ok"):
                logger.info(f"[Xinghuo] deleted chat {chat_id}")
            else:
                logger.warning(f"[Xinghuo] delete failed: {result}")
        except Exception as e:
            logger.warning(f"[Xinghuo] delete exception: {e}")

    async def delete_all_xinghuo_conversations(self):
        """删除所有讯飞星火对话。"""
        try:
            if not self._xinghuo_page or self._xinghuo_page.is_closed():
                logger.warning("[Xinghuo] no page, skip batch delete")
                return 0, 0

            result = await self._xinghuo_page.evaluate("""async () => {
                try {
                    let pageNum = 1;
                    const pageSize = 30;
                    let totalDeleted = 0;
                    let totalFetched = 0;

                    while (true) {
                        const listResp = await fetch('/iflygpt/u/chat-list/v2/chat-list', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({chatType: 1, pageNum, pageSize}),
                            credentials: 'include'
                        });
                        const listData = await listResp.json();
                        const chats = listData?.data?.list || listData?.data || [];
                        totalFetched += chats.length;

                        if (chats.length === 0) break;

                        for (const c of chats) {
                            try {
                                const delResp = await fetch('/iflygpt/u/chat-list/v1/del-chat-list', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({chatListId: Number(c.id)}),
                                    credentials: 'include'
                                });
                                const delData = await delResp.json();
                                if (delData?.flag) totalDeleted++;
                            } catch(e) {}
                        }

                        if (chats.length < pageSize) break;
                        pageNum++;
                    }

                    return { deleted: totalDeleted, total: totalFetched };
                } catch (e) {
                    return { error: String(e) };
                }
            }""")
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Xinghuo] delete_all: deleted {deleted}/{total} chats")
            return deleted, total
        except Exception as e:
            logger.warning(f"[Xinghuo] delete_all exception: {e}")
            return 0, 0

    async def close_xinghuo(self):
        """关闭讯飞星火浏览器。"""
        try:
            if self._xinghuo_page and not self._xinghuo_page.is_closed():
                await self._xinghuo_page.close()
        except Exception as e:
            logger.debug(f"[Xinghuo] close page error: {e}")
        self._xinghuo_page = None
        try:
            if self._xinghuo_browser:
                await self._xinghuo_browser.close()
        except Exception as e:
            logger.debug(f"[Xinghuo] close browser error: {e}")
        self._xinghuo_browser = None
        try:
            if self._xinghuo_pw:
                await self._xinghuo_pw.stop()
        except Exception:
            pass
        self._xinghuo_pw = None
        logger.info("[Xinghuo] resources cleaned up")

    async def get_xinghuo_oss_sign(self) -> dict:
        """获取 OSS 上传签名。"""
        headless = CONFIG.get('_xinghuo_headless', True)
        await self.ensure_xinghuo_ready(headless=headless)
        return await self._xinghuo_page.evaluate("""async () => {
            const resp = await fetch('/iflygpt/oss/sign', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({business: 'chatdoc'}),
                credentials: 'include',
            });
            return await resp.json();
        }""")

    async def get_xinghuo_chatdoc_sign(self) -> dict:
        """获取 chatdoc 上传签名。"""
        headless = CONFIG.get('_xinghuo_headless', True)
        await self.ensure_xinghuo_ready(headless=headless)
        return await self._xinghuo_page.evaluate("""async () => {
            const resp = await fetch('/iflygpt/file_chat/getSign', {
                method: 'GET',
                credentials: 'include',
            });
            return await resp.json();
        }""")

    async def upload_xinghuo_file_to_oss(self, file_data: bytes, file_name: str) -> dict:
        """上传文件到讯飞 OSS 并返回 OSS 签名结果。"""
        headless = CONFIG.get('_xinghuo_headless', True)
        await self.ensure_xinghuo_ready(headless=headless)
        oss_sign_result = await self.get_xinghuo_oss_sign()
        if oss_sign_result.get("code") != 0:
            raise RuntimeError(f"OSS sign failed: {oss_sign_result}")
        sign_data = oss_sign_result["data"]
        authorization = sign_data["authorization"]
        date = sign_data["date"]
        url = sign_data["url"]
        host = sign_data["host"]
        policy_dir = sign_data["policyDir"]
        oss_filename = sign_data["fileName"]
        accessid = sign_data["accessid"]
        policy = sign_data["policy"]

        # 将二进制数据转为 base64，在浏览器端解码为 Blob
        import base64
        file_b64 = base64.b64encode(file_data).decode("ascii")

        resp = await self._xinghuo_page.evaluate("""async (args) => {
            const { fileB64, fileName, url, host, date, policyDir, ossFilename, accessid, policy, signature } = args;
            // base64 解码为 Uint8Array
            const raw = atob(fileB64);
            const arr = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
            const blob = new Blob([arr], { type: 'application/octet-stream' });

            // 构建 FormData
            const fd = new FormData();
            fd.append('key', policyDir + '/' + ossFilename);
            fd.append('OSSAccessKeyId', accessid);
            fd.append('policy', policy);
            fd.append('Signature', signature);
            fd.append('file', blob, fileName);

            try {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Host': host,
                        'Date': date,
                    },
                    body: fd,
                    credentials: 'omit',
                });
                const text = await resp.text();
                // 尝试提取 <Location> 中的 URL（上传成功后 OSS 返回 XML）
                const match = text.match(/<Location>([\\s\\S]*?)<\\/Location>/);
                const link = match ? match[1] : '';
                return { status: resp.status, link: link, text: text.slice(0, 500) };
            } catch(e) {
                return { status: 0, link: '', text: e.message };
            }
        }""", {
            "fileB64": file_b64,
            "fileName": file_name,
            "url": url,
            "host": host,
            "date": date,
            "policyDir": policy_dir,
            "ossFilename": oss_filename,
            "accessid": accessid,
            "policy": policy,
            "signature": authorization,
        })
        return resp

    async def save_xinghuo_file(self, file_url: str, chat_id: str) -> dict:
        """保存已上传的文件到聊天系统。"""
        sign_result = await self.get_xinghuo_chatdoc_sign()
        if sign_result.get("code") != 0:
            raise RuntimeError(f"Chatdoc sign failed: {sign_result}")
        sign_data = sign_result["data"]
        resp = await self._xinghuo_page.evaluate("""async (args) => {
            const params = new URLSearchParams();
            params.append('signature', args.signature);
            params.append('appId', args.appId);
            params.append('timestamp', String(args.timestamp));
            params.append('chatId', args.chatId);
            params.append('fileUrl', args.fileUrl);
            const resp = await fetch('/iflygpt/file_chat/saveFile?' + params.toString(), {
                method: 'POST',
                credentials: 'include',
            });
            return await resp.json();
        }""", {
            "signature": sign_data["signature"],
            "appId": sign_data["appId"],
            "timestamp": str(sign_data["timestamp"]),
            "chatId": chat_id,
            "fileUrl": file_url,
        })
        return resp

    async def poll_xinghuo_file_status(self, chat_id: str, max_wait: int = 30, interval: int = 2) -> dict:
        """轮询文件解析状态，返回解析结果。"""
        for i in range(max_wait // interval):
            await asyncio.sleep(interval)
            result = await self._xinghuo_page.evaluate("""async (args) => {
                const resp = await fetch('/iflygpt/file_chat/listFiles?chatId=' + args.chatId, {
                    method: 'GET',
                    credentials: 'include',
                });
                return await resp.json();
            }""", {"chatId": chat_id})
            files = result.get("data", {}).get("files", [])
            for f in files:
                status = f.get("status", "")
                if status == 2:
                    return {"status": "ready", "file": f}
                elif status == 3:
                    return {"status": "failed", "file": f}
        return {"status": "timeout"}

    async def upload_and_save_xinghuo_file(self, file_data: bytes, file_name: str, chat_id: str) -> dict:
        """完整的文件上传流程：OSS 上传 -> 保存 -> 轮询解析。"""
        upload_result = await self.upload_xinghuo_file_to_oss(file_data, file_name)
        if upload_result.get("status") != 200:
            raise RuntimeError(f"OSS upload failed: {upload_result}")
        saved = await self.save_xinghuo_file(upload_result.get("link", ""), chat_id)
        if saved.get("code") != 0:
            raise RuntimeError(f"Save file failed: {saved}")
        status_result = await self.poll_xinghuo_file_status(chat_id)
        if status_result.get("status") == "ready":
            file_info = status_result["file"]
            return {
                "success": True,
                "file_url": file_info.get("fileUrl", ""),
                "file_id": file_info.get("id", ""),
                "file_name": file_info.get("fileName", file_name),
            }
        return {"success": False, "reason": status_result.get("status", "unknown")}

