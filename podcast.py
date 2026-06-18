import json
import uuid
import time
import asyncio
import logging
import os
import re
from typing import Optional

import aiohttp

from config import CONFIG, SIGN_METHOD, USER_AGENT, cookie_pool
from sse import (
    build_url_params, build_headers, generate_x_flow_trace,
    parse_sse_line, extract_text_from_event, extract_conversation_id
)

logger = logging.getLogger("webchat-api")

PODCAST_TASKS = {}

AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "podcast_audio")
os.makedirs(AUDIO_DIR, exist_ok=True)


def build_podcast_request_body(topic: str, conversation_id: str = "0", file_info: dict = None):
    body = {
        "bot_id": "7338286299411103781",
        "completion_option": {
            "is_regen": False,
            "with_suggest": False,
            "need_create_conversation": conversation_id == "0",
            "launch_stage": 1,
            "use_auto_cot": False,
            "use_deep_think": False
        },
        "conversation_id": conversation_id,
        "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
        "local_message_id": str(uuid.uuid4()),
        "messages": [{
            "content": json.dumps({"text": f"请帮我生成一期关于「{topic}」的AI播客"}, ensure_ascii=False),
            "content_type": 2001,
            "attachments": [],
            "references": []
        }],
        "ext": {
            "fp": CONFIG.get('fp', '')
        }
    }
    if file_info:
        body["messages"][0]["attachments"] = [file_info]
    return body


async def call_fpa_api(api_name: str, params: dict, account: dict):
    url = f"{CONFIG['api_base']}/api/doubao/do_action_v2"
    uid = ""
    try:
        cookie = account.get('cookie', CONFIG.get('cookie', ''))
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith('uid_tt='):
                uid = part.split('=', 1)[1]
                break
    except Exception:
        pass

    payload = {
        "scene": "FPA_Podcast",
        "payload": json.dumps({
            "api_name": api_name,
            "params": json.dumps(params)
        })
    }

    headers = {
        'content-type': 'application/json',
        'cookie': account.get('cookie', CONFIG.get('cookie', '')),
        'origin': 'https://www.doubao.com',
        'referer': 'https://www.doubao.com/',
        'user-agent': USER_AGENT,
        'x-flow-trace': generate_x_flow_trace()
    }

    query_params = {}
    if uid:
        query_params["uid"] = uid

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, params=query_params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            if data.get("code") != 0 or not data.get("data", {}).get("resp") or not data.get("data", {}).get("success"):
                raise Exception(f"FPA API failed: {json.dumps(data, ensure_ascii=False)[:300]}")
            return json.loads(data["data"]["resp"])


async def start_podcast_generation(topic: str, conversation_id: str = "0",
                                    file_info: dict = None,
                                    intro_jingle: bool = True,
                                    outro_jingle: bool = True):
    account = cookie_pool.get_next()
    task_id = f"podcast-{uuid.uuid4().hex[:12]}"

    PODCAST_TASKS[task_id] = {
        "task_id": task_id,
        "topic": topic,
        "status": "generating",
        "created_at": time.time(),
        "conversation_id": conversation_id,
        "episode_id": None,
        "audio_url": None,
        "cover_url": None,
        "title": None,
        "duration": None,
        "script": None,
        "error": None,
        "account": account,
        "file_info": file_info,
        "intro_jingle": intro_jingle,
        "outro_jingle": outro_jingle,
    }

    asyncio.create_task(_run_podcast_generation(task_id, topic, conversation_id, account, file_info))

    return {
        "task_id": task_id,
        "status": "generating",
        "topic": topic
    }


def _build_sse_url(account: dict):
    from config import signer

    base_url = f"{CONFIG['api_base']}/samantha/chat/completion"

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
        try:
            signed_url = asyncio.get_event_loop().run_until_complete(
                signer.get_signed_url(base_url, base_params)
            )
            if signed_url:
                return signed_url
        except Exception:
            pass

    params = build_url_params(account)
    return f"{base_url}?{params}"


async def _run_podcast_generation(task_id: str, topic: str, conversation_id: str,
                                   account: dict, file_info: dict = None,
                                   _retry_count: int = 0):
    try:
        body = build_podcast_request_body(topic, conversation_id, file_info)
        headers = build_headers(account)

        base_url = f"{CONFIG['api_base']}/samantha/chat/completion"

        if SIGN_METHOD == 'b2' and CONFIG.get('_signer') and CONFIG['_signer']._initialized:
            signer = CONFIG['_signer']
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
            signed_url = await signer.get_signed_url(base_url, base_params)
            if signed_url:
                url = signed_url
                logger.info(f"[Podcast] Using B2 signed URL")
            else:
                params = build_url_params(account)
                url = f"{base_url}?{params}"
        else:
            params = build_url_params(account)
            url = f"{base_url}?{params}"

        logger.info(f"[Podcast] Starting generation: topic={topic}, task_id={task_id}, "
                     f"account={account.get('name', 'unknown')}, conv={conversation_id}")

        full_text = ""
        episode_id = None
        audio_url = None
        cover_url = None
        title = None
        duration = None
        real_conv_id = conversation_id

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.error(f"[Podcast] API returned {resp.status}: {err_text[:500]}")
                    logger.error(f"[Podcast] Request URL: {url[:200]}")
                    logger.error(f"[Podcast] Request body: {json.dumps(body, ensure_ascii=False)[:300]}")

                    if cookie_pool.is_cookie_expired(err_text, resp.status):
                        cookie_pool.report_fail(account, "cookie_expired")
                        if _retry_count < 2:
                            next_account = cookie_pool.get_next()
                            logger.info(f"[Podcast] Cookie expired, retrying with: "
                                         f"{next_account.get('name', 'unknown')}")
                            PODCAST_TASKS[task_id]["account"] = next_account
                            await _run_podcast_generation(task_id, topic, conversation_id,
                                                           next_account, file_info, _retry_count + 1)
                            return
                    raise Exception(f"API returned {resp.status}: {err_text[:300]}")

                line_count = 0
                async for raw_line in resp.content:
                    try:
                        line = raw_line.decode('utf-8', errors='replace').strip()
                    except Exception:
                        continue

                    if not line:
                        continue

                    line_count += 1
                    if line_count <= 3:
                        logger.info(f"[Podcast] SSE line {line_count}: {line[:200]}")

                    if not line.startswith('data:'):
                        if line.startswith('event:'):
                            event_name = line[6:].strip()
                            if event_name == 'gateway-error':
                                logger.warning(f"[Podcast] Gateway error event detected")
                                if _retry_count < 2:
                                    next_account = cookie_pool.get_next()
                                    logger.info(f"[Podcast] Retrying after gateway error with: "
                                                 f"{next_account.get('name', 'unknown')}")
                                    PODCAST_TASKS[task_id]["account"] = next_account
                                    await _run_podcast_generation(task_id, topic, conversation_id,
                                                                   next_account, file_info, _retry_count + 1)
                                    return
                                raise Exception("Gateway error from API")
                        continue

                    parsed = parse_sse_line(line)
                    if not parsed:
                        continue

                    event_type = parsed.get("event_type")
                    data = parsed.get("data", {})

                    if event_type == 2001:
                        text, _ = extract_text_from_event(parsed)
                        if text:
                            full_text += text

                        conv_id = extract_conversation_id(parsed)
                        if conv_id:
                            real_conv_id = conv_id

                    elif event_type == 2003:
                        content_obj = data.get("content_obj", {})

                        podcast_data = content_obj.get("podcast")
                        if podcast_data:
                            episode_id = podcast_data.get("id")
                            title = podcast_data.get("podcast_title") or podcast_data.get("title")
                            meta = podcast_data.get("meta", {})
                            cover_url = meta.get("podcast_cover_thumbnail")
                            playback = meta.get("playback_model", {})
                            audio_url = playback.get("audio_link")
                            dur = playback.get("duration")
                            if dur:
                                try:
                                    duration = int(dur)
                                except Exception:
                                    duration = None
                            logger.info(f"[Podcast] Found podcast data: episode_id={episode_id}, "
                                         f"audio={'yes' if audio_url else 'no'}")

                        widget_data_str = content_obj.get("widget_data")
                        if widget_data_str and not episode_id:
                            try:
                                widget_data = json.loads(widget_data_str) if isinstance(widget_data_str, str) else widget_data_str
                                data_section = widget_data.get("data", {})
                                if isinstance(data_section, str):
                                    data_section = json.loads(data_section)
                                episode_list = data_section.get("episodeList", {})
                                episodes = episode_list.get("episodes", [])
                                if episodes:
                                    ep = episodes[0]
                                    episode_id = ep.get("id")
                                    title = ep.get("podcast_title") or ep.get("title")
                                    meta = ep.get("meta", {})
                                    cover_url = meta.get("podcast_cover_thumbnail")
                                    playback = meta.get("playback_model", {})
                                    audio_url = playback.get("audio_link")
                                    dur = playback.get("duration")
                                    if dur:
                                        try:
                                            duration = int(dur)
                                        except Exception:
                                            duration = None
                                    logger.info(f"[Podcast] Found episode from widget_data: "
                                                 f"episode_id={episode_id}")
                            except (json.JSONDecodeError, KeyError, TypeError) as e:
                                logger.warning(f"[Podcast] Failed to parse widget_data: {e}")

        PODCAST_TASKS[task_id]["script"] = full_text
        PODCAST_TASKS[task_id]["conversation_id"] = real_conv_id
        logger.info(f"[Podcast] Stream ended: {line_count} lines, "
                     f"full_text={len(full_text)} chars, episode_id={episode_id}")

        if episode_id:
            PODCAST_TASKS[task_id]["episode_id"] = episode_id
            if title:
                PODCAST_TASKS[task_id]["title"] = title
            if cover_url:
                PODCAST_TASKS[task_id]["cover_url"] = cover_url

            if not audio_url:
                logger.info(f"[Podcast] No audio_url in SSE, polling for detail...")
                await _poll_podcast_detail(task_id, episode_id, account)
            else:
                PODCAST_TASKS[task_id]["audio_url"] = audio_url
                PODCAST_TASKS[task_id]["duration"] = duration
                PODCAST_TASKS[task_id]["status"] = "completed"
                logger.info(f"[Podcast] Generation completed: task_id={task_id}, "
                             f"episode_id={episode_id}")
        else:
            if full_text:
                PODCAST_TASKS[task_id]["title"] = topic
                logger.info(f"[Podcast] Script generated but no audio episode_id. "
                             f"Script Length: {len(full_text)}")
                await _try_generate_audio_via_fpa(task_id, topic, real_conv_id, account)
            else:
                logger.warning(f"[Podcast] No content returned, falling back to browser approach")
                await _run_podcast_via_browser(task_id, topic, conversation_id, account, file_info)

    except Exception as e:
        logger.error(f"[Podcast] Generation failed: {e}")
        if _retry_count < 1:
            logger.info(f"[Podcast] Falling back to browser approach")
            try:
                await _run_podcast_via_browser(task_id, topic, conversation_id, account, file_info)
                return
            except Exception as be:
                logger.error(f"[Podcast] Browser fallback also failed: {be}")

        PODCAST_TASKS[task_id]["status"] = "failed"
        PODCAST_TASKS[task_id]["error"] = str(e)


async def _try_generate_audio_via_fpa(task_id: str, topic: str, conversation_id: str, account: dict):
    try:
        logger.info(f"[Podcast] Attempting audio generation via chat for conv {conversation_id}")

        body = {
            "bot_id": "7338286299411103781",
            "completion_option": {
                "is_regen": False,
                "with_suggest": False,
                "need_create_conversation": False,
                "launch_stage": 1,
                "use_auto_cot": False,
                "use_deep_think": False
            },
            "conversation_id": conversation_id,
            "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
            "local_message_id": str(uuid.uuid4()),
            "messages": [{
                "content": json.dumps({"text": "生成音频"}, ensure_ascii=False),
                "content_type": 2001,
                "attachments": [],
                "references": []
            }],
            "ext": {
                "fp": CONFIG.get('fp', '')
            }
        }

        params = build_url_params(account)
        url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"
        headers = build_headers(account)

        episode_id = None
        audio_url = None
        cover_url = None
        title = None
        duration = None
        api_failed = False

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning(f"[Podcast] Audio gen API returned {resp.status}: {err_text[:300]}")
                    api_failed = True
                else:
                    async for raw_line in resp.content:
                        try:
                            line = raw_line.decode('utf-8', errors='replace').strip()
                        except Exception:
                            continue
                        if not line:
                            continue

                        if line.startswith('event:'):
                            event_name = line[6:].strip()
                            if event_name == 'gateway-error':
                                logger.warning(f"[Podcast] Audio gen gateway error")
                                api_failed = True
                                break
                            continue

                        parsed = parse_sse_line(line)
                        if not parsed:
                            continue

                        event_type = parsed.get("event_type")
                        data = parsed.get("data", {})

                        if event_type == 2003:
                            content_obj = data.get("content_obj", {})
                            podcast_data = content_obj.get("podcast")
                            if podcast_data:
                                episode_id = podcast_data.get("id")
                                title = podcast_data.get("podcast_title") or podcast_data.get("title")
                                meta = podcast_data.get("meta", {})
                                cover_url = meta.get("podcast_cover_thumbnail")
                                playback = meta.get("playback_model", {})
                                audio_url = playback.get("audio_link")
                                dur = playback.get("duration")
                                if dur:
                                    try:
                                        duration = int(dur)
                                    except Exception:
                                        duration = None

        if api_failed:
            raise Exception(f"Audio gen API failed")

        if episode_id:
            PODCAST_TASKS[task_id]["episode_id"] = episode_id
            if title:
                PODCAST_TASKS[task_id]["title"] = title
            if cover_url:
                PODCAST_TASKS[task_id]["cover_url"] = cover_url
            if audio_url:
                PODCAST_TASKS[task_id]["audio_url"] = audio_url
                PODCAST_TASKS[task_id]["duration"] = duration
                PODCAST_TASKS[task_id]["status"] = "completed"
                return
            await _poll_podcast_detail(task_id, episode_id, account)
            return

    except Exception as e:
        logger.warning(f"[Podcast] Chat-based audio gen failed: {e}")

    logger.info(f"[Podcast] Falling back to TTS audio generation...")
    await _generate_audio_via_tts(task_id)


async def _run_podcast_via_browser(task_id: str, topic: str, conversation_id: str,
                                    account: dict, file_info: dict = None):
    try:
        from exporter import _get_valid_account, _launch_browser, _create_context

        cookie_str = account.get('cookie', CONFIG.get('cookie', ''))
        logger.info(f"[Podcast] Starting browser-based generation: topic={topic}, task_id={task_id}")

        browser = await _launch_browser()
        context = await _create_context(browser, cookie_str)
        page = await context.new_page()

        full_text = ""
        episode_id = None
        audio_url = None
        cover_url = None
        title = None
        duration = None
        real_conv_id = conversation_id
        found_audio = False
        current_event = ""

        async def capture_api(route):
            nonlocal full_text, episode_id, audio_url, cover_url, title, duration
            nonlocal real_conv_id, found_audio, current_event
            url = route.request.url

            try:
                response = await route.fetch()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass
                return

            is_completion = '/chat/completion' in url or '/samantha/chat/completion' in url
            is_fpa = '/api/doubao/do_action' in url

            if is_completion:
                try:
                    body = await response.text()
                    for line in body.split('\n'):
                        line = line.strip()
                        if not line:
                            continue

                        if line.startswith('event:'):
                            current_event = line[6:].strip()
                            continue

                        if not line.startswith('data:'):
                            continue

                        data_str = line[5:].strip()
                        if data_str == '[DONE]':
                            break
                        if data_str == '{}':
                            continue

                        try:
                            outer = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        if current_event in ('SSE_HEARTBEAT', 'SSE_ACK'):
                            continue

                        if current_event == 'FULL_MSG_NOTIFY':
                            msg = outer.get("message", {})
                            conv_id = msg.get("conversation_id")
                            if conv_id:
                                real_conv_id = conv_id
                            continue

                        if current_event == 'STREAM_MSG_NOTIFY':
                            content = outer.get("content", {})
                            content_blocks = content.get("content_block", [])
                            for block in content_blocks:
                                bt = block.get("block_type", 0)
                                block_content = block.get("content", {})

                                if bt == 10000:
                                    tb = block_content.get("text_block", {})
                                    text = tb.get("text", "")
                                    if text:
                                        full_text += text

                                elif bt == 2074:
                                    cb = block_content.get("creation_block", block_content)
                                    creations = cb.get("creations", [])
                                    for creation in creations:
                                        img_raw = creation.get("image_raw", {})
                                        if isinstance(img_raw, dict):
                                            img_url = img_raw.get("url", "")
                                            if img_url:
                                                logger.info(f"[Podcast] Browser: found image in creation block")

                            meta = outer.get("meta", {})
                            conv_id = meta.get("conversation_id")
                            if conv_id:
                                real_conv_id = conv_id
                            continue

                        if current_event == 'STREAM_CHUNK':
                            patch_ops = outer.get("patch_op", [])
                            for op in patch_ops:
                                patch_value = op.get("patch_value", {})
                                content_blocks = patch_value.get("content_block", [])
                                for block in content_blocks:
                                    bt = block.get("block_type", 0)
                                    block_content = block.get("content", {})
                                    if bt == 10000:
                                        tb = block_content.get("text_block", {})
                                        text = tb.get("text", "")
                                        if text:
                                            full_text += text
                            continue

                        event_type = outer.get("event_type")
                        if event_type:
                            event_data_raw = outer.get("event_data", "")
                            if isinstance(event_data_raw, str) and event_data_raw:
                                try:
                                    event_data = json.loads(event_data_raw)
                                except Exception:
                                    event_data = {}
                            else:
                                event_data = event_data_raw if isinstance(event_data_raw, dict) else {}

                            if event_type == 2001:
                                msg = event_data.get("message", {})
                                ct = msg.get("content_type")
                                raw_content = msg.get("content", "")
                                if ct in (10000, 2001) and isinstance(raw_content, str):
                                    try:
                                        parsed = json.loads(raw_content)
                                        text = parsed.get("text", "")
                                        if text:
                                            full_text += text
                                    except Exception:
                                        if raw_content:
                                            full_text += raw_content
                                conv_id = event_data.get("conversation_id")
                                if conv_id:
                                    real_conv_id = conv_id

                            if event_type == 2003:
                                content_obj = event_data.get("content_obj", {})
                                _extract_podcast_from_event_2003(
                                    content_obj, task_id,
                                    local_vars={
                                        'episode_id': episode_id,
                                        'audio_url': audio_url,
                                        'cover_url': cover_url,
                                        'title': title,
                                        'duration': duration,
                                        'found_audio': found_audio
                                    }
                                )
                                podcast_data = content_obj.get("podcast")
                                if podcast_data:
                                    episode_id = podcast_data.get("id")
                                    title = podcast_data.get("podcast_title") or podcast_data.get("title")
                                    meta = podcast_data.get("meta", {})
                                    cover_url = meta.get("podcast_cover_thumbnail")
                                    playback = meta.get("playback_model", {})
                                    audio_link = playback.get("audio_link")
                                    if audio_link:
                                        audio_url = audio_link
                                        found_audio = True
                                    dur = playback.get("duration")
                                    if dur:
                                        try:
                                            duration = int(dur)
                                        except Exception:
                                            duration = None
                                    logger.info(f"[Podcast] Browser: found podcast data, "
                                                 f"episode_id={episode_id}")

                                widget_data_str = content_obj.get("widget_data")
                                if widget_data_str and not episode_id:
                                    try:
                                        widget_data = json.loads(widget_data_str) if isinstance(widget_data_str, str) else widget_data_str
                                        data_section = widget_data.get("data", {})
                                        if isinstance(data_section, str):
                                            data_section = json.loads(data_section)
                                        episode_list = data_section.get("episodeList", {})
                                        episodes = episode_list.get("episodes", [])
                                        if episodes:
                                            ep = episodes[0]
                                            episode_id = ep.get("id")
                                            title = ep.get("podcast_title") or ep.get("title")
                                            meta = ep.get("meta", {})
                                            cover_url = meta.get("podcast_cover_thumbnail")
                                            playback = meta.get("playback_model", {})
                                            audio_link = playback.get("audio_link")
                                            if audio_link:
                                                audio_url = audio_link
                                                found_audio = True
                                            dur = playback.get("duration")
                                            if dur:
                                                try:
                                                    duration = int(dur)
                                                except Exception:
                                                    duration = None
                                    except Exception as e:
                                        logger.warning(f"[Podcast] Browser: widget parse error: {e}")

                except Exception as e:
                    logger.warning(f"[Podcast] Browser: SSE parse error: {e}")

            if is_fpa:
                try:
                    body = await response.json()
                    if body.get("code") == 0 and body.get("data", {}).get("success"):
                        resp_data = json.loads(body["data"].get("resp", "{}"))
                        episode = resp_data.get("episode", {})
                        if episode:
                            meta = episode.get("meta", {})
                            playback = meta.get("playback_model", {})
                            audio_link = playback.get("audio_link")
                            if audio_link:
                                audio_url = audio_link
                                found_audio = True
                                if not episode_id:
                                    episode_id = episode.get("id")
                                if not title:
                                    title = episode.get("podcast_title") or episode.get("title")
                                if not cover_url:
                                    cover_url = meta.get("podcast_cover_thumbnail")
                                dur = playback.get("duration")
                                if dur:
                                    try:
                                        duration = int(dur)
                                    except Exception:
                                        duration = None
                                logger.info(f"[Podcast] Browser: found audio via FPA API")
                except Exception:
                    pass

            try:
                await route.fulfill(response=response)
            except Exception:
                pass

        await page.route('**/chat/completion**', capture_api)
        await page.route('**/samantha/**', capture_api)
        await page.route('**/api/doubao/**', capture_api)

        chat_url = 'https://www.doubao.com/chat/'
        logger.info(f"[Podcast] Browser: navigating to {chat_url}")
        await page.goto(chat_url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(5)

        try:
            podcast_btn = page.locator('button:has-text("AI 播客"), button:has-text("播客")').first
            await podcast_btn.click(timeout=5000)
            await asyncio.sleep(2)
            logger.info(f"[Podcast] Browser: clicked podcast button")
        except Exception:
            logger.info(f"[Podcast] Browser: podcast button not found, trying direct input")

        try:
            textarea = page.locator('textarea, [contenteditable="true"]').first
            await textarea.click(timeout=5000)
            await asyncio.sleep(0.5)
            await textarea.fill(topic)
            await asyncio.sleep(1)
            logger.info(f"[Podcast] Browser: filled topic: {topic}")
        except Exception as e:
            logger.error(f"[Podcast] Browser: failed to fill textarea: {e}")
            raise Exception(f"Failed to input podcast topic: {e}")

        try:
            send_btn = page.locator('button[class*="send"], button[aria-label*="发送"], '
                                     'button[aria-label*="Send"]').first
            await send_btn.click(timeout=5000)
            logger.info(f"[Podcast] Browser: clicked send button")
        except Exception:
            try:
                await page.keyboard.press('Enter')
                logger.info(f"[Podcast] Browser: pressed Enter to send")
            except Exception:
                pass

        logger.info(f"[Podcast] Browser: waiting for script generation...")
        for i in range(60):
            await asyncio.sleep(5)
            if full_text and not found_audio and i > 10:
                break
            if found_audio:
                break
            if i > 0 and i % 12 == 0:
                logger.info(f"[Podcast] Browser: still waiting... text={len(full_text)} chars, "
                             f"audio={'yes' if found_audio else 'no'}")

        logger.info(f"[Podcast] Browser: script generated, text={len(full_text)} chars, "
                     f"episode_id={episode_id}, audio={'yes' if found_audio else 'no'}")

        if not found_audio and full_text:
            logger.info(f"[Podcast] Browser: looking for audio generation button...")
            await asyncio.sleep(3)

            try:
                gen_audio_btn = page.locator('text=生成音频').first
                await gen_audio_btn.click(timeout=5000)
                logger.info(f"[Podcast] Browser: clicked 'generate audio' button")

                for i in range(60):
                    await asyncio.sleep(5)
                    if found_audio:
                        logger.info(f"[Podcast] Browser: audio found after clicking generate!")
                        break
            except Exception:
                logger.info(f"[Podcast] Browser: no 'generate audio' button found")

            if not found_audio:
                try:
                    all_buttons = await page.locator('button').all()
                    for btn in all_buttons:
                        try:
                            btn_text = await btn.text_content()
                            if btn_text and any(kw in btn_text for kw in
                                                  ['音频', '播放', '生成', '收听', '朗读']):
                                logger.info(f"[Podcast] Browser: found button: {btn_text}")
                                await btn.click(timeout=3000)
                                await asyncio.sleep(5)
                                if found_audio:
                                    break
                        except Exception:
                            continue
                except Exception:
                    pass

        PODCAST_TASKS[task_id]["script"] = full_text
        PODCAST_TASKS[task_id]["conversation_id"] = real_conv_id

        if found_audio and audio_url:
            PODCAST_TASKS[task_id]["audio_url"] = audio_url
            PODCAST_TASKS[task_id]["duration"] = duration
            PODCAST_TASKS[task_id]["status"] = "completed"
            if episode_id:
                PODCAST_TASKS[task_id]["episode_id"] = episode_id
            if title:
                PODCAST_TASKS[task_id]["title"] = title
            if cover_url:
                PODCAST_TASKS[task_id]["cover_url"] = cover_url
            logger.info(f"[Podcast] Browser: completed with audio! task_id={task_id}")
        elif episode_id:
            PODCAST_TASKS[task_id]["episode_id"] = episode_id
            if title:
                PODCAST_TASKS[task_id]["title"] = title
            if cover_url:
                PODCAST_TASKS[task_id]["cover_url"] = cover_url
            logger.info(f"[Podcast] Browser: episode found, polling for audio...")
            await _poll_podcast_detail(task_id, episode_id, account)
        elif full_text:
            PODCAST_TASKS[task_id]["status"] = "script_ready"
            PODCAST_TASKS[task_id]["title"] = topic
            logger.info(f"[Podcast] Browser: script only, no audio. text={len(full_text)} chars")
        else:
            PODCAST_TASKS[task_id]["status"] = "failed"
            PODCAST_TASKS[task_id]["error"] = "No content returned from podcast generation"

        await browser.close()

    except Exception as e:
        logger.error(f"[Podcast] Browser generation failed: {e}")
        PODCAST_TASKS[task_id]["status"] = "failed"
        PODCAST_TASKS[task_id]["error"] = str(e)
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


def _extract_podcast_from_event_2003(content_obj, task_id, local_vars):
    pass


def _clean_podcast_script(script: str) -> str:
    if not script:
        return script

    patterns = [
        r'^[\s\S]*?(?=#{1,4}\s)',
        r'^.*?(?:好的|没问题|当然|我来帮你|请帮我|让我来|我将|下面是|以下是|这是).{0,30}(?:播客|节目|脚本|内容|对话|稿).{0,50}\n',
        r'^.*?(?:好的|没问题|当然|我来帮你|请帮我|让我来|我将|下面是|以下是|这是).{0,30}(?:播客|节目|脚本|内容|对话|稿).{0,50}\n\n',
    ]

    for pattern in patterns:
        if pattern == patterns[0]:
            m = re.search(r'#{1,4}\s', script)
            if m and m.start() > 0:
                prefix = script[:m.start()].strip()
                if prefix and not re.match(r'^#{1,4}\s', prefix):
                    has_intro_kw = any(kw in prefix for kw in
                                       ['好的', '没问题', '当然', '我来', '请帮', '让我', '下面', '以下', '这是', '生成'])
                    if has_intro_kw:
                        script = script[m.start():]
                        break

    script = re.sub(r'^.*?(?:好的|没问题|当然)[，,].{0,50}(?:播客|节目|脚本).{0,30}\n', '', script, count=1)

    return script.strip()


def _parse_podcast_script(script: str) -> list:
    segments = []
    if not script:
        return segments

    current_speaker = "default"
    current_text = ""

    lines = script.split('\n')
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        speaker_match = re.match(
            r'^(?:#{1,4}\s*)?(?:主播\s*(\d)|主播([一二])|合|旁白|Host\s*(\d))\s*[：:（(]?\s*(.*)',
            stripped
        )

        section_match = re.match(
            r'^#{1,4}\s*[\[【](.+?)[\]】]|^#{1,4}\s*(.+?)[：:]\s*\d',
            stripped
        )

        if speaker_match:
            if current_text.strip():
                segments.append({"speaker": current_speaker, "text": current_text.strip()})
                current_text = ""

            num = speaker_match.group(1) or speaker_match.group(2) or speaker_match.group(3) or ""
            rest = speaker_match.group(4) or ""

            if num in ("1", "一"):
                current_speaker = "host1"
            elif num in ("2", "二"):
                current_speaker = "host2"
            elif '合' in stripped[:5]:
                current_speaker = "both"
            else:
                current_speaker = "host1"

            if rest:
                current_text = rest + " "
        elif section_match:
            if current_text.strip():
                segments.append({"speaker": current_speaker, "text": current_text.strip()})
                current_text = ""
            current_speaker = "default"
        else:
            clean = re.sub(r'[#*`]', '', stripped)
            if clean and not clean.startswith('（') and not clean.startswith('('):
                current_text += clean + " "

    if current_text.strip():
        segments.append({"speaker": current_speaker, "text": current_text.strip()})

    if not segments:
        paragraphs = [p.strip() for p in script.split('\n\n') if p.strip()]
        for p in paragraphs:
            clean = re.sub(r'[#*`]', '', p)
            clean = re.sub(r'（[^）]*）', '', clean)
            clean = re.sub(r'\([^)]*\)', '', clean)
            if clean.strip():
                segments.append({"speaker": "default", "text": clean.strip()})

    return segments


VOICE_MAP = {
    "host1": "zh-CN-XiaoxiaoNeural",
    "host2": "zh-CN-YunxiNeural",
    "both": "zh-CN-XiaoxiaoNeural",
    "default": "zh-CN-XiaoxiaoNeural",
}

VOLCENGINE_VOICE_MAP = {
    "host1": "zh_female_wenroutaozi_uranus_bigtts",
    "host2": "zh_female_wenroutaozi_uranus_bigtts",
    "both": "zh_female_wenroutaozi_uranus_bigtts",
    "default": "zh_female_wenroutaozi_uranus_bigtts",
}

VOLCENGINE_AVAILABLE_VOICES = {"zh_female_wenroutaozi_uranus_bigtts"}

PODCAST_JINGLE_INTRO = os.path.join(AUDIO_DIR, "intro_jingle.mp3")
PODCAST_JINGLE_OUTRO = os.path.join(AUDIO_DIR, "outro_jingle.mp3")

PODCAST_CONFIG = {
    "intro_jingle": True,
    "outro_jingle": True,
}

TTS_PROXIES = [
    "http://localhost:1085",
    "socks5://localhost:1083",
    "http://localhost:1082",
    "socks5://localhost:1086",
    "http://localhost:7892",
    "http://localhost:10809",
    "http://localhost:7891",
    "http://localhost:7899",
    "socks5://localhost:1089",
]


def _merge_jingle_audio(output_file: str, task_id: str) -> bool:
    task = PODCAST_TASKS.get(task_id, {})
    add_intro = task.get("intro_jingle", PODCAST_CONFIG.get("intro_jingle", True)) and os.path.exists(PODCAST_JINGLE_INTRO)
    add_outro = task.get("outro_jingle", PODCAST_CONFIG.get("outro_jingle", True)) and os.path.exists(PODCAST_JINGLE_OUTRO)

    if not add_intro and not add_outro:
        return False

    try:
        import subprocess

        merged_file = output_file + ".merged.mp3"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

        input_idx = 0
        filter_parts = []
        inputs = []

        if add_intro:
            inputs.extend(["-i", PODCAST_JINGLE_INTRO])
            intro_idx = input_idx
            input_idx += 1

        inputs.extend(["-i", output_file])
        content_idx = input_idx
        input_idx += 1

        if add_outro:
            inputs.extend(["-i", PODCAST_JINGLE_OUTRO])
            outro_idx = input_idx
            input_idx += 1

        filter_complex_parts = []
        concat_inputs = []

        if add_intro:
            filter_complex_parts.append(f"[{intro_idx}:a]afade=t=in:st=0:d=0.5,afade=t=out:st=3:d=0.5[intro]")
            concat_inputs.append("[intro]")

        filter_complex_parts.append(f"[{content_idx}:a]afade=t=in:st=0:d=0.3[content]")
        concat_inputs.append("[content]")

        if add_outro:
            filter_complex_parts.append(f"[{outro_idx}:a]afade=t=in:st=0:d=0.3,afade=t=out:st=3:d=1.5[outro]")
            concat_inputs.append("[outro]")

        n = len(concat_inputs)
        concat_str = "".join(concat_inputs)
        filter_complex_parts.append(f"{concat_str}concat=n={n}:v=0:a=1[out]")

        filter_complex = ";".join(filter_complex_parts)

        cmd.extend(inputs)
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-ar", "24000",
            "-b:a", "128k",
            merged_file
        ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0 and os.path.exists(merged_file):
            merged_size = os.path.getsize(merged_file)
            if merged_size > 0:
                os.replace(merged_file, output_file)
                logger.info(f"[Podcast] Merged jingle audio: {merged_size} bytes")
                return True
            else:
                try:
                    os.remove(merged_file)
                except:
                    pass
        else:
            logger.warning(f"[Podcast] ffmpeg merge failed: {result.stderr[:200]}")
            if os.path.exists(merged_file):
                try:
                    os.remove(merged_file)
                except:
                    pass

    except FileNotFoundError:
        logger.debug(f"[Podcast] ffmpeg not available, skipping jingle merge")
    except subprocess.TimeoutExpired:
        logger.warning(f"[Podcast] ffmpeg merge timeout")
    except Exception as e:
        logger.warning(f"[Podcast] Jingle merge error: {e}")

    return False


async def _generate_audio_via_tts(task_id: str):
    script = PODCAST_TASKS[task_id].get("script", "")
    if not script:
        PODCAST_TASKS[task_id]["status"] = "script_ready"
        return

    logger.info(f"[Podcast] Starting TTS audio generation for task {task_id}")

    output_file = os.path.join(AUDIO_DIR, f"{task_id}.mp3")

    clean_script = _clean_podcast_script(script)
    segments = _parse_podcast_script(clean_script)
    if not segments:
        clean_script = re.sub(r'#{1,6}\s*', '', script)
        clean_script = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', clean_script)
        clean_script = re.sub(r'（[^）]*）', '', clean_script)
        clean_script = re.sub(r'\([^)]*\)', '', clean_script)
        clean_script = re.sub(r'`[^`]*`', '', clean_script)
        clean_script = clean_script.strip()
        if clean_script:
            segments = [{"speaker": "default", "text": clean_script}]

    if not segments:
        PODCAST_TASKS[task_id]["status"] = "script_ready"
        return

    volcengine_ok = await _try_volcengine_tts(task_id, segments, output_file)
    if volcengine_ok:
        return

    logger.info(f"[Podcast] Volcengine TTS failed, falling back to edge-tts...")
    await _generate_audio_via_edge_tts(task_id, output_file)


async def _try_volcengine_tts(task_id: str, segments: list, output_file: str) -> bool:
    try:
        from volcengine_tts import volcengine_tts, volcengine_tts_segmented, get_tts_credentials

        account = PODCAST_TASKS[task_id].get("account", cookie_pool.get_next())

        for attempt in range(3):
            creds = await get_tts_credentials(account)
            app_key = creds.get("app_key", "")
            access_key = creds.get("access_key", "")

            if not app_key or not access_key:
                logger.warning(f"[Podcast] Volcengine TTS: no credentials (attempt {attempt+1})")
                account = cookie_pool.get_next()
                continue

            break
        else:
            logger.warning(f"[Podcast] Volcengine TTS: no valid credentials from any account")
            return False

        if len(segments) == 1:
            seg = segments[0]
            speaker_key = seg.get("speaker", "default")
            speaker = VOLCENGINE_VOICE_MAP.get(speaker_key,
                                               VOLCENGINE_VOICE_MAP["default"])
            text = seg.get("text", "").strip()

            result = await volcengine_tts(
                text=text,
                output_path=output_file,
                app_key=app_key,
                access_key=access_key,
                speaker=speaker,
                audio_format="mp3",
            )
        else:
            result = await volcengine_tts_segmented(
                segments=segments,
                output_path=output_file,
                app_key=app_key,
                access_key=access_key,
                speaker_map=VOLCENGINE_VOICE_MAP,
                audio_format="mp3",
            )

        if result.get("success"):
            _merge_jingle_audio(output_file, task_id)

            audio_url = f"/v1/podcast/file/{task_id}.mp3"

            duration = None
            try:
                import subprocess
                probe_result = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', output_file],
                    capture_output=True, text=True, timeout=10
                )
                if probe_result.returncode == 0 and probe_result.stdout.strip():
                    duration = int(float(probe_result.stdout.strip()))
            except Exception:
                pass

            PODCAST_TASKS[task_id]["audio_url"] = audio_url
            PODCAST_TASKS[task_id]["duration"] = duration
            PODCAST_TASKS[task_id]["status"] = "completed"
            logger.info(f"[Podcast] Volcengine TTS audio generated: "
                         f"{result.get('bytes', 0)} bytes, duration={duration}s")
            return True
        else:
            logger.warning(f"[Podcast] Volcengine TTS failed: {result.get('error', '')}")
            return False

    except ImportError:
        logger.warning(f"[Podcast] volcengine_tts module not available")
        return False
    except Exception as e:
        logger.error(f"[Podcast] Volcengine TTS error: {e}")
        return False


async def _generate_audio_via_edge_tts(task_id: str, output_file: str):
    try:
        import edge_tts

        script = PODCAST_TASKS[task_id].get("script", "")
        if not script:
            PODCAST_TASKS[task_id]["status"] = "script_ready"
            return

        clean_script = re.sub(r'#{1,6}\s*', '', script)
        clean_script = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', clean_script)
        clean_script = re.sub(r'（[^）]*）', '', clean_script)
        clean_script = re.sub(r'\([^)]*\)', '', clean_script)
        clean_script = re.sub(r'`[^`]*`', '', clean_script)
        clean_script = re.sub(r'\n{3,}', '\n\n', clean_script)
        clean_script = clean_script.strip()

        if not clean_script:
            PODCAST_TASKS[task_id]["status"] = "script_ready"
            return

        primary_voice = VOICE_MAP["host1"]
        communicate = edge_tts.Communicate(clean_script, primary_voice)

        saved = False
        try:
            await communicate.save(output_file)
            saved = True
        except Exception as e:
            logger.warning(f"[Podcast] Edge-TTS direct connection failed: {e}, trying proxies...")

        if not saved:
            for proxy in TTS_PROXIES:
                try:
                    logger.info(f"[Podcast] Trying edge-TTS with proxy: {proxy}")
                    communicate = edge_tts.Communicate(clean_script, primary_voice, proxy=proxy)
                    await communicate.save(output_file)
                    saved = True
                    logger.info(f"[Podcast] Edge-TTS succeeded with proxy: {proxy}")
                    break
                except Exception as pe:
                    logger.warning(f"[Podcast] Edge-TTS proxy {proxy} failed: {pe}")
                    continue

        if not saved:
            logger.error(f"[Podcast] Edge-TTS failed with all proxies")
            PODCAST_TASKS[task_id]["status"] = "script_ready"
            return

        file_size = os.path.getsize(output_file)
        if file_size == 0:
            logger.error(f"[Podcast] Edge-TTS generated empty file")
            PODCAST_TASKS[task_id]["status"] = "script_ready"
            return

        _merge_jingle_audio(output_file, task_id)

        audio_url = f"/v1/podcast/file/{task_id}.mp3"

        duration = None
        try:
            import subprocess
            result = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', output_file],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                duration = int(float(result.stdout.strip()))
        except Exception:
            pass

        PODCAST_TASKS[task_id]["audio_url"] = audio_url
        PODCAST_TASKS[task_id]["duration"] = duration
        PODCAST_TASKS[task_id]["status"] = "completed"
        logger.info(f"[Podcast] Edge-TTS audio generated: {file_size} bytes, "
                     f"duration={duration}s, url={audio_url}")

    except ImportError:
        logger.warning(f"[Podcast] edge-tts not installed, skipping TTS generation")
        PODCAST_TASKS[task_id]["status"] = "script_ready"
    except Exception as e:
        logger.error(f"[Podcast] Edge-TTS audio generation failed: {e}")
        PODCAST_TASKS[task_id]["status"] = "script_ready"


async def _poll_podcast_detail(task_id: str, episode_id: str, account: dict,
                                max_retries: int = 30):
    for i in range(max_retries):
        try:
            result = await call_fpa_api("GetGenPodcastDetail", {"id": episode_id}, account)

            episode = result.get("episode", {})
            meta = episode.get("meta", {})
            playback = meta.get("playback_model", {})

            audio_link = playback.get("audio_link")
            if audio_link:
                PODCAST_TASKS[task_id]["audio_url"] = audio_link
                PODCAST_TASKS[task_id]["duration"] = int(playback.get("duration", 0)) \
                    if playback.get("duration") else None
                PODCAST_TASKS[task_id]["cover_url"] = meta.get("podcast_cover_thumbnail")
                PODCAST_TASKS[task_id]["title"] = episode.get("podcast_title") or episode.get("title")
                PODCAST_TASKS[task_id]["status"] = "completed"
                logger.info(f"[Podcast] Polling completed: task_id={task_id}")
                return

            status = episode.get("status") or result.get("status")
            logger.info(f"[Podcast] Poll {i+1}/{max_retries}: episode_id={episode_id}, "
                         f"status={status}, no audio_link yet")

            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"[Podcast] Poll attempt {i+1} failed: {e}")
            await asyncio.sleep(5)

    PODCAST_TASKS[task_id]["status"] = "script_ready"
    PODCAST_TASKS[task_id]["error"] = "Audio polling timed out, but script is available"


async def get_podcast_status(task_id: str):
    task = PODCAST_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found", "task_id": task_id}

    return {
        "task_id": task["task_id"],
        "topic": task["topic"],
        "status": task["status"],
        "episode_id": task.get("episode_id"),
        "title": task.get("title"),
        "cover_url": task.get("cover_url"),
        "audio_url": task.get("audio_url"),
        "duration": task.get("duration"),
        "script_length": len(task.get("script", "")) if task.get("script") else 0,
        "error": task.get("error"),
        "created_at": task.get("created_at"),
        "conversation_id": task.get("conversation_id")
    }


async def get_podcast_script(task_id: str):
    task = PODCAST_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}
    return {
        "task_id": task_id,
        "topic": task["topic"],
        "status": task["status"],
        "script": task.get("script", ""),
        "title": task.get("title"),
        "conversation_id": task.get("conversation_id")
    }


async def get_podcast_audio(task_id: str):
    task = PODCAST_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}

    if task["status"] not in ("completed", "script_ready"):
        return {"error": f"Podcast not ready, current status: {task['status']}"}

    episode_id = task.get("episode_id")
    audio_url = task.get("audio_url")

    if audio_url:
        return {
            "task_id": task_id,
            "episode_id": episode_id,
            "audio_url": audio_url,
            "title": task.get("title"),
            "duration": task.get("duration"),
            "cover_url": task.get("cover_url")
        }

    if episode_id:
        try:
            account = task.get("account", cookie_pool.get_next())
            result = await call_fpa_api("GetGenPodcastVideoUrl",
                                         {"episode_id": episode_id}, account)
            video_url = result.get("video_url")
            if video_url:
                task["audio_url"] = video_url
                return {
                    "task_id": task_id,
                    "episode_id": episode_id,
                    "audio_url": video_url,
                    "title": task.get("title"),
                    "duration": task.get("duration"),
                    "cover_url": task.get("cover_url")
                }
        except Exception:
            pass

    return {"error": "Audio not available. Podcast script is ready but audio generation "
                     "requires the Doubao client."}


async def list_podcasts():
    tasks = []
    for task_id, task in PODCAST_TASKS.items():
        tasks.append({
            "task_id": task_id,
            "topic": task["topic"],
            "status": task["status"],
            "title": task.get("title"),
            "duration": task.get("duration"),
            "script_length": len(task.get("script", "")) if task.get("script") else 0,
            "created_at": task.get("created_at")
        })
    tasks.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"tasks": tasks}
