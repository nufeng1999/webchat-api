import json
import uuid
import asyncio
import logging
from typing import AsyncGenerator, Union

import aiohttp
from urllib.parse import urlencode

from config import CONFIG, SIGN_METHOD, signer, cookie_pool, USER_AGENT
from models import ChatMessage, MODEL_CONFIG
from sse import (
    build_url_params, build_headers, build_request_body,
    extract_text_from_content, extract_image_urls_from_content,
    parse_sse_line, extract_text_from_event, extract_image_urls_from_event,
    extract_conversation_id,
    format_openai_chunk, format_openai_done
)
from config import save_conversation_log, save_conversation_state

logger = logging.getLogger("doubao-api")


async def upload_images_for_message(image_urls: list[str], account: dict) -> list[dict]:
    if not image_urls:
        return []

    from uploader import process_image_url
    attachments = []
    for url in image_urls:
        try:
            att = await process_image_url(
                image_url=url,
                cookie=account.get('cookie', CONFIG.get('cookie', '')),
                device_id=account.get('device_id', ''),
                tea_uuid=account.get('tea_uuid', ''),
                web_id=account.get('web_id', '')
            )
            attachments.append(att)
            logger.info(f"Image uploaded for chat: {att.get('key', '')}")
        except Exception as e:
            logger.error(f"Failed to upload image: {e}")
    return attachments


async def call_doubao_api(messages: list[ChatMessage], conversation_id: str = "0",
                          model: str = "doubao-pro-chat", max_retries: int = 2,
                          attachments: list[dict] = None) -> AsyncGenerator[bytes, None]:
    account = cookie_pool.get_next()
    body = build_request_body(messages, conversation_id, model, attachments)

    if SIGN_METHOD == 'b2' and signer and signer._initialized:
        base_params = {
            "aid": "497858",
            "device_platform": "web",
            "language": "zh",
            "pc_version": "3.17.3",
            "pkg_type": "release_version",
            "real_aid": "497858",
            "region": "CN",
            "samantha_web": "1",
            "sys_region": "CN",
            "use-olympus-account": "1",
            "version_code": "20800",
        }
        signed_url = await signer.get_signed_url(
            f"{CONFIG['api_base']}/samantha/chat/completion",
            base_params
        )
        if signed_url:
            url = signed_url
            logger.info(f"Using B2 signed URL with a_bogus")
        else:
            logger.warning("B2 signing failed, falling back to B3")
            params = build_url_params(account)
            url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"
    else:
        params = build_url_params(account)
        url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"

    headers = build_headers(account)

    logger.info(f"Calling Doubao API: conv_id={conversation_id}, account={account['name']}, method={SIGN_METHOD}, model={model}")

    for attempt in range(max_retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status != 200:
                        body_text = await resp.text()
                        logger.error(f"Doubao API returned {resp.status}: {body_text[:200]}")

                        if cookie_pool.is_cookie_expired(body_text, resp.status):
                            cookie_pool.report_fail(account, reason="cookie_expired")
                            cookie_pool.maybe_refresh()
                            if attempt < max_retries:
                                account = cookie_pool.get_next()
                                headers = build_headers(account)
                                continue

                        if attempt < max_retries:
                            account = cookie_pool.get_next()
                            headers = build_headers(account)
                            continue
                        yield json.dumps({"error": True, "status": resp.status, "body": body_text[:500]}).encode()
                        return

                    cookie_pool.report_success(account)

                    collected_chunks = []
                    async for chunk in resp.content.iter_any():
                        collected_chunks.append(chunk)
                        chunk_text = chunk.decode('utf-8', errors='replace')
                        if cookie_pool.is_cookie_expired(chunk_text, 200):
                            logger.warning(f"Cookie expired detected in SSE stream, aborting and retrying {chunk_text}")
                            cookie_pool.report_fail(account, reason="cookie_expired_in_stream")
                            cookie_pool.maybe_refresh()
                            if attempt < max_retries:
                                account = cookie_pool.get_next()
                                headers = build_headers(account)
                                break
                            for c in collected_chunks:
                                yield c
                            return
                    else:
                        for c in collected_chunks:
                            yield c
                        return
                    for c in collected_chunks:
                        yield c
                    continue
        except asyncio.TimeoutError:
            logger.error(f"Doubao API timeout (attempt {attempt + 1})")
            if attempt < max_retries:
                account = cookie_pool.get_next()
                headers = build_headers(account)
                continue
            yield json.dumps({"error": True, "status": 0, "body": "Request timeout after retries"}).encode()
            return
        except Exception as e:
            logger.error(f"Doubao API exception: {e}")
            if attempt < max_retries:
                account = cookie_pool.get_next()
                headers = build_headers(account)
                continue
            yield json.dumps({"error": True, "status": 0, "body": str(e)}).encode()
            return


async def stream_chat_completion(request):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    full_text = ""
    all_image_urls = []
    conversation_id = request.conversation_id or "0"
    user_input = extract_text_from_content(request.messages[-1].content) if request.messages else ""
    buffer = ""
    full_thinking = ""

    model_cfg = MODEL_CONFIG.get(request.model, {})
    is_image_model = model_cfg.get("is_image_model", False)
    is_podcast_model = model_cfg.get("is_podcast_model", False)
    is_music_model = model_cfg.get("is_music_model", False)

    if is_image_model:
        async for chunk in _stream_image_generation(user_input, request.model, chat_id):
            yield chunk
        return

    if is_podcast_model:
        async for chunk in _stream_podcast_generation(user_input, request.model, chat_id):
            yield chunk
        return

    if is_music_model:
        async for chunk in _stream_music_generation(user_input, request.model, chat_id):
            yield chunk
        return

    last_msg = request.messages[-1] if request.messages else None
    image_urls = extract_image_urls_from_content(last_msg.content) if last_msg and isinstance(last_msg.content, list) else []
    attachments = None

    if image_urls:
        try:
            account = cookie_pool.get_next()
            attachments = await upload_images_for_message(image_urls, account)
            if attachments:
                logger.info(f"Uploaded {len(attachments)} images for vision request")
        except Exception as e:
            logger.error(f"Image upload failed, continuing without images: {e}")

    try:
        async for raw_chunk in call_doubao_api(request.messages, conversation_id, request.model, attachments=attachments):
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
                    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
                    save_conversation_state(chat_id, request.messages, conversation_id, request.model)
                    yield format_openai_done()
                    return

                extracted, thinking = extract_text_from_event(event_data)
                if thinking:
                    full_thinking += thinking
                    yield format_openai_chunk("", request.model, chat_id, conversation_id, reasoning_content=thinking)
                if extracted:
                    full_text += extracted
                    yield format_openai_chunk(extracted, request.model, chat_id, conversation_id)

                img_urls = extract_image_urls_from_event(event_data)
                if img_urls:
                    all_image_urls.extend(img_urls)
                    for img_url in img_urls:
                        img_markdown = f"\n![image]({img_url})\n"
                        full_text += img_markdown
                        yield format_openai_chunk(img_markdown, request.model, chat_id, conversation_id)

                conv_id = extract_conversation_id(event_data)
                if conv_id:
                    conversation_id = conv_id

                if event_data.get("event_type") == 2003:
                    # 检测工具调用
                    try:
                        tool_call_data = json.loads(full_text) if isinstance(full_text, str) else full_text
                        is_tool_call = isinstance(tool_call_data, dict) and "tool_calls" in tool_call_data
                    except (json.JSONDecodeError, TypeError):
                        is_tool_call = False

                    if is_tool_call:
                        # logger.info(f"Stream detected tool_calls: {full_text[:200]}")
                        tool_calls = tool_call_data.get("tool_calls", [])
                        for i, tc in enumerate(tool_calls):
                            yield format_openai_chunk(
                                "", request.model, chat_id, conversation_id,
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
                            )
                            args = tc.get("function", {}).get("arguments", "")
                            if args:
                                yield format_openai_chunk(
                                    "", request.model, chat_id, conversation_id,
                                    tool_calls=[{"index": i, "function": {"arguments": args}}]
                                )
                        yield format_openai_chunk(None, request.model, chat_id, conversation_id, finish_reason="tool_calls")
                    else:
                        yield format_openai_chunk("", request.model, chat_id, conversation_id).replace(
                            '"finish_reason": null', '"finish_reason": "stop"')

                    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
                    save_conversation_state(chat_id, request.messages, conversation_id, request.model)
                    yield format_openai_done()
                    return
    except Exception as e:
        logger.error(f"Stream error: {e}")
        if not full_text:
            yield format_openai_chunk(f"[Error: {str(e)}]", request.model, chat_id, conversation_id)

        # 检测工具调用
        try:
            tool_call_data = json.loads(full_text) if isinstance(full_text, str) else full_text
            is_tool_call = isinstance(tool_call_data, dict) and "tool_calls" in tool_call_data
        except (json.JSONDecodeError, TypeError):
            is_tool_call = False

        if is_tool_call:
            # logger.info(f"Buffer completed with tool_calls: {full_text[:200]}")
            tool_calls = tool_call_data.get("tool_calls", [])
            for i, tc in enumerate(tool_calls):
                yield format_openai_chunk(
                    None, request.model, chat_id, conversation_id,
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
                )
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    yield format_openai_chunk(
                        None, request.model, chat_id, conversation_id,
                        tool_calls=[{"index": i, "function": {"arguments": args}}]
                    )
            yield format_openai_chunk(None, request.model, chat_id, conversation_id, finish_reason="tool_calls")
        else:
            # 现有逻辑：常规文本流式输出...（第247-253行的 yield 处理）
            if buffer.strip():
                line = buffer.strip()
                event_data = parse_sse_line(line)
                if event_data is not None:
                    extracted, thinking = extract_text_from_event(event_data)
                    if thinking:
                        full_thinking += thinking
                        yield format_openai_chunk(...) 
                    if extracted:
                        full_text += extracted
                        yield format_openai_chunk(...)
                    img_urls = extract_image_urls_from_event(event_data)
                    if img_urls:
                        all_image_urls.extend(img_urls)


    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
    save_conversation_state(chat_id, request.messages, conversation_id, request.model)
    yield format_openai_done()


async def non_stream_chat_completion(request):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    full_text = ""
    all_image_urls = []
    conversation_id = request.conversation_id or "0"
    user_input = extract_text_from_content(request.messages[-1].content) if request.messages else ""
    buffer = ""
    full_thinking = ""

    model_cfg = MODEL_CONFIG.get(request.model, {})
    is_image_model = model_cfg.get("is_image_model", False)
    is_podcast_model = model_cfg.get("is_podcast_model", False)
    is_music_model = model_cfg.get("is_music_model", False)

    if is_image_model:
        result = await generate_images(user_input)
        img_text = ""
        for img in result.get("data", []):
            url = img.get("url", "")
            error = img.get("error", "")
            if url:
                img_text += f"\n![image]({url})\n"
            elif error:
                img_text = f"⚠️ {error}"
        if not img_text:
            img_text = "图片生成失败，请稍后再试。"
        import time as _time
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(_time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": img_text},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    if is_podcast_model:
        from podcast import start_podcast_generation, get_podcast_status
        import time as _time
        pod_result = await start_podcast_generation(user_input, conversation_id)
        task_id = pod_result["task_id"]
        for _ in range(80):
            await asyncio.sleep(3)
            status = await get_podcast_status(task_id)
            if status["status"] in ("completed", "script_ready", "failed"):
                break
        pod_text = ""
        if status.get("audio_url"):
            dur_sec = status.get('duration', 0)
            dur_str = f"{int(dur_sec)//60}:{str(int(dur_sec)%60).zfill(2)}" if dur_sec else "--:--"
            pod_text = f"🎙️ AI播客已生成！\n\n**{status.get('title', user_input)}**\n时长：{dur_str}\n\n🔊 [收听播客]({status['audio_url']})"
        elif status.get("script_length", 0) > 0:
            from podcast import get_podcast_script
            script = await get_podcast_script(task_id)
            conv_id = status.get("conversation_id", "")
            doubao_link = f"\n\n💡 [在豆包网页版中生成音频](https://www.doubao.com/chat/{conv_id})" if conv_id else ""
            pod_text = f"🎙️ AI播客脚本已生成{doubao_link}\n\n{script.get('script', '')}"
        else:
            pod_text = f"播客生成失败: {status.get('error', '未知错误')}"
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(_time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": pod_text},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    if is_music_model:
        from music import start_music_generation, get_music_status
        import time as _time
        music_result = await start_music_generation(user_input, conversation_id)
        task_id = music_result["task_id"]
        for _ in range(60):
            await asyncio.sleep(3)
            status = await get_music_status(task_id)
            if status["status"] in ("completed", "lyrics_ready", "failed"):
                break
        music_text = ""
        if status.get("audio_url"):
            dur_sec = status.get('duration', 0)
            dur_str = f"{int(dur_sec)//60}:{str(int(dur_sec)%60).zfill(2)}" if dur_sec else "--:--"
            cover_md = f"\n![封面]({status['cover_url']})" if status.get("cover_url") else ""
            lyric_text = ""
            try:
                from music import get_music_lyric
                lyric_data = await get_music_lyric(task_id)
                lyric_raw = lyric_data.get('lyric', '')
                if lyric_raw:
                    lyric_lines = [l.strip() for l in lyric_raw.split('\n') if l.strip()]
                    lyric_text = '\n'.join(f'> {l}' for l in lyric_lines[:30])
            except:
                pass
            music_text = f"🎵 AI音乐已生成！{cover_md}\n\n标题：{status.get('title', user_input)}\n时长：{dur_str}\n\n🔊 [收听音乐]({status['audio_url']})\n\n🎶 **歌词：**\n{lyric_text}"
        elif status.get("lyrics_length", 0) > 0:
            from music import get_music_lyric
            lyric = await get_music_lyric(task_id)
            music_text = f"🎵 AI音乐歌词已生成（音频需在豆包客户端生成）\n\n{lyric.get('lyric', '')}"
        else:
            music_text = f"音乐生成失败: {status.get('error', '未知错误')}"
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(_time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": music_text},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    last_msg = request.messages[-1] if request.messages else None
    image_urls = extract_image_urls_from_content(last_msg.content) if last_msg and isinstance(last_msg.content, list) else []
    attachments = None

    if image_urls:
        account = cookie_pool.get_next()
        attachments = await upload_images_for_message(image_urls, account)
        if attachments:
            logger.info(f"Uploaded {len(attachments)} images for vision request")

    async for raw_chunk in call_doubao_api(request.messages, conversation_id, request.model, attachments=attachments):
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

            extracted, thinking = extract_text_from_event(event_data)
            if thinking:
                full_thinking += thinking
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

    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
    save_conversation_state(chat_id, request.messages, conversation_id, request.model)

    import time
    # 检测 full_text 是否为 tool_calls 格式的工具调用响应
    try:
        tool_call_data = json.loads(full_text) if isinstance(full_text, str) else full_text
        is_tool_call = isinstance(tool_call_data, dict) and "tool_calls" in tool_call_data
        if is_tool_call:
            # logger.info(f"Detected tool_calls response: {json.dumps(tool_call_data, ensure_ascii=False)[:200]}")
            tool_calls = tool_call_data.get("tool_calls", [])
    except (json.JSONDecodeError, TypeError):
        is_tool_call = False
    if is_tool_call:
        result = {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", 
                            "content":None,
                            "tool_calls":tool_calls
                            },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
    else:
        result = {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }

    if full_thinking:
        result["choices"][0]["message"]["reasoning_content"] = full_thinking

    if all_image_urls:
        result["images"] = all_image_urls
    return result


async def _stream_image_generation(prompt: str, model: str, chat_id: str):
    yield format_openai_chunk("🎨 正在生成图片...\n", model, chat_id)
    try:
        result = await generate_images(prompt)
        for img in result.get("data", []):
            if img.get("url"):
                img_markdown = f"\n![image]({img['url']})\n"
                yield format_openai_chunk(img_markdown, model, chat_id)
        if not any(img.get("url") for img in result.get("data", [])):
            yield format_openai_chunk("图片生成失败，请稍后再试。\n", model, chat_id)
    except Exception as e:
        yield format_openai_chunk(f"图片生成错误: {str(e)}\n", model, chat_id)
    yield format_openai_chunk("", model, chat_id).replace('"finish_reason": null', '"finish_reason": "stop"')
    yield format_openai_done()


async def _stream_podcast_generation(topic: str, model: str, chat_id: str):
    from podcast import start_podcast_generation, get_podcast_status, get_podcast_script
    yield format_openai_chunk("🎙️ 正在生成AI播客，请稍候...\n", model, chat_id)
    try:
        pod_result = await start_podcast_generation(topic)
        task_id = pod_result["task_id"]
        for i in range(80):
            await asyncio.sleep(3)
            status = await get_podcast_status(task_id)
            if status["status"] == "generating":
                if i % 5 == 0:
                    yield format_openai_chunk("⏳ 播客脚本生成中...\n", model, chat_id)
            elif status["status"] == "generating_audio":
                if i % 5 == 0:
                    yield format_openai_chunk("🎵 播客音频生成中...\n", model, chat_id)
            elif status["status"] == "completed":
                break
            elif status["status"] in ("script_ready", "failed"):
                break
        if status.get("audio_url"):
            dur_sec = status.get('duration', 0)
            dur_str = f"{int(dur_sec)//60}:{str(int(dur_sec)%60).zfill(2)}" if dur_sec else "--:--"
            yield format_openai_chunk(f"✅ AI播客已生成！\n\n**{status.get('title', topic)}**\n时长：{dur_str}\n\n🔊 [收听播客]({status['audio_url']})\n", model, chat_id)
        elif status.get("script_length", 0) > 0:
            script = await get_podcast_script(task_id)
            conv_id = status.get("conversation_id", "")
            doubao_link = f"\n\n💡 [在豆包网页版中生成音频](https://www.doubao.com/chat/{conv_id})" if conv_id else ""
            yield format_openai_chunk(f"📝 AI播客脚本已生成{doubao_link}\n\n{script.get('script', '')}\n", model, chat_id)
        else:
            yield format_openai_chunk(f"❌ 播客生成失败: {status.get('error', '未知错误')}\n", model, chat_id)
    except Exception as e:
        yield format_openai_chunk(f"❌ 播客生成错误: {str(e)}\n", model, chat_id)
    yield format_openai_chunk("", model, chat_id).replace('"finish_reason": null', '"finish_reason": "stop"')
    yield format_openai_done()


async def _stream_music_generation(prompt: str, model: str, chat_id: str):
    from music import start_music_generation, get_music_status, get_music_lyric
    yield format_openai_chunk("🎵 正在生成AI音乐，请稍候...\n", model, chat_id)
    try:
        music_result = await start_music_generation(prompt)
        task_id = music_result["task_id"]
        for i in range(120):
            await asyncio.sleep(3)
            status = await get_music_status(task_id)
            if status["status"] == "generating":
                if i % 10 == 0 and i > 0:
                    yield format_openai_chunk("⏳ 音乐生成中...\n", model, chat_id)
            elif status["status"] == "completed":
                break
            elif status["status"] in ("lyrics_ready", "failed"):
                break
        if status.get("audio_url"):
            cover_md = ""
            if status.get("cover_url"):
                cover_md = f"\n![封面]({status['cover_url']})"
            dur_sec = status.get('duration', 0)
            dur_str = f"{int(dur_sec)//60}:{str(int(dur_sec)%60).zfill(2)}" if dur_sec else "--:--"
            lyric_text = ""
            try:
                lyric_data = await get_music_lyric(task_id)
                lyric_raw = lyric_data.get('lyric', '')
                if lyric_raw:
                    lyric_lines = [l.strip() for l in lyric_raw.split('\n') if l.strip()]
                    lyric_text = '\n'.join(f'> {l}' for l in lyric_lines[:30])
            except:
                pass
            yield format_openai_chunk(
                f"✅ AI音乐已生成！{cover_md}\n\n"
                f"🎵 **{status.get('title', prompt)}**\n"
                f"⏱ 时长：{dur_str}\n\n"
                f"🔊 [点击收听音乐]({status['audio_url']})\n\n"
                f"🎶 **歌词：**\n{lyric_text}\n",
                model, chat_id
            )
        elif status.get("lyrics_length", 0) > 0:
            lyric = await get_music_lyric(task_id)
            yield format_openai_chunk(
                f"📝 AI音乐歌词已生成（音频仍在生成中）\n\n"
                f"🎵 **{status.get('title', prompt)}**\n\n"
                f"{lyric.get('lyric', '')}\n",
                model, chat_id
            )
        else:
            yield format_openai_chunk(f"❌ 音乐生成失败: {status.get('error', '未知错误')}\n", model, chat_id)
    except Exception as e:
        yield format_openai_chunk(f"❌ 音乐生成错误: {str(e)}\n", model, chat_id)
    yield format_openai_chunk("", model, chat_id).replace('"finish_reason": null', '"finish_reason": "stop"')
    yield format_openai_done()


async def generate_images(prompt: str, n: int = 1, size: str = "1024x1024"):
    from models import ChatMessage
    import time

    all_image_urls = []
    conversation_id = "0"

    img_prompt = f"请帮我画一张关于以下内容的图片：{prompt}。要求图片尺寸大约{size}。"
    messages = [ChatMessage(role="user", content=img_prompt)]
    buffer = ""
    event_count = 0
    raw_lines = []

    async for raw_chunk in call_doubao_api(messages, conversation_id, "doubao-pro-chat", max_retries=1):
        try:
            buffer += raw_chunk.decode('utf-8', errors='replace')
        except:
            continue

        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.strip()
            if not line:
                continue

            raw_lines.append(line)

            event_data = parse_sse_line(line)
            if event_data is None:
                break

            event_count += 1

            conv_id = extract_conversation_id(event_data)
            if conv_id:
                conversation_id = conv_id

            img_urls = extract_image_urls_from_event(event_data)
            if img_urls:
                all_image_urls.extend(img_urls)

            if event_data.get("event_type") == 2003:
                break

    logger.info(f"Image gen first pass: {event_count} events, {len(all_image_urls)} URLs, conv_id={conversation_id}")

    if not all_image_urls and conversation_id != "0":
        logger.warning("No images on first attempt, retrying in same conversation")
        retry_prompt = "请直接生成图片，不需要文字说明。"
        retry_messages = [
            ChatMessage(role="user", content=img_prompt),
            ChatMessage(role="assistant", content="好的，我来为您生成图片。"),
            ChatMessage(role="user", content=retry_prompt)
        ]
        retry_buffer = ""

        async for raw_chunk in call_doubao_api(retry_messages, conversation_id, "doubao-pro-chat", max_retries=1):
            try:
                retry_buffer += raw_chunk.decode('utf-8', errors='replace')
            except:
                continue

            while '\n' in retry_buffer:
                line, retry_buffer = retry_buffer.split('\n', 1)
                line = line.strip()
                if not line:
                    continue

                event_data = parse_sse_line(line)
                if event_data is None:
                    break

                img_urls = extract_image_urls_from_event(event_data)
                if img_urls:
                    all_image_urls.extend(img_urls)

                if event_data.get("event_type") == 2003:
                    break

    result = {
        "created": int(time.time()),
        "data": []
    }

    for i, url in enumerate(all_image_urls[:n]):
        result["data"].append({
            "url": url,
            "revised_prompt": prompt
        })

    if not result["data"]:
        if event_count < 5 and any("710022004" in rl or "rate_limited" in rl.lower() or "verify" in rl.lower() for rl in raw_lines):
            result["data"] = [{
                "url": "",
                "revised_prompt": prompt,
                "error": "请求被限流，请稍后再试或刷新Cookie。"
            }]
        else:
            result["data"] = [{
                "url": "",
                "revised_prompt": prompt,
                "error": "图片生成功能暂时不可用，请稍后再试，或直接在对话中尝试。"
            }]

    logger.info(f"Generated {len(result['data'])} images for prompt: {prompt[:50]}...")
    return result


async def delete_conversation(conversation_id: str) -> tuple[bool, str]:
    if not conversation_id or conversation_id == "0":
        return True, "No conversation to delete"

    account = cookie_pool.get_next()
    params = build_url_params(account)
    url = f"{CONFIG['api_base']}/samantha/thread/delete?{params}"

    headers = {
        'content-type': 'application/json',
        'cookie': account.get('cookie', CONFIG.get('cookie', '')),
        'origin': 'https://www.doubao.com',
        'referer': f"https://www.doubao.com/chat/{conversation_id}",
        'user-agent': USER_AGENT,
        'x-flow-trace': json.dumps({"trace_id": uuid.uuid4().hex, "span_id": uuid.uuid4().hex})
    }

    body = {"conversation_id": conversation_id}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.warning(f"Delete conversation {conversation_id} failed: {resp.status} {error_text[:200]}")
                    return False, f"HTTP {resp.status}"
                logger.info(f"Deleted conversation {conversation_id} on Doubao server")
                return True, ""
    except Exception as e:
        logger.error(f"Delete conversation exception: {e}")
        return False, str(e)
