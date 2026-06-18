# 豆包桌面客户端逆向 API 分析报告

> 分析日期：2026-05-13
> 目标程序：豆包桌面客户端 v2.9.7 (Chromium 135.0.7049.72)
> 分析目的：评估将豆包桌面客户端包装为标准 API（供 Claude Code、OpenCode 等 AI Agent 调用）的可行性

---

## 项目时间轴

| 日期 | 事件 | 备注 |
|---|---|---|
| 05-13 21:00 | 项目启动，探索豆包桌面客户端目录结构 | 发现基于 Chromium 135 定制内核 |
| 05-13 21:10 | 分析 manifest.json、env.json、route.json 等配置文件 | 确认主域名 doubao.com，备用域名 cici.com/ciciai.com/dola.com |
| 05-13 21:20 | 分析 debug.log，确认 Chromium 多进程模型 | Browser/GPU/Renderer/Utility 四类进程 |
| 05-13 21:30 | 搜索 JS 文件中的 API 通信模式 | JS 已混淆，未找到明文 API 端点 |
| 05-13 21:40 | 发现 AppData\Local\Doubao 用户数据目录 | Cookie 存储在 SQLite 数据库中 |
| 05-13 21:50 | 调研 GitHub 上的豆包逆向开源项目 | 发现 doubao-free-api、doubao-2api、DoubaoFreeApi 三个关键项目 |
| 05-13 22:00 | 完成 SSE 流式响应机制分析 | 豆包使用 7 种 SSE 事件类型，远比 OpenAI 复杂 |
| 05-13 22:10 | 完成第一版分析报告 | 初步结论：Playwright 方案最稳定 |
| 05-13 22:20 | 深入调研方案B（直接API+伪造签名） | 关键发现：DoubaoFreeApi 使用 x-flow-trace 绕过 a_bogus |
| 05-13 22:30 | 分析 DoubaoFreeApi 源码 | 发现第三种签名绕过路径：URL参数+请求头组合 |
| 05-13 23:00 | 会话上下文恢复，验证报告完整性 | 确认方案B评估、时间轴、无ARK API引用均已完成 |
| 05-13 23:05 | 创建项目目录 webchat-api/ 和 temp/ | 临时文件独立存放 |
| 05-13 23:10 | 安装依赖：pycryptodome, aiohttp, fastapi, uvicorn, httpx | Python 3.13 环境 |
| 05-13 23:15 | 从 SQLite 提取豆包 Cookie | DPAPI 解密密钥正确，v10 AES-GCM 解密部分乱码，关键 Cookie 可提取 |
| 05-13 23:20 | 提取 sessionid、uid_tt、device_id 等关键参数 | sessionid=<已脱敏> |
| 05-13 23:25 | 实现 B3 方案 FastAPI 服务（main.py） | OpenAI 兼容 /v1/chat/completions + /v1/models |
| 05-13 23:30 | 首次测试 B3 方案 — SSE 解析失败 | 原因：event_data 是 JSON 字符串需二次解析 |
| 05-13 23:35 | 修正 SSE 解析器，二次/三次 JSON 解析 | 外层 event_type + 内层 message.content 三层嵌套 |
| 05-13 23:38 | **B3 方案验证成功！** 非流式+流式均正常 | 豆包回复："我是字节跳动自研的智能助手" |
| 05-13 23:50 | 增加对话日志记录功能 | logs/chat_YYYY-MM-DD.jsonl，记录用户输入+AI输出 |
| 05-13 23:51 | 修正SSE缓冲区解析，修复content_type=2008丢失问题 | buffer机制替代逐chunk分割 |
| 05-13 23:52 | 日志功能验证通过 | 1+1=2 正确记录 |
| 05-14 00:00 | 发现content_type=2071问题 | 通用对话返回2071而非2001/2008 |
| 05-14 00:01 | 修复SSE解析器，增加2071支持 | 非流式回复正常返回完整文本 |
| 05-14 00:02 | Phase2功能测试通过 | 多账号/模型映射/会话管理/错误恢复均正常 |
| 05-14 00:03 | 开始B2方案（Playwright签名） | 基于lza6/doubao-2api项目实现 |
| 05-14 00:05 | 安装Playwright依赖 | playwright + playwright-stealth + Chromium |
| 05-14 00:08 | 创建signer.py | 实现PlaywrightSigner类，封装签名逻辑 |
| 05-14 00:10 | 集成B2到main.py | 添加sign_method配置，支持B2/B3切换 |
| 05-14 00:13 | B2首次测试失败：frontierSign不存在 | window.byted_acrawler已废弃 |
| 05-14 00:14 | **关键发现：签名函数迁移到window.bdms** | bdms.frontierSign替代byted_acrawler.frontierSign |
| 05-14 00:15 | 修复signer.py使用bdms.frontierSign | 返回X-Bogus而非a_bogus |
| 05-14 00:22 | B2签名初始化成功 | bdms.frontierSign加载，msToken捕获 |
| 05-14 00:23 | **B2签名URL调用失败：触发验证机制** | 710022004 rate limited + verify类型 |
| 05-14 00:25 | 尝试B2签名+x-flow-trace组合 | 仍然触发验证 |
| 05-14 00:27 | 浏览器内fetch同样触发验证 | 确认是账号级风控，非请求方式问题 |
| 05-14 00:29 | **B3模式验证仍正常工作** | x-flow-trace绕过方案稳定可靠 |
| 05-14 00:30 | **B2方案结论：不可行** | X-Bogus签名触发服务端验证，B3是唯一可行路径 |
| 05-14 00:35 | extract_session.py 自动更新 config.json | 避免手动编辑配置文件 |
| 05-14 00:50 | 创建 index.html 管理面板 | 状态监控+聊天+日志+账号+配置 |
| 05-14 01:00 | 分析14种豆包特殊功能可行性 | 思考/编程/超能/写作/翻译/解题高可行性 |
| 05-14 01:05 | 创建 uploader.py 图片上传模块 | 4步上传流程：prepare→apply→upload→commit |
| 05-14 01:07 | **V3.0 发布：Vision + 特殊模式 + 图片上传** | 10个模型、图片识别、思考/编程/写作/翻译/解题模式 |
| 05-14 01:08 | **Vision 图片识别测试通过** | 百度Logo成功识别，图片上传→ImageX→附件发送全流程 |
| 05-14 01:09 | **思考模式测试通过** | doubao-thinking use_deep_think参数生效 |
| 05-14 12:00 | 代码模块化重构：main.py → config/models/sse/openai_api/anthropic_api | 提升可维护性 |
| 05-14 12:30 | 实现Anthropic Claude Code兼容API /v1/messages | 支持Claude Code原生对接 |
| 05-14 13:00 | 逆向分析报告脱敏处理 | sessionid/uid_tt/device_id等替换为占位符 |
| 05-14 13:30 | GitHub仓库创建并推送代码 | ZedeX/doubao-free-api，代理localhost:1082 |
| 05-14 14:00 | V3.2：新增数据分析师模型、图片生成API /v1/images/generations | OpenAI Image API兼容 |
| 05-14 14:30 | 逆向JS代码发现播客API架构 | /chat/podcast/chunk + FPA_Podcast + GetGenPodcastDetail |
| 05-14 15:00 | V3.3：实现AI播客生成（脚本+PDF上传） | /v1/podcast/generate，script_ready状态 |
| 05-14 15:15 | 修复图片生成双会话问题，retry复用conversation_id | 同一会话内完成 |
| 05-14 15:20 | 实现会话保持功能 | conversation_id传递+前端保持会话复选框 |
| 05-14 15:25 | 图片生成集成到模型下拉选择（doubao-image），移除单独按钮 | 统一交互方式 |
| 05-14 15:30 | 引入marked.js+highlight.js，完整Markdown渲染 | 标题/列表/代码块/表格/引用/链接/图片 |
| 05-14 15:45 | 播客脚本生成测试通过 | 1900字完整播客脚本，含时间标记和分段 |
| 05-16 02:00 | 开始逆向豆包TTS协议 | 分析SAMI SDK、WebSocket通信、Protobuf编码 |
| 05-16 08:00 | 发现火山引擎V3 WebSocket TTS协议文档 | wss://openspeech.bytedance.com/api/v3/tts/bidirection |
| 05-16 09:00 | 发现豆包TTS凭据来源 | /alice/user/launch 返回 audio_app_key + audio_token |
| 05-16 10:00 | V3协议认证失败，转向V1协议 | V3的X-Api-App-Key头与豆包凭据不兼容 |
| 05-16 10:30 | **V1协议TTS验证成功！** | 首次成功生成5.5秒音频，109KB MP3 |
| 05-16 11:00 | 修复V1协议Audio帧解析 | sequence number字段导致payload偏移错误 |
| 05-16 11:30 | 端到端播客+TTS验证通过 | 848KB音频，42.4秒，8/8段成功 |
| 05-16 12:00 | 发现豆包appid仅支持1个音色 | zh_female_wenroutaozi_uranus_bigtts，男声不可用 |
| 05-16 12:30 | 播客API完整验证通过 | 4.1MB音频，206秒，火山引擎原生TTS |
| 05-16 13:00 | 添加播客前置/后置音乐 | ffmpeg合并intro+content+outro，渐入渐出效果 |
| 05-16 13:30 | 修复前端播放器+下载按钮 | 直接播放本地音频，添加下载按钮 |
| 05-16 14:00 | 去除播客提示词音频 | _clean_podcast_script过滤AI回复前缀 |

---

## 1. 程序架构分析

### 1.1 整体架构

豆包桌面客户端是一个基于 **Chromium 定制内核** 的桌面应用（非 Electron），核心架构如下：

```
Doubao.exe (启动器)
  └── app/
      ├── Doubao.exe              # 主进程 (Chromium Browser Process)
      ├── Doubao.dll              # 核心业务逻辑 DLL
      ├── Doubao_elf.dll          # ELF 加载器
      ├── Doubao_proxy.exe        # 代理进程
      ├── Doubao_browser_proxy.exe # 浏览器代理
      ├── Doubao_wer.dll          # Windows 错误报告
      ├── aha_net.dll             # 网络加速模块
      ├── VolcEngineRTCAudio.dll  # 火山引擎实时音频
      ├── ghelper.exe             # GPU 辅助进程
      ├── local_webcontents/      # 本地 Web 扩展
      │   ├── biz/                # 业务配置
      │   └── extensions/
      │       └── ai-views/       # AI 视图扩展 (Chrome Extension v3)
      └── aha_doctor/             # 诊断修复工具
```

### 1.2 核心发现

| 属性 | 值 |
|---|---|
| 内核版本 | Chromium 135.0.7049.72 |
| 扩展协议 | Manifest V3 |
| 主域名 | `www.doubao.com` |
| 备用域名 | `cici.com`, `ciciai.com`, `dola.com` |
| 用户数据目录 | `C:\Users\{user}\AppData\Local\Doubao\User Data\` |
| Cookie 存储 | SQLite (`User Data\Default\Network\Cookies`) |
| 扩展版本 | ai-views v1.0.0.4414 |

### 1.3 进程模型

从 `debug.log` 可以看到，豆包客户端遵循标准 Chromium 多进程模型：

- **Browser Process** — 主进程，管理窗口和扩展
- **GPU Process** — GPU 渲染进程
- **Renderer Process** — 渲染进程（加载 `www.doubao.com/chat/`）
- **Utility Process** — 工具进程（Storage、Icon Reader 等）

---

## 2. 网络通信分析

### 2.1 核心 API 端点

豆包网页版的聊天 API 端点为：

```
POST https://www.doubao.com/samantha/chat/completion
```

这是所有对话请求的核心入口，支持流式和非流式响应。

### 2.2 认证机制

豆包使用 **Cookie-based 认证**，关键字段为 `sessionid`：

```
Cookie: sessionid=<已脱敏>; ...
```

在桌面客户端中，该 Cookie 存储在 Chromium 的 Cookie 数据库中：
- 路径：`C:\Users\{user}\AppData\Local\Doubao\User Data\Default\Network\Cookies`
- 格式：SQLite3 数据库（加密）

### 2.3 反爬签名机制

豆包 API 请求涉及多个安全参数，存在 **三条绕过路径**：

| 参数 | 说明 | 生成方式 |
|---|---|---|
| `a_bogus` | 请求签名，基于 URL 参数 + 时间戳 + 浏览器指纹生成 | `window.byted_acrawler.frontierSign(params)` |
| `msToken` | 会话令牌 | 可伪造（格式为 Base64 字符串） |
| `x-flow-trace` | 流量追踪头 | 格式固定的 JSON 字符串 |

**三条签名绕过路径**：

| 路径 | 方案 | 原理 | 代表项目 |
|---|---|---|---|
| 路径1 | 伪造 a_bogus + msToken | 纯 JS/Python 实现签名算法 | doubao-free-api (Node.js) |
| 路径2 | Playwright 调用 frontierSign | 在浏览器中执行原始签名函数 | doubao-2api (Python) |
| 路径3 | 使用 x-flow-trace + URL 参数绕过 | 不需要 a_bogus，用其他参数组合替代 | DoubaoFreeApi (Python) |

**关键洞察**：豆包前端 JS 已经劫持了 `window.fetch`，在浏览器内调用 `fetch()` 时，`a_bogus` 和 `msToken` 会被自动注入到 URL query 参数中。这意味着在浏览器环境中执行请求可以自动获得有效签名。

### 2.4 请求体结构

豆包的聊天请求体采用独特的嵌套结构（来自 DoubaoFreeApi 源码验证）：

```json
{
  "completion_option": {
    "is_regen": false,
    "with_suggest": false,
    "need_create_conversation": true,
    "launch_stage": 1,
    "use_auto_cot": false,
    "use_deep_think": false
  },
  "conversation_id": "0",
  "local_conversation_id": "local_12345678901234",
  "local_message_id": "uuid-xxx",
  "messages": [
    {
      "content": "{\"text\":\"你好\"}",
      "content_type": 2001,
      "attachments": [],
      "references": []
    }
  ]
}
```

**关键差异**：
- 使用 `content_type: 2001` 标识文本消息（而非 OpenAI 的 role 字段）
- `content` 是 JSON 字符串 `{"text": "..."}` 而非纯文本
- `local_conversation_id` 是必要字段，缺失会导致会话创建失败
- `completion_option` 控制对话行为（深度思考、自动 CoT 等）

### 2.5 URL 查询参数

来自 DoubaoFreeApi 源码的完整 URL 参数列表：

```
https://www.doubao.com/samantha/chat/completion?
  aid=497858
  &device_id={device_id}
  &device_platform=web
  &language=zh
  &pc_version=2.23.2
  &pkg_type=release_version
  &real_aid=497858
  &region=CN
  &samantha_web=1
  &sys_region=CN
  &tea_uuid={tea_uuid}
  &use-olympus-account=1
  &version_code=20800
  &web_id={web_id}
```

**关键参数说明**：

| 参数 | 说明 | 获取方式 |
|---|---|---|
| `aid` | 应用 ID，固定值 `497858` | 硬编码 |
| `device_id` | 设备标识 | 从浏览器请求中抓取 |
| `tea_uuid` | 追踪 UUID | 从浏览器请求中抓取 |
| `web_id` | Web 标识 | 从浏览器请求中抓取 |
| `version_code` | 版本号 | 固定值 `20800` |

### 2.6 请求头伪装

逆向项目显示需要模拟浏览器请求头：

```http
Content-Type: application/json
Accept: text/event-stream
Agw-Js-Conv: str
Cookie: sessionid=xxx; ...
Origin: https://www.doubao.com
Referer: https://www.doubao.com/chat/{room_id}
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...
X-Flow-Trace: {json_trace}
```

**关键请求头**：

| 头部 | 说明 | 必要性 |
|---|---|---|
| `Agw-Js-Conv: str` | API 网关 JS 转换标识 | 必需 |
| `X-Flow-Trace` | 流量追踪 JSON | 路径3必需 |
| `Referer` | 必须包含 room_id | 必需 |

---

## 3. 流式响应分析

### 3.1 SSE 事件类型

豆包的 SSE 协议比 OpenAI 复杂得多，包含 **7 种事件类型**：

| 事件类型 | 说明 | 数据结构 |
|---|---|---|
| `SSE_HEARTBEAT` | 心跳保活 | 空数据 |
| `SSE_ACK` | 确认收到 | 序列号 |
| `FULL_MSG_NOTIFY` | 完整消息通知 | 完整消息对象 |
| `STREAM_MSG_NOTIFY` | 流式消息通知 | 消息元数据 |
| `CHUNK_DELTA` | 增量文本块 | `patch_value` 中的文本片段 |
| `STREAM_CHUNK` | 流式数据块 | `content_block` 嵌套结构 |
| `SSE_REPLY_END` | 回复结束 | 结束标记 |

### 3.2 实际 SSE 事件类型（来自 DoubaoFreeApi 源码验证）

DoubaoFreeApi 的 `handle_sse` 函数揭示了实际的 SSE 事件编号体系：

| event_type | 说明 | 处理逻辑 |
|---|---|---|
| `2001` | 流消息 | 提取 content_type=10000 的文本，content_type=2074 的图片 |
| `2002` | 流开始 | 提取 conversation_id、message_id、section_id |
| `2003` | 流结束 | 拼接所有文本，返回完整结果 |

**content_type 分类**：

| content_type | 内容类型 | 说明 |
|---|---|---|
| `10000` | 文本 | 纯文本内容 |
| `2001` | 文本（备选） | 另一种文本格式 |
| `2008` | 文本（备选） | 另一种文本格式 |
| `2074` | 图片 | 图片 URL（含 creations.image 结构） |

### 3.3 流式数据提取流程

```
SSE 事件流
  ├── event_type=2002 (流开始)
  │   └── 提取 conversation_id, message_id, section_id
  ├── event_type=2001 (流消息)
  │   └── content_type=10000/2001/2008 → 提取 JSON text
  │   └── content_type=2074 → 提取图片 URL (image_raw/image_thumb/image_ori)
  └── event_type=2003 (流结束)
      └── 拼接所有文本，去除首尾换行
```

### 3.4 与 OpenAI SSE 格式对比

| 维度 | 豆包 SSE | OpenAI SSE |
|---|---|---|
| 事件类型 | 7 种（编号 2001-2003 + 其他） | 1 种（`chat.completion.chunk`） |
| 数据格式 | `content_type` + JSON content | `choices[0].delta.content` |
| 结束标记 | `event_type=2003` | `data: [DONE]` |
| 心跳机制 | `SSE_HEARTBEAT` | 无 |
| 确认机制 | `SSE_ACK` | 无 |
| 图片处理 | `content_type=2074`，含多尺寸 URL | 无原生支持 |

---

## 4. 本地数据与文件操作

### 4.1 Cookie 提取

豆包桌面客户端的 Cookie 存储在 Chromium 标准 SQLite 数据库中：

```
C:\Users\{user}\AppData\Local\Doubao\User Data\Default\Network\Cookies
```

**提取方式**：
1. 直接读取 SQLite 数据库（需处理 Chromium 加密）
2. 通过 CDP (Chrome DevTools Protocol) 远程调试获取
3. 使用 Playwright/Puppeteer 启动时注入
4. 手动从浏览器 F12 → Application → Cookies 复制

### 4.2 本地存储

| 存储类型 | 路径 | 内容 |
|---|---|---|
| Local Storage | `Default\Local Storage\leveldb\` | 会话状态、用户偏好 |
| Session Storage | `Default\Session Storage\` | 临时会话数据 |
| IndexedDB | `Default\IndexedDB\` | 结构化数据 |
| Extension State | `Default\Extension State\` | 扩展数据 |
| Cache | `Default\Cache\` | HTTP 缓存 |

### 4.3 文件上传能力

来自 DoubaoFreeApi 源码的完整文件上传流程：

```
1. POST /alice/resource/prepare_upload → 获取 AWS 凭证 + service_id
2. GET  imagex.bytedanceapi.com/ApplyImageUpload → 获取 StoreUri + SessionKey
3. POST tos-d-x-hl.snssdk.com/upload/v1/{StoreUri} → 上传文件二进制数据
4. POST imagex.bytedanceapi.com/CommitImageUpload → 确认上传，获取 ImageUri
```

**上传参数**：

| 参数 | 说明 |
|---|---|
| `resource_type` | 1=文档，2=图片 |
| `scene_id` | 固定值 `5` |
| `tenant_id` | 固定值 `5` |
| 认证方式 | AWS4Auth（access_key + secret_key + session_token） |

---

## 5. 可行性评估与方案设计

### 5.1 方案对比

| 方案 | 原理 | 优势 | 劣势 | 稳定性 |
|---|---|---|---|---|
| **A. Playwright 浏览器自动化** | 在无头浏览器中执行 fetch，利用字节 JS 自动注入签名 | 无需逆向签名算法，最稳定 | 资源消耗大（需运行 Chromium） | ⭐⭐⭐⭐⭐ |
| **B. 直接 API 调用 + 伪造签名** | 提取 sessionid，伪造 a_bogus/msToken 直接调用 API | 资源消耗小，响应快 | 签名算法可能更新，维护成本高 | ⭐⭐⭐ |
| **C. CDP 远程调试** | 启动豆包客户端时开启远程调试端口，通过 CDP 拦截/转发请求 | 利用已登录的客户端，无需额外认证 | 依赖客户端运行，耦合度高 | ⭐⭐⭐⭐ |
| **B'. x-flow-trace 绕过签名** | 使用 x-flow-trace + URL 参数组合，完全绕过 a_bogus | 无需浏览器，无需逆向签名 | 参数需定期抓取更新 | ⭐⭐⭐⭐ |

### 5.2 推荐方案：B' + A 分层架构

**核心发现**：DoubaoFreeApi 项目验证了 **不需要 a_bogus 也能成功调用豆包 API**。通过提供正确的 URL 查询参数（`device_id`、`web_id`、`tea_uuid`）+ 请求头（`x-flow-trace`、`agw-js-conv`）+ Cookie，可以直接绕过 a_bogus 签名验证。

推荐采用 **B'（x-flow-trace 绕过）为主 + A（Playwright 降级）为辅** 的分层架构：

```
┌─────────────────────────────────────────────────────────────┐
│                    AI Agent (Claude Code / OpenCode)         │
│                         ↓ HTTP Request                       │
│                  OpenAI-compatible API                       │
│                    /v1/chat/completions                      │
│                    /v1/models                                │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                 Doubao API Gateway                           │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ OpenAI 兼容层 │  │  SSE 转换器   │  │  会话管理器      │   │
│  │ (请求/响应)   │  │ (豆包→OpenAI) │  │ (conversation)  │   │
│  └─────────────┘  └──────────────┘  └──────────────────┘   │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ 认证管理器   │  │  参数服务     │  │  文件上传服务    │   │
│  │ (Cookie池)  │  │ (device_id等)│  │ (图片/PDF)      │   │
│  └─────────────┘  └──────────────┘  └──────────────────┘   │
└──────────────────────────┬──────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
    │ 直接 HTTP  │   │ 直接 HTTP  │   │ Playwright │
    │ + x-flow-  │   │ + 伪造     │   │ 浏览器实例  │
    │ trace 绕过 │   │ a_bogus    │   │ (降级方案) │
    │ (主要) ⭐  │   │ (备选)     │   │ (兜底)     │
    └───────────┘   └───────────┘   └───────────┘
```

### 5.3 核心模块设计

#### 5.3.1 OpenAI 兼容层

提供标准 OpenAI API 接口：

```python
POST /v1/chat/completions    # 对话补全（支持流式/非流式）
GET  /v1/models              # 模型列表
POST /v1/images/generations  # 图片生成（文生图）
```

#### 5.3.2 模型映射

| OpenAI model 参数 | 豆包 bot_id | 说明 |
|---|---|---|
| `doubao-pro-chat` | 默认 bot_id | 豆包 Pro 对话 |
| `doubao-lite-chat` | 对应 bot_id | 豆包 Lite 对话 |
| `doubao-thinking` | 对应 bot_id | 深度思考模型 |

#### 5.3.3 SSE 流式转换

将豆包的 SSE 事件转换为 OpenAI 标准格式：

```
豆包 event_type=2001 + content_type=10000
  → 提取 JSON content 中的 text 字段
  → 封装为 OpenAI chat.completion.chunk 格式
  → data: {"choices":[{"delta":{"content":"文本片段"}}]}

豆包 event_type=2003
  → data: [DONE]
```

#### 5.3.4 Cookie 管理

从豆包桌面客户端提取 sessionid 的方案：

```python
# 方案1: 从 SQLite 数据库提取（需解密 Chromium Cookie）
# 方案2: 通过 CDP 远程调试获取
# 方案3: 手动从浏览器 F12 复制
# 方案4: Playwright 自动化抓取（DoubaoFreeApi 的 fetcher.py 方案）
```

### 5.4 对接 AI Agent 的关键要求

| 要求 | 实现方式 | 优先级 |
|---|---|---|
| **OpenAI 兼容 API** | 实现 `/v1/chat/completions` 端点 | P0 |
| **SSE 流式响应** | 转换豆包 SSE 为 OpenAI SSE 格式 | P0 |
| **Bearer Token 认证** | 使用 sessionid 作为 API Key | P0 |
| **多轮对话** | 通过 conversation_id 维护上下文 | P1 |
| **文件上传** | 支持图片/PDF 等附件 | P1 |
| **模型选择** | 映射 bot_id 到 model 名称 | P1 |
| **错误处理** | 超时重试、Cookie 失效检测 | P2 |
| **并发控制** | 请求队列、限流 | P2 |

---

## 6. 已有开源项目参考

### 6.1 doubao-free-api (Node.js/TypeScript)

- **仓库**：https://github.com/LLM-Red-Team/doubao-free-api
- **技术栈**：Koa + TypeScript
- **签名方式**：伪造 `a_bogus` 和 `msToken`（纯 JS 实现）
- **认证**：直接使用 `sessionid`
- **特点**：轻量级，不依赖浏览器；但签名算法可能随豆包更新失效

**核心 API 调用流程**：
```
1. tokenSplit(authorization) → 提取 sessionid
2. extractRefFileUrls(messages) → 提取文件引用
3. messagesPrepare(messages, refs) → 构造豆包请求体
4. generateFakeMsToken() + generateFakeABogus() → 生成签名
5. POST /samantha/chat/completion → 调用豆包 API
6. createTransStream() → 转换 SSE 格式
7. removeConversation(convId) → 清理会话痕迹
```

### 6.2 doubao-2api (Python/FastAPI)

- **仓库**：https://github.com/lzA6/doubao-2api
- **技术栈**：FastAPI + Playwright + httpx
- **签名方式**：利用 Playwright 浏览器中的字节 JS 拦截器自动注入签名
- **认证**：Cookie 注入到浏览器上下文
- **特点**：最稳定（不依赖签名逆向），但资源消耗大

**核心洞察**：
> 不逆向签名，逆向浏览器环境。豆包前端的 `window.fetch` 已被字节 JS 劫持，在浏览器内调用 `fetch()` 时，`a_bogus` 和 `msToken` 会被自动注入。

### 6.3 DoubaoFreeApi (Python/FastAPI) ⭐ 新发现

- **仓库**：https://github.com/XilyFeAAAA/DoubaoFreeApi
- **技术栈**：FastAPI + aiohttp + httpx + Playwright（仅用于初始化抓取）
- **签名方式**：**完全不需要 a_bogus**，使用 `x-flow-trace` + URL 参数组合绕过
- **认证**：完整 Cookie 字符串（sessionid + sid_guard + uid_tt 等）
- **特点**：最轻量，无需浏览器常驻，支持游客模式

**关键发现**：DoubaoFreeApi 的源码证明，豆包 API 的 a_bogus 签名**不是强制必需的**。只要提供正确的 URL 查询参数和请求头组合，API 就会正常响应。

**核心配置参数**（需从浏览器抓取一次）：

```json
{
  "cookie": "sessionid=xxx; sid_guard=xxx; uid_tt=xxx; ...",
  "device_id": "7xxxxxxxxxxxxxx",
  "tea_uuid": "7xxxxxxxxxxxxxx",
  "web_id": "7xxxxxxxxxxxxxx",
  "room_id": "7xxxxxxxxxxxxxx",
  "x_flow_trace": "{\"trace_id\":\"xxx\",\"span_id\":\"xxx\"}"
}
```

**Playwright 仅用于初始化**：`fetcher.py` 使用 Playwright 打开豆包网页、发送一条消息、捕获请求中的参数，之后不再需要浏览器。

---

## 7. 方案B 详细可行性评估：直接 API 调用 + 伪造签名

### 7.1 方案B 的三个子方案

方案B"直接 API 调用 + 伪造签名"实际上可以细分为三个子方案，难度和稳定性递增：

| 子方案 | 签名策略 | 浏览器依赖 | 维护成本 | 稳定性 |
|---|---|---|---|---|
| **B1. 伪造 a_bogus** | 纯代码实现 `frontierSign` 算法 | 无 | 极高 | ⭐⭐ |
| **B2. 伪造 a_bogus（execjs）** | 提取 JS 代码，通过 Node.js/PyExecJS 执行 | Node.js 运行时 | 高 | ⭐⭐⭐ |
| **B3. x-flow-trace 绕过** | 不使用 a_bogus，用其他参数组合替代 | 无（初始化时需一次） | 低 | ⭐⭐⭐⭐ |

### 7.2 子方案 B1：纯代码伪造 a_bogus

#### 7.2.1 a_bogus 算法分析

`a_bogus` 是字节跳动系产品（抖音、豆包等）通用的反爬签名参数，其生成算法具有以下特征：

**算法复杂度**：
- 核心逻辑在 `webmssdk.js` 中，代码量约 **2万+ 行**
- 使用 **自定义虚拟机（VM）** 执行字节码，非明文 JS
- 包含 **178 个自定义操作码**（PUSH/POP/JMP/JZ/NEW/GETPROP 等）
- 采用 **双层加密**：传输层 Base64 + 存储层 AES-256-CBC + Leb128 压缩
- **20+ 种混淆技术**：变量名加密、控制流扁平化、函数指针混淆

**签名输入**：
```
a_bogus = frontierSign(url_query_params)
```
输入为 URL 的查询参数字符串，输出为固定格式的签名字符串。

**签名输出格式**：
```
a_bogus=DRE1xYK...
```
约 100-200 字符的 Base64 编码字符串。

#### 7.2.2 伪造难度评估

| 维度 | 评估 | 说明 |
|---|---|---|
| 算法还原 | ⚠️ 极难 | 需要逆向自定义 VM 字节码，映射 178 个操作码 |
| 环境模拟 | ⚠️ 极难 | 算法依赖浏览器指纹（UA、屏幕尺寸、Canvas 等） |
| 维护成本 | ⚠️ 极高 | 字节平均 2-4 周更新一次签名算法 |
| 现有实现 | ⚠️ 不可靠 | 开源的纯算版（如 uesrsxwj/dy）收费且时效性差 |

#### 7.2.3 可行性结论

**❌ 不推荐**。纯代码伪造 a_bogus 的投入产出比极低：

1. **逆向成本高**：需要完整还原 webmssdk.js 中的 VM 字节码解释器
2. **时效性差**：字节跳动频繁更新算法，2025 年混淆密度增加了 42%
3. **环境依赖**：签名算法检测浏览器指纹，纯 Python/JS 环境难以完美模拟
4. **无可靠开源实现**：目前公开的"纯算版"要么收费，要么已失效

### 7.3 子方案 B2：execjs 执行原始 JS

#### 7.3.1 实现思路

不逆向算法本身，而是提取豆包网页中的 `webmssdk.js`，通过 JS 运行时执行原始签名函数：

```python
import execjs

# 加载从豆包网页提取的 webmssdk.js
with open('webmssdk.js', 'r') as f:
    js_code = f.read()

ctx = execjs.compile(js_code)
a_bogus = ctx.call('window.byted_acrawler.frontierSign', params)
```

#### 7.3.2 技术挑战

| 挑战 | 说明 | 解决方案 |
|---|---|---|
| JS 环境模拟 | `frontierSign` 依赖 `window`、`document`、`navigator` 等浏览器对象 | 使用 jsdom 或补环境脚本 |
| 指纹检测 | 算法会验证 Canvas、WebGL、AudioContext 等指纹 | 伪造指纹返回值 |
| JS 提取 | webmssdk.js 动态加载，URL 随版本变化 | 从浏览器 DevTools 抓取 |
| 运行时依赖 | 需要 Node.js 或 PyExecJS | Docker 中安装 Node.js |

#### 7.3.3 可行性结论

**⚠️ 可行但不推荐作为主方案**。execjs 方案比纯算版好，但仍有以下问题：

1. **环境补丁维护**：浏览器对象模拟需要持续更新
2. **JS 文件更新**：webmssdk.js 版本更新后需重新提取
3. **性能开销**：execjs 每次调用需要启动 JS 运行时，约 50-100ms
4. **稳定性中等**：比纯算版好，但不如 Playwright 方案

### 7.4 子方案 B3：x-flow-trace 绕过 ⭐ 推荐

#### 7.4.1 核心发现

**DoubaoFreeApi 项目验证了一个关键事实：豆包 API 的 a_bogus 签名不是强制校验的。**

当请求中包含以下参数组合时，即使没有 a_bogus，API 也会正常响应：

1. **URL 查询参数**：`aid`、`device_id`、`device_platform`、`web_id`、`tea_uuid`、`version_code` 等
2. **请求头**：`X-Flow-Trace`、`Agw-Js-Conv: str`、`Referer`（含 room_id）
3. **Cookie**：完整的浏览器 Cookie 字符串

#### 7.4.2 x-flow-trace 格式

`X-Flow-Trace` 是一个 JSON 格式的追踪头：

```json
{
  "trace_id": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "span_id": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

其中 `trace_id` 和 `span_id` 为 32 位十六进制字符串，可以随机生成。

#### 7.4.3 参数获取方式

| 参数 | 获取方式 | 有效期 |
|---|---|---|
| `device_id` | 从浏览器请求中抓取一次 | 长期有效 |
| `web_id` | 从浏览器请求中抓取一次 | 长期有效 |
| `tea_uuid` | 通常等于 `web_id` | 长期有效 |
| `room_id` | 从浏览器 URL 或 Referer 中获取 | 长期有效 |
| `x_flow_trace` | 从浏览器请求头中抓取，或随机生成 | 可随机生成 |
| `cookie` | 从浏览器 DevTools 中复制 | 约 30 天 |

#### 7.4.4 完整请求示例

```python
import aiohttp
import json
import uuid

async def call_doubao_api(prompt: str, session_config: dict):
    params = "&".join([
        "aid=497858",
        f"device_id={session_config['device_id']}",
        "device_platform=web",
        "language=zh",
        "pc_version=2.23.2",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={session_config['tea_uuid']}",
        "use-olympus-account=1",
        "version_code=20800",
        f"web_id={session_config['web_id']}"
    ])
    
    url = f"https://www.doubao.com/samantha/chat/completion?{params}"
    
    body = {
        "completion_option": {
            "is_regen": False,
            "with_suggest": False,
            "need_create_conversation": True,
            "launch_stage": 1,
            "use_auto_cot": False,
            "use_deep_think": False
        },
        "conversation_id": "0",
        "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
        "local_message_id": str(uuid.uuid4()),
        "messages": [{
            "content": json.dumps({"text": prompt}),
            "content_type": 2001,
            "attachments": [],
            "references": []
        }]
    }
    
    headers = {
        'content-type': 'application/json',
        'accept': 'text/event-stream',
        'agw-js-conv': 'str',
        'cookie': session_config['cookie'],
        'origin': 'https://www.doubao.com',
        'referer': f"https://www.doubao.com/chat/{session_config['room_id']}",
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...',
        'x-flow-trace': session_config['x_flow_trace']
    }
    
    async with aiohttp.ClientSession() as client:
        async with client.post(url=url, headers=headers, json=body) as resp:
            async for chunk in resp.content.iter_chunked(1024):
                yield chunk.decode('utf-8', errors='replace')
```

#### 7.4.5 方案 B3 优势分析

| 优势 | 说明 |
|---|---|
| **零浏览器依赖** | 运行时不需要 Playwright/Chromium，内存占用 < 50MB |
| **极低延迟** | 直接 HTTP 请求，无浏览器中间层，延迟 < 100ms |
| **简单部署** | 纯 Python，Docker 镜像 < 100MB（vs Playwright 方案 > 1GB） |
| **易于维护** | 不依赖签名算法，豆包更新 a_bogus 不影响此方案 |
| **已验证可行** | DoubaoFreeApi 项目已在生产环境验证 |

#### 7.4.6 方案 B3 风险分析

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| 豆包修复 x-flow-trace 绕过 | 中 | 高 — 方案完全失效 | 降级到 Playwright 方案（方案A） |
| Cookie 过期 | 高 | 低 — 重新抓取即可 | 自动检测 + 告警，30天有效期 |
| 参数格式变更 | 低 | 中 — 需要更新参数 | 监控 API 变化 |
| IP 频率限制 | 中 | 低 — 限流即可 | 多账号轮询，控制 QPS |
| device_id/web_id 失效 | 低 | 低 — 重新抓取 | 初始化时自动获取 |

#### 7.4.7 可行性结论

**✅ 强烈推荐**。方案 B3 是当前最优的轻量级方案：

1. **已验证**：DoubaoFreeApi 项目在生产环境验证了可行性
2. **极轻量**：无需浏览器，纯 HTTP 请求
3. **低维护**：不依赖签名算法，豆包更新 a_bogus 不影响
4. **可降级**：如果 x-flow-trace 绕过被修复，可降级到 Playwright 方案

### 7.5 方案B 综合评估矩阵

| 评估维度 | B1 纯算伪造 | B2 execjs | B3 x-flow-trace |
|---|---|---|---|
| 实现难度 | 🔴 极高 | 🟡 中等 | 🟢 低 |
| 运行时依赖 | 无 | Node.js | 无 |
| 内存占用 | < 50MB | < 100MB | < 50MB |
| 响应延迟 | < 50ms | 50-100ms | < 50ms |
| 维护成本 | 🔴 极高 | 🟡 中等 | 🟢 低 |
| 算法更新影响 | 🔴 每次失效 | 🟡 需更新JS | 🟢 不受影响 |
| 已验证项目 | 无可靠开源 | dengmin/a-bogus | DoubaoFreeApi |
| 综合推荐度 | ❌ | ⚠️ | ✅ |

---

## 8. 实施路线图

### Phase 1: MVP（最小可用产品）— 基于 B3 方案

1. **抓取参数**：从豆包桌面客户端/浏览器中抓取 device_id、web_id、cookie 等参数
2. **搭建 API 网关**：基于 FastAPI，实现 `/v1/chat/completions` 端点
3. **SSE 转换**：将豆包 SSE 转换为 OpenAI 格式
4. **基础测试**：使用 curl 验证流式/非流式对话
5. **Claude Code 对接**：配置 base_url 和 api_key，验证 Agent 调用

### Phase 2: 增强

6. **多账号支持**：Cookie 池 + 轮询
7. **文件上传**：支持图片/PDF 附件
8. **模型映射**：支持多个 bot_id 切换
9. **会话管理**：conversation_id 上下文维护
10. **错误恢复**：Cookie 失效检测、自动重试

### Phase 3: 生产化 + 降级方案

11. **Playwright 降级**：当 B3 方案失效时自动切换到 A 方案
12. **Docker 部署**：一键容器化
13. **性能优化**：连接池复用、请求并发
14. **监控告警**：健康检查、日志收集
15. **安全加固**：API Key 管理、请求限流

---

## 9. 风险与注意事项

### 9.1 法律与合规风险

- ⚠️ 逆向 API 属于灰色地带，可能违反豆包服务条款
- ⚠️ 仅限个人学习和研究使用，禁止对外提供服务或商用

### 9.2 技术风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| x-flow-trace 绕过被修复 | B3 方案失效 | 自动降级到 Playwright 方案（方案A） |
| Cookie 过期 | 认证失败 | 自动检测和刷新机制，30天有效期 |
| IP 封禁 | 请求被拒绝 | 多账号轮询、控制请求频率 |
| 接口变更 | 请求/响应格式变化 | 监控接口变化，及时适配 |
| Chromium 更新 | 桌面客户端升级导致兼容性问题 | 锁定版本，延迟更新 |

### 9.3 性能考量

- **B3 方案**（推荐）：内存 < 50MB，延迟 < 100ms，无冷启动
- **A 方案**（降级）：冷启动 30-60 秒，每个 Browser Context 约 100-200MB
- 建议默认使用 B3 方案，A 方案作为降级备选

---

## 10. 结论

**可行性评估：✅ 可行**

将豆包桌面客户端包装为标准 API 在技术上完全可行，已有多个开源项目验证了该方案。

**推荐方案**：**B3（x-flow-trace 绕过）为主 + A（Playwright）降级为辅**

核心优势：

1. **极轻量** — 无需浏览器，纯 HTTP 请求，内存 < 50MB
2. **低延迟** — 直接 API 调用，无浏览器中间层
3. **抗更新** — 不依赖 a_bogus 签名算法，豆包更新签名不影响
4. **已验证** — DoubaoFreeApi 项目已在生产环境验证
5. **可降级** — 如绕过被修复，自动切换到 Playwright 方案
6. **AI Agent 友好** — 任何支持 OpenAI SDK 的 Agent 均可直接对接

**推荐技术栈**：Python + FastAPI + aiohttp + httpx

**预估开发周期**：MVP 约 1-2 天，完整功能约 1 周

---

## 11. 实施记录

### 11.1 尝试1：Cookie 提取

**时间**：2026-05-13 23:21

**方法**：从豆包桌面客户端的 SQLite Cookie 数据库提取，使用 DPAPI + AES-256-GCM 解密

**过程**：
1. 读取 `Local State` 获取 `os_crypt.encrypted_key`
2. DPAPI 解密获取 AES-256 密钥（32字节）
3. 复制 `Cookies` SQLite 数据库到临时目录（避免锁冲突）
4. 逐条解密 v10 前缀的 Cookie 值

**发现**：
- Chromium 135 的 AES-GCM 解密后，前30字节为乱码，后半段为有效值
- 可能是 Chromium 135 修改了加密格式（添加了额外元数据）
- 但通过正则提取 ASCII 部分，关键 Cookie 均可正确获取

**提取到的关键参数**：

| 参数 | 值 | 来源 |
|---|---|---|
| sessionid | `<已脱敏>` | Cookie DB |
| uid_tt | `<已脱敏>` | Cookie DB |
| device_id | `<已脱敏>` | Local State → aha.device |
| install_id | `<已脱敏>` | Local State → aha.device |

**结论**：✅ Cookie 提取成功，关键认证参数完整

---

### 11.2 尝试2：B3 方案首次测试

**时间**：2026-05-13 23:35

**方法**：基于 FastAPI 实现 OpenAI 兼容 API，使用 x-flow-trace 绕过 a_bogus

**请求格式**：
```
POST https://www.doubao.com/samantha/chat/completion?aid=497858&device_id=...&...
Headers: cookie, x-flow-trace, agw-js-conv: str, referer, origin
Body: {completion_option, conversation_id, messages: [{content_type: 2001, content: '{"text":"你好"}'}]}
```

**首次结果**：豆包 API 返回 200，但 SSE 解析后内容为空

**原因分析**：
- 豆包的 SSE 格式为三层嵌套 JSON：
  1. 外层：`{"event_data": "<JSON字符串>", "event_type": 2001, "event_id": "2"}`
  2. 中层（event_data 解析后）：`{"message": {"content_type": 2001, "content": "<JSON字符串>"}}`
  3. 内层（content 解析后）：`{"text": "你好"}`
- 原代码只做了一层解析，未处理 `event_data` 为 JSON 字符串的情况

**结论**：⚠️ API 调用成功，但 SSE 解析器需修正

---

### 11.3 尝试3：修正 SSE 解析器后重测

**时间**：2026-05-13 23:38

**修正内容**：
1. `parse_sse_line()` — 增加二次 JSON 解析：先解析外层，再解析 `event_data` 字符串
2. `extract_text_from_event()` — 从解析后的 `data.message.content` 中提取文本
3. `extract_conversation_id()` — 新增函数，从任意事件中提取 conversation_id

**测试结果**：

**非流式**：
```json
{
  "id": "chatcmpl-e177d6c7b781",
  "object": "chat.completion",
  "model": "doubao-pro-chat",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "我是字节跳动自研的人工智能，能陪你聊天、解答问题、帮你处理各类学习工作生活需求～"
    },
    "finish_reason": "stop"
  }]
}
```

**流式**：
```
data: {"id":"chatcmpl-d9ea546fd976","object":"chat.completion.chunk","choices":[{"delta":{"content":"我是字节跳动自研的智能助手"},...}]}
data: {"id":"chatcmpl-d9ea546fd976","object":"chat.completion.chunk","choices":[{"delta":{"content":"，能帮你解答问题、创作内容和处理各类事务。"},...}]}
data: [DONE]
```

**结论**：✅ **B3 方案完全成功！** 非流式和流式 API 调用均正常工作

---

### 11.4 B3 方案验证总结

| 测试项 | 结果 | 备注 |
|---|---|---|
| Health Check | ✅ 通过 | `{"status": "ok", "cookie_set": true}` |
| Models List | ✅ 通过 | 返回 3 个模型 |
| 非流式对话 | ✅ 通过 | 完整回复，OpenAI 格式 |
| 流式对话 | ✅ 通过 | 逐字输出，SSE 格式正确 |
| a_bogus 绕过 | ✅ 验证 | 不需要 a_bogus，x-flow-trace 即可 |
| Cookie 认证 | ✅ 验证 | sessionid 有效，豆包识别为已登录用户 |

**关键发现**：
1. **x-flow-trace 可以随机生成** — 不需要从浏览器抓取
2. **device_id/web_id/tea_uuid 来自 Local State** — 不需要抓包
3. **Cookie 是唯一需要定期更新的参数** — 约30天有效期
4. **豆包 SSE 格式是三层嵌套 JSON** — 这是之前所有开源项目文档未明确说明的

---

### 11.5 当前项目文件结构

```
d:\_program\Doubao\webchat-api\
├── main.py              # FastAPI 主服务（OpenAI 兼容 API）
├── config.json          # 会话配置（Cookie、device_id 等）
└── temp\                # 临时文件目录
    ├── extract_cookies.py       # Cookie 提取脚本（首次）
    ├── check_cookies_db.py      # 数据库检查脚本
    ├── debug_cookies.py         # 解密调试脚本
    ├── debug_decrypt.py         # 解密偏移量调试
    ├── extract_session.py       # 最终 Cookie 提取脚本
    ├── raw_test.py              # 原始 API 调用测试
    ├── test_api.py              # OpenAI 格式 API 测试
    ├── extracted_cookies.json   # 首次提取结果（含乱码）
    ├── extracted_session.json   # 最终提取结果
    └── raw_response.txt         # 豆包 API 原始 SSE 响应
```

---

### 11.6 下一步计划

1. **对接 Claude Code / OpenCode** — 配置 `OPENAI_API_BASE=http://localhost:8765/v1`
2. **多轮对话支持** — 利用 conversation_id 维护上下文
3. **Cookie 自动刷新** — 检测过期并提醒
4. **文件上传** — 支持图片/PDF 附件
5. ~~**Docker 部署** — 容器化一键启动~~ → 改为B2/B1方案验证

### 11.7 B2方案（Playwright签名）实施记录

#### 实施过程

1. **安装Playwright依赖**：`pip install playwright playwright-stealth` + `playwright install chromium`
2. **创建signer.py**：实现PlaywrightSigner类，封装签名逻辑
3. **首次测试失败**：`window.byted_acrawler.frontierSign` 不存在
4. **关键发现**：豆包网页签名函数已从 `window.byted_acrawler.frontierSign` 迁移到 `window.bdms.frontierSign`
5. **修复后初始化成功**：`bdms.frontierSign` 正常加载，返回 `X-Bogus` 签名值
6. **签名URL调用失败**：触发服务端验证机制（710022004 rate limited + verify类型）

#### 关键技术发现

| 项目 | 旧版（doubao-2api参考） | 当前版本 |
|---|---|---|
| 签名函数 | `window.byted_acrawler.frontierSign` | `window.bdms.frontierSign` |
| 签名结果 | `a_bogus` | `X-Bogus` |
| SDK版本 | 未知 | `bdms 1.0.1.20-alpha.14` |
| mssdk来源 | `mssdk.bytedance.com/web/r/token` | 同 |

#### 失败原因分析

使用X-Bogus签名URL调用 `/samantha/chat/completion` 时，服务端返回：

```json
{
  "code": 710022004,
  "message": "rate limited",
  "error_detail": {
    "ext": {
      "decision": {
        "type": "verify",
        "subtype": "semantic_reasoning",
        "verify_scene": "doubao_message_web"
      }
    }
  }
}
```

**根本原因**：服务端对带签名参数的请求执行更严格的验证，要求完整的浏览器指纹匹配。即使签名值正确，由于HTTP请求缺少浏览器的完整指纹（Canvas、WebGL、AudioContext等），服务端判定为非浏览器请求并触发验证。

**验证实验**：
- B2签名URL（无x-flow-trace）→ 触发验证 ❌
- B2签名URL + x-flow-trace → 触发验证 ❌
- 浏览器内fetch（完整指纹）→ 触发验证 ❌（可能因频繁调用触发账号级风控）
- B3模式（无签名 + x-flow-trace）→ 正常工作 ✅

#### B2方案结论

**❌ 不可行** — X-Bogus签名会触发服务端更严格的浏览器指纹验证，反而比不带签名更难通过。B3方案（x-flow-trace绕过）是当前唯一可行的签名绕过路径。

### 11.8 B1方案（纯Python a_bogus/X-Bogus实现）评估

基于B2方案的失败经验，B1方案的可行性评估如下：

- B2使用Playwright生成的真实X-Bogus签名尚且无法通过验证
- B1纯Python实现的签名更不可能通过（缺乏浏览器环境）
- **结论：B1方案同样不可行，无需进一步实施**

---

## 12. 豆包特殊功能复用可行性分析

豆包除了基础对话模型外，还提供了14种特殊功能。本节逐一分析这些功能是否可以复用到我们的API服务中。

### 12.1 功能总览与可行性评级

| 功能 | 可行性 | 技术路径 | 核心障碍 |
|---|---|---|---|
| 思考模式 | ⭐⭐⭐ 高 | 同一端点，参数切换 | 需确认 `use_deep_think` 参数 |
| 编程模式 | ⭐⭐⭐ 高 | 同一端点，不同 bot_id | 需捕获编程模式 bot_id |
| 超能模式 | ⭐⭐⭐ 高 | 同一端点，参数切换 | 需确认模式切换参数 |
| 帮我写作 | ⭐⭐⭐ 高 | 同一端点，不同 bot_id | 需捕获写作 bot_id |
| 翻译 | ⭐⭐⭐ 高 | 同一端点，不同 prompt | 无实质障碍 |
| 解题答疑 | ⭐⭐⭐ 高 | 同一端点，不同 bot_id | 需捕获答疑 bot_id |
| 数据分析 | ⭐⭐ 中 | 同一端点 + 代码执行 | 代码执行环境不可复用 |
| 深入研究 | ⭐⭐ 中 | 同一端点 + 联网搜索 | 联网搜索参数未明 |
| 生成图片 | ⭐⭐ 中 | 独立端点 | 需发现端点 + 图片二进制处理 |
| 生成PPT | ⭐ 低 | 独立服务 | 文档生成管线复杂 |
| AI播客 | ⭐ 低 | 独立管线 | 音频生成 + 双角色对话 |
| 视频生成 | ⭐ 低 | 独立端点 | 重型处理 + 视频二进制 |
| 音乐生成 | ⭐ 低 | 独立服务 | 音频生成管线 |
| 记录会议 | ⭐ 低 | 独立转录服务 | 实时音频流处理 |

### 12.2 A类：高可行性功能（同一 samantha 端点）

以下功能共享 `/samantha/chat/completion` 端点，仅需修改请求参数即可实现。

#### 12.2.1 思考模式（Deep Thinking）

**功能描述**：开启后豆包进行"边想边搜"的深度推理，串联多维度信息，拆解复杂问题、梳理逻辑链条，输出更全面、更严谨的结果。

**技术实现**：在请求体 `ext` 字段中设置 `use_deep_think: "1"`：

```json
{
  "messages": [...],
  "completion_option": {...},
  "ext": {
    "use_deep_think": "1",
    "fp": "jverify_xxx"
  }
}
```

**复用障碍**：
- ⚠️ 思考模式的SSE响应可能包含新的 `content_type` 值（思考过程 vs 最终回答），需适配解析
- ⚠️ 思考过程可能以 `content_type: 2008`（代码块）或新类型返回
- ✅ 认证机制完全相同，B3方案直接可用

**实现建议**：在 `MODEL_BOT_MAP` 中新增 `"doubao-thinking"` 映射，请求时自动附加 `use_deep_think` 参数。

#### 12.2.2 编程模式（Coding Mode）

**功能描述**：豆包编程模式基于 Doubao-Seed-Code 模型，支持多文件上传、GitHub仓库引入、代码编辑器，可生成和运行代码，支持可视化编辑。

**技术实现**：编程模式使用独立的 `bot_id`，通过同一 samantha 端点调用：

```json
{
  "messages": [...],
  "bot_id": "<coding_bot_id>",
  "ext": {
    "fp": "jverify_xxx"
  }
}
```

**复用障碍**：
- ⚠️ 编程模式的 `bot_id` 需要从网页端抓包获取
- ⚠️ 代码执行功能（沙箱运行）无法通过API复用，只能获取代码文本
- ⚠️ 可视化编辑功能依赖前端渲染，API无法复用
- ✅ 纯代码生成/问答部分可以复用

**实现建议**：抓取编程模式 bot_id 后添加到 `MODEL_BOT_MAP`，作为纯代码生成模型使用。

#### 12.2.3 超能模式（Super Mode）

**功能描述**：超能模式是豆包的增强对话模式，整合了联网搜索、深度思考、多轮推理等能力，自动判断是否需要搜索和深度分析。

**技术实现**：可能通过 `bot_id` 切换或 `ext` 中的模式参数实现：

```json
{
  "ext": {
    "use_deep_think": "1",
    "use_search": "1",
    "fp": "jverify_xxx"
  }
}
```

**复用障碍**：
- ⚠️ 超能模式的具体参数组合需抓包确认
- ⚠️ 联网搜索结果的SSE格式可能与普通对话不同
- ✅ 认证机制相同

**实现建议**：抓取超能模式请求参数，作为独立模型映射。

#### 12.2.4 帮我写作（Writing Assistant）

**功能描述**：提供公文、邮件、文案、小说、论文等多种写作模板，按场景生成结构化文本。

**技术实现**：使用独立的 `bot_id`，本质是预设 system prompt 的对话：

```json
{
  "bot_id": "<writing_bot_id>",
  "messages": [{"content": "{\"text\":\"<|im_start|>user\n写一篇关于...的文章\n<|im_end|>\n"}...}]
}
```

**复用障碍**：
- ⚠️ 写作模板的 `bot_id` 需抓包获取
- ⚠️ 不同写作类型（公文/邮件/小说）可能使用不同 bot_id
- ✅ 返回格式与普通对话相同，SSE解析无需修改

**实现建议**：抓取常用写作 bot_id，映射为 `doubao-writing-essay`、`doubao-writing-email` 等模型。

#### 12.2.5 翻译（Translation）

**功能描述**：支持多语言互译，自动检测源语言，保持原文语义和语气。

**技术实现**：无需独立 bot_id，直接在 prompt 中指定翻译任务即可：

```
请将以下文本翻译为英文：[原文]
```

**复用障碍**：
- ✅ 无实质障碍，当前 API 已支持翻译功能
- ✅ 可通过 system prompt 优化翻译质量

**实现建议**：在 `MODEL_BOT_MAP` 中新增 `"doubao-translator"` 映射，请求时自动注入翻译 system prompt。

#### 12.2.6 解题答疑（Problem Solving）

**功能描述**：针对数学、物理、化学等学科题目，提供逐步解题过程和答案。

**技术实现**：使用独立的 `bot_id`，可能配合 `use_deep_think` 参数：

```json
{
  "bot_id": "<tutor_bot_id>",
  "ext": {"use_deep_think": "1", "fp": "jverify_xxx"}
}
```

**复用障碍**：
- ⚠️ 解题 bot_id 需抓包获取
- ⚠️ 图片题目需要图片上传能力（当前API不支持）
- ✅ 纯文本题目可直接使用

**实现建议**：抓取解题 bot_id，映射为 `"doubao-tutor"` 模型。

### 12.3 B类：中等可行性功能

#### 12.3.1 数据分析（Data Analysis）

**功能描述**：上传数据文件（CSV/Excel），自动分析数据、生成图表和洞察报告。

**技术实现**：基于 samantha 端点 + 代码执行环境：

1. 用户上传数据文件
2. 豆包生成分析代码（Python）
3. 在沙箱中执行代码
4. 返回分析结果和图表

**复用障碍**：
- ❌ **代码执行环境不可复用**：豆包的沙箱在服务端运行，API无法触发
- ❌ **文件上传机制未逆向**：需研究附件上传端点
- ✅ 纯代码生成部分可复用（生成分析代码但不执行）
- ⚠️ 图表以图片形式返回，需处理图片URL

**实现建议**：作为"代码生成器"使用——用户描述数据，API返回分析代码，用户自行执行。

#### 12.3.2 深入研究（Deep Research）

**功能描述**：对复杂主题进行多轮搜索和分析，生成详细研究报告，支持文档和网页两种输出格式。

**技术实现**：基于 samantha 端点 + 联网搜索 + 多轮推理：

1. 用户提出研究问题
2. 豆包自动分解为多个搜索查询
3. 逐个搜索并整合信息
4. 生成结构化研究报告

**复用障碍**：
- ⚠️ 联网搜索的触发参数未明（可能是 `use_search: "1"` 或特定 bot_id）
- ⚠️ 搜索结果的SSE格式可能包含搜索来源信息
- ⚠️ 多轮推理过程可能产生大量中间SSE事件
- ✅ 最终报告仍是文本格式，可解析

**实现建议**：抓取深入研究模式的请求参数，适配SSE解析器处理搜索中间事件。

#### 12.3.3 生成图片（Image Generation）

**功能描述**：基于 Seedream 模型，支持文生图、图生图、AI修图，可指定风格、比例、分辨率。

**技术实现**：使用独立的图片生成端点（非 samantha）：

- 可能端点：`/samantha/image/generation` 或 `/mira/generation`
- 请求包含 prompt、风格、尺寸等参数
- 响应返回图片URL或base64

**复用障碍**：
- ⚠️ **端点未确认**：需抓包获取实际图片生成API端点
- ⚠️ **图片二进制处理**：需下载图片并转换为base64或URL返回
- ⚠️ **OpenAI images API 兼容**：需实现 `/v1/images/generations` 端点
- ⚠️ **异步生成**：图片生成通常需要10-30秒，可能需要轮询机制
- ✅ 认证机制可能相同（Cookie + x-flow-trace）

**实现建议**：
1. 抓包确认图片生成端点和请求格式
2. 实现 `/v1/images/generations` OpenAI兼容端点
3. 异步生成 + 轮询/回调机制

### 12.4 C类：低可行性功能

#### 12.4.1 生成PPT

**功能描述**：根据主题自动生成完整PPT，包含封面、目录、内容页、结尾页，支持模板选择。

**复用障碍**：
- ❌ PPT生成是独立的服务端管线，涉及模板渲染、布局算法
- ❌ 输出为二进制文件（.pptx），非文本流
- ❌ 可能使用独立的文档生成微服务
- ❌ OpenAI API 无 PPT 生成标准，需自定义接口

**结论**：无法通过 samantha 端点复用，需独立逆向文档生成服务。

#### 12.4.2 AI播客 ✅ 已实现

**功能描述**：上传PDF或网页链接，生成两个AI角色之间的对话式播客，语音自然流畅。

**原评估**（⭐ 低可行性）→ **已实现** ✅

**实现路径**：
1. **脚本生成**：通过 `/chat/completion` API + `doubao-podcast` 模型生成双人对话脚本
2. **TTS音频合成**：逆向火山引擎V1 WebSocket TTS协议，使用豆包原生音色
3. **音频合并**：ffmpeg 合并前置音乐 + 分段音频 + 后置音乐

**关键技术突破**：详见第16章「火山引擎 TTS 逆向工程」

#### 12.4.3 视频生成

**功能描述**：基于 Seedance 模型，支持文生视频、图生视频，生成5-10秒短视频。

**复用障碍**：
- ❌ 视频生成是计算密集型任务，使用独立GPU集群
- ❌ 生成时间长达1-5分钟，需要异步任务队列
- ❌ 输出为视频文件（.mp4），非文本流
- ❌ 可能有独立的风控和配额限制

**结论**：技术上可能通过独立端点调用，但处理流程与文本API差异太大。

#### 12.4.4 音乐生成

**功能描述**：输入主题或歌词，设置风格、情绪、音色，生成约1分钟歌曲。

**复用障碍**：
- ❌ 音乐生成使用独立的音频合成管线
- ❌ 输出为音频文件，非文本
- ❌ 参数复杂（BPM、调性、乐器、音色等）
- ❌ 可能有独立的风控限制

**结论**：与文本API架构差异太大，不建议复用。

#### 12.4.5 记录会议

**功能描述**：自动记录、总结、结构化会议讨论，支持章节分段、音频回放、全文下载。

**复用障碍**：
- ❌ 需要实时音频流输入（麦克风权限）
- ❌ 涉及ASR（语音识别）→ NLU（理解）→ 总结 的多步管线
- ❌ 实时性要求高，非请求-响应模式
- ❌ 输出包含时间戳、章节标记等结构化数据

**结论**：需要实时音频流，API模式无法复用。

### 12.5 实施优先级建议

基于可行性分析，建议按以下优先级实施：

#### Phase 1：零成本扩展（仅需参数调整）

| 优先级 | 功能 | 工作量 | 预期效果 |
|---|---|---|---|
| P0 | 思考模式 | 小 | 添加 `use_deep_think` 参数 |
| P0 | 翻译 | 小 | 注入翻译 system prompt |
| P1 | 编程模式 | 中 | 抓取 bot_id + 映射 |
| P1 | 帮我写作 | 中 | 抓取 bot_id + 映射 |
| P1 | 解题答疑 | 中 | 抓取 bot_id + 映射 |
| P2 | 超能模式 | 中 | 抓取参数组合 |

#### Phase 2：中等成本扩展（需新端点适配）

| 优先级 | 功能 | 工作量 | 预期效果 |
|---|---|---|---|
| P3 | 深入研究 | 大 | 适配搜索SSE事件 |
| P3 | 数据分析 | 大 | 代码生成（无执行） |
| P4 | 生成图片 | 大 | 新端点 + 图片处理 |

#### Phase 3：暂不实施

生成PPT、AI播客、视频生成、音乐生成、记录会议——管线复杂度远超文本API，投入产出比极低。

### 12.6 关键技术问题待确认

1. **各功能的 bot_id**：需在豆包网页端使用各功能时抓包获取
2. **思考模式的 SSE 格式**：思考过程和最终回答的 content_type 值
3. **超能模式的参数组合**：`use_deep_think` + `use_search` 是否足够
4. **图片生成端点**：是否使用 `/samantha/` 前缀还是独立服务
5. **联网搜索触发方式**：是通过参数还是 bot_id 切换

---

## 13. V3.0 实施记录：Vision + 特殊模式 + 图片上传

### 13.1 版本概述

V3.0 在 V2.0 基础上实现了三大核心功能：

1. **图片上传与 Vision 识别**：支持 OpenAI Vision API 格式，自动上传图片到豆包 ImageX 服务
2. **特殊模式模型**：思考、编程、超能、写作、翻译、解题答疑 6 种特殊模式
3. **图片生成响应处理**：解析豆包返回的 AI 生成图片（content_type=2074）

### 13.2 图片上传实现

#### 13.2.1 上传流程

图片上传采用豆包官方的 ImageX 服务 4 步流程：

```
1. prepare_upload → 获取 AWS 临时凭证（access_key, secret_key, session_token）
2. ApplyImageUpload → 获取存储地址（StoreUri, Auth, SessionKey）
3. Upload Binary → 上传图片二进制到 TOS 存储
4. CommitImageUpload → 确认上传，获取 ImageUri
```

#### 13.2.2 关键技术点

- **AWS4 认证**：使用 `requests_aws4auth` 库实现 AWS Signature V4 签名
- **CRC32 校验**：上传时需计算文件的 CRC32 值，放入 `content-crc32` 请求头
- **图片格式支持**：JPEG、PNG、GIF、WebP、BMP
- **输入方式**：支持 URL 下载和 base64 data URL 两种方式

#### 13.2.3 附件格式

上传完成后，生成符合豆包消息格式的附件对象：

```json
{
  "key": "tos-cn-i-a9rns2rl98/xxx.png",
  "name": "upload_xxx.png",
  "type": "image",
  "file_review_state": 3,
  "file_parse_state": 3,
  "identifier": "uuid",
  "option": {"height": 258, "width": 540},
  "md5": "xxx",
  "size": 15444
}
```

### 13.3 Vision API 兼容

#### 13.3.1 OpenAI Vision 格式

完全兼容 OpenAI Vision API 的 `image_url` 格式：

```json
{
  "model": "doubao-pro-chat",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "描述这张图片"},
      {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
    ]
  }]
}
```

#### 13.3.2 处理流程

1. 解析消息中的 `image_url` 类型内容
2. 下载图片（URL）或解码（base64）
3. 调用 `uploader.py` 上传到 ImageX
4. 将附件对象附加到最后一条消息的 `attachments` 字段
5. 发送请求到豆包 API

#### 13.3.3 支持的图片输入方式

| 方式 | 格式 | 示例 |
|---|---|---|
| HTTP URL | `https://...` | `{"url": "https://example.com/img.png"}` |
| Base64 Data URL | `data:image/png;base64,...` | `{"url": "data:image/png;base64,iVBOR..."}` |

### 13.4 特殊模式实现

#### 13.4.1 模型映射表

| 模型 ID | 模式 | 关键参数 | System Prompt |
|---|---|---|---|
| `doubao-pro-chat` | 快速模式 | 默认 | 无 |
| `doubao-thinking` | 思考模式 | `use_deep_think: true` | 无 |
| `doubao-expert` | 超能模式 | `use_auto_cot: true` | 无 |
| `doubao-coding` | 编程模式 | `use_auto_cot: true` | 编程助手 prompt |
| `doubao-writing` | 写作助手 | 默认 | 写作助手 prompt |
| `doubao-translator` | 翻译 | 默认 | 翻译助手 prompt |
| `doubao-tutor` | 解题答疑 | `use_deep_think: true` | 解题老师 prompt |
| `doubao-lite-chat` | 轻量模式 | 默认 | 无 |
| `doubao-pro-32k` | Pro 32K | 默认 | 无 |
| `doubao-pro-128k` | Pro 128K | 默认 | 无 |

#### 13.4.2 参数切换机制

特殊模式通过两种机制实现：

1. **参数切换**：`use_deep_think`（思考模式）、`use_auto_cot`（超能/编程模式）
2. **System Prompt 注入**：写作、翻译、解题模式通过预设 system prompt 实现角色专业化

### 13.5 图片生成响应处理

当豆包返回 AI 生成的图片时（content_type=2074），解析逻辑：

1. 从 SSE 事件中提取 `creations` 数组
2. 遍历每个 creation，提取 `image_raw.url` 或 `image_thumb.url`
3. 以 Markdown 图片格式 `![image](url)` 嵌入到回复文本中
4. 同时在响应的 `images` 字段中返回所有图片 URL 列表

### 13.6 新增 API 端点

| 端点 | 方法 | 功能 |
|---|---|---|
| `/v1/images/upload` | POST | 直接上传图片文件，返回附件对象 |
| `/v1/models` | GET | 返回所有模型列表（含能力描述） |

### 13.7 测试结果

| 测试项 | 结果 | 备注 |
|---|---|---|
| 基础对话 (doubao-pro-chat) | ✅ 通过 | 非流式+流式均正常 |
| 思考模式 (doubao-thinking) | ✅ 通过 | use_deep_think 参数生效 |
| Vision 图片识别 | ✅ 通过 | 百度Logo成功识别 |
| 图片上传流程 | ✅ 通过 | prepare→apply→upload→commit 全流程 |
| URL 图片下载 | ✅ 通过 | HTTP URL 自动下载并上传 |
| 模型列表 API | ✅ 通过 | 10个模型含能力描述 |
| 健康检查 API | ✅ 通过 | 含 features 能力列表 |
| 管理面板图片上传 | ✅ 通过 | 文件选择+预览+发送 |

### 13.8 待解决问题

1. **编程模式 bot_id**：当前使用通用 bot_id，需抓取编程模式专用 bot_id
2. **写作/解题 bot_id**：同上，专用 bot_id 可能提供更好的效果
3. **联网搜索参数**：超能模式的 `use_search` 参数已添加到 ext 字段 ✅
4. **图片生成端点**：doubao-image 模型已重定向到 generate_images() ✅
5. **base64 大图片**：超过 10MB 的 base64 图片可能上传失败

---

## 14. v3.4.0 功能完善 (05-14 20:00)

### 14.1 修复4大用户反馈问题

| 问题 | 修复方案 | 状态 |
|---|---|---|
| doubao-image 不生成图片 | 重定向到 `generate_images()` 并格式化为 chat completion | ✅ |
| 超能模式未激活 | 添加 `use_deep_think: True` + `use_search: "1"` 到 ext 字段 | ✅ |
| thinking 内容未返回 | 通过 `reasoning_content` 字段返回，兼容 OpenAI o1 格式 | ✅ |
| AI播客模式未触发 | 新增 `doubao-podcast` 模型，自动调用播客生成流程 | ✅ |

### 14.2 Cookie 过期检测与自动刷新

- `CookiePool.is_cookie_expired()` 检测 710022004/rate_limit/verify 等错误
- `CookiePool.maybe_refresh()` 自动调用 `extract_session.py` 刷新
- SSE 流内错误检测：`call_doubao_api` 在流中检测到过期时自动重试

### 14.3 请求限流和并发控制

- `RateLimiter`: 30请求/60秒窗口，按IP限流，超限返回 429
- `ConcurrencyLimiter`: 最大5并发，信号量控制核心API端点
- `/health` 端点新增 `concurrency` 和 `rate_limit` 状态信息

### 14.4 会话自动清理

- `delete_conversation()`: 调用豆包 `POST /samantha/thread/delete` API
- `DELETE /v1/conversations/{id}`: 手动删除指定会话
- `POST /v1/conversations/cleanup`: 批量清理过期会话
- 后台定时任务：每小时清理超过24小时的旧会话

### 14.5 其他改进

- 迁移 `on_event` 到 `lifespan` 上下文管理器（消除 DeprecationWarning）
- 前端显示 thinking 内容为可折叠 `<details>` 区块
- 前端新增 `doubao-podcast` 模型选项
- 图片生成限流时显示"请求被限流"而非"图片生成失败"

---

## 15. V3.5 实施记录：音乐生成 + 对话导出 + CloakBrowser (05-15)

### 15.1 音乐生成功能

#### 15.1.1 逆向分析

豆包音乐生成采用两阶段管线：

| 阶段 | API | 说明 |
|---|---|---|
| 歌词生成 | `/samantha/chat/completion` (content_type=2005) | 通过 SSE 流式返回歌词 |
| 音频合成 | 后台异步 + WebSocket 推送 (content_type=2006) | 音乐卡片包含封面、标题、时长、音频URL |

**关键发现**：

1. **content_type=2005** 触发音乐生成模式，豆包自动生成歌词模板
2. **content_type=2006** 是音乐卡片推送，包含 `video_model` 字段（Base64编码的音频URL）
3. 音频URL解码方式：`video_model.video_list.*.main_url` → Base64解码 → 实际音频URL
4. WebSocket 地址：`wss://www.doubao.com/ws-samantha`，需 `Sec-WebSocket-Protocol: samantha`

#### 15.1.2 实现架构

```
用户请求 (doubao-music)
  → openai_api.py: _stream_music_generation()
    → music.py: start_music_generation() [异步任务]
      → _run_music_generation_api() [SSE歌词生成]
        → 失败时 → _run_music_generation_playwright() [Playwright降级]
      → WebSocket 监听音乐卡片推送
      → 轮询音乐状态 (FPA_Music API)
    → SSE流返回歌词 + 音乐任务ID
  → 前端轮询 /v1/music/status/{task_id}
  → 音乐完成后显示播放器
```

#### 15.1.3 Cookie池与故障转移

音乐生成容易触发频率限制（710022错误），实现了多层故障转移：

1. **主账号** → API调用
2. **备用账号** → API调用（Cookie池轮询）
3. **Playwright** → 浏览器自动化（最后手段）

#### 15.1.4 Web界面音乐播放

音乐生成完成后，Web界面显示完整的播放卡片：
- 封面图片
- 歌曲标题
- 时长显示
- HTML5 `<audio>` 播放控件

### 15.2 对话导出功能

#### 15.2.1 技术挑战

豆包网站的API端点分为两类：

| 类型 | 端点示例 | B3方案可用 | 原因 |
|---|---|---|---|
| 聊天补全 | `/samantha/chat/completion` | ✅ 可用 | x-flow-trace 绕过签名 |
| 其他API | `/samantha/chat/conversation/list` | ❌ 不可用 | 需要 a_bogus 签名 |
| 用户信息 | `/alice/profile/self` | ❌ 不可用 | 需要 a_bogus 签名 |

直接调用 `/samantha/chat/conversation/list` 返回 404，调用 `/api/doubao/` 端点返回 401。

#### 15.2.2 解决方案：浏览器路由拦截

通过 CloakBrowser/Playwright 访问豆包网站，设置路由拦截器捕获 API 响应：

```python
async def capture_conv_list(route):
    response = await route.fetch()  # 让浏览器正常发起请求（自动签名）
    body = await response.json()     # 拦截响应
    # 解析对话列表...
    await route.fulfill(response=response)  # 返回原始响应给页面
```

**关键洞察**：浏览器发起请求时，豆包前端 JS 会自动注入 `a_bogus` 签名参数。通过 `route.fetch()` 让浏览器正常发起请求，然后拦截响应数据。

#### 15.2.3 用户信息获取

发现用户信息API的正确路径：

| 尝试的路径 | 结果 | 说明 |
|---|---|---|
| `/samantha/user/info` | 404 | 不存在 |
| `/api/doubao/user/info` | 401 | 需要签名 |
| `/alice/profile/self` | 200 (需签名) | ✅ 正确路径 |

`/alice/profile/self` 返回的数据结构：

```json
{
  "data": {
    "profile_brief": {
      "nickname": "Yhq",
      "user_name": "user_51946402306",
      "id": 51946402306,
      "entity_id": "7xxx",
      "image": {
        "tiny_url": "https://lf3-static.bytednsdoc.com/..."
      }
    }
  }
}
```

#### 15.2.4 智能账号选择

实现了 `_get_valid_account()` 函数，通过解析 `sid_guard` Cookie 字段判断账号有效性：

```python
# sid_guard 格式: session_id|created_timestamp|ttl_seconds|version
# 例如: xxx|1715731200|2592000|v1
# 过期时间 = created_timestamp + ttl_seconds
```

优先选择 `sid_guard` 未过期的账号，避免使用已失效的 Cookie。

#### 15.2.5 媒体下载

导出对话时自动下载媒体文件：

| 媒体类型 | 下载方式 | 存储路径 |
|---|---|---|
| 图片 | aiohttp 直接下载 | `exports/media/{conv_id}/{msg_id}_img0.jpg` |
| 音频 | aiohttp 直接下载 | `exports/media/{conv_id}/{msg_id}_audio.mp3` |
| 视频 | aiohttp 直接下载 | `exports/media/{conv_id}/{msg_id}_video.mp4` |

下载完成后，JSON中的URL替换为本地路径，方便本地引用。

### 15.3 CloakBrowser 集成

#### 15.3.1 为什么需要 CloakBrowser

Playwright 容易被反爬机制识别：

| 检测维度 | Playwright | CloakBrowser |
|---|---|---|
| `navigator.webdriver` | `true` | `undefined` |
| Chrome DevTools Protocol | 可检测 | 已修补 |
| reCAPTCHA v3 得分 | 0.1-0.3 | 0.9 |
| Cloudflare Turnstile | 被拦截 | 通过 |
| 浏览器指纹 | 自动化特征 | 与真实用户一致 |

#### 15.3.2 集成方式

CloakBrowser 是 Playwright 的直接替代品，API完全兼容：

```python
# 优先使用 CloakBrowser
try:
    from cloakbrowser import async_launch
    browser = await async_launch(headless=True, humanize=True)
except ImportError:
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, ...)
```

`humanize=True` 启用人类行为模拟（随机延迟、自然鼠标移动等）。

#### 15.3.3 应用场景

| 场景 | 使用方式 |
|---|---|
| 用户信息获取 | CloakBrowser 访问豆包网站，拦截 `/alice/profile/self` |
| 对话列表获取 | CloakBrowser 访问豆包网站，拦截 `/samantha/chat/conversation/list` |
| 对话消息获取 | CloakBrowser 访问指定对话，拦截消息API |
| 音乐生成降级 | CloakBrowser 替代 Playwright 作为最后手段 |

### 15.4 Web界面改进

| 改进项 | 说明 |
|---|---|
| Favicon | 内联SVG（紫色圆形+豆字） |
| 用户名显示 | 侧边栏显示头像+昵称 |
| 播客按钮移除 | 统一到模型选择标签页 |
| 导出/导入面板 | 新增面板，含豆包对话抓取、本地导出导入、媒体任务管理 |
| 音乐播放卡片 | 聊天界面内嵌音频播放器（封面+标题+时长+控件） |

### 15.5 新增API端点

| 端点 | 方法 | 功能 |
|---|---|---|
| `/v1/user/info` | GET | 获取当前豆包用户信息（昵称、头像、用户ID） |
| `/v1/doubao/conversations` | GET | 获取豆包网站对话列表 |
| `/v1/doubao/conversations/{id}/export` | GET | 导出指定对话（含媒体下载） |
| `/v1/music/generate` | POST | 音乐生成 |
| `/v1/music/status/{task_id}` | GET | 音乐生成状态查询 |
| `/v1/music/audio/{task_id}` | GET | 获取音乐音频URL |
| `/v1/music/list` | GET | 音乐任务列表 |
| `/v1/music/styles` | GET | 音乐风格列表 |

### 15.6 测试结果

| 测试项 | 结果 | 备注 |
|---|---|---|
| 音乐生成 (doubao-music) | ✅ 通过 | 歌词+音频完整生成，Web端可播放 |
| 用户信息获取 | ✅ 通过 | 昵称"Yhq"，头像URL正确 |
| CloakBrowser 集成 | ✅ 通过 | 自动检测并使用，降级到Playwright正常 |
| Cookie有效性检测 | ✅ 通过 | sid_guard过期时间解析正确 |
| Web界面音乐播放 | ✅ 通过 | 音频播放器正常工作，封面+标题+时长显示 |
| Favicon显示 | ✅ 通过 | 内联SVG正常渲染 |
| 导出/导入面板 | ✅ 通过 | 面板功能完整 |

---

## 16. 火山引擎 TTS 逆向工程

### 16.1 背景

豆包的播客功能使用火山引擎（字节跳动云服务）的语音合成（TTS）服务生成音频。为实现原生音质的播客音频生成，我们对TTS协议进行了完整逆向。

### 16.2 TTS 凭据获取

#### 16.2.1 凭据来源

TTS认证凭据通过豆包的 `/alice/user/launch` API获取：

```
POST https://www.doubao.com/alice/user/launch?aid=497858&...
Cookie: <用户cookie>
```

响应中的关键字段：

```json
{
  "data": {
    "config": {
      "audio_app_key": "<APP_KEY>",
      "audio_token": "<JWT_TOKEN>",
      "enterprise_audio_app_key": "<ENTERPRISE_APP_KEY>"
    }
  }
}
```

| 字段 | 用途 | 示例值 |
|------|------|--------|
| `audio_app_key` | TTS appid | `<APP_KEY>` |
| `audio_token` | TTS JWT token | `<JWT_TOKEN>` (有效期约1小时) |
| `enterprise_audio_app_key` | 企业版appid | `<ENTERPRISE_APP_KEY>` |

#### 16.2.2 JWT Token 解析

`audio_token` 是标准JWT，payload包含：

```json
{
  "iss": "data.speech.saas_ram",
  "account_id": "<ACCOUNT_ID>",
  "app_id": <APP_ID>,
  "appkey": "<APP_KEY>",
  "exp": 1718841600,
  "iat": 1718838000
}
```

#### 16.2.3 B2签名器

使用B2签名器（Playwright + a_bogus签名）调用launch API时，凭据返回正常。不使用签名时，凭据字段为空。

### 16.3 协议分析

#### 16.3.1 V3 双向流式协议（失败）

火山引擎公开文档描述了V3协议：

- **端点**: `wss://openspeech.bytedance.com/api/v3/tts/bidirection`
- **认证**: HTTP请求头 `X-Api-App-Key` + `X-Api-Access-Key` + `X-Api-Resource-Id`
- **协议**: 4字节header + event number + session_id + payload

**失败原因**：V3协议的 `X-Api-App-Key` 头期望火山引擎控制台注册的appid，而豆包的appid不被V3端点识别，返回401。

#### 16.3.2 V1 二进制协议（成功 ✅）

V1协议是火山引擎TTS的旧版协议，与豆包的凭据兼容：

- **端点**: `wss://openspeech.bytedance.com/api/v1/tts/ws_binary`
- **认证**: JSON payload中的 `appid` + `token` 字段
- **无需HTTP头认证**

### 16.4 V1 协议详细规范

#### 16.4.1 请求帧格式

```
┌──────────┬──────────────┬──────────────┬──────────────┬──────────────┐
│ Byte 0   │ Byte 1       │ Byte 2       │ Byte 3       │ Payload      │
├──────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ ver│hsz  │ type│flags   │ ser│comp     │ reserved     │ JSON payload │
└──────────┴──────────────┴──────────────┴──────────────┴──────────────┘
```

| 字段 | 位数 | 说明 |
|------|------|------|
| ver (4bit) | 协议版本，固定 `0b0001` | |
| hsz (4bit) | header大小/4，固定 `0b0001` (4字节) | |
| type (4bit) | 消息类型：`0b0001`=FullClient, `0b1001`=FullServer, `0b1011`=AudioOnly, `0b1111`=Error | |
| flags (4bit) | 序列号标志：`0b0000`=无, `0b0001`=正序, `0b0010`=负序(最后一帧) | |
| ser (4bit) | 序列化方式：`0b0000`=Raw, `0b0001`=JSON | |
| comp (4bit) | 压缩方式：`0b0000`=无, `0b0001`=gzip | |

#### 16.4.2 请求JSON结构

```json
{
  "app": {
    "appid": "<APP_KEY>",
    "token": "<audio_token>",
    "cluster": "volcano_tts"
  },
  "user": {
    "uid": "doubao_podcast"
  },
  "audio": {
    "voice_type": "zh_female_wenroutaozi_uranus_bigtts",
    "encoding": "mp3",
    "speed_ratio": 1.0,
    "volume_ratio": 1.0,
    "pitch_ratio": 1.0
  },
  "request": {
    "reqid": "<uuid>",
    "text": "你好世界",
    "text_type": "plain",
    "operation": "submit"
  }
}
```

#### 16.4.3 响应帧解析

**Full Server Response** (type=0b1001, flags=0b0000):
- 4字节header + 4字节payload_size + JSON payload
- 包含TTS任务状态信息

**Audio Only Response** (type=0b1011, flags=0b0001或0b0010):
- 4字节header + **4字节sequence_number** + 4字节payload_size + raw audio bytes
- flags=0b0010 表示最后一帧

**Error Response** (type=0b1111):
- 4字节header + 4字节payload_size + JSON payload
- 包含错误详情，如 `{"code": 3001, "message": "engine process fail!"}`

#### 16.4.4 关键发现：sequence number 字段

Audio-only帧在flags指示有序列号时（flags & 0x01 或 flags & 0x02），header和payload_size之间有一个4字节的**有符号整数**序列号字段。这是解析V1协议最容易出错的地方：

```
错误解析（缺少seq字段）：
  header(4) + payload_size(4) + payload → 只读到5字节数据

正确解析：
  header(4) + seq_num(4) + payload_size(4) + payload → 读到7425字节数据
```

### 16.5 音色兼容性

#### 16.5.1 豆包appid支持的音色

豆包web客户端的appid仅注册了1个音色：

| 音色ID | 名称 | 状态 |
|--------|------|------|
| `zh_female_wenroutaozi_uranus_bigtts` | 温柔桃子（女声） | ✅ 可用 |
| `zh_male_chunhou_uranus_bigtts` | 醇厚（男声） | ❌ engine process fail |
| `zh_male_chunhou_moon_bigtts` | 醇厚moon（男声） | ❌ engine process fail |
| `zh_female_shuangkuaisisi_moon_bigtts` | 双快丝丝（女声） | ❌ engine process fail |

#### 16.5.2 cluster 测试

| cluster | 结果 |
|---------|------|
| `volcano_tts` | ✅ 女声可用 |
| `volcano_mega_tts` | ❌ 所有音色失败 |

**结论**：豆包的appid是受限的，只允许使用注册时指定的音色和cluster。如需更多音色，需在火山引擎控制台自行申请。

### 16.6 实现架构

```
┌─────────────────────────────────────────────────────────┐
│                    播客生成流程                           │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  1. 脚本生成                                            │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │ 用户请求  │───→│ /chat/       │───→│ 双人对话脚本  │  │
│  │ topic    │    │ completion   │    │ Markdown格式  │  │
│  └──────────┘    └──────────────┘    └──────┬───────┘  │
│                                              │          │
│  2. 脚本清洗 + 解析                          │          │
│  ┌──────────────────────────────────────────┐│          │
│  │ _clean_podcast_script()                  ││          │
│  │  - 过滤AI回复前缀（"好的，我来帮你..."）    ││          │
│  │  - 保留从第一个#标题开始的正文              ││          │
│  │ _parse_podcast_script()                  ││          │
│  │  - 按主播分段：host1, host2              │←┘          │
│  └──────────────────────────────────────────┘           │
│                                              │          │
│  3. TTS音频合成                              │          │
│  ┌──────────────────────────────────────────┐│          │
│  │ 优先：火山引擎V1 TTS                     ││          │
│  │  - get_tts_credentials() 获取凭据        ││          │
│  │  - volcengine_tts_segmented() 分段合成    ││          │
│  │  - 每段独立WebSocket连接                 ││          │
│  │ 回退：edge-tts                           ││          │
│  │  - 代理支持（9个本地代理轮询）            ││          │
│  └──────────────────────────────────────────┘           │
│                                              │          │
│  4. 音频合并                                 │          │
│  ┌──────────────────────────────────────────┐│          │
│  │ _merge_jingle_audio()                    ││          │
│  │  - intro_jingle.mp3 + 内容 + outro.mp3   ││          │
│  │  - ffmpeg concat + fade in/out           ││          │
│  │  - 可通过参数配置开关                     ││          │
│  └──────────────────────────────────────────┘           │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 16.7 播客API端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/podcast/generate` | POST | 生成播客（支持 `intro_jingle`/`outro_jingle` 参数） |
| `/v1/podcast/status/{task_id}` | GET | 查询生成状态 |
| `/v1/podcast/audio/{task_id}` | GET | 获取音频URL+元数据 |
| `/v1/podcast/script/{task_id}` | GET | 获取播客脚本 |
| `/v1/podcast/list` | GET | 播客任务列表 |
| `/v1/podcast/file/{filename}` | GET | 音频文件下载 |
| `/v1/podcast/config` | GET/POST | 播客配置（前置/后置音乐开关） |

### 16.8 前端适配

#### 16.8.1 播客播放器

前端播客播放器支持：
- **直接播放**：`<audio>` 元素，本地文件通过 `/v1/podcast/file/` URL直接加载
- **下载按钮**：`<a download>` 链接，点击即下载MP3
- **前置/后置音乐开关**：复选框控制，参数传递到生成API
- **封面+时长显示**：从API元数据获取

#### 16.8.2 音频URL路由

```javascript
// 本地播客文件 - 直接使用API URL
if (data.audio_url.startsWith('/v1/podcast/file/')) {
  audioSrc = getApiUrl(data.audio_url);
}
// 外部URL - 走代理
else if (data.audio_url.startsWith('http')) {
  audioSrc = getApiUrl('/api/proxy/audio') + '?url=' + encodeURIComponent(data.audio_url);
}
```

### 16.9 性能数据

| 指标 | 数值 |
|------|------|
| 单段TTS延迟 | ~1-2秒（5秒音频） |
| 8段播客总耗时 | ~16秒（42秒音频） |
| 完整播客（含音乐） | ~30秒（251秒音频） |
| 音频质量 | MP3 24kHz 128kbps |
| 文件大小 | ~16KB/秒音频 |

### 16.10 已知限制与改进方向

| 限制 | 说明 | 改进方案 |
|------|------|----------|
| 单音色 | 豆包appid仅支持1个女声 | 申请火山引擎控制台自己的appid |
| Token时效 | audio_token约1小时过期 | 自动刷新机制已实现 |
| Cookie依赖 | 需要有效豆包登录态 | 多账号池轮询 |
| V3不可用 | 豆包凭据与V3认证不兼容 | 需火山引擎控制台appid |
| 无SSML | V1协议不支持SSML标记 | 使用V3协议+自有appid |

### 16.11 逆向工程方法论总结

本次TTS逆向的关键步骤：

1. **浏览器流量分析**：使用Playwright + CDP拦截WebSocket帧和HTTP请求
2. **JS代码分析**：提取并搜索压缩后的SAMI SDK代码，找到TTS客户端类
3. **公开文档对照**：火山引擎官方文档提供了V3协议完整规范
4. **协议版本切换**：V3认证失败后，尝试V1旧版协议成功
5. **二进制帧调试**：逐字节分析WebSocket帧，发现sequence number字段
6. **音色兼容性测试**：批量测试15个音色，确认豆包appid的限制

**核心教训**：当新版协议认证不兼容时，旧版协议往往是更简单的突破口。
