import json
import uuid
import logging
from typing import Union

from config import cookie_pool, save_conversation_log, save_conversation_state
from models import ChatMessage, ANTHROPIC_MODEL_MAP
from sse import (
    extract_text_from_content, extract_image_urls_from_content,
    parse_sse_line, extract_text_from_event, extract_image_urls_from_event,
    extract_conversation_id, format_anthropic_sse
)
from openai_api import call_doubao_api, upload_images_for_message

logger = logging.getLogger("webchat-api")


def anthropic_content_to_openai(content) -> tuple[str, list[str]]:
    text_parts = []
    image_urls = []
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        image_urls.append(f"data:{media_type};base64,{data}")
                    elif source.get("type") == "url":
                        image_urls.append(source.get("url", ""))
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url:
                        image_urls.append(url)
        return "\n".join(text_parts), image_urls
    return str(content), []


def anthropic_messages_to_openai(messages: list[dict], system_prompt=None) -> list[ChatMessage]:
    openai_messages = []
    if system_prompt:
        if isinstance(system_prompt, str):
            sys_text = system_prompt
        elif isinstance(system_prompt, list):
            sys_texts = []
            for block in system_prompt:
                if isinstance(block, dict) and block.get("type") == "text":
                    sys_texts.append(block.get("text", ""))
            sys_text = "\n".join(sys_texts)
        else:
            sys_text = str(system_prompt)
        if sys_text:
            openai_messages.append(ChatMessage(role="system", content=sys_text))

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text, image_urls = anthropic_content_to_openai(content)

        if role == "assistant":
            openai_messages.append(ChatMessage(role="assistant", content=text))
        else:
            if image_urls:
                content_list = [{"type": "text", "text": text}]
                for url in image_urls:
                    content_list.append({"type": "image_url", "image_url": {"url": url}})
                openai_messages.append(ChatMessage(role="user", content=content_list))
            else:
                openai_messages.append(ChatMessage(role="user", content=text))
    return openai_messages


def map_anthropic_model(model: str) -> str:
    return ANTHROPIC_MODEL_MAP.get(model, "doubao-pro-chat")


async def stream_anthropic_messages(request):
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    doubao_model = map_anthropic_model(request.model)
    openai_messages = anthropic_messages_to_openai(request.messages, request.system)
    full_text = ""
    all_image_urls = []
    conversation_id = "0"
    user_input = extract_text_from_content(openai_messages[-1].content) if openai_messages else ""
    buffer = ""
    input_tokens = sum(len(extract_text_from_content(m.content)) for m in openai_messages) // 4
    output_tokens = 0

    last_msg = openai_messages[-1] if openai_messages else None
    image_urls = extract_image_urls_from_content(last_msg.content) if last_msg and isinstance(last_msg.content, list) else []
    attachments = None

    if image_urls:
        try:
            account = cookie_pool.get_next()
            attachments = await upload_images_for_message(image_urls, account)
        except Exception as e:
            logger.error(f"Image upload failed: {e}")

    yield format_anthropic_sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": request.model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0}
        }
    })

    yield format_anthropic_sse("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""}
    })

    try:
        async for raw_chunk in call_doubao_api(openai_messages, conversation_id, doubao_model, attachments=attachments):
            try:
                buffer += raw_chunk.decode('utf-8', errors='replace')
            except:
                continue

            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.strip()
                if not line:
                    continue

                event_data = parse_sse_line(line)
                if event_data is None:
                    break

                extracted = extract_text_from_event(event_data)
                if extracted:
                    full_text += extracted
                    output_tokens += len(extracted) // 4
                    yield format_anthropic_sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": extracted}
                    })

                img_urls = extract_image_urls_from_event(event_data)
                if img_urls:
                    all_image_urls.extend(img_urls)
                    for img_url in img_urls:
                        img_markdown = f"\n![image]({img_url})\n"
                        full_text += img_markdown
                        yield format_anthropic_sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": img_markdown}
                        })

                conv_id = extract_conversation_id(event_data)
                if conv_id:
                    conversation_id = conv_id

                if event_data.get("event_type") == 2003:
                    break
    except Exception as e:
        logger.error(f"Anthropic stream error: {e}")
        if not full_text:
            full_text = f"[Error: {str(e)}]"
            yield format_anthropic_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": full_text}
            })

    yield format_anthropic_sse("content_block_stop", {
        "type": "content_block_stop",
        "index": 0
    })

    yield format_anthropic_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": max(output_tokens, 1)}
    })

    yield format_anthropic_sse("message_stop", {"type": "message_stop"})

    save_conversation_log(user_input, full_text, doubao_model, conversation_id, msg_id, all_image_urls)
    save_conversation_state(msg_id, openai_messages, conversation_id, doubao_model)


async def non_stream_anthropic_messages(request):
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    doubao_model = map_anthropic_model(request.model)
    openai_messages = anthropic_messages_to_openai(request.messages, request.system)
    full_text = ""
    all_image_urls = []
    conversation_id = "0"
    user_input = extract_text_from_content(openai_messages[-1].content) if openai_messages else ""
    buffer = ""
    input_tokens = sum(len(extract_text_from_content(m.content)) for m in openai_messages) // 4

    last_msg = openai_messages[-1] if openai_messages else None
    image_urls = extract_image_urls_from_content(last_msg.content) if last_msg and isinstance(last_msg.content, list) else []
    attachments = None

    if image_urls:
        account = cookie_pool.get_next()
        attachments = await upload_images_for_message(image_urls, account)

    async for raw_chunk in call_doubao_api(openai_messages, conversation_id, doubao_model, attachments=attachments):
        try:
            buffer += raw_chunk.decode('utf-8', errors='replace')
        except:
            continue

        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.strip()
            if not line:
                continue

            event_data = parse_sse_line(line)
            if event_data is None:
                break

            extracted = extract_text_from_event(event_data)
            if extracted:
                full_text += extracted

            img_urls = extract_image_urls_from_event(event_data)
            if img_urls:
                all_image_urls.extend(img_urls)
                for img_url in img_urls:
                    full_text += f"\n![image]({img_url})\n"

            conv_id = extract_conversation_id(event_data)
            if conv_id:
                conversation_id = conv_id

            if event_data.get("event_type") == 2003:
                break

    save_conversation_log(user_input, full_text, doubao_model, conversation_id, msg_id, all_image_urls)
    save_conversation_state(msg_id, openai_messages, conversation_id, doubao_model)

    content_blocks = [{"type": "text", "text": full_text}]
    for img_url in all_image_urls:
        content_blocks.append({"type": "text", "text": f"\n![image]({img_url})\n"})

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": request.model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": max(len(full_text) // 4, 1)
        }
    }
