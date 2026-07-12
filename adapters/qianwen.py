import json
import uuid
import time
import base64
import asyncio
import logging
import os
from typing import AsyncGenerator, Optional
from datetime import datetime
from adapters.base import BaseAdapter
from models import ChatCompletionRequest
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import format_openai_chunk, format_openai_done, extract_text_from_content

logger = logging.getLogger("qianwen-adapter")

QIANWEN_MODELS = {}

DEFAULT_MODELS = {
    "qianwen-pro-chat": {"model": "qwen-max", "desc": "千问 Pro (Qwen Max)", "is_qianwen": True},
    "qianwen-lite-chat": {"model": "qwen-turbo", "desc": "千问 Lite (Qwen Turbo)", "is_qianwen": True},
    "qianwen-thinking": {"model": "qwen-max", "desc": "千问思考模式 (Qwen Max)", "is_qianwen": True, "use_deep_think": True},
    "qianwen-coding": {"model": "qwen-coder", "desc": "千问编程 (Qwen Coder)", "is_qianwen": True},
    "qianwen-3.7": {"model": "qwen-3.7", "desc": "Qwen3.7-千问", "is_qianwen": True},
    "qianwen-3.7-max": {"model": "qwen-3.7-max", "desc": "Qwen3.7-Max", "is_qianwen": True},
    "qianwen-3.5-flash": {"model": "qwen-3.5-flash", "desc": "Qwen3.5-Flash", "is_qianwen": True},
    "qianwen-3-max": {"model": "qwen-3-max", "desc": "Qwen3-Max", "is_qianwen": True},
    "qianwen-3-max-thinking": {"model": "qwen-3-max-thinking", "desc": "Qwen3-Max-Thinking", "is_qianwen": True, "use_deep_think": True},
    "qianwen-3-coder": {"model": "qwen-3-coder", "desc": "Qwen3-Coder", "is_qianwen": True},
}

SYSTEM_PROMPT_MAP = {
    "qianwen-coding": "你是一个专业的编程助手，擅长多种编程语言，能够编写、调试、优化代码，并解释技术概念。请用代码块格式输出代码。",
}

QIANWEN_MODELS.update(DEFAULT_MODELS)


async def refresh_qianwen_models():
    from browser_client import browser_client
    try:
        models = await browser_client.fetch_qianwen_models()
        if models:
            for m in models:
                display_name = m.get("display_name", "")
                model_id = m.get("model_id", display_name)
                if display_name:
                    key = f"qianwen-{model_id.replace('qwen-', '').replace('qwen', '')}"
                    if key not in QIANWEN_MODELS:
                        QIANWEN_MODELS[key] = {
                            "model": model_id,
                            "desc": display_name,
                            "is_qianwen": True
                        }
            logger.info(f"[Qwen] Models refreshed: {list(QIANWEN_MODELS.keys())}")
    except Exception as e:
        logger.warning(f"[Qwen] Failed to refresh models: {e}")


class QianwenAdapter(BaseAdapter):
    """千问 (qianwen.com) 适配器，使用模板模式。"""

    def __init__(self):
        self._session_id = ""
        self._topic_id = ""
        self._last_session_id = ""
        self._qianwen_lock = asyncio.Lock()
        self._last_model = None
        self._pending_messages: list = []

    def get_adapter_name(self) -> str:
        return "qianwen"

    def get_models(self) -> dict[str, dict]:
        return QIANWEN_MODELS

    async def init(self):
        logger.info("Qianwen adapter initialized")

    async def close(self):
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # Hook 方法（模板调用）
    # ═══════════════════════════════════════════════════════════════════════

    def _get_lock(self):
        return self._qianwen_lock

    async def _prepare_messages(self, request, browser_client, is_agent: bool, reuse_conversation: bool = False):
        if not is_agent:
            file_items = self._extract_file_items(request)
            if file_items:
                for fi in file_items:
                    try:
                        if fi["kind"] == "image":
                            data = await self._download_url(fi["url"])
                            ext = fi['url'].rsplit('.', 1)[-1] if '.' in fi['url'] else 'png'
                            await browser_client.upload_file_via_qianwen_page(data, f"image.{ext}")
                            await asyncio.sleep(8)
                        elif fi["kind"] == "file":
                            await browser_client.upload_file_via_qianwen_page(fi["data"], fi["name"])
                            await asyncio.sleep(8)
                    except Exception as e:
                        logger.error(f"[Qwen] file upload error: {e}")
            last_msg = request.messages[-1] if request.messages else None
            last_text = self._extract_last_text(last_msg) if last_msg else ""
            last_text = last_text.replace('\n', ' ').replace('\r', ' ')
            logger.info(f"Qianwen stream_chat: model={request.model}, last_text={len(last_text)} chars")
            self._pending_messages = [{
                "mime_type": "text/plain",
                "content": last_text,
                "meta_data": {"ori_query": last_text},
                "status": "complete"
            }]
            return last_text, None

        if reuse_conversation:
            logger.info(f"[Qianwen] skipping file upload for reused conversation")
            request_dict = request.model_dump()
            last_msg = request.messages[-1] if request.messages else None
            last_role = getattr(last_msg, 'role', '') if last_msg else ''
            if last_role == 'tool':
                logger.debug(f"------------[is_tool_return]-------------")
                prompt_text = get_ret_format_prompt(self.get_adapter_name()) + "\n " + self._get_last_three_messages_as_json(request_dict)
            else:
                prompt_text = get_exectask_prompt(self.get_adapter_name()) + "\n " + self._get_last_message_as_json(request_dict)

            try:
                file_name = "request.json"
                if last_role == 'tool':
                    file_name = "toolreturn.json"
                    request_dict['task'] = get_ret_format_prompt(self.get_adapter_name())
                    request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')
                request_json = json.dumps(request_dict, ensure_ascii=False, indent=2)
                logs_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(logs_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tool_path = os.path.join(logs_dir, f"toolreturn_{ts}.{file_name.rsplit('.', 1)[-1] if '.' in file_name else 'json'}")
                with open(tool_path, 'w', encoding='utf-8') as f:
                    f.write(request_json)
                logger.info(f"[Qianwen] saved {Colors.BOLD_RED}{file_name}{Colors.RESET} to {tool_path}")
            except Exception as e:
                logger.warning(f"[Qianwen] save {file_name} failed: {e}")

            self._pending_messages = [{
                "mime_type": "text/plain",
                "content": prompt_text,
                "meta_data": {"ori_query": prompt_text},
                "status": "complete"
            }]
            return prompt_text, None

        last_msg = request.messages[-1] if request.messages else None
        last_role = getattr(last_msg, 'role', '') if last_msg else ''
        file_name = "request.json"
        if last_role == 'tool':
            file_name = "toolreturn.json"
            request_dict = request.model_dump()
            request_dict['task'] = get_ret_format_prompt(self.get_adapter_name())
            request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')
            request_json = json.dumps(request_dict, ensure_ascii=False, indent=2)
            try:
                logs_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(logs_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tool_path = os.path.join(logs_dir, f"toolreturn_{ts}.{file_name.rsplit('.', 1)[-1] if '.' in file_name else 'json'}")
                with open(tool_path, 'w', encoding='utf-8') as f:
                    f.write(request_json)
                logger.info(f"[Qwen] saved {Colors.BOLD_RED}toolreturn.json{Colors.RESET} to {tool_path}")
                await browser_client.upload_file_via_qianwen_page(
                    file_data=request_json.encode('utf-8'), file_name=tool_path
                )
                logger.info(f"[Qwen] uploaded toolreturn.json ({len(request_json)} bytes)")
            except Exception as e:
                logger.error(f"[Qwen] upload toolreturn.json failed: {e}")
                raise
            prompt_text =get_ret_format_prompt(self.get_adapter_name()) + "\n " + self._get_last_three_messages_as_json(request_dict)
            logger.info(f"Qianwen tool return: model={request.model}, uploaded {tool_path}")
        else:
            file_name = "request.json"
            request_dict = request.model_dump()
            prompt_text = get_exectask_prompt(self.get_adapter_name()) + "\n " + self._get_last_message_as_json(request_dict)
            request_dict['task'] = get_webchat_task(self.get_adapter_name())
            request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')
            request_json = json.dumps(request_dict, ensure_ascii=False, indent=2)
            try:
                logs_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(logs_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                saved_path = os.path.join(logs_dir, f"request_{ts}.{file_name.rsplit('.', 1)[-1] if '.' in file_name else 'json'}")
                with open(saved_path, 'w', encoding='utf-8') as f:
                    f.write(request_json)
                logger.info(f"[Qwen] saved {Colors.BOLD_RED}request.json{Colors.RESET} to {saved_path}")
                await browser_client.upload_file_via_qianwen_page(
                    file_data=request_json.encode('utf-8'), file_name=saved_path
                )
                logger.info(f"[Qwen] uploaded {Colors.BOLD_RED}request.json{Colors.RESET} ({len(request_json)} bytes)")
            except Exception as e:
                logger.error(f"[Qwen] upload request.json failed: {e}")
                raise
            logger.info(f"Qianwen agent request: model={request.model}, exectask_prompt ({len(prompt_text)} chars)")

        self._pending_messages = [{
            "mime_type": "text/plain",
            "content": prompt_text,
            "meta_data": {"ori_query": prompt_text},
            "status": "complete"
        }]
        return prompt_text, None

    def _build_stream_kwargs(self, prompt_text: str, file_content, is_agent: bool, current_prompt: str) -> dict:
        return {
            "messages": self._pending_messages,
            "session_id": self._session_id,
            "topic_id": self._topic_id,
        }

    async def _call_stream(self, **kwargs):
        from browser_client import browser_client
        messages = kwargs.get("messages", [])
        session_id = kwargs.get("session_id", "")
        topic_id = kwargs.get("topic_id", "")
        async for kind, value in browser_client.stream_qianwen_chat(messages, session_id, topic_id):
            yield kind, value

    async def _on_session_id(self, value):
        self._session_id = value
        self._last_session_id = value
        logger.info(f"[Qwen] session_id: {value}")

    async def _delete_conversation(self):
        """仅清除 adapter 本地状态，不删除 web 对话实例。"""
        self._session_id = ""
        self._last_session_id = ""

    def _use_parse_error_history(self) -> bool:
        return False

    def _stream_error_no_delete(self) -> bool:
        return True

    async def _on_done_extra(self):
        """done 后重新捕获 session_id（带重试）。"""
        try:
            from browser_client import browser_client
            for attempt in range(10):
                sid = await browser_client.get_qianwen_session_id()
                if sid:
                    self._session_id = sid
                    self._last_session_id = sid
                    logger.info(f"[Qwen] captured session_id after done: {sid}")
                    return
                await asyncio.sleep(0.5)
            logger.debug(f"[Qwen] no session_id captured after done (url may not contain /chat/)")
        except Exception as e:
            logger.debug(f"[Qwen] _on_done_extra session_id capture error: {e}")

    async def _on_finally_extra(self):
        """finally 中捕获 session_id（带重试）。"""
        try:
            from browser_client import browser_client
            for attempt in range(10):
                sid = await browser_client.get_qianwen_session_id()
                if sid:
                    self._session_id = sid
                    self._last_session_id = sid
                    logger.debug(f"[Qwen] captured session_id in finally: {sid}")
                    return
                await asyncio.sleep(0.5)
            logger.debug(f"[Qwen] no session_id captured in finally")
        except Exception as e:
            logger.debug(f"[Qwen] _on_finally_extra session_id capture error: {e}")

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        self._session_id = ""
        self._last_session_id = ""
        if request.model != self._last_model:
            from browser_client import browser_client
            model_id = self._get_model_name(request.model)
            await browser_client.select_qianwen_model(model_id)
            self._last_model = request.model
        async for chunk in self._stream_chat_template(request):
            yield chunk

    # ═══════════════════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════════════════

    def _get_model_name(self, model: str) -> str:
        cfg = QIANWEN_MODELS.get(model, {})
        return cfg.get("model", "qwen-max")

    def _extract_file_items(self, request: ChatCompletionRequest) -> list[dict]:
        file_items = []
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
        return file_items

    async def _download_url(self, url: str) -> bytes:
        if url.startswith("data:"):
            _, b64 = url.split(",", 1)
            return base64.b64decode(b64)
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    def _extract_last_text(self, message) -> str:
        return extract_text_from_content(getattr(message, 'content', ''))

    async def _delete_qianwen_conversation(self):
        session_id = self._session_id
        if session_id:
            try:
                from browser_client import browser_client
                await browser_client.delete_qianwen_conversation(session_id)
                logger.info(f"[Qianwen] deleted session {session_id}")
            except Exception as e:
                logger.warning(f"[Qianwen] delete session {session_id} exception: {e}")
        else:
            logger.debug("[Qianwen] no session to delete (session_id empty)")
        self._session_id = ""
        self._last_session_id = ""
