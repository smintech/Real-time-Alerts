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
from apify_client import ApifyApiError
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
def scrape_ordinary_website(url: str, timeout: int = 12) -> Dict[str, Any]:
    """
    Best-effort fallback scraper for ordinary websites (Shopify, WooCommerce, static HTML).
    Returns normalized dict in the same structure as Apify adapters — fields may be None.
    """
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (compatible)"})
        if resp.status_code != 200:
            raise NoDataError(f"HTTP {resp.status_code} for {url}")
        html = resp.text
    except Exception as e:
        logger.exception("Ordinary site fetch failed for %s: %s", url, e)
        raise NoDataError(f"Failed to fetch {url}: {e}")

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Title heuristics
    title = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
        if og and og.get("content"):
            title = og.get("content").strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    # Price heuristics: search for currency symbols and numeric patterns nearby
    text = soup.get_text(" ")
    price_candidates = []
    # look for ₦, NGN, $ and numeric groups
    for match in re.finditer(r"(?:₦|NGN|NGN\s|NGN\.|₦\s?)\s*[\d,]+(?:\.\d+)?|[$]\s*[\d,]+(?:\.\d+)?", text):
        price_str = match.group(0)
        price_candidates.append(price_str)

    # fallback: look for common price selectors
    if not price_candidates:
        selectors = [
            "[class*=price]", "[id*=price]", "[class*=amount]", "[id*=amount]",
            ".product-price", ".price", ".selling-price"
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                price_candidates.append(el.get_text(" ", strip=True))

    def _parse_price(s: str) -> Optional[float]:
        if not s:
            return None
        # Remove currency tokens, non-digit except dot and comma
        s = s.replace("NGN", "").replace("₦", "").replace("$", "")
        s = re.sub(r"[^\d\.,]", "", s)
        s = s.replace(",", "")
        try:
            return float(s)
        except Exception:
            return None

    price_val = None
    for pc in price_candidates:
        parsed = _parse_price(pc)
        if parsed is not None:
            price_val = parsed
            break

    # stock heuristics
    page_text = text.lower()
    if "out of stock" in page_text or "sold out" in page_text:
        stock = "out_of_stock"
    elif "limited" in page_text or "only" in page_text and "left" in page_text:
        stock = "limited"
    else:
        stock = "available"

    return {
        "title": title,
        "current_price": price_val,
        "previous_price": None,
        "discount_percent": None,
        "stock_status": stock,
        "url": url,
        "site": _get_domain_from_url(url),
        "currency": "NGN",
        "raw": {"html_snippet": (title or "")[:300]},
    }

def _apify_product_scrape_for_domain(domain: str, url: str) -> Dict[str, Any]:
    """
    Use SITE_ACTOR_MAP to call an Apify actor, but gracefully fallback
    to scrape_ordinary_website if actor is missing or Apify returns a non-retryable error.
    """
    actor_id = SITE_ACTOR_MAP.get(domain)
    if not actor_id:
        # try partial match
        for k, v in SITE_ACTOR_MAP.items():
            if k in domain and v:
                actor_id = v
                break

    if not actor_id:
        # No actor configured — fallback to ordinary scraper
        logger.warning("No Apify actor mapped for %s — falling back to ordinary HTML scraper", domain)
        return scrape_ordinary_website(url)

    run_input = {
        "scrape_type": "product",
        "product_urls": [url],
        "get_reviews": False,
        "image_resolution": "low",
    }

    try:
        items = _call_apify_actor_and_get_items(actor_id, run_input)
    except ApifyError as ae:
        # If actor not found (retryable==False) or persistent Apify error -> fallback
        logger.warning("ApifyError for actor %s: %s (retryable=%s) — using HTML fallback", actor_id, ae, getattr(ae, "retryable", None))
        return scrape_ordinary_website(url)
    except Exception as e:
        logger.exception("Unexpected error when calling apify actor %s — falling back to HTML scraper", actor_id)
        return scrape_ordinary_website(url)

    if not items:
        raise NoDataError(f"No data extracted for {url} (actor {actor_id})")

    raw = items[0]
    if not isinstance(raw, dict):
        raise NoDataError(f"Dataset item for {url} is not a dict")

    price_info = raw.get("price") or {}
    current_price = price_info.get("price_ngn") or price_info.get("price") or raw.get("price") or None
    previous_price = price_info.get("old_price_ngn") or price_info.get("old_price") or None

    stock_status = "available" if current_price is not None else "out_of_stock"
    title = raw.get("name") or raw.get("title") or raw.get("product_name") or None

    return {
        "title": title,
        "current_price": current_price,
        "previous_price": previous_price,
        "discount_percent": price_info.get("discount"),
        "stock_status": stock_status,
        "url": url,
        "site": domain,
        "currency": price_info.get("currency") or "NGN",
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