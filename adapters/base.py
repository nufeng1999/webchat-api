import json
import time
import uuid
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional
from models import ChatCompletionRequest
from sse import format_openai_chunk, format_openai_done, extract_text_from_content

logger = logging.getLogger("base-adapter")


class BaseAdapter(ABC):
    """多站点适配器基类，包含所有可公用的工具方法。"""

    @abstractmethod
    def get_adapter_name(self) -> str:
        ...

    @abstractmethod
    def get_models(self) -> dict[str, dict]:
        ...

    def get_model_ids(self) -> list[str]:
        return list(self.get_models().keys())

    def supports_model(self, model: str) -> bool:
        return model in self.get_models()

    @abstractmethod
    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        ...

    @abstractmethod
    async def non_stream_chat(self, request: ChatCompletionRequest) -> dict:
        ...

    async def init(self):
        pass

    async def close(self):
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_is_agent_request
    # ═══════════════════════════════════════════════════════════════════════

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

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_validate_json_nested
    # ═══════════════════════════════════════════════════════════════════════

    def _validate_json_nested(self, obj, path=""):
        """验证 JSON 对象中 "arguments" 字段的值是否为合法 JSON 字符串。"""
        adapter_name = self.get_adapter_name()
        if isinstance(obj, dict):
            for key in ("arguments", "args", "arguments_str"):
                if key in obj:
                    val = obj[key]
                    if isinstance(val, str) and val.strip().startswith("{"):
                        try:
                            json.loads(val, strict=False)
                        except json.JSONDecodeError as e:
                            logger.warning(f"{adapter_name} nested JSON validation failed: {path}[{key}]: {e}")
                            return False, f"{key} 字段的值 JSON 解析失败，请重新生成 {key} 字段的json内容，并确保其格式正确，结构不会错乱。"
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

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_validate_tool_calls_arguments
    # ═══════════════════════════════════════════════════════════════════════

    def _validate_tool_calls_arguments(self, tool_calls: list) -> tuple[bool, str]:
        """验证 tool_calls 中每个 function 的 arguments 是否为合法 JSON。"""
        adapter_name = self.get_adapter_name()
        for tc in tool_calls:
            args_val = tc.get("function", {}).get("arguments", "")
            if isinstance(args_val, str) and args_val.strip().startswith("{"):
                try:
                    json.loads(args_val, strict=False)
                except json.JSONDecodeError as ae:
                    err_msg = str(ae)[:200]
                    logger.warning(f"{adapter_name} arguments validation failed: {err_msg}")
                    return False, err_msg
        return True, ""

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_parse_response
    # ═══════════════════════════════════════════════════════════════════════

    def _parse_response(self, full_text: str) -> tuple:
        """解析 full_text 为 (content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls)。"""
        adapter_name = self.get_adapter_name()
        is_openai_chunk = False
        is_tool_calls = False
        text_to_parse = full_text.strip()

        # 去掉 markdown 代码块
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

        # 快速判断文本是否可能为 JSON，纯文字直接跳过解析
        first_char = text_to_parse[0]
        if first_char not in ('{', '[', '"'):
            return None, None, None, is_openai_chunk, is_tool_calls

        # 尝试解析 JSON
        try:
            parsed = json.loads(text_to_parse, strict=False)
        except json.JSONDecodeError as e:
            pos = e.pos
            snippet = text_to_parse[max(0, pos - 40):pos + 40]
            logger.warning(f"{adapter_name} JSON decode error at char {pos}: ...{snippet!r}...")
            try:
                from config import BASE_DIR
                import os
                debug_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(debug_dir, exist_ok=True)
                with open(os.path.join(debug_dir, f"{adapter_name}_full_text_debug.txt"), 'w', encoding='utf-8') as f:
                    f.write(full_text)
            except Exception:
                pass

            # 尝试修复转义层级问题
            repaired = False
            for _ in range(5):
                try:
                    parsed = json.loads(text_to_parse, strict=False)
                    repaired = True
                    break
                except json.JSONDecodeError:
                    pass
                try:
                    fixed = text_to_parse.replace('\\\\\\\\', '\\\\').replace('\\\\"', '\\"').replace('\\\\n', '\\n').replace('\\\\t', '\\t').replace('\\\\/', '/')
                    text_to_parse = fixed
                except Exception:
                    break

            if not repaired:
                brace_diff = text_to_parse.count("}") - text_to_parse.count("{")
                if brace_diff > 0 and text_to_parse.rstrip().endswith("}"):
                    stripped = text_to_parse.rstrip()
                    while stripped.endswith("}") and stripped.count("}") > stripped.count("{"):
                        stripped = stripped[:-1].rstrip()
                        try:
                            parsed = json.loads(stripped, strict=False)
                            repaired = True
                            break
                        except json.JSONDecodeError:
                            continue
                if not repaired:
                    logger.error(f"{adapter_name} JSON repair failed after escape fix, text={text_to_parse[:200]!r}")
                    return None, None, None, is_openai_chunk, is_tool_calls
        except TypeError as e:
            logger.error(f"{adapter_name} JSON decode TypeError: {e}")
            return None, None, None, is_openai_chunk, is_tool_calls

        if not isinstance(parsed, dict):
            return None, None, None, is_openai_chunk, is_tool_calls

        # OpenAI chunk 格式: {choices: [{delta/message, ...}]}
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
                    return content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls

        # tool_calls 字段: {tool_calls: [...]}
        if "tool_calls" in parsed and isinstance(parsed.get("tool_calls"), list):
            is_tool_calls = True
            tool_calls = parsed["tool_calls"]
            if not tool_calls:
                tool_calls = None
            return None, tool_calls, "tool_calls", is_openai_chunk, is_tool_calls

        # 单个 tool_call: {id, type, function}
        if parsed.get("id") and parsed.get("type") == "function" and parsed.get("function"):
            is_tool_calls = True
            return None, [parsed], "tool_calls", is_openai_chunk, is_tool_calls

        return None, None, None, is_openai_chunk, is_tool_calls

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_yield_tool_calls
    # ═══════════════════════════════════════════════════════════════════════

    def _yield_tool_calls(self, tool_calls: list, model: str, chat_id: str, content=None) -> AsyncGenerator[bytes, None]:
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

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_format_chunk / _format_error / _format_done
    # ═══════════════════════════════════════════════════════════════════════

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

    def _format_done(self) -> bytes:
        return b"data: [DONE]\n\n"

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

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_extract_last_text
    # ═══════════════════════════════════════════════════════════════════════

    def _extract_last_text(self, request: ChatCompletionRequest) -> str:
        last_msg = request.messages[-1] if request.messages else None
        return extract_text_from_content(getattr(last_msg, 'content', '')) if last_msg else ""

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_generate_chat_id
    # ═══════════════════════════════════════════════════════════════════════

    def _generate_chat_id(self) -> str:
        return f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：non_stream_chat
    # ═══════════════════════════════════════════════════════════════════════

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
            logger.error(f"[{self.get_adapter_name()}] non_stream_chat error: {e}")
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

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_handle_chunk_streaming（流式处理增量 chunk）
    # ═══════════════════════════════════════════════════════════════════════

    async def _handle_chunk_streaming(
        self,
        kind: str,
        value,
        *,
        model: str,
        chat_id: str,
        is_agent: bool,
        full_text: str,
        suppress_text: bool,
        buffered_chunks: Optional[list],
    ) -> tuple[str, bool, Optional[list], bool, Optional[bytes]]:
        """
        处理一个 kind+value 增量。
        返回: (new_full_text, new_suppress_text, new_buffered_chunks, should_return, return_value_or_None)
        """
        if kind == "error":
            return full_text, suppress_text, buffered_chunks, True, self._format_error(str(value), model, chat_id)

        if kind == "chunk":
            full_text += value
            if not suppress_text:
                ft = full_text.lstrip()
                if ft[:1] == "{" or ft[:3] == "```":
                    suppress_text = True
                    adapter_name = self.get_adapter_name()
                    logger.info(f"{adapter_name} suppress_text triggered: ft_start={ft[:100]!r}")
            if not suppress_text:
                if is_agent and buffered_chunks is not None:
                    buffered_chunks.append(self._format_chunk(value, model, chat_id))
                else:
                    return full_text, suppress_text, buffered_chunks, True, self._format_chunk(value, model, chat_id)

        return full_text, suppress_text, buffered_chunks, False, None

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_validate_done_response（验证 done 后的响应是否合法）
    # ═══════════════════════════════════════════════════════════════════════

    def _validate_done_response(
        self,
        content,
        tool_calls,
        finish_reason,
        is_openai_chunk: bool,
        is_tool_calls: bool,
        suppress_text: bool,
        is_agent: bool,
        is_tool_return: bool,
        full_text: str,
    ) -> tuple[bool, str, Optional[str], str]:
        """
        验证 done 后的响应是否合法，返回 (should_retry, err_msg, tool_return_content_or_None, normalized_full_text)。
        - should_retry: True 表示需要重试
        - err_msg: 错误信息
        - tool_return_content_or_None: 如果是 tool_return 响应，返回 JSON 字符串
        - normalized_full_text: 规范化后的 full_text（用于后续输出）
        """
        adapter_name = self.get_adapter_name()

        if not is_agent:
            return False, "", None, full_text

        # agent 非 tool_return 请求：允许普通文本回复
        if not is_tool_return and content is None and tool_calls is None:
            # suppress_text=True 时：chunks 未被 yield，需要把 full_text 作为 content 输出
            # suppress_text=False 时：chunks 已缓冲，由 _yield_final_response 从 buffered_chunks 输出
            if suppress_text:
                return False, "", full_text, full_text
            return False, "", None, full_text

        if content is None and tool_calls is None:
            if suppress_text:
                # 先尝试 JSON 规范化
                normalized = full_text
                is_valid_json = False
                try:
                    normalized = json.dumps(json.loads(full_text, strict=False), ensure_ascii=False, indent=4)
                    is_valid_json = True
                except Exception:
                    pass

                if is_valid_json:
                    # JSON 嵌套验证
                    try:
                        validated = json.loads(normalized, strict=False)
                        ok, nested_err = self._validate_json_nested(validated)
                        if not ok:
                            logger.warning(f"{adapter_name} nested JSON validation failed: {nested_err}")
                            return True, nested_err, None, normalized
                    except Exception as ve:
                        logger.warning(f"{adapter_name} nested JSON validation exception: {ve}")
                        return True, str(ve)[:200], None, normalized

                    # tool_return 响应：内容是 JSON 字符串且不是 OpenAI chunk/tool_calls 格式
                    if is_tool_return and not is_openai_chunk and not is_tool_calls:
                        # 尝试从 JSON 中提取 content 字段
                        content_val = validated.get("content")
                        if not content_val and isinstance(validated, dict):
                            # 搜索嵌套字段（如 import.content、choices[0].message.content）
                            for key, val in validated.items():
                                if isinstance(val, dict) and "content" in val:
                                    content_val = val["content"]
                                    break
                        if content_val:
                            return False, "", content_val, normalized
                        # 没有 content 字段，返回整个 JSON 作为回复
                        return False, "", normalized, normalized

                    # 合法 JSON，finish_reason=stop 或非 agent → 直接放行，作为 content 输出
                    if finish_reason == "stop" or not is_agent:
                        logger.info(f"{adapter_name} valid JSON with finish_reason={finish_reason}, passing through as content")
                        return False, "", None, normalized

                    # agent 请求且 finish_reason != stop，需要重试
                    logger.warning(f"{adapter_name} JSON parse failed (content=None, tool_calls=None, agent, finish_reason={finish_reason})")
                    return True, "JSON format error", None, normalized
                else:
                    # JSON 不合法，需要重试
                    logger.warning(f"{adapter_name} invalid JSON (content=None, tool_calls=None, suppress_text=True)")
                    return True, "Invalid JSON", None, full_text
            else:
                # suppress_text=False 且 agent 请求，需要重试
                if is_agent:
                    logger.warning(f"{adapter_name} agent request got non-JSON response (suppress_text=False)")
                    return True, "Non-JSON response from agent", None, full_text
                else:
                    return False, "", None, full_text

        # content/tool_calls 不为 None，验证 arguments
        if tool_calls:
            all_valid, args_err = self._validate_tool_calls_arguments(tool_calls)
            if not all_valid:
                return True, f"arguments JSON invalid: {args_err}", None, full_text

        return False, "", None, full_text

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_yield_final_response（yield 最终响应）
    # ═══════════════════════════════════════════════════════════════════════

    async def _yield_final_response(
        self,
        content,
        tool_calls,
        finish_reason,
        suppress_text: bool,
        is_agent: bool,
        buffered_chunks: Optional[list],
        full_text: str,
        model: str,
        chat_id: str,
        is_openai_chunk: bool,
        is_tool_calls: bool,
    ) -> AsyncGenerator[bytes, None]:
        """yield 最终响应（先 yield 缓冲的 chunk，再 yield 最终内容/tool_calls/done）。"""
        adapter_name = self.get_adapter_name()

        # 先 yield 缓冲的 chunk（非 suppress_text 的 agent 请求）
        if is_agent and buffered_chunks is not None and not suppress_text:
            for chunk in buffered_chunks:
                yield chunk
            buffered_chunks.clear()

        # yield 内容
        if content and not tool_calls:
            yield self._format_chunk(content, model, chat_id)
        elif not tool_calls and suppress_text and full_text.strip() and not is_openai_chunk and not is_tool_calls:
            yield self._format_chunk(full_text, model, chat_id)

        # yield tool_calls
        if tool_calls:
            logger.info(f"{adapter_name} yielding {len(tool_calls)} tool_calls")
            for chunk in self._yield_tool_calls(tool_calls, model, chat_id, content=content):
                logger.debug(f"{adapter_name} tool_call chunk: \n{chunk.decode(errors='replace')[:2000]}")
                yield chunk
            fr = finish_reason or "tool_calls"
            yield format_openai_chunk(None, model, chat_id, "", finish_reason=fr).encode()
        else:
            yield format_openai_chunk(None, model, chat_id, "", finish_reason="stop").encode()

        yield self._format_done()
