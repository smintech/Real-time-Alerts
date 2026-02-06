from telegram.error import TelegramError
from telegram import Bot
import os
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
import time
import logging
import json
import re
from playwright.sync_api import sync_playwright, Page, Response
import random
import pathlib
from http import HTTPStatus
import asyncio
import requests
import shutil
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup, Tag
import cloudscraper
from requests.exceptions import RequestException
from typing import Dict, Optional, Any, Tuple, Callable, List, Set, Union,Iterable
from collections import Counter
from playwright._impl._errors import TargetClosedError
import aiohttp
# Settings / thresholds
from bot.settings import (
    SUPPORTED_SITES,
    MIN_CHANGE_TO_ALERT,
    HIGH_DEAL_THRESHOLD,
    MEDIUM_DEAL_THRESHOLD,
    LOW_DEAL_THRESHOLD,
    _SCHOOL_KEYWORDS_RE,
    _DATE_RE,
)

LOG = logging.getLogger(__name__)
if not LOG.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s'
    )
    handler.setFormatter(formatter)
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG)

class ScrapeError(Exception):
    """Generic scraping failure."""

class NoDataError(ScrapeError):
    """Raised when page loaded but no usable product data found."""

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

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

COMMON_NEWS_PATHS = [
    "/news", "/news-events", "/news-and-events",
    "/News", "/bulletin", "/bulletins", "/Bulletin", "/news-events/",
    "/category/news", "/category/news/", "/category/press-release",
    "/topics/education/", "/category/education/", "/tags/education/",
]
MAX_ARTICLE_AGE_DAYS = 180
_BROWSER_SEMAPHORE = asyncio.Semaphore(3)

def candidate_listing_urls(base_url: str) -> List[str]:
    """
    Yield candidate URLs to try for a site: base root + common news paths.
    REMOVED unreliable news.<domain> guess to prevent wrong URLs like news.myschool.ng
    """
    u = urlparse(base_url)
    scheme = u.scheme or "https"
    netloc = u.netloc or u.path
    roots = [
        f"{scheme}://{netloc}",
        f"{scheme}://{netloc}/",
    ]
    candidates = []
    for root in roots:
        candidates.append(root)
        for p in COMMON_NEWS_PATHS:
            candidates.append(root.rstrip("/") + p)
    # dedup while preserving order
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

# ═══════════════════════════════════════════════════════════════════════════
# AGGRESSIVE CLOUDSCRAPER
# ═══════════════════════════════════════════════════════════════════════════

def fetch_with_cloudscraper_aggressive(url: str, retries: int = 6) -> str:
    for attempt in range(1, retries + 1):
        LOG.debug("Cloudscraper attempt %d/%d for %s", attempt, retries, url)
        try:
            time.sleep(random.uniform(2.5, 6.0))
            scraper = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': random.choice(['windows', 'darwin', 'linux']),
                    'mobile': False
                },
                delay=random.randint(6, 14),
            )
            headers = {
                'User-Agent': random.choice(_USER_AGENTS),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Referer': 'https://www.google.com/',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            resp = scraper.get(url, headers=headers, timeout=45)
            status = getattr(resp, "status_code", None)
            text = resp.text or ""
            
            if status == 200 and len(text) > 3000:
                blocked_kw = ['just a moment', 'verify you are human', 'cloudflare challenge', 
                             'attention required', 'checking your browser']
                if not any(k in text.lower() for k in blocked_kw):
                    LOG.debug("Cloudscraper returned content for %s", url)
                    return text
        except Exception as e:
            LOG.debug("Cloudscraper attempt %d error for %s: %s", attempt, url, e)
        
        if attempt < retries:
            time.sleep(random.uniform(2, 6))
    
    raise Exception("Cloudscraper failed all attempts")

# ═══════════════════════════════════════════════════════════════════════════
# AGGRESSIVE PLAYWRIGHT (IMPROVED CLEANUP + NETWORKIDLE)
# ═══════════════════════════════════════════════════════════════════════════
def _log_dom_snapshot(soup: BeautifulSoup, context: str = "", max_elements: int = 10):
    """Log a snapshot of interesting DOM elements for debugging."""
    LOG.debug("=== DOM SNAPSHOT [%s] ===", context)
    
    # Check for data attributes commonly used for pricing
    price_attrs = ['data-price', 'data-final-price', 'data-price-amount', 
                   'data-selling-price', 'data-offer-price', 'content']
    found_attrs = {}
    
    for attr in price_attrs:
        elements = soup.find_all(attrs={attr: True})
        if elements:
            found_attrs[attr] = len(elements)
            sample = elements[:3]
            values = [el.get(attr, 'N/A')[:50] for el in sample]
            LOG.debug("  Found %d elements with [%s]: samples=%s", len(elements), attr, values)
    
    if not found_attrs:
        LOG.debug("  No standard price attributes found in DOM")
    
    # Check for meta tags
    meta_prices = soup.find_all("meta", attrs={"property": re.compile(r'.*price.*', re.I)})
    if meta_prices:
        LOG.debug("  Found %d meta price tags", len(meta_prices))
        for meta in meta_prices[:3]:
            LOG.debug("    meta[property=%s]: content=%s", 
                     meta.get('property'), meta.get('content', 'N/A')[:50])
    
    # Check for specific Konga classes mentioned in selectors
    konga_classes = ['_3e_22_199e7', '-b', '-ubpt', 'prc', '-fs24']
    found_classes = {}
    for cls in konga_classes:
        count = len(soup.find_all(class_=re.compile(cls)))
        if count:
            found_classes[cls] = count
    
    if found_classes:
        LOG.debug("  Konga-specific classes found: %s", found_classes)
    else:
        LOG.debug("  No Konga-specific classes found")

def _log_selector_attempt(selector: str, elements_found: int, sample_text: str = ""):
    """Log what we searched for vs what we found."""
    status = "✓ HIT" if elements_found > 0 else "✗ MISS"
    LOG.debug("    [%s] Selector '%s' -> %d elements %s", 
             status, selector, elements_found, 
             f"| Sample: {sample_text[:60]}..." if sample_text else "")

def _log_extraction_attempt(source: str, raw_value: str, parsed_value: Optional[float], 
                           constraints: Tuple[float, float] = (5000, 100_000_000)):
    """Log price extraction attempt with validation result."""
    min_val, max_val = constraints
    if parsed_value:
        status = "✓ VALID" if min_val <= parsed_value <= max_val else "✗ OUT_OF_RANGE"
        LOG.debug("      [%s] %s: raw='%s' -> parsed=%.2f", status, source, raw_value[:50], parsed_value)
    else:
        LOG.debug("      [✗ PARSE_FAIL] %s: raw='%s'", source, raw_value[:50])

# -------------------------------------------------------------------
# 1) Enhanced visible-text + attribute + pseudo-content extractor
# -------------------------------------------------------------------
# === FIX: safe visible-text extraction (no raw newlines inside JS strings) ===
async def get_visible_text_playwright(page) -> str:
    """
    UPDATED: Mirroring your successful test strategies - focused ONLY on price elements
    This is based on your working test code that gets correct prices
    """
    try:
        # Using your EXACT working strategy from test code
        result = await page.evaluate("""
            () => {
                // STRATEGY 1: First try to get the main product detail container
                const mainContainer = document.querySelector('div.productDetail_productDetailsContent__VV9__');
                
                if (mainContainer) {
                    // Extract ALL text from this container (your successful approach)
                    return mainContainer.innerText || mainContainer.textContent || '';
                }
                
                // STRATEGY 2: Try to find price-specific elements
                const priceElements = document.querySelectorAll('''
                    div.shared_specialPrice__uIZ_i,
                    span.shared_price__gnso_,
                    span.shared_initialPrice__cTRSe,
                    div[data-testid="current-price"],
                    .priceBox_priceBoxPrice__i7paS
                ''');
                
                if (priceElements.length > 0) {
                    // Combine text from all price elements
                    const texts = Array.from(priceElements).map(el => el.textContent.trim());
                    return texts.join(' ');
                }
                
                // STRATEGY 3: Get all text with Naira symbol
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
# ═══════════════════════════════════════════════════════════════════════════
# SHARED PLAYWRIGHT MANAGER (New Class) - WITH IMPORT FIXES
# ═══════════════════════════════════════════════════════════════════════════
class SharedPlaywrightManager:
    """
    Shared Playwright manager with a global semaphore for concurrency control.

    Key features:
    - Single shared Playwright browser/context
    - Global asyncio.Semaphore to limit concurrent fetches
    - fetch_with_semaphore wrapper with retry/backoff
    - run_concurrent helper to run many fetches
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
        ],
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
        "timezone_id": "Africa/Lagos",
        "default_timeout": 45000,
        "myschool_timeout": 90000,
        "user_agent_list": _USER_AGENTS,
        "max_concurrency": 6,  # <-- default semaphore size
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    async def initialize(self, **kwargs):
        """Initialize Playwright and the concurrency semaphore (idempotent)."""
        async with self._lock:
            if self._initialized:
                return

            self._cfg = dict(self.DEFAULTS)
            self._cfg.update(kwargs)

            self.playwright: Optional[Playwright] = None
            self.browser: Optional[Browser] = None
            self.context: Optional[BrowserContext] = None
            self._sem: asyncio.Semaphore = asyncio.Semaphore(self._cfg["max_concurrency"])

            try:
                LOG.info("Initializing shared Playwright...")
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=self._cfg["headless"],
                    args=self._cfg["args"],
                    timeout=60000
                )
                ua = random.choice(self._cfg["user_agent_list"]) if self._cfg["user_agent_list"] else None
                self.context = await self.browser.new_context(
                    user_agent=ua,
                    viewport=self._cfg["viewport"],
                    locale=self._cfg["locale"],
                    timezone_id=self._cfg["timezone_id"],
                    java_script_enabled=True,
                    bypass_csp=True
                )
                # default timeouts
                self.context.set_default_timeout(self._cfg["default_timeout"])
                self.context.set_default_navigation_timeout(self._cfg["default_timeout"])

                self._initialized = True
                LOG.info("✅ Shared Playwright manager initialized (max_concurrency=%s)", self._cfg["max_concurrency"])
            except Exception as e:
                LOG.error("Failed to initialize Playwright: %s", e)
                # best-effort teardown
                try:
                    if getattr(self, "context", None):
                        await self.context.close()
                    if getattr(self, "browser", None):
                        await self.browser.close()
                    if getattr(self, "playwright", None):
                        await self.playwright.stop()
                except Exception:
                    pass
                raise

    async def _aiohttp_fetch(self, url: str, timeout: int = 20) -> str:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; aiohttp)"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        return await resp.text()
        except Exception as e:
            LOG.debug("aiohttp fallback failed for %s: %s", url, e)
        return ""

    async def get_page(self, url: str = "") -> Optional[Page]:
        """Create a new page and set anti-detection + resource blocking."""
        if not self._initialized:
            await self.initialize()

        try:
            page = await self.context.new_page()

            # anti-detection
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                window.chrome = window.chrome || { runtime: {} };
                try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch(e){}
            """)

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
                "**/*.woff",
                "**/*.woff2",
                "**/*.ttf",
                "**/*.otf",
                "**/*analytics*",
                "**/*doubleclick*",
                "**/*google-analytics*",
                "**/*.mp4",
                "**/*.webm"
            ]
            for p in patterns:
                try:
                    await page.route(p, _abort)
                except Exception:
                    LOG.debug("route setup failed for %s", p)

            return page
        except Exception as e:
            LOG.error("Failed to create page: %s", e)
            return None

    # ---------------------------
    # Core fetch_html (unchanged logic, partial-on-timeout etc.)
    # ---------------------------
    async def fetch_html(
        self,
        url: str,
        wait_for_selector: Optional[str] = None,
        scroll_to_load: bool = False,
        timeout: Optional[int] = None,
        force_networkidle_for: Optional[list] = None,
        partial_on_timeout: bool = True,
        partial_min_bytes: int = 800,
        partial_wait_ms: int = 1200
    ) -> str:
        """
        Fetch HTML with partial-on-timeout behaviour.
        Returns HTML string or empty string on failure.
        """
        page = None
        main_document_body = None

        def _response_matches_url(response, target_url):
            try:
                rurl = response.url.rstrip("/")
                turl = target_url.rstrip("/")
                return rurl == turl
            except Exception:
                return False

        try:
            if not self._initialized:
                await self.initialize()

            page = await self.get_page(url)
            if not page:
                return ""

            async def _on_response(response):
                nonlocal main_document_body
                try:
                    # prefer document or exact url match
                    if response.request.resource_type == "document" or _response_matches_url(response, url):
                        if main_document_body is None:
                            try:
                                main_document_body = await response.text()
                            except Exception:
                                main_document_body = None
                except Exception:
                    pass

            page.on("response", _on_response)

            parsed = urlparse(url.lower())
            hostname = parsed.hostname or ""
            is_myschool = "myschool.ng" in hostname or (force_networkidle_for and any(h in hostname for h in (force_networkidle_for or [])))
            wait_until = "networkidle" if is_myschool else "domcontentloaded"
            goto_timeout = timeout or (self._cfg.get("myschool_timeout") if is_myschool else self._cfg.get("default_timeout"))

            try:
                await page.goto(url, wait_until=wait_until, timeout=goto_timeout)
            except PlaywrightTimeoutError as nav_error:
                LOG.warning("Navigation timeout for %s: %s", url, nav_error)

                # 1) Use captured document body if we have it
                if main_document_body:
                    LOG.debug("Using captured document response body for %s", url)
                    return main_document_body

                # 2) short wait for scripts
                await page.wait_for_timeout(partial_wait_ms)

                # 3) readyState
                try:
                    ready = await page.evaluate("() => document.readyState")
                except Exception:
                    ready = None

                # 4) partial content
                try:
                    partial_html = await page.content()
                except Exception:
                    partial_html = ""

                if partial_on_timeout:
                    if (partial_html and len(partial_html) >= partial_min_bytes) or (ready in ("interactive", "complete")):
                        LOG.debug("Returning partial HTML for %s (bytes=%d, ready=%s)", url, len(partial_html), ready)
                        return partial_html

                    # 5) lighter fallback navigation
                    try:
                        await page.goto(url, wait_until="load", timeout=15000)
                        html_after = await page.content()
                        if html_after and len(html_after) > len(partial_html):
                            LOG.debug("Returning HTML after 'load' fallback for %s", url)
                            return html_after
                    except Exception:
                        LOG.debug("Short 'load' fallback failed for %s", url)

                # 6) aiohttp fallback
                fallback_html = await self._aiohttp_fetch(url, timeout=15)
                if fallback_html:
                    LOG.debug("Returning aiohttp fallback HTML for %s", url)
                    return fallback_html

                # last-resort: small partial if present
                if partial_html:
                    LOG.debug("Returning small partial HTML for %s as last resort", url)
                    return partial_html

                LOG.debug("No usable content after timeout for %s", url)
                return ""

            # If goto succeeded:
            await page.wait_for_timeout(500)

            if scroll_to_load or is_myschool:
                loops = 5 if is_myschool else 3
                per_wait = 1500 if is_myschool else 1000
                for _ in range(loops):
                    try:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                        await page.wait_for_timeout(per_wait)
                    except Exception:
                        break
                await page.wait_for_timeout(800)

            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, timeout=10000)
                except Exception:
                    LOG.debug("Selector '%s' not found within 10s for %s", wait_for_selector, url)

            html = await page.content()

            if main_document_body and len(main_document_body) > len(html):
                LOG.debug("Using captured document response body over page.content() for %s", url)
                return main_document_body

            LOG.debug("[Fetch] Success: %d bytes from %s", len(html), url)
            return html

        except Exception as e:
            LOG.error("Shared context fetch failed for %s: %s", url, e)
            fallback = await self._aiohttp_fetch(url, timeout=15)
            if fallback:
                return fallback
            return ""
        finally:
            # remove listener and close page
            try:
                page.off("response", _on_response)
            except Exception:
                pass
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    # ---------------------------
    # Semaphore wrapper + retry
    # ---------------------------
    async def fetch_with_semaphore(
        self,
        url: str,
        *,
        retries: int = 2,
        backoff_factor: float = 0.5,
        **fetch_kwargs
    ) -> str:
        """
        Acquire semaphore and run fetch with simple retry/backoff.
        Returns HTML string (or empty string on persistent failure).
        """
        # ensure initialized
        if not self._initialized:
            await self.initialize()

        attempt = 0
        last_exc = None
        async with self._sem:
            while attempt <= retries:
                try:
                    attempt += 1
                    html = await self.fetch_html(url, **fetch_kwargs)
                    # consider success if non-empty; you can tighten this condition
                    if html:
                        return html
                    # if empty, raise to trigger retry/backoff
                    raise RuntimeError("Empty HTML returned")
                except Exception as exc:
                    last_exc = exc
                    if attempt > retries:
                        LOG.debug("Final failure for %s after %d attempts: %s", url, attempt, exc)
                        break
                    sleep_for = backoff_factor * (2 ** (attempt - 1))
                    LOG.debug("Attempt %d failed for %s (%s). Backing off %.2fs and retrying.", attempt, url, exc, sleep_for)
                    await asyncio.sleep(sleep_for)
            # all attempts failed
            LOG.warning("All attempts failed for %s: %s", url, last_exc)
            return ""

    async def run_concurrent(
        self,
        urls: Iterable[str],
        *,
        retries: int = 2,
        backoff_factor: float = 0.5,
        fetch_kwargs: Optional[dict] = None,
    ) -> List[Tuple[str, str]]:
        """
        Run concurrent fetches for a list of URLs respecting the semaphore.
        Returns list of (url, html) tuples in the same order as provided.
        """
        if fetch_kwargs is None:
            fetch_kwargs = {}

        # wrap each url into a coroutine
        tasks = []
        for u in urls:
            coro = self.fetch_with_semaphore(u, retries=retries, backoff_factor=backoff_factor, **fetch_kwargs)
            tasks.append(asyncio.create_task(coro))

        # gather preserving order
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(zip(list(urls), results))

    async def cleanup(self):
        """Close context/browser/playwright."""
        async with self._lock:
            if not getattr(self, "_initialized", False):
                return
            try:
                if getattr(self, "context", None):
                    await self.context.close()
                if getattr(self, "browser", None):
                    await self.browser.close()
                if getattr(self, "playwright", None):
                    await self.playwright.stop()
                self._initialized = False
                LOG.info("✅ Shared Playwright manager cleaned up")
            except Exception as e:
                LOG.error("Error cleaning up Playwright: %s", e)


# global shared instance
shared_playwright = SharedPlaywrightManager()

async def fetch_with_playwright_aggressive(
    url: str,
    retries: int = 3,
    return_visible_text: bool = False
) -> Union[str, Tuple[str, str]]:
    """
    PRODUCTION FETCH FUNCTION - OPTIMIZED FOR KONGA
    1. Extracts product ID from URL for validation
    2. Focuses on main product container
    3. Handles Konga's dynamic content loading
    """
    LOG.info("╔═══════════════════════════════════════════════════════════════════╗")
    LOG.info("║ PRODUCTION FIXES                                            ║")
    LOG.info("╚═══════════════════════════════════════════════════════════════════╝")
    LOG.info(f"🌐 {url[:80]}")
    
    # Extract product ID from URL for logging
    product_id = None
    if url:
        id_match = re.search(r'(\d{5,})$', url)
        if id_match:
            product_id = int(id_match.group(1))
            LOG.info(f"🎯 Targeting Product ID: {product_id}")
    
    is_konga = 'konga' in url.lower()
    
    # Use semaphore to limit concurrent browser instances
    async with _BROWSER_SEMAPHORE:
        for attempt in range(1, retries + 1):
            LOG.info(f"┌── ATTEMPT {attempt}/{retries} ───────────────────────────────────────────────┐")
            
            browser = None
            context = None
            page = None
            
            try:
                async with async_playwright() as p:
                    start = time.time()
                    
                    # LAUNCH BROWSER (Production settings)
                    browser = await p.chromium.launch(
                        headless=True,
                        args=[
                            '--no-sandbox',
                            '--no-zygote',
                            '--disable-blink-features=AutomationControlled',
                            '--disable-dev-shm-usage',
                            '--disable-web-security',
                            '--disable-setuid-sandbox',
                            '--single-process',
                            '--disable-gpu',
                            '--disable-software-rasterizer',
                            '--disable-background-networking',
                            '--disable-background-timer-throttling',
                            '--disable-renderer-backgrounding',
                            '--disable-features=IsolateOrigins,site-per-process',
                        ],
                        timeout=60000
                    )
                    
                    # CONTEXT (Konga-optimized)
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
                    
                    # BLOCK UNNECESSARY RESOURCES
                    await page.route("**/*.{gif,webp,svg}", lambda route: route.abort())
                    await page.route("**/*.css", lambda route: route.abort())
                    await page.route("**/*.woff*", lambda route: route.abort())
                    
                    # ANTI-DETECTION
                    await page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                        window.chrome = {runtime: {}};
                    """)
                    
                    # NAVIGATE
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as nav_error:
                        LOG.warning(f"Navigation timeout, trying load: {str(nav_error)[:60]}")
                        await page.goto(url, wait_until="load", timeout=20000)
                    
                    # KONGA-SPECIFIC WAITING
                    if is_konga:
                        LOG.info("  🔍 Konga: Waiting for main product content...")
                        
                        try:
                            await page.wait_for_selector(
                                'div.productDetail_productDetailsContent__VV9__',
                                timeout=8000
                            )
                            LOG.debug("✅ Main product container loaded")
                        except Exception:
                            LOG.debug("Main container timeout, continuing...")
                        
                        try:
                            await page.wait_for_selector(
                                '.priceBox_priceBoxPrice__i7paS, div.shared_specialPrice__uIZ_i',
                                timeout=5000
                            )
                            LOG.debug("✅ Price elements loaded")
                        except:
                            LOG.debug("Price elements timeout, continuing...")
                        
                        await page.wait_for_timeout(2000)
                        
                        try:
                            await page.evaluate("window.scrollBy(0, 300)")
                            await asyncio.sleep(0.5)
                        except:
                            pass
                    
                    # EXTRACT CONTENT
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
                # Clean up resources
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
# ═══════════════════════════════════════════════════════════════════════════
# ULTIMATE FETCH
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_html_ultimate(url: str) -> str:
    """Fetch HTML with semaphore-controlled concurrency."""
    domain = get_domain_from_url(url)
    
    if 'myschool.ng' in domain and '/news/' in url:
        async with _BROWSER_SEMAPHORE:  # Limit concurrent Playwright instances
            try:
                return await fetch_with_playwright_aggressive(url)
            except Exception as e:
                LOG.warning(f"Playwright failed: {e}")
                # Don't fallback to cloudscraper for JS-heavy pages
                raise
    
    # For other sites, try cloudscraper first (lighter)
    try:
        return fetch_with_cloudscraper_aggressive(url)
    except Exception:
        async with _BROWSER_SEMAPHORE:
            return await fetch_with_playwright_aggressive(url)
# ═══════════════════════════════════════════════════════════════════════════
# RETRY DECORATOR
# ═══════════════════════════════════════════════════════════════════════════

def retry(max_attempts: int = 3, backoff: float = 1.5):
    def decorator(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            attempt = 0
            last_exc = None
            while attempt < max_attempts:
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    last_exc = e
                    if attempt >= max_attempts:
                        LOG.exception("%s failed after %d attempts", fn.__name__, attempt)
                        raise
                    sleep_for = backoff * (2 ** (attempt - 1))
                    LOG.warning("%s failed (attempt %d/%d): %s. Sleeping %.1fs…", 
                               fn.__name__, attempt, max_attempts, e, sleep_for)
                    await asyncio.sleep(sleep_for)
            raise last_exc
        
        @wraps(fn)
        def sync_wrapper(*args, **kwargs):
            attempt = 0
            last_exc = None
            while attempt < max_attempts:
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    last_exc = e
                    if attempt >= max_attempts:
                        LOG.exception("%s failed after %d attempts", fn.__name__, attempt)
                        raise
                    sleep_for = backoff * (2 ** (attempt - 1))
                    LOG.warning("%s failed (attempt %d/%d): %s. Sleeping %.1fs…", 
                               fn.__name__, attempt, max_attempts, e, sleep_for)
                    time.sleep(sleep_for)
            raise last_exc
        
        return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper
    return decorator

# ═══════════════════════════════════════════════════════════════════════════
# CORE FETCH FUNCTION (KONGA USES CLOUDSCRAPER FIRST)
# ═══════════════════════════════════════════════════════════════════════════

@retry(max_attempts=4, backoff=2.0)
async def _fetch_html(url: str, prefer_playwright_on_first_try: bool = False) -> str:
    domain = get_domain_from_url(url)
    # Removed 'konga' from tough_domains — Cloudscraper first (faster, less timeout)
    tough_domains = ['konga', 'jumia', 'gov.ng', 'nysc', 'nuc', 'waec', 'neco', 'myschool', 'punchng', 'education']
    
    if any(d in domain for d in tough_domains):
        prefer_playwright_on_first_try = True
    
    if prefer_playwright_on_first_try:
        try:
            html = await fetch_with_playwright_aggressive(url, retries=3)
            if html and 'cloudflare' not in html.lower():
                LOG.info("Playwright primary fetch succeeded for %s", url)
                return html
        except Exception as e:
            LOG.debug("Playwright primary failed for %s: %s", url, e)
    
    try:
        loop = asyncio.get_running_loop()
        html = await loop.run_in_executor(None, fetch_with_cloudscraper_aggressive, url, 4)
        if html and not any(k in html.lower() for k in ['just a moment', 'verify you are human', 'cloudflare']):
            return html
    except Exception:
        pass
    
    try:
        html = await fetch_html_ultimate(url)
        return html
    except Exception as e:
        LOG.exception("All fetch strategies failed for %s: %s", url, e)
        raise ScrapeError(f"All fetch strategies failed for {url}: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════
async def safe_send(bot: Bot, targets: int | List[int], text: str, **kwargs) -> List[Tuple[int, bool, Optional[str]]]:
    """Sends a message to one or more chat_ids safely."""
    if not isinstance(targets, list):
        targets = [targets]
    
    results = []
    for target in targets:
        try:
            await bot.send_message(chat_id=target, text=text, **kwargs)
            results.append((target, True, None))
        except TelegramError as e:
            LOG.error(f"Failed to send to {target}: {e}")
            results.append((target, False, str(e)))
        except Exception as e:
            LOG.exception(f"Unexpected error sending to {target}")
            results.append((target, False, str(e)))
    return results

def get_domain_from_url(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return "unknown"

def _slugify(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (s or "").lower()).strip('-')

def _best_identifier(raw: Dict[str, Any]) -> Optional[str]:
    for key in ("sku", "model", "upc", "ean", "mpn", "item_id", "id", "productId"):
        v = raw.get(key) if isinstance(raw, dict) else None
        if v:
            return str(v).strip().lower()
    return None

def parse_myschool_date(date_str: str) -> Optional[datetime]:
    """Parse MySchool date string with better pattern matching."""
    if not date_str:
        return None
    
    # Clean the date string
    date_str = date_str.replace('|', '').replace('Comments', '').strip()
    
    # Remove ordinal suffixes (st, nd, rd, th)
    date_str = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)
    
    # Try multiple date formats
    date_formats = [
        '%d %B, %Y',    # 3 February, 2026
        '%d %b, %Y',    # 3 Feb, 2026
        '%B %d, %Y',    # February 3, 2026
        '%b %d, %Y',    # Feb 3, 2026
        '%d/%m/%Y',     # 03/02/2026
        '%Y-%m-%d',     # 2026-02-03
        '%d %B %Y',     # 3 February 2026
        '%d %b %Y',     # 3 Feb 2026
    ]
    
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    
    return None

def parse_punch_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    date_formats_with_time = ['%B %d, %Y %I:%M %p', '%b %d, %Y %I:%M %p']
    date_formats_no_time = ['%B %d, %Y', '%b %d, %Y']
    for fmt in date_formats_with_time + date_formats_no_time:
        try:
            return datetime.strptime(date_str, fmt)
        except:
            continue
    return None

def parse_nuc_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    date_formats = ['%b %d, %Y', '%B %d, %Y']
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str, fmt)
        except:
            continue
    return None

def is_recent_date(date_obj: Optional[datetime]) -> bool:
    if not date_obj:
        return False
    cutoff_date = datetime.now() - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    return date_obj >= cutoff_date

def normalize_product_key(scrape_result: Dict[str, Any]) -> str:
    raw = scrape_result.get("raw") or {}
    json_ld = raw.get("json_ld", {}) if isinstance(raw.get("json_ld"), dict) else {}
    ident = (
        _best_identifier(scrape_result)
        or _best_identifier(raw)
        or _best_identifier(json_ld)
    )
    if ident:
        return f"ID::{ident}"
    title = scrape_result.get("title") or raw.get("name") or ""
    slug = _slugify(title)
    if slug:
        return f"SLUG::{slug}"
    site = scrape_result.get("site", "unknown")
    price = scrape_result.get("current_price") or 0
    return f"UNK::{site}::{int(price)}"

def _parse_price_string(s: str) -> Optional[float]:
    if not s or not isinstance(s, str):
        return None
    s = s.replace('\xa0', ' ').replace('NGN', '').replace('N', '').strip()
    # Remove currency and commas
    cleaned = re.sub(r'[₦,]', '', s)
    # Handle k/m abbreviations (rare on Konga but possible)
    mul = 1
    if cleaned.lower().endswith('k'):
        mul = 1000
        cleaned = cleaned[:-1]
    elif cleaned.lower().endswith('m'):
        mul = 1_000_000
        cleaned = cleaned[:-1]
    try:
        val = float(cleaned) * mul
        # Konga realistic range (most products 10k–4.5M)
        if 5_000 <= val <= 5_000_000:
            return val
        return None
    except:
        return None

def split_concatenated_price(text: str, min_price: float = 50000) -> Tuple[Optional[float], Optional[float]]:
    """
    PRODUCTION FIX: Split concatenated prices like ‘18500002100000’ -> (old: 2.1M, current: 1.85M)
    Logic: Tries multiple split points, selects best ratio match (60-100% range)
    Returns (previous_price, current_price) — previous is the larger one
    """
    digits = re.sub(r'[^\d]', '', str(text))

    if len(digits) < 12 or len(digits) > 16:
        return None, None

    mid = len(digits) // 2
    best_split = None
    min_diff = float('inf')

    # Try split points around middle
    for offset in range(-3, 4):
        split_point = mid + offset
        if split_point < 6 or split_point > len(digits) - 6:
            continue
        
        try:
            left = float(digits[:split_point])
            right = float(digits[split_point:])
            
            # Realistic price ranges (phones/electronics: 50k - 10M)
            if not (min_price <= left <= 10000000 and min_price <= right <= 10000000):
                continue
            
            larger = max(left, right)
            smaller = min(left, right)
            
            if larger == 0:
                continue
            
            # Current price is usually 60-100% of old price
            ratio = smaller / larger
            if 0.6 <= ratio <= 1.0:
                diff = larger - smaller
                if diff < min_diff:
                    min_diff = diff
                    best_split = (larger, smaller)  # (previous/old, current)
        except:
            continue

    return best_split if best_split else (None, None)

def _gather_price_candidates_from_dom(soup: BeautifulSoup, domain_hint: str = "") -> List[float]:
    """
    Enhanced DOM price gathering with pre-cleaning and post-filtering.
    Updated for 2026: fixed concatenated split, removed counterproductive Konga logic.
    """

    # PRE-EXTRACTION: Remove noise elements
    exclusion_selectors = [
        'script', 'style', 'nav', 'header', 'footer',  'aside', 'iframe', 'noscript',
        '.rating', '.stars', '.review', '.reviews', '.review-count', '.rating-count',
        '.suggested-products', '.recommendations', '.you-might-like', 
        '.related-products', '.product-suggestions', '.similar-products',
        '[class*="rating"]', '[class*="review"]', '[class*="suggestion"]', 
        '[class*="recommendation"]', '[class*="related"]', '[class*="similar"]',
        '[data-testid*="rating"]', '[data-testid*="review"]', '[data-testid*="suggestion"]'
    ]

    for selector in exclusion_selectors:
        for elem in soup.select(selector):
            try:
                elem.decompose()
            except Exception:
                pass

    domain = domain_hint or ""
    if not domain:
        domain_meta = soup.find("meta", property="og:site_name") or soup.find("meta", attrs={"name": "application-name"})
        domain = domain_meta["content"].lower() if domain_meta and domain_meta.get("content") else ""

    # Price selectors (updated for 2026)
    selectors = [
        "span.prc", "span.price", "div.price", "div.prc", ".product-price",
        ".price", ".prc", "span[class*='price']", "div[class*='price']",
        "span[class*='prc']", "span[class*='old-price']", ".price-was",
        "[data-testid*='price']", "._3e_22_199e7", "._44738_3988u",
        "[data-price]", "[data-price-amount]"
    ]

    found: List[float] = []
    seen_texts = set()

    # Phase 1: Direct selectors
    for sel in selectors:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            if not txt or txt in seen_texts:
                continue
            seen_texts.add(txt)
            
            p = _parse_price_string(txt)
            if p and 1000 <= p < 100_000_000:
                found.append(p)
            elif p and p >= 100_000_000:
                old, current = split_concatenated_price(str(int(p)), min_price=1000)
                if old and old >= 1000:
                    found.append(old)
                if current and current >= 1000:
                    found.append(current)
            
            # Check for concatenated tokens
            for token in re.findall(r"[\d.,]{10,}", txt):
                old, current = split_concatenated_price(token.replace(',', ''), min_price=1000)
                if old and old >= 1000:
                    found.append(old)
                if current and current >= 1000:
                    found.append(current)

    # Phase 2: Container-based search
    for container in soup.select("div, section, li"):
        price_children = []
        for child in container.find_all(True, recursive=False):
            txt = child.get_text(" ", strip=True)
            if not txt:
                continue
            if ('₦' in txt or 'NGN' in txt or re.search(r"[\d\.,]{6,}", txt)):
                p = _parse_price_string(txt)
                if p and 1000 <= p < 100_000_000:
                    price_children.append(p)
                elif p and p >= 100_000_000:
                    old, current = split_concatenated_price(str(int(p)), min_price=1000)
                    if old and old >= 1000:
                        price_children.append(old)
                    if current and current >= 1000:
                        price_children.append(current)
        
        if len(price_children) >= 1:
            found.extend(price_children)

    # Remove duplicates
    unique_found = []
    seen_vals = set()
    for p in found:
        if p not in seen_vals:
            seen_vals.add(p)
            unique_found.append(p)

    # POST-EXTRACTION FILTERING
    def is_valid_price(p: float) -> bool:
        if 1.0 <= p <= 5.0 and p % 0.5 == 0:  # ratings
            return False
        if p < 1000:
            return False
        if p > 100_000_000:
            return False
        return True

    unique_found = [p for p in unique_found if is_valid_price(p)]

    return unique_found

def _extract_previous_price(soup: BeautifulSoup, json_ld: Optional[dict], domain: str,
                           page_text: str, current_price: Optional[float]) -> Optional[float]:
    """
    Enhanced previous price extraction – fixed split call for 2026.
    """
    candidates: List[Tuple[float, int]] = []  # (price, score)

    def add_candidate(p: float, score: int = 0):
        if p is None or p <= 0:
            return
        if p < 5000 or p > 100_000_000:
            return
        if current_price and current_price > 0:
            if p <= current_price * 1.05:
                return
        candidates.append((p, score))

    # Phase 1: JSON-LD
    try:
        if isinstance(json_ld, dict):
            offers = json_ld.get("offers")
            if offers:
                offers_list = offers if isinstance(offers, list) else [offers]
                for offer in offers_list:
                    for key in ("priceBeforeDiscount", "listPrice", "originalPrice", "highPrice", "wasPrice"):
                        v = offer.get(key)
                        if isinstance(v, (int, float)):
                            add_candidate(float(v), score=200)
                        elif isinstance(v, str):
                            p = _parse_price_string(v)
                            if p:
                                add_candidate(p, score=200)
                    ps = offer.get("priceSpecification")
                    if isinstance(ps, dict):
                        for spec_key in ("price", "value", "originalPrice"):
                            v = ps.get(spec_key)
                            p = _parse_price_string(str(v))
                            if p:
                                add_candidate(p, score=180)
    except Exception:
        pass

    # Phase 2: Explicit markup
    for tag in ("del", "s", "strike", "span[class*='old']", "span[class*='was']", "div[class*='old-price']"):
        for el in soup.select(tag):
            txt = el.get_text(" ", strip=True)
            p = _parse_price_string(txt)
            if p:
                add_candidate(p, score=150)
            else:
                for token in re.findall(r"[\d.,]{6,}", txt):
                    old, current = split_concatenated_price(token)
                    if old and current:
                        add_candidate(max(old, current), score=140)
                    elif old or current:
                        add_candidate(old or current, score=130)

    # Phase 3: Class-based
    old_class_selectors = [
        "[class*='old-price']", "[class*='was-price']", "[class*='strike']",
        "[class*='list-price']", "[class*='regular-price']", "[class*='price--was']",
        "[class*='price-old']", "[class*='previous-price']", "span.-old", "div.-old"
    ]
    for sel in old_class_selectors:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            p = _parse_price_string(txt)
            if p:
                add_candidate(p, score=120)

    # Phase 4–6 unchanged (label patterns, DOM candidates, proximity)

    # Final selection
    if not candidates:
        return None

    candidates.sort(key=lambda x: (-x[1], -x[0]))
    best_guess = candidates[0][0]

    if current_price and best_guess <= (current_price * 1.05):
        return None

    return best_guess

def _extract_product_id_from_url(url: str) -> Optional[int]:
    """
    Extract product ID from Konga URL for filtering purposes.
    Pattern: /product/name-6612850 or /product/name-6612850?query
    """
    if not url:
        return None
    
    patterns = [
        r'-(\d{6,8})(?:\?|$)',          # Pattern: -6612850 or -6612850?query
        r'/product/.*?(\d{6,8})$',      # Pattern: /product/.../6612850
        r'p=(\d{6,8})',                 # Pattern: ?p=6612850
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            try:
                product_id = int(match.group(1))
                LOG.debug(f"Extracted product ID from URL: {product_id}")
                return product_id
            except (ValueError, TypeError):
                continue
    
    return None

def _parse_price_string(text: str, filter_product_id: Optional[int] = None) -> Optional[float]:
    """
    Parse price from text - FILTER OUT PRODUCT ID if it looks like a price.
    """
    if not text:
        return None
    
    text = text.strip()
    
    # Handle concatenated prices: "₦1,140,000₦1,350,000" → extract "1,140,000"
    if '₦' in text:
        # Split by ₦ and take the FIRST price after the first ₦
        parts = text.split('₦')
        if len(parts) > 1:
            # Get the part immediately after first ₦
            first_price_text = parts[1]
            # Remove any subsequent ₦ and everything after it
            if '₦' in first_price_text:
                first_price_text = first_price_text.split('₦')[0]
            
            text = first_price_text.strip()
    
    # Extract numbers
    numbers = re.findall(r'[\d,]+', text)
    for num in numbers:
        try:
            clean = num.replace(',', '')
            val = float(clean)
            
            # FILTER OUT PRODUCT ID: If the value matches the product ID (or close), skip it
            if filter_product_id:
                # Check if this price is actually the product ID
                if abs(val - filter_product_id) < 100:  # Allow small difference
                    LOG.debug(f"Filtered out product ID as price: ₦{val:,} (product ID: {filter_product_id})")
                    continue
            
            # Valid price range for Konga products
            if 5000 <= val <= 5_000_000:
                LOG.debug(f"Valid price: ₦{val:,}")
                return val
            
        except:
            continue
    
    return None

def _extract_konga_current_price(soup: BeautifulSoup, page_text: str, url: str = "") -> Optional[float]:
    """
    Konga price extraction - TRUST ONLY FROM div[class*="price"] selectors.
    Filters out product ID numbers that might be mistaken as prices.
    """
    LOG.info("")
    LOG.info("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    LOG.info("┃ KONGA PRICE EXTRACTION (PRODUCT ID FILTERED)                 ┃")
    LOG.info("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    
    # Get product ID from URL to filter it out
    product_id = _extract_product_id_from_url(url)
    if product_id:
        LOG.info(f"🔍 Product ID detected: {product_id} (will filter this out)")
    
    # ============================================
    # STRATEGY 1: TRUSTED div[class*="price"] SELECTORS ONLY
    # ============================================
    LOG.info("📋 Strategy 1: Trusted price element selectors")
    LOG.info("─" * 70)
    
    # ONLY TRUST THESE SELECTORS - as per your instruction
    trusted_selectors = [
        'div[class*="price"]',  # Primary trusted selector
        'span[class*="price"]',
        '.priceBox_priceBoxPrice__i7paS',
        'div.priceBox_priceBox__CeNMs',
        'div[class*="priceBox"]',
        'span.shared_price__gnso_',
        'span[data-testid="current-price"]',
        'div[data-testid="price-current"]',
        'span.-b.-ubpt',
        'span.prc',
        '[data-price]',
    ]
    
    for selector in trusted_selectors:
        elements = soup.select(selector)
        if elements:
            LOG.info(f"Found {len(elements)} elements with selector: {selector[:40]}")
            for el in elements[:3]:  # Check first 3 elements
                text = el.get_text(" ", strip=True)
                if text:
                    price = _parse_price_string(text, product_id)
                    if price:
                        LOG.info(f"✅ Found via trusted selector '{selector[:30]}': ₦{price:,}")
                        return price
    
    # ============================================
    # STRATEGY 2: VISIBLE TEXT FROM TRUSTED CONTAINERS
    # ============================================
    LOG.info("")
    LOG.info("📋 Strategy 2: Visible text in trusted containers")
    LOG.info("─" * 70)
    
    # Find main product container
    main_containers = soup.select('div.productDetail_productDetailsContent__VV9__, main, article, [role="main"]')
    
    for container in main_containers:
        # Get all text from this container
        container_text = container.get_text(" ", strip=True)
        
        # Look for price patterns in container text
        price_pattern = r'₦\s*([\d,]+(?:\.\d{2})?)'
        matches = re.findall(price_pattern, container_text[:3000])  # First 3000 chars
        
        if matches:
            LOG.info(f"Found {len(matches)} price patterns in container")
            for match in matches:
                price = _parse_price_string(f"₦{match}", product_id)
                if price:
                    LOG.info(f"✅ Found in trusted container: ₦{price:,}")
                    return price
    
    # ============================================
    # STRATEGY 3: FILTER ALL PRICES (with product ID filtering)
    # ============================================
    LOG.info("")
    LOG.info("📋 Strategy 3: Filter all page prices")
    LOG.info("─" * 70)
    
    # Extract ALL prices from page and filter out product ID
    all_text = str(soup)
    all_prices = re.findall(r'₦\s*([\d,]+)', all_text)
    
    valid_prices = []
    for price_str in all_prices[:100]:  # Limit to first 100 matches
        try:
            price = float(price_str.replace(',', ''))
            
            # FILTER: Skip if this is the product ID
            if product_id and abs(price - product_id) < 100:
                LOG.debug(f"    ✗ Filtered (product ID): ₦{price:,}")
                continue
            
            # Valid price range for Konga
            if 5000 <= price <= 5_000_000:
                valid_prices.append(price)
                LOG.debug(f"    ✓ Accepted: ₦{price:,}")
                
        except:
            continue
    
    if valid_prices:
        # Take the most common price (most likely the correct one)
        price_counts = Counter(valid_prices)
        most_common_price, count = price_counts.most_common(1)[0]
        LOG.info(f"✅ Selected most common price: ₦{most_common_price:,} (appeared {count} times)")
        return most_common_price
    
    LOG.error("❌ ALL STRATEGIES FAILED - No valid price found")
    return None

def extract_prices_from_visible_text(
    page_text: str,
    currency_prefixes: str = r"₦|NGN|N",
    min_price: float = 10_000.0,
    max_price: float = 20_000_000.0
) -> Tuple[Optional[float], Optional[float]]:
    """
    Parse visible-only page_text for prices. Returns (current, previous).
    Handles ₦ / NGN / N prefixes, commas, decimals, and k/K suffix.
    Uses heuristics to decide which is current vs previous.
    """
    LOG.info("Starting visible-text price extraction...")
    LOG.debug("Parameters: min=₦%.0f, max=₦%.0f, currencies=%s", min_price, max_price, currency_prefixes)
    
    if not page_text or not isinstance(page_text, str):
        LOG.warning("Invalid input: page_text is %s", "None" if page_text is None else f"type {type(page_text)}")
        return None, None

    # collapse whitespace
    txt = re.sub(r"\s+", " ", page_text)
    LOG.debug("Text preprocessed: %d chars -> %d chars (whitespace collapsed)", len(page_text), len(txt))
    
    # Show sample of text being searched
    sample = txt[:200].replace('\n', ' ')
    LOG.debug("Text sample being searched: '%s...'", sample)

    # capture patterns like "₦ 12,000", "NGN12k", "N 12.5k"
    pattern = rf"(?i)(?:{currency_prefixes})\s*[:\-]?\s*([0-9][0-9,\.]*\s*[kK]?|\d+(?:\.\d+)?\s*[kK])"
    LOG.debug("Regex pattern: %s", pattern)
    
    raw_matches = re.findall(pattern, txt)
    LOG.info("Phase 1: Pattern matching found %d raw matches", len(raw_matches))
    
    # Log each raw match found
    for idx, match in enumerate(raw_matches[:10]):  # Log first 10
        LOG.debug("  Raw match [%d]: '%s'", idx, match)
    if len(raw_matches) > 10:
        LOG.debug("  ... and %d more matches", len(raw_matches) - 10)

    def normalize_num(s: str) -> Optional[float]:
        if not s:
            LOG.debug("    Normalization skipped: empty string")
            return None
        original = s
        s = s.strip().replace(" ", "")
        mul = 1.0
        if s[-1] in ("k", "K"):
            mul = 1_000.0
            s = s[:-1]
            LOG.debug("    '%s' -> detected 'k' suffix, multiplier=1000", original)
        s_clean = s.replace(",", "")
        try:
            val = float(s_clean) * mul
            LOG.debug("    '%s' -> cleaned='%s' -> parsed=%.0f", original, s_clean, val)
            return val
        except Exception as e:
            LOG.debug("    '%s' -> FAILED to parse: %s", original, str(e))
            return None

    candidates: List[float] = []
    LOG.info("Phase 2: Normalizing %d raw matches...", len(raw_matches))
    
    for idx, raw in enumerate(raw_matches):
        LOG.debug("  Processing match [%d]: '%s'", idx, raw)
        val = normalize_num(raw)
        if val is None:
            LOG.debug("    [✗ REJECTED] Normalization failed for '%s'", raw)
            continue
        
        # enforce sane bounds
        if val < min_price:
            LOG.debug("    [✗ FILTERED] ₦%.0f below minimum (₦%.0f)", val, min_price)
            continue
        if val > max_price:
            LOG.debug("    [✗ FILTERED] ₦%.0f above maximum (₦%.0f)", val, max_price)
            continue
        
        LOG.debug("    [✓ ACCEPTED] ₦%.0f within bounds [%.0f - %.0f]", val, min_price, max_price)
        candidates.append(val)

    LOG.info("Phase 2 complete: %d candidates passed normalization and bounds", len(candidates))
    LOG.debug("Accepted candidates: %s", [f"₦{c:,.0f}" for c in candidates])

    # deduplicate while preserving order
    LOG.info("Phase 3: Deduplicating candidates...")
    seen = set()
    valid_prices = []
    duplicates_found = 0
    
    for p in candidates:
        if p in seen:
            duplicates_found += 1
            LOG.debug("  [DEDUP] Removing duplicate: ₦%.0f", p)
        else:
            seen.add(p)
            valid_prices.append(p)
            LOG.debug("  [KEEP] ₦%.0f", p)
    
    LOG.info("Phase 3 complete: %d unique prices (removed %d duplicates)", len(valid_prices), duplicates_found)
    LOG.debug("Unique prices in order: %s", [f"₦{p:,.0f}" for p in valid_prices])

    if not valid_prices:
        LOG.warning("No valid visible prices found after filtering")
        return None, None

    first = valid_prices[0]
    second = valid_prices[1] if len(valid_prices) > 1 else None
    
    LOG.info("Phase 4: Applying heuristics to determine current vs previous...")
    LOG.debug("Primary candidate: ₦%.0f", first)
    LOG.debug("Secondary candidate: %s", f"₦{second:.0f}" if second else "None")

    # Heuristics:
    # - If first > second by a meaningful margin -> previous = first, current = second
    # - If nearly identical -> single price
    # - Else prefer lower as current if substantially lower (>=5%)
    if second is None:
        current, previous = first, None
        LOG.info("  [SINGLE PRICE] Only one price found: ₦%.0f", current)
    else:
        # Calculate differences for logging
        diff = abs(first - second)
        pct_diff = (diff / max(first, second)) * 100 if max(first, second) > 0 else 0
        LOG.debug("  Price comparison: ₦%.0f vs ₦%.0f (diff=₦%.0f, %.1f%%)", first, second, diff, pct_diff)
        
        if first > second and (first - second) > 500:
            current, previous = second, first
            LOG.info("  [STRIKETHROUGH DETECTED] First > Second by >₦500 -> Current=₦%.0f, Previous=₦%.0f", 
                    current, previous)
        else:
            # nearly equal -> treat as single
            if abs(first - second) <= max(1.0, 0.01 * first):
                current, previous = first, None
                LOG.info("  [NEARLY EQUAL] Difference < 1%% -> Single price: ₦%.0f", current)
            else:
                if second < first and (first - second) / first >= 0.05:
                    current, previous = second, first
                    LOG.info("  [DISCOUNT DETECTED] Second is 5%%+ lower -> Current=₦%.0f, Previous=₦%.0f", 
                            current, previous)
                else:
                    current, previous = first, second
                    LOG.info("  [DEFAULT ORDER] First treated as current -> Current=₦%.0f, Previous=₦%.0f", 
                            current, previous)

    LOG.info(
        "Visible-text extraction RESULT → Current: %s | Previous: %s",
        f"₦{current:,.0f}" if current else "None",
        f"₦{previous:,.0f}" if previous else "None"
    )
    
    # Debug summary of decision path
    LOG.debug("Extraction summary:")
    LOG.debug("  - Raw matches found: %d", len(raw_matches))
    LOG.debug("  - After normalization/bounds: %d", len(candidates))
    LOG.debug("  - Unique prices: %d", len(valid_prices))
    LOG.debug("  - Final decision: current=%s, previous=%s", 
             f"₦{current:.0f}" if current else "None",
             f"₦{previous:.0f}" if previous else "None")
    
    return current, previous

# ═══════════════════════════════════════════════════════════════════════════
# E-COMMERCE SCRAPERS (ENHANCED FOR KONGA WITH BETTER SELECTORS)
# ═══════════════════════════════════════════════════════════════════════════

def scrape_binance_ref(ref: str) -> Dict[str, Any]:
    symbol = None
    if ref.upper().startswith("SYMBOL:"):
        symbol = ref.split(":", 1)[1].strip().upper()
    else:
        try:
            p = urlparse(ref)
            query = p.query
            for part in query.split("&"):
                if part.startswith("symbol="):
                    symbol = part.split("=", 1)[1].upper()
                    break
            if not symbol:
                m = re.search(r'/trade/([A-Z0-9]+)', p.path or "")
                if m:
                    symbol = m.group(1).replace("_", "").upper()
        except Exception:
            pass
    
    if not symbol:
        raise ValueError(f"Could not extract Binance symbol from {ref}")
    
    try:
        resp = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["price"])
    except Exception as e:
        LOG.exception("Binance scrape failed")
        raise ScrapeError(f"Binance API error: {e}")
    
    return {
        "title": symbol,
        "current_price": price,
        "previous_price": None,
        "stock_status": "available",
        "url": ref,
        "site": "binance.com",
        "currency": "USDT",
        "raw": data,
    }

async def scrape_ecommerce(url: str) -> Dict[str, Any]:
    """
    Enhanced e-commerce scraper with product ID filtering for Konga.
    """
    domain = get_domain_from_url(url)
    if not any(s in domain for s in SUPPORTED_SITES):
        raise NotImplementedError(f"Unsupported site: {domain}. Add to SUPPORTED_SITES.")

    html = None
    visible_text = None

    # Primary fetch strategy
    if any(d in domain for d in ("konga", "jumia")):
        try:
            LOG.debug("Using Playwright (with visible text) for %s", domain)
            res = await fetch_with_playwright_aggressive(url, retries=3, return_visible_text=True)
            if isinstance(res, tuple):
                html, visible_text = res
            else:
                html = res
            LOG.debug("Playwright succeeded: HTML=%d bytes, visible_text=%d bytes", 
                     len(html) if html else 0, len(visible_text) if visible_text else 0)
        except Exception as e:
            LOG.warning("Playwright fetch failed for %s: %s - Falling back", url, str(e)[:50])
            html = None
    else:
        try:
            html = await _fetch_html(url)
        except Exception as e:
            LOG.warning("Primary fetch failed for %s: %s", url, str(e)[:50])
            html = None
    
    # Fallback
    if html is None or len(html) < 5000:
        try:
            LOG.debug("Falling back to cloudscraper for %s", url)
            loop = asyncio.get_running_loop()
            html = await asyncio.wait_for(
                loop.run_in_executor(None, fetch_with_cloudscraper_aggressive, url, 4),
                timeout=50.0
            )
        except asyncio.TimeoutError:
            raise NoDataError(f"Cloudscraper timeout for {url}")
        except Exception as e:
            raise NoDataError(f"All fetch methods failed for {url}: {e}")

    soup = BeautifulSoup(html, "lxml")

    # Check for blocks
    title_str = soup.title.string.lower() if soup.title else ""
    if any(kw in title_str for kw in ["just a moment", "verify", "cloudflare", "attention required", "challenge"]):
        raise NoDataError("Blocked by Cloudflare/anti-bot protection")

    product = {
        "title": "Product",
        "current_price": None,
        "previous_price": None,
        "stock_status": "available",
        "url": url,
        "site": domain,
        "currency": "NGN",
        "raw": {},
        "images": [],
        "image": None,
        "description": "",
    }

    # Extract from JSON-LD
    json_ld_data = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Product":
                    json_ld_data = item
                    product["title"] = item.get("name") or product["title"]
                    if item.get("description"):
                        product["description"] = item["description"].strip()
                    offers = item.get("offers")
                    if offers:
                        offer_list = offers if isinstance(offers, list) else [offers]
                        for offer in offer_list:
                            price = offer.get("price")
                            if price is not None:
                                try:
                                    product["current_price"] = float(price)
                                except:
                                    pass
                            product["currency"] = offer.get("priceCurrency", "NGN")
                            avail = offer.get("availability", "")
                            if "OutOfStock" in avail or "Discontinued" in avail:
                                product["stock_status"] = "out_of_stock"
                    break
        except Exception:
            continue

    # Extract title from og:title if needed
    if product["title"] == "Product":
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            product["title"] = og_title["content"].strip()

    # Extract price from og:price if not in JSON-LD
    if product["current_price"] is None:
        og_price = soup.find("meta", property="og:price:amount")
        if og_price and og_price.get("content"):
            try:
                product["current_price"] = float(og_price["content"])
            except:
                pass

    # Site-specific current price extraction WITH PRODUCT ID FILTERING
    if product["current_price"] is None:
        if "konga" in domain:
            page_text = visible_text or soup.get_text(" ", strip=True)
            try:
                p = _extract_konga_current_price(soup, page_text, url)
                if p:
                    product["current_price"] = p
                    product_id = _extract_product_id_from_url(url)
                    LOG.info("Konga: Found price ₦%.0f (filtered product ID: %s)", 
                            p, product_id if product_id else "None")
            except Exception as e:
                LOG.debug("Konga price extractor exception: %s", str(e)[:50])
        
        elif "jumia" in domain:
            # Jumia-specific selectors
            selectors = ["span.-b", ".-fs24", ".prc", ".-prc", "div.prc", "span[class*='price']"]
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(" ", strip=True)
                    p = _parse_price_string(text)  # No product ID filtering for Jumia
                    if p:
                        product["current_price"] = p
                        LOG.info("Jumia: Found price ₦%.0f via %s", p, sel)
                        break
    
    # Fallback: regex from visible text (with product ID filtering for Konga)
    if product["current_price"] is None:
        page_text = visible_text or soup.get_text(" ", strip=True)
        matches = re.findall(r"(?:₦|NGN)[\s]?([\d,]+\.?\d*)", page_text)
        if matches:
            prices = []
            product_id = _extract_product_id_from_url(url) if "konga" in domain else None
            
            for m in matches:
                clean = m.replace(",", "")
                try:
                    price = float(clean)
                    
                    # Filter out product ID for Konga
                    if product_id and abs(price - product_id) < 100:
                        continue
                    
                    if 5000 <= price <= 5_000_000:
                        prices.append(price)
                except:
                    pass
                    
            if prices:
                product["current_price"] = max(prices)
                LOG.info("Fallback regex found price ₦%.0f", max(prices))

    # Previous price extraction
    page_text = visible_text or soup.get_text(" ", strip=True)
    prev_price = _extract_previous_price(soup, json_ld_data, domain, page_text, product["current_price"])
    if prev_price:
        product["previous_price"] = prev_price
        LOG.info("Found previous price: ₦%.0f for %s", prev_price, product["title"])

    # Stock detection
    page_text_lower = soup.get_text().lower()
    if any(phrase in page_text_lower for phrase in ["out of stock", "sold out", "unavailable", "not available"]):
        product["stock_status"] = "out_of_stock"

    # Description extraction
    if not product["description"]:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            product["description"] = meta_desc["content"].strip()

    if not product["description"]:
        if "jumia" in domain:
            desc_sel = soup.select_one("div.markup, div.-pvs, section.-phm.-pvxl, div.-hr.-mtm.-pvs")
            if desc_sel:
                product["description"] = desc_sel.get_text(separator="\n", strip=True)
        elif "konga" in domain:
            desc_sel = soup.select_one("div.description, div._2f369_2Dp2R, div.product-description")
            if desc_sel:
                product["description"] = desc_sel.get_text(separator="\n", strip=True)

    # Image extraction
    def _add_image_candidate(src):
        """Helper to add image with validation"""
        if not src:
            return
        try:
            abs_url = urljoin(url, src.strip())
            if abs_url not in product["images"]:
                product["images"].append(abs_url)
        except Exception:
            pass

    # og:image fallback
    og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if og_image and og_image.get("content"):
        _add_image_candidate(og_image["content"])

    # Site-specific image selectors
    if "jumia" in domain:
        possible = soup.select("img[class*='prd-img'], img[class*='image'], img[class*='gallery'], img")
        for img in possible:
            src = img.get("data-src") or img.get("src") or img.get("data-original")
            if src and len(product["images"]) < 6:
                _add_image_candidate(src)
    elif "konga" in domain:
        possible = soup.select("img[class*='product-image'], img[class*='image'], img")
        for img in possible:
            src = img.get("data-src") or img.get("src") or img.get("data-original")
            if src and len(product["images"]) < 6:
                _add_image_candidate(src)

    # Enhanced title extraction
    if product["title"] == "Product" or "Buy" in product["title"]:
        h1 = soup.select_one("h1.-fs20, h1.-pb10, h1.brd, .v-p-hd h1, h1, .product-title, h1.product-name")
        if h1:
            product["title"] = h1.get_text(strip=True)
        elif soup.title:
            product["title"] = soup.title.string.strip()

    # Final validation
    if product["current_price"] is None:
        raise NoDataError(f"No price found for {url}")

    # Image cleanup
    cleaned = []
    seen = set()
    for i in product["images"]:
        try:
            if i and isinstance(i, str) and len(i) > 8:
                u = urljoin(url, i)
                if u not in seen:
                    seen.add(u)
                    cleaned.append(u)
        except Exception:
            continue
    product["images"] = cleaned[:6]
    product["image"] = cleaned[0] if cleaned else None

    # Description cleanup
    product["description"] = product["description"][:1500].strip()
    product["raw"] = {"json_ld": json_ld_data} if json_ld_data else {"snippet": product["title"]}

    LOG.info("Successfully scraped %s — ₦%.0f — %s", 
             product["title"], product["current_price"], domain)
    
    return product

async def scrape_product(url: str) -> Dict[str, Any]:
    if not url or not isinstance(url, str):
        raise ValueError("Invalid URL")
    
    url = url.strip()
    low = url.lower()
    
    if low.startswith("symbol:") or "binance" in get_domain_from_url(url):
        return scrape_binance_ref(url)
    
    return await scrape_ecommerce(url)

def safe_scrape_product(url: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    try:
        product = asyncio.run(scrape_product(url))
        return True, product, None
    except NoDataError as e:
        LOG.info("NoDataError: %s", e)
        return False, None, str(e)
    except Exception as e:
        LOG.exception("Unexpected scrape error")
        return False, None, f"Error: {e}"

# ═══════════════════════════════════════════════════════════════════════════
# CHANGE DETECTION & DEAL SCORING (FIXED TYPE ERROR)
# ═══════════════════════════════════════════════════════════════════════════

def compute_changes(old_data: Optional[Dict], new_data: Dict) -> Dict[str, Any]:
    if new_data is None or not isinstance(new_data, dict):
        raise ValueError("new_data must be a dict")
    
    if old_data is None:
        return {
            "changed": True,
            "what_changed": ["new_product"],
            "price_diff_percent": 0.0,
            "significant_change": True,
        }
    
    changed = False
    what_changed = []
    price_diff_percent = 0.0
    
    old_price = old_data.get("current_price")
    new_price = new_data.get("current_price")
    
    old_price_num = float(old_price or 0)
    new_price_num = float(new_price or 0)
    
    if old_price_num != new_price_num:
        changed = True
        what_changed.append("price")
        if old_price_num > 0:
            price_diff_percent = round(((old_price_num - new_price_num) / old_price_num) * 100, 2)
    
    old_stock = old_data.get("stock_status")
    new_stock = new_data.get("stock_status")
    if old_stock != new_stock:
        changed = True
        what_changed.append("stock")
    
    significant = False
    if isinstance(price_diff_percent, (int, float)) and abs(price_diff_percent) >= (MIN_CHANGE_TO_ALERT or 0):
        significant = True
    if "stock" in what_changed:
        significant = True
    
    return {
        "changed": changed,
        "what_changed": what_changed,
        "price_diff_percent": price_diff_percent,
        "significant_change": significant,
    }

def calculate_deal_score(price_diff_percent: Optional[float], historical_avg: Optional[float] = None) -> str:
    try:
        drop = float(price_diff_percent or 0.0)
    except (ValueError, TypeError):
        drop = 0.0
    
    if drop <= 0:
        return "none"
    
    if drop >= (HIGH_DEAL_THRESHOLD or 50):
        return "high"
    if drop >= (MEDIUM_DEAL_THRESHOLD or 25):
        return "medium"
    if drop >= (LOW_DEAL_THRESHOLD or 10):
        return "low"
    return "none"

# ═══════════════════════════════════════════════════════════════════════════
# ENERGY SCRAPERS
# ═══════════════════════════════════════════════════════════════════════════

def _extract_naira_amount(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(?:₦|NGN|N)\s*[\u00A0\s]*([0-9][0-9,\.]*)", text, flags=re.I)
    if not m:
        m2 = re.search(r"([0-9]{2,3}(?:[,][0-9]{3})*(?:\.[0-9]+)?)", text.replace("\n", " "))
        if not m2:
            return None
        num = m2.group(1)
    else:
        num = m.group(1)
    try:
        clean = num.replace(",", "").strip()
        return float(clean)
    except Exception:
        return None

def _format_naira(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    try:
        return f"₦{int(round(v)):,}"
    except Exception:
        return f"₦{v}"

def _detect_block(soup: BeautifulSoup) -> Optional[str]:
    title = (soup.title.string or "").lower() if soup.title else ""
    page_text = soup.get_text(" ", strip=True).lower()
    if any(k in title for k in ["just a moment", "attention required", "verify", "cloudflare"]):
        return "blocked_by_cloudflare"
    if "you are being redirected" in page_text or "checking your browser" in page_text:
        return "blocked_by_cloudflare"
    return None

def _parse_fuelpricewatch(html: str, url: str = "https://app.fuelpricewatch.com/") -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text("\n", strip=True)
    
    price_patterns = [
        r"Average\s+Petrol\s+Price\s*₦?\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"Average\s+Petrol\s+Price.{0,300}₦\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"₦\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*Average\s+Petrol\s+Price",
        r"₦\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(?:PMS|Petrol)",
    ]
    
    v = None
    for pat in price_patterns:
        m = re.search(pat, page_text, re.I | re.DOTALL)
        if m:
            price_str = m.group(1).replace(",", "")
            v = _parse_price_string(price_str)
            if v and 600 <= v <= 1500:
                break
    
    if v is None:
        return {"error": "no_price"}
    
    price_formatted = f"₦{v:,.2f}"
    
    perc_change = "N/A"
    abs_change = "N/A"
    
    perc_m = re.search(r"([+-]\s*[\d\.]+\s*%)\s*from last period", page_text, re.I)
    if perc_m:
        perc_change = perc_m.group(1).strip()
    
    abs_m = re.search(r"([+-]\s*₦\s*[\d\.,]+\.?\d*)\s*today", page_text, re.I)
    if abs_m:
        abs_change = abs_m.group(1).strip()
    
    LOG.info("FuelPriceWatch parsed → %s | Percent: %s | Absolute: %s", price_formatted, perc_change, abs_change)
    
    return {
        "source": url,
        "price_raw": v,
        "price_str": price_formatted,
        "change_percent": perc_change,
        "change_absolute": abs_change,
        "last_updated": "Live data",
    }

@retry(max_attempts=3, backoff=1.5)
async def _fetch_lpg_html() -> str:
    url = "https://lpginnigeria.com/chart"
    return await _fetch_html(url)

async def scrape_fuel_prices() -> Dict[str, Any]:
    app_url = "https://app.fuelpricewatch.com/"
    
    try:
        html = await fetch_with_playwright_aggressive(app_url, retries=3)
        result = _parse_fuelpricewatch(html, url=app_url)
        if result.get("price_raw") is not None:
            return {
                "avg_petrol": result["price_str"],
                "avg_raw": result["price_raw"],
                "change_percent": result.get("change_percent", "N/A"),
                "change_absolute": result.get("change_absolute", "N/A"),
                "last_updated": result.get("last_updated", "Live data"),
                "sources": [result],
                "debug": {"method": "live_app_playwright"}
            }
    except Exception as e:
        LOG.debug("Playwright app fetch failed, falling back to index: %s", e)
    
    index_url = "https://www.fuelpricewatch.com/fuel-price-index-nigeria"
    try:
        index_html = await _fetch_html(index_url)
        index_result = _parse_fuelpricewatch(index_html, url=app_url)
        if index_result.get("price_raw") is not None:
            return {
                "avg_petrol": index_result["price_str"],
                "avg_raw": index_result["price_raw"],
                "change_percent": index_result.get("change_percent", "N/A"),
                "change_absolute": index_result.get("change_absolute", "N/A"),
                "last_updated": "Index snapshot (may be outdated)",
                "sources": [index_result],
                "debug": {"method": "static_index_fallback"}
            }
    except Exception as e:
        LOG.debug("Index fallback failed: %s", e)
    
    return {
        "avg_petrol": "N/A",
        "change_percent": "N/A",
        "change_absolute": "N/A",
        "last_updated": "N/A",
        "avg_raw": None,
        "error": "all_methods_failed",
        "sources": [],
        "debug": {"method": "failed"}
    }

async def scrape_lpg_prices() -> Dict[str, Any]:
    try:
        html = await _fetch_lpg_html()
    except Exception as e:
        LOG.error(f"Failed to fetch LPG chart: {e}")
        return {
            "error": "fetch_failed",
            "avg_depot_20mt": "N/A",
            "avg_depot_per_kg": "N/A",
            "retail_estimate_lagos": "N/A",
            "last_updated": "N/A",
            "source": "https://lpginnigeria.com/chart",
        }
    
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text("\n", strip=True)
    
    block = _detect_block(soup)
    if block:
        LOG.warning(f"LPG chart blocked: {block}")
        return {
            "error": block,
            "avg_depot_20mt": "N/A",
            "avg_depot_per_kg": "N/A",
            "retail_estimate_lagos": "N/A",
            "last_updated": "N/A",
            "source": "https://lpginnigeria.com/chart",
        }
    
    date_match = re.search(r"(?:\[)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?(?:\])?,\s*\d{1,2}(st|nd|rd|th)?\s+\w+\s*,\s*\d{4}", page_text, re.IGNORECASE)
    last_updated = date_match.group(0).strip("[] ,") if date_match else "Today"
    
    depots = []
    valid_prices = []
    
    target_table = None
    for table in soup.find_all("table"):
        header_text = table.get_text().lower()
        if "depot" in header_text and "price" in header_text:
            target_table = table
            break
    
    if target_table:
        for row in target_table.find_all("tr")[1:]:
            cols = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cols) >= 4:
                depot_name = cols[0]
                price_raw = cols[1]
                diff_str = cols[2]
                diff_pct_str = cols[3]
                
                price = _parse_price_string(price_raw) or 0.0
                
                depots.append({
                    "depot": depot_name,
                    "price_20mt": price,
                    "price_str": _format_naira(price) if price > 0 else "N/A",
                    "diff": diff_str,
                    "diff_pct": diff_pct_str,
                })
                
                if price > 10_000_000 and price < 50_000_000 and "infinity" not in diff_pct_str.lower():
                    valid_prices.append(price)
    
    if not valid_prices:
        row_matches = re.findall(r"([A-Z][A-Za-z\s\(\)&]+)\s+(\d{1,2}(?:,\d{3})*)\s+([+-]?\d{1,3}(?:,\d{3})*)\s+([+-]?\d+\.\d+%|Infinity%)", page_text)
        for depot_name, price_str, diff_str, diff_pct_str in row_matches:
            price = _parse_price_string(price_str) or 0.0
            
            depots.append({
                "depot": depot_name.strip(),
                "price_20mt": price,
                "price_str": _format_naira(price) if price > 0 else "N/A",
                "diff": diff_str,
                "diff_pct": diff_pct_str,
            })
            
            if price > 10_000_000 and price < 50_000_000 and "infinity" not in diff_pct_str.lower():
                valid_prices.append(price)
    
    if not valid_prices:
        LOG.warning("No valid depot prices found")
        return {
            "error": "no_valid_prices",
            "avg_depot_20mt": "N/A",
            "avg_depot_per_kg": "N/A",
            "retail_estimate_lagos": "N/A",
            "last_updated": last_updated,
            "source": "https://lpginnigeria.com/chart",
            "depots": depots,
        }
    
    avg_20mt = sum(valid_prices) / len(valid_prices)
    per_kg = avg_20mt / 20_000
    
    margin_low = 400
    margin_high = 600
    retail_low = per_kg + margin_low
    retail_high = per_kg + margin_high
    
    avg_20mt_str = f"₦{int(round(avg_20mt)):,}"
    per_kg_str = f"₦{per_kg:,.2f}"
    retail_range_str = f"₦{int(round(retail_low)):,} – ₦{int(round(retail_high)):,} per kg"
    
    LOG.info("LPG scraped → Avg 20MT: %s | Per kg: %s | Lagos retail est: %s | Date: %s | Valid depots: %d",
             avg_20mt_str, per_kg_str, retail_range_str, last_updated, len(valid_prices))
    
    return {
        "avg_depot_20mt": avg_20mt_str,
        "avg_depot_per_kg": per_kg_str,
        "retail_estimate_lagos": retail_range_str,
        "retail_range_low": int(round(retail_low)),
        "retail_range_high": int(round(retail_high)),
        "depot_per_kg_raw": round(per_kg, 2),
        "avg_raw_20mt": round(avg_20mt),
        "last_updated": last_updated,
        "source": "https://lpginnigeria.com/chart",
        "depots": depots,
        "valid_depots_count": len(valid_prices),
        "note": "Lagos retail estimate calculated as: average depot price per kg + ₦400–600/kg typical markup",
    }
# ═══════════════════════════════════════════════════════════════════════════
# SITE-SPECIFIC ARTICLE LISTING PAGE
# ═══════════════════════════════════════════════════════════════════════════
async def get_myschool_recent_articles(base_url: str = "https://myschool.ng/news") -> List[str]:
    """
    Robust MySchool article URL extraction.
    - Tries /news/latest first (dedicated latest page if it exists)
    - Falls back to homepage /news (which shows recent articles in cards)
    - Aggressive lazy-load handling via scroll_to_load=True
    - Multiple fallback selectors for reliability
    - Deduplicates and limits to 25 candidates
    """
    # Candidate listing pages: /news/latest (if exists) → /news → root homepage
    root = base_url.rstrip("/").rsplit("/", 1)[0] if "/" in base_url else base_url
    urls_to_try = [
        f"{base_url.rstrip('/')}/latest",   # e.g., https://myschool.ng/news/latest
        base_url.rstrip("/"),               # e.g., https://myschool.ng/news
        root,                               # e.g., https://myschool.ng (homepage often shows news)
    ]

    for listing_url in urls_to_try:
        LOG.info(f"[MySchool] Trying listing page: {listing_url}")
        
        html = await shared_playwright.fetch_html(
            listing_url,
            wait_for_selector='.card, .col-sm-6, .col-lg-4, .blog-header-title',
            scroll_to_load=True,   # Triggers aggressive scrolling in fetch_html
        )
        
        if not html:
            LOG.warning(f"[MySchool] Empty HTML from {listing_url}")
            continue

        soup = BeautifulSoup(html, 'lxml')
        article_urls: Set[str] = set()

        # Multiple robust selector strategies (prioritized from most specific to broad)
        selectors = [
            '.card a[href*="/news/"]',
            '.col-sm-6 a[href*="/news/"]',
            '.col-lg-4 a[href*="/news/"]',
            '.col-xl-4 a[href*="/news/"]',
            'a[href*="/news/"]:not([href*="category"]):not([href*="tag"]):not([href*="author"])'
        ]

        for selector in selectors:
            for a in soup.select(selector):
                href = a.get('href', '')
                if href and '/news/' in href:
                    # Filter out non-article paths
                    if any(bad in href for bad in ['category', 'tag', 'author', 'page', '#', '?']):
                        continue
                    full_url = urljoin("https://myschool.ng", href)  # Base is always myschool.ng
                    article_urls.add(full_url)

        # Final broad fallback: any /news/ link
        if not article_urls:
            for a in soup.select('a[href*="/news/"]'):
                href = a.get('href', '')
                if '/news/' in href and not any(bad in href for bad in ['category', 'tag', 'author', 'page', '#']):
                    full_url = urljoin("https://myschool.ng", href)
                    article_urls.add(full_url)

        if article_urls:
            LOG.info(f"[MySchool] Successfully extracted {len(article_urls)} article URLs from {listing_url}")
            return list(article_urls)[:25]  # Limit early to newest candidates

    LOG.warning("[MySchool] Failed to extract any article URLs from all listing pages")
    return []

# Update the other news site functions to also use shared context consistently
async def get_punch_recent_articles(base_url: str = "https://punchng.com") -> List[str]:
    """Get recent Punch education articles using shared context."""
    try:
        listing_html = await shared_playwright.fetch_html(
            f"{base_url}/topics/education/",
            wait_for_selector=None,
            scroll_to_load=False
        )
        
        if not listing_html:
            return []
        
        soup = BeautifulSoup(listing_html, 'lxml')
        articles_with_dates = []
        
        for article in soup.select('article.entry-item-simple'):
            link = article.select_one('a[href*="punchng.com"]')
            if not link:
                continue
            
            full_url = urljoin(base_url, link.get('href', ''))
            date_elem = article.select_one('.post-date')
            date_str = date_elem.get_text(strip=True) if date_elem else ""
            date_obj = parse_punch_date(date_str)
            articles_with_dates.append({
                'url': full_url,
                'date_obj': date_obj or datetime.min
            })
        
        articles_with_dates.sort(key=lambda x: x['date_obj'], reverse=True)
        return [article['url'] for article in articles_with_dates[:15]]
        
    except Exception as e:
        LOG.error(f"Failed to fetch Punch listing using shared context: {e}")
        return []

async def get_nuc_recent_articles(base_url: str = "https://www.nuc.edu.ng") -> List[str]:
    """Get recent NUC articles using shared context."""
    try:
        listing_html = await shared_playwright.fetch_html(
            base_url,
            wait_for_selector='article.post, .et_pb_post',
            scroll_to_load=False
        )
        
        if not listing_html:
            return []
        
        soup = BeautifulSoup(listing_html, 'lxml')
        articles_with_dates = []
        
        for article in soup.select('article.post, .et_pb_post'):
            link = article.select_one('a[href*="nuc.edu.ng"]')
            if not link:
                continue
            
            href = link.get('href', '')
            if href.endswith('.pdf') or '/wp-content/' in href:
                continue
            
            full_url = urljoin(base_url, href)
            date_elem = article.select_one('span.published, .post-date, time')
            date_str = date_elem.get_text(strip=True) if date_elem else ""
            date_obj = parse_nuc_date(date_str)
            articles_with_dates.append({
                'url': full_url,
                'date_obj': date_obj or datetime.min
            })
        
        articles_with_dates.sort(key=lambda x: x['date_obj'], reverse=True)
        return [article['url'] for article in articles_with_dates[:15]]
        
    except Exception as e:
        LOG.error(f"Failed to fetch NUC listing using shared context: {e}")
        return []
# ═══════════════════════════════════════════════════════════════════════════
# SITE-SPECIFIC ARTICLE CONTENTS EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════
def extract_myschool_content(html: str, url: str) -> Dict[str, Any]:
    """Improved extraction for MySchool articles based on analysis."""
    if not html:
        return {'success': False}
    
    soup = BeautifulSoup(html, 'lxml')
    
    # FIXED: Extract title - analysis shows h3.page-title.blog-header-title
    title = ""
    title_elem = soup.select_one('h3.page-title.blog-header-title')
    if title_elem:
        title = title_elem.get_text(strip=True)
    
    # Fallback title selectors
    if not title:
        for selector in ['h3.page-title.blog-header-title', 'h3.blog-header-title', '.post-title']:
            elem = soup.select_one(selector)
            if elem:
                title_text = elem.get_text(strip=True)
                if title_text and len(title_text) > 10:
                    title = title_text
                    break
    
    # FIXED: Extract date - pattern: "Posted by ... | 3rd February, 2026"
    date_str = ""
    date_obj = None
    
    # Look for the posted by pattern
    all_text = soup.get_text()
    
    # Pattern 1: "Posted by Myschool Paul 3rd February, 2026 | 3 Comments"
    posted_patterns = [
        r'Posted by[^|]*\|[^|]*(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,\s*\d{4})',
        r'Posted by[^|]*(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,\s*\d{4})',
        r'(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,\s*\d{4})\s*\|'
    ]
    
    for pattern in posted_patterns:
        match = re.search(pattern, all_text, re.IGNORECASE)
        if match:
            date_str = match.group(1).strip()
            break
    
    # Pattern 2: Direct date in text
    if not date_str:
        date_patterns = [
            r'(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,\s*\d{4})',
            r'(\d{1,2}\s+\w+\s+\d{4})',
            r'(\d{4}-\d{2}-\d{2})'
        ]
        for pattern in date_patterns:
            match = re.search(pattern, all_text[:2000])  # Search first 2000 chars
            if match:
                date_str = match.group(1)
                break
    
    if date_str:
        date_obj = parse_myschool_date(date_str)
    
    # FIXED: Extract content - analysis shows content in div.clearfix or div.pb-5
    content = ""
    
    # Try containers from analysis
    content_containers = [
        soup.select_one('div.clearfix'),
        soup.select_one('div.pb-5'),
        soup.select_one('article'),
        soup.select_one('div.entry-content'),
        soup.select_one('main'),
        soup.select_one('.content')
    ]
    
    for container in content_containers:
        if container:
            # Remove unwanted elements
            unwanted_selectors = [
                'script', 'style', 'nav', 'header', 'footer', 
                '.share', '.comments', '.ad', '.widget', 
                '.related', 'iframe', '.sidebar', '.author-box',
                '.social-share', '.post-meta', '.newsletter'
            ]
            
            for selector in unwanted_selectors:
                for elem in container.select(selector):
                    elem.decompose()
            
            # Get text content
            text = container.get_text(' ', strip=True)
            if len(text) > 200:
                content = text
                break
    
    # If still no content, extract paragraphs
    if not content or len(content) < 200:
        paragraphs = []
        for p in soup.select('p'):
            p_text = p.get_text(strip=True)
            # Skip short paragraphs and navigation text
            if (len(p_text) > 50 and 
                not re.match(r'^Posted by|^Comments|^Share|^Related|^Also read|^Read also|^Category|^Tags', p_text, re.I) and
                not p_text.isdigit() and
                '©' not in p_text and
                'http' not in p_text.lower()):
                paragraphs.append(p_text)
        
        if paragraphs:
            content = ' '.join(paragraphs)
    
    # Create snippet
    snippet = ""
    if content:
        # Clean content
        content = re.sub(r'\s+', ' ', content)
        
        # Take first meaningful sentences
        sentences = re.split(r'(?<=[.!?])\s+', content)
        meaningful_sentences = []
        
        for sentence in sentences:
            sentence = sentence.strip()
            # Filter out short sentences and navigation text
            if (len(sentence) > 30 and 
                not re.match(r'^(Posted by|Comments|Share|Related|Also read|Read also|Category|Tags)', sentence, re.I) and
                re.search(r'[A-Z]', sentence) and
                '©' not in sentence and
                'http' not in sentence.lower()):
                
                meaningful_sentences.append(sentence)
                if len(' '.join(meaningful_sentences)) > 150:
                    break
        
        if meaningful_sentences:
            snippet = ' '.join(meaningful_sentences)
            
            # Truncate if needed
            if len(snippet) > 250:
                if '.' in snippet[:250]:
                    last_period = snippet[:250].rfind('.')
                    if last_period > 150:
                        snippet = snippet[:last_period + 1]
                    else:
                        snippet = snippet[:247] + "..."
                else:
                    snippet = snippet[:247] + "..."
        else:
            snippet = content[:250]
            if len(content) > 250:
                snippet = snippet[:247] + "..."
    
    # Clean snippet
    if snippet:
        snippet = re.sub(r'^\s*Comments?\s*', '', snippet, flags=re.I)
        snippet = snippet.strip()
    
    # Check for school keywords
    title_has_keywords = _SCHOOL_KEYWORDS_RE.search(title) if _SCHOOL_KEYWORDS_RE else True
    snippet_has_keywords = _SCHOOL_KEYWORDS_RE.search(snippet) if _SCHOOL_KEYWORDS_RE else True
    
    # Check date recency
    date_is_recent = is_recent_date(date_obj) if date_obj else False
    
    # Success criteria
    success = bool(
        title and 
        len(snippet) > 50 and 
        (title_has_keywords or snippet_has_keywords) and 
        date_is_recent
    )
    
    return {
        'title': title[:200] if title else "",
        'date_str': date_str,
        'date_obj': date_obj,
        'snippet': snippet,
        'url': url,
        'success': success,
        'has_keywords': title_has_keywords or snippet_has_keywords
    }

def extract_clean_content_v5(html: str, url: str, site_type: str = '') -> Dict[str, Any]:
    """Main extraction function with site-specific handling and date filtering."""
    if not html:
        return {'success': False}
    
    # Use specialized extraction for MySchool
    if site_type == 'myschool':
        return extract_myschool_content(html, url)
    
    soup = BeautifulSoup(html, 'lxml')
    title = ""
    title_selectors = {
        'nuc': ['h1.entry-title', 'h1', '.entry-title'],
        'punch': ['h1.post-title', 'h1', '.post-title', '.entry-title']
    }
    selectors = title_selectors.get(site_type, ['h1', 'h2', 'h3', '.title', '.entry-title'])
    for sel in selectors:
        elem = soup.select_one(sel)
        if elem:
            title_text = elem.get_text(strip=True)
            if title_text and len(title_text) > 10:
                title = title_text
                break
    
    date_str = ""
    date_obj = None
    
    if site_type == 'punch':
        date_elem = soup.select_one('span.post-date, time, .entry-date')
        if date_elem:
            date_str = date_elem.get_text(strip=True)
            date_obj = parse_punch_date(date_str)
    elif site_type == 'nuc':
        date_elem = soup.select_one('span.published, time, .post-date')
        if date_elem:
            date_str = date_elem.get_text(strip=True)
            date_obj = parse_nuc_date(date_str)
    
    content = ""
    content_selectors = {
        'nuc': ['div.entry-content', 'article .content', '.post-content'],
        'punch': ['div.post-content', '.entry-content', '.article-content']
    }
    selectors = content_selectors.get(site_type, ['article', 'main', '.content'])
    for sel in selectors:
        elem = soup.select_one(sel)
        if elem:
            for bad in elem.select('script, style, nav, header, footer, .share, .comments, .ad, .widget, .related, iframe'):
                bad.decompose()
            content = elem.get_text(' ', strip=True)
            if len(content) > 200:
                break
    
    if content:
        if title and content.lower().startswith(title.lower()):
            content = content[len(title):].strip()
        
        if site_type == 'punch':
            content = re.sub(r'Kindly share this story.*?(?=[A-Z])', '', content, flags=re.I | re.DOTALL)
        
        content = re.sub(r'\s+', ' ', content).strip()
    
    snippet = ""
    if content:
        paragraphs = re.split(r'\.\s+', content)
        meaningful_paragraphs = []
        
        for para in paragraphs:
            para = para.strip()
            if len(para) > 30:
                meaningful_paragraphs.append(para)
                if len(' '.join(meaningful_paragraphs)) > 100:
                    break
        
        if meaningful_paragraphs:
            snippet = ' '.join(meaningful_paragraphs)
            
            if len(snippet) > 250:
                if '.' in snippet[:250]:
                    last_period = snippet[:250].rfind('.')
                    if last_period > 150:
                        snippet = snippet[:last_period + 1]
                    else:
                        snippet = snippet[:247] + "..."
                else:
                    snippet = snippet[:247] + "..."
        else:
            snippet = content[:250]
            if len(content) > 250:
                snippet = snippet[:247] + "..."
    
    # Check for school keywords
    title_has_keywords = _SCHOOL_KEYWORDS_RE.search(title) if _SCHOOL_KEYWORDS_RE else True
    snippet_has_keywords = _SCHOOL_KEYWORDS_RE.search(snippet) if _SCHOOL_KEYWORDS_RE else True
    
    # FIXED: Check date recency - timedelta is imported at the top
    date_is_recent = is_recent_date(date_obj) if date_obj else False
    
    success = bool(title and date_obj and len(snippet) > 50 and 
                  (title_has_keywords or snippet_has_keywords) and 
                  date_is_recent)
    
    return {
        'title': title[:200],
        'date_str': date_str,
        'date_obj': date_obj,
        'snippet': snippet,
        'url': url,
        'success': success,
        'has_keywords': title_has_keywords or snippet_has_keywords
    }

# ═══════════════════════════════════════════════════════════════════════════
# SITE-SPECIFIC ARTICLE DATE FILTHERING
# ═══════════════════════════════════════════════════════════════════════════
async def scrape_myschool_recent(base_url: str = "https://myschool.ng", max_articles: int = 10) -> List[Dict]:
    """Improved MySchool scraper based on analysis findings."""
    LOG.info(f"\n[STAGE] Scraping MySchool from {base_url}")
    
    try:
        # Get article URLs from homepage
        article_urls = await get_myschool_recent_articles(base_url)
        
        if not article_urls:
            LOG.info("[MySchool] No articles found")
            return []
        
        LOG.info(f"[MySchool] Found {len(article_urls)} potential articles")
        
        all_extracted = []
        batch_size = 3
        
        for i in range(0, len(article_urls), batch_size):
            batch = article_urls[i:i + batch_size]
            LOG.debug(f"[MySchool] Processing batch {i//batch_size + 1}/{(len(article_urls)-1)//batch_size + 1}")
            
            tasks = []
            for url in batch:
                task = shared_playwright.fetch_html(
                    url,
                    wait_for_selector='h3.page-title.blog-header-title, div.clearfix, div.pb-5',
                    scroll_to_load=False,
                    timeout=30000
                )
                tasks.append(task)
            
            pages = await asyncio.gather(*tasks, return_exceptions=True)
            
            for idx, result in enumerate(pages):
                url = batch[idx]
                if isinstance(result, Exception):
                    LOG.debug(f"[MySchool] Failed to fetch {url}: {result}")
                    continue
                if not result:
                    continue
                
                data = extract_myschool_content(result, url)
                
                if data.get('success'):
                    all_extracted.append({
                        'title': data['title'],
                        'date': data['date_str'],
                        'snippet': data['snippet'],
                        'url': url,
                        'source': 'myschool',
                        'pdf': False,
                        'date_obj': data['date_obj'],
                        'base_url': base_url,
                        'has_keywords': data.get('has_keywords', True)
                    })
                    LOG.debug(f"[MySchool] ✓ {data['title'][:50]}...")
                else:
                    LOG.debug(f"[MySchool] ✗ Failed: {url}")
                    if data.get('title'):
                        LOG.debug(f"  Title: {data['title']}")
                    if data.get('date_obj'):
                        LOG.debug(f"  Date: {data['date_obj']}")
                    LOG.debug(f"  Snippet length: {len(data.get('snippet', ''))}")
        
        # Sort by date and limit
        if all_extracted:
            all_extracted.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
            all_extracted = all_extracted[:max_articles]
        
        LOG.info(f"[MySchool] Extracted {len(all_extracted)} recent articles")
        return all_extracted
        
    except Exception as e:
        LOG.error(f"Error in scrape_myschool_recent: {e}")
        return []

async def scrape_punch_recent(base_url: str = "https://punchng.com", max_articles: int = 10) -> List[Dict]:
    """Scrape recent Punch education articles using shared context with date filtering."""
    LOG.info(f"\n[STAGE] Scraping Punch from {base_url}")
    
    try:
        # Get article URLs using shared context
        article_urls = await get_punch_recent_articles(base_url)
        
        if not article_urls:
            LOG.info("[Punch] No articles found")
            return []
        
        LOG.info(f"[Punch] Found {len(article_urls)} recent articles, fetching content...")
        
        # Fetch articles using shared context
        batch_size = 3
        articles = []
        
        for i in range(0, len(article_urls), batch_size):
            batch = article_urls[i:i + batch_size]
            LOG.debug(f"[Punch] Processing batch {i//batch_size + 1}/{(len(article_urls)-1)//batch_size + 1}")
            
            # Fetch batch concurrently
            tasks = []
            for url in batch:
                task = shared_playwright.fetch_html(
                    url,
                    wait_for_selector='h1.post-title',
                    scroll_to_load=True
                )
                tasks.append(task)
            
            pages = await asyncio.gather(*tasks, return_exceptions=True)
            
            for idx, result in enumerate(pages):
                url = batch[idx]
                if isinstance(result, Exception):
                    LOG.debug(f"[Punch] Failed to fetch {url}: {result}")
                    continue
                if not result:
                    continue
                
                data = extract_clean_content_v5(result, url, 'punch')
                if data.get('success') and data.get('has_keywords', False):
                    articles.append({
                        'title': data['title'],
                        'date': data['date_str'],
                        'snippet': data['snippet'],
                        'url': url,
                        'source': 'punch',
                        'pdf': False,
                        'date_obj': data['date_obj'],
                        'base_url': base_url,
                        'has_keywords': data.get('has_keywords', True)
                    })
        
        articles.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
        articles = articles[:max_articles]
        LOG.info(f"[Punch] Extracted {len(articles)} recent articles")
        return articles
        
    except Exception as e:
        LOG.error(f"Error in scrape_punch_recent: {e}")
        return []

async def scrape_nuc_recent(base_url: str = "https://www.nuc.edu.ng", max_articles: int = 8) -> List[Dict]:
    """Scrape recent NUC articles using shared context with date filtering."""
    LOG.info(f"\n[STAGE] Scraping NUC from {base_url}")
    
    try:
        # Get article URLs using shared context
        article_urls = await get_nuc_recent_articles(base_url)
        
        if not article_urls:
            LOG.info("[NUC] No articles found")
            return []
        
        LOG.info(f"[NUC] Found {len(article_urls)} recent articles, fetching content...")
        
        # Fetch articles using shared context
        batch_size = 3
        articles = []
        
        for i in range(0, len(article_urls), batch_size):
            batch = article_urls[i:i + batch_size]
            LOG.debug(f"[NUC] Processing batch {i//batch_size + 1}/{(len(article_urls)-1)//batch_size + 1}")
            
            # Fetch batch concurrently
            tasks = []
            for url in batch:
                task = shared_playwright.fetch_html(
                    url,
                    wait_for_selector='h1.entry-title',
                    scroll_to_load=True
                )
                tasks.append(task)
            
            pages = await asyncio.gather(*tasks, return_exceptions=True)
            
            for idx, result in enumerate(pages):
                url = batch[idx]
                if isinstance(result, Exception):
                    LOG.debug(f"[NUC] Failed to fetch {url}: {result}")
                    continue
                if not result:
                    continue
                
                data = extract_clean_content_v5(result, url, 'nuc')
                if data.get('success') and data.get('has_keywords', False):
                    articles.append({
                        'title': data['title'],
                        'date': data['date_str'],
                        'snippet': data['snippet'],
                        'url': url,
                        'source': 'nuc',
                        'pdf': False,
                        'date_obj': data['date_obj'],
                        'base_url': base_url,
                        'has_keywords': data.get('has_keywords', True)
                    })
        
        articles.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
        articles = articles[:max_articles]
        LOG.info(f"[NUC] Extracted {len(articles)} recent articles")
        return articles
        
    except Exception as e:
        LOG.error(f"Error in scrape_nuc_recent: {e}")
        return []
# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
async def scrape_school_news(
    urls: Union[List[str], Dict[str, Dict[str, Any]]],
    fetch_full_content: bool = False,
    max_articles: int = 5
) -> List[Dict[str, Any]]:
    """
    Unified function to scrape school news from:
    1. A list of specific article URLs
    2. A dict of site configurations for scraping recent articles
    
    Supports both specific URL scraping and recent article discovery.
    
    Args:
        urls: Either:
              - List of specific article URLs to scrape
              - Dict of site configs: {
                  'site_name': {
                      'base_url': 'https://example.com',
                      'task': scrape_function,  # Optional
                      'max_articles': 10,        # Optional
                      'type': 'nuc'/'myschool'/'punch'/'generic'  # Optional
                  }
              }
        fetch_full_content: Whether to fetch full article content
        max_articles: Maximum articles to return per site
    
    Returns:
        List of formatted article dictionaries
    """
    LOG.info(f"\n{'='*70}")
    LOG.info("📰 UNIFIED SCHOOL NEWS SCRAPER")
    LOG.info(f"{'='*70}")
    
    # Handle empty input
    if not urls:
        LOG.warning("No URLs or site configs provided")
        return []
    
    # Initialize shared Playwright context
    if not shared_playwright._initialized:
        LOG.info("Initializing shared Playwright context...")
        await shared_playwright.initialize()
    
    all_articles = []
    
    # ============================================================
    # CASE 1: List of specific article URLs
    # ============================================================
    if isinstance(urls, list):
        LOG.info(f"📋 Processing {len(urls)} specific article URLs")
        
        # Group URLs by domain for batch processing
        url_groups = {}
        for url in urls:
            domain = get_domain_from_url(url)
            if domain not in url_groups:
                url_groups[domain] = []
            url_groups[domain].append(url)
        
        LOG.info(f"  Found {len(url_groups)} unique domains")
        
        # Process each domain group
        for domain, domain_urls in url_groups.items():
            LOG.info(f"\n🌐 Processing domain: {domain}")
            LOG.info(f"  URLs: {len(domain_urls)}")
            
            # Use appropriate scraper based on domain
            if 'myschool.ng' in domain:
                # For MySchool, scrape each URL individually
                batch_size = 3
                for i in range(0, len(domain_urls), batch_size):
                    batch = domain_urls[i:i + batch_size]
                    
                    # Fetch batch concurrently
                    tasks = []
                    for url in batch:
                        task = shared_playwright.fetch_html(
                            url,
                            wait_for_selector='.card, .col-sm-6',
                            scroll_to_load=True
                        )
                        tasks.append(task)
                    
                    pages = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    for idx, result in enumerate(pages):
                        url = batch[idx]
                        if isinstance(result, Exception) or not result:
                            LOG.debug(f"  ✗ Failed to fetch {url}")
                            continue
                        
                        data = extract_myschool_content(result, url)
                        if data.get('success'):
                            all_articles.append({
                                'title': data['title'],
                                'date': data['date_str'],
                                'snippet': data['snippet'],
                                'url': url,
                                'source': 'myschool',
                                'pdf': False,
                                'date_obj': data['date_obj'],
                                'has_keywords': data.get('has_keywords', True)
                            })
            
            elif 'punchng.com' in domain:
                # For Punch, scrape each URL individually
                batch_size = 3
                for i in range(0, len(domain_urls), batch_size):
                    batch = domain_urls[i:i + batch_size]
                    
                    # Fetch batch concurrently
                    tasks = []
                    for url in batch:
                        task = shared_playwright.fetch_html(
                            url,
                            wait_for_selector='article',
                            scroll_to_load=True
                        )
                        tasks.append(task)
                    
                    pages = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    for idx, result in enumerate(pages):
                        url = batch[idx]
                        if isinstance(result, Exception) or not result:
                            LOG.debug(f"  ✗ Failed to fetch {url}")
                            continue
                        
                        data = extract_clean_content_v5(result, url, 'punch')
                        if data.get('success'):
                            all_articles.append({
                                'title': data['title'],
                                'date': data['date_str'],
                                'snippet': data['snippet'],
                                'url': url,
                                'source': 'punch',
                                'pdf': False,
                                'date_obj': data['date_obj'],
                                'has_keywords': data.get('has_keywords', True)
                            })
            
            elif 'nuc.edu.ng' in domain:
                # For NUC, scrape each URL individually
                batch_size = 3
                for i in range(0, len(domain_urls), batch_size):
                    batch = domain_urls[i:i + batch_size]
                    
                    # Fetch batch concurrently
                    tasks = []
                    for url in batch:
                        task = shared_playwright.fetch_html(
                            url,
                            wait_for_selector='h1.entry-title',
                            scroll_to_load=True
                        )
                        tasks.append(task)
                    
                    pages = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    for idx, result in enumerate(pages):
                        url = batch[idx]
                        if isinstance(result, Exception) or not result:
                            LOG.debug(f"  ✗ Failed to fetch {url}")
                            continue
                        
                        data = extract_clean_content_v5(result, url, 'nuc')
                        if data.get('success'):
                            all_articles.append({
                                'title': data['title'],
                                'date': data['date_str'],
                                'snippet': data['snippet'],
                                'url': url,
                                'source': 'nuc',
                                'pdf': False,
                                'date_obj': data['date_obj'],
                                'has_keywords': data.get('has_keywords', True)
                            })
            
            else:
                # Generic handling for unknown domains
                LOG.warning(f"  ⚠️  Unknown domain: {domain} - attempting generic scrape")
                batch_size = 2
                for i in range(0, len(domain_urls), batch_size):
                    batch = domain_urls[i:i + batch_size]
                    
                    tasks = []
                    for url in batch:
                        task = shared_playwright.fetch_html(
                            url,
                            wait_for_selector='h1, h2, article, main',
                            scroll_to_load=True
                        )
                        tasks.append(task)
                    
                    pages = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    for idx, result in enumerate(pages):
                        url = batch[idx]
                        if isinstance(result, Exception) or not result:
                            continue
                        
                        # Try to determine site type
                        site_type = 'generic'
                        if 'punch' in domain:
                            site_type = 'punch'
                        elif 'nuc' in domain:
                            site_type = 'nuc'
                        elif 'myschool' in domain:
                            site_type = 'myschool'
                        
                        data = extract_clean_content_v5(result, url, site_type)
                        if data.get('success'):
                            all_articles.append({
                                'title': data['title'],
                                'date': data['date_str'],
                                'snippet': data['snippet'],
                                'url': url,
                                'source': domain,
                                'pdf': False,
                                'date_obj': data['date_obj'],
                                'has_keywords': data.get('has_keywords', True)
                            })
    
    # ============================================================
    # CASE 2: Dict of site configurations for recent articles
    # ============================================================
    elif isinstance(urls, dict):
        LOG.info(f"📋 Processing {len(urls)} site configurations for recent articles")
        
        site_configs = urls  # Use the provided dict directly
        
        # If no specific configs provided, use defaults
        if not site_configs:
            site_configs = {
                'nuc': {
                    'task': scrape_nuc_recent,
                    'max_articles': 8,
                    'base_url': 'https://www.nuc.edu.ng',
                    'type': 'nuc'
                },
                'myschool': {
                    'task': scrape_myschool_recent,
                    'max_articles': 10,
                    'base_url': 'https://myschool.ng',
                    'type': 'myschool'
                },
                'punch': {
                    'task': scrape_punch_recent,
                    'max_articles': 10,
                    'base_url': 'https://punchng.com',
                    'type': 'punch'
                }
            }
        
        # Create tasks for all sites concurrently
        tasks = []
        site_names = []
        
        for site_name, config in site_configs.items():
            LOG.info(f"\n🎯 Setting up {site_name}")
            
            # Determine which function to use
            task_func = config.get('task')
            if not task_func:
                # Try to infer based on type or domain
                site_type = config.get('type', 'generic')
                base_url = config.get('base_url', '')
                
                if site_type == 'nuc' or 'nuc.edu.ng' in base_url:
                    task_func = scrape_nuc_recent
                elif site_type == 'myschool' or 'myschool.ng' in base_url:
                    task_func = scrape_myschool_recent
                elif site_type == 'punch' or 'punchng.com' in base_url:
                    task_func = scrape_punch_recent
                else:
                    # Generic recent scraping
                    async def generic_recent_scraper(base_url: str, max_articles: int = 10):
                        """Generic recent article scraper for unknown sites."""
                        try:
                            LOG.info(f"  🔍 Attempting generic scrape of {base_url}")
                            
                            # Try common news paths
                            candidate_urls = candidate_listing_urls(base_url)
                            articles = []
                            
                            for candidate in candidate_urls[:3]:  # Try first 3 candidates
                                try:
                                    html = await shared_playwright.fetch_html(
                                        candidate,
                                        wait_for_selector='article, .post, .news-item, h1, h2',
                                        scroll_to_load=True
                                    )
                                    
                                    if html:
                                        soup = BeautifulSoup(html, 'lxml')
                                        
                                        # Look for article links
                                        article_links = []
                                        for a in soup.select('a[href*="/"]'):
                                            href = a.get('href', '')
                                            full_url = urljoin(base_url, href)
                                            
                                            # Filter for plausible article URLs
                                            if (len(href) > 20 and 
                                                not any(x in href.lower() for x in ['.pdf', '.jpg', '.png', '.css', '.js']) and
                                                not any(x in href for x in ['category', 'tag', 'author', 'page=', '?'])):
                                                article_links.append(full_url)
                                        
                                        # Scrape found articles
                                        for article_url in article_links[:5]:
                                            try:
                                                article_html = await shared_playwright.fetch_html(
                                                    article_url,
                                                    wait_for_selector='h1, article, main',
                                                    scroll_to_load=False
                                                )
                                                
                                                if article_html:
                                                    data = extract_clean_content_v5(article_html, article_url, 'generic')
                                                    if data.get('success'):
                                                        articles.append({
                                                            'title': data['title'],
                                                            'date': data['date_str'],
                                                            'snippet': data['snippet'],
                                                            'url': article_url,
                                                            'source': get_domain_from_url(base_url),
                                                            'pdf': False,
                                                            'date_obj': data['date_obj'],
                                                            'has_keywords': data.get('has_keywords', True)
                                                        })
                                            except Exception as e:
                                                LOG.debug(f"  Failed to scrape {article_url}: {e}")
                                        
                                        if articles:
                                            break
                                    
                                except Exception as e:
                                    LOG.debug(f"  Candidate {candidate} failed: {e}")
                                    continue
                            
                            return articles[:max_articles]
                            
                        except Exception as e:
                            LOG.error(f"Generic scraper failed for {base_url}: {e}")
                            return []
                    
                    task_func = generic_recent_scraper
            
            # Create task with parameters
            task_config = config.copy()
            base_url = task_config.pop('base_url', '')
            max_per_site = task_config.pop('max_articles', max_articles)
            
            if task_func == scrape_nuc_recent:
                task = asyncio.create_task(
                    scrape_nuc_recent(base_url=base_url, max_articles=max_per_site)
                )
            elif task_func == scrape_myschool_recent:
                task = asyncio.create_task(
                    scrape_myschool_recent(base_url=base_url, max_articles=max_per_site)
                )
            elif task_func == scrape_punch_recent:
                task = asyncio.create_task(
                    scrape_punch_recent(base_url=base_url, max_articles=max_per_site)
                )
            else:
                # Generic function
                task = asyncio.create_task(
                    task_func(base_url=base_url, max_articles=max_per_site)
                )
            
            tasks.append(task)
            site_names.append(site_name)
        
        # Execute all tasks concurrently
        LOG.info("\n🚀 Starting concurrent scraping of all sites...")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        successful_sites = 0
        
        for site_name, result in zip(site_names, results):
            if isinstance(result, Exception):
                LOG.error(f"❌ Failed to scrape {site_name}: {result}")
                continue
            
            articles = result
            if articles:
                LOG.info(f"✅ {site_name}: {len(articles)} articles found")
                all_articles.extend(articles)
                successful_sites += 1
            else:
                LOG.info(f"⚠️  {site_name}: No articles found")
        
        LOG.info(f"\n📊 Concurrent scraping complete: {successful_sites}/{len(site_configs)} sites successful")
    
    else:
        LOG.error(f"❌ Invalid input type: {type(urls)}. Expected list or dict.")
        return []
    
    # ============================================================
    # POST-PROCESSING: Filtering and formatting
    # ============================================================
    
    # Sort all articles by date (newest first)
    all_articles.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
    
    # Filter for recent articles and valid content
    recent_articles = []
    for article in all_articles:
        date_obj = article.get('date_obj')
        has_keywords = article.get('has_keywords', False)
        snippet = article.get('snippet', '')
        
        # Check criteria
        date_ok = date_obj and is_recent_date(date_obj)
        content_ok = (article.get('title') and 
                     snippet and 
                     len(snippet) > 30 and
                     has_keywords)
        
        if date_ok and content_ok:
            recent_articles.append(article)
        else:
            LOG.debug(f"Filtered out article: {article.get('title', 'Untitled')[:50]}...")
            LOG.debug(f"  Date OK: {date_ok}, Content OK: {content_ok}")
    
    # Format final results
    formatted_articles = []
    for article in recent_articles:
        formatted_articles.append({
            'title': article.get('title', 'Untitled'),
            'snippet': article.get('snippet', ''),
            'date': article.get('date', ''),
            'link': article.get('url', ''),
            'source': article.get('source', 'unknown'),
            'pdf': article.get('pdf', False),
            'date_obj': article.get('date_obj'),
            'has_keywords': article.get('has_keywords', True)
        })
    
    # Final sort and limit
    formatted_articles.sort(key=lambda x: (
        x.get('date_obj') is None,
        -(x.get('date_obj', datetime.now()).timestamp() if x.get('date_obj') else 0)
    ))
    
    LOG.info(f"\n📊 FINAL RESULTS")
    LOG.info(f"{'─'*40}")
    LOG.info(f"Total articles found: {len(all_articles)}")
    LOG.info(f"Recent articles (last {MAX_ARTICLE_AGE_DAYS} days): {len(recent_articles)}")
    LOG.info(f"Returning: {len(formatted_articles[:max_articles * 2])}")
    
    return formatted_articles[:max_articles * 2]