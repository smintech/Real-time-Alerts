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

def fetch_with_cloudscraper_aggressive(url: str, retries: int = 5) -> str:
    for attempt in range(1, retries + 1):
        LOG.debug("Cloudscraper attempt %d/%d for %s", attempt, retries, url)
        try:
            time.sleep(random.uniform(1.5, 4.0))
            scraper = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': random.choice(['windows', 'darwin', 'linux']),
                    'mobile': False
                },
                delay=random.randint(4, 10),
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
                """)
                
                await asyncio.sleep(random.uniform(1.5, 4.0))
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)  # networkidle + higher timeout
                
                await page.wait_for_timeout(random.randint(2000, 4000))
                
                try:
                    await page.evaluate('window.scrollTo(0, document.body.scrollHeight/2)')
                    await page.wait_for_timeout(800)
                except Exception:
                    pass
                
                html = await page.content()
                
                if len(html) > 3000 and 'cloudflare' not in html.lower() and 'just a moment' not in html.lower():
                    LOG.debug("Playwright succeeded for %s", url)
                    return html
        except Exception as e:
            LOG.debug("Playwright attempt %d exception for %s: %s", attempt, url, e)
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
        
        if attempt < retries:
            await asyncio.sleep(random.uniform(3, 8))
    
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

@retry(max_attempts=3, backoff=1.5)
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

def _split_concatenated_numeric_token(token: str) -> List[float]:
    cleaned = re.sub(r"[^\d]", "", token)
    n = len(cleaned)
    results = []
    if n < 6:
        return []
    for i in range(3, n - 2):
        a = cleaned[:i]
        b = cleaned[i:]
        try:
            va = float(a)
            vb = float(b)
        except Exception:
            continue
        if va >= 10 and vb >= 10:
            cur = min(va, vb)
            prev = max(va, vb)
            results.append((cur, prev))
    if not results:
        return []
    best = min(results, key=lambda p: abs(p[0] - p[1]))
    return [float(best[0]), float(best[1])]

def _gather_price_candidates_from_dom(soup: BeautifulSoup) -> List[float]:
    selectors = [
        "span.prc", "span.price", "div.price", "div.prc", ".product-price",
        ".price", ".prc", "span[class*='price']", "div[class*='price']",
        "span[class*='prc']", "span[class*='old-price']", ".price--was",
        "span[class*='_3e_22_199e7']", "[data-testid*='price']"
    ]
    found: List[float] = []
    seen_texts = set()
    
    for sel in selectors:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            if not txt or txt in seen_texts:
                continue
            seen_texts.add(txt)
            p = _parse_price_string(txt)
            if p:
                found.append(p)
            else:
                for token in re.findall(r"[\d.,]{6,}", txt):
                    split = _split_concatenated_numeric_token(token)
                    if split:
                        found.extend(split)
    
    for container in soup.select("div, section, li"):
        price_children = []
        for child in container.find_all(True, recursive=False):
            txt = child.get_text(" ", strip=True)
            if not txt:
                continue
            if ('₦' in txt or 'NGN' in txt or re.search(r"[\d\.,]{6,}", txt)):
                p = _parse_price_string(txt)
                if p:
                    price_children.append(p)
                else:
                    for token in re.findall(r"[\d\.,]{6,}", txt):
                        split = _split_concatenated_numeric_token(token)
                        if split:
                            price_children.extend(split)
        if len(price_children) >= 2:
            found.extend(price_children)
    
    return found

def _extract_previous_price(soup: BeautifulSoup, json_ld: Optional[dict], domain: str, 
                           page_text: str, current_price: Optional[float]) -> Optional[float]:
    candidates: List[float] = []
    
    try:
        if isinstance(json_ld, dict):
            offers = json_ld.get("offers")
            if offers:
                offers_list = offers if isinstance(offers, list) else [offers]
                for offer in offers_list:
                    for key in ("priceBeforeDiscount", "listPrice", "originalPrice", "highPrice"):
                        v = offer.get(key)
                        if isinstance(v, (int, float)):
                            candidates.append(float(v))
                        elif isinstance(v, str):
                            p = _parse_price_string(v)
                            if p: candidates.append(p)
                    ps = offer.get("priceSpecification")
                    if isinstance(ps, dict):
                        v = ps.get("price") or ps.get("value")
                        p = _parse_price_string(str(v))
                        if p: candidates.append(p)
    except Exception:
        pass
    
    try:
        dom_prices = _gather_price_candidates_from_dom(soup)
        if dom_prices:
            candidates.extend(dom_prices)
    except Exception:
        pass
    
    for tag in ("del", "s", "strike"):
        for el in soup.find_all(tag):
            txt = el.get_text(" ", strip=True)
            p = _parse_price_string(txt)
            if p:
                candidates.append(p)
    
    class_selectors = [
        "[class*='old-price']", "[class*='was-price']", "[class*='wasprice']",
        "[class*='strike']", "[class*='list-price']", "[class*='regular-price']",
        "[class*='price--was']", "[class*='price-old']", "[class*='previous-price']",
        "span.-old", "div.-old"
    ]
    for sel in class_selectors:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            p = _parse_price_string(txt)
            if p: candidates.append(p)
    
    label_patterns = [
        r"(?:was|old|list|rrp)\s*[:\-\u2014]?\s*(?:₦|NGN|N)?\s*([\d\.,]+)",
        r"(?:₦|NGN|N)\s*([\d\.,]+)",
    ]
    for pat in label_patterns:
        for m in re.findall(pat, page_text, flags=re.IGNORECASE):
            p = _parse_price_string(m)
            if p: candidates.append(p)
    
    for m in re.findall(r"(?:₦|NGN|N)\s*[\d\.,]+\s*(?:₦|NGN|N)\s*[\d\.,]+", page_text):
        parts = re.findall(r"[\d\.,]+", m)
        if len(parts) >= 2:
            p1 = _parse_price_string(parts[0])
            p2 = _parse_price_string(parts[1])
            if p1: candidates.append(p1)
            if p2: candidates.append(p2)
    
    cleaned: List[float] = []
    limit_multiplier = 10.0
    
    for c in candidates:
        try:
            v = float(c)
            if v <= 0 or v < 10:
                continue
            if current_price and current_price > 0:
                if v > (current_price * limit_multiplier):
                    continue
                if v > 1_000_000_000:
                    continue
            cleaned.append(v)
        except Exception:
            continue
    
    if not cleaned:
        return None
    
    best_guess = max(cleaned)
    if current_price and abs(best_guess - current_price) < 1.0:
        return None
    return best_guess

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
            selectors = ["span.price", "p.price", "h4.price", "div.price", "[class*='price']"]
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(" ", strip=True)
                    p = _parse_price_string(text)
                    if p and p > 100_000_000:
                        split = _split_concatenated_numeric_token(str(int(p)))
                        if split:
                            p = min(split)
                    if p:
                        product["current_price"] = p
                        break
        
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
    """MySchool.ng-specific extraction."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()

    containers = soup.select(".news-item, .post, .article")

    for c in containers:
        try:
            title_elem = c.select_one("h2 a, h3 a, .title a")
            if title_elem:
                title = title_elem.get_text(strip=True)
                link = urljoin(base_url, title_elem.get("href"))
            else:
                continue

            date = c.select_one(".date, .post-date") and c.select_one(".date, .post-date").get_text(strip=True) or None

            snippet = ""
            p = c.select_one(".excerpt, p")
            if p:
                snippet = p.get_text(strip=True)

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
        except:
            continue

    return items

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
    Extract news listings with site-specific improvements.
    """
    if not html or len(html) < 1000:
        LOG.debug("HTML too short (len=%d) for %s", len(html), base_url)
        return []
    
    domain = get_domain_from_url(base_url)
    
    # Site-specific extractors
    if "punchng.com" in domain:
        LOG.debug("Using Punch-specific extractor")
        return extract_punch_items(html, base_url)
    
    if "myschool.ng" in domain:
        LOG.debug("Using MySchool-specific extractor")
        return extract_myschool_items(html, base_url)
    
    if "jamb.gov.ng" in domain:
        LOG.debug("Using JAMB-specific extractor")
        return extract_jamb_items(html, base_url)
    
    if "neco.gov.ng" in domain:
        LOG.debug("Using NECO-specific extractor")
        return extract_neco_items(html, base_url)
    
    if "nuc.edu.ng" in domain:
        LOG.debug("Using NUC-specific extractor")
        return extract_nuc_items(html, base_url)
    
    if "education.gov.ng" in domain:
        LOG.debug("Using Education.gov.ng-specific extractor")
        return extract_education_items(html, base_url)
    
    if "lasubeb.lg.gov.ng" in domain:
        LOG.debug("Using LASUBEB-specific extractor")
        return extract_lasubeb_items(html, base_url)
    
    # Generic extractor (as before, with improvements)
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen = set()
    
    for selector in ['nav', 'header', 'footer', '[id*="header"]', '[id*="footer"]']:
        for elem in soup.select(selector):
            try:
                elem.decompose()
            except:
                pass
    
    article_container_selectors = [
        'article',
        '.post', '.entry', '.article',
        '[class*="post-"]', '[class*="article-"]',
        '.news-item', '.story', '.content-item',
        '.tdb_single_post', '.td-post-content',
        '.entry-content', '.post-content',
        '.theiaStickySidebar article',
        'div[class*="news"]', 'div[class*="story"]',
    ]
    
    article_containers = []
    for selector in article_container_selectors:
        found = soup.select(selector)
        if found:
            article_containers.extend(found)
            if len(article_containers) >= 15:
                break
    
    for container in article_containers[:20]:
        try:
            title_elem = None
            title_text = ""
            article_url = ""
            
            for heading_tag in ['h1', 'h2', 'h3', 'h4']:
                heading = container.find(heading_tag)
                if heading:
                    link = heading.find('a', href=True)
                    if link:
                        title_text = link.get_text(" ", strip=True)
                        article_url = link.get('href', '')
                        title_elem = link
                        break
                    else:
                        title_text = heading.get_text(" ", strip=True)
            
            if not title_text:
                for link in container.find_all('a', href=True):
                    link_text = link.get_text(" ", strip=True)
                    if len(link_text) >= 8:
                        title_text = link_text
                        article_url = link.get('href', '')
                        title_elem = link
                        break
            
            if not title_text:
                for li in container.select("li"):
                    a = li.find('a', href=True)
                    if a:
                        txt = a.get_text(" ", strip=True)
                        if txt and len(txt) >= 6:
                            title_text = txt
                            article_url = a.get('href')
                            title_elem = a
                            break
                
                if not title_text:
                    for a in container.find_all('a', href=True):
                        txt = a.get_text(" ", strip=True)
                        if txt and len(txt) >= 8 and ('news' in a.get('href', '').lower() or '/news' in a.get('href', '').lower()):
                            title_text = txt
                            article_url = a.get('href')
                            title_elem = a
                            break
            
            if not title_text:
                continue
            
            if len(title_text) < 8:
                if title_elem is not None:
                    alt = (title_elem.get('title') or title_elem.get('aria-label') or "").strip()
                    if alt and len(alt) >= 8:
                        title_text = alt
            
            title_text = re.sub(r'^(News|Update|Article)\s+', '', title_text, flags=re.I)
            title_text = re.sub(r'\s+', ' ', title_text).strip()
            
            if article_url:
                full_url = urljoin(base_url, article_url)
            else:
                continue
            
            dedup_key = f"{title_text[:50]}|{full_url}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            
            date_str = None
            
            time_elem = container.find(['time', 'span[class*="date"]', 'div[class*="date"]'])
            if time_elem:
                date_str = time_elem.get_text(" ", strip=True)
            
            if not date_str and _DATE_RE:
                container_text = container.get_text(" ", strip=True)[:500]
                date_match = _DATE_RE.search(container_text)
                if date_match:
                    date_str = date_match.group(0)
            
            if date_str:
                date_str = re.sub(r'^News\s+', '', date_str, flags=re.I)
                date_str = date_str.strip()
            
            snippet = ""
            
            excerpt_selectors = [
                '.entry-summary', '.excerpt', '.post-excerpt',
                '[class*="summary"]', '[class*="excerpt"]',
                '.article-content p', '.post-content p',
                '.entry-content p',
            ]
            
            for sel in excerpt_selectors:
                excerpt_elem = container.select_one(sel)
                if excerpt_elem:
                    snippet_text = excerpt_elem.get_text(" ", strip=True)
                    if len(snippet_text) >= 50:
                        snippet = snippet_text
                        break
            
            if not snippet:
                paragraphs = container.find_all('p')
                for p in paragraphs[:3]:
                    p_text = p.get_text(" ", strip=True)
                    if len(p_text) < 30:
                        continue
                    if p_text.lower() == title_text.lower():
                        continue
                    if re.match(r'^(by|posted|published|source)', p_text, re.I):
                        continue
                    
                    snippet = p_text
                    break
            
            if not snippet and title_elem is not None:
                parent = title_elem.parent
                for sib in parent.find_next_siblings(limit=3):
                    if sib.name == 'p':
                        t = sib.get_text(" ", strip=True)
                        if len(t) >= 40:
                            snippet = t
                            break
            
            if not snippet:
                container_text = container.get_text(" ", strip=True)
                container_text = container_text.replace(title_text, '').strip()
                if len(container_text) >= 50:
                    snippet = container_text[:300]
            
            if snippet:
                snippet = re.sub(r'\s+', ' ', snippet)
                snippet = snippet.replace("Read More", "").replace("Continue Reading", "").strip()
                
                if snippet.lower().strip() == title_text.lower().strip():
                    snippet = "Click to read full update..."
                
                nav_keywords = ['home', 'about us', 'contact', 'search', 'login', 'register', 'menu']
                if sum(1 for kw in nav_keywords if kw in snippet.lower()) >= 3:
                    continue
                
                words = snippet.split()
                if len(words) > 200:
                    snippet = ' '.join(words[:200]) + "..."
                elif len(words) < 10:
                    snippet = "Click to read full update..."
            else:
                snippet = "Click to read full update..."
            
            item = {
                "title": title_text,
                "snippet": snippet,
                "date": date_str,
                "link": full_url,
                "source": urlparse(base_url).netloc,
                "pdf": _is_pdf_link(full_url)
            }
            
            items.append(item)
            
        except Exception as e:
            LOG.debug("Failed to extract article from container: %s", e)
            continue
    
    if not items:
        body = soup.select_one('body')
        context = body.get_text(" ", strip=True)[:2000] if body else html[:2000]
        LOG.debug("No items extracted from %s — content sample: %s", base_url, context)
    
    items.sort(key=lambda x: (
        x['date'] is None,
        -len(x.get('snippet', '')),
    ))
    
    LOG.info("Extracted %d items from %s", len(items), base_url)
    return items[:15]

async def scrape_school_news(
    urls: List[str], 
    fetch_full_content: bool = False, 
    max_articles: int = 10
) -> List[Dict[str, Any]]:
    """
    Scrape school news with parallel fetching for speed.
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
        
        items = extract_school_news_listings(html, successful_url)
        LOG.info(f"  ✓ Found {len(items)} articles from {successful_url}")
        
        if not items:
            continue
        
        if fetch_full_content and max_articles > 0:
            LOG.info(f"\n  📄 Fetching full content for top {max_articles} articles in parallel...")
            
            # Parallel fetch
            tasks = [fetch_article_details(item['link']) for item in items[:max_articles] if item['link']]
            details_list = await asyncio.gather(*tasks, return_exceptions=True)
            
            for item, details in zip(items[:max_articles], details_list):
                if not isinstance(details, Exception) and details:
                    item['snippet'] = details['content']
                    item['full_content'] = details['full_content']
                    item['word_count'] = details['word_count']
                    item['key_info'] = details['key_info']
                    if details['date'] and not item['date']:
                        item['date'] = details['date']
                else:
                    LOG.warning(f"Failed parallel fetch for {item['title'][:30]}")
        
        all_news.extend(items)
        LOG.info(f"\n  ✓ Processed {len(items)} articles from {successful_url}")
        
        LOG.info("-" * 70)
        await asyncio.sleep(random.uniform(3, 6))  # Reduced
    
    LOG.info(f"\n{'='*70}")
    LOG.info(f"📊 TOTAL: {len(all_news)} articles from {len(urls)} sources")
    LOG.info(f"{'='*70}\n")
    
    return all_news