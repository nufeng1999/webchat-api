import json
import uuid
import time
import logging
from typing import Union

from models import ChatMessage, MODEL_CONFIG, SYSTEM_PROMPT_MAP
from config import CONFIG, USER_AGENT

logger = logging.getLogger("doubao-api")


def generate_x_flow_trace():
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex
    return json.dumps({"trace_id": trace_id, "span_id": span_id})


def build_url_params(account: dict):
    return "&".join([
        "aid=497858",
        f"device_id={account.get('device_id', CONFIG.get('device_id', ''))}",
        "device_platform=web",
        "language=zh",
        "pc_version=3.17.3",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={account.get('tea_uuid', CONFIG.get('tea_uuid', ''))}",
        "use-olympus-account=1",
        "version_code=20800",
        f"web_id={account.get('web_id', CONFIG.get('web_id', ''))}"
    ])


def build_headers(account: dict):
    return {
        'content-type': 'application/json',
        'accept': 'text/event-stream',
        'agw-js-conv': 'str',
        'cookie': account.get('cookie', CONFIG.get('cookie', '')),
        'origin': 'https://www.doubao.com',
        'referer': f"https://www.doubao.com/chat/{account.get('room_id', CONFIG.get('room_id', ''))}",
        'user-agent': USER_AGENT,
        'x-flow-trace': generate_x_flow_trace()
    }


def extract_text_from_content(content: Union[str, list]) -> str:
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
    custom_prompt = CONFIG.get('custom_prompt', '')   # 新增

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
        # 新增：仅对最后一条用户消息，在末尾追加自定义提示词
        if msg is last_msg and msg.role == "user" and custom_prompt:
            text = f"{text}\n\n\{custom_prompt}"

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
    if tool_calls is not None:
        delta["role"] = "assistant"
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
    if tool_calls is not None:
        chunk["choices"][0]["finish_reason"] = "tool_calls"
    if reasoning_content:
        chunk["choices"][0]["delta"]["reasoning_content"] = reasoning_content
    if conversation_id and conversation_id != "0":
        chunk["conversation_id"] = conversation_id
    logger.info(f"流式应答内容: {json.dumps(chunk, ensure_ascii=False)}")
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
