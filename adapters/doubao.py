from typing import AsyncGenerator

from adapters.base import BaseAdapter
from models import ChatCompletionRequest, MODEL_CONFIG

DOUBAO_MODELS = {k: v for k, v in MODEL_CONFIG.items()}


class DoubaoAdapter(BaseAdapter):
    """Doubao (豆包) 适配器，复用现有 openai_api / browser_client 逻辑。"""

    def get_adapter_name(self) -> str:
        return "doubao"

    def get_models(self) -> dict[str, dict]:
        return DOUBAO_MODELS

    async def init(self):
        pass

    async def close(self):
        pass

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncGenerator[bytes, None]:
        from openai_api import stream_chat_completion
        async for chunk in stream_chat_completion(request):
            yield chunk

    async def non_stream_chat(self, request: ChatCompletionRequest) -> dict:
        from openai_api import non_stream_chat_completion
        return await non_stream_chat_completion(request)