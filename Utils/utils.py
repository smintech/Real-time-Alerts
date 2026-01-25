# Utils/utils.py - Fully updated scraper (Apify completely removed, cloudscraper-based, bulletproof for JS-heavy sites)
from telegram.error import TelegramError  # For safe_send
from telegram import Bot  # Type hint for safe_send
import os
from playwright.async_api import async_playwright
import time
import logging
import json
import re
from playwright.sync_api import sync_playwright
# Add these imports near the top of the file
import random
import pathlib
from http import HTTPStatus
import asyncio
import requests
from functools import wraps
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup,Tag
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

# ---------------------------
# safe_send (brought back as requested)
# ---------------------------
async def safe_send(bot: Bot, targets: int | List[int], text: str, **kwargs) -> List[Tuple[int, bool, Optional[str]]]:
    """
    Sends a message to one or more chat_ids safely.
    Returns a list of (target, success_bool, error_message).
    """
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

# Replace the existing `_fetch_rendered_html` synchronous function
# with these two functions:

def _fetch_rendered_html_sync(url: str, wait_for_selector: str | None = "text=Average Petrol Price") -> str:
    """
    Enhanced Playwright renderer with anti-detection measures.
    - Stealth mode launch arguments
    - Realistic user agent rotation
    - Request interception to block tracking
    - Proper context/page isolation
    """
    try:
        with sync_playwright() as p:
            # Enhanced launch args with anti-detection
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",  # Fixes memory issues in Docker
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-web-resources",
                "--disable-component-extensions-with-background-pages",
                "--disable-default-apps",
                "--enable-automation=false",  # Hide automation flag
            ]
            
            browser = p.chromium.launch(
                headless=True,
                args=launch_args,
                timeout=30000,
            )
            
            # Create context with stealth settings
            context = browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
                java_script_enabled=True,
                # Realistic browser fingerprint
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            
            page = context.new_page()
            
            # Inject stealth script before loading page
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => false,
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
            """)
            
            # Block tracking/ads (speeds up page load)
            def route_handler(route):
                if any(block in route.request.url for block in ['google-analytics', 'facebook.com', 'doubleclick']):
                    route.abort()
                else:
                    route.continue_()
            
            page.route("**/*", route_handler)
            
            # Navigate with reasonable timeout
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as nav_err:
                LOG.warning("Navigation timeout/error for %s: %s", url, nav_err)
            
            # Wait for selector if provided
            if wait_for_selector:
                try:
                    page.wait_for_selector(wait_for_selector, timeout=20000)
                except Exception:
                    pass  # Selector might not exist
            
            # Let JavaScript settle
            import time
            time.sleep(random.uniform(1.0, 2.5))
            
            content = page.content()
            
            # Cleanup
            page.close()
            context.close()
            browser.close()
            
            return content or ""
            
    except Exception as e:
        LOG.error("Playwright enhanced render failed for %s: %s", url, e)
        return ""

async def _fetch_rendered_html(url: str, wait_for_selector: str | None = "text=Average Petrol Price") -> str:
    """
    Async wrapper which runs the synchronous renderer in a thread executor.
    Use this from coroutines (await _fetch_rendered_html(...)).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_rendered_html_sync, url, wait_for_selector)

# ---------------------------
# Retry decorator (robust for network/cloudflare issues)
# ---------------------------
def retry(max_attempts: int = 4, backoff: float = 2.0):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
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
                    LOG.warning("%s failed (attempt %d/%d): %s. Sleeping %.1fs...", fn.__name__, attempt, max_attempts, e, sleep_for)
                    time.sleep(sleep_for)
            # unreachable, but keep explicit
            raise last_exc
        return wrapper
    return decorator

@retry(max_attempts=4, backoff=2.0)
def _fetch_lpg_html() -> str:
    url = "https://lpginnigeria.com/chart"
    return _fetch_html(url)  # Reuses existing cloudscraper with retry
# ---------------------------
# Helpers
# ---------------------------
def _get_domain_from_url(u: str) -> str:
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

# --- add near the top of the file with other helpers ---
def _parse_price_string(s: str) -> Optional[float]:
    """
    Robust price string parser.
    Handles NBSP, commas/dots, 'N' or 'NGN' tokens, and chooses the longest numeric piece.
    Returns float or None.
    """
    if not s or not isinstance(s, str):
        return None
    # Normalize common whitespace (including NBSP) and remove non-digits except dot/comma
    s = s.replace('\xa0', ' ').strip()
    # Find numeric pieces like 1,234.56 or 1234 or 1.234
    m = re.findall(r"[\d\.,]+", s)
    if not m:
        return None
    # choose the longest numeric piece (usually the full price)
    num = max(m, key=len)
    try:
        # If both comma and dot exist and dot appears after the last comma, treat commas as thousands sep
        if ',' in num and '.' in num and num.rfind('.') > num.rfind(','):
            clean = num.replace(',', '')
        elif num.count(',') > 0 and num.count('.') == 0:
            # Ambiguous like "1,234" - remove commas as thousands sep
            clean = num.replace(',', '')
        else:
            clean = num
        # final safe replace any stray non-digit/dot
        clean = re.sub(r"[^\d\.]", "", clean)
        if clean == "":
            return None
        v = float(clean)
        return v
    except Exception:
        return None


def _split_concatenated_numeric_token(token: str) -> List[float]:
    """
    When page text collapsed two adjacent price elements into one numeric token
    like '270000290000', attempt to split into plausible pairs (current, previous).
    Returns [current, previous] (floats) for the best candidate, or [] if none found.
    Heuristic: try all splits with at least 3 digits each, keep pairs where both >= 10,
    then choose the split with smallest absolute difference.
    """
    cleaned = re.sub(r"[^\d]", "", token)
    n = len(cleaned)
    results = []
    if n < 6:  # too short to represent two prices
        return []
    # try splits leaving at least 3 digits on each side
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
    """
    Find elements likely containing prices and parse them individually.
    Returns a list of parsed prices (floats). Attempts to avoid concatenation issues
    by parsing element-level text where possible and attempting splits when necessary.
    """
    selectors = [
        "span.prc",
        "span.price",
        "div.price",
        "div.prc",
        ".product-price",
        ".price",
        ".prc",
        "span[class*='price']",
        "div[class*='price']",
        "span[class*='prc']",
        "span[class*='old-price']",
        ".price--was",
        "span[class*='_3e_22_199e7']",
        "[data-testid*='price']"
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
                # maybe element text collapsed two numbers — try splitting numeric tokens inside
                for token in re.findall(r"[\d\.,]{6,}", txt):
                    split = _split_concatenated_numeric_token(token)
                    if split:
                        found.extend(split)

    # Heuristic: look for sibling price elements inside the same container
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

def _extract_previous_price(soup: BeautifulSoup, json_ld: Optional[dict], domain: str, page_text: str, current_price: Optional[float]) -> Optional[float]:
    """
    Try many heuristics to find a 'struck' / earlier / list price on the page.
    Returns float or None.
    """
    candidates: List[float] = []

    # 1) JSON-LD common fields
    try:
        if isinstance(json_ld, dict):
            offers = json_ld.get("offers")
            if offers:
                offers_list = offers if isinstance(offers, list) else [offers]
                for offer in offers_list:
                    # Check common price fields
                    for key in ("priceBeforeDiscount", "listPrice", "originalPrice", "highPrice"):
                        v = offer.get(key)
                        if isinstance(v, (int, float)):
                            candidates.append(float(v))
                        elif isinstance(v, str):
                            p = _parse_price_string(v)
                            if p: candidates.append(p)
                    
                    # nested priceSpecification
                    ps = offer.get("priceSpecification")
                    if isinstance(ps, dict):
                        v = ps.get("price") or ps.get("value")
                        p = _parse_price_string(str(v))
                        if p: candidates.append(p)
    except Exception:
        pass

    # 2) DOM-based element extraction (preferred)
    try:
        dom_prices = _gather_price_candidates_from_dom(soup)
        if dom_prices:
            candidates.extend(dom_prices)
    except Exception:
        pass

    # 3) Semantic HTML tags often used for struck price
    for tag in ("del", "s", "strike"):
        for el in soup.find_all(tag):
            txt = el.get_text(" ", strip=True)
            p = _parse_price_string(txt)
            if p:
                candidates.append(p)

    # 4) Class/attribute patterns (extra coverage)
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

    # 5) Inline label-based fallbacks
    # strictly require a currency symbol or label nearby to avoid phone numbers
    label_patterns = [
        r"(?:was|old|list|rrp)\s*[:\-\u2014]?\s*(?:₦|NGN|N)?\s*([\d\.,]+)",
        r"(?:₦|NGN|N)\s*([\d\.,]+)", # strict currency match
    ]
    for pat in label_patterns:
        for m in re.findall(pat, page_text, flags=re.IGNORECASE):
            p = _parse_price_string(m)
            if p: candidates.append(p)

    # 6) Page text fallback - REMOVED the "naked" number scan to prevent phone number matches
    # Only keep the specific split logic for concatenated currency strings
    for m in re.findall(r"(?:₦|NGN|N)\s*[\d\.,]+\s*(?:₦|NGN|N)\s*[\d\.,]+", page_text):
        parts = re.findall(r"[\d\.,]+", m)
        if len(parts) >= 2:
            p1 = _parse_price_string(parts[0])
            p2 = _parse_price_string(parts[1])
            if p1: candidates.append(p1)
            if p2: candidates.append(p2)

    # --- CLEANUP & SANITY CHECK ---
    cleaned: List[float] = []
    
    # Define a sane upper limit multiplier. 
    # If previous price is > 10x current price, it's likely a phone number or error.
    # Exception: If current_price is very low (e.g. < 1000), 10x might still be valid, 
    # but for phones/laptops, 10x is impossible.
    limit_multiplier = 10.0 
    
    for c in candidates:
        try:
            v = float(c)
            if v <= 0 or v < 10: 
                continue
                
            # SANITY CHECK:
            if current_price and current_price > 0:
                # If the candidate is less than current price, it's not a "previous" price (usually)
                # But sometimes it is (if price increased).
                # However, if candidate is > 10x current price, reject it.
                if v > (current_price * limit_multiplier):
                    continue
                
                # Double check against specific massive numbers you saw in logs (optional hard block)
                if v > 1_000_000_000: # 1 Billion threshold
                     continue

            cleaned.append(v)
        except Exception:
            continue

    if not cleaned:
        return None

    # Usually previous price is the highest valid number found
    best_guess = max(cleaned)
    
    # Final logical check: if best guess is same as current, return None
    if current_price and abs(best_guess - current_price) < 1.0:
        return None
        
    return best_guess

# ---------------------------
# Binance scraper (unchanged)
# ---------------------------
def _scrape_binance_ref(ref: str) -> Dict[str, Any]:
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
                m = re.search(r'/trade/([A-Z0-9_]+)', p.path or "")
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


# ---------------------------
# Core fetch with cloudscraper
# ---------------------------
# ---------- New globals / config ----------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:116.0) Gecko/20100101 Firefox/116.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# Optionally allow a proxy list via env: CSV of proxy URLs like "http://user:pw@1.2.3.4:3128"
_SCRAPER_PROXIES = os.environ.get("SCRAPER_PROXIES", "") and [p.strip() for p in os.environ.get("SCRAPER_PROXIES").split(",") if p.strip()] or []

# reuse one scraper instance so cookies / session are kept between calls
_SCRAPER_SINGLETON = {"scraper": None, "created_with": None}
_SCRAPER_LOCK = asyncio.Lock() if "asyncio" in globals() else None

# ---------- helper functions ----------
def _create_scraper(interpreter: str = "js2py"):
    """
    Create and return a cloudscraper session configured for robust scraping.
    Recreate only when interpreter param changes.
    """
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False, "desktop": True},
        delay=10,  # default delay; we also inject jitter before requests
        interpreter=interpreter,
        captcha={"provider": None},  # explicit: we don't auto-resolve CAPTCHAs here
    )
    # Reasonable connection pool settings
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return s

def _get_shared_scraper(interpreter="js2py"):
    """
    Return a shared cloudscraper instance (reused across calls so cookies persist).
    """
    global _SCRAPER_SINGLETON
    if _SCRAPER_SINGLETON["scraper"] is None or _SCRAPER_SINGLETON["created_with"] != interpreter:
        _SCRAPER_SINGLETON["scraper"] = _create_scraper(interpreter=interpreter)
        _SCRAPER_SINGLETON["created_with"] = interpreter
    return _SCRAPER_SINGLETON["scraper"]

def _is_blocked_html(html: str) -> bool:
    if not html:
        return True
    t = (html or "").lower()
    keywords = [
        "just a moment", "verify you are human", "attention required",
        "checking your browser", "cloudflare", "are you human",
        "javascript required", "enable javascript", "access denied",
        "403", "429", "bot", "automated"
    ]
    return any(k in t for k in keywords)

def _choose_proxy():
    """Return a proxy dict for requests if proxies configured (rotates)."""
    if not _SCRAPER_PROXIES:
        return None
    proxy = random.choice(_SCRAPER_PROXIES)
    # cloudscraper/requests accept 'http'/'https' mapping
    return {"http": proxy, "https": proxy}

# ---------- resilient _fetch_html (drop-in replacement) ----------
@retry(max_attempts=5, backoff=3.0)
def _fetch_html(url: str, prefer_playwright_on_first_try: bool = False, interpreter: str = "js2py") -> str:
    """
    Robust synchronous fetch:
     - Reuses a cloudscraper session (keeps cookies)
     - Rotates user-agents
     - Optional proxy rotation if SCRAPER_PROXIES env var is set
     - Heuristic detection of anti-bot blocks -> falls back to Playwright renderer (sync)
    """
    scraper = _get_shared_scraper(interpreter=interpreter)
    # small jitter before issuing the request (helps avoid very regular patterns)
    time.sleep(random.uniform(0.8, 2.2))

    # Try fast Playwright first if caller asks (for the most JS-correct result)
    if prefer_playwright_on_first_try:
        try:
            rendered = _fetch_rendered_html_sync(url)
            if rendered and not _is_blocked_html(rendered):
                LOG.info("Playwright primary fetch succeeded for %s", url)
                return rendered
            LOG.info("Playwright primary fetch returned blocked/empty; continuing with cloudscraper fallback for %s", url)
        except Exception as e:
            LOG.warning("Playwright primary fetch failed (continuing to cloudscraper) for %s: %s", url, e)

    # Strategy list: try a few different UA / proxy combos before giving up
    strategies = []
    # prefer a random subset of UAs to reduce detectability
    try:
        random_uas = random.sample(_USER_AGENTS, min(len(_USER_AGENTS), 3))
    except Exception:
        random_uas = _USER_AGENTS[:1]
    proxies_list = _SCRAPER_PROXIES or [None]

    for ua in random_uas:
        for _proxy in proxies_list:
            strategies.append((ua, _proxy))

    # ensure at least one default strategy exists
    if not strategies:
        strategies = [(_USER_AGENTS[0], None)]

    last_exc = None
    for idx, (ua, proxy) in enumerate(strategies, start=1):
        headers = {"User-Agent": ua}
        # Merge/override session headers just for this request
        try:
            # attach small jitter between strategy attempts
            time.sleep(random.uniform(0.3, 1.2))

            kwargs = {"headers": headers, "timeout": 45, "allow_redirects": True}
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}

            LOG.debug("Attempt %d/%d fetching %s (ua=%s proxy=%s)", idx, len(strategies), url, ua[:40], bool(proxy))

            resp = scraper.get(url, **kwargs)

            status = getattr(resp, "status_code", None)
            if status in (HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS):
                LOG.warning("Status %s for %s (UA=%s, proxy=%s). Trying alternate strategy.", status, ua, bool(proxy))
                last_exc = ScrapeError(f"HTTP {status}")
                continue

            text = resp.text or ""
            # Quick block detection
            if _is_blocked_html(text):
                LOG.warning("Blocked/Challenge page detected (UA=%s, proxy=%s). Trying alternate strategy.", ua[:40], bool(proxy))
                last_exc = ScrapeError("Blocked by anti-bot")
                continue

            # Good response
            return text

        except requests.exceptions.RequestException as e:
            LOG.warning("requests error for %s with ua=%s proxy=%s -> %s", url, ua[:40], bool(proxy), e)
            last_exc = e
            # small extra wait if proxy errored
            time.sleep(random.uniform(1.0, 2.0))
            continue
        except Exception as e:
            LOG.exception("Unexpected error fetching %s (ua=%s, proxy=%s): %s", url, ua[:40], bool(proxy), e)
            last_exc = e
            continue

    # If we reach here, cloudscraper failed or all strategies looked blocked.
    # Try Playwright (synchronous renderer) as a last-resort
    try:
        LOG.info("All cloudscraper strategies exhausted or blocked for %s; attempting Playwright fallback (sync).", url)
        rendered = _fetch_rendered_html_sync(url)
        if rendered and not _is_blocked_html(rendered):
            LOG.info("Playwright fallback succeeded for %s", url)
            return rendered
        LOG.warning("Playwright fallback returned blocked/empty for %s", url)
    except Exception as e:
        LOG.exception("Playwright fallback failed for %s: %s", url, e)
        last_exc = e

    # Give one last diagnostic log and raise
    err_msg = f"All fetch strategies failed for {url}"
    if last_exc:
        err_msg += f": {last_exc}"
    LOG.error(err_msg)
    raise ScrapeError(err_msg)
# ---------------------------
# Main e-commerce scraper (multi-layer extraction)
# ---------------------------
def scrape_ecommerce(url: str) -> Dict[str, Any]:
    domain = _get_domain_from_url(url)
    if not any(s in domain for s in SUPPORTED_SITES):
        raise NotImplementedError(f"Unsupported site: {domain}. Add to SUPPORTED_SITES.")

    try:
        html = _fetch_html(url)
    except Exception as e:
        raise NoDataError(f"Failed to fetch page: {e}")

    soup = BeautifulSoup(html, "lxml")

    # Block detection
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

                    # images from JSON-LD
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

    # Open Graph / meta tags
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

    # Meta description fallback
    if not product["description"]:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            product["description"] = meta_desc["content"].strip()

    # Site-specific description
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
            # Filter small icons
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

    # -------------------------------------------------------
    # CRITICAL FIX: CURRENT PRICE EXTRACTION
    # -------------------------------------------------------
    if product["current_price"] is None:
        
        # Jumia specific
        if "jumia" in domain:
            selectors = ["span.-b", ".-fs24", ".prc", ".-prc", "div.prc"]
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    # FIX: Use " " separator so ₦45000₦50000 becomes "₦45000 ₦50000"
                    text = el.get_text(" ", strip=True) 
                    p = _parse_price_string(text)
                    if p:
                        product["current_price"] = p
                        break

        # Konga specific
        if "konga" in domain and product["current_price"] is None:
            selectors = ["span._3e_22_199e7", "._3e_22_199e7", "h4._44738_3988u", "div.price", "[class*='price']"]
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    # FIX: Force space between elements inside the tag
                    text = el.get_text(" ", strip=True)
                    
                    # FIX: Use robust parser instead of blind regex replace
                    p = _parse_price_string(text)
                    
                    # FIX: If parser still returns a massive number (concatenation happened in text source), try split
                    if p and p > 100_000_000: 
                        split = _split_concatenated_numeric_token(str(int(p)))
                        if split:
                            p = min(split) # Current price is usually the lower one (discounted)
                            
                    if p:
                        product["current_price"] = p
                        break

        # Generic regex (last resort)
        if product["current_price"] is None:
            page_text = soup.get_text(" ", strip=True) # Use space separator here too!
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
                    # usually the largest price on page is NOT the current price (it might be old price), 
                    # but for generic fallback, it's risky. 
                    # Let's try to pick the most frequent or reasonable one, but max() is the standard fallback behavior.
                    product["current_price"] = max(prices)

    # -------------------------------------------------------
    # PREVIOUS PRICE EXTRACTION (With Sanity Checks)
    # -------------------------------------------------------
    page_text = soup.get_text(" ", strip=True) # Ensure spaces
    
    # Pass current_price to helper for validation
    prev_price = _extract_previous_price(soup, json_ld_data, domain, page_text, product["current_price"])
    
    if prev_price:
        product["previous_price"] = prev_price
        LOG.info("Found previous price: ₦%.0f for %s", prev_price, product["title"])

    # Stock & title fallbacks
    page_text_lower = soup.get_text().lower()
    if any(phrase in page_text_lower for phrase in ["out of stock", "sold out", "unavailable", "not available"]):
        product["stock_status"] = "out_of_stock"

    if product["title"] == "Product" or "Buy" in product["title"]:
        h1 = soup.select_one("h1.-fs20, h1.-pb10, h1.brd, .v-p-hd h1, h1")
        if h1:
            product["title"] = h1.get_text(strip=True)
        elif soup.title:
            product["title"] = soup.title.string.strip()

    if product["current_price"] is None:
        raise NoDataError("No price found after all extraction methods")

    # Clean images & set primary
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

    # Truncate description
    product["description"] = product["description"][:1500].strip()

    product["raw"] = {"json_ld": json_ld_data} if json_ld_data else {"snippet": product["title"]}

    LOG.info("Successfully scraped %s — ₦%.0f — %s (desc len=%d)", product["title"], product["current_price"], domain, len(product["description"]))
    return product


# ---------------------------
# Public scrape_product router
# ---------------------------
def scrape_product(url: str) -> Dict[str, Any]:
    if not url or not isinstance(url, str):
        raise ValueError("Invalid URL")

    url = url.strip()
    low = url.lower()

    if low.startswith("symbol:") or "binance" in _get_domain_from_url(url):
        return _scrape_binance_ref(url)

    return scrape_ecommerce(url)


# ---------------------------
# Compute changes & deal score (unchanged from your original)
# ---------------------------
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

    try:
        old_price_num = float(old_price) if old_price is not None else None
    except (ValueError, TypeError):
        old_price_num = None

    try:
        new_price_num = float(new_price) if new_price is not None else None
    except (ValueError, TypeError):
        new_price_num = None

    if old_price_num != new_price_num:
        changed = True
        what_changed.append("price")
        if old_price_num and old_price_num > 0 and new_price_num is not None:
            price_diff_percent = round(((old_price_num - new_price_num) / old_price_num) * 100, 2)
        else:
            price_diff_percent = 0.0

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


# ---------------------------
# Safe wrapper
# ---------------------------
def safe_scrape_product(url: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    try:
        product = scrape_product(url)
        return True, product, None
    except NoDataError as e:
        LOG.info("NoDataError: %s", e)
        return False, None, str(e)
    except Exception as e:
        LOG.exception("Unexpected scrape error")
        return False, None, f"Error: {e}"

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
    """
    Parse fuel price from FuelPriceWatch live app page.
    Improved change detection: uses label-specific regex ("today" for absolute, "from last period" for percent)
    to avoid picking changes from Diesel/Kerosene sections.
    """
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text("\n", strip=True)

    # Price extraction (unchanged - reliable)
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

    # === IMPROVED CHANGE EXTRACTION ===
    perc_change = "N/A"
    abs_change = "N/A"

    # Specific: percent with "from last period" label (only matches Petrol section)
    perc_m = re.search(r"([+-]\s*[\d\.]+\s*%)\s*from last period", page_text, re.I)
    if perc_m:
        perc_change = perc_m.group(1).strip()

    # Specific: absolute with "today" label (only matches Petrol section)
    abs_m = re.search(r"([+-]\s*₦\s*[\d\.,]+\.?\d*)\s*today", page_text, re.I)
    if abs_m:
        abs_change = abs_m.group(1).strip()

    # Fallback: if labels missing (unlikely), use old context-based method
    if perc_change == "N/A" or abs_change == "N/A":
        # Find context around price for fallback
        price_match = re.search(r"Average\s+Petrol\s+Price", page_text, re.I)
        if price_match:
            start = max(0, price_match.start() - 300)
            end = price_match.end() + 500
            context = page_text[start:end]

            change_matches = re.findall(r"([+-]\s*[\d\.]+\s*%|[+-]?\s*₦?\s*[\d\.,]+\.?\d*)", context, re.I)
            for match in change_matches:
                cleaned = match.strip()
                if "%" in cleaned and perc_change == "N/A":
                    perc_change = cleaned
                elif "₦" in cleaned or cleaned.startswith(("+", "-")) and abs_change == "N/A":
                    abs_change = cleaned

    LOG.info(
        "FuelPriceWatch parsed → %s | Percent: %s | Absolute: %s",
        price_formatted, perc_change, abs_change
    )

    return {
        "source": url,
        "price_raw": v,
        "price_str": price_formatted,
        "change_percent": perc_change,
        "change_absolute": abs_change,
        "last_updated": "Live data",
    }

def _parse_total(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    block = _detect_block(soup)
    if block:
        return {"source": "TotalEnergies (or marketer)", "error": block, "price_raw": None, "price_str": None, "last_updated": None, "raw": None}

    text = soup.get_text(" ", strip=True)
    m = re.search(r"(?:PMS|Petrol|Price).{0,120}?(?:₦|NGN|N)\s*[0-9][0-9,\.]*", text, flags=re.I)
    if m:
        value = _extract_naira_amount(m.group(0))
    else:
        value = _extract_naira_amount(text)

    return {"source": "TotalEnergies (or marketer)", "price_raw": value, "price_str": _format_naira(value) if value else None, "last_updated": None, "raw": None}

def _parse_oando(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    block = _detect_block(soup)
    if block:
        return {"source": "Marketer (Oando/Mobil/etc)", "error": block, "price_raw": None, "price_str": None, "last_updated": None, "raw": None}

    text = soup.get_text(" ", strip=True)
    m = re.search(r"(?:PMS|Petrol|Fuel Price).{0,120}?(?:₦|NGN|N)\s*[0-9][0-9,\.]*", text, flags=re.I)
    if m:
        value = _extract_naira_amount(m.group(0))
    else:
        value = _extract_naira_amount(text)

    return {"source": "Marketer (Oando/Mobil/etc)", "price_raw": value, "price_str": _format_naira(value) if value else None, "last_updated": None, "raw": None}
# ---------------------------
# Async entrypoint for fuel scrapes (uses the sync _fetch_html in executor)
# ---------------------------
FUEL_SITE_SOURCES = [
    {"url": "https://app.fuelpricewatch.com/", "parser": _parse_fuelpricewatch},
    #{"url": "https://www.totalenergies.com.ng/en", "parser": _parse_total},
]

async def scrape_fuel_prices() -> Dict[str, Any]:
    app_url = "https://app.fuelpricewatch.com/"
    
    try:
        # Primary method: Try the live rendered app first (most up-to-date)
        html = await _fetch_rendered_html(app_url)
        result = _parse_fuelpricewatch(html, url=app_url)
        
        if result.get("price_raw") is not None:
            # Success! Return immediately — no need for fallback
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
        LOG.exception(f"Live app scrape failed: {e}")
        LOG.warning("Playwright app failed - falling back to static index")

    # Fallback: Only reached if primary method failed
    index_url = "https://www.fuelpricewatch.com/fuel-price-index-nigeria"
    try:
        loop = asyncio.get_running_loop()
        index_html = await loop.run_in_executor(None, _fetch_html, index_url)  # cloudscraper
        # Note: We pass the original app_url so the source link remains clickable
        # (even though this is the index page, the data is the same)
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
        LOG.exception(f"Index fallback failed: {e}")

    # Ultimate failure case
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

def scrape_lpg_prices() -> Dict[str, Any]:
    """
    Scrape current LPG depot prices from lpginnigeria.com/chart (Markdown-based table).
    Integrates robust parsing: tries HTML table first, then regex fallback for Markdown/text.
    Calculates average depot price per 20MT (excluding 0/invalid), per kg, and Lagos retail estimate.
    """
    try:
        html = _fetch_lpg_html()
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

    # Detect block
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

    # Extract Date (Robust regex handling day name, brackets, commas)
    date_match = re.search(r"(?:\[)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?(?:\])?,\s*\d{1,2}(st|nd|rd|th)?\s+\w+\s*,\s*\d{4}", page_text, re.IGNORECASE)
    last_updated = date_match.group(0).strip("[] ,") if date_match else "Today"

    # Parse Data: Try HTML table first, then fallback to regex on page_text (for Markdown)
    depots = []
    valid_prices = []

    # Attempt HTML Table Parsing (if site reverts to <table>)
    target_table = None
    for table in soup.find_all("table"):
        header_text = table.get_text().lower()
        if "depot" in header_text and "price" in header_text:
            target_table = table
            break

    if target_table:
        for row in target_table.find_all("tr")[1:]:  # Skip header
            cols = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cols) >= 4:  # Expect depot, price, diff, diff%
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

    # Regex Fallback (for current Markdown format)
    if not valid_prices:
        # Matches rows like "Depot Name 16,900,000 +200,000 +1.20%"
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
        LOG.warning("No valid depot prices found after table and regex parsing")
        return {
            "error": "no_valid_prices",
            "avg_depot_20mt": "N/A",
            "avg_depot_per_kg": "N/A",
            "retail_estimate_lagos": "N/A",
            "last_updated": last_updated,
            "source": "https://lpginnigeria.com/chart",
            "depots": depots,
        }

    # Calculations
    avg_20mt = sum(valid_prices) / len(valid_prices)
    per_kg = avg_20mt / 20_000  # 20MT = 20,000 kg

    margin_low = 400
    margin_high = 600
    retail_low = per_kg + margin_low
    retail_high = per_kg + margin_high

    avg_20mt_str = f"₦{int(round(avg_20mt)):,}"
    per_kg_str = f"₦{per_kg:,.2f}"
    retail_range_str = f"₦{int(round(retail_low)):,} – ₦{int(round(retail_high)):,} per kg"

    LOG.info(
        "LPG scraped → Avg 20MT: %s | Per kg: %s | Lagos retail est: %s | Date: %s | Valid depots: %d",
        avg_20mt_str, per_kg_str, retail_range_str, last_updated, len(valid_prices)
    )

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
        "note": "Lagos retail estimate calculated as: average depot price per kg + ₦400–600/kg typical markup (for transport, bottling, dealer margin, etc.)",
    }

def _is_pdf_link(href: str) -> bool:
    """
    Return True if href looks like a PDF link (ignores query/hash).
    """
    if not href:
        return False
    href_clean = href.split('?', 1)[0].split('#', 1)[0].lower()
    return href_clean.endswith(".pdf")


def _get_pdf_head_info(url: str, timeout: float = 8.0) -> dict:
    """
    Lightweight HEAD probe for PDFs. Returns dict with keys:
      - ok: bool
      - content_type: str or None
      - content_length: int or None
      - final_url: str (after redirects)
      - status_code: int or None
      - error: str if any
    Uses HEAD first, falls back to GET(stream=True) when necessary.
    """
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        status = getattr(resp, "status_code", None)
        content_type = (resp.headers.get("Content-Type") or "").lower()

        # If HEAD isn't convincing, try a small GET (streamed so we don't download body)
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

        # Close streaming response to free connection
        try:
            if getattr(resp, "raw", None):
                resp.close()
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

def extract_anchors_from_soup(
    target_soup: BeautifulSoup,
    base_url: str,
    seen: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Robust replacement for the inline 'anchors -> items' block you had.
    Returns a list of dict items with keys:
      title, snippet, date, link, origin, detail_available, pdf, pdf_url, pdf_size_kb, anchor (the Tag)
    NOTE: This function uses module-level regexes: _SCHOOL_KEYWORDS_RE and _DATE_RE.
    """
    if seen is None:
        seen = set()

    anchors: List[Dict[str, Any]] = []

    for a in target_soup.find_all("a", href=True):
        try:
            link_text = a.get_text(" ", strip=True)
        except Exception:
            link_text = ""
        href = a.get("href", "").strip()

        # Basic filtering
        if not link_text or len(link_text) < 6:
            continue

        # prioritize anchors with keywords or PDFs/docs
        try:
            matches_keyword = bool(_SCHOOL_KEYWORDS_RE.search(link_text) if _SCHOOL_KEYWORDS_RE else False) \
                              or bool(_SCHOOL_KEYWORDS_RE.search(href) if _SCHOOL_KEYWORDS_RE else False)
        except Exception:
            matches_keyword = False

        if not (matches_keyword or href.lower().endswith((".pdf", ".doc", ".docx"))):
            continue

        full_link = urljoin(base_url, href)
        key = f"{link_text[:100]}|{full_link[:200]}"
        if key in seen:
            continue
        seen.add(key)

        item: Dict[str, Any] = {
            "title": link_text,
            "snippet": "",
            "date": None,
            "link": full_link,
            "origin": "link",
            "detail_available": False,
            "pdf": False,
            "pdf_url": None,
            "pdf_size_kb": None,
            "anchor": a,  # keep the Tag for possible follow-up processing
        }

        # try to find date near the link (parent p/div/li/article)
        parent_block = a.find_parent(["p", "div", "li", "article"])
        if parent_block is not None:
            try:
                text_block = parent_block.get_text(" ", strip=True)
                if _DATE_RE:
                    date_match = _DATE_RE.search(text_block)
                    if date_match:
                        item["date"] = date_match.group(0)
            except Exception:
                pass

        # If PDF link -> probe it and annotate item accordingly (no full download)
        if _is_pdf_link(full_link):
            head = _get_pdf_head_info(full_link)
            if head.get("ok") and head.get("content_type") and "pdf" in (head.get("content_type") or ""):
                item["pdf"] = True
                item["pdf_url"] = head.get("final_url") or full_link
                if head.get("content_length"):
                    item["pdf_size_kb"] = round(head["content_length"] / 1024)
                    item["snippet"] = f"(PDF — {item['pdf_size_kb']} KB) — open link to view"
                else:
                    item["snippet"] = "(PDF — open link to view)"
            else:
                # If the HEAD/GET check failed, still include link but note probe failure
                err = head.get("error") or f"status={head.get('status_code')}"
                item["snippet"] = f"(PDF link — probe failed: {err})"
            anchors.append(item)
            continue

        # Non-PDF: give a small snippet based on nearby content
        snippet = ""
        if parent_block is not None:
            container_text = parent_block.get_text(" ", strip=True)

            # 1) Try next-sibling descriptive elements
            siblings = list(a.find_next_siblings(["p", "div", "span"]))
            if not siblings:
                # maybe anchor inside a heading; check heading siblings
                parent_heading = a.find_parent(["h1", "h2", "h3", "h4", "h5"])
                if parent_heading:
                    siblings = list(parent_heading.find_next_siblings(["p", "div", "span"]))

            for sib in siblings:
                try:
                    sib_text = sib.get_text(" ", strip=True)
                except Exception:
                    sib_text = ""
                if len(sib_text) > 20 and sib_text != item.get("date"):
                    snippet = sib_text[:300]
                    break

            # 2) Fallback: parent's text minus title & date
            if not snippet and len(container_text) > len(link_text) + 20:
                clean_text = container_text.replace(link_text, "").replace("Read More", "").strip()
                if item.get("date"):
                    clean_text = clean_text.replace(item["date"], "").strip()
                if len(clean_text) > 20:
                    snippet = clean_text[:300]

        if not snippet:
            snippet = "Open link for full update details."

        item["snippet"] = snippet
        anchors.append(item)

    return anchors


def _extract_preview_from_html(html: str, base_url: Optional[str] = None) -> str:
    """
    Pick the best preview snippet from an article page:
      1) <article> text
      2) first long <div class="content"> or similar
      3) first long <p>
      4) meta description
    Returns a short cleaned snippet (or empty string).
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")

    def _clean_and_truncate(s: str, limit: int = 800) -> str:
        txt = " ".join((s or "").split())
        return txt[:limit]

    # 1) article
    article = soup.find("article")
    if article:
        text = article.get_text(" ", strip=True)
        if len(text) > 60:
            return _clean_and_truncate(text)

    # 2) common content containers
    for sel in ("div.content", "div.article-body", "div.post-content", "main", "section.article"):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) > 60:
                return _clean_and_truncate(text)

    # 3) first paragraph with reasonable length
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if len(txt) >= 60:
            return _clean_and_truncate(txt)

    # 4) meta description
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        return _clean_and_truncate(meta["content"])

    # fallback: page text
    page_text = soup.get_text(" ", strip=True)
    return _clean_and_truncate(page_text) if page_text and len(page_text) > 20 else ""


def _extract_snippets_from_html(html: str, base_url: str, follow_links: bool = False, max_follow: int = 2) -> List[Dict[str, Any]]:
    """
    Extracts title, link, date, and update details (snippet) from HTML.
    Prioritizes extracting the summary text directly from the listing page.

    Returns up to 15 items with keys: title, snippet, date, link, pdf
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    items: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    # Identify the Main News Container (optional but helps precision)
    news_selectors = [
        "section#news", ".news-list", ".blog-posts", ".latest-news",
        "div.news", "ul.news", "div.posts", "#main-content", "article"
    ]
    target_area = soup
    for sel in news_selectors:
        found = soup.select(sel)
        if found:
            # pick the one with the most text content to be safe
            target_area = max(found, key=lambda t: len(t.get_text() or ""))
            break

    # Iterate anchors
    for a in target_area.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        href = a.get("href", "").strip()

        # Skip if title too short and doesn't contain school keywords
        if not title or len(title) < 10:
            low = title.lower() if title else ""
            if any(x in low for x in ("read", "view", "click")):
                prev_header = a.find_previous(["h1", "h2", "h3", "h4", "h5"])
                if prev_header and len(prev_header.get_text(strip=True)) > 10:
                    title = prev_header.get_text(" ", strip=True)
                else:
                    continue
            else:
                if not (_SCHOOL_KEYWORDS_RE.search(title) if _SCHOOL_KEYWORDS_RE else False) and \
                   not (_SCHOOL_KEYWORDS_RE.search(href) if _SCHOOL_KEYWORDS_RE else False):
                    continue

        full_link = urljoin(base_url, href)
        key = f"{title[:50]}|{full_link}"
        if key in seen:
            continue
        seen.add(key)

        snippet = ""
        date_str = None

        container = a.find_parent(["li", "div", "article", "p"])
        if container is not None:
            container_text = container.get_text(" ", strip=True)

            # A) Date
            try:
                if _DATE_RE:
                    date_match = _DATE_RE.search(container_text)
                    if date_match:
                        date_str = date_match.group(0)
            except Exception:
                date_str = None

            # B) Snippet: siblings first
            siblings = list(a.find_next_siblings(["p", "div", "span"]))
            if not siblings:
                parent_heading = a.find_parent(["h1", "h2", "h3", "h4", "h5"])
                if parent_heading:
                    siblings = list(parent_heading.find_next_siblings(["p", "div", "span"]))

            for sib in siblings:
                try:
                    sib_text = sib.get_text(" ", strip=True)
                except Exception:
                    sib_text = ""
                if len(sib_text) > 20 and sib_text != date_str:
                    snippet = sib_text[:300]
                    break

            # C) Fallback: parent's text minus title/date
            if not snippet and len(container_text) > len(title) + 20:
                clean_text = container_text.replace(title, "").replace("Read More", "").strip()
                if date_str:
                    clean_text = clean_text.replace(date_str, "").strip()
                if len(clean_text) > 20:
                    snippet = clean_text[:300]

        # PDF flag
        is_pdf = _is_pdf_link(full_link) or full_link.lower().endswith(".pdf")
        if is_pdf:
            snippet = f"[PDF Document] {snippet}".strip()

        if not snippet:
            snippet = "Open link for full update details."

        item = {
            "title": title,
            "snippet": snippet,
            "date": date_str,
            "link": full_link,
            "pdf": is_pdf
        }
        items.append(item)

    # Sort: items with dates first, then longer snippets first
    items.sort(key=lambda x: (x['date'] is None, -len(x.get('snippet') or "")))

    return items[:15]