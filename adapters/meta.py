"""Meta.ai 文生图适配器，通过浏览器（持久化 profile + Urban VPN 扩展）生成图片。"""
import time
import asyncio
import logging
import json
import uuid
from typing import AsyncGenerator

from adapters.base import BaseAdapter
from models import ChatCompletionRequest
from config import CONFIG

logger = logging.getLogger("meta-adapter")

META_IMAGE_TIMEOUT_SEC = 270

META_MODELS = {
    "meta-image": {
        "name": "Meta AI Imagine",
        "desc": "Meta.ai 图片生成（文生图）",
        "is_image_model": True,
    },
}


class MetaAdapter(BaseAdapter):
    """Meta.ai 图片生成适配器。

    通过 Playwright 持久化 profile（含 Urban VPN 扩展）驱动 meta.ai 网页端
    "创建图片"（Imagine）模式生成图片，返回 OpenAI 兼容格式。
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_conversation_url = ""

    def get_adapter_name(self) -> str:
        return "meta"

    def get_models(self) -> dict[str, dict]:
        return META_MODELS

    def supports_model(self, model: str) -> bool:
        return model in META_MODELS or model.startswith("meta-image")

    async def init(self):
        logger.info("Meta adapter initialized")

    async def close(self):
        pass

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        """Meta.ai 适配器仅支持图片生成，不支持聊天流式接口。"""
        yield f"data: {json.dumps({
            'id': f'chatcmpl-{uuid.uuid4().hex[:12]}',
            'object': 'chat.completion.chunk',
            'created': int(time.time()),
            'model': request.model,
            'choices': [{
                'index': 0,
                'delta': {'content': 'Meta适配器仅支持图片生成接口(/v1/images/generations)，不支持聊天流式接口。'},
                'finish_reason': 'stop'
            }]
        }, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def _call_stream(self, **kwargs):
        """Meta适配器不需要流式聊天调用。"""
        return
        yield  # Make it a generator

    async def _prepare_messages(self, request, browser_client, is_agent: bool, reuse_conversation: bool = False):
        """Meta适配器不需要消息准备。"""
        return "", None

    async def _delete_conversation(self):
        """Meta适配器不需要对话删除。"""
        pass

    async def _get_lock(self):
        """返回用于并发控制的锁。"""
        return self._lock

    async def _safe_eval(self, page, expr, arg=None, retries=3):
        """安全执行 page.evaluate，失败时重试并返回空值。"""
        for i in range(retries):
            try:
                return await page.evaluate(expr, arg)
            except Exception as e:
                if i == retries - 1:
                    logger.warning(f"[meta] evaluate failed: {str(e)[:80]}")
                    return None
                await asyncio.sleep(1.5)
        return None

    async def generate_images(self, prompt: str, n: int = 1, size: str = "1024x1024", **kwargs) -> dict:
        """
        通过浏览器驱动 meta.ai "创建图片" 模式生成图片。
        流程：
        1. 启动持久化浏览器，必须先连接 Urban VPN 成功才打开 meta.ai（见 ensure_meta_ready）
        2. 新建聊天并进入"创建图片"模式
        3. 输入提示词并发送（Enter）
        4. 轮询等待新图片出现在页面
        5. 下载图片到本地，返回 localhost URL
        """
        from browser_client import browser_client

        adapter_name = self.get_adapter_name()
        max_retries = 3

        await self._lock.acquire()
        try:
            last_error = None

            for attempt in range(max_retries):
                all_image_urls = []

                logger.info(f"[{adapter_name} ImageGen] {attempt + 1}/{max_retries} attempt start")

                try:
                    headless = CONFIG.get('_meta_headless', True)
                    await browser_client.ensure_meta_ready(headless=headless)

                    page = browser_client._meta_page
                    if not page or page.is_closed():
                        raise RuntimeError("Meta page not available")

                    # ── 1. 新建聊天 ──
                    await self._safe_eval(page, """() => {
                        const b = [...document.querySelectorAll('a,button,[role=button]')].find(
                            x => /^新建聊天$|^new chat$/i.test((x.textContent || '').trim())
                        );
                        if (b) b.click();
                        return !!b;
                    }""")
                    await asyncio.sleep(2)

                    # ── 3. 点击"创建图片"进入文生图模式 ──
                    for _img_click in range(10):
                        clicked = await self._safe_eval(page, """() => {
                            const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                            let n, hits = [];
                            while (n = w.nextNode()) {
                                const t = (n.textContent || '').trim();
                                if (t === '创建图片' || /^create images$/i.test(t)) hits.push(n.parentElement);
                            }
                            if (hits.length) { hits[0].click(); return 'ok'; }
                            return '';
                        }""")
                        if clicked:
                            logger.info(f"[{adapter_name} ImageGen] clicked '创建图片'")
                            break
                        await asyncio.sleep(1)
                    await asyncio.sleep(3)

                    # ── 4. 输入提示词并发送 ──
                    await self._safe_eval(page, """() => {
                        const ta = document.querySelector('textarea');
                        if (ta) { ta.focus(); ta.click(); }
                        return !!ta;
                    }""")
                    await asyncio.sleep(1)
                    await page.keyboard.type(prompt, delay=15)
                    await asyncio.sleep(2)

                    # 校验输入
                    text_check = await self._safe_eval(page, """() => {
                        const el = document.querySelector('textarea');
                        return el ? el.value : '';
                    }""")
                    if not (text_check or '').strip():
                        logger.warning(f"[{adapter_name} ImageGen] text not entered, trying JS set...")
                        await self._safe_eval(page, """(p) => {
                            const el = document.querySelector('textarea');
                            if (el) {
                                el.focus();
                                el.value = p;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                            }
                        }""", prompt)
                        await asyncio.sleep(1)

                    logger.info(f"[{adapter_name} ImageGen] sending prompt...")
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(4)

                    # ── 5. 记录已有图片，轮询新图片 ──
                    before = set(await self._safe_eval(page,
                        """() => [...document.querySelectorAll('img')].map(i => i.src || '').filter(s => s)""",
                    ) or [])
                    new_imgs = []
                    start = time.time()
                    stable = 0
                    MAX_POLL = 90  # 90 × 3s ≈ 4.5 分钟
                    for _poll in range(MAX_POLL):
                        await asyncio.sleep(3)
                        cur = set(await self._safe_eval(page,
                            """() => [...document.querySelectorAll('img')].map(i => i.src || '').filter(s => s)""",
                        ) or [])
                        fresh = [u for u in cur if u not in before and u not in new_imgs]
                        for u in fresh:
                            new_imgs.append(u)
                            logger.info(f"[{adapter_name} ImageGen] +new image {u[:120]}")
                        if len(new_imgs) >= n:
                            stable += 1
                            if stable >= 4:
                                logger.info(f"[{adapter_name} ImageGen] images stable: {len(new_imgs)}")
                                break
                        else:
                            stable = 0
                        if time.time() - start > META_IMAGE_TIMEOUT_SEC:
                            break

                    if not new_imgs:
                        logger.warning(f"[{adapter_name} ImageGen] no new images after {int(time.time() - start)}s")
                        body_text = await self._safe_eval(page, "() => (document.body?.innerText || '').slice(0, 500)") or ""
                        logger.info(f"[{adapter_name} ImageGen] page text: {body_text[:300]!r}")

                except Exception as e:
                    logger.error(f"[{adapter_name} ImageGen] browser error: {e}")
                    last_error = str(e)

                if new_imgs:
                    logger.info(f"[{adapter_name} ImageGen] downloading {len(new_imgs)} images...")
                    downloaded = []
                    try:
                        downloaded = await browser_client.download_images_from_urls(new_imgs, n)
                        logger.info(f"[{adapter_name} ImageGen] download completed: {len(downloaded)}/{n}")
                    except Exception as dl_e:
                        logger.warning(f"[{adapter_name} ImageGen] download error: {dl_e}")

                    if downloaded:
                        result = {"created": int(time.time()), "data": []}
                        for local_url in downloaded[:n]:
                            result["data"].append({"url": local_url})
                        return result
                    else:
                        return {"created": int(time.time()), "data": [{"url": "", "revised_prompt": prompt, "size": size, "error": "Download failed"}]}

                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                    continue
                break

            error_msg = last_error or "图片生成功能暂时不可用，请稍后再试。"
            return {
                "created": int(time.time()),
                "data": [{"url": "", "revised_prompt": prompt, "size": size, "error": error_msg}]
            }
        except Exception as e:
            logger.error(f"[{adapter_name} ImageGen] fatal error: {e}")
            return {
                "created": int(time.time()),
                "data": [{"url": "", "revised_prompt": prompt, "size": size, "error": str(e)}]
            }
        finally:
            self._lock.release()
