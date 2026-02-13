"""
All parsing utilities: dates, prices, product IDs, DOM price extraction.
"""
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any, Set, Counter
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Tag

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
# Date parsing (MySchool, Punch, NUC, recency)
# -------------------------------------------------------------------
MAX_ARTICLE_AGE_DAYS = 180   # used by is_recent_date

def parse_myschool_date(date_str: str) -> Optional[datetime]:
    """Parse MySchool date string with better pattern matching."""
    if not date_str:
        return None
    date_str = date_str.replace('|', '').replace('Comments', '').strip()
    date_str = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)
    date_formats = [
        '%d %B, %Y', '%d %b, %Y', '%B %d, %Y', '%b %d, %Y',
        '%d/%m/%Y', '%Y-%m-%d', '%d %B %Y', '%d %b %Y',
    ]
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None

def parse_punch_date(date_str: str) -> Optional[datetime]:
    """Parse Punch.ng date strings into datetime objects."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    date_str = re.sub(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*', '', date_str, flags=re.I)
    date_formats = [
        '%B %d, %Y %I:%M %p', '%b %d, %Y %I:%M %p',
        '%B %d, %Y %I:%M%p', '%b %d, %Y %I:%M%p',
        '%B %d, %Y %H:%M', '%b %d, %Y %H:%M',
        '%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y',
        '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S.%f%z',
        '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S',
        '%d %B, %Y', '%d %b, %Y', '%d/%m/%Y', '%Y-%m-%d',
    ]
    for fmt in date_formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            LOG.debug(f"[parse_punch_date] Parsed '{date_str}' using format '{fmt}'")
            return dt
        except ValueError:
            continue
    if 'T' in date_str and date_str.endswith('Z'):
        try:
            iso_str = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(iso_str)
            return dt
        except Exception:
            pass
    if 'T' in date_str and ('+' in date_str or date_str.count('-') > 2):
        try:
            dt = datetime.fromisoformat(date_str)
            return dt
        except Exception:
            pass
    LOG.warning(f"[parse_punch_date] Failed to parse date: '{date_str}'")
    return None

def extract_punch_date_from_html(soup: BeautifulSoup, url: str = "") -> Optional[datetime]:
    """Extract date from Punch article HTML using multiple strategies."""
    LOG.debug(f"[extract_punch_date] Strategy 1: JSON-LD for {url[:60]}")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            items = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            for item in items:
                if item.get("@type") == "Article":
                    date_str = item.get("datePublished") or item.get("dateModified")
                    if date_str:
                        dt = parse_punch_date(date_str)
                        if dt:
                            LOG.info(f"[extract_punch_date] ✅ Found via JSON-LD: {dt}")
                            return dt
        except Exception:
            continue
    LOG.debug("[extract_punch_date] Strategy 2: Meta tags")
    meta_selectors = [
        'meta[property="article:published_time"]', 'meta[property="og:published_time"]',
        'meta[name="article:published_time"]', 'meta[name="pubdate"]',
    ]
    for selector in meta_selectors:
        elem = soup.select_one(selector)
        if elem:
            date_str = elem.get('content', '')
            dt = parse_punch_date(date_str)
            if dt:
                LOG.info(f"[extract_punch_date] ✅ Found via meta tag: {dt}")
                return dt
    LOG.debug("[extract_punch_date] Strategy 3: HTML elements")
    date_selectors = [
        'span.text-gray-500', 'span.post-date', 'time',
        'div.flex.items-center span', '.entry-date', '.published'
    ]
    for selector in date_selectors:
        elem = soup.select_one(selector)
        if elem:
            date_str = elem.get('datetime', '') or elem.get_text(strip=True)
            dt = parse_punch_date(date_str)
            if dt:
                LOG.info(f"[extract_punch_date] ✅ Found via {selector}: {dt}")
                return dt
    LOG.debug("[extract_punch_date] Strategy 4: Text patterns")
    page_text = soup.get_text()
    pattern1 = r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)'
    match = re.search(pattern1, page_text, re.IGNORECASE)
    if match:
        dt = parse_punch_date(match.group(1))
        if dt:
            LOG.info(f"[extract_punch_date] ✅ Found via text pattern: {dt}")
            return dt
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
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    if date_obj.tzinfo is None:
        date_obj = date_obj.replace(tzinfo=timezone.utc)
    return date_obj >= cutoff_date

# -------------------------------------------------------------------
# Price parsing utilities (shared by e‑commerce scrapers)
# -------------------------------------------------------------------
def _parse_price_string(s: str) -> Optional[float]:
    """Parse a price string without product ID filtering."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace('\xa0', ' ').replace('NGN', '').replace('N', '').strip()
    cleaned = re.sub(r'[₦,]', '', s)
    mul = 1
    if cleaned.lower().endswith('k'):
        mul = 1000
        cleaned = cleaned[:-1]
    elif cleaned.lower().endswith('m'):
        mul = 1_000_000
        cleaned = cleaned[:-1]
    try:
        val = float(cleaned) * mul
        if 5_000 <= val <= 5_000_000:
            return val
        return None
    except:
        return None

def split_concatenated_price(text: str, min_price: float = 50000) -> Tuple[Optional[float], Optional[float]]:
    """Split concatenated prices like ‘18500002100000’ -> (old, current)."""
    digits = re.sub(r'[^\d]', '', str(text))
    if len(digits) < 12 or len(digits) > 16:
        return None, None
    mid = len(digits) // 2
    best_split = None
    min_diff = float('inf')
    for offset in range(-3, 4):
        split_point = mid + offset
        if split_point < 6 or split_point > len(digits) - 6:
            continue
        try:
            left = float(digits[:split_point])
            right = float(digits[split_point:])
            if not (min_price <= left <= 10000000 and min_price <= right <= 10000000):
                continue
            larger = max(left, right)
            smaller = min(left, right)
            if larger == 0:
                continue
            ratio = smaller / larger
            if 0.6 <= ratio <= 1.0:
                diff = larger - smaller
                if diff < min_diff:
                    min_diff = diff
                    best_split = (larger, smaller)
        except:
            continue
    return best_split if best_split else (None, None)

def _gather_price_candidates_from_dom(soup: BeautifulSoup, domain_hint: str = "") -> List[float]:
    """Enhanced DOM price gathering with noise removal and concatenated price splitting."""
    exclusion_selectors = [
        'script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'noscript',
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
    selectors = [
        "span.prc", "span.price", "div.price", "div.prc", ".product-price",
        ".price", ".prc", "span[class*='price']", "div[class*='price']",
        "span[class*='prc']", "span[class*='old-price']", ".price-was",
        "[data-testid*='price']", "._3e_22_199e7", "._44738_3988u",
        "[data-price]", "[data-price-amount]"
    ]
    found = []
    seen_texts = set()
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
            for token in re.findall(r"[\d.,]{10,}", txt):
                old, current = split_concatenated_price(token.replace(',', ''), min_price=1000)
                if old and old >= 1000:
                    found.append(old)
                if current and current >= 1000:
                    found.append(current)
    # Container-based search
    for container in soup.select("div, section, li"):
        price_children = []
        for child in container.find_all(True, recursive=False):
            txt = child.get_text(" ", strip=True)
            if not txt:
                continue
            if '₦' in txt or 'NGN' in txt or re.search(r"[\d\.,]{6,}", txt):
                p = _parse_price_string(txt)
                if p and 1000 <= p < 100_000_000:
                    price_children.append(p)
                elif p and p >= 100_000_000:
                    old, current = split_concatenated_price(str(int(p)), min_price=1000)
                    if old and old >= 1000:
                        price_children.append(old)
                    if current and current >= 1000:
                        price_children.append(current)
        if price_children:
            found.extend(price_children)
    # Dedup and filter
    unique = []
    seen_vals = set()
    for p in found:
        if p not in seen_vals and 1000 <= p < 100_000_000:
            seen_vals.add(p)
            unique.append(p)
    return unique

def _extract_product_id_from_url(url: str) -> Optional[int]:
    """Extract product ID from Konga URL for filtering purposes."""
    if not url:
        return None
    patterns = [
        r'-(\d{6,8})(?:\?|$)',
        r'/product/.*?(\d{6,8})$',
        r'p=(\d{6,8})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            try:
                return int(match.group(1))
            except:
                continue
    return None

def _parse_price_string_filtered(text: str, filter_product_id: Optional[int] = None) -> Optional[float]:
    """Parse price and optionally filter out product ID numbers."""
    if not text:
        return None
    text = text.strip()
    if '₦' in text:
        parts = text.split('₦')
        if len(parts) > 1:
            first_price_text = parts[1]
            if '₦' in first_price_text:
                first_price_text = first_price_text.split('₦')[0]
            text = first_price_text.strip()
    numbers = re.findall(r'[\d,]+', text)
    for num in numbers:
        try:
            clean = num.replace(',', '')
            val = float(clean)
            if filter_product_id and abs(val - filter_product_id) < 100:
                LOG.debug(f"Filtered out product ID as price: ₦{val:,} (product ID: {filter_product_id})")
                continue
            if 5000 <= val <= 5_000_000:
                LOG.debug(f"Valid price: ₦{val:,}")
                return val
        except:
            continue
    return None

def _extract_konga_current_price(soup: BeautifulSoup, page_text: str, url: str = "") -> Optional[float]:
    """Konga price extraction – trusts only div[class*="price"] selectors and filters product IDs."""
    LOG.info("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    LOG.info("┃ KONGA PRICE EXTRACTION (PRODUCT ID FILTERED)                 ┃")
    LOG.info("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    product_id = _extract_product_id_from_url(url)
    if product_id:
        LOG.info(f"🔍 Product ID detected: {product_id} (will filter this out)")
    # Strategy 1: trusted selectors
    trusted_selectors = [
        'div[class*="price"]', 'span[class*="price"]',
        '.priceBox_priceBoxPrice__i7paS', 'div.priceBox_priceBox__CeNMs',
        'div[class*="priceBox"]', 'span.shared_price__gnso_',
        'span[data-testid="current-price"]', 'div[data-testid="price-current"]',
        'span.-b.-ubpt', 'span.prc', '[data-price]',
    ]
    for selector in trusted_selectors:
        elements = soup.select(selector)
        if elements:
            LOG.info(f"Found {len(elements)} elements with selector: {selector[:40]}")
            for el in elements[:3]:
                text = el.get_text(" ", strip=True)
                if text:
                    price = _parse_price_string_filtered(text, product_id)
                    if price:
                        LOG.info(f"✅ Found via trusted selector '{selector[:30]}': ₦{price:,}")
                        return price
    # Strategy 2: visible text in trusted containers
    main_containers = soup.select('div.productDetail_productDetailsContent__VV9__, main, article, [role="main"]')
    for container in main_containers:
        container_text = container.get_text(" ", strip=True)
        price_pattern = r'₦\s*([\d,]+(?:\.\d{2})?)'
        matches = re.findall(price_pattern, container_text[:3000])
        if matches:
            LOG.info(f"Found {len(matches)} price patterns in container")
            for match in matches:
                price = _parse_price_string_filtered(f"₦{match}", product_id)
                if price:
                    LOG.info(f"✅ Found in trusted container: ₦{price:,}")
                    return price
    # Strategy 3: all page prices with filtering
    all_text = str(soup)
    all_prices = re.findall(r'₦\s*([\d,]+)', all_text)
    valid_prices = []
    for price_str in all_prices[:100]:
        try:
            price = float(price_str.replace(',', ''))
            if product_id and abs(price - product_id) < 100:
                LOG.debug(f"    ✗ Filtered (product ID): ₦{price:,}")
                continue
            if 5000 <= price <= 5_000_000:
                valid_prices.append(price)
                LOG.debug(f"    ✓ Accepted: ₦{price:,}")
        except:
            continue
    if valid_prices:
        price_counts = Counter(valid_prices)
        most_common_price, count = price_counts.most_common(1)[0]
        LOG.info(f"✅ Selected most common price: ₦{most_common_price:,} (appeared {count} times)")
        return most_common_price
    LOG.error("❌ ALL STRATEGIES FAILED - No valid price found")
    return None

def _extract_previous_price(soup: BeautifulSoup, json_ld: Optional[dict], domain: str,
                           page_text: str, current_price: Optional[float]) -> Optional[float]:
    """Extract previous (original) price using various signals."""
    candidates = []  # (price, score)
    def add_candidate(p: float, score: int = 0):
        if p is None or p <= 0:
            return
        if p < 5000 or p > 100_000_000:
            return
        if current_price and current_price > 0 and p <= current_price * 1.05:
            return
        candidates.append((p, score))
    # JSON‑LD
    try:
        if isinstance(json_ld, dict):
            offers = json_ld.get("offers")
            if offers:
                offers_list = offers if isinstance(offers, list) else [offers]
                for offer in offers_list:
                    for key in ("priceBeforeDiscount", "listPrice", "originalPrice", "highPrice", "wasPrice"):
                        v = offer.get(key)
                        if isinstance(v, (int, float)):
                            add_candidate(float(v), 200)
                        elif isinstance(v, str):
                            p = _parse_price_string(v)
                            if p:
                                add_candidate(p, 200)
                    ps = offer.get("priceSpecification")
                    if isinstance(ps, dict):
                        for spec_key in ("price", "value", "originalPrice"):
                            v = ps.get(spec_key)
                            p = _parse_price_string(str(v))
                            if p:
                                add_candidate(p, 180)
    except Exception:
        pass
    # Explicit markup
    for tag in ("del", "s", "strike", "span[class*='old']", "span[class*='was']", "div[class*='old-price']"):
        for el in soup.select(tag):
            txt = el.get_text(" ", strip=True)
            p = _parse_price_string(txt)
            if p:
                add_candidate(p, 150)
            else:
                for token in re.findall(r"[\d.,]{6,}", txt):
                    old, current = split_concatenated_price(token)
                    if old and current:
                        add_candidate(max(old, current), 140)
                    elif old or current:
                        add_candidate(old or current, 130)
    # Class‑based
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
                add_candidate(p, 120)
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[1], -x[0]))
    best_guess = candidates[0][0]
    if current_price and best_guess <= (current_price * 1.05):
        return None
    return best_guess

def extract_prices_from_visible_text(
    page_text: str,
    currency_prefixes: str = r"₦|NGN|N",
    min_price: float = 10_000.0,
    max_price: float = 20_000_000.0
) -> Tuple[Optional[float], Optional[float]]:
    """Parse visible‑only page_text for prices. Returns (current, previous)."""
    if not page_text or not isinstance(page_text, str):
        return None, None
    txt = re.sub(r"\s+", " ", page_text)
    pattern = rf"(?i)(?:{currency_prefixes})\s*[:\-]?\s*([0-9][0-9,\.]*\s*[kK]?|\d+(?:\.\d+)?\s*[kK])"
    raw_matches = re.findall(pattern, txt)
    def normalize_num(s: str) -> Optional[float]:
        if not s:
            return None
        s = s.strip().replace(" ", "")
        mul = 1.0
        if s[-1] in ("k", "K"):
            mul = 1_000.0
            s = s[:-1]
        s_clean = s.replace(",", "")
        try:
            return float(s_clean) * mul
        except:
            return None
    candidates = []
    for raw in raw_matches:
        val = normalize_num(raw)
        if val and min_price <= val <= max_price:
            candidates.append(val)
    seen = set()
    valid_prices = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            valid_prices.append(p)
    if not valid_prices:
        return None, None
    first = valid_prices[0]
    second = valid_prices[1] if len(valid_prices) > 1 else None
    if second is None:
        return first, None
    if first > second and (first - second) > 500:
        return second, first
    if abs(first - second) <= max(1.0, 0.01 * first):
        return first, None
    if second < first and (first - second) / first >= 0.05:
        return second, first
    return first, second