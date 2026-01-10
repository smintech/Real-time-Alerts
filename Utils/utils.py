import os
import time
import logging
from typing import Dict, Optional, Any, Tuple, Callable
from functools import wraps
import requests
from difflib import SequenceMatcher
import asyncio
from apify_client import ApifyClient

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
# Core scraping function
# ---------------------------
@retry(max_attempts=3, backoff=2, allowed_exceptions=(ApifyError, ConnectionError, TimeoutError, OSError))
def _call_apify_actor_and_get_items(actor_id: str, run_input: Dict[str, Any]) -> list:
    """
    Internal helper — runs an Apify actor and returns list of dataset items.
    Retries on transient errors (network/Apify temporary errors).
    Raises ApifyError on non-recoverable problems.
    """
    if client is None:
        raise ApifyError("Apify client not initialized (APIFY_TOKEN missing)", retryable=False)

    try:
        logger.info("Starting Apify actor %s with input keys: %s", actor_id, list(run_input.keys()))
        run = client.actor(actor_id).call(run_input=run_input)
    except Exception as e:
        # Apify client throws generic exceptions for many issues; consider these transient
        logger.exception("Apify actor call failed")
        raise ApifyError(f"Apify actor call failed: {e}", retryable=True)

    # Validate run result
    dataset_id = run.get("defaultDatasetId") or run.get("defaultDataset", {}).get("id")
    if not dataset_id:
        # Some actors may return dataset id under different keys — defensive
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
        # Some Apify client versions return a list directly
        if isinstance(dataset_items, list):
            items = dataset_items
        else:
            logger.error("Dataset list_items returned unexpected structure: %s", type(dataset_items))
            raise ApifyError("Unexpected dataset response structure", retryable=False)

    return items


def scrape_product(url: str) -> Dict[str, Any]:
    """
    Scrape a single product URL using the best available Apify actor for Jumia.
    Returns a normalized product dict or raises an exception on failure.

    Raises:
      - ValueError for invalid input
      - ApifyError for actor/dataset related problems
      - NoDataError when actor returned no items
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")

    url_lower = url.lower().strip()

    # Validate supported site
    if not any(site in url_lower for site in SUPPORTED_SITES):
        raise ValueError(f"Unsupported site in MVP. Supported: {SUPPORTED_SITES}. Got: {url}")

    if "jumia.com.ng" not in url_lower:
        # Keep clear and explicit — avoid partial behavior for unsupported sites
        raise NotImplementedError("Only Jumia.ng fully implemented in MVP")

    actor_id = "buseta/jumia-advanced-scraper"
    run_input = {
        "scrape_type": "product",
        "product_urls": [url],
        "get_reviews": False,
        # Keep fast & cheap for price monitoring
        "image_resolution": "low",
    }

    try:
        items = _call_apify_actor_and_get_items(actor_id, run_input)
    except ApifyError as e:
        # Bubble up Apify problems with context
        logger.error("ApifyError while scraping product: %s (retryable=%s)", e, getattr(e, "retryable", False))
        raise
    except Exception as e:
        logger.exception("Unexpected error during Apify scraping")
        raise ApifyError(f"Unexpected error during scraping: {e}", retryable=True)

    if not items:
        logger.info("Apify actor returned empty items for %s", url)
        raise NoDataError(f"No data extracted for {url}")

    # We expect a single product entry
    raw = items[0]
    if not isinstance(raw, dict):
        logger.warning("Unexpected item type from dataset: %s", type(raw))
        raise NoDataError(f"Dataset item for {url} is not a dict")

    # Defensive extraction of price info
    price_info = raw.get("price") or {}
    current_price = price_info.get("price_ngn") or price_info.get("price") or None
    previous_price = price_info.get("old_price_ngn") or price_info.get("old_price") or None

    # stock inference
    stock_status = "available" if current_price is not None else "out_of_stock"

    # Normalize title safely
    title = raw.get("name") or raw.get("title") or raw.get("product_name") or None

    product = {
        "title": title,
        "current_price": current_price,
        "previous_price": previous_price,
        "discount_percent": price_info.get("discount"),
        "stock_status": stock_status,
        "url": url,
        "raw": raw,  # keep raw payload for debugging (consumer may ignore)
    }

    logger.info("Scraped product '%s' price=%s previous=%s", product.get("title"), current_price, previous_price)
    return product


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