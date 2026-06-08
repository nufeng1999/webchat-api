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
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._queues = {}

    async def ensure_ready(self):
        if self._page and self._browser and self._browser.is_connected():
            return True
        async with self._init_lock:
            if self._page and self._browser and self._browser.is_connected():
                return True
            await self._launch()
            return True

    async def _launch(self):
        from playwright.async_api import async_playwright
        if not os.path.exists(STORAGE_STATE_PATH):
            raise RuntimeError("storage_state.json 不存在，请先运行 python main.py --login 登录")

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
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
        logger.info("Browser client ready")

    def _on_push(self, stream_id: str, kind: str, value):
        q = self._queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    async def stream_completion(self, body: dict):
        await self.ensure_ready()
        stream_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[stream_id] = queue

        url = _build_completion_url()

        js = """
        async (args) => {
            const { url, body, streamId } = args;
            try {
                const resp = await fetch(url, {
                    method: "POST",
                    headers: {
                        "content-type": "application/json",
                        "agw-js-conv": "str",
                        "accept": "text/event-stream",
                    },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) {
                    const t = await resp.text();
                    window.__sse_push(streamId, "error", "HTTP " + resp.status + ": " + t.slice(0, 300));
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
                self._page.evaluate(js, {"url": url, "body": body, "streamId": stream_id})
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


browser_client = BrowserClient()


