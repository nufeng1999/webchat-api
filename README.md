# WebChat Free API

将豆包、千问、DeepSeek、z.ai（智谱）、MiMo（小米）、MiniMax、讯飞星火 7 大 AI 平台的对话能力包装为标准 OpenAI / Anthropic 兼容 API，供 Claude Code、OpenCode 等 AI Agent 软件直接调用。

## 功能特性

- **OpenAI 兼容** — 完全兼容 `/v1/chat/completions`，支持流式/非流式、Tool Calls、Function Calling
- **Anthropic 兼容** — 完全兼容 `/v1/messages`，Claude Code 原生对接
- **7 大平台统一接入** — 豆包、千问、DeepSeek、z.ai、MiMo、MiniMax、讯飞星火
- **Vision 图片识别** — 支持 OpenAI Vision 格式，自动上传图片
- **图片生成** — 兼容 OpenAI `/v1/images/generations`
- **AI 音乐生成** — 歌词 + 音频自动生成，Web 端播放
- **AI 播客生成** — 脚本 + 火山引擎 TTS 音频，前置/后置音乐
- **Agent 模式** — Tool Calls、System Prompt、多轮工具调用
- **多账号池** — Cookie 轮询 + 自动故障转移
- **限流与并发控制** — 按平台独立限流，避免触发风控
- **JSON 完整性保障** — 自动过滤思考过程前缀、修复不完整 JSON、修补为标准 OpenAI 格式
- **管理面板** — Web UI 聊天、日志查看、账号管理、对话导出/导入、明暗主题

## 快速开始

### 1. 安装依赖

```bash
pip install aiohttp fastapi uvicorn httpx pycryptodome requests-aws4auth python-multipart playwright websockets edge-tts
playwright install chromium
```

### 2. 登录平台

首次使用需登录各平台（会打开浏览器窗口，登录后自动保存 Cookie）：

```bash
# 登录豆包
python main.py --login doubao

# 登录千问
python main.py --login qianwen

# 登录 DeepSeek
python main.py --login deepseek

# 登录 z.ai（智谱）
python main.py --login zai

# 登录 MiMo（小米）
python main.py --login mimo

# 登录 MiniMax
python main.py --login minimax

# 登录讯飞星火
python main.py --login xinghuo
```

### 3. 启动服务

```bash
python main.py
```

服务启动后监听 `http://localhost:8765`，浏览器打开即可使用管理面板。

### 4. 验证服务

```bash
# 健康检查
curl http://localhost:8765/health

# 查看模型列表
curl http://localhost:8765/v1/models

# 非流式对话
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-pro-chat","messages":[{"role":"user","content":"你好"}]}'

# 流式对话
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-normal","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 可用模型

### 豆包 Doubao

| 模型 ID | 说明 |
|---------|------|
| `doubao-pro-chat` | 快速模式（默认），Doubao-Seed-2.0-Mini |
| `doubao-lite-chat` | 轻量模式 |
| `doubao-thinking` | 思考模式，深度推理 |
| `doubao-expert` | 超能模式，自动搜索 + 深度分析 |
| `doubao-pro-32k` | Pro 32K 长上下文 |
| `doubao-pro-128k` | Pro 128K 超长上下文 |
| `doubao-coding` | 编程模式，Doubao-Seed-Code |
| `doubao-writing` | 写作助手 |
| `doubao-translator` | 翻译 |
| `doubao-tutor` | 解题答疑 |
| `doubao-data-analyst` | 数据分析师 |
| `doubao-image` | 图片生成（文生图） |
| `doubao-podcast` | AI 播客生成 |
| `doubao-music` | AI 音乐生成 |

### DeepSeek

| 模型 ID | 说明 |
|---------|------|
| `deepseek-normal` | 快速模式（支持文件上传） |
| `deepseek-thinking` | 深度思考模式（支持文件） |
| `deepseek-search` | 智能搜索模式（支持文件） |
| `deepseek-expert` | 专家模式，复杂问题 |
| `deepseek-vision` | 识图模式（支持文件） |

### z.ai（智谱）

| 模型 ID | 说明 |
|---------|------|
| `zai-glm-5.2` | GLM-5.2 旗舰（编程 + 长程任务） |
| `zai-glm-5.1` | GLM-5.1 上一代旗舰 |
| `zai-glm-5-turbo` | GLM-5-Turbo（日常 + 编程 + Agent） |
| `zai-glm-5v-turbo` | GLM-5V-Turbo 视觉模型 |
| `zai-glm-4.7` | GLM-4.7 经典强大模型 |

### 千问 Qianwen

| 模型 ID | 说明 |
|---------|------|
| `qianwen-pro-chat` | 千问 Pro（Qwen Max） |
| `qianwen-lite-chat` | 千问 Lite（Qwen Turbo） |
| `qianwen-thinking` | 思考模式 |
| `qianwen-coding` | 编程模式（Qwen Coder） |
| `qianwen-3.7` | Qwen3.7 |
| `qianwen-3.7-max` | Qwen3.7-Max |
| `qianwen-3.5-flash` | Qwen3.5-Flash |
| `qianwen-3-max` | Qwen3-Max |
| `qianwen-3-max-thinking` | Qwen3-Max-Thinking |
| `qianwen-3-coder` | Qwen3-Coder |

### MiMo（小米）

| 模型 ID | 说明 |
|---------|------|
| `mimo-v2.5-pro` | MiMo-V2.5-Pro 旗舰 |
| `mimo-v2.5` | MiMo-V2.5 全模态基础 |
| `mimo-v2.5-tts` | 语音合成 |
| `mimo-v2.5-tts-voicedesign` | 音色合成 |
| `mimo-v2.5-tts-voiceclone` | 音色克隆 |

### MiniMax

| 模型 ID | 说明 |
|---------|------|
| `minimax-m3` | MiniMax-M3 旗舰多模态 |

### 讯飞星火 Xinghuo

| 模型 ID | 说明 |
|---------|------|
| `xinghuo-4.0-ultra` | 星火 4.0 Ultra 旗舰 |
| `xinghuo-4.0` | 星火 4.0 |
| `xinghuo-3.5` | 星火 3.5 |

### Anthropic Claude 模型映射

| Claude 模型 | 映射到 |
|-------------|--------|
| `claude-3-5-sonnet-latest` | `doubao-pro-chat` |
| `claude-3-5-haiku-latest` | `doubao-lite-chat` |
| `claude-3-opus-latest` | `doubao-expert` |
| `claude-sonnet-4-*` | `doubao-pro-chat` |

## 对接 AI Agent

### Claude Code（Anthropic 原生）

```bash
ANTHROPIC_API_KEY=any-string ANTHROPIC_BASE_URL=http://localhost:8765 claude
```

### Claude Code（OpenAI 兼容）

```bash
OPENAI_API_BASE=http://localhost:8765/v1 OPENAI_API_KEY=sk-doubao claude
```

### OpenCode

```json
{
  "provider": "openai",
  "api_base": "http://localhost:8765/v1",
  "api_key": "any-string",
  "model": "doubao-pro-chat"
}
```

### Python SDK

```python
from openai import OpenAI

client = OpenAI(api_key="any-string", base_url="http://localhost:8765/v1")

# 非流式
response = client.chat.completions.create(
    model="zai-glm-5.2",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="deepseek-normal",
    messages=[{"role": "user", "content": "你好"}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## API 端点

### 核心对话

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 对话补全（流式/非流式） |
| `/v1/messages` | POST | Anthropic Messages API（流式/非流式） |
| `/v1/models` | GET | 模型列表（含能力描述） |

### 图片

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/images/upload` | POST | 上传图片文件 |
| `/v1/images/generations` | POST | 图片生成（OpenAI 兼容） |

### 音乐

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/music/generate` | POST | 生成音乐 |
| `/v1/music/status/{task_id}` | GET | 查询状态 |
| `/v1/music/audio/{task_id}` | GET | 获取音频 |
| `/v1/music/lyric/{task_id}` | GET | 获取歌词 |
| `/v1/music/list` | GET | 任务列表 |
| `/v1/music/styles` | GET | 风格列表 |

### 播客

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/podcast/generate` | POST | 生成播客 |
| `/v1/podcast/status/{task_id}` | GET | 查询状态 |
| `/v1/podcast/audio/{task_id}` | GET | 获取音频 |
| `/v1/podcast/script/{task_id}` | GET | 获取脚本 |
| `/v1/podcast/list` | GET | 任务列表 |
| `/v1/podcast/file/{filename}` | GET | 文件下载 |
| `/v1/podcast/config` | GET/POST | 配置（前置/后置音乐） |

### 管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 + 功能状态 |
| `/v1/status` | GET | 服务状态 |
| `/logs/today` | GET | 今日日志 |
| `/logs/{date}` | GET | 指定日期日志 |
| `/accounts` | GET/POST | 账号池管理 |
| `/accounts/{name}` | DELETE | 删除账号 |
| `/api/conversations` | GET/POST | 本地对话 CRUD |
| `/api/conversations/{id}` | GET/DELETE | 查看/删除会话 |
| `/api/conversations/search` | GET | 搜索对话 |
| `/v1/conversations/{id}` | DELETE | 删除远端对话 |
| `/v1/conversations/cleanup` | POST | 清理旧对话 |

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--login [platform]` | 登录指定平台 |
| `--host` | 服务器主机地址 |
| `--port` | 服务器端口 |
| `--show-doubao` | 显示豆包浏览器窗口 |
| `--show-qianwen` | 显示千问浏览器窗口 |
| `--show-deepseek` | 显示 DeepSeek 浏览器窗口 |
| `--show-zai` | 显示 z.ai 浏览器窗口 |
| `--show-mimo` | 显示 MiMo 浏览器窗口 |
| `--show-minimax` | 显示 MiniMax 浏览器窗口 |
| `--show-xinghuo` | 显示讯飞星火浏览器窗口 |
| `--keep-conversations` | 保留对话历史（默认关闭时删除） |
| `--browser` | 浏览器引擎（chromium/chrome/edge） |
| `--clear-history [platforms]` | 清除对话历史 |
| `-q/--quiet` | 抑制控制台日志 |
| `--log-level` | 日志级别 |

## 配置

### config.json

```json
{
    "server_host": "0.0.0.0",
    "server_port": 8765,
    "sign_method": "b3",
    "_doubao_headless": true,
    "_qianwen_headless": true,
    "_deepseek_headless": true,
    "_zai_headless": true,
    "_mimo_headless": true,
    "_minimax_headless": true,
    "_xinghuo_headless": true,
    "_keep_conversations": false,
    "conversation_retention_days": 7,
    "request_limiter_max_concurrent": {
        "doubao": 1,
        "qianwen": 1,
        "deepseek": 1,
        "zai": 1,
        "mimo": 1,
        "minimax": 1,
        "xinghuo": 1
    }
}
```

### accounts.json

多账号 Cookie 池，支持按平台配置多个账号，自动轮询和故障转移。

### Prompt 配置

`my_prompt/` 目录下支持按适配器定制 prompt：
- `request_task.md` — 请求任务指令
- `exectask_prompt.md` — 执行任务指令
- `ret_format_task.md` — 结果格式化指令
- `{adapter}_*.md` — 特定适配器的定制指令

## 项目结构

```
webchat-api/
├── main.py                  # FastAPI 入口 + 路由
├── config.py                # 配置加载 + Cookie池 + 限流
├── models.py                # 数据模型 + 模型配置
├── sse.py                   # SSE 解析 + 格式化
├── openai_api.py            # OpenAI 兼容逻辑
├── anthropic_api.py         # Anthropic 兼容逻辑
├── browser_client.py        # Playwright 浏览器自动化客户端
├── adapters/                # 适配器层
│   ├── __init__.py          # 适配器注册 + 模型路由
│   ├── base.py              # 基类（模板方法 + JSON过滤/修补）
│   ├── doubao.py            # 豆包适配器
│   ├── qianwen.py           # 千问适配器
│   ├── deepseek.py          # DeepSeek 适配器
│   ├── zai.py               # z.ai 适配器
│   ├── mimo.py              # MiMo 适配器
│   ├── minimax.py           # MiniMax 适配器
│   └── xinghuo.py           # 讯飞星火适配器
├── music.py                 # 音乐生成
├── podcast.py               # 播客生成
├── volcengine_tts.py        # 火山引擎 TTS
├── storage.py               # SQLite 持久存储
├── exporter.py              # 对话导出
├── uploader.py              # 图片上传
├── json_fixer.py            # JSON 修复器
├── wsession_store.py        # Web 会话映射
├── index.html               # 管理面板前端
├── config.json              # 配置（自动生成）
├── accounts.json            # 账号池
└── config.example.json      # 配置示例
```

## 技术原理

### 适配器架构

采用**模板方法模式**，`BaseAdapter` 提供统一的流式处理、JSON 过滤、响应修补、重试逻辑，各适配器只需覆盖 Hook 方法：

- `_prepare_messages()` — 准备请求数据
- `_call_stream()` — 调用浏览器客户端流式方法
- `_on_session_id()` — 处理会话 ID
- `_delete_conversation()` — 删除对话

### JSON 完整性保障

`BaseAdapter` 内置两层防护：

1. **JSON 前后过滤器** (`_strip_json_prefix` + `_normalize_openai_payload_text`)：剥离 `思考过程`、`reasoning`、`json` 等非 JSON 前缀，仅保留完整 JSON 主体
2. **OpenAI API JSON 修补器** (`_parse_response` + `JsonFixer`)：修复不完整 JSON（缺括号、转义错误等），自动提取 `content`/`tool_calls`/`finish_reason`，修补为标准 OpenAI ChatCompletion 格式

### 浏览器自动化

通过 Playwright 拦截各平台页面的 fetch/XHR 请求，捕获 SSE 流式响应并桥接到 Python。支持 headless 模式和反检测策略。

## 注意事项

- 仅供学习研究，请勿用于商业用途
- Cookie 属于敏感信息，请勿泄露（`config.json` 和 `accounts.json` 已加入 `.gitignore`）
- 各平台 API 可能随时变更，导致服务失效
- 建议不要高频调用，以免触发风控

## License

MIT
