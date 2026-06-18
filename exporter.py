import json
import os
import asyncio
import logging
import time
import base64
from typing import Optional
from urllib.parse import unquote

import aiohttp

from config import CONFIG, USER_AGENT, cookie_pool, BASE_DIR

logger = logging.getLogger("webchat-api")

EXPORT_DIR = os.path.join(BASE_DIR, "exports")
MEDIA_DIR = os.path.join(EXPORT_DIR, "media")
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

PW_EXPORT_LOCK = asyncio.Lock()

_USER_INFO_CACHE = {}
_USER_INFO_CACHE_TIME = 0

_USE_CLOAK = None


async def _launch_browser():
    global _USE_CLOAK
    if _USE_CLOAK is None:
        try:
            from cloakbrowser import async_launch
            _USE_CLOAK = True
            logger.info("[Export] CloakBrowser available, using stealth mode")
        except ImportError:
            _USE_CLOAK = False
            logger.info("[Export] CloakBrowser not available, using Playwright")

    if _USE_CLOAK:
        from cloakbrowser import async_launch
        return await async_launch(headless=True, humanize=True)
    else:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        return await pw.chromium.launch(
            headless=True,
            channel="msedge",
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )


async def _create_context(browser, cookie_str: str):
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

    context = await browser.new_context(
        viewport={'width': 1280, 'height': 900},
        user_agent=USER_AGENT
    )
    await context.add_cookies(cookies)
    return context


def _get_valid_account():
    import time as _time

    best_account = None
    best_expiry = 0

    for acc in cookie_pool.accounts:
        cookie = acc.get('cookie', '')
        if not cookie:
            continue
        if 'sessionid' not in cookie and 'session_id' not in cookie:
            continue

        sid_guard = ''
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith('sid_guard='):
                sid_guard = unquote(part.split('=', 1)[1])
                break

        if sid_guard:
            try:
                parts = sid_guard.split('|')
                if len(parts) >= 2:
                    created = int(parts[1])
                    ttl = int(parts[2]) if len(parts) >= 3 else 2592000
                    expiry = created + ttl
                    if expiry > _time.time() and expiry > best_expiry:
                        best_expiry = expiry
                        best_account = acc
            except:
                if not best_account:
                    best_account = acc
        else:
            if not best_account:
                best_account = acc

    return best_account or cookie_pool.get_next()


def _parse_im_message(msg: dict) -> Optional[dict]:
    content_type = msg.get("content_type", 0)
    raw_content = msg.get("content", "")
    sender_id = msg.get("sender_id", "")
    user_type = msg.get("user_type", 1)
    message_id = msg.get("message_id", "")
    create_time = msg.get("create_time", "0")
    content_blocks = msg.get("content_block", [])

    try:
        ct = int(create_time) if isinstance(create_time, str) else create_time
    except:
        ct = 0

    result = {
        "message_id": message_id,
        "role": "user" if user_type == 1 else "assistant",
        "content_type": content_type,
        "created_at": ct,
        "sender_id": sender_id,
        "text": "",
        "images": [],
        "audio_url": None,
        "video_url": None,
        "raw_content": None
    }

    if content_type == 9999 and content_blocks:
        texts = []
        thinking_text = ""
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("block_type", 0)
            block_content = block.get("content", {})

            if block_type == 10000:
                tb = block_content.get("text_block", {})
                t = tb.get("text", "")
                if t:
                    texts.append(t)

            elif block_type == 10040:
                tb = block_content.get("thinking_block", {})
                st = tb.get("status", 0)
                if st == 2:
                    thinking_text = tb.get("thinking_content", "")
                    if thinking_text and texts:
                        texts.insert(0, f"💭 思考过程:\n{thinking_text}\n\n---\n")

            elif block_type == 10052:
                ab = block_content.get("attachment_block", {})
                attachments = ab.get("attachments", [])
                for att in attachments:
                    att_type = att.get("type", 0)
                    if att_type == 1:
                        img_info = att.get("image", {})
                        if isinstance(img_info, dict):
                            img_ori = img_info.get("image_ori", {})
                            url = img_ori.get("url", "") if isinstance(img_ori, dict) else ""
                            if url:
                                result["images"].append(url)
                    elif att_type == 3:
                        file_info = att.get("file", {})
                        if isinstance(file_info, dict):
                            fname = file_info.get("name", "")
                            furi = file_info.get("uri", "")
                            if fname:
                                texts.append(f"📎 附件: {fname}")

            elif block_type == 10056:
                rb = block_content.get("reference_block", {})
                ref_type = rb.get("type", 0)
                if ref_type == 3:
                    file_info = rb.get("file", {})
                    if isinstance(file_info, dict):
                        fname = file_info.get("name", "")
                        if fname:
                            texts.append(f"📎 引用文件: {fname}")
                elif ref_type == 1:
                    url_info = rb.get("url", {})
                    if isinstance(url_info, dict):
                        link = url_info.get("url", "")
                        title = url_info.get("title", "")
                        if link:
                            texts.append(f"🔗 [{title}]({link})")

            elif block_type == 2074:
                cb = block_content.get("creation_block", block_content)
                creations = cb.get("creations", [])
                for creation in creations:
                    img_raw = creation.get("image_raw", {})
                    if isinstance(img_raw, dict):
                        url = img_raw.get("url", "")
                        if url:
                            result["images"].append(url)

        result["text"] = "\n".join(texts)
        return result

    if isinstance(raw_content, str) and raw_content:
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            result["text"] = raw_content
            return result
    elif isinstance(raw_content, dict):
        parsed = raw_content
    else:
        return None

    if content_type == 1:
        result["text"] = parsed.get("text", "")

    elif content_type == 72:
        result["raw_content"] = raw_content
        task_nums = parsed.get("task_nums", 0)
        for i in range(task_nums):
            task_key = str(i)
            task = parsed.get(task_key, {})
            if isinstance(task, dict):
                result["text"] = task.get("title", "")
                lyric = task.get("lyric", "")
                if lyric:
                    result["text"] += f"\n\n{lyric}"
                chunk_uri = task.get("chunk_uri", "")
                if chunk_uri:
                    result["audio_url"] = f"https://www.doubao.com{chunk_uri}"
            break

    elif content_type in (10000, 2001):
        result["text"] = parsed.get("text", "")
        images = parsed.get("images", [])
        for img in images:
            if isinstance(img, dict):
                url = img.get("url", img.get("image_ori", {}).get("url", ""))
                if url:
                    result["images"].append(url)
            elif isinstance(img, str):
                result["images"].append(img)

    elif content_type == 2006:
        result["raw_content"] = raw_content
        tasks = parsed.get("tasks", {})
        for key, task in tasks.items():
            if isinstance(task, dict):
                result["text"] = task.get("title", "")
                result["audio_url"] = _extract_audio_from_music(task)
                cover = task.get("cover", {})
                if cover:
                    img_ori = cover.get("image_ori", {})
                    if img_ori and img_ori.get("url"):
                        result["images"].append(img_ori["url"])
                lyric = task.get("lyric", "")
                if lyric:
                    result["text"] += f"\n\n{lyric}"
            break

    else:
        if isinstance(raw_content, str):
            result["raw_content"] = raw_content
            try:
                inner = json.loads(raw_content)
                result["text"] = inner.get("text", inner.get("title", ""))
                images = inner.get("images", [])
                for img in images:
                    if isinstance(img, dict):
                        url = img.get("url", img.get("image_ori", {}).get("url", ""))
                        if url:
                            result["images"].append(url)
                    elif isinstance(img, str):
                        result["images"].append(img)
            except:
                result["text"] = raw_content[:500]
        else:
            result["raw_content"] = json.dumps(raw_content, ensure_ascii=False)

    return result


def _extract_audio_from_music(task: dict) -> str:
    video_model_str = task.get("video_model")
    if not video_model_str:
        return ""
    try:
        if isinstance(video_model_str, str):
            video_model = json.loads(video_model_str)
        else:
            video_model = video_model_str
        video_list = video_model.get("video_list", {})
        for vkey, vdata in video_list.items():
            main_url_b64 = vdata.get("main_url", "")
            if main_url_b64:
                try:
                    return base64.b64decode(main_url_b64).decode('utf-8')
                except:
                    return main_url_b64
    except:
        pass
    return ""


async def fetch_user_info() -> dict:
    global _USER_INFO_CACHE, _USER_INFO_CACHE_TIME
    if _USER_INFO_CACHE and time.time() - _USER_INFO_CACHE_TIME < 300:
        return _USER_INFO_CACHE

    try:
        from browser_client import browser_client
        user_info = await browser_client.get_user_info()
        if user_info:
            _USER_INFO_CACHE = user_info
            _USER_INFO_CACHE_TIME = time.time()
        return user_info
    except Exception as e:
        logger.error(f"[User] Failed to fetch user info: {e}")
        return {}


async def fetch_conversation_list() -> list:
    account = _get_valid_account()
    cookie_str = account.get('cookie', CONFIG.get('cookie', ''))

    if 'sessionid' not in cookie_str and 'session_id' not in cookie_str:
        return []

    try:
        async with PW_EXPORT_LOCK:
            browser = await _launch_browser()
            conversations = []

            context = await _create_context(browser, cookie_str)
            page = await context.new_page()

            async def capture(route):
                nonlocal conversations
                url = route.request.url
                response = await route.fetch()
                try:
                    body = await response.json()
                    if '/im/chain/recent_conv' in url:
                        dl = body.get("downlink_body", {})
                        rc = dl.get("pull_recent_conv_chain_downlink_body", {})
                        cells = rc.get("cells", [])
                        for cell in cells:
                            conv = cell.get("conversation", {})
                            conv_id = conv.get("conversation_id", "")
                            if conv_id and not any(c["id"] == conv_id for c in conversations):
                                conversations.append({
                                    "id": conv_id,
                                    "title": conv.get("name", ""),
                                    "updated_at": int(conv.get("conv_version", "0")),
                                    "created_at": 0,
                                    "message_count": 0,
                                    "bot_type": conv.get("bot_type", 0),
                                })
                except:
                    pass
                await route.fulfill(response=response)

            await page.route('**/im/**', capture)
            await page.goto('https://www.doubao.com/chat/', wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(10)
            await browser.close()

        if not conversations:
            logger.info("[Export] IM API returned no conversations, trying DOM extraction")
            conversations = await _fetch_conv_list_from_dom(cookie_str)

        return conversations

    except Exception as e:
        logger.error(f"[Export] Failed to fetch conversation list: {e}")
        return []


async def _fetch_conv_list_from_dom(cookie_str: str) -> list:
    try:
        browser = await _launch_browser()
        conversations = []
        context = await _create_context(browser, cookie_str)
        page = await context.new_page()

        await page.goto('https://www.doubao.com/chat/', wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(8)

        dom_convs = await page.evaluate('''() => {
            const result = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href*="/chat/"]');
            for (const link of links) {
                const href = link.getAttribute('href') || '';
                const match = href.match(/\\/chat\\/([0-9]+)/);
                if (match && !seen.has(match[1])) {
                    seen.add(match[1]);
                    const text = link.textContent?.trim() || '';
                    if (text && text.length < 100 && text !== '豆包') {
                        result.push({id: match[1], title: text, updated_at: 0, created_at: 0, message_count: 0});
                    }
                }
            }
            return result;
        }''')
        if dom_convs:
            conversations = dom_convs
        await browser.close()
        return conversations
    except Exception as e:
        logger.error(f"[Export] DOM conv list extraction failed: {e}")
        return []


async def fetch_conversation_messages(conversation_id: str) -> dict:
    account = _get_valid_account()
    cookie_str = account.get('cookie', CONFIG.get('cookie', ''))

    if 'sessionid' not in cookie_str and 'session_id' not in cookie_str:
        return {"conversation_id": conversation_id, "messages": [], "message_count": 0, "exported_at": time.time()}

    try:
        async with PW_EXPORT_LOCK:
            browser = await _launch_browser()
            all_messages = []
            conv_title = ""

            context = await _create_context(browser, cookie_str)
            page = await context.new_page()

            async def capture(route):
                nonlocal all_messages, conv_title
                url = route.request.url
                response = await route.fetch()
                try:
                    body = await response.json()
                    if '/im/chain/single' in url:
                        dl = body.get("downlink_body", {})
                        sc = dl.get("pull_singe_chain_downlink_body", {})
                        messages = sc.get("messages", [])
                        for msg in messages:
                            parsed = _parse_im_message(msg)
                            if parsed:
                                all_messages.append(parsed)
                    elif '/im/conversation/info' in url:
                        dl = body.get("downlink_body", {})
                        ci = dl.get("get_conv_info_downlink_body", {})
                        conv_info = ci.get("conversation_info", {})
                        if conv_info.get("name"):
                            conv_title = conv_info["name"]
                except:
                    pass
                await route.fulfill(response=response)

            await page.route('**/im/**', capture)
            await page.route('**/samantha/**', capture)

            chat_url = f'https://www.doubao.com/chat/{conversation_id}'
            await page.goto(chat_url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(10)
            await browser.close()

        all_messages.sort(key=lambda m: m.get("created_at", 0))

        return {
            "conversation_id": conversation_id,
            "title": conv_title,
            "messages": all_messages,
            "message_count": len(all_messages),
            "exported_at": time.time()
        }

    except Exception as e:
        logger.error(f"[Export] Failed to fetch messages for {conversation_id}: {e}")
        return {"conversation_id": conversation_id, "messages": [], "message_count": 0, "exported_at": time.time(), "error": str(e)}


async def download_media(url: str, filename: str) -> Optional[str]:
    if not url:
        return None
    filepath = os.path.join(MEDIA_DIR, filename)
    if os.path.exists(filepath):
        return filepath

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    logger.info(f"[Export] Downloaded media: {filename} ({len(content)} bytes)")
                    return filepath
                else:
                    logger.warning(f"[Export] Failed to download {url}: HTTP {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"[Export] Download error for {url}: {e}")
        return None


async def export_conversation_full(conversation_id: str, download_media_files: bool = True) -> dict:
    conv_data = await fetch_conversation_messages(conversation_id)

    if download_media_files:
        conv_media_dir = os.path.join(MEDIA_DIR, conversation_id)
        os.makedirs(conv_media_dir, exist_ok=True)

        for msg in conv_data.get("messages", []):
            for i, img_url in enumerate(msg.get("images", [])):
                if img_url.startswith(("http://", "https://")):
                    ext = _get_ext_from_url(img_url, ".jpg")
                    filename = f"{msg.get('message_id', 'unknown')}_img{i}{ext}"
                    local_path = await download_media(img_url, os.path.join(conversation_id, filename))
                    if local_path:
                        msg["images"][i] = local_path

            if msg.get("audio_url") and msg["audio_url"].startswith(("http://", "https://")):
                ext = _get_ext_from_url(msg["audio_url"], ".mp3")
                filename = f"{msg.get('message_id', 'unknown')}_audio{ext}"
                local_path = await download_media(msg["audio_url"], os.path.join(conversation_id, filename))
                if local_path:
                    msg["audio_url"] = local_path

            if msg.get("video_url") and msg["video_url"].startswith(("http://", "https://")):
                ext = _get_ext_from_url(msg["video_url"], ".mp4")
                filename = f"{msg.get('message_id', 'unknown')}_video{ext}"
                local_path = await download_media(msg["video_url"], os.path.join(conversation_id, filename))
                if local_path:
                    msg["video_url"] = local_path

    export_path = os.path.join(EXPORT_DIR, f"{conversation_id}.json")
    with open(export_path, 'w', encoding='utf-8') as f:
        json.dump(conv_data, f, ensure_ascii=False, indent=2)

    logger.info(f"[Export] Conversation {conversation_id} exported to {export_path}")
    return conv_data


def _get_ext_from_url(url: str, default: str = "") -> str:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path
        if '.' in path.split('/')[-1]:
            ext = '.' + path.split('/')[-1].split('.')[-1].split('?')[0]
            if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp3', '.mp4', '.m4a', '.aac', '.wav'):
                return ext
    except:
        pass
    return default
