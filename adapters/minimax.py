"""MiniMax Agent Adapter for webchat-api"""
import json
import uuid
import time
import asyncio
import logging
import os
from datetime import datetime
from typing import AsyncGenerator, Optional
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, MINIMAX_MODEL_CONFIG
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import extract_text_from_content

logger = logging.getLogger("minimax-adapter")


class MinimaxAdapter(BaseAdapter):
    """MiniMax Agent (https://agent.minimaxi.com/) 适配器。"""

    def __init__(self):
        self._minimax_lock = asyncio.Lock()
        self._last_session_id = ""
        self._pending_file_items = None

    def get_adapter_name(self) -> str:
        return "minimax"

    def get_models(self) -> dict[str, dict]:
        return MINIMAX_MODEL_CONFIG

    async def init(self):
        logger.info("Minimax adapter initialized")

    async def close(self):
        from browser_client import browser_client
        await browser_client.close_minimax()
        logger.info("[Minimax] adapter closed")

    def _get_model_type(self, model: str) -> str:
        cfg = MINIMAX_MODEL_CONFIG.get(model, {})
        return cfg.get("model_type", "m3")

    def _get_thinking_enabled(self, model: str) -> bool:
        cfg = MINIMAX_MODEL_CONFIG.get(model, {})
        return cfg.get("use_deep_think", False)

    def _get_search_enabled(self, model: str) -> bool:
        cfg = MINIMAX_MODEL_CONFIG.get(model, {})
        return cfg.get("use_search", False)

    def _supports_file(self, model: str) -> bool:
        cfg = MINIMAX_MODEL_CONFIG.get(model, {})
        return cfg.get("supports_file", True)

    # ═══════════════════════════════════════════════════════════════════════
    # Hook 方法覆盖
    # ═══════════════════════════════════════════════════════════════════════

    def _get_lock(self):
        return self._minimax_lock

    async def _on_session_id(self, value):
        self._last_session_id = value
        logger.info(f"[Minimax] session_id: {value}")

    async def _delete_conversation(self):
        """仅清除 adapter 本地状态，不删除 web 对话实例。"""
        self._last_session_id = ""

    async def _prepare_messages(self, request: ChatCompletionRequest, browser_client, is_agent: bool, reuse_conversation: bool = False):
        import base64

        if not is_agent:
            last_msg = request.messages[-1] if request.messages else None
            prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
            logger.debug(f"{Colors.BLUE}Minimax prepare_messages: non-agent, text_len={len(prompt_text)}{Colors.RESET}")
            self._pending_file_items = None
            return prompt_text, None

        # For agent mode: extract file items only if NOT reusing conversation
        if reuse_conversation:
            logger.info(f"[Minimax] skipping file upload for reused conversation")
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
                file_content = self._prepare_inline_file_content(request, is_tool_return)
                fname = "toolreturn.json" if is_tool_return else "request.json"
                saved_path = os.path.join(logs_dir, f"{fname.rsplit('.', 1)[0]}_{ts}.json")
                with open(saved_path, 'w', encoding='utf-8') as f:
                    f.write(file_content)
                logger.info(f"[Minimax] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
            except Exception as e:
                logger.warning(f"[Minimax] save {fname} failed: {e}")

            self._pending_file_items = None
            return prompt_text, None

        # New conversation: extract file items
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

        last_msg = request.messages[-1] if request.messages else None
        is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
        
        if not is_tool_return and isinstance(getattr(last_msg, 'content', None), list):
                if len(request.messages) >= 2:
                    last_msg = request.messages[-2] if request.messages else None
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
            logger.info(f"[Minimax] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
        except Exception as e:
            logger.warning(f"[Minimax] save {fname} failed: {e}")

        if is_tool_return:
            logger.debug(f"{Colors.BLUE}[Minimax]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}toolreturn.json{Colors.RESET} ({len(file_content)} bytes)")
        else:
            logger.debug(f"{Colors.BLUE}[Minimax]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}request.json{Colors.RESET} ({len(file_content)} bytes)")

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
        kwargs = {"prompt": current_prompt}
        extra = getattr(self, '_stream_extra_kwargs', {})
        if extra:
            kwargs.update(extra)
        if is_agent and file_content:
            kwargs["inline_file_content"] = file_content
        if self._pending_file_items:
            kwargs["file_info"] = self._pending_file_items[0]
            self._pending_file_items = None
        return kwargs

    async def _call_stream(self, **kwargs):
        from browser_client import browser_client
        prompt = kwargs.get("prompt", "")
        inline_file_content = kwargs.get("inline_file_content")
        file_info = kwargs.get("file_info")
        model_name = kwargs.get("model_name", "MiniMax-M3")
        thinking_enabled = kwargs.get("thinking_enabled", False)
        search_enabled = kwargs.get("search_enabled", False)
        
        file_result = None
        
        if file_info:
            kind = file_info.get("kind", "file")
            if kind == "file":
                file_data = file_info.get("data", b"")
                file_name = file_info.get("name", "upload.bin")
                mime = "text/plain"
                if file_name.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                    mime = "image/" + file_name.rsplit('.', 1)[-1].lower()
                    if mime == "image/jpg":
                        mime = "image/jpeg"
                elif file_name.endswith('.pdf'):
                    mime = "application/pdf"
                logger.info(f"[Minimax] Uploading file: {file_name} ({len(file_data)} bytes)")
                try:
                    file_result = await browser_client.upload_minimax_file_via_ui(
                        file_data=file_data,
                        file_name=file_name,
                        mime_type=mime,
                    )
                    logger.info(f"[Minimax] File uploaded via UI: file_id={file_result['file_id']}")
                except Exception as e:
                    logger.error(f"[Minimax] File upload via UI failed: {e}")
                    yield ("error", str(e))
                    return
            elif kind == "image":
                url = file_info.get("url", "")
                if url:
                    file_result = {"file_url": url, "file_name": "image.png", "mime_type": "image/png"}
        
        content = inline_file_content if (inline_file_content and not file_result) else prompt
        
        # 优先使用 UI 发送方式（参考 MiMo 实现），确保消息能发送出去
        try:
            logger.info("[Minimax] Trying UI send mode...")
            ui_sent = False
            async for kind, value in browser_client.send_minimax_message_via_ui(
                content=content,
                model_name=model_name,
                thinking_enabled=thinking_enabled,
                search_enabled=search_enabled,
            ):
                ui_sent = True
                yield kind, value
            if ui_sent:
                logger.info("[Minimax] UI send mode succeeded")
                return
        except Exception as e:
            logger.warning(f"[Minimax] UI send mode failed: {e}, falling back to API mode")

        # 回退到 API 签名方式发送
        logger.info("[Minimax] Falling back to API send mode...")
        session_id = await browser_client.create_minimax_session(model_name=model_name)
        self._last_session_id = session_id
        logger.info(f"[Minimax] Session: {session_id}, sending via API...")
        
        async for kind, value in browser_client.send_minimax_message_with_sse(
            session_id=session_id,
            content=content,
            attachments=[file_result] if file_result else None,
            model_name=model_name,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
        ):
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
            logger.error(f"[Minimax] non_stream_chat error: {e}")
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
