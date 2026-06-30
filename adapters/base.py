import json
import time
import uuid
import logging
import asyncio
from json_fixer import JsonFixer
from config import Colors
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

    async def generate_images(self, prompt: str, n: int = 1, size: str = "1024x1024", **kwargs) -> dict:
        """
        生成图片。默认实现返回"不支持"错误，具体适配器覆盖此方法。
        
        返回 OpenAI 兼容格式:
        {
            "created": timestamp,
            "data": [
                {"url": "...", "revised_prompt": "...", "size": "..."},
                ...
            ]
        }
        """
        return {
            "created": int(time.time()),
            "data": [{
                "url": "",
                "revised_prompt": prompt,
                "size": size,
                "error": f"{self.get_adapter_name()} adapter does not support image generation yet"
            }]
        }

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
                            # logger.debug(f"----------------1")
                            JsonFixer().fix(val)
                        except ValueError as e:
                            logger.warning(f"{adapter_name} nested JSON validation failed: {path}[{key}]: {e}")
                            return False, f"{e}"
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
                    # logger.debug(f"----------------2")
                    JsonFixer().fix(args_val)
                except ValueError as ae:
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

        # 清除空字节
        text_to_parse = text_to_parse.replace('\x00', '')

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

        # 尝试解析 JSON（使用 JsonFixer 一站式修复：markdown 去除、arguments 修复、转义层级、括号平衡、json_repair 兜底）
        try:
            # logger.debug(f"----------------3")
            parsed = JsonFixer().fix(text_to_parse)
        except ValueError as e:
            pos = getattr(e, 'pos', 0) or 0
            msg = getattr(e, 'msg', str(e))
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
            logger.error(f"{adapter_name} JSON repair failed: {msg}")
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
    # 公共工具方法：_strip_think_tags
    # ═══════════════════════════════════════════════════════════════════════

    def _strip_think_tags(self, text: str) -> str:
        """去除 AI 回复中的 <think>...</think> 标签及其内容。"""
        import re
        return re.sub(r'<think>[\s\S]*?</think>', '', text).strip()

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
        _think_buf: str = "",
    ) -> tuple[str, bool, Optional[list], bool, Optional[bytes], str]:
        """
        处理一个 kind+value 增量。
        返回: (new_full_text, new_suppress_text, new_buffered_chunks, should_return, return_value_or_None, new_think_buf)
        """
        if kind == "error":
            return full_text, suppress_text, buffered_chunks, True, self._format_error(str(value), model, chat_id), _think_buf

        if kind == "chunk":
            value = value.replace('\x00', '')
            if not value:
                return full_text, suppress_text, buffered_chunks, False, None, _think_buf
            full_text += value
            if not suppress_text:
                ft = full_text.lstrip()
                if ft[:1] == "{" or ft[:3] == "```":
                    suppress_text = True
                    adapter_name = self.get_adapter_name()
                    logger.info(f"{adapter_name} suppress_text triggered: ft_start={ft[:100]!r}")
            if not suppress_text:
                # 合并 _think_buf + value 做跨 chunk think 标签过滤
                combined = _think_buf + value
                cleaned, _think_buf = self._strip_think_tags_with_buf(combined)
                if is_agent and buffered_chunks is not None:
                    if cleaned:
                        buffered_chunks.append(self._format_chunk(cleaned, model, chat_id))
                else:
                    if cleaned:
                        return full_text, suppress_text, buffered_chunks, True, self._format_chunk(cleaned, model, chat_id), _think_buf
                    elif _think_buf:
                        # 缓冲区有内容但不输出，等待闭合
                        return full_text, suppress_text, buffered_chunks, False, None, _think_buf
                    else:
                        # 清空缓冲区（可能是空 chunk）
                        return full_text, suppress_text, buffered_chunks, False, None, ""

        return full_text, suppress_text, buffered_chunks, False, None, _think_buf

    def _strip_think_tags_with_buf(self, text: str) -> tuple[str, str]:
        """
        去除 think 标签，支持跨 chunk 的缓冲区。
        返回: (cleaned_output, remaining_buf)
        - 如果检测到完整的 ...`` 标签，直接去除并返回空 buf
        - 如果检测到未闭合的 ``，将剩余内容暂存到 buf 中
        - 如果 buf 中有残留的 `` 且当前文本包含闭合的 ``，则组合后去除
        """
        import re
        OPEN_TAG = '<think>'
        CLOSE_TAG = '</think>'

        buf = ""
        result = []

        # 如果缓冲区有未闭合的 ``，先追加到当前文本开头
        if buf:
            text = buf + text

        while text:
            open_pos = text.find(OPEN_TAG)
            if open_pos == -1:
                # 没有更多 `` 了，剩余全部输出
                result.append(text)
                break

            # 输出 `` 之前的内容
            if open_pos > 0:
                result.append(text[:open_pos])

            # 找到闭合标签
            close_pos = text.find(CLOSE_TAG, open_pos)
            if close_pos == -1:
                # 未闭合，将 `` 及之后内容暂存到缓冲区
                buf = text[open_pos:]
                break
            else:
                # 完整标签，跳过 ...`` 之间的内容
                text = text[close_pos + len(CLOSE_TAG):]

        return "".join(result), buf

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

        if not is_agent and not suppress_text and tool_calls is None:
            cleaned = self._strip_think_tags(full_text)
            if cleaned != full_text:
                logger.info(f"{adapter_name} stripped think tags from non-agent response")
            return False, "", None, cleaned

        # agent 非 tool_return 请求：允许普通文本回复，但也验证嵌套 JSON
        if not is_tool_return and content is None and tool_calls is None:
            # suppress_text=True 时：chunks 未被 yield，需要把 full_text 作为 content 输出
            # suppress_text=False 时：chunks 已缓冲，由 _yield_final_response 从 buffered_chunks 输出
            if suppress_text:
                # 先剥离 think 标签，再做 JSON 验证
                cleaned = self._strip_think_tags(full_text)
                ft = cleaned.strip()
                if ft and ft[0] in ('{', '['):
                    try:
                        # logger.debug(f"----------------4")
                        parsed = JsonFixer().fix(cleaned)
                        ok, nested_err = self._validate_json_nested(parsed)
                        if not ok:
                            logger.warning(f"{adapter_name} nested JSON validation failed (non-tool-return): {nested_err}")
                            return True, nested_err, None, cleaned
                    except ValueError as e:
                        logger.warning(f"{adapter_name} JSON parse failed in validate: {e}")
                        return True, f"Invalid JSON: {e}", None, cleaned
                    return False, "", cleaned, cleaned
            # suppress_text=False: chunks 已缓冲，但 full_text 仍可能含 think 标签
            cleaned = self._strip_think_tags(full_text)
            return False, "", None, cleaned

        if content is None and tool_calls is None:
            if suppress_text:
                # 先剥离 think 标签，再尝试 JSON 规范化
                cleaned = self._strip_think_tags(full_text)
                normalized = cleaned
                is_valid_json = False
                try:
                    # logger.debug(f"----------------5")
                    parsed = JsonFixer().fix(cleaned)
                    normalized = json.dumps(parsed, ensure_ascii=False, indent=4)
                    is_valid_json = True
                except ValueError:
                    pass

                if is_valid_json:
                    # JSON 嵌套验证
                    try:
                        ok, nested_err = self._validate_json_nested(parsed)
                        if not ok:
                            logger.warning(f"{adapter_name} nested JSON validation failed: {nested_err}")
                            return True, nested_err, None, normalized
                    except Exception as ve:
                        logger.warning(f"{adapter_name} nested JSON validation exception: {ve}")
                        return True, str(ve)[:200], None, normalized

                    # tool_return 响应：内容是 JSON 字符串且不是 OpenAI chunk/tool_calls 格式
                    if is_tool_return and not is_openai_chunk and not is_tool_calls:
                        # 尝试从 JSON 中提取 content 字段
                        content_val = parsed.get("content")
                        if not content_val and isinstance(parsed, dict):
                            # 搜索嵌套字段（如 import.content、choices[0].message.content）
                            for key, val in parsed.items():
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
                    # 非 JSON 内容：去除 think 标签
                    cleaned = self._strip_think_tags(full_text)
                    if cleaned != full_text:
                        logger.info(f"{adapter_name} stripped think tags from non-JSON response")
                    return False, "", None, cleaned

        # content/tool_calls 不为 None，验证 arguments
        if tool_calls:
            all_valid, args_err = self._validate_tool_calls_arguments(tool_calls)
            if not all_valid:
                return True, f"arguments JSON invalid: {args_err}", None, full_text

        # 对 content 中的 JSON 和 tool_calls.arguments 中的嵌套 JSON 进行深层验证
        json_to_validate = None
        if content and isinstance(content, str) and content.strip().startswith("{"):
            try:
                # logger.debug(f"----------------6")
                json_to_validate = JsonFixer().fix(content)
            except Exception as e:
                logger.warning(f"nested JSON validation failed: {e}")
        if json_to_validate is None and suppress_text and full_text.strip().startswith("{"):
            try:
                # logger.debug(f"----------------7")
                json_to_validate = JsonFixer().fix(full_text)
            except Exception as e:
                logger.warning(f"nested JSON validation failed: {e}")
        if json_to_validate is not None:
            ok, nested_err = self._validate_json_nested(json_to_validate)
            if not ok:
                logger.warning(f"{adapter_name} nested JSON validation failed: {nested_err}")
                return True, nested_err, None, full_text

            # 如果 content 是嵌套 JSON（如 {"finish_reason":"stop","content":"..."}），提取 inner content
            tool_return_content = None
            if content and isinstance(content, str) and content.strip().startswith("{"):
                if isinstance(json_to_validate, dict) and "content" in json_to_validate:
                    inner_content = json_to_validate.get("content")
                    if inner_content is not None:
                        tool_return_content = inner_content if isinstance(inner_content, str) else json.dumps(inner_content, ensure_ascii=False)
                        logger.info(f"{adapter_name} extracted inner content from nested JSON")

            return False, "", tool_return_content, full_text

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

    # ═══════════════════════════════════════════════════════════════════════
    # 公共工具方法：_stream_chat_template（流式对话统一模板）
    # ═══════════════════════════════════════════════════════════════════════
    #
    # 适配器只需覆盖以下 hook 方法即可实现差异化：
    #   _get_lock()                      → 返回 asyncio.Lock
    #   _prepare_messages(req, bc, agent) → 返回 (prompt_text, file_content)
    #   _build_stream_kwargs(prompt, file_content, is_agent) → dict
    #   _call_stream(**kwargs)            → AsyncGenerator[kind, value]
    #   _on_session_id(value)             → 处理 session_id 事件
    #   _delete_conversation()            → 删除当前对话
    #   _handle_rate_limit()              → (doubao 专用) 限流处理
    #   _on_error_yield(chat_id)          → (doubao 专用) 额外错误处理
    #   _on_success(chat_id)              → (doubao 专用) 成功后回调
    # ═══════════════════════════════════════════════════════════════════════

    def _get_lock(self):
        raise NotImplementedError

    @abstractmethod
    async def _prepare_messages(self, request, browser_client, is_agent: bool):
        """返回 (prompt_text, file_content)。file_content 非 agent 时为 None。"""
        ...

    def _build_stream_kwargs(self, prompt_text: str, file_content, is_agent: bool, current_prompt: str) -> dict:
        """构建传给 _call_stream 的 kwargs。默认实现适合 mimo/zai/deepseek 风格。"""
        kwargs = {"prompt": current_prompt}
        extra = getattr(self, '_stream_extra_kwargs', {})
        if extra:
            kwargs.update(extra)
        if is_agent and file_content:
            kwargs["inline_file_content"] = file_content
        return kwargs

    @abstractmethod
    async def _call_stream(self, **kwargs):
        """调用浏览器客户端的流式方法，返回 AsyncGenerator[kind, value]。"""
        ...

    async def _on_session_id(self, value):
        """处理流中收到的 session_id/conversation_id 事件。"""
        pass

    @abstractmethod
    async def _delete_conversation(self):
        """删除当前对话/会话。"""
        ...

    async def _handle_rate_limit(self, attempt: int, max_retries: int, error_msg: str = None):
        """处理限流。返回 True 表示已处理可继续重试，False 表示无法处理。"""
        return False

    async def _on_error_yield(self, chat_id: str):
        """流错误 yield 后的额外处理。"""
        pass

    async def _on_success(self, chat_id: str):
        """成功完成后的额外处理。"""
        pass

    async def _on_done_extra(self):
        """done 处理完成后、_delete_conversation 前的额外处理（如 qianwen 重捕获 session_id）。"""
        pass

    async def _on_finally_extra(self):
        """finally 块中 lock.release() 前的额外清理。"""
        pass

    def _session_id_kind(self) -> str:
        """流事件中携带 session ID 的 kind 值。"""
        return "session_id"

    def _stream_error_no_delete(self) -> bool:
        """流 error 事件后不删除对话（qianwen 用）。"""
        return False

    def _use_parse_error_history(self) -> bool:
        """是否使用 parse_error_history 构建 retry prompt（qianwen 不用）。"""
        return True

    async def _stream_chat_template(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from browser_client import browser_client
        adapter_name = self.get_adapter_name()
        logger.debug(f"{Colors.BOLD_GREEN}[{adapter_name}] -------------------new request---------------------{Colors.RESET}")
        chat_id = self._generate_chat_id()
        model = request.model
        is_agent = self._is_agent_request(request)
        max_retries = 6
        use_peh = self._use_parse_error_history()
        parse_error_history = [] if use_peh else None

        lock = self._get_lock()
        await lock.acquire()
        try:
            prompt_text, file_content = await self._prepare_messages(request, browser_client, is_agent)
            is_tool_return = getattr(request.messages[-1], 'role', None) == 'tool' if (is_agent and request.messages) else False
            self._last_is_tool_return = is_tool_return

            for attempt in range(max_retries):
                full_text = ""
                suppress_text = False
                buffered_chunks = [] if is_agent else None
                think_buf = ""
                got_rate_limit = False
                rate_limit_error = None
                parse_success = False
                is_retry_break = False

                if use_peh and parse_error_history is not None:
                    current_prompt = self._build_retry_prompt(prompt_text, is_tool_return, parse_error_history)
                    if parse_error_history:
                        logger.info(f"{adapter_name} retry with parse error feedback: {parse_error_history[-1][0][:200]}")
                else:
                    current_prompt = prompt_text

                stream_kwargs = self._build_stream_kwargs(prompt_text, file_content, is_agent, current_prompt)

                # Wsession: 激活已有对话（如果 conversation_id 存在）
                conv_id = getattr(request, 'conversation_id', None)
                if conv_id and conv_id != "0":
                    from browser_client import browser_client
                    activate_map = {
                        "doubao": browser_client.activate_doubao_conversation,
                        "qianwen": browser_client.activate_qianwen_conversation,
                        "deepseek": browser_client.activate_deepseek_conversation,
                        "zai": browser_client.activate_zai_conversation,
                        "mimo": browser_client.activate_mimo_conversation,
                        "minimax": browser_client.activate_minimax_conversation,
                        "xinghuo": browser_client.activate_xinghuo_conversation,
                    }
                    activate_fn = activate_map.get(adapter_name)
                    if activate_fn:
                        activated = await activate_fn(conv_id)
                        if activated:
                            # 传递会话 ID 给底层，某些站点（如星火）使用
                            if adapter_name == "xinghuo":
                                stream_kwargs["conversation_id"] = conv_id
                            # 标记复用对话，避免 protocol 再次导航到新对话
                            stream_kwargs["reuse_conversation"] = True
                            # 复用对话时，SSE 可能不返回新的 conversation_id，预置 _last_conversation_id
                            self._last_conversation_id = conv_id
                        else:
                            logger.warning(f"[{adapter_name}] conversation activation failed: conv_id={conv_id}, will create new")
                            request.conversation_id = "0"

                logger.info(f"[{adapter_name} Adapter] {Colors.RED}Attempt {attempt+1}/{max_retries}{Colors.RESET}")

                async for kind, value in self._call_stream(**stream_kwargs):
                    if kind == "error":
                        err_str = str(value).lower()
                        if "rate" in err_str and "limit" in err_str:
                            got_rate_limit = True
                            rate_limit_error = str(value)
                            logger.warning(f"{adapter_name} rate limited (attempt {attempt+1}/{max_retries})")
                            break
                        # Timeout or route not triggered — should retry
                        if "timeout" in err_str or "fetch error" in err_str:
                            logger.warning(f"{adapter_name} {err_str} (attempt {attempt+1}/{max_retries}), will retry")
                            got_rate_limit = True  # reuse retry mechanism
                            rate_limit_error = str(value)
                            break
                        yield self._format_error(str(value), model, chat_id)
                        await self._on_error_yield(chat_id)
                        return

                    if kind == self._session_id_kind():
                        await self._on_session_id(value)
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
                            return

                    if kind == "done":
                        logger.debug(f"{adapter_name} done: suppress_text={suppress_text}, full_text_len={len(full_text)}, full_text_preview=\n{full_text[:2000]!r}")
                        try:
                            content, tool_calls, finish_reason, is_openai_chunk, is_tool_calls = self._parse_response(full_text)
                            logger.info(f"{adapter_name} done: content={content!r}, tool_calls={tool_calls!r}, finish_reason={finish_reason!r}")

                            should_retry, err_msg, tool_return_content, full_text = self._validate_done_response(
                                content, tool_calls, finish_reason,
                                is_openai_chunk, is_tool_calls,
                                suppress_text, is_agent, is_tool_return, full_text
                            )
                            logger.debug(f"{adapter_name} done: should_retry={should_retry}, err_msg={err_msg!r}, tool_return_content={tool_return_content!r}")

                            if tool_return_content is not None:
                                content = tool_return_content
                                finish_reason = "stop"
                                logger.info(f"{adapter_name} tool_return response: {tool_return_content[:2000]}")
                                parse_success = True
                            elif should_retry:
                                if use_peh and parse_error_history is not None:
                                    parse_error_history.append((err_msg, full_text))
                                if attempt < max_retries - 1:
                                    logger.info(f"{adapter_name} retrying (attempt {attempt+1}/{max_retries}): {err_msg[:200]}")
                                    await asyncio.sleep(5)
                                    is_retry_break = True
                                    break
                                else:
                                    logger.error(f"{adapter_name} parse failed after {max_retries} attempts")
                                    yield self._format_error("服务器内部错误！", model, chat_id)
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
                                await self._on_success(chat_id)
                                await self._on_done_extra()
                                return
                        except Exception as e:
                            logger.error(f"{adapter_name} done handler error: {e}")
                            err_msg = str(e)[:200]
                            if use_peh and parse_error_history is not None:
                                parse_error_history.append((err_msg, full_text))
                            if attempt < max_retries - 1:
                                logger.info(f"{adapter_name} retrying after done handler error: {err_msg}")
                                is_retry_break = True
                                break
                            else:
                                yield self._format_error("服务器内部错误！", model, chat_id)
                                return

                if got_rate_limit:
                    handled = await self._handle_rate_limit(attempt, max_retries, rate_limit_error)
                    if handled:
                        continue
                    if attempt < max_retries - 1:
                        continue
                    break

                if attempt < max_retries - 1 and not parse_success:
                    continue
                break

            yield self._format_error("千淘万漉虽辛苦，吹尽狂沙始到金。", model, chat_id)
        except Exception as e:
            logger.error(f"[{adapter_name}] stream_chat error: {e}")
            yield self._format_error(str(e), model, chat_id)
        finally:
            await self._on_finally_extra()
            lock.release()
