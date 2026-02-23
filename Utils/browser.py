"""
Playwright browser management and fetch functions.
"""
import asyncio
import logging
import random
import re
import time
import importlib
from typing import Optional, Tuple, Union, List, Iterable, Dict, Any
from urllib.parse import urlparse
import os
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright._impl._errors import TargetClosedError

# -------------------------------------------------------------------
# Logging setup
# -------------------------------------------------------------------
LOG = logging.getLogger(__name__)
if not LOG.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s'
    )
    handler.setFormatter(formatter)
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG)

# -------------------------------------------------------------------
# Global constants
# -------------------------------------------------------------------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:116.0) Gecko/20100101 Firefox/116.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Vivaldi/7.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
]

_SCRAPER_PROXIES = os.environ.get("SCRAPER_PROXIES", "") and [
    p.strip() for p in os.environ.get("SCRAPER_PROXIES").split(",") if p.strip()
] or []

if not _SCRAPER_PROXIES:
    LOG.debug("No proxies configured — running proxy-free mode")

_BROWSER_SEMAPHORE = asyncio.Semaphore(3)

# -------------------------------------------------------------------
# SharedPlaywrightManager (optimised, lazy browser)
# -------------------------------------------------------------------
_cloudscraper_spec = importlib.util.find_spec("cloudscraper")
if _cloudscraper_spec is not None:
    import cloudscraper
else:
    cloudscraper = None

class SharedPlaywrightManager:
    """
    Optimised hybrid manager:
    - HTTP-first (cloudscraper → aiohttp) for most sites
    - Lazy browser creation (only when needed)
    - Auto-recreate browser when Cloudflare blocks detected
    - Sequential processing to minimise memory
    """
    _instance = None
    _lock = asyncio.Lock()

    DEFAULTS = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-gpu",
            "--single-process",
            "--disable-software-rasterizer",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-breakpad",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disable-sync",
            "--mute-audio",
            "--no-first-run",
        ],
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
        "timezone_id": "Africa/Lagos",
        "default_timeout": 45000,
        "myschool_timeout": 45000,
        "user_agent_list": _USER_AGENTS,
        "max_concurrency": 1,
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._playwright = None
            cls._instance._browser = None
            cls._instance._context = None
            cls._instance._active_page = None
            cls._instance._browser_uses = 0
            cls._instance._cleanup_lock = asyncio.Lock()
            cls._instance._browser_lock = asyncio.Lock()
        return cls._instance

    async def initialize(self, **kwargs):
        async with self._lock:
            if self._initialized:
                LOG.info("[INIT] Already initialised")
                return
            self._cfg = dict(self.DEFAULTS)
            self._cfg.update(kwargs)
            LOG.info("[INIT] 🚀 Starting Playwright (lazy browser creation)...")
            try:
                self._playwright = await async_playwright().start()
                self._sem = asyncio.Semaphore(self._cfg["max_concurrency"])
                self._initialized = True
                LOG.info("[INIT] ✅ Playwright started")
            except Exception as e:
                LOG.error(f"[INIT] ❌ Failed: {e}")
                raise

    async def _ensure_browser(self, force_new: bool = False):
        async with self._browser_lock:
            if not force_new and self._browser and self._context:
                LOG.debug("[ENSURE_BROWSER] ✅ Browser already exists")
                return
            if force_new and (self._browser or self._context):
                LOG.info("[ENSURE_BROWSER] ♻️ Force recreate - cleaning up old browser...")
                await self._cleanup_browser()
            LOG.info("[ENSURE_BROWSER] 🌐 Creating fresh browser + context...")
            try:
                self._browser = await asyncio.wait_for(
                    self._playwright.chromium.launch(
                        headless=self._cfg["headless"],
                        args=self._cfg["args"],
                        timeout=60000
                    ),
                    timeout=65
                )
                LOG.info("[ENSURE_BROWSER] ✅ Browser launched")
                ua = random.choice(self._cfg["user_agent_list"]) if self._cfg["user_agent_list"] else None
                LOG.info(f"[ENSURE_BROWSER] 🎭 Using UA: {ua[:50] if ua else 'None'}...")
                self._context = await asyncio.wait_for(
                    self._browser.new_context(
                        user_agent=ua,
                        viewport=self._cfg["viewport"],
                        locale=self._cfg["locale"],
                        timezone_id=self._cfg["timezone_id"],
                        java_script_enabled=True,
                        bypass_csp=True,
                        is_mobile=False,
                        has_touch=False,
                        color_scheme='light',
                        extra_http_headers={
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                            'Accept-Language': 'en-US,en;q=0.9',
                            'Accept-Encoding': 'gzip, deflate, br',
                            'DNT': '1',
                            'Connection': 'keep-alive',
                            'Upgrade-Insecure-Requests': '1',
                            'Sec-Fetch-Dest': 'document',
                            'Sec-Fetch-Mode': 'navigate',
                            'Sec-Fetch-Site': 'none',
                            'Sec-Fetch-User': '?1',
                        }
                    ),
                    timeout=30
                )
                self._context.set_default_timeout(self._cfg["default_timeout"])
                self._context.set_default_navigation_timeout(self._cfg["default_timeout"])
                self._browser_uses = 0
                LOG.info("[ENSURE_BROWSER] ✅ Context created")
            except asyncio.TimeoutError:
                LOG.error("[ENSURE_BROWSER] ❌ Timeout creating browser/context")
                await self._cleanup_browser()
                raise
            except Exception as e:
                LOG.error(f"[ENSURE_BROWSER] ❌ Failed: {e}")
                await self._cleanup_browser()
                raise

    async def _cleanup_browser(self):
        async with self._cleanup_lock:
            LOG.info("[CLEANUP_BROWSER] 🧹 Closing browser resources...")
            if self._active_page:
                try:
                    if not self._active_page.is_closed():
                        await asyncio.wait_for(self._active_page.close(), timeout=5)
                    LOG.debug("[CLEANUP_BROWSER]   Active page closed")
                except Exception as e:
                    LOG.debug(f"[CLEANUP_BROWSER]   Page close error: {e}")
                finally:
                    self._active_page = None
            if self._context:
                try:
                    await asyncio.wait_for(self._context.close(), timeout=10)
                    LOG.debug("[CLEANUP_BROWSER]   Context closed")
                except Exception as e:
                    LOG.debug(f"[CLEANUP_BROWSER]   Context close error: {e}")
                finally:
                    self._context = None
            if self._browser:
                try:
                    await asyncio.wait_for(self._browser.close(), timeout=15)
                    LOG.info("[CLEANUP_BROWSER] ✅ Browser closed gracefully")
                except asyncio.TimeoutError:
                    LOG.warning("[CLEANUP_BROWSER]   ⚠️ Browser close timeout, forcing...")
                    try:
                        if hasattr(self._browser, '_browser'):
                            proc = self._browser._browser
                            if proc and hasattr(proc, 'pid'):
                                import os, signal
                                os.kill(proc.pid, signal.SIGKILL)
                                LOG.info("[CLEANUP_BROWSER]   💀 Browser process killed")
                    except Exception as kill_e:
                        LOG.debug(f"[CLEANUP_BROWSER]   Kill error: {kill_e}")
                except Exception as e:
                    LOG.warning(f"[CLEANUP_BROWSER] ⚠️ Browser close error: {e}")
                finally:
                    self._browser = None
            self._browser_uses = 0

    async def _setup_page(self, page: Page) -> None:
        LOG.debug("[SETUP_PAGE] 🔧 Setting up page...")
        width = random.randint(1280, 1920)
        height = random.randint(720, 1080)
        await page.set_viewport_size({"width": width, "height": height})
        LOG.debug(f"[SETUP_PAGE] 📐 Viewport: {width}x{height}")
        try:
            await page.add_init_script(
                f"""
                Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
                delete navigator.__proto__.webdriver;
                window.chrome = {{
                    runtime: {{}},
                    loadTimes: function() {{}},
                    csi: function() {{}},
                    app: {{}}
                }};
                Object.defineProperty(navigator, 'plugins', {{ get: () => [1,2,3,4,5] }});
                Object.defineProperty(navigator, 'languages', {{ get: () => ['en-US','en'] }});
                Object.defineProperty(navigator, 'platform', {{ get: () => 'Win32' }});
                Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {random.choice([4,8,16])} }});
                Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {random.choice([4,8,16])} }});
                Object.defineProperty(screen, 'width', {{ get: () => {width} }});
                Object.defineProperty(screen, 'height', {{ get: () => {height} }});
                Object.defineProperty(screen, 'availWidth', {{ get: () => {width} }});
                Object.defineProperty(screen, 'availHeight', {{ get: () => {height - 40} }});
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({{ state: Notification.permission }}) :
                        originalQuery(parameters)
                );
                if (!navigator.getBattery) {{
                    navigator.getBattery = () => Promise.resolve({{
                        charging: true,
                        chargingTime: 0,
                        dischargingTime: Infinity,
                        level: {random.uniform(0.5, 1.0):.2f}
                   }});
                }}
                """
            )
            LOG.debug("[SETUP_PAGE] ✅ Anti-detection applied")
        except Exception as e:
            LOG.warning(f"[SETUP_PAGE] ⚠️ Init script failed: {e}")

        async def _abort(route):
            try:
                await route.abort()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass
        patterns = [
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico}",
            "**/*.css",
            "**/*.woff*",
            "**/*.ttf",
            "**/*analytics*",
            "**/*doubleclick*",
        ]
        for p in patterns:
            try:
                await page.route(p, _abort)
            except Exception:
                pass
        LOG.debug("[SETUP_PAGE] ✅ Resource blocking configured")

    def _is_cloudflare_blocked(self, status: int, title: str, body: str) -> bool:
        """
        Detect common Cloudflare / anti-bot blocks.
        """
        title = (title or "").lower()
        body = (body or "").lower()

        return (
            status == 403 or
            "just a moment" in title or
            "verifying you are human" in body or
            "checking your browser" in body or
            (
                "cloudflare" in body and
                (
                    "challenge" in body or
                    "ray id" in body or
                    "attention required" in body
                )
            )
        )

    async def _cloudscraper_fetch(self, url: str, timeout: int = 20) -> str:
        if cloudscraper is None:
            return ""
        LOG.debug(f"[CLOUDSCRAPER] 📡 Fetching {url[:60]}...")
        try:
            loop = asyncio.get_running_loop()
            def _sync_get():
                s = cloudscraper.create_scraper(
                    browser={
                        'browser': 'chrome',
                        'platform': random.choice(['windows', 'darwin', 'linux']),
                        'mobile': False
                    }
                )
                headers = {
                    'User-Agent': random.choice(self._cfg["user_agent_list"]),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate',
                    'Cache-Control': 'max-age=0',
                    'Connection': 'keep-alive',
                    'DNT': '1',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Sec-CH-UA': '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                    'Sec-CH-UA-Mobile': '?0',
                    'Sec-CH-UA-Platform': '"Windows"',
                }
                if random.random() > 0.3:
                    referers = ['https://www.google.com/', 'https://www.bing.com/', f'https://{urlparse(url).netloc}/']
                    headers['Referer'] = random.choice(referers)
                r = s.get(url, headers=headers, timeout=timeout)
                if r.status_code == 200:
                    content = r.content
                    if isinstance(content, str):
                        return content
                    encoding = r.encoding
                    if encoding:
                        try:
                            return content.decode(encoding)
                        except:
                            pass
                    for enc in ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']:
                        try:
                            return content.decode(enc)
                        except UnicodeDecodeError:
                            continue
                    return content.decode('utf-8', errors='replace')
                return ""
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_get),
                timeout=timeout + 5
            )
            if result and isinstance(result, str):
                sample = result[:1000]
                non_printable = sum(1 for c in sample if ord(c) < 32 and c not in '\n\r\t')
                if non_printable > len(sample) * 0.1:
                    LOG.warning(f"[CLOUDSCRAPER] ⚠️ Response appears to be binary ({non_printable} non-printable chars)")
                    return ""
            if result:
                LOG.debug(f"[CLOUDSCRAPER] ✅ Success: {len(result)} bytes")
            return result
        except Exception as e:
            LOG.debug(f"[CLOUDSCRAPER] ❌ Failed: {e}")
            return ""

    async def _aiohttp_fetch(self, url: str, timeout: int = 20) -> str:
        LOG.debug(f"[AIOHTTP] 📡 Fetching {url[:60]}...")
        import aiohttp
        parsed = urlparse(url)
        domain = parsed.netloc
        headers = {
            'User-Agent': random.choice(self._cfg["user_agent_list"]),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9,en-GB;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Cache-Control': 'max-age=0',
            'Connection': 'keep-alive',
            'DNT': '1',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Sec-CH-UA': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"',
            'Sec-CH-UA-Platform-Version': '"15.0.0"',
        }
        if random.random() > 0.3:
            referers = ['https://www.google.com/', 'https://www.google.com.ng/', 'https://www.bing.com/', f'https://{domain}/']
            headers['Referer'] = random.choice(referers)
        try:
            connector = aiohttp.TCPConnector(
                limit=10, limit_per_host=5, ttl_dns_cache=300, ssl=False
            )
            client_timeout = aiohttp.ClientTimeout(total=timeout, connect=10, sock_read=timeout)
            async with aiohttp.ClientSession(headers=headers, connector=connector, timeout=client_timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        result = await resp.text()
                        LOG.debug(f"[AIOHTTP] ✅ Success: {len(result)} bytes")
                        return result
                    LOG.debug(f"[AIOHTTP] ⚠️ Status {resp.status}")
        except Exception as e:
            LOG.debug(f"[AIOHTTP] ❌ Failed: {type(e).__name__}: {str(e)[:100]}")
        return ""

    async def fetch_html(
        self,
        url: str,
        wait_for_selector: Optional[str] = None,
        scroll_to_load: bool = False,
        timeout: Optional[int] = None,
        partial_on_timeout: bool = True,
        recreate_on_block: bool = True,
    ) -> str:
        LOG.info(f"[FETCH_HTML] 🚀 ENTRY → {url[:80]}")
        await self._ensure_browser()
        LOG.info("[FETCH_HTML] 📄 Creating fresh page...")
        page = None
        try:
            page = await asyncio.wait_for(self._context.new_page(), timeout=15)
            await self._setup_page(page)
            self._active_page = page
            self._browser_uses += 1
            LOG.info(f"[FETCH_HTML] ✅ Page created (browser_uses={self._browser_uses})")
        except Exception as e:
            LOG.error(f"[FETCH_HTML] ❌ Page creation failed: {e}")
            return ""
        main_document_body = None
        async def _on_response(response):
            nonlocal main_document_body
            try:
                if response.request.resource_type == "document":
                    if main_document_body is None:
                        main_document_body = await response.text()
                        LOG.debug(f"[FETCH_HTML] Captured response: {len(main_document_body)} bytes")
            except Exception:
                pass
        try:
            page.on("response", _on_response)
            parsed = urlparse(url.lower())
            hostname = parsed.hostname or ""
            is_myschool = "myschool.ng" in hostname
            wait_until = "load" if is_myschool else "domcontentloaded"
            goto_timeout = timeout or (self._cfg["myschool_timeout"] if is_myschool else self._cfg["default_timeout"])
            LOG.info(f"[FETCH_HTML] 🧭 Strategy: wait_until={wait_until}, timeout={goto_timeout}ms")
            LOG.info(f"[FETCH_HTML] 🌐 STARTING navigation...")
            nav_start = asyncio.get_event_loop().time()
            try:
                response = await page.goto(url, wait_until=wait_until, timeout=goto_timeout)
                nav_duration = asyncio.get_event_loop().time() - nav_start
                if response:
                    status = response.status
                    LOG.info(f"[FETCH_HTML] ✅ Navigation SUCCESS in {nav_duration:.2f}s, Status: {status}")
                    await page.wait_for_timeout(1500)
                    page_title = await page.title()
                    body_text = await page.evaluate("() => document.body?.innerText || ''")
                    is_blocked = (
                        status == 403 or
                        'just a moment' in page_title.lower() or
                        'verifying you are human' in body_text.lower() or
                        'checking your browser' in body_text.lower() or
                        ('cloudflare' in body_text.lower() and ('challenge' in body_text.lower() or 'ray id' in body_text.lower()))
                    )
                    if is_blocked:
                        LOG.warning(f"[FETCH_HTML] ☁️ Cloudflare BLOCK detected!")
                        if recreate_on_block:
                            LOG.warning(f"[FETCH_HTML] ♻️ RECREATING browser to bypass block...")
                            try:
                                await asyncio.wait_for(page.close(), timeout=5)
                            except Exception:
                                pass
                            await self._ensure_browser(force_new=True)
                            await asyncio.sleep(random.uniform(3,7))
                            LOG.info(f"[FETCH_HTML] 🔄 Retrying with fresh browser...")
                            try:
                                page = await asyncio.wait_for(self._context.new_page(), timeout=15)
                                await self._setup_page(page)
                                self._active_page = page
                                page.on("response", _on_response)
                                await page.goto(url, wait_until=wait_until, timeout=goto_timeout)
                                await page.wait_for_timeout(2000)
                                retry_title = await page.title()
                                retry_body = await page.evaluate("() => document.body?.innerText || ''")
                                if self._is_cloudflare_blocked(200, retry_title, retry_body):
                                    LOG.error(f"[FETCH_HTML] ❌ Still blocked after browser recreation")
                                    return ""
                                LOG.info(f"[FETCH_HTML] ✅ Retry successful - block bypassed!")
                            except Exception as retry_e:
                                LOG.error(f"[FETCH_HTML] ❌ Retry failed: {retry_e}")
                                return ""
                        else:
                            return ""
                else:
                    LOG.info(f"[FETCH_HTML] ✅ No block detected")
            except PlaywrightTimeoutError as nav_error:
                nav_duration = asyncio.get_event_loop().time() - nav_start
                LOG.warning(f"[FETCH_HTML] ⏰ Navigation timeout after {nav_duration:.2f}s")
                if main_document_body:
                    LOG.info(f"[FETCH_HTML] 💾 Using captured response: {len(main_document_body)} bytes")
                    return main_document_body
                try:
                    partial_html = await page.content()
                    if partial_on_timeout and partial_html and len(partial_html) > 800:
                        LOG.info(f"[FETCH_HTML] ✅ Returning partial: {len(partial_html)} bytes")
                        return partial_html
                except Exception:
                    pass
                LOG.error(f"[FETCH_HTML] ❌ Timeout - no content")
                return ""
            await page.wait_for_timeout(1000)
            if scroll_to_load or is_myschool:
                LOG.info(f"[FETCH_HTML] 📜 Scrolling...")
                loops = 3 if is_myschool else 3
                for i in range(loops):
                    try:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                        await page.wait_for_timeout(1500 if is_myschool else 1000)
                    except Exception:
                        break
                await page.wait_for_timeout(800)
            if wait_for_selector:
                try:
                    LOG.info(f"[FETCH_HTML] ⏳ Waiting for selector...")
                    await page.wait_for_selector(wait_for_selector, timeout=10000)
                    LOG.info(f"[FETCH_HTML] ✅ Selector found")
                except Exception:
                    LOG.warning(f"[FETCH_HTML] ⚠️ Selector timeout")
            LOG.info(f"[FETCH_HTML] 📥 Getting final content...")
            html = await page.content()
            if main_document_body and len(main_document_body) > len(html):
                html = main_document_body
            LOG.info(f"[FETCH_HTML] 🎉 SUCCESS → {len(html)} bytes")
            return html
        except Exception as e:
            LOG.error(f"[FETCH_HTML] 💥 FATAL: {type(e).__name__}: {e}")
            import traceback
            LOG.error(f"[FETCH_HTML] Traceback:\n{traceback.format_exc()}")
            return ""
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass
            try:
                if page and not page.is_closed():
                    await asyncio.wait_for(page.close(), timeout=5)
                    LOG.debug("[FETCH_HTML] Page closed")
            except Exception:
                pass
            if self._active_page is page:
                self._active_page = None

    async def smart_fetch(
        self,
        url: str,
        *,
        prefer_http: bool = True,
        allow_playwright: bool = True,
        http_timeout: int = 12,
        play_timeout: Optional[int] = None,
        wait_for_selector: Optional[str] = None,
        scroll_to_load: bool = False,
        partial_on_timeout: bool = True,
        min_http_length: int = 800
    ) -> str:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        LOG.info(f"[SMART_FETCH] 🎯 ENTRY for {url[:60]}")
        if "myschool.ng" in hostname:
            LOG.info(f"[SMART_FETCH] 🎭 Using Playwright directly for MySchool")
            return await self.fetch_html(
                url,
                wait_for_selector=wait_for_selector,
                scroll_to_load=scroll_to_load,
                timeout=play_timeout,
                partial_on_timeout=partial_on_timeout,
                recreate_on_block=True
            )
        if prefer_http:
            LOG.info(f"[SMART_FETCH] ☁️ Trying cloudscraper...")
            html = await self._cloudscraper_fetch(url, http_timeout)
            if html and len(html) >= min_http_length:
                LOG.info(f"[SMART_FETCH] ✅ Cloudscraper success: {len(html)} bytes")
                return html
            LOG.info(f"[SMART_FETCH] 🌐 Trying aiohttp...")
            html = await self._aiohttp_fetch(url, http_timeout)
            if html and len(html) >= min_http_length:
                LOG.info(f"[SMART_FETCH] ✅ Aiohttp success: {len(html)} bytes")
                return html
        if allow_playwright:
            LOG.info(f"[SMART_FETCH] 🎭 Falling back to Playwright...")
            return await self.fetch_html(
                url,
                wait_for_selector=wait_for_selector,
                scroll_to_load=scroll_to_load,
                timeout=play_timeout,
                partial_on_timeout=partial_on_timeout,
                recreate_on_block=True
            )
        LOG.error(f"[SMART_FETCH] 💀 All methods failed for {url[:60]}")
        return ""

    async def run_concurrent(
        self,
        urls: Iterable[str],
        *,
        retries: int = 2,
        use_http_first: bool = True,
        allow_playwright: bool = True,
        fetch_kwargs: Optional[dict] = None,
    ) -> List[Tuple[str, str]]:
        if fetch_kwargs is None:
            fetch_kwargs = {}
        url_list = list(urls)
        results = []
        LOG.info(f"[run_concurrent] 🔄 Processing {len(url_list)} URLs")
        try:
            for idx, url in enumerate(url_list):
                LOG.info(f"[run_concurrent] [{idx+1}/{len(url_list)}] {url[:60]}...")
                attempt = 0
                html = ""
                while attempt <= retries:
                    attempt += 1
                    LOG.info(f"[run_concurrent]   Attempt {attempt}/{retries+1}")
                    try:
                        html = await self.smart_fetch(
                            url,
                            prefer_http=use_http_first,
                            allow_playwright=allow_playwright,
                            **fetch_kwargs
                        )
                        if html and len(html) > 800:
                            LOG.info(f"[run_concurrent]   ✅ Success: {len(html)} bytes")
                            break
                        else:
                            LOG.warning(f"[run_concurrent]   ⚠️ Empty/small: {len(html)} bytes")
                            if attempt <= retries:
                                await asyncio.sleep(3)
                    except Exception as e:
                        LOG.error(f"[run_concurrent]   ❌ Error: {e}")
                        if attempt <= retries:
                            await asyncio.sleep(3)
                results.append((url, html))
                if idx < len(url_list) - 1:
                    delay = random.uniform(5, 10)
                    LOG.info(f"[run_concurrent] ⏳ Waiting {delay:.1f}s before next URL...")
                    await asyncio.sleep(delay)
            if self._browser:
                LOG.info(f"[run_concurrent] 🧹 Cleaning up browser (used {self._browser_uses} times)...")
                try:
                    await asyncio.wait_for(self._cleanup_browser(), timeout=30)
                except asyncio.TimeoutError:
                    LOG.error("[run_concurrent] ⚠️ Browser cleanup timeout, forcing...")
                    self._browser = None
                    self._context = None
                    self._active_page = None
                    self._browser_uses = 0
            success_count = sum(1 for _, html in results if html and len(html) > 800)
            LOG.info(f"[run_concurrent] 📊 COMPLETE: {success_count}/{len(url_list)} successful")
            return results
        except Exception as e:
            LOG.error(f"[run_concurrent] 💥 FATAL: {e}")
            try:
                await asyncio.wait_for(self._cleanup_browser(), timeout=10)
            except Exception:
                self._browser = None
                self._context = None
                self._active_page = None
            raise

    async def cleanup(self):
        LOG.info("[CLEANUP] 🧹 Full cleanup...")
        try:
            await asyncio.wait_for(self._cleanup_browser(), timeout=30)
        except asyncio.TimeoutError:
            LOG.error("[CLEANUP] ⚠️ Browser cleanup timeout")
        if self._playwright:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=10)
                LOG.info("[CLEANUP] ✅ Playwright stopped")
            except Exception as e:
                LOG.warning(f"[CLEANUP] ⚠️ Playwright stop error: {e}")
            finally:
                self._playwright = None
        self._initialized = False
        LOG.info("[CLEANUP] ✅ Complete")

# Global instance
shared_playwright = SharedPlaywrightManager()

# -------------------------------------------------------------------
# Legacy standalone fetch
# -------------------------------------------------------------------
async def get_visible_text_playwright(page) -> str:
    """
    UPDATED: Mirroring your successful test strategies - focused ONLY on price elements
    This is based on your working test code that gets correct prices
    """
    try:
        result = await page.evaluate("""
            () => {
                const mainContainer = document.querySelector('div.productDetail_productDetailsContent__VV9__');
                if (mainContainer) {
                    return mainContainer.innerText || mainContainer.textContent || '';
                }
                const priceElements = document.querySelectorAll('''
                    div.shared_specialPrice__uIZ_i,
                    span.shared_price__gnso_,
                    span.shared_initialPrice__cTRSe,
                    div[data-testid="current-price"],
                    .priceBox_priceBoxPrice__i7paS
                ''');
                if (priceElements.length > 0) {
                    const texts = Array.from(priceElements).map(el => el.textContent.trim());
                    return texts.join(' ');
                }
                const allElements = document.querySelectorAll('*');
                const nairaTexts = [];
                for (const el of allElements) {
                    const text = el.textContent;
                    if (text && (text.includes('₦') || text.includes('NGN'))) {
                        nairaTexts.push(text.trim());
                    }
                }
                return nairaTexts.join(' ');
            }
        """)
        text = str(result).strip()
        text = re.sub(r'\s+', ' ', text)
        LOG.info("Extracted %d chars of price-focused text", len(text))
        return text
    except Exception as e:
        LOG.error("JS extraction failed: %s", str(e)[:80])
        return ""

async def fetch_with_playwright_aggressive(
    url: str,
    retries: int = 3,
    return_visible_text: bool = False,
    wait_for_selector: Optional[str] = None,
    wait_timeout: int = 15000,
    settle_after_selector_ms: int = 500
) -> Union[str, Tuple[str, str]]:
    """
    PRODUCTION FETCH FUNCTION - OPTIMISED FOR KONGA
    """
    LOG.info("╔═══════════════════════════════════════════════════════════════════╗")
    LOG.info("║ PRODUCTION FIXES (with wait_for_selector & settle support)        ║")
    LOG.info("╚═══════════════════════════════════════════════════════════════════╝")
    LOG.info(f"🌐 {url[:80]}")
    if wait_for_selector:
        LOG.info(f"⏳ Will wait for selector: {wait_for_selector} (timeout={wait_timeout}ms)")
    product_id = None
    if url:
        id_match = re.search(r'(\d{5,})$', url)
        if id_match:
            product_id = int(id_match.group(1))
            LOG.info(f"🎯 Targeting Product ID: {product_id}")
    is_konga = 'konga' in url.lower()
    async with _BROWSER_SEMAPHORE:
        for attempt in range(1, retries + 1):
            LOG.info(f"┌── ATTEMPT {attempt}/{retries} ───────────────────────────────────────────────┐")
            browser = None
            context = None
            page = None
            try:
                async with async_playwright() as p:
                    start = time.time()
                    browser = await p.chromium.launch(
                        headless=True,
                        args=[
                            '--no-sandbox', '--no-zygote', '--disable-blink-features=AutomationControlled',
                            '--disable-dev-shm-usage', '--disable-web-security', '--disable-setuid-sandbox',
                            '--single-process', '--disable-gpu', '--disable-software-rasterizer',
                            '--disable-background-networking', '--disable-background-timer-throttling',
                            '--disable-renderer-backgrounding', '--disable-features=IsolateOrigins,site-per-process',
                        ],
                        timeout=60000
                    )
                    context = await browser.new_context(
                        user_agent=random.choice(_USER_AGENTS),
                        viewport={'width': 1366, 'height': 768},
                        locale='en-US',
                        timezone_id='Africa/Lagos',
                        java_script_enabled=True,
                        bypass_csp=True
                    )
                    context.set_default_timeout(30000)
                    context.set_default_navigation_timeout(30000)
                    page = await context.new_page()
                    await page.route("**/*.{gif,webp,svg}", lambda route: route.abort())
                    await page.route("**/*.css", lambda route: route.abort())
                    await page.route("**/*.woff*", lambda route: route.abort())
                    await page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                        window.chrome = {runtime: {}};
                    """)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        LOG.info("  ✓ Navigation complete (domcontentloaded)")
                    except Exception as nav_error:
                        LOG.warning(f"Navigation timeout, trying load: {str(nav_error)[:60]}")
                        await page.goto(url, wait_until="load", timeout=20000)
                        LOG.info("  ✓ Navigation complete (load)")
                    if wait_for_selector:
                        LOG.info(f"  ⏳ Waiting for selector: {wait_for_selector}")
                        try:
                            await page.wait_for_selector(wait_for_selector, timeout=wait_timeout)
                            LOG.info(f"  ✓ Selector found: {wait_for_selector}")
                            try:
                                await page.wait_for_timeout(settle_after_selector_ms)
                                LOG.debug(f"  ✓ Settled for {settle_after_selector_ms}ms after selector")
                            except Exception:
                                pass
                        except Exception as wait_error:
                            LOG.warning(f"  ⚠️ Selector wait timeout ({wait_timeout}ms): {str(wait_error)[:60]}")
                    if is_konga:
                        LOG.info("  🔍 Konga: Waiting for main product content...")
                        try:
                            await page.wait_for_selector(
                                'div.productDetail_productDetailsContent__VV9__',
                                timeout=8000
                            )
                            LOG.debug("✅ Main product container loaded")
                            await page.wait_for_timeout(settle_after_selector_ms)
                        except Exception:
                            LOG.debug("Main container timeout, continuing...")
                        try:
                            await page.wait_for_selector(
                                '.priceBox_priceBoxPrice__i7paS, div.shared_specialPrice__uIZ_i',
                                timeout=5000
                            )
                            LOG.debug("✅ Price elements loaded")
                            await page.wait_for_timeout(settle_after_selector_ms)
                        except Exception:
                            LOG.debug("Price elements timeout, continuing...")
                        try:
                            await page.wait_for_timeout(max(300, settle_after_selector_ms))
                        except Exception:
                            pass
                        try:
                            await page.evaluate("window.scrollBy(0, 300)")
                            await asyncio.sleep(0.5)
                        except:
                            pass
                    try:
                        await page.wait_for_timeout(max(250, settle_after_selector_ms // 2))
                        LOG.debug("  ✓ Final settle before content extraction")
                    except Exception:
                        pass
                    html = await page.content()
                    visible_text = ""
                    if return_visible_text or is_konga:
                        visible_text = await get_visible_text_playwright(page)
                    duration = time.time() - start
                    LOG.info(f"└── SUCCESS {duration:.1f}s | HTML:{len(html)} | Text:{len(visible_text)} ───────────────────────────────┘")
                    return (html, visible_text) if return_visible_text else html
            except Exception as e:
                error_type = type(e).__name__
                if error_type == 'TargetClosedError':
                    LOG.warning(f"TargetClosedError while fetching {url}")
                elif error_type == 'CancelledError':
                    LOG.info("Fetch cancelled")
                    raise
                else:
                    LOG.error(f"└── FAILED: {error_type}: {str(e)[:100]}")
            finally:
                for resource in [page, context, browser]:
                    if resource:
                        try:
                            await resource.close()
                        except:
                            pass
                if attempt < retries:
                    wait_time = min(2 ** attempt + random.uniform(0, 1), 10)
                    LOG.info(f"  ⏳ Waiting {wait_time:.1f} seconds before retry...")
                    await asyncio.sleep(wait_time)
                else:
                    LOG.error(f"  ❌ All {retries} attempts exhausted")
    raise Exception(f"Failed to fetch {url} after {retries} attempts")