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

    async def _prepare_inline_file_content(self, request: ChatCompletionRequest, is_tool_return: bool) -> str:
        """Prepare the file content as inline text."""
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

    def _validate_json_nested(self, obj, path=""):
        """验证 JSON 对象中 "arguments" 字段的值是否为合法 JSON 字符串。
        
        递归检查所有键为 "arguments" 或 "args" 的字符串值，
        如果疑似 JSON 则验证，否则返回 (False, error_msg)。
        """
        if isinstance(obj, dict):
            for key in ("arguments", "args", "arguments_str"):
                if key in obj:
                    val = obj[key]
                    if isinstance(val, str) and val.strip().startswith("{"):
                        try:
                            logger.debug(f'验证 JSON 对象中 "arguments" 字段的值是否为合法 JSON 字符串:\n{val}')
                            json.loads(val, strict=False)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Doubao nested JSON validation failed: {e}")
                            err_msg = f"{key} 字段的值 JSON 解析失败，请重新生成 {key} 字段的json内容，并确保其格式正确，结构不会错乱。"
                            return False, err_msg
            for v in obj.values():
                ok, msg = self._validate_json_nested(v, path + "." + str(list(obj.keys())))
                if not ok:
                    return False, msg
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                ok, msg = self._validate_json_nested(item, path + f"[{i}]")
                if not ok:
                    return False, msg
        return True, ""

    def _parse_response(self, full_text):
        is_openai_chunk = False
        is_tool_calls = False
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
            return None, None, None, is_openai_chunk, is_tool_calls

        try:
            parsed = json.loads(text_to_parse, strict=False)
        except json.JSONDecodeError as e:
            pos = e.pos
            snippet = text_to_parse[max(0, pos - 40):pos + 40]
            logger.warning(f"Doubao JSON decode error at char {pos}: ...{snippet!r}...")
            try:
                debug_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(debug_dir, exist_ok=True)
                with open(os.path.join(debug_dir, "doubao_full_text_debug.txt"), 'w', encoding='utf-8') as f:
                    f.write(full_text)
            except Exception:
                pass
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
                    logger.error(f"Doubao JSON repair failed after brace strip, text={text_to_parse[:200]!r}")
                    return None, None, None, is_openai_chunk, is_tool_calls
            else:
                logger.error(f"Doubao JSON decode internal error at char {pos}, text={text_to_parse[:200]!r}")
                return None, None, None, is_openai_chunk, is_tool_calls
        except TypeError as e:
            logger.error(f"Doubao JSON decode TypeError: {e}")
            return None, None, None, is_openai_chunk, is_tool_calls

        if not isinstance(parsed, dict):
            return None, None, None, is_openai_chunk, is_tool_calls

        if parsed.get("choices") and isinstance(parsed["choices"], list) and parsed["choices"]:
            choice = parsed["choices"][0]
            if isinstance(choice, dict):
                is_openai_chunk = True
                delta_or_message = choice.get("delta") or choice.get("message", {})
                if isinstance(delta_or_message, dict):
                    content = delta_or_message.get("content") or None
                    if content == "":
                        content = None
                    tool_calls = delta_or_message.get("tool_calls") or choice.get("tool_calls")
                    if not tool_calls:
                        tool_calls = None
                    finish_reason = choice.get("finish_reason") or parsed.get("finish_reason")
                    logger.info(f"========1=====>finish_reason={finish_reason}")
                    return content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls

        if "tool_calls" in parsed and isinstance(parsed.get("tool_calls"), list):
            is_tool_calls = True
            tool_calls = parsed["tool_calls"]
            if not tool_calls:
                tool_calls = None
            return None, tool_calls, "tool_calls", is_openai_chunk, is_tool_calls

        if parsed.get("id") and parsed.get("type") == "function" and parsed.get("function"):
            is_tool_calls = True
            return None, [parsed], "tool_calls", is_openai_chunk, is_tool_calls

        return None, None, None, is_openai_chunk, is_tool_calls

    def _yield_tool_calls(self, tool_calls, model, chat_id, content=None):
        """逐条 yield tool_calls 块。"""
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
        """立即删除当前 attempt 的对话（HTTP-only，不依赖浏览器）。"""
        conv_id = self._last_conversation_id
        if conv_id and conv_id != "0":
            try:
                from openai_api import delete_conversation
                await delete_conversation(conv_id, skip_browser=True)
            except Exception:
                pass
        self._last_conversation_id = ""

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client
        logger.debug(f"{Colors.BOLD_GREEN}[Doubao] -------------------new request---------------------{Colors.RESET}")
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model = request.model
        is_agent = self._is_agent_request(request)
        self._last_conversation_id = ""
        self._last_chat_id = chat_id

        await self._doubao_lock.acquire()
        try:
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
            parse_error_history = []  # (error_msg, raw_text) for retry feedback
            for attempt in range(max_retries):
                full_text = ""
                suppress_text = False
                buffered_chunks = [] if is_agent else None

                # 构建带错误反馈的 prompt
                current_prompt = prompt_text
                if parse_error_history:
                    err_msg, raw_text = parse_error_history[-1]
                    if is_tool_return:
                        extra_hint = "，不要给回复 JSON 的 delta.content 赋值为 修正后的json内容"
                    else:
                        extra_hint = ""
                    current_prompt = (
                        f"{prompt_text}\n\n"
                        f"[系统提示：你上一次回复的JSON格式解析失败，请修正后重新输出完整且合法的JSON{extra_hint}。]\n"
                        f"[错误信息：{err_msg}]\n"
                        f"[你的原始回复（前500字符）：{raw_text[:2000]}]"
                    )
                    logger.info(f"Doubao retry with parse error feedback: {err_msg[:200]}")

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
                        logger.info(f"Doubao done: suppress_text={suppress_text}, full_text_len={len(full_text)}, full_text_preview=\n{full_text[:2000]!r}")
                        try:
                            content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls = self._parse_response(full_text)
                            logger.info(f"Doubao done: content={content!r}, tool_calls=\n{tool_calls!r}, finish_reason={finish_reason!r}, is_openai_chunk={is_openai_chunk}, is_tool_calls={is_tool_calls}")

                            parse_success = False
                            if content is None and tool_calls is not None:
                                if suppress_text:
                                    err_msg = ""
                                    is_valid_json = False
                                    try:
                                        import json as _json
                                        full_text=json.dumps(_json.loads(full_text), ensure_ascii=False, indent=4)
                                        is_valid_json = True
                                    except Exception as parse_e:
                                        err_msg = str(parse_e)[:200]
                                        logger.warning(f"----Doubao parse failed: {err_msg}")

                                    if is_valid_json:
                                        # 验证嵌套的 arguments JSON
                                        try:
                                            import json as _json2
                                            validated = _json2.loads(full_text)
                                            logger.debug(f"Doubao 验证嵌套的 arguments JSON...")
                                            ok, nested_err = self._validate_json_nested(validated)
                                            if not ok:
                                                err_msg = nested_err
                                                is_valid_json = False
                                        except Exception as ve:
                                            err_msg = str(ve)[:200]
                                            is_valid_json = False

                                    if is_tool_return and is_valid_json and not is_openai_chunk and not is_tool_calls:
                                        content = full_text
                                        finish_reason = "stop"
                                        logger.info(f"Doubao tool_return response: {full_text[:2000]}")
                                        parse_success = True
                                    elif not is_valid_json:
                                        logger.warning(f"Doubao parse failed (attempt {attempt+1}/{max_retries}): {err_msg}")
                                        parse_error_history.append((err_msg, full_text))
                                        if attempt < max_retries - 1:
                                            logger.info(f"Doubao retrying with parse error feedback...")
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
                                    if is_agent:
                                        logger.warning(f"Doubao agent request got non-JSON response (suppress_text=False), content is empty")
                                        parse_error_history.append(("Non-JSON response from agent", full_text))
                                        if attempt < max_retries - 1:
                                            await asyncio.sleep(5)
                                            await self._delete_current_conversation()
                                            break
                                        else:
                                            yield self._format_error("服务器内部错误！", model, chat_id)
                                            self._save_conversation_id(chat_id)
                                            await self._delete_current_conversation()
                                            return
                            elif content is not None or tool_calls is not None:
                                parse_success = True

                            if parse_success:
                                if is_agent and buffered_chunks is not None and not suppress_text:
                                    for chunk in buffered_chunks:
                                        yield chunk
                                    buffered_chunks.clear()

                                if content and not tool_calls:
                                    yield self._format_chunk(content, model, chat_id)
                                elif not tool_calls and suppress_text and full_text.strip() and not is_openai_chunk and not is_tool_calls:
                                    yield self._format_chunk(full_text, model, chat_id)

                                if tool_calls:
                                    logger.info(f"Doubao yielding {len(tool_calls)} tool_calls")
                                    for chunk in self._yield_tool_calls(tool_calls, model, chat_id, content=content):
                                        logger.debug(f"Doubao tool_call chunk: \n{chunk.decode(errors='replace')[:2000]}")
                                        yield chunk
                                    fr = finish_reason or "tool_calls"
                                    yield format_openai_chunk(None, model, chat_id, "", finish_reason=fr).encode()
                                else:
                                    yield format_openai_chunk(None, model, chat_id, "", finish_reason="stop").encode()

                                yield format_openai_done().encode()
                                self._save_conversation_id(chat_id)
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
                # After async-for loop ends (without a return from done handler):
                if got_rate_limit:
                    if attempt < max_retries - 1:
                        wait_time = 5 + attempt * 10
                        logger.info(f"Doubao rate limited, waiting {wait_time}s before retry (attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(wait_time)
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
            yield self._format_error("服务器内部错误！", model, chat_id)
        except Exception as e:
            logger.error(f"[Doubao] stream_chat error: {e}")
            self._save_conversation_id(chat_id)
            await self._delete_current_conversation()
            yield self._format_error(str(e), model, chat_id)
        finally:
            self._doubao_lock.release()

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

