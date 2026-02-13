"""
Generic helper functions, decorators, and custom exceptions.
"""
import asyncio
import logging
import time
from functools import wraps
from typing import Dict, Optional, Any, Tuple, List, Union
from urllib.parse import urlparse

from telegram import Bot
from telegram.error import TelegramError

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
# Custom exceptions
# -------------------------------------------------------------------
class ScrapeError(Exception):
    """Generic scraping failure."""
    pass

class NoDataError(ScrapeError):
    """Raised when page loaded but no usable product data found."""
    pass

# -------------------------------------------------------------------
# Retry decorator (sync/async)
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# URL helpers
# -------------------------------------------------------------------
def get_domain_from_url(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return "unknown"

# -------------------------------------------------------------------
# Product / text normalisation
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# Telegram safe sender
# -------------------------------------------------------------------
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