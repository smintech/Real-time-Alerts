from telegram.error import TelegramError
from telegram import Bot
import os
from playwright.async_api import async_playwright
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
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup, Tag
import cloudscraper
from requests.exceptions import RequestException
from typing import Dict, Optional, Any, Tuple, Callable, List, Set

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

# ═══════════════════════════════════════════════════════════════════════════
# NEWS PATH DETECTION (IMPROVED - REMOVED SUBDOMAIN GUESS)
# ═══════════════════════════════════════════════════════════════════════════

COMMON_NEWS_PATHS = [
    "/news", "/news.aspx", "/news-events", "/news-and-events",
    "/News", "/bulletin", "/bulletins", "/Bulletin", "/news-events/",
    "/category/news", "/category/news/", "/category/press-release",
    "/topics/education/", "/category/education/", "/tags/education/",
]

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

async def fetch_with_playwright_aggressive(url: str, retries: int = 3) -> str:
    for attempt in range(1, retries + 1):
        LOG.debug("Playwright attempt %d/%d for %s", attempt, retries, url)
        browser = None
        context = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--disable-web-security',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--disable-setuid-sandbox',
                ])
                context = await browser.new_context(
                    user_agent=random.choice(_USER_AGENTS),
                    viewport={'width': 1280, 'height': 800},
                    locale='en-US',
                    timezone_id='Africa/Lagos',
                )
                page = await context.new_page()

                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'permissions', {get: () => ({ query: () => Promise.resolve({state: 'granted'}) })});
                """)

                await page.goto(url, wait_until='domcontentloaded', timeout=90000)
                 await page.wait_for_load_state("networkidle", timeout=90000)
                await page.wait_for_timeout(random.randint(3000, 7000))
                
                # Full scroll to trigger lazy-loaded prices
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(1000)
                
                # Original half-scroll
                try:
                    await page.evaluate('window.scrollTo(0, document.body.scrollHeight/2)')
                    await page.wait_for_timeout(800)
                except Exception:
                    pass
                
                html = await page.content()
                
                if len(html) > 8000 and 'cloudflare' not in html.lower() and 'just a moment' not in html.lower():
                    LOG.debug("Playwright succeeded for %s", url)
                    return html
        except Exception as e:
            LOG.debug("Playwright attempt %d exception for %s: %s", attempt, url, e)
        finally:
            if context:
                try:
                    await context.close()
                except:
                    pass
            if browser:
                try:
                    await browser.close()
                except:
                    pass
        
        if attempt < retries:
            await asyncio.sleep(random.uniform(5, 10))

    raise Exception("Playwright failed all attempts")
# ═══════════════════════════════════════════════════════════════════════════
# ULTIMATE FETCH
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_html_ultimate(url: str) -> str:
    LOG.info("fetch_html_ultimate: fetching %s", url)
    
    try:
        loop = asyncio.get_running_loop()
        html = await loop.run_in_executor(None, fetch_with_cloudscraper_aggressive, url, 5)
        return html
    except Exception as e:
        LOG.debug("Cloudscraper exhausted for %s: %s", url, e)
    
    try:
        html = await fetch_with_playwright_aggressive(url, retries=3)
        return html
    except Exception as e:
        LOG.debug("Playwright exhausted for %s: %s", url, e)
    
    raise Exception(f"Failed to fetch {url}")

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
    s = s.replace('\xa0', ' ').strip()
    m = re.findall(r"[\d.,]+", s)
    if not m:
        return None
    num = max(m, key=len)
    try:
        if ',' in num and '.' in num and num.rfind('.') > num.rfind(','):
            clean = num.replace(',', '')
        elif num.count(',') > 0 and num.count('.') == 0:
            clean = num.replace(',', '')
        else:
            clean = num
        clean = re.sub(r"[^\d.]", "", clean)
        if clean == "":
            return None
        v = float(clean)
        return v
    except Exception:
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

def _extract_konga_current_price(soup: BeautifulSoup, page_text: str) -> Optional[float]:
    """
    Konga-specific extraction for 2026 with aggressive anti-rating/review logic.
    """
    
    # Primary selectors based on Konga's 2026 HTML structure
    konga_selectors = [
        "span[data-testid='current-price']", 
        "div[data-testid='price-current']",
        "span[data-testid='product-price']",
        "div._3e_22_199e7",  # Main price container
        "span.-b.-ubpt",     # Bold price element
        "span.prc.-fs24",    # Large price display
        "[data-price-type='final']",
        "meta[property='og:price:amount']",
        "meta[name='product:price:amount']"
    ]
    
    for selector in konga_selectors:
        el = soup.select_one(selector)
        if not el:
            continue
            
        # CRITICAL: Exclude rating/review/suggestion sections
        parent_classes = ' '.join(el.parent.get('class', [])).lower() if el.parent else ""
        ancestors = el.find_parents()
        ancestor_classes = ' '.join([' '.join(a.get('class', [])) for a in ancestors]).lower()
        
        if any(x in parent_classes + ' ' + ancestor_classes for x in [
            'rating', 'stars', 'review', 'reviews', 'suggestion', 'recommendation', 
            'you-might-like', 'related-products', 'product-suggestions', 'similar-products'
        ]):
            continue

        # Get price value
        price_val = el.get("content") if el.name == "meta" else el.get_text(" ", strip=True)
        p = _parse_price_string(price_val)
        if p and 10000 <= p < 100_000_000:
            LOG.debug("Konga price found via %s: ₦%.0f", selector, p)
            return p

    # Fallback DOM gathering with Konga-specific exclusion
    candidates = _gather_price_candidates_from_dom(soup, domain_hint="konga")
    valid_candidates = []
    
    for p in candidates:
        # Filter for realistic product prices only
        if 10000 <= p <= 10_000_000:
            # Exclude rating scores (1.0-5.0) and review counts
            if not (1.0 <= p <= 5.0 or (p < 1000 and p.is_integer())):
                valid_candidates.append(p)
    
    if valid_candidates:
        # Current price is typically the highest displayed
        return max(valid_candidates)
    
    LOG.debug("Konga: No valid price found after all 2026 filters")
    return None

def extract_prices_from_visible_text(
    page_text: str,
    currency_prefixes: str = r"₦|N|NGN",
    min_price: float = 10000,
    max_price: float = 20000000
) -> Tuple[Optional[float], Optional[float]]:
    """
    Pure visible-text fallback:
    - Scans the flat rendered page text in document order
    - Finds prices starting with ₦/N/NGN followed by numbers
    - First valid price → current
    - If second price exists and is lower → second = current, first = previous (common discount pattern)
    - Otherwise second = previous if different
    """
    pattern = rf"({currency_prefixes})\s*([\d,]+(?:\.\d+)?)"
    matches = list(re.finditer(pattern, page_text, re.IGNORECASE))

    valid_prices = []
    for m in matches:
        num_part = m.group(2).replace(",", "").strip()
        try:
            value = float(num_part)
            if min_price <= value <= max_price:
                valid_prices.append(value)
        except ValueError:
            continue

    if not valid_prices:
        LOG.debug("No valid prices found in rendered page text")
        return None, None

    first = valid_prices[0]
    second = valid_prices[1] if len(valid_prices) >= 2 else None

    if second is None:
        return first, None

    # Common case: old price appears first (higher), then discounted price
    if first > second and (first - second) > 500:  # meaningful discount
        current = second
        previous = first
    else:
        current = first
        previous = second if abs(second - first) > 500 else None  # avoid same price twice

    LOG.info(
        "Visible-text fallback → current: ₦%.0f | previous: %s",
        current,
        f"₦{previous:.0f}" if previous is not None else "None"
    )

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
    domain = get_domain_from_url(url)
    if not any(s in domain for s in SUPPORTED_SITES):
        raise NotImplementedError(f"Unsupported site: {domain}. Add to SUPPORTED_SITES.")
    
    try:
        if any(d in domain for d in ("jumia", "konga")):
            try:
                html = await fetch_with_playwright_aggressive(url, retries=3)
            except Exception:
                html = await _fetch_html(url)
        else:
            html = await _fetch_html(url)
    except Exception as e:
        raise NoDataError(f"Failed to fetch page: {e}")
    
    soup = BeautifulSoup(html, "lxml")
    
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
                    
                    img_field = item.get("image") or item.get("images")
                    if img_field:
                        if isinstance(img_field, str):
                            product["images"].append(urljoin(url, img_field))
                        elif isinstance(img_field, list):
                            for it in img_field:
                                if isinstance(it, str) and it.strip():
                                    product["images"].append(urljoin(url, it))
                    break
        except Exception:
            continue
    
    if product["title"] == "Product":
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            product["title"] = og_title["content"].strip()
    
    if product["current_price"] is None:
        og_price = soup.find("meta", property="og:price:amount")
        if og_price and og_price.get("content"):
            try:
                product["current_price"] = float(og_price["content"])
            except:
                pass
    
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
    
    def _add_image_candidate(src):
        if not src:
            return
        try:
            abs_url = urljoin(url, src.strip())
            if abs_url not in product["images"]:
                product["images"].append(abs_url)
        except Exception:
            pass
    
    og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if og_image and og_image.get("content"):
        _add_image_candidate(og_image["content"])
    
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
    
    if not product["images"]:
        img_tags = soup.find_all("img")
        seen = set()
        for img in img_tags:
            src = img.get("data-src") or img.get("src") or img.get("data-original")
            if not src:
                continue
            src = urljoin(url, src.strip())
            if src in seen:
                continue
            w = img.get("width") or img.get("data-width")
            h = img.get("height") or img.get("data-height")
            try:
                w_n = int(w) if w and str(w).isdigit() else 0
                h_n = int(h) if h and str(h).isdigit() else 0
            except Exception:
                w_n = h_n = 0
            if w_n and h_n and (w_n < 50 or h_n < 50):
                continue
            seen.add(src)
            product["images"].append(src)
            if len(product["images"]) >= 6:
                break
    
    # CURRENT PRICE EXTRACTION (ENHANCED FOR KONGA)
    if product["current_price"] is None:
        if "jumia" in domain:
            selectors = ["span.-b", ".-fs24", ".prc", ".-prc", "div.prc"]
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(" ", strip=True)
                    p = _parse_price_string(text)
                    if p:
                        product["current_price"] = p
                        break
        
        if "konga" in domain and product["current_price"] is None:
            page_text = soup.get_text(" ", strip=True)
            try:
               p = _extract_konga_current_price(soup, page_text)
               if p:
                    product["current_price"] = p
            except Exception as e:
                LOG.debug("Konga price extractor exception: %s", e)
        
        if "konga" in domain and product["current_price"] is None:
            try:
                if len(page_text) > 1500:  # minimal sanity check
                    curr, prev = extract_prices_from_visible_text(page_text)
                    if curr is not None:
                        product["current_price"] = curr
                        # Only set previous if it makes sense (higher than current)
                        if prev is not None and prev > curr:
                            product["previous_price"] = prev
                        LOG.info(
                            "Konga visible-text fallback → current ₦%.0f (prev %s)",
                            curr,
                            f"₦{prev:.0f}" if prev else "None"
                        )
                    else:
                        LOG.debug("Visible-text fallback found no valid prices on Konga")
                else:
                    LOG.warning("Page text too short for Konga fallback (%d chars)", len(page_text))
            except Exception as ex:
                LOG.exception("Konga visible-text fallback error: %s", ex)
        
        if product["current_price"] is None:
            page_text = soup.get_text(" ", strip=True)
            matches = re.findall(r"(?:₦|NGN)[\s]?([\d,]+\.?\d*)", page_text)
            if matches:
                prices = []
                for m in matches:
                    clean = m.replace(",", "")
                    try:
                        prices.append(float(clean))
                    except:
                        pass
                if prices:
                    product["current_price"] = max(prices)
    
    # PREVIOUS PRICE EXTRACTION
    page_text = soup.get_text(" ", strip=True)
    prev_price = _extract_previous_price(soup, json_ld_data, domain, page_text, product["current_price"])
    if prev_price:
        product["previous_price"] = prev_price
        LOG.info("Found previous price: ₦%.0f for %s", prev_price, product["title"])
    
    # Stock & title fallbacks (ENHANCED FOR KONGA)
    page_text_lower = soup.get_text().lower()
    if any(phrase in page_text_lower for phrase in ["out of stock", "sold out", "unavailable", "not available"]):
        product["stock_status"] = "out_of_stock"
    
    if product["title"] == "Product" or "Buy" in product["title"]:
        h1 = soup.select_one("h1.-fs20, h1.-pb10, h1.brd, .v-p-hd h1, h1, .product-title, h1.product-name")  # Enhanced
        if h1:
            product["title"] = h1.get_text(strip=True)
        elif soup.title:
            product["title"] = soup.title.string.strip()
    
    if product["current_price"] is None:
        # Fallback 1: DOM candidates (often catches discounted prices)
        candidates = _gather_price_candidates_from_dom(soup, domain)
        if candidates:
            unique = sorted(set(candidates), reverse=True)
            if len(unique) >= 2 and unique[1] / unique[0] >= 0.6:
                product["current_price"] = unique[1]  # lower = current (discount)
                LOG.info("Fallback DOM: selected discounted current price ₦%.0f", product["current_price"])
            elif unique:
                product["current_price"] = unique[0]
                LOG.info("Fallback DOM: selected price ₦%.0f", product["current_price"])

    if product["current_price"] is None:
        # Fallback 2: Regex on full page text
        page_text = soup.get_text(" ", strip=True)
        matches = re.findall(r"(?:₦|NGN)[\s]*([\d,]+\.?\d*)", page_text)
        prices = []
        for m in matches:
            clean = m.replace(",", "").strip()
            try:
                val = float(clean)
                if 10000 <= val <= 20000000:
                    prices.append(val)
            except:
                pass
        if prices:
            unique = sorted(set(prices), reverse=True)
            if len(unique) >= 2 and unique[1] / unique[0] >= 0.6:
                product["current_price"] = unique[1]
            elif unique:
                product["current_price"] = unique[0]
            LOG.info("Fallback regex: selected current price ₦%.0f", product["current_price"])

    if product["current_price"] is None:
        raise NoDataError("No price found after all extraction methods")

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
    
    product["description"] = product["description"][:1500].strip()
    product["raw"] = {"json_ld": json_ld_data} if json_ld_data else {"snippet": product["title"]}
    
    LOG.info("Successfully scraped %s — ₦%.0f — %s (desc len=%d)", 
             product["title"], product["current_price"], domain, len(product["description"]))
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
# SCHOOL NEWS ARTICLE EXTRACTION (IMPROVED WITH SITE-SPECIFIC LOGIC)
# ═══════════════════════════════════════════════════════════════════════════

def extract_key_information(content: str) -> Dict[str, Any]:
    """Extract structured information from article content."""
    key_info = {
        'dates_mentioned': [],
        'institutions': [],
        'exams': [],
        'deadlines': [],
        'important_actions': []
    }
    
    if not content:
        return key_info
    
    # Extract dates
    dates = _DATE_RE.findall(content) if _DATE_RE else []
    found_dates = []
    for d in dates:
        if isinstance(d, tuple):
            for part in d:
                if part and part.strip():
                    found_dates.append(part.strip())
        elif isinstance(d, str):
            found_dates.append(d)
    key_info['dates_mentioned'] = list(dict.fromkeys(found_dates))[:5]
    
    # Extract institutions
    institutions_pattern = r'\b([A-Z][A-Za-z]+\s+(?:University|Polytechnic|College|Institute))\b'
    key_info['institutions'] = list(set(re.findall(institutions_pattern, content)))[:10]
    
    # Extract exam mentions
    exam_pattern = r'\b(JAMB|WAEC|NECO|POST-UTME|UTME|GCE|BECE)\b'
    key_info['exams'] = list(set(re.findall(exam_pattern, content, re.I)))
    
    # Extract deadlines
    deadline_pattern = r'(?:deadline|closes?|ends?|due|before|by)\s+.*?(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})'
    key_info['deadlines'] = re.findall(deadline_pattern, content, re.I)[:3]
    
    # Extract action items
    action_pattern = r'\b(register|apply|submit|pay|upload|visit|contact|download)\b.*?(?:\.|;|\n)'
    actions = re.findall(action_pattern, content, re.I)
    key_info['important_actions'] = [a.strip() for a in actions[:5]]
    
    return key_info

def clean_text(text: str) -> str:
    """Clean extracted text."""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'(Advertisement|ADVERTISEMENT|Subscribe|Share|Tweet|Pin)', '', text)
    return text.strip()

def extract_article_content(html: str, url: str) -> Dict[str, str]:
    """
    Extract article content with site-specific improvements.
    Handles CSR/SSR via Playwright fetch (already done).
    """
    soup = BeautifulSoup(html, 'lxml')
    
    # Remove noise
    for elem in soup.select('script, style, [class*="ad"], [id*="ad"], [class*="banner"], [id*="banner"], iframe, noscript'):
        try:
            elem.decompose()
        except Exception:
            pass
    
    domain = get_domain_from_url(url)
    
    # Site-specific title selectors
    title_selectors = {
        'default': ['h1.entry-title', 'h1.post-title', 'h1', 'title', 'meta[property="og:title"]', 'h2', 'h3'],
        'jamb.gov.ng': ['.article-title', 'h1', 'h2'],
        'neco.gov.ng': ['.post-title', 'h1'],
        'nuc.edu.ng': ['.page-title', 'h2 a'],
        'myschool.ng': ['.news-title', 'h1', 'h2'],
        'punchng.com': ['h1.entry-title', 'h1.post-title'],
        'education.gov.ng': ['.article-header h1', 'h2'],
        'lasubeb.lg.gov.ng': ['.news-headline', 'h3'],
    }
    
    # Site-specific content selectors
    content_selectors = {
        'default': ['article.post-content', 'div.entry-content', 'div.post-content', 'div.article-content', 'div.content', 'article', 'div.post', 'div.article-body', 'div.main-content', 'section.article', 'div.body-text', 'div#article', 'div#content'],
        'jamb.gov.ng': ['.article-body', 'div.content'],
        'neco.gov.ng': ['.post-body', 'div.entry'],
        'nuc.edu.ng': ['.page-content', 'div.article'],
        'myschool.ng': ['.news-content', 'div.body'],
        'punchng.com': ['.entry-content', 'div.post-content'],
        'education.gov.ng': ['.article-content', 'div.main'],
        'lasubeb.lg.gov.ng': ['.news-body', 'div.content'],
    }
    
    # Select based on domain
    site_key = next((k for k in content_selectors if k in domain), 'default')
    selected_title_selectors = title_selectors.get(site_key, title_selectors['default'])
    selected_content_selectors = content_selectors.get(site_key, content_selectors['default'])
    
    # Extract title
    title = ""
    for selector in selected_title_selectors:
        elem = soup.select_one(selector)
        if elem:
            if selector.startswith('meta'):
                title = clean_text(elem.get('content', ''))
            else:
                title = clean_text(elem.get_text())
            if title:
                break
    
    # Extract date
    date = None
    if _DATE_RE:
        date_match = _DATE_RE.search(html)
        if date_match:
            date = date_match.group(0)
    
    # Extract content
    content = ""
    for selector in selected_content_selectors:
        article_elem = soup.select_one(selector)
        if article_elem:
            # Remove inner noise
            for inner_ad in article_elem.select('[class*="ad"], [id*="ad"], [class*="banner"], [id*="banner"]'):
                try:
                    inner_ad.decompose()
                except Exception:
                    pass
            
            paragraphs = article_elem.find_all('p')
            if len(paragraphs) >= 2:
                content_parts = []
                for p in paragraphs:
                    text = clean_text(p.get_text())
                    if len(text) > 50:
                        content_parts.append(text)
                content = "\n\n".join(content_parts)
                break
    
    # Fallback
    if not content or len(content) < 200:
        all_paragraphs = soup.find_all('p')
        content_parts = []
        for p in all_paragraphs:
            text = clean_text(p.get_text())
            if len(text) > 30 and (_SCHOOL_KEYWORDS_RE.search(text) if _SCHOOL_KEYWORDS_RE else True):
                content_parts.append(text)
        content = "\n\n".join(content_parts[:20])
    
    if not content:
        body = soup.select_one('body')
        if body:
            content = clean_text(body.get_text(separator="\n", strip=True))
    
    key_info = extract_key_information(content)
    
    # Create snippet
    snippet = ""
    if content:
        words = content.split()
        if len(words) > 200:
            snippet = ' '.join(words[:200]) + "..."
        else:
            snippet = content
    
    return {
        'title': title or "Untitled Article",
        'date': date,
        'content': snippet,
        'full_content': content,
        'key_info': key_info,
        'url': url,
        'word_count': len(content.split()) if content else 0
    }

def _is_pdf_link(href: str) -> bool:
    """Check if link is a PDF."""
    if not href:
        return False
    href_clean = href.split('?', 1)[0].split('#', 1)[0].lower()
    return href_clean.endswith(".pdf")

def _get_pdf_head_info(url: str, timeout: float = 8.0) -> dict:
    """Get PDF metadata without downloading full file."""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        status = getattr(resp, "status_code", None)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        
        if status is None or status >= 400 or "pdf" not in content_type:
            try:
                resp = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
            except Exception as e_get:
                return {
                    "ok": False,
                    "error": f"GET failed after HEAD: {e_get}",
                    "final_url": getattr(resp, "url", url) if resp is not None else url,
                    "status_code": status
                }
        
        final_status = getattr(resp, "status_code", None)
        final_url = getattr(resp, "url", url)
        final_content_type = (resp.headers.get("Content-Type") or "").lower()
        content_length = None
        if resp.headers.get("Content-Length"):
            try:
                content_length = int(resp.headers.get("Content-Length"))
            except Exception:
                content_length = None
        
        try:
            if getattr(resp, "raw", None):
                try:
                    resp.close()
                except Exception:
                    pass
        except Exception:
            pass
        
        return {
            "ok": True,
            "content_type": final_content_type,
            "content_length": content_length,
            "final_url": final_url,
            "status_code": final_status
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "final_url": url, "status_code": None}

async def fetch_article_details(article_url: str) -> Optional[Dict[str, Any]]:
    """Fetch and extract full article details."""
    LOG.info(f"Fetching article: {article_url[:60]}…")
    try:
        html = await fetch_html_ultimate(article_url)
        details = extract_article_content(html, article_url)
        LOG.info(f"✓ Extracted {details['word_count']} words")
        return details
    except Exception as e:
        LOG.error(f"✗ Failed to fetch article: {str(e)[:50]}")
        return None

def extract_myschool_news_from_visible(soup: BeautifulSoup, max_items: int = 10) -> List[Dict[str, Any]]:
    """
    myschool.ng optimized visible-text extraction: handles link-heavy pages.
    - Scans likely containers or all <a> tags with title-like text
    - Recovers href directly from <a> for reliable links
    - Sets placeholder snippet ("Read more...") — details fetched later
    """
    items = []
    seen_titles = set()

    # Step 1: Get full visible text for fallback filtering
    page_text = soup.get_text(separator="\n", strip=True)

    # Step 2: Find potential title links (hybrid: use <a> for links, text for filtering)
    a_tags = soup.find_all('a', href=True)
    for a in a_tags:
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 25 or len(title) > 140:
            continue
        if any(n in title.lower() for n in ['read more', 'see more', 'comments', 'share', 'like', 'views', 'advertisement', 'sponsored']):
            continue
        if title in seen_titles:
            continue

        # Keyword filter (required for relevance)
        if not any(kw.lower() in title.lower() for kw in ['jamb', 'waec', 'neco', 'utme', 'admission', 'post-utme', 'cut off', 'result', 'school fees', 'registration', '2026']):
            continue

        # Guess if it's a news link (myschool structure: /news/slug)
        href = a['href']
        if '/news/' not in href.lower() and '/blog/' not in href.lower():
            continue  # skip non-news links

        link = urljoin("https://myschool.ng", href)

        # Look for date in visible text near title (search in page_text around title position)
        date = None
        if _DATE_RE:
            # Find approximate position in full text
            pos = page_text.lower().find(title.lower())
            if pos != -1:
                nearby_text = page_text[max(0, pos-200):pos+len(title)+200]
                m = _DATE_RE.search(nearby_text)
                if m:
                    date = m.group(0)

        items.append({
            "title": title,
            "date": date,
            "snippet": "Read more for full update...",  # placeholder — fetch details later
            "link": link,
            "source": "myschool.ng"
        })
        seen_titles.add(title)

        if len(items) >= max_items:
            break

    # Sort: prefer items with dates, then title alpha
    items.sort(key=lambda x: (x['date'] is None, x['title']))

    LOG.info("myschool visible-text extraction → %d items with placeholder snippets", len(items))
    return items[:max_items]

# ═══════════════════════════════════════════════════════════════════════════
# SITE-SPECIFIC EXTRACTORS (UPDATED FOR ALL SOURCES)
# ═══════════════════════════════════════════════════════════════════════════

def extract_punch_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """Punch-specific extraction for better results."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    # candidate containers
    container_selectors = ["article", ".td_module_wrap", ".td_module", ".post", ".entry", ".td-block-span6"]
    title_selectors = [".td-module-title a", "h2.entry-title a", "h3.entry-title a", "h2 a", "h3 a", "a.td-image-wrap"]
    date_selectors = ["time", ".td-post-date", ".entry-meta time", ".post-meta time"]
    excerpt_selectors = [".td-excerpt", ".entry-summary p", ".post-excerpt p", ".td-module-meta-info .td-excerpt"]

    containers = []
    for sel in container_selectors:
        found = soup.select(sel)
        if found:
            containers.extend(found)
    if not containers:
        containers = soup.select("ul li, .latest-news li, .news-list li")

    for c in containers:
        try:
            title = None
            link = None
            for tsel in title_selectors:
                t = c.select_one(tsel)
                if t and t.get_text(strip=True):
                    title = t.get_text(" ", strip=True)
                    link = t.get("href") or t.get("data-href")
                    break
            if not title:
                continue

            link = urljoin(base_url, link) if link else None

            date = None
            for dsel in date_selectors:
                d = c.select_one(dsel)
                if d:
                    date = d.get_text(" ", strip=True)
                    break

            snippet = None
            for es in excerpt_selectors:
                e = c.select_one(es)
                if e:
                    snippet = e.get_text(" ", strip=True)
                    if len(snippet) >= 40:
                        break

            if not snippet and link:
                snippet = "Click to read full update..."

            key = f"{title[:50]}|{link}"
            if key in seen:
                continue
            seen.add(key)

            items.append({
                "title": title,
                "snippet": snippet or "Click to read full update...",
                "date": date,
                "link": link,
                "source": urlparse(base_url).netloc,
                "pdf": False
            })
        except Exception:
            continue

    return items

def extract_myschool_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """MySchool.ng-specific extraction with 2026-proof selectors and robust fallback."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    # 2026-optimized container selectors with data attributes and broader patterns
    containers = soup.select(
        ".news-item, .post, .article, .news, .news-list, .news-listing, li.news, div[class*='news'], "
        "article, .post, .entry, li, .content-item, .story, .td_module_wrap, div[class*='post'], "
        "div[class*='item'] , .news-card, .article-card, .post-item, "
        "div[data-testid*='news'], a[data-testid*='news-link'], div[data-testid*='article'], "
        ".post-content, .article-content, div[data-testid*='content-card'], .content-card, .news-block"
    )

    for c in containers:
        try:
            # Multi-level title/link extraction
            title_elem = c.select_one("h2 a, h3 a, .title a, a.title, a, h2, h3, .news-title a")
            if not title_elem:
                continue
                
            title = title_elem.get_text(" ", strip=True)
            link = title_elem.get("href") or title_elem.get("data-href")
            
            if not title or len(title) < 10 or not link:
                continue
                
            link = urljoin(base_url, link)

            # Date extraction from multiple possible locations
            date = None
            date_elem = c.select_one(".date, .post-date, time, span.date, .news-date, [data-testid*='date']")
            if date_elem:
                date = date_elem.get_text(" ", strip=True)

            # Snippet extraction with fallback chain
            snippet = None
            for sel in (".excerpt, .summary, .post-excerpt, .news-snippet, p, div.description, [data-testid*='excerpt']"):
                s = c.select_one(sel)
                if s:
                    snippet = s.get_text(" ", strip=True)
                    if len(snippet) >= 40:
                        break
            
            if not snippet:
                snippet = "Click to read full update..."

            # Deduplication
            key = f"{title[:60]}|{link}"
            if key in seen:
                continue
            seen.add(key)

            items.append({
                "title": re.sub(r'\s+', ' ', title).strip(),
                "snippet": snippet,
                "date": date,
                "link": link,
                "source": urlparse(base_url).netloc,
                "pdf": _is_pdf_link(link)
            })
        except Exception:
            continue

    # Enhanced fallback for MySchool's dynamic rendering
    if len(items) < 3:
        LOG.debug("MySchool fallback: Using generic a-tag scan due to low yield (%d items)", len(items))
        for a in soup.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            href = a.get("href", "").strip()
            
            if not title or len(title) < 10:
                continue
            if _SCHOOL_KEYWORDS_RE and not _SCHOOL_KEYWORDS_RE.search(title):
                continue
                
            full_link = urljoin(base_url, href)
            key = f"{title[:60]}|{full_link}"
            if key in seen:
                continue
            seen.add(key)
                
            # Extract context from parent container
            container = a.find_parent(["li", "div", "article", "p", "td"])
            snippet = "Click link for details."
            date_str = None
            
            if container:
                container_text = container.get_text(" ", strip=True)
                if _DATE_RE:
                    date_match = _DATE_RE.search(container_text)
                    if date_match:
                        date_str = date_match.group(0)
                        
                # Get sibling text for snippet
                for sib in a.find_next_siblings(["p", "div", "span"])[:2]:
                    sib_text = sib.get_text(" ", strip=True)
                    if len(sib_text) > 30:
                        snippet = sib_text[:300]
                        break

            items.append({
                "title": title,
                "snippet": snippet,
                "date": date_str,
                "link": full_link,
                "source": urlparse(base_url).netloc,
                "pdf": _is_pdf_link(full_link)
            })

    items.sort(key=lambda x: (x['date'] is None, -len(x['snippet'])))
    return items[:10]

def extract_jamb_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """JAMB-specific extraction (flat p tags)."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    paragraphs = soup.find_all('p')
    i = 0
    while i < len(paragraphs) - 3:
        title_p = paragraphs[i]
        snippet_p = paragraphs[i+1]
        link_p = paragraphs[i+2]
        date_p = paragraphs[i+3]

        title = title_p.get_text(strip=True)
        if not title or len(title) < 10:
            i += 1
            continue

        snippet = snippet_p.get_text(strip=True)
        link_elem = link_p.find('a', href=True)
        link = urljoin(base_url, link_elem.get('href')) if link_elem else None
        date = date_p.get_text(strip=True)

        key = f"{title[:50]}|{link}"
        if key in seen:
            i += 4
            continue
        seen.add(key)

        items.append({
            "title": title,
            "snippet": snippet or "Click to read full update...",
            "date": date,
            "link": link,
            "source": "jamb.gov.ng",
            "pdf": _is_pdf_link(link) if link else False
        })
        i += 4

    return items

def extract_neco_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """NECO-specific extraction."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    containers = soup.select(".news-item")
    for c in containers:
        title = c.select_one(".news-title, h3").get_text(strip=True) if c.select_one(".news-title, h3") else None
        if not title:
            continue

        date = c.select_one(".news-date, span").get_text(strip=True) if c.select_one(".news-date, span") else None
        snippet = c.select_one(".news-snippet, p").get_text(strip=True) if c.select_one(".news-snippet, p") else "Click to read full update..."
        link_elem = c.select_one(".read-more-link, a")
        link = urljoin(base_url, link_elem.get("href")) if link_elem else None

        key = f"{title[:50]}|{link}"
        if key in seen:
            continue
        seen.add(key)

        items.append({
            "title": title,
            "snippet": snippet,
            "date": date,
            "link": link,
            "source": "neco.gov.ng",
            "pdf": _is_pdf_link(link) if link else False
        })

    return items

def extract_nuc_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """NUC-specific extraction (markdown-like)."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    headings = soup.select("h2")
    for h2 in headings:
        title_a = h2.find("a")
        title = title_a.get_text(strip=True) if title_a else h2.get_text(strip=True)
        link = urljoin(base_url, title_a.get("href")) if title_a else None

        next_p = h2.find_next("p")
        date = None
        if next_p and "by |" in next_p.get_text():
            date_match = re.search(r"by \|\s*(.+?)\s*\|", next_p.get_text())
            date = date_match.group(1) if date_match else None

        excerpt_p = next_p.find_next("p") if next_p else None
        snippet = excerpt_p.get_text(strip=True) if excerpt_p else "Click to read full update..."

        read_more_a = excerpt_p.find_next("a") if excerpt_p else None
        read_more_link = urljoin(base_url, read_more_a.get("href")) if read_more_a and "read more" in read_more_a.get_text().lower() else link

        key = f"{title[:50]}|{read_more_link}"
        if key in seen:
            continue
        seen.add(key)

        items.append({
            "title": title,
            "snippet": snippet,
            "date": date,
            "link": read_more_link,
            "source": "nuc.edu.ng",
            "pdf": _is_pdf_link(read_more_link) if read_more_link else False
        })

    return items

def extract_education_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """Education.gov.ng-specific extraction."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    containers = soup.select(".news-item")
    for c in containers:
        title_h3 = c.select_one("h3")
        title = title_h3.get_text(strip=True) if title_h3 else None
        if not title:
            continue

        link_a = c.select_one("a")
        link = urljoin(base_url, link_a.get("href")) if link_a else None

        snippet_p = c.select_one("p")
        snippet = snippet_p.get_text(strip=True) if snippet_p else "Click to read full update..."

        date = None  # No explicit date in structure; parse from snippet if needed
        date_match = _DATE_RE.search(snippet) if _DATE_RE else None
        date = date_match.group(0) if date_match else None

        key = f"{title[:50]}|{link}"
        if key in seen:
            continue
        seen.add(key)

        items.append({
            "title": title,
            "snippet": snippet,
            "date": date,
            "link": link,
            "source": "education.gov.ng",
            "pdf": _is_pdf_link(link) if link else False
        })

    return items

def extract_lasubeb_items(html: str, base_url: str) -> List[Dict[str, Any]]:
    """LASUBEB-specific extraction (h2 + p)."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    headings = soup.select("h2")
    for h2 in headings:
        title = h2.get_text(strip=True)
        if not title or len(title) < 10:
            continue

        date_p = h2.find_next("p")
        date = date_p.get_text(strip=True) if date_p and re.match(r"^[A-z]+ \d{1,2}(?:st|nd|rd|th) \d{4}$", date_p.get_text(strip=True)) else None

        snippet_p = date_p.find_next("p") if date_p else h2.find_next("p")
        snippet = snippet_p.get_text(strip=True) if snippet_p else "Click to read full update..."

        link = None  # No links in structure; assume base or none

        key = f"{title[:50]}|{link}"
        if key in seen:
            continue
        seen.add(key)

        items.append({
            "title": title,
            "snippet": snippet,
            "date": date,
            "link": link,
            "source": "lasubeb.lg.gov.ng",
            "pdf": False
        })

    return items

# ═══════════════════════════════════════════════════════════════════════════
# IMPROVED SCHOOL NEWS LISTING EXTRACTOR WITH SITE-SPECIFIC SUPPORT
# ═══════════════════════════════════════════════════════════════════════════

def extract_school_news_listings(html: str, base_url: str) -> List[Dict[str, Any]]:
    """
    FIXED: Robust generic extraction for MySchool/Punch + fallback to site-specific for government sites.
    Returns list of articles with title, snippet (from listing page), date, and link.
    """
    if not html or len(html) < 1000:
        LOG.debug("HTML too short for extraction: %s (len=%d)", base_url, len(html) if html else 0)
        return []
    
    domain = get_domain_from_url(base_url)
    
    # Try site-specific extractors first for tricky government sites
    site_specific_items = []
    if "jamb.gov.ng" in domain:
        site_specific_items = extract_jamb_items(html, base_url)
    elif "neco.gov.ng" in domain:
        site_specific_items = extract_neco_items(html, base_url)
    elif "nuc.edu.ng" in domain:
        site_specific_items = extract_nuc_items(html, base_url)
    elif "education.gov.ng" in domain:
        site_specific_items = extract_education_items(html, base_url)
    elif "lasubeb.lg.gov.ng" in domain:
        site_specific_items = extract_lasubeb_items(html, base_url)
    
    # If site-specific found enough items, return those
    if site_specific_items and len(site_specific_items) >= 2:
        return site_specific_items[:15]
    
    # OTHERWISE: Use robust generic link extraction (WORKS for MySchool.ng & Punch)
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen = set()
    
    # Remove navigation noise
    for elem in soup.select('script, style, nav, header, footer, aside, iframe, noscript, [class*="advertisement"], [class*="banner"], [class*="sidebar"]'):
        try:
            elem.decompose()
        except:
            pass
    
    # Find all links that look like news articles
    for a in soup.find_all("a", href=True):
        try:
            title = a.get_text(" ", strip=True)
            href = a.get("href", "").strip()
            
            # Basic validation
            if not title or len(title) < 10 or len(title) > 200:
                continue
            
            # Skip obvious non-content links
            if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                continue
            if any(x in href.lower() for x in ['.jpg', '.png', '.pdf', '.zip', '/wp-content/uploads']):
                continue
            
            # Filter for school keywords OR news URL patterns (but keep if looks like article)
            is_likely_news = any(x in href.lower() for x in ['/news/', '/article/', '/post/', '/story/', '/blog/', '?p=']) or \
                           any(x in title.lower() for x in ['admission', 'exam', 'jamb', 'waec', 'neco', 'utme', 'post-utme', 'university', 'school'])
            
            if _SCHOOL_KEYWORDS_RE and not _SCHOOL_KEYWORDS_RE.search(title) and not is_likely_news:
                continue
            
            full_link = urljoin(base_url, href)
            
            # Skip PDFs (unless we want them marked)
            is_pdf = _is_pdf_link(full_link)
            
            # Deduplicate by title+link
            dedup_key = f"{title[:60]}|{full_link.split('?')[0]}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            
            # Extract snippet from surrounding context (NOT by fetching the page)
            snippet = "Click to read full update..."
            date_str = None
            
            # Find parent container
            container = a.find_parent(["div", "li", "article", "td", "section"])
            
            if container:
                container_text = container.get_text(" ", strip=True)
                
                # Look for date in container (but not in the title itself)
                if _DATE_RE:
                    date_match = _DATE_RE.search(container_text)
                    if date_match:
                        date_candidate = date_match.group(0)
                        # Ensure date is close to the title in the text (not footer date)
                        title_pos = container_text.find(title[:30])
                        date_pos = container_text.find(date_candidate)
                        if title_pos >= 0 and date_pos >= 0 and abs(date_pos - title_pos) < 300:
                            date_str = date_candidate
                
                # Extract snippet: Try to get text after the link or in siblings
                snippet_text = ""
                
                # Method 1: Look for next siblings (p, div, span)
                siblings = a.find_next_siblings(["p", "div", "span"])
                for sib in siblings[:2]:
                    sib_text = sib.get_text(" ", strip=True)
                    # Must be substantial and not just metadata
                    if len(sib_text) > 50 and not sib_text.startswith(("by ", "By ", "posted ", "Source:")):
                        snippet_text = sib_text
                        break
                
                # Method 2: If in li, get remaining text in li after link
                if not snippet_text and container.name == "li":
                    li_text = container.get_text(" ", strip=True)
                    # Remove the link text itself to get the description
                    snippet_text = li_text.replace(title, "", 1).strip()
                
                # Method 3: Look for p tags within container
                if not snippet_text:
                    for p in container.find_all("p", limit=2):
                        p_text = p.get_text(" ", strip=True)
                        if len(p_text) > 40 and p_text != title:
                            snippet_text = p_text
                            break
                
                # Clean up snippet
                if snippet_text:
                    snippet_text = re.sub(r'\s+', ' ', snippet_text)
                    snippet_text = snippet_text.replace("Read More", "").replace("Continue Reading", "").replace("...", "").strip()
                    if len(snippet_text) > 300:
                        snippet_text = snippet_text[:297] + "..."
                    if len(snippet_text) > 20:
                        snippet = snippet_text
            
            items.append({
                "title": clean_text(title),
                "snippet": snippet,
                "date": date_str,
                "link": full_link,
                "source": urlparse(base_url).netloc,
                "pdf": is_pdf
            })
            
        except Exception as e:
            continue  # Skip problematic entries
    
    # Sort: items with dates first, then by snippet length (longer = more likely real content)
    items.sort(key=lambda x: (x['date'] is None, -len(x['snippet']), x['title']))
    
    LOG.info("Extracted %d items from %s", len(items), base_url)
    return items[:15]

async def scrape_school_news(
    urls: List[str],
    fetch_full_content: bool = False,
    max_articles: int = 5
) -> List[Dict[str, Any]]:
    """
    Scrape school news from given URLs.
    Primary: generic extract_school_news_listings
    Fallback: visible-text strategy for myschool.ng if primary yields <2 items.
    Fetches article details/snippets for top items.
    """
    all_news = []

    for base_url in urls:
        LOG.info(f"\n{'='*70}")
        LOG.info(f"📰 Scraping: {base_url}")
        LOG.info(f"{'='*70}")
        
        candidates = candidate_listing_urls(base_url)
        LOG.info(f"  Trying {len(candidates)} candidate URLs...")
        
        html = None
        successful_url = None
        
        for candidate_url in candidates:
            try:
                LOG.debug(f"  Attempting: {candidate_url}")
                test_html = await _fetch_html(candidate_url)
                
                if test_html and len(test_html) > 1000:
                    test_items = extract_school_news_listings(test_html, candidate_url)
                    if test_items and len(test_items) > 0:
                        LOG.info(f"  ✓ SUCCESS with {candidate_url} ({len(test_items)} items)")
                        html = test_html
                        successful_url = candidate_url
                        break
                    else:
                        LOG.debug(f"    No items found at {candidate_url}")
                else:
                    LOG.debug(f"    Insufficient content at {candidate_url} (len={len(test_html) if test_html else 0})")
                
                await asyncio.sleep(random.uniform(1, 2))
                
            except Exception as e:
                LOG.debug(f"    Failed {candidate_url}: {str(e)[:50]}")
                continue
        
        if not html or not successful_url:
            LOG.warning(f"  ✗ All candidate URLs failed for {base_url}")
            continue
        
        soup = BeautifulSoup(html, "lxml")
        
        # Primary extraction: your generic method
        listing_items = extract_school_news_listings(html, successful_url)
        
        # Fallback for myschool.ng: if primary yields <2 items, switch to visible-text
        if "myschool.ng" in successful_url.lower() and len(listing_items) < 2:
            LOG.warning("Primary extraction yielded only %d items for myschool.ng — falling back to visible-text strategy", len(listing_items))
            listing_items = extract_myschool_news_from_visible(soup, max_items=15)
        
        LOG.info(f"  ✓ Found {len(listing_items)} articles from {successful_url}")
        
        if not listing_items:
            continue
        
        all_news.extend(listing_items)
        
        # Fetch full content/details for top articles, using snippets from articles
        if fetch_full_content:
            LOG.info(f"\n  📄 Fetching full content for top {max_articles} articles in parallel...")
            
            tasks = [fetch_article_details(item['link']) for item in listing_items[:max_articles] if item['link']]
            details_list = await asyncio.gather(*tasks, return_exceptions=True)
            
            for item, details in zip(listing_items[:max_articles], details_list):
                if not isinstance(details, Exception) and details:
                    item['snippet'] = details['content']  # Use fetched summary as snippet
                    item['full_content'] = details['full_content']
                    item['key_info'] = details['key_info']
                    item['word_count'] = details['word_count']
                    if details['date'] and not item['date']:
                        item['date'] = details['date']
                else:
                    LOG.warning("Failed to fetch details for %s", item['title'][:30])
        
        LOG.info("\n  ✓ Processed {len(listing_items)} articles from {successful_url}")
        
        LOG.info("-" * 70)
        await asyncio.sleep(random.uniform(3, 6))

    LOG.info(f"\n{'='*70}")
    LOG.info(f"📊 TOTAL: {len(all_news)} articles from {len(urls)} sources")
    LOG.info(f"{'='*70}\n")

    return all_news