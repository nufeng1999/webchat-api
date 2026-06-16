import json
import os
import logging
import asyncio
import time
from datetime import datetime


class Colors:
    """ANSI 颜色静态类，方便其它模块使用。"""
    BLACK   = "\033[30m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    BOLD_RED     = "\033[1;31m"
    BOLD_GREEN   = "\033[1;32m"
    BOLD_YELLOW  = "\033[1;33m"
    RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG':   Colors.GREEN,
        # 'INFO':    Colors.BLUE,
        'WARNING': Colors.YELLOW,
        'ERROR':   Colors.RED,
        'CRITICAL':Colors.MAGENTA,
    }

    def format(self, record):
        color = self.COLORS.get(record.levelname, '')
        record.levelname = f'{color}[{record.levelname}]{Colors.RESET}'
        return super().format(record)

logger = logging.getLogger("doubao-api")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
ACCOUNTS_PATH = os.path.join(BASE_DIR, 'accounts.json')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
CONVERSATION_DIR = os.path.join(BASE_DIR, 'conversations')

def escape_md_for_json(text: str) -> str:
    """将 Markdown 文本转义为 JSON 安全的字符串，确保 json.dumps 后不会破坏结构。"""
    return text.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t').replace('"', '\\"')

def get_webchat_task() -> str:
    """从 request_task.md 文件读取 webchat_task 配置。"""
    try:
        task_path = os.path.join(BASE_DIR, "request_task.md")
        if os.path.exists(task_path):
            with open(task_path, 'r', encoding='utf-8') as f:
                return escape_md_for_json(f.read().strip())
    except Exception as e:
        logger.warning(f"Failed to read request_task.md: {e}")
    return ""

def get_ret_format_prompt() -> str:
    """从 ret_format_task.md 文件读取 ret_format_prompt 配置。"""
    try:
        task_path = os.path.join(BASE_DIR, "ret_format_task.md")
        if os.path.exists(task_path):
            with open(task_path, 'r', encoding='utf-8') as f:
                return escape_md_for_json(f.read().strip())
    except Exception as e:
        logger.warning(f"Failed to read ret_format_task.md: {e}")
    return ""

def get_exectask_prompt() -> str:
    """从 exectask_prompt.md 文件读取 exectask_prompt 配置。"""
    try:
        task_path = os.path.join(BASE_DIR, "exectask_prompt.md")
        if os.path.exists(task_path):
            with open(task_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return f"[{timestamp}]\n{escape_md_for_json(content)}"
    except Exception as e:
        logger.warning(f"Failed to read exectask_prompt.md: {e}")
    return ""

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONVERSATION_DIR, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

COOKIE_EXPIRY_PATTERNS = [
    "login",
    "verify",
    "captcha",
    "forbidden",
    "unauthorized",
    "rate_limit",
    "710022004",
    "need_verify",
    "risk_check",
]

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def reload_config():
    global CONFIG, SIGN_METHOD
    CONFIG = load_config()
    SIGN_METHOD = CONFIG.get('sign_method', 'b3')
    logger.info("Config reloaded")

def setup_logging():
    """设置全局日志格式，带颜色。"""
    import sys
    
    # Windows 控制台启用 ANSI 颜色
    if sys.platform == 'win32':
        try:
            from colorama import init
            init(autoreset=True)
        except ImportError:
            pass

    fmt = '%(asctime)s %(levelname)s %(name)s: %(message)s'
    date_fmt = '%Y-%m-%d %H:%M:%S'
    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter(fmt, datefmt=date_fmt))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 清除默认 handler，避免重复
    if not root.handlers:
        root.addHandler(handler)

setup_logging()

def load_accounts():
    if os.path.exists(ACCOUNTS_PATH):
        with open(ACCOUNTS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_accounts(accounts):
    with open(ACCOUNTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)

CONFIG = load_config()
ACCOUNTS = load_accounts()
SIGN_METHOD = CONFIG.get('sign_method', 'b3')

signer = None
if SIGN_METHOD == 'b2':
    try:
        from signer import PlaywrightSigner
        signer = PlaywrightSigner(
            cookie=CONFIG.get('cookie', ''),
            device_id=CONFIG.get('device_id', ''),
            web_id=CONFIG.get('web_id', ''),
            tea_uuid=CONFIG.get('tea_uuid', ''),
            fp=CONFIG.get('fp', '')
        )
        logger.info("B2 Playwright signer module loaded (will initialize on startup)")
    except ImportError as e:
        logger.error(f"Failed to import signer module: {e}")
        SIGN_METHOD = 'b3'
        signer = None


class CookiePool:
    def __init__(self):
        self.accounts = []
        self.current_index = 0
        self._last_refresh = time.time()
        self._refresh_interval = 3600
        self._init_pool()

    def _init_pool(self):
        primary = {
            "name": "primary",
            "cookie": CONFIG.get('cookie', ''),
            "device_id": CONFIG.get('device_id', ''),
            "web_id": CONFIG.get('web_id', ''),
            "tea_uuid": CONFIG.get('tea_uuid', ''),
            "room_id": CONFIG.get('room_id', ''),
            "fail_count": 0,
            "last_fail": None,
            "last_success": None,
            "enabled": True
        }
        self.accounts = [primary]
        for acc in ACCOUNTS:
            self.accounts.append({
                "name": acc.get("name", "unknown"),
                "cookie": acc.get("cookie", ""),
                "device_id": acc.get("device_id", CONFIG.get('device_id', '')),
                "web_id": acc.get("web_id", CONFIG.get('web_id', '')),
                "tea_uuid": acc.get("tea_uuid", CONFIG.get('tea_uuid', '')),
                "room_id": acc.get("room_id", CONFIG.get('room_id', '')),
                "fail_count": 0,
                "last_fail": None,
                "last_success": None,
                "enabled": True
            })
        logger.info(f"Cookie pool initialized: {len(self.accounts)} accounts")

    def get_next(self) -> dict:
        available = [a for a in self.accounts if a["enabled"] and a["cookie"]]
        if not available:
            logger.warning("No available accounts, re-enabling all")
            for a in self.accounts:
                a["enabled"] = True
                a["fail_count"] = 0
            available = [a for a in self.accounts if a["cookie"]]
        if not available:
            return self.accounts[0] if self.accounts else {}

        account = available[self.current_index % len(available)]
        self.current_index = (self.current_index + 1) % len(available)
        return account

    def report_fail(self, account: dict, reason: str = ""):
        account["fail_count"] += 1
        account["last_fail"] = datetime.now().isoformat()
        if account["fail_count"] >= 3:
            account["enabled"] = False
            logger.warning(f"Account '{account['name']}' disabled after 3 failures. Reason: {reason}")
        else:
            logger.warning(f"Account '{account['name']}' failure #{account['fail_count']}. Reason: {reason}")

    def report_success(self, account: dict):
        account["fail_count"] = 0
        account["last_success"] = datetime.now().isoformat()

    def is_cookie_expired(self, response_text: str, status_code: int = 200) -> bool:
        if status_code in (401, 403):
            return True
        text_lower = response_text.lower()
        for pattern in COOKIE_EXPIRY_PATTERNS:
            if pattern in text_lower:
                return True
        return False

    def refresh_cookies(self):
        try:
            extract_script = os.path.join(BASE_DIR, "temp", "extract_session.py")
            if os.path.exists(extract_script):
                import subprocess
                result = subprocess.run(
                    ["python", extract_script],
                    capture_output=True, text=True, timeout=30,
                    cwd=BASE_DIR
                )
                if result.returncode == 0:
                    reload_config()
                    for acc in self.accounts:
                        if acc["name"] == "primary":
                            acc["cookie"] = CONFIG.get('cookie', acc["cookie"])
                            acc["device_id"] = CONFIG.get('device_id', acc["device_id"])
                            acc["web_id"] = CONFIG.get('web_id', acc["web_id"])
                            acc["tea_uuid"] = CONFIG.get('tea_uuid', acc["tea_uuid"])
                            acc["enabled"] = True
                            acc["fail_count"] = 0
                    self._last_refresh = time.time()
                    logger.info("Cookies refreshed successfully from extract_session.py")
                    return True
                else:
                    logger.warning(f"Cookie refresh script failed: {result.stderr[:200]}")
                    return False

            logger.info("extract_session.py not found, trying browser login flow...")
            try:
                asyncio.get_running_loop()
                logger.warning("Event loop already running, cannot launch sync login flow here. "
                               "Run 'python main.py --login' manually to refresh cookies.")
                return False
            except RuntimeError:
                pass

            from login import do_login
            result = asyncio.run(do_login(show_browser=True))
            if result.get("success"):
                reload_config()
                for acc in self.accounts:
                    if acc["name"] == "primary":
                        acc["cookie"] = CONFIG.get('cookie', acc["cookie"])
                        acc["device_id"] = CONFIG.get('device_id', acc["device_id"])
                        acc["web_id"] = CONFIG.get('web_id', acc["web_id"])
                        acc["tea_uuid"] = CONFIG.get('tea_uuid', acc["tea_uuid"])
                        acc["enabled"] = True
                        acc["fail_count"] = 0
                self._last_refresh = time.time()
                logger.info("Cookies refreshed via browser login flow")
                return True
            return False
        except Exception as e:
            logger.error(f"Cookie refresh failed: {e}")
            return False

    def maybe_refresh(self):
        if time.time() - self._last_refresh > self._refresh_interval:
            all_disabled = all(not a["enabled"] for a in self.accounts)
            if all_disabled:
                logger.info("All accounts disabled, attempting cookie refresh...")
                return self.refresh_cookies()
        return False

    def status(self) -> list:
        return [{
            "name": a["name"],
            "enabled": a["enabled"],
            "fail_count": a["fail_count"],
            "last_fail": a["last_fail"],
            "last_success": a["last_success"],
            "cookie_length": len(a.get("cookie", ""))
        } for a in self.accounts]


cookie_pool = CookiePool()


class RateLimiter:
    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        if key not in self._requests:
            self._requests[key] = []
        self._requests[key] = [t for t in self._requests[key] if now - t < self.window_seconds]
        if len(self._requests[key]) >= self.max_requests:
            return False
        self._requests[key].append(now)
        return True

    def get_status(self, key: str) -> dict:
        now = time.time()
        if key not in self._requests:
            return {"remaining": self.max_requests, "reset_at": now + self.window_seconds}
        window = [t for t in self._requests[key] if now - t < self.window_seconds]
        remaining = max(0, self.max_requests - len(window))
        reset_at = min(window) + self.window_seconds if window else now + self.window_seconds
        return {"remaining": remaining, "reset_at": reset_at}


class ConcurrencyLimiter:
    def __init__(self, max_concurrent: int = 5):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_count = 0
        self._total_count = 0

    async def acquire(self):
        await self._semaphore.acquire()
        self._active_count += 1
        self._total_count += 1

    def release(self):
        self._semaphore.release()
        self._active_count = max(0, self._active_count - 1)

    @property
    def active(self) -> int:
        return self._active_count

    @property
    def total(self) -> int:
        return self._total_count


rate_limiter = RateLimiter(
    max_requests=CONFIG.get('rate_limit_max', 30),
    window_seconds=CONFIG.get('rate_limit_window', 60)
)
concurrency_limiter = ConcurrencyLimiter(
    max_concurrent=CONFIG.get('max_concurrent', 5)
)


def save_conversation_log(user_input: str, ai_output: str, model: str, conversation_id: str = "", chat_id: str = "", image_urls: list = None):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    log_file = os.path.join(LOG_DIR, f"chat_{date_str}.jsonl")

    record = {
        "timestamp": now.isoformat(),
        "date": date_str,
        "time": time_str,
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "model": model,
        "user_input": user_input,
        "ai_output": ai_output,
        "output_length": len(ai_output),
        "image_urls": image_urls or []
    }

    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

    logger.info(f"Conversation logged: {date_str} {time_str} | user={len(user_input)}chars | ai={len(ai_output)}chars | images={len(image_urls or [])}")


def save_conversation_state(chat_id: str, messages: list, doubao_conv_id: str = "", model: str = ""):
    state_file = os.path.join(CONVERSATION_DIR, f"{chat_id}.json")
    state = {
        "chat_id": chat_id,
        "doubao_conversation_id": doubao_conv_id,
        "model": model,
        "updated_at": datetime.now().isoformat(),
        "messages": [{"role": m.role, "content": m.content} if hasattr(m, 'role') else m for m in messages]
    }
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_conversation_state(chat_id: str) -> dict:
    state_file = os.path.join(CONVERSATION_DIR, f"{chat_id}.json")
    if os.path.exists(state_file):
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}
