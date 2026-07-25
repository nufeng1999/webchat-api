"""Doubao (豆包) 视频生成适配器。"""
import json
import uuid
import time
import asyncio
import logging
from typing import Optional

from adapters.base import BaseAdapter
from models import ChatCompletionRequest
from config import CONFIG

logger = logging.getLogger("doubao-video-adapter")

# Doubao 视频生成模型配置
DOUBAO_VIDEO_MODELS = {
    "doubao-video": {
        "name": "Doubao Video",
        "desc": "Doubao AI 视频生成 (文本到视频)",
        "max_duration": 10,
        "supported_resolutions": ["720p", "1080p"],
        "default_fps": 30,
        "is_video_model": True
    },
    "doubao-video-1080p": {
        "name": "Doubao Video 1080p",
        "desc": "Doubao AI 视频生成 (高清 1080p)",
        "max_duration": 10,
        "supported_resolutions": ["1080p"],
        "default_fps": 30,
        "is_video_model": True
    }
}


class DoubaoVideoAdapter(BaseAdapter):
    """Doubao 视频生成适配器，使用浏览器持久化 profile 进行视频生成。"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_task_id = None
        self._browser_client = None

    def get_adapter_name(self) -> str:
        return "doubao_video"

    def get_models(self) -> dict[str, dict]:
        return DOUBAO_VIDEO_MODELS

    def supports_model(self, model: str) -> bool:
        return model in DOUBAO_VIDEO_MODELS or model.startswith("doubao-video")

    async def init(self):
        from browser_client import browser_client
        self._browser_client = browser_client
        logger.info("DoubaoVideo adapter initialized")

    async def close(self):
        pass

    async def _call_stream(self, **kwargs):
        """Doubao视频适配器不需要流式聊天调用。"""
        return
        yield  # Make it a generator

    async def _prepare_messages(self, request, browser_client, is_agent: bool, reuse_conversation: bool = False):
        """Doubao视频适配器不需要消息准备。"""
        return "", None

    async def _delete_conversation(self):
        """Doubao视频适配器不需要对话删除。"""
        pass

    async def stream_chat(self, request: ChatCompletionRequest):
        """Doubao视频适配器不支持聊天流式接口。"""
        yield f"data: {json.dumps({
            'id': f'chatcmpl-{uuid.uuid4().hex[:12]}',
            'object': 'chat.completion.chunk',
            'created': int(time.time()),
            'model': request.model,
            'choices': [{
                'index': 0,
                'delta': {'content': 'Doubao视频适配器仅支持视频生成接口(/v1/video/generations)，不支持聊天流式接口。'},
                'finish_reason': 'stop'
            }]
        }, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def _get_lock(self):
        """返回用于并发控制的锁。"""
        return self._lock

    async def generate_video(
        self,
        prompt: str,
        duration: int = 5,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        seed: Optional[int] = None,
        n: int = 1,
        image: Optional[str] = None,
        response_format: Optional[str] = None,
        user: Optional[str] = None,
        metadata: Optional[dict] = None,
        model: str = "doubao-video"
    ) -> dict:
        """调用Doubao视频生成API。

        使用浏览器自动化调用Doubao的视频生成端点。
        返回格式与Jimeng一致，供video.py统一处理。
        """
        if not self._browser_client:
            await self.init()

        try:
            # 确保浏览器就绪
            if not hasattr(self._browser_client, 'ensure_doubao_ready'):
                raise RuntimeError("Browser client does not have Doubao support. Check browser_client._doubao integration.")

            # 为避免频繁登录，这里 headless 应与配置文件保持一致
            # 测试时可手动改为 headless=False 可视化观察
            headless_mode = CONFIG.get('_doubao_headless', True)
            await self._browser_client.ensure_doubao_ready(headless=headless_mode)

            # 构建视频生成请求体
            model_info = DOUBAO_VIDEO_MODELS.get(model, DOUBAO_VIDEO_MODELS["doubao-video"])
            resolution = self._determine_resolution(width, height)
            duration_ms = duration * 1000

            # 生成 seed
            if seed is None:
                seed = int(time.time() * 1000) % 2**31

            # 构建请求体
            body = self._build_video_body(
                prompt=prompt,
                image=image,
                resolution=resolution,
                duration_ms=duration_ms,
                fps=fps,
                seed=seed
            )

            # 通过浏览器页面调用API
            if not hasattr(self._browser_client, 'call_doubao_video_generate_api'):
                raise RuntimeError("Browser client missing call_doubao_video_generate_api method")

            result = await self._browser_client.call_doubao_video_generate_api(body)

            # 解析响应
            if not result or result.get("ret") != "0":
                error_msg = result.get("errmsg", result.get("error", "Unknown error"))
                logger.error(f"Doubao video API error: {error_msg}")
                return {
                    "error": True,
                    "message": error_msg,
                    "task_id": None
                }

            task_data = result.get("data", {})
            task_info = task_data.get("task", {})
            task_id = task_info.get("task_id") or task_data.get("task_id")

            if not task_id:
                logger.error("Doubao video API returned no task_id")
                return {
                    "error": True,
                    "message": "No task_id in response",
                    "task_id": None
                }

            self._last_task_id = task_id

            return {
                "task_id": task_id,
                "status": "queued",
                "model": model,
                "queue_position": task_data.get("queue_info", {}).get("queue_idx", 0)
            }

        except Exception as e:
            logger.error(f"Doubao video generation failed: {e}")
            return {
                "error": True,
                "message": str(e),
                "task_id": None
            }

    async def get_video_status(self, task_id: str) -> dict:
        """获取Doubao视频生成任务状态。"""
        if not self._browser_client:
            await self.init()

        try:
            if not hasattr(self._browser_client, 'call_doubao_video_status_api'):
                raise RuntimeError("Browser client missing call_doubao_video_status_api method")

            # 通过 submit_id 查询
            result = await self._browser_client.call_doubao_video_status_api([task_id])

            if not result or result.get("ret") != "0":
                logger.error(f"Doubao video status API error: {result}")
                return {"error": True, "message": result.get("errmsg", "Unknown error")}

            data = result.get("data", {})

            # 查找任务数据
            task_data = data.get(task_id) or data.get("tasks", [{}])[0] if isinstance(data.get("tasks"), list) else {}
            if not task_data:
                return {"error": True, "message": "Task not found"}

            status_code = task_data.get("status", 0)
            status = self._map_status(status_code)

            response = {
                "task_id": task_id,
                "status": status,
                "status_code": status_code,
                "created_at": task_data.get("created_time", int(time.time() * 1000)),
                "model": "doubao-video"
            }

            # 如果完成，提取视频信息
            if status == "completed":
                video_info = self._extract_video_info(task_data)
                response.update(video_info)

            return response

        except Exception as e:
            logger.error(f"Doubao get_status failed: {e}")
            return {"error": True, "message": str(e)}

    def _determine_resolution(self, width: int, height: int) -> str:
        """确定分辨率字符串。"""
        if width >= 1920 and height >= 1080:
            return "1080p"
        elif width >= 1280 and height >= 720:
            return "720p"
        else:
            return "720p"

    def _build_video_body(self, prompt: str, image: Optional[str], resolution: str,
                          duration_ms: int, fps: int, seed: int) -> dict:
        """构建Doubao视频生成请求体。

        基于Doubao chat completion API的视频生成格式。
        content_type: 2015 用于视频生成。
        """
        submit_id = str(uuid.uuid4())

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
                            "text": prompt,
                            "icon_url": "",
                            "icon_url_dark": "",
                            "summary": ""
                        },
                        "video_params": json.dumps({
                            "duration": duration_ms // 1000,
                            "width": 1280 if resolution == "720p" else 1920,
                            "height": 720 if resolution == "720p" else 1080,
                            "fps": fps,
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
                "unique_key": submit_id,
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
                "ability_type": 3,  # 视频生成能力类型
                "ability_param": json.dumps({
                    "ability_type": 2,  # 视频生成子类型
                    "model": "Seedance",  # 视频生成模型
                    "video_params": {
                        "duration": duration_ms // 1000,
                        "resolution": resolution,
                        "fps": fps,
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

        return body

    def _map_status(self, status_code: int) -> str:
        """映射状态码到状态字符串。"""
        STATUS_MAP = {
            10: "generating",
            20: "queued",
            30: "processing",
            40: "completed",
            50: "failed",
            60: "cancelled"
        }
        return STATUS_MAP.get(status_code, "unknown")

    def _extract_video_info(self, task_data: dict) -> dict:
        """从任务结果中提取视频信息。"""
        result = {}

        # 尝试从多个位置提取视频URL
        video_url = None
        video_data = task_data.get("video_data", {})

        if video_data:
            video_url = video_data.get("main_url") or video_data.get("url", "")
            result.update({
                "duration": video_data.get("duration_sec") or task_data.get("duration"),
                "width": video_data.get("width"),
                "height": video_data.get("height"),
                "format": video_data.get("format", "mp4"),
                "thumbnail_url": video_data.get("cover_url") or video_data.get("thumbnail", "")
            })

        # 如果video_data为空，尝试其他位置
        if not video_url:
            for key in ["video_url", "url", "download_url", "output_url"]:
                if task_data.get(key):
                    video_url = task_data[key]
                    break

        if video_url:
            result["video_url"] = video_url
            if "video_url" not in result:
                result["video_url"] = video_url

        return result