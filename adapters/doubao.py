import json
import uuid
import os
import logging
import asyncio
import time
from datetime import datetime
from typing import AsyncGenerator, Optional
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, ChatMessage, MODEL_CONFIG
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt, get_rate_limit_wait_seconds
from sse import format_openai_chunk, format_openai_done, extract_text_from_content

logger = logging.getLogger("doubao-adapter")

DOUBAO_MODELS = {k: v for k, v in MODEL_CONFIG.items()}


class DoubaoAdapter(BaseAdapter):
    """Doubao (豆包) 适配器，使用模板模式。"""

    def __init__(self):
        self._doubao_lock = asyncio.Lock()
        self._last_conversation_id = ""
        self._last_chat_id = ""

    def get_adapter_name(self) -> str:
        return "doubao"

    def get_models(self) -> dict[str, dict]:
        return DOUBAO_MODELS

    async def init(self):
        logger.info("Doubao adapter initialized")

    async def close(self):
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # Hook 方法（模板调用）
    # ═══════════════════════════════════════════════════════════════════════

    def _get_lock(self):
        return self._doubao_lock

    async def _prepare_messages(self, request, browser_client, is_agent: bool, reuse_conversation: bool = False):
        """准备 Doubao 的请求数据。对于非 agent 请求，提取最后一条消息文本；对于 agent 请求，序列化 request 为 JSON。"""
        if not is_agent:
            last_msg = request.messages[-1] if request.messages else None
            prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
            logger.debug(f"{Colors.BLUE}Doubao prepare_messages: non-agent, text_len={len(prompt_text)}{Colors.RESET}")
            return prompt_text, None

        if reuse_conversation:
            logger.info(f"[Doubao] skipping file upload for reused conversation")
            request_dict = request.model_dump()
            last_msg = request.messages[-1] if request.messages else None
            is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
            if not is_tool_return and isinstance(getattr(last_msg, 'content', None), list):
                if len(request.messages) >= 2:
                    last_msg = request.messages[-2] if request.messages else None
                    is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
            if is_tool_return:
                logger.debug(f"------------[is_tool_return]-------------")
                prompt_text = get_ret_format_prompt(self.get_adapter_name()) + "\n " + self._get_last_three_messages_as_json(request_dict)
            else:
                prompt_text = get_exectask_prompt(self.get_adapter_name()) + "\n " + self._get_last_message_as_json(request_dict)

            try:
                logs_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(logs_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                file_content = await self._prepare_inline_file_content(request, is_tool_return)
                fname = "toolreturn.json" if is_tool_return else "request.json"
                saved_path = os.path.join(logs_dir, f"{fname.rsplit('.', 1)[0]}_{ts}.json")
                with open(saved_path, 'w', encoding='utf-8') as f:
                    f.write(file_content)
                logger.info(f"[Doubao] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
            except Exception as e:
                logger.warning(f"[Doubao] save {fname} failed: {e}")

            return prompt_text, None

        last_msg = request.messages[-1] if request.messages else None
        is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
        if not is_tool_return and isinstance(getattr(last_msg, 'content', None), list):
            if len(request.messages) >= 2:
                last_msg = request.messages[-2] if request.messages else None
                is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
        file_content = await self._prepare_inline_file_content(request, is_tool_return)
        request_dict = request.model_dump()
        if is_tool_return:
            prompt_text = get_ret_format_prompt(self.get_adapter_name()) + "\n " + self._get_last_three_messages_as_json(request_dict)
        else:
            prompt_text = get_exectask_prompt(self.get_adapter_name()) + "\n " + self._get_last_message_as_json(request_dict)

        try:
            logs_dir = os.path.join(BASE_DIR, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = "toolreturn.json" if is_tool_return else "request.json"
            saved_path = os.path.join(logs_dir, f"{fname.rsplit('.', 1)[0]}_{ts}.json")
            with open(saved_path, 'w', encoding='utf-8') as f:
                f.write(file_content)
            logger.info(f"[Doubao] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
        except Exception as e:
            logger.warning(f"[Doubao] save {fname} failed: {e}")

        if is_tool_return:
            logger.debug(f"{Colors.BLUE}[Doubao]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}toolreturn.json{Colors.RESET} ({len(file_content)} bytes)")
        else:
            logger.debug(f"{Colors.BLUE}[Doubao]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}request.json{Colors.RESET} ({len(file_content)} bytes)")

        return prompt_text, file_content

    def _build_stream_kwargs(self, prompt_text: str, file_content, is_agent: bool, current_prompt: str) -> dict:
        kwargs = {"text": current_prompt}
        if is_agent and file_content:
            kwargs["inline_file_content"] = f"[文件 request.json 内容]\n{file_content}\n[/文件内容]"
        # Increase timeout to 180 seconds to allow for large request processing
        kwargs["timeout"] = 180
        return kwargs

    async def _call_stream(self, **kwargs):
        """调用浏览器客户端的流式方法。"""
        from browser_client import browser_client
        async for kind, value in browser_client.stream_doubao_chat_via_type(**kwargs):
            yield kind, value

    async def _on_session_id(self, value):
        """处理流中收到的 conversation_id 事件。"""
        self._last_conversation_id = value
        logger.info(f"[Doubao] conversation_id: {value}")

    async def _delete_conversation(self):
        """仅清除 adapter 本地状态，不删除 web 对话实例。"""
        self._last_conversation_id = ""

    async def _refresh_doubao_page_after_rate_limit(self):
        """限流等待后刷新豆包页面，确保重试时页面状态正确"""
        try:
            from browser_client import browser_client
            bc = browser_client
            if bc._doubao_page is None or bc._doubao_page.is_closed():
                logger.info("[Doubao] page is closed, skipping refresh")
                return False
            logger.info("[Doubao] refreshing page after rate limit wait...")
            await bc._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            logger.info("[Doubao] page refreshed, ready for retry")
            return True
        except Exception as e:
            logger.warning(f"[Doubao] failed to refresh page: {e}")
            return False

    async def _handle_rate_limit(self, attempt: int, max_retries: int, error_msg: str = None):
        """处理限流：前6次临时限流静默等待15秒+刷新页面后重试，6次后显示浏览器让用户处理5分钟+刷新页面，然后继续重试。返回 True 表示已处理可继续重试。"""
        needs_verification = False
        if error_msg:
            try:
                err_data = json.loads(error_msg)
                decision = err_data.get("error_detail", {}).get("ext", {}).get("decision", {})
                if decision.get("type") == "verify":
                    needs_verification = True
                    logger.info(f"[Doubao] rate limit includes verify decision (subtype={decision.get('subtype')}, scene={decision.get('verify_scene')}) → needs user action")
            except json.JSONDecodeError:
                pass

        if needs_verification:
            wait_seconds = get_rate_limit_wait_seconds()
            logger.warning(f"[Doubao] rate limited with verification needed! showing visible browser for user to handle...waiting up to {wait_seconds} seconds")
            try:
                from browser_client import browser_client
                await browser_client.show_doubao_for_rate_limit()
            except Exception as e:
                logger.warning(f"[Doubao] failed to show visible browser: {e}")
            logger.info(f"[Doubao] waiting up to {wait_seconds} seconds for user to handle rate limit...")
            visible_start = browser_client._visible_browser_started_at
            min_wait = get_rate_limit_wait_seconds()
            for sec in range(0, min_wait, 10):
                await asyncio.sleep(10)
                try:
                    from browser_client import browser_client
                    bc = browser_client
                    elapsed = time.time() - visible_start if visible_start else sec + 10
                    page_alive = bc._doubao_page is not None and (not hasattr(bc._doubao_page, 'is_closed') or not bc._doubao_page.is_closed())
                    if page_alive and elapsed < min_wait:
                        continue
                    if not page_alive:
                        logger.info(f"[Doubao] browser closed by user after {int(elapsed)}s, stopping wait")
                        break
                except Exception:
                    logger.info(f"[Doubao] browser check failed after {sec+10}s, assuming closed")
                    break
            try:
                from browser_client import browser_client
                await browser_client.hide_doubao_browser()
            except Exception as e:
                logger.warning(f"[Doubao] failed to hide visible browser: {e}")
            await self._refresh_doubao_page_after_rate_limit()
            logger.info(f"[Doubao] resuming after rate limit handling, retry {attempt+2}/{max_retries}")
            return True
        
        retry_seconds = CONFIG.get('_rate_limit_retry_seconds', 15)
        
        if attempt < max_retries - 1:
            logger.info(f"[Doubao] temporary rate limit (no verify), waiting {retry_seconds}s then refreshing page... (attempt {attempt+1}/{max_retries})")
            await asyncio.sleep(retry_seconds)
            await self._refresh_doubao_page_after_rate_limit()
            logger.info(f"[Doubao] resuming after temporary rate limit, retry {attempt+2}/{max_retries}")
            return True
        else:
            logger.warning(f"[Doubao] rate limited after {max_retries} retries, showing visible browser for user to handle captcha/verification...")
            wait_seconds = get_rate_limit_wait_seconds()
            try:
                from browser_client import browser_client
                await browser_client.show_doubao_for_rate_limit()
            except Exception as e:
                logger.warning(f"[Doubao] failed to show visible browser: {e}")
            logger.info(f"[Doubao] giving user {wait_seconds} seconds to handle the rate limit/captcha in visible browser...")
            visible_start = browser_client._visible_browser_started_at
            min_wait = get_rate_limit_wait_seconds()
            user_closed_browser = False
            for sec in range(0, min_wait, 10):
                await asyncio.sleep(10)
                try:
                    from browser_client import browser_client
                    bc = browser_client
                    elapsed = time.time() - visible_start if visible_start else sec + 10
                    page_alive = bc._doubao_page is not None and (not hasattr(bc._doubao_page, 'is_closed') or not bc._doubao_page.is_closed())
                    if page_alive and elapsed < min_wait:
                        continue
                    if not page_alive:
                        logger.info(f"[Doubao] browser closed by user after {int(elapsed)}s, stopping wait")
                        user_closed_browser = True
                        break
                except Exception:
                    logger.info(f"[Doubao] browser check failed after {sec+10}s, assuming closed")
                    user_closed_browser = True
                    break
            
            if not user_closed_browser:
                try:
                    from browser_client import browser_client
                    await browser_client.hide_doubao_browser()
                except Exception as e:
                    logger.warning(f"[Doubao] failed to hide visible browser: {e}")
            
            await self._refresh_doubao_page_after_rate_limit()
            logger.info(f"[Doubao] user handled rate limit, resuming request retry...")
            return True

    async def _on_success(self, chat_id: str):
        """成功后保存 conversation_id。"""
        self._save_conversation_id(chat_id)

    def _session_id_kind(self) -> str:
        return "conversation_id"

    def _use_parse_error_history(self) -> bool:
        return True

    def _stream_error_no_delete(self) -> bool:
        return False

    async def _on_finally_extra(self):
        """无额外清理。"""
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # 辅助方法（保持不变）
    # ═══════════════════════════════════════════════════════════════════════

    async def _prepare_inline_file_content(self, request: ChatCompletionRequest, is_tool_return: bool) -> str:
        """Prepare the file content as inline text。"""
        request_dict = request.model_dump()
        if is_tool_return:
            request_dict['task'] = get_ret_format_prompt(self.get_adapter_name())
        else:
            request_dict['task'] = get_webchat_task(self.get_adapter_name())

        request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')

        msgs = request_dict.get('messages', [])
        if len(msgs) > 25:
            new_msgs = msgs[:15] + msgs[-10:]
        else:
            new_msgs = msgs
        request_dict['messages'] = new_msgs

        return json.dumps(request_dict, ensure_ascii=False, indent=None, separators=(',', ':'))

    def _save_conversation_id(self, chat_id: str):
        """保存当前 conversation_id 到文件，供 shutdown 清理时删除。"""
        if chat_id and self._last_conversation_id:
            try:
                from config import CONVERSATION_DIR
                os.makedirs(CONVERSATION_DIR, exist_ok=True)
                state_file = os.path.join(CONVERSATION_DIR, f"{chat_id}.json")
                with open(state_file, 'w', encoding='utf-8') as f:
                    json.dump({"doubao_conversation_id": self._last_conversation_id}, f, ensure_ascii=False)
            except Exception as save_e:
                logger.warning(f"[Doubao] failed to save conversation state: {save_e}")

    async def _delete_current_conversation(self):
        """立即删除当前 attempt 的对话。优先使用浏览器方式。"""
        conv_id = self._last_conversation_id
        if not conv_id or conv_id == "0":
            logger.debug(f"[Doubao] no conversation to delete (conv_id={conv_id})")
            self._last_conversation_id = ""
            return

        try:
            from browser_client import browser_client
            ok, err = await browser_client.delete_conversation_via_browser(conv_id, skip_lock=True)
            if ok:
                logger.info(f"[Doubao] deleted conversation {conv_id} via browser")
                self._last_conversation_id = ""
                return
            if "cancelled" in err.lower() or "cancel scope" in err.lower():
                logger.info(f"[Doubao] browser delete cancelled (likely succeeded), skipping HTTP fallback")
                self._last_conversation_id = ""
                return
            logger.warning(f"[Doubao] browser delete failed: {err}")
        except Exception as e:
            err_str = str(e)
            if "cancelled" in err_str.lower() or "cancel scope" in err_str.lower():
                logger.info(f"[Doubao] browser delete cancelled (likely succeeded), skipping HTTP fallback")
                self._last_conversation_id = ""
                return
            logger.warning(f"[Doubao] browser delete exception: {e}")

        self._last_conversation_id = ""

    def _build_retry_prompt(self, prompt_text: str, is_tool_return: bool, parse_error_history: list) -> str:
        """构建带错误反馈的 prompt。"""
        if not parse_error_history:
            return prompt_text
        err_msg, raw_text = parse_error_history[-1]
        if is_tool_return:
            extra_hint = "，不要给回复 JSON 的 delta.content 赋值为 修正后的json内容"
        else:
            extra_hint = ""
        return (
            f"{prompt_text}\n\n"
            f"[系统提示：你上一次回复的JSON格式解析失败，请修正后重新输出完整且合法的JSON{extra_hint}。]\n"
            f"[错误信息：{err_msg}]\n"
            f"[你的原始回复（前500字符）：{raw_text[:2000]}]"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # 图片生成
    # ═══════════════════════════════════════════════════════════════════════

    async def generate_images(self, prompt: str, n: int = 1, size: str = "1024x1024", **kwargs) -> dict:
        """
        通过浏览器代理生成图片（豆包 "图像生成" 模式）。
        流程：
        1. 导航到 /chat/ 页面
        2. 点击"图像生成"按钮切换到文生图模式
        3. 等待输入框 placeholder 变为 "描述你想要的图片"（确认进入图像生成模式）
        4. 输入提示词并发送
        5. 轮询等待图片生成完成
        6. 右键图片 → 上下文菜单"下载原图" → 拦截带签名的 image_dld_watermark URL
        7. 用 page.request.get() 下载原图到本地，返回 localhost URL
        """
        from browser_client import browser_client
        import time

        adapter_name = self.get_adapter_name()
        max_retries = 3

        lock = self._get_lock()
        await lock.acquire()
        try:
            last_error = None
            self._last_conversation_id = ""

            for attempt in range(max_retries):
                all_image_urls = []
                conversation_id = "0"

                logger.info(f"[{adapter_name} ImageGen] {Colors.RED}Attempt {attempt+1}/{max_retries}{Colors.RESET}")

                try:
                    headless = CONFIG.get('_doubao_headless', True)
                    await browser_client.ensure_doubao_ready(headless=headless)

                    page = browser_client._doubao_page
                    if not page or page.is_closed():
                        raise RuntimeError("Doubao page not available")

                    # ── Helper: detect and handle error page ──
                    async def _handle_error_page(timeout=30000):
                        """Check for error page and handle it. Returns True if page is OK."""
                        for _err in range(5):
                            check = await page.evaluate("""() => {
                                const txt = (document.body?.innerText || '');
                                const hasError = txt.includes('该页面暂时不可用') || txt.includes('页面渲染异常') || txt.includes('page temporarily unavailable');
                                const hasRefreshBtn = !!Array.from(document.querySelectorAll('button')).find(b => (b.textContent||'').includes('刷新页面'));
                                const hasBackBtn = !!Array.from(document.querySelectorAll('button,a')).find(b => (b.textContent||'').includes('返回首页'));
                                return { hasError, hasRefreshBtn, hasBackBtn, url: location.href };
                            }""")
                            if not check.get('hasError'):
                                return True
                            logger.warning(f"[{adapter_name} ImageGen] error page detected: url={check.get('url','')}")
                            if check.get('hasRefreshBtn'):
                                await page.evaluate("""() => { const b=[...document.querySelectorAll('button')].find(b=>(b.textContent||'').includes('刷新页面')); if(b)b.click(); }""")
                            elif check.get('hasBackBtn'):
                                await page.evaluate("""() => { const b=[...document.querySelectorAll('button,a')].find(b=>(b.textContent||'').includes('返回首页')); if(b)b.click(); }""")
                            else:
                                await page.reload(wait_until="domcontentloaded", timeout=timeout)
                            await asyncio.sleep(5)
                            # After refresh/reload, wait a moment for page to stabilize
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                            except Exception:
                                pass
                        return False

                    # ── Step 1: Ensure we're on /chat/ ──
                    logger.info(f"[{adapter_name} ImageGen] navigating to /chat/...")
                    await page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(5)
                    await _handle_error_page(timeout=60000)

                    # ── Step 2: Wait for chat page fully loaded ──
                    logger.info(f"[{adapter_name} ImageGen] waiting for chat page to fully load...")
                    for _load in range(30):
                        load_state = await page.evaluate("""() => {
                            const hasInput = !!document.querySelector('textarea');
                            const hasSendBtn = !!document.getElementById('flow-end-msg-send');
                            const bodyLen = (document.body?.innerText || '').length;
                            return { hasInput, hasSendBtn, bodyLen, url: location.href };
                        }""")
                        if load_state.get('hasInput') or load_state.get('hasSendBtn'):
                            logger.info(f"[{adapter_name} ImageGen] chat page loaded: input={load_state.get('hasInput')} sendBtn={load_state.get('hasSendBtn')}")
                            break
                        await asyncio.sleep(2)
                    else:
                        logger.warning(f"[{adapter_name} ImageGen] chat page load timeout, continuing anyway")

                    # ── Step 3: Click "图像生成" button (below textarea) to switch to image gen mode ──
                    logger.info(f"[{adapter_name} ImageGen] clicking '图像生成' button to enter image gen mode...")
                    img_gen_clicked = False
                    for _img_click in range(20):
                        await _handle_error_page()
                        img_gen_clicked = await page.evaluate("""() => {
                            const all = document.querySelectorAll('*');
                            for (const el of all) {
                                if (el.childElementCount > 2) continue;
                                const txt = (el.textContent || '').trim();
                                if (txt === '图像生成') {
                                    el.click();
                                    return el.tagName + ':' + el.className.substring(0,30);
                                }
                            }
                            return '';
                        }""")
                        if img_gen_clicked:
                            logger.info(f"[{adapter_name} ImageGen] clicked '图像生成': {img_gen_clicked}")
                            break
                        await asyncio.sleep(1)
                    
                    if not img_gen_clicked:
                        logger.warning(f"[{adapter_name} ImageGen] '图像生成' button not found")
                        all_btns_text = await page.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                            return btns.slice(0, 50).map(b => b.textContent.trim().substring(0, 40)).filter(t => t);
                        }""")
                        logger.warning(f"[{adapter_name} ImageGen] visible buttons: {all_btns_text}")
                        last_error = "Could not find '图像生成' button"
                        if attempt < max_retries - 1:
                            await asyncio.sleep(5)
                            continue
                        break
                    
                    # ── Step 4: Wait for image gen mode to be active (placeholder "描述你想要的图片" appears) ──
                    logger.info(f"[{adapter_name} ImageGen] waiting for image gen mode placeholder...")
                    for _mode_wait in range(30):
                        await _handle_error_page()
                        mode_check = await page.evaluate("""() => {
                            const el = document.querySelector('textarea, [contenteditable="true"], [role="textbox"]');
                            if (!el) return { found: false };
                            const placeholder = el.getAttribute('data-placeholder') || el.placeholder || '';
                            const childP = el.querySelector('[data-placeholder]');
                            const childPlaceholder = childP ? childP.getAttribute('data-placeholder') : '';
                            return { found: true, placeholder: placeholder, childPlaceholder: childPlaceholder, tagName: el.tagName };
                        }""")
                        logger.debug(f"[{adapter_name} ImageGen] mode check: {mode_check}")
                        ph = mode_check.get('placeholder') or ''
                        cph = mode_check.get('childPlaceholder') or ''
                        if '描述你想要的图片' in ph or '描述你想要的图片' in cph:
                            logger.info(f"[{adapter_name} ImageGen] IMAGE GEN MODE ACTIVE (placeholder detected)!")
                            break
                        # Also detect by tagName change: textarea -> div[contenteditable] means mode switch
                        if mode_check.get('tagName') == 'DIV' and mode_check.get('found'):
                            logger.info(f"[{adapter_name} ImageGen] IMAGE GEN MODE ACTIVE (div contenteditable detected)!")
                            break
                        await asyncio.sleep(1)
                    else:
                        logger.warning(f"[{adapter_name} ImageGen] image gen mode placeholder not detected, continuing anyway")

                    # ── Step 5: Type prompt in SAME input and send ──
                    logger.info(f"[{adapter_name} ImageGen] typing prompt in input...")
                    try:
                        ta_el = await page.query_selector('textarea, [contenteditable="true"], [role="textbox"]')
                        if ta_el:
                            await ta_el.scroll_into_view_if_needed()
                            await ta_el.focus()
                            await ta_el.click()
                        else:
                            await page.evaluate("() => { const el = document.querySelector('textarea, [contenteditable=true], [role=textbox]'); if(el) { el.scrollIntoView({block:'center'}); el.focus(); el.click(); } }")
                    except Exception as e:
                        logger.warning(f"[{adapter_name} ImageGen] scroll/focus error: {e}")
                    
                    await asyncio.sleep(0.5)
                    
                    # Type using keyboard events
                    await page.keyboard.press("Control+A")
                    await asyncio.sleep(0.1)
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(0.2)
                    await page.keyboard.type(prompt, delay=30)
                    logger.info(f"[{adapter_name} ImageGen] typed {len(prompt)} chars")
                    await asyncio.sleep(1)

                    # Verify text in input
                    text_check = await page.evaluate("""() => {
                        const el = document.querySelector('textarea, [contenteditable=true], [role="textbox"]');
                        if (!el) return { ok: false };
                        const text = el.tagName === 'TEXTAREA' ? el.value : (el.textContent || el.innerText || '');
                        return { ok: text.trim().length > 0, text: text.trim().substring(0, 50) };
                    }""")
                    logger.debug(f"[{adapter_name} ImageGen] text in input: {text_check}")
                    
                    if not text_check.get('ok'):
                        logger.warning(f"[{adapter_name} ImageGen] text not entered! Trying JS value set...")
                        await page.evaluate("""(p) => {
                            const el = document.querySelector('textarea, [contenteditable=true], [role="textbox"]');
                            if (el) {
                                el.focus();
                                if (el.tagName === 'TEXTAREA') {
                                    el.value = p;
                                    el.dispatchEvent(new Event('input', {bubbles: true}));
                                } else {
                                    el.textContent = p;
                                    el.dispatchEvent(new Event('input', {bubbles: true}));
                                }
                            }
                        }""", prompt)
                        await asyncio.sleep(1)

                    # Send - try multiple strategies
                    send_result = await page.evaluate("""() => {
                        // Strategy 1: 生成 button (image gen mode)
                        const genBtn = [...document.querySelectorAll('button')].find(b => {
                            const txt = (b.textContent || '').trim();
                            return txt === '生成' || txt === '开始生成' || txt === '立即生成';
                        });
                        if (genBtn && genBtn.offsetParent !== null) {
                            genBtn.click();
                            return 'gen-btn';
                        }
                        
                        // Strategy 2: flow-end-msg-send button
                        const sendBtn = document.getElementById('flow-end-msg-send');
                        if (sendBtn && !sendBtn.disabled && sendBtn.offsetParent !== null) {
                            sendBtn.click();
                            return 'send-btn';
                        }
                        
                        return 'none';
                    }""")
                    logger.info(f"[{adapter_name} ImageGen] send result: {send_result}")
                    
                    if send_result == 'none':
                        logger.info(f"[{adapter_name} ImageGen] trying Enter key...")
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(1)

                    # Verify generation started
                    await asyncio.sleep(3)
                    gen_status = await page.evaluate("""() => {
                        const bodyText = (document.body?.innerText || '');
                        const hasLoading = bodyText.includes('生成中') || bodyText.includes('正在生成') || bodyText.includes('loading') || bodyText.includes('Loading');
                        const hasGenerating = !!document.querySelector('[class*="loading"]') || !!document.querySelector('[class*="generating"]') || !!document.querySelector('[class*="progress"]');
                        const inputNow = (() => {
                            const el = document.querySelector('textarea') || document.querySelector('[contenteditable=true]');
                            if (!el) return '';
                            return el.tagName === 'TEXTAREA' ? el.value : (el.textContent || el.innerText || '');
                        })();
                        return {
                            hasLoading, hasGenerating,
                            inputCleared: inputNow.trim().length === 0,
                            inputText: inputNow.trim().substring(0, 30)
                        };
                    }""")
                    logger.debug(f"[{adapter_name} ImageGen] generation status: {gen_status}")
                    
                    if not gen_status.get('hasLoading') and not gen_status.get('hasGenerating') and not gen_status.get('inputCleared'):
                        logger.warning(f"[{adapter_name} ImageGen] generation may not have started! Retrying...")
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(3)

                    # ── Step 6: Record existing images, then poll for new ones (up to 5 minutes) ──
                    await asyncio.sleep(3)
                    old_img_srcs = await page.evaluate("""() => {
                        const imgs = Array.from(document.querySelectorAll('img'));
                        return imgs.filter(img => {
                            const src = img.src || '';
                            return (src.includes('image_generation') || src.includes('rc_gen_image'))
                                   && src.includes('byteimg.com');
                        }).map(i => i.src || '');
                    }""")
                    logger.info(f"[{adapter_name} ImageGen] {len(old_img_srcs)} existing images, waiting for new ones (up to 5min)...")

                    prev_new_count = 0
                    stable_count = 0
                    MAX_POLL = 150  # 150 × 2s = 300s = 5 minutes
                    for poll_i in range(MAX_POLL):
                        await asyncio.sleep(2)
                        await _handle_error_page()
                        img_info = await page.evaluate("""() => {
                            const imgs = Array.from(document.querySelectorAll('img'));
                            const genImgs = imgs.filter(img => {
                                const src = img.src || '';
                                return (src.includes('image_generation') || src.includes('rc_gen_image'))
                                       && src.includes('byteimg.com');
                            });
                            const bodyText = document.body?.innerText || '';
                            const isRateLimit = bodyText.includes('请求过于频繁') || bodyText.includes('rate limit');
                            return {
                                totalImgs: imgs.length,
                                genImgCount: genImgs.length,
                                genImgSrcs: genImgs.map(i => i.src || ''),
                                isRateLimit: isRateLimit
                            };
                        }""")

                        if img_info.get('isRateLimit'):
                            rate_limit_error = "Rate limit detected on page"
                            logger.warning(f"[{adapter_name} ImageGen] rate limit detected")
                            handled = await self._handle_rate_limit(attempt, max_retries, rate_limit_error)
                            if handled:
                                break
                            break

                        # Count only NEW images (not in old_img_srcs)
                        all_srcs = img_info.get('genImgSrcs', [])
                        new_srcs = [s for s in all_srcs if s and s not in old_img_srcs]
                        new_count = len(new_srcs)

                        if new_count > 0:
                            if new_count == prev_new_count:
                                stable_count += 1
                            else:
                                stable_count = 0
                                prev_new_count = new_count
                                logger.info(f"[{adapter_name} ImageGen] new images appearing: {new_count} at {(poll_i+1)*2}s")

                            if stable_count >= 5:
                                for src in new_srcs:
                                    if src not in all_image_urls:
                                        all_image_urls.append(src)
                                logger.info(f"[{adapter_name} ImageGen] images stable: {len(all_image_urls)} new after {(poll_i+1)*2}s")
                                break

                        if poll_i % 30 == 29:  # Log every 60 seconds
                            logger.info(f"[{adapter_name} ImageGen] poll {(poll_i+1)*2}s: newImgs={new_count} totalGenImgs={img_info.get('genImgCount',0)}")

                    # Final extraction
                    if not all_image_urls:
                        final_info = await page.evaluate("""() => {
                            const imgs = Array.from(document.querySelectorAll('img'));
                            const genImgs = imgs.filter(img => {
                                const src = img.src || '';
                                return (src.includes('image_generation') || src.includes('rc_gen_image'))
                                       && src.includes('byteimg.com');
                            });
                            return genImgs.map(i => i.src || '');
                        }""")
                        for src in final_info:
                            if src and src not in all_image_urls and src not in old_img_srcs:
                                all_image_urls.append(src)

                    # Try to get conversation_id from page URL
                    page_url = page.url
                    if "/chat/" in page_url:
                        parts = page_url.split("/chat/", 1)[1].split("?")[0].split("#")[0]
                        if parts and parts != "create-image" and parts != "":
                            conversation_id = parts
                            self._last_conversation_id = conversation_id

                    logger.info(f"[{adapter_name} ImageGen] attempt {attempt+1}: {len(all_image_urls)} URLs from DOM")

                except Exception as e:
                    logger.error(f"[{adapter_name} ImageGen] browser error: {e}")
                    last_error = str(e)

                if all_image_urls:
                    logger.info(f"[{adapter_name} ImageGen] downloading {len(all_image_urls)} images: {[u[:100] for u in all_image_urls[:3]]}")
                    downloaded = []
                    try:
                        downloaded = await browser_client.download_images_from_urls(all_image_urls, n)
                        logger.info(f"[{adapter_name} ImageGen] download completed: {len(downloaded)}/{n}")
                    except Exception as dl_e:
                        import traceback as _tb
                        logger.warning(f"[{adapter_name} ImageGen] download error: {dl_e}\n{_tb.format_exc()}")

                    await self._delete_conversation()
                    if downloaded:
                        result = {"created": int(time.time()), "data": []}
                        for local_url in downloaded[:n]:
                            # result["data"].append({"url": local_url, "revised_prompt": prompt, "size": size})
                            result["data"].append({"url": local_url})
                        return result
                    else:
                        return {"created": int(time.time()), "data": [{"url": "", "revised_prompt": prompt, "size": size, "error": "Download failed"}]}

                await self._delete_conversation()
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
            lock.release()

    # ═══════════════════════════════════════════════════════════════════════
    # 流式与非流式接口
    # ═══════════════════════════════════════════════════════════════════════

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        """流式对话（使用模板）"""
        self._last_conversation_id = ""
        async for chunk in self._stream_chat_template(request):
            yield chunk
