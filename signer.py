import asyncio
import logging
import uuid
from typing import Optional, Dict
from urllib.parse import urlencode, quote

from playwright.async_api import async_playwright, Browser, Page, TimeoutError
from playwright_stealth import Stealth

logger = logging.getLogger("doubao-signer")


class PlaywrightSigner:
    def __init__(self, cookie: str, device_id: str, web_id: str, tea_uuid: str, fp: str = ""):
        self.cookie = cookie
        self.device_id = device_id
        self.web_id = web_id
        self.tea_uuid = tea_uuid
        self.fp = fp
        self.ms_token: Optional[str] = None
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        if self._initialized:
            return True
        async with self._lock:
            if self._initialized:
                return True
            try:
                logger.info("Initializing Playwright signer...")
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=True,
                    channel="msedge",
                    args=["--no-sandbox", "--disable-setuid-sandbox"]
                )
                stealth = Stealth()
                stealth.hook_playwright_context(self.playwright)
                self.page = await self.browser.new_page()
                await stealth.apply_stealth_async(self.page)
                await self.page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                async def _handle_response(response):
                    try:
                        if 'x-ms-token' in response.headers:
                            token = response.headers['x-ms-token']
                            if token != self.ms_token:
                                self.ms_token = token
                                logger.info("Captured msToken from response header")
                    except Exception:
                        pass

                self.page.on("response", _handle_response)

                cookie_list = []
                for c in self.cookie.split(';'):
                    c = c.strip()
                    if '=' in c:
                        name, value = c.split('=', 1)
                        cookie_list.append({
                            "name": name.strip(),
                            "value": value.strip(),
                            "domain": ".doubao.com",
                            "path": "/"
                        })

                if cookie_list:
                    await self.page.context.add_cookies(cookie_list)
                    logger.info(f"Set {len(cookie_list)} cookies")

                logger.info("Navigating to doubao.com to load signing script...")
                await self.page.goto(
                    "https://www.doubao.com/chat/",
                    wait_until="load",
                    timeout=60000
                )
                logger.info("Page loaded, waiting for bdms.frontierSign...")
                
                try:
                    await self.page.wait_for_function(
                        "() => typeof window.bdms?.frontierSign === 'function'",
                        timeout=60000
                    )
                    logger.info("bdms.frontierSign function loaded successfully!")
                except TimeoutError:
                    bdms_check = await self.page.evaluate(
                        "() => ({ has_bdms: typeof window.bdms !== 'undefined', keys: typeof window.bdms === 'object' ? Object.keys(window.bdms) : [], has_byted: typeof window.byted_acrawler !== 'undefined' })"
                    )
                    logger.error(f"bdms.frontierSign not found. State: {bdms_check}")
                    raise TimeoutError("bdms.frontierSign function not loaded within timeout")

                if not self.ms_token:
                    logger.info("Waiting for msToken (up to 10s)...")
                    await asyncio.sleep(10)

                self._initialized = True
                logger.info("Playwright signer initialized successfully")
                return True

            except TimeoutError as e:
                logger.error(f"Timeout during initialization: {e}")
                await self._cleanup()
                return False
            except Exception as e:
                logger.error(f"Failed to initialize Playwright signer: {e}")
                await self._cleanup()
                return False

    async def _cleanup(self):
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.browser = None
        self.page = None
        self.playwright = None
        self._initialized = False

    async def get_signed_url(self, base_url: str, base_params: Dict[str, str]) -> Optional[str]:
        async with self._lock:
            if not self._initialized:
                raise RuntimeError("PlaywrightSigner not initialized")

            try:
                final_params = base_params.copy()
                final_params['device_id'] = self.device_id
                final_params['web_id'] = self.web_id
                final_params['tea_uuid'] = self.tea_uuid
                if self.fp:
                    final_params['fp'] = self.fp
                final_params['web_tab_id'] = str(uuid.uuid4())

                if self.ms_token:
                    final_params['msToken'] = self.ms_token
                else:
                    logger.warning("msToken not available, signing may fail")

                sorted_params = dict(sorted(final_params.items()))
                query_string = urlencode(sorted_params)

                logger.info("Calling bdms.frontierSign...")
                signature_obj = await self.page.evaluate(
                    f'window.bdms.frontierSign("{query_string}")'
                )

                if isinstance(signature_obj, dict):
                    bogus_value = signature_obj.get('a_bogus') or signature_obj.get('X-Bogus')
                    if bogus_value:
                        bogus_key = 'a_bogus' if 'a_bogus' in signature_obj else 'X-Bogus'
                        logger.info(f"Got {bogus_key}: {bogus_value[:20]}...")
                        signed_url = f"{base_url}?{query_string}&{bogus_key}={quote(bogus_value, safe='')}"
                        return signed_url
                    else:
                        logger.error(f"frontierSign returned dict without a_bogus/X-Bogus: {list(signature_obj.keys())}")
                        return None
                else:
                    logger.error(f"frontierSign returned unexpected format: {type(signature_obj)}")
                    return None

            except Exception as e:
                logger.error(f"Signing error: {e}")
                return None

    def update_ms_token(self, token: str):
        if token and token != self.ms_token:
            self.ms_token = token
            logger.info("msToken updated from response header")

    async def close(self):
        await self._cleanup()
        logger.info("Playwright signer closed")
