"""即梦 (Jimeng) 适配器，使用浏览器持久化 profile 进行视频生成。"""
import json
import uuid
import time
import asyncio
import logging
from typing import AsyncGenerator
from adapters.base import BaseAdapter
from models import ChatCompletionRequest
from config import CONFIG
from sse import format_openai_chunk, format_openai_done

logger = logging.getLogger("jimeng-adapter")

JIMENG_MODELS = {
    "jimeng": {
        "name": "即梦",
        "desc": "即梦AI视频生成 (Seedance 2.0)",
        "model_req_key": "dreamina_seedance_40_pro",
        "max_duration": 15,
        "supported_resolutions": ["720p"],
        "default_fps": 24
    },
    "jimeng-seedance-40-pro": {
        "name": "即梦 Seedance 2.0 Pro",
        "desc": "即梦 Seedance 2.0 专业版",
        "model_req_key": "dreamina_seedance_40_pro",
        "max_duration": 15,
        "supported_resolutions": ["720p"],
        "default_fps": 24
    }
}

# 视频状态映射
STATUS_MAP = {
    10: "generating",
    20: "queued",
    30: "processing",
    40: "completed",
    50: "failed",
    60: "cancelled"
}


class JimengAdapter(BaseAdapter):
    """即梦适配器，支持视频生成。"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_task_id = None
        self._browser_client = None

    def get_adapter_name(self) -> str:
        return "jimeng"

    def get_models(self) -> dict[str, dict]:
        return JIMENG_MODELS

    def supports_model(self, model: str) -> bool:
        return model in JIMENG_MODELS or model.startswith("jimeng")

    async def init(self):
        from browser_client import browser_client
        self._browser_client = browser_client
        logger.info("Jimeng adapter initialized")

    async def close(self):
        pass

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        """即梦不支持流式聊天，返回错误提示。"""
        yield format_openai_chunk(
            "即梦适配器仅支持视频生成接口(/v1/video/generations)调用，不支持聊天流式接口。",
            request.model,
            f"chatcmpl-{uuid.uuid4().hex[:12]}"
        ).encode()
        yield format_openai_done()

    async def _call_stream(self, **kwargs):
        """即梦适配器不需要流式聊天调用。"""
        return
        yield  # Make it a generator

    async def _prepare_messages(self, request, browser_client, is_agent: bool, reuse_conversation: bool = False):
        """即梦适配器不需要消息准备。"""
        return "", None

    async def _delete_conversation(self):
        """即梦适配器不需要对话删除。"""
        pass

    async def _get_lock(self):
        """返回用于并发控制的锁。"""
        return self._lock

    async def generate_video(
        self,
        prompt: str,
        duration: int = 5,
        width: int = 1280,
        height: int = 720,
        fps: int = 24,
        seed: Optional[int] = None,
        n: int = 1,
        image: Optional[str] = None,
        response_format: Optional[str] = None,
        user: Optional[str] = None,
        metadata: Optional[dict] = None,
        model: str = "jimeng"
    ) -> dict:
        """调用即梦视频生成 API。"""
        if not self._browser_client:
            await self.init()

        try:
            # 确保浏览器就绪（使用混入的方法）
            if not hasattr(self._browser_client, 'ensure_jimeng_ready'):
                raise RuntimeError("Browser client does not have Jimeng support. Check browser_client._jimeng integration.")
            
            await self._browser_client.ensure_jimeng_ready(headless=True)
            
            # 构建请求体
            model_req_key = self._get_model_req_key(model)
            resolution = self._determine_resolution(width, height)
            duration_ms = duration * 1000
            
            # 生成 seed
            if seed is None:
                seed = int(time.time() * 1000) % 2**31
            
            # 构建draft_content
            draft_content = self._build_draft_content(
                prompt=prompt,
                image=image,
                model_req_key=model_req_key,
                resolution=resolution,
                duration_ms=duration_ms,
                fps=fps,
                seed=seed
            )
            
            # 构建完整的 API 请求体
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
                # workspace_id 可选，可以从 account 中获�取
            }
            
            metrics_extra = self._build_metrics_extra(
                submit_id=submit_id,
                model_req_key=model_req_key,
                resolution=resolution,
                duration=duration
            )
            
            body = {
                "extend": extend,
                "submit_id": submit_id,
                "metrics_extra": metrics_extra,
                "draft_content": draft_content,
                "http_common_info": {
                    "aid": CONFIG.get("aid", 513695)
                }
            }
            
            # 通过浏览器页面调用 API
            if not hasattr(self._browser_client, 'call_video_generate_api'):
                raise RuntimeError("Browser client missing call_video_generate_api method")
            
            result = await self._browser_client.call_video_generate_api(body)
            
            # 解析响应
            if result.get("ret") != "0":
                error_msg = result.get("errmsg", "Unknown error")
                logger.error(f"Jimeng API error: {error_msg}")
                return {
                    "error": True,
                    "message": error_msg,
                    "task_id": None
                }
            
            task_data = result.get("data", {})
            task_info = task_data.get("task", {})
            task_id = task_info.get("task_id")
            
            if not task_id:
                logger.error("Jimeng API returned no task_id")
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
                "submit_id": submit_id,
                "queue_position": task_data.get("queue_info", {}).get("queue_idx", 0),
                "queue_length": task_data.get("queue_info", {}).get("queue_length", 0)
            }
            
        except Exception as e:
            logger.error(f"Jimeng video generation failed: {e}")
            return {
                "error": True,
                "message": str(e),
                "task_id": None
            }

    async def get_video_status(self, task_id: str) -> dict:
        """获取视频生成任务状态。"""
        if not self._browser_client:
            await self.init()
        
        try:
            if not hasattr(self._browser_client, 'call_history_api'):
                raise RuntimeError("Browser client missing call_history_api method")
            
            # 通过 submit_id 查询
            result = await self._browser_client.call_history_api([task_id])
            
            if result.get("ret") != "0":
                logger.error(f"Jimeng history API error: {result.get('errmsg')}")
                return {"error": True, "message": result.get("errmsg", "Unknown error")}
            
            data = result.get("data", {})
            # 根据 submit_id 查找任务
            task_data = data.get(task_id, {})
            if not task_data:
                return {"error": True, "message": "Task not found"}
            
            status_code = task_data.get("status", 0)
            status = STATUS_MAP.get(status_code, "unknown")
            
            response = {
                "task_id": task_id,
                "status": status,
                "status_code": status_code,
                "created_at": task_data.get("created_time", 0),
                "model": "jimeng"
            }
            
            # 如果完成，提取视频信息
            if status == "completed":
                item_list = task_data.get("item_list", [])
                if item_list and len(item_list) > 0:
                    video_info = self._extract_video_info(item_list[0])
                    response.update(video_info)
            
            return response
            
        except Exception as e:
            logger.error(f"Jimeng get_status failed: {e}")
            return {"error": True, "message": str(e)}

    def _get_model_req_key(self, model: str) -> str:
        """获取模型请求 key。"""
        model_info = JIMENG_MODELS.get(model, JIMENG_MODELS.get("jimeng", {}))
        return model_info.get("model_req_key", "dreamina_seedance_40_pro")

    def _determine_resolution(self, width: int, height: int) -> str:
        """确定分辨率字符串。"""
        if width >= 1920 and height >= 1080:
            return "1080p"
        elif width >= 1280 and height >= 720:
            return "720p"
        else:
            return "720p"

    def _build_draft_content(self, prompt: str, image: Optional[str], model_req_key: str,
                           resolution: str, duration_ms: int, fps: int, seed: int) -> str:
        """构建 draft_content JSON。"""
        # 这里需要根据即梦的完整 draft_content schema 构建
        # 简化版本，仅包含必要字段
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
                                        "video_mode": 1 if image else 2,
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
        
        # 如果有图片，添加到 video_gen_inputs
        if image:
            draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"]["video_gen_inputs"][0]["image"] = image
        
        return json.dumps(draft, ensure_ascii=False)

    def _build_metrics_extra(self, submit_id: str, model_req_key: str, resolution: str, duration: int) -> str:
        """构建 metrics_extra 字段。"""
        metrics = {
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
                "videoDuration": duration,
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
        }
        return json.dumps(metrics, ensure_ascii=False)

    def _extract_video_info(self, item: dict) -> dict:
        """从任务结果中提取视频信息。"""
        result = {}
        
        # 视频 URL 可能在多个位置
        video_data = item.get("video_data", {})
        video_url = video_data.get("main_url") or video_data.get("url", "")
        
        if not video_url:
            # 尝试从 item 的其它字段提取
            for key in ["video_url", "url", "download_url"]:
                if item.get(key):
                    video_url = item[key]
                    break
        
        if video_url:
            result["video_url"] = video_url
            # 提取元数据
            result.update({
                "duration": item.get("duration_sec") or item.get("duration", 0),
                "width": item.get("width") or item.get("video_width", 0),
                "height": item.get("height") or item.get("video_height", 0),
                "format": item.get("format", "mp4"),
                "thumbnail_url": item.get("cover_url") or item.get("thumbnail", "")
            })
        
        return result
