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
from config import CONFIG, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import format_openai_chunk, format_openai_done

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
            logger.info(f"[Qianwen] Models refreshed: {list(QIANWEN_MODELS.keys())}")
    except Exception as e:
        logger.warning(f"[Qianwen] Failed to refresh models: {e}")


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

    async def _prepare_messages(self, request, browser_client, is_agent):
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
            last_text = self._extract_last_text_content(last_msg) if last_msg else ""
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
        file_name="request.json"
        if last_role == 'tool':
            file_name="toolreturn.json"
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
                logger.info(f"[Qwen] saved toolreturn.json to {tool_path}")
                await browser_client.upload_file_via_qianwen_page(
                    file_data=request_json.encode('utf-8'), file_name=tool_path
                )
                # await asyncio.sleep(8)
                time.sleep(8)
                logger.info(f"[Qwen] uploaded toolreturn.json ({len(request_json)} bytes)")
            except Exception as e:
                logger.error(f"[Qwen] upload toolreturn.json failed: {e}")
            prompt_text = get_exectask_prompt()
            logger.info(f"Qianwen tool return: model={request.model}, uploaded {tool_path}")
        else:
            file_name="request.json"
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
                logger.info(f"[Qwen] saved request.json to {saved_path}")
                await browser_client.upload_file_via_qianwen_page(
                    file_data=request_json.encode('utf-8'), file_name=saved_path
                )
                # await asyncio.sleep(8)
                time.sleep(8)
                logger.info(f"[Qwen] uploaded request.json ({len(request_json)} bytes)")
            except Exception as e:
                logger.error(f"[Qwen] upload request.json failed: {e}")
            prompt_text = get_exectask_prompt() if CONFIG.get('exectask_prompt', '') else "请查看我上传的请求文件。"
            logger.info(f"Qianwen agent request: model={request.model}, sent exectask_prompt ({len(prompt_text)} chars), uploaded {saved_path}")

        return [{
            "mime_type": "text/plain",
            "content": prompt_text,
            "meta_data": {"ori_query": prompt_text},
            "status": "complete"
        }]

    def _validate_json_nested(self, obj, path=""):
        """验证 JSON 对象中 "arguments" 字段的值是否为合法 JSON 字符串。"""
        if isinstance(obj, dict):
            for key in ("arguments", "args", "arguments_str"):
                if key in obj:
                    val = obj[key]
                    if isinstance(val, str) and val.strip().startswith("{"):
                        try:
                            json.loads(val, strict=False)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Qianwen nested JSON validation failed: {path}[{key}]: {e}")
                            return False, ""
            for v in obj.values():
                ok, _ = self._validate_json_nested(v, path + "." + str(list(obj.keys())))
                if not ok:
                    return False, ""
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                ok, _ = self._validate_json_nested(item, path + f"[{i}]")
                if not ok:
                    return False, ""
        return True, ""

    def _parse_response(self, full_text):
        is_openai_chunk=False
        is_tool_calls=False
        text_to_parse = full_text.strip()
        if text_to_parse.startswith("```"):
            lines = text_to_parse.split("\n")
            json_lines = []
            in_code_block = False
            for line in lines:
                if line.startswith("```"):
                    if in_code_block:
                        break
                    in_code_block = True
                    continue
                if in_code_block:
                    json_lines.append(line)
            text_to_parse = "\n".join(json_lines).strip()

        if not text_to_parse:
            return None, None, None,is_openai_chunk,is_tool_calls

        try:
            parsed = json.loads(text_to_parse, strict=False)
        except json.JSONDecodeError as e:
            pos = e.pos
            snippet = text_to_parse[max(0,pos-40):pos+40]
            logger.warning(f"Qianwen JSON decode error at char {pos}: ...{snippet!r}...")
            brace_diff = text_to_parse.count("}") - text_to_parse.count("{")
            if brace_diff > 0 and text_to_parse.rstrip().endswith("}"):
                stripped = text_to_parse.rstrip()
                while stripped.endswith("}") and stripped.count("}") > stripped.count("{"):
                    stripped = stripped[:-1].rstrip()
                    try:
                        parsed = json.loads(stripped, strict=False)
                        break
                    except json.JSONDecodeError:
                        continue
                else:
                    logger.error(f"Qianwen JSON repair failed after brace strip, text={text_to_parse[:200]!r}")
                    return None, None, None,is_openai_chunk,is_tool_calls
            else:
                logger.error(f"Qianwen JSON decode internal error at char {pos}, text={text_to_parse[:200]!r}")
                return None, None, None,is_openai_chunk,is_tool_calls
        except TypeError as e:
            logger.error(f"Qianwen JSON decode TypeError: {e}")
            return None, None, None,is_openai_chunk,is_tool_calls

        if not isinstance(parsed, dict):
            return None, None, None,is_openai_chunk,is_tool_calls

        if parsed.get("choices") and isinstance(parsed["choices"], list) and parsed["choices"]:
            choice = parsed["choices"][0]
            if isinstance(choice, dict):
                is_openai_chunk=True
                delta_or_message = choice.get("delta") or choice.get("message", {})
                if isinstance(delta_or_message, dict):
                    content = delta_or_message.get("content") or None
                    if content == "":
                        content = None
                    tool_calls = delta_or_message.get("tool_calls") or choice.get("tool_calls")
                    finish_reason = choice.get("finish_reason") or parsed.get("finish_reason")
                    return content, tool_calls, finish_reason,is_openai_chunk,is_tool_calls

        if "tool_calls" in parsed and isinstance(parsed.get("tool_calls"), list):
            is_tool_calls=True
            return None, parsed["tool_calls"], "tool_calls",is_openai_chunk,is_tool_calls

        if parsed.get("id") and parsed.get("type") == "function" and parsed.get("function"):
            is_tool_calls=True
            return None, [parsed], "tool_calls",is_openai_chunk,is_tool_calls

        return None, None, None,is_openai_chunk,is_tool_calls

    def _yield_tool_calls(self, tool_calls, model, chat_id, content=None):
        for i, tc in enumerate(tool_calls):
            yield format_openai_chunk(
                content if i == 0 else None,
                model, chat_id, "",
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
                    None, model, chat_id, "",
                    tool_calls=[{"index": i, "function": {"arguments": args}}]
                ).encode()

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model = request.model
        is_agent = self._is_agent_request(request)
        self._last_session_id = ""

        await self._qianwen_lock.acquire()
        try:
            # 如果模型发生变化，在页面上切换模型
            if model != self._last_model:
                model_id = self._get_model_name(model)
                await browser_client.select_qianwen_model(model_id)
                self._last_model = model

            messages = await self._prepare_messages(request, browser_client, is_agent)
            logger.info(f"Qianwen stream_chat: model={model}, messages={len(messages)}, is_agent={is_agent}")

            max_retries = 3
            for attempt in range(max_retries):
                full_text = ""
                suppress_text = False
                buffered_chunks = [] if is_agent else None

                async for kind, value in browser_client.stream_qianwen_chat(
                    messages, self._session_id, self._topic_id
                ):
                    if kind == "error":
                        yield self._format_error(str(value), model, chat_id)
                        return
                    if kind == "chunk":
                        full_text += value
                        if not suppress_text:
                            ft = full_text.lstrip()
                            if ft[:1] == "{" or ft[:3] == "```":
                                suppress_text = True
                                logger.info(f"Qianwen suppress_text triggered: ft_start={ft[:100]!r}")
                        if not suppress_text:
                            if is_agent and buffered_chunks is not None:
                                buffered_chunks.append(self._format_chunk(value, model, chat_id))
                            else:
                                yield self._format_chunk(value, model, chat_id)
                    if kind == "done":
                        logger.info(f"Qianwen done: suppress_text={suppress_text}, full_text_len={len(full_text)}, full_text_preview={full_text[:1000]!r}")
                        try:
                            content, tool_calls, finish_reason,is_openai_chunk,is_tool_calls = self._parse_response(full_text)
                            logger.info(f"Qianwen done: content={content!r}, tool_calls={tool_calls!r}, finish_reason={finish_reason!r}, is_openai_chunk={is_openai_chunk}, is_tool_calls={is_tool_calls}")

                            # 验证嵌套的 arguments JSON
                            nested_validation_ok = True
                            nested_err_msg = ""
                            if content is None and tool_calls is None and suppress_text:
                                try:
                                    import json as _json2
                                    validated = _json2.loads(full_text)
                                    nested_ok, nested_err_msg = self._validate_json_nested(validated)
                                    if not nested_ok:
                                        nested_validation_ok = False
                                except Exception as ve:
                                    nested_validation_ok = False
                                    nested_err_msg = str(ve)

                                if attempt < max_retries - 1:
                                    if not nested_validation_ok:
                                        logger.warning(f"Qianwen nested JSON validation failed: {nested_err_msg}, retrying in 5s...")
                                    else:
                                        logger.warning(f"Qianwen parse failed (attempt {attempt+1}/{max_retries}), retrying in 5s...")
                                    await asyncio.sleep(5)
                                    break
                                else:
                                    logger.error(f"Qianwen parse failed after {max_retries} attempts")
                                    if not nested_validation_ok:
                                        yield self._format_error(f"服务器内部错误！嵌套JSON验证失败: {nested_err_msg}", model, chat_id)
                                    else:
                                        yield self._format_error("服务器内部错误！", model, chat_id)
                                    return

                            if content is None and tool_calls is None and not suppress_text:
                                if is_agent:
                                    logger.warning(f"Qianwen agent request got non-JSON response (suppress_text=False)")
                                    if attempt < max_retries - 1:
                                        await asyncio.sleep(5)
                                        break
                                    else:
                                        yield self._format_error("服务器内部错误！", model, chat_id)
                                        return

                            if is_agent and buffered_chunks is not None and not suppress_text:
                                for chunk in buffered_chunks:
                                    yield chunk
                                buffered_chunks.clear()

                            if content and not tool_calls:
                                yield self._format_chunk(content, model, chat_id)
                            elif not tool_calls and suppress_text and full_text.strip():
                                yield self._format_chunk(full_text, model, chat_id)

                            if tool_calls:
                                logger.info(f"Qianwen yielding {len(tool_calls)} tool_calls")
                                for chunk in self._yield_tool_calls(tool_calls, model, chat_id, content=content):
                                    logger.debug(f"Qianwen tool_call chunk: {chunk.decode(errors='replace')[:200]}")
                                    yield chunk
                                fr = finish_reason or "tool_calls"
                                yield format_openai_chunk(None, model, chat_id, "", finish_reason=fr).encode()
                            else:
                                yield format_openai_chunk(None, model, chat_id, "", finish_reason="stop").encode()

                            yield format_openai_done().encode()
                            return
                        except Exception as e:
                            logger.error(f"Qianwen done handler error: {e}")
                            yield self._format_error("服务器内部错误！", model, chat_id)
                            return
            yield self._format_error("服务器内部错误！", model, chat_id)
        finally:
            try:
                self._last_session_id = await browser_client.get_qianwen_session_id()
                if self._last_session_id:
                    logger.info(f"[Qwen] captured session_id: {self._last_session_id}")
            except Exception:
                pass
            self._qianwen_lock.release()

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
