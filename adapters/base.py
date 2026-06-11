from abc import ABC, abstractmethod
from typing import AsyncGenerator
from models import ChatCompletionRequest


class BaseAdapter(ABC):
    """多站点适配器基类。每个适配器实现一个目标站点的聊天能力。"""

    @abstractmethod
    def get_adapter_name(self) -> str:
        ...

    @abstractmethod
    def get_models(self) -> dict[str, dict]:
        """返回该适配器支持的模型列表 {model_id: {desc, ...}}"""
        ...

    def get_model_ids(self) -> list[str]:
        return list(self.get_models().keys())

    def supports_model(self, model: str) -> bool:
        return model in self.get_models()

    @abstractmethod
    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        """流式聊天，产出 OpenAI 格式的 SSE chunk bytes"""
        ...

    @abstractmethod
    async def non_stream_chat(self, request: ChatCompletionRequest) -> dict:
        """非流式聊天，返回 OpenAI 格式的响应 dict"""
        ...

    async def init(self):
        """适配器初始化（浏览器启动、认证等），在 lifespan 中调用"""
        pass

    async def close(self):
        """适配器清理，在 lifespan 结束时调用"""
        pass