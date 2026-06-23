"""z.ai Adapter for webchat-api"""
import json
import uuid
import time
import asyncio
import logging
import os
from datetime import datetime
from typing import AsyncGenerator, Optional
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, ZAI_MODEL_CONFIG
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import extract_text_from_content

logger = logging.getLogger("zai-adapter")


class ZaiAdapter(BaseAdapter):
    """z.ai (https://chat.z.ai/) 适配器。"""

    def __init__(self):
        self._zai_lock = asyncio.Lock()
        self._last_session_id = ""

    def get_adapter_name(self) -> str:
        return "zai"

    def get_models(self) -> dict[str, dict]:
        return ZAI_MODEL_CONFIG

    async def init(self):
        logger.info("Zai adapter initialized")

    async def close(self):
        """Close ZaiAdapter resources."""
        from browser_client import browser_client
        await browser_client.close_zai()
        logger.info("[Zai] adapter closed")

    def _get_model_type(self, model: str) -> str:
        cfg = ZAI_MODEL_CONFIG.get(model, {})
        return cfg.get("model_type", "default")

    def _get_thinking_enabled(self, model: str) -> bool:
        cfg = ZAI_MODEL_CONFIG.get(model, {})
        return cfg.get("use_deep_think", False)

    def _get_search_enabled(self, model: str) -> bool:
        cfg = ZAI_MODEL_CONFIG.get(model, {})
        return cfg.get("use_search", False)

    def _supports_file(self, model: str) -> bool:
        cfg = ZAI_MODEL_CONFIG.get(model, {})
        return cfg.get("supports_file", True)

    # ═══════════════════════════════════════════════════════════════════════
    # Hook 方法覆盖
    # ═══════════════════════════════════════════════════════════════════════

    def _get_lock(self):
        return self._zai_lock

    async def _on_session_id(self, value):
        self._last_session_id = value
        logger.info(f"[Zai] session_id: {value}")

    async def _delete_conversation(self):
        session_id = self._last_session_id
        if session_id:
            try:
                from browser_client import browser_client
                await browser_client.delete_zai_conversation(session_id)
                logger.info(f"[Zai] deleted session {session_id}")
            except Exception as e:
                logger.warning(f"[Zai] delete session {session_id} exception: {e}")
        else:
            logger.debug("[Zai] no session to delete (session_id empty)")
        self._last_session_id = ""

    async def _prepare_messages(self, request: ChatCompletionRequest, browser_client, is_agent: bool):
        if not is_agent:
            last_msg = request.messages[-1] if request.messages else None
            prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
            logger.debug(f"{Colors.BLUE}Zai prepare_messages: non-agent, text_len={len(prompt_text)}{Colors.RESET}")
            return prompt_text, None

        last_msg = request.messages[-1] if request.messages else None
        is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
        file_content = self._prepare_inline_file_content(request, is_tool_return)

        prompt_text = get_exectask_prompt(self.get_adapter_name())

        try:
            logs_dir = os.path.join(BASE_DIR, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = "toolreturn.json" if is_tool_return else "request.json"
            saved_path = os.path.join(logs_dir, f"{fname.rsplit('.', 1)[0]}_{ts}.json")
            with open(saved_path, 'w', encoding='utf-8') as f:
                f.write(file_content)
            logger.info(f"[Zai] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
        except Exception as e:
            logger.warning(f"[Zai] save {fname} failed: {e}")

        if is_tool_return:
            logger.debug(f"{Colors.BLUE}[Zai]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}toolreturn.json{Colors.RESET} ({len(file_content)} bytes)")
        else:
            logger.debug(f"{Colors.BLUE}[Zai]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}request.json{Colors.RESET} ({len(file_content)} bytes)")

        return prompt_text, file_content

    def _prepare_inline_file_content(self, request: ChatCompletionRequest, is_tool_return: bool) -> str:
        from config import escape_md_for_json
        request_dict = request.model_dump()
        if is_tool_return:
            request_dict['task'] = get_ret_format_prompt(self.get_adapter_name())
        else:
            request_dict['task'] = get_webchat_task(self.get_adapter_name())
        request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')
        msgs = request_dict.get('messages', [])
        if len(msgs) > 5:
            msgs = msgs[-5:]
        request_dict['messages'] = msgs
        return json.dumps(request_dict, ensure_ascii=False, indent=None, separators=(',', ':'))

    def _build_retry_prompt(self, prompt_text: str, is_tool_return: bool, parse_error_history: list) -> str:
        if not parse_error_history:
            return prompt_text
        err_msg, raw_text = parse_error_history[-1]
        if is_tool_return:
            extra_hint = "，不要给回复 JSON 的 delta.content 赋值为 修正后的json内容"
        else:
            extra_hint = ""
        return (
            f"{prompt_text}\n\n"
            f"[系统提示：你上一次回复的JSON格式解析失败，请修正这个错误后重新输出完整且合法的JSON{extra_hint}。]\n"
            f"[Error：{err_msg}]\n"
            f"[你的原始回复（前500字符）：{raw_text[:2000]}]"
        )

    async def _call_stream(self, **kwargs):
        from browser_client import browser_client
        async for kind, value in browser_client.stream_zai_chat(**kwargs):
            yield kind, value

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        self._last_session_id = ""
        self._stream_extra_kwargs = {
            "model_type": self._get_model_type(request.model),
            "thinking_enabled": self._get_thinking_enabled(request.model),
            "search_enabled": self._get_search_enabled(request.model),
            "model_name": request.model,
        }
        async for chunk in self._stream_chat_template(request):
            yield chunk

    async def non_stream_chat(self, request: ChatCompletionRequest) -> dict:
        chat_id = self._generate_chat_id()
        full_text = ""
        try:
            async for chunk in self.stream_chat(request):
                try:
                    text = chunk.decode('utf-8', errors='replace')
                    if text.startswith("data: ") and not text.startswith("data: [DONE]"):
                        data_str = text[6:].strip()
                        if data_str:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_text += content
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Zai] non_stream_chat error: {e}")
            return {"error": str(e)}

        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
