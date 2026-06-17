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
    """从千问页面刷新模型列表，更新 QIANWEN_MODELS。"""
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
    """千问 (qianwen.com) 适配器。"""

    def __init__(self):
        self._session_id = ""
        self._topic_id = ""
        self._qianwen_lock = asyncio.Lock()
        self._last_model = None

    def get_adapter_name(self) -> str:
        return "qianwen"

    def get_models(self) -> dict[str, dict]:
        return QIANWEN_MODELS

    async def init(self):
        logger.info("Qianwen adapter initialized")

    async def close(self):
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # Qianwen 专有方法
    # ═══════════════════════════════════════════════════════════════════════

    async def _delete_qianwen_conversation(self):
        """删除当前 attempt 的千问历史对话。"""
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

    def _get_model_name(self, model: str) -> str:
        cfg = QIANWEN_MODELS.get(model, {})
        return cfg.get("model", "qwen-max")

    def _build_messages(self, request: ChatCompletionRequest) -> list:
        qianwen_messages = []
        for msg in request.messages:
            role = getattr(msg, 'role', 'user')
            content = getattr(msg, 'content', '')
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "image_url":
                            url = item.get("image_url", {}).get("url", "")
                            if url:
                                text_parts.append(f"[image: {url[:80]}]")
                        elif item.get("type") == "file":
                            fname = item.get("file", {}).get("filename", "file")
                            text_parts.append(f"[file: {fname}]")
                content = "\n".join(text_parts)
            qianwen_messages.append({
                "mime_type": "text/plain",
                "content": content,
                "meta_data": {"ori_query": content},
                "status": "complete"
            })
        return qianwen_messages

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

    async def upload_file(self, file_data: bytes, file_name: str) -> str:
        from browser_client import browser_client
        return await browser_client.upload_file_via_qianwen_page(file_data, file_name)

    async def _download_url(self, url: str) -> bytes:
        if url.startswith("data:"):
            _, b64 = url.split(",", 1)
            return base64.b64decode(b64)
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    def _render_messages_as_text(self, messages: list) -> str:
        parts = []
        for i, m in enumerate(messages):
            role = getattr(m, 'role', '') or ''
            name = getattr(m, 'name', None)
            tool_call_id = getattr(m, 'tool_call_id', None)
            tool_calls = getattr(m, 'tool_calls', None)
            content = getattr(m, 'content', None)

            header = f"[{i}] role={role}"
            if name:
                header += f" name={name}"
            if tool_call_id:
                header += f" tool_call_id={tool_call_id}"
            parts.append(header)

            if tool_calls:
                try:
                    parts.append("tool_calls:")
                    parts.append(json.dumps(tool_calls, ensure_ascii=False, indent=2))
                except Exception:
                    parts.append(f"tool_calls: {tool_calls}")

            if isinstance(content, str):
                parts.append("content:")
                parts.append(content)
            elif isinstance(content, list):
                parts.append("content:")
                try:
                    parts.append(json.dumps(content, ensure_ascii=False, indent=2))
                except Exception:
                    parts.append(str(content))
            elif content is None:
                parts.append("content:")
                parts.append("<null>")
            else:
                parts.append("content:")
                try:
                    parts.append(json.dumps(content, ensure_ascii=False, indent=2))
                except Exception:
                    parts.append(str(content))

            parts.append("")
        return "\n".join(parts)

    def _extract_last_text(self, message) -> str:
        return extract_text_from_content(getattr(message, 'content', ''))

    async def _prepare_messages(self, request, browser_client, is_agent, file_content=""):
        """准备千问的消息列表。对于非 agent 请求，需要处理文件上传；对于 agent 请求，直接返回提示词。"""
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
            return [{
                "mime_type": "text/plain",
                "content": last_text,
                "meta_data": {"ori_query": last_text},
                "status": "complete"
            }]

        last_msg = request.messages[-1] if request.messages else None
        last_role = getattr(last_msg, 'role', '') if last_msg else ''
        file_name = "request.json"
        if last_role == 'tool':
            file_name = "toolreturn.json"
            request_dict = request.model_dump()
            request_dict['task'] = get_ret_format_prompt()
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
            prompt_text = get_exectask_prompt()
            logger.info(f"Qianwen tool return: model={request.model}, uploaded {tool_path}")
        else:
            file_name = "request.json"
            prompt_text = get_exectask_prompt()
            request_dict = request.model_dump()
            request_dict['task'] = get_webchat_task()
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

        return [{
            "mime_type": "text/plain",
            "content": prompt_text,
            "meta_data": {"ori_query": prompt_text},
            "status": "complete"
        }]

    # ═══════════════════════════════════════════════════════════════════════
    # 核心方法：stream_chat
    # ═══════════════════════════════════════════════════════════════════════

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client
        logger.debug(f"{Colors.BOLD_GREEN}[Doubao] -------------------new request---------------------{Colors.RESET}")
        chat_id = self._generate_chat_id()
        model = request.model
        is_agent = self._is_agent_request(request)
        self._session_id = ""

        await self._qianwen_lock.acquire()
        try:
            # 如果模型发生变化，在页面上切换模型
            if model != self._last_model:
                model_id = self._get_model_name(model)
                await browser_client.select_qianwen_model(model_id)
                self._last_model = model

            # 准备消息
            messages = await self._prepare_messages(request, browser_client, is_agent)
            logger.info(f"Qianwen stream_chat: model={model}, messages={len(messages)}, is_agent={is_agent}")

            max_retries = 3
            for attempt in range(max_retries):
                full_text = ""
                suppress_text = False
                buffered_chunks = [] if is_agent else None

                logger.info(f"[Qianwen Adapter] Attempt {attempt+1}/{max_retries}: calling stream_qianwen_chat")

                async for kind, value in browser_client.stream_qianwen_chat(messages, self._session_id, self._topic_id):
                    if kind == "error":
                        yield self._format_error(str(value), model, chat_id)
                        return
                    processed = await self._handle_chunk_streaming(
                        kind, value,
                        model=model,
                        chat_id=chat_id,
                        is_agent=is_agent,
                        full_text=full_text,
                        suppress_text=suppress_text,
                        buffered_chunks=buffered_chunks
                    )
                    full_text, suppress_text, buffered_chunks, should_return, return_value = processed
                    if should_return and return_value is not None:
                        yield return_value
                        if kind == "error":
                            return
                    if kind == "done":
                        logger.debug(f"Qianwen done: suppress_text={suppress_text}, full_text_len={len(full_text)}, full_text_preview=\n{full_text[:1000]!r}")
                        # 重要：done 意味着流结束，立即获取 session_id（用于可能的删除）
                        try:
                            self._session_id = await browser_client.get_qianwen_session_id()
                            if self._session_id:
                                logger.info(f"[Qwen] got session_id after done: {self._session_id}")
                        except Exception:
                            pass
                        try:
                            content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls = self._parse_response(full_text)
                            logger.info(f"Qianwen done: content={content!r}, tool_calls={tool_calls!r}, finish_reason={finish_reason!r}, is_openai_chunk={is_openai_chunk}, is_tool_calls={is_tool_calls}")

                            # 验证响应是否合法
                            should_retry, err_msg, tool_return_content, full_text = self._validate_done_response(
                                content, tool_calls, finish_reason,
                                is_openai_chunk, is_tool_calls,
                                suppress_text, is_agent, False, full_text
                            )

                            if should_retry:
                                logger.warning(f"Qianwen retry (attempt {attempt+1}/{max_retries}): {err_msg}")
                                if attempt < max_retries - 1:
                                    await asyncio.sleep(5)
                                    await self._delete_qianwen_conversation()
                                    break
                                else:
                                    yield self._format_error("服务器内部错误！", model, chat_id)
                                    return

                            # 非重试路径，正常 yield 响应
                            async for chunk in self._yield_final_response(
                                content, tool_calls, finish_reason,
                                suppress_text, is_agent, buffered_chunks, full_text,
                                model, chat_id, is_openai_chunk, is_tool_calls
                            ):
                                yield chunk
                            # 成功完成后也获取一次 session_id（确保最新）
                            try:
                                self._session_id = await browser_client.get_qianwen_session_id()
                                if self._session_id:
                                    logger.debug(f"[Qwen] captured session_id: {self._session_id}")
                            except Exception:
                                pass
                            return
                        except Exception as e:
                            logger.error(f"Qianwen done handler error: {e}")
                            yield self._format_error("服务器内部错误！", model, chat_id)
                            return

                yield self._format_error("服务器内部错误！", model, chat_id)
        finally:
            try:
                sid = await browser_client.get_qianwen_session_id()
                if sid:
                    self._session_id = sid
                    logger.debug(f"[Qwen] captured session_id: {sid}")
            except Exception:
                pass
            self._qianwen_lock.release()

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
            logger.error(f"[Qianwen] non_stream_chat error: {e}")
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
