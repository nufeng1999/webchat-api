import json
import uuid
import time
import asyncio
import logging
import os
import base64
from typing import Optional

import aiohttp

from config import CONFIG, USER_AGENT, cookie_pool
from sse import build_url_params, build_headers

logger = logging.getLogger("webchat-api")

VIDEO_TASKS = {}

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", "video")
os.makedirs(MEDIA_DIR, exist_ok=True)


async def _download_video_to_local(task_id: str, video_url: str):
    """Download video to local storage."""
    try:
        local_path = os.path.join(MEDIA_DIR, f"{task_id}.mp4")
        if os.path.exists(local_path):
            VIDEO_TASKS[task_id]["local_video_path"] = local_path
            logger.info(f"[Video] Video already cached: {local_path}")
            return local_path

        headers = {
            "User-Agent": "python-requests/2.31.0",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=300),
                                   headers=headers, allow_redirects=True) as resp:
                if resp.status == 200:
                    body = await resp.read()
                    with open(local_path, "wb") as f:
                        f.write(body)
                    VIDEO_TASKS[task_id]["local_video_path"] = local_path
                    logger.info(f"[Video] Downloaded video: {local_path} ({len(body)} bytes)")
                    return local_path
                else:
                    logger.warning(f"[Video] Failed to download video: HTTP {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"[Video] Error downloading video: {e}")
        return None


async def start_video_generation(prompt: str, model: str = "jimeng",
                               duration: int = 5, width: int = 1280, height: int = 720,
                               fps: int = 30, seed: Optional[int] = None, n: int = 1,
                               image: Optional[str] = None, response_format: Optional[str] = None,
                               user: Optional[str] = None, metadata: Optional[dict] = None):
    """
    Start a video generation task.

    Returns immediately with task_id and status. The actual generation runs in background.
    """
    account = cookie_pool.get_next()
    task_id = f"video-{uuid.uuid4().hex[:12]}"

    VIDEO_TASKS[task_id] = {
        "task_id": task_id,
        "prompt": prompt,
        "model": model,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "seed": seed,
        "n": n,
        "image": image,
        "response_format": response_format,
        "user": user,
        "metadata": metadata or {},
        "status": "queued",
        "created_at": time.time(),
        "video_url": None,
        "local_video_path": None,
        "error": None,
        "account": account
    }

    asyncio.create_task(_run_video_generation(task_id))

    return {
        "task_id": task_id,
        "status": "queued",
        "model": model
    }


async def _run_video_generation(task_id: str):
    """Background task to generate video using API."""
    task = VIDEO_TASKS.get(task_id)
    if not task:
        return

    account = task["account"]
    model = task["model"]

    try:
        task["status"] = "in_progress"
        logger.info(f"[Video] Starting generation: task_id={task_id}, model={model}, prompt={task['prompt'][:50]}")

        # Route based on model type
        if model == "jimeng" or model.startswith("jimeng-"):
            # Jimeng video generation via browser
            success, result = await _video_generation_jimeng(task_id)
        elif model == "doubao-video" or model.startswith("doubao-video"):
            # Doubao video generation via browser
            success, result = await _video_generation_doubao(task_id)
        else:
            # Try API method first (existing Doubao API method for other models)
            success, result = await _video_generation_api(task_id)

        if success:
            # For Doubao, if no URL returned yet, keep as in_progress and let polling handle it
            video_url = result.get("url")
            if video_url:
                task["status"] = "completed"
                task["video_url"] = video_url
                task.setdefault("metadata", {})
                task["metadata"].update({
                    "duration": result.get("duration", task["duration"]),
                    "width": result.get("width", task["width"]),
                    "height": result.get("height", task["height"]),
                    "fps": result.get("fps", task["fps"]),
                    "format": result.get("format", "mp4"),
                    "seed": result.get("seed", task["seed"])
                })
                logger.info(f"[Video] Generation completed: task_id={task_id}")
            else:
                # No URL yet, keep in_progress for polling
                task["status"] = "in_progress"
                logger.info(f"[Video] Generation submitted, waiting for completion: task_id={task_id}")
        else:
            # API failed, try fallback methods
            logger.warning(f"[Video] API failed for task {task_id}, trying fallback")
            success_fallback, result_fallback = await _video_generation_fallback(task_id)
            if success_fallback:
                task["status"] = "completed"
                task["video_url"] = result_fallback.get("url")
                task.setdefault("metadata", {})
                task["metadata"].update(result_fallback.get("metadata", {}))
                logger.info(f"[Video] Generation completed via fallback: task_id={task_id}")
            else:
                task["status"] = "failed"
                task["error"] = result_fallback.get("error", "Video generation failed")
                logger.error(f"[Video] All methods failed for task {task_id}")

        # Download video if URL is available and status is completed
        if task["status"] == "completed" and task["video_url"]:
            asyncio.create_task(_download_video_to_local(task_id, task["video_url"]))

    except Exception as e:
        logger.error(f"[Video] Generation error for task {task_id}: {e}")
        task["status"] = "failed"
        task["error"] = str(e)
        cookie_pool.report_fail(account, str(e))


async def _video_generation_api(task_id: str):
    """Primary method: use Doubao API for video generation."""
    task = VIDEO_TASKS[task_id]
    account = task["account"]

    params = _build_video_params(task)
    url = f"{CONFIG['api_base']}/samantha/chat/completion?{build_url_params(account)}"
    headers = build_headers(account)
    headers['referer'] = 'https://www.doubao.com/chat/video'

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=params, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.error(f"[Video] API error {resp.status}: {err_text[:300]}")
                    return False, {"error": f"API returned {resp.status}"}

                video_url = None
                metadata = {}

                async for raw_line in resp.content:
                    try:
                        line = raw_line.decode('utf-8', errors='replace').strip()
                    except:
                        continue

                    if not line or not line.startswith('data:'):
                        continue

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

                        if event_type == 2001:
                            msg = event_data.get("message", {})
                            ct = msg.get("content_type")
                            raw_content = msg.get("content", "")

                            if ct == 2015 and isinstance(raw_content, str):  # Video content type
                                try:
                                    ct_content = json.loads(raw_content)
                                    video_url = _extract_video_url(ct_content)
                                    if video_url:
                                        metadata = _extract_video_metadata(ct_content)
                                        logger.info(f"[Video] Got video URL: {video_url}")
                                        break
                                except json.JSONDecodeError as e:
                                    logger.warning(f"[Video] Failed to parse video content: {e}")

                        elif event_type == 2005:
                            error_msg = event_data.get("message", "")
                            error_code = event_data.get("code", "")
                            logger.error(f"[Video] Error 2005: code={error_code} msg={error_msg}")
                            return False, {"error": f"Video generation error: {error_msg}"}

                    except json.JSONDecodeError:
                        pass

                if video_url:
                    return True, {"url": video_url, **metadata}
                else:
                    return False, {"error": "No video URL in response"}

    except Exception as e:
        logger.error(f"[Video] API request failed: {e}")
        return False, {"error": str(e)}


def _build_video_params(task: dict) -> dict:
    """Build request parameters for video generation API."""
    text = task["prompt"]

    # Build Doubao video chat message format
    body = {
        "messages": [{
            "content": json.dumps({
                "text": text,
                "video_model": json.dumps({
                    "duration": task["duration"],
                    "width": task["width"],
                    "height": task["height"],
                    "fps": task["fps"],
                    "seed": task["seed"]
                }) if task.get("seed") else None,
                "negative_prompt": task["metadata"].get("negative_prompt", ""),
                "style": task["metadata"].get("style", ""),
                "quality_level": task["metadata"].get("quality_level", "")
            }, ensure_ascii=False),
            "content_type": 2015  # Video content type
        }],
        "completion_option": {
            "is_regen": False,
            "with_suggest": True,
            "need_create_conversation": True,
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
        "conversation_id": "0",
        "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
        "local_message_id": str(uuid.uuid4())
    }

    # Add image if provided
    if task.get("image"):
        body["messages"][0]["content"] = json.dumps({
            "text": text,
            "image": task["image"],
            "video_model": json.dumps({
                "duration": task["duration"],
                "width": task["width"],
                "height": task["height"],
                "fps": task["fps"],
                "seed": task["seed"]
            }) if task.get("seed") else None,
            "negative_prompt": task["metadata"].get("negative_prompt", ""),
            "style": task["metadata"].get("style", ""),
            "quality_level": task["metadata"].get("quality_level", "")
        }, ensure_ascii=False)

    return body


def _extract_video_url(ct_content: dict) -> Optional[str]:
    """Extract video URL from content."""
    videos = ct_content.get("videos", [])
    if videos and len(videos) > 0:
        main_url = videos[0].get("main_url", "")
        if main_url:
            # Decode base64 if needed
            try:
                if _is_base64(main_url):
                    return base64.b64decode(main_url).decode('utf-8')
            except:
                pass
            return main_url
    return None


def _extract_video_metadata(ct_content: dict) -> dict:
    """Extract video metadata from content."""
    metadata = {}
    videos = ct_content.get("videos", [])
    if videos and len(videos) > 0:
        video = videos[0]
        metadata.update({
            "duration": video.get("duration_sec"),
            "width": video.get("width"),
            "height": video.get("height"),
            "format": video.get("format", "mp4"),
            "thumbnail_url": video.get("cover_url")
        })
    return metadata


def _is_base64(s: str) -> bool:
    """Check if a string is base64 encoded."""
    import re
    pattern = r'^[A-Za-z0-9+/]+=*$'
    if not re.match(pattern, s):
        return False
    if len(s) % 4 != 0:
        return False
    return True


async def _video_generation_doubao(task_id: str):
    """Doubao video generation via browser client.
    
    Uses the browser_client's call_doubao_video_generate_api method which handles:
    1. Browser persistence and login state
    2. API signature generation (msToken, a_bogus, sign)
    3. Request submission and response parsing
    """
    task = VIDEO_TASKS.get(task_id)
    if not task:
        return False, {"error": "Task not found"}

    from browser_client import browser_client

    # Ensure browser is ready
    try:
        await browser_client.ensure_doubao_ready(headless=True)
    except Exception as e:
        logger.error(f"[DoubaoVideo] Browser init failed: {e}")
        return False, {"error": f"Browser init failed: {e}"}

    # Build request body
    model = task.get("model", "doubao-video")
    resolution = "1080p" if task.get("height", 720) >= 1080 else "720p"
    duration_ms = task["duration"] * 1000

    # Generate seed if not provided
    seed = task.get("seed") or int(time.time() * 1000) % 2**31

    body = {
        "client_meta": {
            "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
            "conversation_id": "",
            "bot_id": "7338286299411103781",
            "last_section_id": "",
            "last_message_index": None
        },
        "messages": [{
            "local_message_id": str(uuid.uuid4()),
            "content_block": [{
                "block_type": 10000,
                "content": {
                    "text_block": {
                        "text": task["prompt"],
                        "icon_url": "",
                        "icon_url_dark": "",
                        "summary": ""
                    },
                    "video_params": json.dumps({
                        "duration": task["duration"],
                        "width": task["width"],
                        "height": task["height"],
                        "fps": task["fps"],
                        "seed": seed,
                        "resolution": resolution
                    }),
                    "pc_event_block": ""
                },
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": []
            }],
            "message_status": 0
        }],
        "option": {
            "send_message_scene": "video",
            "create_time_ms": int(time.time() * 1000),
            "collect_id": "",
            "is_audio": False,
            "answer_with_suggest": False,
            "tts_switch": False,
            "need_deep_think": 0,
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
            "need_create_conversation": True,
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
        },
        "chat_ability": {
            "ability_type": 3,
            "ability_param": json.dumps({
                "ability_type": 2,
                "model": "Seedance",
                "video_params": {
                    "duration": task["duration"],
                    "width": task["width"],
                    "height": task["height"],
                    "fps": task["fps"],
                    "seed": seed
                }
            })
        },
        "ext": {
            "input_skill": "",
            "use_deep_think": "0",
            "fp": "",
            "sub_conv_firstmet_type": "1",
            "collection_id": "",
            "conversation_init_option": '{"need_ack_conversation":true}',
            "commerce_credit_config_enable": "0"
        }
    }

    try:
        result = await browser_client.call_doubao_video_generate_api(body)

        if result.get("ret") != "0":
            error_msg = result.get("errmsg", "Unknown error")
            logger.error(f"[DoubaoVideo] API error: {error_msg}")
            return False, {"error": error_msg}

        task_data = result.get("data", {})
        task_info = task_data.get("task", {})
        history_record_id = task_info.get("task_id") or task_data.get("task_id")
        conversation_id = task_info.get("conversation_id") or history_record_id

        if not history_record_id:
            logger.error("[DoubaoVideo] No task_id in response")
            return False, {"error": "No task_id in response"}

        task["history_record_id"] = history_record_id
        task["conversation_id"] = conversation_id

        video_url = result.get("data", {}).get("url")
        return True, {
            "url": video_url,
            "task_id": history_record_id,
            "submit_id": str(uuid.uuid4())
        }

    except Exception as e:
        logger.error(f"[DoubaoVideo] API request failed: {e}")
        return False, {"error": str(e)}


async def _video_generation_fallback(task_id: str):
    """Fallback method: use Playwright to generate video."""
    task = VIDEO_TASKS.get(task_id)
    if not task:
        return False, {"error": "Task not found"}

    try:
        # This would be a Playwright-based implementation similar to music.py
        # For now, return a placeholder that needs to be implemented
        logger.warning(f"[Video] Fallback not fully implemented for task {task_id}")
        return False, {"error": "Video generation fallback not implemented"}
    except Exception as e:
        logger.error(f"[Video] Fallback failed for task {task_id}: {e}")
        return False, {"error": str(e)}


async def _video_generation_jimeng(task_id: str):
    """Jimeng video generation via browser client.
    
    Uses the browser_client's call_video_generate_api method which handles:
    1. Browser persistence and login state
    2. API signature generation (msToken, a_bogus, sign)
    3. Request submission and response parsing
    """
    task = VIDEO_TASKS.get(task_id)
    if not task:
        return False, {"error": "Task not found"}

    from browser_client import browser_client

    # Ensure browser is ready
    try:
        await browser_client.ensure_jimeng_ready(headless=True)
    except Exception as e:
        logger.error(f"[Jimeng] Browser init failed: {e}")
        return False, {"error": f"Browser init failed: {e}"}

    # Build request body
    model_req_key = "dreamina_seedance_40_pro"
    resolution = "720p"  # Default resolution
    duration_ms = task["duration"] * 1000
    
    # Generate seed if not provided
    seed = task.get("seed") or int(time.time() * 1000) % 2**31
    
    # Build draft_content
    draft_content = _build_jimeng_draft_content(
        prompt=task["prompt"],
        image=task.get("image"),
        model_req_key=model_req_key,
        resolution=resolution,
        duration_ms=duration_ms,
        fps=task.get("fps", 24),
        seed=seed
    )
    
    submit_id = str(uuid.uuid4())
    
    extend = {
        "root_model": model_req_key,
        "m_video_commerce_info": {
            "amount": 1,
            "benefit_type": "dreamina_video_seedance_20_pro",
            "resource_id": "generate_video",
            "resource_id_type": "str",
            "resource_sub_type": "aigc"
        }
    }
    
    metrics_extra = json.dumps({
        "isDefaultSeed": 1,
        "originSubmitId": submit_id,
        "isRegenerate": False,
        "enterFrom": "api",
        "position": "api",
        "aiFeatureName": "video",
        "promptType": "original_prompt",
        "functionMode": "omni_reference",
        "sceneOptions": json.dumps([{
            "type": "video",
            "scene": "BasicVideoGenerateButton",
            "resolution": resolution,
            "modelReqKey": model_req_key,
            "videoDuration": task["duration"],
            "batchNumber": 1,
            "chargeInputVideoDuration": False,
            "isLongVideo": False,
            "hasInputVideo": False,
            "useSeedanceFast5sFreeTrial": False,
            "reportParams": {
                "enterSource": "api",
                "vipSource": "api",
                "extraVipFunctionKey": f"{model_req_key}-{resolution}",
                "useVipFunctionDetailsReporterHoc": True
            },
            "materialTypes": []
        }]),
        "batchNumber": 1,
        "submitGroupId": str(uuid.uuid4()),
        "hasRejectedAudit": 0
    }, ensure_ascii=False)
    
    body = {
        "extend": extend,
        "submit_id": submit_id,
        "metrics_extra": metrics_extra,
        "draft_content": draft_content,
        "http_common_info": {
            "aid": CONFIG.get("aid", 513695)
        }
    }
    
    try:
        result = await browser_client.call_video_generate_api(body)
        
        if result.get("ret") != "0":
            error_msg = result.get("errmsg", "Unknown error")
            logger.error(f"[Jimeng] API error: {error_msg}")
            return False, {"error": error_msg}
        
        task_data = result.get("data", {})
        task_info = task_data.get("task", {})
        history_record_id = task_info.get("task_id")
        
        if not history_record_id:
            logger.error("[Jimeng] No task_id in response")
            return False, {"error": "No task_id in response"}
        
        # Update task with the real history_record_id
        task["history_record_id"] = history_record_id
        
        return True, {
            "url": None,  # Video URL will be available after polling
            "history_record_id": history_record_id,
            "submit_id": submit_id
        }
        
    except Exception as e:
        logger.error(f"[Jimeng] API request failed: {e}")
        return False, {"error": str(e)}


def _build_jimeng_draft_content(prompt: str, image: Optional[str], model_req_key: str,
                               resolution: str, duration_ms: int, fps: int, seed: int) -> str:
    """Build Jimeng draft_content JSON string."""
    video_mode = 1 if image else 2  # 1=image-to-video, 2=text-to-video
    
    draft = {
        "type": "draft",
        "id": str(uuid.uuid4()),
        "min_version": "3.0.5",
        "min_features": [],
        "is_from_tsn": True,
        "version": "3.3.21",
        "main_component_id": str(uuid.uuid4()),
        "component_list": [
            {
                "type": "video_base_component",
                "id": str(uuid.uuid4()),
                "min_version": "1.0.0",
                "aigc_mode": "workbench",
                "gen_type": 10,
                "metadata": {
                    "type": "",
                    "id": str(uuid.uuid4()),
                    "created_platform": 3,
                    "created_platform_version": "",
                    "created_time_in_ms": str(int(time.time() * 1000)),
                    "created_did": ""
                },
                "generate_type": "gen_video",
                "abilities": {
                    "type": "",
                    "id": str(uuid.uuid4()),
                    "gen_video": {
                        "type": "",
                        "id": str(uuid.uuid4()),
                        "text_to_video_params": {
                            "type": "",
                            "id": str(uuid.uuid4()),
                            "video_gen_inputs": [
                                {
                                    "type": "",
                                    "id": str(uuid.uuid4()),
                                    "min_version": "3.0.5",
                                    "prompt": prompt,
                                    "video_mode": video_mode,
                                    "fps": fps,
                                    "duration_ms": duration_ms,
                                    "resolution": resolution,
                                    "idip_meta_list": []
                                }
                            ],
                            "video_aspect_ratio": "16:9",
                            "seed": seed,
                            "model_req_key": model_req_key,
                            "priority": 0
                        },
                        "video_task_extra": json.dumps({
                            "isDefaultSeed": 1,
                            "originSubmitId": "",
                            "isRegenerate": False,
                            "enterFrom": "api",
                            "position": "api",
                            "aiFeatureName": "video",
                            "promptType": "original_prompt",
                            "functionMode": "omni_reference",
                            "sceneOptions": json.dumps([{
                                "type": "video",
                                "scene": "BasicVideoGenerateButton",
                                "resolution": resolution,
                                "modelReqKey": model_req_key,
                                "videoDuration": duration_ms // 1000,
                                "batchNumber": 1,
                                "chargeInputVideoDuration": False,
                                "isLongVideo": False,
                                "hasInputVideo": False,
                                "useSeedanceFast5sFreeTrial": False,
                                "reportParams": {
                                    "enterSource": "api",
                                    "vipSource": "api",
                                    "extraVipFunctionKey": f"{model_req_key}-{resolution}",
                                    "useVipFunctionDetailsReporterHoc": True
                                },
                                "materialTypes": []
                            }]),
                            "batchNumber": 1,
                            "submitGroupId": str(uuid.uuid4()),
                            "hasRejectedAudit": 0
                        })
                    }
                },
                "process_type": 1
            }
        ]
    }
    
    # Add image if provided
    if image:
        draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"]["video_gen_inputs"][0]["image"] = image
    
    return json.dumps(draft, ensure_ascii=False)


async def get_video_status(task_id: str):
    """Get status of a video generation task.
    
    Response:
    - task_id: str
    - prompt: str
    - status: "queued" | "in_progress" | "completed" | "failed"
    - model: str
    - created_at: float
    - url: str (if status=="completed")
    - local_video_path: str (if downloaded)
    - format: str (e.g., "mp4")
    - metadata: dict
    - error: {code, message} (if status=="failed")
    - queue_position: int (if status=="queued")
    """
    # Try to find in VIDEO_TASKS first
    task = VIDEO_TASKS.get(task_id)
    
    # If not in VIDEO_TASKS and task_id looks like a Doubao conversation ID (numeric),
    # try to query Doubao directly using it as conversation_id
    if not task and task_id.isdigit():
        try:
            from browser_client import browser_client
            if hasattr(browser_client, 'call_doubao_video_status_api'):
                result = await browser_client.call_doubao_video_status_api([task_id])
                if result and result.get("ret") == "0":
                    data = result.get("data", {})
                    task_data = data.get(task_id, {})
                    if task_data:
                        status_code = task_data.get("status", 20)
                        # Map Doubao status codes to standard status strings
                        STATUS_MAP = {
                            10: "in_progress",
                            20: "in_progress",
                            30: "in_progress",
                            40: "completed",
                            50: "failed",
                            60: "failed"
                        }
                        new_status = STATUS_MAP.get(status_code, "in_progress")
                        video_url = task_data.get("video_url")
                        
                        response = {
                            "task_id": task_id,
                            "status": new_status,
                            "model": "doubao-video",
                            "created_at": 0
                        }
                        if new_status == "completed" and video_url:
                            response.update({
                                "url": video_url,
                                "format": "mp4"
                            })
                        elif new_status == "failed":
                            response["error"] = {"code": 0, "message": task_data.get("error_msg", "Video generation failed")}
                        return response
        except Exception as e:
            logger.warning(f"[Video] Failed to query historical task {task_id}: {e}")
        
        return {"error": "Task not found", "task_id": task_id}

    if not task:
        return {"error": "Task not found", "task_id": task_id}

    model = task.get("model", "")
    
    # For doubao-video tasks, poll the upstream status if we have a history_record_id
    if (model == "doubao-video" or model.startswith("doubao-video")):
        if task.get("history_record_id") and task["status"] in ("queued", "in_progress"):
            try:
                from browser_client import browser_client
                if hasattr(browser_client, 'call_doubao_video_status_api'):
                    result = await browser_client.call_doubao_video_status_api([task["history_record_id"]])
                    if result and result.get("ret") == "0":
                        data = result.get("data", {})
                        task_data = data.get(task["history_record_id"], {})
                        if task_data:
                            status_code = task_data.get("status", 20)
                            
                            # Map Doubao status codes to standard status strings
                            STATUS_MAP = {
                                10: "in_progress",
                                20: "in_progress",
                                30: "in_progress",
                                40: "completed",
                                50: "failed",
                                60: "failed"
                            }
                            new_status = STATUS_MAP.get(status_code, "in_progress")
                            
                            if new_status != task["status"]:
                                task["status"] = new_status
                                logger.info(f"[Video] Task {task_id} status updated to: {new_status} from history")
                            
                            if new_status == "completed":
                                # Extract video URL from response
                                video_url = task_data.get("video_url")
                                if video_url:
                                    task["video_url"] = video_url
                                    task.setdefault("metadata", {})
                                    task["metadata"].update({
                                        "format": "mp4"
                                    })
                                    # Download video
                                    asyncio.create_task(_download_video_to_local(task_id, video_url))
                                else:
                                    logger.warning(f"[Video] Completed but no video_url for {task_id}")
                            elif new_status == "failed":
                                task["error"] = task_data.get("error_msg", "Video generation failed")
            except Exception as e:
                logger.warning(f"[Video] Status polling failed for task {task_id}: {e}")

    response = {
        "task_id": task["task_id"],
        "prompt": task["prompt"],
        "status": task["status"],
        "model": task.get("model"),
        "created_at": task.get("created_at")
    }

    if task["status"] == "completed":
        response.update({
            "url": task.get("video_url"),
            "local_video_path": task.get("local_video_path"),
            "format": task["metadata"].get("format", "mp4") if task.get("metadata") else "mp4",
            "metadata": task.get("metadata", {})
        })
    elif task["status"] == "failed":
        response["error"] = {"code": 0, "message": task.get("error", "Unknown error")}
    elif task["status"] == "queued":
        response["queue_position"] = _get_queue_position(task_id)

    return response


def _get_queue_position(task_id: str) -> int:
    """Get position in queue (approximate)."""
    queued = [tid for tid, t in VIDEO_TASKS.items() if t["status"] == "queued"]
    if task_id in queued:
        return queued.index(task_id) + 1
    return 0


async def list_videos():
    """List all video generation tasks."""
    tasks = []
    for task_id, task in VIDEO_TASKS.items():
        tasks.append({
            "task_id": task_id,
            "prompt": task["prompt"],
            "status": task["status"],
            "model": task.get("model"),
            "duration": task.get("duration"),
            "created_at": task.get("created_at")
        })
    tasks.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"tasks": tasks}
