import logging
from typing import Optional

from adapters.base import BaseAdapter
from adapters.doubao import DoubaoAdapter
from adapters.doubao_video import DoubaoVideoAdapter
from adapters.qianwen import QianwenAdapter
from adapters.deepseek import DeepseekAdapter
from adapters.zai import ZaiAdapter
from adapters.mimo import MimoAdapter
from adapters.minimax import MinimaxAdapter
from adapters.xinghuo import XinghuoAdapter
from adapters.kimi import KimiAdapter
from adapters.jimeng import JimengAdapter
from adapters.meta import MetaAdapter
from models import MODEL_CONFIG, KIMI_MODEL_CONFIG

logger = logging.getLogger("webchat-api")

_ADAPTER_INSTANCES: dict[str, BaseAdapter] = {}

_MODEL_ADAPTER_MAP: dict[str, str] = {
    "doubao-": "doubao",
    "doubao-video": "doubao_video",
    "doubao-video-": "doubao_video",
    "qianwen-": "qianwen",
    "deepseek-": "deepseek",
    "deepseek": "deepseek",
    "zai-": "zai",
    "mimo-": "mimo",
    "minimax-": "minimax",
    "xinghuo-": "xinghuo",
    "kimi-": "kimi",
    "jimeng-": "jimeng",
    "jimeng": "jimeng",
    # Anthropic 兼容模型映射到 doubao
    "claude-3-5-sonnet": "doubao",
    "claude-3-5-haiku": "doubao",
    "claude-3-opus": "doubao",
    "claude-sonnet-4": "doubao",
}

_DEFAULT_ADAPTER = "doubao"

_IMAGE_ADAPTER_MAP: dict[str, str] = {
    "doubao-image": "doubao",
    "doubao-": "doubao",
    "meta-image": "meta",
    "meta-": "meta",
}

_DEFAULT_IMAGE_ADAPTER = "doubao"


def _init_adapters():
    """创建所有适配器实例。"""
    if _ADAPTER_INSTANCES:
        return

    doubao = DoubaoAdapter()
    _ADAPTER_INSTANCES["doubao"] = doubao

    doubao_video = DoubaoVideoAdapter()
    _ADAPTER_INSTANCES["doubao_video"] = doubao_video

    qianwen = QianwenAdapter()
    _ADAPTER_INSTANCES["qianwen"] = qianwen

    deepseek = DeepseekAdapter()
    _ADAPTER_INSTANCES["deepseek"] = deepseek

    zai = ZaiAdapter()
    _ADAPTER_INSTANCES["zai"] = zai

    mimo = MimoAdapter()
    _ADAPTER_INSTANCES["mimo"] = mimo

    minimax = MinimaxAdapter()
    _ADAPTER_INSTANCES["minimax"] = minimax

    xinghuo = XinghuoAdapter()
    _ADAPTER_INSTANCES["xinghuo"] = xinghuo

    kimi = KimiAdapter()
    _ADAPTER_INSTANCES["kimi"] = kimi

    jimeng = JimengAdapter()
    _ADAPTER_INSTANCES["jimeng"] = jimeng

    meta = MetaAdapter()
    _ADAPTER_INSTANCES["meta"] = meta

    for name, adapter in _ADAPTER_INSTANCES.items():
        logger.info(f"Adapter registered: {name} ({len(adapter.get_models())} models)")


def get_models() -> dict[str, dict]:
    """返回所有适配器的模型列表（合并）。"""
    _init_adapters()
    result = {}
    for adapter in _ADAPTER_INSTANCES.values():
        result.update(adapter.get_models())
    return result


def get_adapter(model: str) -> Optional[BaseAdapter]:
    """根据模型名称获取对应的适配器实例。"""
    _init_adapters()

    # 1. 精确匹配
    if model in _MODEL_ADAPTER_MAP:
        name = _MODEL_ADAPTER_MAP[model]
        return _ADAPTER_INSTANCES.get(name)

    # 2. 前缀匹配
    for prefix, name in _MODEL_ADAPTER_MAP.items():
        if prefix.endswith("-") and model.startswith(prefix):
            return _ADAPTER_INSTANCES.get(name)

    # 3. 遍历所有适配器检查是否支持该模型
    for adapter in _ADAPTER_INSTANCES.values():
        if adapter.supports_model(model):
            return adapter

    # 4. 默认适配器
    return _ADAPTER_INSTANCES.get(_DEFAULT_ADAPTER)


async def init_all():
    """初始化所有适配器。"""
    _init_adapters()
    for name, adapter in _ADAPTER_INSTANCES.items():
        try:
            await adapter.init()
            logger.info(f"Adapter initialized: {name}")
        except Exception as e:
            logger.warning(f"Adapter {name} init failed: {e}")


async def close_all():
    """关闭所有适配器。"""
    for name, adapter in _ADAPTER_INSTANCES.items():
        try:
            await adapter.close()
            logger.info(f"Adapter closed: {name}")
        except Exception as e:
            logger.warning(f"Adapter {name} close error: {e}")


def get_image_adapter(model: str) -> Optional[BaseAdapter]:
    """根据模型名称获取支持图片生成的适配器实例。"""
    _init_adapters()

    # 1. 精确匹配
    if model in _IMAGE_ADAPTER_MAP:
        name = _IMAGE_ADAPTER_MAP[model]
        return _ADAPTER_INSTANCES.get(name)

    # 2. 前缀匹配
    for prefix, name in _IMAGE_ADAPTER_MAP.items():
        if prefix.endswith("-") and model.startswith(prefix):
            return _ADAPTER_INSTANCES.get(name)

    # 3. 遍历所有适配器检查是否实现了 generate_images
    for adapter in _ADAPTER_INSTANCES.values():
        if hasattr(adapter, 'generate_images'):
            return adapter

    # 4. 默认适配器
    return _ADAPTER_INSTANCES.get(_DEFAULT_IMAGE_ADAPTER)