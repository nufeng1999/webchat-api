import json
import uuid
import os
import logging
import asyncio
import time
from typing import AsyncGenerator, Optional
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, ChatMessage, MODEL_CONFIG
from config import CONFIG, BASE_DIR
from sse import format_openai_chunk, format_openai_done, extract_text_from_content

logger = logging.getLogger("doubao-adapter")

DOUBAO_MODELS = {k: v for k, v in MODEL_CONFIG.items()}


class DoubaoAdapter(BaseAdapter):
    """Doubao (豆包) 适配器，参考 qianwen.py 实现文件上传+提示词关联。"""

    def __init__(self):
        self._doubao_lock = asyncio.Lock()
        self._last_conversation_id = ""

    def get_adapter_name(self) -> str:
        return "doubao"

    def get_models(self) -> dict[str, dict]:
        return DOUBAO_MODELS

    async def init(self):
        logger.info("Doubao adapter initialized")

    async def close(self):
        pass

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

    async def _prepare_agent_upload(self, request: ChatCompletionRequest, is_tool_return: bool) -> Optional[str]:
        """将请求内容添加task和sample_response_format后保存为JSON文件，上传到豆包，返回exectask_prompt。"""
        from browser_client import browser_client

        request_dict = request.model_dump()
        if is_tool_return:
            request_dict['task'] = CONFIG.get('ret_format_prompt', '')
            file_name = "toolreturn.json"
        else:
            request_dict['task'] = CONFIG.get('webchat_task', '')
            file_name = "request.json"

        request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')
        request_json = json.dumps(request_dict, ensure_ascii=False, indent=2)

        try:
            logs_dir = os.path.join(BASE_DIR, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            saved_path = os.path.join(logs_dir, file_name)
            with open(saved_path, 'w', encoding='utf-8') as f:
                f.write(request_json)
            logger.info(f"[Doubao] saved {file_name} to {saved_path}")

            await browser_client.upload_document_via_page(
                file_data=request_json.encode('utf-8'),
                file_name=file_name
            )
            await asyncio.sleep(5)
            logger.info(f"[Doubao] uploaded {file_name} ({len(request_json)} bytes)")
        except Exception as e:
            logger.error(f"[Doubao] upload {file_name} failed: {e}")

        return CONFIG.get('exectask_prompt', '')

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client
        from openai_api import call_doubao_api, upload_images_for_message, cookie_pool
        from sse import extract_image_urls_from_content, parse_sse_line, extract_text_from_event, extract_image_urls_from_event, extract_conversation_id
        import json as _json

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model = request.model
        is_agent = self._is_agent_request(request)
        conversation_id = request.conversation_id or "0"
        last_msg = request.messages[-1] if request.messages else None
        last_is_tool = getattr(last_msg, 'role', None) == 'tool' if last_msg else False

        await self._doubao_lock.acquire()
        try:
            attachments = None
            if is_agent:
                conversation_id = "0"
                prompt_text = await self._prepare_agent_upload(request, last_is_tool)
                if prompt_text:
                    messages_to_send = [ChatMessage(role="user", content=prompt_text)]
                else:
                    messages_to_send = request.messages
            else:
                messages_to_send = request.messages
                # 非agent请求：原有逻辑，处理图片
                image_urls = extract_image_urls_from_content(
                    getattr(last_msg, 'content', '') if last_msg and isinstance(getattr(last_msg, 'content', None), list) else []
                ) if last_msg else []
                if image_urls:
                    try:
                        account = cookie_pool.get_next()
                        attachments = await upload_images_for_message(image_urls, account)
                    except Exception as e:
                        logger.error(f"[Doubao] image upload failed: {e}")

            buffer = ""
            full_text = ""
            suppress_text = False

            async for raw_chunk in call_doubao_api(
                messages_to_send, conversation_id, model,
                attachments=attachments
            ):
                try:
                    buffer += raw_chunk.decode('utf-8', errors='replace')
                except:
                    continue

                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue

                    event_data = parse_sse_line(line)
                    if event_data is None:
                        yield format_openai_done().encode()
                        return

                    extracted, thinking = extract_text_from_event(event_data)
                    if thinking:
                        yield format_openai_chunk("", model, chat_id, conversation_id, reasoning_content=thinking)
                    if extracted:
                        full_text += extracted
                        if not suppress_text and full_text.lstrip()[:1] == "{":
                            suppress_text = True
                        if not suppress_text:
                            yield format_openai_chunk(extracted, model, chat_id, conversation_id)

                    img_urls = extract_image_urls_from_event(event_data)
                    if img_urls:
                        for img_url in img_urls:
                            img_md = f"\n![image]({img_url})\n"
                            full_text += img_md
                            yield format_openai_chunk(img_md, model, chat_id, conversation_id)

                    conv_id = extract_conversation_id(event_data)
                    if conv_id:
                        conversation_id = conv_id
                        self._last_conversation_id = conv_id

                    if event_data.get("event_type") == 2003:
                        try:
                            tcd = _json.loads(full_text) if isinstance(full_text, str) else full_text
                            is_tool_call = isinstance(tcd, dict) and "tool_calls" in tcd
                        except (_json.JSONDecodeError, TypeError):
                            is_tool_call = False

                        if is_tool_call:
                            tool_calls = tcd.get("tool_calls", [])
                            for i, tc in enumerate(tool_calls):
                                yield format_openai_chunk(
                                    "", model, chat_id, conversation_id,
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
                                )
                                args = tc.get("function", {}).get("arguments", "")
                                if args:
                                    yield format_openai_chunk(
                                        None, model, chat_id, conversation_id,
                                        tool_calls=[{"index": i, "function": {"arguments": args}}]
                                    )
                            yield format_openai_chunk(None, model, chat_id, conversation_id, finish_reason="tool_calls")
                        else:
                            if suppress_text and full_text.strip():
                                yield format_openai_chunk(full_text, model, chat_id, conversation_id)
                            yield format_openai_chunk("", model, chat_id, conversation_id).replace(
                                '"finish_reason": null', '"finish_reason": "stop"')

                        yield format_openai_done().encode()
                        return
        except Exception as e:
            logger.error(f"[Doubao] stream_chat error: {e}")
            yield _format_doubao_error(str(e), model, chat_id)
        finally:
            self._doubao_lock.release()

    async def non_stream_chat(self, request: ChatCompletionRequest) -> dict:
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        full_text = ""
        try:
            async for chunk in self.stream_chat(request):
                try:
                    text = chunk.decode('utf-8', errors='replace')
                    if text.startswith("data: ") and not text.startswith("data: [DONE]"):
                        data_str = text[6:].strip()
                        if data_str:
                            import json as _json
                            data = _json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_text += content
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Doubao] non_stream_chat error: {e}")
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


def _format_doubao_error(error: str, model: str, chat_id: str) -> bytes:
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