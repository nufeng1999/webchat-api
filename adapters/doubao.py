import json
import uuid
import os
import logging
import asyncio
import time
from typing import AsyncGenerator, Optional
from adapters.base import BaseAdapter
from models import ChatCompletionRequest, ChatMessage, MODEL_CONFIG
from config import CONFIG, Colors, BASE_DIR, get_webchat_task, get_ret_format_prompt, get_exectask_prompt
from sse import format_openai_chunk, format_openai_done, extract_text_from_content

logger = logging.getLogger("doubao-adapter")

DOUBAO_MODELS = {k: v for k, v in MODEL_CONFIG.items()}


class DoubaoAdapter(BaseAdapter):
    """Doubao (豆包) 适配器，参考 qianwen.py 实现文件上传+提示词关联。"""

    def __init__(self):
        self._doubao_lock = asyncio.Lock()
        self._last_conversation_id = ""
        self._last_chat_id = ""

    def get_adapter_name(self) -> str:
        return "doubao"

    def get_models(self) -> dict[str, dict]:
        return DOUBAO_MODELS

    async def init(self):
        logger.info("Doubao adapter initialized")

    async def close(self):
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # Doubao 专有方法
    # ═══════════════════════════════════════════════════════════════════════

    async def _prepare_inline_file_content(self, request: ChatCompletionRequest, is_tool_return: bool) -> str:
        """Prepare the file content as inline text。"""
        request_dict = request.model_dump()
        if is_tool_return:
            request_dict['task'] = get_ret_format_prompt()
        else:
            request_dict['task'] = get_webchat_task()

        request_dict['sample_response_format'] = CONFIG.get('sample_response_format', '')

        # 只保留最后 5 条消息
        msgs = request_dict.get('messages', [])
        if len(msgs) > 5:
            msgs = msgs[-5:]
        request_dict['messages'] = msgs

        return json.dumps(request_dict, ensure_ascii=False, indent=None, separators=(',', ':'))

    def _save_conversation_id(self, chat_id: str):
        """保存当前 conversation_id 到文件，供 shutdown 清理时删除。"""
        if chat_id and self._last_conversation_id:
            try:
                from config import CONVERSATION_DIR
                os.makedirs(CONVERSATION_DIR, exist_ok=True)
                state_file = os.path.join(CONVERSATION_DIR, f"{chat_id}.json")
                with open(state_file, 'w', encoding='utf-8') as f:
                    json.dump({"doubao_conversation_id": self._last_conversation_id}, f, ensure_ascii=False)
            except Exception as save_e:
                logger.warning(f"[Doubao] failed to save conversation state: {save_e}")

    async def _delete_current_conversation(self):
        """立即删除当前 attempt 的对话。优先使用浏览器方式（stream_chat 已持锁，skip_lock=True）。"""
        conv_id = self._last_conversation_id
        if not conv_id or conv_id == "0":
            logger.debug(f"[Doubao] no conversation to delete (conv_id={conv_id})")
            self._last_conversation_id = ""
            return

        # 优先使用浏览器方式（skip_lock=True 因为 stream_chat 已持锁）
        try:
            from browser_client import browser_client
            ok, err = await browser_client.delete_conversation_via_browser(conv_id, skip_lock=True)
            if ok:
                logger.info(f"[Doubao] deleted conversation {conv_id} via browser")
                self._last_conversation_id = ""
                return
            # cancel 错误时，page.evaluate 可能已执行但结果未返回，删除实际已成功
            if "cancelled" in err.lower() or "cancel scope" in err.lower():
                logger.info(f"[Doubao] browser delete cancelled (likely succeeded), skipping HTTP fallback")
                self._last_conversation_id = ""
                return
            logger.warning(f"[Doubao] browser delete failed: {err}")
        except Exception as e:
            err_str = str(e)
            if "cancelled" in err_str.lower() or "cancel scope" in err_str.lower():
                logger.info(f"[Doubao] browser delete cancelled (likely succeeded), skipping HTTP fallback")
                self._last_conversation_id = ""
                return
            logger.warning(f"[Doubao] browser delete exception: {e}")

        self._last_conversation_id = ""

    def _build_retry_prompt(self, prompt_text: str, is_tool_return: bool, parse_error_history: list) -> str:
        """构建带错误反馈的 prompt。"""
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

    # ═══════════════════════════════════════════════════════════════════════
    # 核心方法：stream_chat
    # ═══════════════════════════════════════════════════════════════════════

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client
        logger.debug(f"{Colors.BOLD_GREEN}[Doubao] -------------------new request---------------------{Colors.RESET}")
        chat_id = self._generate_chat_id()
        model = request.model
        is_agent = self._is_agent_request(request)
        self._last_conversation_id = ""
        self._last_chat_id = chat_id

        await self._doubao_lock.acquire()
        try:
            # 准备请求参数
            is_tool_return = False
            if is_agent:
                last_msg = request.messages[-1] if request.messages else None
                is_tool_return = getattr(last_msg, 'role', None) == 'tool' if last_msg else False
                file_content = await self._prepare_inline_file_content(request, is_tool_return)
                prompt_text = get_exectask_prompt()
                if is_tool_return:
                    logger.debug(f"{Colors.BLUE}[Doubao]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}toolreturn.json{Colors.RESET} {Colors.BLUE} ({len(file_content)} bytes){Colors.RESET}")
                else:
                    logger.debug(f"{Colors.BLUE}[Doubao]===>agent new request prepared {Colors.RESET} {Colors.BOLD_RED}request.json{Colors.RESET} {Colors.BLUE} ({len(file_content)} bytes){Colors.RESET}")
                logger.debug(f"Doubao agent: model={model}, inline_file_len={len(file_content)}{Colors.RESET}")
            else:
                last_msg = request.messages[-1] if request.messages else None
                prompt_text = extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""
                logger.debug(f"{Colors.BLUE}Doubao ===>non agent new request regular: model={model}, text_len={len(prompt_text)}{Colors.RESET}")

            max_retries = 6
            parse_error_history = []

            for attempt in range(max_retries):
                full_text = ""
                suppress_text = False
                buffered_chunks = [] if is_agent else None

                # 构建带错误反馈的 prompt
                current_prompt = self._build_retry_prompt(prompt_text, is_tool_return, parse_error_history)
                if parse_error_history:
                    logger.info(f"Doubao retry with parse error feedback: {parse_error_history[-1][0][:200]}")

                kwargs = {"text": current_prompt}
                if is_agent and file_content:
                    kwargs["inline_file_content"] = f"[文件 request.json 内容]\n{file_content}\n[/文件内容]"

                logger.info(f"[Doubao Adapter] {Colors.RED}Attempt {attempt+1}/{max_retries}{Colors.RESET}: calling stream_doubao_chat_via_type, inline_file_content={'yes' if is_agent and file_content else 'no'}")
                got_rate_limit = False
                parse_success = False

                async for kind, value in browser_client.stream_doubao_chat_via_type(**kwargs):
                    if kind == "error":
                        err_str = str(value).lower()
                        if "rate" in err_str and "limit" in err_str:
                            got_rate_limit = True
                            logger.warning(f"Doubao rate limited (attempt {attempt+1}/{max_retries})")
                            await self._delete_current_conversation()
                            break
                        yield self._format_error(str(value), model, chat_id)
                        await self._delete_current_conversation()
                        return
                    if kind == "conversation_id":
                        self._last_conversation_id = value
                        logger.info(f"[Doubao] conversation_id: {value}")
                        continue
                    if kind == "chunk":
                        full_text += value
                        if not suppress_text:
                            ft = full_text.lstrip()
                            if ft[:1] == "{" or ft[:3] == "```":
                                suppress_text = True
                                logger.info(f"Doubao suppress_text triggered: ft_start={ft[:100]!r}")
                        if not suppress_text:
                            if is_agent and buffered_chunks is not None:
                                buffered_chunks.append(self._format_chunk(value, model, chat_id))
                            else:
                                yield self._format_chunk(value, model, chat_id)
                    if kind == "done":
                        logger.debug(f"Doubao done: suppress_text={suppress_text}, full_text_len={len(full_text)}, full_text_preview=\n{full_text[:2000]!r}")
                        try:
                            content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls = self._parse_response(full_text)
                            logger.info(f"Doubao done: content={content!r}, tool_calls=\n{tool_calls!r}, finish_reason={finish_reason!r}, is_openai_chunk={is_openai_chunk}, is_tool_calls={is_tool_calls}")

                            # 验证响应是否合法
                            should_retry, err_msg, tool_return_content, full_text = self._validate_done_response(
                                content, tool_calls, finish_reason,
                                is_openai_chunk, is_tool_calls,
                                suppress_text, is_agent, is_tool_return, full_text
                            )

                            if tool_return_content is not None:
                                content = tool_return_content
                                finish_reason = "stop"
                                logger.info(f"Doubao tool_return response: {tool_return_content[:2000]}")
                                parse_success = True
                            elif should_retry:
                                parse_error_history.append((err_msg, full_text))
                                if attempt < max_retries - 1:
                                    logger.info(f"Doubao retrying (attempt {attempt+1}/{max_retries}): {err_msg[:200]}")
                                    await asyncio.sleep(5)
                                    await self._delete_current_conversation()
                                    break
                                else:
                                    logger.error(f"Doubao parse failed after {max_retries} attempts")
                                    yield self._format_error("服务器内部错误！", model, chat_id)
                                    self._save_conversation_id(chat_id)
                                    await self._delete_current_conversation()
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
                                self._save_conversation_id(chat_id)
                                await self._delete_current_conversation()
                                return
                        except Exception as e:
                            logger.error(f"Doubao done handler error: {e}")
                            err_msg = str(e)[:200]
                            parse_error_history.append((err_msg, full_text))
                            if attempt < max_retries - 1:
                                logger.info(f"Doubao retrying after done handler error: {err_msg}")
                                await self._delete_current_conversation()
                                break
                            else:
                                yield self._format_error("服务器内部错误！", model, chat_id)
                                self._save_conversation_id(chat_id)
                                await self._delete_current_conversation()
                                return

                # After async-for loop ends (without a return from done handler)
                if got_rate_limit:
                    if attempt < max_retries - 1:
                        # 1. 显示 visible 浏览器，让用户处理限流/验证码
                        logger.warning("[Doubao] rate limited! showing visible browser for user to handle...")
                        try:
                            await browser_client.show_doubao_for_rate_limit()
                        except Exception as e:
                            logger.warning(f"[Doubao] failed to show visible browser: {e}")
                        # 2. 分段等待 3 分钟（每 10 秒检查一次，用户关浏览器则提前退出）
                        logger.info("[Doubao] waiting up to 180 seconds for user to handle rate limit...")
                        for sec in range(0, 180, 10):
                            await asyncio.sleep(10)
                            try:
                                if browser_client._doubao_page and not browser_client._doubao_page.is_closed():
                                    break_if_disconnected = False
                                else:
                                    logger.info(f"[Doubao] browser closed by user after {sec+10}s, stopping wait")
                                    break
                            except Exception:
                                logger.info(f"[Doubao] browser check failed after {sec+10}s, assuming closed")
                                break
                        # 3. 关闭 visible 浏览器（可能已被用户关了，容错）
                        try:
                            await browser_client.hide_doubao_browser()
                        except Exception as e:
                            logger.warning(f"[Doubao] failed to hide visible browser: {e}")
                        # 4. 继续重试
                        logger.info(f"[Doubao] resuming after rate limit handling, retry {attempt+2}/{max_retries}")
                        continue
                    else:
                        logger.error("Doubao rate limited after max retries")
                        yield self._format_error("服务器内部错误！", model, chat_id)
                        self._save_conversation_id(chat_id)
                        await self._delete_current_conversation()
                        return
                else:
                    # Normal empty/early exit without done (should not happen often)
                    if attempt < max_retries - 1:
                        continue
                    break

            self._save_conversation_id(chat_id)
            await self._delete_current_conversation()
            yield self._format_error("已进行多次重试！", model, chat_id)
        except Exception as e:
            logger.error(f"[Doubao] stream_chat error: {e}")
            self._save_conversation_id(chat_id)
            await self._delete_current_conversation()
            yield self._format_error(str(e), model, chat_id)
        finally:
            self._doubao_lock.release()

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
