import json
import uuid
import time
import asyncio
import logging
import base64
import os
import subprocess
import sys
from typing import Optional

import aiohttp

from config import CONFIG, USER_AGENT, cookie_pool
from sse import (
    build_url_params, build_headers, generate_x_flow_trace,
    parse_sse_line, extract_conversation_id
)

logger = logging.getLogger("doubao-api")

MUSIC_TASKS = {}

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", "audio")


async def _download_audio_to_local(task_id: str, audio_url: str, account: dict):
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        local_path = os.path.join(MEDIA_DIR, f"{task_id}.mp3")
        if os.path.exists(local_path):
            MUSIC_TASKS[task_id]["local_audio_path"] = local_path
            logger.info(f"[Music] Audio already cached: {local_path}")
            return
        headers = {
            "User-Agent": "python-requests/2.31.0",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=120),
                                   headers=headers, allow_redirects=True) as resp:
                if resp.status == 200:
                    body = await resp.read()
                    with open(local_path, "wb") as f:
                        f.write(body)
                    MUSIC_TASKS[task_id]["local_audio_path"] = local_path
                    logger.info(f"[Music] Downloaded audio: {local_path} ({len(body)} bytes)")
                else:
                    logger.warning(f"[Music] Failed to download audio: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"[Music] Error downloading audio: {e}")

MUSIC_STYLES = ["流行", "嘻哈", "国风", "DJ", "摇滚", "民谣", "R&B", "雷鬼", "朋克", "电音", "爵士"]
MUSIC_MOODS = ["快乐", "放松", "活力", "兴奋", "忧郁", "鼓舞", "伤感", "怀旧", "浪漫"]
MUSIC_VOICES = ["女声", "男声"]

PW_LOCK = asyncio.Lock()


def build_music_request_body(prompt: str, conversation_id: str = "0",
                              style: str = "", mood: str = "", voice: str = "",
                              lyric: str = ""):
    text = prompt if prompt else "我想创作一首歌曲"
    if style or mood or voice:
        parts = ["我想创作一首歌曲，用AI 帮我写歌词。"]
        if style:
            parts.append(f"这首歌是{style}音乐风格，")
        if mood:
            parts.append(f"传达{mood}的情绪，")
        if voice:
            parts.append(f"使用{voice} 音色。")
        text = "".join(parts).rstrip("，") + "。"

    body = {
        "messages": [{
            "content": json.dumps({
                "text": text,
                "lyric": lyric if lyric else "",
                "theme": style or "",
                "mood": mood or "",
                "genre": style or "",
                "gender": voice or "",
                "generation_type": ""
            }, ensure_ascii=False),
            "content_type": 2005
        }],
        "completion_option": {
            "is_regen": False,
            "with_suggest": True,
            "need_create_conversation": conversation_id == "0",
            "launch_stage": 1,
            "is_replace": False,
            "is_delete": False,
            "is_ai_playground": False,
            "message_from": 0,
            "action_bar_skill_id": 9,
            "use_auto_cot": False,
            "resend_for_regen": False,
            "enable_commerce_credit": False,
            "event_id": "0"
        },
        "evaluate_option": {
            "web_ab_params": ""
        },
        "conversation_id": conversation_id,
        "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
        "local_message_id": str(uuid.uuid4())
    }
    return body


def _decode_base64_url(b64_str: str) -> str:
    try:
        return base64.b64decode(b64_str).decode('utf-8')
    except Exception:
        return b64_str


def _extract_music_from_content(ct_content: dict):
    result = {
        "title": None, "lyric": None, "audio_url": None,
        "cover_url": None, "duration": None, "vid": None
    }

    tasks = ct_content.get("tasks", {})
    if isinstance(tasks, dict):
        for key, task in tasks.items():
            if isinstance(task, dict):
                result["title"] = task.get("title")
                result["lyric"] = task.get("lyric")
                result["vid"] = task.get("vid")

                cover = task.get("cover", {})
                if cover:
                    image_ori = cover.get("image_ori", {})
                    image_thumb = cover.get("image_thumb", {})
                    result["cover_url"] = (image_ori or image_thumb).get("url", "")

                video_model_str = task.get("video_model")
                if video_model_str:
                    try:
                        if isinstance(video_model_str, str):
                            video_model = json.loads(video_model_str)
                        else:
                            video_model = video_model_str

                        result["duration"] = video_model.get("video_duration")

                        video_list = video_model.get("video_list", {})
                        for vkey, vdata in video_list.items():
                            main_url_b64 = vdata.get("main_url", "")
                            if main_url_b64:
                                result["audio_url"] = _decode_base64_url(main_url_b64)
                                break
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        logger.warning(f"[Music] Failed to parse video_model: {e}")

                break

    return result


async def start_music_generation(prompt: str, conversation_id: str = "0",
                                  style: str = "", mood: str = "", voice: str = "",
                                  lyric: str = ""):
    account = cookie_pool.get_next()
    task_id = f"music-{uuid.uuid4().hex[:12]}"

    MUSIC_TASKS[task_id] = {
        "task_id": task_id,
        "prompt": prompt,
        "style": style,
        "mood": mood,
        "voice": voice,
        "lyric": lyric,
        "status": "generating",
        "created_at": time.time(),
        "conversation_id": conversation_id,
        "music_id": None,
        "audio_url": None,
        "cover_url": None,
        "title": None,
        "duration": None,
        "generated_lyric": None,
        "error": None,
        "account": account
    }

    asyncio.create_task(_run_music_generation(
        task_id, prompt, conversation_id, account,
        style=style, mood=mood, voice=voice, lyric=lyric
    ))

    return {
        "task_id": task_id,
        "status": "generating",
        "prompt": prompt,
        "style": style,
        "mood": mood,
        "voice": voice
    }


async def _run_music_generation(task_id: str, prompt: str, conversation_id: str,
                                 account: dict, style: str = "", mood: str = "",
                                 voice: str = "", lyric: str = ""):
    try:
        await _run_music_generation_api(task_id, prompt, conversation_id, account, style, mood, voice, lyric)
        task = MUSIC_TASKS.get(task_id)
        if task and task["status"] == "failed" and "No content returned" in (task.get("error") or ""):
            cookie_pool.report_fail(account, "Empty SSE response")
            next_account = cookie_pool.get_next()
            if next_account.get("cookie") != account.get("cookie"):
                logger.info(f"[Music] Retrying with account: {next_account.get('name', 'unknown')}")
                MUSIC_TASKS[task_id]["status"] = "generating"
                MUSIC_TASKS[task_id]["error"] = None
                await _run_music_generation_api(task_id, prompt, conversation_id, next_account, style, mood, voice, lyric)
                task = MUSIC_TASKS.get(task_id)
                if task and task["status"] == "completed":
                    cookie_pool.report_success(next_account)
                elif task and task["status"] == "failed":
                    cookie_pool.report_fail(next_account, task.get("error", ""))
            else:
                logger.info("[Music] No alternative account available, trying Playwright")
                try:
                    MUSIC_TASKS[task_id]["status"] = "generating"
                    MUSIC_TASKS[task_id]["error"] = None
                    await _run_music_generation_playwright(task_id, prompt, style, mood, voice, lyric)
                except Exception as pw_err:
                    logger.error(f"[Music] Playwright fallback also failed: {pw_err}")
                    MUSIC_TASKS[task_id]["status"] = "failed"
                    MUSIC_TASKS[task_id]["error"] = f"API empty response; Playwright: {str(pw_err)}"
    except Exception as e:
        err_str = str(e)
        cookie_pool.report_fail(account, err_str)
        if "rate limited" in err_str or "block" in err_str or "710022" in err_str:
            logger.info(f"[Music] API rate limited, trying next account or Playwright: {e}")
            next_account = cookie_pool.get_next()
            if next_account.get("cookie") != account.get("cookie"):
                try:
                    MUSIC_TASKS[task_id]["status"] = "generating"
                    MUSIC_TASKS[task_id]["error"] = None
                    await _run_music_generation_api(task_id, prompt, conversation_id, next_account, style, mood, voice, lyric)
                    task = MUSIC_TASKS.get(task_id)
                    if task and task["status"] == "completed":
                        cookie_pool.report_success(next_account)
                except Exception as e2:
                    cookie_pool.report_fail(next_account, str(e2))
                    MUSIC_TASKS[task_id]["status"] = "failed"
                    MUSIC_TASKS[task_id]["error"] = f"API: {err_str}; Retry: {str(e2)}"
            else:
                try:
                    MUSIC_TASKS[task_id]["status"] = "generating"
                    MUSIC_TASKS[task_id]["error"] = None
                    await _run_music_generation_playwright(task_id, prompt, style, mood, voice, lyric)
                except Exception as pw_err:
                    logger.error(f"[Music] Playwright fallback also failed: {pw_err}")
                    MUSIC_TASKS[task_id]["status"] = "failed"
                    MUSIC_TASKS[task_id]["error"] = f"API: {err_str}; Playwright: {str(pw_err)}"
        else:
            logger.error(f"[Music] Generation failed: {e}")
            MUSIC_TASKS[task_id]["status"] = "failed"
            MUSIC_TASKS[task_id]["error"] = str(e)


async def _run_music_generation_api(task_id: str, prompt: str, conversation_id: str,
                                     account: dict, style: str = "", mood: str = "",
                                     voice: str = "", lyric: str = ""):
    params = build_url_params(account)
    url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"
    headers = build_headers(account)
    headers['referer'] = 'https://www.doubao.com/chat/music'
    body = build_music_request_body(prompt, conversation_id, style, mood, voice, lyric)

    logger.info(f"[Music] Starting generation via API: prompt={prompt}, task_id={task_id}")

    full_text = ""
    audio_url = None
    cover_url = None
    title = None
    duration = None
    vid = None
    real_conv_id = conversation_id

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=300)) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise Exception(f"Samantha API returned {resp.status}: {err_text[:300]}")

            async for raw_line in resp.content:
                try:
                    line = raw_line.decode('utf-8', errors='replace').strip()
                except:
                    continue

                if not line:
                    continue

                if line.startswith('data:'):
                    data_str = line[5:].strip()
                    if data_str == '[DONE]':
                        break
                    try:
                        outer = json.loads(data_str)
                        event_type = outer.get("event_type")
                        event_data_raw = outer.get("event_data", "")

                        if isinstance(event_data_raw, str) and event_data_raw:
                            try:
                                event_data = json.loads(event_data_raw)
                            except json.JSONDecodeError:
                                event_data = {}
                        else:
                            event_data = event_data_raw if isinstance(event_data_raw, dict) else {}

                        conv_id = event_data.get("conversation_id")
                        if conv_id:
                            real_conv_id = conv_id

                        if event_type == 2001:
                            msg = event_data.get("message", {})
                            ct = msg.get("content_type")
                            raw_content = msg.get("content", "")

                            if ct == 2006 and isinstance(raw_content, str):
                                try:
                                    ct_content = json.loads(raw_content)
                                    extracted = _extract_music_from_content(ct_content)
                                    if extracted.get("title"):
                                        title = extracted["title"]
                                    if extracted.get("lyric"):
                                        full_text = extracted["lyric"]
                                    if extracted.get("audio_url"):
                                        audio_url = extracted["audio_url"]
                                    if extracted.get("cover_url"):
                                        cover_url = extracted["cover_url"]
                                    if extracted.get("duration"):
                                        try:
                                            duration = int(float(extracted["duration"]))
                                        except:
                                            duration = None
                                    if extracted.get("vid"):
                                        vid = extracted["vid"]
                                    logger.info(f"[Music] ct=2006: title={title}, has_audio={bool(audio_url)}, vid={vid}")
                                except (json.JSONDecodeError, KeyError, TypeError) as e:
                                    logger.warning(f"[Music] Failed to parse ct=2006: {e}")

                            elif ct in (10000, 2001) and isinstance(raw_content, str):
                                try:
                                    parsed = json.loads(raw_content)
                                    text = parsed.get("text", "")
                                    if text:
                                        full_text += text
                                except json.JSONDecodeError:
                                    if raw_content:
                                        full_text += raw_content

                            elif ct not in (10000, 2001, 2006):
                                logger.info(f"[Music] Unknown content_type={ct}: {str(raw_content)[:200]}")

                        elif event_type == 2005:
                            error_msg = event_data.get("message", "")
                            error_code = event_data.get("code", "")
                            logger.error(f"[Music] Error 2005: code={error_code} msg={error_msg}")
                            raise Exception(f"Music generation error: code={error_code} msg={error_msg}")

                    except json.JSONDecodeError:
                        pass

    MUSIC_TASKS[task_id]["generated_lyric"] = full_text
    MUSIC_TASKS[task_id]["conversation_id"] = real_conv_id
    MUSIC_TASKS[task_id]["title"] = title
    MUSIC_TASKS[task_id]["cover_url"] = cover_url

    if audio_url:
        MUSIC_TASKS[task_id]["audio_url"] = audio_url
        MUSIC_TASKS[task_id]["duration"] = duration
        MUSIC_TASKS[task_id]["vid"] = vid
        MUSIC_TASKS[task_id]["status"] = "completed"
        logger.info(f"[Music] Generation completed: task_id={task_id}, title={title}, duration={duration}s")
        asyncio.create_task(_download_audio_to_local(task_id, audio_url, account))
    elif full_text:
        MUSIC_TASKS[task_id]["status"] = "lyrics_ready"
        logger.info(f"[Music] Lyrics generated but no audio yet. Lyrics length: {len(full_text)}")
    else:
        MUSIC_TASKS[task_id]["status"] = "failed"
        MUSIC_TASKS[task_id]["error"] = "No content returned from music generation"


async def _run_music_generation_playwright(task_id: str, prompt: str,
                                            style: str = "", mood: str = "",
                                            voice: str = "", lyric: str = ""):
    async with PW_LOCK:
        logger.info(f"[Music] Starting generation via Playwright: task_id={task_id}")

        from playwright.async_api import async_playwright

        cookie_str = CONFIG.get('cookie', '')
        cookies = []
        for part in cookie_str.split(';'):
            part = part.strip()
            if '=' in part:
                name, value = part.split('=', 1)
                cookies.append({
                    'name': name.strip(),
                    'value': value.strip(),
                    'domain': '.doubao.com',
                    'path': '/'
                })

        text = prompt if prompt else "我想创作一首歌曲"
        if style or mood or voice:
            text = f"我想创作一首歌曲，用AI 帮我写歌词。这首歌是{style or '流行'}音乐风格，传达{mood or '快乐'}的情绪，使用{voice or '女声'} 音色。"

        sse_response_body = None

        try:
            from cloakbrowser import async_launch
            browser = await async_launch(headless=True, humanize=True)
            logger.info("[Music] Using CloakBrowser for stealth browsing")
        except ImportError:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                channel="msedge",
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
            )
            logger.info("[Music] CloakBrowser not available, using Playwright")

        context = await browser.new_context(
            viewport={'width': 1280, 'height': 900},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'
        )
        await context.add_cookies(cookies)

        page = await context.new_page()

        async def handle_sse(route):
            nonlocal sse_response_body
            response = await route.fetch()
            body = await response.text()
            sse_response_body = body
            logger.info(f"[Music-PW] SSE captured: {len(body)} chars")
            await route.fulfill(response=response)

        await page.route('**/samantha/chat/completion**', handle_sse)

        await page.goto('https://www.doubao.com/chat/music', wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(8)

        current_url = page.url
        if 'login' in current_url.lower() or 'passport' in current_url.lower():
            await browser.close()
            raise Exception("Need login in Playwright session")

        textarea = await page.query_selector('[contenteditable="true"]')
        if not textarea:
            textarea = await page.query_selector('textarea')
        if not textarea:
            await browser.close()
            raise Exception("No input found in Playwright session")

        await textarea.click()
        await asyncio.sleep(0.5)
        await textarea.fill(text)
        await asyncio.sleep(1)

        send_btn = None
        for selector in ['[class*="send"]', 'button[type="submit"]']:
            send_btn = await page.query_selector(selector)
            if send_btn:
                is_visible = await send_btn.is_visible()
                if is_visible:
                    break
                send_btn = None

        if send_btn:
            await send_btn.click()
        else:
            await textarea.press('Enter')

        logger.info(f"[Music-PW] Message sent, waiting for SSE...")

        for i in range(60):
            await asyncio.sleep(3)
            if sse_response_body:
                break

        await browser.close()

        if not sse_response_body:
            raise Exception("No SSE response captured via Playwright")

        full_text = ""
        audio_url = None
        cover_url = None
        title = None
        duration = None
        vid = None

        for line in sse_response_body.split('\n'):
            line = line.strip()
            if not line.startswith('data:'):
                continue
            data_str = line[5:].strip()
            if data_str == '[DONE]':
                break
            try:
                outer = json.loads(data_str)
                et = outer.get("event_type")
                ed_raw = outer.get("event_data", "")
                ed = json.loads(ed_raw) if isinstance(ed_raw, str) and ed_raw else (ed_raw if isinstance(ed_raw, dict) else {})

                if et == 2001:
                    msg = ed.get("message", {})
                    ct = msg.get("content_type")
                    raw_content = msg.get("content", "")
                    if ct == 2006 and isinstance(raw_content, str):
                        ct_content = json.loads(raw_content)
                        extracted = _extract_music_from_content(ct_content)
                        if extracted.get("title"):
                            title = extracted["title"]
                        if extracted.get("lyric"):
                            full_text = extracted["lyric"]
                        if extracted.get("audio_url"):
                            audio_url = extracted["audio_url"]
                        if extracted.get("cover_url"):
                            cover_url = extracted["cover_url"]
                        if extracted.get("duration"):
                            try:
                                duration = int(float(extracted["duration"]))
                            except:
                                duration = None
                        if extracted.get("vid"):
                            vid = extracted["vid"]
                    elif ct in (10000, 2001) and isinstance(raw_content, str):
                        try:
                            parsed = json.loads(raw_content)
                            text_chunk = parsed.get("text", "")
                            if text_chunk:
                                full_text += text_chunk
                        except:
                            pass
                elif et == 2005:
                    error_code = ed.get("code", "")
                    error_msg = ed.get("message", "")
                    raise Exception(f"Playwright music error: code={error_code} msg={error_msg}")
            except json.JSONDecodeError:
                pass

        MUSIC_TASKS[task_id]["generated_lyric"] = full_text
        MUSIC_TASKS[task_id]["title"] = title
        MUSIC_TASKS[task_id]["cover_url"] = cover_url

        if audio_url:
            MUSIC_TASKS[task_id]["audio_url"] = audio_url
            MUSIC_TASKS[task_id]["duration"] = duration
            MUSIC_TASKS[task_id]["vid"] = vid
            MUSIC_TASKS[task_id]["status"] = "completed"
            logger.info(f"[Music-PW] Completed: task_id={task_id}, title={title}, duration={duration}s")
            account = {"cookie": cookie_str}
            asyncio.create_task(_download_audio_to_local(task_id, audio_url, account))
        elif full_text:
            MUSIC_TASKS[task_id]["status"] = "lyrics_ready"
        else:
            MUSIC_TASKS[task_id]["status"] = "failed"
            MUSIC_TASKS[task_id]["error"] = "No music content in Playwright SSE response"


async def get_music_status(task_id: str):
    task = MUSIC_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found", "task_id": task_id}

    return {
        "task_id": task["task_id"],
        "prompt": task["prompt"],
        "status": task["status"],
        "style": task.get("style"),
        "mood": task.get("mood"),
        "voice": task.get("voice"),
        "title": task.get("title"),
        "cover_url": task.get("cover_url"),
        "audio_url": task.get("audio_url"),
        "duration": task.get("duration"),
        "lyrics_length": len(task.get("generated_lyric", "")) if task.get("generated_lyric") else 0,
        "error": task.get("error"),
        "created_at": task.get("created_at")
    }


async def get_music_lyric(task_id: str):
    task = MUSIC_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}
    return {
        "task_id": task_id,
        "prompt": task["prompt"],
        "status": task["status"],
        "lyric": task.get("generated_lyric", ""),
        "title": task.get("title")
    }


async def get_music_audio(task_id: str):
    task = MUSIC_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}

    if task["status"] not in ("completed", "lyrics_ready"):
        return {"error": f"Music not ready, current status: {task['status']}"}

    audio_url = task.get("audio_url")
    if audio_url:
        return {
            "task_id": task_id,
            "audio_url": audio_url,
            "title": task.get("title"),
            "duration": task.get("duration"),
            "cover_url": task.get("cover_url")
        }

    return {"error": "Audio not available yet. Music is still being generated.", "status": task["status"]}


async def list_music():
    tasks = []
    for task_id, task in MUSIC_TASKS.items():
        tasks.append({
            "task_id": task_id,
            "prompt": task["prompt"],
            "status": task["status"],
            "style": task.get("style"),
            "mood": task.get("mood"),
            "title": task.get("title"),
            "duration": task.get("duration"),
            "lyrics_length": len(task.get("generated_lyric", "")) if task.get("generated_lyric") else 0,
            "created_at": task.get("created_at")
        })
    tasks.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"tasks": tasks}


async def get_music_styles():
    return {
        "styles": MUSIC_STYLES,
        "moods": MUSIC_MOODS,
        "voices": MUSIC_VOICES
    }
