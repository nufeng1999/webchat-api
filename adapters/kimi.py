"""Kimi Adapter for webchat-api"""
import json
import re
import uuid
import time
import asyncio
import logging
import os
from datetime import datetime
from typing import AsyncGenerator, Optional
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, KIMI_MODEL_CONFIG
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import extract_text_from_content

logger = logging.getLogger("kimi-adapter")


class KimiAdapter(BaseAdapter):
    """Kimi (https://www.kimi.com/zh/) 适配器。"""

    def __init__(self):
        self._kimi_lock = asyncio.Lock()
        self._last_session_id = ""

    def get_adapter_name(self) -> str:
        return "kimi"

    def get_models(self) -> dict[str, dict]:
        return KIMI_MODEL_CONFIG

    async def init(self):
        logger.info("Kimi adapter initialized")

    async def close(self):
        """Close KimiAdapter resources."""
        from browser_client import browser_client
        await browser_client.close_kimi()
        logger.info("[Kimi] adapter closed")

    def _get_model_type(self, model: str) -> str:
        cfg = KIMI_MODEL_CONFIG.get(model, {})
        return cfg.get("model_type", "default")

    def _get_thinking_enabled(self, model: str) -> bool:
        cfg = KIMI_MODEL_CONFIG.get(model, {})
        return cfg.get("use_deep_think", False)

    def _get_search_enabled(self, model: str) -> bool:
        cfg = KIMI_MODEL_CONFIG.get(model, {})
        return cfg.get("use_search", False)

    def _supports_file(self, model: str) -> bool:
        cfg = KIMI_MODEL_CONFIG.get(model, {})
        return cfg.get("supports_file", True)

    # ═══════════════════════════════════════════════════════════════════════
    # Hook 方法覆盖
    # ═══════════════════════════════════════════════════════════════════════

    def _get_lock(self):
        return self._kimi_lock

    async def _on_session_id(self, value):
        self._last_session_id = value
        logger.info(f"[Kimi] session_id: {value}")

    async def _delete_conversation(self):
        """仅清除 adapter 本地状态，不删除 web 对话实例。"""
        self._last_session_id = ""

    async def _prepare_messages(self, request: ChatCompletionRequest, browser_client, is_agent: bool, reuse_conversation: bool = False):
        if not is_agent:
            last_msg = request.messages[-1] if request.messages else None
            prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
            logger.debug(f"{Colors.BLUE}Kimi prepare_messages: non-agent, text_len={len(prompt_text)}{Colors.RESET}")
            return prompt_text, None

        if reuse_conversation:
            logger.info(f"[Kimi] skipping file upload for reused conversation")
            request_dict = request.model_dump()
            last_msg = request.messages[-1] if request.messages else None
            is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
            if is_tool_return:
                logger.debug(f"------------[is_tool_return]-------------")
                prompt_text = get_ret_format_prompt(self.get_adapter_name()) + "\n " + self._get_last_three_messages_as_json(request_dict)
            else:
                prompt_text = get_exectask_prompt(self.get_adapter_name()) + "\n " + self._get_last_message_as_json(request_dict)
            return prompt_text, None

        last_msg = request.messages[-1] if request.messages else None
        is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
        file_content = self._prepare_inline_file_content(request, is_tool_return)
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
            logger.info(f"[Kimi] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
        except Exception as e:
            logger.warning(f"[Kimi] save {fname} failed: {e}")

        if is_tool_return:
            logger.debug(f"{Colors.BLUE}[Kimi]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}toolreturn.json{Colors.RESET} ({len(file_content)} bytes)")
        else:
            logger.debug(f"{Colors.BLUE}[Kimi]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}request.json{Colors.RESET} ({len(file_content)} bytes)")

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
        if len(msgs) > 25:
            new_msgs = msgs[:15] + msgs[-10:]
        else:
            new_msgs = msgs
        request_dict['messages'] = new_msgs
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
        """构建传给 browser_client.stream_kimi_chat 的 kwargs。"""
        kwargs = {"prompt": current_prompt}
        extra = getattr(self, '_stream_extra_kwargs', {})
        if extra:
            kwargs.update(extra)
        if is_agent and file_content:
            # stream_kimi_chat 期望 file_content 而不是 inline_file_content
            kwargs["file_content"] = file_content
        return kwargs

    async def _call_stream(self, **kwargs):
        from browser_client import browser_client
        async for kind, value in browser_client.stream_kimi_chat(**kwargs):
            if kind == "chunk":
                text = value.strip()
                match = re.search(r'\{.*\}', text, re.DOTALL)
                
                if match:
                    json_str = match.group(0)
                    try:
                        data = json.loads(json_str)
                        
                        # 已是完整的OpenAI格式：直接透传
                        if "choices" in data and data.get("object") == "chat.completion.chunk":
                            yield "chunk", json_str
                            continue
                        
                        # 需要包装的情况
                        if data.get("object") == "chat.requests":
                            # Kimi 完整响应格式：将 message 直接映射到 delta
                            req_list = data.get("chat.requests", [])
                            if req_list:
                                req = req_list[0]
                                message = req.get("message", {})
                                finish_reason = req.get("finish_reason", "stop")
                                sse_chunk = {
                                    "id": data.get("id", ""),
                                    "object": "chat.completion.chunk",
                                    "created": data.get("created", 0),
                                    "model": data.get("model", ""),
                                    "choices": [{
                                        "index": req.get("index", 0),
                                        "delta": message,
                                        "finish_reason": finish_reason
                                    }]
                                }
                                yield "chunk", json.dumps(sse_chunk, ensure_ascii=False)
                                continue

                        content = None
                        tool_calls = None
                        finish_reason = "stop"
                        
                        if "content" in data:
                            # 简单内容响应
                            content = data.get("content")
                        elif "tool_calls" in data:
                            # tool_calls 响应
                            tool_calls = data.get("tool_calls")
                            finish_reason = "tool_calls"
                        else:
                            # 未知格式，透传
                            yield kind, value
                            continue
                        
                        # 构建标准 OpenAI SSE chunk
                        sse_chunk = {
                            "id": data.get("id", ""),
                            "object": "chat.completion.chunk",
                            "created": data.get("created", 0),
                            "model": data.get("model", ""),
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                                "finish_reason": finish_reason
                            }]
                        }
                        yield "chunk", json.dumps(sse_chunk, ensure_ascii=False)
                        continue
                    except json.JSONDecodeError:
                        pass

                yield kind, value
            else:
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
            logger.error(f"[Kimi] non_stream_chat error: {e}")
            return {"error": str(e)}

        cleaned = self._strip_json_prefix(full_text)
        cleaned = self._strip_think_tags(cleaned)

        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": cleaned},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }