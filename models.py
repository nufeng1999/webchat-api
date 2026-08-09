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
    "meta-image": {"bot_id": "", "use_deep_think": False, "use_auto_cot": False, "desc": "Meta 图片生成（文生图）", "is_image_model": True},
    "doubao-podcast": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "AI播客生成", "is_podcast_model": True},
    "doubao-music": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "AI音乐生成", "is_music_model": True},
}

DEEPSEEK_MODEL_CONFIG = {
    "deepseek-normal": {"model_type": "default", "desc": "快速模式 (适合日常对话)", "use_deep_think": False, "use_search": True, "supports_file": True},
    "deepseek-thinking": {"model_type": "default", "desc": "深度思考模式", "use_deep_think": True, "use_search": True, "supports_file": True},
    "deepseek-search": {"model_type": "default", "desc": "智能搜索模式", "use_deep_think": False, "use_search": True, "supports_file": True},
    "deepseek-expert": {"model_type": "expert", "desc": "专家模式 (复杂问题)", "use_deep_think": True, "use_search": False, "supports_file": False},
    "deepseek-vision": {"model_type": "vision", "desc": "识图模式", "use_deep_think": False, "use_search": False, "supports_file": True},
}

ZAI_MODEL_CONFIG = {
    "zai-glm-5.2": {"model_type": "glm-5.2", "desc": "GLM-5.2 (NEW - 旗舰模型，擅长编程与长程任务)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "GLM-5.2"},
    "zai-glm-5.1": {"model_type": "glm-5.1", "desc": "GLM-5.1 (上一代旗舰模型)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "GLM-5.1"},
    "zai-glm-5-turbo": {"model_type": "glm-5-turbo", "desc": "GLM-5-Turbo (最新日常任务处理，编程与智能体模型)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "GLM-5-Turbo"},
    "zai-glm-5v-turbo": {"model_type": "glm-5v-turbo", "desc": "GLM-5V-Turbo (新一代视觉模型，视觉理解全面升级)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "GLM-5V-Turbo"},
    "zai-glm-4.7": {"model_type": "glm-4.7", "desc": "GLM-4.7 (经典强大模型)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "GLM-4.7"},
}

MIMO_MODEL_CONFIG = {
    "mimo-v2.5-pro": {"model_type": "default", "desc": "MiMo-V2.5-Pro (资源密集型旗舰)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "MiMo-V2.5-Pro"},
    "mimo-v2.5": {"model_type": "default", "desc": "MiMo-V2.5 (全模态基础模型)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "MiMo-V2.5"},
    "mimo-v2.5-tts": {"model_type": "default", "desc": "MiMo-V2.5-TTS (语音合成模型)", "use_deep_think": False, "use_search": False, "supports_file": False, "display_name": "MiMo-V2.5-TTS"},
    "mimo-v2.5-tts-voicedesign": {"model_type": "default", "desc": "MiMo-V2.5-TTS-VoiceDesign (音色合成模型)", "use_deep_think": False, "use_search": False, "supports_file": False, "display_name": "MiMo-V2.5-TTS-VoiceDesign"},
    "mimo-v2.5-tts-voiceclone": {"model_type": "default", "desc": "MiMo-V2.5-TTS-VoiceClone (音色克隆模型)", "use_deep_think": False, "use_search": False, "supports_file": False, "display_name": "MiMo-V2.5-TTS-VoiceClone"},
}

MINIMAX_MODEL_CONFIG = {
    "minimax-m3": {"model_type": "m3", "desc": "MiniMax-M3 (旗舰多模态模型)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "MiniMax-M3"},
}

XINGHUO_MODEL_CONFIG = {
    "xinghuo-4.0-ultra": {"model_type": "4.0-ultra", "desc": "讯飞星火 4.0 Ultra (旗舰模型)", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "星火 4.0 Ultra"},
    "xinghuo-4.0": {"model_type": "4.0", "desc": "讯飞星火 4.0", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "星火 4.0"},
    "xinghuo-3.5": {"model_type": "3.5", "desc": "讯飞星火 3.5", "use_deep_think": False, "use_search": False, "supports_file": True, "display_name": "星火 3.5"},
}

KIMI_MODEL_CONFIG = {
    "kimi-k2.7-code": {"model_type": "k2.7-code", "desc": "Kimi K2.7 Code (最强 Coding 模型)", "use_deep_think": True, "use_search": True, "supports_file": True, "display_name": "Kimi K2.7 Code"},
    "kimi-k2.7-code-highspeed": {"model_type": "k2.7-code-highspeed", "desc": "Kimi K2.7 Code Highspeed (高速版)", "use_deep_think": True, "use_search": True, "supports_file": True, "display_name": "Kimi K2.7 Code Highspeed"},
    "kimi-k2.6": {"model_type": "k2.6", "desc": "Kimi K2.6 (综合能力强，支持多模态)", "use_deep_think": True, "use_search": True, "supports_file": True, "display_name": "Kimi K2.6"},
    "kimi-k2.5": {"model_type": "k2.5", "desc": "Kimi K2.5 (支持视觉与文本输入)", "use_deep_think": True, "use_search": True, "supports_file": True, "display_name": "Kimi K2.5"},
}

SYSTEM_PROMPT_MAP = {
    "doubao-coding": "你是一个专业的编程助手，擅长多种编程语言，能够编写、调试、优化代码，并解释技术概念。请用代码块格式输出代码。",
    "doubao-writing": "你是一个专业的写作助手，擅长各类文体写作，包括公文、邮件、文案、小说、论文等。请根据用户需求生成高质量的结构化文本。",
    "doubao-translator": "你是一个专业的翻译助手，支持多语言互译，自动检测源语言，保持原文语义和语气。请直接输出翻译结果，不要添加额外解释。",
    "doubao-tutor": "你是一个专业的解题答疑老师，擅长数学、物理、化学等学科。请逐步分析问题，给出详细的解题过程和答案，标注关键步骤和易错点。",
    "doubao-data-analyst": "你是一个专业的数据分析师，擅长数据分析、可视化和Python编程。请根据用户描述的数据生成分析代码，使用pandas、matplotlib等库，确保代码可运行且有注释。注意：代码是生成供用户自行执行，不要试图直接运行代码。",
    "doubao-image": "你是一个专业的AI图片生成助手。当用户描述想要的图片时，请直接使用你的图片生成能力创建图片。不需要过多文字说明，直接生成图片即可。",
    "meta-image": "你是一个专业的AI图片生成助手。当用户描述想要的图片时，请直接使用你的图片生成能力创建图片。不需要过多文字说明，直接生成图片即可。",
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


class ImageGenerationRequest(BaseModel):
    """OpenAI 兼容的图片生成请求模型。"""
    model: str = "doubao-image"
    prompt: str
    n: int = Field(default=1, ge=1, le=10)
    size: str = "1024x1024"
    quality: Optional[str] = None
    style: Optional[str] = None
    response_format: str = "url"
    user: Optional[str] = None
    metadata: Optional[dict] = None

    model_config = ConfigDict(extra='allow')


class VideoGenerationRequest(BaseModel):
    """视频生成请求模型。"""
    model: Optional[str] = None
    prompt: Optional[str] = None
    image: Optional[str] = None
    duration: Optional[int] = Field(default=None, ge=1, le=60)
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    seed: Optional[int] = None
    n: int = Field(default=1, ge=1, le=4)
    response_format: Optional[str] = None
    user: Optional[str] = None
    metadata: Optional[dict] = None

    model_config = ConfigDict(extra='allow')
