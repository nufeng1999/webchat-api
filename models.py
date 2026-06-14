from typing import Union, Optional
from pydantic import BaseModel, Field, ConfigDict

MODEL_CONFIG = {
    "doubao-pro-chat": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "快速模式 (Doubao-Seed-2.0-Mini)"},
    "doubao-lite-chat": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "轻量模式"},
    "doubao-thinking": {"bot_id": "7338286299411103781", "use_deep_think": True, "use_auto_cot": False, "desc": "思考模式 (Doubao-Seed-2.0-lite)"},
    "doubao-expert": {"bot_id": "7338286299411103781", "use_deep_think": True, "use_auto_cot": True, "use_search": True, "desc": "专家/超能模式"},
    "doubao-pro-32k": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "Pro 32K"},
    "doubao-pro-128k": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "Pro 128K"},
    "doubao-coding": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": True, "desc": "编程模式 (Doubao-Seed-Code)"},
    "doubao-writing": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "写作助手"},
    "doubao-translator": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "翻译"},
    "doubao-tutor": {"bot_id": "7338286299411103781", "use_deep_think": True, "use_auto_cot": False, "desc": "解题答疑"},
    "doubao-data-analyst": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": True, "desc": "数据分析师（生成分析代码）"},
    "doubao-image": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "图片生成（文生图）", "is_image_model": True},
    "doubao-podcast": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "AI播客生成", "is_podcast_model": True},
    "doubao-music": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "AI音乐生成", "is_music_model": True},
}

SYSTEM_PROMPT_MAP = {
    "doubao-coding": "你是一个专业的编程助手，擅长多种编程语言，能够编写、调试、优化代码，并解释技术概念。请用代码块格式输出代码。",
    "doubao-writing": "你是一个专业的写作助手，擅长各类文体写作，包括公文、邮件、文案、小说、论文等。请根据用户需求生成高质量的结构化文本。",
    "doubao-translator": "你是一个专业的翻译助手，支持多语言互译，自动检测源语言，保持原文语义和语气。请直接输出翻译结果，不要添加额外解释。",
    "doubao-tutor": "你是一个专业的解题答疑老师，擅长数学、物理、化学等学科。请逐步分析问题，给出详细的解题过程和答案，标注关键步骤和易错点。",
    "doubao-data-analyst": "你是一个专业的数据分析师，擅长数据分析、可视化和Python编程。请根据用户描述的数据生成分析代码，使用pandas、matplotlib等库，确保代码可运行且有注释。注意：代码是生成供用户自行执行，不要试图直接运行代码。",
    "doubao-image": "你是一个专业的AI图片生成助手。当用户描述想要的图片时，请直接使用你的图片生成能力创建图片。不需要过多文字说明，直接生成图片即可。",
}

ANTHROPIC_MODEL_MAP = {
    "claude-3-5-sonnet-latest": "doubao-pro-chat",
    "claude-3-5-sonnet-20241022": "doubao-pro-chat",
    "claude-3-5-haiku-latest": "doubao-lite-chat",
    "claude-3-haiku-20240307": "doubao-lite-chat",
    "claude-3-opus-latest": "doubao-expert",
    "claude-sonnet-4-20250514": "doubao-pro-chat",
    "claude-sonnet-4-5-20250929": "doubao-pro-chat",
}


class ChatMessage(BaseModel):
    role: str = "user"
    content: Union[str, list, None] = ""
    name: Optional[str] = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = "doubao-pro-chat"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    conversation_id: Optional[str] = None
    tools: Optional[list] = None
    tool_choice: Optional[Union[str, dict]] = None
    stream_options: Optional[dict] = None
    user: Optional[str] = None
    metadata: Optional[dict] = None

    model_config = ConfigDict(extra='allow')


class AnthropicMessageRequest(BaseModel):
    model: str = "claude-3-5-sonnet-latest"
    messages: list[dict]
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    stream: bool = False
    temperature: float = Field(default=0.7, ge=0, le=1)
    system: Optional[Union[str, list]] = None
    stop_sequences: Optional[list[str]] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
