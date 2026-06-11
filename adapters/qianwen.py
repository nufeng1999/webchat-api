import json
import uuid
import time
import base64
import asyncio
import logging
from typing import AsyncGenerator, Optional
from urllib.parse import urlparse
import os

from adapters.base import BaseAdapter
from models import ChatCompletionRequest
from config import CONFIG, BASE_DIR
from sse import format_openai_chunk, format_openai_done

logger = logging.getLogger("qianwen-adapter")

QIANWEN_MODELS = {
    "qianwen-pro-chat": {"model": "qwen-max", "desc": "千问 Pro (Qwen Max)", "is_qianwen": True},
    "qianwen-lite-chat": {"model": "qwen-turbo", "desc": "千问 Lite (Qwen Turbo)", "is_qianwen": True},
    "qianwen-thinking": {"model": "qwen-max", "desc": "千问思考模式 (Qwen Max)", "is_qianwen": True, "use_deep_think": True},
    "qianwen-coding": {"model": "qwen-coder", "desc": "千问编程 (Qwen Coder)", "is_qianwen": True},
}

SYSTEM_PROMPT_MAP = {
    "qianwen-coding": "你是一个专业的编程助手，擅长多种编程语言，能够编写、调试、优化代码，并解释技术概念。请用代码块格式输出代码。",
}


class QianwenAdapter(BaseAdapter):
    """千问 (qianwen.com) 适配器。
    
    通过浏览器代理访问千问网页版，利用页面 JS 处理签名。
    API 端点: https://chat2.qianwen.com/api/v2/chat
    
    未登录用户也可以使用千问网页版进行基础对话。
    """

    def __init__(self):
        self._session_id = ""
        self._topic_id = ""

    def get_adapter_name(self) -> str:
        return "qianwen"

    def get_models(self) -> dict[str, dict]:
        return QIANWEN_MODELS

    async def init(self):
        logger.info("Qianwen adapter initialized")

    async def close(self):
        pass

    def _get_model_name(self, model: str) -> str:
        cfg = QIANWEN_MODELS.get(model, {})
        return cfg.get("model", "qwen-max")

    def _build_messages(self, request: ChatCompletionRequest) -> list:
        """将 OpenAI 格式消息转换为千问格式。
        支持 text、image_url、file 类型内容。"""
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
        """从请求中提取需要上传的文件/图片项。"""
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
        """上传文件到千问页面，返回上传后的引用。"""
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

    def _is_agent_request(self, request: ChatCompletionRequest) -> bool:
        has_agent_traits = False
        if request.messages:
            for m in request.messages:
                if getattr(m, 'tool_calls', None) is not None or getattr(m, 'role', None) == 'tool':
                    has_agent_traits = True
                    break
            if not has_agent_traits:
                for m in request.messages:
                    if getattr(m, 'role', None) == 'system':
                        has_agent_traits = True
                        break
        return has_agent_traits

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

    def _extract_last_text_content(self, message) -> str:
        from sse import extract_text_from_content
        return extract_text_from_content(getattr(message, 'content', ''))

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model = request.model

        # 判断是否为 agent 请求
        is_agent = self._is_agent_request(request)

        # 非 agent 请求：保留原有的文件上传逻辑
        if not is_agent:
            file_items = self._extract_file_items(request)
            if file_items:
                for fi in file_items:
                    try:
                        if fi["kind"] == "image":
                            data = await self._download_url(fi["url"])
                            ext = fi['url'].rsplit('.', 1)[-1] if '.' in fi['url'] else 'png'
                            await browser_client.upload_file_via_qianwen_page(data, f"image.{ext}")
                            logger.info("[Qwen] waiting after file upload...")
                            await asyncio.sleep(5)
                        elif fi["kind"] == "file":
                            await browser_client.upload_file_via_qianwen_page(fi["data"], fi["name"])
                            logger.info("[Qwen] waiting after file upload...")
                            await asyncio.sleep(5)
                    except Exception as e:
                        logger.error(f"[Qwen] file upload error: {e}")
            last_msg = request.messages[-1] if request.messages else None
            last_text = self._extract_last_text_content(last_msg) if last_msg else ""
            last_text = last_text.replace('\n', ' ').replace('\r', ' ')
            messages = [{
                "mime_type": "text/plain",
                "content": last_text,
                "meta_data": {"ori_query": last_text},
                "status": "complete"
            }]
            logger.info(f"Qianwen stream_chat: model={model}, last_text={len(last_text)} chars")
        else:
            last_msg = request.messages[-1] if request.messages else None
            last_role = getattr(last_msg, 'role', '') if last_msg else ''
            if last_role == 'tool':
                tool_content = getattr(last_msg, 'content', '') or ''
                tool_text = tool_content if isinstance(tool_content, str) else json.dumps(tool_content, ensure_ascii=False, indent=2)
                try:
                    logs_dir = os.path.join(BASE_DIR, "logs")
                    os.makedirs(logs_dir, exist_ok=True)
                    tool_path = os.path.join(logs_dir, "toolreturn.txt")
                    with open(tool_path, 'w', encoding='utf-8') as f:
                        f.write(tool_text)
                    logger.info(f"[Qwen] saved toolreturn.txt to {tool_path}")
                    await browser_client.upload_file_via_qianwen_page(
                        file_data=tool_text.encode('utf-8'),
                        file_name="toolreturn.txt"
                    )
                    logger.info(f"[Qwen] uploaded toolreturn.txt ({len(tool_text)} bytes)")
                except Exception as e:
                    logger.error(f"[Qwen] upload toolreturn.txt failed: {e}")

                prompt_text = "将这次上传的附件中的文件内容美化后返回给我，回复的内容前后不要附加任何其它内容。"
                messages = [{
                    "mime_type": "text/plain",
                    "content": prompt_text,
                    "meta_data": {"ori_query": prompt_text},
                    "status": "complete"
                }]
                logger.info(f"Qianwen tool return: model={model}, uploaded toolreturn.txt")
            else:
                custom_prompt = CONFIG.get('custom_prompt', '')
                custom_prompt = custom_prompt.replace('\n', ' ').replace('\r', ' ')
                messages_text = json.dumps(
                    [m.model_dump() for m in request.messages],
                    ensure_ascii=False, indent=2
                )
                try:
                    logs_dir = os.path.join(BASE_DIR, "logs")
                    os.makedirs(logs_dir, exist_ok=True)
                    saved_path = os.path.join(logs_dir, "messages.txt")
                    with open(saved_path, 'w', encoding='utf-8') as f:
                        f.write(messages_text)
                    logger.info(f"[Qwen] saved messages.txt to {saved_path}")
                    await browser_client.upload_file_via_qianwen_page(
                        file_data=messages_text.encode('utf-8'),
                        file_name="messages.txt"
                    )
                    logger.info(f"[Qwen] uploaded messages.txt ({len(messages_text)} bytes)")
                except Exception as e:
                    logger.error(f"[Qwen] upload messages.txt failed: {e}")

                prompt_text = custom_prompt if custom_prompt else "请查看我上传的消息文件。"
                messages = [{
                    "mime_type": "text/plain",
                    "content": prompt_text,
                    "meta_data": {"ori_query": prompt_text},
                    "status": "complete"
                }]
                logger.info(f"Qianwen agent request: model={model}, sent custom_prompt ({len(prompt_text)} chars), uploaded messages.txt")

        full_text = ""
        suppress_text = False

        async def _process_sse():
            async for kind, value in browser_client.stream_qianwen_chat(
                messages, self._session_id, self._topic_id
            ):
                if kind == "error":
                    yield ("error", str(value))
                    return
                if kind == "done":
                    yield ("done", "")
                    return
                if kind == "chunk":
                    yield ("chunk", value)

        try:
            async for kind, value in _process_sse():
                if kind == "error":
                    yield self._format_error(value, model, chat_id)
                    return
                if kind == "chunk":
                    full_text += value
                    if not suppress_text:
                        ft = full_text.lstrip()
                        if ft[:1] == "{" or ft[:3] == "```":
                            suppress_text = True
                    if not suppress_text:
                        yield self._format_chunk(value, model, chat_id)
                if kind == "done":
                    logger.info(f"[Qwen] done event, full_text length={len(full_text)}, content={full_text[:200]}")
                    is_tool_call = False
                    tool_call_data = None
                    try:
                        text_to_parse = full_text.strip()
                        if text_to_parse.startswith("```"):
                            lines = text_to_parse.split("\n")
                            json_lines = []
                            in_code_block = False
                            for line in lines:
                                if line.startswith("```"):
                                    in_code_block = not in_code_block
                                    continue
                                if in_code_block:
                                    json_lines.append(line)
                            text_to_parse = "\n".join(json_lines).strip()
                        if text_to_parse:
                            tool_call_data = json.loads(text_to_parse)
                            is_tool_call = isinstance(tool_call_data, dict) and "tool_calls" in tool_call_data
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"[Qwen] JSON parse failed: {e}, text_to_parse={text_to_parse[:200] if text_to_parse else 'empty'}")

                    if is_tool_call:
                        tool_calls = tool_call_data.get("tool_calls", [])
                        for i, tc in enumerate(tool_calls):
                            yield format_openai_chunk(
                                "", model, chat_id, "",
                                role="assistant" if i == 0 else None,
                                tool_calls=[{
                                    "index": i,
                                    "id": tc.get("id", ""),
                                    "type": tc.get("type", "function"),
                                    "function": {
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": ""
                                    }
                                }]
                            ).encode()
                            args = tc.get("function", {}).get("arguments", "")
                            if args:
                                yield format_openai_chunk(
                                    "", model, chat_id, "",
                                    tool_calls=[{"index": i, "function": {"arguments": args}}]
                                ).encode()
                        yield format_openai_chunk(None, model, chat_id, "", finish_reason="tool_calls").encode()
                    else:
                        if suppress_text and full_text.strip():
                            yield self._format_chunk(full_text, model, chat_id)
                        yield self._format_chunk("", model, chat_id).replace(b'"finish_reason": null', b'"finish_reason": "stop"')
                    yield format_openai_done().encode()
                    return
        except Exception as e:
            logger.error(f"Qianwen stream error: {e}")
            yield self._format_error(str(e), model, chat_id)

    async def non_stream_chat(self, request: ChatCompletionRequest) -> dict:
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        full_text = ""

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

    def _format_chunk(self, delta: str, model: str, chat_id: str) -> bytes:
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": delta},
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def _format_usage(self, usage: dict, model: str, chat_id: str) -> bytes:
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": None
            }],
            "usage": usage
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def _format_done(self) -> bytes:
        return b"data: [DONE]\n\n"

    def _format_error(self, error: str, model: str, chat_id: str) -> bytes:
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": f"[Error: {error}]"},
                "finish_reason": "stop"
            }]
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
