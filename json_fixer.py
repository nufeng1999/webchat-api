"""
JSON 矫正器：修复 LLM 输出的破损 JSON，特别是 arguments 字段未正确转义为字符串的问题。

用法:
    from json_fixer import JsonFixer

    fixer = JsonFixer()
    result = fixer.fix(raw_json_str)
    result = fixer.fix_tool_calls(raw_json_str)

支持的修复场景:
    0. 首尾空白、字面量 \\n
    1. markdown 代码块包裹
    2. arguments 裸对象（未转义的 JSON 对象）
    3. arguments 字符串内过转义（\\\\\\" → \\\\\\" → \\" → \"）
    4. 全局转义层级修复（四级→二级→一级）
    5. 单引号→双引号
    6. 未加引号的键名
    7. 括号不平衡（多余的 } 或缺 }）
    8. 数组内对象缺少 }
    9. 尾部逗号（,] 或 ,}）
    10. 顶层非 dict 时从 json_repair 的 list 结果中合并提取
    11. json_repair 兜底
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
        多种修复策略逐级尝试，只要某一步成功即返回。
        """
        logger.debug(f"开始 JSON 矫正\n{raw_json_str}")
        if not raw_json_str or not raw_json_str.strip():
            raise ValueError("输入为空，无法解析")

        text = raw_json_str

        # ── Step 0: 首尾清理 ──
        text = text.strip()
        if text.startswith('\\n'):
            text = text[2:].lstrip()
        if text.endswith('\\n'):
            text = text[:-2].rstrip()

        # ── Step 1: 过转义修复（最关键的 LLM 专属问题）──
        text = self._fix_over_escape(text)

        # ── Step 2: 直接解析 ──
        try:
            return self._normalize_arguments(json.loads(text))
        except json.JSONDecodeError:
            pass

        # ── Step 3: 去除 markdown 代码块 ──
        cleaned = self._strip_code_block(text)
        if cleaned != text:
            try:
                return self._normalize_arguments(json.loads(cleaned))
            except json.JSONDecodeError:
                text = cleaned

        # ── Step 4: 修复 arguments 裸/半裸对象 ──
        fixed = self._fix_bare_arguments(text)
        if fixed != text:
            try:
                return self._normalize_arguments(json.loads(fixed))
            except json.JSONDecodeError:
                text = fixed

        # ── Step 5: 转义层级迭代修复 ──
        fixed_esc = self._fix_escape_levels(text)
        try:
            return self._normalize_arguments(json.loads(fixed_esc, strict=False))
        except json.JSONDecodeError:
            pass

        # ── Step 6: 单引号→双引号 ──
        fixed_sq = self._fix_single_quotes(fixed_esc)
        if fixed_sq != fixed_esc:
            try:
                return self._normalize_arguments(json.loads(fixed_sq, strict=False))
            except json.JSONDecodeError:
                pass

        # ── Step 7: 未加引号的键名 ──
        fixed_uq = self._fix_unquoted_keys(fixed_sq)
        if fixed_uq != fixed_sq:
            try:
                return self._normalize_arguments(json.loads(fixed_uq, strict=False))
            except json.JSONDecodeError:
                pass

        # ── Step 8: 尾部逗号 ──
        fixed_tc = self._fix_trailing_commas(fixed_uq)
        try:
            return self._normalize_arguments(json.loads(fixed_tc, strict=False))
        except json.JSONDecodeError:
            pass

        # ── Step 9: 括号不平衡（多余 }）──
        fixed_ub = self._fix_unbalanced_braces(fixed_tc)
        try:
            return self._normalize_arguments(json.loads(fixed_ub, strict=False))
        except json.JSONDecodeError:
            pass

        # ── Step 9: 数组内对象缺 } ──
        fixed_mb = self._fix_missing_closing_braces(fixed_ub)
        try:
            return self._normalize_arguments(json.loads(fixed_mb, strict=False))
        except json.JSONDecodeError:
            pass

        # ── Step 10: json_repair 兜底 ──
        try:
            import json_repair
            repaired = json_repair.loads(fixed_mb)
            logger.debug(f"json_repair修复的\n{repaired}")
            repaired = self._merge_repaired(repaired)
            if isinstance(repaired, dict):
                return self._normalize_arguments(repaired)
        except ImportError:
            logger.debug("json_repair 库未安装，跳过")
        except Exception as e:
            logger.debug(f"json_repair 修复失败: {e}")

        # ── Step 11: 最终失败 ──
        try:
            return self._normalize_arguments(json.loads(fixed_mb, strict=False))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"JSON 矫正失败 | pos={e.pos} | msg={e.msg}\n"
                f"修复后内容片段: {fixed_mb[max(0, e.pos-50):e.pos+50]}"
            ) from e

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
                if key == "arguments":
                    if isinstance(val, dict):
                        data[key] = json.dumps(val, ensure_ascii=False)
                    elif isinstance(val, str) and val.strip().startswith("{"):
                        data[key] = JsonFixer._repair_arguments_inner(val)
                elif isinstance(val, (dict, list)):
                    JsonFixer._normalize_arguments(val)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    JsonFixer._normalize_arguments(item)
        return data

    @staticmethod
    def _repair_arguments_inner(val: str) -> str:
        """修复 arguments 值的内部 JSON：去除尾部垃圾、补齐缺失括号。"""
        try:
            json.loads(val)
            return val
        except json.JSONDecodeError:
            pass
        try:
            json.loads(val, strict=False)
            return val
        except json.JSONDecodeError:
            pass
        # 尝试 raw_decode 提取有效 JSON（去除尾部垃圾如 }]}}],）
        try:
            decoder = json.JSONDecoder()
            obj, end = decoder.raw_decode(val)
            repaired = val[:end].strip()
            try:
                json.loads(repaired)
                return json.dumps(obj, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        except json.JSONDecodeError:
            pass
        # 补齐缺失的 }
        try:
            stripped = val.rstrip()
            open_count = stripped.count("{")
            close_count = stripped.count("}")
            diff = open_count - close_count
            if diff > 0:
                repaired = stripped + "}" * diff
                if json.loads(repaired):
                    return json.dumps(json.loads(repaired), ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        # json_repair 兜底
        try:
            import json_repair
            repaired = json_repair.loads(val)
            return json.dumps(repaired, ensure_ascii=False)
        except Exception:
            pass
        return val

    @staticmethod
    def _strip_code_block(text: str) -> str:
        if not text.startswith("```"):
            return text
        m = JsonFixer._CODE_BLOCK_PATTERN.search(text)
        if m:
            return m.group(1).strip()
        return text

    @staticmethod
    def _fix_over_escape(text: str) -> str:
        """修复 arguments 值内的过转义问题。
        LLM 常把 arguments 写成: "arguments":"{\\"description\\":\\"...\\",...}"
        其中 \\" 在 JSON 解析时会被当作: 一个反斜杠 + 一个未转义的引号 → 字符串提前结束。
        本方法只修复 arguments 字符串值内部的 \\" → \"，不影响外层 JSON 结构。"""
        result = text
        search_start = 0
        while True:
            idx = result.find('"arguments"', search_start)
            if idx == -1:
                break
            colon_pos = result.find(':', idx + len('"arguments"'))
            if colon_pos == -1:
                search_start = idx + 1
                continue
            after_colon = colon_pos + 1
            while after_colon < len(result) and result[after_colon] in ' \t\n\r':
                after_colon += 1
            if after_colon >= len(result) or result[after_colon] != '"':
                search_start = idx + 1
                continue
            quote_start = after_colon

            # 判断是否是对象值（以 { 开头），决定扫描策略
            content_starts_with_brace = (
                quote_start + 1 < len(result) and result[quote_start + 1] == '{'
            )

            if content_starts_with_brace:
                # ── 花括号感知扫描 ──
                # LLM 常输出 arguments 值内部有过转义（\\"）或未转义的引号，
                # 导致简单扫描提前终止。通过跟踪 {/} 嵌套深度，只接受
                # depth == 0 时的引号为真正的字符串终结符。
                # 同时在扫描过程中构建修正后的内容：
                #   - \\"（过转义）→ \"
                #   - 裸 " 位于 depth>0 → \"（补齐转义）
                fixed_parts = []
                brace_depth = 0
                i = quote_start + 1
                while i < len(result):
                    ch = result[i]
                    if ch == '\\':
                        if i + 1 < len(result) and result[i + 1] == '\\':
                            # \\ 对 — 可能是 \\" 过转义
                            if i + 2 < len(result) and result[i + 2] == '"' and brace_depth > 0:
                                fixed_parts.append('\\"')
                                i += 3
                                continue
                            else:
                                fixed_parts.append('\\\\')
                                i += 2
                                continue
                        else:
                            fixed_parts.append(result[i:i + 2])
                            i += 2
                            continue
                    if ch == '"':
                        if brace_depth == 0:
                            break
                        else:
                            fixed_parts.append('\\"')
                            i += 1
                            continue
                    if ch == '{':
                        brace_depth += 1
                    elif ch == '}':
                        if brace_depth > 0:
                            brace_depth -= 1
                    fixed_parts.append(ch)
                    i += 1

                if i >= len(result):
                    search_start = idx + 1
                    continue
                quote_end = i
                fixed_inner = ''.join(fixed_parts)
                fixed_inner = JsonFixer._fix_over_escape_inner(fixed_inner)
            else:
                # ── 简单扫描（非对象值，如纯字符串）──
                i = quote_start + 1
                while i < len(result):
                    ch = result[i]
                    if ch == '\\':
                        i += 2
                        continue
                    if ch == '"':
                        break
                    i += 1
                if i >= len(result):
                    search_start = idx + 1
                    continue
                quote_end = i
                inner = result[quote_start + 1:quote_end]
                fixed_inner = JsonFixer._fix_over_escape_inner(inner)

            if fixed_inner != result[quote_start + 1:quote_end]:
                result = result[:quote_start + 1] + fixed_inner + result[quote_end:]
                search_start = quote_start + 1 + len(fixed_inner)
                continue
            search_start = quote_end + 1
        return result

    @staticmethod
    def _fix_over_escape_inner(s: str) -> str:
        """修复字符串内部的过转义。
        输入是 arguments 的字符串值内容（不含外层引号）。
        处理: \\" → \" (修复过度转义的双引号)
              \' → '   (单引号在 JSON 字符串中不需要转义)
        保留: \n, \t, \\ 等合法转义。
        """
        result = []
        i = 0
        n = len(s)
        while i < n:
            if i + 3 < n and s[i] == '\\' and s[i + 1] == '\\' and s[i + 2] == '\\' and s[i + 3] == '"':
                # \\\" → \"  (三级转义→一级)
                result.append('\\"')
                i += 4
            elif i + 2 < n and s[i] == '\\' and s[i + 1] == '\\' and s[i + 2] == '"':
                # \\" → \"  (二级转义→一级)
                result.append('\\"')
                i += 3
            elif i + 1 < n and s[i] == '\\' and s[i + 1] == '"':
                # 已经是正确的 \"
                result.append('\\"')
                i += 2
            elif i + 1 < n and s[i] == '\\' and s[i + 1] == "'":
                # \' → '  (单引号在 JSON 字符串中非法转义，移除反斜杠)
                result.append("'")
                i += 2
            elif i + 1 < n and s[i] == '\\' and s[i + 1] in 'ntrb/f\\':
                # 合法的转义序列 \n, \t, \r, \b, \f, \\
                result.append(s[i])
                result.append(s[i + 1])
                i += 2
            else:
                result.append(s[i])
                i += 1
        return ''.join(result)

    @staticmethod
    def _fix_single_quotes(text: str) -> str:
        """修复单引号字符串 → 双引号字符串。
        仅修复外层 JSON 结构中的单引号键和值。"""
        result = []
        i = 0
        n = len(text)
        in_single = False
        while i < n:
            ch = text[i]
            if ch == "'" and not in_single:
                in_single = True
                result.append('"')
                i += 1
                continue
            if ch == "'" and in_single:
                in_single = False
                result.append('"')
                i += 1
                continue
            if in_single:
                if ch == '\\' and i + 1 < n:
                    # 单引号字符串内的转义
                    result.append('\\')
                    result.append(text[i + 1])
                    i += 2
                    continue
                result.append(ch)
                i += 1
                continue
            result.append(ch)
            i += 1
        return ''.join(result)

    @staticmethod
    def _fix_unquoted_keys(text: str) -> str:
        """修复未加引号的键名，如 {a:1} → {"a":1}。
        使用简单的正则：在 { 或 , 后面的未加引号标识符。"""
        # 先处理 { 后的键
        text = re.sub(r'([{,]\s*)([a-zA-Z_]\w*)(\s*:)', r'\1"\2"\3', text)
        return text

    @staticmethod
    def _fix_escape_levels(text: str) -> str:
        """迭代修复转义层级：\\\\\\\\ → \\\\ → \\。
        每次尝试解析，成功即返回。"""
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
        """找到与 start 位置 { 匹配的 }。正确处理 JSON 字符串中的转义。
        \\（双反斜杠）→ 字面量反斜杠对，不影响字符串状态。
        \"（反斜杠+引号）→ 转义引号，不切换 in_string。"""
        if start >= len(text) or text[start] != '{':
            return 0
        depth = 0
        in_string = False
        i = start
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == '\\':
                # 看下一个字符
                if i + 1 < n:
                    next_ch = text[i + 1]
                    if next_ch == '\\':
                        # 双反斜杠 → 字面量，跳过两个字符
                        i += 2
                        continue
                    else:
                        # 转义序列（\"、\n、\t 等）→ 跳过两个字符
                        i += 2
                        continue
                else:
                    i += 1
                    continue
            if ch == '"':
                in_string = not in_string
                i += 1
                continue
            if in_string:
                i += 1
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return 0

    @staticmethod
    def _escape_for_string(raw: str) -> str:
        escaped = raw.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'

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
