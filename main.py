import json
import os
import sys
import logging
import asyncio
import aiohttp
from datetime import datetime
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from config import (
    BASE_DIR, CONFIG, SIGN_METHOD, signer, cookie_pool,
    load_accounts, save_accounts, load_conversation_state,
    LOG_DIR, ACCOUNTS_PATH, rate_limiter, concurrency_limiter
)
from models import ChatCompletionRequest, AnthropicMessageRequest, MODEL_CONFIG
from openai_api import stream_chat_completion, non_stream_chat_completion, generate_images, delete_conversation
from anthropic_api import stream_anthropic_messages, non_stream_anthropic_messages
from podcast import start_podcast_generation, get_podcast_status, get_podcast_audio, get_podcast_script, list_podcasts, AUDIO_DIR
from music import start_music_generation, get_music_status, get_music_audio, get_music_lyric, list_music, get_music_styles
from exporter import fetch_user_info, fetch_conversation_list, export_conversation_full
from storage import init_db, save_conversation, list_conversations as db_list_conversations, get_conversation as db_get_conversation, save_message, get_messages as db_get_messages, delete_conversation as db_delete_conversation, search_conversations
from adapters import init_all as adapters_init_all, close_all as adapters_close_all, get_adapter, get_models as get_adapter_models

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("doubao-api")

async def refresh_qianwen_models():
    """异步刷新千问模型列表，失败时静默处理。"""
    try:
        from adapters.qianwen import refresh_qianwen_models as _refresh
        await _refresh()
    except Exception as e:
        logger.warning(f"Failed to refresh Qianwen models: {e}")


_cleanup_task = None
_qianwen_model_refresh_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global signer, SIGN_METHOD, _cleanup_task
    if SIGN_METHOD == 'b2' and signer:
        logger.info("Initializing B2 Playwright signer (this may take 30-60s)...")
        success = await signer.initialize()
        if success:
            logger.info("B2 Playwright signer initialized successfully")
        else:
            logger.error("B2 Playwright signer initialization failed, falling back to B3")
            SIGN_METHOD = 'b3'
    logger.info(f"Active sign method: {SIGN_METHOD}")
    _cleanup_task = asyncio.create_task(_auto_cleanup_task())
    await init_db()

    # 浏览器按需初始化：Doubao → ensure_doubao_ready(), Qianwen → ensure_qianwen_ready()
    # 千问模型列表启动时异步刷新，失败则使用默认模型列表
    global _qianwen_model_refresh_task
    _qianwen_model_refresh_task = asyncio.create_task(refresh_qianwen_models())
    logger.info("Browser clients will be initialized on-demand (lazy loading)")

    yield
    if not CONFIG.get('_keep_conversations', False):
        logger.info("Cleaning up conversation history before shutdown...")
        try:
            from storage import clear_all_conversations
            await clear_all_conversations()
            logger.info("SQLite database conversations cleared")
        except Exception as e:
            logger.warning(f"Failed to clear SQLite conversations: {e}")

        try:
            import glob
            conv_dir = os.path.join(BASE_DIR, "conversations")
            if os.path.exists(conv_dir):
                doubao_conv_ids = []
                for f in glob.glob(os.path.join(conv_dir, "*.json")):
                    try:
                        with open(f, 'r', encoding='utf-8') as fh:
                            state = json.load(fh)
                        conv_id = state.get("doubao_conversation_id", "")
                        if conv_id and conv_id != "0":
                            doubao_conv_ids.append(conv_id)
                        os.remove(f)
                    except Exception as e:
                        logger.warning(f"Failed to process conversation file {f}: {e}")
                if doubao_conv_ids:
                    from browser_client import browser_client
                    await browser_client.delete_doubao_conversations(doubao_conv_ids)
                    logger.info(f"Deleted {len(doubao_conv_ids)} Doubao conversations from server")
                logger.info("Conversation JSON files removed")
        except Exception as e:
            logger.warning(f"Failed to clean conversation files: {e}")

        try:
            from browser_client import browser_client
            await browser_client.delete_all_qianwen_conversations()
            logger.info("Qianwen conversations deleted from server")
        except Exception as e:
            logger.warning(f"Failed to delete Qianwen conversations: {e}")

    if _cleanup_task:
        _cleanup_task.cancel()
    if signer:
        await signer.close()
    try:
        from browser_client import browser_client
        await browser_client.close()
    except Exception:
        pass
    await adapters_close_all()

app = FastAPI(title="Doubao Free API", version="3.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path

    if path.startswith("/v1/") and not rate_limiter.is_allowed(client_ip):
        logger.warning(f"Rate limit exceeded for {client_ip} on {path}")
        return JSONResponse(
            status_code=429,
            content={
                "error": {"message": "Rate limit exceeded. Please slow down.", "type": "rate_limit_error", "code": 429}
            },
            headers={"Retry-After": str(rate_limiter.get_status(client_ip).get("reset_at", 60))}
        )

    if path in ("/v1/chat/completions", "/v1/messages", "/v1/images/generations", "/v1/podcast/generate"):
        await concurrency_limiter.acquire()
        try:
            response = await call_next(request)
            return response
        finally:
            concurrency_limiter.release()

    return await call_next(request)


async def _delete_adapter_conversation(adapter):
    try:
        adapter_name = adapter.get_adapter_name()
        if adapter_name == 'doubao':
            conv_id = getattr(adapter, '_last_conversation_id', '')
            if conv_id and conv_id != '0':
                from openai_api import delete_conversation
                await delete_conversation(conv_id)
                logger.info(f"[Cleanup] deleted doubao conversation {conv_id}")
        elif adapter_name == 'qianwen':
            session_id = getattr(adapter, '_last_session_id', '')
            if session_id:
                from browser_client import browser_client
                await browser_client.delete_qianwen_conversation(session_id)
                logger.info(f"[Cleanup] deleted qianwen session {session_id}")
            adapter._last_session_id = ""
    except Exception as e:
        logger.warning(f"[Cleanup] failed to delete conversation: {e}")


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    adapter = get_adapter(request.model)
    if request.stream:
        async def stream_with_cleanup():
            async for chunk in adapter.stream_chat(request):
                yield chunk
            await _delete_adapter_conversation(adapter)
        return StreamingResponse(
            stream_with_cleanup(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        result = await adapter.non_stream_chat(request)
        await _delete_adapter_conversation(adapter)
        return JSONResponse(content=result)

@app.post("/v1/messages")
async def anthropic_messages(request: AnthropicMessageRequest):
    if request.stream:
        return StreamingResponse(
            stream_anthropic_messages(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        result = await non_stream_anthropic_messages(request)
        return JSONResponse(content=result)

@app.get("/v1/models")
async def list_models():
    adapter_models = get_adapter_models()
    models = []
    for model_id, cfg in adapter_models.items():
        models.append({
            "id": model_id,
            "object": "model",
            "owned_by": "doubao",
            "description": cfg.get("desc", ""),
            "capabilities": {
                "vision": True,
                "deep_think": cfg.get("use_deep_think", False),
                "auto_cot": cfg.get("use_auto_cot", False)
            }
        })
    return {"object": "list", "data": models}

@app.post("/v1/images/upload")
async def upload_image_endpoint(file: UploadFile = File(...)):
    from uploader import upload_image

    account = cookie_pool.get_next()
    file_data = await file.read()
    file_name = file.filename or "upload.png"

    try:
        attachment = await upload_image(
            file_data=file_data,
            file_name=file_name,
            cookie=account.get('cookie', CONFIG.get('cookie', '')),
            device_id=account.get('device_id', ''),
            tea_uuid=account.get('tea_uuid', ''),
            web_id=account.get('web_id', '')
        )
        return {"status": "ok", "attachment": attachment}
    except Exception as e:
        logger.error(f"Image upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/images/generations")
async def generate_images_endpoint(request: Request):
    """
    OpenAI 兼容的图片生成 API
    请求体示例: { "prompt": "一只可爱的小猫", "n": 1, "size": "1024x1024" }
    """
    try:
        body = await request.json()
        prompt = body.get("prompt", "")
        n = body.get("n", 1)
        size = body.get("size", "1024x1024")
        
        if not prompt:
            raise HTTPException(status_code=400, detail="Missing 'prompt' is required")
        
        result = await generate_images(prompt, n=n, size=size)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations")
async def api_list_conversations():
    convs = await db_list_conversations()
    return JSONResponse(content={"conversations": convs})

@app.get("/api/conversations/{conv_id}")
async def api_get_conversation(conv_id: str):
    conv = await db_get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await db_get_messages(conv_id)
    return JSONResponse(content={"conversation": conv, "messages": messages})

@app.post("/api/conversations")
async def api_create_conversation(request: Request):
    data = await request.json()
    conv_id = data.get("id", "")
    title = data.get("title", "")
    model = data.get("model", "")
    if not conv_id:
        raise HTTPException(status_code=400, detail="id is required")
    conv = await save_conversation(conv_id, title, model)
    return JSONResponse(content=conv)

@app.post("/api/conversations/{conv_id}/messages")
async def api_save_message(conv_id: str, request: Request):
    data = await request.json()
    role = data.get("role", "")
    content = data.get("content", "")
    model = data.get("model", "")
    msg_id = data.get("id", None)
    msg = await save_message(msg_id, conv_id, role, content, model)
    return JSONResponse(content=msg)

@app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: str):
    ok = await db_delete_conversation(conv_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(content={"deleted": True})

ALLOWED_AUDIO_DOMAINS = [
    "douyinvod.com", "byteimg.com", "bytedance.com", "bdurl.net",
    "bytegecko.com", "bdemc.com", "tiktokcdn.com", "volcengine.com",
    "douyin.com", "ibytedtos.com", "bytevcloud.com", "tosv.org",
]

def _is_allowed_audio_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return any(host == d or host.endswith("." + d) for d in ALLOWED_AUDIO_DOMAINS)
    except Exception:
        return False

@app.get("/api/proxy/audio")
async def proxy_audio(url: str = "", task_id: str = ""):
    if not url and not task_id:
        raise HTTPException(status_code=400, detail="url or task_id is required")
    if task_id and not url:
        from music import MUSIC_TASKS
        task = MUSIC_TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        url = task.get("audio_url", "")
        if not url:
            raise HTTPException(status_code=404, detail="Audio not available")
    elif not url:
        raise HTTPException(status_code=400, detail="url or task_id is required")
    
    if url and not _is_allowed_audio_url(url):
        raise HTTPException(status_code=403, detail="URL domain not allowed")
    
    media_dir = os.path.join(BASE_DIR, "media", "audio")
    os.makedirs(media_dir, exist_ok=True)
    
    local_path = ""
    if task_id:
        task_local = os.path.join(media_dir, f"{task_id}.mp3")
        if os.path.exists(task_local):
            local_path = task_local
    
    if not local_path:
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
        hash_local = os.path.join(media_dir, f"{url_hash}.mp3")
        if os.path.exists(hash_local):
            local_path = hash_local
    
    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            body = f.read()
        return Response(content=body, media_type="audio/mpeg",
                        headers={"Content-Disposition": "inline",
                                 "Accept-Ranges": "bytes",
                                 "Content-Length": str(len(body)),
                                 "Cache-Control": "public, max-age=86400"})
    
    account = cookie_pool.get_next()
    cookie_str = account.get("cookie", CONFIG.get("cookie", ""))
    req_headers = {
        "User-Agent": "python-requests/2.31.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120),
                                   headers=req_headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=502, detail=f"Upstream returned {resp.status}")
                content_type = resp.headers.get("Content-Type", "audio/mpeg")
                body = await resp.read()
                with open(local_path, "wb") as f:
                    f.write(body)
                logger.info(f"Cached audio: {local_path} ({len(body)} bytes)")
                return Response(content=body, media_type=content_type,
                                headers={"Content-Disposition": "inline",
                                         "Accept-Ranges": "bytes",
                                         "Content-Length": str(len(body)),
                                         "Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Audio proxy error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/proxy/download/{task_id}")
async def proxy_download_music(task_id: str):
    from music import MUSIC_TASKS
    task = MUSIC_TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    url = task.get("audio_url", "")
    if not url:
        raise HTTPException(status_code=404, detail="Audio not available")
    title = task.get("title") or task.get("prompt", "music")
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-")[:50]
    if not safe_title:
        safe_title = "music"
    filename = f"{safe_title}.mp3"
    from urllib.parse import quote
    encoded_filename = quote(filename)
    
    local_path = ""
    task_local = os.path.join(BASE_DIR, "media", "audio", f"{task_id}.mp3")
    if os.path.exists(task_local):
        local_path = task_local
    
    if not local_path:
        media_dir = os.path.join(BASE_DIR, "media", "audio")
        os.makedirs(media_dir, exist_ok=True)
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
        hash_local = os.path.join(media_dir, f"{url_hash}.mp3")
        if os.path.exists(hash_local):
            local_path = hash_local
    
    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            body = f.read()
        return Response(content=body, media_type="audio/mpeg",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                                 "Content-Length": str(len(body))})
    
    req_headers = {
        "User-Agent": "python-requests/2.31.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120),
                                   headers=req_headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=502, detail=f"Upstream returned {resp.status}")
                content_type = resp.headers.get("Content-Type", "audio/mpeg")
                body = await resp.read()
                with open(local_path, "wb") as f:
                    f.write(body)
                logger.info(f"Cached audio: {local_path} ({len(body)} bytes)")
                return Response(content=body, media_type=content_type,
                                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                                         "Content-Length": str(len(body))})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Music download proxy error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/conversations/search")
async def api_search_conversations(q: str = ""):
    convs = await search_conversations(q)
    return JSONResponse(content={"conversations": convs})

@app.get("/health")
async def health():
    pool_status = cookie_pool.status()
    active_count = sum(1 for a in pool_status if a["enabled"])
    result = {
        "status": "ok" if active_count > 0 else "degraded",
        "version": "3.3.0",
        "cookie_set": bool(CONFIG.get('cookie')),
        "sign_method": SIGN_METHOD,
        "accounts_total": len(pool_status),
        "accounts_active": active_count,
        "accounts": pool_status,
        "concurrency": {
            "active": concurrency_limiter.active,
            "max": concurrency_limiter.max_concurrent,
            "total_served": concurrency_limiter.total
        },
        "rate_limit": {
            "max_requests": rate_limiter.max_requests,
            "window_seconds": rate_limiter.window_seconds
        },
        "models": list(MODEL_CONFIG.keys()),
        "features": {
            "vision": True,
            "image_upload": True,
            "image_generation": True,
            "deep_think": True,
            "expert_mode": True,
            "coding_mode": True,
            "writing_mode": True,
            "translation": True,
            "tutor_mode": True,
            "data_analyst_mode": True,
            "anthropic_api": True,
            "podcast": True
        }
    }
    if SIGN_METHOD == 'b2' and signer:
        result["signer_initialized"] = signer._initialized
        result["ms_token_available"] = bool(signer.ms_token)
    return result

@app.post("/v1/podcast/generate")
async def podcast_generate(request: Request):
    try:
        body = await request.json()
        topic = body.get("topic", "")
        conversation_id = body.get("conversation_id", "0")
        file_info = body.get("file_info")
        intro_jingle = body.get("intro_jingle", True)
        outro_jingle = body.get("outro_jingle", True)
        if not topic and not file_info:
            raise HTTPException(status_code=400, detail="'topic' or 'file_info' is required")
        result = await start_podcast_generation(
            topic, conversation_id, file_info,
            intro_jingle=intro_jingle, outro_jingle=outro_jingle
        )
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Podcast generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/podcast/upload")
async def podcast_upload_pdf(file: UploadFile = File(...)):
    from uploader import upload_image
    account = cookie_pool.get_next()
    file_data = await file.read()
    file_name = file.filename or "upload.pdf"

    try:
        attachment = await upload_image(
            file_data=file_data,
            file_name=file_name,
            cookie=account.get('cookie', CONFIG.get('cookie', '')),
            device_id=account.get('device_id', ''),
            tea_uuid=account.get('tea_uuid', ''),
            web_id=account.get('web_id', '')
        )
        return {"status": "ok", "file_info": attachment}
    except Exception as e:
        logger.error(f"Podcast file upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/podcast/status/{task_id}")
async def podcast_status(task_id: str):
    result = await get_podcast_status(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.get("/v1/podcast/audio/{task_id}")
async def podcast_audio(task_id: str):
    result = await get_podcast_audio(task_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return JSONResponse(content=result)

@app.get("/v1/podcast/list")
async def podcast_list():
    result = await list_podcasts()
    return JSONResponse(content=result)

@app.get("/v1/podcast/script/{task_id}")
async def podcast_script(task_id: str):
    result = await get_podcast_script(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.get("/v1/podcast/file/{filename}")
async def podcast_file(filename: str):
    file_path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    from pathlib import Path
    content_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
    return Response(
        content=open(file_path, "rb").read(),
        media_type=content_type,
        headers={
            "Content-Disposition": f"inline; filename=\"{filename}\"",
            "Accept-Ranges": "bytes",
        }
    )

@app.post("/v1/music/generate")
async def music_generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    conversation_id = body.get("conversation_id", "0")
    style = body.get("style", "")
    mood = body.get("mood", "")
    voice = body.get("voice", "")
    lyric = body.get("lyric", "")
    if not prompt and not lyric:
        raise HTTPException(status_code=400, detail="prompt or lyric is required")
    result = await start_music_generation(prompt, conversation_id, style=style, mood=mood, voice=voice, lyric=lyric)
    return JSONResponse(content=result)

@app.get("/v1/podcast/config")
async def podcast_config_get():
    from podcast import PODCAST_CONFIG
    return JSONResponse(content=PODCAST_CONFIG)

@app.post("/v1/podcast/config")
async def podcast_config_set(request: Request):
    from podcast import PODCAST_CONFIG
    body = await request.json()
    for key in ("intro_jingle", "outro_jingle"):
        if key in body:
            PODCAST_CONFIG[key] = bool(body[key])
    return JSONResponse(content=PODCAST_CONFIG)

@app.get("/v1/music/status/{task_id}")
async def music_status(task_id: str):
    result = await get_music_status(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.get("/v1/music/audio/{task_id}")
async def music_audio(task_id: str):
    result = await get_music_audio(task_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return JSONResponse(content=result)

@app.get("/v1/music/lyric/{task_id}")
async def music_lyric(task_id: str):
    result = await get_music_lyric(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.get("/v1/music/list")
async def music_list():
    result = await list_music()
    return JSONResponse(content=result)

@app.get("/v1/music/styles")
async def music_styles():
    result = await get_music_styles()
    return JSONResponse(content=result)

@app.get("/v1/user/info")
async def user_info():
    result = await fetch_user_info()
    return JSONResponse(content=result)

@app.get("/v1/doubao/conversations")
async def doubao_conversations():
    result = await fetch_conversation_list()
    return JSONResponse(content={"conversations": result})

@app.get("/v1/doubao/conversations/{conversation_id}/export")
async def doubao_conversation_export(conversation_id: str):
    try:
        result = await export_conversation_full(conversation_id)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Conversation export failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/logs/today")
async def get_today_logs():
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"chat_{date_str}.jsonl")
    if not os.path.exists(log_file):
        return {"date": date_str, "count": 0, "logs": []}
    records = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    return {"date": date_str, "count": len(records), "logs": records}

@app.get("/logs/{date_str}")
async def get_date_logs(date_str: str):
    log_file = os.path.join(LOG_DIR, f"chat_{date_str}.jsonl")
    if not os.path.exists(log_file):
        return {"date": date_str, "count": 0, "logs": []}
    records = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    return {"date": date_str, "count": len(records), "logs": records}

@app.get("/accounts")
async def list_accounts():
    return {"accounts": cookie_pool.status()}

@app.post("/accounts")
async def add_account(request: Request):
    body = await request.json()
    required = ["name", "cookie"]
    for field in required:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing field: {field}")

    new_account = {
        "name": body["name"],
        "cookie": body["cookie"],
        "device_id": body.get("device_id", CONFIG.get('device_id', '')),
        "web_id": body.get("web_id", CONFIG.get('web_id', '')),
        "tea_uuid": body.get("tea_uuid", CONFIG.get('tea_uuid', '')),
        "room_id": body.get("room_id", CONFIG.get('room_id', '')),
        "fail_count": 0,
        "last_fail": None,
        "enabled": True
    }
    cookie_pool.accounts.append(new_account)

    accounts_data = []
    for a in cookie_pool.accounts[1:]:
        accounts_data.append({
            "name": a["name"],
            "cookie": a["cookie"],
            "device_id": a.get("device_id", ""),
            "web_id": a.get("web_id", ""),
            "tea_uuid": a.get("tea_uuid", ""),
            "room_id": a.get("room_id", "")
        })
    save_accounts(accounts_data)

    return {"status": "ok", "message": f"Account '{body['name']}' added", "total": len(cookie_pool.accounts)}

@app.delete("/accounts/{name}")
async def remove_account(name: str):
    cookie_pool.accounts = [a for a in cookie_pool.accounts if a["name"] != name]
    accounts_data = []
    for a in cookie_pool.accounts[1:]:
        accounts_data.append({
            "name": a["name"],
            "cookie": a["cookie"],
            "device_id": a.get("device_id", ""),
            "web_id": a.get("web_id", ""),
            "tea_uuid": a.get("tea_uuid", ""),
            "room_id": a.get("room_id", "")
        })
    save_accounts(accounts_data)
    return {"status": "ok", "message": f"Account '{name}' removed", "total": len(cookie_pool.accounts)}

@app.get("/conversations/{chat_id}")
async def get_conversation(chat_id: str):
    state = load_conversation_state(chat_id)
    if not state:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return state

@app.delete("/v1/conversations/{conversation_id}")
async def delete_conversation_endpoint(conversation_id: str):
    success, error = await delete_conversation(conversation_id)
    if success:
        return {"status": "ok", "message": f"Conversation {conversation_id} deleted"}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {error}")

@app.post("/v1/conversations/cleanup")
async def cleanup_conversations():
    import glob
    deleted = 0
    errors = 0
    now = datetime.now()

    pattern = os.path.join(BASE_DIR, "conversations", "*.json")
    for filepath in glob.glob(pattern):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                state = json.load(f)
            conv_id = state.get("doubao_conversation_id", "")
            updated_at = state.get("updated_at", "")
            if conv_id and conv_id != "0":
                if updated_at:
                    try:
                        updated_time = datetime.fromisoformat(updated_at)
                        age_hours = (now - updated_time).total_seconds() / 3600
                    except:
                        age_hours = 999
                else:
                    age_hours = 999

                if age_hours > CONFIG.get('conversation_cleanup_hours', 24):
                    success, _ = await delete_conversation(conv_id)
                    if success:
                        os.remove(filepath)
                        deleted += 1
                    else:
                        errors += 1
        except Exception as e:
            logger.error(f"Cleanup error for {filepath}: {e}")
            errors += 1

    return {"status": "ok", "deleted": deleted, "errors": errors}

@app.get("/")
async def index():
    html_path = os.path.join(BASE_DIR, 'index.html')
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Doubao API</h1><p>index.html not found</p>")

async def _auto_cleanup_task():
    interval = CONFIG.get('cleanup_interval_seconds', 3600)
    cleanup_hours = CONFIG.get('conversation_cleanup_hours', 24)
    logger.info(f"Auto cleanup task started: interval={interval}s, cleanup_age={cleanup_hours}h")
    while True:
        await asyncio.sleep(interval)
        try:
            import glob
            now = datetime.now()
            deleted = 0
            pattern = os.path.join(BASE_DIR, "conversations", "*.json")
            for filepath in glob.glob(pattern):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    conv_id = state.get("doubao_conversation_id", "")
                    updated_at = state.get("updated_at", "")
                    if conv_id and conv_id != "0":
                        if updated_at:
                            try:
                                updated_time = datetime.fromisoformat(updated_at)
                                age_hours = (now - updated_time).total_seconds() / 3600
                            except:
                                age_hours = 999
                        else:
                            age_hours = 999
                        if age_hours > cleanup_hours:
                            success, _ = await delete_conversation(conv_id)
                            if success:
                                os.remove(filepath)
                                deleted += 1
                except Exception as e:
                    logger.error(f"Auto cleanup error: {e}")
            if deleted > 0:
                logger.info(f"Auto cleanup: deleted {deleted} old conversations")
        except Exception as e:
            logger.error(f"Auto cleanup task error: {e}")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Doubao Free API")
    parser.add_argument("--login", type=str, nargs='?', const='doubao', default=None,
                        help="Open browser for login and save credentials. Specify 'doubao' or 'qianwen' (default: doubao)")
    parser.add_argument("--host", default=None,
                        help="Server host (default: from config.json)")
    parser.add_argument("--port", type=int, default=None,
                        help="Server port (default: from config.json)")
    parser.add_argument("--show", action="store_true", default=False,
                        help="Show browser windows for all sites (default: headless mode)")
    parser.add_argument("--show-doubao", action="store_true", default=False,
                        help="Show Doubao browser window only")
    parser.add_argument("--show-qianwen", action="store_true", default=False,
                        help="Show Qianwen browser window only")
    parser.add_argument("--keep-conversations", action="store_true", default=False,
                        help="Keep all conversation history after server shutdown (default: delete)")
    args = parser.parse_args()

    # 全局 headless 配置（旧参数兼容）
    CONFIG['_headless_browser'] = not args.show
    # 各站点独立配置（优先级：--show-xxx > --show > 默认 headless）
    CONFIG['_doubao_headless'] = not (args.show or args.show_doubao)
    CONFIG['_qianwen_headless'] = not (args.show or args.show_qianwen)
    CONFIG['_keep_conversations'] = args.keep_conversations

    if args.login:
        target = args.login.lower()
        if target not in ("doubao", "qianwen"):
            print(f"Unknown login target: {target}. Use 'doubao' or 'qianwen'", file=sys.stderr)
            os._exit(1)
        if target == "doubao":
            from login import do_login
            result = asyncio.run(do_login(show_browser=True))
            if result.get("success"):
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(0)
            else:
                print(f"Login failed: {result.get('message', 'unknown error')}", file=sys.stderr)
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)
        else:
            from qianwen_login import do_qianwen_login
            result = asyncio.run(do_qianwen_login(show_browser=True))
            if result.get("success"):
                print("=" * 50)
                print("千问登录成功！")
                print("注意：千问使用的是会话 cookie，每次启动服务器时")
                print("如果检测到未登录，会自动打开浏览器让你登录。")
                print("请保持浏览器窗口打开直到服务器关闭。")
                print("=" * 50)
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(0)
            else:
                print(f"Login failed: {result.get('message', 'unknown error')}", file=sys.stderr)
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)

    host = args.host or CONFIG.get('server_host', '0.0.0.0')
    port = args.port or CONFIG.get('server_port', 8765)
    import uvicorn
    uvicorn.run(app, host=host, port=port)
