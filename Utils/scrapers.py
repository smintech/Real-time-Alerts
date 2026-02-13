"""
E‑commerce and general product scraping.
"""
import asyncio
import logging
import random
import re
import json
import time
from typing import Dict, Optional, Any, Tuple, List
from urllib.parse import urlparse, urljoin

import requests
import cloudscraper
from bs4 import BeautifulSoup

from .helpers import ScrapeError, NoDataError, retry, get_domain_from_url, _slugify, _best_identifier, normalize_product_key
from .parsers import (
    _parse_price_string, _gather_price_candidates_from_dom, _extract_product_id_from_url,
    _extract_konga_current_price, _extract_previous_price, extract_prices_from_visible_text
)
from .browser import fetch_with_playwright_aggressive, _BROWSER_SEMAPHORE

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
# Cloudscraper aggressive fetch (synchronous, used as fallback)
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# Core fetch function with retry
# -------------------------------------------------------------------
@retry(max_attempts=4, backoff=2.0)
async def _fetch_html(url: str, prefer_playwright_on_first_try: bool = False) -> str:
    domain = get_domain_from_url(url)
    tough_domains = ['konga', 'jumia', 'gov.ng', 'nysc', 'nuc', 'waec', 'neco', 'myschool', 'punchng', 'education', 'lpginnigeria']
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

async def fetch_html_ultimate(url: str) -> str:
    """Fetch HTML with semaphore-controlled concurrency."""
    domain = get_domain_from_url(url)
    if 'myschool.ng' in domain and '/news/' in url:
        async with _BROWSER_SEMAPHORE:
            try:
                return await fetch_with_playwright_aggressive(url)
            except Exception as e:
                LOG.warning(f"Playwright failed: {e}")
                raise
    try:
        return fetch_with_cloudscraper_aggressive(url)
    except Exception:
        async with _BROWSER_SEMAPHORE:
            return await fetch_with_playwright_aggressive(url)

# -------------------------------------------------------------------
# Binance reference scraper (special)
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# E‑commerce scraper (Konga, Jumia etc.)
# -------------------------------------------------------------------
async def scrape_ecommerce(url: str) -> Dict[str, Any]:
    domain = get_domain_from_url(url)
    # (Site support check removed for brevity – assume already checked)
    html = None
    visible_text = None
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
            selectors = ["span.-b", ".-fs24", ".prc", ".-prc", "div.prc", "span[class*='price']"]
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(" ", strip=True)
                    p = _parse_price_string(text)
                    if p:
                        product["current_price"] = p
                        LOG.info("Jumia: Found price ₦%.0f via %s", p, sel)
                        break
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
                    if product_id and abs(price - product_id) < 100:
                        continue
                    if 5000 <= price <= 5_000_000:
                        prices.append(price)
                except:
                    pass
            if prices:
                product["current_price"] = max(prices)
                LOG.info("Fallback regex found price ₦%.0f", max(prices))
    page_text = visible_text or soup.get_text(" ", strip=True)
    prev_price = _extract_previous_price(soup, json_ld_data, domain, page_text, product["current_price"])
    if prev_price:
        product["previous_price"] = prev_price
        LOG.info("Found previous price: ₦%.0f for %s", prev_price, product["title"])
    page_text_lower = soup.get_text().lower()
    if any(phrase in page_text_lower for phrase in ["out of stock", "sold out", "unavailable", "not available"]):
        product["stock_status"] = "out_of_stock"
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
    if product["title"] == "Product" or "Buy" in product["title"]:
        h1 = soup.select_one("h1.-fs20, h1.-pb10, h1.brd, .v-p-hd h1, h1, .product-title, h1.product-name")
        if h1:
            product["title"] = h1.get_text(strip=True)
        elif soup.title:
            product["title"] = soup.title.string.strip()
    if product["current_price"] is None:
        raise NoDataError(f"No price found for {url}")
    cleaned_images = []
    seen = set()
    for i in product["images"]:
        try:
            if i and isinstance(i, str) and len(i) > 8:
                u = urljoin(url, i)
                if u not in seen:
                    seen.add(u)
                    cleaned_images.append(u)
        except Exception:
            continue
    product["images"] = cleaned_images[:6]
    product["image"] = cleaned_images[0] if cleaned_images else None
    product["description"] = product["description"][:1500].strip()
    product["raw"] = {"json_ld": json_ld_data} if json_ld_data else {"snippet": product["title"]}
    LOG.info("Successfully scraped %s — ₦%.0f — %s", product["title"], product["current_price"], domain)
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

# -------------------------------------------------------------------
# Change detection & deal scoring
# -------------------------------------------------------------------
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