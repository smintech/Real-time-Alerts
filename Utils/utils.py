# Utils/utils.py - Fully updated scraper (Apify completely removed, cloudscraper-based, bulletproof for JS-heavy sites)
from telegram.error import TelegramError  # For safe_send
from telegram import Bot  # Type hint for safe_send
import os
import time
import logging
import json
import re
import asyncio
import requests
from typing import Dict, Optional, Any, Tuple, Callable
from functools import wraps
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import cloudscraper
from requests.exceptions import RequestException
from typing import Dict, Optional, Any, Tuple, Callable, List
# Settings / thresholds
from bot.settings import (
    SUPPORTED_SITES,
    MIN_CHANGE_TO_ALERT,
    HIGH_DEAL_THRESHOLD,
    MEDIUM_DEAL_THRESHOLD,
    LOW_DEAL_THRESHOLD,
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


# ---------------------------
# Retry decorator (robust for network/cloudflare issues)
# ---------------------------
def retry(max_attempts: int = 4, backoff: float = 2.0):
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < max_attempts:
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        raise
                    sleep_for = backoff * (2 ** (attempt - 1))
                    LOG.warning(f"{fn.__name__} failed (attempt {attempt}/{max_attempts}): {e}. Retrying in {sleep_for:.1f}s...")
                    time.sleep(sleep_for)
            return None
        return wrapper
    return decorator


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
@retry(max_attempts=4, backoff=2.0)
def _fetch_html(url: str) -> str:
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True,
        },
        delay=10,
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }
    response = scraper.get(url, headers=headers, timeout=30)
    if response.status_code != 200:
        raise ScrapeError(f"HTTP {response.status_code} for {url}")
    return response.text
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

def _parse_fuelpricewatch(html: str) -> Dict[str, Any]:
    """
    Robust parser for FuelPriceWatch SPA + index page + hidden JSON/XHR endpoints.
    Returns a dict:
    {
      "source": "FuelPriceWatch",
      "price_raw": float|None,
      "price_str": str|None,
      "change_today": str|None,
      "last_updated": str|None,
      "raw": { ... debug info ... }
    }
    """
    soup = BeautifulSoup(html, "lxml")

    # quick block detection
    block = _detect_block(soup)
    if block:
        return {
            "source": "FuelPriceWatch",
            "error": block,
            "price_raw": None,
            "price_str": None,
            "last_updated": None,
            "change_today": None,
            "raw": None
        }

    price_candidates: List[float] = []
    price_sources: List[str] = []
    change_today = None
    last_updated = None
    debug = {"tried_index": False, "next_data_found": False, "xhr_tried": [], "regex_candidates": [], "dom_candidates": []}

    # ---------- 1) Try server-rendered index page (fast fallback) ----------
    try:
        debug["tried_index"] = True
        idx_html = None
        try:
            idx_html = _fetch_html("https://www.fuelpricewatch.com/fuel-price-index-nigeria")
        except Exception:
            # Some deployments may use www vs app; try alternate path
            try:
                idx_html = _fetch_html("https://www.fuelpricewatch.com/")
            except Exception:
                idx_html = None

        if idx_html:
            idx_text = BeautifulSoup(idx_html, "lxml").get_text(" ", strip=True)
            # attempt robust patterns for "Petrol (PMS)" or "PMS"
            m = re.search(r"Petrol\s*\(PMS\).{0,120}?(?:₦|NGN|N)?\s*([\d,]+\.?\d*)", idx_text, flags=re.I)
            if not m:
                m = re.search(r"(?:Average\s*)?(?:PMS|Petrol)[^\d]{0,30}(?:₦|NGN|N)?\s*([\d,]+\.?\d*)", idx_text, flags=re.I)
            if m:
                v = _parse_price_string(m.group(1))
                if v:
                    return {
                        "source": "FuelPriceWatch (index page)",
                        "price_raw": v,
                        "price_str": _format_naira(v),
                        "change_today": None,
                        "last_updated": None,
                        "raw": {"from": "index_page", "pattern": m.group(0)[:200]}
                    }
    except Exception:
        # non-fatal: continue to SPA logic
        LOG.debug("Index page attempt failed", exc_info=True)

    # ---------- Helpers ----------
    def find_numbers_in_json(obj, path=""):
        """Recursively search JSON for numeric values near petrol/pms/price keywords."""
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                new_path = f"{path}/{k}"
                # If key contains a hint, try parsing directly
                if any(tok in kl for tok in ("petrol", "pms", "pmsprice", "petrolprice", "price", "average", "pms_price")):
                    if isinstance(v, (int, float)):
                        found.append((new_path, float(v)))
                    elif isinstance(v, str):
                        p = _parse_price_string(v)
                        if p:
                            found.append((new_path, p))
                # If value is string and contains hint words, scan numbers inside it
                if isinstance(v, str) and re.search(r"(petrol|pms|price|average)", v, flags=re.I):
                    for m in re.findall(r"[\d,]{2,}\.?\d*", v):
                        p = _parse_price_string(m)
                        if p:
                            found.append((new_path, p))
                # Recurse
                found.extend(find_numbers_in_json(v, new_path))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                found.extend(find_numbers_in_json(item, f"{path}[{i}]"))
        return found

    # ---------- 2) Try __NEXT_DATA__ hydration JSON ----------
    try:
        next_data_el = soup.find("script", id="__NEXT_DATA__")
        if next_data_el and next_data_el.string:
            debug["next_data_found"] = True
            try:
                nd = json.loads(next_data_el.string)
                found = find_numbers_in_json(nd)
                for pth, val in found:
                    # prefer plausible fuel-range candidates or scaled numbers
                    try:
                        v = float(val)
                    except Exception:
                        continue
                    if 10 <= v <= 5000:
                        price_candidates.append(v)
                        price_sources.append(f"__NEXT_DATA__:{pth}")
                    else:
                        # try scale/100 if the value looks like cents or integer without decimal
                        if 5000 < v <= 500000 and 10 <= (v / 100) <= 5000:
                            price_candidates.append(round(v / 100, 2))
                            price_sources.append(f"__NEXT_DATA__:{pth}:scaled/100")
            except Exception:
                LOG.debug("Failed to parse __NEXT_DATA__ JSON", exc_info=True)
    except Exception:
        pass

    # ---------- 3) Search page for obvious XHR/JSON endpoints and call them ----------
    # Look for absolute or relative URLs that look like /api, /prices, /data, /v1
    try:
        scripts_text = " ".join([s.string or "" for s in soup.find_all("script") if s.string])
        # regex for absolute endpoints
        for m in re.findall(r"https?://[^\s'\"<>]+(?:/api|/prices|/data|/v1|/v2)[^\s'\"<>]*", scripts_text, flags=re.I):
            if m in debug["xhr_tried"]:
                continue
            debug["xhr_tried"].append(m)
            try:
                # prefer JSON endpoints
                hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/json"}
                resp = requests.get(m, headers=hdrs, timeout=8)
                if resp.status_code == 200:
                    try:
                        j = resp.json()
                        found = find_numbers_in_json(j)
                        for pth, val in found:
                            try:
                                v = float(val)
                            except Exception:
                                continue
                            if 10 <= v <= 5000:
                                price_candidates.append(v)
                                price_sources.append(f"xhr_json:{m}:{pth}")
                            elif 5000 < v <= 500000 and 10 <= (v / 100) <= 5000:
                                price_candidates.append(round(v / 100, 2))
                                price_sources.append(f"xhr_json:{m}:{pth}:scaled/100")
                    except ValueError:
                        # not JSON, try parse for a naira-like token
                        txt = resp.text
                        mm = re.search(r"(?:₦|NGN|N)\s*([\d,]+\.?\d*)", txt)
                        if mm:
                            p = _parse_price_string(mm.group(1))
                            if p:
                                price_candidates.append(p)
                                price_sources.append(f"xhr_text:{m}")
            except Exception:
                LOG.debug("XHR attempt failed for %s", m, exc_info=True)
        # also look for relative paths like "/api/..." and try to build absolute from the main host
        rels = set(re.findall(r"(?:(?:/api|/prices|/data|/v1|/v2)[^\s'\"<>]*)", scripts_text, flags=re.I))
        base_url = None
        try:
            base_url = f"https://{_get_domain_from_url(soup.base["href"]) if soup.base and soup.base.get('href') else ''}"
        except Exception:
            base_url = None
        if not base_url:
            # derive base from known host used to fetch page if available in html
            # fallback: try fuelpricewatch.com root
            base_url = "https://app.fuelpricewatch.com"
        for rel in rels:
            full = rel if rel.startswith("http") else urljoin(base_url, rel)
            if full in debug["xhr_tried"]:
                continue
            debug["xhr_tried"].append(full)
            try:
                hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/json"}
                resp = requests.get(full, headers=hdrs, timeout=8)
                if resp.status_code == 200:
                    try:
                        j = resp.json()
                        found = find_numbers_in_json(j)
                        for pth, val in found:
                            try:
                                v = float(val)
                            except Exception:
                                continue
                            if 10 <= v <= 5000:
                                price_candidates.append(v)
                                price_sources.append(f"xhr_json:{full}:{pth}")
                            elif 5000 < v <= 500000 and 10 <= (v / 100) <= 5000:
                                price_candidates.append(round(v / 100, 2))
                                price_sources.append(f"xhr_json:{full}:{pth}:scaled/100")
                    except ValueError:
                        mm = re.search(r"(?:₦|NGN|N)\s*([\d,]+\.?\d*)", resp.text)
                        if mm:
                            p = _parse_price_string(mm.group(1))
                            if p:
                                price_candidates.append(p)
                                price_sources.append(f"xhr_text:{full}")
            except Exception:
                LOG.debug("XHR attempt failed for %s", full, exc_info=True)
    except Exception:
        LOG.debug("XHR detection/attempt failed", exc_info=True)

    # ---------- 4) DOM/text regex strategies on the provided HTML (fallback) ----------
    page_text = soup.get_text(" ", strip=True)

    regex_patterns = [
        r"(?:Average\s*(?:PMS|Petrol|Petrol\s*\(PMS\)|PMS Price|Petrol Price)[\s:\-–]*)?(?:₦|NGN|N)?\s*([\d,]+\.?\d*)",
        r"(?:PMS|Petrol|Average PMS|Average Petrol)[^\d]{0,30}((?:₦|NGN|N)?\s*[\d,]+\.?\d*)",
        r"(?:₦|NGN|N)\s*([\d,]{2,}\.?\d*)",
        r"([\d]{3,5}\.\d{0,2})"
    ]
    for pat in regex_patterns:
        for m in re.findall(pat, page_text, flags=re.I):
            p = _parse_price_string(m)
            if p:
                price_candidates.append(p)
                price_sources.append(f"regex:{pat}")
                debug["regex_candidates"].append((pat, m))

    # DOM-specific common selectors (attempt)
    dom_selectors = [
        "div.average-price", ".average-price", ".avg-petrol", "#avg-petrol",
        ".pms-price", ".petrol-price", ".price .value", ".price-value", ".price--value"
    ]
    for sel in dom_selectors:
        try:
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(" ", strip=True)
                p = _parse_price_string(txt)
                if p:
                    price_candidates.append(p)
                    price_sources.append(f"dom:{sel}")
                    debug["dom_candidates"].append((sel, txt[:120]))
        except Exception:
            continue

    # ---------- 5) Extract change_today and last_updated if present ----------
    try:
        ch = re.search(r"([+-]?\s*(?:₦|NGN|N)?\s*[\d,]+\.?\d*)\s*(?:today|since\s+yesterday)", page_text, flags=re.I)
        if ch:
            change_today = ch.group(1).strip()
    except Exception:
        pass

    try:
        date_cand = re.search(r"(?:Last Updated|as at|updated)\s*[:\-–]?\s*([0-3]?\d(?:\s+\w+)?(?:\s+\d{4})?(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)?)", page_text, flags=re.I)
        if date_cand:
            last_updated = date_cand.group(1).strip()
    except Exception:
        pass

    # ---------- 6) Sanitize & deduplicate price candidates ----------
    sanitized: List[float] = []
    for c in price_candidates:
        try:
            c = float(c)
        except Exception:
            continue
        # direct reasonable range
        if 5 <= c <= 5000:
            sanitized.append(round(c, 2))
            continue
        # if extremely large, try dividing by 100 (common concatenation)
        if 5000 < c <= 500000 and 5 <= (c / 100) <= 5000:
            sanitized.append(round(c / 100, 2))
            continue
        # attempt split for concatenated token
        try:
            split_try = _split_concatenated_numeric_token(str(int(c)))
            if split_try:
                sanitized.append(round(min(split_try), 2))
        except Exception:
            continue

    # remove duplicates but keep order
    seen = set()
    final_candidates = []
    for v in sanitized:
        if v not in seen:
            seen.add(v)
            final_candidates.append(v)

    chosen = None
    chosen_source = None
    if final_candidates:
        # choose most frequent value (in original sanitized list) else median
        freq = {}
        for v in sanitized:
            freq[v] = freq.get(v, 0) + 1
        # pick value with highest freq; tie-breaker: closest to mean
        mean_val = sum(sanitized) / len(sanitized) if sanitized else None
        best = max(freq.items(), key=lambda kv: (kv[1], -abs(kv[0] - (mean_val or kv[0]))))[0]
        chosen = float(best)
        # find a source for chosen (first occurrence)
        for i, v in enumerate(price_candidates):
            try:
                if round(float(v), 2) == round(chosen, 2):
                    chosen_source = price_sources[i] if i < len(price_sources) else None
                    break
            except Exception:
                continue

    # final sanity attempt: if no candidate found, try simple near-keyword extraction (last resort)
    if not chosen:
        try:
            m = re.search(r"(?:Petrol|PMS)[^\d]{0,120}(?:₦|NGN|N)?\s*([\d,]+\.?\d*)", page_text, flags=re.I)
            if m:
                v = _parse_price_string(m.group(1))
                if v and 5 <= v <= 5000:
                    chosen = v
                    chosen_source = "fallback:near_keyword_regex"
        except Exception:
            pass

    # final formatting / debug return
    if not chosen:
        LOG.info("Fuel parser couldn't find petrol price; debug=%s", debug)
        return {
            "source": "FuelPriceWatch",
            "error": "no_price_found",
            "price_raw": None,
            "price_str": None,
            "last_updated": last_updated,
            "change_today": change_today or "N/A",
            "raw": {
                "debug": debug,
                "candidates": price_candidates,
                "sanitized": final_candidates,
                "regex_samples": debug.get("regex_candidates", [])[:6],
                "dom_samples": debug.get("dom_candidates", [])[:6],
            }
        }

    # minor final correction: if chosen looks like integer massively > expected, scale down
    if chosen > 5000 and 5 <= (chosen / 100) <= 5000:
        chosen = round(chosen / 100, 2)

    return {
        "source": "FuelPriceWatch",
        "price_raw": chosen,
        "price_str": _format_naira(chosen),
        "change_today": change_today or "N/A",
        "last_updated": last_updated,
        "raw": {
            "chosen_source": chosen_source,
            "candidates": price_candidates,
            "sanitized": final_candidates,
            "xhr_attempts": debug.get("xhr_tried", [])[:8],
            "snippet_len": len(html)
        }
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

async def scrape_fuel_prices(site_sources: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Async wrapper that fetches multiple fuel price sources using the sync _fetch_html
    function inside an executor. Adds validation/salvage logic but does NOT use Playwright.
    Returns a dict similar to before plus improved debug info.
    """
    from datetime import datetime

    loop = asyncio.get_event_loop()
    sources_cfg = site_sources or FUEL_SITE_SOURCES

    async def _fetch_and_parse(source_cfg: Dict[str, Any]) -> Dict[str, Any]:
        url = source_cfg["url"]
        parser = source_cfg["parser"]
        try:
            html = await loop.run_in_executor(None, _fetch_html, url)
        except Exception as e:
            LOG.exception("Fuel site fetch failed for %s", url)
            return {"url": url, "error": f"fetch_error: {e}", "price_raw": None, "price_str": None, "source": getattr(parser, "__name__", "unknown"), "raw": None}

        try:
            parsed = parser(html)
            parsed["url"] = url
            parsed["raw"] = parsed.get("raw") or {"snippet_len": len(html)}
            return parsed
        except Exception as e:
            LOG.exception("Fuel site parse failed for %s", url)
            return {"url": url, "error": f"parse_error: {e}", "price_raw": None, "price_str": None, "source": getattr(parser, "__name__", "unknown"), "raw": None}

    tasks = [_fetch_and_parse(cfg) for cfg in sources_cfg]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    nums: List[float] = []
    change_today = "N/A"
    last_updated = "N/A"

    # collect raw values & debug
    debug = {"per_source": [], "salvage_attempts": []}

    for r in results:
        pr = r.get("price_raw")
        debug["per_source"].append({
            "url": r.get("url"),
            "source": r.get("source") or r.get("url"),
            "price_raw": pr,
            "price_str": r.get("price_str"),
            "error": r.get("error"),
            "raw": r.get("raw")
        })
        if pr is not None:
            try:
                nums.append(float(pr))
            except Exception:
                pass
        # Prefer change from the first successful source
        if change_today == "N/A" and r.get("change_today"):
            change_today = r["change_today"]
        if last_updated == "N/A" and r.get("last_updated"):
            last_updated = r["last_updated"]

    avg_raw = sum(nums) / len(nums) if nums else None

    # ---------- VALIDATION & SALVAGE ----------
    # Tune these bounds to your country's typical fuel range
    MIN_PLAUSIBLE = 500.0   # conservative lower bound (₦)
    MAX_PLAUSIBLE = 1000.0  # realistic upper bound (₦)

    def _is_plausible(v: Optional[float]) -> bool:
        try:
            return v is not None and MIN_PLAUSIBLE <= float(v) <= MAX_PLAUSIBLE
        except Exception:
            return False

    # If avg_raw is missing or outside plausible bounds, attempt salvage
    if not _is_plausible(avg_raw):
        LOG.warning("avg_raw=%s outside plausible range [%s,%s] — attempting salvage", avg_raw, MIN_PLAUSIBLE, MAX_PLAUSIBLE)
        debug["salvage_attempts"].append(f"initial_avg:{avg_raw}")

        # 1) Use only per-source candidates that are plausible
        plausible_candidates = []
        for r in results:
            try:
                pr = r.get("price_raw")
                if pr is None:
                    continue
                prf = float(pr)
                if _is_plausible(prf):
                    plausible_candidates.append(prf)
            except Exception:
                continue

        if plausible_candidates:
            avg_raw = sum(plausible_candidates) / len(plausible_candidates)
            debug["salvage_attempts"].append({"method": "use_plausible_sources", "candidates": plausible_candidates, "new_avg": avg_raw})
            LOG.info("Salvaged avg using plausible per-source values: %s", avg_raw)
        else:
            # 2) Try scaled-down candidates (common concatenation: 88394 -> 883.94 or integer tokens)
            scaled_candidates = []
            for r in results:
                pr = r.get("price_raw")
                if pr is None:
                    continue
                try:
                    prf = float(pr)
                except Exception:
                    continue
                # if candidate is huge, try dividing by 100
                if prf > MAX_PLAUSIBLE and 10 <= (prf / 100) <= MAX_PLAUSIBLE:
                    scaled_candidates.append(round(prf / 100, 2))
                # also attempt split concatenation token if pr looks integer-like and long
                elif prf >= 10000:
                    split_try = _split_concatenated_numeric_token(str(int(prf)))
                    if split_try:
                        scaled_candidates.append(min(split_try))
            if scaled_candidates:
                avg_raw = sum(scaled_candidates) / len(scaled_candidates)
                debug["salvage_attempts"].append({"method": "scaled_candidates", "candidates": scaled_candidates, "new_avg": avg_raw})
                LOG.info("Salvaged avg using scaled/split candidates: %s", avg_raw)
            else:
                # 3) No further automated salvage available (Playwright intentionally removed)
                debug["salvage_attempts"].append({"method": "none", "reason": "no plausible or scaled candidates"})
                LOG.warning("Could not salvage avg_raw from candidates; leaving avg_raw as %s", avg_raw)

    # final formatting
    avg_formatted = _format_naira(avg_raw) if avg_raw else "N/A"

    return {
        "avg_petrol": avg_formatted,
        "change_today": change_today,
        "last_updated": last_updated or "Live data",
        "avg_raw": avg_raw,
        "sources": results,
        "debug": debug,
        "timestamp": datetime.now().isoformat()
    }