from ._shared import *
from ._doubao import DoubaoMixin
from ._qianwen import QianwenMixin
from ._deepseek import DeepSeekMixin
from ._zai import ZaiMixin
from ._mimo import MimoMixin
from ._minimax import MiniMaxMixin
from ._xinghuo import XinghuoMixin
from ._kimi import KimiMixin
from ._jimeng import JimengMixin


class BrowserClient(DoubaoMixin, QianwenMixin, DeepSeekMixin, ZaiMixin, MimoMixin, MiniMaxMixin, XinghuoMixin, KimiMixin, JimengMixin):
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

        # Jimeng 专属
        self._jimeng_pw = None
        self._jimeng_browser = None
        self._jimeng_page = None
        self._jimeng_lock = asyncio.Lock()
        self._jimeng_user_data_dir = os.path.join(BASE_DIR, "jimeng_profile")

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

        # Kimi 专属
        self._kimi_pw = None
        self._kimi_browser = None
        self._kimi_page = None
        self._kimi_lock = asyncio.Lock()
        self._kimi_user_data_dir = os.path.join(BASE_DIR, "kimi_profile")

    async def close(self):
        """关闭所有浏览器。"""
        # 先关闭页面，再关闭浏览器上下文，最后停止 Playwright，避免 driver 已断开后仍访问 page。
        for attr in ['_doubao_page', '_qianwen_page', '_deepseek_page', '_zai_page', '_mimo_page', '_minimax_page', '_xinghuo_page', '_jimeng_page']:
            page = getattr(self, attr, None)
            if page:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception as e:
                    logger.debug(f"Error closing page {attr}: {e}")
                setattr(self, attr, None)

        for attr in ['_doubao_browser', '_qianwen_browser', '_deepseek_browser', '_zai_browser', '_mimo_browser', '_minimax_browser', '_xinghuo_browser', '_jimeng_browser']:
            browser = getattr(self, attr, None)
            if browser:
                try:
                    await browser.close()
                except Exception as e:
                    logger.debug(f"Error closing browser {attr}: {e}")
                setattr(self, attr, None)

        for attr in ['_pw', '_doubao_pw', '_qianwen_pw', '_deepseek_pw', '_zai_pw', '_mimo_pw', '_minimax_pw', '_xinghuo_pw', '_jimeng_pw']:
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


browser_client = BrowserClient()
