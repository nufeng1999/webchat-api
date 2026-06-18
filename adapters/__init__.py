import logging
from typing import Optional

from adapters.base import BaseAdapter
from adapters.doubao import DoubaoAdapter
from adapters.qianwen import QianwenAdapter
from models import MODEL_CONFIG

logger = logging.getLogger("webchat-api")

_ADAPTER_INSTANCES: dict[str, BaseAdapter] = {}

# 模型名到适配器的映射规则：doubao-* → doubao, qianwen-* → qianwen
# 优先级：精确匹配 > 前缀匹配 > 默认适配器
_MODEL_ADAPTER_MAP: dict[str, str] = {
    # 前缀匹配规则
    "doubao-": "doubao",
    "qianwen-": "qianwen",
    # Anthropic 兼容模型映射到 doubao
    "claude-3-5-sonnet": "doubao",
    "claude-3-5-haiku": "doubao",
    "claude-3-opus": "doubao",
    "claude-sonnet-4": "doubao",
}

_DEFAULT_ADAPTER = "doubao"


def _init_adapters():
    """创建所有适配器实例。"""
    if _ADAPTER_INSTANCES:
        return

    doubao = DoubaoAdapter()
    _ADAPTER_INSTANCES["doubao"] = doubao

    qianwen = QianwenAdapter()
    _ADAPTER_INSTANCES["qianwen"] = qianwen

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