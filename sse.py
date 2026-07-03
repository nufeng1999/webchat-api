import json
import uuid
import time
import logging
from typing import Union

from models import ChatMessage, MODEL_CONFIG, SYSTEM_PROMPT_MAP
from config import CONFIG, USER_AGENT, get_webchat_task

logger = logging.getLogger("webchat-api")


def generate_x_flow_trace():
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex
    return json.dumps({"trace_id": trace_id, "span_id": span_id})


def build_url_params(account: dict):
    return "&".join([
        "aid=497858",
        f"device_id={account.get('device_id', '')}",
        "device_platform=web",
        "language=zh",
        "pc_version=3.17.3",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={account.get('tea_uuid', '')}",
        "use-olympus-account=1",
        "version_code=20800",
        f"web_id={account.get('web_id', '')}"
    ])


def build_headers(account: dict):
    return {
        'content-type': 'application/json',
        'accept': 'text/event-stream',
        'agw-js-conv': 'str',
        'cookie': account.get('cookie', ''),
        'origin': 'https://www.doubao.com',
        'referer': 'https://www.doubao.com/chat/',
        'user-agent': USER_AGENT,
        'x-flow-trace': generate_x_flow_trace()
    }


def extract_text_from_content(content: Union[str, list]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
        return "\n".join(texts)
    return str(content)


def extract_image_urls_from_content(content: Union[str, list]) -> list[str]:
    if isinstance(content, str):
        return []
    if isinstance(content, list):
        urls = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                url = item.get("image_url", {}).get("url", "")
                if url:
                    urls.append(url)
        return urls
    return []


def build_request_body(messages: list[ChatMessage], conversation_id: str = "0",
                       model: str = "doubao-pro-chat", attachments: list[dict] = None):
    last_msg = messages[-1] if messages else None
    webchat_task = get_webchat_task()

    need_create = conversation_id == "0"

    model_cfg = MODEL_CONFIG.get(model, MODEL_CONFIG["doubao-pro-chat"])
    bot_id = model_cfg.get("bot_id", "7338286299411103781")
    use_deep_think = model_cfg.get("use_deep_think", False)
    use_auto_cot = model_cfg.get("use_auto_cot", False)
    use_search = model_cfg.get("use_search", False)

    system_prompt = SYSTEM_PROMPT_MAP.get(model, "")

    body_messages = []
    for msg in messages:
        text = extract_text_from_content(msg.content)
        if msg is last_msg and msg.role == "user" and webchat_task:
            text = f"{text}\n\n{webchat_task}"

        msg_attachments = []

        if isinstance(msg.content, list):
            img_urls = extract_image_urls_from_content(msg.content)
            if img_urls and msg == last_msg and attachments:
                msg_attachments = attachments

        body_messages.append({
            "content": json.dumps({"text": text}, ensure_ascii=False),
            "content_type": 2001,
            "attachments": msg_attachments,
            "references": []
        })

    if system_prompt and need_create:
        body_messages.insert(0, {
            "content": json.dumps({"text": system_prompt}, ensure_ascii=False),
            "content_type": 2001,
            "attachments": [],
            "references": []
        })

    ext = {"fp": CONFIG.get('fp', '')}
    if use_deep_think:
        ext["use_deep_think"] = "1"
    if use_search:
        ext["use_search"] = "1"

    return {
        "bot_id": bot_id,
        "completion_option": {
            "is_regen": False,
            "with_suggest": False,
            "need_create_conversation": need_create,
            "launch_stage": 1,
            "use_auto_cot": use_auto_cot,
            "use_deep_think": use_deep_think
        },
        "conversation_id": conversation_id,
        "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
        "local_message_id": str(uuid.uuid4()),
        "messages": body_messages[-1:] if need_create else body_messages,
        "ext": ext
    }


def parse_sse_line(line: str):
    if line.startswith('data:'):
        data_str = line[5:].strip()
        if data_str == '[DONE]':
            return None
        try:
            outer = json.loads(data_str)
            event_type = outer.get("event_type")
            raw_event_data = outer.get("event_data", "")
            if isinstance(raw_event_data, str) and raw_event_data:
                try:
                    inner = json.loads(raw_event_data)
                except json.JSONDecodeError:
                    inner = {}
            else:
                inner = raw_event_data if isinstance(raw_event_data, dict) else {}
            return {"event_type": event_type, "data": inner, "event_id": outer.get("event_id")}
        except json.JSONDecodeError:
            return None
    return None


def extract_text_from_event(parsed: dict) -> tuple[str, str]:
    if not parsed:
        return "", ""
    event_type = parsed.get("event_type")
    data = parsed.get("data", {})

    if event_type == 2001:
        message = data.get("message", {})
        content_type = message.get("content_type")
        if content_type in (10000, 2001, 2008, 2071):
            raw_content = message.get("content", "")
            thinking = ""
            if isinstance(raw_content, str) and raw_content:
                try:
                    content_parsed = json.loads(raw_content)
                    text = content_parsed.get("text", "")
                    thinking = content_parsed.get("thinking", "") or content_parsed.get("reasoning_content", "")
                    if content_type == 2008 and not thinking:
                        thinking = text
                        text = ""
                    return text, thinking
                except json.JSONDecodeError:
                    return raw_content, ""
            elif isinstance(raw_content, dict):
                text = raw_content.get("text", "")
                thinking = raw_content.get("thinking", "") or raw_content.get("reasoning_content", "")
                return text, thinking
    return "", ""


def extract_image_urls_from_event(parsed: dict) -> list[str]:
    if not parsed:
        return []
    event_type = parsed.get("event_type")
    data = parsed.get("data", {})

    if event_type == 2001:
        message = data.get("message", {})
        content_type = message.get("content_type")
        if content_type == 2074:
            raw_content = message.get("content", "")
            if isinstance(raw_content, str):
                try:
                    content_parsed = json.loads(raw_content)
                except json.JSONDecodeError:
                    return []
            elif isinstance(raw_content, dict):
                content_parsed = raw_content
            else:
                return []

            image_urls = []
            creations = content_parsed.get("creations", [])
            for creation in creations:
                image_info = creation.get("image", {})
                status = image_info.get("status")
                url = (image_info.get("image_raw", {}).get("url") or
                       image_info.get("image_thumb", {}).get("url") or
                       image_info.get("image_ori", {}).get("url") or
                       image_info.get("image_url", ""))
                if status == 2 and url and url not in image_urls:
                    image_urls.append(url)
            return image_urls
    return []


def extract_conversation_id(parsed: dict) -> str:
    data = parsed.get("data", {})
    return data.get("conversation_id", "")

def format_openai_chunk(content: str, model: str, chat_id: str, conversation_id: str = None,
                        reasoning_content: str = None, tool_calls: list = None,
                        role: str = None, finish_reason: str = None) -> str:
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if content == "":
        delta["content"] = None
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls

    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason
        }]
    }
    if reasoning_content:
        chunk["choices"][0]["delta"]["reasoning_content"] = reasoning_content
    if conversation_id and conversation_id != "0":
        chunk["conversation_id"] = conversation_id
    #logger.info(f"format_openai_chunk data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def format_openai_chunk1(content: str, model: str, chat_id: str, conversation_id: str = None, reasoning_content: str = None) -> str:
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": None
        }]
    }
    if reasoning_content:
        chunk["choices"][0]["delta"]["reasoning_content"] = reasoning_content
    if conversation_id and conversation_id != "0":
        chunk["conversation_id"] = conversation_id
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def format_openai_done() -> str:
    return "data: [DONE]\n\n"


def format_anthropic_sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def is_cookie_expired(status_code: int, body: str) -> bool:
    if status_code == 401 or status_code == 403:
        return True
    expired_keywords = ["login", "session expired", "unauthorized", "need_login", "csrf"]
    body_lower = body.lower()
    return any(kw in body_lower for kw in expired_keywords)


def build_browser_body(messages: list, conversation_id: str = "0",
                        model: str = "doubao-pro-chat", attachments: list = None,
                        doc_attachments: list = None):
    """构建新版豆包 /chat/completion 请求体（content_block 格式，配合浏览器代理）。
    
    doc_attachments: 文档附件列表（新版协议 type=3），挂载为独立 block_type 10052 块。"""
    import time
    
    last_msg = messages[-1] if messages else None
    webchat_task = get_webchat_task()

    model_cfg = MODEL_CONFIG.get(model, MODEL_CONFIG["doubao-pro-chat"])
    bot_id = model_cfg.get("bot_id", "7338286299411103781")
    use_deep_think = model_cfg.get("use_deep_think", False)

    need_create = (not conversation_id) or conversation_id == "0"

    if doc_attachments:
        text = extract_text_from_content(last_msg.content) if last_msg else ""
        user_request=""
        if last_msg and getattr(last_msg, "role", "") == "user" and webchat_task:
            user_request = f"{text}"
        text = f"{str(uuid.uuid4())}\n\n{webchat_task}\n{user_request}\n" if webchat_task else f"{str(uuid.uuid4())}\n\n\n{user_request}\n请阅读附件中的文件内容（包含完整对话历史），并根据文件后面的内容继续回复。"
    else:
        text = extract_text_from_content(last_msg.content) if last_msg else ""
        if last_msg and getattr(last_msg, "role", "") == "user" and webchat_task:
            text = f"{text}\n\n{webchat_task}"

    content_block = []
    
    # 文档附件块（block_type 10052）必须排在最前面
    if doc_attachments:
        attachment_block = {
            "block_type": 10052,
            "content": {
                "attachment_block": {"attachments": doc_attachments},
                "pc_event_block": ""
            },
            "block_id": str(uuid.uuid4()),
            "parent_id": "",
            "meta_info": [],
            "append_fields": []
        }
        content_block.append(attachment_block)
    
    # 文本块（block_type 10000）
    text_block = {
        "block_type": 10000,
        "content": {
            "text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""},
            "pc_event_block": "",
        },
        "block_id": str(uuid.uuid4()),
        "parent_id": "",
        "meta_info": [],
        "append_fields": [],
    }
    content_block.append(text_block)

    # 完全对齐抓包的 option 结构
    option = {
        "send_message_scene": "",
        "create_time_ms": int(time.time() * 1000),
        "collect_id": "",
        "is_audio": False,
        "answer_with_suggest": False,
        "tts_switch": False,
        "need_deep_think": 1 if use_deep_think else 0,
        "click_clear_context": False,
        "from_suggest": False,
        "is_regen": False,
        "is_replace": False,
        "is_from_click_option": False,
        "disable_sse_cache": False,
        "select_text_action": "",
        "is_select_text": False,
        "resend_for_regen": False,
        "scene_type": 0,
        "unique_key": str(uuid.uuid4()),
        "start_seq": 0,
        "need_create_conversation": need_create,
        "conversation_init_option": {"need_ack_conversation": True},
        "regen_query_id": [],
        "edit_query_id": [],
        "regen_instruction": "",
        "no_replace_for_regen": False,
        "message_from": 0,
        "shared_app_name": "",
        "shared_app_id": "",
        "sse_recv_event_options": {"support_chunk_delta": True},
        "is_ai_playground": False,
        "is_old_user": True,
        "recovery_option": {
            "is_recovery": False,
            "req_create_time_sec": int(time.time()),
            "append_sse_event_scene": 0
        },
        "message_storage_type": 0
    }
    
    # ext 字段对齐抓包
    ext = {
        "input_skill": '{"skill_id":"16","skill_type":16,"template_key":""}' if doc_attachments else "",
        "use_deep_think": "1" if use_deep_think else "0",
        "fp": CONFIG.get('fp', ''),
        "sub_conv_firstmet_type": "1",
        "collection_id": "",
        "conversation_init_option": '{"need_ack_conversation":true}',
        "commerce_credit_config_enable": "0"
    }

    body = {
        "client_meta": {
            "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
            "conversation_id": "" if need_create else conversation_id,
            "bot_id": bot_id,
            "last_section_id": "",
            "last_message_index": None,
        },
        "messages": [{
            "local_message_id": str(uuid.uuid4()),
            "content_block": content_block,
            "message_status": 0,
        }],
        "option": option,
        "chat_ability": {"ability_type": 16} if doc_attachments else {},
        "user_context": [],
        "ext": ext,
    }
    
    # 如果没有 doc_attachments，移除 chat_ability（空字典不需要）
    if not doc_attachments:
        body.pop("chat_ability", None)
    
    return body


def parse_browser_sse(text: str):
    """解析新版浏览器代理 SSE 文本，返回 (delta_text, conversation_id, finished)。
    文本增量来源：
    - STREAM_MSG_NOTIFY 首帧：data.content.content（JSON 字符串 {"text":...}）
    - STREAM_CHUNK 增量：patch_op 中 patch_object==102 的 patch_value.content（JSON 字符串 {"text":...}）
    - content_block 形态：patch_value.content_block[].content.text_block.text"""
    delta = ""
    conv_id = ""
    finished = False

    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type = ""
        data_str = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()

        if not data_str:
            continue

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if event_type == "SSE_ACK":
            ack = data.get("ack_client_meta", {})
            cid = ack.get("conversation_id", "")
            if cid and cid != "0":
                conv_id = cid
        elif event_type == "STREAM_MSG_NOTIFY":
            content_obj = data.get("content", {})
            # 解析嵌套字符串形态
            raw = content_obj.get("content", "")
            if raw:
                try:
                    delta += json.loads(raw).get("text", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            # 解析 content_block 形态（首段文本可能在这里）
            for cb in content_obj.get("content_block", []):
                if cb.get("block_type") == 10000:
                    t = cb.get("content", {}).get("text_block", {}).get("text", "")
                    if t:
                        delta += t
                elif cb.get("block_type") == 10052:
                    pass  # 附件块，忽略
            cid = data.get("meta", {}).get("conversation_id", "")
            if cid and cid != "0":
                conv_id = cid
        elif event_type == "STREAM_CHUNK":
            for op in data.get("patch_op", []):
                pv = op.get("patch_value", {})
                if op.get("patch_object") == 102:
                    content = pv.get("content", "")
                    if content:
                        try:
                            delta += json.loads(content).get("text", "")
                        except (json.JSONDecodeError, TypeError):
                            pass
                for cb in pv.get("content_block", []):
                    if cb.get("block_type") == 10000:
                        t = cb.get("content", {}).get("text_block", {}).get("text", "")
                        if t:
                            delta += t
        elif event_type == "CHUNK_DELTA":
            delta += data.get("text", "")
        elif event_type == "SSE_REPLY_END":
            finished = True

    return delta, conv_id, finished

