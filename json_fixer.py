"""
JSON 矫正器：修复 LLM 输出的破损 JSON，特别是 arguments 字段未正确转义为字符串的问题。

用法:
    from json_fixer import JsonFixer

    fixer = JsonFixer()
    result = fixer.fix(raw_json_str)
    result = fixer.fix_arguments(raw_json_str)

支持的修复场景:
    1. arguments 字段包含未转义的裸 JSON 对象
    2. 多层嵌套的 arguments 修复
    3. 不完整的 JSON 截断修复
    4. markdown 代码块包裹的 JSON
"""
import json
import re
import logging
from typing import Optional

logger = logging.getLogger("json-fixer")


class JsonFixer:
    """JSON 矫正器：修复 LLM 输出的破损 JSON。"""

    # 匹配 "arguments": 后面的 { ... } 块（支持跨行），直到遇到外层的 } 或 ,
    # 注意：贪婪匹配，但受限于 (?=\n\s*\}|\n\s*,) 前瞻断言
    _ARGUMENTS_PATTERN = re.compile(
        r'("arguments"\s*:\s*)(\{[\s\S]*?\})\s*(?=\n\s*\}|\n\s*,)',
        re.MULTILINE
    )

    # 匹配 markdown 代码块
    _CODE_BLOCK_PATTERN = re.compile(r'```(?:json)?\s*\n(.*?)\n\s*```', re.DOTALL)

    def fix(self, raw_json_str: str) -> dict:
        """
        主入口：修复 LLM 输出的破损 JSON，返回解析后的 dict。
        自动尝试多种修复策略，按优先级排列：
            1. 直接解析
            2. 去除 markdown 代码块
            3. 修复 arguments 裸对象
            4. 尝试 json_repair 库
            5. 括号不平衡修复
        """
        if not raw_json_str or not raw_json_str.strip():
            raise ValueError("输入为空，无法解析")

        text = raw_json_str.strip()

        # 1. 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2. 去除 markdown 代码块
        cleaned = self._strip_code_block(text)
        if cleaned != text:
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                text = cleaned

        # 3. 修复 arguments 裸对象
        fixed_arguments = self._fix_arguments(text)
        try:
            return json.loads(fixed_arguments)
        except json.JSONDecodeError:
            pass

        # 4. 修复转义层级问题
        fixed_escape = self._fix_escape_levels(fixed_arguments)
        try:
            return json.loads(fixed_escape, strict=False)
        except json.JSONDecodeError:
            pass

        # 5. 括号不平衡修复（多余的 }）
        fixed_braces = self._fix_unbalanced_braces(fixed_escape)
        try:
            return json.loads(fixed_braces, strict=False)
        except json.JSONDecodeError:
            pass

        # 6. 尝试 json_repair 库（作为最后手段）
        try:
            import json_repair
            repaired = json_repair.loads(fixed_braces)
            if isinstance(repaired, dict):
                return repaired
        except ImportError:
            logger.debug("json_repair 库未安装，跳过")
        except Exception as e:
            logger.debug(f"json_repair 修复失败: {e}")

        # 所有策略失败
        try:
            final_err = json.loads(fixed_braces, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"JSON 矫正失败 | pos={e.pos} | msg={e.msg}\n"
                f"修复后内容片段: {fixed_braces[max(0, e.pos-50):e.pos+50]}"
            ) from e
        return final_err

    def fix_arguments(self, raw_json_str: str) -> str:
        """
        仅修复 arguments 字段，返回修复后的 JSON 字符串（未解析）。
        适用于需要先修复再进一步处理的场景。
        """
        if not raw_json_str:
            return raw_json_str

        text = raw_json_str.strip()

        # 1. 直接解析成功则原样返回
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

        # 2. 去除 markdown 代码块
        text = self._strip_code_block(text)

        # 3. 修复 arguments 裸对象
        fixed = self._fix_arguments(text)

        # 4. 修复转义层级
        fixed = self._fix_escape_levels(fixed)

        return fixed

    def fix_tool_calls(self, raw_json_str: str) -> Optional[list]:
        """
        从 JSON 中提取并修复 tool_calls 数组。
        返回修复后的 tool_calls list，失败返回 None。
        """
        try:
            result = self.fix(raw_json_str)
        except ValueError:
            return None

        if not isinstance(result, dict):
            return None

        # 从 OpenAI chunk 格式提取
        choices = result.get("choices", [])
        if choices and isinstance(choices, list):
            choice = choices[0]
            delta = choice.get("delta") or choice.get("message", {})
            if isinstance(delta, dict):
                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    return self._fix_tool_calls_list(tool_calls)

        # 直接 tool_calls 字段
        tool_calls = result.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            return self._fix_tool_calls_list(tool_calls)

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _strip_code_block(text: str) -> str:
        """去除 markdown 代码块包裹。"""
        if not text.startswith("```"):
            return text
        m = JsonFixer._CODE_BLOCK_PATTERN.search(text)
        if m:
            return m.group(1).strip()
        return text

    @staticmethod
    def _fix_arguments(text: str) -> str:
        """
        核心修复：将 arguments 后面非法的裸 JSON 对象提取出来，转为合法的 JSON 字符串。
        匹配 "arguments": 后面的 { ... } 块（支持跨行），直到遇到外层的 } 或 ,
        """

        def _fix_one(match):
            prefix = match.group(1)       # "arguments":
            raw_value = match.group(2)    # 未转义的裸 JSON 对象内容

            # 尝试将裸对象当作合法 JSON 解析
            try:
                parsed_obj = json.loads(raw_value)
                # 解析成功后，用 json.dumps 重新序列化为带正确转义的字符串
                fixed_value = json.dumps(parsed_obj, ensure_ascii=False)
            except json.JSONDecodeError:
                # 如果裸对象本身也破损（如截断），强制转义内部双引号并包裹为字符串
                escaped = raw_value.replace('\\', '\\\\').replace('"', '\\"')
                fixed_value = f'"{escaped}"'

            return prefix + fixed_value

        return JsonFixer._ARGUMENTS_PATTERN.sub(_fix_one, text)

    @staticmethod
    def _fix_escape_levels(text: str) -> str:
        """修复过度转义的字符串（如 \\\\\" 应为 \\\"）。"""
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
        """修复多余的右括号（常见于 JSON 截断）。"""
        brace_diff = text.count("}") - text.count("{")
        if brace_diff <= 0:
            return text

        stripped = text.rstrip()
        while stripped.endswith("}") and stripped.count("}") > stripped.count("{"):
            stripped = stripped[:-1].rstrip()

        return stripped

    @staticmethod
    def _fix_tool_calls_list(tool_calls: list) -> list:
        """修复 tool_calls 列表中每个条目的 arguments 字段。"""
        fixed = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue

            args_val = tc.get("function", {}).get("arguments", "")

            if isinstance(args_val, str) and args_val.strip().startswith("{"):
                try:
                    repaired_dict = json.loads(args_val)
                    args_val = json.dumps(repaired_dict, ensure_ascii=False)
                except json.JSONDecodeError:
                    # 尝试修复裸对象
                    try:
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


# ═══════════════════════════════════════════════════════════════════════
# 便捷函数（向后兼容）
# ═══════════════════════════════════════════════════════════════════════

_default_fixer = None


def fix_llm_json(raw_json_str: str) -> dict:
    """
    修复 LLM 输出的破损 JSON，特别是 arguments 字段未正确转义为字符串的问题。
    这是 JsonFixer.fix() 的便捷函数版本。
    """
    global _default_fixer
    if _default_fixer is None:
        _default_fixer = JsonFixer()
    return _default_fixer.fix(raw_json_str)


def fix_llm_arguments(raw_json_str: str) -> str:
    """仅修复 arguments 字段，返回 JSON 字符串。"""
    global _default_fixer
    if _default_fixer is None:
        _default_fixer = JsonFixer()
    return _default_fixer.fix_arguments(raw_json_str)


def fix_llm_tool_calls(raw_json_str: str) -> Optional[list]:
    """提取并修复 tool_calls 数组。"""
    global _default_fixer
    if _default_fixer is None:
        _default_fixer = JsonFixer()
    return _default_fixer.fix_tool_calls(raw_json_str)
