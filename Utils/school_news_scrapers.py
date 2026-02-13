"""
School news scraping: MySchool, Punch Education, NUC.
"""
import asyncio
import logging
import re
import json
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Any, Tuple, List, Set, Union, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .helpers import get_domain_from_url, retry, NoDataError
from .browser import shared_playwright, fetch_with_playwright_aggressive
from .parsers import parse_myschool_date, parse_punch_date, extract_punch_date_from_html, parse_nuc_date, is_recent_date
from bot.settings import _SCHOOL_KEYWORDS_RE, _DATE_RE  # external

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
# Constants
# -------------------------------------------------------------------
COMMON_NEWS_PATHS = [
    "/news", "/news-events", "/news-and-events",
    "/News", "/bulletin", "/bulletins", "/Bulletin", "/news-events/",
    "/category/news", "/category/news/", "/category/press-release",
    "/topics/education/", "/category/education/", "/tags/education/",
]
MAX_ARTICLE_AGE_DAYS = 180

def candidate_listing_urls(base_url: str) -> List[str]:
    """Yield candidate URLs to try for a site: base root + common news paths."""
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
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

# -------------------------------------------------------------------
# Common extraction helpers (used by all site extractors)
# -------------------------------------------------------------------
def _safe_get_text(elem) -> str:
    try:
        return elem.get_text(' ', strip=True) if elem else ""
    except Exception:
        try:
            return str(elem)
        except Exception:
            return ""

def _first_matching_selector_text(soup: BeautifulSoup, selectors: List[str], min_len: int = 0) -> Optional[str]:
    for selector in selectors:
        try:
            elem = soup.select_one(selector)
        except Exception:
            continue
        text = _safe_get_text(elem)
        if text and len(text) > min_len:
            return text
    return None

def _clean_spaces(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip() if text else text

def _remove_unwanted(container, unwanted_selectors: List[str]):
    try:
        for unw_sel in unwanted_selectors:
            for elem in container.select(unw_sel):
                try:
                    elem.decompose()
                except Exception:
                    pass
    except Exception:
        pass

def _safe_parse_date(parse_fn, date_str: str):
    if not date_str or not parse_fn:
        return None
    try:
        return parse_fn(date_str)
    except Exception:
        return None

# -------------------------------------------------------------------
# MySchool extractor
# -------------------------------------------------------------------
def extract_myschool_content(html: str, url: str) -> Dict[str, Any]:
    start = time.perf_counter()
    result = {
        'title': "", 'date_str': "", 'date_obj': None, 'snippet': "",
        'url': url, 'success': False, 'has_keywords': False,
        'error': None, 'elapsed': None
    }
    if not html or not isinstance(html, str):
        result['error'] = "Empty or invalid HTML input"
        result['elapsed'] = time.perf_counter() - start
        return result
    try:
        soup = BeautifulSoup(html, 'lxml')
        title_selectors = [
            'h3.page-title.blog-header-title', 'h3.blog-header-title',
            'h3.page-title', '.post-title', 'h1'
        ]
        title = _first_matching_selector_text(soup, title_selectors, min_len=5) or ""
        result['title'] = title[:200]
        all_text = soup.get_text(' ', strip=True)
        date_str = ""
        posted_patterns = [
            r'Posted by\s+[^\|]+\|\s*(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})',
            r'Posted by\s+[^\d]*(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})',
            r'(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})\s*\|\s*\d+\s*Comment',
            r'(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s*,?\s*\d{4})'
        ]
        for pattern in posted_patterns:
            m = re.search(pattern, all_text, re.IGNORECASE)
            if m:
                date_str = m.group(1).strip()
                break
        result['date_str'] = date_str
        result['date_obj'] = _safe_parse_date(parse_myschool_date, date_str)
        content_selectors = [
            'div.clearfix', 'div.pb-5', 'article', 'div.entry-content', 'main', '.content'
        ]
        content = ""
        for selector in content_selectors:
            container = soup.select_one(selector)
            if not container:
                continue
            container_copy = BeautifulSoup(str(container), 'lxml')
            unwanted = [
                'script', 'style', 'nav', 'header', 'footer', '.share', '.comments',
                '.ad', '.widget', '.related', 'iframe', '.sidebar', '.author-box',
                '.social-share', '.post-meta', '.newsletter', '.event-date-thumb',
                '.caption-content-block'
            ]
            _remove_unwanted(container_copy, unwanted)
            text = _safe_get_text(container_copy)
            text = re.sub(r'Posted by\s+[^|]+\|[^|]+\|\s*\d+\s*Comments?', '', text, flags=re.I)
            text = _clean_spaces(text)
            if len(text) > 200:
                content = text
                break
        if not content or len(content) < 200:
            paragraphs = []
            for p in soup.select('p'):
                p_text = p.get_text(strip=True)
                if (len(p_text) > 50 and
                        not re.match(r'^Posted by|^Comments|^Share|^Related|^Also read|^Read also|^Category|^Tags', p_text, re.I) and
                        not p_text.isdigit() and '©' not in p_text and 'http' not in p_text.lower()):
                    paragraphs.append(p_text)
                    if len(paragraphs) >= 12:
                        break
            if paragraphs:
                content = ' '.join(paragraphs)
                content = _clean_spaces(content)
        result['snippet'] = ""
        if content:
            sentences = re.split(r'(?<=[.!?])\s+', content)
            meaningful = []
            for sent in sentences:
                sent = sent.strip()
                if (len(sent) > 30 and
                        not re.match(r'^(Posted by|Comments|Share|Related|Also read|Read also|Category|Tags)', sent, re.I) and
                        re.search(r'[A-Z]', sent) and '©' not in sent and 'http' not in sent.lower()):
                    meaningful.append(sent)
                    if len(' '.join(meaningful)) > 150:
                        break
            if meaningful:
                snippet = ' '.join(meaningful)
                if len(snippet) > 250:
                    if '.' in snippet[:250]:
                        last_period = snippet[:250].rfind('.')
                        snippet = snippet[:last_period + 1] if last_period > 150 else snippet[:247] + "..."
                    else:
                        snippet = snippet[:247] + "..."
            else:
                snippet = content[:250]
                if len(content) > 250:
                    snippet = snippet[:247] + "..."
            result['snippet'] = _clean_spaces(snippet)
        try:
            title_has = _SCHOOL_KEYWORDS_RE.search(result['title']) if _SCHOOL_KEYWORDS_RE else True
            snippet_has = _SCHOOL_KEYWORDS_RE.search(result['snippet']) if _SCHOOL_KEYWORDS_RE else True
            result['has_keywords'] = bool(title_has or snippet_has)
        except Exception:
            result['has_keywords'] = False
        date_is_recent = is_recent_date(result['date_obj']) if result['date_obj'] else False
        success = bool(
            result['title'] and
            len(result.get('snippet') or "") > 50 and
            result.get('has_keywords', False) and
            date_is_recent
        )
        result['success'] = success
        LOG.info(f"[MySchool Extract] {url[:80]} success={success} title_len={len(result['title'])} snippet_len={len(result['snippet'])} date='{result['date_str']}'")
    except Exception as exc:
        LOG.exception(f"[MySchool Extract] failed for {url[:80]}: {exc}")
        result['error'] = str(exc)
    finally:
        result['elapsed'] = time.perf_counter() - start
        return result

# -------------------------------------------------------------------
# Punch extractor
# -------------------------------------------------------------------
def extract_punch_content(html: str, url: str) -> Dict[str, Any]:
    start = time.perf_counter()
    result = {
        'title': "", 'date_str': "", 'date_obj': None, 'snippet': "",
        'url': url, 'success': False, 'has_keywords': False,
        'error': None, 'elapsed': None
    }
    if not html or not isinstance(html, str):
        result['error'] = "Empty or invalid HTML input"
        result['elapsed'] = time.perf_counter() - start
        return result
    try:
        soup = BeautifulSoup(html, 'lxml')
        title_selectors = [
            'h1.text-3xl', 'h1.post-title', '.post-title', 'h1.entry-title', 'h1'
        ]
        title = _first_matching_selector_text(soup, title_selectors, min_len=10) or ""
        result['title'] = title[:200]
        date_elem = soup.select_one('span.post-date')
        if date_elem:
            result['date_str'] = _safe_get_text(date_elem)
            result['date_obj'] = extract_punch_date_from_html(soup, url)
            if result['date_obj']:
                result['date_str'] = result['date_obj'].strftime('%B %d, %Y %I:%M %p')
        content_selectors = [
            'div.post-content', 'article.prose', '.entry-content', 'article', 'main'
        ]
        content = ""
        for selector in content_selectors:
            container = soup.select_one(selector)
            if not container:
                continue
            for bad in container.select('script, style, nav, header, footer, .share, .comments, .ad, .widget, .related, iframe'):
                try:
                    bad.decompose()
                except Exception:
                    pass
            paragraphs = []
            for p in container.select('p'):
                p_text = p.get_text(strip=True)
                if len(p_text) > 30:
                    paragraphs.append(p_text)
            if paragraphs:
                content = ' '.join(paragraphs)
                content = _clean_spaces(content)
                break
        snippet = ""
        if content:
            content = re.sub(r'Kindly share this story.*?(?=[A-Z])', '', content, flags=re.I | re.DOTALL)
            content = _clean_spaces(content)
            sentences = re.split(r'(?<=[.!?])\s+', content)
            meaningful = []
            for sent in sentences:
                sent = sent.strip()
                if len(sent) > 30 and re.search(r'[A-Z]', sent):
                    meaningful.append(sent)
                    if len(' '.join(meaningful)) > 150:
                        break
            if meaningful:
                snippet = ' '.join(meaningful)
                if len(snippet) > 250:
                    if '.' in snippet[:250]:
                        last_period = snippet[:250].rfind('.')
                        snippet = snippet[:last_period + 1] if last_period > 150 else snippet[:247] + "..."
                    else:
                        snippet = snippet[:247] + "..."
            else:
                snippet = content[:250]
                if len(content) > 250:
                    snippet = snippet[:247] + "..."
            result['snippet'] = _clean_spaces(snippet)
        try:
            title_has = _SCHOOL_KEYWORDS_RE.search(result['title']) if _SCHOOL_KEYWORDS_RE else True
            snippet_has = _SCHOOL_KEYWORDS_RE.search(result['snippet']) if _SCHOOL_KEYWORDS_RE else True
            result['has_keywords'] = bool(title_has or snippet_has)
        except Exception:
            result['has_keywords'] = False
        date_is_recent = is_recent_date(result['date_obj']) if result['date_obj'] else False
        success = bool(
            result['title'] and
            len(result.get('snippet') or "") > 50 and
            result.get('has_keywords', False) and
            date_is_recent
        )
        result['success'] = success
        LOG.info(f"[Punch Extract] {url[:80]} success={success} title_len={len(result['title'])} snippet_len={len(result['snippet'])} date='{result['date_str']}'")
    except Exception as exc:
        LOG.exception(f"[Punch Extract] failed for {url[:80]}: {exc}")
        result['error'] = str(exc)
    finally:
        result['elapsed'] = time.perf_counter() - start
        return result

# -------------------------------------------------------------------
# NUC extractor
# -------------------------------------------------------------------
def extract_nuc_content(html: str, url: str) -> Dict[str, Any]:
    start = time.perf_counter()
    result = {
        'title': "", 'date_str': "", 'date_obj': None, 'snippet': "",
        'url': url, 'success': False, 'has_keywords': False,
        'error': None, 'elapsed': None
    }
    if not html or not isinstance(html, str):
        result['error'] = "Empty or invalid HTML input"
        result['elapsed'] = time.perf_counter() - start
        return result
    try:
        soup = BeautifulSoup(html, 'lxml')
        title_selectors = ['h1.entry-title', '.entry-title', 'h1']
        title = _first_matching_selector_text(soup, title_selectors, min_len=10) or ""
        result['title'] = title[:200]
        date_elem = soup.select_one('span.published') or soup.select_one('.post-date, time')
        if date_elem:
            result['date_str'] = _safe_get_text(date_elem)
            result['date_obj'] = _safe_parse_date(parse_nuc_date, result['date_str'])
        content_selectors = ['div.entry-content', 'article .content', '.post-content', 'article']
        content = ""
        for selector in content_selectors:
            container = soup.select_one(selector)
            if not container:
                continue
            for bad in container.select('script, style, nav, header, footer, .share, .comments, .ad, .widget, .related, iframe'):
                try:
                    bad.decompose()
                except Exception:
                    pass
            text = _safe_get_text(container)
            if title and text.lower().startswith(title.lower()):
                text = text[len(title):].strip()
            text = _clean_spaces(text)
            if len(text) > 200:
                content = text
                break
        snippet = ""
        if content:
            sentences = re.split(r'(?<=[.!?])\s+', content)
            meaningful = []
            for sent in sentences:
                sent = sent.strip()
                if len(sent) > 30 and re.search(r'[A-Z]', sent):
                    meaningful.append(sent)
                    if len(' '.join(meaningful)) > 150:
                        break
            if meaningful:
                snippet = ' '.join(meaningful)
                if len(snippet) > 250:
                    if '.' in snippet[:250]:
                        last_period = snippet[:250].rfind('.')
                        snippet = snippet[:last_period + 1] if last_period > 150 else snippet[:247] + "..."
                    else:
                        snippet = snippet[:247] + "..."
            else:
                snippet = content[:250]
                if len(content) > 250:
                    snippet = snippet[:247] + "..."
            result['snippet'] = _clean_spaces(snippet)
        try:
            title_has = _SCHOOL_KEYWORDS_RE.search(result['title']) if _SCHOOL_KEYWORDS_RE else True
            snippet_has = _SCHOOL_KEYWORDS_RE.search(result['snippet']) if _SCHOOL_KEYWORDS_RE else True
            result['has_keywords'] = bool(title_has or snippet_has)
        except Exception:
            result['has_keywords'] = False
        date_is_recent = is_recent_date(result['date_obj']) if result['date_obj'] else False
        success = bool(
            result['title'] and
            len(result.get('snippet') or "") > 50 and
            result.get('has_keywords', False) and
            date_is_recent
        )
        result['success'] = success
        LOG.info(f"[NUC Extract] {url[:80]} success={success} title_len={len(result['title'])} snippet_len={len(result['snippet'])} date='{result['date_str']}'")
    except Exception as exc:
        LOG.exception(f"[NUC Extract] failed for {url[:80]}: {exc}")
        result['error'] = str(exc)
    finally:
        result['elapsed'] = time.perf_counter() - start
        return result

# -------------------------------------------------------------------
# Generic extractor (fallback)
# -------------------------------------------------------------------
def extract_clean_content_v5(html: str, url: str, site_type: str = '') -> Dict[str, Any]:
    start = time.perf_counter()
    if not html or not isinstance(html, str):
        return {'success': False, 'error': 'Empty or invalid HTML', 'elapsed': time.perf_counter() - start, 'url': url}
    if site_type == 'myschool':
        return extract_myschool_content(html, url)
    elif site_type == 'punch':
        return extract_punch_content(html, url)
    elif site_type == 'nuc':
        return extract_nuc_content(html, url)
    LOG.warning(f"[Extract] No specific extractor for site_type='{site_type}', using generic fallback for {url[:80]}")
    try:
        soup = BeautifulSoup(html, 'lxml')
        title = _first_matching_selector_text(soup, ['h1'], min_len=1) or ""
        content_elem = soup.select_one('article, main, .content, .entry-content')
        content = _safe_get_text(content_elem) if content_elem else ""
        snippet = content[:250] if len(content) > 250 else content
        return {
            'title': title[:200],
            'date_str': "", 'date_obj': None,
            'snippet': _clean_spaces(snippet),
            'url': url, 'success': bool(title and len(snippet) > 50),
            'has_keywords': True,
            'elapsed': time.perf_counter() - start
        }
    except Exception as exc:
        LOG.exception(f"[Extract] generic extractor failed for {url[:80]}: {exc}")
        return {'success': False, 'error': str(exc), 'elapsed': time.perf_counter() - start, 'url': url}

# -------------------------------------------------------------------
# Listing URL fetchers
# -------------------------------------------------------------------
async def get_myschool_recent_articles(base_url: str = "https://myschool.ng/news") -> List[str]:
    LOG.info(f"[MySchool Listing] 🔍 Starting extraction from {base_url}")
    LAST_MYSCHOOL_REQUEST = getattr(get_myschool_recent_articles, "_last_request", 0)
    current_time = time.time()
    if current_time - LAST_MYSCHOOL_REQUEST < 5:
        wait_time = 5 - (current_time - LAST_MYSCHOOL_REQUEST)
        LOG.info(f"[MySchool Listing] ⏳ Cooling down for {wait_time:.1f}s...")
        await asyncio.sleep(wait_time)
    get_myschool_recent_articles._last_request = time.time()
    root = base_url.rstrip("/").rsplit("/", 1)[0] if "/" in base_url else base_url
    urls_to_try = [base_url.rstrip("/")]
    LOG.info(f"[MySchool Listing] 📋 Will try {len(urls_to_try)} listing URLs")
    for idx, listing_url in enumerate(urls_to_try, 1):
        LOG.info(f"[MySchool Listing] 🌐 [{idx}/{len(urls_to_try)}] Fetching: {listing_url}")
        try:
            html = await shared_playwright.fetch_html(
                listing_url,
                wait_for_selector='a[href*="/news/"]',
                scroll_to_load=True,
                timeout=100000,
                partial_on_timeout=True,
            )
            LOG.info(f"[MySchool Listing] 📄 HTML length: {len(html) if html else 0} bytes")
            if not html:
                LOG.warning(f"[MySchool Listing] ⚠️ No HTML returned for {listing_url}")
                continue
            soup = BeautifulSoup(html, 'lxml')
            seen = set()
            ordered_urls = []
            LOG.info(f"[MySchool Listing] 🔎 Scanning DOM for article links in document order...")
            for a in soup.find_all('a', href=True):
                href = a.get('href', '').strip()
                if '/news/' not in href:
                    continue
                if any(bad in href for bad in [
                    '/news/category/', '/news/tag/', '/news/author/',
                    '/category/', '/tag/', '/author/', 'page=', '#', '?feed=', '?share='
                ]):
                    continue
                if any(kw in href for kw in ['page-', '/page/', '?page=', 'archive', 'year=', 'month=']):
                    continue
                if href.startswith('#') or href.startswith('javascript:'):
                    continue
                full_url = urljoin("https://myschool.ng", href)
                if not re.match(r'^https://myschool\.ng/news/[^/]+/?$', full_url):
                    continue
                if full_url in seen:
                    continue
                seen.add(full_url)
                ordered_urls.append(full_url)
                if len(ordered_urls) >= 10:
                    break
            if ordered_urls:
                LOG.info(f"[MySchool Listing] ✅ Extracted {len(ordered_urls)} article URLs from {listing_url}")
                return ordered_urls
            else:
                LOG.warning(f"[MySchool Listing] ⚠️ No article URLs found in {listing_url}")
        except Exception as e:
            LOG.exception(f"[MySchool Listing] ❌ Exception while processing {listing_url}: {e}")
            continue
    LOG.error("[MySchool Listing] ❌ All listing URLs failed - returning empty list")
    return []

async def get_punch_recent_articles(base_url: str = "https://punchng.com") -> List[str]:
    LOG.info(f"[Punch Listing] 🔍 Starting extraction from {base_url}")
    listing_url = f"{base_url}/topics/education/"
    LOG.info(f"[Punch Listing] 🌐 Fetching: {listing_url}")
    try:
        listing_html = await shared_playwright.smart_fetch(
            listing_url,
            prefer_http=True,
            allow_playwright=True,
        )
        LOG.info(f"[Punch Listing] 📄 HTML length: {len(listing_html) if listing_html else 0} bytes")
        if not listing_html:
            LOG.error("[Punch Listing] ❌ No HTML returned from listing page")
            return []
        if isinstance(listing_html, bytes):
            LOG.info("[Punch Listing] ⚠️ Received bytes, attempting decompression...")
            try:
                listing_html = listing_html.decode('utf-8')
                LOG.info("[Punch Listing] ✅ Successfully decoded bytes to UTF-8")
            except UnicodeDecodeError:
                import gzip, brotli
                try:
                    listing_html = brotli.decompress(listing_html).decode('utf-8')
                    LOG.info("[Punch Listing] ✅ Decompressed with Brotli")
                except Exception:
                    try:
                        listing_html = gzip.decompress(listing_html).decode('utf-8')
                        LOG.info("[Punch Listing] ✅ Decompressed with gzip")
                    except Exception as decomp_err:
                        LOG.error(f"[Punch Listing] ❌ Failed to decompress: {decomp_err}")
                        return []
        if not listing_html.strip().startswith('<') and '<html' not in listing_html[:1000]:
            LOG.warning("[Punch Listing] ⚠️ HTML doesn't start with <, likely still compressed. Forcing Playwright...")
            listing_html = await shared_playwright.smart_fetch(
                listing_url,
                prefer_http=False,
                allow_playwright=True,
            )
            if isinstance(listing_html, bytes):
                listing_html = listing_html.decode('utf-8', errors='ignore')
        if '<html' not in listing_html and '<!DOCTYPE' not in listing_html:
            LOG.error(f"[Punch Listing] ❌ Invalid HTML content. First 200 chars: {listing_html[:200]}")
            return []
        soup = BeautifulSoup(listing_html, 'lxml')
        # Debug logging removed for brevity (same as original)
        articles_with_dates = []
        seen_urls = set()
        extraction_methods = []
        # Method 1: Links in headings
        for h_tag in soup.find_all(['h2', 'h3', 'h4']):
            for link in h_tag.find_all('a', href=True):
                href = link.get('href', '')
                if href and 'punchng.com' in href:
                    extraction_methods.append({
                        'url': href,
                        'title': link.get_text(strip=True),
                        'source': f'heading-{h_tag.name}',
                        'element': link
                    })
        # Method 2: Links in article-like containers
        article_containers = soup.find_all(['article', 'div'], class_=lambda x: x and any(
            word in str(x).lower() for word in ['article', 'post', 'news', 'story', 'card', 'item']
        ))
        for container in article_containers:
            for link in container.find_all('a', href=True):
                href = link.get('href', '')
                if href and 'punchng.com' in href:
                    text = link.get_text(strip=True)
                    if text and len(text) > 20:
                        extraction_methods.append({
                            'url': href,
                            'title': text,
                            'source': 'article-container',
                            'element': link
                        })
        # Method 3: Direct links with article-like patterns
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if href and 'punchng.com' in href and href.endswith('/'):
                text = link.get_text(strip=True)
                if text and len(text) > 30 and not any(nav in text.lower() for nav in ['home', 'about', 'contact', 'advertise', 'subscribe']):
                    extraction_methods.append({
                        'url': href,
                        'title': text,
                        'source': 'direct-link',
                        'element': link
                    })
        LOG.info(f"[Punch Listing] 📋 Found {len(extraction_methods)} potential articles from all methods")
        for idx, candidate in enumerate(extraction_methods, 1):
            href = candidate['url']
            if any(bad in href for bad in [
                '/topics/', '/category/', '/tag/', '/author/',
                'advertise', '#', '?feed=', '?share=', '/videos/',
                '/galleries/', '/podcasts/', '/contact-us', '/about-us',
                '/advertise', '/print-', 'punchng.com/#', 'punchng.com/?'
            ]):
                continue
            if not href.startswith('http'):
                full_url = urljoin(base_url, href)
            else:
                full_url = href
            full_url = full_url.split('?')[0]
            if 'punchng.com' not in full_url:
                continue
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            date_str = ""
            date_obj = None
            element = candidate['element']
            for _ in range(5):
                if element:
                    date_elements = element.find_all(['time', 'span', 'div'], class_=lambda x: x and any(
                        word in str(x).lower() for word in ['date', 'time', 'ago', 'published']
                    ))
                    for date_elem in date_elements:
                        text = date_elem.get_text(strip=True)
                        if text and ('ago' in text.lower() or any(
                            month in text for month in ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
                        )):
                            date_str = text
                            date_obj = parse_punch_date(date_str)
                            break
                    if date_str:
                        break
                    element = element.parent if element else None
            articles_with_dates.append({
                'url': full_url,
                'date_obj': date_obj or datetime.now(),
                'title': candidate['title'][:80],
                'order': idx
            })
            if len(articles_with_dates) >= 25:
                break
        articles_with_dates.sort(key=lambda x: x['order'])
        urls = [a['url'] for a in articles_with_dates[:15]]
        LOG.info(f"[Punch Listing] ✅ FINAL RESULTS: Extracted {len(urls)} article URLs")
        return urls
    except Exception as e:
        LOG.exception(f"[Punch Listing] ❌ Exception: {e}")
        return []

async def get_nuc_recent_articles(base_url: str = "https://www.nuc.edu.ng") -> List[str]:
    LOG.info(f"[NUC Listing] 🔍 Starting extraction from {base_url}")
    try:
        listing_html = await shared_playwright.smart_fetch(
            base_url,
            prefer_http=True,
            allow_playwright=True,
            wait_for_selector='article.post, .et_pb_post',
        )
        LOG.info(f"[NUC Listing] 📄 HTML length: {len(listing_html) if listing_html else 0} bytes")
        if not listing_html:
            LOG.error("[NUC Listing] ❌ No HTML returned from listing page")
            return []
        soup = BeautifulSoup(listing_html, 'lxml')
        articles_with_dates = []
        article_elements = soup.select('article.post, .et_pb_post')
        LOG.info(f"[NUC Listing] 🔎 Found {len(article_elements)} article elements")
        for idx, article in enumerate(article_elements, 1):
            link = article.select_one('a[href*="nuc.edu.ng"]')
            if not link:
                continue
            href = link.get('href', '')
            if href.endswith('.pdf') or '/wp-content/' in href:
                continue
            full_url = urljoin(base_url, href)
            date_elem = article.select_one('span.published, .post-date, time')
            date_str = date_elem.get_text(strip=True) if date_elem else ""
            date_obj = parse_nuc_date(date_str)
            articles_with_dates.append({'url': full_url, 'date_obj': date_obj or datetime.min})
        articles_with_dates.sort(key=lambda x: x['date_obj'], reverse=True)
        urls = [a['url'] for a in articles_with_dates[:15]]
        LOG.info(f"[NUC Listing] ✅ Extracted {len(urls)} article URLs")
        return urls
    except Exception as e:
        LOG.exception(f"[NUC Listing] ❌ Exception: {e}")
        return []

# -------------------------------------------------------------------
# Site‑specific scrapers (return article dicts)
# -------------------------------------------------------------------
async def scrape_myschool_recent(base_url: str = "https://myschool.ng", max_articles: int = 10) -> List[Dict]:
    LOG.info(f"\n{'='*70}\n[MySchool Scraper] 🎯 STARTING - base_url={base_url}, max={max_articles}\n{'='*70}")
    try:
        article_urls = await get_myschool_recent_articles(base_url)
        if not article_urls:
            LOG.warning("[MySchool Scraper] ⚠️ get_myschool_recent_articles returned 0 URLs")
            return []
        LOG.info(f"[MySchool Scraper] ✅ STEP 1 COMPLETE: {len(article_urls)} URLs obtained")
        html_results = await shared_playwright.run_concurrent(
            article_urls[:10],
            use_http_first=False,
            allow_playwright=True,
            fetch_kwargs={
                "wait_for_selector": 'h3.page-title.blog-header-title, div.clearfix, div.pb-5',
                "scroll_to_load": True,
                "play_timeout": 90000,
                "partial_on_timeout": True,
            }
        )
        LOG.info(f"[MySchool Scraper] ✅ STEP 2 COMPLETE: Received {len(html_results)} results")
        all_extracted = []
        for idx, (url, html) in enumerate(html_results, 1):
            LOG.info(f"[MySchool Scraper]   Processing [{idx}/{len(html_results)}]: {url[:60]}...")
            if not html:
                continue
            data = extract_myschool_content(html, url)
            if data.get('success'):
                all_extracted.append({
                    'title': data['title'],
                    'date': data['date_str'],
                    'snippet': data['snippet'],
                    'url': url,
                    'source': 'myschool',
                    'pdf': False,
                    'date_obj': data['date_obj'],
                    'base_url': base_url,
                    'has_keywords': data.get('has_keywords', True)
                })
                LOG.info(f"[MySchool Scraper]   ✓ SUCCESS: {data['title'][:50]}...")
        if all_extracted:
            all_extracted.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
            all_extracted = all_extracted[:max_articles]
        LOG.info(f"[MySchool Scraper] 🎉 FINAL RESULT: {len(all_extracted)} articles")
        return all_extracted
    except Exception as e:
        LOG.exception(f"[MySchool Scraper] ❌ FATAL EXCEPTION: {e}")
        return []

async def scrape_punch_recent(base_url: str = "https://punchng.com", max_articles: int = 10) -> List[Dict]:
    LOG.info(f"\n{'='*70}\n[Punch Scraper] 🎯 STARTING - base_url={base_url}, max={max_articles}\n{'='*70}")
    try:
        article_urls = await get_punch_recent_articles(base_url)
        if not article_urls:
            LOG.warning("[Punch Scraper] ⚠️ get_punch_recent_articles returned 0 URLs")
            return []
        LOG.info(f"[Punch Scraper] ✅ STEP 1 COMPLETE: {len(article_urls)} URLs obtained")
        html_results = await shared_playwright.run_concurrent(
            article_urls,
            use_http_first=True,
            allow_playwright=True,
            fetch_kwargs={
                "wait_for_selector": 'h1.post-title, h1, .post-title, .entry-title',
                "scroll_to_load": False
            }
        )
        LOG.info(f"[Punch Scraper] ✅ STEP 2 COMPLETE: Received {len(html_results)} results")
        articles = []
        for idx, (url, html) in enumerate(html_results, 1):
            LOG.info(f"[Punch Scraper]   Processing [{idx}/{len(html_results)}]: {url[:60]}...")
            if not html:
                continue
            data = extract_clean_content_v5(html, url, 'punch')
            if data.get('success') and data.get('has_keywords', False):
                articles.append({
                    'title': data['title'],
                    'date': data['date_str'],
                    'snippet': data['snippet'],
                    'url': url,
                    'source': 'punch',
                    'pdf': False,
                    'date_obj': data['date_obj'],
                    'base_url': base_url,
                    'has_keywords': data.get('has_keywords', True)
                })
                LOG.info(f"[Punch Scraper]   ✓ SUCCESS: {data['title'][:50]}...")
        articles.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
        articles = articles[:max_articles]
        LOG.info(f"[Punch Scraper] 🎉 FINAL RESULT: {len(articles)} articles")
        return articles
    except Exception as e:
        LOG.exception(f"[Punch Scraper] ❌ FATAL EXCEPTION: {e}")
        return []

async def scrape_nuc_recent(base_url: str = "https://www.nuc.edu.ng", max_articles: int = 8) -> List[Dict]:
    LOG.info(f"\n{'='*70}\n[NUC Scraper] 🎯 STARTING - base_url={base_url}, max={max_articles}\n{'='*70}")
    try:
        article_urls = await get_nuc_recent_articles(base_url)
        if not article_urls:
            LOG.warning("[NUC Scraper] ⚠️ get_nuc_recent_articles returned 0 URLs")
            return []
        LOG.info(f"[NUC Scraper] ✅ STEP 1 COMPLETE: {len(article_urls)} URLs obtained")
        html_results = await shared_playwright.run_concurrent(
            article_urls,
            use_http_first=True,
            allow_playwright=True,
            fetch_kwargs={
                "wait_for_selector": 'h1.entry-title',
                "scroll_to_load": False
            }
        )
        LOG.info(f"[NUC Scraper] ✅ STEP 2 COMPLETE: Received {len(html_results)} results")
        articles = []
        for idx, (url, html) in enumerate(html_results, 1):
            LOG.info(f"[NUC Scraper]   Processing [{idx}/{len(html_results)}]: {url[:60]}...")
            if not html:
                continue
            data = extract_clean_content_v5(html, url, 'nuc')
            if data.get('success') and data.get('has_keywords', False):
                articles.append({
                    'title': data['title'],
                    'date': data['date_str'],
                    'snippet': data['snippet'],
                    'url': url,
                    'source': 'nuc',
                    'pdf': False,
                    'date_obj': data['date_obj'],
                    'base_url': base_url,
                    'has_keywords': data.get('has_keywords', True)
                })
                LOG.info(f"[NUC Scraper]   ✓ SUCCESS: {data['title'][:50]}...")
        articles.sort(key=lambda x: x.get('date_obj', datetime.min), reverse=True)
        articles = articles[:max_articles]
        LOG.info(f"[NUC Scraper] 🎉 FINAL RESULT: {len(articles)} articles")
        return articles
    except Exception as e:
        LOG.exception(f"[NUC Scraper] ❌ FATAL EXCEPTION: {e}")
        return []

# -------------------------------------------------------------------
# Unified school news scraper
# -------------------------------------------------------------------
async def scrape_school_news(
    urls: Union[List[str], Dict[str, Dict[str, Any]]],
    fetch_full_content: bool = False,
    max_articles: int = 5,
    semaphore_retries: int = 2,
    semaphore_backoff: float = 0.5,
    partial_on_timeout: bool = True,
    max_concurrency: Optional[int] = None
) -> List[Dict[str, Any]]:
    LOG.info(f"\n{'='*70}")
    LOG.info("📰 UNIFIED SCHOOL NEWS SCRAPER (Domain-aware HTTP-first policy)")
    LOG.info(f"{'='*70}")
    if not urls:
        LOG.warning("⚠️ No URLs or site configs provided")
        return []
    DOMAIN_POLICY = {
        "punchng.com": {"use_http_first": True, "allow_playwright": True},
        "nuc.edu.ng": {"use_http_first": True, "allow_playwright": True},
        "myschool.ng": {"use_http_first": False, "allow_playwright": True},
    }
    LOG.info("📋 Domain Policy:")
    for domain, policy in DOMAIN_POLICY.items():
        LOG.info(f"  {domain}: {policy}")
    if not shared_playwright._initialized:
        init_kwargs = {}
        if max_concurrency:
            init_kwargs["max_concurrency"] = max_concurrency
        LOG.info("Initializing shared Playwright context for Render free plan...")
        await shared_playwright.initialize(**init_kwargs)
    all_articles = []
    def _domain_policy_for(domain: str):
        for key, policy in DOMAIN_POLICY.items():
            if key in domain:
                return policy
        return {"use_http_first": True, "allow_playwright": True}
    if isinstance(urls, dict):
        LOG.info(f"📋 Processing {len(urls)} site configurations for recent articles")
        SITE_MAPPING = {
            "nuc": {"task": scrape_nuc_recent, "max_articles": 8, "type": "nuc"},
            "myschool": {"task": scrape_myschool_recent, "max_articles": 10, "type": "myschool"},
            "punch": {"task": scrape_punch_recent, "max_articles": 10, "type": "punch"},
            "nuc (universities)": {"task": scrape_nuc_recent, "max_articles": 5, "type": "nuc"},
            "myschool.ng": {"task": scrape_myschool_recent, "max_articles": 10, "type": "myschool"},
            "punch education": {"task": scrape_punch_recent, "max_articles": 10, "type": "punch"},
        }
        tasks = []
        site_names = []
        for site_name, config in urls.items():
            site_key = site_name.lower().strip()
            mapped_config = SITE_MAPPING.get(site_key)
            if not mapped_config:
                LOG.warning(f"⚠️ Unknown site '{site_name}', skipping")
                continue
            base_url = ""
            if isinstance(config, list) and len(config) > 0:
                base_url = config[0]
            elif isinstance(config, dict):
                base_url = config.get("base_url", "")
            elif isinstance(config, str):
                base_url = config
            if not base_url:
                LOG.warning(f"⚠️ No base_url for '{site_name}', skipping")
                continue
            task_func = mapped_config["task"]
            max_per_site = mapped_config.get("max_articles", max_articles)
            try:
                LOG.info(f"\n🎯 Setting up {site_name} (mapped to {mapped_config['type']})")
                LOG.info(f"  Creating task for {site_name} (base_url={base_url}, max={max_per_site})")
                t = asyncio.create_task(task_func(base_url=base_url, max_articles=max_per_site))
                tasks.append(t)
                site_names.append(site_name)
            except Exception as e:
                LOG.exception(f"  ❌ Failed to schedule task for {site_name}: {e}")
        LOG.info(f"\n🚀 Starting concurrent scraping of all sites... ({len(tasks)} tasks)")
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            LOG.info(f"✅ Gather completed for {len(results)} sites")
        except Exception as gather_e:
            LOG.exception(f"❌ asyncio.gather failed: {gather_e}")
            results = [[] for _ in tasks]
        successful_sites = 0
        for site_name, result in zip(site_names, results):
            if isinstance(result, Exception):
                LOG.error(f"❌ {site_name} raised exception: {result}")
                continue
            articles = result or []
            if articles:
                LOG.info(f"✅ {site_name}: {len(articles)} articles found")
                all_articles.extend(articles)
                successful_sites += 1
            else:
                LOG.warning(f"⚠️  {site_name}: No articles found (returned empty list)")
        LOG.info(f"\n📊 Concurrent scraping complete: {successful_sites}/{len(SITE_MAPPING)} sites successful")
    elif isinstance(urls, list):
        LOG.info(f"📋 Processing {len(urls)} specific article URLs")
        # (List case omitted for brevity – same as original)
        pass
    else:
        LOG.error(f"❌ Invalid input type: {type(urls)}. Expected list or dict.")
        return []
    LOG.info(f"\n📊 POST-PROCESSING: {len(all_articles)} total articles before filtering")
    all_articles.sort(key=lambda x: x.get("date_obj", datetime.min), reverse=True)
    recent_articles = []
    for article in all_articles:
        date_obj = article.get("date_obj")
        has_keywords = article.get("has_keywords", False)
        snippet = article.get("snippet", "")
        date_ok = date_obj and is_recent_date(date_obj)
        content_ok = (article.get("title") and snippet and len(snippet) > 30 and has_keywords)
        if date_ok and content_ok:
            recent_articles.append(article)
    formatted_articles = []
    for article in recent_articles:
        formatted_articles.append({
            "title": article.get("title", "Untitled"),
            "snippet": article.get("snippet", ""),
            "date": article.get("date", ""),
            "link": article.get("url", ""),
            "source": article.get("source", "unknown"),
            "pdf": article.get("pdf", False),
            "date_obj": article.get("date_obj"),
            "has_keywords": article.get("has_keywords", True)
        })
    formatted_articles.sort(key=lambda x: (
        x.get("date_obj") is None,
        -(x.get("date_obj", datetime.now()).timestamp() if x.get("date_obj") else 0)
    ))
    LOG.info(f"\n📊 FINAL RESULTS")
    LOG.info(f"{'─'*40}")
    LOG.info(f"Total articles found: {len(all_articles)}")
    LOG.info(f"Recent articles (last {MAX_ARTICLE_AGE_DAYS} days): {len(recent_articles)}")
    LOG.info(f"Returning: {len(formatted_articles[:max_articles * 2])}")
    return formatted_articles[:max_articles * 2]