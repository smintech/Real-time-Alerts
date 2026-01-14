import os
import time
import logging
from typing import Dict, Optional, Any, Tuple, Callable
from functools import wraps
import requests
from difflib import SequenceMatcher
import asyncio
from apify_client import ApifyClient
from urllib.parse import urlparse
import re
from bs4 import BeautifulSoup
from apify_client.errors import ApifyApiError
from Utils.config import SITE_ACTOR_MAP
# Settings / thresholds (imported from your project)
from bot.settings import (
    SUPPORTED_SITES,
    HIGH_DEAL_THRESHOLD,
    MEDIUM_DEAL_THRESHOLD,
    LOW_DEAL_THRESHOLD,
    MIN_CHANGE_TO_ALERT,
)

# Initialize logger
logger = logging.getLogger("apify_scraper")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(ch)

# Initialize Apify client (use token from config or env)
APIFY_TOKEN = os.getenv("APIFY_TOKEN")  # you can also import from config
if not APIFY_TOKEN:
    logger.warning("APIFY_TOKEN is not set. Apify calls will fail until token is provided.")
client = ApifyClient(APIFY_TOKEN) if APIFY_TOKEN else None


# ---------------------------
# Custom Exceptions
# ---------------------------
class ScrapeError(Exception):
    """Generic scraping failure (non-retryable)."""

class ApifyError(Exception):
    """Apify actor/dataset related failure (may be retryable)."""
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class NoDataError(ScrapeError):
    """Raised when a scrape completes but yields no data."""

# ---------------------------
# Utility: retry decorator
# ---------------------------
def retry(max_attempts: int = 3, backoff: float = 1.5, allowed_exceptions: Tuple = (Exception,)):
    """
    Simple retry decorator with exponential backoff.
    Only retry when allowed_exceptions are raised.
    """
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except allowed_exceptions as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.debug(f"Function {fn.__name__} failed after {attempt} attempts.")
                        raise
                    sleep_for = backoff * (2 ** (attempt - 1))
                    logger.warning(f"{fn.__name__} attempt {attempt} failed with {e!r}; retrying in {sleep_for:.1f}s...")
                    time.sleep(sleep_for)
        return wrapper
    return decorator


# ---------------------------
# Core scraping function (Apify helper already present)
# ---------------------------

@retry(max_attempts=3, backoff=2, allowed_exceptions=(ApifyError, ConnectionError, TimeoutError, OSError))
def _call_apify_actor_and_get_items(actor_id: str, run_input: Dict[str, Any]) -> list:
    """
    Run Apify actor and return dataset items.
    - If actor not found -> raise ApifyError(..., retryable=False)
    - Other Apify errors remain retryable
    """
    if client is None:
        raise ApifyError("Apify client not initialized (APIFY_TOKEN missing)", retryable=False)

    try:
        logger.info("Starting Apify actor %s with input keys: %s", actor_id, list(run_input.keys()))
        run = client.actor(actor_id).call(run_input=run_input)
    except Exception as e:
        # Detect Apify "actor not found" and avoid retries for that case
        err_msg = str(e)
        logger.exception("Apify actor call failed for %s: %s", actor_id, err_msg)
        # common Apify message: "Actor with this name was not found"
        if "Actor with this name was not found" in err_msg or "Actor with id" in err_msg or "Not Found" in err_msg:
            # do not retry: configuration error (actor name wrong/removed)
            raise ApifyError(f"Apify actor not found: {actor_id}", retryable=False)
        # otherwise treat as transient
        raise ApifyError(f"Apify actor call failed: {err_msg}", retryable=True)

    # Validate run result
    dataset_id = run.get("defaultDatasetId") or run.get("defaultDataset", {}).get("id")
    if not dataset_id:
        logger.error("Apify actor returned no dataset id in run result: %s", run)
        raise ApifyError("Apify actor run did not provide a dataset id", retryable=False)

    try:
        logger.info("Pulling dataset items from dataset %s", dataset_id)
        dataset_items = client.dataset(dataset_id).list_items()
    except Exception as e:
        logger.exception("Failed to list dataset items")
        raise ApifyError(f"Failed to read dataset items: {e}", retryable=True)

    items = dataset_items.get("items") if isinstance(dataset_items, dict) else getattr(dataset_items, "items", None)
    if items is None:
        if isinstance(dataset_items, list):
            items = dataset_items
        else:
            logger.error("Dataset list_items returned unexpected structure: %s", type(dataset_items))
            raise ApifyError("Unexpected dataset response structure", retryable=False)

    return items


# ---------------------------
# Helpers: domain parsing, normalization, product key
# ---------------------------
def _get_domain_from_url(u: str) -> str:
    try:
        p = urlparse(u)
        host = p.netloc.lower()
        return host.replace("www.", "")
    except Exception:
        return ""

def _slugify(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (s or "").lower()).strip('-')

def _best_identifier(raw: Dict[str, Any]) -> Optional[str]:
    """
    Try common canonical fields present in many scrapers: sku, model, upc, ean, mpn, id, product_code.
    """
    if not raw or not isinstance(raw, dict):
        return None
    for key in ("sku", "model", "upc", "ean", "mpn", "item_id", "id"):
        v = raw.get(key)
        if v:
            return str(v).strip().lower()
    # sometimes nested under raw payload
    nested = raw.get("raw") if isinstance(raw.get("raw"), dict) else None
    if nested:
        for key in ("sku", "model", "product_code", "mpn", "id"):
            v = nested.get(key)
            if v:
                return str(v).strip().lower()
    return None

def normalize_product_key(scrape_result: Dict[str, Any]) -> str:
    """
    Best-effort unique key for same product across sites.
    Order:
      1. canonical id fields (sku/model/upc/...)
      2. slug(title)
      3. fallback to site+slug(title)
    """
    raw = scrape_result.get("raw") or {}
    ident = _best_identifier(scrape_result) or _best_identifier(raw)
    if ident:
        return f"ID::{ident}"
    title = (scrape_result.get("title") or (raw.get("name") if isinstance(raw, dict) else "") or "")
    slug = _slugify(title)
    if slug:
        return f"SLUG::{slug}"
    # final fallback
    site = scrape_result.get("site") or scrape_result.get("url") or "unknown"
    return f"UNK::{site}::{int(scrape_result.get('current_price') or 0)}"


# ---------------------------
# Adapters
# ---------------------------
def scrape_ordinary_website(url: str, timeout: int = 15) -> Dict[str, Any]:
    """
    Robust fallback scraper with specific support for Jumia HTML structure.
    """
    domain = _get_domain_from_url(url)
    
    # 1. Use Real Browser Headers to avoid immediate 403 blocks
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        # Use a Session for better connection handling
        session = requests.Session()
        resp = session.get(url, headers=headers, timeout=timeout)
        
        # Check for non-200 or blocking
        if resp.status_code in [403, 503]:
            # This is the most common reason for failure on Render
            raise NoDataError(f"Access Denied (Anti-Bot Block) HTTP {resp.status_code} for {url}")
        
        if resp.status_code != 200:
            raise NoDataError(f"HTTP {resp.status_code} for {url}")
            
        html = resp.text
        
    except Exception as e:
        logger.error(f"Ordinary site fetch failed for {url}: {e}")
        raise NoDataError(f"Connection failed: {e}")

    # 2. Parse HTML
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # --- BLOCK DETECTION ---
    title_text = soup.title.get_text().lower() if soup.title else ""
    if "just a moment" in title_text or "verify" in title_text or "challenge" in title_text:
        raise NoDataError(f"Request was challenged by Cloudflare/Anti-Bot: {url}")

    # --- TITLE EXTRACTION ---
    title = None
    # Jumia specific: Title is often in h1.-fs20 or similar
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    
    if not title:
        # Fallback to meta tags
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = og_title.get("content")

    # --- PRICE EXTRACTION (Jumia Focused) ---
    current_price = None
    
    # STRATEGY A: Jumia Specific Classes (Look for bold text usually containing currency)
    # Jumia often puts price in a span with class "-b" inside a div
    # Example: <div class="df ..."><span class="-b -ltr -tal -fs24">₦ 2,980</span></div>
    if "jumia" in domain:
        # Try finding the specific price container class often used by Jumia
        # Note: These class names like '-fs24' might change, but '-b' (bold) is sticky
        price_spans = soup.select("span.-b") 
        for span in price_spans:
            text = span.get_text(strip=True)
            if "₦" in text or "NGN" in text:
                # Clean and parse
                clean_text = re.sub(r"[^\d.]", "", text.replace(",", ""))
                if clean_text:
                    try:
                        current_price = float(clean_text)
                        break # Found it
                    except ValueError:
                        continue
    
    # STRATEGY B: Generic Regex Fallback (if Jumia strategy failed)
    if current_price is None:
        text_blob = soup.get_text(" ")
        # Regex to find NGN 1,200 or ₦ 1,200
        # Matches: "₦ 2,980", "₦2,980", "NGN 2980"
        match = re.search(r"(?:₦|NGN)\s?([\d,]+(?:\.\d{2})?)", text_blob)
        if match:
            clean_text = match.group(1).replace(",", "")
            try:
                current_price = float(clean_text)
            except ValueError:
                pass

    # --- STOCK STATUS ---
    # Jumia usually puts "Out of Stock" in a button or warning message
    stock = "available"
    page_text_lower = soup.get_text().lower()
    if "out of stock" in page_text_lower or "sold out" in page_text_lower:
        stock = "out_of_stock"
    elif "currently unavailable" in page_text_lower:
        stock = "out_of_stock"

    # Validate result
    if current_price is None:
        # If we have HTML but no price, dumping a small snippet to logs helps debug
        logger.warning(f"HTML fetched but no price found for {url}. Title: {title}")
        # Return what we have, even if price is None (bot can handle None)
    
    return {
        "title": title,
        "current_price": current_price,
        "previous_price": None,
        "discount_percent": None,
        "stock_status": stock,
        "url": url,
        "site": domain,
        "currency": "NGN",
        "raw": {"snippet": title},
    }

def _apify_product_scrape_for_domain(domain: str, url: str) -> Dict[str, Any]:
    """
    Robust handling for Jumia actor output:
    - Accepts list of strings for startUrls (fixes "No start URLs" error)
    - Handles multi-item results (search/listing pages) by selecting lowest price
    - Normalizes fields from your actor output (priceNumeric, oldPriceNumeric, etc.)
    - Graceful fallback to HTML scraper
    """
    actor_id = SITE_ACTOR_MAP.get(domain)
    if not actor_id:
        for k, v in SITE_ACTOR_MAP.items():
            if k in domain and v:
                actor_id = v
                break

    if not actor_id:
        logger.warning("No Apify actor mapped for %s — falling back to ordinary scraper", domain)
        return scrape_ordinary_website(url)

    # Correct input format for fatihtahta/jumia-scraper and similar
    run_input = {
        "startUrls": [url],          # ← List of strings – fixes the actor error
        "maxListings": 10,           # Optional: limit results
        "get_reviews": False,
        "image_resolution": "low",
    }

    try:
        items = _call_apify_actor_and_get_items(actor_id, run_input)
    except ApifyError as ae:
        logger.warning("ApifyError for actor %s: %s — using HTML fallback", actor_id, ae)
        return scrape_ordinary_website(url)
    except Exception as e:
        logger.exception("Unexpected Apify call error for %s — fallback", url)
        return scrape_ordinary_website(url)

    if not items:
        raise NoDataError(f"No data extracted for {url} (actor {actor_id})")

    logger.info("Actor returned %d items for %s", len(items), url)

    # HANDLE MULTI-ITEM RESULTS (your actor output shows search page with many products)
    valid_items = [
        it for it in items
        if it.get("priceNumeric") is not None or it.get("price_ngn") is not None
    ]

    if not valid_items:
        raise NoDataError(f"No priced items found on {url}")

    # Select the cheapest product (best deal for channel)
    best_item = min(
        valid_items,
        key=lambda x: float(x.get("priceNumeric") or x.get("price_ngn") or float("inf"))
    )

    raw = best_item

    # Normalize from your exact actor fields
    current_price = raw.get("priceNumeric") or raw.get("price_ngn")
    previous_price = raw.get("oldPriceNumeric") or raw.get("old_price_ngn")
    discount = raw.get("discountText") or raw.get("discount")

    title = raw.get("title") or raw.get("name") or "Product"

    return {
        "title": title.strip(),
        "current_price": float(current_price) if current_price is not None else None,
        "previous_price": float(previous_price) if previous_price is not None else None,
        "discount_percent": discount,
        "stock_status": "available",  # Actor doesn't flag OOS reliably
        "url": url,  # Original search URL – or use raw.get("url") for specific product link if available
        "site": domain,
        "currency": "NGN",  # Hardcode – actor sometimes mangles it ("时")
        "raw": raw,
    }

def _scrape_binance_ref(ref: str) -> Dict[str, Any]:
    """
    Resolve a Binance symbol from a URL or 'SYMBOL:BTCUSDT' style string and fetch the public ticker price.
    Returns normalized dict (currency = USDT).
    """
    symbol = None
    # Accept "SYMBOL:BTCUSDT"
    if isinstance(ref, str) and ref.upper().startswith("SYMBOL:"):
        symbol = ref.split(":", 1)[1].strip().upper()
    else:
        try:
            p = urlparse(ref)
            # look for symbol= in query
            q = p.query or ""
            for kv in q.split("&"):
                if kv.startswith("symbol="):
                    symbol = kv.split("=", 1)[1].upper()
                    break
            # fallback: /trade/BTC_USDT or similar in path
            if not symbol:
                m = re.search(r'/trade/([A-Z0-9_]+)', p.path or "")
                if m:
                    symbol = m.group(1).replace("_", "").upper()
        except Exception:
            symbol = None

    if not symbol:
        raise ValueError("Could not determine Binance symbol from: " + str(ref))

    # public endpoint ticker
    try:
        resp = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=10)
        if resp.status_code != 200:
            logger.error("Binance API returned %s for symbol %s", resp.status_code, symbol)
            raise RuntimeError(f"Binance API error: status {resp.status_code}")
        data = resp.json()
        price_val = float(data.get("price"))
    except Exception as e:
        logger.exception("Failed fetching Binance ticker for %s: %s", symbol, e)
        raise

    return {
        "title": symbol,
        "current_price": price_val,
        "previous_price": None,
        "discount_percent": None,
        "stock_status": "available",
        "url": ref,
        "site": "binance.com",
        "currency": "USDT",
        "raw": data,
    }


# ---------------------------
# Public scrape_product (router) — replaced single-site-only behavior
# ---------------------------
def scrape_product(url: str) -> Dict[str, Any]:
    """
    Scrape a single product reference (URL or symbol).
    Routes to:
      - Binance REST adapter if domain contains binance or ref like 'SYMBOL:...'
      - Apify actor if SITE_ACTOR_MAP has a mapping for domain
    Returns normalized product dict similar to previous shape.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")

    raw_ref = url.strip()
    low = raw_ref.lower()

    # quick validation against SUPPORTED_SITES (config-driven)
    if not any(site in low for site in SUPPORTED_SITES):
        # allow binance symbol style even if not in SUPPORTED_SITES
        if not raw_ref.upper().startswith("SYMBOL:") and "binance" not in low:
            raise ValueError(f"Unsupported site in config. Supported: {SUPPORTED_SITES}. Got: {url}")

    domain = _get_domain_from_url(raw_ref)

    try:
        # Binance path
        if raw_ref.upper().startswith("SYMBOL:") or "binance" in domain:
            product = _scrape_binance_ref(raw_ref)
            logger.info("Scraped Binance symbol %s price=%s", product.get("title"), product.get("current_price"))
            return product

        # Try Apify actor mapping from config
        actor_domain = None
        # exact domain lookup
        if domain in SITE_ACTOR_MAP and SITE_ACTOR_MAP.get(domain):
            actor_domain = domain
        else:
            # fallback: find any configured map whose key is contained in the URL
            for key in SITE_ACTOR_MAP:
                if key in low and SITE_ACTOR_MAP.get(key):
                    actor_domain = key
                    break

        if actor_domain:
            product = _apify_product_scrape_for_domain(actor_domain, raw_ref)
            logger.info("Scraped product '%s' from %s price=%s", product.get("title"), actor_domain, product.get("current_price"))
            return product

        # Not implemented for this URL
        raise NotImplementedError(f"No scraper available for {url} — ensure SITE_ACTOR_MAP contains an actor for the domain")

    except ApifyError:
        # bubble up ApifyError unchanged
        raise
    except NoDataError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error during scraping for %s", url)
        raise ApifyError(f"Unexpected error during scraping: {exc}", retryable=True)


# ---------------------------
# Compute changes (defensive)
# ---------------------------
def compute_changes(old_data: Optional[Dict], new_data: Dict) -> Dict[str, Any]:
    """
    Compute differences between old and new snapshots.
    Returns a dict describing what changed and whether it's significant.

    - If old_data is None -> new product
    - Price diff percent: positive => price drop (old > new)
    """
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

    # Defensive: ensure numeric prices if possible
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
            # Unable to compute meaningful percentage
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


# ---------------------------
# Deal scoring (defensive)
# ---------------------------
def calculate_deal_score(price_diff_percent: Optional[float], historical_avg: Optional[float] = None) -> str:
    """
    Deal scoring using thresholds.
    Returns one of: "high", "medium", "low", "none"
    """
    try:
        drop = float(price_diff_percent or 0.0)
    except (ValueError, TypeError):
        drop = 0.0

    # Only positive drops (price went down) count as deals
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
# Phase 2 stubs (clear predictable failure)
# ---------------------------
def scrape_fuel_prices(state: Optional[str] = None) -> Dict:
    """
    Placeholder — Phase 2: fuel price scraping
    Predictable failure: not implemented yet.
    """
    raise NotImplementedError("Fuel scraping coming in Month 2")


def scrape_electricity_tariffs() -> Dict:
    raise NotImplementedError("Tariff scraping coming in Month 2")


# ---------------------------
# Small example helper: safe wrapper for calling and logging
# ---------------------------
def safe_scrape_product(url: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Convenience wrapper used by callers that prefer error-safe returns
    Returns: (ok: bool, data: dict|None, error_msg: str|None)
    """
    try:
        product = scrape_product(url)
        return True, product, None
    except NoDataError as e:
        logger.info("NoDataError: %s", e)
        return False, None, str(e)
    except ApifyError as e:
        logger.error("ApifyError: %s", e)
        return False, None, str(e)
    except Exception as e:
        logger.exception("Unhandled error in safe_scrape_product")
        return False, None, f"unexpected error: {e}"