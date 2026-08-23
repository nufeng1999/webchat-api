import os
import sys
import json
import asyncio
import logging
import uuid
import re
import httpx
import ctypes
import time
import hashlib
import urllib.parse

from config import CONFIG, USER_AGENT, BASE_DIR

logger = logging.getLogger("webchat-browser")


def _parse_grpc_web_json_stream(raw_bytes: bytes) -> list[dict]:
    """Parse gRPC-Web JSON stream frames: [flags:1][length:4][payload]."""
    chunks = []
    offset = 0
    total = len(raw_bytes)
    while offset + 5 <= total:
        flags = raw_bytes[offset]
        length = int.from_bytes(raw_bytes[offset + 1:offset + 5], "big")
        offset += 5
        if length < 0 or offset + length > total:
            break
        payload = raw_bytes[offset:offset + length]
        offset += length
        if flags & 0x80:
            continue
        try:
            text = payload.decode("utf-8", errors="replace")
            if text:
                chunks.append(json.loads(text))
        except Exception as e:
            logger.debug(f"[gRPC-Web] parse frame failed: {e}")
    return chunks


def _bring_window_to_front():
    """用 Win32 API 查找 Edge 窗口并强制置顶显示。仅 Windows 有效。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # 枚举所有顶层窗口，找到包含 "z.ai" 或 "Edge" 的
        result = []
        def enum_callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if 'z.ai' in title.lower() or ('edge' in title.lower() and 'z.ai' in title.lower()):
                        result.append(hwnd)
            return True
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        for hwnd in result:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        if result:
            logger.info(f"[Zai] activated {len(result)} window(s) via Win32")
    except Exception as e:
        logger.debug(f"[Zai] Win32 bring to front failed: {e}")

STORAGE_STATE_PATH = os.path.join(BASE_DIR, "storage_state.json")
DOUBAO_USER_DATA_DIR = os.path.join(BASE_DIR, "profiles", "doubao_profile")


def _read_cookie_from_profile_db() -> str:
    """从 doubao_profile 的 Chrome Cookie SQLite 数据库读取 doubao.com cookie 字符串。"""
    cookie_db = os.path.join(DOUBAO_USER_DATA_DIR, "Default", "Cookies")
    if not os.path.exists(cookie_db):
        return ""
    try:
        import sqlite3
        import shutil
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"doubao_cookie_{int(time.time())}.db")
        shutil.copy2(cookie_db, tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%doubao.com%'"
        ).fetchall()
        conn.close()
        os.remove(tmp)
        if not rows:
            return ""
        parts = []
        for name, enc_val in rows:
            try:
                if not enc_val:
                    continue
                if enc_val[:3] == b'v10' or enc_val[:3] == b'v11':
                    import win32crypt
                    val = win32crypt.CryptUnprotectData(enc_val, None, None, None, 0)[1]
                else:
                    val = enc_val
                parts.append(f"{name}={val.decode('utf-8', errors='replace')}")
            except Exception:
                continue
        return "; ".join(parts) if parts else ""
    except Exception:
        return ""


async def _get_latest_cookie_async() -> str:
    """从当前 browser context 获取 cookie，fallback 到 profile DB 读取。"""
    try:
        from browser_client import browser_client
        bc = browser_client
        if bc._doubao_browser and not bc._doubao_browser.is_closed():
            cks = await bc._doubao_browser.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cks if c.get("value"))
            if cookie_str:
                return cookie_str
    except Exception:
        pass
    return _read_cookie_from_profile_db()


def get_doubao_cookie() -> str:
    """公开方法：同步获取 Doubao cookie（优先浏览器上下文，fallback 到 profile DB）。"""
    try:
        loop = asyncio.get_running_loop()
        if loop and not loop.is_closed():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _get_latest_cookie_async())
                return future.result(timeout=5)
    except RuntimeError:
        # 没有运行的事件循环，直接走 DB fallback
        pass
    return _read_cookie_from_profile_db()
COMPLETION_URL_BASE = "https://www.doubao.com/chat/completion"


def _build_completion_url():
    """构造带静态参数的 completion URL，SDK 会自动补充 msToken/a_bogus"""
    params = [
        "aid=497858",
        f"device_id={CONFIG.get('device_id', '')}",
        "device_platform=web",
        f"fp={CONFIG.get('fp', '')}",
        "language=zh",
        "pc_version=3.22.0",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={CONFIG.get('tea_uuid', '')}",
        "use-olympus-account=1",
        "version_code=20800",
        f"web_id={CONFIG.get('web_id', '')}",
        "web_platform=browser",
        f"web_tab_id={uuid.uuid4()}",
    ]
    return COMPLETION_URL_BASE + "?" + "&".join(params)


def _browser_channel():
    """获取 Playwright channel 参数。"""
    ch = CONFIG.get("_browser_channel")
    if ch is None:
        return "msedge" if sys.platform.startswith("win") else None
    # main.py 中 _browser_channel_map 将 "chromium" 映射为 None
    _channel_map = {"chromium": None, "chrome": "chrome", "edge": "msedge"}
    return _channel_map.get(ch, ch)


def _linux_safe_args():
    """构建浏览器 launch args。优先从 CONFIG['_browser_launch_args'] 读取，否则使用跨平台默认值。"""
    custom = CONFIG.get("_browser_launch_args")
    if custom and isinstance(custom, list):
        return custom
    base = ["--no-sandbox", "--disable-setuid-sandbox"]
    if not sys.platform.startswith("win"):
        base.extend(["--disable-gpu", "--disable-dev-shm-usage"])
    return base


ZAI_INIT_SCRIPT = """
// --- 反检测 & 页面增强脚本 ---
// 在页面脚本运行前注入，确保对 Playwright 的检测有效

// 1. Webdriver 反检测
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. Chrome 对象扩展
window.chrome = {
    runtime: {},
    loadTimes: () => ({}),
    csi: () => ({}),
};

// 3. 语言设置
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });

// 4. Plugins & MimeTypes 伪装
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbgmofphofjnbeflankc', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
    ],
});
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => [
        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: { name: 'Chrome PDF Plugin' } },
        { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: { name: 'Chrome PDF Viewer' } }
    ],
});

// 5. Hardware Concurrency & Device Memory
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

// 6. Touch Support (如果不需要触摸，设为0)
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

// 7. Automation 标记
Object.defineProperty(navigator, 'automation', { get: () => false });

// 8. Permissions API 覆盖
if ('permissions' in navigator) {
    const originalQuery = navigator.permissions.query;
    navigator.permissions.query = async (parameters) => {
        if (parameters.name === 'notifications') {
            return { state: 'granted' };
        }
        return originalQuery.call(navigator.permissions, parameters);
    };
}

// 9. SpeechSynthesis 覆盖
if ('speechSynthesis' in window) {
    const originalGetVoices = window.speechSynthesis.getVoices;
    window.speechSynthesis.getVoices = () => {
        return [];
    };
}

// 10. WebGL 指纹覆盖 (简单版，模拟常见显卡信息)
try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(...args) {
        const param = args[0];
        // UNMASKED_VENDOR_WEBGL (0x9245)
        if (param === 0x9245) { return 'Google Inc. (AMD)'; }
        // UNMASKED_RENDERER_WEBGL (0x9246)
        if (param === 0x9246) { return 'ANGLE (AMD, AMD Radeon(TM) Graphics (AMD Radeon(TM) Graphics), OpenGL 4.5.0)'; }
        return getParameter.apply(this, args);
    };
} catch (e) {
    console.warn('[Zai Anti-Detection] Failed to patch WebGL:', e);
}

// --- SSE 事件缓冲区（在桥接函数注册前暂存事件）---
window.__zai_sse_events = window.__zai_sse_events || [];
window.__zai_sse_flushed = false;

// --- fetch 拦截器 ---
if (!window.__zai_fetch_patched) {
    window.__zai_fetch_patched = true;
    const origFetch = window.fetch;
    const newFetch = async function(...args) {
        const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
        if (url.includes('/chat/completions')) {
            try {
                const resp = await origFetch.apply(this, args);
                const cloned = resp.clone();
                const reader = cloned.body.getReader();
                const decoder = new TextDecoder();
                (async () => {
                    let buf = '';
                    while (true) {
                        const {value, done} = await reader.read();
                        if (done) {
                            if (window.zaiOnSseChunk) {
                                window.zaiOnSseChunk(JSON.stringify({type:'chat:completion:done',data:{done:true}}));
                            } else {
                                window.__zai_sse_events.push(JSON.stringify({type:'chat:completion:done',data:{done:true}}));
                            }
                            break;
                        }
                        buf += decoder.decode(value, {stream: true});
                        const lines = buf.split('\\n');
                        buf = lines.pop() || '';
                        for (const line of lines) {
                            if (!line.startsWith('data:')) continue;
                            const raw = line.slice(5).trim();
                            if (raw === '[DONE]') {
                                if (window.zaiOnSseChunk) {
                                    window.zaiOnSseChunk(JSON.stringify({type:'chat:completion:done',data:{done:true}}));
                                } else {
                                    window.__zai_sse_events.push(JSON.stringify({type:'chat:completion:done',data:{done:true}}));
                                }
                                continue;
                            }
                            try {
                                const parsed = JSON.parse(raw);
                                const delta = parsed.choices?.[0]?.delta?.content
                                    || parsed.data?.delta_content
                                    || parsed.delta_content
                                    || '';
                                const phase = parsed.data?.phase || parsed.phase || 'answer';
                                const done = parsed.data?.done || parsed.choices?.[0]?.finish_reason === 'stop' || false;
                                if (delta) {
                                    const event = JSON.stringify({type:'chat:completion',data:{delta_content:delta,phase:phase,done:false}});
                                    if (window.zaiOnSseChunk) {
                                        window.zaiOnSseChunk(event);
                                    } else {
                                        window.__zai_sse_events.push(event);
                                    }
                                }
                                if (done) {
                                    const event = JSON.stringify({type:'chat:completion:done',data:{done:true}});
                                    if (window.zaiOnSseChunk) {
                                        window.zaiOnSseChunk(event);
                                    } else {
                                        window.__zai_sse_events.push(event);
                                    }
                                }
                            } catch(e) {
                                // 解析失败通常是思考过程纯文本，忽略
                            }
                        }
                    }
                })().catch(e => console.error('[zai-sse-read]', e));
                return resp;
            } catch(e) {
                console.error('[zai-fetch-intercept]', e);
                return origFetch.apply(this, args);
            }
        }
        return origFetch.apply(this, args);
    };
    window.fetch = newFetch;
    // 禁止覆盖
    try {
        Object.defineProperty(window, 'fetch', { value: newFetch, writable: false, configurable: false });
    } catch(e) {}
}
"""


def _browser_launch_kwargs(**kwargs):
    """构建 Playwright chromium.launch_persistent_context 的参数。
    自动处理 channel 参数：如果 CONFIG 中未指定（None），则省略 channel，
    让 Playwright 使用内置 Chromium（跨平台安全）。
    Linux 自动添加 --disable-gpu --disable-dev-shm-usage 避免白屏。
    """
    channel = _browser_channel()
    if channel:
        kwargs["channel"] = channel
    if "args" not in kwargs:
        kwargs["args"] = _linux_safe_args()
    return kwargs


__all__ = [
    "BASE_DIR",
    "COMPLETION_URL_BASE",
    "CONFIG",
    "DOUBAO_USER_DATA_DIR",
    "STORAGE_STATE_PATH",
    "USER_AGENT",
    "ZAI_INIT_SCRIPT",
    "_bring_window_to_front",
    "_browser_channel",
    "_browser_launch_kwargs",
    "_build_completion_url",
    "_get_latest_cookie_async",
    "_linux_safe_args",
    "_parse_grpc_web_json_stream",
    "_read_cookie_from_profile_db",
    "asyncio",
    "ctypes",
    "get_doubao_cookie",
    "hashlib",
    "httpx",
    "json",
    "logger",
    "logging",
    "os",
    "re",
    "sys",
    "time",
    "urllib",
    "uuid",
]
