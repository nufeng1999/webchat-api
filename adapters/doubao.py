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
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
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

    async def _prepare_messages(self, request, browser_client, is_agent: bool):
        """准备 Doubao 的请求数据。对于非 agent 请求，提取最后一条消息文本；对于 agent 请求，序列化 request 为 JSON。"""
        if not is_agent:
            last_msg = request.messages[-1] if request.messages else None
            prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
            logger.debug(f"{Colors.BLUE}Doubao prepare_messages: non-agent, text_len={len(prompt_text)}{Colors.RESET}")
            return prompt_text, None

        last_msg = request.messages[-1] if request.messages else None
        is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
        file_content = await self._prepare_inline_file_content(request, is_tool_return)
        prompt_text = get_exectask_prompt()

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
        """构建传给 _call_stream 的 kwargs。"""
        kwargs = {"text": current_prompt}
        if is_agent and file_content:
            kwargs["inline_file_content"] = f"[文件 request.json 内容]\n{file_content}\n[/文件内容]"
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
        """删除当前对话。"""
        await self._delete_current_conversation()

    async def _handle_rate_limit(self, attempt: int, max_retries: int):
        """处理限流：显示可见浏览器让用户处理，等待最多 180 秒。返回 True 表示已处理可继续重试。"""
        if attempt < max_retries - 1:
            logger.warning("[Doubao] rate limited! showing visible browser for user to handle...")
            try:
                from browser_client import browser_client
                await browser_client.show_doubao_for_rate_limit()
            except Exception as e:
                logger.warning(f"[Doubao] failed to show visible browser: {e}")
            logger.info("[Doubao] waiting up to 180 seconds for user to handle rate limit...")
            for sec in range(0, 180, 10):
                await asyncio.sleep(10)
                try:
                    from browser_client import browser_client
                    if browser_client._doubao_page and not browser_client._doubao_page.is_closed():
                        break
                    else:
                        logger.info(f"[Doubao] browser closed by user after {sec+10}s, stopping wait")
                        break
                except Exception:
                    logger.info(f"[Doubao] browser check failed after {sec+10}s, assuming closed")
                    break
            try:
                from browser_client import browser_client
                await browser_client.hide_doubao_browser()
            except Exception as e:
                logger.warning(f"[Doubao] failed to hide visible browser: {e}")
            logger.info(f"[Doubao] resuming after rate limit handling, retry {attempt+2}/{max_retries}")
            return True
        return False

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
            request_dict['task'] = get_ret_format_prompt()
        else:
            request_dict['task'] = get_webchat_task()

        request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')

        msgs = request_dict.get('messages', [])
        if len(msgs) > 5:
            msgs = msgs[-5:]
        request_dict['messages'] = msgs

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
    # 流式与非流式接口
    # ═══════════════════════════════════════════════════════════════════════

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        """流式对话（使用模板）"""
        self._last_conversation_id = ""
        async for chunk in self._stream_chat_template(request):
            yield chunk
