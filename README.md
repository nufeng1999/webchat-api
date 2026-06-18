# Doubao API （Doubao Free API 分支版）

将豆包桌面客户端的对话能力包装为 OpenAI 兼容的标准 API，供 Claude Code、OpenCode 等 AI Agent 软件直接调用。

## ✨ 功能特性

- 🔄 **OpenAI 兼容** — 完全兼容 `/v1/chat/completions` 接口，支持流式/非流式，支持工具调用
- 🤖 **Anthropic 兼容** — 完全兼容 `/v1/messages` 接口，支持 Claude Code 原生对接
- 🖼️ **Vision 图片识别** — 支持 OpenAI Vision 格式，自动上传图片到豆包 ImageX
- 🎨 **图片生成** — 支持 `/v1/images/generations`，兼容 OpenAI Image API
- 🎵 **音乐生成** — 支持 AI 音乐创作，自动生成歌词+音频，Web端直接播放
- 🎙️ **播客生成** — 支持 AI 播客脚本+音频生成，火山引擎原生TTS，前置/后置音乐
- 🧠 **思考模式** — 深度推理，边想边搜
- 💻 **编程模式** — 基于 Doubao-Seed-Code 的代码生成
- ✍️ **写作/翻译/解题** — 7种特殊模式，参数切换
- 📊 **数据分析师** — 生成数据分析代码（pandas/matplotlib）
- 📤 **对话导出/导入** — 从豆包网站抓取已有对话，支持媒体下载，JSON/JSONL 格式
- 💾 **服务端持久存储** — SQLite 数据库存储对话和消息，刷新不丢失
- 🌓 **明暗主题切换** — 白天模式（豆包色系）+ 暗黑模式，自动记忆偏好
- 📊 **管理面板** — Web UI 状态监控、在线对话、日志查看，设置面板集成服务状态
- 👥 **多账号池** — Cookie 轮询 + 自动故障转移
- 🔒 **CloakBrowser** — 隐蔽浏览器自动化，避免反爬检测
- 📝 **对话日志** — 自动记录每次输入输出

## 快速开始

### 1. 安装依赖

```bash
pip install aiohttp fastapi uvicorn httpx pycryptodome requests-aws4auth python-multipart cloakbrowser websockets edge-tts
```

### 2. 提取 Cookie

确保豆包桌面客户端已登录，然后运行提取脚本：

```bash
python temp/extract_session.py
```

脚本会自动从豆包客户端提取 Cookie 和设备参数，并写入 `config.json`。

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
  -d '{"model":"doubao-pro-chat","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 对接 AI Agent

### Claude Code (Anthropic 原生模式)

```bash
ANTHROPIC_API_KEY=any-string ANTHROPIC_BASE_URL=http://localhost:8765 claude
```

Claude Code 使用 Anthropic Messages API (`/v1/messages`)，本服务已完整兼容，包括流式 SSE 事件格式。

### Claude Code (OpenAI 兼容模式)

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
    model="doubao-pro-chat",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="doubao-pro-chat",
    messages=[{"role": "user", "content": "你好"}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## 可用模型

| 模型 ID | 模式 | 说明 |
|---|---|---|
| `doubao-pro-chat` | 快速模式 | 默认，Doubao-Seed-2.0-Mini |
| `doubao-thinking` | 思考模式 | 深度推理，边想边搜 |
| `doubao-expert` | 超能模式 | 自动搜索+深度分析 |
| `doubao-coding` | 编程模式 | Doubao-Seed-Code 代码生成 |
| `doubao-writing` | 写作助手 | 公文/邮件/文案/小说 |
| `doubao-translator` | 翻译 | 多语言互译 |
| `doubao-tutor` | 解题答疑 | 数学/物理/化学逐步解题 |
| `doubao-data-analyst` | 数据分析师 | 生成数据分析代码 |
| `doubao-lite-chat` | 轻量模式 | 轻量快速 |
| `doubao-pro-32k` | Pro 32K | 长上下文 |
| `doubao-pro-128k` | Pro 128K | 超长上下文 |
| `doubao-image` | 图片生成 | 文生图，兼容 OpenAI Image API |
| `doubao-podcast` | 播客生成 | AI 播客脚本+音频 |
| `doubao-music` | 音乐生成 | AI 音乐创作（歌词+音频） |

## Vision 图片识别

支持 OpenAI Vision API 格式，自动上传图片到豆包 ImageX 服务：

```bash
# URL 图片
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao-pro-chat",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
      ]
    }]
  }'
```

```python
# Python SDK - base64 图片
import base64
from openai import OpenAI

client = OpenAI(api_key="any-string", base_url="http://localhost:8765/v1")

with open("photo.jpg", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="doubao-pro-chat",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]
    }]
)
```

支持两种图片输入：
- **HTTP URL**：`https://...` — 自动下载并上传
- **Base64 Data URL**：`data:image/png;base64,...` — 自动解码并上传

## Anthropic Claude Code 兼容

本服务完整实现了 Anthropic Messages API (`/v1/messages`)，Claude Code 可直接使用：

### 模型映射

| Claude 模型 | 豆包模型 |
|---|---|
| `claude-3-5-sonnet-latest` | doubao-pro-chat |
| `claude-3-5-haiku-latest` | doubao-lite-chat |
| `claude-3-opus-latest` | doubao-expert |
| `claude-sonnet-4-*` | doubao-pro-chat |

### 启动 Claude Code

```bash
ANTHROPIC_API_KEY=any-string ANTHROPIC_BASE_URL=http://localhost:8765 claude
```

### 直接调用

```bash
curl -X POST http://localhost:8765/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: any-string" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-latest",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

### Python SDK

```python
from anthropic import Anthropic

client = Anthropic(api_key="any-string", base_url="http://localhost:8765")

# 非流式
message = client.messages.create(
    model="claude-3-5-sonnet-latest",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}]
)
print(message.content[0].text)

# 流式
with client.messages.stream(
    model="claude-3-5-sonnet-latest",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}]
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

### 支持的 Anthropic 特性

- ✅ 流式 SSE（message_start, content_block_start, content_block_delta, message_delta, message_stop）
- ✅ 非流式响应
- ✅ system 提示词（字符串和 content blocks 格式）
- ✅ Vision 图片识别（base64 和 URL）
- ✅ 多轮对话
- ✅ stop_reason（end_turn）
- ✅ usage 统计

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI 对话补全（流式/非流式） |
| `/v1/messages` | POST | Anthropic Messages API（流式/非流式） |
| `/v1/models` | GET | 模型列表（含能力描述） |
| `/v1/images/upload` | POST | 上传图片文件 |
| `/v1/images/generations` | POST | 图片生成（OpenAI 兼容 API） |
| `/v1/music/generate` | POST | 音乐生成 |
| `/v1/music/status/{task_id}` | GET | 音乐生成状态查询 |
| `/v1/music/audio/{task_id}` | GET | 获取音乐音频URL |
| `/v1/music/styles` | GET | 获取音乐风格列表 |
| `/v1/podcast/generate` | POST | 播客生成（自动账号重试） |
| `/v1/podcast/status/{task_id}` | GET | 播客生成状态查询 |
| `/v1/podcast/audio/{task_id}` | GET | 获取播客音频URL |
| `/v1/podcast/script/{task_id}` | GET | 获取播客脚本 |
| `/v1/podcast/list` | GET | 播客任务列表 |
| `/v1/podcast/config` | GET/POST | 播客配置（前置/后置音乐开关） |
| `/v1/user/info` | GET | 获取当前豆包用户信息 |
| `/v1/doubao/conversations` | GET | 获取豆包网站对话列表 |
| `/v1/doubao/conversations/{id}/export` | GET | 导出指定对话（含媒体下载） |
| `/health` | GET | 健康检查 + 功能状态 |
| `/logs/today` | GET | 查看今日对话日志 |
| `/logs/{date}` | GET | 查看指定日期日志 |
| `/accounts` | GET/POST | 账号池管理 |
| `/accounts/{name}` | DELETE | 删除账号 |
| `/conversations/{id}` | GET | 查看会话状态 |

## 图片生成

支持 OpenAI 兼容的 `/v1/images/generations` 端点，可用于生成图片：

```bash
curl -X POST http://localhost:8765/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只可爱的小猫",
    "n": 1,
    "size": "1024x1024"
  }'
```

Python SDK 示例：
```python
from openai import OpenAI

client = OpenAI(api_key="any-string", base_url="http://localhost:8765/v1")

response = client.images.generate(
    model="dall-e-3",  # 模型名可任意，实际使用豆包
    prompt="一只可爱的小猫",
    size="1024x1024",
    n=1
)

print(response.data[0].url)
```

响应格式：
```json
{
  "created": 1715731200,
  "data": [
    {
      "url": "https://...",
      "revised_prompt": "一只可爱的小猫"
    }
  ]
}
```

## 管理面板

浏览器打开 `http://localhost:8765` 即可使用管理面板：

- 📊 **状态页** — 服务状态、账号健康度、模型列表（集成在设置面板中）
- 💬 **对话页** — 在线对话测试，支持图片上传，音乐/播客生成
- 📋 **日志页** — 按日期/关键词/模型筛选日志
- 👤 **账号页** — Cookie 账号池管理
- ⚙️ **设置页** — 服务配置+服务状态+可用模型
- 📤 **导出/导入** — 豆包网站对话抓取、本地对话导出导入
- 🌓 **主题切换** — 白天/暗黑模式，自动记忆

## 播客生成

通过 `doubao-podcast` 模型生成 AI 播客，支持脚本生成 + 原生TTS音频合成：

```bash
# 生成播客
curl -X POST http://localhost:8765/v1/podcast/generate \
  -H "Content-Type: application/json" \
  -d '{"topic": "人工智能的未来发展"}'

# 查询状态
curl http://localhost:8765/v1/podcast/status/{task_id}

# 获取音频
curl http://localhost:8765/v1/podcast/audio/{task_id}
```

### TTS 音频合成

播客音频使用**火山引擎原生TTS**（豆包同款音色）：

| 项目 | 详情 |
|------|------|
| 协议 | 火山引擎 V1 WebSocket 二进制协议 |
| 端点 | `wss://openspeech.bytedance.com/api/v1/tts/ws_binary` |
| 认证 | 通过豆包 `/alice/user/launch` API 获取 appid + token |
| 音色 | `zh_female_wenroutaozi_uranus_bigtts`（温柔桃子女声） |
| 格式 | MP3, 24kHz 采样率 |

**工作流程**：
1. 调用豆包 FPA API 生成播客脚本（双人对话格式）
2. 解析脚本，按主播分段
3. 通过火山引擎 WebSocket TTS 合成每段音频
4. 合并所有音频段，返回完整播客

**自动重试机制**：
- Cookie 过期时自动切换账号池中的其他账号
- 火山引擎 TTS 失败时自动回退到 edge-tts

**前置/后置音乐**：
- 播客音频自动添加前置音乐（intro_jingle.mp3）和后置音乐（outro_jingle.mp3）
- 使用 ffmpeg 合并音频，带渐入渐出效果
- 可通过前端复选框或 API 参数控制开关：
  - 生成时传参：`{"topic": "...", "intro_jingle": true, "outro_jingle": true}`
  - 配置端点：`GET/POST /v1/podcast/config`

### API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/podcast/generate` | POST | 生成播客（自动账号重试） |
| `/v1/podcast/status/{task_id}` | GET | 播客生成状态查询 |
| `/v1/podcast/audio/{task_id}` | GET | 获取播客音频URL |
| `/v1/podcast/script/{task_id}` | GET | 获取播客脚本 |
| `/v1/podcast/list` | GET | 播客任务列表 |
| `/v1/podcast/file/{filename}` | GET | 音频文件下载 |
| `/v1/podcast/config` | GET/POST | 播客配置（前置/后置音乐开关） |

通过 `doubao-music` 模型生成 AI 音乐，支持歌词+音频自动生成：

```bash
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao-music",
    "messages": [{"role": "user", "content": "创作一首关于春天的歌曲"}],
    "stream": true
  }'
```

音乐生成流程：
1. 豆包先通过 SSE 生成歌词（content_type=2005）
2. 后台异步合成音频（约30-120秒）
3. 通过 WebSocket 推送音乐卡片（content_type=2006）
4. Web 界面自动显示音频播放器（封面+标题+时长+播放控件）

也可直接调用音乐 API：

```bash
# 生成音乐
curl -X POST http://localhost:8765/v1/music/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一首快乐的流行歌曲","style":"流行","mood":"快乐","voice":"女声"}'

# 查询状态
curl http://localhost:8765/v1/music/status/{task_id}

# 获取音频URL
curl http://localhost:8765/v1/music/audio/{task_id}
```

## 对话导出/导入

### 从豆包网站抓取对话

系统通过 CloakBrowser（隐蔽浏览器）自动访问豆包网站，拦截 API 响应获取对话数据：

1. **抓取对话列表** — 访问豆包网站，拦截 `/samantha/chat/conversation/list` API 响应
2. **导出单个对话** — 访问指定对话页面，拦截 `/samantha/chat/conversation/message/list` API 响应
3. **下载媒体文件** — 自动下载对话中的图片、音频、视频到本地 `exports/media/` 目录
4. **保存为 JSON** — 导出结果保存到 `exports/{conversation_id}.json`

```bash
# 获取对话列表
curl http://localhost:8765/v1/doubao/conversations

# 导出指定对话（含媒体下载）
curl http://localhost:8765/v1/doubao/conversations/{conversation_id}/export
```

### 技术原理

豆包网站的 API 端点（如 `/samantha/chat/conversation/list`、`/alice/profile/self`）需要 `a_bogus` 签名参数才能直接调用。由于 B3 方案（x-flow-trace 绕过）仅适用于 `/samantha/chat/completion` 端点，其他端点必须通过浏览器自动化访问。

系统采用 **CloakBrowser 优先 + Playwright 降级** 策略：

- **CloakBrowser**：基于 Chromium 的隐蔽浏览器，33个C++源码级补丁，reCAPTCHA v3 得分 0.9，通过 Cloudflare Turnstile 检测
- **Playwright 降级**：CloakBrowser 不可用时自动回退到 Playwright（添加 `--disable-blink-features=AutomationControlled` 参数）

工作流程：
```
1. 从 Cookie 池中选择有效账号（检查 sid_guard 过期时间）
2. 启动 CloakBrowser/Playwright，注入 Cookie
3. 设置路由拦截器（page.route），捕获目标 API 响应
4. 访问豆包网站，等待 API 响应被拦截
5. 解析响应数据，提取对话/消息/用户信息
6. 关闭浏览器，返回结果
```

### 导出数据格式

```json
{
  "conversation_id": "7xxxxxxxxxx",
  "messages": [
    {
      "message_id": "msg_xxx",
      "role": "user",
      "content_type": 10000,
      "created_at": 1715731200,
      "text": "你好",
      "images": [],
      "audio_url": null,
      "video_url": null
    },
    {
      "message_id": "msg_yyy",
      "role": "assistant",
      "content_type": 2006,
      "text": "春暖人间烂漫\n\n(歌词...)",
      "images": ["exports/media/conv_id/msg_yyy_img0.jpg"],
      "audio_url": "exports/media/conv_id/msg_yyy_audio.mp3",
      "video_url": null
    }
  ],
  "message_count": 2,
  "exported_at": 1715731200.0
}
```

### 本地对话导出/导入

Web 管理面板支持：
- **导出本地对话**：将系统中的对话导出为 JSON 或 JSONL 格式
- **导入对话文件**：导入 JSON/JSONL 格式的对话文件，自动去重

## 项目结构

```
webchat-api/
├── main.py                      # FastAPI 入口 + 路由
├── config.py                    # 配置加载 + Cookie池 + 日志
├── models.py                    # 数据模型 + 模型配置
├── sse.py                       # SSE 解析 + 格式化工具
├── openai_api.py                # OpenAI 兼容 API 逻辑
├── anthropic_api.py             # Anthropic 兼容 API 逻辑
├── music.py                     # 音乐生成模块（歌词+音频+WebSocket）
├── podcast.py                   # 播客生成模块（脚本+音频+自动重试+前置/后置音乐）
├── volcengine_tts.py            # 火山引擎 TTS 客户端（WebSocket V1 协议）
├── storage.py                   # 服务端持久存储（SQLite）
├── exporter.py                  # 对话导出模块（CloakBrowser+媒体下载）
├── uploader.py                  # 图片上传模块 (ImageX 4步流程)
├── signer.py                    # Playwright 签名模块 (B2, 实验性)
├── generate_podcast_jingle.py   # 播客前置/后置音乐生成脚本
├── config.json                  # 会话配置 (自动生成)
├── accounts.json                # 多账号配置
├── config.example.json          # 配置示例
├── index.html                   # 管理面板（明暗主题+播客播放器+音乐播放器）
├── run-server.bat               # Windows 启动脚本
├── .gitignore                   # Git 忽略规则
├── README.md                    # 项目文档
├── docs/
│   └── reverse-engineering-report.md  # 逆向分析报告
├── data/                        # 数据目录 (自动生成)
│   ├── conversations.db         # SQLite 对话数据库
│   └── podcast_audio/           # 播客音频文件
│       ├── intro_jingle.mp3     # 前置音乐
│       └── outro_jingle.mp3     # 后置音乐
├── exports/                     # 对话导出 (自动生成)
│   └── media/                   # 下载的媒体文件
├── logs/                        # 对话日志 (自动生成)
├── media/                       # 音乐/音频缓存 (自动生成)
│   └── audio/                   # 生成的音频文件
└── temp/                        # 临时文件 (自动生成)
```

## 技术原理

使用 **x-flow-trace 绕过方案（B3）**，无需伪造签名：

1. 从豆包客户端提取 Cookie 和设备参数
2. 构造包含 `x-flow-trace`、`agw-js-conv: str` 等请求头的请求
3. 直接调用豆包 `/samantha/chat/completion` API
4. 将三层嵌套 SSE 响应转换为 OpenAI 标准格式

### 签名方案对比

| 方案 | 方法 | 结果 | 原因 |
|---|---|---|---|
| B3 | x-flow-trace 绕过 | ✅ 成功 | 内部调试头绕过签名验证 |
| B2 | Playwright 生成 X-Bogus | ❌ 失败 | 触发服务端浏览器指纹验证 |
| B1 | 纯 Python a_bogus | ❌ 不可行 | 缺乏浏览器环境 |

### 图片上传流程

```
1. prepare_upload → 获取 AWS 临时凭证
2. ApplyImageUpload → 获取 TOS 存储地址
3. Upload Binary → 上传图片二进制 (CRC32 校验)
4. CommitImageUpload → 确认上传，获取 ImageUri
```

## Cookie 过期处理

Cookie 有效期约 30 天。过期后重新提取即可：

```bash
python temp/extract_session.py
```

脚本会自动更新 `config.json`，无需手动编辑。

## 注意事项

- 仅供学习研究，请勿用于商业用途
- Cookie 属于敏感信息，请勿泄露（`config.json` 已加入 .gitignore）
- 豆包 API 可能随时变更，导致服务失效
- 建议不要高频调用，以免触发风控

## License

MIT
