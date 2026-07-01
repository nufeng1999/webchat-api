import json
import uuid
import time
import asyncio
import logging
import os
from datetime import datetime
from typing import AsyncGenerator, Optional
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, DEEPSEEK_MODEL_CONFIG
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import extract_text_from_content

logger = logging.getLogger("deepseek-adapter")


class DeepseekAdapter(BaseAdapter):
    """DeepSeek (chat.deepseek.com) 适配器。"""

    def __init__(self):
        self._deepseek_lock = asyncio.Lock()
        self._last_session_id = ""

    def get_adapter_name(self) -> str:
        return "deepseek"

    def get_models(self) -> dict[str, dict]:
        return DEEPSEEK_MODEL_CONFIG

    async def init(self):
        logger.info("DeepSeek adapter initialized")

    async def close(self):
        pass

    def _get_model_type(self, model: str) -> str:
        cfg = DEEPSEEK_MODEL_CONFIG.get(model, {})
        return cfg.get("model_type", "default")

    def _get_thinking_enabled(self, model: str) -> bool:
        cfg = DEEPSEEK_MODEL_CONFIG.get(model, {})
        return cfg.get("use_deep_think", False)

    def _get_search_enabled(self, model: str) -> bool:
        cfg = DEEPSEEK_MODEL_CONFIG.get(model, {})
        return cfg.get("use_search", True)

    def _supports_file(self, model: str) -> bool:
        cfg = DEEPSEEK_MODEL_CONFIG.get(model, {})
        return cfg.get("supports_file", False)

    async def _delete_conversation(self):
        """仅清除 adapter 本地状态，不删除 web 对话实例。"""
        self._last_session_id = ""

    async def _call_stream(self, **kwargs):
        """抽象方法实现：DeepSeek 使用自定义 stream_chat，不走模板。"""
        raise NotImplementedError("DeepSeek uses custom stream_chat, not template")

    async def _delete_deepseek_conversation(self):
        """仅清除 adapter 本地状态，不删除 web 对话实例。"""
        self._last_session_id = ""

    async def _prepare_messages(self, request: ChatCompletionRequest, browser_client, is_agent: bool, reuse_conversation: bool = False):
        """准备 DeepSeek 的请求数据。对于非 agent 请求，提取最后一条消息文本；对于 agent 请求，序列化 request 为 JSON 并保存日志。"""
        if not is_agent:
            last_msg = request.messages[-1] if request.messages else None
            prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
            logger.debug(f"{Colors.BLUE}DeepSeek prepare_messages: non-agent, text_len={len(prompt_text)}{Colors.RESET}")
            return prompt_text, None

        if reuse_conversation:
            logger.info(f"[DeepSeek] skipping file upload for reused conversation")
            request_dict = request.model_dump()
            last_msg = request.messages[-1] if request.messages else None
            is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
            if is_tool_return:
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
            logger.info(f"[DeepSeek] saved {Colors.BOLD_RED}{fname}{Colors.RESET} to {saved_path}")
        except Exception as e:
            logger.warning(f"[DeepSeek] save {fname} failed: {e}")

        if is_tool_return:
            logger.debug(f"{Colors.BLUE}[DeepSeek]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}toolreturn.json{Colors.RESET} ({len(file_content)} bytes)")
        else:
            logger.debug(f"{Colors.BLUE}[DeepSeek]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}request.json{Colors.RESET} ({len(file_content)} bytes)")

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
            f"[系统提示：你上一次回复的JSON格式解析失败，请修正后重新输出完整且合法的JSON{extra_hint}。]\n"
            f"[错误信息：{err_msg}]\n"
            f"[你的原始回复（前500字符）：{raw_text[:2000]}]"
        )

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client
        logger.debug(f"{Colors.BOLD_GREEN}[DeepSeek] -------------------new request---------------------{Colors.RESET}")
        chat_id = self._generate_chat_id()
        model = request.model
        is_agent = self._is_agent_request(request)
        self._last_session_id = ""

        model_type = self._get_model_type(model)
        thinking_enabled = self._get_thinking_enabled(model)
        search_enabled = self._get_search_enabled(model) if model_type != "expert" else False

        # Determine conversation reuse BEFORE preparing messages
        reuse_conversation = False
        conv_id = getattr(request, 'conversation_id', None)
        if conv_id and conv_id != "0":
            try:
                activated = await browser_client.activate_deepseek_conversation(conv_id)
                if activated:
                    reuse_conversation = True
                    logger.info(f"[DeepSeek] activated conversation {conv_id}, skipping file upload")
                else:
                    logger.warning(f"[DeepSeek] conversation activation failed: conv_id={conv_id}, will create new")
                    request.conversation_id = "0"
            except Exception as e:
                logger.warning(f"[DeepSeek] conversation activation error: {e}")
                request.conversation_id = "0"

        await self._deepseek_lock.acquire()
        try:
            prompt_text, file_content = await self._prepare_messages(request, browser_client, is_agent, reuse_conversation=reuse_conversation)
            is_tool_return = getattr(request.messages[-1], 'role', None) == 'tool' if (is_agent and request.messages) else False

            max_retries = 6
            parse_error_history = []

            for attempt in range(max_retries):
                full_text = ""
                suppress_text = False
                buffered_chunks = [] if is_agent else None

                current_prompt = self._build_retry_prompt(prompt_text, is_tool_return, parse_error_history)
                if parse_error_history:
                    logger.info(f"DeepSeek retry with parse error feedback: {parse_error_history[-1][0][:200]}")

                logger.info(f"[DeepSeek Adapter] {Colors.RED}Attempt {attempt+1}/{max_retries}{Colors.RESET}: model_type={model_type}, thinking={thinking_enabled}, search={search_enabled}")

                parse_success = False
                think_buf = ""
                async for kind, value in browser_client.stream_deepseek_chat(
                    prompt=current_prompt,
                    model_type=model_type,
                    thinking_enabled=thinking_enabled,
                    search_enabled=search_enabled,
                    inline_file_content=file_content if is_agent else None,
                ):
                    if kind == "session_id":
                        self._last_session_id = value
                        logger.info(f"[DeepSeek] session_id: {value}")
                        continue
                    processed = await self._handle_chunk_streaming(
                        kind, value,
                        model=model, chat_id=chat_id, is_agent=is_agent,
                        full_text=full_text, suppress_text=suppress_text,
                        buffered_chunks=buffered_chunks,
                        _think_buf=think_buf,
                    )
                    full_text, suppress_text, buffered_chunks, should_return, return_value, think_buf = processed
                    if should_return and return_value is not None:
                        yield return_value
                        if kind == "error":
                            await self._delete_deepseek_conversation()
                            return
                    if kind == "done":
                        logger.debug(f"DeepSeek done: suppress_text={suppress_text}, full_text_len={len(full_text)}, full_text_preview=\n{full_text[:2000]!r}")
                        try:
                            content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls = self._parse_response(full_text)
                            logger.info(f"DeepSeek done: content={content!r}, tool_calls={tool_calls!r}, finish_reason={finish_reason!r}")

                            should_retry, err_msg, tool_return_content, full_text = self._validate_done_response(
                                content, tool_calls, finish_reason,
                                is_openai_chunk, is_tool_calls,
                                suppress_text, is_agent, is_tool_return, full_text
                            )

                            if tool_return_content is not None:
                                content = tool_return_content
                                finish_reason = "stop"
                                logger.info(f"DeepSeek tool_return response: {tool_return_content[:2000]}")
                                parse_success = True
                            elif should_retry:
                                parse_error_history.append((err_msg, full_text))
                                if attempt < max_retries - 1:
                                    logger.info(f"DeepSeek retrying (attempt {attempt+1}/{max_retries}): {err_msg[:200]}")
                                    await asyncio.sleep(5)
                                    await self._delete_deepseek_conversation()
                                    break
                                else:
                                    logger.error(f"DeepSeek parse failed after {max_retries} attempts")
                                    yield self._format_error("服务器内部错误！", model, chat_id)
                                    await self._delete_deepseek_conversation()
                                    return
                            else:
                                parse_success = True

                            if parse_success:
                                async for chunk in self._yield_final_response(
                                    content, tool_calls, finish_reason,
                                    suppress_text, is_agent, buffered_chunks, full_text,
                                    model, chat_id, is_openai_chunk, is_tool_calls
                                ):
                                    yield chunk
                                await self._delete_deepseek_conversation()
                                return
                        except Exception as e:
                            logger.error(f"DeepSeek done handler error: {e}")
                            err_msg = str(e)[:200]
                            parse_error_history.append((err_msg, full_text))
                            if attempt < max_retries - 1:
                                logger.info(f"DeepSeek retrying after done handler error: {err_msg}")
                                await self._delete_deepseek_conversation()
                                break
                            else:
                                yield self._format_error("服务器内部错误！", model, chat_id)
                                await self._delete_deepseek_conversation()
                                return

                if attempt < max_retries - 1 and not parse_success:
                    continue
                break

            yield self._format_error("老骥伏枥，志在千里；烈士暮年，壮心不已。", model, chat_id)
        except Exception as e:
            logger.error(f"[DeepSeek] stream_chat error: {e}")
            yield self._format_error(str(e), model, chat_id)
        finally:
            self._deepseek_lock.release()

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
            logger.error(f"[DeepSeek] non_stream_chat error: {e}")
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
