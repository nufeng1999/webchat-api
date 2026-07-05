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
DOUBAO_USER_DATA_DIR = os.path.join(BASE_DIR, "doubao_profile")


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


class BrowserClient:
    def __init__(self):
        # Doubao 专属
        self._doubao_pw = None
        self._doubao_browser = None
        self._doubao_page = None
        self._doubao_lock = asyncio.Lock()
        self._doubao_queues = {}
        self._doubao_user_data_dir = DOUBAO_USER_DATA_DIR
        self._visible_browser_started_at = None
        self._profile_params = {}

        # Qianwen 专属
        self._qianwen_pw = None
        self._qianwen_browser = None
        self._qianwen_page = None
        self._qianwen_lock = asyncio.Lock()
        self._qianwen_queues = {}
        self._qianwen_user_data_dir = os.path.join(BASE_DIR, "qianwen_profile")

        # DeepSeek 专属
        self._deepseek_pw = None
        self._deepseek_browser = None
        self._deepseek_page = None
        self._deepseek_lock = asyncio.Lock()
        self._deepseek_queues = {}
        self._deepseek_user_data_dir = os.path.join(BASE_DIR, "deepseek_profile")

        # Zai 专属
        self._zai_pw = None
        self._zai_browser = None
        self._zai_page = None
        self._zai_lock = asyncio.Lock()
        self._zai_queues = {}
        self._zai_user_data_dir = os.path.join(BASE_DIR, "zai_profile")

        # Mimo 专属
        self._mimo_pw = None
        self._mimo_browser = None
        self._mimo_page = None
        self._mimo_lock = asyncio.Lock()
        self._mimo_queues = {}
        self._mimo_user_data_dir = os.path.join(BASE_DIR, "mimo_profile")

        self._minimax_pw = None
        self._minimax_browser = None
        self._minimax_page = None
        self._minimax_lock = asyncio.Lock()
        self._minimax_user_data_dir = os.path.join(BASE_DIR, "minimax_profile")

        self._xinghuo_pw = None
        self._xinghuo_browser = None
        self._xinghuo_page = None
        self._xinghuo_lock = asyncio.Lock()
        self._xinghuo_user_data_dir = os.path.join(BASE_DIR, "spark_user_data")

    def _on_doubao_push(self, stream_id: str, kind: str, value):
        q = self._doubao_queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    # ═══════════════════════════════════════════════════════════════════════
    # wsession 对话激活方法
    # ═══════════════════════════════════════════════════════════════════════

    async def activate_doubao_conversation(self, conversation_id: str) -> bool:
        """导航到豆包指定对话页面，激活该对话实例。"""
        if not conversation_id or conversation_id == "0":
            return False
        try:
            await self.ensure_doubao_ready()
            url = f"https://www.doubao.com/chat/{conversation_id}"
            await self._doubao_page.goto(url, wait_until="load", timeout=60000)
            logger.info(f"[Doubao] after nav to {url}, actual URL: {self._doubao_page.url}")
            for _ in range(360):  # 最长等待3分钟
                found = await self._doubao_page.evaluate("""() => {
                    const ta = document.querySelector('textarea');
                    const ce = document.querySelector('[contenteditable="true"]');
                    return !!(ta || ce);
                }""")
                if found:
                    break
                await asyncio.sleep(0.5)
            logger.info(f"[Doubao] activated conversation {conversation_id}")
            return True
        except Exception as e:
            logger.warning(f"[Doubao] activate conversation failed: {e}")
            return False

    async def activate_qianwen_conversation(self, session_id: str) -> bool:
        """导航到千问指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_qianwen_ready()
            url = f"https://www.qianwen.com/chat/{session_id}"
            await self._qianwen_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # 等待编辑器就绪（React 组件渲染完成）
            editor_found = False
            for _ in range(60):
                try:
                    has_editor = await self._qianwen_page.evaluate("""() => {
                        const ed = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                        return !!ed;
                    }""")
                    if has_editor:
                        editor_found = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if not editor_found:
                logger.warning(f"[Qianwen] editor not found after activation (session={session_id})")
                return False
            logger.info(f"[Qianwen] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[Qianwen] activate conversation failed: {e}")
            return False

    async def activate_deepseek_conversation(self, session_id: str) -> bool:
        """导航到 DeepSeek 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_deepseek_ready()
            url = f"https://chat.deepseek.com/a/chat/s/{session_id}"
            await self._deepseek_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[DeepSeek] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[DeepSeek] activate conversation failed: {e}")
            return False

    async def activate_zai_conversation(self, session_id: str) -> bool:
        """导航到 z.ai 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_zai_ready()
            url = f"https://chat.z.ai/c/{session_id}"
            await self._zai_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[Zai] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[Zai] activate conversation failed: {e}")
            return False

    async def activate_mimo_conversation(self, session_id: str) -> bool:
        """导航到 MiMo 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_mimo_ready()
            # MiMo SPA hash router: after sending, URL contains '/chat/{session_id}'
            url = f"https://aistudio.xiaomimimo.com/#/chat/{session_id}"
            await self._mimo_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[MiMo] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[MiMo] activate conversation failed: {e}")
            return False

    async def activate_minimax_conversation(self, session_id: str) -> bool:
        """导航到 MiniMax 指定对话页面。"""
        if not session_id:
            return False
        try:
            await self.ensure_minimax_ready()
            url = f"https://agent.minimaxi.com/mavis/{session_id}"
            await self._minimax_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[MiniMax] activated session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[MiniMax] activate conversation failed: {e}")
            return False

    async def activate_xinghuo_conversation(self, chat_id: str) -> bool:
        """导航到讯飞星火指定对话页面。URL 格式：https://xinghuo.xfyun.cn/desk?chatId=xxx&botId=4255"""
        if not chat_id:
            return False
        try:
            await self.ensure_xinghuo_ready()
            # Xinghuo 使用 desk 页面并带 chatId 参数，botId 固定为 4255（星火 4.0 Ultra）
            url = f"https://xinghuo.xfyun.cn/desk?chatId={chat_id}&botId=4255"
            await self._xinghuo_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            logger.info(f"[Xinghuo] activated chat {chat_id}")
            return True
        except Exception as e:
            logger.warning(f"[Xinghuo] activate conversation failed: {e}")
            return False

    def _on_qianwen_push(self, stream_id: str, kind: str, value):
        q = self._qianwen_queues.get(stream_id)
        if q is None:
            return
        q.put_nowait((kind, value))

    async def ensure_doubao_ready(self, headless=True):
        """确保 Doubao 浏览器就绪，使用持久化 user_data_dir 保留登录状态。"""
        # 检测浏览器是否已关闭（用户手动关闭后自动重建）
        page_closed = self._doubao_page is None or (hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed())
        context_closed = self._doubao_browser is None or (hasattr(self._doubao_browser, 'is_connected') and not self._doubao_browser.is_connected())
        if not page_closed and not context_closed and self._doubao_browser and self._doubao_browser.pages:
            return True
        # 任一页面或上下文关闭都重建
        if page_closed or context_closed:
            logger.info("[Doubao] browser or page closed by user, rebuilding...")
            self._doubao_page = None
            self._doubao_browser = None
            self._doubao_pw = None
        async with self._doubao_lock:
            # 二次检查
            page_closed = self._doubao_page is None or (hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed())
            context_closed = self._doubao_browser is None or (hasattr(self._doubao_browser, 'is_connected') and not self._doubao_browser.is_connected())
            if not page_closed and not context_closed and self._doubao_browser and self._doubao_browser.pages:
                await self._refresh_profile_params()
                return True

            if not os.path.exists(self._doubao_user_data_dir):
                os.makedirs(self._doubao_user_data_dir, exist_ok=True)

            _last_exc = None
            for _rebuild_attempt in range(3):
                try:
                    from playwright.async_api import async_playwright
                    self._doubao_pw = await async_playwright().start()
                    self._doubao_browser = await self._doubao_pw.chromium.launch_persistent_context(
                        user_data_dir=self._doubao_user_data_dir,
                        headless=headless,
                        channel=_browser_channel(),
                        args=_linux_safe_args() + ["--disable-blink-features=AutomationControlled"],
                        ignore_default_args=["--enable-automation"],
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                    )
                    self._doubao_page = self._doubao_browser.pages[0] if self._doubao_browser.pages else await self._doubao_browser.new_page()

                    await self._doubao_page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        window.chrome = { runtime: {} };
                        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    """)

                    await self._doubao_page.expose_function("__sse_push", self._on_doubao_push)

                    logger.info("Doubao: navigating to doubao.com/chat/ ...")
                    try:
                        await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        logger.warning("[Doubao] initial goto timed out, trying reload...")
                        try:
                            await self._doubao_page.reload(wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                    await asyncio.sleep(2)

                    try:
                        err_buttons = await self._doubao_page.evaluate("""() => {
                            const btns = document.querySelectorAll('button');
                            return Array.from(btns).map(b => (b.textContent || '').trim()).filter(t => t);
                        }""")
                        if len(err_buttons) >= 1 and err_buttons[0] == '刷新页面':
                            logger.info("[Doubao] error page at init, clicking '刷新页面'...")
                            await self._doubao_page.evaluate("document.querySelector('button').click()")
                            await asyncio.sleep(5)
                    except Exception as e:
                        logger.debug(f"[Doubao] error page check failed: {e}")

                    try:
                        await self._doubao_page.wait_for_function(
                            "() => typeof window.bdms?.frontierSign === 'function'",
                            timeout=30000
                        )
                        logger.info("Doubao: bdms.frontierSign SDK ready")
                    except Exception as e:
                        logger.warning(f"Doubao: bdms.frontierSign not available: {e}")

                    has_chat_ui = await self._doubao_page.evaluate("""() => {
                        const hasInput = !!document.querySelector('textarea') || !!document.querySelector('[contenteditable=true]');
                        const hasSend = !!document.getElementById('flow-end-msg-send') ||
                                        !!document.querySelector('button[data-testid*="send"]') ||
                                        !!document.querySelector('button[class*="send"]');
                        const bodyText = document.body.innerText || '';
                        const hasLoginPrompt = bodyText.includes('请先登录') || bodyText.includes('扫码登录');
                        const modal = document.querySelector('.semi-modal-wrap, .semi-modal-wrap-center, .semi-portal');
                        const hasBlockingModal = !!modal;
                        const modalText = modal ? (modal.textContent || '').trim().substring(0, 100) : '';
                        return { hasInput, hasSend, hasLoginPrompt, hasBlockingModal, modalText };
                    }""") or {}
                    if has_chat_ui.get('hasBlockingModal') or (has_chat_ui.get('hasLoginPrompt') and not has_chat_ui.get('hasSend')):
                        logger.warning(f"Doubao: login/modal issue detected. hasBlockingModal={has_chat_ui.get('hasBlockingModal')}, modalText='{has_chat_ui.get('modalText')}'")
                        await self._doubao_login_recovery()

                    logger.info("Doubao browser ready")
                    await self._refresh_profile_params()
                    return True
                except Exception as _rebuild_exc:
                    _last_exc = _rebuild_exc
                    logger.warning(f"[Doubao] rebuild attempt {_rebuild_attempt+1}/3 failed: {_rebuild_exc}")
                    for _attr in ('_doubao_page', '_doubao_browser', '_doubao_pw'):
                        _obj = getattr(self, _attr, None)
                        if _obj:
                            try:
                                if _attr == '_doubao_pw':
                                    await _obj.stop()
                                else:
                                    await _obj.close()
                            except Exception:
                                pass
                            setattr(self, _attr, None)
                    await asyncio.sleep(2)
            if _last_exc:
                raise _last_exc

    async def _refresh_profile_params(self):
        """Refresh profile parameters from the current page's localStorage."""
        if not self._doubao_page or self._doubao_page.is_closed():
            return
        try:
            params = await self._doubao_page.evaluate("""() => {
                const get = (k) => {
                    try { return localStorage.getItem(k) || ''; } catch (e) { return ''; }
                };
                return {
                    device_id: get('device_id') || (window.deviceId || ''),
                    web_id: get('web_id') || (window.webId || ''),
                    tea_uuid: get('tea_uuid') || (window.teaUuid || ''),
                    fp: get('fp') || (window.fp || '')
                };
            }""")
            if params.get('device_id') and params.get('web_id') and params.get('tea_uuid'):
                self._profile_params.update(params)
            else:
                logger.warning(f"[Doubao] Incomplete profile params: {params}")
        except Exception as e:
            logger.warning(f"[Doubao] _refresh_profile_params error: {e}")

    async def _ensure_and_refresh(self):
        await self.ensure_doubao_ready(headless=True)
        await self._refresh_profile_params()

    def get_profile_params(self) -> dict:
        """Synchronously get profile parameters, ensuring browser is ready."""
        if self._profile_params:
            return dict(self._profile_params)
        try:
            loop = asyncio.get_running_loop()
            if loop and not loop.is_closed():
                # Schedule the coroutine on the running loop from this thread
                future = asyncio.run_coroutine_threadsafe(self._ensure_and_refresh(), loop)
                future.result(timeout=120)
            else:
                asyncio.run(self._ensure_and_refresh())
        except Exception as e:
            logger.warning(f"[Doubao] get_profile_params error: {e}")
        return dict(self._profile_params)

    async def _doubao_login_recovery(self):
        """打开可见浏览器让用户手动登录 Doubao，使用 user_data_dir 持久化状态。"""
        from playwright.async_api import async_playwright
        try:
            # 关闭当前 headless 实例
            if self._doubao_page:
                try:
                    await self._doubao_page.close()
                except Exception:
                    pass
                self._doubao_page = None
            if self._doubao_browser:
                try:
                    await self._doubao_browser.close()
                except Exception:
                    pass
                self._doubao_browser = None
            if self._doubao_pw:
                try:
                    await self._doubao_pw.stop()
                except Exception:
                    pass
                self._doubao_pw = None

            pw = await async_playwright().start()
            login_browser = await pw.chromium.launch_persistent_context(
                user_data_dir=self._doubao_user_data_dir,
                headless=False,
                channel=_browser_channel(),
                args=_linux_safe_args() + ["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            login_page = login_browser.pages[0] if login_browser.pages else await login_browser.new_page()
            await login_page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            await login_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
            logger.info("Doubao: visible browser opened for manual login. Please log in...")

            while True:
                await asyncio.sleep(1)
                if not login_browser.pages:
                    break
                try:
                    body = await login_page.text_content("body") or ""
                    if "登录" not in body and "请先登录" not in body:
                        logger.info("Doubao: login detected")
                        break
                except Exception:
                    pass

            await login_browser.close()
            await pw.stop()

            # 重新创建 headless 上下文（复用已保存的 user_data_dir）
            self._doubao_pw = await async_playwright().start()
            self._doubao_browser = await self._doubao_pw.chromium.launch_persistent_context(
                user_data_dir=self._doubao_user_data_dir,
                headless=True,
                channel=_browser_channel(),
                args=_linux_safe_args() + ["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._doubao_page = self._doubao_browser.pages[0] if self._doubao_browser.pages else await self._doubao_browser.new_page()
            await self._doubao_page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            await self._doubao_page.expose_function("__sse_push", self._on_doubao_push)
            await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=60000)
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Doubao login recovery failed: {e}")
            raise

    async def _download_images_via_contextmenu(self) -> list:
        """使用 page.request.get() 在浏览器上下文中下载图片，自动携带 Cookie 和正确 Headers。"""
        import uuid as _uuid
        local_urls = []
        images_dir = os.path.join(BASE_DIR, "images")
        os.makedirs(images_dir, exist_ok=True)
        try:
            # 获取页面上的图片（rc_gen_image）
            img_elements = await self._doubao_page.query_selector_all('img')
            if not img_elements:
                logger.warning("[Doubao] no img elements found for download")
                return []
            targets = []
            for img_el in img_elements:
                try:
                    src = await img_el.get_attribute('src') or ''
                    visible = await img_el.is_visible()
                    if not visible or not src or not src.startswith('http'):
                        continue
                    if 'rc_gen_image' in src and 'byteimg.com' in src:
                        targets.append(src)
                except Exception:
                    continue
            if not targets:
                logger.warning("[Doubao] no visible generated images found")
                return []
            # 去重
            seen_hashes = set()
            base_urls = []
            import re as _re
            for src in targets:
                m = _re.search(r'rc_gen_image/([a-f0-9]+)\.jpeg', src)
                if not m:
                    continue
                base_hash = m.group(1)
                if base_hash in seen_hashes:
                    continue
                seen_hashes.add(base_hash)
                base_urls.append((base_hash, src))
            # 并行下载（使用浏览器上下文）
            async def download_one(url: str, base_hash: str) -> str | None:
                try:
                    resp = await self._doubao_page.request.get(url)
                    if resp.status != 200:
                        logger.warning(f"[Doubao] page.request.get {base_hash}: HTTP {resp.status}")
                        return None
                    content = await resp.body()
                    if not content or len(content) < 1024:  # 至少 1KB
                        logger.warning(f"[Doubao] image {base_hash} too small: {len(content) if content else 0} bytes")
                        return None
                    # 确定扩展名
                    ct = resp.headers.get('content-type', '').lower()
                    ext = '.jpg' if 'jpeg' in ct or 'jpg' in ct else '.png' if 'png' in ct else '.webp'
                    filename = f"{base_hash[:8]}_{_uuid.uuid4().hex[:6]}{ext}"
                    filepath = os.path.join(images_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    local_url = f"{CONFIG.get('img_url', 'http://localhost:8765/images')}/{filename}"
                    logger.info(f"[Doubao] downloaded via page.request: {local_url} ({len(content)} bytes)")
                    return local_url
                except Exception as e:
                    logger.warning(f"[Doubao] page.request error for {base_hash}: {e}")
                    return None
            import asyncio as _asyncio
            tasks = [download_one(url, h) for h, url in base_urls]
            results = await _asyncio.gather(*tasks)
            for r in results:
                if r:
                    local_urls.append(r)
        except Exception as e:
            logger.warning(f"[Doubao] _download_images_via_contextmenu error: {e}")
        return local_urls

    async def download_images_from_urls(self, urls: list, n: int = 1) -> list:
        """从 URL 列表下载图片到本地。
        对于 rc_gen_image URL，优先通过浏览器上下文获取真实的“下载原图”签名链接；
        对于 ocean-cloud-tos/image_generation URL，使用 httpx 直接下载。
        """
        import uuid as _uuid
        import httpx as _httpx
        import hashlib

        local_urls = []
        images_dir = os.path.join(BASE_DIR, "images")
        os.makedirs(images_dir, exist_ok=True)

        logger.info(f"[Doubao] download_images_from_urls called with {len(urls)} URLs")

        seen_hashes = set()
        unique_urls = []
        for url in urls:
            m_rc = re.search(r'rc_gen_image/([a-f0-9]+)\.jpeg', url)
            if m_rc:
                uid = f"rc:{m_rc.group(1)}"
            else:
                m_oc = re.search(r'image_generation/([a-f0-9]+)_(\d+)', url)
                if m_oc:
                    uid = f"oc:{m_oc.group(1)}"
                else:
                    uid = f"hash:{hashlib.md5(url.encode()).hexdigest()[:8]}"

            if uid not in seen_hashes:
                seen_hashes.add(uid)
                unique_urls.append((uid, url))

        logger.info(f"[Doubao] download: {len(unique_urls)} unique images, requesting {n}")
        selected = unique_urls[:n]

        page = self._doubao_page
        if page:
            for uid, preview_url in selected:
                if not uid.startswith("rc:"):
                    continue
                try:
                    fullsize_url = await self._get_fullsize_download_url_via_browser(page, uid, preview_url)
                    target_url = fullsize_url or preview_url
                    if fullsize_url:
                        logger.info(f"[Doubao] got full-size signed URL for {uid}: {target_url[:120]}...")
                    else:
                        logger.warning(f"[Doubao] no full-size signed URL for {uid}, using preview URL")
                    resp = await page.request.get(target_url)
                    content = await resp.body()
                    logger.info(f"[Doubao] browser download {uid}: HTTP {resp.status}, {len(content)} bytes, ct={resp.headers.get('content-type','')}")
                    if resp.status == 200 and len(content) > 1024:
                        ct = resp.headers.get('content-type', '').lower()
                        ext = '.jpg' if 'jpeg' in ct else '.png' if 'png' in ct else '.webp'
                        m_rc2 = re.search(r'rc_gen_image/([a-f0-9]+)', target_url)
                        base_hash = m_rc2.group(1) if m_rc2 else uid.split(":")[1]
                        filename = f"{base_hash[:8]}_{_uuid.uuid4().hex[:6]}{ext}"
                        filepath = os.path.join(images_dir, filename)
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        local_url = f"{CONFIG.get('img_url', 'http://localhost:8765/images')}/{filename}"
                        local_urls.append(local_url)
                        logger.info(f"[Doubao] saved: {local_url} ({len(content)} bytes)")
                    elif resp.status == 200:
                        logger.warning(f"[Doubao] image too small for {uid}: {len(content)} bytes")
                    else:
                        logger.warning(f"[Doubao] browser download failed for {uid}: HTTP {resp.status}")
                except Exception as e:
                    logger.warning(f"[Doubao] browser download error for {uid}: {e}")

        async with _httpx.AsyncClient(timeout=60, follow_redirects=True, headers={
            'accept': '*/*',
            'origin': 'https://www.doubao.com',
            'referer': 'https://www.doubao.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0',
            'sec-fetch-site': 'cross-site',
            'sec-fetch-mode': 'cors',
            'sec-fetch-dest': 'empty',
        }) as client:
            for uid, target_url in selected:
                if uid.startswith("rc:"):
                    continue
                try:
                    logger.info(f"[Doubao] downloading {uid}: {target_url[:120]}...")
                    resp = await client.get(target_url)
                    logger.info(f"[Doubao] download {uid}: HTTP {resp.status_code}, {len(resp.content)} bytes, ct={resp.headers.get('content-type','')}")
                    if resp.status_code == 200 and len(resp.content) > 1024:
                        ct = resp.headers.get('content-type', '').lower()
                        ext = '.jpg' if 'jpeg' in ct else '.png' if 'png' in ct else '.webp'
                        if uid.startswith("oc:"):
                            m_oc = re.search(r'image_generation/([a-f0-9]+)', target_url)
                            base_hash = m_oc.group(1) if m_oc else uid.split(":")[1]
                        else:
                            base_hash = uid.split(":", 1)[1]
                        filename = f"{base_hash[:8]}_{_uuid.uuid4().hex[:6]}{ext}"
                        filepath = os.path.join(images_dir, filename)
                        with open(filepath, 'wb') as f:
                            f.write(resp.content)
                        local_url = f"{CONFIG.get('img_url', 'http://localhost:8765/images')}/{filename}"
                        local_urls.append(local_url)
                        logger.info(f"[Doubao] saved: {local_url} ({len(resp.content)} bytes)")
                    elif resp.status_code == 200:
                        logger.warning(f"[Doubao] image too small for {uid}: {len(resp.content)} bytes")
                    else:
                        logger.warning(f"[Doubao] download failed for {uid}: HTTP {resp.status_code}")
                except Exception as e:
                    logger.warning(f"[Doubao] download error for {uid}: {e}")

        logger.info(f"[Doubao] download completed: {len(local_urls)}/{n} success")
        return local_urls

    async def _get_fullsize_download_url_via_browser(self, page, uid: str, preview_url: str) -> str | None:
        """右键图片打开上下文菜单，点击"下载原图"菜单项，拦截真实签名请求。"""
        import asyncio as _asyncio

        if not page or page.is_closed():
            return None

        base_hash = uid.split(":", 1)[1]
        captured_urls = []

        async def _capture(route):
            req_url = route.request.url
            if 'rc_gen_image' in req_url and 'image_dld' in req_url:
                captured_urls.append(req_url)
                logger.info(f"[Doubao] captured full-size request for {uid}: {req_url[:150]}")
                await route.abort()
                return
            await route.continue_()

        try:
            await page.route("**/*", _capture)
        except Exception as e:
            logger.warning(f"[Doubao] route setup failed for {uid}: {e}")
            return None

        try:
            # Step 1: Find the image and right-click it to open context menu
            img_info = await page.evaluate("""(baseHash) => {
                const imgs = Array.from(document.querySelectorAll('img[src*="rc_gen_image"]'));
                const img = imgs.find(i => (i.src || '').includes(baseHash));
                if (!img) return { found: false };
                const rect = img.getBoundingClientRect();
                return { found: true, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
            }""", base_hash)
            logger.info(f"[Doubao] img location for {uid}: {img_info}")

            if not img_info or not img_info.get('found'):
                return None

            # Right-click on the image to open context menu
            await page.mouse.click(img_info['x'], img_info['y'], button='right')
            await _asyncio.sleep(0.5)

            # Step 2: Click "下载原图" context menu item
            clicked = await page.evaluate("""() => {
                const menuItems = document.querySelectorAll('[class*="context-menu-item"]');
                for (const item of menuItems) {
                    const txt = (item.textContent || '').trim();
                    if (txt.includes('下载原图')) {
                        item.click();
                        return { clicked: true, text: txt, className: (item.className || '').substring(0, 80) };
                    }
                }
                return { clicked: false, reason: 'context menu item not found' };
            }""")
            logger.info(f"[Doubao] 下载原图 click result for {uid}: {clicked}")

            if not clicked or not clicked.get('clicked'):
                # Fallback: press Escape to close context menu, try a different approach
                await page.keyboard.press('Escape')
                await _asyncio.sleep(0.3)
                return None

            # Step 3: Wait for the intercepted full-size URL
            for _ in range(20):
                if captured_urls:
                    return captured_urls[0]
                await _asyncio.sleep(0.5)
            return None
        except Exception as e:
            logger.warning(f"[Doubao] capture full-size url failed for {uid}: {e}")
            return None
        finally:
            try:
                await page.unroute("**/*", _capture)
            except Exception:
                pass
    async def ensure_qianwen_ready(self, headless=True):
        """确保 Qianwen 浏览器就绪，使用持久化 user_data_dir 保留登录状态。"""
        logger.info(f"[Qwen] ensure_qianwen_ready entry: headless={headless}")
        page_closed = self._qianwen_page is None or (hasattr(self._qianwen_page, 'is_closed') and self._qianwen_page.is_closed())
        context_closed = self._qianwen_browser is None or (hasattr(self._qianwen_browser, 'is_connected') and not self._qianwen_browser.is_connected())
        if not page_closed and not context_closed and self._qianwen_browser and self._qianwen_browser.pages:
            logger.info("[Qwen] browser/page already ready, skipping")
            return True
        if page_closed or context_closed:
            logger.info("[Qwen] browser or page closed/crashed, rebuilding...")
            self._qianwen_page = None
            self._qianwen_browser = None
            self._qianwen_pw = None
        async with self._qianwen_lock:
            page_closed = self._qianwen_page is None or (hasattr(self._qianwen_page, 'is_closed') and self._qianwen_page.is_closed())
            context_closed = self._qianwen_browser is None or (hasattr(self._qianwen_browser, 'is_connected') and not self._qianwen_browser.is_connected())
            if not page_closed and not context_closed and self._qianwen_browser and self._qianwen_browser.pages:
                return True

            if not os.path.exists(self._qianwen_user_data_dir):
                os.makedirs(self._qianwen_user_data_dir, exist_ok=True)

            _qwen_last_exc = None
            for _qwen_attempt in range(3):
                try:
                    from playwright.async_api import async_playwright
                    self._qianwen_pw = await async_playwright().start()
                    logger.info(f"[Qwen] launching persistent context: headless={headless}, channel=chromium, user_data_dir={self._qianwen_user_data_dir}")
                    self._qianwen_browser = await self._qianwen_pw.chromium.launch_persistent_context(
                        user_data_dir=self._qianwen_user_data_dir,
                        headless=headless,
                        channel="chromium",
                        args=_linux_safe_args(),
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                    )
                    logger.info(f"[Qwen] browser launched, pages: {len(self._qianwen_browser.pages)}")
                    self._qianwen_page = self._qianwen_browser.pages[0] if self._qianwen_browser.pages else await self._qianwen_browser.new_page()
                    await self._qianwen_page.expose_function("__sse_push", self._on_qianwen_push)
                    logger.info("Qianwen: navigating to qianwen.com ...")
                    await self._qianwen_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
                    await asyncio.sleep(3)

                    body_text = await self._qianwen_page.text_content("body") or ""
                    logger.info(f"[Qianwen] body_text[:300]: {body_text[:300]}")
                    if any(kw in body_text for kw in ["扫码登录", "手机号登录", "账号登录", "登录/注册"]):
                        logger.warning("Qianwen: login required - session expired. Opening visible browser...")
                        await self._qianwen_login_recovery()
                    else:
                        logger.info("Qianwen page ready")
                    return True
                except Exception as _qwen_exc:
                    _qwen_last_exc = _qwen_exc
                    logger.warning(f"[Qianwen] rebuild attempt {_qwen_attempt+1}/3 failed: {_qwen_exc}")
                    for _attr in ('_qianwen_page', '_qianwen_browser', '_qianwen_pw'):
                        _obj = getattr(self, _attr, None)
                        if _obj:
                            try:
                                if _attr == '_qianwen_pw':
                                    await _obj.stop()
                                else:
                                    await _obj.close()
                            except Exception:
                                pass
                            setattr(self, _attr, None)
                    await asyncio.sleep(2)
            if _qwen_last_exc:
                raise _qwen_last_exc

    async def _qianwen_login_recovery(self):
        """千问登录恢复：显示浏览器让用户手动登录，登录后重建 headless 实例。"""
        from playwright.async_api import async_playwright
        try:
            # 关闭当前 headless 实例
            if self._qianwen_page:
                try:
                    await self._qianwen_page.close()
                except Exception:
                    pass
                self._qianwen_page = None
            if self._qianwen_browser:
                try:
                    await self._qianwen_browser.close()
                except Exception:
                    pass
                self._qianwen_browser = None
            if self._qianwen_pw:
                try:
                    await self._qianwen_pw.stop()
                except Exception:
                    pass
                self._qianwen_pw = None

            pw = await async_playwright().start()
            logger.info(f"[Qwen] launching login context: headless=False, channel=chromium, user_data_dir={self._qianwen_user_data_dir}")
            login_browser = await pw.chromium.launch_persistent_context(
                user_data_dir=self._qianwen_user_data_dir,
                headless=False,
                channel="chromium",
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            logger.info(f"[Qwen] login browser launched, pages: {len(login_browser.pages)}")
            login_page = login_browser.pages[0] if login_browser.pages else await login_browser.new_page()
            await login_page.expose_function("__sse_push", self._on_qianwen_push)
            await login_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
            logger.info("Qianwen: visible browser opened for manual login. Please log in...")

            while True:
                await asyncio.sleep(1)
                if not login_browser.pages:
                    break
                try:
                    body = await login_page.text_content("body") or ""
                    if not any(kw in body for kw in ["扫码登录", "手机号登录", "账号登录", "登录/注册"]):
                        logger.info("Qianwen: login detected")
                        break
                except Exception:
                    pass

            await login_browser.close()
            await pw.stop()

            # 重新创建 headless 上下文
            self._qianwen_pw = await async_playwright().start()
            logger.info(f"[Qwen] launching headless context after login: headless=True, channel=chromium, user_data_dir={self._qianwen_user_data_dir}")
            self._qianwen_browser = await self._qianwen_pw.chromium.launch_persistent_context(
                user_data_dir=self._qianwen_user_data_dir,
                headless=True,
                channel="chromium",
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            logger.info(f"[Qwen] headless browser launched after login, pages: {len(self._qianwen_browser.pages)}")
            self._qianwen_page = self._qianwen_browser.pages[0] if self._qianwen_browser.pages else await self._qianwen_browser.new_page()
            await self._qianwen_page.expose_function("__sse_push", self._on_qianwen_push)
            await self._qianwen_page.goto("https://www.qianwen.com/", wait_until="load", timeout=60000)
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Qianwen login recovery failed: {e}")
            raise

    async def stream_qianwen_chat(self, messages: list, session_id: str, topic_id: str):
        """Route interception for qianwen API response + DOM typing."""
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._qianwen_queues[stream_id] = q

        user_text = messages[0].get("content", "") if messages else ""
        logger.info(f"{len(user_text)} chars")

        async def handle_route(route):
            if 'chat2.qianwen.com' not in route.request.url or 'api/v2/chat' not in route.request.url:
                await route.continue_()
                return
            try:
                resp = await route.fetch(timeout=120000)
                body = await resp.body()
                text = body.decode("utf-8", errors="replace")
                logger.info(f"[Qwen] API: {len(text)} bytes")
                last = ""
                count = 0
                for line in text.split("\n"):
                    if line.startswith("data:"):
                        ds = line[5:].strip()
                        if ds and ds != "[DONE]":
                            try:
                                ev = json.loads(ds)
                                if isinstance(ev, dict):
                                    for m in ev.get("data", {}).get("messages", []):
                                        if m.get("mime_type") == "multi_load/iframe":
                                            c = m.get("content", "")
                                            if c and c != last:
                                                delta = c[len(last):]
                                                last = c
                                                if delta:
                                                    count += 1
                                                    self._qianwen_queues[stream_id].put_nowait(("chunk", delta))
                            except json.JSONDecodeError:
                                pass
                logger.info(f"[Qwen] parsed {count} chunks")
                q.put_nowait(("done", ""))
                await route.fulfill(response=resp)
            except Exception as e:
                logger.warning(f"[Qwen] route err: {e}")
                q.put_nowait(("error", str(e)))
                q.put_nowait(("done", ""))
                await route.continue_()

        await self._qianwen_page.route("**/api/v2/chat**", handle_route)

        try:
            # 定位编辑器所在的 frame（可能在 iframe 中）
            edit_frame = self._qianwen_page
            try:
                iframe_elements = await self._qianwen_page.query_selector_all("iframe")
                for iframe_el in iframe_elements:
                    try:
                        box = await iframe_el.bounding_box()
                        if box and box['width'] > 0 and box['height'] > 0:
                            candidate_frame = await iframe_el.content_frame()
                            if candidate_frame:
                                ed = await candidate_frame.query_selector("[contenteditable], textarea")
                                if ed:
                                    edit_frame = candidate_frame
                                    break
                    except Exception:
                        continue
            except Exception:
                pass

            ok = await edit_frame.evaluate("""() => {
                const ed = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                if (ed) { ed.focus(); ed.click(); return true; }
                return false;
            }""")
            if not ok:
                yield ("error", "墙角数枝梅，凌寒独自开。遥知不是雪，为有暗香来。")
                yield ("done", "")
                return
            import sys as _sys
            if _sys.platform == "darwin":
                _paste_mod = "Meta"
            else:
                _paste_mod = "Control"
            await self._qianwen_page.keyboard.press(f"{_paste_mod}+a")
            await self._qianwen_page.keyboard.press("Backspace")
            pasted = False
            try:
                if self._qianwen_browser:
                    try:
                        await self._qianwen_browser.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.qianwen.com")
                    except Exception:
                        pass
                await edit_frame.evaluate("""async (text) => {
                    await navigator.clipboard.writeText(text);
                }""", user_text)
                await self._qianwen_page.keyboard.press(f"{_paste_mod}+v")
                pasted = True
            except Exception as paste_e:
                logger.warning(f"[Qwen] clipboard paste failed, fallback insert_text: {paste_e}")
                await self._qianwen_page.keyboard.insert_text(user_text)
            await asyncio.sleep(0.3)
            await self._qianwen_page.keyboard.press("End")
            await self._qianwen_page.keyboard.type(" ")
            await self._qianwen_page.keyboard.press("Backspace")
            await asyncio.sleep(0.2)
            await self._qianwen_page.keyboard.press("Enter")
            logger.info(f"[Qwen] {'clipboard pasted' if pasted else 'insert_text'} + Enter")
        except Exception as e:
            yield ("error", f"Keyboard: {e}")
            yield ("done", "")
            return

        try:
            # Use longer timeout for file chunks
            while True:
                kind, value = await asyncio.wait_for(q.get(), timeout=120)
                if kind == "done":
                    yield ("done", "")
                    break
                if kind == "error":
                    yield ("error", value)
                    continue
                yield ("chunk", value)
        except asyncio.TimeoutError:
            logger.warning("[Qwen] timeout")
            yield ("error", "人间有味是清欢。")
            yield ("done", "")
        finally:
            self._qianwen_queues.pop(stream_id, None)
            try:
                if self._qianwen_page and not self._qianwen_page.is_closed():
                    await self._qianwen_page.unroute("**/api/v2/chat**", handle_route)
            except Exception:
                pass

    async def get_user_info(self) -> dict:
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        async with self._doubao_lock:
            user_info = {}
            got_data = asyncio.Event()

            async def on_response(response):
                url = response.url
                try:
                    if '/alice/profile/self' in url:
                        body = await response.json()
                        profile = body.get("data", {}).get("profile_brief", {})
                        if profile and profile.get("nickname"):
                            user_info["name"] = profile.get("nickname", "")
                            user_info["username"] = profile.get("user_name", "")
                            user_info["nick_name"] = profile.get("nickname", "")
                            user_info["user_id"] = str(profile.get("id", ""))
                            img_data = profile.get("image", {})
                            if isinstance(img_data, dict):
                                user_info["avatar_url"] = img_data.get("tiny_url", "")
                            got_data.set()
                    elif '/im/conversation/info' in url:
                        body = await response.json()
                        dl = body.get("downlink_body", {})
                        conv_body = dl.get("get_conv_info_downlink_body", {})
                        participants = conv_body.get("first_page_participant_list", [])
                        for p in participants:
                            if p.get("user_type") == 1:
                                user_info["name"] = user_info.get("name") or p.get("nick_name", "")
                                user_info["nick_name"] = user_info.get("nick_name") or p.get("nick_name", "")
                                avatar = p.get("avatar_url", {})
                                if isinstance(avatar, dict):
                                    user_info["avatar_url"] = user_info.get("avatar_url") or avatar.get("key", "")
                                got_data.set()
                except Exception:
                    pass

            self._doubao_page.on("response", on_response)
            try:
                await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                try:
                    await asyncio.wait_for(got_data.wait(), timeout=12)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(1)
            finally:
                self._doubao_page.remove_listener("response", on_response)
            return user_info

    async def stream_completion(self, body: dict):
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        self._doubao_queues[stream_id] = queue

        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex

        # 通过浏览器页面调用 Doubao SDK 签名生成带 a_bogus 的完整 URL
        js = """
        async (args) => {
            const { body, traceId, spanId, deviceId, webId, teaUuid, fp, streamId } = args;
            const baseUrl = "https://www.doubao.com/chat/completion";

            try {
                window.__sse_push(streamId, "chunk", "[DEBUG] step1: args received\\n");
            } catch(e) {}

            // 等待 bdms.frontierSign 可用
            let retries = 0;
            while (typeof window.bdms?.frontierSign !== 'function' && retries < 50) {
                await new Promise(r => setTimeout(r, 100));
                retries++;
            }
            if (typeof window.bdms?.frontierSign !== 'function') {
                window.__sse_push(streamId, "error", "bdms.frontierSign not available after 5s");
                window.__sse_push(streamId, "done", "");
                return;
            }

            try {
                window.__sse_push(streamId, "chunk", "[DEBUG] step2: frontierSign available\\n");
            } catch(e) {}
            
            const params = {
                aid: '497858',
                device_id: deviceId,
                device_platform: 'web',
                fp: fp || '',
                language: 'zh',
                pc_version: '3.22.0',
                pkg_type: 'release_version',
                real_aid: '497858',
                region: 'CN',
                samantha_web: '1',
                sys_region: 'CN',
                tea_uuid: teaUuid,
                'use-olympus-account': '1',
                version_code: '20800',
                web_id: webId,
                web_platform: 'browser',
                web_tab_id: crypto.randomUUID(),
            };
            
            // 获取 msToken
            const msToken = (document.cookie.match(/msToken=([^;]+)/) || [null, ''])[1];
            if (msToken) params.msToken = msToken;
            
            // 排序并编码参数
            const sortedKeys = Object.keys(params).sort();
            const queryParts = sortedKeys.map(k => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`);
            const queryString = queryParts.join('&');
            
            // 调用 frontierSign 生成 a_bogus
            let signedUrl;
            try {
                window.__sse_push(streamId, "chunk", "[DEBUG] step3: calling frontierSign\\n");
            } catch(e) {}
            try {
                const signResult = await window.bdms.frontierSign(queryString);
                window.__sse_push(streamId, "chunk", "[DEBUG] step4: frontierSign returned: " + JSON.stringify(signResult).slice(0,200) + "\\n");
                if (signResult && (signResult.a_bogus || signResult['X-Bogus'])) {
                    const bogusKey = signResult.a_bogus ? 'a_bogus' : 'X-Bogus';
                    const bogusVal = signResult.a_bogus || signResult['X-Bogus'];
                    signedUrl = `${baseUrl}?${queryString}&${bogusKey}=${encodeURIComponent(bogusVal)}`;
                } else {
                    signedUrl = `${baseUrl}?${queryString}`;
                }
            } catch (e) {
                window.__sse_push(streamId, "chunk", "[DEBUG] step4 FAIL: " + String(e) + "\\n");
                signedUrl = `${baseUrl}?${queryString}`;
            }
            
            window.__sse_push(streamId, "chunk", "[DEBUG] step5: url=" + signedUrl.slice(0, 200) + "...\\n");
            
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 30000);
                const resp = await fetch(signedUrl, {
                    method: "POST",
                    headers: {
                        "content-type": "application/json",
                        "agw-js-conv": "str",
                        "accept": "text/event-stream",
                        "x-flow-trace": JSON.stringify({trace_id: traceId, span_id: spanId}),
                    },
                    body: JSON.stringify(body),
                    signal: controller.signal,
                });
                clearTimeout(timeoutId);
                window.__sse_push(streamId, "chunk", "[DEBUG] step6: resp.status=" + resp.status + "\\n");
                if (!resp.ok) {
                    const t = await resp.text();
                    window.__sse_push(streamId, "error", "HTTP " + resp.status + ": " + t.slice(0, 500));
                    window.__sse_push(streamId, "done", "");
                    return;
                }
                const reader = resp.body.getReader();
                const decoder = new TextDecoder("utf-8");
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const text = decoder.decode(value, { stream: true });
                    window.__sse_push(streamId, "chunk", text);
                }
                window.__sse_push(streamId, "done", "");
            } catch (e) {
                window.__sse_push(streamId, "error", String(e));
                window.__sse_push(streamId, "done", "");
            }
        }
        """

        async with self._doubao_lock:
            profile = self.get_profile_params()
            eval_task = asyncio.create_task(
                self._doubao_page.evaluate(js, {
                    "body": body,
                    "traceId": trace_id,
                    "spanId": span_id,
                    "streamId": stream_id,
                    "deviceId": profile.get('device_id', ''),
                    "webId": profile.get('web_id', ''),
                    "teaUuid": profile.get('tea_uuid', ''),
                    "fp": profile.get('fp', ''),
                })
            )
            try:
                while True:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=180)
                    if kind == "done":
                        break
                    if kind == "error":
                        logger.error(f"Browser fetch error: {value}")
                        yield ("error", value)
                        continue
                    yield ("chunk", value)
            finally:
                self._doubao_queues.pop(stream_id, None)
                try:
                    await eval_task
                except Exception:
                    pass

    async def stream_doubao_chat_via_type(self, text: str, attachments: list | None = None, inline_file_content: str | None = None, image_generation: bool = False, timeout: int = 60, reuse_conversation: bool = False):
        """Route interception for doubao API response + DOM typing.
        attachments: 文档附件列表 (type=3)，注入 attachment_block + input_skill + chat_ability。
        inline_file_content: 如果提供，直接作为 text_block 内容注入（不上传云存储）。
        image_generation: 如果为 True，注入图像生成所需的 chat_ability 字段，并自动添加 "生成图片：" 前缀。
        """
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._doubao_queues[stream_id] = q
        _attachments = attachments or []

        # 闭包共享状态：跨多个 /chat/completion 请求传递图片生成状态
        _shared_image_found = False
        _shared_image_urls = set()
        _request_num = 0

        # 通用图片 URL 提取器：递归搜索 JSON 对象中所有类图片 URL
        def _extract_image_urls_from_json(obj):
            """从任意 JSON 对象中递归提取所有类图片 URL（http/https + 常见图片扩展名）"""
            urls = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    urls.update(_extract_image_urls_from_json(k))
                    urls.update(_extract_image_urls_from_json(v))
            elif isinstance(obj, list):
                for item in obj:
                    urls.update(_extract_image_urls_from_json(item))
            elif isinstance(obj, str):
                # 匹配 http/https URL 且以常见图片扩展名结尾，或包含 /image/ 路径
                matches = re.findall(
                    r'https?://[^\s"\'<>]+(?:\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&))',
                    obj, re.IGNORECASE
                )
                for m in matches:
                    urls.add(m)
                # 额外匹配：已知图片域名或路径特征（如 p16-tiktok、p9-doubao 等）
                matches2 = re.findall(
                    r'https?://[^\s"\'<>]*(?:p\d+-|image|img|pic|photo|media)[^\s"\'<>]*\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&)',
                    obj, re.IGNORECASE
                )
                for m in matches2:
                    urls.add(m)
            return urls

        # 清除所有旧的 route handler（防止旧 handler 拦截请求）
        try:
            await self._doubao_page.unroute("**/chat/completion**")
        except Exception:
            pass

        # ⚠️ route handler 定义，但不在此时注册
        # 页面导航（goto/reload）会自动清除 route，所以必须在导航完成后才注册
        async def handle_route(route):
            logger.info(f"[Doubao] handle_route: {route.request.method} {route.request.url[:150]}")
            if 'doubao.com/chat/completion' not in route.request.url:
                logger.info("[Doubao] URL not target, continuing normally")
                await route.continue_()
                return
            logger.info("[Doubao] Target URL intercepted, processing request")
            nonlocal _request_num, _shared_image_found, _shared_image_urls
            _image_found = False
            _request_num += 1
            logger.info(f"[Doubao] handle_route call #{_request_num}")
            try:
                modify_body = False
                body_dict = {}
                orig_body = route.request.post_data
                logger.info(f"[Doubao] Request method: {route.request.method}, has_body: {orig_body is not None}, content_len: {len(orig_body) if orig_body else 0}")
                if orig_body:
                    try:
                        body_dict = json.loads(orig_body)
                        # 如果是恢复消息，跳过处理，继续发送原始请求
                        recovery_info = body_dict.get('option', {}).get('recovery_option', {})
                        if recovery_info.get('is_recovery', False):
                            logger.info("[Doubao] intercepted recovery request, continuing original request without modification.")
                            await route.continue_()
                            return
                        logger.info(f"[Doubao] Request body keys: {list(body_dict.keys())}")
                        # Save request body for debugging
                        try:
                            debug_dir = os.path.join(os.path.dirname(__file__), "logs")
                            os.makedirs(debug_dir, exist_ok=True)
                            with open(os.path.join(debug_dir, f"intercepted_request_{_request_num}.json"), 'w', encoding='utf-8') as f:
                                json.dump(body_dict, f, ensure_ascii=False, indent=2)
                            logger.info(f"[Doubao] saved request body to logs/intercepted_request_{_request_num}.json")
                        except Exception:
                            pass
                        
                        messages = body_dict.get("messages", [])
                        if messages:
                            msg = messages[0]
                            cbs = msg.get("content_block", [])
                            logger.info(f"[Doubao] route handler: inline_file_content present: {inline_file_content is not None}, len: {len(inline_file_content) if inline_file_content else 0}")
                            logger.info(f"[Doubao] route handler: attachments present: {_attachments is not None}, len: {len(_attachments) if _attachments else 0}")
                            logger.info(f"[Doubao] route handler: image_generation: {image_generation}")
                            
                            # Inject attachment block if provided
                            if _attachments:
                                file_block = {
                                    "block_type": 10052,
                                    "content": {
                                        "attachment_block": {"attachments": _attachments},
                                        "pc_event_block": ""
                                    },
                                    "block_id": str(uuid.uuid4()),
                                    "parent_id": "", "meta_info": [], "append_fields": []
                                }
                                cbs.insert(0, file_block)
                                body_dict["chat_ability"] = {"ability_type": 16}
                                body_dict.setdefault("ext", {})["input_skill"] = '{"skill_id":"16","skill_type":16,"template_key":""}'
                                modify_body = True
                                msg["content_block"] = cbs
                            
                            # Inject inline file content as additional text_block
                            if inline_file_content:
                                file_text_block = {
                                    "block_type": 10000,
                                    "content": {
                                        "text_block": {"text": inline_file_content, "icon_url": "", "icon_url_dark": "", "summary": ""},
                                        "pc_event_block": ""
                                    },
                                    "block_id": str(uuid.uuid4()),
                                    "parent_id": "", "meta_info": [], "append_fields": []
                                }
                                cbs.append(file_text_block)
                                modify_body = True
                                msg["content_block"] = cbs
                        
                        # Inject chat_ability for image generation if not already present
                        if image_generation and 'chat_ability' not in body_dict:
                            body_dict["chat_ability"] = {
                                "ability_type": 3,
                                "ability_param": json.dumps({
                                    "ability_param": {"model": "Seedream 4.5"},
                                    "ability_type": 1
                                }, ensure_ascii=False)
                            }
                            modify_body = True
                            logger.info("[Doubao] injected chat_ability for image generation")
                        elif image_generation and 'chat_ability' in body_dict:
                            logger.info(f"[Doubao] chat_ability already present: {json.dumps(body_dict['chat_ability'], ensure_ascii=False)[:100]}")
                    except Exception as json_e:
                        logger.warning(f"[Doubao] Failed to parse request body: {json_e}")
                        body_dict = {}
                
                try:
                    if modify_body:
                        modified_body = json.dumps(body_dict, ensure_ascii=False)
                        logger.info(f"[Doubao] route.fetch with modified body (len={len(modified_body)}), request_num={_request_num}")
                        resp = await route.fetch(timeout=180000, post_data=modified_body)
                    else:
                        logger.info(f"[Doubao] route.fetch without modification, request_num={_request_num}")
                        resp = await route.fetch(timeout=180000)
                    logger.info(f"[Doubao] route.fetch completed, status={resp.status}")
                except Exception as fetch_e:
                    logger.warning(f"[Doubao] route.fetch failed or timed out: {fetch_e}")
                    q.put_nowait(("error", f"Fetch error: {fetch_e}"))
                    q.put_nowait(("done", ""))
                    await route.abort() # Abort the route to prevent further processing
                    return

                body = await resp.body()
                raw_text = body.decode("utf-8", errors="replace")
                logger.info(f"[Doubao] API: {len(raw_text)} bytes")
                try:
                    debug_dir = os.path.join(os.path.dirname(__file__), "logs")
                    os.makedirs(debug_dir, exist_ok=True)
                    with open(os.path.join(debug_dir, "doubao_api_response_debug.txt"), 'w', encoding='utf-8') as f:
                        f.write(raw_text)
                except Exception:
                    pass

                last_block_text = {}
                count = 0

                for block in raw_text.split("\n\n"):
                    block = block.strip()
                    if not block:
                        continue

                    event_type = ""
                    data_str = ""
                    for line in block.split("\n"):
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()

                    if not data_str:
                        continue

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if event_type == "SSE_ACK":
                        cid = data.get("ack_client_meta", {}).get("conversation_id", "")
                        if cid:
                            q.put_nowait(("conversation_id", cid))
                        continue

                    if event_type == "STREAM_ERROR":
                        msg = json.dumps(data, ensure_ascii=False)
                        if image_generation:
                            error_code = data.get("error_code")
                            error_msg_text = data.get("error_msg", "")
                            is_rate_limit = (error_code == 710022004 or
                                             "rate" in error_msg_text.lower() and "limit" in error_msg_text.lower())
                            if is_rate_limit:
                                logger.warning(f"[Doubao] STREAM_ERROR rate-limit in image gen mode, emitting error for retry: {msg[:200]}")
                                q.put_nowait(("error", msg))
                                q.put_nowait(("done", ""))
                                await route.fulfill(response=resp)
                                return
                            logger.warning(f"[Doubao] STREAM_ERROR in image gen mode (non-rate-limit, will wait for images): {msg[:200]}")
                            continue
                        q.put_nowait(("error", msg))
                        q.put_nowait(("done", ""))
                        await route.fulfill(response=resp)
                        return

                    if event_type == "CHUNK_DELTA":
                        delta = data.get("text", "")
                        if delta:
                            count += 1
                            q.put_nowait(("chunk", delta))
                            # 图片生成模式：从 CHUNK_DELTA 文本中提取 Markdown 图片链接
                            img_matches = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', delta)
                            for img_url in img_matches:
                                q.put_nowait(("image_url", img_url))
                                _image_found = True
                                _shared_image_found = True
                                _shared_image_urls.add(img_url)
                                logger.info(f"[Doubao] extracted image URL from CHUNK_DELTA: {img_url}")
                        continue

                    content_blocks = []
                    if event_type == "STREAM_MSG_NOTIFY":
                        content_blocks = data.get("content", {}).get("content_block", [])
                    elif event_type == "STREAM_CHUNK":
                        for op in data.get("patch_op", []):
                            pv = op.get("patch_value", {})
                            content_blocks.extend(pv.get("content_block", []))

                    # 通用提取：如果还没找到图片，搜索整个 JSON 结构
                    if image_generation and not _image_found:
                        for url in _extract_image_urls_from_json(data):
                            if url not in _shared_image_urls:
                                q.put_nowait(("image_url", url))
                                _image_found = True
                                _shared_image_found = True
                                _shared_image_urls.add(url)
                                logger.info(f"[Doubao] extracted image URL from JSON search: {url}")

                    for cb in content_blocks:
                        if cb.get("block_type") == 2074:
                            cb_content = cb.get("content", {})
                            creation_data = cb_content.get("creation_block", cb_content)
                            creations = creation_data.get("creations", [])
                            for creation in creations:
                                img_info = creation.get("image", {}) or creation
                                img_url = (img_info.get("image_raw", {}).get("url") or
                                           img_info.get("image_thumb", {}).get("url") or
                                           img_info.get("image_ori", {}).get("url"))
                                if img_url:
                                    q.put_nowait(("image_url", img_url))
                                    _image_found = True
                                    _shared_image_found = True
                                    _shared_image_urls.add(img_url)
                            continue
                        if cb.get("block_type") != 10000:
                            continue
                        block_id = cb.get("block_id", "") or "default"
                        text_block = cb.get("content", {}).get("text_block", {})
                        current = text_block.get("text", "")
                        if not current:
                            continue
                        previous = last_block_text.get(block_id, "")
                        delta = current[len(previous):] if current.startswith(previous) else current
                        last_block_text[block_id] = current
                        if delta:
                            count += 1
                            q.put_nowait(("chunk", delta))
                        if cb.get("is_finish"):
                            img_urls = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', current)
                            for img_url in img_urls:
                                q.put_nowait(("image_url", img_url))
                                _image_found = True
                                _shared_image_found = True
                                _shared_image_urls.add(img_url)
                                logger.info(f"[Doubao] extracted image URL from text block: {img_url}")

                    if event_type == "SSE_REPLY_END" and data.get("end_type") == 3:
                        if image_generation:
                            if _image_found or _request_num >= 2:
                                q.put_nowait(("image_urls_sse", list(_shared_image_urls)))
                                q.put_nowait(("done", ""))
                            # else: first request without images, wait for second without emitting done
                            await route.fulfill(response=resp)
                            return
                        q.put_nowait(("done", ""))
                        logger.info(f"[Doubao] parsed {count} chunks")
                        await route.fulfill(response=resp)
                        return

                logger.info(f"[Doubao] parsed {count} chunks (end of response), image_found={_image_found}, request_num={_request_num}")
                if not image_generation:
                    q.put_nowait(("done", ""))
                elif _image_found:
                    logger.info("[Doubao] image found at end of response (no SSE_REPLY_END end_type=3), emitting done")
                    q.put_nowait(("image_urls_sse", list(_shared_image_urls)))
                    q.put_nowait(("done", ""))
                elif _request_num >= 2:
                    logger.info("[Doubao] no image found in tool result (no SSE_REPLY_END), emitting done to close attempt")
                    q.put_nowait(("image_urls_sse", list(_shared_image_urls)))
                    q.put_nowait(("done", ""))
                try:
                    await route.fulfill(response=resp)
                except Exception as inner_e:
                    if "already handled" in str(inner_e).lower():
                        pass
                    else:
                        raise
            except Exception as e:
                if "already handled" in str(e).lower():
                    return
                logger.warning(f"[Doubao] route err: {e}")
                q.put_nowait(("error", str(e)))
                q.put_nowait(("done", ""))
                try:
                    await route.continue_()
                except Exception:
                    pass

        # 确保页面在新对话状态（而非旧对话），除非明确复用已有对话
        if not reuse_conversation:
            current_url = self._doubao_page.url
            if not current_url.endswith("/chat/") and "/chat/" in current_url:
                # 页面在旧对话中，导航到新对话
                logger.info("[Doubao] navigating to new chat (was in existing conversation)")
                try:
                    await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                except Exception as nav_err:
                    logger.warning(f"[Doubao] navigation to /chat/ failed (will retry): {nav_err}")
                    try:
                        await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                    except Exception as nav_err2:
                        logger.warning(f"[Doubao] second navigation attempt also failed: {nav_err2}")
                await asyncio.sleep(1)

        try:
            # 注册 route handler：拦截并注入 request 内容
            logger.info("[Doubao] registering route handler: **/chat/completion**")
            await self._doubao_page.route("**/chat/completion**", handle_route)
            logger.info("[Doubao] route handler registered, current URL: " + self._doubao_page.url)
            # 图像生成模式：在 /chat/ 页面点击"图像生成"按钮，再通过 contenteditable 输入提示词
            if image_generation:
                logger.info("[Doubao] switching to image generation mode on /chat/ page")
                # 确保在新对话页面
                current = self._doubao_page.url
                need_navigate = "/chat/" not in current or "/chat/create-image" in current or not current.rstrip('/').endswith('/chat')
                if need_navigate:
                    try:
                        await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        logger.warning("[Doubao] navigate to /chat/ timed out, force reloading...")
                        try:
                            await self._doubao_page.reload(wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                    await asyncio.sleep(2)

                 # 等待"图像生成"按钮出现（最多30秒）
                clicked_btn = False
                for _poll in range(30):
                    # 1) 检查错误页
                    page_buttons = await self._doubao_page.evaluate("""() => {
                        const btns = document.querySelectorAll('button');
                        return Array.from(btns).map(b => (b.textContent || '').trim()).filter(t => t);
                    }""")
                    # 检查页面是否需要刷新（错误页通常含"刷新页面"按钮）
                    has_refresh = any(t == '刷新页面' for t in page_buttons)
                    if has_refresh:
                        logger.info(f"[Doubao] page error (found '刷新页面'), attempting refresh cycle...")
                        # 点击第一个"刷新页面"按钮
                        await self._doubao_page.evaluate("""() => {
                            const btns = document.querySelectorAll('button');
                            for (const b of btns) {
                                if ((b.textContent || '').trim() === '刷新页面') {
                                    b.click();
                                    return true;
                                }
                            }
                            return false;
                        }""")
                        await asyncio.sleep(5)  # 等待刷新操作
                        continue

                    # 2) Playwright 原生点击（触发 React 合成事件）
                    try:
                        img_btn = self._doubao_page.locator('[data-skill-id="skill_bar_button_3"]')
                        if await img_btn.count() > 0:
                            await img_btn.click(force=True, timeout=3000)
                            clicked_btn = True
                            logger.info("[Doubao] clicked '图像生成' via Playwright native click")
                            break
                    except Exception as e:
                        logger.info(f"[Doubao] Playwright click failed: {e}")

                    # 未找到，等待后重试
                    await asyncio.sleep(1)
                else:
                    # 超时且未找到
                    try:
                        btns_dump = await self._doubao_page.evaluate("""() => {
                            const btns = document.querySelectorAll('button[data-component-type="skill-item"]');
                            return Array.from(btns).map(b => ({
                                text: (b.textContent || '').trim().substring(0, 30),
                                skillId: b.getAttribute('data-skill-id') || '',
                                                               skillType: b.getAttribute('data-skill-type') || '',
                                visible: b.offsetParent !== null,
                                disabled: b.disabled
                            }));
                        }""")
                        logger.warning(f"[Doubao] image gen button not found. skill-item dump: {json.dumps(btns_dump, ensure_ascii=False)}")
                    except Exception:
                        pass
                    yield ("error", "Cannot find '图像生成' button on /chat/ page")
                    yield ("done", "")
                    return

                # 等待 UI 稳定：点击按钮 + 关闭 modal 后，输入框可能延迟出现
                # 等 3 秒基础时间，再轮询 30 次（每次 1 秒）
                await asyncio.sleep(3)
                page_state = {}
                for _wait in range(30):
                    page_state = await self._doubao_page.evaluate("""() => {
                        const textarea = document.querySelector('textarea');
                        const contenteditable = document.querySelector('[contenteditable=true][role=textbox]');
                        const visibleTextarea = document.querySelector('textarea:not([aria-hidden="true"])');
                        const anyContenteditable = document.querySelector('[contenteditable="true"]');
                        const sendBtn = document.getElementById('flow-end-msg-send');
                        const sendBtnAny = document.querySelector('button[data-testid*="send"], button[class*="send"]');
                        const ariaHidden = textarea ? textarea.getAttribute('aria-hidden') : 'none';
                        return {
                            hasTextarea: !!textarea,
                            textareaVisible: textarea ? (textarea.offsetParent !== null && ariaHidden !== 'true') : false,
                            textareaAriaHidden: ariaHidden,
                            visibleTextareaExists: !!visibleTextarea,
                            hasContenteditable: !!contenteditable,
                            hasAnyContenteditable: !!anyContenteditable,
                            hasSendBtn: !!sendBtn,
                            sendBtnVisible: sendBtn ? (sendBtn.offsetParent !== null && !sendBtn.disabled) : false,
                            hasAnySendBtn: !!sendBtnAny,
                            url: location.href
                        };
                    }""")
                    if page_state.get('hasContenteditable') or page_state.get('visibleTextareaExists') or page_state.get('hasAnyContenteditable') or page_state.get('textareaVisible'):
                        break
                    await asyncio.sleep(1)
                logger.info(f"[Doubao] page state after click: {json.dumps(page_state, ensure_ascii=False)}")

                # 即使 textarea aria-hidden=true，也强制尝试输入（有时候它其实是可用的）
                send_text = text
                input_used = None
                send_result = None

                if page_state.get('hasContenteditable'):
                    input_used = "contenteditable"
                    logger.info(f"[Doubao] typing prompt into contenteditable: {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const el = document.querySelector('[contenteditable=true][role=textbox]');
                        if (!el) return;
                        el.focus();
                        el.innerHTML = '';
                        el.textContent = send_text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    send_result = await self._doubao_page.evaluate("""() => {
                        const btn = document.getElementById('flow-end-msg-send');
                        if (btn && !btn.disabled && btn.offsetParent !== null) {
                            btn.click();
                            return 'send-btn';
                        }
                        return '';
                    }""")

                elif page_state.get('visibleTextareaExists') or page_state.get('textareaVisible'):
                    input_used = "textarea(visible)"
                    logger.info(f"[Doubao] typing prompt into visible textarea: {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const ta = document.querySelector('textarea');
                        if (!ta) return;
                        ta.focus();
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        nativeSetter.call(ta, send_text);
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    send_result = await self._doubao_page.evaluate("""() => {
                        const btn = document.getElementById('flow-end-msg-send');
                        if (btn && !btn.disabled && btn.offsetParent !== null) {
                            btn.click();
                            return 'send-btn';
                        }
                        return '';
                    }""")

                elif page_state.get('hasAnyContenteditable'):
                    input_used = "contenteditable(any)"
                    logger.info(f"[Doubao] typing prompt into any contenteditable: {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const el = document.querySelector('[contenteditable="true"]');
                        if (!el) return;
                        el.focus();
                        el.innerText = send_text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    send_result = 'eval-fallback'

                elif page_state.get('hasTextarea'):
                    # textarea 存在但 aria-hidden=true，强制尝试
                    input_used = "textarea(force)"
                    logger.info(f"[Doubao] forcing textarea input (aria-hidden={page_state.get('textareaAriaHidden')}): {send_text[:60]}...")
                    await self._doubao_page.evaluate("""(send_text) => {
                        const ta = document.querySelector('textarea');
                        if (!ta) return;
                        ta.removeAttribute('aria-hidden');
                        ta.focus();
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        nativeSetter.call(ta, send_text);
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", send_text)
                    await asyncio.sleep(0.5)
                    # 尝试点击发送或按 Enter
                    send_result = await self._doubao_page.evaluate("""() => {
                        const btn = document.getElementById('flow-end-msg-send');
                        if (btn && !btn.disabled) {
                            btn.click();
                            return 'send-btn';
                        }
                        return '';
                    }""")

                if input_used:
                    logger.info(f"[Doubao] input method: {input_used}")
                    if send_result == 'send-btn':
                        logger.info("[Doubao] image gen: sent via send-btn")
                    else:
                        await self._doubao_page.keyboard.press("Enter")
                        logger.info("[Doubao] image gen: sent via Enter")
                else:
                    logger.warning("[Doubao] No viable input method found after image gen button click")
                    logger.warning(f"[Doubao] page_state: {json.dumps(page_state, ensure_ascii=False)}")
                    yield ("error", "No input found after clicking '图像生成' button")
                    yield ("done", "")
                    return
            else:
                # 复用对话时，页面在历史对话，需轮询等待输入框
                if reuse_conversation:
                    ta_found = False
                    for _poll in range(360):  # 最长等待3分钟
                        ta_found = await self._doubao_page.evaluate("""() => {
                            const ta = document.querySelector('textarea');
                            return !!ta;
                        }""")
                        if ta_found:
                            break
                        await asyncio.sleep(0.5)
                    if not ta_found:
                        logger.warning("[Doubao] reuse: textarea not found after polling")
                        yield ("error", "欲渡黄河冰塞川，将登太行雪满山。")
                        yield ("done", "")
                        return
                    logger.info("[Doubao] reuse: textarea found, typing prompt")
                    logger.info(f"[Doubao] before typing, current page URL: {self._doubao_page.url}")
                else:
                    # 新对话：等待 textarea 或 contenteditable 渲染完成
                    input_ready = False
                    for _poll in range(20):  # 最多 20 秒
                        input_ready = await self._doubao_page.evaluate("""() => {
                            const ta = document.querySelector('textarea');
                            const ce = document.querySelector('[contenteditable=true][role=textbox]');
                            return !!(ta || ce);
                        }""")
                        if input_ready:
                            break
                        await asyncio.sleep(1)
                    if not input_ready:
                        logger.warning("[Doubao] input (textarea/contenteditable) not ready after polling for new conversation")
                        yield ("error", "欲渡黄河冰塞川，将登太行雪满山。")
                        yield ("done", "")
                        return
                    logger.info("[Doubao] input element found for new conversation")

                # 原有逻辑：使用 textarea
                ok = await self._doubao_page.evaluate("""() => {
                    const ta = document.querySelector('textarea');
                    if (ta) {
                        ta.focus();
                        ta.click();
                        return true;
                    }
                    const ce = document.querySelector('[contenteditable=true][role=textbox]');
                    if (ce) {
                        ce.focus();
                        ce.click();
                        return true;
                    }
                    return false;
                }""")
                if not ok:
                    logger.warning("[Doubao] textarea focus failed")
                    yield ("error", "欲渡黄河冰塞川，将登太行雪满山。")
                    yield ("done", "")
                    return
                import sys as _sys
                _mod = "Meta" if _sys.platform == "darwin" else "Control"
                await self._doubao_page.keyboard.press(f"{_mod}+a")
                await self._doubao_page.keyboard.press("Backspace")
                pasted = False
                try:
                    if self._doubao_browser:
                        try:
                            await self._doubao_browser.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.doubao.com")
                        except Exception:
                            pass
                    await self._doubao_page.evaluate("""async (text) => {
                        await navigator.clipboard.writeText(text);
                    }""", text)
                    await self._doubao_page.keyboard.press(f"{_mod}+v")
                    pasted = True
                except Exception as paste_e:
                    logger.warning(f"[Doubao] clipboard paste failed, fallback insert_text: {paste_e}")
                    await self._doubao_page.keyboard.insert_text(text)
                await asyncio.sleep(0.3)
                await self._doubao_page.keyboard.press("End")
                await self._doubao_page.keyboard.type(" ")
                await self._doubao_page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                await self._doubao_page.keyboard.press("Enter")
                logger.info(f"[Doubao] {'clipboard pasted' if pasted else 'insert_text'} + Enter (initial send attempt)")

                # Verification and retry loop
                max_send_retries = 8
                for _verify_attempt in range(max_send_retries):
                    await asyncio.sleep(1) # Wait for UI to react after previous action

                    check_result = await self._doubao_page.evaluate("""() => {
                        const ta = document.querySelector('textarea');
                        const hasText = (ta && ta.value && ta.value.trim().length > 0) ||
                                        (() => {
                                            const ce = document.querySelector('[contenteditable=true][role=textbox]');
                                            return ce && ce.innerText && ce.innerText.trim().length > 0;
                                        })();

                        const getSendBtn = () => {
                            const btn = document.getElementById('flow-end-msg-send') ||
                                       document.querySelector('button[data-testid*="send"]') ||
                                       document.querySelector('button[class*="send"]');
                            return btn;
                        };
                        const btn = getSendBtn();
                        let sendEnabled = false;
                        if (btn) {
                            const isDisabled = btn.disabled === true ||
                                               btn.getAttribute('aria-disabled') === 'true' ||
                                               btn.classList.contains('disabled') ||
                                               btn.getAttribute('disabled') !== null;
                            sendEnabled = !isDisabled;
                        }
                        return { hasText, sendEnabled };
                    }""") or {}

                    still_has_text = bool(check_result.get('hasText'))
                    send_enabled = bool(check_result.get('sendEnabled'))

                    if not still_has_text:
                        logger.info("[Doubao] Input cleared, message sent successfully.")
                        break # Success, input cleared

                    if still_has_text and send_enabled:
                        logger.warning(f"[Doubao] Input still has text (attempt {_verify_attempt+1}/{max_send_retries}), send button is enabled. Retrying via Playwright click.")
                        try:
                            # Use Playwright's click method which is more robust
                            await self._doubao_page.click('#flow-end-msg-send, button[data-testid*="send"], button[class*="send"]', timeout=5000)
                            logger.info("[Doubao] Playwright click on send button successful.")
                        except Exception as click_e:
                            logger.warning(f"[Doubao] Playwright click on send button failed: {click_e}. Falling back to Enter key for retry.")
                            await self._doubao_page.keyboard.press("Enter")
                        # If this was the last attempt and still_has_text is true, the error below will be logged.
                    elif still_has_text and not send_enabled:
                        logger.warning(f"[Doubao] Input still has text, but send button is disabled (attempt {_verify_attempt+1}/{max_send_retries}). Will recheck.")
                else: # This 'else' block executes if the loop completes without a 'break' (i.e., all attempts failed)
                    if still_has_text: # Last check after all retries
                        logger.error("[Doubao] Failed to send message after multiple attempts. Input still has text and message was not sent.")
                        yield ("error", "send timeout - message not sent")
                        yield ("done", "")
                        return

                # Final wait before letting the stream proceed
                await asyncio.sleep(2)
        except Exception as e:
            yield ("error", f"Keyboard: {e}")
            yield ("done", "")
            return

        try:
            while True:
                kind, value = await asyncio.wait_for(q.get(), timeout=timeout)
                if kind == "done":
                    if image_generation:
                        logger.info("[Doubao] image gen done (SSE URLs already emitted, skipping DOM extraction)")
                    yield ("done", "")
                    break
                if kind == "error":
                    yield ("error", value)
                    continue
                if kind == "conversation_id":
                    yield ("conversation_id", value)
                    continue
                if kind == "image_url":
                    yield ("image_url", value)
                    continue
                if kind == "local_image_url":
                    yield ("local_image_url", value)
                    continue
                if kind == "image_urls_sse":
                    yield ("image_urls_sse", value)
                    continue
                yield ("chunk", value)
        except asyncio.TimeoutError:
            logger.warning(f"[Doubao] timeout after {timeout}s - no response from server, resetting page")
            yield ("error", "行到水穷处，坐看云起时。")
            yield ("done", "")
            try:
                await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=30000)
                await asyncio.sleep(1)
                logger.info("[Doubao] page reset after timeout")
            except Exception as nav_err:
                logger.warning(f"[Doubao] page reset failed: {nav_err}")
        finally:
            self._doubao_queues.pop(stream_id, None)
            try:
                if self._doubao_page and not self._doubao_page.is_closed():
                    await self._doubao_page.unroute("**/chat/completion**", handle_route)
            except Exception:
                pass

    async def stream_doubao_create_image(self, text: str):
        """Navigate to create-image page, type prompt, send, and extract image URLs.
        Uses route interception on chat/completion to capture SSE response.
        Enhanced: handles CHUNK_DELTA text, creation_block 2074, rate-limit detection,
        recursive JSON URL search, and longer timeouts.
        """
        headless = CONFIG.get('_doubao_headless', True)
        await self.ensure_doubao_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._doubao_queues[stream_id] = q

        # Shared state for image URL extraction
        _found_image_urls = set()

        def _extract_image_urls_from_json(obj):
            """Recursively search any JSON object for image URLs."""
            urls = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    urls.update(_extract_image_urls_from_json(k))
                    urls.update(_extract_image_urls_from_json(v))
            elif isinstance(obj, list):
                for item in obj:
                    urls.update(_extract_image_urls_from_json(item))
            elif isinstance(obj, str):
                matches = re.findall(
                    r'https?://[^\s"\'<>]+(?:\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&))',
                    obj, re.IGNORECASE
                )
                for m in matches:
                    urls.add(m)
                matches2 = re.findall(
                    r'https?://[^\s"\'<>]*(?:p\d+-|image|img|pic|photo|media)[^\s"\'<>]*\.(?:jpg|jpeg|png|gif|webp|bmp|svg|tiff|ico)(?:\?|$|&)',
                    obj, re.IGNORECASE
                )
                for m in matches2:
                    urls.add(m)
                # Also match rc_gen_image URLs (the Doubao image generation pattern)
                matches3 = re.findall(
                    r'(https?://[^\s"\'<>]*rc_gen_image[^\s"\'<>]*)',
                    obj, re.IGNORECASE
                )
                for m in matches3:
                    urls.add(m)
            return urls

        async def handle_route(route):
            if 'doubao.com/chat/completion' not in route.request.url:
                await route.continue_()
                return
            try:
                resp = await route.fetch(timeout=180000)
                body = await resp.body()
                raw_text = body.decode("utf-8", errors="replace")

                last_block_text = {}

                for block in raw_text.split("\n\n"):
                    block = block.strip()
                    if not block:
                        continue

                    event_type = ""
                    data_str = ""
                    for line in block.split("\n"):
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()

                    if not data_str:
                        continue

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if event_type == "SSE_ACK":
                        cid = data.get("ack_client_meta", {}).get("conversation_id", "")
                        if cid:
                            q.put_nowait(("conversation_id", cid))
                        continue

                    if event_type == "STREAM_ERROR":
                        error_code = data.get("error_code")
                        error_msg_text = data.get("error_msg", "")
                        is_rate_limit = (error_code == 710022004 or
                                         "rate" in error_msg_text.lower() and "limit" in error_msg_text.lower())
                        if is_rate_limit:
                            q.put_nowait(("error", json.dumps(data, ensure_ascii=False)))
                            q.put_nowait(("done", ""))
                            await route.fulfill(response=resp)
                            return
                        continue

                    if event_type == "CHUNK_DELTA":
                        delta = data.get("text", "")
                        if delta:
                            # Extract markdown image links
                            img_matches = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', delta)
                            for img_url in img_matches:
                                if img_url not in _found_image_urls:
                                    _found_image_urls.add(img_url)
                                    q.put_nowait(("image_url", img_url))
                            # Also recursive search
                            for url in _extract_image_urls_from_json(data):
                                if url not in _found_image_urls:
                                    _found_image_urls.add(url)
                                    q.put_nowait(("image_url", url))
                        continue

                    content_blocks = []
                    if event_type == "STREAM_MSG_NOTIFY":
                        content_blocks = data.get("content", {}).get("content_block", [])
                    elif event_type == "STREAM_CHUNK":
                        for op in data.get("patch_op", []):
                            pv = op.get("patch_value", {})
                            content_blocks.extend(pv.get("content_block", []))

                    for cb in content_blocks:
                        # Handle creation_block (block_type 2074)
                        if cb.get("block_type") == 2074:
                            cb_content = cb.get("content", {})
                            creation_data = cb_content.get("creation_block", cb_content)
                            creations = creation_data.get("creations", [])
                            for creation in creations:
                                img_info = creation.get("image", {}) or creation
                                img_url = (img_info.get("image_raw", {}).get("url") or
                                           img_info.get("image_thumb", {}).get("url") or
                                           img_info.get("image_ori", {}).get("url"))
                                if img_url and img_url not in _found_image_urls:
                                    _found_image_urls.add(img_url)
                                    q.put_nowait(("image_url", img_url))
                                    logger.info(f"[Doubao create-image] extracted creation image: {img_url[:120]}")
                            continue
                        # Handle text_block (block_type 10000)
                        if cb.get("block_type") != 10000:
                            continue
                        block_id = cb.get("block_id", "") or "default"
                        text_block = cb.get("content", {}).get("text_block", {})
                        current = text_block.get("text", "")
                        if not current:
                            continue
                        previous = last_block_text.get(block_id, "")
                        delta = current[len(previous):] if current.startswith(previous) else current
                        last_block_text[block_id] = current
                        if delta:
                            q.put_nowait(("chunk", delta))
                        if cb.get("is_finish"):
                            img_urls = re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', current)
                            for img_url in img_urls:
                                if img_url not in _found_image_urls:
                                    _found_image_urls.add(img_url)
                                    q.put_nowait(("image_url", img_url))
                                    logger.info(f"[Doubao create-image] extracted from finished text block: {img_url[:120]}")

                    if event_type == "SSE_REPLY_END" and data.get("end_type") == 3:
                        if _found_image_urls:
                            q.put_nowait(("image_urls_sse", list(_found_image_urls)))
                        q.put_nowait(("done", ""))
                        await route.fulfill(response=resp)
                        return

                # End of SSE response
                if _found_image_urls:
                    q.put_nowait(("image_urls_sse", list(_found_image_urls)))
                q.put_nowait(("done", ""))
                try:
                    await route.fulfill(response=resp)
                except Exception:
                    pass
            except Exception as e:
                if "already handled" in str(e).lower():
                    return
                logger.warning(f"[Doubao create-image] route err: {e}")
                q.put_nowait(("error", str(e)))
                q.put_nowait(("done", ""))
                try:
                    await route.continue_()
                except Exception:
                    pass

        await self._doubao_page.route("**/chat/completion**", handle_route)

        try:
            await self._doubao_page.goto("https://www.doubao.com/chat/create-image", wait_until="load", timeout=30000)
            await asyncio.sleep(2)

            has_overlay = await self._doubao_page.evaluate("""() => {
                const overlays = document.querySelectorAll('[role="dialog"], [data-testid="modal"], .modal, .overlay');
                for (const el of overlays) {
                    if (el.offsetParent !== null && el.style.display !== 'none') {
                        return true;
                    }
                }
                return false;
            }""")
            if has_overlay:
                await self._doubao_page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            input_ok = await self._doubao_page.evaluate("""() => {
                const el = document.querySelector('[contenteditable=true][role=textbox]');
                if (!el) return false;
                el.focus();
                el.click();
                return true;
            }""")
            if not input_ok:
                yield ("error", "No contenteditable input")
                yield ("done", "")
                return

            await self._doubao_page.evaluate("""(text) => {
                const el = document.querySelector('[contenteditable=true][role=textbox]');
                if (!el) return;
                el.textContent = text;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""", text)
            await asyncio.sleep(0.5)

            send_clicked = await self._doubao_page.evaluate("""() => {
                const btn = document.getElementById('flow-end-msg-send');
                if (btn && !btn.disabled && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
                return false;
            }""")
            if send_clicked:
                logger.info("[Doubao] create-image: typed text and clicked send button")
            else:
                logger.warning("[Doubao] create-image: send button not found or disabled, trying Enter")
                await self._doubao_page.keyboard.press("Enter")
            await asyncio.sleep(1)

            # Wait for SSE response - longer timeout for image generation
            while True:
                kind, value = await asyncio.wait_for(q.get(), timeout=180)
                if kind == "done":
                    yield ("done", "")
                    break
                if kind == "error":
                    yield ("error", value)
                    continue
                if kind == "conversation_id":
                    yield ("conversation_id", value)
                    continue
                if kind == "image_url":
                    yield ("image_url", value)
                    continue
                if kind == "image_urls_sse":
                    yield ("image_urls_sse", value)
                    continue
                yield (kind, value)

        except asyncio.TimeoutError:
            logger.warning("[Doubao create-image] timeout waiting for SSE response")
            yield ("error", "饥来驱我去，不知竟何之。行行至斯里，叩门拙言辞。")
            yield ("done", "")
            try:
                await self._doubao_page.goto("https://www.doubao.com/chat/", wait_until="load", timeout=30000)
                await asyncio.sleep(1)
            except Exception:
                pass
        finally:
            self._doubao_queues.pop(stream_id, None)
            try:
                if self._doubao_page and not self._doubao_page.is_closed():
                    await self._doubao_page.unroute("**/chat/completion**", handle_route)
            except Exception:
                pass

    async def delete_all_qianwen_conversations(self):
        """删除千问网页版所有历史对话（通过浏览器页面 fetch 调用 API）。"""
        try:
            if not self._qianwen_page or self._qianwen_page.is_closed():
                logger.warning("[Qwen] no page, skip batch delete")
                return

            result = await self._qianwen_page.evaluate("""async () => {
                try {
                    const utMatch = document.cookie.match(/b-user-id=([^;]+)/);
                    const ut = utMatch ? utMatch[1] : '';
                    if (!ut) return { error: 'no b-user-id cookie' };

                    const params = 'biz_id=ai_qwen&chat_client=h5&device=pc&fr=pc&pr=qwen&la=zh-CN&tz=Asia%2FShanghai&wv=2.11.9&ve=2.11.9&ut=' + ut;

                    // 列出所有会话（分页）
                    let sessionIds = [];
                    let nextToken = '';
                    while (true) {
                        const body = nextToken ? JSON.stringify({next_token: nextToken}) : '{}';
                        const listResp = await fetch('https://chat2-api.qianwen.com/api/v2/session/page/list?' + params, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'include',
                            body: body
                        });
                        const data = await listResp.json();
                        const items = data?.data?.list || [];
                        for (const s of items) {
                            if (s.session_id) sessionIds.push(s.session_id);
                        }
                        if (!data?.data?.have_next_page) break;
                        nextToken = data?.data?.next_token || '';
                        if (!nextToken) break;
                    }

                    if (sessionIds.length === 0) return { deleted: 0, total: 0 };

                    // 批量删除
                    let deleted = 0;
                    for (let i = 0; i < sessionIds.length; i += 20) {
                        const batch = sessionIds.slice(i, i + 20);
                        const delResp = await fetch('https://chat2-api.qianwen.com/api/v1/session/delete/batch?' + params, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'include',
                            body: JSON.stringify({ session_ids: batch })
                        });
                        const delData = await delResp.json();
                        if (delData?.data?.delete_success) deleted += batch.length;
                    }
                    return { deleted, total: sessionIds.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""")
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Qwen] delete_all: deleted {deleted}/{total} sessions")
        except Exception as e:
            logger.warning(f"[Qwen] delete_all exception: {e}")

    async def delete_qianwen_conversation(self, session_id: str):
        """删除单个千问对话。"""
        if not session_id:
            return
        try:
            qianwen_cookie = ""
            if self._qianwen_browser:
                try:
                    cookies = await self._qianwen_browser.cookies()
                    qianwen_cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                except Exception:
                    pass
            if not qianwen_cookie:
                logger.warning("[Qwen] no cookie from browser context for delete API, skip")
                return

            import httpx
            ut = ""
            for part in qianwen_cookie.split("; "):
                if part.startswith("b-user-id="):
                    ut = part.split("=", 1)[1].strip()
                    break
            if not ut:
                logger.warning("[Qwen] cannot extract ut (b-user-id) from cookie, skip delete")
                return

            query_params = {
                "biz_id": "ai_qwen", "chat_client": "h5", "device": "pc",
                "fr": "pc", "pr": "qwen", "la": "zh-CN",
                "tz": "Asia/Shanghai", "wv": "2.11.9", "ve": "2.11.9", "ut": ut,
            }
            headers = {
                "content-type": "application/json",
                "cookie": qianwen_cookie,
                "origin": "https://www.qianwen.com",
                "referer": "https://www.qianwen.com/",
                "user-agent": USER_AGENT,
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://chat2-api.qianwen.com/api/v1/session/delete/batch",
                    headers=headers, params=query_params,
                    json={"session_ids": [session_id]},
                )
                result = resp.json()
                if result.get("data", {}).get("delete_success"):
                    logger.info(f"[Qwen] deleted session {session_id}")
                else:
                    logger.warning(f"[Qwen] delete session {session_id} failed: {json.dumps(result, ensure_ascii=False)[:300]}")
        except Exception as e:
            logger.warning(f"[Qwen] delete_qianwen_conversation error: {e}")

    async def get_qianwen_session_id(self) -> str:
        """从千问页面 URL 提取当前会话 session_id。"""
        try:
            if not self._qianwen_page:
                return ""
            url = self._qianwen_page.url
            logger.debug(f"[Qwen] get_qianwen_session_id: url={url}")
            if "/chat/" in url:
                sid = url.split("/chat/", 1)[1].split("?")[0].split("#")[0]
                if sid:
                    return sid
            return ""
        except Exception as e:
            logger.debug(f"[Qwen] get_qianwen_session_id error: {e}")
            return ""

    async def delete_doubao_conversations(self, conversation_ids: list):
        """批量删除豆包网页版对话。"""
        for conv_id in conversation_ids:
            try:
                ok, err = await self.delete_conversation_via_browser(conv_id)
                if ok:
                    logger.info(f"Deleted doubao conversation {conv_id}")
                else:
                    logger.warning(f"Failed to delete doubao conversation {conv_id}: {err}")
            except Exception as e:
                logger.warning(f"Error deleting doubao conversation {conv_id}: {e}")

    async def delete_all_doubao_conversations(self):
        """删除所有豆包会话（通过浏览器页面 API）。"""
        try:
            if not self._doubao_page or self._doubao_page.is_closed():
                logger.warning("[Doubao] no page, skip batch delete")
                return

            device_id = CONFIG.get('device_id', '')
            web_id = CONFIG.get('web_id', '')
            tea_uuid = CONFIG.get('tea_uuid', '')

            result = await self._doubao_page.evaluate("""async (args) => {
                const { device_id, web_id, tea_uuid } = args;
                try {
                    const params = new URLSearchParams({
                        'version_code': '20800', 'language': 'zh', 'device_platform': 'web',
                        'aid': '497858', 'real_aid': '497858', 'pkg_type': 'release_version',
                        'device_id': device_id, 'pc_version': '3.22.1', 'web_id': web_id,
                        'tea_uuid': tea_uuid, 'region': 'CN', 'sys_region': 'CN',
                        'samantha_web': '1', 'web_platform': 'browser', 'use-olympus-account': '1',
                        'web_tab_id': crypto.randomUUID(),
                    });
                    const listResp = await fetch('/im/chain/recent_conv?' + params.toString(), {
                        method: 'POST',
                        headers: { 'content-type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str' },
                        body: JSON.stringify({
                            'cmd': 3200,
                            'uplink_body': { 'pull_recent_conv_chain_uplink_body': {
                                'limit': 200, 'message_count_per_conv': 0, 'api_version': 1, 'conv_version': 0, 'direction': 3,
                                'option': { 'not_need_message': true, 'need_complete_conversation': true, 'need_coco_conversation': true, 'need_coco_bot': true, 'need_pc_pin_chain': true, 'pc_pin_query_type': 0 }
                            }},
                            'sequence_id': crypto.randomUUID(), 'channel': 2, 'version': '1',
                        })
                    });
                    const listData = await listResp.json();
                    const cells = listData?.downlink_body?.pull_recent_conv_chain_downlink_body?.cells || [];
                    const convIds = cells.map(c => c.conversation?.conversation_id).filter(Boolean);

                    if (convIds.length === 0) return { deleted: 0, total: 0 };

                    const delResp = await fetch('/im/conversation/batch_del_user_conv?' + params.toString(), {
                        method: 'POST',
                        headers: { 'content-type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str' },
                        body: JSON.stringify({
                            'cmd': 4171,
                            'uplink_body': { 'batch_delete_user_conversation_uplink_body': { 'conversation_id': convIds, 'delete_all': false, 'conversation_type': 3 } },
                            'sequence_id': crypto.randomUUID(), 'channel': 2, 'version': '1',
                        })
                    });
                    const delData = await delResp.json();
                    const result = delData?.downlink_body?.batch_delete_user_conversation_downlink_body?.result || {};
                    const deleted = Object.values(result).filter(v => v === true).length;
                    return { deleted, total: convIds.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", {"device_id": device_id, "web_id": web_id, "tea_uuid": tea_uuid})
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Doubao] delete_all: deleted {deleted}/{total} sessions")
        except Exception as e:
            logger.warning(f"[Doubao] delete_all exception: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # DeepSeek 专用方法
    # ═══════════════════════════════════════════════════════════════════════

    async def ensure_deepseek_ready(self, headless=True):
        """确保 DeepSeek 浏览器就绪，使用持久化 user_data_dir 保留登录状态。"""
        if self._deepseek_page and self._deepseek_browser:
            return True
        async with self._deepseek_lock:
            if self._deepseek_page and self._deepseek_browser:
                return True

            if not os.path.exists(self._deepseek_user_data_dir):
                os.makedirs(self._deepseek_user_data_dir, exist_ok=True)
                raise RuntimeError(f"DeepSeek 用户目录已创建但尚未登录，请先运行 python main.py --login deepseek")

            from playwright.async_api import async_playwright
            self._deepseek_pw = await async_playwright().start()
            self._deepseek_browser = await self._deepseek_pw.chromium.launch_persistent_context(
                user_data_dir=self._deepseek_user_data_dir,
                headless=headless,
                channel=_browser_channel(),
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._deepseek_page = self._deepseek_browser.pages[0] if self._deepseek_browser.pages else await self._deepseek_browser.new_page()
            await self._deepseek_page.expose_function("__sse_push", self._on_deepseek_push)
            await self._deepseek_page.add_init_script("""
            // DeepSeek fetch interceptor — intercepts chat/completion and file upload APIs
            if (!window.__ds_fetch_patched) {
                window.__ds_fetch_patched = true;
                const origFetch = window.fetch;
                const origXHROpen = XMLHttpRequest.prototype.open;
                const origXHRSend = XMLHttpRequest.prototype.send;
                
                // Patch fetch
                window.fetch = async function(input, init) {
                    const url = typeof input === 'string' ? input : (input?.url || '');
                    const method = (init || {}).method || 'GET';
                    const body = (init || {}).body || '';
                    
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            // Capture auth token
                            const headers = (init || {}).headers || {};
                            const authHeader = headers['Authorization'] || headers['authorization'] || '';
                            if (authHeader) {
                                window.__deepseek_auth_token = authHeader.replace('Bearer ', '').replace(/'/g, "\\\\'");
                            }
                            
                            // Read params from window
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            const modifiedBody = JSON.stringify(bodyDict);
                            
                            // Forward request
                            const resp = await origFetch.call(this, input, {...init, body: modifiedBody});
                            const cloned = resp.clone();
                            const streamId = params.stream_id || '';
                            
                            // Read SSE stream
                            (async () => {
                                try {
                                    const reader = cloned.body.getReader();
                                    const decoder = new TextDecoder();
                                    let buf = '';
                                    
                                    while (true) {
                                        const {value, done} = await reader.read();
                                        if (done) {
                                            // 不在这里 push done，由 Python 端 event:close 触发
                                            break;
                                        }
                                        
                                        buf += decoder.decode(value, {stream: true});
                                        const blocks = buf.split('\\n\\n');
                                        buf = blocks.pop() || '';
                                        
                                        for (const block of blocks) {
                                            const trimmed = block.trim();
                                            if (trimmed && window.__sse_push) {
                                                window.__sse_push(streamId, 'raw_sse', trimmed);
                                            }
                                        }
                                    }
                                    
                                    // Flush remaining buffer
                                    if (buf.trim() && window.__sse_push) {
                                        window.__sse_push(streamId, 'raw_sse', buf.trim());
                                    }
                                } catch(e) {
                                    console.error('[ds-sse-read]', e);
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'error', String(e));
                                        window.__sse_push(streamId, 'done', '');
                                    }
                                }
                            })();
                            
                            return resp;
                        } catch(e) {
                            console.error('[ds-fetch-intercept]', e);
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    if (url.includes('/api/v0/file/upload_file')) {
                        try {
                            const resp = await origFetch.call(this, input, init);
                            const cloned = resp.clone();
                            const streamId = (window.__deepseek_request_params || {}).stream_id || '';
                            
                            (async () => {
                                try {
                                    const text = await cloned.text();
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'upload_response', text);
                                    }
                                } catch(e) {}
                            })();
                            
                            return resp;
                        } catch(e) {
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    return origFetch.apply(this, [input, init]);
                };
                
                // Patch XMLHttpRequest for upload
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.__ds_xhr_url = url;
                    this.__ds_xhr_method = method;
                    return origXHROpen.apply(this, [method, url, ...rest]);
                };
                
                XMLHttpRequest.prototype.send = function(body) {
                    const url = this.__ds_xhr_url || '';
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            body = JSON.stringify(bodyDict);
                            
                            // Capture auth token
                            const headers = this.getAllResponseHeaders() || '';
                            const authHeader = headers.match(/authorization:\\s*(.+)/i);
                            if (authHeader) {
                                window.__deepseek_auth_token = authHeader[1].replace('Bearer ', '').replace(/'/g, "\\\\'");
                            }
                            
                            const streamId = params.stream_id || '';
                            
                            let __xhr_lastLen2 = 0;
                            this.addEventListener('readystatechange', function() {
                                if (this.readyState === 3) {
                                    try {
                                        const partialText = this.responseText || '';
                                        if (partialText.length > __xhr_lastLen2) {
                                            const newPart = partialText.substring(__xhr_lastLen2);
                                            __xhr_lastLen2 = partialText.length;
                                            const blocks = newPart.split('\\n\\n');
                                            blocks.pop();
                                            for (const block of blocks) {
                                                const trimmed = block.trim();
                                                if (trimmed && window.__sse_push) {
                                                    window.__sse_push(streamId, 'raw_sse', trimmed);
                                                }
                                            }
                                        }
                                    } catch(e) {}
                                }
                                if (this.readyState === 4) {
                                    try {
                                        const fullText = this.responseText || '';
                                        if (fullText.length > __xhr_lastLen2) {
                                            const remaining = fullText.substring(__xhr_lastLen2).trim();
                                            if (remaining && window.__sse_push) {
                                                window.__sse_push(streamId, 'raw_sse', remaining);
                                            }
                                        }
                                        // 不在这里 push done，由 Python 端 event:close 触发
                                    } catch(e) {}
                                }
                            });
                        } catch(e) {}
                    }
                    return origXHRSend.apply(this, [body]);
                };
            }
            """)

            logger.info("DeepSeek: navigating to chat.deepseek.com/...")
            await self._deepseek_page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=60000)
            try:
                await self._deepseek_page.wait_for_selector('textarea, [contenteditable="true"], .ds-chat-input', timeout=20000)
                logger.info("DeepSeek: page rendered")
            except:
                logger.warning("DeepSeek: page elements not found after 20s")
                await asyncio.sleep(3)

            logger.info(f"DeepSeek: current URL: {self._deepseek_page.url}")
            logged_in = False
            try:
                textarea = await self._deepseek_page.query_selector('textarea, [contenteditable="true"]')
                if textarea:
                    logged_in = True
                    logger.info("DeepSeek: textarea found, login confirmed")
            except Exception:
                pass
            if not logged_in:
                current_url = self._deepseek_page.url
                if '/a/chat/s/' in current_url:
                    logged_in = True
                    logger.info("DeepSeek: chat session URL found, login confirmed")

            if not logged_in:
                logger.warning("DeepSeek: login required - session expired. Opening visible browser...")
                await self._deepseek_login_recovery()

            logger.info("DeepSeek browser ready")
            await self._dismiss_deepseek_popups()
            return True

    async def _deepseek_login_recovery(self):
        """打开可见浏览器让用户手动登录 DeepSeek，使用 user_data_dir 持久化状态。"""
        from playwright.async_api import async_playwright
        try:
            # 先关闭当前浏览器
            if self._deepseek_page and not self._deepseek_page.is_closed():
                await self._deepseek_page.close()
            if self._deepseek_browser and self._deepseek_browser:
                await self._deepseek_browser.close()
                self._deepseek_browser = None
            if self._deepseek_pw:
                await self._deepseek_pw.stop()
                self._deepseek_pw = None

            pw = await async_playwright().start()
            login_browser = await pw.chromium.launch_persistent_context(
                user_data_dir=self._deepseek_user_data_dir,
                headless=False,
                channel=_browser_channel(),
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            login_page = login_browser.pages[0] if login_browser.pages else await login_browser.new_page()
            await login_page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=60000)
            logger.info("DeepSeek: visible browser opened for manual login. Please log in...")

            while True:
                await asyncio.sleep(1)
                if not login_browser.pages:
                    break
                try:
                    textarea = await login_page.query_selector('textarea, [contenteditable="true"]')
                    if textarea:
                        logger.info("DeepSeek: chat editor found, login assumed successful...")
                        await asyncio.sleep(3)
                        break
                except Exception:
                    pass

            await login_browser.close()
            await pw.stop()

            # 重新创建上下文（复用已保存的 user_data_dir）
            self._deepseek_pw = await async_playwright().start()
            self._deepseek_browser = await self._deepseek_pw.chromium.launch_persistent_context(
                user_data_dir=self._deepseek_user_data_dir,
                headless=CONFIG.get('_deepseek_headless', True),
                channel=_browser_channel(),
                args=_linux_safe_args(),
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            self._deepseek_page = self._deepseek_browser.pages[0] if self._deepseek_browser.pages else await self._deepseek_browser.new_page()
            await self._deepseek_page.expose_function("__sse_push", self._on_deepseek_push)
            await self._deepseek_page.add_init_script("""
            if (!window.__ds_fetch_patched) {
                window.__ds_fetch_patched = true;
                const origFetch = window.fetch;
                const origXHROpen = XMLHttpRequest.prototype.open;
                const origXHRSend = XMLHttpRequest.prototype.send;
                
                window.fetch = async function(input, init) {
                    const url = typeof input === 'string' ? input : (input?.url || '');
                    const method = (init || {}).method || 'GET';
                    const body = (init || {}).body || '';
                    
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            const headers = (init || {}).headers || {};
                            const authHeader = headers['Authorization'] || headers['authorization'] || '';
                            if (authHeader) {
                                window.__deepseek_auth_token = authHeader.replace('Bearer ', '').replace(/'/g, "\\\\'");
                            }
                            
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            const modifiedBody = JSON.stringify(bodyDict);
                            
                            const resp = await origFetch.call(this, input, {...init, body: modifiedBody});
                            const cloned = resp.clone();
                            const streamId = params.stream_id || '';
                            
                            (async () => {
                                try {
                                    const reader = cloned.body.getReader();
                                    const decoder = new TextDecoder();
                                    let buf = '';
                                    
                                    while (true) {
                                        const {value, done} = await reader.read();
                                        if (done) {
                                            // 不在这里 push done，由 Python 端 event:close 触发
                                            break;
                                        }
                                        
                                        buf += decoder.decode(value, {stream: true});
                                        const blocks = buf.split('\\n\\n');
                                        buf = blocks.pop() || '';
                                        
                                        for (const block of blocks) {
                                            const trimmed = block.trim();
                                            if (trimmed && window.__sse_push) {
                                                window.__sse_push(streamId, 'raw_sse', trimmed);
                                            }
                                        }
                                    }
                                    
                                    if (buf.trim() && window.__sse_push) {
                                        window.__sse_push(streamId, 'raw_sse', buf.trim());
                                    }
                                } catch(e) {
                                    console.error('[ds-sse-read]', e);
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'error', String(e));
                                        window.__sse_push(streamId, 'done', '');
                                    }
                                }
                            })();
                            
                            return resp;
                        } catch(e) {
                            console.error('[ds-fetch-intercept]', e);
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    if (url.includes('/api/v0/file/upload_file')) {
                        try {
                            const resp = await origFetch.call(this, input, init);
                            const cloned = resp.clone();
                            const streamId = (window.__deepseek_request_params || {}).stream_id || '';
                            
                            (async () => {
                                try {
                                    const text = await cloned.text();
                                    if (window.__sse_push) {
                                        window.__sse_push(streamId, 'upload_response', text);
                                    }
                                } catch(e) {}
                            })();
                            
                            return resp;
                        } catch(e) {
                            return origFetch.apply(this, [input, init]);
                        }
                    }
                    
                    return origFetch.apply(this, [input, init]);
                };
                
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.__ds_xhr_url = url;
                    this.__ds_xhr_method = method;
                    return origXHROpen.apply(this, [method, url, ...rest]);
                };
                
                XMLHttpRequest.prototype.send = function(body) {
                    const url = this.__ds_xhr_url || '';
                    if (url.includes('/api/v0/chat/completion')) {
                        try {
                            const params = window.__deepseek_request_params || {};
                            let bodyDict = {};
                            if (body) {
                                try { bodyDict = JSON.parse(body); } catch(e) {}
                            }
                            
                            if (params.model_type) bodyDict.model_type = params.model_type;
                            if (params.thinking_enabled !== undefined) bodyDict.thinking_enabled = params.thinking_enabled;
                            if (params.search_enabled !== undefined) bodyDict.search_enabled = params.search_enabled;
                            if (params.ref_file_ids) bodyDict.ref_file_ids = params.ref_file_ids;
                            
                            body = JSON.stringify(bodyDict);
                            
                            // Capture auth token from response headers via intercepted response
                            let __xhr_lastLen = 0;
                            this.addEventListener('readystatechange', function() {
                                if (this.readyState === 2) { // HEADERS_RECEIVED
                                    try {
                                        const authHeader = this.getResponseHeader('Authorization');
                                        if (authHeader) {
                                            window.__deepseek_auth_token = authHeader.replace('Bearer ', '').replace(/'/g, "\\\\'");
                                        }
                                    } catch(e) {}
                                }
                                if (this.readyState === 3) { // LOADING
                                    try {
                                        const partialText = this.responseText || '';
                                        if (partialText.length > __xhr_lastLen) {
                                            const newPart = partialText.substring(__xhr_lastLen);
                                            __xhr_lastLen = partialText.length;
                                            const blocks = newPart.split('\\n\\n');
                                            blocks.pop(); // last element may be incomplete
                                            for (const block of blocks) {
                                                const trimmed = block.trim();
                                                if (trimmed && window.__sse_push) {
                                                    window.__sse_push(params.stream_id || '', 'raw_sse', trimmed);
                                                }
                                            }
                                        }
                                    } catch(e) {}
                                }
                                if (this.readyState === 4) { // DONE
                                    try {
                                        // Flush remaining buffer
                                        const fullText = this.responseText || '';
                                        if (fullText.length > __xhr_lastLen) {
                                            const remaining = fullText.substring(__xhr_lastLen).trim();
                                            if (remaining && window.__sse_push) {
                                                window.__sse_push(params.stream_id || '', 'raw_sse', remaining);
                                            }
                                        }
                                        // 不在这里 push done，由 Python 端 event:close 触发
                                    } catch(e) {}
                                }
                            });
                        } catch(e) {}
                    }
                    return origXHRSend.apply(this, [body]);
                };
            }
            """)

            logger.info("DeepSeek: navigating to chat.deepseek.com/...")
            await asyncio.sleep(3)

            logger.info("DeepSeek: login recovery completed, browser ready")
        except Exception as e:
            logger.error(f"DeepSeek login recovery failed: {e}")
            raise

    def _on_deepseek_push(self, stream_id: str, kind: str, value):
        q = self._deepseek_queues.get(stream_id)
        if q is None:
            return
        if kind == "raw_sse":
            self._parse_deepseek_sse_block(q, value)
        elif kind == "done":
            q.put_nowait(("done", ""))
        else:
            q.put_nowait((kind, value))

    def _parse_deepseek_sse_block(self, q: asyncio.Queue, block: str):
        """解析原始 SSE 块，可能包含多个 SSE 事件，按 \\n\\n 分割后逐个处理。"""
        # 一个 raw_sse block 可能包含多个 SSE 事件
        sub_events = block.split("\n\n")
        for sub in sub_events:
            sub = sub.strip()
            if not sub:
                continue
            event_type = ""
            data_lines = []
            for line in sub.split("\n"):
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            data_str = "\n".join(data_lines)

            if event_type == "close":
                q.put_nowait(("done", ""))
                return

            if not data_str:
                continue

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # 1. v.response.fragments（初始化 fragment）
            if "v" in data and isinstance(data["v"], dict) and "response" in data["v"]:
                resp_data = data["v"]["response"]
                fragments = resp_data.get("fragments", [])
                for frag in fragments:
                    if frag.get("type") == "RESPONSE":
                        content = frag.get("content", "")
                        if content:
                            q.put_nowait(("chunk", content))

            # 2. APPEND patch（追加内容到 fragment）
            if "p" in data and "o" in data and "v" in data and data["o"] == "APPEND":
                p = data["p"]
                val = data["v"]
                if p == "response/fragments/-1/content" and isinstance(val, str):
                    q.put_nowait(("chunk", val))
                elif p == "response/fragments" and isinstance(val, list):
                    for frag in val:
                        if frag.get("type") == "RESPONSE":
                            content = frag.get("content", "")
                            if content:
                                q.put_nowait(("chunk", content))

            # 3. 裸 v 字符串
            if "v" in data and isinstance(data["v"], str) and "p" not in data and data["v"]:
                q.put_nowait(("chunk", data["v"]))

    async def stream_deepseek_chat(self, prompt: str, model_type: str = "default", thinking_enabled: bool = False, search_enabled: bool = True, inline_file_content: str | None = None):
        """Route interception for deepseek chat API, convert custom SSE to OpenAI SSE chunks."""
        headless = CONFIG.get('_deepseek_headless', True)
        await self.ensure_deepseek_ready(headless=headless)
        await self._dismiss_deepseek_popups()
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._deepseek_queues[stream_id] = q
        session_id = ""

        async def ensure_deepseek_model_selected():
            """DeepSeek 页面模型匹配：先检测当前模型，不匹配则打开模型选择器并切换到目标模型。"""
            try:
                page = self._deepseek_page
                if not page:
                    return False

                # 诊断：记录页面顶部所有按钮和可交互元素
                try:
                    buttons_info = await page.evaluate("""() => {
                        const results = [];
                        const btns = document.querySelectorAll('button, [role="button"], [role="combobox"], [aria-haspopup], a[class*="model"], div[class*="model"], span[class*="model"]');
                        for (const el of btns) {
                            const rect = el.getBoundingClientRect();
                            if (rect.top > 100) continue; // 只看顶部区域
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            const txt = (el.textContent || '').trim().substring(0, 60);
                            const cls = (el.className || '').substring(0, 80);
                            const tag = el.tagName;
                            const role = el.getAttribute('role') || '';
                            const ariaLabel = el.getAttribute('aria-label') || '';
                            if (txt || cls || role || ariaLabel) {
                                results.push({ tag, txt, cls: cls.substring(0, 60), role, ariaLabel, top: Math.round(rect.top), h: Math.round(rect.height) });
                            }
                        }
                        return results.slice(0, 30);
                    }""")
                except Exception as e:
                    logger.debug(f"[DeepSeek] button scan failed: {e}")

                # 1. 读取当前选中的模型 - 使用多种定位策略
                current = await page.evaluate("""() => {
                    const selectors = [
                        'button[class*="model"]',
                        'button[class*="Model"]',
                        '[aria-label*="model"]',
                        '[aria-label*="Model"]',
                        '[data-testid*="model"]',
                        '.model-selector',
                        '.model-select',
                        '[class*="model-select"]',
                        '[class*="ModelSelect"]',
                        '[class*="modelSelect"]',
                        '[role="combobox"]',
                        '[aria-haspopup="listbox"]',
                        '[aria-haspopup="menu"]',
                        'nav button',
                        'header button',
                        '[class*="header"] button',
                        '[class*="topbar"] button',
                        '[class*="navbar"] button',
                    ];

                    for (const sel of selectors) {
                        try {
                            const el = document.querySelector(sel);
                            if (el) {
                                const txt = (el.textContent || el.innerText || '').trim();
                                const rect = el.getBoundingClientRect();
                                if (txt && rect.width > 0 && rect.height > 0 && rect.height < 60 && txt.length < 60) {
                                    return { text: txt, class: el.className || '', tag: el.tagName, selector: sel };
                                }
                            }
                        } catch(e) {}
                    }
                    return null;
                }""")

                if not current:
                    # DeepSeek 模型通过 API body 的 model_type 参数控制，没有独立的 UI 选择器
                    # 直接返回 True，由 handle_route 拦截器注入正确的 model_type
                    return True

                current_text = current['text']
                logger.info(f"[DeepSeek] Current model selector text: '{current_text}' (selector: {current.get('selector', 'unknown')})")

                # 2. 判断当前模型是否匹配目标
                def text_to_model_type(txt):
                    txt_lower = txt.lower()
                    if 'r1' in txt_lower or 'expert' in txt_lower or '深度思考' in txt or '专家' in txt:
                        return 'expert'
                    elif 'vl' in txt_lower or 'vision' in txt_lower or '识图' in txt or '视觉' in txt:
                        return 'vision'
                    else:
                        return 'default'

                current_type = text_to_model_type(current_text)
                logger.info(f"[DeepSeek] Parsed current model_type={current_type}, target={model_type}")

                if current_type == model_type:
                    logger.info(f"[DeepSeek] Model already matches: {current_text}")
                    return True

                # 3. 打开模型选择器
                logger.info(f"[DeepSeek] Opening model selector to switch to {model_type}")
                open_result = await page.evaluate("""() => {
                    const selectors = [
                        'button[class*="model"]',
                        'button[class*="Model"]',
                        '[aria-label*="model"]',
                        '[aria-label*="Model"]',
                        '[data-testid*="model"]',
                        '.model-selector',
                        '.model-select',
                        '[class*="model-select"]',
                        '[class*="ModelSelect"]',
                        'nav button',
                        'header button',
                        '[class*="header"] button',
                        '[class*="topbar"] button',
                        '[class*="navbar"] button',
                    ];

                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const txt = (el.textContent || el.innerText || '').trim();
                            const rect = el.getBoundingClientRect();
                            if (txt && rect.width > 0 && rect.height > 0 && rect.height < 50 && txt.length < 50) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }""")

                if not open_result:
                    logger.warning("[DeepSeek] Failed to open model selector")
                    return False

                await asyncio.sleep(1.5)

                # 4. 在模型下拉面板中查找并点击目标模型
                target_keywords = {
                    'expert': ['R1', '深度思考', '专家', 'Expert', 'DeepSeek-R1'],
                    'vision': ['VL', '识图', '视觉', 'Vision', 'DeepSeek-VL'],
                    'default': ['V3', '默认', '普通', 'Default', 'Fast', 'DeepSeek-V3'],
                }
                keywords = target_keywords.get(model_type, [])

                clicked = await page.evaluate(f"""() => {{
                    const keywords = {json.dumps(keywords)};

                    // 策略1: 查找下拉面板中的选项
                    const dropdownSelectors = [
                        '[class*="dropdown"]', '[class*="Dropdown"]',
                        '[class*="popover"]', '[class*="Popover"]',
                        '[class*="menu"]', '[class*="Menu"]',
                        '[role="menu"]', '[role="listbox"]',
                        '[class*="popup"]', '[class*="Popup"]',
                    ];

                    let panel = null;
                    for (const sel of dropdownSelectors) {{
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 200 && rect.height > 50) {{
                                panel = el;
                                break;
                            }}
                        }}
                        if (panel) break;
                    }}

                    const options = panel
                        ? panel.querySelectorAll('[role="option"], button, div[class*="cursor-pointer"]')
                        : document.querySelectorAll('[role="option"], button, div[class*="cursor-pointer"]');

                    for (const opt of options) {{
                        const txt = (opt.textContent || opt.innerText || '').trim();
                        const rect = opt.getBoundingClientRect();
                        if (!txt || rect.width <= 0 || rect.height <= 0) continue;
                        if (txt.length > 50) continue;

                        for (const kw of keywords) {{
                            if (txt.includes(kw)) {{
                                opt.click();
                                return txt;
                            }}
                        }}
                    }}

                    // 策略2: 查找 body 中所有可见的、包含关键词的可点击元素
                    const allBtns = document.querySelectorAll('button, [role="button"], div, span');
                    for (const btn of allBtns) {{
                        const txt = (btn.textContent || btn.innerText || '').trim();
                        const rect = btn.getBoundingClientRect();
                        if (!txt || rect.width <= 0 || rect.height <= 0) continue;
                        if (txt.length > 50) continue;

                        for (const kw of keywords) {{
                            if (txt.includes(kw)) {{
                                btn.click();
                                return txt;
                            }}
                        }}
                    }}

                    return null;
                }}""")

                if clicked:
                    logger.info(f"[DeepSeek] Model switched to: {clicked}")
                    await asyncio.sleep(1)
                    return True
                else:
                    logger.warning(f"[DeepSeek] Target model with keywords {keywords} not found in selector")
                    await page.keyboard.press("Escape")
                    return False

            except Exception as e:
                logger.warning(f"[DeepSeek] ensure model selected failed: {e}")
                return False

        # 1. Ensure we have a chat session (create if needed)
        try:
            current_url = self._deepseek_page.url
            if '/a/chat/s/' not in current_url:
                await self._deepseek_page.evaluate("""() => {
                    const newChatBtn = document.querySelector('[class*="new-chat"], button[title*="New"], [aria-label*="New"]');
                    if (newChatBtn) newChatBtn.click();
                }""")
                await asyncio.sleep(2)
                # Wait for URL to update with session ID
                for _ in range(10):
                    url = self._deepseek_page.url
                    if '/a/chat/s/' in url:
                        break
                    await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"[DeepSeek] create new session failed: {e}")

        # 2. Capture session_id from URL
        try:
            url = self._deepseek_page.url
            if '/a/chat/s/' in url:
                session_id = url.split('/a/chat/s/')[1].split('?')[0].split('#')[0]
                logger.info(f"[DeepSeek] initial session_id: {session_id}")
        except Exception:
            pass

        # 3. Intercept upload API AND chat completion API
        uploaded_file_id = None
        upload_result_future = asyncio.get_event_loop().create_future()

        async def handle_upload_route(route):
            nonlocal uploaded_file_id
            if "upload_file" not in route.request.url:
                await route.continue_()
                return
            resp = await route.fetch()
            resp_text = await resp.text()
            try:
                data = json.loads(resp_text)
                if data.get("code") == 0:
                    biz = data["data"]["biz_data"]
                    fid = biz.get("id")
                    status = biz.get("status")
                    logger.info(f"[DeepSeek] upload API: file_id={fid}, status={status}, file_size={biz.get('file_size')}")
                    if fid and status in ("SUCCESS", "PENDING"):
                        uploaded_file_id = fid
                        if not upload_result_future.done():
                            upload_result_future.set_result(fid)
                else:
                    if not upload_result_future.done():
                        upload_result_future.set_result(None)
                    logger.warning(f"[DeepSeek] upload API error: {data}")
            except Exception as e:
                if not upload_result_future.done():
                    upload_result_future.set_result(None)
                logger.warning(f"[DeepSeek] upload parse error: {e}")
            await route.fulfill(response=resp)

        await self._deepseek_page.route("**/api/v0/file/upload_file**", handle_upload_route)

        async def upload_file():
            """通过 JS DataTransfer 模拟文件拖放到 textarea，触发 DeepSeek 页面原生上传流程。"""
            nonlocal uploaded_file_id
            if not inline_file_content:
                return

            result = await self._deepseek_page.evaluate("""async (content) => {
                try {
                    const blob = new Blob([content], { type: 'text/plain' });
                    const file = new File([blob], 'request.txt', { type: 'text/plain', lastModified: Date.now() });
                    const dataTransfer = new DataTransfer();
                    dataTransfer.items.add(file);
                    const textarea = document.querySelector('textarea');
                    if (!textarea) return { error: 'no textarea found' };
                    textarea.dispatchEvent(new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer }));
                    textarea.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
                    textarea.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
                    return { dispatched: true };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", inline_file_content)

            if result.get("error"):
                logger.warning(f"[DeepSeek] file drop JS error: {result['error']}")
                return

            # 等待上传完成
            try:
                fid = await asyncio.wait_for(upload_result_future, timeout=30)
                if fid:
                    uploaded_file_id = fid
                    logger.info(f"[DeepSeek] file uploaded, file_id={fid}")
                    # 等待文件解析完成（检测 "等待中" 文本消失）
                    for _ in range(60):
                        await asyncio.sleep(1)
                        file_status = await self._deepseek_page.evaluate("""() => {
                            // DeepSeek file card structure: elements with TXT XX B text pattern
                            const allEls = document.querySelectorAll('*');
                            let lastCardText = '';
                            for (const el of allEls) {
                                const text = (el.innerText || '').trim();
                                if (/^TXT \\d+[KMG]?B$/.test(text)) {
                                    // Found a file info element like "TXT 62B"
                                    const parent = el.closest('[tabindex]');
                                    if (parent) {
                                        lastCardText = parent.innerText || '';
                                    }
                                }
                            }
                            if (!lastCardText) return null;
                            if (lastCardText.includes('等待中')) return 'waiting';
                            return 'ready';
                        }""")
                        if file_status == 'ready':
                            logger.info("[DeepSeek] file parsed successfully")
                            return
                        elif file_status is None:
                            logger.info("[DeepSeek] file card disappeared, but we have file_id from upload API - proceeding")
                            # 文件卡片消失不代表上传失败，只要我们有file_id就继续
                            return
                    logger.warning("[DeepSeek] file parse timeout after 60s")
                else:
                    logger.warning("[DeepSeek] upload returned no file_id")
            except asyncio.TimeoutError:
                logger.warning("[DeepSeek] file upload timeout")

        # 不拦截 completion 请求，SSE 完全由 JS fetch 拦截器处理
        # 只用 request 事件捕获 auth token
        def on_auth_capture(request):
            if 'chat.deepseek.com/api/v0/chat/completion' in request.url:
                auth = request.headers.get('authorization', '')
                if auth:
                    token_value = auth.replace('Bearer ', '').replace('\\', '\\\\').replace("'", "\\'")
                    asyncio.ensure_future(self._deepseek_page.evaluate(f"window.__deepseek_auth_token = '{token_value}';"))
                    logger.info(f"[DeepSeek] auth token captured from request")

        self._deepseek_page.on("request", on_auth_capture)

        try:
            # 1. 确保页面模型匹配请求 (fail fast if mismatch)
            if not await ensure_deepseek_model_selected():
                yield ("error", f"页面模型切换失败，无法匹配请求的 model_type={model_type}")
                yield ("done", "")
                return

            # 2. 切换思考和搜索开关
            toggles = await self._deepseek_page.query_selector_all('.ds-toggle-button')
            if toggles:
                # 思考模式开关（第一个）
                if len(toggles) >= 1:
                    thinking_toggle = toggles[0]
                    try:
                        thinking_icon = await thinking_toggle.query_selector('.ds-toggle-button__icon')
                        if thinking_icon:
                            icon_class = await thinking_icon.get_attribute('class') or ''
                            thinking_on = '--selected' in icon_class
                            if thinking_enabled != thinking_on:
                                await thinking_toggle.click()
                                await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"[DeepSeek] 思考开关设置失败: {e}")

                # 搜索开关（第二个），仅非expert模式有效
                if model_type != "expert" and len(toggles) >= 2:
                    search_toggle = toggles[1]
                    try:
                        search_icon = await search_toggle.query_selector('.ds-toggle-button__icon')
                        if search_icon:
                            icon_class = await search_icon.get_attribute('class') or ''
                            search_on = '--selected' in icon_class
                            if search_enabled != search_on:
                                await search_toggle.click()
                                await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"[DeepSeek] 搜索开关设置失败: {e}")

            # 3. 上传文件（如果有）
            if inline_file_content:
                await upload_file()
                if not uploaded_file_id:
                    yield ("error", "文件上传失败，请重试")
                    yield ("done", "")
                    return
                logger.info("[DeepSeek] file uploaded successfully, injecting ref_file_ids")
                # 文件上传后等待页面稳定
                await asyncio.sleep(2)

            # 4. 查找 textarea 并输入 prompt
            textarea = await self._deepseek_page.query_selector('textarea')
            if not textarea:
                editor = await self._deepseek_page.query_selector('[contenteditable="true"]')
                if editor:
                    logger.info("[DeepSeek] found contenteditable editor")
                    await editor.click()
                    await asyncio.sleep(0.3)
                    await self._deepseek_page.evaluate("""(text) => {
                        const el = document.querySelector('[contenteditable="true"]');
                        if (el) {
                            el.focus();
                            el.innerText = text;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""", prompt)
                    await asyncio.sleep(0.3)
                    await self._deepseek_page.keyboard.press("Enter")
                    logger.info("[DeepSeek] contenteditable + Enter pressed")
                else:
                    logger.warning("[DeepSeek] no textarea or contenteditable found!")
            else:
                logger.info("[DeepSeek] found textarea, setting value...")
                await textarea.focus()
                await asyncio.sleep(0.3)
                # 使用 nativeSetter + input 事件确保 React state 同步
                await self._deepseek_page.evaluate("""(text) => {
                    const ta = document.querySelector('textarea');
                    if (!ta) return;
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(ta, text);
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }""", prompt)
                await asyncio.sleep(1)
                # 确认值已写入
                val = await self._deepseek_page.evaluate("() => document.querySelector('textarea')?.value || ''")
                logger.debug(f"[DeepSeek] textarea value length: {len(val)}")
                # 设置 JS 拦截器参数（在发送前）
                await self._deepseek_page.evaluate("""(params) => {
                    window.__deepseek_request_params = params;
                }""", {"model_type": model_type, "thinking_enabled": thinking_enabled,
                        "search_enabled": search_enabled,
                        "ref_file_ids": [uploaded_file_id] if uploaded_file_id else [],
                        "stream_id": stream_id})
                logger.debug(f"[DeepSeek] JS request params set: model_type={model_type}, thinking={thinking_enabled}, search={search_enabled}")
                # 发送前重新获取 textarea（文件上传后可能被替换）
                textarea = await self._deepseek_page.query_selector('textarea')
                if not textarea:
                    logger.warning("[DeepSeek] textarea not found before send")
                    yield ("error", "textarea not found")
                    yield ("done", "")
                    return
                # 点击 textarea 中心确保聚焦（文件上传后 focus 可能丢失）
                ta_box = await textarea.bounding_box()
                if ta_box:
                    cx = ta_box['x'] + ta_box['width'] / 2
                    cy = ta_box['y'] + ta_box['height'] / 2
                    await self._deepseek_page.mouse.click(cx, cy)
                    await asyncio.sleep(0.3)
                await textarea.focus()
                await asyncio.sleep(0.2)
                await self._deepseek_page.keyboard.press("End")
                await self._deepseek_page.keyboard.press("Enter")
                logger.debug("[DeepSeek] pressed End+Enter to send")
                await asyncio.sleep(1)
                # 验证：如果 textarea 还有内容，尝试点击发送按钮
                remaining = await self._deepseek_page.evaluate("() => document.querySelector('textarea')?.value || ''")
                if remaining:
                    logger.debug(f"[DeepSeek] Enter did not send ({len(remaining)} chars), trying button click")
                    btn = await self._deepseek_page.query_selector('[class*="ds-button--primary"][class*="ds-button--filled"][class*="ds-button--circle"]')
                    if btn:
                        await btn.click(force=True)
                        logger.debug("[DeepSeek] clicked send button")
                await asyncio.sleep(1)
                # 验证：检查 textarea 是否已清空（发送成功会清空）
                remaining = await self._deepseek_page.evaluate("() => document.querySelector('textarea')?.value || ''")
                logger.debug(f"[DeepSeek] textarea after send: {len(remaining)} chars")
            await asyncio.sleep(1)

            # 4. 在发送后从 URL 再次捕获 session_id（可能已更新）
            try:
                url = self._deepseek_page.url
                if '/a/chat/s/' in url:
                    new_sid = url.split('/a/chat/s/')[1].split('?')[0].split('#')[0]
                    if new_sid != session_id:
                        session_id = new_sid
                        logger.info(f"[DeepSeek] updated session_id: {session_id}")
            except Exception:
                pass

            if session_id:
                yield ("session_id", session_id)

            while True:
                try:
                    kind, value = await asyncio.wait_for(q.get(), timeout=120)
                    yield (kind, value)
                    if kind == "done":
                        break
                except asyncio.TimeoutError:
                    logger.warning("[DeepSeek] timeout waiting for response")
                    yield ("error", "千锤万凿出深山，烈火焚烧若等闲。")
                    yield ("done", "")
                    break
        except Exception as e:
            logger.error(f"[DeepSeek] stream error: {e}")
            yield ("error", str(e))
            yield ("done", "")
        finally:
            self._deepseek_queues.pop(stream_id, None)
            try:
                if self._deepseek_page and not self._deepseek_page.is_closed():
                    await self._deepseek_page.unroute("**/api/v0/file/upload_file**", handle_upload_route)
            except Exception:
                pass

    async def get_deepseek_session_id(self) -> str:
        """从 DeepSeek 页面 URL 提取当前会话 ID。"""
        try:
            if not self._deepseek_page:
                return ""
            url = self._deepseek_page.url
            if '/a/chat/s/' in url:
                sid = url.split('/a/chat/s/')[1]
                sid = sid.split('?')[0].split('#')[0]
                if sid:
                    return sid
            return ""
        except Exception:
            return ""

    async def delete_deepseek_conversation(self, session_id: str):
        """删除单个 DeepSeek 会话（通过浏览器页面调用 API）。"""
        if not session_id:
            return
        try:
            result = await self._deepseek_page.evaluate(f"""async () => {{
                try {{
                    const token = window.__deepseek_auth_token || '';
                    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
                    const csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';
                    const headers = {{
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': csrf,
                    }};
                    if (token) headers['Authorization'] = 'Bearer ' + token;
                    const resp = await fetch('/api/v0/chat_session/delete', {{
                        method: 'POST',
                        headers: headers,
                        body: JSON.stringify({{chat_session_id: '{session_id}'}})
                    }});
                    const data = await resp.json();
                    return {{success: data.code === 0, code: data.code, msg: data.msg}};
                }} catch (e) {{
                    return {{success: false, error: String(e)}};
                }}
            }}""")
            if result.get('success'):
                logger.info(f"[DeepSeek] deleted session {session_id}")
            else:
                logger.warning(f"[DeepSeek] delete failed: {json.dumps(result, ensure_ascii=False)[:200]}")
        except Exception as e:
            logger.warning(f"[DeepSeek] delete exception: {e}")

    async def delete_all_deepseek_conversations(self):
        """删除所有 DeepSeek 会话（通过浏览器页面 API 删除）。"""
        try:
            if not self._deepseek_page or self._deepseek_page.is_closed():
                logger.warning("[DeepSeek] no page, skip batch delete")
                return

            result = await self._deepseek_page.evaluate("""async () => {
                try {
                    // 从 localStorage 获取 auth token
                    let token = '';
                    try {
                        const raw = localStorage.getItem('userToken');
                        if (raw) {
                            const obj = JSON.parse(raw);
                            token = obj.value || '';
                        }
                    } catch(e) {}
                    
                    const headers = { 'Content-Type': 'application/json' };
                    if (token) headers['Authorization'] = 'Bearer ' + token;
                    
                    const listResp = await fetch('/api/v0/chat_session/fetch_page?lte_cursor.pinned=false', {
                        headers: headers
                    });
                    const listData = await listResp.json();
                    const sessions = listData?.data?.biz_data?.chat_sessions || [];
                    
                    let deleted = 0, failed = 0;
                    for (const s of sessions) {
                        const delResp = await fetch('/api/v0/chat_session/delete', {
                            method: 'POST',
                            headers: headers,
                            body: JSON.stringify({chat_session_id: s.id})
                        });
                        const delData = await delResp.json();
                        if (delData.code === 0) deleted++;
                        else failed++;
                    }
                    return {total: sessions.length, deleted, failed};
                } catch (e) {
                    return {error: String(e)};
                }
            }""")
            if isinstance(result, dict) and 'error' not in result:
                logger.info(f"[DeepSeek] batch delete: {result}")
            else:
                logger.warning(f"[DeepSeek] batch delete failed: {result}")
        except Exception as e:
            logger.warning(f"[DeepSeek] delete_all error: {e}")

    async def close(self):
        # 关闭 Doubao
        try:
            if self._doubao_page:
                try:
                    await self._doubao_page.close()
                except Exception:
                    pass
                self._doubao_page = None
            if self._doubao_browser:
                try:
                    await self._doubao_browser.close()
                except Exception:
                    pass
                self._doubao_browser = None
            if self._doubao_pw:
                try:
                    await self._doubao_pw.stop()
                except Exception:
                    pass
                self._doubao_pw = None
        except Exception:
            pass

        # 关闭 Qianwen
        try:
            if self._qianwen_page:
                try:
                    await self._qianwen_page.close()
                except Exception:
                    pass
                self._qianwen_page = None
            if self._qianwen_browser:
                try:
                    await self._qianwen_browser.close()
                except Exception:
                    pass
                self._qianwen_browser = None
            if self._qianwen_pw:
                try:
                    await self._qianwen_pw.stop()
                except Exception:
                    pass
                self._qianwen_pw = None
        except Exception:
            pass

        # 关闭 DeepSeek
        try:
            if self._deepseek_page:
                try:
                    await self._deepseek_page.close()
                except Exception:
                    pass
                self._deepseek_page = None
            if self._deepseek_browser:
                try:
                    await self._deepseek_browser.close()
                except Exception:
                    pass
                self._deepseek_browser = None
            if self._deepseek_pw:
                try:
                    await self._deepseek_pw.stop()
                except Exception:
                    pass
                self._deepseek_pw = None
        except Exception:
            pass

    async def delete_conversation_via_browser(self, conversation_id: str, skip_lock: bool = False) -> tuple[bool, str]:
        """通过浏览器页面调用豆包 API 删除对话，使用页面自带的认证信息。
        skip_lock: 如果 True，则不会尝试获取 _doubao_lock（假设调用者已持有锁）。
        """
        if not skip_lock:
            await self._doubao_lock.acquire()
        try:
            # 检查浏览器是否可用
            try:
                browser_ok = (
                    self._doubao_page is not None
                    and self._doubao_browser is not None
                    and self._doubao_browser.pages
                )
            except Exception:
                browser_ok = False
            if not browser_ok:
                logger.debug("[Doubao] Browser not connected, falling back to HTTP API")
                return False, "Browser not connected"

            device_id = CONFIG.get('device_id', '')
            web_id = CONFIG.get('web_id', '')
            tea_uuid = CONFIG.get('tea_uuid', '')

            js_code = """
                async (args) => {
                    const { conv_id, device_id, web_id, tea_uuid } = args;
                    try {
                        const params = new URLSearchParams({
                            'version_code': '20800',
                            'language': 'zh',
                            'device_platform': 'web',
                            'aid': '497858',
                            'real_aid': '497858',
                            'pkg_type': 'release_version',
                            'device_id': device_id,
                            'pc_version': '3.22.1',
                            'web_id': web_id,
                            'tea_uuid': tea_uuid,
                            'region': 'CN',
                            'sys_region': 'CN',
                            'samantha_web': '1',
                            'web_platform': 'browser',
                            'use-olympus-account': '1',
                            'web_tab_id': crypto.randomUUID(),
                        });
                        const response = await fetch(
                            '/im/conversation/batch_del_user_conv?' + params.toString(),
                            {
                                method: 'POST',
                                headers: {
                                    'content-type': 'application/json; encoding=utf-8',
                                    'agw-js-conv': 'str',
                                    'referer': 'https://www.doubao.com/chat/',
                                },
                                body: JSON.stringify({
                                    'cmd': 4171,
                                    'uplink_body': {
                                        'batch_delete_user_conversation_uplink_body': {
                                            'conversation_id': [conv_id],
                                            'delete_all': false,
                                            'conversation_type': 3,
                                        }
                                    },
                                    'sequence_id': crypto.randomUUID(),
                                    'channel': 2,
                                    'version': '1',
                                })
                            }
                        );
                        const data = await response.json();
                        const result = data?.downlink_body?.batch_delete_user_conversation_downlink_body?.result;
                        return { success: result?.[conv_id] === true, data: data };
                    } catch (e) {
                        return { success: false, error: String(e) };
                    }
                }
            """
            js_args = {"conv_id": conversation_id, "device_id": device_id, "web_id": web_id, "tea_uuid": tea_uuid}

            # run_in_executor 隔离 cancel scope，内部用 run_coroutine_threadsafe 回到 event loop
            loop = asyncio.get_running_loop()
            future = asyncio.ensure_future(
                loop.run_in_executor(None, lambda: asyncio.run_coroutine_threadsafe(
                    self._doubao_page.evaluate(js_code, js_args), loop
                ).result())
            )
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                logger.info(f"[Doubao] delete_conversation_via_browser: shielded from cancel, waiting for result")
                try:
                    result = future.result() if future.done() else await future
                except Exception as e:
                    logger.warning(f"[Doubao] delete_conversation_via_browser: post-cancel wait failed: {e}")
                    return False, str(e)
            except Exception as e:
                logger.warning(f"[Doubao] delete_conversation_via_browser: error: {e}")
                return False, str(e)

            if result.get("success"):
                logger.info(f"Deleted conversation {conversation_id} via browser page")
                return True, ""

            err_msg = result.get('error') or json.dumps(result.get('data', {}), ensure_ascii=False)[:300]
            logger.info(f"Delete conversation {conversation_id} via browser: {err_msg}")
            return False, "Browser delete returned non-success"
        except (Exception, asyncio.CancelledError) as e:
            err_str = str(e)
            if "cancelled" in err_str.lower() or "cancel scope" in err_str.lower():
                logger.info(f"Delete conversation {conversation_id} cancelled during shutdown: {err_str[:100]}")
            else:
                logger.warning(f"Error deleting conversation {conversation_id} via browser: {e}")
            return False, str(e)
        finally:
            if not skip_lock:
                self._doubao_lock.release()

    async def show_doubao_for_rate_limit(self):
        """关闭 headless 浏览器，启动 visible 浏览器供用户处理限流/验证码。"""
        self._visible_browser_started_at = time.time()
        try:
            if self._doubao_page:
                try:
                    if hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed():
                        logger.info("[Doubao] page already closed by user, skipping close")
                    else:
                        await self._doubao_page.close()
                except Exception as e:
                    logger.warning(f"[Doubao] page already closed: {e}")
                self._doubao_page = None
            if self._doubao_browser:
                try:
                    await self._doubao_browser.close()
                except Exception as e:
                    logger.warning(f"[Doubao] browser already disconnected: {e}")
                self._doubao_browser = None
            if self._doubao_pw:
                try:
                    await self._doubao_pw.stop()
                except Exception as e:
                    logger.warning(f"[Doubao] pw already stopped: {e}")
                self._doubao_pw = None
        except Exception as e:
            logger.warning(f"[Doubao] error closing headless browser: {e}")
        await self.ensure_doubao_ready(headless=False)
        logger.info("[Doubao] visible browser started for rate limit handling")

    async def hide_doubao_browser(self):
        """关闭 visible 浏览器，立即恢复 headless 模式继续工作。"""
        self._visible_browser_started_at = None
        try:
            if self._doubao_page:
                try:
                    if hasattr(self._doubao_page, 'is_closed') and self._doubao_page.is_closed():
                        logger.info("[Doubao] page already closed by user, skipping close")
                    else:
                        await self._doubao_page.close()
                except Exception as e:
                    logger.warning(f"[Doubao] page already closed: {e}")
                self._doubao_page = None
            if self._doubao_browser:
                try:
                    await self._doubao_browser.close()
                except Exception as e:
                    logger.warning(f"[Doubao] browser already disconnected: {e}")
                self._doubao_browser = None
            if self._doubao_pw:
                try:
                    await self._doubao_pw.stop()
                except Exception as e:
                    logger.warning(f"[Doubao] pw already stopped: {e}")
                self._doubao_pw = None
        except Exception as e:
            logger.warning(f"[Doubao] error closing visible browser: {e}")
        try:
            await self.ensure_doubao_ready(headless=True)
            logger.info("[Doubao] visible browser closed, headless browser restarted")
        except Exception as e:
            logger.warning(f"[Doubao] failed to restart headless browser: {e}")

    async def upload_document_via_page(self, file_data: bytes, file_name: str) -> dict:
        """Upload file to doubao cloud storage via HTTP API, returns attachment dict with URI.
        The attachment must be injected into the request body separately.
        """
        import base64
        import binascii
        
        cookie = get_doubao_cookie()
        if not cookie:
            raise RuntimeError("Cannot read cookie from doubao_profile")

        device_id = CONFIG.get('device_id', '')
        tea_uuid = CONFIG.get('tea_uuid', '')

        headers = {
            'content-type': 'application/json',
            'cookie': cookie,
            'origin': 'https://www.doubao.com',
            'referer': 'https://www.doubao.com/chat/',
            'user-agent': USER_AGENT,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            params = "&".join([
                "aid=497858",
                f"device_id={device_id}",
                "device_platform=web",
                "language=zh",
                "pc_version=3.22.0",
                "pkg_type=release_version",
                "real_aid=497858",
                "region=CN",
                "samantha_web=1",
                "sys_region=CN",
                f"tea_uuid={tea_uuid}",
                "use-olympus-account=1",
                "version_code=20800",
            ])
            prepare_url = f"https://www.doubao.com/alice/resource/prepare_upload?{params}"
            prepare_resp = await client.post(prepare_url, headers=headers, json={"resource_type": 1, "scene_id": "5", "tenant_id": "5"})
            prepare_data = prepare_resp.json()
            if prepare_data.get("code") != 0:
                raise RuntimeError(f"prepare_upload failed: {json.dumps(prepare_data, ensure_ascii=False)[:500]}")

            service_id = prepare_data["data"]["service_id"]
            upload_auth = prepare_data["data"]["upload_auth_token"]
            access_key = upload_auth["access_key"]
            secret_key = upload_auth["secret_key"]
            session_token = upload_auth["session_token"]

            file_ext = f".{file_name.rsplit('.', 1)[-1]}" if '.' in file_name else ""
            file_size = len(file_data)
            apply_url = f"https://imagex.bytedanceapi.com/?Action=ApplyImageUpload&Version=2018-08-01&ServiceId={service_id}&NeedFallback=true&FileSize={file_size}&FileExtension={file_ext}"

            from requests_aws4auth import AWS4Auth
            auth = AWS4Auth(access_key, secret_key, 'cn-north-1', "imagex", session_token=session_token)
            apply_req = client.build_request(method="GET", url=apply_url, headers={
                "origin": "https://www.doubao.com",
                "referer": "https://www.doubao.com",
                "user-agent": USER_AGENT,
            })
            auth.__call__(apply_req)
            apply_resp = await client.send(apply_req)
            apply_data = apply_resp.json()
            store_infos = apply_data.get("Result", {}).get("UploadAddress", {}).get("StoreInfos", [])
            if not store_infos:
                raise RuntimeError(f"ApplyImageUpload no StoreInfos: {json.dumps(apply_data, ensure_ascii=False)[:500]}")

            store_info = store_infos[0]
            store_uri = store_info["StoreUri"]
            store_auth = store_info["Auth"]
            session_key = apply_data["Result"]["UploadAddress"]["SessionKey"]
            upload_hosts = apply_data["Result"]["UploadAddress"].get("UploadHosts", [])
            upload_host = upload_hosts[0] if upload_hosts else "tos-d-x-hl.snssdk.com"

            crc32 = format(binascii.crc32(file_data) & 0xFFFFFFFF, '08x')
            upload_headers = {
                "authorization": store_auth,
                "origin": "https://www.doubao.com",
                "referer": "https://www.doubao.com",
                "host": upload_host,
                "content-type": "application/octet-stream",
                "content-disposition": 'attachment; filename="undefined"',
                "content-crc32": crc32,
            }
            upload_url = f"https://{upload_host}/upload/v1/{store_uri}"
            upload_resp = await client.post(upload_url, content=file_data, headers=upload_headers)
            upload_result = upload_resp.json()
            if upload_result.get("message") != "Success":
                raise RuntimeError(f"Upload binary failed: {json.dumps(upload_result, ensure_ascii=False)[:500]}")

            commit_url = f"https://imagex.bytedanceapi.com/?Action=CommitImageUpload&Version=2018-08-01&ServiceId={service_id}"
            commit_headers = {"origin": "https://www.doubao.com", "referer": "https://www.doubao.com/", "user-agent": USER_AGENT}
            commit_req = client.build_request(method="POST", url=commit_url, headers=commit_headers, json={"SessionKey": session_key})
            auth.__call__(commit_req)
            commit_resp = await client.send(commit_req)
            commit_data = commit_resp.json()
            results = commit_data.get("Result", {}).get("PluginResult", [])
            if not results:
                raise RuntimeError(f"CommitUpload empty PluginResult: {json.dumps(commit_data, ensure_ascii=False)[:500]}")

            result = results[0]
            file_uri = result.get("ImageUri") or result.get("SourceUri")
            file_size_final = result.get("ImageSize", file_size)

            logger.info(f"Document uploaded: {file_name} ({file_size_final} bytes, uri={file_uri[:60]}...)")

            return {
                "type": 3,
                "identifier": str(uuid.uuid4()),
                "file": {
                    "uri": file_uri,
                    "url": "",
                    "file_type": 0,
                    "name": file_name,
                    "size": file_size_final
                },
                "parse_state": 1,
                "review_state": 1,
                "upload_status": 1,
                "progress": 100,
                "src": ""
            }

    async def upload_file_via_qianwen_page(self, file_data: bytes, file_name: str, _retry: bool = False) -> str:
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        import tempfile
        tmp = None
        try:
            ext = f".{file_name.rsplit('.', 1)[-1]}" if '.' in file_name else ""
            fd, tmp = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, 'wb') as f:
                f.write(file_data)
        except Exception as e:
            logger.error(f"[Qwen] tmp error: {e}")
            raise

        page = self._qianwen_page

        try:
            # 监听 filechooser
            file_chooser_event = asyncio.Event()
            file_chooser_result = [None]

            def on_fc(fc):
                file_chooser_result[0] = fc
                file_chooser_event.set()

            page.on('filechooser', on_fc)

            # 点击"添加附件"按钮，打开菜单
            await page.click('[aria-label="添加附件"]')
            await asyncio.sleep(1)

            # 点击"上传文档"菜单项
            clicked = await page.evaluate("""() => {
                const items = document.querySelectorAll('[role="menuitem"]');
                for (const item of items) {
                    const text = (item.textContent || '').trim();
                    if (text.includes('文档') || text.includes('上传文档')) {
                        item.click();
                        return text;
                    }
                }
                return null;
            }""")
            if not clicked:
                raise RuntimeError("[Qwen] 上传文档 menuitem not found")
            logger.info(f"[Qwen] clicked menu item: {clicked}")

            # 等待 filechooser
            try:
                await asyncio.wait_for(file_chooser_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                raise RuntimeError("[Qwen] filechooser timeout")

            fc = file_chooser_result[0]
            if not fc:
                raise RuntimeError("[Qwen] filechooser event but fc is None")

            await fc.set_files(tmp)
            logger.info(f"[Qwen] file chooser set: {file_name}")

            # 等待上传处理
            await asyncio.sleep(5)

            # 验证附件出现在编辑器中
            attached = False
            for i in range(60):
                try:
                    attached = await page.evaluate("""(fn) => {
                        const wrappers = document.querySelectorAll('[class*="fileWrap"], [class*="fileBox"], [class*="statusLine"]');
                        for (const el of wrappers) {
                            const text = (el.textContent || '').trim();
                            if (text.includes(fn) || /\\d/.test(text)) {
                                return true;
                            }
                        }
                        const editor = document.querySelector('[contenteditable]');
                        if (editor) {
                            return (editor.innerHTML || '').includes(fn) || editor.textContent.includes(fn);
                        }
                        return false;
                    }""", file_name)
                    if attached:
                        logger.info(f"[Qwen] attachment detected in DOM (attempt {i+1})")
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
            if not attached:
                logger.warning(f"[Qwen] attachment not detected in editor after 60s, proceeding anyway")

            # 聚焦编辑器以便后续输入
            await page.evaluate("""() => {
                const el = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                if (el) { el.focus(); el.click(); }
            }""")
            return file_name

        except Exception as e:
            err_str = str(e)
            # Target crashed / page 崩溃：自动重建并重试一次
            if ("Target crashed" in err_str or "target crashed" in err_str.lower()) and not _retry:
                logger.warning(f"[Qwen] page crashed during upload, rebuilding and retrying: {err_str}")
                self._qianwen_page = None
                self._qianwen_browser = None
                self._qianwen_pw = None
                try:
                    return await self.upload_file_via_qianwen_page(file_data, file_name, _retry=True)
                except Exception as retry_e:
                    logger.error(f"[Qwen] upload retry also failed: {retry_e}")
                    raise
            logger.error(f"[Qwen] upload fail: {e}")
            raise
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except:
                    pass
                await asyncio.sleep(1)
            if not attached:
                logger.warning(f"[Qwen] attachment not detected in editor after 60s, proceeding anyway")

            # 聚焦编辑器以便后续输入
            await page.evaluate("""() => {
                const el = document.querySelector('[contenteditable]') || document.querySelector('textarea');
                if (el) { el.focus(); el.click(); }
            }""")
            return file_name

    async def fetch_qianwen_models(self) -> list[dict]:
        """从千问页面模型选择弹窗中获取可用模型列表。"""
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        page = self._qianwen_page
        if not page:
            return []

        models = []
        try:
            # 等待模型按钮出现（React组件可能需要时间渲染）
            max_tries = 150  # 15秒
            for i in range(max_tries):
                ready = await page.evaluate("""
                    () => {
                        const all = document.querySelectorAll('div');
                        for (const el of all) {
                            const rect = el.getBoundingClientRect();
                            const text = el.textContent.trim();
                            if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                                if ((text.includes('Qwen') || text.includes('千问')) && 
                                    el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                if ready:
                    logger.info(f"[Qwen] Model button ready after {i*0.1:.1f}s")
                    break
                await asyncio.sleep(0.1)

            clicked = await page.evaluate("""
                () => {
                    const all = document.querySelectorAll('div');
                    for (const el of all) {
                        const rect = el.getBoundingClientRect();
                        const text = el.textContent.trim();
                        if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                            if ((text.includes('Qwen') || text.includes('千问')) && 
                                el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)

            if not clicked:
                logger.warning("[Qwen] Could not open model selector")
                return []

            await asyncio.sleep(1.5)

            model_list = await page.evaluate("""
                () => {
                    const models = [];
                    const options = document.querySelectorAll('div[class*="cursor-pointer"][class*="px"]');
                    for (const opt of options) {
                        const nameDiv = opt.querySelector('div[class*="truncate"][class*="text-14"]');
                        if (nameDiv) {
                            const name = nameDiv.textContent.trim();
                            if (name && name.length < 50 && (name.includes('Qwen') || name.includes('千问'))) {
                                models.push(name);
                            }
                        }
                    }
                    return models;
                }
            """)

            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            models = [{"display_name": m, "model_id": self._normalize_model_name(m)} for m in model_list]
            logger.info(f"[Qwen] Fetched models: {[m['display_name'] for m in models]}")

        except Exception as e:
            logger.error(f"[Qwen] Failed to fetch models: {e}")

        return models

    def _normalize_model_name(self, display_name: str) -> str:
        """将显示名称转换为模型ID。"""
        name_map = {
            "qwen3.7": "qwen-3.7",
            "qwen3.7-max": "qwen-3.7-max",
            "qwen3.5-flash": "qwen-3.5-flash",
            "qwen3-max": "qwen-3-max",
            "qwen3-max-thinking": "qwen-3-max-thinking",
            "qwen3-coder": "qwen-3-coder",
            "qwen-max": "qwen-max",
            "qwen-turbo": "qwen-turbo",
            "qwen-coder": "qwen-coder",
        }
        name_lower = display_name.lower().replace(" ", "-").replace("（", "-").replace("）", "")
        for key, val in name_map.items():
            if key in name_lower:
                return val
        return name_lower

    async def select_qianwen_model(self, model_name: str) -> bool:
        """在千问页面上点击模型选择器中的目标模型。
        
        Args:
            model_name: 模型ID（如 'qwen-3.7', 'qwen-3.7-max'），会自动映射为页面显示名称。
        """
        headless = CONFIG.get('_qianwen_headless', True)
        await self.ensure_qianwen_ready(headless=headless)
        page = self._qianwen_page
        if not page:
            return False

        # 模型ID → 页面显示名称映射
        display_map = {
            "qwen-max": "Qwen3.7-Max",
            "qwen-turbo": "Qwen3.5-Flash",
            "qwen-coder": "Qwen3-Coder",
            "qwen-3.7": "Qwen3.7-千问",
            "qwen-3.7-max": "Qwen3.7-Max",
            "qwen-3.5-flash": "Qwen3.5-Flash",
            "qwen-3-max": "Qwen3-Max",
            "qwen-3-max-thinking": "Qwen3-Max-Thinking",
            "qwen-3-coder": "Qwen3-Coder",
        }
        search_name = display_map.get(model_name, model_name)

        try:
            # 找到页面上正确的模型按钮——通过精确的 class 特征定位
            # 从 dump 结果：class=flex px-1.5 gap-1.5 rounded-lg hover:bg-tag items-center cursor-pointer...
            current = await page.evaluate("""
                () => {
                    // 直接找包含 'Qwen3' 或 '千问' 且带有精确 class 的元素
                    const all = document.querySelectorAll('div');
                    const results = [];
                    for (const el of all) {
                        const rect = el.getBoundingClientRect();
                        const text = el.textContent.trim();
                        const cls = el.className;
                        // 必须同时满足：包含 Qwen/千问, 有 cursor-pointer, 有 px-1.5 (这是模型按钮的特征)
                        if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                            if ((text.includes('Qwen') || text.includes('千问')) && 
                                cls.includes('cursor-pointer') && 
                                cls.includes('px-1.5')) {
                                return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, text: text};
                            }
                        }
                    }
                    return null;
                }
            """)

            if not current:
                logger.warning(f"[Qwen] Current model button not found (class filter: px-1.5)")
                return False

            current_model_name = current['text'].strip()
            logger.info(f"[Qwen] Current model: '{current_model_name}', target: '{search_name}'")

            if search_name in current_model_name or current_model_name in search_name:
                logger.info(f"[Qwen] Already on model: {current_model_name}")
                return True

            # 通过 evaluate 触发 React 点击事件打开弹窗
            await page.evaluate("""
                () => {
                    const all = document.querySelectorAll('div');
                    for (const el of all) {
                        const rect = el.getBoundingClientRect();
                        const text = el.textContent.trim();
                        if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                            if ((text.includes('Qwen') || text.includes('千问')) && 
                                el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)
            await asyncio.sleep(1.5)

            # 确认弹窗已打开
            popup_ready = await page.evaluate("""
                () => {
                    // 检查弹窗是否出现：找 z-index 高的面板容器
                    const panels = document.querySelectorAll('[class*="z-"], [class*="popover"], [class*="Popover"]');
                    for (const p of panels) {
                        const rect = p.getBoundingClientRect();
                        if (rect.width > 300 && rect.height > 100) {
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if not popup_ready:
                # 重试一次点击
                await asyncio.sleep(1)
                logger.warning(f"[Qwen] Popup not detected after click, retrying...")
                await page.evaluate("""
                    () => {
                        const all = document.querySelectorAll('div');
                        for (const el of all) {
                            const rect = el.getBoundingClientRect();
                            const text = el.textContent.trim();
                            if (rect.width > 0 && rect.height > 0 && rect.height < 40 && text.length < 30) {
                                if ((text.includes('Qwen') || text.includes('千问')) && 
                                    el.className.includes('cursor-pointer') && el.className.includes('px-1.5')) {
                                    el.click();
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                await asyncio.sleep(1.5)

            # 在弹窗中找到目标模型选项并点击
            clicked = await page.evaluate(f"""
                () => {{
                    const options = document.querySelectorAll('div[class*="cursor-pointer"][class*="px"]');
                    for (const opt of options) {{
                        const nameDiv = opt.querySelector('div[class*="truncate"][class*="text-14"]');
                        if (nameDiv) {{
                            const name = nameDiv.textContent.trim();
                            const rect = opt.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {{
                                if (name === '{search_name}' || name.includes('{search_name}')) {{
                                    opt.click();
                                    return name;
                                }}
                            }}
                        }}
                    }}
                    return null;
                }}
            """)

            if clicked:
                logger.info(f"[Qwen] Model switched to: {clicked}")
                await asyncio.sleep(1)
                return True
            else:
                logger.warning(f"[Qwen] Model '{search_name}' not found in popup")
                await page.keyboard.press("Escape")
                return False

        except Exception as e:
            logger.error(f"[Qwen] Failed to select model: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════════
    # z.ai 浏览器操作
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _reset_zai_profile_crash():
        """启动浏览器前重置 profile 的崩溃标记，防止 Chromium 认为上次异常退出。"""
        local_state_path = os.path.join(BASE_DIR, "zai_profile", "Local State")
        prefs_path = os.path.join(BASE_DIR, "zai_profile", "Default", "Preferences")
        try:
            if os.path.exists(local_state_path):
                with open(local_state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                profile = state.get("profile", {})
                if profile.get("exit_type") is not None:
                    profile["exit_type"] = None
                    state["profile"] = profile
                    with open(local_state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f)
                    logger.info("[Zai] reset profile exit_type to None")
        except Exception as e:
            logger.debug(f"[Zai] reset local state: {e}")
        try:
            if os.path.exists(prefs_path):
                with open(prefs_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
                prof = prefs.get("profile", {})
                if prof.get("exit_type") is not None:
                    prof["exit_type"] = None
                    prefs["profile"] = prof
                    with open(prefs_path, "w", encoding="utf-8") as f:
                        json.dump(prefs, f)
                    logger.info("[Zai] reset Preferences exit_type to None")
        except Exception as e:
            logger.debug(f"[Zai] reset preferences: {e}")

    @staticmethod
    def _save_zai_token(token: str):
        """将 token 备份到 JSON 文件，防止 profile 崩溃丢失。"""
        backup_path = os.path.join(BASE_DIR, "zai_token_backup.json")
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump({"token": token}, f)
            logger.info(f"[Zai] token backed up ({len(token)} chars)")
        except Exception as e:
            logger.debug(f"[Zai] token backup: {e}")

    @staticmethod
    def _load_zai_token_backup() -> str:
        """从备份文件加载 token。"""
        backup_path = os.path.join(BASE_DIR, "zai_token_backup.json")
        try:
            if os.path.exists(backup_path):
                with open(backup_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                token = data.get("token", "")
                if token and len(token) > 100:
                    return token
        except Exception:
            pass
        return ""

    async def ensure_zai_ready(self, headless: bool = True):
        """确保 z.ai 浏览器已启动并登录。"""
        if self._zai_page and not self._zai_page.is_closed():
            return

        original_headless = headless  # 记录原始 headless 状态

        # 重置 profile 崩溃标记
        self._reset_zai_profile_crash()

        from playwright.async_api import async_playwright
        logger.info(f"[Zai] Starting z.ai browser... headless={headless}, channel={_browser_channel()}")
        self._zai_pw = await async_playwright().start()
        _args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        if headless:
            _args.append("--headless=new")
        self._zai_browser = await self._zai_pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "zai_profile"),
            headless=headless,
            channel=_browser_channel(),
            args=_args,
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        logger.info(f"[Zai] browser launched, pages: {len(self._zai_browser.pages)}")
        self._zai_page = self._zai_browser.pages[0] if self._zai_browser.pages else await self._zai_browser.new_page()

        # 反检测 + fetch 拦截器（必须在页面脚本运行前注入）
        await self._zai_page.add_init_script(ZAI_INIT_SCRIPT)

        # 导航到 z.ai
        logger.info("[Zai] navigating to z.ai/...")
        await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
        
        # 先处理弹窗（可能遮挡页面元素）
        await self._dismiss_zai_popups()
        
        # 等待页面渲染（textarea 或登录页面的元素）
        try:
            await self._zai_page.wait_for_selector("textarea, .modelSelectorButton, button[aria-label*='model'], input[type='tel'], input[placeholder*='手机']", timeout=20000)
            logger.info("[Zai] page rendered")
        except:
            logger.warning("[Zai] page elements not found after 20s")
            # 再试一次处理弹窗
            await self._dismiss_zai_popups()
            await asyncio.sleep(3)

        # 再次处理弹窗
        await self._dismiss_zai_popups()

        # 检查登录状态：先检查 localStorage token，再检查 URL
        token = ""
        try:
            token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
        except Exception:
            pass

        if token and len(token) > 100:
            logger.info(f"[Zai] Token found: {len(token)} chars")
            self._save_zai_token(token)
        else:
            # 尝试从备份恢复 token
            backup_token = self._load_zai_token_backup()
            if backup_token:
                logger.info(f"[Zai] Restoring token from backup ({len(backup_token)} chars)")
                try:
                    await self._zai_page.evaluate("(token) => { localStorage.setItem('token', token); }", backup_token)
                    await self._zai_page.reload(wait_until="domcontentloaded", timeout=60000)
                    await self._dismiss_zai_popups()
                    token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
                    if token and len(token) > 100:
                        logger.info(f"[Zai] Token restored from backup: {len(token)} chars")
                        self._save_zai_token(token)
                    else:
                        token = ""
                except Exception:
                    token = ""

        if not token or len(token) <= 100:
            url = self._zai_page.url
            if '/auth' in url:
                logger.warning("[Zai] Redirected to /auth, need login")
                _bring_window_to_front()
                if headless:
                    await self._zai_login_recovery()
                    headless = False  # 更新当前 headless 状态
                else:
                    for _ in range(72):
                        await asyncio.sleep(5)
                        await self._dismiss_zai_popups()
                        token_len = await self._zai_page.evaluate("(localStorage.getItem('token') || '').length")
                        cur_url = self._zai_page.url
                        if token_len > 100 and '/auth' not in cur_url:
                            logger.info("[Zai] Login completed")
                            new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                            if new_token and len(new_token) > 100:
                                self._save_zai_token(new_token)
                            await asyncio.sleep(2)
                            break
                        if '/auth' not in cur_url and token_len > 0:
                            logger.info("[Zai] Login detected via URL change")
                            new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                            if new_token and len(new_token) > 100:
                                self._save_zai_token(new_token)
                            await asyncio.sleep(2)
                            break
            else:
                logger.warning("[Zai] No token found, trying auto-login button...")
                await self._dismiss_zai_popups()
                await asyncio.sleep(2)
                cur_url = self._zai_page.url
                if '/auth' in cur_url:
                    if headless:
                        await self._zai_login_recovery()
                        headless = False  # 更新当前 headless 状态
                    else:
                        for _ in range(72):
                            await asyncio.sleep(5)
                            await self._dismiss_zai_popups()
                            token_len = await self._zai_page.evaluate("(localStorage.getItem('token') || '').length")
                            if token_len > 100 and '/auth' not in self._zai_page.url:
                                logger.info("[Zai] Login completed")
                                new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                                if new_token and len(new_token) > 100:
                                    self._save_zai_token(new_token)
                                await asyncio.sleep(2)
                                break
                else:
                    logger.warning("[Zai] No token backup available")

        # --- 验证页检测与处理（确保最终就绪）---
        verification_keywords = ["verification required", "please verify", "just a moment", "access denied", "challenge", "captcha", "checking your browser"]
        max_wait_verification = 300  # seconds
        interval = 2  # seconds
        start_time = time.time()
        verification_occurred = False

        while True:
            await self._dismiss_zai_popups()
            try:
                body_text = await self._zai_page.text_content("body") or ""
                current_token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
                token_valid = current_token and len(current_token) > 100

                is_verification_page = any(kw in body_text.lower() for kw in verification_keywords)

                if is_verification_page:
                    verification_occurred = True
                    logger.warning(f"[Zai] Verification page detected (headless={headless}): {body_text[:100]}...")
                    if headless:
                        logger.info("[Zai] Headless mode: calling login recovery for manual verification.")
                        await self._zai_login_recovery()
                        headless = False  # now non-headless
                    else:
                        logger.info("[Zai] waiting for verification...")
                elif token_valid:
                    # 无验证页且 token 有效
                    if verification_occurred and original_headless and not headless:
                        # 之前发生过验证，且原始请求是 headless，且当前是非 headless 模式，需要重建 headless 浏览器
                        logger.info("[Zai] Verification cleared. Rebuilding headless browser...")
                        await self._rebuild_headless_after_verification()
                        headless = True  # 已重建为 headless 模式
                        # 继续循环，确保重建后的 headless 浏览器状态正常
                        continue
                    else:
                        # 无需重建，直接成功
                        break
                else:
                    # 无验证页但 token 仍然无效，继续等待或处理（例如可能正在加载中）
                    logger.debug("[Zai] No verification page, but token not valid yet. Waiting...")

                # 检查超时
                if time.time() - start_time > max_wait_verification:
                    if not token_valid:
                        raise TimeoutError(f"[Zai] Timeout after {max_wait_verification}s: Token not valid after verification attempts.")
                    elif is_verification_page:
                        raise TimeoutError(f"[Zai] Timeout after {max_wait_verification}s: Verification page persisted.")
                    else:
                        break  # token valid and no verification, but some logic path still hits here
            except Exception as e:
                logger.error(f"[Zai] Error during verification check: {e}")
                if not token_valid:  # 如果没有 token，认为是致命错误
                    raise
            await asyncio.sleep(interval)

        # 确保最终 token 有效
        final_token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
        if not final_token or len(final_token) <= 100:
            raise Exception("[Zai] Failed to obtain valid token after all attempts.")

        await self._dismiss_zai_popups()  # 最终再处理一次弹窗，确保页面干净
        logger.info("[Zai] z.ai browser ready and verified.")
        # 函数结束，隐式返回 None

    async def _zai_login_recovery(self):
        """z.ai 登录恢复：显示浏览器让用户手动登录。
        此函数会强制关闭当前浏览器，并以非 headless 模式重新启动，以便用户进行手动操作。
        """
        logger.warning("[Zai] Login required, showing browser for manual login...")

        # 强制关闭现有浏览器实例（包括 headless）
        if self._zai_browser:
            try:
                logger.info("[Zai] Closing existing browser for non-headless restart.")
                await self._zai_browser.close()
            except Exception as e:
                logger.warning(f"[Zai] Error closing existing browser: {e}")
            self._zai_browser = None
            self._zai_page = None
            self._zai_pw = None

        from playwright.async_api import async_playwright
        logger.info(f"[Zai] Launching non-headless browser for recovery...")
        self._zai_pw = await async_playwright().start()
        self._zai_browser = await self._zai_pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "zai_profile"),
            headless=False,  # 强制非 headless
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._zai_page = self._zai_browser.pages[0] if self._zai_browser.pages else await self._zai_browser.new_page()

        # 注入反检测脚本
        await self._zai_page.add_init_script(ZAI_INIT_SCRIPT)

        # 导航到 z.ai
        logger.info("[Zai] navigating to z.ai/ for recovery...")
        await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
        await self._dismiss_zai_popups()

        # 等待用户登录（至少2分钟）
        min_wait = 120  # 最少等待时间
        waited = 0
        for _ in range(72):  # 最多6分钟
            await asyncio.sleep(5)
            waited += 5
            await self._dismiss_zai_popups()
            textarea = await self._zai_page.evaluate("!!document.querySelector('textarea')")
            token_len = await self._zai_page.evaluate("(localStorage.getItem('token') || '').length")
            cur_url = self._zai_page.url
            if textarea and token_len > 100 and '/auth' not in cur_url:
                logger.info(f"[Zai] Login recovered! (waited {waited}s)")
                new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                if new_token and len(new_token) > 100:
                    self._save_zai_token(new_token)
                break
            if '/auth' not in cur_url and token_len > 0:
                logger.info(f"[Zai] Login detected via URL change (waited {waited}s)")
                new_token = await self._zai_page.evaluate("localStorage.getItem('token')")
                if new_token and len(new_token) > 100:
                    self._save_zai_token(new_token)
                break
            if waited >= min_wait and waited % 30 == 0:
                logger.info(f"[Zai] still waiting for login... ({waited}s elapsed)")
        else:
            logger.warning(f"[Zai] Login timed out after {waited}s during recovery.")

    async def _rebuild_headless_after_verification(self):
        """在手动验证完成后，重建 headless 浏览器实例。"""
        logger.info("[Zai] _rebuild_headless_after_verification: Closing non-headless browser...")
        if self._zai_browser:
            try:
                await self._zai_browser.close()
            except Exception as e:
                logger.warning(f"[Zai] Error closing browser during headless rebuild: {e}")
            self._zai_browser = None
            self._zai_page = None
            self._zai_pw = None

        from playwright.async_api import async_playwright
        logger.info("[Zai] _rebuild_headless_after_verification: Launching new headless browser...")
        self._zai_pw = await async_playwright().start()
        _args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        _args.append("--headless=new")  # 确保是 headless
        self._zai_browser = await self._zai_pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(BASE_DIR, "zai_profile"),
            headless=True,  # 强制 headless
            channel=_browser_channel(),
            args=_args,
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._zai_page = self._zai_browser.pages[0] if self._zai_browser.pages else await self._zai_browser.new_page()

        await self._zai_page.add_init_script(ZAI_INIT_SCRIPT)
        logger.info("[Zai] _rebuild_headless_after_verification: Navigating to z.ai/...")
        await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
        await self._dismiss_zai_popups()

        # 验证 token 是否仍然有效（应该已在 profile 中）
        token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
        if token and len(token) > 100:
            self._save_zai_token(token)
            logger.info(f"[Zai] Token found after headless rebuild ({len(token)} chars).")
        else:
            logger.warning("[Zai] Token NOT found after headless rebuild. Trying backup...")
            backup_token = self._load_zai_token_backup()
            if backup_token:
                await self._zai_page.evaluate("(token) => { localStorage.setItem('token', token); }", backup_token)
                await self._zai_page.reload(wait_until="domcontentloaded", timeout=60000)
                await self._dismiss_zai_popups()
                final_token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
                if final_token and len(final_token) > 100:
                    self._save_zai_token(final_token)
                    logger.info(f"[Zai] Token restored from backup after headless rebuild ({len(final_token)} chars).")
                else:
                    logger.error("[Zai] Failed to restore token from backup after headless rebuild.")
            else:
                logger.error("[Zai] No token found or restored after headless rebuild.")

    async def fetch_zai_models(self) -> list[dict]:
        """从 Zai 页面模型选择器中获取可用模型列表。"""
        headless = CONFIG.get('_zai_headless', True)
        await self.ensure_zai_ready(headless=headless)
        page = self._zai_page
        if not page:
            return []

        models = []
        try:
            btn = page.locator('.modelSelectorButton, button[aria-label="选择一个模型"]').first
            if await btn.count() == 0:
                logger.warning("[Zai] Model selector button not found")
                return []
            await btn.click()
            await asyncio.sleep(1.5)

            model_list = await page.evaluate("""() => {
                const models = [];
                const items = document.querySelectorAll('[role="option"], [data-value], [class*="modelItem"], [class*="selectorItem"]');
                if (items.length > 0) {
                    for (const item of items) {
                        const name = (item.textContent || '').trim();
                        const dataValue = item.getAttribute('data-value') || '';
                        models.push({ name, dataValue });
                    }
                }
                if (models.length === 0) {
                    const popover = document.querySelector('[class*="z-40"], [class*="z-50"], [class*="popover"], [class*="dropdown"]');
                    if (popover) {
                        const buttons = popover.querySelectorAll('button, [role="option"], [tabindex]');
                        for (const btn of buttons) {
                            const name = (btn.textContent || '').split('NEW')[0].split('  ')[0].trim();
                            if (name && name.length > 1 && name.length < 40 && !name.includes('模式') && !name.includes('聊天') && !name.includes('幻灯片')) {
                                models.push({ name });
                            }
                        }
                    }
                }
                return models;
            }""")

            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            for m in model_list:
                name = m.get('name', '').strip()
                model_id = "zai-" + name.lower().replace(" ", "-").replace(".", "-")
                models.append({"display_name": name, "model_id": model_id})
            logger.info(f"[Zai] Fetched models: {[m['display_name'] for m in models]}")

        except Exception as e:
            logger.error(f"[Zai] Failed to fetch models: {e}")
        return models

    async def select_zai_model(self, model_name: str) -> bool:
        """在 Zai 页面上点击模型选择器中的目标模型。

        Args:
            model_name: 模型ID（如 'zai-glm-5.1', 'zai-glm-5.2'），会自动映射为页面显示名称。
        """
        headless = CONFIG.get('_zai_headless', True)
        await self.ensure_zai_ready(headless=headless)
        page = self._zai_page
        if not page:
            return False

        from models import ZAI_MODEL_CONFIG
        cfg = ZAI_MODEL_CONFIG.get(model_name, {})
        display_name = cfg.get("display_name", model_name.replace("zai-", ""))

        try:
            # 1. 读取当前选中的模型
            current = await page.evaluate("""() => {
                const btn = document.querySelector('.modelSelectorButton, button[aria-label="选择一个模型"]');
                return btn ? btn.textContent.trim() : null;
            }""")

            logger.info(f"[Zai] Current model: '{current}', target: '{display_name}'")

            if current and (display_name in current or current in display_name):
                logger.info(f"[Zai] Already on model: {current}")
                return True

            # 2. 点击模型选择器按钮
            btn = page.locator('.modelSelectorButton, button[aria-label="选择一个模型"]').first
            if await btn.count() == 0:
                logger.warning("[Zai] Model selector button not found")
                return False

            # 等待按钮可用（z.ai 生成回复时该按钮会 disabled）
            for _ in range(120):  # 最多等 60 秒
                try:
                    disabled = await page.evaluate("""() => {
                        const btn = document.querySelector('.modelSelectorButton, button[aria-label="选择一个模型"]');
                        return btn ? (btn.disabled || btn.hasAttribute('disabled') || btn.getAttribute('data-disabled') !== null) : true;
                    }""")
                    if not disabled:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            else:
                logger.warning("[Zai] Model selector button stayed disabled for 60s")
                return False

            await btn.click()
            await asyncio.sleep(1.5)

            # 3. 在下拉面板中点击目标模型
            clicked = await page.evaluate(f"""() => {{
                // 尝试通过 data-value 精确匹配
                const exactBtn = document.querySelector('[data-value="{display_name}"]');
                if (exactBtn) {{
                    exactBtn.click();
                    return '{display_name}';
                }}
                // 尝试文本匹配
                const allBtns = document.querySelectorAll('button, [role="option"], [tabindex]');
                for (const btn of allBtns) {{
                    const txt = (btn.textContent || '').split('NEW')[0].trim();
                    if (txt === '{display_name}' || txt.includes('{display_name}')) {{
                        btn.click();
                        return txt;
                    }}
                }}
                return null;
            }}""")

            if clicked:
                logger.info(f"[Zai] Model switched to: {clicked}")
                await asyncio.sleep(1)
                return True
            else:
                logger.warning(f"[Zai] Model '{display_name}' not found in selector")
                await page.keyboard.press("Escape")
                return False

        except Exception as e:
            logger.error(f"[Zai] Failed to select model: {e}")
            return False

    async def stream_zai_chat(self, prompt: str, model_type: str = "glm-4.7", thinking_enabled: bool = False, search_enabled: bool = True, inline_file_content: str | None = None, model_name: str | None = None, reuse_conversation: bool = False, conversation_id: str | None = None):
        """z.ai 流式对话：先上传文件等待解析完成，再创建聊天 + SSE 流式解析。

        Args:
            model_name: 模型ID（如 'zai-glm-5.1', 'zai-glm-5.2'），用于在页面模型选择器中切换模型。
        """
        headless = CONFIG.get('_zai_headless', True)
        await self.ensure_zai_ready(headless=headless)
        await self._dismiss_zai_popups()
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._zai_queues[stream_id] = q
        session_id = ""
        uploaded_file_id: str | None = None

        # 队列通过 stream_id 索引，避免并发覆盖
        self._zai_active_stream = stream_id

        # 注册 JS→Python 桥接函数（仅注册一次）
        if not getattr(self, "_zai_bridge_registered", False):
            async def _stable_sse_callback(chunk_json: str):
                active_stream = getattr(self, "_zai_active_stream", None)
                target_q = self._zai_queues.get(active_stream)
                if target_q is None:
                    return
                try:
                    chunk_str = str(chunk_json).strip()
                    data = json.loads(chunk_str)
                    event_type = data.get("type", "")
                    event_data = data.get("data", {})

                    if event_type == "chat:completion":
                        delta = event_data.get("delta_content", "")
                        phase = event_data.get("phase", "answer")
                        if delta and phase != "thinking":
                            # 过滤掉 "思考过程" 前缀（z.ai 可能将思考过程和答案拼接）
                            delta = delta.replace("思考过程", "").replace("reasoning", "").strip()
                            if delta:
                                target_q.put_nowait(("chunk", delta))
                        elif not delta:
                            pass
                    elif event_type == "chat:completion:done":
                        target_q.put_nowait(("done", ""))
                    elif event_type == "chat:completion:error":
                        target_q.put_nowait(("error", event_data.get("message", "z.ai error")))
                        target_q.put_nowait(("done", ""))
                    else:
                        if event_data.get("done"):
                            target_q.put_nowait(("done", ""))
                        else:
                            logger.debug(f"[Zai] SSE callback: event_type={event_type}, data={event_data}")
                except Exception as e:
                    logger.debug(f"[Zai] sse chunk parse: {e}")

            try:
                # 先注册桥接函数
                await self._zai_page.expose_function("zaiOnSseChunk", _stable_sse_callback)
                self._zai_bridge_registered = True

                #  drains 缓冲区中积累的事件（页面加载时拦截器可能已经捕获了事件）
                try:
                    buffered = await self._zai_page.evaluate("""() => {
                        const events = window.__zai_sse_events || [];
                        window.__zai_sse_events = [];
                        window.__zai_sse_flushed = true;
                        return events;
                    }""")
                    if buffered:
                        logger.info(f"[Zai] draining {len(buffered)} buffered SSE events")
                        for event_json in buffered:
                            try:
                                await _stable_sse_callback(event_json)
                            except Exception as e:
                                logger.debug(f"[Zai] drain event error: {e}")
                except Exception as e:
                    logger.debug(f"[Zai] drain buffered events: {e}")
            except (ValueError, AttributeError):
                self._zai_bridge_registered = True

        # 0. 模型切换：如果提供了 model_name，先切换到目标模型
        if model_name:
            await self.select_zai_model(model_name)
            await asyncio.sleep(1)

        # 1. 从URL捕获session_id
        try:
            url = self._zai_page.url
            if '/c/' in url:
                session_id = url.split('/c/')[1].split('?')[0].split('#')[0]
                logger.info(f"[Zai] initial session_id: {session_id}")
        except Exception:
            pass

        # 2. 文件上传（通过隐藏的 input[type=file] + 路由拦截获取完整文件信息）
        uploaded_file_info = {}

        async def upload_file() -> bool:
            nonlocal uploaded_file_info
            if not inline_file_content:
                return True

            file_future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

            async def handle_upload_route(route):
                try:
                    resp = await route.fetch()
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if not file_future.done():
                                file_future.set_result(data)
                                logger.info(f"[Zai] upload API returned file data: id={data.get('id')}")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[Zai] upload route error: {e}")
                finally:
                    try:
                        await route.fulfill()
                    except Exception:
                        pass

            await self._zai_page.route("**/api/v1/files/**", handle_upload_route)

            result = await self._zai_page.evaluate("""async (content) => {
                try {
                    const blob = new Blob([content], { type: 'text/plain' });
                    const file = new File([blob], 'request.txt', { type: 'text/plain', lastModified: Date.now() });
                    const fileInput = document.querySelector('input[type="file"]');
                    if (!fileInput) return { error: 'no file input found' };
                    const dt = new DataTransfer();
                    dt.items.add(file);
                    fileInput.files = dt.files;
                    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
                    return { dispatched: true };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", inline_file_content)

            if result.get("error"):
                logger.warning(f"[Zai] file upload JS error: {result['error']}")
                await self._zai_page.unroute("**/api/v1/files/**", handle_upload_route)
                return False

            try:
                data = await asyncio.wait_for(file_future, timeout=60)
                uploaded_file_info = data
                logger.info(f"[Zai] file upload complete, file_id={data.get('id')}")
                return True
            except asyncio.TimeoutError:
                logger.warning("[Zai] file upload timeout (no file response received)")
                return False
            finally:
                await self._zai_page.unroute("**/api/v1/files/**", handle_upload_route)

        # 3. 执行文件上传（如果需要） — 仅在新会话且提供文件内容时上传
        if inline_file_content and not reuse_conversation:
            logger.info("[Zai] starting file upload...")
            upload_ok = await upload_file()
            if not upload_ok:
                logger.warning("[Zai] file upload failed, proceeding without file")
            else:
                logger.info(f"[Zai] file upload complete, file_id={uploaded_file_info.get('id')}")

        # 4. 让页面前端发送消息，我们拦截SSE响应
        async def call_zai_api():
            nonlocal session_id

            prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)

            # 4a. 切换思考和搜索开关
            try:
                await self._zai_page.evaluate("""({thinking, search}) => {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.dataset.autoThink !== undefined) {
                            const cur = b.dataset.autoThink === 'true';
                            if (thinking !== cur) b.click();
                        }
                        if (b.dataset.autoSearch !== undefined) {
                            const cur = b.dataset.autoSearch === 'true';
                            if (search !== cur) b.click();
                        }
                    }
                }""", {"thinking": thinking_enabled, "search": search_enabled})
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"[Zai] thinking toggle failed (non-critical): {e}")

            # 4b. 注意：fetch 拦截器已在 add_init_script 中注入（页面加载前）

            # 4c. 通过Svelte store设置文件信息（如果有），然后填入消息并回车
            file_set = False
            if uploaded_file_info and uploaded_file_info.get('id'):
                fid = uploaded_file_info['id']
                meta = uploaded_file_info.get('meta', {})
                file_set = await self._zai_page.evaluate("""({fid, meta}) => {
                    try {
                        // 通过window对象找到Svelte store并设置文件
                        const fileObj = {
                            id: fid,
                            type: 'file',
                            name: meta.name || 'request.txt',
                            url: '/api/v1/files/' + fid,
                            status: 'uploaded',
                            size: meta.size || 0,
                            file: { id: fid, meta: meta }
                        };
                        // 尝试通过input事件触发文件添加
                        const input = document.querySelector('input[type="file"]');
                        if (input) {
                            const dt = new DataTransfer();
                            // 如果已有文件则保留
                            if (input.files) {
                                for (const f of input.files) dt.items.add(f);
                            }
                            // 创建新File对象
                            const blob = new Blob([''], {type:'text/plain'});
                            const file = new File([blob], meta.name || 'request.txt', {type:'text/plain'});
                            dt.items.add(file);
                            input.files = dt.files;
                            input.dispatchEvent(new Event('change', {bubbles:true}));
                            return true;
                        }
                        return false;
                    } catch(e) {
                        console.error('[zai-file-set]', e);
                        return false;
                    }
                }""", {"fid": fid, "meta": meta})
                if file_set:
                    logger.info(f"[Zai] file info set via input")
                    await asyncio.sleep(2)

            # 4d. 处理弹窗 → 确认textarea → 填入消息并发送
            logger.info(f"[Zai] dismissing popups before entering message...")
            
            # 多次处理弹窗，确保页面元素可访问（减少次数：内部已有超时保护）
            for i in range(2):
                logger.debug(f"[Zai] dismiss popups attempt {i+1}/2")
                await self._dismiss_zai_popups()
                await asyncio.sleep(0.3)
            
            logger.info(f"[Zai] dismiss popups done, checking page state...")
            
            # 确认页面已加载textarea（如果不在主页则导航）
            cur_url = self._zai_page.url
            logger.info(f"[Zai] current URL: {cur_url}")
            if '/auth' in cur_url:
                logger.warning("[Zai] Still on /auth, navigating to main page...")
                await self._zai_page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=60000)
                for _ in range(3):
                    await self._dismiss_zai_popups()
                    await asyncio.sleep(0.5)
            
            # 等待textarea出现
            logger.info(f"[Zai] waiting for textarea selector...")
            try:
                await self._zai_page.wait_for_selector("textarea", timeout=15000)
                logger.info(f"[Zai] textarea found")
            except:
                logger.warning("[Zai] textarea not found, trying dismiss popups and reload...")
                for _ in range(3):
                    await self._dismiss_zai_popups()
                    await asyncio.sleep(0.5)
                await self._zai_page.reload(wait_until="domcontentloaded", timeout=60000)
                for _ in range(3):
                    await self._dismiss_zai_popups()
                    await asyncio.sleep(0.5)
                try:
                    await self._zai_page.wait_for_selector("textarea", timeout=15000)
                except:
                    logger.error("[Zai] textarea still not found after reload")
                    raise Exception("z.ai textarea not found")

            # 输入消息：短文本逐字输入（更像真人），长文本直接粘贴（避免超时）
            import random as _random
            textarea = self._zai_page.locator("textarea")
            await textarea.click()
            await asyncio.sleep(0.3 + _random.random() * 0.5)
            
            # 清空现有内容（如果有）
            await self._zai_page.keyboard.press("Control+A")
            await asyncio.sleep(0.1)
            await self._zai_page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)

            if len(prompt_text) > 200:
                # 长文本：直接粘贴
                await self._zai_page.evaluate("(text) => { const ta = document.querySelector('textarea'); ta.focus(); const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set; setter.call(ta, text); ta.dispatchEvent(new Event('input', {bubbles:true})); }", prompt_text)
                logger.info(f"[Zai] pasted prompt ({len(prompt_text)} chars)")
                await asyncio.sleep(0.5)
            else:
                # 短文本：逐字输入
                for ch in prompt_text:
                    await self._zai_page.keyboard.type(ch, delay=50 + int(100 * _random.random()))
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.5)

            # 发送：按 Enter 或点击发送按钮
            send_clicked = await self._zai_page.evaluate("""() => {
                // 尝试按 Enter（如果表单允许）
                const form = document.querySelector('form');
                if (form) {
                    const ev = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles:true});
                    const textarea = document.querySelector('textarea');
                    if (textarea) textarea.dispatchEvent(ev);
                    return 'form-enter';
                }
                // 否则点击发送按钮
                const btns = Array.from(document.querySelectorAll('button'));
                const sendBtn = btns.find(b => {
                    const txt = (b.textContent || '').trim();
                    return txt === '' || txt === '发送' || b.getAttribute('aria-label')?.includes('send') || b.querySelector('[class*="send"]');
                }) || btns[0];
                if (sendBtn) {
                    sendBtn.click();
                    return 'button-click';
                }
                return 'none';
            }""")
            logger.info(f"[Zai] sent message via: {send_clicked}")
            await asyncio.sleep(0.5)

            # 4e. 等待SSE数据通过zaiOnSseChunk桥接到Python队列（最多2分钟）
            logger.info(f"[Zai] waiting for SSE response...")
            # session_id 从页面URL获取
            try:
                url = self._zai_page.url
                if '/c/' in url:
                    session_id = url.split('/c/')[1].split('?')[0].split('#')[0]
            except Exception:
                pass

        # 5. 执行 API 调用（文件已上传）
        await call_zai_api()

        # 6. yield session_id
        if session_id:
            yield ("session_id", session_id)
        

        # 7. 从 SSE 队列消费流式响应（主路径）
        logger.info("[Zai] consuming SSE queue for response content...")
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        total_timeout = 120  # seconds
        sse_seen = False

        while True:
            if loop.time() - start_time > total_timeout:
                logger.warning("[Zai] total timeout exceeded")
                yield ("error", "z.ai response timeout")
                yield ("done", "")
                break

            try:
                q_item = await asyncio.wait_for(q.get(), timeout=2.0)
                sse_seen = True
                event_type, content = q_item

                if event_type == "chunk":
                    if content.strip():
                        yield ("chunk", content)
                elif event_type == "done":
                    logger.info("[Zai] SSE stream done")
                    yield ("done", "")
                    break
                elif event_type == "error":
                    logger.error(f"[Zai] SSE stream error: {content}")
                    yield ("error", content)
                    yield ("done", "")
                    break
                else:
                    logger.debug(f"[Zai] SSE unexpected event type: {event_type}")
            except asyncio.TimeoutError:
                # If SSE never started after 30s, fallback to DOM polling as last resort
                if not sse_seen and loop.time() - start_time > 30:
                    logger.warning("[Zai] no SSE events after 30s, using DOM fallback")
                    try:
                        # Single DOM snapshot as fallback
                        result = await self._zai_page.evaluate("""() => {
                            const all = document.querySelectorAll('[class*="markdown-prose"]');
                            const candidates = [];
                            for (const el of all) {
                                const cls = (el.className || '').toLowerCase();
                                if (cls.includes('chat-user')) continue;
                                const txt = (el.textContent || '').trim();
                                if (txt.length > 0) candidates.push(txt);
                            }
                            return candidates.length > 0 ? candidates[0] : '';
                        }""")
                        if result:
                            yield ("chunk", result)
                        yield ("done", "")
                        break
                    except Exception as e:
                        logger.debug(f"[Zai] DOM fallback error: {e}")
                        yield ("error", "z.ai response could not be retrieved")
                        yield ("done", "")
                        break
                # otherwise keep waiting for SSE
                continue
        # Cleanup
        self._zai_queues.pop(stream_id, None)
        self._zai_active_stream = None


    async def get_zai_session_id(self) -> str:
        """从 z.ai 页面 URL 提取当前会话 ID。"""
        try:
            if not self._zai_page:
                return ""
            url = self._zai_page.url
            if '/c/' in url:
                sid = url.split('/c/')[1]
                sid = sid.split('?')[0].split('#')[0]
                if sid:
                    return sid
            return ""
        except Exception:
            return ""

    async def delete_zai_conversation(self, session_id: str):
        """删除单个 z.ai 会话。DELETE /api/v1/chats body={chat_ids:[id]}"""
        if not session_id:
            return
        try:
            result = await self._zai_page.evaluate("""async (params) => {
                const [token, sid] = params;
                try {
                    const resp = await fetch('/api/v1/chats', {
                        method: 'DELETE',
                        headers: {
                            'Authorization': `Bearer ${token}`,
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ chat_ids: [sid] })
                    });
                    const text = await resp.text();
                    return { ok: resp.ok, status: resp.status, body: text.substring(0, 200) };
                } catch (e) {
                    return { ok: false, error: String(e) };
                }
            }""", [await self._zai_page.evaluate("localStorage.getItem('token') || ''"), session_id])
            if result.get('ok'):
                logger.info(f"[Zai] deleted session {session_id}")
            else:
                logger.warning(f"[Zai] delete failed: {json.dumps(result, ensure_ascii=False)[:200]}")
        except Exception as e:
            logger.warning(f"[Zai] delete exception: {e}")

    async def delete_all_zai_conversations(self):
        """删除所有 z.ai 会话：先列出再批量删除。"""
        try:
            if not self._zai_page or self._zai_page.is_closed():
                return
            token = await self._zai_page.evaluate("localStorage.getItem('token') || ''")
            if not token:
                logger.warning("[Zai] no token, cannot delete conversations")
                return
            result = await self._zai_page.evaluate("""async (tok) => {
                try {
                    // 步骤1: 列出所有会话
                    const listResp = await fetch('/api/v1/chats', {
                        headers: { 'Authorization': `Bearer ${tok}`, 'Content-Type': 'application/json' }
                    });
                    const chats = await listResp.json();
                    if (!chats || chats.length === 0) return { deleted: 0 };

                    // 步骤2: 批量删除
                    const ids = chats.map(c => c.id);
                    const delResp = await fetch('/api/v1/chats', {
                        method: 'DELETE',
                        headers: { 'Authorization': `Bearer ${tok}`, 'Content-Type': 'application/json' },
                        body: JSON.stringify({ chat_ids: ids })
                    });
                    return { deleted: ids.length, ok: delResp.ok, body: (await delResp.text()).substring(0, 100) };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", token)
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            logger.info(f"[Zai] delete_all: deleted {deleted} sessions")
        except Exception as e:
            logger.warning(f"[Zai] delete_all exception: {e}")

    async def close_zai(self):
        """Close Zai browser context and cleanup resources."""
        if self._zai_page and not self._zai_page.is_closed():
            try:
                await self._zai_page.close()
                logger.info("[Zai] page closed")
            except Exception as e:
                logger.warning(f"[Zai] close page error: {e}")
        self._zai_page = None
        self._zai_bridge_registered = False
        self._zai_queues.clear()
        logger.info("[Zai] resources cleaned up")

    # ═══════════════════════════════════════════════════════════════════════
    # MiMo 浏览器操作
    # ═══════════════════════════════════════════════════════════════════════

    async def fetch_mimo_models(self) -> list[dict]:
        """从 MiMo 页面模型下拉面板中获取可用模型列表。"""
        headless = CONFIG.get('_mimo_headless', True)
        await self.ensure_mimo_ready(headless=headless)
        page = self._mimo_page
        if not page:
            return []

        models = []
        try:
            clicked = await page.evaluate("""() => {
                const all = document.querySelectorAll('div');
                for (const el of all) {
                    const text = (el.textContent || '').trim();
                    const cls = el.className || '';
                    const rect = el.getBoundingClientRect();
                    if (rect.top < 60 && rect.height > 0 && rect.height < 40 &&
                        cls.includes('cursor-pointer') &&
                        (text.includes('MiMo') || text.includes('V2.5') || text.includes('Pro'))) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            if not clicked:
                logger.warning("[MiMo] Model selector trigger not found")
                return []
            await asyncio.sleep(1.5)

            model_list = await page.evaluate("""() => {
                const models = [];
                const btns = document.querySelectorAll('button[data-track-id^="model_selector_"]');
                for (const btn of btns) {
                    const name = btn.getAttribute('data-track-name') || '';
                    const isActive = btn.className.includes('bg-mimo-fill-neutral-active');
                    const descEl = btn.querySelector('.text-mimo-text-h3-placeholder');
                    const desc = descEl ? descEl.textContent.trim() : '';
                    models.push({ name, desc, isActive });
                }
                return models;
            }""")

            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            for m in model_list:
                model_id = "mimo-" + m['name'].lower().replace(".", "-").replace(" ", "-")
                models.append({"display_name": m['name'], "model_id": model_id, "desc": m['desc'], "is_active": m['isActive']})
            logger.info(f"[MiMo] Fetched models: {[m['display_name'] for m in models]}")

        except Exception as e:
            logger.error(f"[MiMo] Failed to fetch models: {e}")
        return models

    async def select_mimo_model(self, model_name: str) -> bool:
        """在 MiMo 页面顶部的模型下拉面板中选择目标模型。"""
        headless = CONFIG.get('_mimo_headless', True)
        await self.ensure_mimo_ready(headless=headless)
        page = self._mimo_page
        if not page:
            return False

        from models import MIMO_MODEL_CONFIG
        cfg = MIMO_MODEL_CONFIG.get(model_name, {})
        display_name = cfg.get("display_name", model_name.replace("mimo-", "MiMo-").replace("-", "."))

        try:
            # 1. 读取当前选中的模型
            current = await page.evaluate("""() => {
                const nav = document.querySelector('nav, [class*="navbar"], [class*="nav"]');
                if (nav) {
                    const divs = nav.querySelectorAll('div[class*="cursor-pointer"]');
                    for (const d of divs) {
                        const txt = (d.textContent || '').trim();
                        if (txt.includes('MiMo') || txt.includes('V2')) {
                            return txt.replace('New', '').replace(/\\s+/g, ' ').trim();
                        }
                    }
                }
                return null;
            }""")
            logger.info(f"[MiMo] Current model: '{current}', target: '{display_name}'")

            if current and display_name in current:
                logger.info(f"[MiMo] Already on model: {current}")
                return True

            # 2. 用 Playwright 原生定位器点击触发器
            logger.info(f"[MiMo] Clicking model selector trigger for: {display_name}")
            trigger = page.locator('nav div[class*="cursor-pointer"]').filter(has_text="MiMo").first
            if await trigger.count() == 0:
                logger.warning("[MiMo] Model selector trigger not found")
                return False
            await trigger.click()
            await asyncio.sleep(1.5)

            # 3. 用 Playwright 定位器查找下拉面板中的目标模型
            # data-track-name 在 button 或 div 上
            target_el = page.locator(f'[data-track-name="{display_name}"]').first
            if await target_el.count() > 0:
                await target_el.click()
                logger.info(f"[MiMo] Model switched to: {display_name}")
                await asyncio.sleep(1)
                return True

            # 模糊匹配
            all_options = page.locator('[data-dropdown-menu] [data-track-name]')
            count = await all_options.count()
            available = []
            for i in range(count):
                el = all_options.nth(i)
                name = await el.get_attribute("data-track-name")
                available.append(name)
                if display_name in name or name in display_name:
                    await el.click()
                    logger.info(f"[MiMo] Model switched to: {name}")
                    await asyncio.sleep(1)
                    return True

            logger.warning(f"[MiMo] Model '{display_name}' not found. Available: {available}")
            await page.keyboard.press("Escape")
            return False

        except Exception as e:
            logger.error(f"[MiMo] Failed to select model: {e}")
            return False

    async def ensure_mimo_ready(self, headless: bool = True):
        """确保 MiMo 浏览器已启动并登录。"""
        if self._mimo_page and not self._mimo_page.is_closed():
            return

        logger.info("[MiMo] Starting MiMo browser...")
        from playwright.async_api import async_playwright
        self._mimo_pw = await async_playwright().start()
        self._mimo_browser = await self._mimo_pw.chromium.launch_persistent_context(
            user_data_dir=self._mimo_user_data_dir,
            headless=headless,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._mimo_page = self._mimo_browser.pages[0] if self._mimo_browser.pages else await self._mimo_browser.new_page()

        await self._mimo_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        logger.info("[MiMo] navigating to aistudio.xiaomimimo.com...")
        await self._mimo_page.goto("https://aistudio.xiaomimimo.com/#/c", wait_until="domcontentloaded", timeout=60000)
        # 等待 textarea 出现（说明 React SPA 已渲染完成）
        try:
            await self._mimo_page.wait_for_selector("textarea", timeout=30000)
            logger.info("[MiMo] textarea found, page rendered")
        except:
            logger.warning("[MiMo] textarea not found after 30s, continuing anyway")
            await asyncio.sleep(3)

        try:
            await self._mimo_page.locator('button[aria-label="关闭公告"]').first.click(timeout=3000)
            await asyncio.sleep(1)
        except:
            pass

        await self._dismiss_mimo_popups()

        ph = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
        if ph and '登录' in ph:
            logger.warning("[MiMo] Not logged in, trying auto-login button first...")
            # 尝试点击页面上的登录按钮（如果存在）
            clicked = await self._try_mimo_auto_login()
            if clicked:
                await asyncio.sleep(3)
                ph2 = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
                if ph2 and '登录' not in ph2:
                    logger.info("[MiMo] Auto-login succeeded")
                    # 登录成功，继续后续流程
                else:
                    logger.info("[MiMo] Auto-login button click did not result in login")
            else:
                logger.info("[MiMo] No auto-login button found")

            # 再次检查登录状态
            ph3 = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
            if ph3 and '登录' in ph3:
                logger.warning("[MiMo] Still not logged in, showing browser for manual login")
                if headless:
                    await self._mimo_login_recovery()
        else:
            logger.info("[MiMo] Logged in, textarea placeholder: " + (ph or "N/A"))

        logger.info("[MiMo] MiMo browser ready")

    async def _try_mimo_auto_login(self) -> bool:
        """尝试自动点击 MiMo 页面上的登录按钮。返回是否找到了并点击了登录按钮。"""
        if not self._mimo_page or self._mimo_page.is_closed():
            return False
        try:
            return await self._mimo_page.evaluate(r"""() => {
                const loginPatterns = ['登录', '登 录', 'Login', 'Sign in', 'Log in'];
                const allBtns = document.querySelectorAll('button, a, [role="button"], [class*="login"], [class*="Login"]');
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim();
                    const ariaLabel = btn.getAttribute('aria-label') || '';
                    const href = btn.getAttribute('href') || '';
                    if (loginPatterns.some(p => txt.includes(p) || ariaLabel.includes(p))) {
                        if (txt.length < 20 && btn.getBoundingClientRect().height > 0) {
                            btn.click();
                            return true;
                        }
                    }
                    if (href && loginPatterns.some(p => href.toLowerCase().includes(p.toLowerCase()))) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
        except Exception as e:
            logger.debug(f"[MiMo] auto-login attempt error: {e}")
            return False

    async def _dismiss_mimo_popups(self):
        """处理 MiMo 页面上的各种弹窗：关闭公告、同意协议、确认提示等。"""
        if not self._mimo_page or self._mimo_page.is_closed():
            return
        try:
            await self._mimo_page.evaluate(r"""() => {
                // 1. 关闭公告弹窗
                const closeBtns = document.querySelectorAll('button[aria-label="关闭公告"], button[aria-label="Close"], [class*="close"][class*="announcement"], [data-track-id*="close"]');
                for (const btn of closeBtns) {
                    try { btn.click(); } catch(e) {}
                }
                // 2. 同意/接受/确定 按钮
                const agreePatterns = ['同意', '接受', '确定', '确认', ' Agree', 'Accept', 'OK', 'Confirm', 'Got it', '知道了', '我已知晓'];
                const allBtns = document.querySelectorAll('button, [role="button"], a[role="button"]');
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim();
                    const cls = btn.className || '';
                    if (agreePatterns.some(p => txt.includes(p)) || btn.getAttribute('aria-label') === '同意') {
                        if (cls.includes('primary') || cls.includes('confirm') || cls.includes('agree') || cls.includes('submit')) {
                            try { btn.click(); } catch(e) {}
                        }
                    }
                }
                // 3. 关闭遮罩层弹窗（点击遮罩关闭）
                const overlays = document.querySelectorAll('[class*="overlay"], [class*="mask"], [class*="backdrop"]');
                for (const overlay of overlays) {
                    if (overlay.style && overlay.onclick === null) {
                        try { overlay.click(); } catch(e) {}
                    }
                }
                // 4. Escape 关闭模态弹窗
            }""")
            await self._mimo_page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"[MiMo] dismiss popups: {e}")

    async def _dismiss_zai_popups(self):
        """处理 Zai 页面上的各种弹窗：关闭公告、同意协议、确认提示等。"""
        if not self._zai_page or self._zai_page.is_closed():
            return
        try:
            # 多次尝试处理弹窗（弹窗可能延迟加载）
            for attempt in range(3):
                # 1. 自动点击登录按钮（如果页面显示登录提示）
                try:
                    await asyncio.wait_for(self._zai_page.evaluate(r"""() => {
                        const loginPatterns = ['登录', '登 录', 'Login', 'Sign in', 'Log in', '登录/注册', 'Sign up'];
                        const allBtns = document.querySelectorAll('button, a, [role="button"], [class*="login"], [class*="Login"]');
                        for (const btn of allBtns) {
                            const txt = (btn.textContent || '').trim();
                            const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                            const cls = (btn.className || '').toLowerCase();
                            if (loginPatterns.some(p => txt.includes(p) || ariaLabel.includes(p.toLowerCase()))) {
                                if (txt.includes('注册') && !txt.includes('登录')) continue;
                                if (txt.includes('取消') || txt.includes('关闭') || txt.includes('Cancel') || txt.includes('Close')) continue;
                                try { btn.click(); return true; } catch(e) {}
                            }
                        }
                        return false;
                    }"""), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.debug(f"[Zai] dismiss popups step1 timeout (attempt {attempt+1})")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups step1 error: {e}")

                # 2. 关闭按钮、同意按钮、遮罩层、弹窗点击
                try:
                    await asyncio.wait_for(self._zai_page.evaluate(r"""() => {
                        const closeSelectors = [
                            'button[aria-label="关闭"]', 'button[aria-label="Close"]',
                            'button[aria-label="关闭公告"]', '[class*="close"]', '[data-testid*="close"]',
                            '[class*="modal"] button:first-child', '[class*="dialog"] button:first-child'
                        ];
                        for (const sel of closeSelectors) {
                            document.querySelectorAll(sel).forEach(btn => {
                                try { if (btn.offsetParent !== null) btn.click(); } catch(e) {}
                            });
                        }
                        const agreePatterns = ['同意', '接受', '确定', '确认', 'Agree', 'Accept', 'OK', 'Confirm', 
                                              'Got it', '知道了', '我已知晓', '我再想想', '继续', 'Continue'];
                        document.querySelectorAll('button, [role="button"], a[role="button"]').forEach(btn => {
                            const txt = (btn.textContent || '').trim();
                            const cls = (btn.className || '').toLowerCase();
                            if (agreePatterns.some(p => txt === p || txt.includes(p))) {
                                if (cls.includes('primary') || cls.includes('confirm') || cls.includes('agree') || 
                                    cls.includes('submit') || cls.includes('bg-') || cls.includes('solid')) {
                                    try { if (btn.offsetParent !== null) btn.click(); } catch(e) {}
                                }
                            }
                        });
                        document.querySelectorAll('[class*="overlay"], [class*="mask"], [class*="backdrop"], [class*="modal-overlay"]').forEach(el => {
                            if (el.style && el.onclick === null && el.offsetParent !== null) {
                                try { el.click(); } catch(e) {}
                            }
                        });
                        document.querySelectorAll('[class*="modal"], [class*="dialog"], [class*="popup"]').forEach(el => {
                            if (el.style && el.style.display !== 'none' && el.onclick === null) {
                                const hasContent = el.querySelector('button, input, [class*="content"]');
                                if (!hasContent) { try { el.click(); } catch(e) {} }
                            }
                        });
                    }"""), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.debug(f"[Zai] dismiss popups step2 timeout (attempt {attempt+1})")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups step2 error: {e}")

                # 3. 按 Escape 键
                try:
                    await self._zai_page.keyboard.press("Escape")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups Escape error: {e}")
                await asyncio.sleep(0.5)

                # 4. 检查是否还有弹窗
                try:
                    has_modal = await asyncio.wait_for(self._zai_page.evaluate("""() => {
                        const modals = document.querySelectorAll('[class*="modal"], [class*="dialog"], [class*="popup"]');
                        for (const m of modals) {
                            if (m.style && m.style.display !== 'none' && m.offsetParent !== null && m.style.zIndex > 100) {
                                return true;
                            }
                        }
                        return false;
                    }"""), timeout=5.0)
                    if not has_modal:
                        break
                except asyncio.TimeoutError:
                    logger.debug(f"[Zai] dismiss popups has_modal timeout (attempt {attempt+1})")
                except Exception as e:
                    logger.debug(f"[Zai] dismiss popups has_modal error: {e}")
        except Exception as e:
            logger.debug(f"[Zai] dismiss popups error: {e}")
    async def _dismiss_deepseek_popups(self):
        """处理 DeepSeek 页面上的各种弹窗。"""
        if not self._deepseek_page or self._deepseek_page.is_closed():
            return
        try:
            await self._deepseek_page.evaluate(r"""() => {
                const closeBtns = document.querySelectorAll('[class*="close"], [aria-label="关闭"], [aria-label="Close"], button[data-testid*="close"]');
                for (const btn of closeBtns) { try { btn.click(); } catch(e) {} }
                const agreePatterns = ['同意', '接受', '确定', '确认', 'Agree', 'Accept', 'OK', 'Confirm', 'Got it', '知道了'];
                const allBtns = document.querySelectorAll('button, [role="button"]');
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim();
                    if (agreePatterns.some(p => txt === p || txt.includes(p))) {
                        try { btn.click(); } catch(e) {}
                    }
                }
            }""")
            await self._deepseek_page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"[DeepSeek] dismiss popups: {e}")

    async def _mimo_login_recovery(self):
        """显示浏览器让用户登录，等待登录完成后继续。"""
        logger.warning("[MiMo] Login required, switching to headful mode...")
        if not self._mimo_page:
            return
        try:
            await self._mimo_browser.close()
        except Exception:
            pass

        from playwright.async_api import async_playwright

        self._mimo_pw = await async_playwright().start()
        self._mimo_browser = await self._mimo_pw.chromium.launch_persistent_context(
            user_data_dir=self._mimo_user_data_dir,
            headless=False,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._mimo_page = self._mimo_browser.pages[0] if self._mimo_browser.pages else await self._mimo_browser.new_page()
        await self._mimo_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        await self._mimo_page.goto("https://aistudio.xiaomimimo.com/#/c", wait_until="domcontentloaded", timeout=60000)

        logger.info("[MiMo] Please login in the browser window. Waiting up to 180s...")
        for i in range(180):
            await asyncio.sleep(1)
            try:
                ph = await self._mimo_page.evaluate("document.querySelector('textarea')?.placeholder")
                if ph and '登录' not in ph:
                    logger.info(f"[MiMo] Login detected at {i}s!")
                    await asyncio.sleep(3)
                    break
            except:
                pass
        else:
            logger.warning("[MiMo] Login timeout after 180s")

    async def stream_mimo_chat(self, prompt: str, model_type: str = "default", thinking_enabled: bool = False, search_enabled: bool = False, inline_file_content: str | None = None, model_name: str | None = None):
        """MiMo 流式对话：上传文件→等待解析→发送消息→SSE流式解析。

        Args:
            model_name: 模型ID（如 'mimo-v2.5-pro'），用于在页面模型选择器中切换模型。
        """
        headless = CONFIG.get('_mimo_headless', True)
        await self.ensure_mimo_ready(headless=headless)
        stream_id = uuid.uuid4().hex
        q = asyncio.Queue()
        self._mimo_queues[stream_id] = q
        session_id = ""

        # 0. 模型切换：如果提供了 model_name，先切换到目标模型
        if model_name:
            await self.select_mimo_model(model_name)
            await asyncio.sleep(1)

        # 1. 注册消息回调
        async def _on_mimo_chunk(chunk_json: str):
            try:
                data = json.loads(chunk_json)
                event_type = data.get("type", "")
                if event_type == "text":
                    content = data.get("content", "")
                    if content:
                        q.put_nowait(("chunk", content))
                elif event_type == "finish":
                    q.put_nowait(("done", ""))
                elif event_type == "error":
                    msg = data.get("message", "mimo error")
                    q.put_nowait(("error", msg))
                    q.put_nowait(("done", ""))
            except Exception as e:
                logger.debug(f"[MiMo] chunk parse error: {e}")

        try:
            await self._mimo_page.expose_function("mimoOnChunk", _on_mimo_chunk)
        except Exception:
            pass

        # 2. 文件上传
        async def upload_file(content: str | None) -> bool:
            """上传文件并等待解析完成。返回 True 表示成功。"""
            if not content:
                return True
            try:
                result = await self._mimo_page.evaluate("""async (content) => {
                    try {
                        const blob = new Blob([content], { type: 'text/plain' });
                        const file = new File([blob], 'request.txt', { type: 'text/plain', lastModified: Date.now() });
                        const fileInput = document.querySelector('input[type="file"]');
                        if (!fileInput) return { error: 'no file input found' };
                        const dt = new DataTransfer();
                        dt.items.add(file);
                        fileInput.files = dt.files;
                        fileInput.dispatchEvent(new Event('change', { bubbles: true }));
                        return { dispatched: true };
                    } catch (e) {
                        return { error: String(e) };
                    }
                }""", content)

                if result.get("error"):
                    logger.warning(f"[MiMo] file upload JS error: {result['error']}")
                    return False

                await asyncio.sleep(5)

                for i in range(120):
                    ready = await self._mimo_page.evaluate(r"""() => {
                        const els = document.querySelectorAll('*');
                        for (const el of els) {
                            const text = (el.textContent || '').trim();
                            if (text.includes('request.txt')) {
                                // 向上查找父级元素，检查是否包含文件大小信息
                                let parent = el.parentElement;
                                for (let j = 0; j < 5 && parent; j++) {
                                    const parentText = parent.textContent || '';
                                    if (/\d+\s*(B|KB|MB)/.test(parentText)) {
                                        return true;
                                    }
                                    parent = parent.parentElement;
                                }
                                // 也检查同级兄弟元素
                                const siblings = el.parentElement ? el.parentElement.children : [];
                                for (const sib of siblings) {
                                    const sibText = sib.textContent || '';
                                    if (/\d+\s*(B|KB|MB)/.test(sibText)) {
                                        return true;
                                    }
                                }
                            }
                        }
                        return false;
                    }""")
                    if ready:
                        logger.info("[MiMo] file parsing complete (card visible)")
                        return True
                    await asyncio.sleep(1)
                logger.warning("[MiMo] file parse timeout after 120s")
                return False
            except Exception as e:
                logger.warning(f"[MiMo] file upload error: {e}")
                return False

        if inline_file_content:
            logger.info("[MiMo] starting file upload...")
            upload_ok = await upload_file(inline_file_content)
            if not upload_ok:
                logger.error("[MiMo] file upload failed, aborting chat")
                yield ("error", "文件上传失败")
                yield ("done", "")
                return

        # 3. 拦截 fetch 捕获 SSE 流并桥接到 Python
        self._mimo_sse_queue = q

        if not getattr(self, "_mimo_fetch_intercepted", False):
            await self._mimo_page.evaluate(r"""() => {
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                    const response = await origFetch.apply(this, args);
                    if (url.includes('/bot/chat') && response.headers.get('content-type')?.includes('text/event-stream')) {
                        const reader = response.body.getReader();
                        const decoder = new TextDecoder();
                        let buf = '';
                        (async () => {
                            while (true) {
                                const { value, done } = await reader.read();
                                if (done) break;
                                buf += decoder.decode(value, { stream: true });
                                const lines = buf.split('\n');
                                buf = lines.pop() || '';
                                for (const line of lines) {
                                    if (!line.startsWith('data:')) continue;
                                    const raw = line.slice(5).trim();
                                    if (!raw) continue;
                                    try {
                                        const data = JSON.parse(raw);
                                        if (data.type === 'text' && data.content) {
                                            window.mimoOnChunk(JSON.stringify({ type: 'text', content: data.content }));
                                        } else if (data.content === '[DONE]') {
                                            window.mimoOnChunk(JSON.stringify({ type: 'finish' }));
                                        }
                                    } catch (e) {}
                                }
                            }
                            window.mimoOnChunk(JSON.stringify({ type: 'finish' }));
                        })();
                    }
                    return response;
                };
            }""")
            self._mimo_fetch_intercepted = True

        # 4. 关闭弹窗，切换到新对话
        try:
            await self._mimo_page.locator('button[aria-label="关闭公告"]').first.click(timeout=3000)
            await asyncio.sleep(0.5)
        except:
            pass

        await self._dismiss_mimo_popups()

        # 5. 填写 prompt 并发送
        await self._mimo_page.locator('textarea').fill(prompt)
        await asyncio.sleep(1)

        send_btn = self._mimo_page.locator('button[data-track-id="home_send_btn"]')
        if await send_btn.count() > 0:
            await send_btn.first.click()
        else:
            await self._mimo_page.locator('textarea').press('Enter')

        await asyncio.sleep(2)

        # 6. 获取 session_id
        try:
            url = self._mimo_page.url
            if '/chat/' in url:
                session_id = url.split('/chat/')[-1].split('/')[0].split('?')[0]
                logger.info(f"[MiMo] session_id: {session_id}")
        except:
            pass

        if session_id:
            yield ("session_id", session_id)

        # 6. 从队列读取结果
        while True:
            try:
                kind, value = await asyncio.wait_for(q.get(), timeout=180)
                yield (kind, value)
                if kind == "done":
                    break
            except asyncio.TimeoutError:
                logger.warning("[MiMo] timeout waiting for response")
                yield ("error", "时人不识凌云木，直待凌云始道高。")
                yield ("done", "")
                break
            except asyncio.CancelledError:
                break

        self._mimo_queues.pop(stream_id, None)

    async def delete_mimo_conversation(self, session_id: str):
        """删除单个 MiMo 会话。"""
        if not session_id:
            return
        try:
            ph_token = await self._mimo_page.evaluate("""() => {
                const raw = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('xiaomichatbot_ph='));
                if (!raw) return null;
                let val = raw.split('=').slice(1).join('=');
                if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
                return val || null;
            }""")
            if not ph_token:
                logger.warning("[MiMo] no xiaomichatbot_ph cookie found")
                return

            result = await self._mimo_page.evaluate("""async (args) => {
                const resp = await fetch('/open-apis/chat/conversation/delete?xiaomichatbot_ph=' + encodeURIComponent(args.ph), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify([args.convId])
                });
                const text = await resp.text();
                return { status: resp.status, body: text.substring(0, 200) };
            }""", {'ph': ph_token, 'convId': session_id})
            if result.get('status') == 200:
                logger.info(f"[MiMo] deleted session {session_id}")
            else:
                logger.warning(f"[MiMo] delete failed: {json.dumps(result, ensure_ascii=False)[:200]}")
        except Exception as e:
            logger.warning(f"[MiMo] delete exception: {e}")

    async def delete_all_mimo_conversations(self):
        """删除所有 MiMo 会话。"""
        try:
            if not self._mimo_page or self._mimo_page.is_closed():
                return
            ph_token = await self._mimo_page.evaluate("""() => {
                const raw = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('xiaomichatbot_ph='));
                if (!raw) return null;
                let val = raw.split('=').slice(1).join('=');
                if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
                return val || null;
            }""")
            if not ph_token:
                logger.warning("[MiMo] no xiaomichatbot_ph cookie found")
                return

            result = await self._mimo_page.evaluate("""async (args) => {
                try {
                    const listResp = await fetch('/open-apis/chat/conversation/list?xiaomichatbot_ph=' + encodeURIComponent(args.ph), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        credentials: 'include',
                        body: JSON.stringify({ pageInfo: { pageNum: 1, pageSize: 50 } })
                    });
                    const listData = await listResp.json();
                    if (!listData.data || !listData.data.dataList || listData.data.dataList.length === 0) return { deleted: 0 };

                    const convIds = listData.data.dataList.map(c => c.conversationId);
                    let deleted = 0;
                    for (const id of convIds) {
                        const delResp = await fetch('/open-apis/chat/conversation/delete?xiaomichatbot_ph=' + encodeURIComponent(args.ph), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'include',
                            body: JSON.stringify([id])
                        });
                        if (delResp.status === 200) deleted++;
                    }
                    return { deleted: deleted, total: convIds.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", {'ph': ph_token})
            logger.info(f"[MiMo] delete_all result: {json.dumps(result, ensure_ascii=False)}")
        except Exception as e:
            logger.warning(f"[MiMo] delete_all exception: {e}")

    async def get_mimo_session_id(self) -> str:
        """从 URL 获取当前 MiMo 会话 ID。"""
        try:
            if not self._mimo_page or self._mimo_page.is_closed():
                return ""
            url = self._mimo_page.url
            if '/chat/' in url:
                return url.split('/chat/')[-1].split('/')[0].split('?')[0]
        except:
            pass
        return ""

    async def close(self):
        """关闭所有浏览器。"""
        # 先关闭页面，再关闭浏览器上下文，最后停止 Playwright，避免 driver 已断开后仍访问 page。
        for attr in ['_doubao_page', '_qianwen_page', '_deepseek_page', '_zai_page', '_mimo_page', '_minimax_page', '_xinghuo_page']:
            page = getattr(self, attr, None)
            if page:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception as e:
                    logger.debug(f"Error closing page {attr}: {e}")
                setattr(self, attr, None)

        for attr in ['_doubao_browser', '_qianwen_browser', '_deepseek_browser', '_zai_browser', '_mimo_browser', '_minimax_browser', '_xinghuo_browser']:
            browser = getattr(self, attr, None)
            if browser:
                try:
                    await browser.close()
                except Exception as e:
                    logger.debug(f"Error closing browser {attr}: {e}")
                setattr(self, attr, None)

        for attr in ['_pw', '_doubao_pw', '_qianwen_pw', '_deepseek_pw', '_zai_pw', '_mimo_pw', '_minimax_pw', '_xinghuo_pw']:
            pw = getattr(self, attr, None)
            if pw:
                try:
                    await pw.stop()
                except Exception as e:
                    logger.debug(f"Error stopping Playwright {attr}: {e}")
                setattr(self, attr, None)

        if hasattr(self, '_zai_queues') and self._zai_queues:
            for stream_id in list(self._zai_queues.keys()):
                q = self._zai_queues.pop(stream_id, None)
                if q:
                    try:
                        await q.put(("done", ""))
                    except Exception:
                        pass
        if hasattr(self, '_zai_active_stream'):
            self._zai_active_stream = None


    # ═══════════════════════════════════════════════════════════════════════
    # MiniMax Agent 浏览器操作
    # ═══════════════════════════════════════════════════════════════════════

    async def ensure_minimax_ready(self, headless: bool = True):
        """确保 MiniMax Agent 浏览器已启动并登录。"""
        if self._minimax_page and not self._minimax_page.is_closed():
            return

        from playwright.async_api import async_playwright
        logger.info("[Minimax] Starting MiniMax Agent browser...")
        self._minimax_pw = await async_playwright().start()
        self._minimax_browser = await self._minimax_pw.chromium.launch_persistent_context(
            user_data_dir=self._minimax_user_data_dir,
            headless=headless,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._minimax_page = self._minimax_browser.pages[0] if self._minimax_browser.pages else await self._minimax_browser.new_page()

        # 反检测
        await self._minimax_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        await self._minimax_page.goto("https://agent.minimaxi.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        # 移除遮挡弹窗
        await self._minimax_page.evaluate("""() => {
            document.querySelectorAll('[data-connect-mobile-hint-dismiss-boundary]').forEach(el => el.remove());
            document.querySelectorAll('.fixed').forEach(el => {
                const z = el.style.zIndex || window.getComputedStyle(el).zIndex;
                if (parseInt(z) >= 999) el.remove();
            });
        }""")
        await asyncio.sleep(1)

        # 检查是否已登录（查找登录按钮）
        is_logged_in = await self._minimax_page.evaluate("""() => {
            return !document.querySelector('[data-testid="sidebar-login-button"]');
        }""")

        if not is_logged_in:
            if headless:
                raise RuntimeError("MiniMax Agent 未登录，请先运行 python main.py --login minimax")
            # 非 headless 模式，尝试登录
            logger.info("[Minimax] Not logged in, opening login page...")
            await self._minimax_page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="sidebar-login-button"]');
                if (btn) btn.click();
            }""")
            await asyncio.sleep(3)

            # 等待用户登录（最多 3 分钟）
            for i in range(90):
                await asyncio.sleep(2)
                url = self._minimax_page.url
                if (url.startswith('https://agent.minimaxi.com') and
                    'account.minimaxi.com' not in url and
                    'unified-login' not in url):
                    logger.info("[Minimax] Login successful!")
                    break
            else:
                raise RuntimeError("MiniMax Agent 登录超时")

        logger.info("[Minimax] MiniMax Agent browser ready")

    async def get_minimax_session_id(self) -> str:
        """从 MiniMax 页面 URL 提取当前会话 ID。"""
        try:
            if not self._minimax_page:
                return ""
            url = self._minimax_page.url
            if '/mavis/' in url:
                sid = url.split('/mavis/')[-1].split('?')[0].split('#')[0]
                if sid:
                    return sid
            return ""
        except Exception:
            return ""

    async def stream_minimax_chat(self, prompt: str, model_type: str = "m3",
                                   thinking_enabled: bool = False,
                                   search_enabled: bool = False,
                                   inline_file_content: str | None = None,
                                   model_name: str | None = None):
        """向 MiniMax Agent 发送消息并流式返回响应。

        Yields: (kind, value) 元组
            kind: "session_id", "chunk", "done", "error"
        """
        if not self._minimax_page or self._minimax_page.is_closed():
            yield ("error", "MiniMax page not available")
            return

        async with self._minimax_lock:
            try:
                # 创建新会话
                await self._minimax_page.goto("https://agent.minimaxi.com/", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                # 移除弹窗
                await self._minimax_page.evaluate("""() => {
                    document.querySelectorAll('[data-connect-mobile-hint-dismiss-boundary]').forEach(el => el.remove());
                    document.querySelectorAll('.fixed').forEach(el => {
                        const z = el.style.zIndex || window.getComputedStyle(el).zIndex;
                        if (parseInt(z) >= 999) el.remove();
                    });
                }""")
                await asyncio.sleep(1)

                # 注入 SSE 拦截器
                await self._minimax_page.evaluate("""() => {
                    if (!window.__minimax_sse_patched) {
                        window.__minimax_sse_patched = true;
                        window.__minimax_sse_events = [];
                        const origFetch = window.fetch;
                        window.fetch = async function(...args) {
                            const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                            const resp = await origFetch.apply(this, args);
                            if (url.includes('/chat/') || url.includes('/send_msg') || url.includes('/continue_run')) {
                                try {
                                    const cloned = resp.clone();
                                    const reader = cloned.body.getReader();
                                    const decoder = new TextDecoder();
                                    (async () => {
                                        let buf = '';
                                        while (true) {
                                            const {value, done} = await reader.read();
                                            if (done) {
                                                window.__minimax_sse_events.push(JSON.stringify({type: 'done'}));
                                                break;
                                            }
                                            buf += decoder.decode(value, {stream: true});
                                            const lines = buf.split('\\n');
                                            buf = lines.pop() || '';
                                            for (const line of lines) {
                                                if (line.startsWith('data:')) {
                                                    const raw = line.slice(5).trim();
                                                    if (raw === '[DONE]') {
                                                        window.__minimax_sse_events.push(JSON.stringify({type: 'done'}));
                                                    } else if (raw) {
                                                        window.__minimax_sse_events.push(JSON.stringify({type: 'data', data: raw}));
                                                    }
                                                } else if (line.startsWith('event:')) {
                                                    const evt = line.slice(6).trim();
                                                    if (evt === 'done' || evt === 'close') {
                                                        window.__minimax_sse_events.push(JSON.stringify({type: 'done'}));
                                                    }
                                                }
                                            }
                                        }
                                    })();
                                } catch(e) {}
                            }
                            return resp;
                        };
                    }
                }""")

                # 准备发送内容
                content = inline_file_content if inline_file_content else prompt

                # 查找输入框并输入
                textarea = self._minimax_page.locator('textarea, [contenteditable="true"]').first
                await textarea.click()
                await asyncio.sleep(0.5)

                # 使用 native setter 确保 React 状态同步
                await self._minimax_page.evaluate("""(text) => {
                    const el = document.querySelector('textarea, [contenteditable="true"]');
                    if (!el) return;
                    if (el.tagName === 'TEXTAREA') {
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        nativeSetter.call(el, text);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    } else {
                        el.textContent = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""", content)
                await asyncio.sleep(0.5)

                # 清空 SSE 事件缓冲
                await self._minimax_page.evaluate("window.__minimax_sse_events = []")

                # 点击发送按钮或按 Enter
                send_btn = self._minimax_page.locator('button[data-testid*="send"], button[aria-label*="发送"]').first
                try:
                    await send_btn.click(timeout=3000)
                except Exception:
                    await textarea.press("Enter")

                await asyncio.sleep(1)

                # 获取会话 ID
                session_id = await self.get_minimax_session_id()
                if session_id:
                    yield ("session_id", session_id)

                # 读取 SSE 事件
                last_event_count = 0
                empty_count = 0
                max_empty = 30  # 60 秒无数据则超时

                while empty_count < max_empty:
                    await asyncio.sleep(2)
                    events = await self._minimax_page.evaluate("window.__minimax_sse_events || []")
                    new_events = events[last_event_count:]
                    last_event_count = len(events)

                    if not new_events:
                        empty_count += 1
                        continue

                    empty_count = 0
                    for evt_str in new_events:
                        try:
                            evt = json.loads(evt_str)
                            if evt.get("type") == "done":
                                yield ("done", "")
                                return
                            elif evt.get("type") == "data":
                                data_str = evt.get("data", "")
                                # 尝试解析 SSE data
                                try:
                                    data = json.loads(data_str)
                                    # MiniMax 可能使用不同的数据格式
                                    content = (data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                              or data.get("data", {}).get("delta_content", "")
                                              or data.get("delta_content", "")
                                              or "")
                                    if content:
                                        yield ("chunk", content)
                                except json.JSONDecodeError:
                                    # 可能是纯文本
                                    if data_str and data_str != "[DONE]":
                                        yield ("chunk", data_str)
                        except Exception:
                            pass

                yield ("done", "")

            except Exception as e:
                logger.warning(f"[Minimax] stream_chat error: {e}")
                yield ("error", str(e))

    async def delete_minimax_conversation(self, session_id: str):
        """删除单个 MiniMax 会话。"""
        if not session_id or not self._minimax_page or self._minimax_page.is_closed():
            return
        try:
            # 获取 token
            token = await self._minimax_page.evaluate("localStorage.getItem('token') || ''")
            if not token:
                logger.warning("[Minimax] no token for delete")
                return

            result = await self._minimax_page.evaluate("""async (args) => {
                try {
                    const resp = await fetch('/sidebar/session/' + args.sid + '?device_platform=web&biz_id=3&app_id=3001', {
                        method: 'DELETE',
                        headers: { 'Authorization': 'Bearer ' + args.token, 'Content-Type': 'application/json' }
                    });
                    return { ok: resp.ok, status: resp.status };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", {"sid": session_id, "token": token})
            if result.get("ok"):
                logger.info(f"[Minimax] deleted session {session_id}")
            else:
                logger.warning(f"[Minimax] delete failed: {result}")
        except Exception as e:
            logger.warning(f"[Minimax] delete exception: {e}")

    async def delete_all_minimax_conversations(self):
        """删除所有 MiniMax 会话。"""
        try:
            if not self._minimax_page or self._minimax_page.is_closed():
                logger.warning("[Minimax] no page, skip batch delete")
                return

            result = await self._minimax_page.evaluate("""async () => {
                try {
                    const token = localStorage.getItem('token') || '';
                    if (!token) return { error: 'no token' };

                    // 获取会话列表
                    const listResp = await fetch('/sidebar/session?device_platform=web&biz_id=3&app_id=3001', {
                        headers: { 'Authorization': 'Bearer ' + token }
                    });
                    const listData = await listResp.json();
                    const sessions = listData?.data?.sessions || [];
                    if (sessions.length === 0) return { deleted: 0, total: 0 };

                    let deleted = 0;
                    for (const s of sessions) {
                        try {
                            const delResp = await fetch('/sidebar/session/' + s.id + '?device_platform=web&biz_id=3&app_id=3001', {
                                method: 'DELETE',
                                headers: { 'Authorization': 'Bearer ' + token }
                            });
                            if (delResp.ok) deleted++;
                        } catch(e) {}
                    }
                    return { deleted, total: sessions.length };
                } catch (e) {
                    return { error: String(e) };
                }
            }""")
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Minimax] delete_all: deleted {deleted}/{total} sessions")
        except Exception as e:
            logger.warning(f"[Minimax] delete_all exception: {e}")

    async def close_minimax(self):
        """关闭 MiniMax 浏览器。"""
        try:
            if self._minimax_page and not self._minimax_page.is_closed():
                await self._minimax_page.close()
        except Exception:
            pass
        self._minimax_page = None
        try:
            if self._minimax_browser:
                await self._minimax_browser.close()
        except Exception:
            pass
        self._minimax_browser = None
        try:
            if self._minimax_pw:
                await self._minimax_pw.stop()
        except Exception:
            pass
        self._minimax_pw = None
        logger.info("[Minimax] resources cleaned up")

    async def _minimax_fetch_with_signature(self, path: str, extra: dict | None = None, body: dict | None = None, method: str = "GET") -> dict:
        """在浏览器内完整执行 Minimax API 请求，包括构建 URL 和生成签名。"""
        signature_js = """
        async (path, extra, body, method) => {
            // 从 localStorage 获取 token
            const token = localStorage.getItem('_token');
            if (!token) throw new Error('no token');
            
            // 获取用户信息
            const ud = localStorage.getItem('user_detail_agent');
            let userId = '';
            if (ud) {
                try { userId = JSON.parse(ud).realUserID || JSON.parse(ud).userID || ''; } catch(e) {}
            }
            const deviceId = localStorage.getItem('UNIQUE_USER_ID') || '';
            
            // 基础参数
            const base = {{
                device_platform: "web",
                biz_id: "3",
                app_id: "3001",
                version_code: "22201",
                timezone_offset: String(-new Date().getTimezoneOffset() * 60),
                sys_language: navigator.language || "zh",
                lang: "zh",
                uuid: deviceId,
                os_name: navigator.userAgent.includes("Windows") ? "Windows" : navigator.userAgent.includes("Mac") ? "macOS" : "Linux",
                browser_name: "Chrome",
                device_memory: String(navigator.deviceMemory || 8),
                cpu_core_num: String(navigator.hardwareConcurrency || 8),
                browser_language: navigator.language || "zh-CN",
                browser_platform: navigator.platform || "Win32",
                user_id: userId,
                screen_width: String(screen.width || 1280),
                screen_height: String(screen.height || 900),
                client: "web"
            }};
            
            // 合并额外参数
            const allParams = { ...base, ...extra };
            // 加上 Unix 时间戳
            const now = Date.now();
            const unix = String(now);
            allParams.unix = unix;
            
            // 构建查询字符串
            const usp = new URLSearchParams();
            for (const [k, v] of Object.entries(allParams)) {
                usp.append(k, String(v));
            }
            const queryString = usp.toString();
            
            // 构建完整 URL
            const url = `https://agent.minimaxi.com${path}?${queryString}`;
            
            // 提取路径+查询用于签名（去掉 hostname）
            const pathWithQS = path + '?' + queryString;
            
            // 生成 x-timestamp
            const xTimestamp = Math.floor(now / 1000).toString();
            
            // 生成 x-signature: MD5(xTimestamp + "I*7Cf%WZ#S&%1RlZJ&C2" + bodyString)
            let bodyStr = "{}";
            if (body && typeof body === 'object') {
                bodyStr = JSON.stringify(body);
            } else if (typeof body === 'string') {
                bodyStr = body;
            }
            const secret = "I*7Cf%WZ#S&%1RlZJ&C2";
            const xSignature = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(xTimestamp + secret + bodyStr))
                .then(buf => Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join(''))
                .then(hex => {
                    // crypto.subtle only supports SHA-256, SHA-384, SHA-512. We need MD5.
                    // Fallback to simple custom MD5 or use crypto.js?
                    // For now, we'll implement a simple MD5 in JS
                    return md5(xTimestamp + secret + bodyStr);
                });
            
            // 生成 yy: MD5(encodeURIComponent(pathWithQS) + '_' + bodyStr + MD5(xTimestamp) + 'ooui')
            const tsMd5 = md5(xTimestamp);
            const yyInput = encodeURIComponent(pathWithQS) + '_' + bodyStr + tsMd5 + 'ooui';
            const yy = md5(yyInput);
            
            // 发送请求
            const headers = {
                'Content-Type': 'application/json',
                'token': token,
                'x-timestamp': xTimestamp,
                'x-signature': xSignature,
                'yy': yy
            };
            
            try {
                const resp = await fetch(url, {
                    method: method,
                    headers: headers,
                    body: body && method === 'POST' ? JSON.stringify(body) : undefined
                });
                const text = await resp.text();
                return { status: resp.status, body: text, ok: resp.ok };
            } catch (e) {
                return { error: String(e) };
            }
        }
        
        // MD5 implementation in JS (copy from captured context)
        function md5(string) {
            function rotateLeft(lValue, iShiftBits) {
                return (lValue<<iShiftBits) | (lValue>>>(32-iShiftBits));
            }
            function addUnsigned(lX,lY) {
                var lX4,lY4,lX8,lY8,lResult;
                lX8 = (lX & 0x80000000); lY8 = (lY & 0x80000000);
                lX4 = (lX & 0x40000000); lY4 = (lY & 0x40000000);
                lResult = (lX & 0x3FFFFFF) + (lY & 0x3FFFFFF);
                if (lX4 & lY4) return (lResult ^ 0x80000000 ^ lX8 ^ lY8);
                if (lX4 | lY4) {
                    if (lResult & 0x40000000) return (lResult ^ 0xC0000000 ^ lX8 ^ lY8);
                    else return (lResult ^ 0x40000000 ^ lX8 ^ lY8);
                } else return (lResult ^ lX8 ^ lY8);
            }
            // ... full implementation needed but truncated for brevity
            return "00000000000000000000000000000000"; // Placeholder
        }
        """
        # Actually, we need to move the full MD5 implementation to an earlier injection or include it
        
        # For now, let's call an existing page method if available
        raise NotImplementedError("Use browser-side signing instead")

    async def _minimax_common_params(self) -> dict:
        """获取 Minimax API 公共查询参数。"""
        import time as _time
        token = await self._minimax_page.evaluate("localStorage.getItem('_token')")
        if not token:
            raise RuntimeError("[Minimax] no token in localStorage._token")
        
        user_info = await self._minimax_page.evaluate("""() => {
            const ud = localStorage.getItem('user_detail_agent');
            let userId = '';
            if (ud) {
                try { userId = JSON.parse(ud).realUserID || JSON.parse(ud).userID || ''; } catch(e) {}
            }
            return {
                userId: userId,
                deviceId: localStorage.getItem('UNIQUE_USER_ID') || ''
            };
        }""")
        
        return {
            "device_platform": "web",
            "biz_id": "3",
            "app_id": "3001",
            "version_code": "22201",
            "unix": str(int(_time.time() * 1000)),
            "timezone_offset": "28800",
            "sys_language": "zh",
            "lang": "zh",
            "uuid": user_info.get("deviceId", ""),
            "os_name": "Windows" if sys.platform.startswith("win") else "macOS" if sys.platform == "darwin" else "Linux",
            "browser_name": "Chrome",
            "device_memory": "32",
            "cpu_core_num": "8",
            "browser_language": "zh-CN",
            "browser_platform": "Win32" if sys.platform.startswith("win") else "MacIntel" if sys.platform == "darwin" else "Linux x86_64",
            "user_id": user_info.get("userId", ""),
            "screen_width": "1280",
            "screen_height": "900",
            "token": token,
            "client": "web",
        }

    async def _minimax_api_request(self, path: str, extra: dict | None = None, body: dict | None = None, method: str = "GET") -> dict:
        """发送带签名的 Minimax API 请求。Python 端计算签名（hashlib.md5），浏览器端构建 URL 和 fetch。"""
        params = await self._minimax_common_params()
        if extra:
            params.update(extra)
        params["unix"] = str(int(time.time() * 1000))
        
        unix_ms = params["unix"]
        x_timestamp = str(int(int(unix_ms) / 1000))
        
        body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else None
        
        # 计算签名所需的 body_str：GET 为空串，POST 为 JSON（或空串）
        body_for_sig = body_json if (method.upper() == "POST" and body_json) else ""
        
        # 在浏览器端用 URLSearchParams 构建查询字符串（与 Minimax 原生 JS 一致）
        build_result = await self._minimax_page.evaluate("""
            (args) => {
                const usp = new URLSearchParams();
                for (const [k, v] of Object.entries(args.params)) {
                    usp.append(k, String(v));
                }
                const qs = usp.toString();
                const pathWithQS = args.path + '?' + qs;
                const fullUrl = 'https://agent.minimaxi.com' + pathWithQS;
                return { qs, pathWithQS, fullUrl };
            }
        """, {"params": params, "path": path})
        
        path_with_qs = build_result["pathWithQS"]
        full_url = build_result["fullUrl"]
        
        # x-signature = MD5(xTimestamp + secret + bodyStr)
        secret = "I*7Cf%WZ#S&%1RlZJ&C2"
        x_signature = hashlib.md5(f"{x_timestamp}{secret}{body_for_sig}".encode("utf-8")).hexdigest()
        
        # yy = MD5(encodeURIComponent(pathWithQS) + '_' + bodyStr + MD5(xTimestamp) + 'ooui')
        ts_md5 = hashlib.md5(x_timestamp.encode("utf-8")).hexdigest()
        yy_input = urllib.parse.quote(path_with_qs, safe="") + "_" + body_for_sig + ts_md5 + "ooui"
        yy = hashlib.md5(yy_input.encode("utf-8")).hexdigest()
        
        token = params.get("token", "")
        
        logger.debug(f"[Minimax] {method} {path[:60]}.. yy={yy[:8]}.. sig={x_signature[:8]}..")
        
        result = await self._minimax_page.evaluate("""
            async (args) => {
                const {url, bodyObj, method, token, xTimestamp, xSignature, yy} = args;
                const headers = {
                    'Content-Type': 'application/json',
                    'token': token,
                    'x-timestamp': xTimestamp,
                    'x-signature': xSignature,
                    'yy': yy
                };
                try {
                    const resp = await fetch(url, {
                        method: method,
                        headers: headers,
                        body: bodyObj ? JSON.stringify(bodyObj) : undefined
                    });
                    return { status: resp.status, body: await resp.text(), ok: resp.ok };
                } catch (e) {
                    return { error: String(e) };
                }
            }
        """, {
            "url": full_url,
            "bodyObj": body,
            "method": method,
            "token": token,
            "xTimestamp": x_timestamp,
            "xSignature": x_signature,
            "yy": yy,
        })
        
        return result

    async def _minimax_build_url(self, base_path: str, extra: dict | None = None) -> str:
        """构建 Minimax API URL。在浏览器内部用 URLSearchParams 构建，确保编码与浏览器一致。"""
        import time
        params = await self._minimax_common_params()
        params["unix"] = str(int(time.time() * 1000))
        if extra:
            params.update(extra)
        # 在浏览器内部构建 URL，避免 Python urlencode 编码 JWT token 中的 . 字符
        js_params = json.dumps(params)
        result = await self._minimax_page.evaluate(f"""
            (() => {{
                const p = {js_params};
                const usp = new URLSearchParams();
                for (const [k, v] of Object.entries(p)) {{
                    usp.append(k, String(v));
                }}
                return 'https://agent.minimaxi.com{base_path}?' + usp.toString();
            }})()
        """)
        return result

    async def upload_minimax_file(self, file_data: bytes, file_name: str, mime_type: str = "text/plain") -> dict:
        """上传文件到 Minimax：获取策略 → Python oss2 直接上传 OSS → 回调注册。
        返回 {file_id, file_url, file_name, mime_type}。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        import hashlib
        import uuid
        import time
        import base64
        
        file_md5 = hashlib.md5(file_data).hexdigest()
        file_size = len(file_data)
        ext = file_name.rsplit('.', 1)[-1] if '.' in file_name else 'txt'
        oss_filename = f"{uuid.uuid4().hex}.{ext}"

        # 1. 获取 OSS 上传策略
        policy_resp = await self._minimax_api_request("/v1/api/files/request_policy", method="GET")
        
        if "error" in policy_resp:
            raise RuntimeError(f"[Minimax] request_policy error: {policy_resp['error']}")
        
        policy_data = json.loads(policy_resp["body"])
        policy = policy_data.get("data", {})
        if not policy:
            raise RuntimeError(f"[Minimax] request_policy empty: {policy_resp['body'][:500]}")
        
        dir_path = policy.get("dir", "cdn-yingshi-ai-com/prod/user/multi_chat_file")
        endpoint = policy.get("endpoint", "oss-cn-wulanchabu.aliyuncs.com")
        bucket = policy["bucketName"]
        oss_key = f"{dir_path}/{oss_filename}"
        
        access_key_id = policy["accessKeyId"]
        access_key_secret = policy["accessKeySecret"]
        security_token = policy["securityToken"]
        
        logger.info(f"[Minimax] OSS policy OK, uploading to: {oss_key}")

        # 2. 使用 oss2 Python 库签名并上传到 OSS（避免浏览器端 OSS V1 签名兼容问题）
        import oss2
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
        oss_bucket = oss2.Bucket(auth, f"https://{endpoint}", bucket)

        try:
            result = oss_bucket.put_object(oss_key, file_data, headers={'Content-Type': mime_type})
            if result.status != 200:
                raise RuntimeError(f"HTTP {result.status}, request_id={result.request_id}")
            logger.info(f"[Minimax] OSS upload success: etag={result.etag}")
        except Exception as e:
            raise RuntimeError(f"[Minimax] OSS upload exception: {e}")

        # 3. 回调通知 Minimax
        cb_body = {
            "fileName": oss_filename,
            "originFileName": file_name,
            "dir": dir_path,
            "endpoint": endpoint,
            "bucketName": bucket,
            "size": str(file_size),
            "mimeType": mime_type,
            "fileMd5": file_md5,
        }
        
        cb_resp = await self._minimax_api_request("/v1/api/files/policy_callback", body=cb_body, method="POST")
        
        if "error" in cb_resp:
            raise RuntimeError(f"[Minimax] policy_callback error: {cb_resp['error']}")
        
        cb_data = json.loads(cb_resp["body"])
        file_info = cb_data.get("data", {})
        if not file_info:
            raise RuntimeError(f"[Minimax] policy_callback failed: {cb_resp['body'][:500]}")
        
        logger.info(f"[Minimax] File registered: fileID={file_info.get('fileID')}, ossPath={file_info.get('ossPath')}")
        return {
            "file_id": file_info.get("fileID"),
            "file_url": file_info.get("ossPath"),
            "file_name": file_name,
            "mime_type": mime_type,
        }

    async def upload_minimax_file_via_ui(self, file_data: bytes, file_name: str, mime_type: str = "text/plain") -> dict:
        """通过页面 UI 上传文件到 Minimax：写入临时文件 → file-input.set_input_files() → 等待页面完成 OSS 上传 → 拦截 policy_callback 响应。
        返回 {file_id, file_url, file_name, mime_type}。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        import tempfile
        import re

        with tempfile.NamedTemporaryFile(suffix=f".{file_name.rsplit('.', 1)[-1] if '.' in file_name else 'txt'}", delete=False) as tf:
            tf.write(file_data)
            temp_path = tf.name

        try:
            callback_data = {}

            async def intercept_callback(route):
                response = await route.fetch()
                body = await response.text()
                url = route.request.url
                if "policy_callback" in url:
                    try:
                        data = json.loads(body)
                        callback_data.update(data.get("data", {}))
                    except Exception:
                        pass
                await route.fulfill(response=response)

            await self._minimax_page.route("**/policy_callback**", intercept_callback)

            file_input = self._minimax_page.locator('[data-testid="file-input"]')
            await file_input.set_input_files(temp_path)
            logger.info(f"[Minimax] UI upload initiated for: {file_name}")

            upload_done = False
            for i in range(120):
                await self._minimax_page.wait_for_timeout(1000)
                att_state = await self._minimax_page.evaluate("""() => {
                    const bar = document.querySelector('[data-testid="attachment-bar"]');
                    if (!bar) return null;
                    const uploading = bar.querySelector('[data-testid="attachment-uploading"]');
                    const removeBtn = bar.querySelector('[aria-label="移除附件"]');
                    // 检查任何"分析中/解析中/processing"相关文字
                    const text = bar.textContent || '';
                    const hasAnalyzing = /分析|解析|处理|processing|analyzing|loading/i.test(text);
                    return {
                        hasUploadBar: !!uploading,
                        hasRemoveBtn: !!removeBtn,
                        hasAnalyzing: hasAnalyzing,
                        barText: text.slice(0, 50),
                    };
                }""")
                if att_state is None:
                    continue
                if not att_state['hasUploadBar'] and att_state['hasRemoveBtn'] and not att_state['hasAnalyzing']:
                    if not upload_done:
                        upload_done = True
                        logger.info(f"[Minimax] UI upload (OSS+register) complete in {i+1}s, waiting for analysis...")
                        # 上传完成后再等待 2 秒确保后端分析完成
                        await self._minimax_page.wait_for_timeout(2000)
                    break
                if not att_state['hasUploadBar'] and not att_state['hasRemoveBtn']:
                    await self._minimax_page.wait_for_timeout(2000)
                    bar_exists = await self._minimax_page.evaluate("!!document.querySelector('[data-testid=\"attachment-bar\"]')")
                    if not bar_exists:
                        raise RuntimeError(f"[Minimax] Upload bar disappeared after {i+1}s - upload may have failed")
                if i % 10 == 9:
                    logger.debug(f"[Minimax] Still processing... {i+1}s, state: {att_state}")

            if not callback_data:
                raise RuntimeError("[Minimax] policy_callback was not intercepted - upload may have failed")

            file_info = callback_data
            file_id = file_info.get("fileID")
            file_url = file_info.get("ossPath") or file_info.get("coverUrl")

            if not file_id or not file_url:
                raise RuntimeError(f"[Minimax] Missing file info in callback: {json.dumps(callback_data)}")

            logger.info(f"[Minimax] File uploaded via UI: fileID={file_id}, url={file_url}")
            return {
                "file_id": file_id,
                "file_url": file_url,
                "file_name": file_name,
                "mime_type": mime_type,
            }
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            try:
                await self._minimax_page.unroute("**/policy_callback**", intercept_callback)
            except Exception:
                pass

    async def create_minimax_session(self, model_name: str = "MiniMax-M3") -> str:
        """创建 Minimax Agent 会话，返回 session_id。"""
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        agent_id = await self._minimax_page.evaluate("""() => {
            return localStorage.getItem('agentId') || '411762200674378';
        }""")
        
        resp = await self._minimax_api_request(
            f"/archon/api/v1/agent/{agent_id}/session",
            extra={"region": "cn"},
            body={"model": f"minimax/{model_name}"},
            method="POST"
        )
        
        if "error" in resp:
            raise RuntimeError(f"[Minimax] create session error: {resp['error']}")
        
        data = json.loads(resp["body"])
        session_id = data.get("session_id", "") or data.get("data", {}).get("id", "")
        if not session_id:
            raise RuntimeError(f"[Minimax] create session failed: {resp['body'][:500]}")
        
        logger.info(f"[Minimax] Session created: {session_id}")
        return session_id

    async def send_minimax_message_with_sse(self, session_id: str, content: str, attachments: list | None = None, model_name: str = "MiniMax-M3", thinking_enabled: bool = False, search_enabled: bool = False):
        """[备用] 发送消息到 Minimax Agent 并以 SSE 流式返回。通过签名+fetch 直接调用 API。
        Yields (kind, value)。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        import uuid as _uuid
        
        att_list = []
        if attachments:
            for att in attachments:
                att_list.append({
                    "type": "file",
                    "file_path": att.get("file_name", ""),
                    "file_name": att.get("file_name", ""),
                    "mime_type": att.get("mime_type", "text/plain"),
                    "data_url": att.get("file_url", ""),
                })
        
        variant = "thinking" if thinking_enabled else "default"
        msg_body_dict = {
            "content": content,
            "attachments": att_list,
            "model": {
                "provider_id": "minimax",
                "model_id": model_name,
                "variant": variant,
            },
            "turn_id": str(_uuid.uuid4()),
            "enable_team": True,
            "worktreeMode": False,
        }
        msg_body_json = json.dumps(msg_body_dict, separators=(",", ":"), ensure_ascii=False)
        
        stream_path = f"/archon/api/v1/session/{session_id}/message"
        params = await self._minimax_common_params()
        params["region"] = "cn"
        params["unix"] = str(int(time.time() * 1000))
        
        build_result = await self._minimax_page.evaluate("""
            (args) => {
                const usp = new URLSearchParams();
                for (const [k, v] of Object.entries(args.params)) {
                    usp.append(k, String(v));
                }
                const qs = usp.toString();
                const pathWithQS = args.path + '?' + qs;
                const fullUrl = 'https://agent-stream.minimaxi.com' + pathWithQS;
                return { qs, pathWithQS, fullUrl };
            }
        """, {"params": params, "path": stream_path})
        
        full_url = build_result["fullUrl"]
        path_with_qs = build_result["pathWithQS"]
        
        x_timestamp = str(int(int(params["unix"]) / 1000))
        secret = "I*7Cf%WZ#S&%1RlZJ&C2"
        x_signature = hashlib.md5(f"{x_timestamp}{secret}{msg_body_json}".encode("utf-8")).hexdigest()
        ts_md5 = hashlib.md5(x_timestamp.encode("utf-8")).hexdigest()
        yy_input = urllib.parse.quote(path_with_qs, safe="") + "_" + msg_body_json + ts_md5 + "ooui"
        yy = hashlib.md5(yy_input.encode("utf-8")).hexdigest()
        token = params.get("token", "")

        await self._minimax_page.evaluate("""() => {
            window.__minimax_sse_chunks = [];
            window.__minimax_sse_done = false;
            window.__minimax_sse_error = null;
        }""")

        asyncio.create_task(self._minimax_page.evaluate("""async (args) => {
            try {
                const resp = await fetch(args.url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'text/event-stream',
                        'token': args.token,
                        'x-timestamp': args.xTimestamp,
                        'x-signature': args.xSignature,
                        'yy': args.yy,
                    },
                    body: args.body,
                });
                if (!resp.ok) {
                    const text = await resp.text();
                    window.__minimax_sse_error = 'HTTP ' + resp.status + ' ' + text.slice(0, 300);
                    window.__minimax_sse_done = true;
                    return;
                }
                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buf = '';
                while (true) {
                    const { value, done } = await reader.read();
                    if (done) { window.__minimax_sse_done = true; break; }
                    buf += decoder.decode(value, { stream: true });
                    const lines = buf.split('\\n');
                    buf = lines.pop() || '';
                    for (const line of lines) {
                        if (line.startsWith('data:')) {
                            const raw = line.slice(5).trim();
                            if (raw && raw !== '[DONE]') {
                                try { window.__minimax_sse_chunks.push(raw); } catch(e) {}
                            }
                        }
                    }
                }
            } catch (e) {
                window.__minimax_sse_error = String(e);
                window.__minimax_sse_done = true;
            }
        }""", {
            "url": full_url,
            "body": msg_body_json,
            "token": token,
            "xTimestamp": x_timestamp,
            "xSignature": x_signature,
            "yy": yy,
        }))

        last_count = 0
        empty_count = 0
        while empty_count < 60:
            await asyncio.sleep(1)
            chunks = await self._minimax_page.evaluate("window.__minimax_sse_chunks || []")
            done = await self._minimax_page.evaluate("window.__minimax_sse_done")
            err = await self._minimax_page.evaluate("window.__minimax_sse_error")
            
            if err:
                yield ("error", err)
                return
            
            new_chunks = chunks[last_count:]
            last_count = len(chunks)
            
            if new_chunks:
                empty_count = 0
                for chunk_str in new_chunks:
                    try:
                        data = json.loads(chunk_str)
                        c = ""
                        if isinstance(data, dict):
                            msg_type = data.get("type")
                            if msg_type == 6:
                                chunk = data.get("agent_message_chunk", {})
                                c = chunk.get("msg_content", "") or chunk.get("content", "") or chunk.get("thinking_content", "")
                            elif msg_type in (2, 10):
                                pass
                            elif "content" in data:
                                c = data["content"]
                        if c:
                            yield ("chunk", c)
                    except json.JSONDecodeError:
                        if chunk_str and chunk_str != "[DONE]":
                            yield ("chunk", chunk_str)
            else:
                empty_count += 1
            if done:
                break
        yield ("done", "")

    async def send_minimax_message_via_ui(self, content: str, model_name: str = "MiniMax-M3", thinking_enabled: bool = False, search_enabled: bool = False):
        """[主要] 通过页面 UI 发送消息到 Minimax Agent 并拦截 fetch 捕获 SSE 流式返回。
        前提：附件已通过 upload_minimax_file_via_ui 上传且分析完成，attachment-bar 中已有文件卡片。
        参考MiMo的 stream_mimo_chat 实现。
        Yields (kind, value)。
        """
        headless = CONFIG.get('_minimax_headless', True)
        await self.ensure_minimax_ready(headless=headless)

        q = asyncio.Queue()

        async def _on_minimax_chunk(chunk_json: str):
            try:
                data = json.loads(chunk_json)
                msg_type = data.get("type")
                if msg_type == 6:
                    chunk = data.get("agent_message_chunk", {})
                    text = chunk.get("msg_content", "") or chunk.get("content", "") or chunk.get("thinking_content", "")
                    if text:
                        q.put_nowait(("chunk", text))
                elif msg_type == 2:
                    pass
                elif msg_type == 10:
                    pass
                else:
                    cont = data.get("content", "")
                    if cont:
                        q.put_nowait(("chunk", cont))
            except Exception:
                pass

        async def _on_minimax_done():
            q.put_nowait(("done", ""))

        try:
            await self._minimax_page.expose_function("minimaxOnChunk", _on_minimax_chunk)
        except Exception:
            pass
        try:
            await self._minimax_page.expose_function("minimaxOnDone", _on_minimax_done)
        except Exception:
            pass

        if not getattr(self, "_minimax_fetch_intercepted", False):
            await self._minimax_page.evaluate(r"""() => {
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                    const response = await origFetch.apply(this, args);
                    if (url.includes('/session/') && url.includes('/message') && response.headers.get('content-type')?.includes('text/event-stream')) {
                        const reader = response.body.getReader();
                        const decoder = new TextDecoder();
                        let buf = '';
                        (async () => {
                            while (true) {
                                const { value, done } = await reader.read();
                                if (done) break;
                                buf += decoder.decode(value, { stream: true });
                                const lines = buf.split('\n');
                                buf = lines.pop() || '';
                                for (const line of lines) {
                                    if (!line.startsWith('data:')) continue;
                                    const raw = line.slice(5).trim();
                                    if (!raw || raw === '[DONE]') continue;
                                    try {
                                        window.minimaxOnChunk(raw);
                                    } catch (e) {}
                                }
                            }
                            window.minimaxOnDone();
                        })();
                    }
                    return response;
                };
            }""")
            self._minimax_fetch_intercepted = True
            logger.info("[Minimax] fetch interceptor installed")

        # 确保附件分析完成（参考 MiMo：查找文件大小信息出现 = 解析完成）
        att_bar = await self._minimax_page.evaluate("""() => {
            const bar = document.querySelector('[data-testid="attachment-bar"]');
            if (!bar) return { exists: false };
            const uploading = bar.querySelector('[data-testid="attachment-uploading"]');
            const removeBtn = bar.querySelector('[aria-label="移除附件"]');
            const text = bar.textContent || '';
            const hasAnalyzing = /分析|解析|处理|processing|analyzing|loading/i.test(text);
            return {
                exists: true,
                hasUploading: !!uploading,
                hasRemoveBtn: !!removeBtn,
                hasAnalyzing: hasAnalyzing,
            };
        }""")
        if att_bar.get("exists"):
            if att_bar.get("hasUploading") or att_bar.get("hasAnalyzing"):
                logger.info("[Minimax] Attachment still processing, waiting for analysis to complete...")
                for i in range(120):
                    await self._minimax_page.wait_for_timeout(1000)
                    st = await self._minimax_page.evaluate("""() => {
                        const bar = document.querySelector('[data-testid="attachment-bar"]');
                        if (!bar) return { exists: false };
                        const uploading = bar.querySelector('[data-testid="attachment-uploading"]');
                        const removeBtn = bar.querySelector('[aria-label="移除附件"]');
                        const text = bar.textContent || '';
                        const hasAnalyzing = /分析|解析|处理|processing|analyzing|loading/i.test(text);
                        return { exists: true, hasUploading: !!uploading, hasRemoveBtn: !!removeBtn, hasAnalyzing: hasAnalyzing };
                    }""")
                    if not st.get("exists"):
                        logger.warning("[Minimax] Attachment bar disappeared during analysis wait")
                        break
                    if not st.get("hasUploading") and not st.get("hasAnalyzing") and st.get("hasRemoveBtn"):
                        logger.info(f"[Minimax] Attachment analysis complete in {i+1}s")
                        break
                    if i % 10 == 9:
                        logger.debug(f"[Minimax] Still analyzing attachment... {i+1}s")

        # 填写 textarea（ProseMirror rich-text-editor）
        await self._minimax_page.evaluate("""(args) => {
            const ta = document.querySelector('[data-testid="message-textarea"]');
            if (!ta) throw new Error('message-textarea not found');

            // ProseMirror: 需要正确设置 document state
            // 方法1: 直接操作 ProseMirror view
            const view = ta.pmViewDesc;
            if (view) {
                const { TextSelection } = prosemirrorState || {};
                const tr = view.state.tr;
                const pos = 1;
                tr.insertText(args.text, pos);
                view.dispatch(tr);
            } else {
                // 方法2: fallback - 设置 textContent + 触发 input 事件
                ta.textContent = args.text;
                // 通知 React/ProseMirror 输入变更
                ta.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""", {"text": content})

        await asyncio.sleep(0.5)

        # 确保 send-button 可用
        send_btn = self._minimax_page.locator('[data-testid="send-button"]')
        btn_count = await send_btn.count()
        if btn_count == 0:
            yield ("error", "send-button not found")
            return

        btn_visible = await send_btn.first.is_visible()
        if not btn_visible:
            yield ("error", "send-button not visible")
            return

        await send_btn.first.click()
        logger.info(f"[Minimax] Message sent via UI: {content[:50]}...")

        await asyncio.sleep(2)

        # 从队列读取 SSE 结果
        while True:
            try:
                kind, value = await asyncio.wait_for(q.get(), timeout=180)
                yield (kind, value)
                if kind == "done":
                    break
            except asyncio.TimeoutError:
                logger.warning("[Minimax] timeout waiting for SSE response")
                yield ("error", "不见五陵豪杰墓，无花无酒锄作田。")
                break

    # ═══════════════════════════════════════════════════════════════════════
    # 讯飞星火 (xinghuo.xfyun.cn) 相关方法
    # ═══════════════════════════════════════════════════════════════════════

    async def ensure_xinghuo_ready(self, headless: bool = True):
        """确保讯飞星火浏览器已启动并登录。"""
        if self._xinghuo_page and not self._xinghuo_page.is_closed():
            return

        from playwright.async_api import async_playwright
        logger.info("[Xinghuo] Starting Xinghuo SparkDesk browser...")
        self._xinghuo_pw = await async_playwright().start()
        self._xinghuo_browser = await self._xinghuo_pw.chromium.launch_persistent_context(
            user_data_dir=self._xinghuo_user_data_dir,
            headless=headless,
            channel=_browser_channel(),
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
            ignore_default_args=["--enable-automation"],
        )
        self._xinghuo_page = self._xinghuo_browser.pages[0] if self._xinghuo_browser.pages else await self._xinghuo_browser.new_page()

        # 轻量 init_script：仅做基本反爬虫规避。API 在 / 上下文完全可用,
        # /desk 的重定向由网站 DevTools 检测引起，无需也不应强行阻止。
        await self._xinghuo_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        # 访问根路径，使用 / 上下文（API 正常工作）。缩短等待时间。
        await self._xinghuo_page.goto("https://xinghuo.xfyun.cn/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        # 检查登录状态（已登录的页面不会显示"登录"按钮，或会显示"退出"）
        is_logged = await self._xinghuo_page.evaluate("""() => {
            const t = document.body?.innerText || '';
            return !t.includes('登录') || t.includes('退出');
        }""")

        if not is_logged:
            if headless:
                raise RuntimeError("讯飞星火未登录，请先运行 python main.py --login xinghuo")
            # 非 headless 模式，等待用户登录（至少 2 分钟）
            logger.info("[Xinghuo] Not logged in, waiting for login...")
            for i in range(60):
                await asyncio.sleep(3)
                try:
                    check = await self._xinghuo_page.evaluate("""() => {
                        const t = document.body?.innerText || '';
                        return { logged: t.includes('退出') || !t.includes('登录') };
                    }""")
                    if check.get('logged'):
                        logger.info("[Xinghuo] Login successful!")
                        break
                except:
                    pass
            else:
                raise RuntimeError("讯飞星火登录超时")

        logger.info("[Xinghuo] SparkDesk browser ready")

    async def stream_xinghuo_chat(self, prompt: str, model_type: str = "4.0-ultra",
                                   thinking_enabled: bool = False,
                                   search_enabled: bool = False,
                                   inline_file_content: str | None = None,
                                   model_name: str | None = None,
                                   file_info: list | None = None,
                                   conversation_id: str = ""):
        """向讯飞星火发送消息并流式返回响应。

        Yields: (kind, value) 元组
            kind: "chat_id", "chunk", "done", "error"
        """
        if not self._xinghuo_page or self._xinghuo_page.is_closed():
            headless = CONFIG.get('_xinghuo_headless', True)
            await self.ensure_xinghuo_ready(headless=headless)
        if not self._xinghuo_page or self._xinghuo_page.is_closed():
            yield ("error", "Xinghuo page not available")
            return

        async with self._xinghuo_lock:
            try:
                current_url = self._xinghuo_page.url
                if 'xinghuo.xfyun.cn' not in current_url:
                    await self._xinghuo_page.goto("https://xinghuo.xfyun.cn/", wait_until="networkidle", timeout=60000)
                    await asyncio.sleep(5)

                content = inline_file_content if inline_file_content else prompt

                model_map = {
                    "4.0-ultra": "4.0Ultra",
                    "4.0": "4.0",
                    "3.5": "3.5",
                    "max": "max",
                    "pro": "pro",
                }
                selected_model = model_map.get(model_type, "4.0Ultra")

                # Step 1: 获取 chat_id（复用已有 或 创建新对话）
                if conversation_id and conversation_id != "0":
                    chat_id = conversation_id
                    logger.info(f"[Xinghuo] reusing existing chat_id: {chat_id}")
                else:
                    chat_id = await self._xinghuo_page.evaluate("""async () => {
                        try {
                            const createResp = await fetch('/iflygpt/u/chat-list/v1/create-chat-list', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({title: '新对话', chatType: 1}),
                                credentials: 'include'
                            });
                            const createData = await createResp.json();
                            return String(createData?.data?.id || '');
                        } catch(e) {
                            return '';
                        }
                    }""")

                if not chat_id:
                    yield ("error", "create chat failed")
                    return

                yield ("chat_id", chat_id)

                # Step 2: 如果有文件附件，上传到 OSS 并等待解析完成
                if file_info:
                    logger.info(f"[Xinghuo] Uploading {len(file_info)} file(s) for chat {chat_id}")
                    import json as _json
                    file_refs = []
                    for fi in file_info:
                        file_data = fi["data"]
                        file_name = fi["name"]
                        try:
                            upload_result = await self.upload_xinghuo_file_to_oss(file_data, file_name)
                            if upload_result.get("status") != 200:
                                logger.warning(f"[Xinghuo] OSS upload failed: {upload_result.get('text', '')[:200]}")
                                continue
                            file_url = upload_result.get("link", "")
                            if not file_url:
                                logger.warning(f"[Xinghuo] OSS upload returned no link")
                                continue
                            saved = await self.save_xinghuo_file(file_url, chat_id)
                            if saved.get("code") != 0:
                                logger.warning(f"[Xinghuo] saveFile failed: {saved}")
                                continue
                            status_result = await self.poll_xinghuo_file_status(chat_id)
                            if status_result.get("status") == "ready":
                                finfo = status_result["file"]
                                file_refs.append({
                                    "fileUrl": finfo.get("fileUrl", file_url),
                                    "fileId": finfo.get("id", ""),
                                    "fileName": finfo.get("fileName", file_name),
                                })
                                logger.info(f"[Xinghuo] File uploaded: {file_name} -> {finfo.get('fileUrl', '')[:80]}")
                            else:
                                logger.warning(f"[Xinghuo] File parse status: {status_result.get('status')}")
                        except Exception as e:
                            logger.warning(f"[Xinghuo] File upload exception: {e}")

                    # 替换 content JSON 中的 file_data 为文件引用
                    if file_refs and inline_file_content:
                        try:
                            content_obj = _json.loads(content)
                            ref_idx = [0]
                            def _replace_file_data(obj):
                                if isinstance(obj, dict):
                                    if obj.get("type") == "file" and "file_data" in obj.get("file", {}):
                                        if ref_idx[0] < len(file_refs):
                                            ref = file_refs[ref_idx[0]]
                                            ref_idx[0] += 1
                                            obj["file"] = {
                                                "fileUrl": ref["fileUrl"],
                                                "fileId": ref["fileId"],
                                                "fileName": ref["fileName"],
                                            }
                                    for v in obj.values():
                                        if isinstance(v, (dict, list)):
                                            _replace_file_data(v)
                                elif isinstance(obj, list):
                                    for item in obj:
                                        _replace_file_data(item)
                            _replace_file_data(content_obj)
                            content = _json.dumps(content_obj, ensure_ascii=False, separators=(',', ':'))
                        except Exception as e:
                            logger.warning(f"[Xinghuo] Failed to update content with file refs: {e}")

                # Step 3: 发送消息（FormData）+ 流式读取
                stream_state = await self._xinghuo_page.evaluate("""async (args) => {
                    window.__xh_stream_chunks = [];
                    window.__xh_stream_chunk_idx = 0;
                    window.__xh_stream_done = false;
                    window.__xh_stream_error = '';
                    window.__xh_stream_chat_id = args.chatId;

                    try {
                        const ts = String(+new Date);
                        const fd = ts.substring(ts.length - 6);
                        const body = new FormData();
                        body.append('fd', fd);
                        body.append('text', args.content);
                        body.append('isBot', '0');
                        body.append('clientType', '1');
                        body.append('chatId', args.chatId);

                        const resp = await fetch('/iflygpt-chat/u/chat_message/chat', {
                            method: 'POST',
                            body: body,
                            credentials: 'include',
                            headers: { 'Botweb': '1', 'clientType': '1' }
                        });

                        const ct = resp.headers.get('content-type') || '';
                        if (!ct.includes('text/event-stream')) {
                            const text = await resp.text();
                            window.__xh_stream_error = 'unexpected content-type: ' + ct + ' body: ' + text.substring(0, 200);
                            window.__xh_stream_done = true;
                            return { ok: false, error: window.__xh_stream_error };
                        }

                        // 流式读取 SSE，逐 chunk 解码
                        const reader = resp.body.getReader();
                        const decoder = new TextDecoder();
                        let buf = '';

                        while (true) {
                            const {value, done} = await reader.read();
                            if (done) break;

                            buf += decoder.decode(value, {stream: true});
                            const lines = buf.split('\\n');
                            buf = lines.pop() || '';

                            for (const line of lines) {
                                if (line.startsWith('data:')) {
                                    const raw = line.slice(5).trim();
                                    if (raw && raw !== '[DONE]') {
                                        try {
                                            const bin = atob(raw);
                                            const bytes = new Uint8Array(bin.length);
                                            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                                            const decoded = new TextDecoder('utf-8').decode(bytes);
                                            window.__xh_stream_chunks.push(decoded);
                                        } catch(e) {
                                            window.__xh_stream_chunks.push(raw);
                                        }
                                    }
                                }
                            }
                        }
                        window.__xh_stream_done = true;
                        return { ok: true, chatId: args.chatId };
                    } catch(e) {
                        window.__xh_stream_error = String(e);
                        window.__xh_stream_done = true;
                        return { ok: false, error: String(e) };
                    }
                }""", {"content": content, "chatId": chat_id, "model": selected_model})

                if not stream_state.get('ok'):
                    yield ("error", stream_state.get('error', 'unknown error'))
                    return

                chat_id = stream_state.get('chatId', '')
                if chat_id:
                    yield ("chat_id", chat_id)

                # 轮询读取新 chunk，逐个 yield
                empty_count = 0
                max_empty = 90
                last_idx = 0

                while empty_count < max_empty:
                    await asyncio.sleep(0.3)
                    state = await self._xinghuo_page.evaluate("""() => ({
                        done: window.__xh_stream_done,
                        error: window.__xh_stream_error,
                        chunkCount: (window.__xh_stream_chunks || []).length
                    })""")

                    chunk_count = state.get('chunkCount', 0)
                    if chunk_count > last_idx:
                        new_chunks = await self._xinghuo_page.evaluate("""(fromIdx) => {
                            const chunks = window.__xh_stream_chunks || [];
                            return chunks.slice(fromIdx);
                        }""", last_idx)
                        for chunk in new_chunks:
                            if chunk:
                                yield ("chunk", chunk)
                        last_idx = chunk_count
                        empty_count = 0
                    else:
                        empty_count += 1

                    if state.get('done'):
                        # 读取可能遗漏的最后一批 chunk
                        if chunk_count > last_idx:
                            final_chunks = await self._xinghuo_page.evaluate("""(fromIdx) => {
                                const chunks = window.__xh_stream_chunks || [];
                                return chunks.slice(fromIdx);
                            }""", last_idx)
                            for chunk in final_chunks:
                                if chunk:
                                    yield ("chunk", chunk)
                        err = state.get('error', '')
                        if err:
                            yield ("error", err)
                        break

                yield ("done", "")

            except Exception as e:
                logger.warning(f"[Xinghuo] stream_chat error: {e}")
                yield ("error", str(e))

    async def delete_xinghuo_conversation(self, chat_id: str):
        """删除单个讯飞星火对话。"""
        if not chat_id or not self._xinghuo_page or self._xinghuo_page.is_closed():
            return
        try:
            result = await self._xinghuo_page.evaluate("""async (chatId) => {
                try {
                    const resp = await fetch('/iflygpt/u/chat-list/v1/del-chat-list', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({chatListId: Number(chatId)}),
                        credentials: 'include'
                    });
                    const data = await resp.json();
                    return { ok: data.flag, data: data };
                } catch (e) {
                    return { error: String(e) };
                }
            }""", chat_id)
            if result.get("ok"):
                logger.info(f"[Xinghuo] deleted chat {chat_id}")
            else:
                logger.warning(f"[Xinghuo] delete failed: {result}")
        except Exception as e:
            logger.warning(f"[Xinghuo] delete exception: {e}")

    async def delete_all_xinghuo_conversations(self):
        """删除所有讯飞星火对话。"""
        try:
            if not self._xinghuo_page or self._xinghuo_page.is_closed():
                logger.warning("[Xinghuo] no page, skip batch delete")
                return 0, 0

            result = await self._xinghuo_page.evaluate("""async () => {
                try {
                    let pageNum = 1;
                    const pageSize = 30;
                    let totalDeleted = 0;
                    let totalFetched = 0;

                    while (true) {
                        const listResp = await fetch('/iflygpt/u/chat-list/v2/chat-list', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({chatType: 1, pageNum, pageSize}),
                            credentials: 'include'
                        });
                        const listData = await listResp.json();
                        const chats = listData?.data?.list || listData?.data || [];
                        totalFetched += chats.length;

                        if (chats.length === 0) break;

                        for (const c of chats) {
                            try {
                                const delResp = await fetch('/iflygpt/u/chat-list/v1/del-chat-list', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({chatListId: Number(c.id)}),
                                    credentials: 'include'
                                });
                                const delData = await delResp.json();
                                if (delData?.flag) totalDeleted++;
                            } catch(e) {}
                        }

                        if (chats.length < pageSize) break;
                        pageNum++;
                    }

                    return { deleted: totalDeleted, total: totalFetched };
                } catch (e) {
                    return { error: String(e) };
                }
            }""")
            deleted = result.get('deleted', 0) if isinstance(result, dict) else 0
            total = result.get('total', 0) if isinstance(result, dict) else 0
            logger.info(f"[Xinghuo] delete_all: deleted {deleted}/{total} chats")
            return deleted, total
        except Exception as e:
            logger.warning(f"[Xinghuo] delete_all exception: {e}")
            return 0, 0

    async def close_xinghuo(self):
        """关闭讯飞星火浏览器。"""
        try:
            if self._xinghuo_page and not self._xinghuo_page.is_closed():
                await self._xinghuo_page.close()
        except Exception as e:
            logger.debug(f"[Xinghuo] close page error: {e}")
        self._xinghuo_page = None
        try:
            if self._xinghuo_browser:
                await self._xinghuo_browser.close()
        except Exception as e:
            logger.debug(f"[Xinghuo] close browser error: {e}")
        self._xinghuo_browser = None
        try:
            if self._xinghuo_pw:
                await self._xinghuo_pw.stop()
        except Exception:
            pass
        self._xinghuo_pw = None
        logger.info("[Xinghuo] resources cleaned up")

    async def get_xinghuo_oss_sign(self) -> dict:
        """获取 OSS 上传签名。"""
        headless = CONFIG.get('_xinghuo_headless', True)
        await self.ensure_xinghuo_ready(headless=headless)
        return await self._xinghuo_page.evaluate("""async () => {
            const resp = await fetch('/iflygpt/oss/sign', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({business: 'chatdoc'}),
                credentials: 'include',
            });
            return await resp.json();
        }""")

    async def get_xinghuo_chatdoc_sign(self) -> dict:
        """获取 chatdoc 上传签名。"""
        headless = CONFIG.get('_xinghuo_headless', True)
        await self.ensure_xinghuo_ready(headless=headless)
        return await self._xinghuo_page.evaluate("""async () => {
            const resp = await fetch('/iflygpt/file_chat/getSign', {
                method: 'GET',
                credentials: 'include',
            });
            return await resp.json();
        }""")

    async def upload_xinghuo_file_to_oss(self, file_data: bytes, file_name: str) -> dict:
        """上传文件到讯飞 OSS 并返回 OSS 签名结果。"""
        headless = CONFIG.get('_xinghuo_headless', True)
        await self.ensure_xinghuo_ready(headless=headless)
        oss_sign_result = await self.get_xinghuo_oss_sign()
        if oss_sign_result.get("code") != 0:
            raise RuntimeError(f"OSS sign failed: {oss_sign_result}")
        sign_data = oss_sign_result["data"]
        authorization = sign_data["authorization"]
        date = sign_data["date"]
        url = sign_data["url"]
        host = sign_data["host"]
        policy_dir = sign_data["policyDir"]
        oss_filename = sign_data["fileName"]
        accessid = sign_data["accessid"]
        policy = sign_data["policy"]

        # 将二进制数据转为 base64，在浏览器端解码为 Blob
        import base64
        file_b64 = base64.b64encode(file_data).decode("ascii")

        resp = await self._xinghuo_page.evaluate("""async (args) => {
            const { fileB64, fileName, url, host, date, policyDir, ossFilename, accessid, policy, signature } = args;
            // base64 解码为 Uint8Array
            const raw = atob(fileB64);
            const arr = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
            const blob = new Blob([arr], { type: 'application/octet-stream' });

            // 构建 FormData
            const fd = new FormData();
            fd.append('key', policyDir + '/' + ossFilename);
            fd.append('OSSAccessKeyId', accessid);
            fd.append('policy', policy);
            fd.append('Signature', signature);
            fd.append('file', blob, fileName);

            try {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Host': host,
                        'Date': date,
                    },
                    body: fd,
                    credentials: 'omit',
                });
                const text = await resp.text();
                // 尝试提取 <Location> 中的 URL（上传成功后 OSS 返回 XML）
                const match = text.match(/<Location>([\\s\\S]*?)<\\/Location>/);
                const link = match ? match[1] : '';
                return { status: resp.status, link: link, text: text.slice(0, 500) };
            } catch(e) {
                return { status: 0, link: '', text: e.message };
            }
        }""", {
            "fileB64": file_b64,
            "fileName": file_name,
            "url": url,
            "host": host,
            "date": date,
            "policyDir": policy_dir,
            "ossFilename": oss_filename,
            "accessid": accessid,
            "policy": policy,
            "signature": authorization,
        })
        return resp

    async def save_xinghuo_file(self, file_url: str, chat_id: str) -> dict:
        """保存已上传的文件到聊天系统。"""
        sign_result = await self.get_xinghuo_chatdoc_sign()
        if sign_result.get("code") != 0:
            raise RuntimeError(f"Chatdoc sign failed: {sign_result}")
        sign_data = sign_result["data"]
        resp = await self._xinghuo_page.evaluate("""async (args) => {
            const params = new URLSearchParams();
            params.append('signature', args.signature);
            params.append('appId', args.appId);
            params.append('timestamp', String(args.timestamp));
            params.append('chatId', args.chatId);
            params.append('fileUrl', args.fileUrl);
            const resp = await fetch('/iflygpt/file_chat/saveFile?' + params.toString(), {
                method: 'POST',
                credentials: 'include',
            });
            return await resp.json();
        }""", {
            "signature": sign_data["signature"],
            "appId": sign_data["appId"],
            "timestamp": str(sign_data["timestamp"]),
            "chatId": chat_id,
            "fileUrl": file_url,
        })
        return resp

    async def poll_xinghuo_file_status(self, chat_id: str, max_wait: int = 30, interval: int = 2) -> dict:
        """轮询文件解析状态，返回解析结果。"""
        for i in range(max_wait // interval):
            await asyncio.sleep(interval)
            result = await self._xinghuo_page.evaluate("""async (args) => {
                const resp = await fetch('/iflygpt/file_chat/listFiles?chatId=' + args.chatId, {
                    method: 'GET',
                    credentials: 'include',
                });
                return await resp.json();
            }""", {"chatId": chat_id})
            files = result.get("data", {}).get("files", [])
            for f in files:
                status = f.get("status", "")
                if status == 2:
                    return {"status": "ready", "file": f}
                elif status == 3:
                    return {"status": "failed", "file": f}
        return {"status": "timeout"}

    async def upload_and_save_xinghuo_file(self, file_data: bytes, file_name: str, chat_id: str) -> dict:
        """完整的文件上传流程：OSS 上传 -> 保存 -> 轮询解析。"""
        upload_result = await self.upload_xinghuo_file_to_oss(file_data, file_name)
        if upload_result.get("status") != 200:
            raise RuntimeError(f"OSS upload failed: {upload_result}")
        saved = await self.save_xinghuo_file(upload_result.get("link", ""), chat_id)
        if saved.get("code") != 0:
            raise RuntimeError(f"Save file failed: {saved}")
        status_result = await self.poll_xinghuo_file_status(chat_id)
        if status_result.get("status") == "ready":
            file_info = status_result["file"]
            return {
                "success": True,
                "file_url": file_info.get("fileUrl", ""),
                "file_id": file_info.get("id", ""),
                "file_name": file_info.get("fileName", file_name),
            }
        return {"success": False, "reason": status_result.get("status", "unknown")}


browser_client = BrowserClient()




