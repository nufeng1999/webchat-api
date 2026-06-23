"""
JSON 矫正器：修复 LLM 输出的破损 JSON，特别是 arguments 字段未正确转义为字符串的问题。

用法:
    from json_fixer import JsonFixer

    fixer = JsonFixer()
    result = fixer.fix(raw_json_str)
    result = fixer.fix_tool_calls(raw_json_str)

支持的修复场景:
    1. arguments 字段包含未转义的裸 JSON 对象
    2. 多层嵌套的 arguments 修复
    3. 不完整的 JSON 截断修复
    4. markdown 代码块包裹的 JSON
    5. 转义层级混乱（\\\\\\\\ 应为 \\\\）
    6. 括号不平衡（多余的 } 或缺 }）
    7. 数组内对象缺少 }（tool_calls 内的 tool_call 缺右括号）
    8. 多余的尾部逗号（,] 或 ,}）
    9. 顶层非 dict 时从 json_repair 的 list 结果中合并提取
"""
import json
import re
import logging
from typing import Optional

logger = logging.getLogger("json-fixer")


class JsonFixer:
    """JSON 矫正器：修复 LLM 输出的破损 JSON。"""

    _CODE_BLOCK_PATTERN = re.compile(r'```(?:json)?\s*\n(.*?)\n\s*```', re.DOTALL)

    def fix(self, raw_json_str: str) -> dict:
        """
        主入口：修复 LLM 输出的破损 JSON，返回解析后的 dict。
        修复策略按优先级排列：
            1. 直接解析
            2. 去除 markdown 代码块
            3. 修复 arguments 裸对象
            4. 修复转义层级
            5. 括号不平衡修复（多余 }）
            6. 数组内对象缺 } 修复
            7. 尾部逗号修复
            8. json_repair 兜底
        """
        logger.debug(f"开始 JSON 矫正\n{raw_json_str}")
        if not raw_json_str or not raw_json_str.strip():
            raise ValueError("输入为空，无法解析")

        text = raw_json_str.strip()

        # 1. 直接解析
        try:
            parsed = json.loads(text)
            return self._normalize_arguments(parsed)
        except json.JSONDecodeError:
            pass

        # 2. 去除 markdown 代码块
        cleaned = self._strip_code_block(text)
        if cleaned != text:
            try:
                parsed = json.loads(cleaned)
                return self._normalize_arguments(parsed)
            except json.JSONDecodeError:
                text = cleaned

        # 3. 修复 arguments 裸对象
        fixed = self._fix_bare_arguments(text)
        if fixed != text:
            try:
                parsed = json.loads(fixed)
                return self._normalize_arguments(parsed)
            except json.JSONDecodeError:
                text = fixed

        # 4. 修复转义层级
        fixed_escape = self._fix_escape_levels(text)
        try:
            parsed = json.loads(fixed_escape, strict=False)
            return self._normalize_arguments(parsed)
        except json.JSONDecodeError:
            pass

        # 5. 括号不平衡修复（多余的 }）
        fixed_braces = self._fix_unbalanced_braces(fixed_escape)
        try:
            parsed = json.loads(fixed_braces, strict=False)
            return self._normalize_arguments(parsed)
        except json.JSONDecodeError:
            pass

        # 6. 数组内对象缺 } 修复
        fixed_missing = self._fix_missing_closing_braces(fixed_braces)
        try:
            parsed = json.loads(fixed_missing, strict=False)
            return self._normalize_arguments(parsed)
        except json.JSONDecodeError:
            pass

        # 7. 尾部逗号修复
        fixed_trailing = self._fix_trailing_commas(fixed_missing)
        try:
            parsed = json.loads(fixed_trailing, strict=False)
            return self._normalize_arguments(parsed)
        except json.JSONDecodeError:
            pass

        # 8. json_repair 兜底
        try:
            import json_repair
            repaired = json_repair.loads(fixed_trailing)
            logger.debug(f"json_repair修复的\n{repaired}")
            repaired = self._merge_repaired(repaired)
            if isinstance(repaired, dict):
                return self._normalize_arguments(repaired)
        except ImportError:
            logger.debug("json_repair 库未安装，跳过")
        except Exception as e:
            logger.debug(f"json_repair 修复失败: {e}")

        # 所有策略失败
        try:
            final_err = json.loads(fixed_trailing, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"JSON 矫正失败 | pos={e.pos} | msg={e.msg}\n"
                f"修复后内容片段: {fixed_trailing[max(0, e.pos-50):e.pos+50]}"
            ) from e
        return self._normalize_arguments(final_err)

    def fix_arguments(self, raw_json_str: str) -> str:
        if not raw_json_str:
            return raw_json_str
        text = raw_json_str.strip()
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass
        text = self._strip_code_block(text)
        fixed = self._fix_bare_arguments(text)
        fixed = self._fix_escape_levels(fixed)
        return fixed

    def fix_tool_calls(self, raw_json_str: str) -> Optional[list]:
        try:
            result = self.fix(raw_json_str)
        except ValueError:
            return None
        if not isinstance(result, dict):
            return None
        choices = result.get("choices", [])
        if choices and isinstance(choices, list):
            choice = choices[0]
            delta = choice.get("delta") or choice.get("message", {})
            if isinstance(delta, dict):
                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    return self._fix_tool_calls_list(tool_calls)
        tool_calls = result.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            return self._fix_tool_calls_list(tool_calls)
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_arguments(data):
        if isinstance(data, dict):
            for key, val in data.items():
                if key == "arguments" and isinstance(val, dict):
                    data[key] = json.dumps(val, ensure_ascii=False)
                elif isinstance(val, (dict, list)):
                    JsonFixer._normalize_arguments(val)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    JsonFixer._normalize_arguments(item)
        return data

    @staticmethod
    def _strip_code_block(text: str) -> str:
        if not text.startswith("```"):
            return text
        m = JsonFixer._CODE_BLOCK_PATTERN.search(text)
        if m:
            return m.group(1).strip()
        return text

    @staticmethod
    def _fix_bare_arguments(text: str) -> str:
        result = text
        search_start = 0
        while True:
            idx = result.find('"arguments"', search_start)
            if idx == -1:
                break
            if idx > 0 and result[idx - 1] not in ('"', '{', ',', '[', '\n', '\t', ' '):
                search_start = idx + 1
                continue
            colon_pos = result.find(':', idx + len('"arguments"'))
            if colon_pos == -1:
                search_start = idx + 1
                continue
            after_colon = colon_pos + 1
            while after_colon < len(result) and result[after_colon] in ' \t\n\r':
                after_colon += 1
            if after_colon >= len(result):
                search_start = after_colon
                continue
            ch = result[after_colon]
            if ch == '"':
                fixed = JsonFixer._fix_bare_string_arguments(result, idx, after_colon)
                if fixed != result:
                    result = fixed
                    search_start = idx + 20
                else:
                    search_start = after_colon + 1
                continue
            if ch != '{':
                search_start = after_colon
                continue
            end_idx = JsonFixer._find_matching_brace(result, after_colon)
            if end_idx == 0:
                search_start = after_colon
                continue
            bare_object = result[after_colon:end_idx + 1]
            remainder = result[end_idx + 1:]
            try:
                parsed_obj = json.loads(bare_object)
                fixed_value = json.dumps(json.dumps(parsed_obj, ensure_ascii=False))
            except json.JSONDecodeError:
                try:
                    import json_repair
                    parsed_obj = json_repair.loads(bare_object)
                    if isinstance(parsed_obj, dict):
                        fixed_value = json.dumps(json.dumps(parsed_obj, ensure_ascii=False))
                    else:
                        fixed_value = JsonFixer._escape_for_string(bare_object)
                except Exception:
                    fixed_value = JsonFixer._escape_for_string(bare_object)
            result = result[:idx] + '"arguments":' + fixed_value + remainder
            search_start = idx + len(fixed_value)
        return result

    @staticmethod
    def _fix_bare_string_arguments(text: str, key_idx: int, open_quote_pos: int) -> str:
        content_start = open_quote_pos + 1
        if content_start >= len(text) or text[content_start] != '{':
            return text
        end_brace = JsonFixer._find_matching_brace(text, content_start)
        if end_brace == 0:
            return text
        if end_brace + 1 >= len(text) or text[end_brace + 1] != '"':
            return text
        bare_content = text[content_start:end_brace + 1]
        remainder = text[end_brace + 2:]
        try:
            parsed_obj = json.loads(bare_content)
            fixed_value = json.dumps(json.dumps(parsed_obj, ensure_ascii=False))
        except json.JSONDecodeError:
            try:
                import json_repair
                parsed_obj = json_repair.loads(bare_content)
                if isinstance(parsed_obj, dict):
                    fixed_value = json.dumps(json.dumps(parsed_obj, ensure_ascii=False))
                else:
                    fixed_value = JsonFixer._escape_for_string(bare_content)
            except Exception:
                fixed_value = JsonFixer._escape_for_string(bare_content)
        return text[:key_idx] + '"arguments":' + fixed_value + remainder

    @staticmethod
    def _find_matching_brace(text: str, start: int) -> int:
        if start >= len(text) or text[start] != '{':
            return 0
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\':
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
        return 0

    @staticmethod
    def _escape_for_string(raw: str) -> str:
        escaped = raw.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _fix_escape_levels(text: str) -> str:
        fixed = text
        for _ in range(5):
            try:
                json.loads(fixed, strict=False)
                return fixed
            except json.JSONDecodeError:
                pass
            try:
                fixed = (
                    fixed
                    .replace('\\\\\\\\', '\\\\')
                    .replace('\\\\"', '\\"')
                    .replace('\\\\n', '\\n')
                    .replace('\\\\t', '\\t')
                    .replace('\\\\/', '/')
                )
            except Exception:
                break
        return fixed

    @staticmethod
    def _fix_unbalanced_braces(text: str) -> str:
        brace_diff = text.count("}") - text.count("{")
        if brace_diff <= 0:
            return text
        stripped = text.rstrip()
        while stripped.endswith("}") and stripped.count("}") > stripped.count("{"):
            stripped = stripped[:-1].rstrip()
        return stripped

    @staticmethod
    def _fix_missing_closing_braces(text: str) -> str:
        """修复数组内对象缺少 } 的问题。
        在 ] 前如果 brace_depth > 0，说明有未闭合的 {，补上缺失的 }。"""
        result = []
        i = 0
        n = len(text)
        in_string = False
        escape_next = False
        brace_depth = 0

        while i < n:
            ch = text[i]
            if escape_next:
                escape_next = False
                result.append(ch)
                i += 1
                continue
            if ch == '\\' and in_string:
                escape_next = True
                result.append(ch)
                i += 1
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                result.append(ch)
                i += 1
                continue
            if in_string:
                result.append(ch)
                i += 1
                continue
            if ch == '{':
                brace_depth += 1
                result.append(ch)
            elif ch == '}':
                if brace_depth > 0:
                    brace_depth -= 1
                result.append(ch)
            elif ch == ']':
                j = len(result) - 1
                while j >= 0 and result[j] in ' \t\n\r,':
                    j -= 1
                if j >= 0 and result[j] not in ('}', ']', '[', 'null', 'true', 'false') and brace_depth > 0:
                    while brace_depth > 0:
                        result.append('}')
                        brace_depth -= 1
                result.append(ch)
            else:
                result.append(ch)
            i += 1

        while brace_depth > 0:
            result.append('}')
            brace_depth -= 1

        return ''.join(result)

    @staticmethod
    def _fix_trailing_commas(text: str) -> str:
        """修复尾部逗号：,] → ]  ,} → }"""
        result = []
        in_string = False
        escape_next = False
        n = len(text)

        for i, ch in enumerate(text):
            if escape_next:
                escape_next = False
                result.append(ch)
                continue
            if ch == '\\' and in_string:
                escape_next = True
                result.append(ch)
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                continue
            if in_string:
                result.append(ch)
                continue
            if ch == ',':
                j = i + 1
                while j < n and text[j] in ' \t\n\r':
                    j += 1
                if j < n and text[j] in (']', '}'):
                    continue
            result.append(ch)

        return ''.join(result)

    @staticmethod
    def _merge_repaired(repaired):
        """json_repair 返回 list 时合并为 dict（常见于缺 } 导致顶层拆分）。"""
        if isinstance(repaired, list) and repaired and isinstance(repaired[0], dict):
            merged = repaired[0]
            for extra in repaired[1:]:
                if isinstance(extra, dict):
                    for k, v in extra.items():
                        if k not in merged:
                            merged[k] = v
            return merged
        return repaired

    @staticmethod
    def _fix_tool_calls_list(tool_calls: list) -> list:
        fixed = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            args_val = tc.get("function", {}).get("arguments", "")
            if isinstance(args_val, dict):
                args_val = json.dumps(args_val, ensure_ascii=False)
            elif isinstance(args_val, str) and args_val.strip().startswith("{"):
                try:
                    repaired_dict = json.loads(args_val)
                    args_val = json.dumps(repaired_dict, ensure_ascii=False)
                except json.JSONDecodeError:
                    try:
                        import json_repair
                        repaired_dict = json_repair.loads(args_val)
                        args_val = json.dumps(repaired_dict, ensure_ascii=False)
                    except Exception:
                        pass
            tc_copy = dict(tc)
            if "function" in tc_copy and isinstance(tc_copy["function"], dict):
                tc_copy["function"] = dict(tc_copy["function"])
                tc_copy["function"]["arguments"] = args_val
            fixed.append(tc_copy)
        return fixed


_default_fixer = None


def fix_llm_json(raw_json_str: str) -> dict:
    global _default_fixer
    if _default_fixer is None:
        _default_fixer = JsonFixer()
    return _default_fixer.fix(raw_json_str)


def fix_llm_arguments(raw_json_str: str) -> str:
    global _default_fixer
    if _default_fixer is None:
        _default_fixer = JsonFixer()
    return _default_fixer.fix_arguments(raw_json_str)


def fix_llm_tool_calls(raw_json_str: str) -> Optional[list]:
    global _default_fixer
    if _default_fixer is None:
        _default_fixer = JsonFixer()
    return _default_fixer.fix_tool_calls(raw_json_str)
