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
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup, Tag
import cloudscraper
from requests.exceptions import RequestException
from typing import Dict, Optional, Any, Tuple, Callable, List, Set, Union,Iterable
from collections import Counter, deque
from playwright._impl._errors import TargetClosedError
import aiohttp
import importlib
import time
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
_cloudscraper_spec = importlib.util.find_spec("cloudscraper")
if _cloudscraper_spec is not None:
    import cloudscraper
else:
    cloudscraper = None

class SharedPlaywrightManager:
    """
    Optimized hybrid manager:
    - HTTP-first (cloudscraper → aiohttp) for most sites
    - Lazy browser creation (only when needed)
    - Auto-recreate browser when Cloudflare blocks detected
    - Sequential processing to minimize memory
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
            cls._instance._cleanup_lock = asyncio.Lock()  # NEW: Separate lock for cleanup
            cls._instance._browser_lock = asyncio.Lock()  # NEW: Separate lock for browser ops
        return cls._instance

    async def initialize(self, **kwargs):
        """Lightweight init - just start Playwright."""
        async with self._lock:
            if self._initialized:
                LOG.info("[INIT] Already initialized")
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
        """Create browser + context only when needed."""
        async with self._browser_lock:  # NEW: Use dedicated browser lock
            if not force_new and self._browser and self._context:
                LOG.debug("[ENSURE_BROWSER] ✅ Browser already exists")
                return
            
            if force_new and (self._browser or self._context):
                LOG.info("[ENSURE_BROWSER] ♻️ Force recreate - cleaning up old browser...")
                await self._cleanup_browser()
                
            LOG.info("[ENSURE_BROWSER] 🌐 Creating fresh browser + context...")
            
            try:
                # Launch browser with explicit timeout
                self._browser = await asyncio.wait_for(
                    self._playwright.chromium.launch(
                        headless=self._cfg["headless"],
                        args=self._cfg["args"],
                        timeout=60000
                    ),
                    timeout=65  # Slightly longer than internal timeout
                )
                LOG.info("[ENSURE_BROWSER] ✅ Browser launched")
                
                # Create context with anti-detection
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
        """Close everything - browser, context, pages."""
        async with self._cleanup_lock:  # NEW: Use dedicated cleanup lock
            LOG.info("[CLEANUP_BROWSER] 🧹 Closing browser resources...")
            
            # Close active page with timeout
            if self._active_page:
                try:
                    if not self._active_page.is_closed():
                        await asyncio.wait_for(self._active_page.close(), timeout=5)
                    LOG.debug("[CLEANUP_BROWSER]   Active page closed")
                except asyncio.TimeoutError:
                    LOG.warning("[CLEANUP_BROWSER]   ⚠️ Page close timeout, forcing...")
                    try:
                        # Try harder to close
                        await self._active_page.evaluate("window.stop()")
                        await asyncio.sleep(0.1)
                        if not self._active_page.is_closed():
                            await self._active_page.close()
                    except Exception:
                        pass
                except Exception as e:
                    LOG.debug(f"[CLEANUP_BROWSER]   Page close error: {e}")
                finally:
                    self._active_page = None
            
            # Close context with timeout
            if self._context:
                try:
                    await asyncio.wait_for(self._context.close(), timeout=10)
                    LOG.debug("[CLEANUP_BROWSER]   Context closed")
                except asyncio.TimeoutError:
                    LOG.warning("[CLEANUP_BROWSER]   ⚠️ Context close timeout")
                except Exception as e:
                    LOG.debug(f"[CLEANUP_BROWSER]   Context close error: {e}")
                finally:
                    self._context = None
            
            # Close browser with timeout - MOST CRITICAL
            if self._browser:
                try:
                    # First try graceful close
                    await asyncio.wait_for(self._browser.close(), timeout=15)
                    LOG.info("[CLEANUP_BROWSER] ✅ Browser closed gracefully")
                except asyncio.TimeoutError:
                    LOG.warning("[CLEANUP_BROWSER]   ⚠️ Browser close timeout, forcing...")
                    try:
                        # Force kill if possible
                        if hasattr(self._browser, '_browser'):
                            proc = self._browser._browser
                            if proc and hasattr(proc, 'pid'):
                                import os
                                import signal
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
        """Apply anti-detection to page."""
        LOG.debug("[SETUP_PAGE] 🔧 Setting up page...")
        width = random.randint(1280, 1920)
        height = random.randint(720, 1080)
        await page.set_viewport_size({"width": width, "height": height})
        LOG.debug(f"[SETUP_PAGE] 📐 Viewport: {width}x{height}")
        try:
            # Enhanced anti-detection
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
                
                Object.defineProperty(navigator, 'plugins', {{ get: () => [1, 2, 3, 4, 5] }});
                Object.defineProperty(navigator, 'languages', {{ get: () => ['en-US', 'en'] }});
                Object.defineProperty(navigator, 'platform', {{ get: () => 'Win32' }});
                Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {random.choice([4, 8, 16])} }});
                Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {random.choice([4, 8, 16])} }});
                
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

        # Block resources
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

    async def _cloudscraper_fetch(self, url: str, timeout: int = 20) -> str:
        """Fetch using cloudscraper with realistic browser headers."""
        if cloudscraper is None:
            LOG.debug("[CLOUDSCRAPER] ℹ️  Module not available")
            return ""
        
        LOG.debug(f"[CLOUDSCRAPER] 📡 Fetching {url[:60]}...")
        try:
            loop = asyncio.get_running_loop()
            
            def _sync_get():
                # Create scraper with browser emulation
                s = cloudscraper.create_scraper(
                    browser={
                        'browser': 'chrome',
                        'platform': random.choice(['windows', 'darwin', 'linux']),
                        'mobile': False
                    }
                )
                
                # Comprehensive headers that mimic real browser
                parsed = urlparse(url)
                domain = parsed.netloc
                
                headers = {
                    'User-Agent': random.choice(self._cfg["user_agent_list"]),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                    'Accept-Language': 'en-US,en;q=0.9',
                    # ✅ FIX: Don't request Brotli (br) - only gzip/deflate which are reliably handled
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
                
                # Add random referer for non-direct visits (looks more natural)
                if random.random() > 0.3:  # 70% of time add referer
                    referers = [
                        'https://www.google.com/',
                        'https://www.bing.com/',
                        f'https://{domain}/',
                    ]
                    headers['Referer'] = random.choice(referers)
                
                r = s.get(url, headers=headers, timeout=timeout)
                
                # ✅ FIX: Handle potential binary/compressed responses
                if r.status_code == 200:
                    content = r.content  # Get raw bytes first
                    
                    # Check if it's already text
                    if isinstance(content, str):
                        return content
                    
                    # Try to detect encoding and decode
                    encoding = r.encoding
                    if encoding:
                        try:
                            return content.decode(encoding)
                        except (UnicodeDecodeError, LookupError):
                            pass
                    
                    # Try common encodings
                    for enc in ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']:
                        try:
                            return content.decode(enc)
                        except UnicodeDecodeError:
                            continue
                    
                    # If all else fails, use errors='replace'
                    return content.decode('utf-8', errors='replace')
                
                return ""
            
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_get),
                timeout=timeout + 5
            )
            
            # ✅ Additional safety check for binary data
            if result and isinstance(result, str):
                # Check if it looks like binary (high ratio of non-printable chars)
                sample = result[:1000]
                non_printable = sum(1 for c in sample if ord(c) < 32 and c not in '\n\r\t')
                if non_printable > len(sample) * 0.1:  # More than 10% non-printable
                    LOG.warning(f"[CLOUDSCRAPER] ⚠️ Response appears to be binary ({non_printable} non-printable chars)")
                    return ""
            
            if result:
                LOG.debug(f"[CLOUDSCRAPER] ✅ Success: {len(result)} bytes")
            else:
                LOG.debug("[CLOUDSCRAPER] ⚠️ Empty result")
            return result
            
        except asyncio.TimeoutError:
            LOG.debug("[CLOUDSCRAPER] ❌ Timeout")
            return ""
        except Exception as e:
            LOG.debug(f"[CLOUDSCRAPER] ❌ Failed: {e}")
            return ""
    
    async def _aiohttp_fetch(self, url: str, timeout: int = 20) -> str:
        """Fetch using aiohttp with comprehensive browser headers."""
        LOG.debug(f"[AIOHTTP] 📡 Fetching {url[:60]}...")
        
        # Parse URL for domain-specific headers
        parsed = urlparse(url)
        domain = parsed.netloc
        
        # Comprehensive browser headers
        headers = {
            'User-Agent': random.choice(self._cfg["user_agent_list"]),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9,en-GB;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
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
        
        # Add random referer for credibility (70% of time)
        if random.random() > 0.3:
            referers = [
                'https://www.google.com/',
                'https://www.google.com.ng/',  # Nigerian Google
                'https://www.bing.com/',
                f'https://{domain}/',
            ]
            headers['Referer'] = random.choice(referers)
        
        try:
            # Use TCPConnector with better settings
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=5,
                ttl_dns_cache=300,
                ssl=False,  # Disable SSL verification for problematic sites
            )
            
            client_timeout = aiohttp.ClientTimeout(
                total=timeout,
                connect=10,
                sock_read=timeout
            )
            
            async with aiohttp.ClientSession(
                headers=headers,
                connector=connector,
                timeout=client_timeout
            ) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        result = await resp.text()
                        LOG.debug(f"[AIOHTTP] ✅ Success: {len(result)} bytes")
                        return result
                    else:
                        LOG.debug(f"[AIOHTTP] ⚠️ Status {resp.status}")
                        
        except asyncio.TimeoutError:
            LOG.debug("[AIOHTTP] ❌ Timeout")
        except aiohttp.ClientError as e:
            LOG.debug(f"[AIOHTTP] ❌ Client error: {type(e).__name__}: {str(e)[:100]}")
        except Exception as e:
            LOG.debug(f"[AIOHTTP] ❌ Failed: {type(e).__name__}: {str(e)[:100]}")
        
        return ""

    async def _wait_for_cloudflare_clearance(self, page: Page, timeout: int = 20000) -> bool:
        """Wait for Cloudflare challenge to complete."""
        LOG.info("[CLOUDFLARE] ⏳ Waiting for clearance...")
        
        try:
            await page.wait_for_function(
                """
                () => {
                    const title = document.title || '';
                    const body = document.body?.innerText || '';
                    
                    const isChallenging = title.toLowerCase().includes('just a moment') || 
                                         body.toLowerCase().includes('verifying you are human') ||
                                         body.toLowerCase().includes('checking your browser');
                    
                    return !isChallenging;
                }
                """,
                timeout=timeout
            )
            LOG.info("[CLOUDFLARE] ✅ Clearance obtained")
            return True
            
        except Exception as e:
            LOG.warning(f"[CLOUDFLARE] ⚠️ Timeout: {e}")
            return False

    def _is_cloudflare_blocked(self, status: int, title: str, body: str) -> bool:
        """Check if page is Cloudflare blocked."""
        title_lower = title.lower()
        body_lower = body.lower()
        
        return (
            status == 403 or
            'just a moment' in title_lower or
            'verifying you are human' in body_lower or
            'checking your browser' in body_lower or
            'cloudflare' in body_lower and ('challenge' in body_lower or 'ray id' in body_lower)
        )

    async def fetch_html(
        self,
        url: str,
        wait_for_selector: Optional[str] = None,
        scroll_to_load: bool = False,
        timeout: Optional[int] = None,
        partial_on_timeout: bool = True,
        recreate_on_block: bool = True,
    ) -> str:
        """
        Fetch HTML with Playwright.
        Creates browser if needed, recreates if blocked.
        """
        LOG.info(f"[FETCH_HTML] 🚀 ENTRY → {url[:80]}")
        
        # Ensure browser exists
        await self._ensure_browser()
        
        # Create fresh page with timeout
        LOG.info("[FETCH_HTML] 📄 Creating fresh page...")
        page = None
        try:
            page = await asyncio.wait_for(self._context.new_page(), timeout=15)
            await self._setup_page(page)
            self._active_page = page
            self._browser_uses += 1
            LOG.info(f"[FETCH_HTML] ✅ Page created (browser_uses={self._browser_uses})")
        except asyncio.TimeoutError:
            LOG.error("[FETCH_HTML] ❌ Page creation timeout")
            return ""
        except Exception as e:
            LOG.error(f"[FETCH_HTML] ❌ Page creation failed: {e}")
            return ""
        
        main_document_body = None
        
        # Response handler
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
            
            # Navigation strategy
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
                    LOG.info(f"[FETCH_HTML] ✅ Navigation SUCCESS in {nav_duration:.2f}s")
                    LOG.info(f"[FETCH_HTML]   Status: {status}")
                    
                    # Check for Cloudflare block
                    await page.wait_for_timeout(1500)
                    page_title = await page.title()
                    body_text = await page.evaluate("() => document.body?.innerText || ''")
                    
                    is_blocked = self._is_cloudflare_blocked(status, page_title, body_text)
                    
                    if is_blocked:
                        LOG.warning(f"[FETCH_HTML] ☁️ Cloudflare BLOCK detected!")
                        LOG.warning(f"[FETCH_HTML]   Title: '{page_title}'")
                        LOG.warning(f"[FETCH_HTML]   Body preview: {body_text[:200]}")
                        
                        if recreate_on_block:
                            LOG.warning(f"[FETCH_HTML] ♻️ RECREATING browser to bypass block...")
                            
                            # Close page first with timeout
                            try:
                                await asyncio.wait_for(page.close(), timeout=5)
                            except Exception:
                                pass
                                
                            await self._ensure_browser(force_new=True)
                            
                            delay = random.uniform(3, 7)
                            LOG.info(f"[FETCH_HTML] ⏳ Waiting {delay:.1f}s before retry...")
                            await asyncio.sleep(delay)
                            
                            # Try again with fresh browser
                            LOG.info(f"[FETCH_HTML] 🔄 Retrying with fresh browser...")
                                
                            try:
                                page = await asyncio.wait_for(self._context.new_page(), timeout=15)
                                await self._setup_page(page)
                                self._active_page = page
                                page.on("response", _on_response)
                                    
                                await page.goto(url, wait_until=wait_until, timeout=goto_timeout)
                                await page.wait_for_timeout(2000)
                                    
                                # Check again
                                retry_title = await page.title()
                                retry_body = await page.evaluate("() => document.body?.innerText || ''")
                                    
                                if self._is_cloudflare_blocked(200, retry_title, retry_body):
                                    LOG.error(f"[FETCH_HTML] ❌ Still blocked after browser recreation")
                                    return ""
                                else:
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
                
                # Try partial content
                try:
                    partial_html = await page.content()
                    if partial_on_timeout and partial_html and len(partial_html) > 800:
                        LOG.info(f"[FETCH_HTML] ✅ Returning partial: {len(partial_html)} bytes")
                        return partial_html
                except Exception:
                    pass
                
                LOG.error(f"[FETCH_HTML] ❌ Timeout - no content")
                return ""
            
            # Post-navigation
            await page.wait_for_timeout(1000)
            
            # Scrolling
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

            # Wait for selector
            if wait_for_selector:
                try:
                    LOG.info(f"[FETCH_HTML] ⏳ Waiting for selector...")
                    await page.wait_for_selector(wait_for_selector, timeout=10000)
                    LOG.info(f"[FETCH_HTML] ✅ Selector found")
                except Exception:
                    LOG.warning(f"[FETCH_HTML] ⚠️ Selector timeout")

            # Get content
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
            # Remove handler
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass
            
            # Close page with timeout protection
            try:
                if page and not page.is_closed():
                    await asyncio.wait_for(page.close(), timeout=5)
                    LOG.debug("[FETCH_HTML] Page closed")
            except asyncio.TimeoutError:
                LOG.warning("[FETCH_HTML] ⚠️ Page close timeout")
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
        """
        Smart fetch with HTTP-first strategy.
        Falls back to Playwright when needed.
        """
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        
        LOG.info(f"[SMART_FETCH] 🎯 ENTRY for {url[:60]}")
        LOG.info(f"[SMART_FETCH]   Hostname: {hostname}")
        LOG.info(f"[SMART_FETCH]   prefer_http: {prefer_http}, playwright: {allow_playwright}")

        # Direct to Playwright for MySchool (known to block HTTP)
        if "myschool.ng" in hostname:
            LOG.info(f"[SMART_FETCH] 🎭 Using Playwright directly for MySchool")
            result = await self.fetch_html(
                url,
                wait_for_selector=wait_for_selector,
                scroll_to_load=scroll_to_load,
                timeout=play_timeout,
                partial_on_timeout=partial_on_timeout,
                recreate_on_block=True
            )
            LOG.info(f"[SMART_FETCH] ✅ Playwright returned {len(result)} bytes")
            return result

        # Try HTTP methods first if preferred
        if prefer_http:
            # Try cloudscraper
            LOG.info(f"[SMART_FETCH] ☁️  Trying cloudscraper...")
            html = ""
            if cloudscraper is not None:
                try:
                    html = await self._cloudscraper_fetch(url, http_timeout)
                    if html and len(html) >= min_http_length:
                        LOG.info(f"[SMART_FETCH] ✅ Cloudscraper success: {len(html)} bytes")
                        return html
                    else:
                        LOG.info(f"[SMART_FETCH] ⚠️ Cloudscraper: {len(html)} bytes (< {min_http_length})")
                except Exception as e:
                    LOG.warning(f"[SMART_FETCH] ❌ Cloudscraper failed: {e}")

            # Try aiohttp
            LOG.info(f"[SMART_FETCH] 🌐 Trying aiohttp...")
            try:
                html = await self._aiohttp_fetch(url, http_timeout)
                if html and len(html) >= min_http_length:
                    LOG.info(f"[SMART_FETCH] ✅ Aiohttp success: {len(html)} bytes")
                    return html
                else:
                    LOG.info(f"[SMART_FETCH] ⚠️ Aiohttp: {len(html)} bytes (< {min_http_length})")
            except Exception as e:
                LOG.warning(f"[SMART_FETCH] ❌ Aiohttp failed: {e}")

        # Fallback to Playwright
        if allow_playwright:
            LOG.info(f"[SMART_FETCH] 🎭 Falling back to Playwright...")
            result = await self.fetch_html(
                url,
                wait_for_selector=wait_for_selector,
                scroll_to_load=scroll_to_load,
                timeout=play_timeout,
                partial_on_timeout=partial_on_timeout,
                recreate_on_block=True
            )
            LOG.info(f"[SMART_FETCH] ✅ Playwright returned {len(result)} bytes")
            return result

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
        """
        Process URLs sequentially with smart_fetch.
        Cleans up browser after batch.
        """
        if fetch_kwargs is None:
            fetch_kwargs = {}
            
        url_list = list(urls)
        results = []
        
        LOG.info(f"[run_concurrent] 🔄 Processing {len(url_list)} URLs")
        LOG.info(f"[run_concurrent]   http_first={use_http_first}, playwright={allow_playwright}")
        
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
                                LOG.info(f"[run_concurrent]   ⏳ Waiting 3s before retry...")
                                await asyncio.sleep(3)
                                
                    except Exception as e:
                        LOG.error(f"[run_concurrent]   ❌ Error: {e}")
                        
                        if attempt <= retries:
                            await asyncio.sleep(3)
                
                results.append((url, html))
                
                # Delay between URLs
                if idx < len(url_list) - 1:
                    delay = random.uniform(5, 10)
                    LOG.info(f"[run_concurrent] ⏳ Waiting {delay:.1f}s before next URL...")
                    await asyncio.sleep(delay)
            
            # Cleanup browser after batch with timeout protection
            if self._browser:
                LOG.info(f"[run_concurrent] 🧹 Cleaning up browser (used {self._browser_uses} times)...")
                try:
                    await asyncio.wait_for(self._cleanup_browser(), timeout=30)
                except asyncio.TimeoutError:
                    LOG.error("[run_concurrent] ⚠️ Browser cleanup timeout, forcing...")
                    # Force cleanup
                    self._browser = None
                    self._context = None
                    self._active_page = None
                    self._browser_uses = 0
            
            # Summary
            success_count = sum(1 for _, html in results if html and len(html) > 800)
            LOG.info(f"[run_concurrent] 📊 COMPLETE: {success_count}/{len(url_list)} successful")
            
            return results
            
        except Exception as e:
            LOG.error(f"[run_concurrent] 💥 FATAL: {e}")
            # Emergency cleanup
            try:
                await asyncio.wait_for(self._cleanup_browser(), timeout=10)
            except Exception:
                self._browser = None
                self._context = None
                self._active_page = None
            raise

    async def cleanup(self):
        """Full cleanup."""
        LOG.info("[CLEANUP] 🧹 Full cleanup...")
        
        try:
            await asyncio.wait_for(self._cleanup_browser(), timeout=30)
        except asyncio.TimeoutError:
            LOG.error("[CLEANUP] ⚠️ Browser cleanup timeout")
        
        if self._playwright:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=10)
                LOG.info("[CLEANUP] ✅ Playwright stopped")
            except asyncio.TimeoutError:
                LOG.warning("[CLEANUP] ⚠️ Playwright stop timeout")
            except Exception as e:
                LOG.warning(f"[CLEANUP] ⚠️ Playwright stop error: {e}")
            finally:
                self._playwright = None
        
        self._initialized = False
        LOG.info("[CLEANUP] ✅ Complete")

# Global instance
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
    """
    Parse Punch.ng date strings into datetime objects.
    
    Handles multiple formats:
    - "February 9, 2026 8:40 pm"
    - "February 9, 2026"
    - "Feb 9, 2026 8:40 pm"
    - "2026-02-09T16:04:46+00:00" (ISO 8601 from JSON-LD)
    - "Monday, February 09, 2026"
    
    Args:
        date_str: Date string from Punch article
        
    Returns:
        datetime object or None if parsing fails
    """
    if not date_str or not isinstance(date_str, str):
        return None
    
    # Clean the string
    date_str = date_str.strip()
    
    # Remove day of week if present (e.g., "Monday, ")
    date_str = re.sub(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*', '', date_str, flags=re.I)
    
    # List of formats to try, in order of likelihood
    date_formats = [
        # With time (12-hour format with am/pm)
        '%B %d, %Y %I:%M %p',      # February 9, 2026 8:40 pm
        '%b %d, %Y %I:%M %p',      # Feb 9, 2026 8:40 pm
        '%B %d, %Y %I:%M%p',       # February 9, 2026 8:40pm (no space)
        '%b %d, %Y %I:%M%p',       # Feb 9, 2026 8:40pm
        
        # With time (24-hour format)
        '%B %d, %Y %H:%M',         # February 9, 2026 16:04
        '%b %d, %Y %H:%M',         # Feb 9, 2026 16:04
        
        # Date only
        '%B %d, %Y',               # February 9, 2026
        '%b %d, %Y',               # Feb 9, 2026
        '%B %d %Y',                # February 9 2026 (no comma)
        '%b %d %Y',                # Feb 9 2026
        
        # ISO 8601 (from JSON-LD)
        '%Y-%m-%dT%H:%M:%S%z',     # 2026-02-09T16:04:46+00:00
        '%Y-%m-%dT%H:%M:%S.%f%z',  # 2026-02-09T16:04:46.123+00:00
        '%Y-%m-%dT%H:%M:%SZ',      # 2026-02-09T16:04:46Z
        '%Y-%m-%dT%H:%M:%S',       # 2026-02-09T16:04:46
        
        # Alternative formats
        '%d %B, %Y',               # 9 February, 2026
        '%d %b, %Y',               # 9 Feb, 2026
        '%d/%m/%Y',                # 09/02/2026
        '%Y-%m-%d',                # 2026-02-09
    ]
    
    # Try each format
    for fmt in date_formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            LOG.debug(f"[parse_punch_date] Parsed '{date_str}' using format '{fmt}'")
            return dt
        except ValueError:
            continue
    
    # Special handling for ISO 8601 with 'Z' timezone
    if 'T' in date_str and date_str.endswith('Z'):
        try:
            # Replace Z with +00:00
            iso_str = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(iso_str)
            LOG.debug(f"[parse_punch_date] Parsed '{date_str}' using fromisoformat")
            return dt
        except Exception:
            pass
    
    # Special handling for timezone offsets like +00:00
    if 'T' in date_str and ('+' in date_str or date_str.count('-') > 2):
        try:
            dt = datetime.fromisoformat(date_str)
            LOG.debug(f"[parse_punch_date] Parsed '{date_str}' using fromisoformat")
            return dt
        except Exception:
            pass
    
    LOG.warning(f"[parse_punch_date] Failed to parse date: '{date_str}'")
    return None

def extract_punch_date_from_html(soup, url: str = "") -> Optional[datetime]:
    """
    Extract date from Punch article HTML using multiple strategies.
    
    Tries in order:
    1. JSON-LD structured data (most reliable)
    2. HTML meta tags
    3. Visible date elements
    4. Text pattern matching
    
    Args:
        soup: BeautifulSoup object of article page
        url: Article URL (for logging)
        
    Returns:
        datetime object or None
    """
    # Strategy 1: JSON-LD (most reliable)
    LOG.debug(f"[extract_punch_date] Strategy 1: Trying JSON-LD for {url[:60]}")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            
            # Handle both single object and @graph array
            items = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            
            for item in items:
                if item.get("@type") == "Article":
                    # Try datePublished first
                    date_str = item.get("datePublished")
                    if date_str:
                        dt = parse_punch_date(date_str)
                        if dt:
                            LOG.info(f"[extract_punch_date] ✅ Found via JSON-LD: {dt}")
                            return dt
                    
                    # Fallback to dateModified
                    date_str = item.get("dateModified")
                    if date_str:
                        dt = parse_punch_date(date_str)
                        if dt:
                            LOG.info(f"[extract_punch_date] ✅ Found via JSON-LD (modified): {dt}")
                            return dt
        except Exception as e:
            LOG.debug(f"[extract_punch_date] JSON-LD parse error: {e}")
            continue
    
    # Strategy 2: Meta tags
    LOG.debug(f"[extract_punch_date] Strategy 2: Trying meta tags")
    meta_selectors = [
        'meta[property="article:published_time"]',
        'meta[property="og:published_time"]',
        'meta[name="article:published_time"]',
        'meta[name="pubdate"]',
    ]
    
    for selector in meta_selectors:
        try:
            elem = soup.select_one(selector)
            if elem:
                date_str = elem.get('content', '')
                if date_str:
                    dt = parse_punch_date(date_str)
                    if dt:
                        LOG.info(f"[extract_punch_date] ✅ Found via meta tag: {dt}")
                        return dt
        except Exception:
            continue
    
    # Strategy 3: HTML date elements
    LOG.debug(f"[extract_punch_date] Strategy 3: Trying HTML elements")
    date_selectors = [
        'span.text-gray-500',           # New 2026 structure
        'span.post-date',               # Traditional structure
        'time',                         # HTML5 time element
        'div.flex.items-center span',  # Date container
        '.entry-date',
        '.published',
    ]
    
    for selector in date_selectors:
        try:
            elem = soup.select_one(selector)
            if elem:
                # Try datetime attribute first (for <time> tags)
                date_str = elem.get('datetime', '')
                if not date_str:
                    date_str = elem.get_text(strip=True)
                
                if date_str:
                    dt = parse_punch_date(date_str)
                    if dt:
                        LOG.info(f"[extract_punch_date] ✅ Found via {selector}: {dt}")
                        return dt
        except Exception:
            continue
    
    # Strategy 4: Text pattern matching (last resort)
    LOG.debug(f"[extract_punch_date] Strategy 4: Trying text patterns")
    try:
        page_text = soup.get_text()
        
        # Pattern 1: "February 9, 2026 8:40 pm"
        pattern1 = r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)'
        match = re.search(pattern1, page_text, re.IGNORECASE)
        if match:
            date_str = match.group(1)
            dt = parse_punch_date(date_str)
            if dt:
                LOG.info(f"[extract_punch_date] ✅ Found via text pattern: {dt}")
                return dt
    except Exception:
        pass
    
    LOG.warning(f"[extract_punch_date] ❌ All strategies failed for {url[:60]}")
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
    
    # Make cutoff timezone-aware
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    
    # If date_obj is naive, make it UTC-aware
    if date_obj.tzinfo is None:
        date_obj = date_obj.replace(tzinfo=timezone.utc)
    
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

def analyze_fuel_html(html: str, url: str = "https://app.fuelpricewatch.com/") -> Dict[str, Any]:
    """
    Analyze HTML and log key findings vs expectations.
    Returns both analysis results AND parsing result.
    """
    
    LOG.info("="*80)
    LOG.info("🔍 FUEL PAGE HTML ANALYSIS")
    LOG.info("="*80)
    
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text()
    
    analysis = {
        "html_length": len(html),
        "text_length": len(page_text),
        "expectations_met": [],
        "expectations_failed": [],
        "found_elements": {},
        "recommendations": []
    }
    
    # ============================================================
    # 1. BASIC PAGE INFO
    # ============================================================
    LOG.info(f"📄 HTML Length: {len(html):,} bytes")
    LOG.info(f"📝 Text Length: {len(page_text):,} chars")
    
    title = soup.title.string if soup.title else "No title"
    LOG.info(f"📋 Page Title: {title}")
    
    # ============================================================
    # 2. EXPECTATION: Card elements should exist
    # ============================================================
    LOG.info("\n" + "─"*80)
    LOG.info("✅ EXPECTATION #1: Card Elements (div.rounded-lg.border.bg-card)")
    LOG.info("─"*80)
    
    expected_selector = 'div.rounded-lg.border.bg-card'
    cards = soup.select(expected_selector)
    
    if cards:
        LOG.info(f"✅ FOUND: {len(cards)} cards with exact selector")
        analysis["expectations_met"].append("exact_card_selector")
        analysis["found_elements"]["exact_cards"] = len(cards)
        
        # Show first card's text
        first_card_text = cards[0].get_text(strip=True)[:150]
        LOG.info(f"   First card text: {first_card_text}...")
    else:
        LOG.warning(f"❌ NOT FOUND: No elements match '{expected_selector}'")
        analysis["expectations_failed"].append("exact_card_selector")
        
        # Try variations
        LOG.info("   Trying variations...")
        variations = [
            ('div.rounded-lg', 'rounded cards'),
            ('div.border', 'bordered divs'),
            ('div.bg-card', 'bg-card divs'),
            ('div[class*="rounded"]', 'divs with "rounded" in class'),
        ]
        
        for var_selector, var_desc in variations:
            var_elements = soup.select(var_selector)
            if var_elements:
                LOG.info(f"   ✓ Found {len(var_elements)} {var_desc} ({var_selector})")
                analysis["found_elements"][var_desc] = len(var_elements)
                
                # Check first one for petrol text
                if 'petrol' in var_elements[0].get_text().lower():
                    LOG.info(f"      → First one contains 'petrol'!")
                    analysis["recommendations"].append(f"Use selector: {var_selector}")
            else:
                LOG.info(f"   ✗ No {var_desc}")
    
    # ============================================================
    # 3. EXPECTATION: "Average Petrol Price" text should exist
    # ============================================================
    LOG.info("\n" + "─"*80)
    LOG.info("✅ EXPECTATION #2: Text 'Average Petrol Price' exists")
    LOG.info("─"*80)
    
    key_phrases = [
        "Average Petrol Price",
        "average petrol price",
        "AVERAGE PETROL PRICE",
        "Petrol Price",
        "PMS Price",
    ]
    
    found_phrases = []
    for phrase in key_phrases:
        if phrase in page_text:
            count = page_text.count(phrase)
            LOG.info(f"✅ FOUND: '{phrase}' ({count}x)")
            found_phrases.append(phrase)
            
            # Show context
            idx = page_text.find(phrase)
            context = page_text[max(0, idx-30):min(len(page_text), idx+80)]
            LOG.info(f"   Context: ...{context}...")
        else:
            LOG.debug(f"   ✗ Not found: '{phrase}'")
    
    if found_phrases:
        analysis["expectations_met"].append("petrol_text")
        analysis["found_elements"]["petrol_phrases"] = found_phrases
    else:
        LOG.warning("❌ NOT FOUND: None of the expected petrol phrases found")
        analysis["expectations_failed"].append("petrol_text")
        
        # Check for variations
        if re.search(r'petrol', page_text, re.I):
            LOG.info("   ⚠️ Found 'petrol' (case-insensitive) but not exact phrases")
    
    # ============================================================
    # 4. EXPECTATION: Price in format ₦XXX.XX should exist
    # ============================================================
    LOG.info("\n" + "─"*80)
    LOG.info("✅ EXPECTATION #3: Price pattern ₦XXX.XX (range 600-1500)")
    LOG.info("─"*80)
    
    price_pattern = r'₦\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)'
    price_matches = list(re.finditer(price_pattern, page_text))
    
    if price_matches:
        LOG.info(f"✅ FOUND: {len(price_matches)} price patterns")
        
        # Categorize by range
        valid_prices = []
        invalid_prices = []
        
        for match in price_matches[:20]:  # Check first 20
            price_str = match.group(1).replace(',', '')
            try:
                price_val = float(price_str)
                if 600 <= price_val <= 1500:
                    valid_prices.append(price_val)
                else:
                    invalid_prices.append(price_val)
            except:
                pass
        
        if valid_prices:
            LOG.info(f"   ✅ {len(valid_prices)} prices in valid range (600-1500):")
            for i, price in enumerate(valid_prices[:5], 1):
                LOG.info(f"      {i}. ₦{price:,.2f}")
            if len(valid_prices) > 5:
                LOG.info(f"      ... and {len(valid_prices) - 5} more")
            
            analysis["expectations_met"].append("valid_price")
            analysis["found_elements"]["valid_prices"] = valid_prices
        else:
            LOG.warning(f"   ⚠️ No prices in valid range (found {len(invalid_prices)} invalid)")
            if invalid_prices:
                LOG.info(f"   Invalid prices: {invalid_prices[:5]}")
    else:
        LOG.warning("❌ NOT FOUND: No ₦ price patterns found")
        analysis["expectations_failed"].append("price_pattern")
    
    # ============================================================
    # 5. EXPECTATION: Change indicators (% and ₦)
    # ============================================================
    LOG.info("\n" + "─"*80)
    LOG.info("✅ EXPECTATION #4: Change indicators (%, ₦ today)")
    LOG.info("─"*80)
    
    # Percentage change
    perc_patterns = [
        r'([+-]?\s*[0-9]+\.?[0-9]*\s*%)\s*from\s+last\s+period',
        r'([+-]?\s*[0-9]+\.?[0-9]*\s*%)',
    ]
    
    perc_found = False
    for pattern in perc_patterns:
        matches = re.findall(pattern, page_text, re.I)
        if matches:
            LOG.info(f"✅ FOUND: Percentage pattern - {matches[:3]}")
            perc_found = True
            analysis["found_elements"]["percent_changes"] = matches
            break
    
    if not perc_found:
        LOG.warning("❌ NOT FOUND: No percentage change pattern")
    
    # Absolute change
    abs_patterns = [
        r'([+-]?\s*₦\s*[0-9]+\.?[0-9]*)\s*today',
        r'today[^\d₦]*([+-]?\s*₦\s*[0-9]+\.?[0-9]*)',
    ]
    
    abs_found = False
    for pattern in abs_patterns:
        matches = re.findall(pattern, page_text, re.I)
        if matches:
            LOG.info(f"✅ FOUND: Absolute change pattern - {matches[:3]}")
            abs_found = True
            analysis["found_elements"]["absolute_changes"] = matches
            break
    
    if not abs_found:
        LOG.warning("❌ NOT FOUND: No '₦X today' pattern")
    
    if perc_found and abs_found:
        analysis["expectations_met"].append("change_indicators")
    else:
        analysis["expectations_failed"].append("change_indicators")
    
    # ============================================================
    # 6. ACTUAL CSS CLASSES FOUND
    # ============================================================
    LOG.info("\n" + "─"*80)
    LOG.info("📊 ACTUAL CSS CLASSES IN PAGE (Top 15)")
    LOG.info("─"*80)
    
    all_classes = []
    for elem in soup.find_all(True)[:500]:  # First 500 elements
        classes = elem.get('class', [])
        all_classes.extend(classes)
    
    class_counter = Counter(all_classes)
    top_classes = class_counter.most_common(15)
    
    for cls, count in top_classes:
        LOG.info(f"   {cls}: {count}x")
        
    analysis["found_elements"]["top_css_classes"] = [cls for cls, _ in top_classes]
    
    # ============================================================
    # 7. ELEMENTS WITH PRICE-RELATED CLASSES
    # ============================================================
    LOG.info("\n" + "─"*80)
    LOG.info("💰 ELEMENTS WITH PRICE-RELATED CLASSES")
    LOG.info("─"*80)
    
    price_class_patterns = ['price', 'Price', 'prc', 'amount', 'cost']
    
    for pattern in price_class_patterns:
        selector = f'[class*="{pattern}"]'
        elements = soup.select(selector)
        
        if elements:
            LOG.info(f"✅ Found {len(elements)} elements with '{pattern}' in class")
            # Show first one
            first_text = elements[0].get_text(strip=True)[:100]
            first_classes = elements[0].get('class', [])
            LOG.info(f"   First: {first_text}...")
            LOG.info(f"   Classes: {first_classes}")
            
            analysis["found_elements"][f"class_{pattern}"] = len(elements)
        else:
            LOG.debug(f"   ✗ No elements with '{pattern}' in class")
    
    # ============================================================
    # 8. STRUCTURAL ANALYSIS
    # ============================================================
    LOG.info("\n" + "─"*80)
    LOG.info("🏗️  PAGE STRUCTURE")
    LOG.info("─"*80)
    
    total_divs = len(soup.find_all('div'))
    total_sections = len(soup.find_all('section'))
    total_articles = len(soup.find_all('article'))
    
    LOG.info(f"   <div>: {total_divs}")
    LOG.info(f"   <section>: {total_sections}")
    LOG.info(f"   <article>: {total_articles}")
    
    # Check for common layout patterns
    has_main = bool(soup.find('main'))
    has_header = bool(soup.find('header'))
    has_nav = bool(soup.find('nav'))
    
    LOG.info(f"   <main>: {'✓' if has_main else '✗'}")
    LOG.info(f"   <header>: {'✓' if has_header else '✗'}")
    LOG.info(f"   <nav>: {'✓' if has_nav else '✗'}")
    
    # ============================================================
    # 9. FINAL RECOMMENDATIONS
    # ============================================================
    LOG.info("\n" + "="*80)
    LOG.info("💡 RECOMMENDATIONS")
    LOG.info("="*80)
    
    if not analysis["expectations_failed"]:
        LOG.info("✅ All expectations met! Original selectors should work.")
    else:
        LOG.warning(f"⚠️ {len(analysis['expectations_failed'])} expectations failed:")
        for failed in analysis["expectations_failed"]:
            LOG.warning(f"   - {failed}")
        
        LOG.info("\n📝 Recommended fixes:")
        
        # Recommendation 1: Use what we found
        if "valid_prices" in analysis["found_elements"]:
            LOG.info("   1. ✅ Valid prices exist - use text-based extraction fallback")
        
        # Recommendation 2: Selector alternatives
        if analysis["recommendations"]:
            LOG.info("   2. Try these alternative selectors:")
            for rec in analysis["recommendations"]:
                LOG.info(f"      - {rec}")
        
        # Recommendation 3: Lenient parser
        LOG.info("   3. Consider using the lenient parser (4 fallback strategies)")
    
    # ============================================================
    # 10. SUMMARY
    # ============================================================
    LOG.info("\n" + "="*80)
    LOG.info("📊 ANALYSIS SUMMARY")
    LOG.info("="*80)
    LOG.info(f"✅ Expectations Met: {len(analysis['expectations_met'])}")
    LOG.info(f"❌ Expectations Failed: {len(analysis['expectations_failed'])}")
    LOG.info(f"💡 Recommendations: {len(analysis['recommendations'])}")
    LOG.info("="*80 + "\n")
    
    return analysis

@dataclass
class FoundElement:
    """Structured logging for found elements"""
    tag: str
    classes: str
    element_id: str
    text_preview: str
    price_found: Optional[str] = None
    change_found: Optional[str] = None
    
    def __str__(self) -> str:
        parts = [
            f"[{self.tag}]",
            f"Classes: {self.classes or 'none'}",
            f"ID: {self.element_id or 'none'}",
            f"Text: '{self.text_preview[:100]}...'"
        ]
        if self.price_found:
            parts.append(f"💰 Price: {self.price_found}")
        if self.change_found:
            parts.append(f"📈 Change: {self.change_found}")
        return " | ".join(parts)


def _parse_fuelpricewatch(html: str, url: str = "https://app.fuelpricewatch.com/") -> Dict[str, Any]:
    """
    Parse Fuel Price Watch - handles modern concatenated card structure.
    
    Current structure (2026-02-11):
    - Cards: div.rounded-lg.border.bg-card with class "gumroad-card"
    - Concatenated text: "Average Petrol Price₦873.88+0.5% from last period+₦5.00 today"
    - No spaces between concatenated values
    """
    soup = BeautifulSoup(html, "lxml")
    LOG.debug("[FuelPriceWatch] Starting parse with %d chars of HTML", len(html))
    
    found_elements: List[FoundElement] = []
    
    def log_element(element: Tag, context: str = "") -> Optional[FoundElement]:
        """Create structured log entry for an element"""
        if not isinstance(element, Tag):
            return None
            
        text = element.get_text(" ", strip=True)
        classes = " ".join(element.get("class", []))
        elem_id = element.get("id", "")
        
        # Extract any price/change info for better logging
        price_match = re.search(r'₦\s*(\d{3,4}(?:\.\d+)?)', text)
        change_match = re.search(r'([+\-]\s*₦?\s*\d+\.?\d*)\s*today', text, re.IGNORECASE)
        
        entry = FoundElement(
            tag=element.name,
            classes=classes,
            element_id=elem_id,
            text_preview=text[:150],
            price_found=price_match.group(0) if price_match else None,
            change_found=change_match.group(0) if change_match else None
        )
        found_elements.append(entry)
        
        LOG.debug("[FuelPriceWatch] %s %s", context, entry)
        return entry

    # ═══════════════════════════════════════════════════════════════════════
    # STRATEGY 1: Find petrol card using updated class selectors
    # ═══════════════════════════════════════════════════════════════════════
    petrol_card = None
    card_text = ""
    
    # Updated selectors based on current page structure
    card_selectors = [
        ('div.gumroad-card', 'gumroad-card (exact)'),           # Your specific target
        ('div.rounded-lg.border.bg-card', 'rounded border card'),  # App page structure
        ('div[class*="bg-card"]', 'bg-card contains'),          # Fallback
        ('div[class*="card"]', 'generic card class'),           # Generic
    ]
    
    LOG.info("[FuelPriceWatch] Scanning for petrol cards using %d selectors...", len(card_selectors))
    
    for selector, desc in card_selectors:
        try:
            cards = soup.select(selector)
            LOG.debug("[FuelPriceWatch] Selector '%s' (%s): %d elements found", selector, desc, len(cards))
            
            for idx, card in enumerate(cards):
                log_element(card, f"CANDIDATE[{idx}]")
                text = card.get_text(" ", strip=True)
                
                # Check for petrol indicator + price in valid range
                if 'petrol' in text.lower() and '₦' in text:
                    price_check = re.search(r'₦\s*(\d{3,4}(?:\.\d+)?)', text)
                    if price_check:
                        price_val = float(price_check.group(1))
                        if 600 <= price_val <= 1500:
                            petrol_card = card
                            card_text = text
                            LOG.info(
                                "[FuelPriceWatch] ✓ MATCH on selector '%s' (card #%d): ₦%.2f",
                                selector, idx, price_val
                            )
                            # Log all found elements in this card for debugging
                            for child in card.find_all(['div', 'p', 'span', 'h3']):
                                log_element(child, "  CHILD")
                            break
                        
            if petrol_card:
                break
                
        except Exception as e:
            LOG.warning("[FuelPriceWatch] Selector '%s' failed: %s", selector, e)
            continue
    
    # ═══════════════════════════════════════════════════════════════════════
    # STRATEGY 2: Fallback - regex extraction from full page
    # ═══════════════════════════════════════════════════════════════════════
    if not petrol_card:
        LOG.warning("[FuelPriceWatch] No petrol card found via selectors, using page-wide regex")
        page_text = soup.get_text(" ", strip=True)
        
        # Look for concatenated pattern: "Average Petrol Price₦XXX.XX+/-X.X%..."
        # Handle no spaces between elements
        patterns = [
            # Pattern 1: Full concatenated string
            r'Average\s+Petrol\s+Price\s*(₦\s*\d{3,4}(?:\.\d+)?)\s*([+\-]\d+\.?\d*)\s*%?\s*from\s+last\s+period\s*([+\-]\s*₦?\s*\d+\.?\d*)\s*today',
            # Pattern 2: Just price and change near "petrol"
            r'Petrol.*?Price\s*(₦\s*\d{3,4}(?:\.\d+)?)',
            # Pattern 3: Any ₦XXX near "Average" and "Petrol"
            r'Average.*?Petrol.*?(₦\s*\d{3,4}(?:\.\d+)?)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, page_text, re.IGNORECASE | re.DOTALL)
            if match:
                card_text = match.group(0)
                LOG.info("[FuelPriceWatch] ✓ Regex match: %s", card_text[:100])
                break
    
    if not card_text:
        LOG.error("[FuelPriceWatch] ❌ Failed to locate petrol price data")
        return {
            "error": "no_petrol_data_found",
            "elements_scanned": len(found_elements),
            "selectors_tried": [s[0] for s in card_selectors],
            "raw_sample": soup.get_text(" ", strip=True)[:300]
        }

    # ═══════════════════════════════════════════════════════════════════════
    # EXTRACTION: Parse concatenated or structured text
    # ═══════════════════════════════════════════════════════════════════════
    LOG.debug("[FuelPriceWatch] Parsing extracted text: %s", card_text[:200])
    
    # Normalize: remove spaces between currency and numbers, handle concatenation
    # "₦ 873.88" -> "₦873.88", "₦873.88+0.5%" -> " ₦873.88 +0.5% "
    normalized = card_text
    normalized = re.sub(r'₦\s+', '₦', normalized)  # Remove space after ₦
    normalized = re.sub(r'(\d)([+\-])', r'\1 \2', normalized)  # Add space before +/-
    normalized = re.sub(r'([+\-])(\d)', r'\1 \2', normalized)  # Add space after +/-
    normalized = re.sub(r'([a-zA-Z])(₦)', r'\1 \2', normalized)  # Space before ₦ if attached to word
    normalized = re.sub(r'(%)([+\-])', r'\1 \2', normalized)  # Space between % and +/-
    normalized = re.sub(r'\s+', ' ', normalized)  # Collapse multiple spaces
    
    LOG.debug("[FuelPriceWatch] Normalized: %s", normalized[:200])

    # Extract price
    price_raw = None
    price_match = re.search(r'₦\s*(\d{3,4}(?:\.\d+)?)', normalized)
    if price_match:
        try:
            price_raw = float(price_match.group(1))
            LOG.debug("[FuelPriceWatch] ✓ Price: ₦%.2f", price_raw)
        except ValueError:
            pass
    
    if not price_raw or not (600 <= price_raw <= 1500):
        LOG.error("[FuelPriceWatch] ❌ Invalid price extracted: %s", price_raw)
        return {
            "error": "invalid_price",
            "extracted_price": price_raw,
            "normalized_text": normalized[:200],
            "elements_found": [str(e) for e in found_elements[:5]]
        }

    # Extract percentage change
    change_percent = "N/A"
    percent_patterns = [
        r'([+\-]\s*\d+\.?\d*)\s*%?\s*from\s+last\s+period',  # +0.5% from last period
        r'from\s+last\s+period\s*([+\-]\s*\d+\.?\d*)',  # from last period +0.5
        r'([+\-]\d+\.?\d*)\s*%',  # generic percent with sign
    ]
    
    for pattern in percent_patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            val = match.group(1).replace(' ', '')
            change_percent = f"{val}%"
            LOG.debug("[FuelPriceWatch] ✓ Percent change: %s", change_percent)
            break
    
    # Extract absolute change
    change_absolute = "N/A"
    abs_patterns = [
        r'([+\-]\s*₦?\s*\d+\.?\d*)\s*today',  # +₦5.00 today or +5.00 today
        r'today\s*([+\-]\s*₦?\s*\d+\.?\d*)',  # today +₦5.00
        r'period\s*([+\-]\s*₦?\s*\d+\.?\d*)\s*today',  # period +5 today
    ]
    
    for pattern in abs_patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            val = match.group(1).strip()
            # Normalize to +₦X.XX format
            val = re.sub(r'\s+', '', val)
            has_plus = '+' in val
            has_minus = '-' in val
            sign = '-' if has_minus else '+'
            
            # Extract number
            num_match = re.search(r'[\d,]+\.?\d*', val)
            if num_match:
                num = num_match.group(0).replace(',', '')
                try:
                    num_float = float(num)
                    change_absolute = f"{sign}₦{num_float:,.2f}"
                    LOG.debug("[FuelPriceWatch] ✓ Absolute change: %s", change_absolute)
                    break
                except ValueError:
                    continue

    # Build detailed elements log
    elements_summary = []
    for i, elem in enumerate(found_elements[:10], 1):  # Top 10 elements
        elements_summary.append(f"{i}. {elem}")

    LOG.info(
        "[FuelPriceWatch] ✅ SUCCESS | Price: ₦%.2f | Change: %s | Today: %s | Elements logged: %d",
        price_raw, change_percent, change_absolute, len(found_elements)
    )

    return {
        "source": url,
        "price_raw": price_raw,
        "price_str": f"₦{price_raw:,.2f}",
        "change_percent": change_percent,
        "change_absolute": change_absolute,
        "last_updated": "Live data",
        "parsed_from": card_text[:200],
        "elements_found_count": len(found_elements),
        "elements_details": elements_summary,
        "selectors_used": [s[0] for s in card_selectors],
    }

@retry(max_attempts=3, backoff=1.5)
async def _fetch_lpg_html() -> str:
    url = "https://lpginnigeria.com/chart"
    return await _fetch_html(url)

async def scrape_fuel_prices() -> Dict[str, Any]:
    """
    Scrape current fuel prices from Fuel Price Watch.
    
    Uses Playwright to fetch dynamic content from app.fuelpricewatch.com,
    then parses the structured data.
    
    Returns:
        Dict containing:
        - avg_petrol: Formatted price string
        - avg_raw: Raw price value (float)
        - change_percent: Percentage change from last period
        - change_absolute: Absolute change today
        - last_updated: Update timestamp
        - sources: List of parsed source data
        - debug: Debug information
    """
    app_url = "https://app.fuelpricewatch.com/"
    
    LOG.info("[FuelPrices] 🚀 Starting fuel price scrape...")
    
    # Try primary method: Live app with Playwright
    try:
        LOG.info("[FuelPrices] Method 1: Fetching live app with Playwright...")
        
        html = await fetch_with_playwright_aggressive(
            app_url,
            retries=3,
            return_visible_text=False
        )
        
        LOG.info(f"[FuelPrices] ✓ Playwright fetch success: {len(html)} bytes")
        
        result = _parse_fuelpricewatch(html, url=app_url)
        analysis = analyze_fuel_html(html, url=app_url)
        
        if result.get("price_raw") is not None:
            LOG.info("[FuelPrices] ✅ Method 1 SUCCESS - Live app data extracted")
            return {
                "avg_petrol": result["price_str"],
                "avg_raw": result["price_raw"],
                "change_percent": result.get("change_percent", "N/A"),
                "change_absolute": result.get("change_absolute", "N/A"),
                "last_updated": result.get("last_updated", "Live data"),
                "sources": [result],
                "debug": {"method": "live_app_playwright", "url": app_url}
            }
        else:
            LOG.warning(f"[FuelPrices] ⚠️ Method 1 parsing failed: {result.get('error')}")
            
    except Exception as e:
        LOG.warning(f"[FuelPrices] ⚠️ Method 1 failed: {type(e).__name__}: {str(e)[:100]}")
        import traceback
        LOG.debug(f"[FuelPrices] Method 1 traceback:\n{traceback.format_exc()}")
    
    # Fallback Method 2: Static index page
    index_url = "https://www.fuelpricewatch.com/fuel-price-index-nigeria"
    
    try:
        LOG.info("[FuelPrices] Method 2: Fetching static index page...")
        
        index_html = await _fetch_html(index_url)
        LOG.info(f"[FuelPrices] ✓ Index fetch success: {len(index_html)} bytes")
        
        index_result = _parse_fuelpricewatch(index_html, url=app_url)
        
        if index_result.get("price_raw") is not None:
            LOG.info("[FuelPrices] ✅ Method 2 SUCCESS - Index data extracted")
            return {
                "avg_petrol": index_result["price_str"],
                "avg_raw": index_result["price_raw"],
                "change_percent": index_result.get("change_percent", "N/A"),
                "change_absolute": index_result.get("change_absolute", "N/A"),
                "last_updated": "Index snapshot (may be outdated)",
                "sources": [index_result],
                "debug": {"method": "static_index_fallback", "url": index_url}
            }
        else:
            LOG.warning(f"[FuelPrices] ⚠️ Method 2 parsing failed: {index_result.get('error')}")
            
    except Exception as e:
        LOG.warning(f"[FuelPrices] ⚠️ Method 2 failed: {type(e).__name__}: {str(e)[:100]}")
        import traceback
        LOG.debug(f"[FuelPrices] Method 2 traceback:\n{traceback.format_exc()}")
    
    # All methods failed
    LOG.error("[FuelPrices] ❌ ALL METHODS FAILED - No fuel price data available")
    
    return {
        "avg_petrol": "N/A",
        "change_percent": "N/A",
        "change_absolute": "N/A",
        "last_updated": "N/A",
        "avg_raw": None,
        "error": "all_methods_failed",
        "sources": [],
        "debug": {"method": "failed", "attempted": ["live_app_playwright", "static_index"]}
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
# SITE-SPECIFIC ARTICLE LISTING PAGES (WITH ENHANCED LOGGING)
# ═══════════════════════════════════════════════════════════════════════════
async def get_myschool_recent_articles(base_url: str = "https://myschool.ng/news") -> List[str]:
    """Get recent article URLs from MySchool - WITH COOLDOWN"""
    LOG.info(f"[MySchool Listing] 🔍 Starting extraction from {base_url}")
    
    # Add cooldown between MySchool requests
    LAST_MYSCHOOL_REQUEST = getattr(get_myschool_recent_articles, "_last_request", 0)
    current_time = time.time()
    
    if current_time - LAST_MYSCHOOL_REQUEST < 5:  # 5 second cooldown
        wait_time = 5 - (current_time - LAST_MYSCHOOL_REQUEST)
        LOG.info(f"[MySchool Listing] ⏳ Cooling down for {wait_time:.1f}s...")
        await asyncio.sleep(wait_time)
    
    get_myschool_recent_articles._last_request = time.time()
    
    root = base_url.rstrip("/").rsplit("/", 1)[0] if "/" in base_url else base_url
    urls_to_try = [
        base_url.rstrip("/"),
    ]
    
    LOG.info(f"[MySchool Listing] 📋 Will try {len(urls_to_try)} listing URLs")
    
    for idx, listing_url in enumerate(urls_to_try, 1):
        LOG.info(f"[MySchool Listing] 🌐 [{idx}/{len(urls_to_try)}] Fetching: {listing_url}")
        
        try:
            html = await shared_playwright.fetch_html(
                listing_url,
                wait_for_selector='a[href*="/news/"]',
                scroll_to_load=True,
                timeout=100000,
                partial_on_timeout=True,
            )
            
            LOG.info(f"[MySchool Listing] 📄 HTML length: {len(html) if html else 0} bytes")
            
            if not html:
                LOG.warning(f"[MySchool Listing] ⚠️ No HTML returned for {listing_url}")
                continue

            soup = BeautifulSoup(html, 'lxml')
            seen: Set[str] = set()
            ordered_urls: List[str] = []
            
            LOG.info(f"[MySchool Listing] 🔎 Scanning DOM for article links in document order...")
            
            # Find all anchor tags in document order
            for a in soup.find_all('a', href=True):
                href = a.get('href', '').strip()
                
                # Must contain /news/ to be an article
                if '/news/' not in href:
                    continue
                
                # Skip non-article links
                if any(bad in href for bad in [
                    '/news/category/', '/news/tag/', '/news/author/',
                    '/category/', '/tag/', '/author/',
                    'page=', '#', '?feed=', '?share='
                ]):
                    LOG.debug(f"[MySchool Listing]   ✗ Filtered out (non-article): {href[:60]}")
                    continue
                
                # Skip pagination, archives
                if any(kw in href for kw in [
                    'page-', '/page/', '?page=', 'archive', 'year=', 'month='
                ]):
                    LOG.debug(f"[MySchool Listing]   ✗ Filtered out (pagination/archive): {href[:60]}")
                    continue
                
                # Skip comment links
                if href.startswith('#') or href.startswith('javascript:'):
                    LOG.debug(f"[MySchool Listing]   ✗ Filtered out (JS/anchor): {href[:60]}")
                    continue
                
                # Construct full URL
                full_url = urljoin("https://myschool.ng", href)
                
                # Ensure it's a proper news article URL pattern
                if not re.match(r'^https://myschool\.ng/news/[^/]+/?$', full_url):
                    LOG.debug(f"[MySchool Listing]   ✗ Filtered out (bad pattern): {full_url[:60]}")
                    continue
                
                # Deduplicate
                if full_url in seen:
                    continue
                
                seen.add(full_url)
                ordered_urls.append(full_url)
                LOG.debug(f"[MySchool Listing]   ✓ Added ({len(ordered_urls)}): {full_url[:60]}")
                
                if len(ordered_urls) >= 10:  # LIMIT TO 25
                    break
            
            if ordered_urls:
                LOG.info(f"[MySchool Listing] ✅ Extracted {len(ordered_urls)} article URLs from {listing_url}")
                LOG.info(f"[MySchool Listing] 📌 Sample URLs (in page order):")
                for sample_idx, sample_url in enumerate(ordered_urls[:3], 1):
                    LOG.info(f"[MySchool Listing]    {sample_idx}. {sample_url}")
                return ordered_urls
            else:
                LOG.warning(f"[MySchool Listing] ⚠️ No article URLs found in {listing_url}")
                
        except Exception as e:
            LOG.exception(f"[MySchool Listing] ❌ Exception while processing {listing_url}: {e}")
            continue

    LOG.error("[MySchool Listing] ❌ All listing URLs failed - returning empty list")
    return []

async def get_punch_recent_articles(base_url: str = "https://punchng.com") -> List[str]:
    """
    Get recent Punch education articles - FIXED FOR COMPRESSED RESPONSES
    """
    LOG.info(f"[Punch Listing] 🔍 Starting extraction from {base_url}")
    
    listing_url = f"{base_url}/topics/education/"
    LOG.info(f"[Punch Listing] 🌐 Fetching: {listing_url}")
    
    try:
        listing_html = await shared_playwright.smart_fetch(
            listing_url,
            prefer_http=True,
            allow_playwright=True,
        )
        
        LOG.info(f"[Punch Listing] 📄 HTML length: {len(listing_html) if listing_html else 0} bytes")
        
        if not listing_html:
            LOG.error("[Punch Listing] ❌ No HTML returned from listing page")
            return []
        
        # ✅ FIX: Check if HTML is compressed/binary and handle it
        if isinstance(listing_html, bytes):
            LOG.info("[Punch Listing] ⚠️ Received bytes, attempting decompression...")
            try:
                # Try to decode as UTF-8 first
                listing_html = listing_html.decode('utf-8')
                LOG.info("[Punch Listing] ✅ Successfully decoded bytes to UTF-8")
            except UnicodeDecodeError:
                # If that fails, try decompressing
                import gzip
                import brotli
                try:
                    # Try Brotli first (common for modern sites)
                    listing_html = brotli.decompress(listing_html).decode('utf-8')
                    LOG.info("[Punch Listing] ✅ Decompressed with Brotli")
                except Exception:
                    try:
                        # Try gzip
                        listing_html = gzip.decompress(listing_html).decode('utf-8')
                        LOG.info("[Punch Listing] ✅ Decompressed with gzip")
                    except Exception as decomp_err:
                        LOG.error(f"[Punch Listing] ❌ Failed to decompress: {decomp_err}")
                        return []
        
        # ✅ Additional check: if it still looks like binary, force Playwright
        if not listing_html.strip().startswith('<') and '<html' not in listing_html[:1000]:
            LOG.warning("[Punch Listing] ⚠️ HTML doesn't start with <, likely still compressed. Forcing Playwright...")
            listing_html = await shared_playwright.smart_fetch(
                listing_url,
                prefer_http=False,  # Force Playwright
                allow_playwright=True,
            )
            if isinstance(listing_html, bytes):
                listing_html = listing_html.decode('utf-8', errors='ignore')
        
        # ✅ Verify we have valid HTML
        if '<html' not in listing_html and '<!DOCTYPE' not in listing_html:
            LOG.error(f"[Punch Listing] ❌ Invalid HTML content. First 200 chars: {listing_html[:200]}")
            return []
        
        soup = BeautifulSoup(listing_html, 'lxml')
        
        # ✅ DEBUG: Log HTML structure first
        LOG.info("=" * 80)
        LOG.info("🔍 DEBUGGING HTML STRUCTURE")
        LOG.info("=" * 80)
        
        # Log first 1000 chars of decoded HTML
        LOG.info("[DEBUG] First 1000 characters of HTML:")
        LOG.info(listing_html[:1000])
        
        # 1. Log all links found in the page
        LOG.info("=" * 80)
        LOG.info("[DEBUG] ALL LINKS FOUND IN PAGE:")
        all_links = soup.find_all('a', href=True)
        LOG.info(f"Total links found: {len(all_links)}")
        
        article_candidates = []
        for idx, link in enumerate(all_links[:50], 1):  # First 50 links
            href = link.get('href', '')
            text = link.get_text(strip=True)[:80]
            parent = link.parent.name if link.parent else 'No parent'
            parent_classes = link.parent.get('class', []) if link.parent and link.parent.get('class') else []
            
            # Check if this looks like an article link
            is_article_candidate = (
                'punchng.com' in href and 
                '/topics/' not in href and 
                '/category/' not in href and
                '/tag/' not in href and
                '/author/' not in href and
                'advertise' not in href.lower() and
                len(text) > 20  # Article titles are usually longer
            )
            
            status = "✅ ARTICLE CANDIDATE" if is_article_candidate else "❌ NOT ARTICLE"
            
            LOG.info(f"[DEBUG Link {idx}] {status}")
            LOG.info(f"    Text: {text}")
            LOG.info(f"    URL: {href}")
            LOG.info(f"    Parent: {parent}, Classes: {parent_classes}")
            
            if is_article_candidate:
                article_candidates.append(link)
        
        # 2. Log all heading elements (h1-h6)
        LOG.info("=" * 80)
        LOG.info("[DEBUG] ALL HEADING ELEMENTS:")
        for i in range(1, 7):
            headings = soup.find_all(f'h{i}')
            LOG.info(f"h{i} tags found: {len(headings)}")
            for idx, h in enumerate(headings[:10], 1):  # First 10 of each
                text = h.get_text(strip=True)[:100]
                LOG.info(f"  h{i}-{idx}: {text}")
                # Check for links inside headings
                links_in_h = h.find_all('a', href=True)
                for link in links_in_h:
                    LOG.info(f"    → Link: {link.get('href')}")
        
        # 3. Log all divs with classes that might contain articles
        LOG.info("=" * 80)
        LOG.info("[DEBUG] DIV ELEMENTS WITH COMMON ARTICLE CLASSES:")
        article_class_patterns = ['article', 'card', 'post', 'news', 'story', 'content', 'entry', 'item']
        for pattern in article_class_patterns:
            divs = soup.find_all('div', class_=lambda x: x and pattern in str(x).lower())
            LOG.info(f"Divs with '{pattern}' in class: {len(divs)}")
            
            for idx, div in enumerate(divs[:5], 1):  # First 5 of each pattern
                # Get first 200 chars of text content
                text = div.get_text(strip=True, separator=' ')[:200]
                links = div.find_all('a', href=True)
                LOG.info(f"  {pattern}-{idx}: Text preview: {text}")
                LOG.info(f"    Links inside: {len(links)}")
                for link in links[:3]:  # First 3 links
                    LOG.info(f"    → {link.get('href')[:80]}")
        
        # 4. Log all elements with article-like structure (based on analysis)
        LOG.info("=" * 80)
        LOG.info("[DEBUG] ELEMENTS WITH SPECIFIC CLASSES FROM ANALYSIS:")
        
        # From your analysis, these are common classes
        target_classes = [
            'bg-white', 'rounded-xl', 'border', 'border-gray-200', 
            'shadow-sm', 'flex', 'items-stretch', 'text-xl', 'font-bold'
        ]
        
        for class_name in target_classes:
            elements = soup.find_all(class_=class_name)
            LOG.info(f"Elements with class '{class_name}': {len(elements)}")
            
            for idx, elem in enumerate(elements[:3], 1):  # First 3
                # Get parent info
                parent = elem.parent
                parent_name = parent.name if parent else 'No parent'
                parent_class = parent.get('class', []) if parent and parent.get('class') else []
                
                # Get text content
                text = elem.get_text(strip=True, separator=' ')[:150]
                
                LOG.info(f"  {class_name}-{idx}:")
                LOG.info(f"    Text: {text}")
                LOG.info(f"    Parent: {parent_name}, Classes: {parent_class}")
                
                # Find links inside
                links = elem.find_all('a', href=True)
                for link_idx, link in enumerate(links[:2], 1):
                    link_text = link.get_text(strip=True)[:50]
                    link_href = link.get('href', '')
                    LOG.info(f"    Link {link_idx}: {link_text} → {link_href[:80]}")
        
        # 6. Try to find the main content container
        LOG.info("=" * 80)
        LOG.info("[DEBUG] LOOKING FOR MAIN CONTENT CONTAINERS:")
        
        # Look for common content container IDs/classes
        content_selectors = [
            '#main', '#content', '.main-content', '.content-area',
            '.posts', '.articles', '.news-list', '.archive'
        ]
        
        for selector in content_selectors:
            try:
                containers = soup.select(selector)
                LOG.info(f"Selector '{selector}': {len(containers)} found")
                if containers:
                    container = containers[0]
                    # Count articles inside
                    articles_in_container = container.find_all(['article', 'div'], class_=lambda x: x and any(
                        cls in str(x) for cls in ['post', 'article', 'news', 'item']
                    ))
                    LOG.info(f"  Articles/divs inside: {len(articles_in_container)}")
                    
                    # Show first few links
                    links = container.find_all('a', href=True)
                    article_links = [l for l in links if 'punchng.com' in l.get('href', '') and '/topics/' not in l.get('href', '')]
                    LOG.info(f"  Article links inside: {len(article_links)}")
                    for link in article_links[:3]:
                        LOG.info(f"    → {link.get_text(strip=True)[:50]} -> {link.get('href')[:80]}")
            except Exception as e:
                LOG.info(f"  Error with selector '{selector}': {e}")
        
        # Now try to extract articles using what we've learned
        LOG.info("=" * 80)
        LOG.info("[Punch Listing] 🚀 Starting article extraction...")
        
        articles_with_dates = []
        seen_urls = set()
        
        # Strategy: Collect from multiple sources
        extraction_methods = []
        
        # Method 1: Links in headings
        for h_tag in soup.find_all(['h2', 'h3', 'h4']):
            for link in h_tag.find_all('a', href=True):
                href = link.get('href', '')
                if href and 'punchng.com' in href:
                    extraction_methods.append({
                        'url': href,
                        'title': link.get_text(strip=True),
                        'source': f'heading-{h_tag.name}',
                        'element': link
                    })
        
        # Method 2: Links in article-like containers
        article_containers = soup.find_all(['article', 'div'], class_=lambda x: x and any(
            word in str(x).lower() for word in ['article', 'post', 'news', 'story', 'card', 'item']
        ))
        
        for container in article_containers:
            for link in container.find_all('a', href=True):
                href = link.get('href', '')
                if href and 'punchng.com' in href:
                    text = link.get_text(strip=True)
                    if text and len(text) > 20:  # Likely article title
                        extraction_methods.append({
                            'url': href,
                            'title': text,
                            'source': 'article-container',
                            'element': link
                        })
        
        # Method 3: Direct links with article-like patterns
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if href and 'punchng.com' in href and href.endswith('/'):
                text = link.get_text(strip=True)
                # Check if this looks like an article title (not navigation, not short)
                if text and len(text) > 30 and not any(nav in text.lower() for nav in ['home', 'about', 'contact', 'advertise', 'subscribe']):
                    extraction_methods.append({
                        'url': href,
                        'title': text,
                        'source': 'direct-link',
                        'element': link
                    })
        
        LOG.info(f"[Punch Listing] 📋 Found {len(extraction_methods)} potential articles from all methods")
        
        # Process candidates
        for idx, candidate in enumerate(extraction_methods, 1):
            href = candidate['url']
            
            # Apply filters
            if any(bad in href for bad in [
                '/topics/', '/category/', '/tag/', '/author/',
                'advertise', '#', '?feed=', '?share=', '/videos/',
                '/galleries/', '/podcasts/', '/contact-us',
                '/about-us', '/advertise', '/print-',
                'punchng.com/#', 'punchng.com/?'
            ]):
                LOG.debug(f"[Punch Listing]   Candidate [{idx}]: 🚫 Filtered: {href[:80]}...")
                continue
            
            # Ensure it's a full URL
            if not href.startswith('http'):
                full_url = urljoin(base_url, href)
            else:
                full_url = href
            
            # Remove query parameters
            full_url = full_url.split('?')[0]
            
            # Skip if not a punchng.com URL
            if 'punchng.com' not in full_url:
                LOG.debug(f"[Punch Listing]   Candidate [{idx}]: 🚫 Not punchng.com URL")
                continue
            
            # Deduplicate
            if full_url in seen_urls:
                LOG.debug(f"[Punch Listing]   Candidate [{idx}]: 🔄 Duplicate")
                continue
            seen_urls.add(full_url)
            
            # Extract date if possible
            date_str = ""
            date_obj = None
            
            # Look for date near the link
            element = candidate['element']
            for _ in range(5):  # Check element and parents
                if element:
                    # Look for date in sibling or child elements
                    date_elements = element.find_all(['time', 'span', 'div'], class_=lambda x: x and any(
                        word in str(x).lower() for word in ['date', 'time', 'ago', 'published']
                    ))
                    
                    for date_elem in date_elements:
                        text = date_elem.get_text(strip=True)
                        if text and ('ago' in text.lower() or any(
                            month in text for month in ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 
                                                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
                        )):
                            date_str = text
                            date_obj = parse_punch_date(date_str)
                            break
                
                if date_str:
                    break
                element = element.parent if element else None
            
            title = candidate['title']
            LOG.info(f"[Punch Listing]   ✅ Article [{idx}]: {title[:60]}...")
            LOG.info(f"[Punch Listing]      Source: {candidate['source']}")
            LOG.info(f"[Punch Listing]      Date: {date_str or 'Not found'}")
            LOG.info(f"[Punch Listing]      URL: {full_url}")
            
            articles_with_dates.append({
                'url': full_url,
                'date_obj': date_obj or datetime.now(),
                'title': title[:80],
                'order': idx
            })
            
            if len(articles_with_dates) >= 25:
                LOG.info(f"[Punch Listing] ⏹️ Reached limit of 25 articles")
                break
        
        # Sort by order found
        articles_with_dates.sort(key=lambda x: x['order'])
        urls = [a['url'] for a in articles_with_dates[:15]]
        
        LOG.info("=" * 80)
        LOG.info(f"[Punch Listing] ✅ FINAL RESULTS: Extracted {len(urls)} article URLs")
        LOG.info(f"[Punch Listing] 📊 Summary:")
        LOG.info(f"[Punch Listing]    - Total candidates analyzed: {len(extraction_methods)}")
        LOG.info(f"[Punch Listing]    - After filtering: {len(articles_with_dates)}")
        LOG.info(f"[Punch Listing]    - Final URLs: {len(urls)}")
        
        if urls:
            LOG.info(f"[Punch Listing] 📌 Articles found:")
            for idx, a in enumerate(articles_with_dates[:10], 1):
                LOG.info(f"[Punch Listing]    {idx}. {a['title']}")
                LOG.info(f"[Punch Listing]       {a['url']}")
        
        return urls
        
    except Exception as e:
        LOG.exception(f"[Punch Listing] ❌ Exception: {e}")
        import traceback
        LOG.error(f"[Punch Listing] Traceback:\n{traceback.format_exc()}")
        return []

async def get_nuc_recent_articles(base_url: str = "https://www.nuc.edu.ng") -> List[str]:
    """Get recent NUC articles - ENHANCED LOGGING"""
    LOG.info(f"[NUC Listing] 🔍 Starting extraction from {base_url}")
    
    try:
        listing_html = await shared_playwright.smart_fetch(
            base_url,
            prefer_http=True,
            allow_playwright=True,
            wait_for_selector='article.post, .et_pb_post',
        )
        
        LOG.info(f"[NUC Listing] 📄 HTML length: {len(listing_html) if listing_html else 0} bytes")
        
        if not listing_html:
            LOG.error("[NUC Listing] ❌ No HTML returned from listing page")
            return []
        
        soup = BeautifulSoup(listing_html, 'lxml')
        articles_with_dates = []
        
        article_elements = soup.select('article.post, .et_pb_post')
        LOG.info(f"[NUC Listing] 🔎 Found {len(article_elements)} article elements")
        
        for idx, article in enumerate(article_elements, 1):
            link = article.select_one('a[href*="nuc.edu.ng"]')
            if not link:
                LOG.debug(f"[NUC Listing]   Article [{idx}]: No link found")
                continue
            
            href = link.get('href', '')
            if href.endswith('.pdf') or '/wp-content/' in href:
                LOG.debug(f"[NUC Listing]   Article [{idx}]: Filtered PDF/wp-content")
                continue
            
            full_url = urljoin(base_url, href)
            date_elem = article.select_one('span.published, .post-date, time')
            date_str = date_elem.get_text(strip=True) if date_elem else ""
            date_obj = parse_nuc_date(date_str)
            
            LOG.debug(f"[NUC Listing]   Article [{idx}]: {full_url[:60]}... | Date: {date_str}")
            
            articles_with_dates.append({
                'url': full_url,
                'date_obj': date_obj or datetime.min
            })
        
        articles_with_dates.sort(key=lambda x: x['date_obj'], reverse=True)
        urls = [a['url'] for a in articles_with_dates[:15]]
        
        LOG.info(f"[NUC Listing] ✅ Extracted {len(urls)} article URLs")
        if urls:
            LOG.info(f"[NUC Listing] 📌 Sample URLs:")
            for sample_url in urls[:3]:
                LOG.info(f"[NUC Listing]    - {sample_url}")
        
        return urls
        
    except Exception as e:
        LOG.exception(f"[NUC Listing] ❌ Exception: {e}")
        import traceback
        LOG.error(f"[NUC Listing] Traceback:\n{traceback.format_exc()}")
        return []

# ═══════════════════════════════════════════════════════════════════════════
# SITE-SPECIFIC ARTICLE CONTENTS EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════
def _safe_get_text(elem) -> str:
    try:
        return elem.get_text(' ', strip=True) if elem else ""
    except Exception:
        LOG.debug("[Extractor] _safe_get_text failed", exc_info=True)
        try:
            return str(elem)
        except Exception:
            return ""

def _first_matching_selector_text(soup: BeautifulSoup, selectors: List[str], min_len: int = 0) -> Optional[str]:
    for selector in selectors:
        try:
            elem = soup.select_one(selector)
        except Exception:
            LOG.debug(f"[Extractor] invalid selector '{selector}'", exc_info=True)
            continue
        text = _safe_get_text(elem)
        if text and len(text) > min_len:
            LOG.debug(f"[Extractor] selector '{selector}' matched with length={len(text)}")
            return text
    return None

def _clean_spaces(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip() if text else text

def _remove_unwanted(container, unwanted_selectors: List[str]):
    try:
        for unw_sel in unwanted_selectors:
            for elem in container.select(unw_sel):
                try:
                    elem.decompose()
                except Exception:
                    LOG.debug(f"[Extractor] failed to decompose element for selector '{unw_sel}'", exc_info=True)
    except Exception:
        LOG.debug("[Extractor] _remove_unwanted failed", exc_info=True)

def _safe_parse_date(parse_fn, date_str: str):
    if not date_str or not parse_fn:
        return None
    try:
        return parse_fn(date_str)
    except Exception:
        LOG.debug(f"[Extractor] date parse failed for '{date_str}'", exc_info=True)
        return None

# ---------- MySchool extractor ----------
def extract_myschool_content(html: str, url: str) -> Dict[str, Any]:
    start = time.perf_counter()
    result = {
        'title': "",
        'date_str': "",
        'date_obj': None,
        'snippet': "",
        'url': url,
        'success': False,
        'has_keywords': False,
        'error': None,
        'elapsed': None
    }

    if not html or not isinstance(html, str):
        result['error'] = "Empty or invalid HTML input"
        LOG.warning(f"[MySchool Extract] {url[:80]} - {result['error']}")
        result['elapsed'] = time.perf_counter() - start
        return result

    try:
        soup = BeautifulSoup(html, 'lxml')

        # Title
        title_selectors = [
            'h3.page-title.blog-header-title',
            'h3.blog-header-title',
            'h3.page-title',
            '.post-title',
            'h1'
        ]
        title = _first_matching_selector_text(soup, title_selectors, min_len=5) or ""
        result['title'] = title[:200]

        # Date: search patterns in page text
        date_str = ""
        all_text = soup.get_text(' ', strip=True)
        posted_patterns = [
            r'Posted by\s+[^\|]+\|\s*(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})',
            r'Posted by\s+[^\d]*(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})',
            r'(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})\s*\|\s*\d+\s*Comment',
            r'(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})'
        ]
        for pattern in posted_patterns:
            try:
                m = re.search(pattern, all_text, re.IGNORECASE)
            except re.error:
                LOG.debug(f"[MySchool Extract] bad regex pattern '{pattern}'", exc_info=True)
                m = None
            if m:
                date_str = m.group(1).strip()
                LOG.debug(f"[MySchool Extract] Date found via regex: {date_str}")
                break
        result['date_str'] = date_str

        # Safe parse date using provided parser (wrap errors)
        result['date_obj'] = _safe_parse_date(parse_myschool_date, date_str)

        # Content extraction
        content_selectors = [
            'div.clearfix',
            'div.pb-5',
            'article',
            'div.entry-content',
            'main',
            '.content'
        ]
        content = ""
        for selector in content_selectors:
            try:
                container = soup.select_one(selector)
            except Exception:
                container = None
            if not container:
                continue

            # Work on a copy to avoid changing original soup
            try:
                container_copy = BeautifulSoup(str(container), 'lxml')
            except Exception:
                LOG.debug("[MySchool Extract] failed to copy container", exc_info=True)
                container_copy = container

            unwanted_selectors = [
                'script', 'style', 'nav', 'header', 'footer',
                '.share', '.comments', '.ad', '.widget', '.related', 'iframe',
                '.sidebar', '.author-box', '.social-share', '.post-meta', '.newsletter',
                '.event-date-thumb', '.caption-content-block'
            ]
            _remove_unwanted(container_copy, unwanted_selectors)

            try:
                text = _safe_get_text(container_copy)
            except Exception:
                text = ""
            # Remove common "Posted by..." lines and extra whitespace
            text = re.sub(r'Posted by\s+[^|]+\|[^|]+\|\s*\d+\s*Comments?', '', text, flags=re.I)
            text = _clean_spaces(text)

            if len(text) > 200:
                content = text
                LOG.debug(f"[MySchool Extract] content length {len(content)} using selector '{selector}'")
                break

        # Fallback: paragraphs
        if not content or len(content) < 200:
            paragraphs = []
            for p in soup.select('p'):
                try:
                    p_text = p.get_text(strip=True)
                except Exception:
                    p_text = ""
                if (len(p_text) > 50 and
                        not re.match(r'^Posted by|^Comments|^Share|^Related|^Also read|^Read also|^Category|^Tags', p_text, re.I) and
                        not p_text.isdigit() and
                        '©' not in p_text and
                        'http' not in p_text.lower()):
                    paragraphs.append(p_text)
                    if len(paragraphs) >= 12:  # limit paragraphs processed
                        break
            if paragraphs:
                content = ' '.join(paragraphs)
                content = _clean_spaces(content)
                LOG.debug(f"[MySchool Extract] fallback paragraphs used, length={len(content)}")

        result['snippet'] = ""
        if content:
            sentences = re.split(r'(?<=[.!?])\s+', content)
            meaningful_sentences = []
            for sentence in sentences:
                sentence = sentence.strip()
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
            result['snippet'] = _clean_spaces(snippet)

        # Keyword checks (guard if _SCHOOL_KEYWORDS_RE is missing)
        try:
            title_has_keywords = _SCHOOL_KEYWORDS_RE.search(result['title']) if _SCHOOL_KEYWORDS_RE else True
            snippet_has_keywords = _SCHOOL_KEYWORDS_RE.search(result['snippet']) if _SCHOOL_KEYWORDS_RE else True
            result['has_keywords'] = bool(title_has_keywords or snippet_has_keywords)
        except Exception:
            LOG.debug("[MySchool Extract] keyword regex check failed", exc_info=True)
            result['has_keywords'] = False

        # Date recency check
        try:
            date_is_recent = is_recent_date(result['date_obj']) if result['date_obj'] else False
        except Exception:
            LOG.debug("[MySchool Extract] is_recent_date failed", exc_info=True)
            date_is_recent = False

        # Success criteria
        success = bool(
            result['title'] and
            len(result['snippet'] or "") > 50 and
            (result.get('has_keywords', False)) and
            date_is_recent
        )
        result['success'] = success

        LOG.info(f"[MySchool Extract] {url[:80]} success={success} title_len={len(result['title'])} snippet_len={len(result['snippet'])} date='{result['date_str']}'")
    except Exception as exc:
        LOG.exception(f"[MySchool Extract] failed for {url[:80]}: {exc}")
        result['error'] = str(exc)
    finally:
        result['elapsed'] = time.perf_counter() - start
        return result


# ---------- Punch extractor ----------
def extract_punch_content(html: str, url: str) -> Dict[str, Any]:
    start = time.perf_counter()
    result = {
        'title': "",
        'date_str': "",
        'date_obj': None,
        'snippet': "",
        'url': url,
        'success': False,
        'has_keywords': False,
        'error': None,
        'elapsed': None
    }

    if not html or not isinstance(html, str):
        result['error'] = "Empty or invalid HTML input"
        LOG.warning(f"[Punch Extract] {url[:80]} - {result['error']}")
        result['elapsed'] = time.perf_counter() - start
        return result

    try:
        soup = BeautifulSoup(html, 'lxml')

        title_selectors = [
             'h1.text-3xl',
            'h1.post-title',
            '.post-title',
            'h1.entry-title',
            'h1'
        ]
        title = _first_matching_selector_text(soup, title_selectors, min_len=10) or ""
        result['title'] = title[:200]

        # Date element
        date_str = ""
        date_elem = None
        try:
            date_elem = soup.select_one('span.post-date')
        except Exception:
            date_elem = None
        if date_elem:
            date_str = _safe_get_text(date_elem)
            result['date_obj'] = extract_punch_date_from_html(soup, url)
            if result['date_obj']:
                result['date_str'] = result['date_obj'].strftime('%B %d, %Y %I:%M %p')

        # Content extraction
        content_selectors = [
            'div.post-content',
            'article.prose',
            '.entry-content',
            'article',
            'main'
        ]
        content = ""
        for selector in content_selectors:
            try:
                container = soup.select_one(selector)
            except Exception:
                container = None
            if not container:
                continue

            # remove unwanted bits
            for bad in container.select('script, style, nav, header, footer, .share, .comments, .ad, .widget, .related, iframe'):
                try:
                    bad.decompose()
                except Exception:
                    LOG.debug("[Punch Extract] failed to decompose element", exc_info=True)

            paragraphs = []
            for p in container.select('p'):
                try:
                    p_text = p.get_text(strip=True)
                except Exception:
                    p_text = ""
                if len(p_text) > 30:
                    paragraphs.append(p_text)
            if paragraphs:
                content = ' '.join(paragraphs)
                content = _clean_spaces(content)
                LOG.debug(f"[Punch Extract] content length {len(content)} using selector '{selector}'")
                break

        # Create snippet
        snippet = ""
        if content:
            content = re.sub(r'Kindly share this story.*?(?=[A-Z])', '', content, flags=re.I | re.DOTALL)
            content = _clean_spaces(content)

            sentences = re.split(r'(?<=[.!?])\s+', content)
            meaningful_sentences = []
            for sentence in sentences:
                sentence = sentence.strip()
                if len(sentence) > 30 and re.search(r'[A-Z]', sentence):
                    meaningful_sentences.append(sentence)
                    if len(' '.join(meaningful_sentences)) > 150:
                        break

            if meaningful_sentences:
                snippet = ' '.join(meaningful_sentences)
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
            result['snippet'] = _clean_spaces(snippet)

        # Keywords and recency checks
        try:
            title_has_keywords = _SCHOOL_KEYWORDS_RE.search(result['title']) if _SCHOOL_KEYWORDS_RE else True
            snippet_has_keywords = _SCHOOL_KEYWORDS_RE.search(result['snippet']) if _SCHOOL_KEYWORDS_RE else True
            result['has_keywords'] = bool(title_has_keywords or snippet_has_keywords)
        except Exception:
            LOG.debug("[Punch Extract] keyword regex check failed", exc_info=True)
            result['has_keywords'] = False

        try:
            date_is_recent = is_recent_date(result['date_obj']) if result['date_obj'] else False
        except Exception:
            LOG.debug("[Punch Extract] is_recent_date failed", exc_info=True)
            date_is_recent = False

        success = bool(
            result['title'] and
            len(result['snippet'] or "") > 50 and
            (result.get('has_keywords', False)) and
            date_is_recent
        )
        result['success'] = success

        LOG.info(f"[Punch Extract] {url[:80]} success={success} title_len={len(result['title'])} snippet_len={len(result['snippet'])} date='{result['date_str']}'")
    except Exception as exc:
        LOG.exception(f"[Punch Extract] failed for {url[:80]}: {exc}")
        result['error'] = str(exc)
    finally:
        result['elapsed'] = time.perf_counter() - start
        return result


# ---------- NUC extractor ----------
def extract_nuc_content(html: str, url: str) -> Dict[str, Any]:
    start = time.perf_counter()
    result = {
        'title': "",
        'date_str': "",
        'date_obj': None,
        'snippet': "",
        'url': url,
        'success': False,
        'has_keywords': False,
        'error': None,
        'elapsed': None
    }

    if not html or not isinstance(html, str):
        result['error'] = "Empty or invalid HTML input"
        LOG.warning(f"[NUC Extract] {url[:80]} - {result['error']}")
        result['elapsed'] = time.perf_counter() - start
        return result

    try:
        soup = BeautifulSoup(html, 'lxml')

        title_selectors = [
            'h1.entry-title',
            '.entry-title',
            'h1'
        ]
        title = _first_matching_selector_text(soup, title_selectors, min_len=10) or ""
        result['title'] = title[:200]

        date_elem = None
        try:
            date_elem = soup.select_one('span.published')
            if not date_elem:
                date_elem = soup.select_one('.post-date, time')
        except Exception:
            date_elem = None

        if date_elem:
            date_str = _safe_get_text(date_elem)
            result['date_str'] = date_str
            result['date_obj'] = _safe_parse_date(parse_nuc_date, date_str)

        content_selectors = [
            'div.entry-content',
            'article .content',
            '.post-content',
            'article'
        ]
        content = ""
        for selector in content_selectors:
            try:
                container = soup.select_one(selector)
            except Exception:
                container = None
            if not container:
                continue

            for bad in container.select('script, style, nav, header, footer, .share, .comments, .ad, .widget, .related, iframe'):
                try:
                    bad.decompose()
                except Exception:
                    LOG.debug("[NUC Extract] failed to decompose element", exc_info=True)

            text = _safe_get_text(container)
            if title and text.lower().startswith(title.lower()):
                text = text[len(title):].strip()
            text = _clean_spaces(text)

            if len(text) > 200:
                content = text
                LOG.debug(f"[NUC Extract] content length {len(content)} using selector '{selector}'")
                break

        snippet = ""
        if content:
            sentences = re.split(r'(?<=[.!?])\s+', content)
            meaningful_sentences = []
            for sentence in sentences:
                sentence = sentence.strip()
                if len(sentence) > 30 and re.search(r'[A-Z]', sentence):
                    meaningful_sentences.append(sentence)
                    if len(' '.join(meaningful_sentences)) > 150:
                        break

            if meaningful_sentences:
                snippet = ' '.join(meaningful_sentences)
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
            result['snippet'] = _clean_spaces(snippet)

        try:
            title_has_keywords = _SCHOOL_KEYWORDS_RE.search(result['title']) if _SCHOOL_KEYWORDS_RE else True
            snippet_has_keywords = _SCHOOL_KEYWORDS_RE.search(result['snippet']) if _SCHOOL_KEYWORDS_RE else True
            result['has_keywords'] = bool(title_has_keywords or snippet_has_keywords)
        except Exception:
            LOG.debug("[NUC Extract] keyword regex check failed", exc_info=True)
            result['has_keywords'] = False

        try:
            date_is_recent = is_recent_date(result['date_obj']) if result['date_obj'] else False
        except Exception:
            LOG.debug("[NUC Extract] is_recent_date failed", exc_info=True)
            date_is_recent = False

        success = bool(
            result['title'] and
            len(result['snippet'] or "") > 50 and
            (result.get('has_keywords', False)) and
            date_is_recent
        )
        result['success'] = success

        LOG.info(f"[NUC Extract] {url[:80]} success={success} title_len={len(result['title'])} snippet_len={len(result['snippet'])} date='{result['date_str']}'")
    except Exception as exc:
        LOG.exception(f"[NUC Extract] failed for {url[:80]}: {exc}")
        result['error'] = str(exc)
    finally:
        result['elapsed'] = time.perf_counter() - start
        return result


# ---------- Generic wrapper ----------
def extract_clean_content_v5(html: str, url: str, site_type: str = '') -> Dict[str, Any]:
    start = time.perf_counter()
    if not html or not isinstance(html, str):
        return {'success': False, 'error': 'Empty or invalid HTML', 'elapsed': time.perf_counter() - start, 'url': url}

    try:
        if site_type == 'myschool':
            return extract_myschool_content(html, url)
        elif site_type == 'punch':
            return extract_punch_content(html, url)
        elif site_type == 'nuc':
            return extract_nuc_content(html, url)

        LOG.warning(f"[Extract] No specific extractor for site_type='{site_type}', using generic fallback for {url[:80]}")

        soup = BeautifulSoup(html, 'lxml')
        title = _first_matching_selector_text(soup, ['h1'], min_len=1) or ""
        content_elem = soup.select_one('article, main, .content, .entry-content')
        content = _safe_get_text(content_elem) if content_elem else ""
        snippet = content[:250] if len(content) > 250 else content

        return {
            'title': title[:200],
            'date_str': "",
            'date_obj': None,
            'snippet': _clean_spaces(snippet),
            'url': url,
            'success': bool(title and len(snippet) > 50),
            'has_keywords': True,
            'elapsed': time.perf_counter() - start
        }
    except Exception as exc:
        LOG.exception(f"[Extract] generic extractor failed for {url[:80]}: {exc}")
        return {'success': False, 'error': str(exc), 'elapsed': time.perf_counter() - start, 'url': url}
# ═══════════════════════════════════════════════════════════════════════════
# SITE-SPECIFIC SCRAPERS (WITH ENHANCED LOGGING)
# ═══════════════════════════════════════════════════════════════════════════
async def scrape_myschool_recent(base_url: str = "https://myschool.ng", max_articles: int = 10) -> List[Dict]:
    """Scrape MySchool recent articles - FIXED FETCH SETTINGS"""
    LOG.info(f"\n{'='*70}")
    LOG.info(f"[MySchool Scraper] 🎯 STARTING - base_url={base_url}, max={max_articles}")
    LOG.info(f"{'='*70}")
    
    try:
        # STEP 1: Get article URLs
        LOG.info("[MySchool Scraper] STEP 1: Getting article URLs...")
        article_urls = await get_myschool_recent_articles(base_url)
        
        if not article_urls:
            LOG.warning("[MySchool Scraper] ⚠️ get_myschool_recent_articles returned 0 URLs")
            return []
        
        LOG.info(f"[MySchool Scraper] ✅ STEP 1 COMPLETE: {len(article_urls)} URLs obtained")
        
        # STEP 2: Fetch HTML for each article
        LOG.info(f"[MySchool Scraper] STEP 2: Fetching HTML for {len(article_urls[:10])} articles...")
        
        # ✅ FIX: MySchool needs Playwright, not HTTP first
        html_results = await shared_playwright.run_concurrent(
            article_urls[:10],
            use_http_first=False,  # MySchool needs Playwright directly
            allow_playwright=True,
            fetch_kwargs={
                "wait_for_selector": 'h3.page-title.blog-header-title, div.clearfix, div.pb-5',
                "scroll_to_load": True,
                "play_timeout": 90000,
                "partial_on_timeout": True,
            }
        )
        
        LOG.info(f"[MySchool Scraper] ✅ STEP 2 COMPLETE: Received {len(html_results)} results")
        
        # STEP 3: Extract content
        LOG.info("[MySchool Scraper] STEP 3: Extracting content from HTML...")
        all_extracted = []
        
        for idx, (url, html) in enumerate(html_results, 1):
            LOG.info(f"[MySchool Scraper]   Processing [{idx}/{len(html_results)}]: {url[:60]}...")
            
            if not html:
                LOG.warning(f"[MySchool Scraper]   ✗ No HTML for {url}")
                continue
            
            LOG.debug(f"[MySchool Scraper]   HTML length: {len(html)} bytes")
            
            try:
                data = extract_myschool_content(html, url)
                
                LOG.debug(f"[MySchool Scraper]   Extraction result:")
                LOG.debug(f"[MySchool Scraper]     - Success: {data.get('success')}")
                LOG.debug(f"[MySchool Scraper]     - Title: {data.get('title', '')[:50]}")
                LOG.debug(f"[MySchool Scraper]     - Date: {data.get('date_str')}")
                LOG.debug(f"[MySchool Scraper]     - Snippet length: {len(data.get('snippet', ''))}")
                LOG.debug(f"[MySchool Scraper]     - Has keywords: {data.get('has_keywords')}")
                
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
                    LOG.info(f"[MySchool Scraper]   ✓ SUCCESS: {data['title'][:50]}...")
                else:
                    LOG.warning(f"[MySchool Scraper]   ✗ FAILED: Extraction unsuccessful")
                    
            except Exception as extract_error:
                LOG.exception(f"[MySchool Scraper]   ❌ EXCEPTION during extraction: {extract_error}")
        
        LOG.info(f"[MySchool Scraper] ✅ STEP 3 COMPLETE: {len(all_extracted)} articles extracted")
        
        # STEP 4: Sort and limit
        if all_extracted:
            all_extracted.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
            all_extracted = all_extracted[:max_articles]
            LOG.info(f"[MySchool Scraper] STEP 4: Returning top {len(all_extracted)} articles")
        
        LOG.info(f"[MySchool Scraper] 🎉 FINAL RESULT: {len(all_extracted)} articles")
        return all_extracted
        
    except Exception as e:
        LOG.exception(f"[MySchool Scraper] ❌ FATAL EXCEPTION: {e}")
        import traceback
        LOG.error(f"[MySchool Scraper] Full traceback:\n{traceback.format_exc()}")
        return []

async def scrape_punch_recent(base_url: str = "https://punchng.com", max_articles: int = 10) -> List[Dict]:
    """Scrape Punch recent articles - ENHANCED LOGGING"""
    LOG.info(f"\n{'='*70}")
    LOG.info(f"[Punch Scraper] 🎯 STARTING - base_url={base_url}, max={max_articles}")
    LOG.info(f"{'='*70}")
    
    try:
        # STEP 1: Get article URLs
        LOG.info("[Punch Scraper] STEP 1: Getting article URLs...")
        article_urls = await get_punch_recent_articles(base_url)
        
        if not article_urls:
            LOG.warning("[Punch Scraper] ⚠️ get_punch_recent_articles returned 0 URLs")
            return []
        
        LOG.info(f"[Punch Scraper] ✅ STEP 1 COMPLETE: {len(article_urls)} URLs obtained")
        
        # STEP 2: Fetch HTML
        LOG.info(f"[Punch Scraper] STEP 2: Fetching HTML for {len(article_urls)} articles...")
        
        html_results = await shared_playwright.run_concurrent(
            article_urls,
            use_http_first=True,
            allow_playwright=True,
            fetch_kwargs={
                "wait_for_selector": 'h1.post-title, h1, .post-title, .entry-title',
                "scroll_to_load": False
            }
        )
        
        LOG.info(f"[Punch Scraper] ✅ STEP 2 COMPLETE: Received {len(html_results)} results")
        
        # STEP 3: Extract content
        LOG.info("[Punch Scraper] STEP 3: Extracting content from HTML...")
        articles = []
        
        for idx, (url, html) in enumerate(html_results, 1):
            LOG.info(f"[Punch Scraper]   Processing [{idx}/{len(html_results)}]: {url[:60]}...")
            
            if not html:
                LOG.warning(f"[Punch Scraper]   ✗ No HTML for {url}")
                continue
            
            LOG.debug(f"[Punch Scraper]   HTML length: {len(html)} bytes")
            
            try:
                data = extract_clean_content_v5(html, url, 'punch')
                
                LOG.debug(f"[Punch Scraper]   Extraction result:")
                LOG.debug(f"[Punch Scraper]     - Success: {data.get('success')}")
                LOG.debug(f"[Punch Scraper]     - Has keywords: {data.get('has_keywords')}")
                
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
                    LOG.info(f"[Punch Scraper]   ✓ SUCCESS: {data['title'][:50]}...")
                else:
                    LOG.warning(f"[Punch Scraper]   ✗ FAILED: success={data.get('success')}, keywords={data.get('has_keywords')}")
                    
            except Exception as extract_error:
                LOG.exception(f"[Punch Scraper]   ❌ EXCEPTION during extraction: {extract_error}")
        
        LOG.info(f"[Punch Scraper] ✅ STEP 3 COMPLETE: {len(articles)} articles extracted")
        
        # STEP 4: Sort and limit
        articles.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
        articles = articles[:max_articles]
        
        LOG.info(f"[Punch Scraper] 🎉 FINAL RESULT: {len(articles)} articles")
        return articles
        
    except Exception as e:
        LOG.exception(f"[Punch Scraper] ❌ FATAL EXCEPTION: {e}")
        import traceback
        LOG.error(f"[Punch Scraper] Full traceback:\n{traceback.format_exc()}")
        return []


async def scrape_nuc_recent(base_url: str = "https://www.nuc.edu.ng", max_articles: int = 8) -> List[Dict]:
    """Scrape NUC recent articles - ENHANCED LOGGING"""
    LOG.info(f"\n{'='*70}")
    LOG.info(f"[NUC Scraper] 🎯 STARTING - base_url={base_url}, max={max_articles}")
    LOG.info(f"{'='*70}")
    
    try:
        # STEP 1: Get article URLs
        LOG.info("[NUC Scraper] STEP 1: Getting article URLs...")
        article_urls = await get_nuc_recent_articles(base_url)
        
        if not article_urls:
            LOG.warning("[NUC Scraper] ⚠️ get_nuc_recent_articles returned 0 URLs")
            return []
        
        LOG.info(f"[NUC Scraper] ✅ STEP 1 COMPLETE: {len(article_urls)} URLs obtained")
        
        # STEP 2: Fetch HTML
        LOG.info(f"[NUC Scraper] STEP 2: Fetching HTML for {len(article_urls)} articles...")
        
        html_results = await shared_playwright.run_concurrent(
            article_urls,
            use_http_first=True,
            allow_playwright=True,
            fetch_kwargs={
                "wait_for_selector": 'h1.entry-title',
                "scroll_to_load": False
            }
        )
        
        LOG.info(f"[NUC Scraper] ✅ STEP 2 COMPLETE: Received {len(html_results)} results")
        
        # STEP 3: Extract content
        LOG.info("[NUC Scraper] STEP 3: Extracting content from HTML...")
        articles = []
        
        for idx, (url, html) in enumerate(html_results, 1):
            LOG.info(f"[NUC Scraper]   Processing [{idx}/{len(html_results)}]: {url[:60]}...")
            
            if not html:
                LOG.warning(f"[NUC Scraper]   ✗ No HTML for {url}")
                continue
            
            LOG.debug(f"[NUC Scraper]   HTML length: {len(html)} bytes")
            
            try:
                data = extract_clean_content_v5(html, url, 'nuc')
                
                LOG.debug(f"[NUC Scraper]   Extraction result:")
                LOG.debug(f"[NUC Scraper]     - Success: {data.get('success')}")
                LOG.debug(f"[NUC Scraper]     - Has keywords: {data.get('has_keywords')}")
                
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
                    LOG.info(f"[NUC Scraper]   ✓ SUCCESS: {data['title'][:50]}...")
                else:
                    LOG.warning(f"[NUC Scraper]   ✗ FAILED: success={data.get('success')}, keywords={data.get('has_keywords')}")
                    
            except Exception as extract_error:
                LOG.exception(f"[NUC Scraper]   ❌ EXCEPTION during extraction: {extract_error}")
        
        LOG.info(f"[NUC Scraper] ✅ STEP 3 COMPLETE: {len(articles)} articles extracted")
        
        # STEP 4: Sort and limit
        articles.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
        articles = articles[:max_articles]
        
        LOG.info(f"[NUC Scraper] 🎉 FINAL RESULT: {len(articles)} articles")
        return articles
        
    except Exception as e:
        LOG.exception(f"[NUC Scraper] ❌ FATAL EXCEPTION: {e}")
        import traceback
        LOG.error(f"[NUC Scraper] Full traceback:\n{traceback.format_exc()}")
        return []
# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
async def scrape_school_news(
    urls: Union[List[str], Dict[str, Dict[str, Any]]],
    fetch_full_content: bool = False,
    max_articles: int = 5,
    semaphore_retries: int = 2,
    semaphore_backoff: float = 0.5,
    partial_on_timeout: bool = True,
    max_concurrency: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Unified school news scraper - ENHANCED LOGGING + EMERGENCY DIAGNOSTIC"""
    
    LOG.info(f"\n{'='*70}")
    LOG.info("📰 UNIFIED SCHOOL NEWS SCRAPER (Domain-aware HTTP-first policy)")
    LOG.info(f"{'='*70}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # 🧪 EMERGENCY DIAGNOSTIC TEST
    # ═══════════════════════════════════════════════════════════════════════
    #LOG.info("\n" + "="*70)
    #LOG.info("🧪 EMERGENCY DIAGNOSTIC: Testing listing extraction directly")
    #LOG.info("="*70)
    
    #try:
        #LOG.info("🧪 Testing MySchool listing extraction...")
        #test_myschool_urls = await get_myschool_recent_articles()
        #LOG.info(f"🧪 MySchool RESULT: {len(test_myschool_urls)} URLs")
        #if test_myschool_urls:
            #LOG.info(f"🧪 Sample: {test_myschool_urls[0]}")
        
        #LOG.info("🧪 Testing Punch listing extraction...")
        #test_punch_urls = await get_punch_recent_articles()
        #LOG.info(f"🧪 Punch RESULT: {len(test_punch_urls)} URLs")
        
        #LOG.info("🧪 Testing NUC listing extraction...")
        #test_nuc_urls = await get_nuc_recent_articles()
        #LOG.info(f"🧪 NUC RESULT: {len(test_nuc_urls)} URLs")
        
        #LOG.info("🧪 DIAGNOSTIC COMPLETE ✅")
        
    #except Exception as diag_e:
        #LOG.exception(f"🧪 DIAGNOSTIC FAILED: {diag_e}")
        #import traceback
        #LOG.error(f"🧪 Diagnostic traceback:\n{traceback.format_exc()}")
    
    #LOG.info("="*70 + "\n")
    # ═══════════════════════════════════════════════════════════════════════

    if not urls:
        LOG.warning("⚠️ No URLs or site configs provided")
        return []

    # Domain policy
    DOMAIN_POLICY = {
        "punchng.com": {"use_http_first": True, "allow_playwright": True},  # Fixed: allow fallback
        "nuc.edu.ng": {"use_http_first": True, "allow_playwright": True},   # Fixed: allow fallback
        "myschool.ng": {"use_http_first": False, "allow_playwright": True},
    }
    
    LOG.info("📋 Domain Policy:")
    for domain, policy in DOMAIN_POLICY.items():
        LOG.info(f"  {domain}: {policy}")

    # Initialize
    if not shared_playwright._initialized:
        init_kwargs = {}
        if max_concurrency:
            init_kwargs["max_concurrency"] = max_concurrency
        LOG.info("Initializing shared Playwright context for Render free plan...")
        await shared_playwright.initialize(**init_kwargs)

    all_articles: List[Dict[str, Any]] = []

    def _domain_policy_for(domain: str):
        for key, policy in DOMAIN_POLICY.items():
            if key in domain:
                return policy
        return {"use_http_first": True, "allow_playwright": True}

    def _fetch_kwargs(wait_for_selector=None, scroll=False):
        return {
            "wait_for_selector": wait_for_selector,
            "scroll_to_load": scroll,
            "partial_on_timeout": partial_on_timeout
        }

    # ═══════════════════════════════════════════════════════════════════════
    # CASE 2: Dict of site configurations (most common case)
    # ═══════════════════════════════════════════════════════════════════════
    if isinstance(urls, dict):
        LOG.info(f"📋 Processing {len(urls)} site configurations for recent articles")

        # Map friendly names to scraper functions
        SITE_MAPPING = {
            "nuc": {"task": scrape_nuc_recent, "max_articles": 8, "type": "nuc"},
            "myschool": {"task": scrape_myschool_recent, "max_articles": 10, "type": "myschool"},
            "punch": {"task": scrape_punch_recent, "max_articles": 10, "type": "punch"},
            # Handle the bot's friendly names too
            "nuc (universities)": {"task": scrape_nuc_recent, "max_articles": 5, "type": "nuc"},
            "myschool.ng": {"task": scrape_myschool_recent, "max_articles": 10, "type": "myschool"},
            "punch education": {"task": scrape_punch_recent, "max_articles": 10, "type": "punch"},
        }

        tasks = []
        site_names = []

        for site_name, config in urls.items():
            site_key = site_name.lower().strip()
            mapped_config = SITE_MAPPING.get(site_key)
            
            if not mapped_config:
                LOG.warning(f"⚠️ Unknown site '{site_name}', skipping")
                continue

            # Extract base_url from the list format
            base_url = ""
            if isinstance(config, list) and len(config) > 0:
                base_url = config[0]
            elif isinstance(config, dict):
                base_url = config.get("base_url", "")
            elif isinstance(config, str):
                base_url = config

            if not base_url:
                LOG.warning(f"⚠️ No base_url for '{site_name}', skipping")
                continue

            task_func = mapped_config["task"]
            max_per_site = mapped_config.get("max_articles", max_articles)

            try:
                LOG.info(f"\n🎯 Setting up {site_name} (mapped to {mapped_config['type']})")
                LOG.info(f"  Creating task for {site_name} (base_url={base_url}, max={max_per_site})")
                t = asyncio.create_task(task_func(base_url=base_url, max_articles=max_per_site))
                tasks.append(t)
                site_names.append(site_name)
            except Exception as e:
                LOG.exception(f"  ❌ Failed to schedule task for {site_name}: {e}")

        LOG.info(f"\n🚀 Starting concurrent scraping of all sites... ({len(tasks)} tasks)")
        
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            LOG.info(f"✅ Gather completed for {len(results)} sites")
        except Exception as gather_e:
            LOG.exception(f"❌ asyncio.gather failed: {gather_e}")
            import traceback
            LOG.error(f"Gather traceback:\n{traceback.format_exc()}")
            results = [[] for _ in tasks]

        successful_sites = 0
        for site_name, result in zip(site_names, results):
            if isinstance(result, Exception):
                LOG.error(f"❌ {site_name} raised exception: {result}")
                import traceback
                LOG.error(f"{site_name} traceback:\n{traceback.format_exception(type(result), result, result.__traceback__)}")
                continue
                
            articles = result or []
            
            if articles:
                LOG.info(f"✅ {site_name}: {len(articles)} articles found")
                all_articles.extend(articles)
                successful_sites += 1
            else:
                LOG.warning(f"⚠️  {site_name}: No articles found (returned empty list)")
                
        LOG.info(f"\n📊 Concurrent scraping complete: {successful_sites}/{len(SITE_MAPPING)} sites successful")

    # ═══════════════════════════════════════════════════════════════════════
    # CASE 1: List of specific URLs (less common)
    # ═══════════════════════════════════════════════════════════════════════
    elif isinstance(urls, list):
        LOG.info(f"📋 Processing {len(urls)} specific article URLs")
        # ... (keep existing implementation for list case)
        pass

    else:
        LOG.error(f"❌ Invalid input type: {type(urls)}. Expected list or dict.")
        return []

    # ═══════════════════════════════════════════════════════════════════════
    # POST-PROCESSING
    # ═══════════════════════════════════════════════════════════════════════
    LOG.info(f"\n📊 POST-PROCESSING: {len(all_articles)} total articles before filtering")
    
    all_articles.sort(key=lambda x: x.get("date_obj", datetime.min), reverse=True)

    recent_articles = []
    for idx, article in enumerate(all_articles, 1):
        date_obj = article.get("date_obj")
        has_keywords = article.get("has_keywords", False)
        snippet = article.get("snippet", "")

        date_ok = date_obj and is_recent_date(date_obj)
        content_ok = (article.get("title") and snippet and len(snippet) > 30 and has_keywords)

        LOG.debug(f"  Article [{idx}]: {article.get('title', 'Untitled')[:40]}")
        LOG.debug(f"    date_ok={date_ok}, content_ok={content_ok}")

        if date_ok and content_ok:
            recent_articles.append(article)
        else:
            LOG.debug(f"    ✗ Filtered out")

    formatted_articles = []
    for article in recent_articles:
        formatted_articles.append({
            "title": article.get("title", "Untitled"),
            "snippet": article.get("snippet", ""),
            "date": article.get("date", ""),
            "link": article.get("url", ""),
            "source": article.get("source", "unknown"),
            "pdf": article.get("pdf", False),
            "date_obj": article.get("date_obj"),
            "has_keywords": article.get("has_keywords", True)
        })

    formatted_articles.sort(key=lambda x: (
        x.get("date_obj") is None,
        -(x.get("date_obj", datetime.now()).timestamp() if x.get("date_obj") else 0)
    ))

    LOG.info(f"\n📊 FINAL RESULTS")
    LOG.info(f"{'─'*40}")
    LOG.info(f"Total articles found: {len(all_articles)}")
    LOG.info(f"Recent articles (last {MAX_ARTICLE_AGE_DAYS} days): {len(recent_articles)}")
    LOG.info(f"Returning: {len(formatted_articles[:max_articles * 2])}")

    return formatted_articles[:max_articles * 2]
