"""讯飞星火 (xinghuo.xfyun.cn) 适配器"""
import json
import time
import asyncio
import logging
import os
import base64
from datetime import datetime
from typing import AsyncGenerator
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, XINGHUO_MODEL_CONFIG
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import extract_text_from_content

logger = logging.getLogger("xinghuo-adapter")


class XinghuoAdapter(BaseAdapter):
    """讯飞星火 (https://xinghuo.xfyun.cn/desk) 适配器。"""

    def __init__(self):
        self._xinghuo_lock = asyncio.Lock()
        self._last_chat_id = ""
        self._pending_file_items = None
        self._pending_file_content = None

    def get_adapter_name(self) -> str:
        return "xinghuo"

    def get_models(self) -> dict[str, dict]:
        return XINGHUO_MODEL_CONFIG

    async def init(self):
        logger.info("Xinghuo adapter initialized")

    async def close(self):
        from browser_client import browser_client
        await browser_client.close_xinghuo()
        logger.info("[Xinghuo] adapter closed")

    def _get_model_type(self, model: str) -> str:
        cfg = XINGHUO_MODEL_CONFIG.get(model, {})
        return cfg.get("model_type", "4.0-ultra")

    def _get_thinking_enabled(self, model: str) -> bool:
        cfg = XINGHUO_MODEL_CONFIG.get(model, {})
        return cfg.get("use_deep_think", False)

    def _get_search_enabled(self, model: str) -> bool:
        cfg = XINGHUO_MODEL_CONFIG.get(model, {})
        return cfg.get("use_search", False)

    def _session_id_kind(self) -> str:
        return "chat_id"

    def _supports_file(self, model: str) -> bool:
        return True

    def _get_lock(self):
        return self._xinghuo_lock

    async def _on_session_id(self, value):
        self._last_chat_id = value
        logger.info(f"[Xinghuo] chat_id: {value}")

    async def _delete_conversation(self):
        """仅清除 adapter 本地状态，不删除 web 对话实例。"""
        self._last_chat_id = ""

    async def _prepare_messages(self, request: ChatCompletionRequest, browser_client, is_agent: bool):
        # 提取文件附件（如有）
        file_items = []
        if request.messages:
            for msg in request.messages:
                content = getattr(msg, 'content', '')
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "image_url":
                                url = item.get("image_url", {}).get("url", "")
                                if url:
                                    file_items.append({"kind": "image", "url": url})
                            elif item.get("type") == "file":
                                file_data_b64 = item.get("file", {}).get("file_data", "")
                                fname = item.get("file", {}).get("filename", "upload.bin")
                                if file_data_b64:
                                    file_items.append({"kind": "file", "data": base64.b64decode(file_data_b64), "name": fname})
        self._pending_file_items = file_items if file_items else None

        if not is_agent:
            last_msg = request.messages[-1] if request.messages else None
            prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
            logger.debug(f"{Colors.BLUE}Xinghuo prepare_messages: non-agent, text_len={len(prompt_text)}{Colors.RESET}")
            return prompt_text, None

        last_msg = request.messages[-1] if request.messages else None
        is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
        file_content = self._prepare_inline_file_content(request, is_tool_return)
        self._pending_file_content = file_content
        prompt_text = get_exectask_prompt(self.get_adapter_name())

        try:
            logs_dir = os.path.join(BASE_DIR, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = "toolreturn.json" if is_tool_return else "request.json"
            saved_path = os.path.join(logs_dir, f"{fname.rsplit('.', 1)[0]}_{ts}.json")
            with open(saved_path, 'w', encoding='utf-8') as f:
                f.write(file_content)
            logger.info(f"[Xinghuo] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
        except Exception as e:
            logger.warning(f"[Xinghuo] save {fname} failed: {e}")

        if is_tool_return:
            logger.debug(f"{Colors.BLUE}[Xinghuo]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}toolreturn.json{Colors.RESET} ({len(file_content)} bytes)")
        else:
            logger.debug(f"{Colors.BLUE}[Xinghuo]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}request.json{Colors.RESET} ({len(file_content)} bytes)")

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

    def _build_stream_kwargs(self, prompt_text: str, file_content, is_agent: bool, current_prompt: str) -> dict:
        kwargs = {"prompt": current_prompt}
        extra = getattr(self, '_stream_extra_kwargs', {})
        if extra:
            kwargs.update(extra)
        if is_agent and file_content:
            kwargs["inline_file_content"] = file_content
        # 传递文件附件给 stream_xinghuo_chat
        if self._pending_file_items:
            kwargs["file_info"] = self._pending_file_items
            self._pending_file_items = None
        return kwargs

    async def _call_stream(self, **kwargs):
        from browser_client import browser_client
        async for kind, value in browser_client.stream_xinghuo_chat(**kwargs):
            yield kind, value

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        self._last_chat_id = ""
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
            logger.error(f"[Xinghuo] non_stream_chat error: {e}")
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
